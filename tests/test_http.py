"""The full HTTP stack: OAuth token endpoint, MCP over Streamable HTTP with a
bearer token, the approval API and the approval page. Runs a real uvicorn
server with the fake ERP gateway."""

import re
import socket
import threading
import time

import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from erp_bridge.api import create_app
from erp_bridge.config import Settings
from tests.fakes import FakeGateway


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path_factory.mktemp('db')}/bridge.db",
        redis_url=None,
        public_url=base,
        jwt_secret="http-test-secret-http-test-secret-1234",
        callback_allowed_prefixes=[],
    )
    gateway = FakeGateway()
    app = create_app(settings, gateway=gateway)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield base, gateway
    srv.should_exit = True
    thread.join(timeout=5)


def get_token(base: str, client_id: str, scope: str | None = None) -> str:
    data = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": f"{client_id}-demo-secret"}
    if scope:
        data["scope"] = scope
    resp = httpx.post(f"{base}/oauth/token", data=data, trust_env=False)
    resp.raise_for_status()
    return resp.json()["access_token"]


def test_token_endpoint(server):
    base, _ = server
    bad = httpx.post(
        f"{base}/oauth/token",
        data={"grant_type": "client_credentials", "client_id": "ops-copilot", "client_secret": "wrong"},
        trust_env=False,
    )
    assert bad.status_code == 401
    too_much = httpx.post(
        f"{base}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "store-klcc",
            "client_secret": "store-klcc-demo-secret",
            "scope": "finance:read",
        },
        trust_env=False,
    )
    assert too_much.status_code == 400
    narrowed = httpx.post(
        f"{base}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "ops-copilot",
            "client_secret": "ops-copilot-demo-secret",
            "scope": "stock:read",
        },
        trust_env=False,
    )
    assert narrowed.json()["scope"] == "stock:read"


def test_mcp_requires_bearer(server):
    base, _ = server
    resp = httpx.post(f"{base}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, trust_env=False)
    assert resp.status_code == 401
    assert "resource_metadata" in resp.headers.get("www-authenticate", "")


async def mcp_call(base: str, token: str, tool: str, args: dict):
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, trust_env=False)
    async with Client(streamable_http_client(f"{base}/mcp", http_client=http)) as client:
        return await client.call_tool(tool, args)


async def test_end_to_end_propose_approve(server):
    base, gateway = server
    agent = get_token(base, "ops-copilot")
    result = await mcp_call(
        base,
        agent,
        "propose_purchase_order",
        {
            "supplier": "SUP-PAS",
            "warehouse": "FAC",
            "lines": [{"sku": "RM-AL-INGOT", "quantity": 5000}],
            "reason": "Ingot forecast 1,450 kg vs 2,000 kg minimum",
        },
    )
    assert not result.is_error, result.content
    action = result.structured_content
    assert action["status"] == "pending"
    writes_before = gateway.writes

    # An agent token cannot use the approval API
    denied = httpx.post(
        f"{base}/api/actions/{action['id']}/approve",
        json={},
        headers={"Authorization": f"Bearer {agent}"},
        trust_env=False,
    )
    assert denied.status_code == 403

    approver = {"Authorization": f"Bearer {get_token(base, 'approver-ops')}"}
    pending = httpx.get(f"{base}/api/actions?status=pending", headers=approver, trust_env=False).json()
    assert action["id"] in [a["id"] for a in pending]
    done = httpx.post(
        f"{base}/api/actions/{action['id']}/approve", json={"note": "Go"}, headers=approver, trust_env=False
    ).json()
    assert done["status"] == "executed"
    assert done["decided_by"] == "Aisyah Rahman (Operations Manager)"
    assert gateway.writes == writes_before + 1

    again = httpx.post(f"{base}/api/actions/{action['id']}/approve", json={}, headers=approver, trust_env=False)
    assert again.status_code == 409

    trail = httpx.get(f"{base}/api/audit?action_id={action['id']}", headers=approver, trust_env=False).json()
    assert {"decision", "execution"} <= {e["kind"] for e in trail}

    status = await mcp_call(base, agent, "get_action_status", {"action_id": action["id"]})
    assert status.structured_content["odoo_ref"] == done["odoo_ref"]


async def test_approval_page_flow(server):
    base, gateway = server
    agent = get_token(base, "ops-copilot")
    result = await mcp_call(
        base,
        agent,
        "propose_manufacturing_order",
        {"sku": "TH-CW-2401", "quantity": 300, "reason": "Deepavali wok demand"},
    )
    action_id = result.structured_content["id"]

    with httpx.Client(base_url=base, trust_env=False, follow_redirects=True) as web:
        assert "/login" in str(web.get("/approvals").url)
        bad = web.post("/login", data={"client_id": "ops-copilot", "secret": "ops-copilot-demo-secret"})
        assert "Wrong credentials" in bad.text  # agents cannot sign in to approve
        page = web.post("/login", data={"client_id": "approver-ops", "secret": "approver-ops-demo-secret"})
        assert "Manufacture 300 x TH-CW-2401" in page.text
        csrf = re.search(r'name="token" value="([^"]+)"', page.text).group(1)

        forged = web.post(f"/approvals/{action_id}/approve", data={"token": "forged"})
        assert forged.status_code == 403
        detail = web.post(f"/approvals/{action_id}/approve", data={"token": csrf, "note": "Plan approved"})
        assert "executed" in detail.text
        assert "Plan approved" in detail.text
        assert "tool_call" in web.get("/audit").text
