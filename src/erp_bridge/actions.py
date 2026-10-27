"""The reviewed write queue.

Agents never write to the ERP. Write tools call `propose`, which validates
the request against live ERP data and stores a pending action. A person
approves or rejects it on the approval page; only then does `execute` write
to Odoo. Every transition is audited.

Guarantees:
* Proposals are idempotent per (client, idempotency_key): an agent that
  retries a tool call gets the same action back, not a duplicate.
* Approval is a compare-and-set on status, so a double-click or two
  approvers racing cannot execute an action twice.
* Execution is idempotent against Odoo: each record is tagged with the
  action id and looked up before creating, so a crash between "Odoo wrote it"
  and "we recorded that" does not produce a second PO on retry.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime

import httpx
from sqlalchemy import select, update

from erp_bridge.audit import AuditLog
from erp_bridge.auth import Principal
from erp_bridge.db import ActionStatus, Database, OAuthClient, PendingAction, utcnow
from erp_bridge.domain import ActionView
from erp_bridge.odoo.client import OdooError, OdooUnavailable
from erp_bridge.odoo.gateway import ErpGateway

log = logging.getLogger(__name__)


class ActionNotFound(LookupError):
    pass


class InvalidTransition(ValueError):
    pass


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def to_view(a: PendingAction) -> ActionView:
    return ActionView(
        id=a.id,
        action_type=a.action_type,
        status=a.status,
        summary=a.summary,
        reason=a.reason,
        payload=a.payload,
        requested_by=a.requested_by,
        requested_subject=a.requested_subject,
        created_at=_iso(a.created_at) or "",
        decided_by=a.decided_by,
        decided_at=_iso(a.decided_at),
        decision_note=a.decision_note,
        executed_at=_iso(a.executed_at),
        odoo_ref=a.odoo_ref,
        error=a.error,
    )


def origin_tag(action_id: str) -> str:
    return f"bridge-action:{action_id}"


class ActionService:
    def __init__(
        self,
        db: Database,
        gateway: ErpGateway,
        audit: AuditLog,
        *,
        execute_retries: int = 3,
        http: httpx.AsyncClient | None = None,
    ):
        self.db = db
        self.gateway = gateway
        self.audit = audit
        self.execute_retries = execute_retries
        self.http = http or httpx.AsyncClient(timeout=10, trust_env=False)

    # ------------------------------------------------------------ propose
    async def propose(
        self,
        principal: Principal,
        action_type: str,
        validated: dict,
        reason: str,
        *,
        idempotency_key: str | None = None,
        callback_url: str | None = None,
    ) -> ActionView:
        key = (
            idempotency_key
            or hashlib.sha256(
                json.dumps({"t": action_type, "p": validated["payload"]}, sort_keys=True, default=str).encode()
            ).hexdigest()
        )
        async with self.db.session() as s:
            existing = await s.scalar(
                select(PendingAction).where(
                    PendingAction.requested_by == principal.client_id, PendingAction.idempotency_key == key
                )
            )
            if existing:
                return to_view(existing)
            action = PendingAction(
                action_type=action_type,
                payload=validated["payload"],
                summary=validated["summary"],
                reason=reason,
                requested_by=principal.client_id,
                requested_subject=principal.subject,
                idempotency_key=key,
                callback_url=callback_url,
            )
            s.add(action)
            client = await s.get(OAuthClient, principal.client_id)
            await s.commit()
        await self.audit.record(
            "decision",
            "ok",
            client_id=principal.client_id,
            subject=principal.subject,
            detail=f"proposed {action_type}: {action.summary}",
            action_id=action.id,
        )
        if client and action_type in (client.auto_approve or []):
            return await self.approve(
                action.id,
                f"policy:auto-approve:{principal.client_id}",
                "Auto-approved by policy for this service account",
            )
        return to_view(action)

    # ----------------------------------------------------------- decisions
    async def get(self, action_id: str) -> ActionView:
        async with self.db.session() as s:
            action = await s.get(PendingAction, action_id)
            if not action:
                raise ActionNotFound(action_id)
            return to_view(action)

    async def list(
        self, status: str | None = None, limit: int = 100, requested_by: str | None = None
    ) -> list[ActionView]:
        q = select(PendingAction).order_by(PendingAction.created_at.desc()).limit(limit)
        if status:
            q = q.where(PendingAction.status == status)
        if requested_by:
            q = q.where(PendingAction.requested_by == requested_by)
        async with self.db.session() as s:
            return [to_view(a) for a in (await s.scalars(q)).all()]

    async def _transition(self, action_id: str, frm: tuple[str, ...], to: str, **values) -> None:
        async with self.db.session() as s:
            result = await s.execute(
                update(PendingAction)
                .where(PendingAction.id == action_id, PendingAction.status.in_(frm))
                .values(status=to, **values)
            )
            await s.commit()
        if result.rowcount != 1:
            current = await self.get(action_id)  # raises ActionNotFound
            raise InvalidTransition(f"Action {action_id} is '{current.status}', expected one of {frm}")

    async def approve(self, action_id: str, approver: str, note: str | None = None) -> ActionView:
        action = await self.get(action_id)
        if action.requested_subject and action.requested_subject == approver:
            raise InvalidTransition("The requester cannot approve their own action")
        await self._transition(
            action_id,
            (ActionStatus.PENDING,),
            ActionStatus.APPROVED,
            decided_by=approver,
            decided_at=utcnow(),
            decision_note=note,
        )
        await self.audit.record(
            "decision", "ok", subject=approver, detail=f"approved: {note or ''}", action_id=action_id
        )
        return await self.execute(action_id)

    async def reject(self, action_id: str, approver: str, note: str | None = None) -> ActionView:
        await self._transition(
            action_id,
            (ActionStatus.PENDING,),
            ActionStatus.REJECTED,
            decided_by=approver,
            decided_at=utcnow(),
            decision_note=note,
        )
        await self.audit.record(
            "decision", "ok", subject=approver, detail=f"rejected: {note or ''}", action_id=action_id
        )
        view = await self.get(action_id)
        await self._notify(view)
        return view

    # ----------------------------------------------------------- execution
    async def execute(self, action_id: str) -> ActionView:
        """Write an approved (or previously failed) action to Odoo."""
        await self._transition(action_id, (ActionStatus.APPROVED, ActionStatus.FAILED), ActionStatus.EXECUTING)
        async with self.db.session() as s:
            action = await s.get(PendingAction, action_id)
            action.attempts += 1
            await s.commit()
            action_type, payload = action.action_type, action.payload

        started = asyncio.get_running_loop().time()
        delay, result, error = 1.0, None, None
        for attempt in range(1, self.execute_retries + 1):
            try:
                result = await self.gateway.execute(action_type, payload, origin_tag(action_id))
                break
            except OdooUnavailable as exc:
                error = f"Odoo unavailable: {exc}"
                if attempt < self.execute_retries:
                    await asyncio.sleep(delay)
                    delay *= 2
            except OdooError as exc:
                error = f"Odoo rejected the write: {exc}"
                break  # a business rejection will not succeed on retry
        latency = int((asyncio.get_running_loop().time() - started) * 1000)

        async with self.db.session() as s:
            action = await s.get(PendingAction, action_id)
            if result:
                action.status = ActionStatus.EXECUTED
                action.executed_at = utcnow()
                action.odoo_model, action.odoo_id, action.odoo_ref = result["model"], result["id"], result["ref"]
                action.error = result.get("warning")
            else:
                action.status = ActionStatus.FAILED
                action.error = error
            await s.commit()
        await self.audit.record(
            "execution",
            "ok" if result else "error",
            detail=(f"wrote {result['model']} {result['ref']}" if result else error),
            latency_ms=latency,
            action_id=action_id,
        )
        view = await self.get(action_id)
        await self._notify(view)
        return view

    async def _notify(self, view: ActionView) -> None:
        """Tell the requester (e.g. a paused agent run) the action was decided."""
        async with self.db.session() as s:
            action = await s.get(PendingAction, view.id)
            url = action.callback_url if action else None
        if not url:
            return
        try:
            await self.http.post(url, json=view.model_dump(mode="json"))
        except httpx.HTTPError as exc:
            # The requester can still poll get_action_status; don't fail the approval.
            log.warning("callback to %s failed: %s", url, exc)
