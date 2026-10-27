from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Odoo (the system of record)
    odoo_url: str = "http://localhost:8069"
    odoo_db: str = "tembusu"
    odoo_user: str = "admin"
    odoo_password: str = "admin"

    # Bridge state: write queue, audit log, OAuth clients
    database_url: str = "postgresql+psycopg://bridge:bridge@localhost:5433/bridge"
    redis_url: str | None = "redis://localhost:6379/0"

    # OAuth 2.0 (client-credentials). The bridge is its own tiny authorization
    # server so the demo runs offline; swap jwt_secret for a JWKS URL from
    # Entra ID / Auth0 / Keycloak in a real deployment.
    public_url: str = "http://localhost:8000"
    jwt_secret: str = Field(default="change-me-in-production-please-32b", min_length=32)
    jwt_ttl_seconds: int = 3600

    # Rate limit per OAuth client, per minute
    rate_limit_per_minute: int = 120
    rate_limit_writes_per_minute: int = 20

    # Signs the approval page's session cookie
    session_secret: str = "change-me-session-secret"

    # Where the bridge may POST decided actions back to (SSRF guard)
    callback_allowed_prefixes: list[str] = ["http://ops-copilot:8100/", "http://localhost:8100/"]

    # Pin "today" for overdue calculations (keeps eval answers stable). Empty = real date.
    business_date: str | None = None

    # Demo OAuth clients are created on startup with these secrets unless they exist
    bootstrap_demo_clients: bool = True

    # Hostnames MCP clients may use (DNS-rebinding protection)
    allowed_hosts: list[str] = ["localhost:*", "127.0.0.1:*", "erp-bridge:*", "host.docker.internal:*"]

    # Token for stdio transport (Claude Desktop launches the server as a subprocess)
    stdio_token: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
