"""HTTP surface of the bridge.

/mcp                      MCP Streamable HTTP endpoint (bearer JWT)
/oauth/token              OAuth 2.0 client-credentials token endpoint
/api/actions...           JSON API for the Agent Approval Console (OpenAPI at /docs)
/approvals, /audit        server-rendered approval page for approvers
/healthz                  liveness + Odoo reachability
"""

import base64
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from erp_bridge.actions import ActionNotFound, ActionService, InvalidTransition
from erp_bridge.audit import AuditLog
from erp_bridge.auth import SCOPES, AuthError, JWTTokenVerifier, TokenService
from erp_bridge.clients import authenticate, bootstrap_demo_clients
from erp_bridge.config import Settings, get_settings
from erp_bridge.db import AuditEvent, Database, OAuthClient
from erp_bridge.domain import ActionView
from erp_bridge.mcp_server import BridgeServices, build_mcp
from erp_bridge.odoo.client import OdooClient
from erp_bridge.odoo.gateway import ErpGateway, OdooGateway
from erp_bridge.ratelimit import MemoryRateLimiter, RateLimiter, RedisRateLimiter

log = logging.getLogger(__name__)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    scope: str


class Decision(BaseModel):
    note: str | None = None


class AuditRow(BaseModel):
    id: int
    ts: str
    kind: str
    client_id: str | None
    subject: str | None
    tool: str | None
    outcome: str
    detail: str | None
    latency_ms: int | None
    action_id: str | None


def create_app(
    settings: Settings | None = None,
    *,
    gateway: ErpGateway | None = None,
    db: Database | None = None,
    limiter: RateLimiter | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    db = db or Database(settings.database_url)
    odoo: OdooClient | None = None
    if gateway is None:
        odoo = OdooClient(settings.odoo_url, settings.odoo_db, settings.odoo_user, settings.odoo_password)
        gateway = OdooGateway(
            odoo, today=date.fromisoformat(settings.business_date) if settings.business_date else None
        )
    if limiter is None:
        limiter = RedisRateLimiter(Redis.from_url(settings.redis_url)) if settings.redis_url else MemoryRateLimiter()
    audit = AuditLog(db)
    actions = ActionService(db, gateway, audit)
    tokens = TokenService(settings.jwt_secret, settings.public_url, settings.jwt_ttl_seconds)
    services = BridgeServices(settings=settings, gateway=gateway, actions=actions, audit=audit, limiter=limiter)

    mcp = build_mcp(
        services,
        token_verifier=JWTTokenVerifier(tokens),
        auth=AuthSettings(
            issuer_url=settings.public_url,
            resource_server_url=f"{settings.public_url}/mcp",
            required_scopes=[],
            validate_token_resource=True,
        ),
    )
    mcp_app = mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=[f"http://{h}" for h in settings.allowed_hosts],
        ),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.create_all()
        if settings.bootstrap_demo_clients:
            await bootstrap_demo_clients(db)
        async with mcp.session_manager.run():
            yield
        if odoo:
            await odoo.aclose()
        await db.dispose()

    app = FastAPI(
        title="ERP MCP Bridge",
        version="1.0.0",
        description="Approval queue and audit API behind the Tembusu ERP MCP server.",
        lifespan=lifespan,
    )
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, same_site="strict")
    app.state.services = services
    app.state.tokens = tokens
    app.state.db = db

    # ------------------------------------------------------------- OAuth
    @app.post("/oauth/token", response_model=TokenResponse, tags=["oauth"])
    async def token(
        grant_type: Annotated[str, Form()],
        client_id: Annotated[str | None, Form()] = None,
        client_secret: Annotated[str | None, Form()] = None,
        scope: Annotated[str | None, Form()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ):
        """OAuth 2.0 client-credentials grant (RFC 6749 4.4). Request a subset of your scopes with `scope`."""
        if grant_type != "client_credentials":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        if authorization and authorization.lower().startswith("basic "):
            client_id, _, client_secret = base64.b64decode(authorization[6:]).decode().partition(":")
        client = await authenticate(db, client_id or "", client_secret or "")
        if not client:
            await audit.record("auth", "denied", client_id=client_id, detail="invalid client credentials")
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        granted = list(client.scopes)
        if scope:
            requested = scope.split()
            if not set(requested) <= set(granted):
                return JSONResponse({"error": "invalid_scope"}, status_code=400)
            granted = requested
        await audit.record("auth", "ok", client_id=client.client_id, detail=f"token issued: {' '.join(granted)}")
        return TokenResponse(
            access_token=tokens.issue(client.client_id, granted, client.warehouses),
            expires_in=settings.jwt_ttl_seconds,
            scope=" ".join(sorted(granted)),
        )

    @app.get("/.well-known/oauth-authorization-server", tags=["oauth"])
    async def as_metadata():
        return {
            "issuer": settings.public_url,
            "token_endpoint": f"{settings.public_url}/oauth/token",
            "grant_types_supported": ["client_credentials"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
            "scopes_supported": list(SCOPES),
        }

    @app.get("/healthz", tags=["ops"])
    async def healthz():
        try:
            await gateway.warehouses()
            erp = "ok"
        except Exception as exc:  # noqa: BLE001 - health must never raise
            erp = f"unavailable: {type(exc).__name__}"
        return {"status": "ok", "erp": erp}

    # ------------------------------------------------ approval JSON API
    async def approver(authorization: Annotated[str | None, Header()] = None) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
        try:
            principal = tokens.principal(authorization[7:])
        except AuthError as exc:
            raise HTTPException(401, "Invalid token") from exc
        if "actions:decide" not in principal.scopes:
            raise HTTPException(403, "actions:decide scope required")
        async with db.session() as s:
            client = await s.get(OAuthClient, principal.client_id)
        return client.display_name if client else principal.client_id

    Approver = Annotated[str, Depends(approver)]

    @app.get("/api/actions", response_model=list[ActionView], tags=["approvals"])
    async def api_list(who: Approver, status: str | None = None, limit: int = Query(100, le=500)):
        return await actions.list(status=status, limit=limit)

    @app.get("/api/actions/{action_id}", response_model=ActionView, tags=["approvals"])
    async def api_get(action_id: str, who: Approver):
        try:
            return await actions.get(action_id)
        except ActionNotFound as exc:
            raise HTTPException(404, "No such action") from exc

    async def _decide(action_id: str, who: str, approve: bool, note: str | None) -> ActionView:
        try:
            if approve:
                return await actions.approve(action_id, who, note)
            return await actions.reject(action_id, who, note)
        except ActionNotFound as exc:
            raise HTTPException(404, "No such action") from exc
        except InvalidTransition as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/actions/{action_id}/approve", response_model=ActionView, tags=["approvals"])
    async def api_approve(action_id: str, body: Decision, who: Approver):
        """Approve and immediately write to the ERP. Returns the executed (or failed) action."""
        return await _decide(action_id, who, True, body.note)

    @app.post("/api/actions/{action_id}/reject", response_model=ActionView, tags=["approvals"])
    async def api_reject(action_id: str, body: Decision, who: Approver):
        return await _decide(action_id, who, False, body.note)

    @app.post("/api/actions/{action_id}/retry", response_model=ActionView, tags=["approvals"])
    async def api_retry(action_id: str, who: Approver):
        """Re-run a failed write. Safe: the executor finds records it already created."""
        try:
            return await actions.execute(action_id)
        except InvalidTransition as exc:
            raise HTTPException(409, str(exc)) from exc

    async def _audit_rows(action_id: str | None, limit: int) -> list[AuditRow]:
        q = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
        if action_id:
            q = q.where(AuditEvent.action_id == action_id)
        async with db.session() as s:
            rows = (await s.scalars(q)).all()
        return [
            AuditRow(
                id=r.id,
                ts=r.ts.isoformat(),
                kind=r.kind,
                client_id=r.client_id,
                subject=r.subject,
                tool=r.tool,
                outcome=r.outcome,
                detail=r.detail,
                latency_ms=r.latency_ms,
                action_id=r.action_id,
            )
            for r in rows
        ]

    @app.get("/api/audit", response_model=list[AuditRow], tags=["audit"])
    async def api_audit(who: Approver, action_id: str | None = None, limit: int = Query(200, le=1000)):
        return await _audit_rows(action_id, limit)

    # ----------------------------------------------------- approval page
    def page_user(request: Request) -> str | None:
        return request.session.get("approver")

    def csrf(request: Request) -> str:
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(24)
        return request.session["csrf"]

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/approvals")

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_form(request: Request, error: str | None = None):
        return TEMPLATES.TemplateResponse(request, "login.html", {"error": error})

    @app.post("/login", include_in_schema=False)
    async def login(request: Request, client_id: Annotated[str, Form()], secret: Annotated[str, Form()]):
        client = await authenticate(db, client_id, secret)
        if not client or "actions:decide" not in client.scopes:
            await audit.record("auth", "denied", client_id=client_id, detail="approval page login failed")
            return RedirectResponse("/login?error=1", status_code=303)
        request.session.clear()
        request.session["approver"] = client.display_name
        await audit.record("auth", "ok", client_id=client_id, subject=client.display_name, detail="approval page login")
        return RedirectResponse("/approvals", status_code=303)

    @app.post("/logout", include_in_schema=False)
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/approvals", response_class=HTMLResponse, include_in_schema=False)
    async def approvals(request: Request):
        who = page_user(request)
        if not who:
            return RedirectResponse("/login", status_code=303)
        pending = await actions.list(status="pending")
        recent = [a for a in await actions.list(limit=50) if a.status != "pending"]
        return TEMPLATES.TemplateResponse(
            request, "approvals.html", {"who": who, "pending": pending, "recent": recent, "csrf": csrf(request)}
        )

    @app.get("/approvals/{action_id}", response_class=HTMLResponse, include_in_schema=False)
    async def approval_detail(request: Request, action_id: str):
        who = page_user(request)
        if not who:
            return RedirectResponse("/login", status_code=303)
        try:
            action = await actions.get(action_id)
        except ActionNotFound as exc:
            raise HTTPException(404) from exc
        return TEMPLATES.TemplateResponse(
            request,
            "action.html",
            {"who": who, "a": action, "events": await _audit_rows(action_id, 50), "csrf": csrf(request)},
        )

    @app.post("/approvals/{action_id}/{decision}", include_in_schema=False)
    async def approval_decide(
        request: Request,
        action_id: str,
        decision: Literal["approve", "reject", "retry"],
        token: Annotated[str, Form()],
        note: Annotated[str | None, Form()] = None,
    ):
        who = page_user(request)
        if not who:
            return RedirectResponse("/login", status_code=303)
        if not secrets.compare_digest(token, request.session.get("csrf", "")):
            raise HTTPException(403, "CSRF check failed")
        try:
            if decision == "retry":
                await actions.execute(action_id)
            else:
                await _decide(action_id, who, decision == "approve", note)
        except (HTTPException, InvalidTransition) as exc:
            log.info("decision on %s refused: %s", action_id, exc)
        return RedirectResponse(f"/approvals/{action_id}", status_code=303)

    @app.get("/audit", response_class=HTMLResponse, include_in_schema=False)
    async def audit_page(request: Request):
        who = page_user(request)
        if not who:
            return RedirectResponse("/login", status_code=303)
        return TEMPLATES.TemplateResponse(request, "audit.html", {"who": who, "events": await _audit_rows(None, 300)})

    # MCP last: FastAPI routes win, everything else (/mcp, /.well-known/oauth-protected-resource) falls through.
    app.mount("/", mcp_app)
    return app
