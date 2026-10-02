"""simulate_sales_history: one-off generation of 6 months of dated Sales Order / Shipment history that draws product stock down.

Runs the real fulfillment pipeline (allocate_stock -> Shipment -> deduct_stock_from_allocation), backdated, so audit trails match real orders.
It deducts Batch.quantity and is NOT re-runnable (it refuses if it looks already run).

Run:  python manage.py simulate_sales_history [--dry-run]"""
from __future__ import annotations

import datetime as dt
import random
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core import analytics
from core.models import (
    Batch, Client, CustomUser, OrderTimeline, Product, RegistryLog,
    SalesOrder, SalesOrderDetail, Shipment, ShipmentItem, StockAllocation,
    Warehouse, WarehouseUtilizationSnapshot,
)
from core.utils import allocate_stock, deduct_stock_from_allocation

SO_PREFIX = "SO-HIST"
SHP_PREFIX = "SIM-SHP-OUT"
WINDOW_DAYS = 180
MIN_END_STOCK, MAX_END_STOCK = 150, 900
Q2 = Decimal("0.01")


def _d(x) -> Decimal:
    return Decimal(str(x)).quantize(Q2)


def _aware(d: dt.date, hour: int) -> dt.datetime:
    return timezone.make_aware(dt.datetime.combine(d, dt.time(hour, 0)))


TREND_SHAPES = {
    # 6 relative monthly weights, oldest -> newest month
    "declining": [3.0, 2.6, 2.0, 1.5, 1.0, 0.7],
    "rising":    [0.7, 1.0, 1.5, 2.0, 2.6, 3.0],
    "flat":      [1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
    "volatile":  [2.2, 0.6, 2.4, 0.8, 2.0, 1.0],
}


class Command(BaseCommand):
    help = "Simulate 6 months of dated Sales Orders/Shipments, draining current product stock below 1k."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Plan and print, write nothing.")
        parser.add_argument("--force", action="store_true", help="Run even if SO-HIST orders already exist.")

    def handle(self, *args, **opts):
        dry = opts.get("dry_run")
        if not dry and SalesOrder.objects.filter(so_number__startswith=SO_PREFIX).exists() and not opts.get("force"):
            self.stderr.write(self.style.ERROR(
                f"{SO_PREFIX}* orders already exist - this command deducts real stock and "
                "is not safely re-runnable. Pass --force only if you know what you're doing."
            ))
            return

        today = timezone.now().date()
        self.users = {u.username: u for u in CustomUser.objects.all()}
        self.clients = list(Client.objects.filter(is_active=True))
        self.fg_hub = Warehouse.objects.filter(name="Finished Goods Hub").first()
        creator = self.users.get("sales_rep")
        approver = self.users.get("manager_user")

        plan = self._build_plan(today)
        self._print_plan(plan)
        if dry:
            self.stdout.write(self.style.WARNING("\n--dry-run: no changes written."))
            return

        events = []  # (date, warehouse_id, mt_removed) - for snapshot backfill
        made_orders = 0
        with transaction.atomic():
            for p_plan in plan:
                product = p_plan["product"]
                for order in p_plan["orders"]:
                    so = self._create_and_fulfill_order(product, order, creator, approver, events)
                    if so is not None:
                        made_orders += 1

            self._backfill_snapshots(today, events)

        self._report(plan, made_orders)

    # ------------------------------------------------------------ planning
    def _build_plan(self, today: dt.date):
        rnd = random.Random("simulate-sales-history")
        plan = []
        for p in Product.objects.all().order_by("sku"):
            batches = list(Batch.objects.filter(product=p, status__in=["Active", "Quarantined"]))
            start_stock = sum(b.quantity for b in batches)
            if start_stock <= 0:
                continue

            target_end = Decimal(rnd.randint(MIN_END_STOCK, MAX_END_STOCK))
            if start_stock <= target_end:
                target_end = (start_stock * Decimal(str(rnd.uniform(0.3, 0.7)))).quantize(Q2)
            total_to_sell = (start_stock - target_end).quantize(Q2)
            if total_to_sell <= 0:
                continue

            shape_name = rnd.choice(list(TREND_SHAPES.keys()))
            weights = TREND_SHAPES[shape_name]
            weight_sum = sum(weights)

            orders = []
            for month_idx in range(6):  # 0 = 6 months ago .. 5 = this month
                month_qty = (total_to_sell * Decimal(str(weights[month_idx] / weight_sum))).quantize(Q2)
                if month_qty <= 0:
                    continue
                month_start = today - dt.timedelta(days=WINDOW_DAYS - month_idx * 30)
                month_end = min(today, month_start + dt.timedelta(days=29))
                n_orders = rnd.randint(1, 3)
                splits = self._random_split(month_qty, n_orders, rnd)
                for qty in splits:
                    if qty <= 0:
                        continue
                    span = max(1, (month_end - month_start).days)
                    order_date = month_start + dt.timedelta(days=rnd.randint(0, span))
                    orders.append({"date": min(order_date, today), "qty": qty})

            orders.sort(key=lambda o: o["date"])
            plan.append({
                "product": p, "start_stock": start_stock, "target_end": target_end,
                "total_to_sell": total_to_sell, "shape": shape_name, "orders": orders,
            })
        return plan

    def _random_split(self, total: Decimal, n: int, rnd: random.Random):
        if n <= 1:
            return [total]
        weights = [rnd.random() + 0.2 for _ in range(n)]
        s = sum(weights)
        parts = [(total * Decimal(str(w / s))).quantize(Q2) for w in weights]
        parts[-1] = (total - sum(parts[:-1])).quantize(Q2)  # last part absorbs rounding
        return parts

    # ------------------------------------------------------------ execution
    def _create_and_fulfill_order(self, product, order, creator, approver, events):
        so_number = f"{SO_PREFIX}-{product.sku}-{order['date'].strftime('%y%m%d')}-{random.randint(100,999)}"
        client = random.choice(self.clients) if self.clients else None
        origin = self.fg_hub or Warehouse.objects.first()

        so = SalesOrder.objects.create(
            so_number=so_number, client_name=client.name if client else "Historical Buyer",
            client=client, origin_warehouse=origin, status="Shipped",
            created_by=creator, approved_by=approver,
            approval_remarks="Simulated historical order (simulate_sales_history).",
        )
        SalesOrder.objects.filter(pk=so.pk).update(order_date=order["date"])
        OrderTimeline.objects.create(sales_order=so, action=f"Order created ({so_number})", user=creator)
        OrderTimeline.objects.filter(
            pk=OrderTimeline.objects.filter(sales_order=so).latest("id").pk
        ).update(timestamp=_aware(order["date"], 9))

        # allocate first so we can see exactly which warehouses/batches it drew from
        before = {sa.id for sa in StockAllocation.objects.filter(sales_order=so)}
        allocated = allocate_stock("sales_order", so, product, order["qty"])
        if allocated <= 0:
            so.delete()  # nothing left to sell against - skip silently
            return None

        new_allocs = StockAllocation.objects.filter(sales_order=so).exclude(id__in=before).select_related("batch__warehouse")
        n_allocs = new_allocs.count()
        for sa in new_allocs:
            wh = sa.batch.warehouse
            if wh is not None:
                mt = float(sa.quantity) * float(product.weight_mt_per_unit)
                events.append((order["date"], wh.id, mt))

        qty_final = _d(allocated)
        SalesOrderDetail.objects.create(
            sales_order=so, product=product, quantity_ordered=qty_final,
            quantity_shipped=qty_final, unit_price=product.price_per_unit,
        )

        trk = f"{SHP_PREFIX}-{so_number}"
        sh = Shipment.objects.create(
            tracking_number=trk, direction="Outbound", status="Completed",
            sales_order=so, origin_warehouse=origin,
            client_address=getattr(client, "delivery_address", "") or "",
            client_contact_name=getattr(client, "contact_person", "") or "",
            dispatch_date=order["date"], expected_eta_date=order["date"] + dt.timedelta(days=2),
            actual_arrival_date=order["date"] + dt.timedelta(days=2),
            external_tracking_id=f"HIST-{so.id:06d}",
        )
        ShipmentItem.objects.create(shipment=sh, product=product, quantity=qty_final, received_quantity=qty_final)

        deduct_stock_from_allocation("sales_order", so, user=creator)

        OrderTimeline.objects.create(sales_order=so, action="Shipped and delivered", user=approver)
        OrderTimeline.objects.filter(
            pk=OrderTimeline.objects.filter(sales_order=so).latest("id").pk
        ).update(timestamp=_aware(order["date"] + dt.timedelta(days=2), 15))

        # Back-date the Outbound RegistryLog rows deduct_stock_from_allocation just created (the last n_allocs by id, within this transaction).
        if n_allocs > 0:
            log_ids = list(
                RegistryLog.objects.filter(action_type="Outbound")
                .order_by("-id").values_list("pk", flat=True)[:n_allocs]
            )
            RegistryLog.objects.filter(pk__in=log_ids).update(
                timestamp=_aware(order["date"] + dt.timedelta(days=2), 15)
            )

        return so

    # ------------------------------------------------------------ snapshots
    def _backfill_snapshots(self, today: dt.date, events):
        final_used = {r["name"]: r["used_mt"] for r in analytics.warehouse_utilization()}
        warehouses = list(Warehouse.objects.all())
        removed_by_wh_date = {}
        for date_, wh_id, mt in events:
            removed_by_wh_date.setdefault(wh_id, []).append((date_, mt))

        made = 0
        for w in warehouses:
            cap = float(w.total_capacity_mt or 1000)
            final = float(final_used.get(w.name, 0.0))
            removals = sorted(removed_by_wh_date.get(w.id, []), key=lambda t: t[0])
            for i in range(WINDOW_DAYS):
                d = today - dt.timedelta(days=WINDOW_DAYS - 1 - i)
                still_to_add_back = sum(mt for (rd, mt) in removals if rd > d)
                used = max(0.0, final + still_to_add_back)
                util = used / cap * 100 if cap else 0.0
                WarehouseUtilizationSnapshot.objects.update_or_create(
                    warehouse=w, snapshot_date=d,
                    defaults=dict(used_mt=_d(used), capacity_mt=_d(cap), utilization_percent=_d(util)),
                )
                made += 1
        self._snaps_made = made

    # ------------------------------------------------------------ reporting
    def _print_plan(self, plan):
        w = self.stdout.write
        w("Simulation plan")
        w("-" * 60)
        for p in plan:
            w(f"  {p['product'].sku:10s} shape={p['shape']:10s} "
              f"start={p['start_stock']:>8} -> target={p['target_end']:>8} "
              f"(sell {p['total_to_sell']:>8}) across {len(p['orders'])} orders")

    def _report(self, plan, made_orders):
        w = self.stdout.write
        w(self.style.SUCCESS("\nsimulate_sales_history complete\n" + "=" * 50))
        w(f"  products simulated        : {len(plan)}")
        w(f"  orders created             : {made_orders}")
        w(f"  snapshot rows upserted     : {getattr(self, '_snaps_made', 0)}")
        w("\nfinal stock per product")
        w("-" * 50)
        for p in Product.objects.all().order_by("sku"):
            total = sum(b.quantity for b in Batch.objects.filter(product=p, status__in=["Active", "Quarantined"]))
            tag = self.style.SUCCESS("OK ") if total < 1000 else self.style.ERROR("BAD")
            w(f"  {tag} {p.sku}: {total} units")
