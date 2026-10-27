"""In-memory stand-in for OdooGateway so unit tests run without Odoo."""

from __future__ import annotations

from datetime import date

from erp_bridge.domain import (
    BomLine,
    CustomerBalance,
    Invoice,
    ManufacturingOrder,
    ManufacturingProposal,
    Product,
    PurchaseOrder,
    PurchaseOrderProposal,
    ReorderSuggestion,
    SalesReport,
    SalesRow,
    StockLevel,
    Transfer,
    TransferProposal,
    VendorBillProposal,
    Warehouse,
)
from erp_bridge.odoo.client import OdooError, OdooUnavailable
from erp_bridge.odoo.gateway import InvalidRequest, NotFound, WriteResult

WAREHOUSES = [
    Warehouse(code="DC", name="Port Klang Distribution Centre", role="distribution centre"),
    Warehouse(code="FAC", name="Shah Alam Factory", role="factory"),
    Warehouse(code="KLCC", name="Store - KLCC", role="retail store"),
    Warehouse(code="PJ", name="Store - Petaling Jaya", role="retail store"),
    Warehouse(code="PNG", name="Store - Penang", role="retail store"),
]

STOCK = {
    ("KLCC", "TH-SA-6104"): 2,
    ("PJ", "TH-SA-6104"): 0,
    ("DC", "TH-SA-6104"): 575,
    ("DC", "TH-CW-2401"): 178,
    ("KLCC", "TH-CW-2401"): 35,
}
NAMES = {"TH-SA-6104": "Air Fryer 4.5L", "TH-CW-2401": "Cast Aluminium Wok 32cm", "RM-AL-INGOT": "Aluminium Ingot A356"}


class FakeGateway:
    def __init__(self) -> None:
        self.today = date(2026, 10, 27)
        self.records: dict[str, WriteResult] = {}  # origin -> record
        self.writes = 0
        self.fail_next: Exception | None = None
        self.crash_after_write = False

    async def warehouses(self) -> list[Warehouse]:
        return list(WAREHOUSES)

    async def search_products(self, query: str, limit: int = 20) -> list[Product]:
        return [
            Product(sku=s, name=n, category="X", list_price=1, cost=1, uom="Units", source="buy")
            for s, n in NAMES.items()
            if query.lower() in (s + n).lower()
        ][:limit]

    async def stock_levels(self, warehouses, skus, query) -> list[StockLevel]:
        out = []
        for (wh, sku), qty in STOCK.items():
            if wh in warehouses and (not skus or sku in skus) and (not query or query.lower() in NAMES[sku].lower()):
                out.append(
                    StockLevel(
                        sku=sku,
                        product=NAMES[sku],
                        warehouse=wh,
                        on_hand=qty,
                        reserved=0,
                        available=qty,
                        incoming=0,
                        outgoing=0,
                        reorder_min=10,
                        reorder_max=40,
                        below_min=qty < 10,
                    )
                )
        return out

    async def reorder_suggestions(self, warehouses) -> list[ReorderSuggestion]:
        return [
            ReorderSuggestion(
                sku=sku,
                product=NAMES[sku],
                warehouse=wh,
                forecast=q,
                reorder_min=10,
                reorder_max=40,
                suggested_qty=40 - q,
                replenish_from="DC",
            )
            for (wh, sku), q in STOCK.items()
            if wh in warehouses and q < 10
        ]

    async def transfers(self, warehouses, state, limit) -> list[Transfer]:
        return []

    async def open_invoices(self, customer, overdue_only, limit) -> list[Invoice]:
        return [
            Invoice(
                number="INV/2026/00002",
                customer="Pasaraya Seri Mutiara Sdn Bhd",
                invoice_date=date(2026, 7, 2),
                due_date=date(2026, 8, 1),
                amount_total=7958.79,
                amount_due=7958.79,
                days_overdue=87,
            )
        ]

    async def customer_balance(self, customer) -> CustomerBalance:
        if "nobody" in customer.lower():
            raise NotFound(f"No customer matches '{customer}'")
        return CustomerBalance(
            customer="Dapur Bonda Trading",
            ref="CUS-DBT",
            receivable=21000,
            overdue=9000,
            open_invoices=3,
            oldest_overdue_days=33,
            credit_limit=20000,
            credit_available=-1000,
            on_credit_hold=True,
        )

    async def sales_by_period(self, date_from, date_to, group_by, warehouses) -> SalesReport:
        rows = [SalesRow(key=w, revenue=1000.0, quantity=10, orders=2) for w in warehouses]
        return SalesReport(
            date_from=date_from,
            date_to=date_to,
            group_by=group_by,
            total_revenue=sum(r.revenue for r in rows),
            rows=rows,
        )

    async def purchase_orders(self, supplier, status, limit) -> list[PurchaseOrder]:
        return []

    async def purchase_order(self, number) -> PurchaseOrder:
        raise NotFound(f"No purchase order '{number}'")

    async def manufacturing_orders(self, state, limit) -> list[ManufacturingOrder]:
        return []

    async def bill_of_materials(self, sku) -> list[BomLine]:
        return []

    async def validate_purchase_order(self, p: PurchaseOrderProposal) -> dict:
        if p.supplier not in ("SUP-PAS", "Perak Alloy Supplies Sdn Bhd"):
            raise NotFound(f"No supplier matches '{p.supplier}'")
        lines = [
            {"sku": line.sku, "quantity": line.quantity, "unit_price": line.unit_price or 11.2} for line in p.lines
        ]
        return {
            "payload": {"supplier": "Perak Alloy Supplies Sdn Bhd", "warehouse": p.warehouse, "lines": lines},
            "summary": f"Purchase order to Perak Alloy Supplies Sdn Bhd for {p.warehouse}",
        }

    async def validate_transfer(self, p: TransferProposal) -> dict:
        for line in p.lines:
            if STOCK.get((p.from_warehouse, line.sku), 0) < line.quantity:
                raise InvalidRequest(f"{p.from_warehouse} has not enough {line.sku}")
        return {
            "payload": {
                "from": p.from_warehouse,
                "to": p.to_warehouse,
                "lines": [line.model_dump() for line in p.lines],
            },
            "summary": f"Transfer {p.from_warehouse} -> {p.to_warehouse}",
        }

    async def validate_manufacturing_order(self, p: ManufacturingProposal) -> dict:
        return {"payload": p.model_dump(exclude={"reason"}), "summary": f"Manufacture {p.quantity:g} x {p.sku}"}

    async def validate_vendor_bill(self, p: VendorBillProposal) -> dict:
        return {"payload": p.model_dump(mode="json", exclude={"reason"}), "summary": f"Bill {p.supplier_invoice_ref}"}

    async def find_existing(self, action_type, idempotency_ref, payload):
        return self.records.get(idempotency_ref)

    async def execute(self, action_type, payload, idempotency_ref) -> WriteResult:
        existing = await self.find_existing(action_type, idempotency_ref, payload)
        if existing:
            return existing
        if self.fail_next:
            exc, self.fail_next = self.fail_next, None
            raise exc
        self.writes += 1
        record = WriteResult(model="purchase.order", id=self.writes, ref=f"P9{self.writes:04d}")
        self.records[idempotency_ref] = record
        if self.crash_after_write:
            self.crash_after_write = False
            raise OdooUnavailable("connection reset after commit")
        return record


__all__ = ["FakeGateway", "OdooError", "OdooUnavailable"]
