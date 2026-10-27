"""MCP tools, exercised through a real MCP client connected in-process."""

import pytest
from mcp import Client
from sqlalchemy import select

from erp_bridge.auth import set_principal
from erp_bridge.db import AuditEvent
from erp_bridge.ratelimit import MemoryRateLimiter
from tests.conftest import AGENT, STORE_KLCC

READ_TOOLS = {
    "list_warehouses",
    "search_products",
    "get_stock_by_warehouse",
    "get_reorder_suggestions",
    "list_transfers",
    "list_open_invoices",
    "get_customer_balance",
    "get_sales_by_period",
    "list_purchase_orders",
    "get_purchase_order",
    "list_manufacturing_orders",
    "get_bill_of_materials",
    "get_action_status",
    "list_my_actions",
}
WRITE_TOOLS = {"propose_purchase_order", "propose_stock_transfer", "propose_manufacturing_order", "propose_vendor_bill"}


@pytest.fixture
def as_agent():
    set_principal(AGENT)
    yield
    set_principal(None)


@pytest.fixture
def as_store():
    set_principal(STORE_KLCC)
    yield
    set_principal(None)


def payload(result):
    assert not result.is_error, result.content
    data = result.structured_content
    return data["result"] if isinstance(data, dict) and set(data) == {"result"} else data


async def test_tool_surface(mcp, as_agent):
    async with Client(mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    for name in READ_TOOLS:
        assert tools[name].annotations.read_only_hint is True, name
    for name in WRITE_TOOLS:
        assert tools[name].annotations.read_only_hint is False, name
    # the internal principal parameter must never leak into the schema
    assert "principal" not in tools["get_stock_by_warehouse"].input_schema["properties"]


async def test_read_stock(mcp, as_agent):
    async with Client(mcp) as client:
        rows = payload(await client.call_tool("get_stock_by_warehouse", {"sku": "TH-SA-6104"}))
    assert {r["warehouse"] for r in rows} == {"KLCC", "PJ", "DC"}


async def test_store_manager_only_sees_own_store(mcp, as_store):
    async with Client(mcp) as client:
        rows = payload(await client.call_tool("get_stock_by_warehouse", {"sku": "TH-SA-6104"}))
        assert {r["warehouse"] for r in rows} == {"KLCC"}
        denied = await client.call_tool("get_stock_by_warehouse", {"sku": "TH-SA-6104", "warehouse": "DC"})
        assert denied.is_error
        assert "may not access warehouse DC" in denied.content[0].text
        sales = payload(
            await client.call_tool("get_sales_by_period", {"date_from": "2026-10-01", "date_to": "2026-10-26"})
        )
        assert [r["key"] for r in sales["rows"]] == ["KLCC"]


async def test_missing_scope_is_denied_and_audited(mcp, as_store, db):
    async with Client(mcp) as client:
        result = await client.call_tool("list_open_invoices", {})
    assert result.is_error
    assert "lacks scope 'finance:read'" in result.content[0].text
    async with db.session() as s:
        event = (await s.scalars(select(AuditEvent).where(AuditEvent.tool == "list_open_invoices"))).one()
    assert event.outcome == "denied"
    assert event.client_id == "store-klcc"


async def test_propose_only_queues(mcp, as_agent, gateway):
    async with Client(mcp) as client:
        view = payload(
            await client.call_tool(
                "propose_purchase_order",
                {
                    "supplier": "SUP-PAS",
                    "warehouse": "FAC",
                    "lines": [{"sku": "RM-AL-INGOT", "quantity": 5000}],
                    "reason": "Ingot forecast below reorder minimum",
                },
            )
        )
        assert view["status"] == "pending"
        assert gateway.writes == 0
        status = payload(await client.call_tool("get_action_status", {"action_id": view["id"]}))
        assert status["status"] == "pending"


async def test_strict_inputs_reject_invented_fields(mcp, as_agent):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "propose_purchase_order",
            {
                "supplier": "SUP-PAS",
                "warehouse": "FAC",
                "reason": "testing extra fields",
                "lines": [{"sku": "RM-AL-INGOT", "quantity": 5, "discount": 50}],
            },
        )
    assert result.is_error


async def test_clear_error_for_unknown_supplier(mcp, as_agent):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "propose_purchase_order",
            {
                "supplier": "Acme",
                "warehouse": "FAC",
                "lines": [{"sku": "RM-AL-INGOT", "quantity": 5}],
                "reason": "unknown supplier test",
            },
        )
    assert result.is_error
    assert "No supplier matches 'Acme'" in result.content[0].text


async def test_transfer_checks_source_stock(mcp, as_agent):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "propose_stock_transfer",
            {
                "from_warehouse": "PJ",
                "to_warehouse": "KLCC",
                "lines": [{"sku": "TH-SA-6104", "quantity": 5}],
                "reason": "rebalance stores",
            },
        )
    assert result.is_error
    assert "not enough" in result.content[0].text


async def test_callback_must_be_allowlisted(mcp, as_agent):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "propose_manufacturing_order",
            {
                "sku": "TH-CW-2401",
                "quantity": 100,
                "reason": "Deepavali demand",
                "callback_url": "http://169.254.169.254/latest/meta-data",
            },
        )
    assert result.is_error
    assert "allowlist" in result.content[0].text


async def test_actions_are_private_to_requester(mcp, services, gateway):
    set_principal(AGENT)
    async with Client(mcp) as client:
        view = payload(
            await client.call_tool(
                "propose_manufacturing_order", {"sku": "TH-CW-2401", "quantity": 100, "reason": "Deepavali demand"}
            )
        )
    set_principal(STORE_KLCC)
    async with Client(mcp) as client:
        result = await client.call_tool("get_action_status", {"action_id": view["id"]})
    set_principal(None)
    assert result.is_error


async def test_rate_limit(mcp, services, as_agent):
    services.settings.rate_limit_per_minute = 3
    services.limiter = MemoryRateLimiter()
    async with Client(mcp) as client:
        for _ in range(3):
            assert not (await client.call_tool("list_warehouses", {})).is_error
        limited = await client.call_tool("list_warehouses", {})
    assert limited.is_error
    assert "Rate limit" in limited.content[0].text
