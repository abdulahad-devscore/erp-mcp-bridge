"""Command line entry points.

erp-bridge serve            HTTP: /mcp + approval page + API (what compose runs)
erp-bridge stdio            stdio transport, for Claude Desktop / MCP Inspector
erp-bridge token CLIENT     print a bearer token for a client (local testing)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date


def serve(args: argparse.Namespace) -> None:
    import uvicorn

    from erp_bridge.api import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, proxy_headers=True)


def stdio(args: argparse.Namespace) -> None:
    """Claude Desktop launches this as a subprocess. The caller's identity comes
    from BRIDGE_STDIO_TOKEN (a token from /oauth/token), so stdio clients get
    exactly the same scopes, limits and audit trail as HTTP clients."""
    from erp_bridge.actions import ActionService
    from erp_bridge.audit import AuditLog
    from erp_bridge.auth import TokenService, set_principal
    from erp_bridge.config import get_settings
    from erp_bridge.db import Database
    from erp_bridge.mcp_server import BridgeServices, build_mcp
    from erp_bridge.odoo.client import OdooClient
    from erp_bridge.odoo.gateway import OdooGateway
    from erp_bridge.ratelimit import MemoryRateLimiter

    settings = get_settings()
    if not settings.stdio_token:
        sys.exit("Set STDIO_TOKEN to a bearer token from POST /oauth/token")
    tokens = TokenService(settings.jwt_secret, settings.public_url, settings.jwt_ttl_seconds)
    principal = tokens.principal(settings.stdio_token)
    db = Database(settings.database_url)
    gateway = OdooGateway(
        OdooClient(settings.odoo_url, settings.odoo_db, settings.odoo_user, settings.odoo_password),
        today=date.fromisoformat(settings.business_date) if settings.business_date else None,
    )
    audit = AuditLog(db)
    services = BridgeServices(settings, gateway, ActionService(db, gateway, audit), audit, MemoryRateLimiter())
    mcp = build_mcp(services)
    set_principal(principal)
    # stdout is the protocol channel; logs must go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

    async def run() -> None:
        await db.create_all()
        await mcp.run_stdio_async()

    asyncio.run(run())


def token(args: argparse.Namespace) -> None:
    from erp_bridge.auth import TokenService
    from erp_bridge.clients import DEMO_CLIENTS
    from erp_bridge.config import get_settings

    settings = get_settings()
    match = [c for c in DEMO_CLIENTS if c[0] == args.client]
    if not match:
        sys.exit(f"Unknown demo client {args.client}. Known: {', '.join(c[0] for c in DEMO_CLIENTS)}")
    _, _, _, scopes, warehouses, _ = match[0]
    print(
        TokenService(settings.jwt_secret, settings.public_url, settings.jwt_ttl_seconds).issue(
            args.client, scopes, warehouses
        )
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="erp-bridge")
    sub = parser.add_subparsers(required=True)
    p = sub.add_parser("serve")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=serve)
    sub.add_parser("stdio").set_defaults(fn=stdio)
    p = sub.add_parser("token")
    p.add_argument("client")
    p.set_defaults(fn=token)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
