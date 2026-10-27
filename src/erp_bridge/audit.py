"""Structured audit log: one row per tool call, decision and write.

Rows go to Postgres (queryable from the approval page and by auditors) and
the same event is printed as a JSON line so log shippers pick it up too.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from erp_bridge.db import AuditEvent, Database

log = logging.getLogger("erp_bridge.audit")

_REDACT = {"password", "secret", "client_secret", "token", "access_token", "authorization"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if k.lower() in _REDACT else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class AuditLog:
    def __init__(self, db: Database):
        self.db = db

    async def record(
        self,
        kind: str,
        outcome: str,
        *,
        client_id: str | None = None,
        subject: str | None = None,
        tool: str | None = None,
        arguments: dict | None = None,
        detail: str | None = None,
        latency_ms: int | None = None,
        action_id: str | None = None,
    ) -> None:
        args = redact(arguments) if arguments is not None else None
        event = AuditEvent(
            kind=kind,
            outcome=outcome,
            client_id=client_id,
            subject=subject,
            tool=tool,
            arguments=args,
            detail=detail,
            latency_ms=latency_ms,
            action_id=action_id,
        )
        async with self.db.session() as s:
            s.add(event)
            await s.commit()
        log.info(
            json.dumps(
                {
                    "audit": kind,
                    "outcome": outcome,
                    "client_id": client_id,
                    "subject": subject,
                    "tool": tool,
                    "args": args,
                    "detail": detail,
                    "latency_ms": latency_ms,
                    "action_id": action_id,
                },
                default=str,
            )
        )
