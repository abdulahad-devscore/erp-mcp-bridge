from __future__ import annotations

import pytest

from erp_bridge.actions import ActionService
from erp_bridge.audit import AuditLog
from erp_bridge.auth import Principal, TokenService
from erp_bridge.clients import bootstrap_demo_clients
from erp_bridge.config import Settings
from erp_bridge.db import Database
from erp_bridge.mcp_server import BridgeServices, build_mcp
from erp_bridge.ratelimit import MemoryRateLimiter
from tests.fakes import FakeGateway


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/bridge.db",
        redis_url=None,
        public_url="http://testserver",
        jwt_secret="test-secret-test-secret-test-secret-123",
        rate_limit_per_minute=1000,
        rate_limit_writes_per_minute=100,
        callback_allowed_prefixes=["http://agent.test/"],
    )


@pytest.fixture
async def db(settings) -> Database:
    database = Database(settings.database_url)
    await database.create_all()
    await bootstrap_demo_clients(database)
    yield database
    await database.dispose()


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def audit(db) -> AuditLog:
    return AuditLog(db)


@pytest.fixture
def actions(db, gateway, audit) -> ActionService:
    svc = ActionService(db, gateway, audit)
    svc.execute_retries = 2
    return svc


@pytest.fixture
def services(settings, gateway, actions, audit) -> BridgeServices:
    return BridgeServices(settings=settings, gateway=gateway, actions=actions, audit=audit, limiter=MemoryRateLimiter())


@pytest.fixture
def mcp(services):
    return build_mcp(services)


@pytest.fixture
def tokens(settings) -> TokenService:
    return TokenService(settings.jwt_secret, settings.public_url)


def principal(client_id: str, scopes: list[str], warehouses: list[str] | None = None) -> Principal:
    return Principal(
        client_id=client_id, subject=client_id, scopes=frozenset(scopes), warehouses=frozenset(warehouses or [])
    )


AGENT = principal(
    "ops-copilot",
    [
        "stock:read",
        "sales:read",
        "finance:read",
        "purchasing:read",
        "manufacturing:read",
        "purchase:propose",
        "transfer:propose",
        "manufacturing:propose",
    ],
)
STORE_KLCC = principal("store-klcc", ["stock:read", "sales:read", "transfer:propose"], ["KLCC"])
INVOICE_FLOW = principal("invoice-flow", ["stock:read", "purchasing:read", "vendor_bill:propose"])
