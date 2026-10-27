"""Thin JSON-RPC client for Odoo's external API.

Odoo exposes everything through one endpoint (`/jsonrpc`) and three services
(`common`, `object`, `db`). We only need `common.login` and
`object.execute_kw`. Retries cover transport failures only: a JSON-RPC error
from Odoo means the request was understood and rejected, so retrying it is
pointless.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any

import httpx


class OdooError(RuntimeError):
    """Odoo understood the call and rejected it (access error, validation error...)."""

    def __init__(self, message: str, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.data = data or {}


class OdooUnavailable(RuntimeError):
    """Odoo could not be reached after retries."""


class OdooClient:
    def __init__(
        self,
        url: str,
        db: str,
        username: str,
        password: str,
        *,
        timeout: float = 30.0,
        max_attempts: int = 4,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.url = url.rstrip("/")
        self.db = db
        self.username = username
        self.password = password
        self.max_attempts = max_attempts
        self._uid: int | None = None
        self._ids = itertools.count(1)
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport, trust_env=False)
        self._login_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _rpc(self, service: str, method: str, *args: Any) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "id": next(self._ids),
            "params": {"service": service, "method": method, "args": list(args)},
        }
        delay = 0.5
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = await self._http.post(f"{self.url}/jsonrpc", json=payload)
                resp.raise_for_status()
                body = resp.json()
                break
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt == self.max_attempts:
                    raise OdooUnavailable(f"Odoo unreachable after {attempt} attempts: {exc}") from exc
                await asyncio.sleep(delay)
                delay *= 2
        if "error" in body:
            err = body["error"]
            data = err.get("data") or {}
            raise OdooError(data.get("message") or err.get("message", "Odoo error"), data)
        return body.get("result")

    async def uid(self) -> int:
        if self._uid is None:
            async with self._login_lock:
                if self._uid is None:
                    uid = await self._rpc("common", "login", self.db, self.username, self.password)
                    if not uid:
                        raise OdooError("Odoo login failed: check ODOO_USER / ODOO_PASSWORD")
                    self._uid = uid
        return self._uid

    async def execute(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        uid = await self.uid()
        return await self._rpc("object", "execute_kw", self.db, uid, self.password, model, method, list(args), kwargs)

    async def search_read(
        self,
        model: str,
        domain: list | None = None,
        fields: list[str] | None = None,
        *,
        limit: int | None = None,
        order: str | None = None,
        context: dict | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"fields": fields or []}
        if limit is not None:
            kwargs["limit"] = limit
        if order:
            kwargs["order"] = order
        if context:
            kwargs["context"] = context
        return await self.execute(model, "search_read", domain or [], **kwargs)

    async def read_group(
        self,
        model: str,
        domain: list,
        fields: list[str],
        groupby: list[str],
        *,
        lazy: bool = False,
        orderby: str | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"lazy": lazy}
        if orderby:
            kwargs["orderby"] = orderby
        return await self.execute(model, "read_group", domain, fields, groupby, **kwargs)

    async def create(self, model: str, values: dict[str, Any], context: dict | None = None) -> int:
        kwargs = {"context": context} if context else {}
        return await self.execute(model, "create", values, **kwargs)
