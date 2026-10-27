"""Against a real, seeded Odoo (docker compose up). Run with:

ODOO_URL=http://localhost:8069 pytest -m integration
"""

import os
from datetime import date

import pytest

from erp_bridge.actions import ActionService, origin_tag
from erp_bridge.audit import AuditLog
from erp_bridge.domain import Line, ManufacturingProposal, PricedLine, PurchaseOrderProposal, TransferProposal
from erp_bridge.odoo.client import OdooClient
from erp_bridge.odoo.gateway import InvalidRequest, NotFound, OdooGateway
from tests.conftest import AGENT

pytestmark = pytest.mark.integration


@pytest.fixture
async def odoo():
    client = OdooClient(
        os.environ.get("ODOO_URL", "http://localhost:8069"),
        os.environ.get("ODOO_DB", "tembusu"),
        os.environ.get("ODOO_USER", "admin"),
        os.environ.get("ODOO_PASSWORD", "admin"),
    )
    yield client
    await client.aclose()


@pytest.fixture
def erp(odoo):
    return OdooGateway(odoo, today=date(2026, 10, 27))


async def test_seeded_reads(erp):
    codes = {w.code for w in await erp.warehouses()}
    assert codes == {"FAC", "DC", "KLCC", "PJ", "PNG"}
    fryers = await erp.stock_levels(["KLCC"], ["TH-SA-6104"], None)
    assert fryers[0].on_hand == 2 and fryers[0].reorder_min == 10  # the seeded KLCC shortage
    suggestions = await erp.reorder_suggestions(["FAC"])
    assert "RM-AL-INGOT" in {s.sku for s in suggestions}
    report = await erp.sales_by_period(date(2026, 7, 1), date(2026, 9, 30), "channel", [])
    assert {r.key for r in report.rows} == {"Wholesale", "Online", "Retail Stores"}
    bom = await erp.bill_of_materials("TH-CW-2420")
    assert {b.sku for b in bom} >= {"RM-AL-INGOT", "RM-LID-GL24"}


async def test_validation_errors_are_business_readable(erp):
    with pytest.raises(NotFound, match="Unknown SKU"):
        await erp.validate_purchase_order(
            PurchaseOrderProposal(
                supplier="SUP-PAS", warehouse="FAC", lines=[PricedLine(sku="NOPE", quantity=1)], reason="testing"
            )
        )
    with pytest.raises(InvalidRequest, match="bought in"):
        await erp.validate_manufacturing_order(ManufacturingProposal(sku="TH-SA-6104", quantity=5, reason="testing"))
    with pytest.raises(InvalidRequest, match="has only"):
        await erp.validate_transfer(
            TransferProposal(
                from_warehouse="PJ", to_warehouse="KLCC", lines=[Line(sku="TH-SA-6104", quantity=500)], reason="testing"
            )
        )


async def test_approved_po_is_written_once(erp, db):
    actions = ActionService(db, erp, AuditLog(db))
    validated = await erp.validate_purchase_order(
        PurchaseOrderProposal(
            supplier="SUP-PPK",
            warehouse="FAC",
            lines=[PricedLine(sku="RM-PKG-SLV", quantity=4000)],
            reason="integration test",
        )
    )
    assert validated["payload"]["lines"][0]["unit_price"] == 0.25  # from the supplier price list
    view = await actions.propose(
        AGENT, "purchase_order", validated, "integration test", idempotency_key=os.urandom(8).hex()
    )
    done = await actions.approve(view.id, "Integration Tester")
    assert done.status == "executed", done.error
    po = await erp.purchase_order(done.odoo_ref)
    assert po.state == "purchase"
    # Re-executing finds the PO by origin tag instead of creating another
    again = await erp.execute("purchase_order", validated["payload"], origin_tag(view.id))
    assert again["ref"] == done.odoo_ref
