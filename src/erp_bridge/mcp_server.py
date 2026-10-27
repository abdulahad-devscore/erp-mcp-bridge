"""The MCP server: the only door AI agents have into the ERP.

Tool design rules (see README "Tool design"):
* small, well-named tools in business terms (SKU, warehouse code, PO number)
* strict input schemas; clear errors the model can act on
* read tools and write tools are separate, and write tools only propose
* every call is scope-checked, warehouse-filtered, rate-limited and audited
"""

import functools
import inspect
import time
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from erp_bridge.actions import ActionNotFound, ActionService
from erp_bridge.audit import AuditLog
from erp_bridge.auth import PermissionDenied, Principal, current_principal
from erp_bridge.config import Settings
from erp_bridge.domain import (
    ActionView,
    BillLine,
    BomLine,
    CustomerBalance,
    Invoice,
    Line,
    ManufacturingOrder,
    ManufacturingProposal,
    PricedLine,
    Product,
    PurchaseOrder,
    PurchaseOrderProposal,
    ReorderSuggestion,
    SalesReport,
    StockLevel,
    Transfer,
    TransferProposal,
    VendorBillProposal,
    Warehouse,
)
from erp_bridge.odoo.client import OdooError, OdooUnavailable
from erp_bridge.odoo.gateway import ErpGateway, InvalidRequest, NotFound
from erp_bridge.ratelimit import RateLimited, RateLimiter

INSTRUCTIONS = """\
ERP access for Tembusu Housewares (manufacturing, distribution and retail of housewares in Malaysia).
Warehouses: FAC (Shah Alam factory), DC (Port Klang distribution centre), KLCC / PJ / PNG (retail stores).
Read tools return live Odoo data. Write tools (propose_*) never change the ERP: they queue an action
for a person to approve on the bridge's approval page. Always tell the user a write is *pending approval*,
and use get_action_status to follow it. Amounts are MYR. SKUs look like TH-CW-2401; raw materials RM-*.
"""

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
PROPOSE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

IdemKey = Annotated[
    str | None,
    Field(
        default=None,
        max_length=128,
        description="Optional. Reusing a key returns the existing proposal instead of creating a duplicate",
    ),
]
Callback = Annotated[
    str | None,
    Field(default=None, description="Optional URL the bridge POSTs the decided action to (allowlisted hosts only)"),
]


@dataclass
class BridgeServices:
    settings: Settings
    gateway: ErpGateway
    actions: ActionService
    audit: AuditLog
    limiter: RateLimiter


def build_mcp(svc: BridgeServices, **server_kwargs: Any) -> MCPServer:
    mcp = MCPServer(
        name="erp-mcp-bridge", title="Tembusu ERP Bridge", instructions=INSTRUCTIONS, version="1.0.0", **server_kwargs
    )

    def guarded(scope: str, *, write: bool = False):
        """Scope check + rate limit + audit + error mapping around a tool."""

        def deco(fn):
            @functools.wraps(fn)
            async def wrapper(**kwargs):
                started = time.perf_counter()
                name = fn.__name__
                principal: Principal | None = None
                outcome, detail = "ok", None
                try:
                    principal = current_principal()
                    await svc.limiter.hit(f"{principal.client_id}:all", svc.settings.rate_limit_per_minute)
                    if write:
                        await svc.limiter.hit(f"{principal.client_id}:write", svc.settings.rate_limit_writes_per_minute)
                    principal.require(scope)
                    return await fn(principal, **kwargs)
                except PermissionDenied as exc:
                    outcome, detail = "denied", str(exc)
                    raise ToolError(f"Permission denied: {exc}") from exc
                except RateLimited as exc:
                    outcome, detail = "rate_limited", str(exc)
                    raise ToolError(str(exc)) from exc
                except (NotFound, InvalidRequest) as exc:
                    outcome, detail = "error", str(exc)
                    raise ToolError(str(exc)) from exc
                except OdooUnavailable as exc:
                    outcome, detail = "error", str(exc)
                    raise ToolError("The ERP is temporarily unreachable. Try again in a minute.") from exc
                except OdooError as exc:
                    outcome, detail = "error", str(exc)
                    raise ToolError(f"The ERP rejected the request: {exc}") from exc
                except ToolError as exc:
                    outcome, detail = "error", str(exc)
                    raise
                finally:
                    await svc.audit.record(
                        "tool_call",
                        outcome,
                        client_id=principal.client_id if principal else None,
                        subject=principal.subject if principal else None,
                        tool=name,
                        arguments={k: _jsonable(v) for k, v in kwargs.items()},
                        detail=detail,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                    )

            # The SDK builds the input schema from the signature: drop `principal`.
            sig = inspect.signature(fn)
            wrapper.__signature__ = sig.replace(parameters=[p for n, p in sig.parameters.items() if n != "principal"])
            del wrapper.__wrapped__
            return wrapper

        return deco

    async def scope_warehouses(p: Principal, requested: str | None) -> list[str]:
        codes = [w.code for w in await svc.gateway.warehouses()]
        if requested:
            code = requested.upper()
            if code not in codes:
                raise NotFound(f"Unknown warehouse '{requested}'. Known: {', '.join(codes)}")
            if not p.can_see_warehouse(code):
                raise PermissionDenied(f"'{p.client_id}' may not access warehouse {code}")
            return [code]
        return [c for c in codes if p.can_see_warehouse(c)]

    # ---------------------------------------------------------------- reads
    @mcp.tool(annotations=READ)
    @guarded("stock:read")
    async def list_warehouses(principal: Principal) -> list[Warehouse]:
        """List the warehouses you can see, with their codes and roles (factory, distribution centre, store)."""
        allowed = set(await scope_warehouses(principal, None))
        return [w for w in await svc.gateway.warehouses() if w.code in allowed]

    @mcp.tool(annotations=READ)
    @guarded("stock:read")
    async def search_products(
        principal: Principal,
        query: Annotated[str, Field(description="Part of a SKU or product name, e.g. 'wok' or 'TH-KS'")],
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> list[Product]:
        """Find products by SKU or name. Shows price, cost, category and whether it is made or bought."""
        return await svc.gateway.search_products(query, limit)

    @mcp.tool(annotations=READ)
    @guarded("stock:read")
    async def get_stock_by_warehouse(
        principal: Principal,
        sku: Annotated[str | None, Field(description="Exact SKU, e.g. TH-SA-6104")] = None,
        query: Annotated[
            str | None, Field(description="Product name or SKU fragment when the exact SKU is unknown")
        ] = None,
        warehouse: Annotated[
            str | None, Field(description="Warehouse code; omit for every warehouse you can see")
        ] = None,
    ) -> list[StockLevel]:
        """Stock on hand, reserved, available, incoming and outgoing per warehouse, with reorder min/max.

        `below_min` is true when the forecast (on hand + incoming - outgoing) is under the reorder minimum.
        """
        if not sku and not query and not warehouse:
            raise ToolError("Give a sku, a query or a warehouse; the full stock list is too large to return.")
        return await svc.gateway.stock_levels(
            await scope_warehouses(principal, warehouse), [sku] if sku else None, query
        )

    @mcp.tool(annotations=READ)
    @guarded("stock:read")
    async def get_reorder_suggestions(
        principal: Principal,
        warehouse: Annotated[str | None, Field(description="Warehouse code; omit for all you can see")] = None,
    ) -> list[ReorderSuggestion]:
        """Products whose forecast stock is below the reorder minimum, with a suggested quantity and where to
        replenish from (the DC for stores, the factory for made goods, or a supplier)."""
        return await svc.gateway.reorder_suggestions(await scope_warehouses(principal, warehouse))

    @mcp.tool(annotations=READ)
    @guarded("stock:read")
    async def list_transfers(
        principal: Principal,
        state: Annotated[Literal["open", "done", "cancel"] | None, Field(description="'open' = not yet done")] = "open",
        warehouse: str | None = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> list[Transfer]:
        """Inter-warehouse transfers (DC to stores, factory to DC)."""
        return await svc.gateway.transfers(await scope_warehouses(principal, warehouse), state, limit)

    @mcp.tool(annotations=READ)
    @guarded("finance:read")
    async def list_open_invoices(
        principal: Principal,
        customer: Annotated[str | None, Field(description="Customer name or reference, e.g. CUS-DBT")] = None,
        overdue_only: bool = False,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> list[Invoice]:
        """Unpaid or partly paid customer invoices, oldest due date first, with days overdue."""
        return await svc.gateway.open_invoices(customer, overdue_only, limit)

    @mcp.tool(annotations=READ)
    @guarded("finance:read")
    async def get_customer_balance(
        principal: Principal,
        customer: Annotated[str, Field(description="Customer name or reference")],
    ) -> CustomerBalance:
        """Receivable, overdue amount, credit limit and credit-hold status for one customer."""
        return await svc.gateway.customer_balance(customer)

    @mcp.tool(annotations=READ)
    @guarded("sales:read")
    async def get_sales_by_period(
        principal: Principal,
        date_from: date,
        date_to: date,
        group_by: Literal["product", "category", "warehouse", "channel", "customer", "month", "week"] = "product",
    ) -> SalesReport:
        """Confirmed sales revenue (MYR, before tax), quantity and order count for a date range, grouped.

        Channels are Wholesale, Online and Retail Stores. Store-scoped callers only see their store.
        """
        if date_to < date_from:
            raise ToolError("date_to is before date_from")
        if (date_to - date_from).days > 731:
            raise ToolError("Ranges longer than two years are not supported")
        return await svc.gateway.sales_by_period(date_from, date_to, group_by, await scope_warehouses(principal, None))

    @mcp.tool(annotations=READ)
    @guarded("purchasing:read")
    async def list_purchase_orders(
        principal: Principal,
        supplier: str | None = None,
        status: Annotated[
            Literal["to_receive", "to_bill", "draft", "purchase", "done", "cancel"] | None,
            Field(description="to_receive = goods still expected; to_bill = received but no supplier bill yet"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 30,
    ) -> list[PurchaseOrder]:
        """Purchase orders, newest first, with receipt and billing status (no lines; use get_purchase_order)."""
        return await svc.gateway.purchase_orders(supplier, status, limit)

    @mcp.tool(annotations=READ)
    @guarded("purchasing:read")
    async def get_purchase_order(
        principal: Principal,
        po_number: Annotated[str, Field(description="e.g. P00012")],
    ) -> PurchaseOrder:
        """One purchase order with lines (ordered / received / billed quantities, price, tax) and its receipts.
        This is what a 3-way match checks a supplier invoice against."""
        return await svc.gateway.purchase_order(po_number)

    @mcp.tool(annotations=READ)
    @guarded("manufacturing:read")
    async def list_manufacturing_orders(
        principal: Principal,
        state: Literal["draft", "confirmed", "progress", "done", "cancel"] | None = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> list[ManufacturingOrder]:
        """Manufacturing orders at the Shah Alam factory, with component availability."""
        return await svc.gateway.manufacturing_orders(state, limit)

    @mcp.tool(annotations=READ)
    @guarded("manufacturing:read")
    async def get_bill_of_materials(principal: Principal, sku: str) -> list[BomLine]:
        """Components and quantity per unit for a manufactured product."""
        return await svc.gateway.bill_of_materials(sku)

    # -------------------------------------------------------- write queue
    def check_callback(url: str | None) -> str | None:
        if url and not any(url.startswith(p) for p in svc.settings.callback_allowed_prefixes):
            raise ToolError("callback_url is not on the bridge's allowlist")
        return url

    async def queue(
        principal: Principal,
        action_type: str,
        validated: dict,
        reason: str,
        idempotency_key: str | None,
        callback_url: str | None,
    ) -> ActionView:
        return await svc.actions.propose(
            principal,
            action_type,
            validated,
            reason,
            idempotency_key=idempotency_key,
            callback_url=check_callback(callback_url),
        )

    @mcp.tool(annotations=PROPOSE)
    @guarded("purchase:propose", write=True)
    async def propose_purchase_order(
        principal: Principal,
        supplier: Annotated[str, Field(description="Supplier name or reference, e.g. SUP-PAS")],
        warehouse: Annotated[
            str, Field(description="Receiving warehouse code (FAC for raw materials, DC for traded goods)")
        ],
        lines: Annotated[list[PricedLine], Field(min_length=1, max_length=50)],
        reason: Annotated[str, Field(min_length=5, description="Why; the approver reads this")],
        idempotency_key: IdemKey = None,
        callback_url: Callback = None,
    ) -> ActionView:
        """Queue a purchase order for human approval. Nothing is written to the ERP until a person approves.
        Unit prices default to the supplier's price list."""
        await scope_warehouses(principal, warehouse)
        p = PurchaseOrderProposal(supplier=supplier, warehouse=warehouse, lines=lines, reason=reason)
        return await queue(
            principal,
            "purchase_order",
            await svc.gateway.validate_purchase_order(p),
            reason,
            idempotency_key,
            callback_url,
        )

    @mcp.tool(annotations=PROPOSE)
    @guarded("transfer:propose", write=True)
    async def propose_stock_transfer(
        principal: Principal,
        from_warehouse: str,
        to_warehouse: str,
        lines: Annotated[list[Line], Field(min_length=1, max_length=50)],
        reason: Annotated[str, Field(min_length=5)],
        idempotency_key: IdemKey = None,
        callback_url: Callback = None,
    ) -> ActionView:
        """Queue an inter-warehouse transfer (e.g. DC -> KLCC) for approval. Checks the source has the stock."""
        await scope_warehouses(principal, to_warehouse)
        p = TransferProposal(from_warehouse=from_warehouse, to_warehouse=to_warehouse, lines=lines, reason=reason)
        return await queue(
            principal, "transfer", await svc.gateway.validate_transfer(p), reason, idempotency_key, callback_url
        )

    @mcp.tool(annotations=PROPOSE)
    @guarded("manufacturing:propose", write=True)
    async def propose_manufacturing_order(
        principal: Principal,
        sku: str,
        quantity: Annotated[float, Field(gt=0, le=100_000)],
        reason: Annotated[str, Field(min_length=5)],
        idempotency_key: IdemKey = None,
        callback_url: Callback = None,
    ) -> ActionView:
        """Queue a manufacturing order at the Shah Alam factory for approval. Only for products with a BOM."""
        p = ManufacturingProposal(sku=sku, quantity=quantity, reason=reason)
        return await queue(
            principal,
            "manufacturing_order",
            await svc.gateway.validate_manufacturing_order(p),
            reason,
            idempotency_key,
            callback_url,
        )

    @mcp.tool(annotations=PROPOSE)
    @guarded("vendor_bill:propose", write=True)
    async def propose_vendor_bill(
        principal: Principal,
        po_number: str,
        supplier_invoice_ref: str,
        invoice_date: date,
        lines: Annotated[list[BillLine], Field(min_length=1)],
        amount_untaxed: float,
        amount_tax: float,
        amount_total: float,
        match_evidence: Annotated[dict, Field(description="The caller's 3-way match result, kept for audit")] = {},  # noqa: B006
        idempotency_key: IdemKey = None,
        callback_url: Callback = None,
    ) -> ActionView:
        """Queue a supplier invoice to be posted as a vendor bill against its purchase order."""
        p = VendorBillProposal(
            po_number=po_number,
            supplier_invoice_ref=supplier_invoice_ref,
            invoice_date=invoice_date,
            lines=lines,
            amount_untaxed=amount_untaxed,
            amount_tax=amount_tax,
            amount_total=amount_total,
            match_evidence=match_evidence,
        )
        return await queue(
            principal,
            "vendor_bill",
            await svc.gateway.validate_vendor_bill(p),
            p.reason,
            idempotency_key or f"bill:{supplier_invoice_ref}",
            callback_url,
        )

    @mcp.tool(annotations=READ)
    @guarded("stock:read")
    async def get_action_status(principal: Principal, action_id: str) -> ActionView:
        """Status of an action you proposed: pending, approved, executed (with the ERP reference), rejected, failed."""
        try:
            view = await svc.actions.get(action_id)
        except ActionNotFound as exc:
            raise ToolError(f"No action {action_id}") from exc
        if view.requested_by != principal.client_id:
            raise ToolError(f"No action {action_id}")  # don't reveal other clients' actions
        return view

    @mcp.tool(annotations=READ)
    @guarded("stock:read")
    async def list_my_actions(
        principal: Principal,
        status: Literal["pending", "approved", "executing", "executed", "rejected", "failed"] | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> list[ActionView]:
        """Actions you have proposed, newest first."""
        return await svc.actions.list(status=status, limit=limit, requested_by=principal.client_id)

    return mcp


def _jsonable(v: Any) -> Any:
    if hasattr(v, "model_dump"):
        return v.model_dump(mode="json")
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if isinstance(v, date):
        return v.isoformat()
    return v
