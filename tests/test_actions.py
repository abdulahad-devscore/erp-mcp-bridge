"""The reviewed write queue: nothing reaches the ERP without a decision, and
nothing reaches it twice."""

import pytest

from erp_bridge.actions import InvalidTransition, origin_tag
from erp_bridge.odoo.client import OdooError, OdooUnavailable
from tests.conftest import AGENT, INVOICE_FLOW

VALID = {
    "payload": {"supplier": "Perak Alloy", "lines": [{"sku": "RM-AL-INGOT", "quantity": 5000}]},
    "summary": "PO to Perak Alloy",
}


async def test_proposal_does_not_write(actions, gateway):
    view = await actions.propose(AGENT, "purchase_order", VALID, "Ingot below minimum")
    assert view.status == "pending"
    assert gateway.writes == 0


async def test_proposal_is_idempotent(actions):
    a = await actions.propose(AGENT, "purchase_order", VALID, "reason one")
    b = await actions.propose(AGENT, "purchase_order", VALID, "retry of the same call")
    assert a.id == b.id
    c = await actions.propose(AGENT, "purchase_order", VALID, "deliberate second order", idempotency_key="second")
    assert c.id != a.id


async def test_approve_writes_once(actions, gateway):
    view = await actions.propose(AGENT, "purchase_order", VALID, "Ingot below minimum")
    done = await actions.approve(view.id, "Aisyah Rahman", "ok")
    assert done.status == "executed"
    assert done.odoo_ref == "P90001"
    assert gateway.writes == 1
    with pytest.raises(InvalidTransition):
        await actions.approve(view.id, "Daniel Tan")  # double click / second approver
    assert gateway.writes == 1


async def test_reject_never_writes(actions, gateway):
    view = await actions.propose(AGENT, "purchase_order", VALID, "Ingot below minimum")
    rejected = await actions.reject(view.id, "Aisyah Rahman", "Supplier on hold")
    assert rejected.status == "rejected"
    assert rejected.decision_note == "Supplier on hold"
    assert gateway.writes == 0
    with pytest.raises(InvalidTransition):
        await actions.approve(view.id, "Aisyah Rahman")


async def test_requester_cannot_self_approve(actions):
    view = await actions.propose(AGENT, "purchase_order", VALID, "Ingot below minimum")
    with pytest.raises(InvalidTransition, match="own action"):
        await actions.approve(view.id, AGENT.subject)


async def test_business_rejection_fails_without_retry(actions, gateway):
    gateway.fail_next = OdooError("Supplier is archived")
    view = await actions.propose(AGENT, "purchase_order", VALID, "Ingot below minimum")
    failed = await actions.approve(view.id, "Aisyah Rahman")
    assert failed.status == "failed"
    assert "archived" in failed.error
    retried = await actions.execute(view.id)  # someone fixed the supplier
    assert retried.status == "executed"
    assert gateway.writes == 1


async def test_crash_after_write_does_not_duplicate(actions, gateway):
    """Odoo committed the PO but the response was lost. The retry must find
    the PO by its origin tag instead of creating a second one."""
    gateway.crash_after_write = True
    view = await actions.propose(AGENT, "purchase_order", VALID, "Ingot below minimum")
    done = await actions.approve(view.id, "Aisyah Rahman")
    assert done.status == "executed"
    assert gateway.writes == 1
    assert gateway.records[origin_tag(view.id)]["ref"] == done.odoo_ref


async def test_transient_outage_retries(actions, gateway):
    gateway.fail_next = OdooUnavailable("timeout")
    view = await actions.propose(AGENT, "purchase_order", VALID, "Ingot below minimum")
    done = await actions.approve(view.id, "Aisyah Rahman")
    assert done.status == "executed"


async def test_policy_auto_approval_for_service_account(actions, gateway):
    bill = {"payload": {"po_number": "P00008", "amount_total": 61600}, "summary": "Bill PAS-INV-1"}
    view = await actions.propose(INVOICE_FLOW, "vendor_bill", bill, "3-way match passed")
    assert view.status == "executed"
    assert view.decided_by == "policy:auto-approve:invoice-flow"
    # ...but only for the action types the policy names
    other = await actions.propose(INVOICE_FLOW, "purchase_order", VALID, "not covered by policy")
    assert other.status == "pending"


async def test_callback_receives_decision(actions):
    calls = []

    class Recorder:
        async def post(self, url, json):
            calls.append((url, json))

    actions.http = Recorder()
    view = await actions.propose(AGENT, "purchase_order", VALID, "Ingot", callback_url="http://agent.test/cb/1")
    await actions.approve(view.id, "Aisyah Rahman")
    assert calls[-1][0] == "http://agent.test/cb/1"
    assert calls[-1][1]["status"] == "executed"
