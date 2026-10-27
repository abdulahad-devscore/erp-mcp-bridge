"""Seed a fresh Odoo 17 database with ~4 months of Tembusu Housewares history.

Run once against an empty database (the compose `seed` service does this):

    python -m seed.seed_odoo

What it creates, in order:
  company + currency (MYR) -> warehouses -> products, BOMs, suppliers ->
  opening stock -> reorder rules -> 4 months of sales (delivered, invoiced,
  partly paid) -> purchase orders (billed, received, partial, open) ->
  inter-warehouse transfers -> manufacturing orders.

Everything is deterministic (fixed RNG seed, fixed "today") so eval answers in
ops-copilot-agent stay stable across machines.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from erp_bridge.odoo.client import OdooClient, OdooError  # noqa: E402
from seed import catalog  # noqa: E402

log = logging.getLogger("seed")

TODAY = date.fromisoformat(os.environ.get("SEED_TODAY", "2026-10-27"))
HISTORY_DAYS = 120
RNG = random.Random(20261027)

# (from, to, days ago, lines, done?): inter-warehouse replenishment history
TRANSFER_PLAN = [
    ("DC", "KLCC", 20, [("TH-CW-2401", 12), ("TH-KS-4203", 60), ("TH-SA-6102", 6)], True),
    ("DC", "PJ", 13, [("TH-TW-5101", 80), ("TH-KS-4202", 48), ("TH-CW-2410", 10)], True),
    ("DC", "PNG", 6, [("TH-SA-6101", 8), ("TH-BW-3101", 12), ("TH-KS-4220", 24)], True),
    ("FAC", "DC", 9, [("TH-CW-2401", 120), ("TH-CW-2411", 150), ("TH-KS-4203", 300)], True),
    ("DC", "KLCC", 1, [("TH-CW-2402", 8), ("TH-KS-4210", 6)], False),
]

# (sku, quantity, final state): factory production
MO_PLAN = [
    ("TH-CW-2401", 200, "done"),
    ("TH-KS-4201", 1500, "done"),
    ("TH-CW-2410", 300, "confirmed"),
    ("TH-BW-3104", 250, "confirmed"),
    ("TH-KS-4210", 120, "confirmed"),
    ("TH-CW-2421", 150, "draft"),
]


def dt(d: date, hour: int = 10) -> str:
    return datetime(d.year, d.month, d.day, hour, 0, 0).strftime("%Y-%m-%d %H:%M:%S")


class Seeder:
    def __init__(self, odoo: OdooClient):
        self.o = odoo
        self.wh: dict[str, dict] = {}
        self.products: dict[str, int] = {}  # sku -> product.product id
        self.templates: dict[str, int] = {}
        self.partners: dict[str, int] = {}  # ref -> id
        self.teams: dict[str, int] = {}
        self.terms: dict[int, int] = {}
        self.uom: dict[str, int] = {}

    async def xmlid(self, module: str, name: str) -> int:
        rows = await self.o.search_read("ir.model.data", [["module", "=", module], ["name", "=", name]], ["res_id"])
        if not rows:
            raise RuntimeError(f"missing xmlid {module}.{name}")
        return rows[0]["res_id"]

    # ------------------------------------------------------------------ setup
    async def company(self) -> None:
        myr = await self.o.search_read("res.currency", [["name", "=", "MYR"]], ["id"], context={"active_test": False})
        await self.o.execute("res.currency", "write", [myr[0]["id"]], {"active": True})
        my = await self.o.search_read("res.country", [["code", "=", "MY"]], ["id"])
        values = dict(catalog.COMPANY, currency_id=myr[0]["id"], country_id=my[0]["id"])
        await self.o.execute("res.company", "write", [1], values)
        self.company_id = 1

    async def taxes(self) -> None:
        # Sales & Service Tax: 10% sales tax on finished goods. Raw materials
        # bought by a licensed manufacturer are exempt, so purchases of RM
        # carry no tax while traded goods do.
        self.sst_sale = await self.o.create(
            "account.tax", {"name": "SST 10% (Sales)", "amount": 10, "type_tax_use": "sale", "amount_type": "percent"}
        )
        self.sst_purchase = await self.o.create(
            "account.tax",
            {"name": "SST 10% (Purchase)", "amount": 10, "type_tax_use": "purchase", "amount_type": "percent"},
        )

    async def payment_terms(self) -> None:
        names = {
            0: "account_payment_term_immediate",
            14: "account_payment_term_15days",
            30: "account_payment_term_30days",
            45: "account_payment_term_45days",
        }
        for days, xml in names.items():
            self.terms[days] = await self.xmlid("account", xml)
        # Odoo ships no 60-day term; overseas suppliers need one.
        self.terms[60] = await self.o.create(
            "account.payment.term",
            {"name": "60 Days", "line_ids": [(0, 0, {"value": "percent", "value_amount": 100, "nb_days": 60})]},
        )

    async def warehouses(self) -> None:
        for code, name, role in catalog.WAREHOUSES:
            existing = await self.o.search_read("stock.warehouse", [["code", "=", code]], ["id"])
            if code == "FAC":
                await self.o.execute("stock.warehouse", "write", [1], {"name": name, "code": code})
                wid = 1
            elif existing:
                wid = existing[0]["id"]
            else:
                wid = await self.o.create("stock.warehouse", {"name": name, "code": code})
            self.wh[code] = {"id": wid, "role": role}
        dc = self.wh["DC"]["id"]
        for code, _, role in catalog.WAREHOUSES:
            if role == "store":
                await self.o.execute(
                    "stock.warehouse", "write", [self.wh[code]["id"]], {"resupply_wh_ids": [(6, 0, [dc])]}
                )
        rows = await self.o.search_read(
            "stock.warehouse", [], ["code", "lot_stock_id", "in_type_id", "out_type_id", "int_type_id", "manu_type_id"]
        )
        for r in rows:
            self.wh[r["code"]].update(
                stock=r["lot_stock_id"][0],
                in_type=r["in_type_id"][0],
                out_type=r["out_type_id"][0],
                int_type=r["int_type_id"][0],
                manu_type=r["manu_type_id"][0] if r.get("manu_type_id") else None,
            )

    async def partners_and_teams(self) -> None:
        for name in catalog.SALES_TEAMS:
            self.teams[name] = await self.o.create("crm.team", {"name": name})
        for name, ref, terms, _ in catalog.SUPPLIERS:
            self.partners[ref] = await self.o.create(
                "res.partner",
                {
                    "name": name,
                    "ref": ref,
                    "is_company": True,
                    "supplier_rank": 1,
                    "property_supplier_payment_term_id": self.terms[terms],
                },
            )
        for name, ref, channel, terms, limit, _, _ in catalog.CUSTOMERS:
            values = {
                "name": name,
                "ref": ref,
                "is_company": True,
                "customer_rank": 1,
                "property_payment_term_id": self.terms[terms],
                "team_id": self.teams[channel],
            }
            if limit:
                values.update(use_partner_credit_limit=True, credit_limit=limit)
            self.partners[ref] = await self.o.create("res.partner", values)

    async def products_and_boms(self) -> None:
        uoms = await self.o.search_read("uom.uom", [["name", "in", ["Units", "kg", "L"]]], ["name"])
        self.uom = {u["name"]: u["id"] for u in uoms}
        categ = {}
        for c in catalog.CATEGORIES:
            categ[c] = await self.o.create("product.category", {"name": c})
        manufacture = await self.xmlid("mrp", "route_warehouse0_manufacture")
        buy = await self.xmlid("purchase_stock", "route_warehouse0_buy")
        sellers: dict[str, list] = defaultdict(list)
        for _, ref, _, supplies in catalog.SUPPLIERS:
            for sku, price, lead in supplies:
                sellers[sku].append(
                    (0, 0, {"partner_id": self.partners[ref], "price": price, "delay": lead, "min_qty": 1})
                )

        for sku, name, cat, price, cost, source in catalog.FINISHED_GOODS:
            self.templates[sku] = await self.o.create(
                "product.template",
                {
                    "name": name,
                    "default_code": sku,
                    "detailed_type": "product",
                    "categ_id": categ[cat],
                    "list_price": price,
                    "standard_price": cost,
                    "invoice_policy": "order",
                    "route_ids": [(6, 0, [manufacture if source == "make" else buy])],
                    "taxes_id": [(6, 0, [self.sst_sale])],
                    "supplier_taxes_id": [(6, 0, [self.sst_purchase])],
                    "seller_ids": sellers.get(sku, []),
                },
            )
        for sku, name, uom, cost in catalog.RAW_MATERIALS:
            self.templates[sku] = await self.o.create(
                "product.template",
                {
                    "name": name,
                    "default_code": sku,
                    "detailed_type": "product",
                    "categ_id": categ["Raw Materials"],
                    "standard_price": cost,
                    "sale_ok": False,
                    "uom_id": self.uom[uom],
                    "uom_po_id": self.uom[uom],
                    "route_ids": [(6, 0, [buy])],
                    "taxes_id": [(5, 0, 0)],
                    "supplier_taxes_id": [(5, 0, 0)],
                    "seller_ids": sellers.get(sku, []),
                },
            )
        rows = await self.o.search_read("product.product", [["default_code", "!=", False]], ["default_code"])
        self.products = {r["default_code"]: r["id"] for r in rows}
        self.bom_ids = {}
        for sku, lines in catalog.BOMS.items():
            self.bom_ids[sku] = await self.o.create(
                "mrp.bom",
                {
                    "product_tmpl_id": self.templates[sku],
                    "product_qty": 1,
                    "type": "normal",
                    "bom_line_ids": [(0, 0, {"product_id": self.products[c], "product_qty": q}) for c, q in lines],
                },
            )

    # ------------------------------------------------------------ demand plan
    def plan_sales(self) -> list[dict]:
        """Generate order headers + lines without touching Odoo."""
        fg = [p for p in catalog.FINISHED_GOODS]
        start = TODAY - timedelta(days=HISTORY_DAYS)
        orders: list[dict] = []

        def weight(sku: str, day: date) -> float:
            # Air fryers trend up through the period, woks spike in the run-up
            # to Deepavali (late Oct), storage sells steadily.
            progress = (day - start).days / HISTORY_DAYS
            w = 1.0
            if sku == "TH-SA-6104":
                w *= 0.5 + 1.5 * progress
            if sku.startswith("TH-CW-24") and day >= TODAY - timedelta(days=25):
                w *= 1.8
            if sku.startswith("TH-KS-42"):
                w *= 1.3
            return w

        def pick_lines(day: date, n: int, qty_range: tuple[int, int]) -> list[tuple[str, int, float]]:
            weights = [weight(p[0], day) for p in fg]
            chosen = set()
            while len(chosen) < n:
                chosen.add(RNG.choices(range(len(fg)), weights=weights)[0])
            lines = []
            for i in sorted(chosen):
                sku, _, _, price, _, _ = fg[i]
                lines.append((sku, RNG.randint(*qty_range), price))
            return lines

        wholesale = [c for c in catalog.CUSTOMERS if c[2] == "Wholesale"]
        for _name, ref, _, _, _, _, size in wholesale:
            day = start + timedelta(days=RNG.randint(0, 9))
            while day < TODAY:
                lines = pick_lines(day, RNG.randint(3, 8), (max(6, int(12 * size)), max(12, int(96 * size))))
                # wholesale gets a 15% trade discount off list
                lines = [(s, q, round(p * 0.85, 2)) for s, q, p in lines]
                orders.append({"ref": ref, "team": "Wholesale", "wh": "DC", "day": day, "lines": lines})
                day += timedelta(days=RNG.randint(7, 13))

        day = start
        while day < TODAY:
            orders.append(
                {
                    "ref": "CUS-WEB",
                    "team": "Online",
                    "wh": "DC",
                    "day": day,
                    "lines": pick_lines(day, RNG.randint(2, 6), (1, 6)),
                }
            )
            day += timedelta(days=2)

        for store in ("KLCC", "PJ", "PNG"):
            day = start + timedelta(days=6)
            scale = catalog.STORE_STOCK_PROFILE[store]
            while day < TODAY:
                lines = pick_lines(day, RNG.randint(6, 12), (2, max(3, int(14 * scale))))
                orders.append({"ref": f"CUS-{store}", "team": "Retail Stores", "wh": store, "day": day, "lines": lines})
                day += timedelta(days=7)

        orders.sort(key=lambda o: o["day"])
        return orders

    # ----------------------------------------------------------------- stock
    async def opening_stock(self, orders: list[dict]) -> None:
        demand: dict[tuple[str, str], int] = defaultdict(int)
        for o in orders:
            for sku, qty, _ in o["lines"]:
                demand[(o["wh"], sku)] += qty
        targets: dict[tuple[str, str], float] = {}
        for sku, *_ in catalog.FINISHED_GOODS:
            targets[("DC", sku)] = RNG.randint(60, 420)
            targets[("FAC", sku)] = RNG.randint(0, 150) if sku in catalog.BOMS else 0
            for store, scale in catalog.STORE_STOCK_PROFILE.items():
                targets[(store, sku)] = max(0, int(RNG.randint(4, 45) * scale))
        # A few deliberate shortages so reorder / transfer questions are real.
        for key, qty in {
            ("KLCC", "TH-SA-6104"): 2,
            ("PJ", "TH-SA-6104"): 0,
            ("PNG", "TH-CW-2401"): 3,
            ("DC", "TH-SA-6104"): 35,
            ("DC", "TH-CW-2401"): 70,
            ("KLCC", "TH-KS-4201"): 12,
        }.items():
            targets[key] = qty
        for sku, _, _, _ in catalog.RAW_MATERIALS:
            lo, hi = catalog.FACTORY_RM_REORDER[sku]
            targets[("FAC", sku)] = round(RNG.uniform(lo * 0.7, hi * 0.9), 0)
        targets[("FAC", "RM-AL-INGOT")] = 1450  # below min: buyer should reorder
        targets[("FAC", "RM-NS-COAT")] = 52

        # Quants are set to the target end state plus everything that later
        # flows out (sales, transfers out, MO consumption). Inflows that happen
        # later (transfers in, MO output) are not netted off: they arrive after
        # the sales that would otherwise run short.
        for src, _dst, _, lines, _done in TRANSFER_PLAN:
            for sku, qty in lines:
                demand[(src, sku)] += qty
        for sku, qty, state in MO_PLAN:
            if state == "done":
                for comp, per in catalog.BOMS[sku]:
                    demand[("FAC", comp)] += per * qty
        values = []
        for (wh, sku), qty in targets.items():
            total = max(0, qty + demand.get((wh, sku), 0))
            if total > 0:
                values.append(
                    {"product_id": self.products[sku], "location_id": self.wh[wh]["stock"], "inventory_quantity": total}
                )
        ids = await self.o.execute("stock.quant", "create", values, context={"inventory_mode": True})
        await self.o.execute("stock.quant", "action_apply_inventory", ids, context={"inventory_mode": True})
        log.info("opening stock: %d quants", len(ids))

    async def reorder_rules(self) -> None:
        values = []
        for sku, *_ in catalog.FINISHED_GOODS:
            for store in ("KLCC", "PJ", "PNG"):
                lo, hi = catalog.STORE_REORDER.get(sku, catalog.STORE_REORDER["default"])
                values.append(
                    {
                        "product_id": self.products[sku],
                        "warehouse_id": self.wh[store]["id"],
                        "location_id": self.wh[store]["stock"],
                        "product_min_qty": lo,
                        "product_max_qty": hi,
                        "trigger": "manual",
                    }
                )
            lo, hi = catalog.DC_REORDER["default"]
            values.append(
                {
                    "product_id": self.products[sku],
                    "warehouse_id": self.wh["DC"]["id"],
                    "location_id": self.wh["DC"]["stock"],
                    "product_min_qty": lo,
                    "product_max_qty": hi,
                    "trigger": "manual",
                }
            )
        for sku, (lo, hi) in catalog.FACTORY_RM_REORDER.items():
            values.append(
                {
                    "product_id": self.products[sku],
                    "warehouse_id": self.wh["FAC"]["id"],
                    "location_id": self.wh["FAC"]["stock"],
                    "product_min_qty": lo,
                    "product_max_qty": hi,
                    "trigger": "manual",
                }
            )
        await self.o.execute("stock.warehouse.orderpoint", "create", values)

    async def validate_pickings(self, picking_ids: list[int], ratio: float = 1.0) -> None:
        """Set done quantities on every move and validate. ratio < 1 leaves a backorder."""
        if not picking_ids:
            return
        if ratio >= 1:
            # Opening stock covers all demand, so reservation already set the
            # full quantity on each move: mark picked and validate in 3 calls.
            await self.o.execute("stock.picking", "action_assign", picking_ids)
            moves = await self.o.search_read(
                "stock.move", [["picking_id", "in", picking_ids]], ["product_uom_qty", "quantity"]
            )
            for m in moves:  # anything not fully reserved gets its full quantity forced
                if m["quantity"] < m["product_uom_qty"]:
                    await self.o.execute("stock.move", "write", [m["id"]], {"quantity": m["product_uom_qty"]})
            await self.o.execute("stock.move", "write", [m["id"] for m in moves], {"picked": True})
            await self.o.execute("stock.picking", "button_validate", picking_ids, context={"skip_backorder": True})
            return
        moves = await self.o.search_read("stock.move", [["picking_id", "in", picking_ids]], ["product_uom_qty"])
        for m in moves:
            qty = m["product_uom_qty"] if ratio >= 1 else round(m["product_uom_qty"] * ratio)
            await self.o.execute("stock.move", "write", [m["id"]], {"quantity": qty, "picked": True})
        await self.o.execute("stock.picking", "button_validate", picking_ids, context={"skip_backorder": True})

    async def set_picking_dates(self, picking_ids: list[int], day: date) -> None:
        if picking_ids:
            await self.o.execute("stock.picking", "write", picking_ids, {"date_done": dt(day, 15)})
            moves = await self.o.search_read("stock.move", [["picking_id", "in", picking_ids]], ["id"])
            await self.o.execute("stock.move", "write", [m["id"] for m in moves], {"date": dt(day, 15)})

    # ----------------------------------------------------------------- sales
    async def sales(self, orders: list[dict]) -> None:
        late_payers = {c[1] for c in catalog.CUSTOMERS if c[5]}
        terms_days = {c[1]: c[3] for c in catalog.CUSTOMERS}
        for n, o in enumerate(orders, 1):
            so = await self.o.create(
                "sale.order",
                {
                    "partner_id": self.partners[o["ref"]],
                    "team_id": self.teams[o["team"]],
                    "warehouse_id": self.wh[o["wh"]]["id"],
                    "date_order": dt(o["day"]),
                    "order_line": [
                        (0, 0, {"product_id": self.products[s], "product_uom_qty": q, "price_unit": p})
                        for s, q, p in o["lines"]
                    ],
                },
            )
            await self.o.execute("sale.order", "action_confirm", [so])
            await self.o.execute("sale.order", "write", [so], {"date_order": dt(o["day"])})
            age = (TODAY - o["day"]).days
            pickings = (await self.o.search_read("sale.order", [["id", "=", so]], ["picking_ids"]))[0]["picking_ids"]
            # Orders from the last 3 days are still waiting to ship.
            if age > 3 or o["team"] == "Retail Stores":
                await self.validate_pickings(pickings)
                await self.set_picking_dates(
                    pickings, o["day"] + timedelta(days=0 if o["team"] == "Retail Stores" else 2)
                )
            if age <= 1 and o["team"] == "Wholesale":
                continue  # not invoiced yet
            inv = await self.invoice_order(so, o["day"] + timedelta(days=1 if o["team"] == "Wholesale" else 0))
            due_days = terms_days[o["ref"]]
            paid = o["team"] != "Wholesale" or age > due_days + (25 if o["ref"] in late_payers else 3)
            if o["team"] == "Wholesale" and o["ref"] not in late_payers and RNG.random() < 0.08:
                paid = False  # the occasional invoice slips even for good payers
            if paid:
                pay_day = o["day"] + timedelta(
                    days=0
                    if o["team"] != "Wholesale"
                    else min(
                        age - 1, due_days + (RNG.randint(15, 24) if o["ref"] in late_payers else RNG.randint(-5, 2))
                    )
                )
                await self.register_payment(inv, pay_day)
            if n % 25 == 0:
                log.info("sales: %d/%d", n, len(orders))

    async def invoice_order(self, so: int, inv_day: date) -> int:
        ctx = {"active_model": "sale.order", "active_ids": [so], "active_id": so}
        wiz = await self.o.create("sale.advance.payment.inv", {"advance_payment_method": "delivered"}, context=ctx)
        await self.o.execute("sale.advance.payment.inv", "create_invoices", [wiz], context=ctx)
        inv = (await self.o.search_read("sale.order", [["id", "=", so]], ["invoice_ids"]))[0]["invoice_ids"][-1]
        await self.o.execute("account.move", "write", [inv], {"invoice_date": inv_day.isoformat()})
        await self.o.execute("account.move", "action_post", [inv])
        return inv

    async def register_payment(self, move_id: int, day: date) -> None:
        ctx = {"active_model": "account.move", "active_ids": [move_id]}
        wiz = await self.o.create("account.payment.register", {"payment_date": day.isoformat()}, context=ctx)
        await self.o.execute("account.payment.register", "action_create_payments", [wiz], context=ctx)

    # ------------------------------------------------------------- purchases
    async def purchases(self) -> None:
        """Purchase history the invoice-flow demo matches supplier invoices against.

        status: billed   -> received and vendor bill posted (history)
                received -> fully received, waiting for the supplier's invoice
                partial  -> part received, backorder open
                open     -> confirmed, nothing received
        """
        plan = [
            # supplier ref, warehouse, days ago, [(sku, qty)], status
            ("SUP-PAS", "FAC", 75, [("RM-AL-INGOT", 6000)], "billed"),
            ("SUP-SCS", "FAC", 70, [("RM-NS-COAT", 180)], "billed"),
            ("SUP-KVP", "FAC", 64, [("RM-PP-RESIN", 3000), ("RM-SIL-SEAL", 10000)], "billed"),
            ("SUP-PPK", "FAC", 58, [("RM-PKG-BOX", 8000), ("RM-PKG-SLV", 15000)], "billed"),
            ("SUP-BHA", "DC", 90, [("TH-SA-6101", 300), ("TH-SA-6102", 200), ("TH-SA-6104", 240)], "billed"),
            ("SUP-HTT", "DC", 85, [("TH-TW-5101", 2000), ("TH-TW-5102", 2000)], "billed"),
            ("SUP-BST", "FAC", 40, [("RM-STL-SHEET", 2500)], "billed"),
            ("SUP-PAS", "FAC", 18, [("RM-AL-INGOT", 5000)], "received"),
            ("SUP-KVP", "FAC", 14, [("RM-PP-RESIN", 1000), ("RM-HDL-BAK", 6000)], "partial"),
            ("SUP-PPK", "FAC", 12, [("RM-PKG-BOX", 6000), ("RM-PKG-SLV", 12000)], "received"),
            ("SUP-JGW", "FAC", 10, [("RM-LID-GL24", 800), ("RM-LID-GL28", 600)], "received"),
            ("SUP-SCS", "FAC", 9, [("RM-NS-COAT", 150)], "received"),
            ("SUP-BHA", "DC", 21, [("TH-SA-6104", 300), ("TH-SA-6103", 120)], "received"),
            ("SUP-HTT", "DC", 16, [("TH-TW-5110", 400), ("TH-TW-5101", 1500), ("TH-TW-5102", 1500)], "partial"),
            ("SUP-JGW", "DC", 8, [("TH-TW-5120", 500)], "received"),
            ("SUP-BST", "FAC", 5, [("RM-STL-SHEET", 2000)], "open"),
            ("SUP-BHA", "DC", 3, [("TH-SA-6104", 400), ("TH-SA-6102", 150)], "open"),
        ]
        prices = {sku: price for _, _, _, sup in catalog.SUPPLIERS for sku, price, _ in sup}
        for ref, wh, ago, lines, status in plan:
            day = TODAY - timedelta(days=ago)
            po = await self.o.create(
                "purchase.order",
                {
                    "partner_id": self.partners[ref],
                    "picking_type_id": self.wh[wh]["in_type"],
                    "date_order": dt(day),
                    "order_line": [
                        (
                            0,
                            0,
                            {
                                "product_id": self.products[s],
                                "product_qty": q,
                                "price_unit": prices[s],
                                "date_planned": dt(day + timedelta(days=5)),
                            },
                        )
                        for s, q in lines
                    ],
                },
            )
            await self.o.execute("purchase.order", "button_confirm", [po])
            await self.o.execute("purchase.order", "write", [po], {"date_approve": dt(day)})
            pickings = (await self.o.search_read("purchase.order", [["id", "=", po]], ["picking_ids"]))[0][
                "picking_ids"
            ]
            if status in ("billed", "received"):
                await self.validate_pickings(pickings)
                await self.set_picking_dates(pickings, day + timedelta(days=5))
            elif status == "partial":
                await self.validate_pickings(pickings, ratio=0.8)
                await self.set_picking_dates(pickings, day + timedelta(days=6))
            if status == "billed":
                await self.o.execute("purchase.order", "action_create_invoice", [po])
                bill = (await self.o.search_read("purchase.order", [["id", "=", po]], ["invoice_ids"]))[0][
                    "invoice_ids"
                ][-1]
                bill_day = day + timedelta(days=7)
                await self.o.execute(
                    "account.move",
                    "write",
                    [bill],
                    {"invoice_date": bill_day.isoformat(), "ref": f"{ref.split('-')[1]}-INV-{bill_day:%y%m}{po:04d}"},
                )
                await self.o.execute("account.move", "action_post", [bill])
                if ago > 45:
                    await self.register_payment(bill, bill_day + timedelta(days=30))
        log.info("purchases: %d orders", len(plan))

    # ------------------------------------------------------------- logistics
    async def transfers(self) -> None:
        plan = TRANSFER_PLAN
        for src, dst, ago, lines, done in plan:
            day = TODAY - timedelta(days=ago)
            pick = await self.o.create(
                "stock.picking",
                {
                    "picking_type_id": self.wh[src]["int_type"],
                    "location_id": self.wh[src]["stock"],
                    "location_dest_id": self.wh[dst]["stock"],
                    "scheduled_date": dt(day),
                    "origin": f"Replenish {dst}",
                    "move_ids": [
                        (
                            0,
                            0,
                            {
                                "name": s,
                                "product_id": self.products[s],
                                "product_uom_qty": q,
                                "product_uom": self.uom["Units"],
                                "location_id": self.wh[src]["stock"],
                                "location_dest_id": self.wh[dst]["stock"],
                            },
                        )
                        for s, q in lines
                    ],
                },
            )
            await self.o.execute("stock.picking", "action_confirm", [pick])
            await self.o.execute("stock.picking", "action_assign", [pick])
            if done:
                await self.validate_pickings([pick])
                await self.set_picking_dates([pick], day)

    # --------------------------------------------------------- manufacturing
    async def manufacturing(self) -> None:
        plan = MO_PLAN
        for sku, qty, state in plan:
            mo = await self.o.create(
                "mrp.production",
                {
                    "product_id": self.products[sku],
                    "product_qty": qty,
                    "bom_id": self.bom_ids[sku],
                    "picking_type_id": self.wh["FAC"]["manu_type"],
                    "origin": "Weekly production plan",
                },
            )
            if state == "draft":
                continue
            await self.o.execute("mrp.production", "action_confirm", [mo])
            if state == "done":
                await self.o.execute("mrp.production", "write", [mo], {"qty_producing": qty})
                await self.o.execute("mrp.production", "action_assign", [mo])
                raws = await self.o.search_read(
                    "stock.move", [["raw_material_production_id", "=", mo]], ["product_uom_qty"]
                )
                for m in raws:
                    await self.o.execute(
                        "stock.move", "write", [m["id"]], {"quantity": m["product_uom_qty"], "picked": True}
                    )
                await self.o.execute("mrp.production", "button_mark_done", [mo])

    async def run(self) -> None:
        companies = await self.o.search_read("res.company", [["id", "=", 1]], ["name"])
        if companies and companies[0]["name"] == catalog.COMPANY["name"]:
            log.info("database already seeded, nothing to do")
            return
        steps = [
            self.company,
            self.taxes,
            self.payment_terms,
            self.warehouses,
            self.partners_and_teams,
            self.products_and_boms,
        ]
        for step in steps:
            log.info("step: %s", step.__name__)
            await step()
        orders = self.plan_sales()
        log.info("planned %d sales orders", len(orders))
        # Receipts first: the opening-stock adjustment then sets absolute
        # quantities, so received POs don't inflate the intended end state.
        await self.purchases()
        await self.opening_stock(orders)
        await self.reorder_rules()
        await self.sales(orders)
        await self.transfers()
        await self.manufacturing()
        await self.verify()
        log.info("seed complete")

    async def verify(self) -> None:
        """Everything older than 3 days should be shipped; say so loudly if not."""
        cutoff = dt(TODAY - timedelta(days=4))
        stuck = await self.o.search_read(
            "stock.picking",
            [
                ["state", "not in", ["done", "cancel"]],
                ["picking_type_code", "=", "outgoing"],
                ["sale_id.date_order", "<", cutoff],
            ],
            ["name", "origin"],
        )
        if stuck:
            log.warning("%d old deliveries were not validated: %s", len(stuck), [p["name"] for p in stuck])


async def wait_for_odoo(odoo: OdooClient, attempts: int = 60) -> None:
    for _ in range(attempts):
        try:
            await odoo.uid()
            return
        except Exception as exc:  # noqa: BLE001 - Odoo may still be booting
            log.info("waiting for Odoo (%s)", type(exc).__name__)
            await asyncio.sleep(3)
    raise SystemExit("Odoo did not come up")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("ODOO_URL", "http://localhost:8069"))
    parser.add_argument("--db", default=os.environ.get("ODOO_DB", "tembusu"))
    parser.add_argument("--user", default=os.environ.get("ODOO_USER", "admin"))
    parser.add_argument("--password", default=os.environ.get("ODOO_PASSWORD", "admin"))
    args = parser.parse_args()
    odoo = OdooClient(args.url, args.db, args.user, args.password, timeout=300)
    try:
        await wait_for_odoo(odoo)
        await Seeder(odoo).run()
    except OdooError as exc:
        log.error("Odoo rejected a seed call: %s", exc)
        raise
    finally:
        await odoo.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
