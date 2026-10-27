"""OAuth client registry and the demo clients the compose stack starts with."""

from __future__ import annotations

import os

from sqlalchemy import select

from erp_bridge.auth import SCOPES, hash_secret, verify_secret
from erp_bridge.db import Database, OAuthClient

ALL_READ = ["stock:read", "sales:read", "finance:read", "purchasing:read", "manufacturing:read"]

# client_id, display name, kind, scopes, warehouses, auto_approve
DEMO_CLIENTS = [
    (
        "ops-copilot",
        "Ops Copilot agent",
        "service",
        ALL_READ + ["purchase:propose", "transfer:propose", "manufacturing:propose"],
        [],
        [],
    ),
    (
        "invoice-flow",
        "Invoice Flow automation",
        "service",
        ["stock:read", "purchasing:read", "vendor_bill:propose"],
        [],
        ["vendor_bill"],
    ),
    ("store-klcc", "KLCC store manager", "user", ["stock:read", "sales:read", "transfer:propose"], ["KLCC"], []),
    ("finance-analyst", "Finance analyst", "user", ["stock:read", "sales:read", "finance:read"], [], []),
    (
        "claude-desktop",
        "Claude Desktop (demo user)",
        "user",
        ALL_READ + ["purchase:propose", "transfer:propose", "manufacturing:propose"],
        [],
        [],
    ),
    ("approver-ops", "Aisyah Rahman (Operations Manager)", "user", ["actions:decide"], [], []),
    ("approver-finance", "Daniel Tan (Finance Controller)", "user", ["actions:decide"], [], []),
]


def demo_secret(client_id: str) -> str:
    env = "BRIDGE_SECRET_" + client_id.upper().replace("-", "_")
    return os.environ.get(env, f"{client_id}-demo-secret")


async def bootstrap_demo_clients(db: Database) -> None:
    async with db.session() as s:
        for client_id, name, kind, scopes, warehouses, auto in DEMO_CLIENTS:
            if await s.get(OAuthClient, client_id):
                continue
            unknown = set(scopes) - set(SCOPES)
            assert not unknown, unknown
            s.add(
                OAuthClient(
                    client_id=client_id,
                    secret_hash=hash_secret(demo_secret(client_id)),
                    display_name=name,
                    kind=kind,
                    scopes=scopes,
                    warehouses=warehouses,
                    auto_approve=auto,
                )
            )
        await s.commit()


async def authenticate(db: Database, client_id: str, secret: str) -> OAuthClient | None:
    async with db.session() as s:
        client = await s.scalar(select(OAuthClient).where(OAuthClient.client_id == client_id, OAuthClient.active))
    if client and verify_secret(secret, client.secret_hash):
        return client
    return None
