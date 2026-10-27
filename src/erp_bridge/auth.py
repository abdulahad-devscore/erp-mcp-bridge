"""OAuth 2.0 client-credentials + scoped permissions.

Every MCP call carries a bearer JWT. The token says who the caller is, which
scopes they hold and which warehouses they may see. Tools check scopes
explicitly (`require`) and filter results by warehouse, so a store manager's
agent physically cannot read another store's stock or finance data.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass, field

import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken

# Read scopes are per data domain; write scopes only ever create proposals.
SCOPES: dict[str, str] = {
    "stock:read": "Stock levels, reorder rules and transfers",
    "sales:read": "Sales orders and sales analytics",
    "finance:read": "Customer invoices and balances",
    "purchasing:read": "Purchase orders, receipts and vendor bills",
    "manufacturing:read": "Bills of materials and manufacturing orders",
    "purchase:propose": "Propose purchase orders (human approval required)",
    "transfer:propose": "Propose inter-warehouse transfers (human approval required)",
    "manufacturing:propose": "Propose manufacturing orders (human approval required)",
    "vendor_bill:propose": "Propose vendor bills against purchase orders",
    "actions:decide": "Approve or reject queued actions (people, not agents)",
}


class AuthError(Exception):
    pass


class PermissionDenied(Exception):
    def __init__(self, message: str, scope: str | None = None):
        super().__init__(message)
        self.scope = scope


@dataclass(frozen=True)
class Principal:
    client_id: str
    subject: str
    scopes: frozenset[str]
    warehouses: frozenset[str] = field(default_factory=frozenset)  # empty = all

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise PermissionDenied(f"'{self.client_id}' lacks scope '{scope}'", scope)

    def can_see_warehouse(self, code: str) -> bool:
        return not self.warehouses or code in self.warehouses


# --------------------------------------------------------------- secrets
def hash_secret(secret: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(secret.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_secret(secret: str, stored: str) -> bool:
    try:
        _, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    expected = hash_secret(secret, salt=base64.b64decode(salt_b64))
    return hmac.compare_digest(expected.split("$")[2], digest_b64)


# --------------------------------------------------------------- tokens
class TokenService:
    def __init__(self, secret: str, issuer: str, ttl_seconds: int = 3600):
        self.secret = secret
        self.issuer = issuer.rstrip("/")
        self.audience = f"{self.issuer}/mcp"
        self.ttl = ttl_seconds

    def issue(self, client_id: str, scopes: list[str], warehouses: list[str], subject: str | None = None) -> str:
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": self.audience,
            "sub": subject or client_id,
            "client_id": client_id,
            "scope": " ".join(sorted(scopes)),
            "wh": sorted(warehouses),
            "iat": now,
            "exp": now + self.ttl,
            "jti": str(uuid.uuid4()),
        }
        return jwt.encode(claims, self.secret, algorithm="HS256")

    def decode(self, token: str) -> dict:
        try:
            return jwt.decode(token, self.secret, algorithms=["HS256"], audience=self.audience, issuer=self.issuer)
        except jwt.PyJWTError as exc:
            raise AuthError(str(exc)) from exc

    def principal(self, token: str) -> Principal:
        claims = self.decode(token)
        return principal_from_claims(claims)


def principal_from_claims(claims: dict) -> Principal:
    return Principal(
        client_id=claims["client_id"],
        subject=claims.get("sub", claims["client_id"]),
        scopes=frozenset(claims.get("scope", "").split()),
        warehouses=frozenset(claims.get("wh", [])),
    )


class JWTTokenVerifier:
    """Plugs our tokens into the MCP SDK's bearer-auth middleware."""

    def __init__(self, tokens: TokenService):
        self.tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = self.tokens.decode(token)
        except AuthError:
            return None
        return AccessToken(
            token=token,
            client_id=claims["client_id"],
            scopes=claims.get("scope", "").split(),
            expires_at=claims["exp"],
            resource=claims["aud"],
            subject=claims.get("sub"),
            claims=claims,
        )


# Set for stdio transport and in-process tests, where there is no HTTP bearer.
_principal_override: contextvars.ContextVar[Principal | None] = contextvars.ContextVar("principal", default=None)


def set_principal(principal: Principal | None) -> contextvars.Token:
    return _principal_override.set(principal)


def current_principal() -> Principal:
    access = get_access_token()
    if access is not None and access.claims:
        return principal_from_claims(access.claims)
    override = _principal_override.get()
    if override is not None:
        return override
    raise PermissionDenied("No authenticated principal on this request")
