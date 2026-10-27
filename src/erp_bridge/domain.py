"""Typed shapes for everything that crosses the bridge.

Tool inputs are strict (`extra="forbid"`) so a model that invents a field gets
a validation error instead of a silently ignored argument.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ reads
class Warehouse(BaseModel):
    code: str
    name: str
    role: str


class StockLevel(BaseModel):
    sku: str
    product: str
    warehouse: str
    on_hand: float
    reserved: float
    available: float
    incoming: float
    outgoing: float
    reorder_min: float | None = None
    reorder_max: float | None = None
    below_min: bool = False


class ReorderSuggestion(BaseModel):
    sku: str
    product: str
    warehouse: str
    forecast: float
    reorder_min: float
    reorder_max: float
    suggested_qty: float
    replenish_from: str  # "supplier", "manufacture" or a warehouse code


class Product(BaseModel):
    sku: str
    name: str
    category: str
    list_price: float
    cost: float
    uom: str
    source: Literal["make", "buy"]
    suppliers: list[str] = []


class Invoice(BaseModel):
    number: str
    customer: str
    invoice_date: date | None
    due_date: date | None
    amount_total: float
    amount_due: float
    days_overdue: int
    sales_order: str | None = None


class CustomerBalance(BaseModel):
    customer: str
    ref: str | None
    receivable: float
    overdue: float
    open_invoices: int
    oldest_overdue_days: int
    credit_limit: float | None
    credit_available: float | None
    on_credit_hold: bool


class SalesRow(BaseModel):
    key: str
    revenue: float
    quantity: float
    orders: int


class SalesReport(BaseModel):
    date_from: date
    date_to: date
    group_by: str
    currency: str = "MYR"
    total_revenue: float
    rows: list[SalesRow]


class POLine(BaseModel):
    line_id: int
    sku: str
    product: str
    qty_ordered: float
    qty_received: float
    qty_billed: float
    unit_price: float
    tax_percent: float
    uom: str


class Receipt(BaseModel):
    reference: str
    state: str
    date_done: str | None


class PurchaseOrder(BaseModel):
    number: str
    supplier: str
    supplier_ref: str | None
    warehouse: str | None
    state: str
    order_date: str | None
    amount_untaxed: float
    amount_total: float
    receipt_status: str | None
    billing_status: str | None
    lines: list[POLine] = []
    receipts: list[Receipt] = []


class ManufacturingOrder(BaseModel):
    number: str
    sku: str
    product: str
    quantity: float
    state: str
    scheduled: str | None
    components_available: str | None


class BomLine(BaseModel):
    sku: str
    component: str
    qty_per_unit: float
    uom: str


class Transfer(BaseModel):
    reference: str
    from_warehouse: str
    to_warehouse: str
    state: str
    scheduled: str | None
    lines: list[dict]


# ----------------------------------------------------------- write inputs
class Line(Strict):
    sku: str = Field(description="Product internal reference, e.g. TH-CW-2401")
    quantity: float = Field(gt=0, le=1_000_000)


class PricedLine(Line):
    unit_price: float | None = Field(default=None, gt=0, description="Defaults to the supplier's price list")


class PurchaseOrderProposal(Strict):
    supplier: str = Field(description="Supplier name or reference (e.g. SUP-PAS)")
    warehouse: str = Field(description="Receiving warehouse code: FAC, DC, KLCC, PJ or PNG")
    lines: list[PricedLine] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=5, max_length=1000, description="Why this order is needed; shown to the approver")


class TransferProposal(Strict):
    from_warehouse: str
    to_warehouse: str
    lines: list[Line] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=5, max_length=1000)

    @field_validator("to_warehouse")
    @classmethod
    def _different(cls, v: str, info):  # noqa: ANN001
        if v.upper() == str(info.data.get("from_warehouse", "")).upper():
            raise ValueError("from_warehouse and to_warehouse must differ")
        return v


class ManufacturingProposal(Strict):
    sku: str
    quantity: float = Field(gt=0, le=100_000)
    reason: str = Field(min_length=5, max_length=1000)


class BillLine(Strict):
    sku: str
    quantity: float = Field(gt=0)
    unit_price: float = Field(ge=0)


class VendorBillProposal(Strict):
    po_number: str
    supplier_invoice_ref: str = Field(min_length=1, max_length=64)
    invoice_date: date
    lines: list[BillLine] = Field(min_length=1)
    amount_untaxed: float
    amount_tax: float
    amount_total: float
    match_evidence: dict = Field(default_factory=dict, description="3-way match result from the caller")
    reason: str = "Supplier invoice matched to purchase order and goods receipt"


ActionType = Literal["purchase_order", "transfer", "manufacturing_order", "vendor_bill"]


class ActionView(BaseModel):
    """What callers (agents, the approval console) see of a queued action."""

    id: str
    action_type: str
    status: str
    summary: str
    reason: str
    payload: dict
    requested_by: str
    requested_subject: str | None
    created_at: str
    decided_by: str | None = None
    decided_at: str | None = None
    decision_note: str | None = None
    executed_at: str | None = None
    odoo_ref: str | None = None
    error: str | None = None
