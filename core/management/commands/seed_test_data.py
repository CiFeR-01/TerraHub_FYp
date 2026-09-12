"""
seed_test_data — additive, idempotent demo/test data for the analytics & forecasting
build-out (predictive-analysis branch).

What it does NOT touch:
  * Warehouses, Products, Materials, ProductRecipe (the BOM) — reused as-is.
  * Any pre-existing PO / SO / ProductionRun / Batch / RegistryLog.

What it adds (all rows carry a stable natural key or the source='TestSeed' marker,
so re-running is a no-op rather than a pile-up):
  * Contact details on the 3 existing suppliers + 1 existing client that had blanks.
  * 3 new suppliers, 4 new clients, with full contact info.
  * SupplierMaterial links (unit price + real lead time) for every consumed material.
  * ~40 days of daily `Consumed_For_Manufacturing` RegistryLog history for 10
    materials, tuned so the stockout forecast shows every status band.
  * Raw-material on-hand Batches for those 10 materials.
  * 12 Purchase Orders (Completed / Partially Received / Pending) with line items,
    explicit expected + actual dates, linked inbound shipments and received batches.
  * 12 completed Production Runs with RunMaterialUsage + yield logs + FG batches,
    plus 2 in-progress runs linked to sales orders.
  * 9 open Sales Orders with deadlines, line items and outbound shipments covering
    every delivery-risk band, plus linked production runs.
  * ~28 resolved StockAudits across all 4 warehouses over ~4 months (one chronic
    shrinkage item).
  * 61 days of daily WarehouseUtilizationSnapshot per warehouse with a real trend.

Run:  python manage.py seed_test_data
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core import analytics
from core.models import (
    Batch, Client, CustomUser, Material, Product, ProductionRun,
    ProductionRunYieldLog, PurchaseOrder, PurchaseOrderDetail, RegistryLog,
    RunMaterialUsage, SalesOrder, SalesOrderDetail, Shipment, ShipmentItem,
    StockAudit, Supplier, SupplierMaterial, Warehouse, WarehouseUtilizationSnapshot,
)

TODAY = dt.date(2026, 9, 10)          # matches the branch's "today"
Q = Decimal("0.01")


def _d(x) -> Decimal:
    return Decimal(str(x)).quantize(Q)


def _noise(seed: int, pct: float) -> float:
    """Deterministic pseudo-noise in [-pct, +pct] from an int seed."""
    frac = ((seed * 2654435761) % 1000) / 1000.0          # 0..1
    return (frac * 2 - 1) * pct


def _aware(d: dt.date, hour: int = 9) -> dt.datetime:
    return timezone.make_aware(dt.datetime.combine(d, dt.time(hour, 0)))


class Command(BaseCommand):
    help = "Seed additive analytics/forecasting test data (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset", action="store_true",
            help="Delete previously seeded rows (TS-/PO-TS/SO-TS/RUN-TS markers, "
                 "source='TestSeed', '[TS]' registry labels) before re-seeding.",
        )

    def _reset(self):
        from core.models import (ProductionConsumption, StockAllocation, OrderTimeline)
        n = {}
        n["registrylog"] = (
            RegistryLog.objects.filter(item_name__endswith="[TS]").delete()[0]
            + RegistryLog.objects.filter(action_type="Produced",
                                         item_name__contains="Batch TS-FG-").delete()[0]
        )
        n["stockaudit"] = StockAudit.objects.filter(source="TestSeed").delete()[0]
        n["yieldlog"] = ProductionRunYieldLog.objects.filter(
            production_run__run_number__startswith="RUN-TS").delete()[0]
        n["runusage"] = RunMaterialUsage.objects.filter(
            production_run__run_number__startswith="RUN-TS").delete()[0]
        ProductionConsumption.objects.filter(
            production_run__run_number__startswith="RUN-TS").delete()
        StockAllocation.objects.filter(
            production_run__run_number__startswith="RUN-TS").delete()
        StockAllocation.objects.filter(
            sales_order__so_number__startswith="SO-TS").delete()
        OrderTimeline.objects.filter(
            production_run__run_number__startswith="RUN-TS").delete()
        OrderTimeline.objects.filter(
            sales_order__so_number__startswith="SO-TS").delete()
        OrderTimeline.objects.filter(
            purchase_order__po_number__startswith="PO-TS").delete()
        from core.models import ShipmentItem as _SI
        _SI.objects.filter(shipment__tracking_number__startswith="TS-SHP").delete()
        n["shipment"] = Shipment.objects.filter(
            tracking_number__startswith="TS-SHP").delete()[0]
        n["batch"] = Batch.objects.filter(batch_number__startswith="TS-").delete()[0]
        PurchaseOrderDetail.objects.filter(
            purchase_order__po_number__startswith="PO-TS").delete()
        n["po"] = PurchaseOrder.objects.filter(po_number__startswith="PO-TS").delete()[0]
        SalesOrderDetail.objects.filter(
            sales_order__so_number__startswith="SO-TS").delete()
        n["so"] = SalesOrder.objects.filter(so_number__startswith="SO-TS").delete()[0]
        n["run"] = ProductionRun.objects.filter(
            run_number__startswith="RUN-TS").delete()[0]
        self.stdout.write(self.style.WARNING(f"reset: deleted {n}"))

    def handle(self, *args, **opts):
        with transaction.atomic():
            if opts.get("reset"):
                self._reset()
            self.users = {u.username: u for u in CustomUser.objects.all()}
            self.wh = {w.name: w for w in Warehouse.objects.all()}
            self.mat = {m.sku: m for m in Material.objects.all()}
            self.prod = {p.sku: p for p in Product.objects.all()}

            self._fill_existing_contacts()
            self.suppliers = self._suppliers()
            self.clients = self._clients()
            self._supplier_materials()
            self._onhand_batches()
            self._purchase_orders()
            self._consumption_history()      # after batches exist: rate is tuned to real on-hand
            self._production_runs()
            self._sales_orders()
            self._stock_audits()
            self._snapshots()

        self._report()

    # ---------------------------------------------------------------- contacts
    def _fill_existing_contacts(self):
        n = 0
        for s in Supplier.objects.all():
            changed = False
            if not s.contact_person:
                s.contact_person = f"{s.name.split()[0]} Desk"; changed = True
            if not s.email:
                s.email = f"orders@{s.name.split()[0].lower()}.example.com"; changed = True
            if not s.phone:
                s.phone = "+60 3-7000 0100"; changed = True
            if not s.address:
                s.address = "Lot 12, Jalan Perindustrian, 40000 Shah Alam, Selangor"; changed = True
            if changed:
                s.save(); n += 1
        for c in Client.objects.all():
            changed = False
            if not c.contact_person:
                c.contact_person = f"{c.name.split()[0]} Procurement"; changed = True
            if not c.email:
                c.email = f"buyer@{c.name.split()[0].lower()}.example.com"; changed = True
            if not c.phone:
                c.phone = "+60 3-8000 0200"; changed = True
            if not c.delivery_address:
                c.delivery_address = "No. 5, Jalan Ladang, 43000 Kajang, Selangor"; changed = True
            if changed:
                c.save(); n += 1
        self._contacts_fixed = n

    # --------------------------------------------------------------- suppliers
    def _suppliers(self):
        want = {
            "Northline Chemicals Sdn Bhd": dict(contact_person="Farid Osman", email="sales@northline.example.com",
                                                phone="+60 3-7100 2200", address="PT 4471, Kawasan Perindustrian Bukit Raja, 41050 Klang"),
            "Krishna Agrochem Pvt Ltd": dict(contact_person="Anita Rao", email="export@krishna-agro.example.com",
                                             phone="+91 22-6650 4400", address="Plot 21, MIDC Taloja, Navi Mumbai 410208"),
            "Pacific Minerals Trading": dict(contact_person="Grace Lim", email="desk@pacminerals.example.com",
                                             phone="+65 6220 7788", address="8 Tuas Avenue 10, Singapore 639140"),
            "GreenGrow Biotech": dict(contact_person="Daniel Chua", email="supply@greengrow.example.com",
                                      phone="+60 3-8900 5566", address="Lot 9, Techpark @ Enstek, 71760 Bandar Enstek"),
        }
        out = {}
        for name, extra in want.items():
            s, _ = Supplier.objects.get_or_create(name=name, defaults=dict(is_active=True, **extra))
            # backfill if the row pre-existed with blanks
            for k, v in extra.items():
                if not getattr(s, k):
                    setattr(s, k, v)
            s.save()
            out[name] = s
        return out

    # ----------------------------------------------------------------- clients
    def _clients(self):
        want = {
            "Verdant Farms Cooperative": "ops@verdantfarms.example.com",
            "Sunrise Plantations Bhd": "purchasing@sunriseplant.example.com",
            "AgriCore Distributors": "orders@agricore.example.com",
            "Highland Estates Sdn Bhd": "estate@highland.example.com",
        }
        out = {}
        for name, email in want.items():
            c, _ = Client.objects.get_or_create(name=name, defaults=dict(
                is_active=True, contact_person=f"{name.split()[0]} Lead", email=email,
                phone="+60 3-8100 3300",
                delivery_address="Ladang Blok C, Jalan Besar, 36000 Teluk Intan, Perak",
            ))
            out[name] = c
        # existing single client
        apex = Client.objects.filter(name="Apex Dynamics Ltd").first()
        if apex:
            out["Apex Dynamics Ltd"] = apex
        return out

    # -------------------------------------------------------- SupplierMaterial
    def _supplier_materials(self):
        # material sku -> (supplier name, lead days)
        plan = {
            "MAT-1001": ("Northline Chemicals Sdn Bhd", 10), "MAT-1002": ("Northline Chemicals Sdn Bhd", 14),
            "MAT-1004": ("Northline Chemicals Sdn Bhd", 7),  "MAT-1005": ("Northline Chemicals Sdn Bhd", 12),
            "MAT-1007": ("Krishna Agrochem Pvt Ltd", 10),    "MAT-1010": ("Krishna Agrochem Pvt Ltd", 9),
            "MAT-1013": ("Krishna Agrochem Pvt Ltd", 14),    "MAT-1003": ("Pacific Minerals Trading", 15),
            "MAT-1014": ("Pacific Minerals Trading", 12),     "MAT-1016": ("Pacific Minerals Trading", 7),
            "MAT-1017": ("GreenGrow Biotech", 12),            "MAT-1019": ("GreenGrow Biotech", 10),
            "MAT-1018": ("GreenGrow Biotech", 9),             "MAT-1020": ("GreenGrow Biotech", 9),
        }
        n = 0
        for sku, (sname, lead) in plan.items():
            m = self.mat.get(sku)
            if not m:
                continue
            _, created = SupplierMaterial.objects.update_or_create(
                supplier=self.suppliers[sname], material=m,
                defaults=dict(unit_price=_d(m.cost_per_unit or 1000), lead_time_days=lead),
            )
            n += 1
        self._suppliermat = n

    # ---------------------------------------------------- consumption history
    # sku -> (supplier lead days, target days-of-cover). The daily burn rate is
    # derived at run time as (current available on-hand / target days-of-cover),
    # so the stockout forecast lands in a known band regardless of how much stock
    # already exists:  cover < lead => critical, cover-lead in 0..2 => reorder_now,
    # 2..14 => watch, > 14 => ok.
    CONSUME = {
        "MAT-1001": (10, 6),   "MAT-1002": (14, 9),    # critical
        "MAT-1004": (7, 8),    "MAT-1005": (12, 13),   # reorder_now
        "MAT-1007": (10, 16),  "MAT-1010": (9, 18),  "MAT-1013": (14, 22),  # watch
        "MAT-1016": (7, 40),   "MAT-1017": (12, 45),  "MAT-1019": (10, 35),  # ok
    }
    ONHAND = {  # sku -> a modest guaranteed on-hand floor batch (allocated stays 0)
        "MAT-1001": 40, "MAT-1002": 30, "MAT-1004": 60, "MAT-1005": 30, "MAT-1007": 25,
        "MAT-1010": 45, "MAT-1013": 25, "MAT-1016": 50,  "MAT-1017": 40, "MAT-1019": 30,
    }

    def _available(self, m) -> Decimal:
        from django.db.models import Sum
        agg = Batch.objects.filter(status="Active", material=m).aggregate(
            oh=Sum("quantity"), al=Sum("allocated_quantity"))
        return (agg["oh"] or Decimal("0")) - (agg["al"] or Decimal("0"))

    def _consumption_history(self):
        plant = self.wh["Main Assembly Plant"]
        user = self.users.get("production_super")
        made = 0
        self._rates = {}
        for i, (sku, (_lead, target_dc)) in enumerate(self.CONSUME.items()):
            m = self.mat[sku]
            rate = float(self._available(m)) / target_dc
            self._rates[sku] = rate
            for day_off in range(0, 40):
                d = TODAY - dt.timedelta(days=day_off)
                qty = _d(rate * (1 + _noise(i * 97 + day_off, 0.08)))
                label = f"{m.name} (daily production draw) [TS]"
                exists = RegistryLog.objects.filter(
                    action_type="Consumed_For_Manufacturing", material=m,
                    item_name=label, timestamp__date=d,
                ).exists()
                if exists:
                    continue
                log = RegistryLog.objects.create(
                    action_type="Consumed_For_Manufacturing", item_name=label,
                    material=m, quantity_changed=qty, warehouse=plant, user=user,
                )
                RegistryLog.objects.filter(pk=log.pk).update(timestamp=_aware(d, 14))
                made += 1
        self._consume_logs = made

    def _onhand_batches(self):
        alpha, beta = self.wh["Raw Storage Alpha"], self.wh["Raw Storage Beta"]
        made = 0
        for idx, (sku, avail) in enumerate(self.ONHAND.items()):
            m = self.mat[sku]
            wh = alpha if idx < 5 else beta
            bn = f"TS-OH-{sku}"
            if Batch.objects.filter(batch_number=bn).exists():
                continue
            Batch.objects.create(
                batch_number=bn, status="Active", material=m, quantity=_d(avail),
                allocated_quantity=Decimal("0"),
                manufacturing_date=TODAY - dt.timedelta(days=20),
                expiry_date=TODAY + dt.timedelta(days=max(30, m.safe_storage_days or 120)),
                warehouse=wh, location="Zone North Aisle 3",
            )
            made += 1
        self._onhand = made

    # ------------------------------------------------------- purchase orders
    def _purchase_orders(self):
        alpha, beta = self.wh["Raw Storage Alpha"], self.wh["Raw Storage Beta"]
        buyer = self.users.get("purchasing_staff")
        appr = self.users.get("manager_user")
        # (supplier, [material skus], order_days_ago, lead, status, fill_frac, delay_days)
        # delay is (actual arrival - expected); <=0 is on time. Ratings target:
        #   Northline -> good, Pacific -> good, Krishna -> watch, GreenGrow -> watch,
        #   Ghost Corp -> poor. Only Ghost carries the partial / pending POs so the
        #   others' fill_rate stays clean.
        specs = [
            ("Northline Chemicals Sdn Bhd", ["MAT-1001", "MAT-1004"], 140, 10, "Completed", 1.0, -2),
            ("Northline Chemicals Sdn Bhd", ["MAT-1002"],             112, 14, "Completed", 1.0, -1),
            ("Northline Chemicals Sdn Bhd", ["MAT-1005", "MAT-1001"],  84, 12, "Completed", 1.0, 0),
            ("Northline Chemicals Sdn Bhd", ["MAT-1004"],              40,  7, "Completed", 1.0, 0),
            ("Pacific Minerals Trading",    ["MAT-1016", "MAT-1003"], 100,  7, "Completed", 1.0, -1),
            ("Pacific Minerals Trading",    ["MAT-1014"],              58, 12, "Completed", 1.0, 0),
            ("Pacific Minerals Trading",    ["MAT-1016"],              26,  7, "Completed", 1.0, 0),
            ("Krishna Agrochem Pvt Ltd",    ["MAT-1007", "MAT-1010"], 120, 10, "Completed", 1.0, -1),
            ("Krishna Agrochem Pvt Ltd",    ["MAT-1013"],              76, 14, "Completed", 1.0, -1),
            ("Krishna Agrochem Pvt Ltd",    ["MAT-1010"],              48,  9, "Completed", 1.0, 0),
            ("Krishna Agrochem Pvt Ltd",    ["MAT-1007"],              20, 10, "Completed", 1.0, 5),
            ("GreenGrow Biotech",           ["MAT-1017"],             104, 12, "Completed", 1.0, -1),
            ("GreenGrow Biotech",           ["MAT-1019", "MAT-1017"],  70, 12, "Completed", 1.0, -1),
            ("GreenGrow Biotech",           ["MAT-1018"],              44,  9, "Completed", 1.0, -1),
            ("GreenGrow Biotech",           ["MAT-1019"],              19, 10, "Completed", 1.0, 4),
            ("Ghost Corp SDN",              ["MAT-1013"],              96, 14, "Completed", 0.7, 8),
            ("Ghost Corp SDN",              ["MAT-1016"],              62,  7, "Completed", 0.7, 12),
            ("Ghost Corp SDN",              ["MAT-1013"],              24, 14, "Partially Received", 0.5, None),
            ("Ghost Corp SDN",              ["MAT-1016"],              10,  7, "Pending", 0.0, None),
            ("Ghost Corp SDN",              ["MAT-1013"],               5, 14, "Pending", 0.0, None),
        ]
        made = 0
        for i, (sname, skus, ago, lead, status, fill, delay) in enumerate(specs):
            po_no = f"PO-TS{i+1:03d}"
            if PurchaseOrder.objects.filter(po_number=po_no).exists():
                continue
            supplier = self.suppliers.get(sname) or Supplier.objects.filter(name=sname).first()
            order_date = TODAY - dt.timedelta(days=ago)
            expected = order_date + dt.timedelta(days=lead)
            target = alpha if i % 2 == 0 else beta
            po = PurchaseOrder.objects.create(
                po_number=po_no, supplier_name=sname, supplier=supplier,
                target_warehouse=target, expected_delivery_date=expected,
                status=status, created_by=buyer, approved_by=appr,
                approval_remarks="Approved against quarterly procurement plan.",
            )
            PurchaseOrder.objects.filter(pk=po.pk).update(order_date=order_date)
            if status == "Completed":
                po.completed_date = expected + dt.timedelta(days=delay)
                po.save(update_fields=["completed_date"])
            if status == "Completed":
                arrival = expected + dt.timedelta(days=delay)
            elif status == "Partially Received":
                arrival = TODAY - dt.timedelta(days=3)
            else:                                    # Pending: nothing has arrived
                arrival = None

            for j, sku in enumerate(skus):
                m = self.mat[sku]
                ordered = _d(60 + 20 * ((i + j) % 4))
                received = _d(ordered * Decimal(str(fill)))
                PurchaseOrderDetail.objects.create(
                    purchase_order=po, material=m, quantity_ordered=ordered,
                    quantity_received=received, unit_price=_d(m.cost_per_unit or 1000),
                )
                if received > 0 and arrival is not None:
                    bn = f"TS-B-{po_no}-{sku}"
                    if not Batch.objects.filter(batch_number=bn).exists():
                        Batch.objects.create(
                            batch_number=bn, status="Active", material=m, quantity=received,
                            allocated_quantity=Decimal("0"), purchase_order=po,
                            manufacturing_date=arrival,
                            expiry_date=arrival + dt.timedelta(days=365),
                            warehouse=target, location="Zone East Aisle 2",
                        )
            trk = f"TS-SHP-IN-{i+1:03d}"
            if arrival is not None and not Shipment.objects.filter(tracking_number=trk).exists():
                sh = Shipment.objects.create(
                    tracking_number=trk, direction="Inbound",
                    status="Completed" if status == "Completed" else "Arrived",
                    purchase_order=po, destination_warehouse=target,
                    external_origin=sname,
                    dispatch_date=arrival - dt.timedelta(days=lead),
                    expected_eta_date=expected, actual_arrival_date=arrival,
                    external_tracking_id=f"CARRIER-{i+1:05d}",
                )
                for sku in skus:
                    ShipmentItem.objects.create(
                        shipment=sh, material=self.mat[sku], quantity=_d(60),
                        received_quantity=_d(60) if status == "Completed" else Decimal("0"),
                    )
            made += 1
        self._pos = made

    # ------------------------------------------------------- production runs
    def _production_runs(self):
        plant = self.wh["Main Assembly Plant"]
        fg = self.wh["Finished Goods Hub"]
        sups = [self.users.get("production_super"), self.users.get("manager_user"), self.users.get("admin")]
        # (product sku, expected_yield, yield_factor, overuse_factor, days_ago)
        # PROD-2001 = deliberately poor (under-yields ~17%, overuses ~22%);
        # PROD-2008 = watch; everything else = good.
        specs = [
            ("PROD-2001", 40, 0.83, 1.22, 150), ("PROD-2001", 55, 0.82, 1.23, 96),
            ("PROD-2001", 30, 0.85, 1.21, 44),  ("PROD-2001", 45, 0.84, 1.22, 12),
            ("PROD-2002", 60, 0.99, 1.05, 132), ("PROD-2002", 45, 1.01, 1.03, 60),
            ("PROD-2003", 50, 0.98, 1.06, 120), ("PROD-2003", 35, 1.00, 1.04, 38),
            ("PROD-2004", 50, 1.02, 1.00, 108), ("PROD-2004", 25, 0.97, 1.08, 30),
            ("PROD-2005", 70, 1.00, 1.05, 84),  ("PROD-2006", 40, 0.99, 1.02, 52),
            ("PROD-2008", 20, 0.94, 1.12, 26),  ("PROD-2008", 28, 0.93, 1.13, 9),
        ]
        made = 0
        for i, (sku, ey, yf, ouf, ago) in enumerate(specs):
            rn = f"RUN-TS{i+1:03d}"
            if ProductionRun.objects.filter(run_number=rn).exists():
                continue
            p = self.prod[sku]
            end_d = TODAY - dt.timedelta(days=ago)
            start_d = end_d - dt.timedelta(days=1)
            sup = sups[i % 3]
            ay = _d(ey * yf)
            run = ProductionRun.objects.create(
                run_number=rn, target_product=p, expected_yield=_d(ey), actual_yield=ay,
                status="Completed", supervisor=sup, manufacturing_plant=plant,
                start_time=_aware(start_d, 8), end_time=_aware(end_d, 17),
                exact_start_time=_aware(start_d, 8), exact_end_time=_aware(end_d, 16),
                created_by=sup,
            )
            for r in p.recipe_items.all():
                exp_q = _d(Decimal(str(r.quantity_required)) * Decimal(str(ey)))
                RunMaterialUsage.objects.create(
                    production_run=run, material=r.material, expected_qty=exp_q,
                    actual_qty=_d(exp_q * Decimal(str(ouf))),
                    wastage_reason=("Spillage and residue loss during transfer."
                                    if ouf > 1.1 else "Consumption within normal tolerance."),
                )
            half = _d(ay / 2)
            ProductionRunYieldLog.objects.create(
                production_run=run, quantity=half, log_date=end_d,
                logged_by=sup, notes="Shift A finished-goods output.")
            ProductionRunYieldLog.objects.create(
                production_run=run, quantity=_d(ay - half), log_date=end_d,
                logged_by=sup, notes="Shift B finished-goods output.")
            dest = fg if i % 3 != 0 else plant
            bn = f"TS-FG-{rn}"
            if not Batch.objects.filter(batch_number=bn).exists():
                b = Batch.objects.create(
                    batch_number=bn, status="Active", product=p, quantity=ay,
                    allocated_quantity=Decimal("0"), produced_in=run, warehouse=dest,
                    manufacturing_date=end_d, expiry_date=end_d + dt.timedelta(days=365),
                    location="Zone Dispatch Aisle 1",
                )
                lg = RegistryLog.objects.create(
                    action_type="Produced", item_name=f"{p.name} (Batch {b.batch_number})",
                    quantity_changed=ay, warehouse=dest, user=sup,
                )
                RegistryLog.objects.filter(pk=lg.pk).update(timestamp=_aware(end_d, 17))
            made += 1
        self._runs = made

    # ----------------------------------------------------------- sales orders
    def _sales_orders(self):
        fghub = self.wh["Finished Goods Hub"]
        plant = self.wh["Main Assembly Plant"]
        creator = self.users.get("sales_rep")
        appr = self.users.get("manager_user")
        cl = self.clients
        names = list(cl.keys())
        # (client, product, qty, deadline_offset, status, shipped_frac,
        #  ship: None | ("eta", off) | ("arr", off), make_run)
        specs = [
            (names[0], "PROD-2001", 30, -5,  "In Production",           0.0, None,          True),
            (names[1], "PROD-2002", 25, -2,  "Pending",                 0.0, None,          False),
            (names[2], "PROD-2003", 40, 4,   "In Production",           0.0, None,          True),
            (names[3], "PROD-2004", 20, 6,   "Awaiting Acknowledgement",0.0, None,          False),
            (names[0], "PROD-2001", 35, 20,  "Ready to Ship",           0.0, ("eta", 10),  False),
            (names[1], "PROD-2005", 50, 30,  "Partially Shipped",       0.4, ("eta", 12),  False),
            (names[2], "PROD-2006", 28, 15,  "Shipped",                 1.0, ("arr", 2),   False),
            (names[3], "PROD-2008", 18, 3,   "Shipped",                 1.0, ("arr", 8),   False),
            (names[0], "PROD-2003", 22, None,"In Production",           0.0, None,          False),
        ]
        made = 0
        for i, (cname, psku, qty, dloff, status, sfrac, ship, mkrun) in enumerate(specs):
            so_no = f"SO-TS{i+1:03d}"
            if SalesOrder.objects.filter(so_number=so_no).exists():
                continue
            client = cl[cname]
            p = self.prod[psku]
            order_date = TODAY - dt.timedelta(days=25 + i)
            deadline = None if dloff is None else TODAY + dt.timedelta(days=dloff)
            so = SalesOrder.objects.create(
                so_number=so_no, client_name=cname, client=client,
                origin_warehouse=fghub, fulfillment_deadline=deadline, status=status,
                created_by=creator, approved_by=appr, manufacturing_plant=plant,
                approval_remarks="Credit check cleared; released to fulfilment.",
            )
            SalesOrder.objects.filter(pk=so.pk).update(order_date=order_date)
            qd = _d(qty)
            SalesOrderDetail.objects.create(
                sales_order=so, product=p, quantity_ordered=qd,
                quantity_shipped=_d(qd * Decimal(str(sfrac))),
                unit_price=_d(p.price_per_unit or 1000),
            )
            if ship is not None:
                kind, off = ship
                trk = f"TS-SHP-OUT-{i+1:03d}"
                if not Shipment.objects.filter(tracking_number=trk).exists():
                    sh = Shipment.objects.create(
                        tracking_number=trk, direction="Outbound",
                        status="Dispatched" if kind == "eta" else "Completed",
                        sales_order=so, origin_warehouse=fghub,
                        client_address=client.delivery_address,
                        client_contact_name=client.contact_person,
                        client_contact_phone=client.phone,
                        external_tracking_id=f"FLEET-{i+1:05d}",
                        dispatch_date=TODAY - dt.timedelta(days=2),
                        expected_eta_date=TODAY + dt.timedelta(days=off),
                        actual_arrival_date=(TODAY + dt.timedelta(days=off)) if kind == "arr" else None,
                    )
                    ShipmentItem.objects.create(
                        shipment=sh, product=p, quantity=qd,
                        received_quantity=qd if kind == "arr" else Decimal("0"),
                    )
            if mkrun:
                rn = f"RUN-TSIP{i+1:03d}"
                if not ProductionRun.objects.filter(run_number=rn).exists():
                    ProductionRun.objects.create(
                        run_number=rn, target_product=p, expected_yield=qd,
                        status="InProgress", supervisor=self.users.get("production_super"),
                        manufacturing_plant=plant, sales_order=so,
                        start_time=_aware(TODAY - dt.timedelta(days=1), 8),
                        exact_start_time=_aware(TODAY - dt.timedelta(days=1), 8),
                        created_by=creator,
                    )
            made += 1
        self._sos = made

    # ------------------------------------------------------------ stock audits
    def _stock_audits(self):
        auditors = [self.users.get("manager_user"), self.users.get("admin"),
                    self.users.get("logistics_coordinator"), self.users.get("production_super")]
        pool = list(Batch.objects.filter(batch_number__startswith="TS-")
                    .exclude(batch_number__startswith="TS-OH-").order_by("batch_number"))
        chronic = Batch.objects.filter(batch_number="TS-OH-MAT-1013").first()
        made = 0
        # chronic shrinkage on one Raw Storage Beta item
        if chronic:
            for k in range(4):
                d = TODAY - dt.timedelta(days=110 - k * 25)
                if StockAudit.objects.filter(batch=chronic, source="TestSeed",
                                             audit_date__date=d).exists():
                    continue
                exp = _d(chronic.quantity)
                a = StockAudit.objects.create(
                    batch=chronic, expected_quantity=exp,
                    actual_quantity=_d(exp * Decimal("0.94")),
                    auditor=auditors[k % 4], status="Resolved", source="TestSeed",
                )
                StockAudit.objects.filter(pk=a.pk).update(audit_date=_aware(d, 11))
                made += 1
        # spread of exact / shrink / overage across the rest
        for i, b in enumerate(pool):
            d = TODAY - dt.timedelta(days=(i * 13) % 120 + 4)
            if StockAudit.objects.filter(batch=b, source="TestSeed",
                                         audit_date__date=d).exists():
                continue
            exp = _d(b.quantity)
            r = i % 7
            if r < 4:
                act = exp
            elif r < 6:
                act = _d(exp * Decimal("0.96"))
            else:
                act = _d(exp * Decimal("1.03"))
            a = StockAudit.objects.create(
                batch=b, expected_quantity=exp, actual_quantity=act,
                auditor=auditors[i % 4], status="Resolved", source="TestSeed",
            )
            StockAudit.objects.filter(pk=a.pk).update(audit_date=_aware(d, 10))
            made += 1
        self._audits = made

    # --------------------------------------------------------------- snapshots
    def _snapshots(self):
        """
        61 days of daily snapshots per warehouse, ending at the warehouse's real
        current tonnage and ramping up to it on a per-warehouse gradient (so the
        trend chart and weekly_rate_pp are meaningful). NOTE: every warehouse is
        currently well past 100% of total_capacity_mt because the DB already
        holds far more batch tonnage than the 1,000 MT caps — so capacity_forecast
        will read 'critical' for all four until capacities are raised or old stock
        is archived. That is a data condition, not a seeding artefact.
        """
        # fraction of today's tonnage that was present 60 days ago (per warehouse)
        start_frac = {
            "Raw Storage Alpha": 0.78, "Raw Storage Beta": 0.86,
            "Main Assembly Plant": 0.97, "Finished Goods Hub": 0.70,
        }
        used_now = {r["name"]: r["used_mt"] for r in analytics.warehouse_utilization()}
        made = 0
        for wi, w in enumerate(Warehouse.objects.all().order_by("id")):
            cap = float(w.total_capacity_mt or 1000)
            end_used = float(used_now.get(w.name, 0.0))
            begin_used = end_used * start_frac.get(w.name, 0.85)
            for i in range(0, 61):                     # 0 = oldest, 60 = today
                d = TODAY - dt.timedelta(days=60 - i)
                frac = i / 60.0
                used = begin_used + (end_used - begin_used) * frac
                used *= 1 + _noise(wi * 1000 + i, 0.015)      # small deterministic wobble
                used = max(0.0, used)
                util = used / cap * 100 if cap else 0.0
                WarehouseUtilizationSnapshot.objects.update_or_create(
                    warehouse=w, snapshot_date=d,
                    defaults=dict(used_mt=_d(used), capacity_mt=_d(cap),
                                  utilization_percent=_d(util)),
                )
                made += 1
        self._snaps = made

    # ------------------------------------------------------------------ report
    def _report(self):
        w = self.stdout.write
        w(self.style.SUCCESS("\nseed_test_data complete\n" + "=" * 50))
        w(f"  existing supplier/client contacts filled : {self._contacts_fixed}")
        w(f"  suppliers ensured                        : {len(self.suppliers)}")
        w(f"  clients ensured                          : {len(self.clients)}")
        w(f"  SupplierMaterial links                   : {self._suppliermat}")
        w(f"  consumption RegistryLog rows added       : {self._consume_logs}")
        w(f"  raw on-hand batches added                : {self._onhand}")
        w(f"  purchase orders added                    : {self._pos}")
        w(f"  completed production runs added          : {self._runs}")
        w(f"  sales orders added                       : {self._sos}")
        w(f"  stock audits added                       : {self._audits}")
        w(f"  utilization snapshots upserted           : {self._snaps}")

        w("\nanalytics spot-check")
        w("-" * 50)
        sf = analytics.stockout_forecast()
        from collections import Counter
        c = Counter(r["status"] for r in sf)
        w(f"  stockout_forecast statuses : {dict(c)}")
        cf = analytics.capacity_forecast()
        w(f"  capacity_forecast          : " +
          ", ".join(f'{r["name"]}={r["status"]}' for r in cf))
        sr = analytics.supplier_reliability()
        w(f"  supplier_reliability       : " +
          ", ".join(f'{r["supplier_name"]}={r["rating"]}' for r in sr))
        dr = analytics.sales_order_delivery_risk()
        c2 = Counter(r["risk"] for r in dr)
        w(f"  delivery_risk              : {dict(c2)}")
        aa = analytics.audit_accuracy()
        w(f"  audit_accuracy             : {len(aa['by_warehouse'])} warehouses, "
          f"{len(aa['trend'])} month buckets")
        yv = analytics.production_yield_variance()
        w(f"  yield_variance             : " +
          ", ".join(f'{r["name"].split(" - ")[0]}={r["rating"]}' for r in yv["by_product"]))

        # empty-variable audit on analytics-relevant nullable fields
        w("\nempty-variable audit (should all be 0 for seeded rows)")
        w("-" * 50)
        checks = {
            "Supplier w/o email": Supplier.objects.filter(email__isnull=True).count()
                + Supplier.objects.filter(email="").count(),
            "Client w/o email": Client.objects.filter(email__isnull=True).count()
                + Client.objects.filter(email="").count(),
            "SupplierMaterial w/o lead_time": SupplierMaterial.objects.filter(lead_time_days__isnull=True).count(),
            "SupplierMaterial w/o price": SupplierMaterial.objects.filter(unit_price__isnull=True).count(),
            "PO-TS w/o expected_delivery_date": PurchaseOrder.objects.filter(
                po_number__startswith="PO-TS", expected_delivery_date__isnull=True).count(),
            "PO-TS Completed w/o completed_date": PurchaseOrder.objects.filter(
                po_number__startswith="PO-TS", status="Completed", completed_date__isnull=True).count(),
            "PODetail-TS w/o unit_price": PurchaseOrderDetail.objects.filter(
                purchase_order__po_number__startswith="PO-TS", unit_price__isnull=True).count(),
            "SO-TS w/o created_by": SalesOrder.objects.filter(
                so_number__startswith="SO-TS", created_by__isnull=True).count(),
            "SODetail-TS w/o unit_price": SalesOrderDetail.objects.filter(
                sales_order__so_number__startswith="SO-TS", unit_price__isnull=True).count(),
            "RUN-TS Completed w/o actual_yield": ProductionRun.objects.filter(
                run_number__startswith="RUN-TS", status="Completed", actual_yield__isnull=True).count(),
            "RUN-TS w/o supervisor": ProductionRun.objects.filter(
                run_number__startswith="RUN-TS", supervisor__isnull=True).count(),
            "RunMaterialUsage-TS w/o wastage_reason": RunMaterialUsage.objects.filter(
                production_run__run_number__startswith="RUN-TS", wastage_reason="").count(),
            "TS batches w/o warehouse": Batch.objects.filter(
                batch_number__startswith="TS-", warehouse__isnull=True).count(),
            "TS batches w/o location": Batch.objects.filter(
                batch_number__startswith="TS-", location__isnull=True).count()
                + Batch.objects.filter(batch_number__startswith="TS-", location="").count(),
        }
        for k, v in checks.items():
            tag = self.style.SUCCESS("OK ") if v == 0 else self.style.ERROR("BAD")
            w(f"  {tag} {k}: {v}")
