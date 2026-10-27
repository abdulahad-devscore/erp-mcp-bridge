"""Business-level reads and writes against Odoo.

Tools never see Odoo models, domains or ids; they talk to this gateway in
business terms (SKU, warehouse code, PO number). That keeps the tool surface
small and lets the same tools front SAP B1 or Dynamics later by swapping the
gateway implementation.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Protocol

from erp_bridge.domain import (
    BomLine,
    CustomerBalance,
    Invoice,
    ManufacturingOrder,
    ManufacturingProposal,
    POLine,
    Product,
    PurchaseOrder,
    PurchaseOrderProposal,
    Receipt,
    ReorderSuggestion,
    SalesReport,
    SalesRow,
    StockLevel,
    Transfer,
    TransferProposal,
    VendorBillProposal,
    Warehouse,
)
from erp_bridge.odoo.client import OdooClient


class NotFound(LookupError):
    """A business key (SKU, supplier, PO number...) does not exist."""


class InvalidRequest(ValueError):
    """The request is well-formed but the ERP would reject it."""


class WriteResult(dict):
    """{"model": str, "id": int, "ref": str}"""


class ErpGateway(Protocol):
    today: date

    async def warehouses(self) -> list[Warehouse]: ...
    async def search_products(self, query: str, limit: int = 20) -> list[Product]: ...
    async def stock_levels(
        self, warehouses: list[str], skus: list[str] | None, query: str | None
    ) -> list[StockLevel]: ...
    async def reorder_suggestions(self, warehouses: list[str]) -> list[ReorderSuggestion]: ...
    async def open_invoices(self, customer: str | None, overdue_only: bool, limit: int) -> list[Invoice]: ...
    async def customer_balance(self, customer: str) -> CustomerBalance: ...
    async def sales_by_period(
        self, date_from: date, date_to: date, group_by: str, warehouses: list[str]
    ) -> SalesReport: ...
    async def purchase_orders(self, supplier: str | None, status: str | None, limit: int) -> list[PurchaseOrder]: ...
    async def purchase_order(self, number: str) -> PurchaseOrder: ...
    async def manufacturing_orders(self, state: str | None, limit: int) -> list[ManufacturingOrder]: ...
    async def bill_of_materials(self, sku: str) -> list[BomLine]: ...
    async def transfers(self, warehouses: list[str], state: str | None, limit: int) -> list[Transfer]: ...

    async def validate_purchase_order(self, p: PurchaseOrderProposal) -> dict: ...
    async def validate_transfer(self, p: TransferProposal) -> dict: ...
    async def validate_manufacturing_order(self, p: ManufacturingProposal) -> dict: ...
    async def validate_vendor_bill(self, p: VendorBillProposal) -> dict: ...

    async def find_existing(self, action_type: str, idempotency_ref: str, payload: dict) -> WriteResult | None: ...
    async def execute(self, action_type: str, payload: dict, idempotency_ref: str) -> WriteResult: ...


def _m2o(value: Any) -> str | None:
    return value[1] if value else None


def _m2o_id(value: Any) -> int | None:
    return value[0] if value else None


class OdooGateway:
    def __init__(self, odoo: OdooClient, today: date | None = None):
        self.o = odoo
        self._today = today
        self._wh: dict[str, dict] | None = None

    @property
    def today(self) -> date:
        return self._today or date.today()

    # ------------------------------------------------------------ lookups
    async def _warehouse_map(self) -> dict[str, dict]:
        if self._wh is None:
            rows = await self.o.search_read(
                "stock.warehouse", [], ["code", "name", "lot_stock_id", "in_type_id", "int_type_id", "manu_type_id"]
            )
            self._wh = {r["code"]: r for r in rows}
        return self._wh

    async def _warehouse(self, code: str) -> dict:
        wh = (await self._warehouse_map()).get(code.upper())
        if not wh:
            raise NotFound(f"Unknown warehouse '{code}'. Known: {', '.join(sorted(await self._codes()))}")
        return wh

    async def _codes(self) -> list[str]:
        return list((await self._warehouse_map()).keys())

    async def _products_by_sku(self, skus: list[str]) -> dict[str, dict]:
        rows = await self.o.search_read(
            "product.product",
            [["default_code", "in", skus]],
            [
                "default_code",
                "name",
                "uom_id",
                "standard_price",
                "seller_ids",
                "bom_ids",
                "supplier_taxes_id",
                "product_tmpl_id",
            ],
        )
        found = {r["default_code"]: r for r in rows}
        missing = [s for s in skus if s not in found]
        if missing:
            raise NotFound(f"Unknown SKU(s): {', '.join(missing)}")
        return found

    async def _partner(self, name_or_ref: str, *, supplier: bool) -> dict:
        rank = "supplier_rank" if supplier else "customer_rank"
        for domain in (
            [["ref", "=ilike", name_or_ref]],
            [["name", "=ilike", name_or_ref]],
            [["name", "ilike", name_or_ref]],
        ):
            rows = await self.o.search_read(
                "res.partner",
                domain + [[rank, ">", 0], ["is_company", "=", True]],
                ["name", "ref", "credit", "credit_limit", "use_partner_credit_limit"],
                limit=5,
            )
            if len(rows) == 1:
                return rows[0]
            if len(rows) > 1:
                names = "; ".join(r["name"] for r in rows)
                raise InvalidRequest(
                    f"'{name_or_ref}' matches several {'suppliers' if supplier else 'customers'}: {names}"
                )
        raise NotFound(f"No {'supplier' if supplier else 'customer'} matches '{name_or_ref}'")

    async def warehouses(self) -> list[Warehouse]:
        roles = {"FAC": "factory", "DC": "distribution centre"}
        return [
            Warehouse(code=c, name=w["name"], role=roles.get(c, "retail store"))
            for c, w in sorted((await self._warehouse_map()).items())
        ]

    async def search_products(self, query: str, limit: int = 20) -> list[Product]:
        domain = ["|", ["default_code", "ilike", query], ["name", "ilike", query]] if query else []
        rows = await self.o.search_read(
            "product.product",
            domain + [["default_code", "!=", False]],
            ["default_code", "name", "categ_id", "list_price", "standard_price", "uom_id", "bom_ids", "seller_ids"],
            limit=limit,
            order="default_code",
        )
        seller_ids = [s for r in rows for s in r["seller_ids"]]
        sellers = {}
        if seller_ids:
            for s in await self.o.search_read("product.supplierinfo", [["id", "in", seller_ids]], ["partner_id"]):
                sellers[s["id"]] = s["partner_id"][1]
        return [
            Product(
                sku=r["default_code"],
                name=r["name"],
                category=_m2o(r["categ_id"]) or "",
                list_price=r["list_price"],
                cost=r["standard_price"],
                uom=_m2o(r["uom_id"]) or "",
                source="make" if r["bom_ids"] else "buy",
                suppliers=sorted({sellers[s] for s in r["seller_ids"] if s in sellers}),
            )
            for r in rows
        ]

    # -------------------------------------------------------------- stock
    async def stock_levels(self, warehouses: list[str], skus: list[str] | None, query: str | None) -> list[StockLevel]:
        whmap = await self._warehouse_map()
        domain: list = [["default_code", "!=", False]]
        if skus:
            domain.append(["default_code", "in", skus])
        if query:
            domain += ["|", ["default_code", "ilike", query], ["name", "ilike", query]]
        orderpoints = await self.o.search_read(
            "stock.warehouse.orderpoint", [], ["product_id", "warehouse_id", "product_min_qty", "product_max_qty"]
        )
        rules = {(op["product_id"][0], op["warehouse_id"][0]): op for op in orderpoints}
        levels: list[StockLevel] = []
        for code in warehouses:
            wh = whmap.get(code)
            if not wh:
                continue
            rows = await self.o.search_read(
                "product.product",
                domain,
                ["default_code", "name", "qty_available", "free_qty", "incoming_qty", "outgoing_qty"],
                context={"warehouse": wh["id"]},
                order="default_code",
            )
            for r in rows:
                rule = rules.get((r["id"], wh["id"]))
                if not rule and not r["qty_available"] and not r["incoming_qty"]:
                    continue  # product not stocked here
                lvl = StockLevel(
                    sku=r["default_code"],
                    product=r["name"],
                    warehouse=code,
                    on_hand=r["qty_available"],
                    reserved=round(r["qty_available"] - r["free_qty"], 3),
                    available=r["free_qty"],
                    incoming=r["incoming_qty"],
                    outgoing=r["outgoing_qty"],
                    reorder_min=rule["product_min_qty"] if rule else None,
                    reorder_max=rule["product_max_qty"] if rule else None,
                )
                lvl.below_min = (
                    bool(rule)
                    and (r["qty_available"] + r["incoming_qty"] - r["outgoing_qty"]) < rule["product_min_qty"]
                )
                levels.append(lvl)
        return levels

    async def reorder_suggestions(self, warehouses: list[str]) -> list[ReorderSuggestion]:
        whmap = await self._warehouse_map()
        ids = [whmap[c]["id"] for c in warehouses if c in whmap]
        rows = await self.o.search_read(
            "stock.warehouse.orderpoint",
            [["warehouse_id", "in", ids]],
            ["product_id", "warehouse_id", "product_min_qty", "product_max_qty", "qty_forecast"],
        )
        by_id = {w["id"]: c for c, w in whmap.items()}
        products = await self.o.search_read(
            "product.product", [["id", "in", list({r["product_id"][0] for r in rows})]], ["default_code", "bom_ids"]
        )
        info = {p["id"]: p for p in products}
        out = []
        for r in rows:
            if r["qty_forecast"] >= r["product_min_qty"]:
                continue
            code = by_id[r["warehouse_id"][0]]
            p = info[r["product_id"][0]]
            if code in ("KLCC", "PJ", "PNG"):
                source = "DC"
            elif p["bom_ids"] and code == "FAC":
                source = "manufacture"
            elif p["bom_ids"] and code == "DC":
                source = "FAC"
            else:
                source = "supplier"
            out.append(
                ReorderSuggestion(
                    sku=p["default_code"],
                    product=r["product_id"][1].split("] ", 1)[-1],
                    warehouse=code,
                    forecast=r["qty_forecast"],
                    reorder_min=r["product_min_qty"],
                    reorder_max=r["product_max_qty"],
                    suggested_qty=max(0.0, r["product_max_qty"] - r["qty_forecast"]),
                    replenish_from=source,
                )
            )
        return sorted(out, key=lambda s: (s.warehouse, s.forecast - s.reorder_min))

    async def transfers(self, warehouses: list[str], state: str | None, limit: int) -> list[Transfer]:
        whmap = await self._warehouse_map()
        stock_locs = {w["lot_stock_id"][0]: c for c, w in whmap.items()}
        domain: list = [["picking_type_code", "=", "internal"], ["location_dest_id", "in", list(stock_locs)]]
        if state == "open":
            domain.append(["state", "not in", ["done", "cancel"]])
        elif state:
            domain.append(["state", "=", state])
        rows = await self.o.search_read(
            "stock.picking",
            domain,
            ["name", "location_id", "location_dest_id", "state", "scheduled_date", "move_ids"],
            limit=limit,
            order="scheduled_date desc",
        )
        out = []
        for r in rows:
            src, dst = stock_locs.get(r["location_id"][0], "?"), stock_locs.get(r["location_dest_id"][0], "?")
            if warehouses and src not in warehouses and dst not in warehouses:
                continue
            moves = await self.o.search_read(
                "stock.move", [["id", "in", r["move_ids"]]], ["product_id", "product_uom_qty", "quantity"]
            )
            out.append(
                Transfer(
                    reference=r["name"],
                    from_warehouse=src,
                    to_warehouse=dst,
                    state=r["state"],
                    scheduled=r["scheduled_date"],
                    lines=[
                        {"product": m["product_id"][1], "quantity": m["product_uom_qty"], "done": m["quantity"]}
                        for m in moves
                    ],
                )
            )
        return out

    # ------------------------------------------------------------ finance
    async def open_invoices(self, customer: str | None, overdue_only: bool, limit: int) -> list[Invoice]:
        domain: list = [
            ["move_type", "=", "out_invoice"],
            ["state", "=", "posted"],
            ["payment_state", "in", ["not_paid", "partial"]],
        ]
        if customer:
            partner = await self._partner(customer, supplier=False)
            domain.append(["partner_id", "=", partner["id"]])
        if overdue_only:
            domain.append(["invoice_date_due", "<", self.today.isoformat()])
        rows = await self.o.search_read(
            "account.move",
            domain,
            [
                "name",
                "partner_id",
                "invoice_date",
                "invoice_date_due",
                "amount_total",
                "amount_residual",
                "invoice_origin",
            ],
            limit=limit,
            order="invoice_date_due asc",
        )
        return [self._invoice(r) for r in rows]

    def _invoice(self, r: dict) -> Invoice:
        due = date.fromisoformat(r["invoice_date_due"]) if r["invoice_date_due"] else None
        return Invoice(
            number=r["name"],
            customer=_m2o(r["partner_id"]) or "",
            invoice_date=date.fromisoformat(r["invoice_date"]) if r["invoice_date"] else None,
            due_date=due,
            amount_total=r["amount_total"],
            amount_due=r["amount_residual"],
            days_overdue=max(0, (self.today - due).days) if due else 0,
            sales_order=r.get("invoice_origin") or None,
        )

    async def customer_balance(self, customer: str) -> CustomerBalance:
        partner = await self._partner(customer, supplier=False)
        rows = await self.o.search_read(
            "account.move",
            [
                ["move_type", "=", "out_invoice"],
                ["state", "=", "posted"],
                ["payment_state", "in", ["not_paid", "partial"]],
                ["partner_id", "=", partner["id"]],
            ],
            [
                "name",
                "partner_id",
                "invoice_date",
                "invoice_date_due",
                "amount_total",
                "amount_residual",
                "invoice_origin",
            ],
        )
        invoices = [self._invoice(r) for r in rows]
        receivable = round(sum(i.amount_due for i in invoices), 2)
        overdue = [i for i in invoices if i.days_overdue > 0]
        limit = partner["credit_limit"] if partner["use_partner_credit_limit"] else None
        return CustomerBalance(
            customer=partner["name"],
            ref=partner["ref"] or None,
            receivable=receivable,
            overdue=round(sum(i.amount_due for i in overdue), 2),
            open_invoices=len(invoices),
            oldest_overdue_days=max((i.days_overdue for i in overdue), default=0),
            credit_limit=limit,
            credit_available=round(limit - receivable, 2) if limit else None,
            # House rule (see credit policy doc): hold when over limit or anything is 60+ days overdue.
            on_credit_hold=bool((limit and receivable > limit) or any(i.days_overdue >= 60 for i in overdue)),
        )

    async def sales_by_period(
        self, date_from: date, date_to: date, group_by: str, warehouses: list[str]
    ) -> SalesReport:
        field = {
            "product": "product_id",
            "category": "categ_id",
            "warehouse": "warehouse_id",
            "channel": "team_id",
            "customer": "partner_id",
            "month": "date:month",
            "week": "date:week",
        }.get(group_by)
        if not field:
            raise InvalidRequest("group_by must be one of product, category, warehouse, channel, customer, month, week")
        domain: list = [
            ["date", ">=", f"{date_from} 00:00:00"],
            ["date", "<=", f"{date_to} 23:59:59"],
            ["state", "in", ["sale", "done"]],
        ]
        whmap = await self._warehouse_map()
        if warehouses and set(warehouses) != set(whmap):
            domain.append(["warehouse_id", "in", [whmap[c]["id"] for c in warehouses if c in whmap]])
        groups = await self.o.read_group(
            "sale.report",
            domain,
            ["price_subtotal:sum", "product_uom_qty:sum", "order_reference:count_distinct"],
            [field],
            orderby=None if group_by in ("month", "week") else "price_subtotal desc",
        )
        rows = []
        for g in groups:
            key = g[field]
            label = key[1] if isinstance(key, list) else (key or "Undefined")
            rows.append(
                SalesRow(
                    key=str(label),
                    revenue=round(g["price_subtotal"] or 0, 2),
                    quantity=g["product_uom_qty"] or 0,
                    orders=g.get("order_reference") or 0,
                )
            )
        return SalesReport(
            date_from=date_from,
            date_to=date_to,
            group_by=group_by,
            total_revenue=round(sum(r.revenue for r in rows), 2),
            rows=rows,
        )

    # --------------------------------------------------------- purchasing
    _PO_FIELDS = [
        "name",
        "partner_id",
        "partner_ref",
        "picking_type_id",
        "state",
        "date_order",
        "amount_untaxed",
        "amount_total",
        "receipt_status",
        "invoice_status",
        "order_line",
        "picking_ids",
    ]

    async def purchase_orders(self, supplier: str | None, status: str | None, limit: int) -> list[PurchaseOrder]:
        domain: list = []
        if supplier:
            domain.append(["partner_id", "=", (await self._partner(supplier, supplier=True))["id"]])
        if status == "to_receive":
            domain += [["state", "=", "purchase"], ["receipt_status", "in", ["pending", "partial"]]]
        elif status == "to_bill":
            domain += [["state", "=", "purchase"], ["invoice_status", "=", "to invoice"]]
        elif status:
            domain.append(["state", "=", status])
        rows = await self.o.search_read("purchase.order", domain, self._PO_FIELDS, limit=limit, order="date_order desc")
        return [await self._po(r, with_lines=False) for r in rows]

    async def purchase_order(self, number: str) -> PurchaseOrder:
        rows = await self.o.search_read("purchase.order", [["name", "=ilike", number.strip()]], self._PO_FIELDS)
        if not rows:
            raise NotFound(f"No purchase order '{number}'")
        return await self._po(rows[0], with_lines=True)

    async def _po(self, r: dict, *, with_lines: bool) -> PurchaseOrder:
        whmap = await self._warehouse_map()
        in_types = {w["in_type_id"][0]: c for c, w in whmap.items()}
        po = PurchaseOrder(
            number=r["name"],
            supplier=_m2o(r["partner_id"]) or "",
            supplier_ref=r["partner_ref"] or None,
            warehouse=in_types.get(_m2o_id(r["picking_type_id"])),
            state=r["state"],
            order_date=r["date_order"],
            amount_untaxed=r["amount_untaxed"],
            amount_total=r["amount_total"],
            receipt_status=r.get("receipt_status") or None,
            billing_status=r["invoice_status"],
        )
        if with_lines:
            lines = await self.o.search_read(
                "purchase.order.line",
                [["id", "in", r["order_line"]]],
                ["product_id", "product_qty", "qty_received", "qty_invoiced", "price_unit", "taxes_id", "product_uom"],
            )
            tax_ids = list({t for line in lines for t in line["taxes_id"]})
            taxes = (
                {
                    t["id"]: t["amount"]
                    for t in await self.o.search_read("account.tax", [["id", "in", tax_ids]], ["amount"])
                }
                if tax_ids
                else {}
            )
            skus = {
                p["id"]: p["default_code"]
                for p in await self.o.search_read(
                    "product.product", [["id", "in", [line["product_id"][0] for line in lines]]], ["default_code"]
                )
            }
            po.lines = [
                POLine(
                    line_id=line["id"],
                    sku=skus.get(line["product_id"][0], ""),
                    product=line["product_id"][1],
                    qty_ordered=line["product_qty"],
                    qty_received=line["qty_received"],
                    qty_billed=line["qty_invoiced"],
                    unit_price=line["price_unit"],
                    tax_percent=sum(taxes.get(t, 0) for t in line["taxes_id"]),
                    uom=_m2o(line["product_uom"]) or "",
                )
                for line in lines
            ]
            picks = await self.o.search_read(
                "stock.picking", [["id", "in", r["picking_ids"]]], ["name", "state", "date_done"]
            )
            po.receipts = [
                Receipt(reference=p["name"], state=p["state"], date_done=p["date_done"] or None) for p in picks
            ]
        return po

    # ------------------------------------------------------ manufacturing
    async def manufacturing_orders(self, state: str | None, limit: int) -> list[ManufacturingOrder]:
        domain = [["state", "=", state]] if state else []
        rows = await self.o.search_read(
            "mrp.production",
            domain,
            ["name", "product_id", "product_qty", "state", "date_start", "components_availability"],
            limit=limit,
            order="date_start desc",
        )
        skus = (
            {
                p["id"]: p["default_code"]
                for p in await self.o.search_read(
                    "product.product", [["id", "in", [r["product_id"][0] for r in rows]]], ["default_code"]
                )
            }
            if rows
            else {}
        )
        return [
            ManufacturingOrder(
                number=r["name"],
                sku=skus.get(r["product_id"][0], ""),
                product=r["product_id"][1],
                quantity=r["product_qty"],
                state=r["state"],
                scheduled=r["date_start"] or None,
                components_available=r.get("components_availability") or None,
            )
            for r in rows
        ]

    async def bill_of_materials(self, sku: str) -> list[BomLine]:
        product = (await self._products_by_sku([sku]))[sku]
        boms = await self.o.search_read(
            "mrp.bom",
            [["product_tmpl_id", "=", product["product_tmpl_id"][0]]],
            ["bom_line_ids", "product_qty"],
            limit=1,
        )
        if not boms:
            raise NotFound(f"{sku} has no bill of materials (it is bought in, not made)")
        lines = await self.o.search_read(
            "mrp.bom.line", [["id", "in", boms[0]["bom_line_ids"]]], ["product_id", "product_qty", "product_uom_id"]
        )
        skus = {
            p["id"]: p["default_code"]
            for p in await self.o.search_read(
                "product.product", [["id", "in", [line["product_id"][0] for line in lines]]], ["default_code"]
            )
        }
        per = boms[0]["product_qty"] or 1
        return [
            BomLine(
                sku=skus[line["product_id"][0]],
                component=line["product_id"][1].split("] ", 1)[-1],
                qty_per_unit=line["product_qty"] / per,
                uom=_m2o(line["product_uom_id"]) or "",
            )
            for line in lines
        ]

    # ---------------------------------------------- proposal validation
    # Each validate_* resolves business keys to Odoo ids, checks the request
    # is something Odoo will accept, and returns the payload the executor
    # needs plus a one-line summary for the approver.

    async def validate_purchase_order(self, p: PurchaseOrderProposal) -> dict:
        supplier = await self._partner(p.supplier, supplier=True)
        wh = await self._warehouse(p.warehouse)
        products = await self._products_by_sku([line.sku for line in p.lines])
        seller_rows = await self.o.search_read(
            "product.supplierinfo", [["partner_id", "=", supplier["id"]]], ["product_tmpl_id", "price"]
        )
        price_list = {s["product_tmpl_id"][0]: s["price"] for s in seller_rows}
        lines, total = [], 0.0
        for line in p.lines:
            prod = products[line.sku]
            price = line.unit_price or price_list.get(prod["product_tmpl_id"][0])
            if price is None:
                raise InvalidRequest(f"{supplier['name']} has no price for {line.sku}; pass unit_price explicitly")
            lines.append(
                {
                    "sku": line.sku,
                    "product_id": prod["id"],
                    "name": prod["name"],
                    "quantity": line.quantity,
                    "unit_price": price,
                    "uom_id": prod["uom_id"][0],
                    "uom": prod["uom_id"][1],
                }
            )
            total += line.quantity * price
        detail = ", ".join(
            f"{line['quantity']:g} {line['uom']} {line['sku']} @ {line['unit_price']:.2f}" for line in lines
        )
        return {
            "payload": {
                "supplier_id": supplier["id"],
                "supplier": supplier["name"],
                "warehouse": p.warehouse.upper(),
                "picking_type_id": wh["in_type_id"][0],
                "lines": lines,
                "amount_untaxed": round(total, 2),
            },
            "summary": f"Purchase order to {supplier['name']} for {p.warehouse.upper()}: {detail} "
            f"(RM {total:,.2f} before tax)",
        }

    async def validate_transfer(self, p: TransferProposal) -> dict:
        src, dst = await self._warehouse(p.from_warehouse), await self._warehouse(p.to_warehouse)
        products = await self._products_by_sku([line.sku for line in p.lines])
        levels = {
            s.sku: s for s in await self.stock_levels([p.from_warehouse.upper()], [line.sku for line in p.lines], None)
        }
        lines = []
        for line in p.lines:
            avail = levels[line.sku].available if line.sku in levels else 0
            if line.quantity > avail:
                raise InvalidRequest(
                    f"{p.from_warehouse.upper()} has only {avail:g} of {line.sku} available, "
                    f"cannot transfer {line.quantity:g}"
                )
            prod = products[line.sku]
            lines.append(
                {
                    "sku": line.sku,
                    "product_id": prod["id"],
                    "name": prod["name"],
                    "quantity": line.quantity,
                    "uom_id": prod["uom_id"][0],
                }
            )
        detail = ", ".join(f"{line['quantity']:g} x {line['sku']}" for line in lines)
        return {
            "payload": {
                "from": p.from_warehouse.upper(),
                "to": p.to_warehouse.upper(),
                "picking_type_id": src["int_type_id"][0],
                "location_id": src["lot_stock_id"][0],
                "location_dest_id": dst["lot_stock_id"][0],
                "lines": lines,
            },
            "summary": f"Transfer {p.from_warehouse.upper()} -> {p.to_warehouse.upper()}: {detail}",
        }

    async def validate_manufacturing_order(self, p: ManufacturingProposal) -> dict:
        prod = (await self._products_by_sku([p.sku]))[p.sku]
        if not prod["bom_ids"]:
            raise InvalidRequest(f"{p.sku} is bought in, not manufactured; propose a purchase order instead")
        fac = await self._warehouse("FAC")
        return {
            "payload": {
                "sku": p.sku,
                "product_id": prod["id"],
                "bom_id": prod["bom_ids"][0],
                "quantity": p.quantity,
                "picking_type_id": fac["manu_type_id"][0],
            },
            "summary": f"Manufacture {p.quantity:g} x {p.sku} {prod['name']} at Shah Alam Factory",
        }

    async def validate_vendor_bill(self, p: VendorBillProposal) -> dict:
        po = await self.purchase_order(p.po_number)
        rows = await self.o.search_read("purchase.order", [["name", "=", po.number]], ["partner_id"])
        by_sku = {line.sku: line for line in po.lines}
        lines = []
        for bl in p.lines:
            pol = by_sku.get(bl.sku)
            if not pol:
                raise InvalidRequest(f"{bl.sku} is not on {po.number}")
            lines.append(
                {"sku": bl.sku, "purchase_line_id": pol.line_id, "quantity": bl.quantity, "unit_price": bl.unit_price}
            )
        return {
            "payload": {
                "po_number": po.number,
                "supplier_id": rows[0]["partner_id"][0],
                "supplier": po.supplier,
                "supplier_invoice_ref": p.supplier_invoice_ref,
                "invoice_date": p.invoice_date.isoformat(),
                "lines": lines,
                "amount_untaxed": p.amount_untaxed,
                "amount_tax": p.amount_tax,
                "amount_total": p.amount_total,
                "match_evidence": p.match_evidence,
            },
            "summary": f"Vendor bill {p.supplier_invoice_ref} from {po.supplier} against {po.number}: "
            f"RM {p.amount_total:,.2f}",
        }

    # ----------------------------------------------------------- writes
    async def find_existing(self, action_type: str, idempotency_ref: str, payload: dict) -> WriteResult | None:
        """Look for a record this action already created (crash between write and commit)."""
        if action_type == "vendor_bill":
            rows = await self.o.search_read(
                "account.move",
                [
                    ["move_type", "=", "in_invoice"],
                    ["partner_id", "=", payload["supplier_id"]],
                    ["ref", "=", payload["supplier_invoice_ref"]],
                    ["state", "!=", "cancel"],
                ],
                ["name"],
            )
            return WriteResult(model="account.move", id=rows[0]["id"], ref=rows[0]["name"]) if rows else None
        model = {
            "purchase_order": "purchase.order",
            "transfer": "stock.picking",
            "manufacturing_order": "mrp.production",
        }[action_type]
        rows = await self.o.search_read(model, [["origin", "=", idempotency_ref]], ["name"])
        return WriteResult(model=model, id=rows[0]["id"], ref=rows[0]["name"]) if rows else None

    async def execute(self, action_type: str, payload: dict, idempotency_ref: str) -> WriteResult:
        existing = await self.find_existing(action_type, idempotency_ref, payload)
        if existing:
            return existing
        handler = {
            "purchase_order": self._create_po,
            "transfer": self._create_transfer,
            "manufacturing_order": self._create_mo,
            "vendor_bill": self._create_bill,
        }[action_type]
        return await handler(payload, idempotency_ref)

    async def _create_po(self, p: dict, origin: str) -> WriteResult:
        planned = (self.today + timedelta(days=7)).isoformat() + " 10:00:00"
        po_id = await self.o.create(
            "purchase.order",
            {
                "partner_id": p["supplier_id"],
                "picking_type_id": p["picking_type_id"],
                "origin": origin,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": line["product_id"],
                            "product_qty": line["quantity"],
                            "price_unit": line["unit_price"],
                            "date_planned": planned,
                        },
                    )
                    for line in p["lines"]
                ],
            },
        )
        await self.o.execute("purchase.order", "button_confirm", [po_id])
        name = (await self.o.search_read("purchase.order", [["id", "=", po_id]], ["name"]))[0]["name"]
        return WriteResult(model="purchase.order", id=po_id, ref=name)

    async def _create_transfer(self, p: dict, origin: str) -> WriteResult:
        pick_id = await self.o.create(
            "stock.picking",
            {
                "picking_type_id": p["picking_type_id"],
                "location_id": p["location_id"],
                "location_dest_id": p["location_dest_id"],
                "origin": origin,
                "move_ids": [
                    (
                        0,
                        0,
                        {
                            "name": line["name"],
                            "product_id": line["product_id"],
                            "product_uom_qty": line["quantity"],
                            "product_uom": line["uom_id"],
                            "location_id": p["location_id"],
                            "location_dest_id": p["location_dest_id"],
                        },
                    )
                    for line in p["lines"]
                ],
            },
        )
        await self.o.execute("stock.picking", "action_confirm", [pick_id])
        await self.o.execute("stock.picking", "action_assign", [pick_id])
        name = (await self.o.search_read("stock.picking", [["id", "=", pick_id]], ["name"]))[0]["name"]
        return WriteResult(model="stock.picking", id=pick_id, ref=name)

    async def _create_mo(self, p: dict, origin: str) -> WriteResult:
        mo_id = await self.o.create(
            "mrp.production",
            {
                "product_id": p["product_id"],
                "product_qty": p["quantity"],
                "bom_id": p["bom_id"],
                "picking_type_id": p["picking_type_id"],
                "origin": origin,
            },
        )
        await self.o.execute("mrp.production", "action_confirm", [mo_id])
        name = (await self.o.search_read("mrp.production", [["id", "=", mo_id]], ["name"]))[0]["name"]
        return WriteResult(model="mrp.production", id=mo_id, ref=name)

    async def _create_bill(self, p: dict, origin: str) -> WriteResult:
        pol_ids = [line["purchase_line_id"] for line in p["lines"]]
        pols = {
            line["id"]: line
            for line in await self.o.search_read(
                "purchase.order.line", [["id", "in", pol_ids]], ["product_id", "taxes_id", "name"]
            )
        }
        move_id = await self.o.create(
            "account.move",
            {
                "move_type": "in_invoice",
                "partner_id": p["supplier_id"],
                "ref": p["supplier_invoice_ref"],
                "invoice_date": p["invoice_date"],
                "invoice_origin": p["po_number"],
                "invoice_line_ids": [
                    (
                        0,
                        0,
                        {
                            "product_id": pols[line["purchase_line_id"]]["product_id"][0],
                            "name": pols[line["purchase_line_id"]]["name"],
                            "quantity": line["quantity"],
                            "price_unit": line["unit_price"],
                            "purchase_line_id": line["purchase_line_id"],
                            "tax_ids": [(6, 0, pols[line["purchase_line_id"]]["taxes_id"])],
                        },
                    )
                    for line in p["lines"]
                ],
            },
        )
        await self.o.execute("account.move", "action_post", [move_id])
        row = (await self.o.search_read("account.move", [["id", "=", move_id]], ["name", "amount_total"]))[0]
        if abs(row["amount_total"] - p["amount_total"]) > 0.05:
            # Posted, but flag it: the approver saw a different total.
            return WriteResult(
                model="account.move",
                id=move_id,
                ref=row["name"],
                warning=f"Odoo computed RM {row['amount_total']:.2f}, invoice said RM {p['amount_total']:.2f}",
            )
        return WriteResult(model="account.move", id=move_id, ref=row["name"])


def group_stock(levels: list[StockLevel]) -> dict[str, list[StockLevel]]:
    out: dict[str, list[StockLevel]] = defaultdict(list)
    for lvl in levels:
        out[lvl.sku].append(lvl)
    return out
