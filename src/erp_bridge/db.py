"""Bridge-owned state. Odoo stays the system of record; this database only holds
what the bridge itself is responsible for: who may call it, what they asked to
change, and a record of every call."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}


class ActionStatus(enum.StrEnum):
    PENDING = "pending"  # waiting for a person
    APPROVED = "approved"  # approved, not yet written
    EXECUTING = "executing"  # write to Odoo in progress
    EXECUTED = "executed"  # written; odoo_id / odoo_ref set
    REJECTED = "rejected"
    FAILED = "failed"  # approved but the write failed after retries


class OAuthClient(Base):
    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    secret_hash: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20), default="user")  # user | service
    scopes: Mapped[list[str]] = mapped_column(default=list)
    # Warehouse codes this client may see. Empty list = all warehouses.
    warehouses: Mapped[list[str]] = mapped_column(default=list)
    # Action types this client's proposals skip the human queue for (policy approval).
    auto_approve: Mapped[list[str]] = mapped_column(default=list)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PendingAction(Base):
    __tablename__ = "pending_actions"
    __table_args__ = (UniqueConstraint("requested_by", "idempotency_key", name="uq_action_idempotency"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    action_type: Mapped[str] = mapped_column(String(50), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column()
    summary: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default=ActionStatus.PENDING, index=True)
    requested_by: Mapped[str] = mapped_column(String(64), index=True)
    requested_subject: Mapped[str | None] = mapped_column(String(200))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    callback_url: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    decided_by: Mapped[str | None] = mapped_column(String(200))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    odoo_model: Mapped[str | None] = mapped_column(String(64))
    odoo_id: Mapped[int | None] = mapped_column(Integer)
    odoo_ref: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)  # tool_call | decision | execution | auth
    client_id: Mapped[str | None] = mapped_column(String(64), index=True)
    subject: Mapped[str | None] = mapped_column(String(200))
    tool: Mapped[str | None] = mapped_column(String(80))
    arguments: Mapped[dict[str, Any] | None] = mapped_column()
    outcome: Mapped[str] = mapped_column(String(20))  # ok | denied | error | rate_limited
    detail: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    action_id: Mapped[str | None] = mapped_column(String(36), index=True)


class Database:
    def __init__(self, url: str):
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    def session(self) -> AsyncSession:
        return self.sessions()

    async def dispose(self) -> None:
        await self.engine.dispose()
