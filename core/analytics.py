"""
core/analytics.py — computation layer for TerraHub's analytics & forecasting.

Pure functions over the ORM: no request/response objects, no side effects.
Used by web views, management commands, the shell, and tests. Algorithms,
row shapes, and status bands are documented in SYSTEM_DOCUMENTATION.md §8.
"""
from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

# RegistryLog.action_type for material draws into a run (see core/utils.py).
CONSUMPTION_ACTION = "Consumed_For_Manufacturing"

# Trailing window used for burn-rate calculations unless the caller overrides it.
DEFAULT_WINDOW_DAYS = 30


# --------------------------------------------------------------------------------
# RegistryLog.item_name -> Material resolution (used by migration 0036's backfill)
# --------------------------------------------------------------------------------

_UPDATED_MATERIAL_RE = re.compile(r"Updated Material '(.+?)'")
_AUDIT_RESOLVE_RE = re.compile(r"^Batch \S+ \(.+? - (.+?)\)$")


def resolve_material_from_label(item_name, by_name):
    """
    Map a free-text RegistryLog.item_name back to a material via ``by_name``
    (keyed by lowercased material name). Returns None if nothing matches.
    """
    if not item_name:
        return None
    s = item_name.strip()
    low = s.lower()

    for sep in (" (Run ", " (Batch "):
        if sep in s:
            cand = s.split(sep, 1)[0].strip().lower()
            if cand in by_name:
                return by_name[cand]

    if low.startswith("manual receipt of "):
        cand = s[len("Manual Receipt of "):].strip().lower()
        if cand in by_name:
            return by_name[cand]

    mo = _UPDATED_MATERIAL_RE.search(s)
    if mo and mo.group(1).strip().lower() in by_name:
        return by_name[mo.group(1).strip().lower()]

    mo = _AUDIT_RESOLVE_RE.match(s)
    if mo and mo.group(1).strip().lower() in by_name:
        return by_name[mo.group(1).strip().lower()]

    if low in by_name:
        return by_name[low]

    return None


def daily_consumption(material, *, window_days: int = DEFAULT_WINDOW_DAYS, end=None) -> dict:
    """
    {date: Decimal} of material consumed for manufacturing per day over the
    trailing window ending on ``end``. Untagged RegistryLog rows are skipped.
    """
    end = end or timezone.localdate()
    start = end - _dt.timedelta(days=window_days)
    rows = (
        material.registry_logs.filter(
            action_type=CONSUMPTION_ACTION,
            timestamp__date__gt=start,
            timestamp__date__lte=end,
        )
        .values_list("timestamp__date")
        .annotate(total=Sum("quantity_changed"))
    )
    return {day: (total or Decimal("0")) for day, total in rows}


def consumption_rate(material, *, window_days: int = DEFAULT_WINDOW_DAYS, end=None) -> Decimal:
    """Mean daily consumption over the window (idle days included); 0 if none or window_days <= 0."""
    if window_days <= 0:
        return Decimal("0")
    series = daily_consumption(material, window_days=window_days, end=end)
    total = sum(series.values(), Decimal("0"))
    if total <= 0:
        return Decimal("0")
    return total / Decimal(window_days)


# --------------------------------------------------------------------------------
# Tier 1 - Supplier reliability scorecard
# --------------------------------------------------------------------------------

# Statuses with real fulfilment activity - Draft/Pending Approval/Rejected excluded.
SCORECARD_PO_STATUSES = ("Pending", "Partially Received", "Completed")
_OPEN_PO_STATUSES = ("Pending", "Partially Received")

_RATING_RANK = {"poor": 0, "watch": 1, "good": 2, "n/a": 3}


def _po_actual_arrival(po):
    """Best available "goods arrived" date for a PO: completed_date, else the
    latest inbound-shipment arrival, else the latest received-batch date."""
    if po.completed_date is not None:
        return po.completed_date
    dates = [s.actual_arrival_date for s in po.shipments.all() if s.actual_arrival_date]
    if dates:
        return max(dates)
    batch_dates = [b.manufacturing_date for b in po.received_batches.all() if b.manufacturing_date]
    if batch_dates:
        return max(batch_dates)
    return None


def _supplier_lead_map():
    """{(supplier_id, material_id): lead_time_days} for pairs that have one on file."""
    from .models import SupplierMaterial

    return {
        (sm.supplier_id, sm.material_id): sm.lead_time_days
        for sm in SupplierMaterial.objects.filter(lead_time_days__isnull=False)
    }


def _expected_delivery(po, lead_map, default_lead_days):
    """(expected_date, is_estimated) - the PO's own expected_delivery_date if
    set, else order_date + a lead time (per-supplier/material, else default)."""
    if po.expected_delivery_date is not None:
        return po.expected_delivery_date, False
    if po.order_date is None:
        return None, False
    leads = []
    if po.supplier_id:
        for it in po.items.all():
            lt = lead_map.get((po.supplier_id, it.material_id))
            if lt:
                leads.append(lt)
    lead = max(leads) if leads else default_lead_days
    return po.order_date + _dt.timedelta(days=lead), True


def _rate_supplier(on_time_rate, fill_rate):
    """Blend on-time and fill performance into good / watch / poor / n/a."""
    if on_time_rate is None and fill_rate is None:
        return "n/a"
    ot = 1.0 if on_time_rate is None else on_time_rate
    fr = 1.0 if fill_rate is None else fill_rate
    if ot >= 0.9 and fr >= 0.98:
        return "good"
    if ot < 0.7 or fr < 0.9:
        return "poor"
    return "watch"


def supplier_reliability(*, since=None, until=None):
    """
    Per-supplier fulfilment scorecard, worst first. ``since``/``until`` bound
    the POs by order_date. See SYSTEM_DOCUMENTATION.md §8.3 for the row shape.
    """
    from .models import PurchaseOrder
    from .settings_store import get_setting

    default_lead = get_setting("po_default_lead_time_days")
    lead_map = _supplier_lead_map()

    pos = (
        PurchaseOrder.objects.filter(status__in=SCORECARD_PO_STATUSES)
        .select_related("supplier")
        .prefetch_related("items", "shipments", "received_batches")
    )
    if since is not None:
        pos = pos.filter(order_date__gte=since)
    if until is not None:
        pos = pos.filter(order_date__lte=until)

    buckets = {}
    for po in pos:
        if po.supplier_id:
            key = ("id", po.supplier_id)
            name, is_active = po.supplier.name, po.supplier.is_active
        else:
            nm = (po.supplier_name or "Unspecified").strip() or "Unspecified"
            key, name, is_active = ("name", nm.lower()), nm, None

        b = buckets.setdefault(key, {
            "supplier_id": po.supplier_id or None,
            "supplier_name": name,
            "is_active": is_active,
            "po_count": 0, "open_count": 0, "completed_count": 0,
            "ordered_qty": Decimal("0"), "received_qty": Decimal("0"),
            "assessable_count": 0, "on_time_count": 0, "late_count": 0,
            "estimated_count": 0,
            "_delays": [], "_arrivals": [],
        })

        b["po_count"] += 1
        if po.status in _OPEN_PO_STATUSES:
            b["open_count"] += 1
        elif po.status == "Completed":
            b["completed_count"] += 1

        for it in po.items.all():
            b["ordered_qty"] += it.quantity_ordered or Decimal("0")
            b["received_qty"] += it.quantity_received or Decimal("0")

        arrival = _po_actual_arrival(po)
        if arrival is not None:
            b["_arrivals"].append(arrival)
            expected, estimated = _expected_delivery(po, lead_map, default_lead)
            if expected is not None:
                delay = (arrival - expected).days
                b["assessable_count"] += 1
                b["_delays"].append(delay)
                if estimated:
                    b["estimated_count"] += 1
                if delay <= 0:
                    b["on_time_count"] += 1
                else:
                    b["late_count"] += 1

    rows = []
    for b in buckets.values():
        ordered = b.pop("ordered_qty")
        received = b.pop("received_qty")
        delays = b.pop("_delays")
        arrivals = b.pop("_arrivals")
        b["ordered_qty"] = float(ordered)
        b["received_qty"] = float(received)
        b["fill_rate"] = float(received) / float(ordered) if ordered > 0 else None
        b["on_time_rate"] = b["on_time_count"] / b["assessable_count"] if b["assessable_count"] else None
        b["estimated_share"] = b["estimated_count"] / b["assessable_count"] if b["assessable_count"] else None
        b["avg_delay_days"] = sum(delays) / len(delays) if delays else None
        b["last_delivery"] = max(arrivals) if arrivals else None
        b["rating"] = _rate_supplier(b["on_time_rate"], b["fill_rate"])
        rows.append(b)

    rows.sort(key=lambda r: (
        _RATING_RANK[r["rating"]],
        r["on_time_rate"] if r["on_time_rate"] is not None else 1.0,
        r["fill_rate"] if r["fill_rate"] is not None else 1.0,
        r["supplier_name"].lower(),
    ))
    return rows


# --------------------------------------------------------------------------------
# Tier 1 - Sales-order delivery risk
# --------------------------------------------------------------------------------

# Still "in flight" and worth risk-tracking; Delivered/Rejected/Draft excluded.
_OPEN_SO_STATUSES = (
    "Pending", "Awaiting Acknowledgement", "In Production",
    "Ready to Ship", "Partially Shipped", "Shipped",
)
# Pre-shipment statuses where production/prep stands between the SO and its deadline.
_PRE_SHIPMENT_SO_STATUSES = ("Pending", "Awaiting Acknowledgement", "In Production")

_RISK_RANK = {"late": 0, "at_risk": 1, "on_track": 2, "no_deadline": 3}


def _so_projected_arrival(so):
    """(date, is_actual) best estimate of arrival from outbound shipments -
    an actual_arrival_date if landed, else the latest expected_eta_date."""
    actual, expected = [], []
    for s in so.shipments.all():
        if s.direction != "Outbound":
            continue
        if s.actual_arrival_date:
            actual.append(s.actual_arrival_date)
        elif s.expected_eta_date:
            expected.append(s.expected_eta_date)
    if actual:
        return max(actual), True
    if expected:
        return max(expected), False
    return None, False


def _classify_so_risk(deadline, arrival, arrival_is_actual, shipped_fraction,
                      status, today, at_risk_window):
    if deadline is None:
        return "no_deadline"
    if arrival is not None:
        if arrival <= deadline:
            return "on_track"
        # projected/known to miss the deadline
        return "late" if (arrival_is_actual or shipped_fraction >= 1.0) else "at_risk"
    # nothing shipped yet
    days_left = (deadline - today).days
    if days_left < 0:
        return "late"
    if days_left <= at_risk_window and status in _PRE_SHIPMENT_SO_STATUSES:
        return "at_risk"
    return "on_track"


def sales_order_delivery_risk():
    """Delivery-risk row per open sales order, worst first. See §8.4 for the row shape."""
    from .models import SalesOrder
    from .settings_store import get_setting

    at_risk_window = get_setting("so_at_risk_window_days")
    today = timezone.localdate()

    sos = (
        SalesOrder.objects.filter(status__in=_OPEN_SO_STATUSES)
        .select_related("client")
        .prefetch_related("items", "shipments", "production_runs")
        .order_by("fulfillment_deadline")
    )

    rows = []
    for so in sos:
        ordered = sum((it.quantity_ordered or Decimal("0") for it in so.items.all()), Decimal("0"))
        shipped = sum((it.quantity_shipped or Decimal("0") for it in so.items.all()), Decimal("0"))
        shipped_fraction = float(shipped) / float(ordered) if ordered > 0 else 0.0

        arrival, arrival_is_actual = _so_projected_arrival(so)
        risk = _classify_so_risk(
            so.fulfillment_deadline, arrival, arrival_is_actual,
            shipped_fraction, so.status, today, at_risk_window,
        )

        if so.fulfillment_deadline is not None:
            ref = arrival or today
            days_slack = (so.fulfillment_deadline - ref).days
        else:
            days_slack = None

        runs = list(so.production_runs.all())
        if runs:
            done = sum(1 for r in runs if r.status == "Completed")
            production_summary = f"{len(runs)} run{'s' if len(runs) != 1 else ''}, {done} completed"
        else:
            production_summary = None

        rows.append({
            "so_id": so.id,
            "so_number": so.so_number,
            "client_name": so.client_name,
            "status": so.status,
            "deadline": so.fulfillment_deadline,
            "arrival": arrival,
            "arrival_is_actual": arrival_is_actual,
            "shipped_fraction": shipped_fraction,
            "days_slack": days_slack,
            "production_summary": production_summary,
            "risk": risk,
        })

    rows.sort(key=lambda r: (
        _RISK_RANK[r["risk"]],
        r["days_slack"] if r["days_slack"] is not None else 10**6,
        r["so_number"],
    ))
    return rows


# --------------------------------------------------------------------------------
# Tier 1 - Stock-audit accuracy
# --------------------------------------------------------------------------------


def _audit_bucket_init(name):
    return {
        "name": name, "audit_count": 0, "exact_count": 0,
        "net_variance": 0.0, "shrinkage": 0.0, "overage": 0.0, "abs_variance": 0.0,
    }


def _audit_finalize(b):
    n = b["audit_count"]
    b["accuracy_rate"] = b["exact_count"] / n if n else None
    b["mean_abs_variance"] = b["abs_variance"] / n if n else None
    b["chronic_shrinkage"] = b["net_variance"] < 0 and n >= 3
    del b["abs_variance"]
    return b


def audit_accuracy(*, since=None):
    """Stock-count accuracy from StockAudit, grouped by warehouse/item plus a
    monthly trend, worst first. See §8.5 for the row shape."""
    from .models import StockAudit

    audits = StockAudit.objects.select_related(
        "batch", "batch__material", "batch__product", "batch__warehouse"
    )
    if since is not None:
        audits = audits.filter(audit_date__date__gte=since)

    by_wh, by_item, by_month = {}, {}, {}
    for a in audits:
        var = float(a.actual_quantity) - float(a.expected_quantity)
        batch = a.batch
        wh_name = batch.warehouse.name if batch and batch.warehouse else "Unassigned"
        if batch and batch.material:
            item_name = f"{batch.material.sku} - {batch.material.name}"
        elif batch and batch.product:
            item_name = f"{batch.product.sku} - {batch.product.name}"
        else:
            item_name = "Unknown item"
        month = a.audit_date.strftime("%Y-%m")

        for key, store in ((wh_name, by_wh), (item_name, by_item)):
            b = store.setdefault(key, _audit_bucket_init(key))
            b["audit_count"] += 1
            b["abs_variance"] += abs(var)
            b["net_variance"] += var
            if var == 0:
                b["exact_count"] += 1
            elif var < 0:
                b["shrinkage"] += var
            else:
                b["overage"] += var

        m = by_month.setdefault(month, {"bucket": month, "audit_count": 0,
                                        "abs_variance": 0.0, "net_variance": 0.0})
        m["audit_count"] += 1
        m["abs_variance"] += abs(var)
        m["net_variance"] += var

    def _sort_key(r):
        return (
            r["accuracy_rate"] if r["accuracy_rate"] is not None else 1.0,
            -(r["mean_abs_variance"] or 0.0),
            r["name"].lower(),
        )

    by_warehouse = sorted((_audit_finalize(b) for b in by_wh.values()), key=_sort_key)
    by_item = sorted((_audit_finalize(b) for b in by_item.values()), key=_sort_key)

    trend = []
    for m in sorted(by_month.values(), key=lambda x: x["bucket"]):
        n = m["audit_count"]
        trend.append({
            "bucket": m["bucket"],
            "audit_count": n,
            "mean_abs_variance": m["abs_variance"] / n if n else 0.0,
            "net_variance": m["net_variance"],
        })

    return {"by_warehouse": by_warehouse, "by_item": by_item, "trend": trend}


# --------------------------------------------------------------------------------
# Tier 1 - Production yield variance
# --------------------------------------------------------------------------------

_YIELD_RATING_RANK = {"poor": 0, "watch": 1, "good": 2, "n/a": 3}


def _rate_yield(mean_yield_variance_pct, mean_material_overuse_pct):
    if mean_yield_variance_pct is None:
        return "n/a"
    ov = mean_material_overuse_pct or 0.0
    if mean_yield_variance_pct >= -2 and ov <= 5:
        return "good"
    if mean_yield_variance_pct < -10 or ov > 15:
        return "poor"
    return "watch"


def _yield_bucket_init(name):
    return {
        "name": name, "run_count": 0,
        "expected_total": 0.0, "actual_total": 0.0,
        "_yield_var": [], "_mat_overuse": [],
    }


def production_yield_variance():
    """Yield performance of completed runs, grouped by product and supervisor,
    worst first. See §8.6 for the row shape."""
    from .models import ProductionRun

    runs = (
        ProductionRun.objects.filter(status="Completed", actual_yield__isnull=False)
        .select_related("target_product", "supervisor")
        .prefetch_related("material_usages")
    )

    by_product, by_supervisor = {}, {}
    for run in runs:
        exp = float(run.expected_yield or 0)
        act = float(run.actual_yield or 0)
        if exp <= 0:
            continue
        yield_var_pct = (act - exp) / exp * 100.0

        usages = list(run.material_usages.all())
        mat_overuse = (
            sum(float(u.variance_pct) for u in usages) / len(usages) if usages else None
        )

        prod_name = f"{run.target_product.sku} - {run.target_product.name}"
        sup_name = run.supervisor.get_username() if run.supervisor else "Unassigned"

        for key, store in ((prod_name, by_product), (sup_name, by_supervisor)):
            b = store.setdefault(key, _yield_bucket_init(key))
            b["run_count"] += 1
            b["expected_total"] += exp
            b["actual_total"] += act
            b["_yield_var"].append(yield_var_pct)
            if mat_overuse is not None:
                b["_mat_overuse"].append(mat_overuse)

    def _finalize(b):
        yv = b.pop("_yield_var")
        mo = b.pop("_mat_overuse")
        b["mean_yield_variance_pct"] = sum(yv) / len(yv) if yv else None
        b["mean_material_overuse_pct"] = sum(mo) / len(mo) if mo else None
        b["mean_yield_pct"] = (
            b["actual_total"] / b["expected_total"] * 100.0 if b["expected_total"] else None
        )
        b["rating"] = _rate_yield(b["mean_yield_variance_pct"], b["mean_material_overuse_pct"])
        return b

    def _sort_key(r):
        return (
            _YIELD_RATING_RANK[r["rating"]],
            r["mean_yield_variance_pct"] if r["mean_yield_variance_pct"] is not None else 0.0,
            -(r["mean_material_overuse_pct"] or 0.0),
            r["name"].lower(),
        )

    return {
        "by_product": sorted((_finalize(b) for b in by_product.values()), key=_sort_key),
        "by_supervisor": sorted((_finalize(b) for b in by_supervisor.values()), key=_sort_key),
    }


def unrecorded_material_usage(recent_days=7, window_days=28, now=None):
    """Material poured into runs from stock that isn't in the records (the "batch not
    in records" option when a run uses more than was allocated), per material, worst
    first. Each row: events / quantity over the recent window and the longer one, the
    share of what was poured in completed runs over the longer window, the most common
    reasons, who entered them, and the runs involved. Nothing is deducted from stock for
    these, so a high share means inventory records and the shop floor are drifting apart."""
    from collections import Counter

    from .models import RunExtraMaterial, RunMaterialUsage

    now = now or timezone.now()
    recent_cut = now - _dt.timedelta(days=recent_days)
    window_cut = now - _dt.timedelta(days=window_days)

    entries = (
        RunExtraMaterial.objects.filter(batch__isnull=True, created_at__gte=window_cut)
        .select_related("material", "production_run", "recorded_by")
        .order_by("-created_at")
    )

    buckets = {}
    for e in entries:
        qty = float(e.quantity)
        b = buckets.setdefault(e.material_id, {
            "material_id": e.material_id, "name": e.material.name, "sku": e.material.sku,
            "unit": e.material.unit_of_measure,
            "events_recent": 0, "qty_recent": 0.0, "events_window": 0, "qty_window": 0.0,
            "_reasons": Counter(), "_reason_label": {}, "_people": Counter(), "runs": [],
        })
        b["events_window"] += 1
        b["qty_window"] += qty
        if e.created_at >= recent_cut:
            b["events_recent"] += 1
            b["qty_recent"] += qty
        reason = (e.reason or "").strip()
        if reason:
            key = reason.lower()
            b["_reasons"][key] += 1
            b["_reason_label"].setdefault(key, reason)
        person = (e.recorded_by.get_full_name() or e.recorded_by.get_username()) if e.recorded_by else "Unknown"
        b["_people"][person] += 1
        run = e.production_run
        if all(r["pk"] != run.pk for r in b["runs"]):
            b["runs"].append({"pk": run.pk, "number": run.run_number})

    poured = {
        row["material_id"]: float(row["s"] or 0)
        for row in RunMaterialUsage.objects.filter(
            material_id__in=list(buckets),
            production_run__status="Completed",
            production_run__exact_end_time__gte=window_cut,
        ).values("material_id").annotate(s=Sum("actual_qty"))
    }

    rows = []
    for b in buckets.values():
        reasons, labels, people = b.pop("_reasons"), b.pop("_reason_label"), b.pop("_people")
        total = poured.get(b["material_id"], 0.0)
        b["share_pct"] = min(b["qty_window"] / total * 100.0, 100.0) if total > 0 else None
        b["top_reasons"] = [(labels[k], n) for k, n in reasons.most_common(3)]
        b["top_people"] = people.most_common(2)
        b["runs"] = b["runs"][:5]
        rows.append(b)

    # Highest share first (unknown share last), then the biggest quantities
    rows.sort(key=lambda r: (r["share_pct"] is None, -(r["share_pct"] or 0.0), -r["qty_window"], r["name"].lower()))
    return {
        "rows": rows,
        "events_recent": sum(r["events_recent"] for r in rows),
        "materials_recent": sum(1 for r in rows if r["events_recent"]),
        "events_window": sum(r["events_window"] for r in rows),
        "recent_days": recent_days,
        "window_days": window_days,
    }


# --------------------------------------------------------------------------------
# Tier 2 - Consumption-rate stockout forecast
# --------------------------------------------------------------------------------

_STOCKOUT_RANK = {"critical": 0, "reorder_now": 1, "watch": 2, "ok": 3, "no_usage": 4}

# Days-until-reorder bands: <= _REORDER_NOW_DAYS is "reorder now", <= _REORDER_WATCH_DAYS is "watch".
_REORDER_NOW_DAYS = 2
_REORDER_WATCH_DAYS = 14

# Beyond this many days of cover, don't bother projecting a date - it's noise.
_FORECAST_HORIZON_DAYS = 3650


def consumption_rates(material_ids, *, window_days=DEFAULT_WINDOW_DAYS, end=None):
    """Mean daily consumption for many materials in one query. {material_id:
    Decimal}, absent if there was no consumption in the window."""
    ids = list(material_ids)
    if not ids or window_days <= 0:
        return {}
    from .models import RegistryLog

    end = end or timezone.localdate()
    start = end - _dt.timedelta(days=window_days)
    rows = (
        RegistryLog.objects.filter(
            action_type=CONSUMPTION_ACTION,
            material_id__in=ids,
            timestamp__date__gt=start,
            timestamp__date__lte=end,
        )
        .values("material_id")
        .annotate(total=Sum("quantity_changed"))
    )
    w = Decimal(window_days)
    out = {}
    for r in rows:
        total = r["total"] or Decimal("0")
        if total > 0:
            out[r["material_id"]] = total / w
    return out


def _project_cover(available, rate, arrivals, today):
    """Days until stock runs out, drawing `available` down at `rate`/day and adding
    each open-PO delivery on its expected date. A delivery only helps if it lands
    before the stock is gone - one that arrives after that doesn't prevent the
    stockout. `arrivals` = [(date, qty), ...]; overdue dates count as today.
    Returns (days_cover, counted_qty, late_qty)."""
    rate = float(rate)
    cover = max(float(available), 0.0) / rate
    counted = late = 0.0
    for when, qty in sorted(arrivals, key=lambda x: x[0]):
        day = max((when - today).days, 0)
        if day <= cover:
            cover += float(qty) / rate
            counted += float(qty)
        else:
            late += float(qty)
    return cover, counted, late


def stockout_forecast(*, window_days=DEFAULT_WINDOW_DAYS, end=None):
    """Per active material: days of cover, stockout date, and reorder-by date
    from burn rate, counting open-PO deliveries that arrive before the stock runs
    out. Worst first - see §8.7 for the row shape and status bands."""
    from django.db.models import Max
    from .models import Material, Batch, SupplierMaterial, PurchaseOrderDetail
    from .settings_store import get_setting

    today = end or timezone.localdate()
    materials = list(Material.objects.filter(is_active=True))
    ids = [m.id for m in materials]

    on_hand = {
        r["material_id"]: (r["oh"] or Decimal("0"), r["al"] or Decimal("0"))
        for r in Batch.objects.filter(status="Active", material_id__in=ids)
        .values("material_id")
        .annotate(oh=Sum("quantity"), al=Sum("allocated_quantity"))
    }
    lead_map = {
        r["material_id"]: r["lt"]
        for r in SupplierMaterial.objects.filter(
            material_id__in=ids, lead_time_days__isnull=False
        ).values("material_id").annotate(lt=Max("lead_time_days"))
    }
    default_lead = get_setting("po_default_lead_time_days")

    # Open PO lines -> expected arrivals per material (same expected-date rule as
    # the supplier scorecard: the PO's own date, else order date + lead time).
    supplier_leads = _supplier_lead_map()
    arrivals = {}
    for d in (PurchaseOrderDetail.objects
              .filter(material_id__in=ids, purchase_order__status__in=["Pending", "Partially Received"])
              .select_related("purchase_order").prefetch_related("purchase_order__items")):
        remaining = d.quantity_ordered - d.quantity_received
        if remaining <= 0:
            continue
        expected, estimated = _expected_delivery(d.purchase_order, supplier_leads, default_lead)
        arrivals.setdefault(d.material_id, []).append({
            "date": expected or today, "qty": remaining, "estimated": estimated,
            "po_number": d.purchase_order.po_number,
        })

    rates = consumption_rates(ids, window_days=window_days, end=today)

    rows = []
    for m in materials:
        oh, al = on_hand.get(m.id, (Decimal("0"), Decimal("0")))
        available = oh - al
        rate = rates.get(m.id)
        lt = lead_map.get(m.id)
        lead_estimated = lt is None
        lead_days = int(lt) if lt is not None else int(default_lead)
        incoming = arrivals.get(m.id, [])
        on_order = sum((a["qty"] for a in incoming), Decimal("0"))
        next_po = min(incoming, key=lambda a: a["date"]) if incoming else None
        counted = late = 0.0

        if not rate or rate <= 0:
            status = "no_usage"
            days_cover = None
            stockout_date = None
            reorder_by = None
        else:
            days_cover, counted, late = _project_cover(
                available, rate, [(a["date"], a["qty"]) for a in incoming], today
            )
            if days_cover > _FORECAST_HORIZON_DAYS:
                # effectively never at this burn rate; don't project a fake date
                days_cover = None
                stockout_date = None
                reorder_by = None
                status = "ok"
            else:
                stockout_date = today + _dt.timedelta(days=round(days_cover))
                reorder_by = stockout_date - _dt.timedelta(days=lead_days)
                days_until_reorder = days_cover - lead_days
                if days_cover <= 0 or days_until_reorder < 0:
                    status = "critical"   # already out, or can't reorder in time
                elif days_until_reorder <= _REORDER_NOW_DAYS:
                    status = "reorder_now"
                elif days_until_reorder <= _REORDER_WATCH_DAYS:
                    status = "watch"
                else:
                    status = "ok"

        rows.append({
            "material_id": m.id,
            "sku": m.sku,
            "name": m.name,
            "unit": m.unit_of_measure,
            "on_hand": float(oh),
            "allocated": float(al),
            "available": float(available),
            "on_order": float(on_order),
            # how much of on_order lands before the stock runs out (counted in
            # days_cover) vs after it (too late to prevent the stockout)
            "on_order_counted": counted,
            "on_order_late": late,
            "next_po_number": next_po["po_number"] if next_po else None,
            "next_po_date": next_po["date"] if next_po else None,
            "next_po_date_estimated": next_po["estimated"] if next_po else False,
            "po_overdue": any(a["date"] < today for a in incoming),
            "daily_rate": float(rate) if rate else 0.0,
            "days_cover": days_cover,
            "days_cover_on_hand": (max(float(available), 0.0) / float(rate)) if rate and rate > 0 else None,
            "days_until_reorder": (days_cover - lead_days) if days_cover is not None else None,
            "stockout_date": stockout_date,
            "lead_time_days": lead_days,
            "lead_time_estimated": lead_estimated,
            "reorder_by_date": reorder_by,
            "safe_storage_days": m.safe_storage_days,
            "status": status,
        })

    rows.sort(key=lambda r: (
        _STOCKOUT_RANK[r["status"]],
        r["reorder_by_date"] or _dt.date.max,
        -r["daily_rate"],
        r["name"].lower(),
    ))
    return rows


# --------------------------------------------------------------------------------
# Tier 2 - Warehouse capacity runway
# --------------------------------------------------------------------------------

_CAPACITY_RANK = {"critical": 0, "watch": 1, "ok": 2, "stable": 3, "no_data": 4}

# Below this many snapshots there is no trend to fit.
_MIN_SNAPSHOTS = 3
# Slopes flatter than this (percentage points per day) count as "not filling".
_FLAT_SLOPE_PP = 0.02
_CAPACITY_HORIZON_DAYS = 3650


def _tonnage_expr(statuses):
    """ORM expression for a Warehouse's tonnage (quantity x unit weight) over its
    batches in `statuses`."""
    from django.db.models import Case, When, F, Value, DecimalField, Q
    from django.db.models.functions import Coalesce

    in_status = Q(batches__status__in=statuses)
    return Coalesce(
        Sum(Case(
            When(in_status & Q(batches__material__isnull=False),
                 then=F('batches__quantity') * F('batches__material__weight_mt_per_unit')),
            When(in_status & Q(batches__product__isnull=False),
                 then=F('batches__quantity') * F('batches__product__weight_mt_per_unit')),
            default=Value(0), output_field=DecimalField(),
        )),
        Value(0, output_field=DecimalField()),
    )


def used_mt_expr():
    """ORM expression for a Warehouse's occupied tonnage: Active AND Quarantined
    stock - a quarantined batch still takes up space. Shared by dashboard_view,
    facility management and warehouse_utilization()."""
    return _tonnage_expr(['Active', 'Quarantined'])


def quarantined_mt_expr():
    """The Quarantined share of used_mt_expr(), so pages can show how much of the
    occupied space is on hold."""
    return _tonnage_expr(['Quarantined'])


def warehouse_utilization():
    """Current point-in-time utilization per warehouse: [{warehouse_id, name,
    used_mt, capacity_mt, utilization_percent}, ...]."""
    from .models import Warehouse

    rows = []
    for w in Warehouse.objects.annotate(used_mt=used_mt_expr(), quarantined_mt=quarantined_mt_expr()).order_by('name'):
        used = float(w.used_mt or 0)
        cap = float(w.total_capacity_mt or 0)
        rows.append({
            "warehouse_id": w.id,
            "name": w.name,
            "used_mt": round(used, 3),
            "quarantined_mt": round(float(w.quarantined_mt or 0), 3),
            "capacity_mt": round(cap, 3),
            "utilization_percent": round(used / cap * 100, 2) if cap > 0 else 0.0,
        })
    return rows


def snapshot_warehouse_utilization(snap_date=None, warehouse_ids=None):
    """Records each warehouse's current occupancy and daily rent into
    WarehouseUtilizationSnapshot for snap_date (defaults to today, Malaysia time).
    Idempotent per (warehouse, date): later calls the same day overwrite, so the
    last write of the day is that day's figure. Called by core/signals.py whenever
    stock or a warehouse changes (warehouse_ids = just the affected ones), by
    ensure_today_snapshots(), and by the `snapshot_utilization` command.
    Returns (warehouse_count, snap_date)."""
    from .models import WarehouseUtilizationSnapshot

    snap_date = snap_date or timezone.localdate()
    rows = warehouse_rent_burn(warehouse_ids=warehouse_ids)
    for r in rows:
        cap = r["capacity_mt"]
        WarehouseUtilizationSnapshot.objects.update_or_create(
            warehouse_id=r["warehouse_id"],
            snapshot_date=snap_date,
            defaults={
                "used_mt": r["used_mt"],
                "quarantined_mt": r["quarantined_mt"],
                "capacity_mt": cap,
                "utilization_percent": round(r["used_mt"] / cap * 100, 2) if cap > 0 else 0.0,
                "daily_rent_cost": r["daily_cost"],
                "billing_mode": r["billing_mode"],
                "rent_estimated": False,
            },
        )
    return len(rows), snap_date


def ensure_today_snapshots():
    """Safety net for the save-on-change snapshots: if any warehouse has no row for
    today yet (nothing has changed today, or a bulk update skipped the signals),
    write them now. Cheap no-op once today's rows exist."""
    from .models import Warehouse, WarehouseUtilizationSnapshot

    today = timezone.localdate()
    have = set(WarehouseUtilizationSnapshot.objects.filter(snapshot_date=today).values_list('warehouse_id', flat=True))
    missing = set(Warehouse.objects.values_list('id', flat=True)) - have
    if missing:
        snapshot_warehouse_utilization(warehouse_ids=missing)


def estimate_snapshot_rent(snapshot, warehouse):
    """(daily_cost, billing_mode) for a snapshot row that has no recorded rent:
    the day's tonnage x the warehouse's CURRENT rate. Same rule as migration 0058."""
    if warehouse.ownership_type == 'Internal':
        return 0.0, 'Internal'
    if warehouse.rental_billing_method == 'Overall':
        return float(warehouse.total_capacity_mt * warehouse.rental_cost_per_mt), 'Overall Capacity'
    return float(snapshot.used_mt * warehouse.rental_cost_per_mt), 'Usage'


def rent_history(days=180, end=None):
    """Daily rent per rented warehouse for the last `days` days, for the chart on
    Rent Opportunities. A day with no snapshot carries the previous day's value
    forward (nothing changed that day). Rows without recorded rent are estimated
    (estimate_snapshot_rent) and flagged. Warehouses that never cost anything in
    the window (our own) are left out."""
    from .models import Warehouse, WarehouseUtilizationSnapshot

    end = end or timezone.localdate()
    start = end - _dt.timedelta(days=days - 1)
    dates = [start + _dt.timedelta(days=i) for i in range(days)]
    warehouses = {w.id: w for w in Warehouse.objects.all()}

    by_wh = {}
    for snap in WarehouseUtilizationSnapshot.objects.filter(snapshot_date__lte=end).order_by('snapshot_date'):
        w = warehouses.get(snap.warehouse_id)
        if w is None:
            continue
        if snap.daily_rent_cost is None:
            cost, estimated = estimate_snapshot_rent(snap, w)[0], True
        else:
            cost, estimated = float(snap.daily_rent_cost), snap.rent_estimated
        by_wh.setdefault(w.id, []).append((snap.snapshot_date, cost, estimated))

    series = []
    for wid, points in by_wh.items():
        values, estimated_flags = [], []
        i, last = 0, None
        for d in dates:
            while i < len(points) and points[i][0] <= d:
                last = points[i]
                i += 1
            values.append(round(last[1], 2) if last else None)
            estimated_flags.append(bool(last and last[2]))
        if any(v for v in values):
            series.append({'warehouse_id': wid, 'name': warehouses[wid].name,
                           'values': values, 'estimated': estimated_flags})
    series.sort(key=lambda x: x['name'].lower())

    daily_total = [round(sum((srs['values'][i] or 0) for srs in series), 2) for i in range(days)]
    month_start = end.replace(day=1)
    prev_month_end = month_start - _dt.timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)
    this_month = sum(t for d, t in zip(dates, daily_total) if d >= month_start)
    last_month_days = [(d, t) for d, t in zip(dates, daily_total) if prev_month_start <= d <= prev_month_end]
    last_30 = daily_total[-30:]
    return {
        'dates': [d.isoformat() for d in dates],
        'series': series,
        'daily_total': daily_total,
        'this_month': round(this_month, 2),
        'last_month': round(sum(t for _, t in last_month_days), 2) if last_month_days else None,
        'avg_per_day_30d': round(sum(last_30) / len(last_30), 2) if last_30 else 0.0,
        'has_estimates': any(any(srs['estimated']) for srs in series),
    }


RESULT_WINDOW_DAYS = 7          # days averaged before and after a move
RESULT_MIN_DAYS_AFTER = 3       # wait for this many days after the move before judging it


def _recorded_days(warehouse_id, start, end):
    """[(rent, used_mt) or None] for each day start..end from the rows recorded on
    the day (never the estimated backfill). A day with no row carries the latest
    earlier row forward, since snapshots are only written when something changes."""
    from .models import WarehouseUtilizationSnapshot

    qs = WarehouseUtilizationSnapshot.objects.filter(
        warehouse_id=warehouse_id, daily_rent_cost__isnull=False, rent_estimated=False,
    )
    prior = qs.filter(snapshot_date__lt=start).order_by('-snapshot_date').first()
    by_date = {s.snapshot_date: s for s in qs.filter(snapshot_date__gte=start, snapshot_date__lte=end)}
    last, out = prior, []
    for i in range((end - start).days + 1):
        last = by_date.get(start + _dt.timedelta(days=i), last)
        out.append((float(last.daily_rent_cost), float(last.used_mt)) if last else None)
    return out


def _window_avg(days):
    """(avg rent, avg used_mt) over the known days, or None if none are known."""
    known = [d for d in days if d]
    if not known:
        return None
    return sum(d[0] for d in known) / len(known), sum(d[1] for d in known) / len(known)


def rent_results(limit=10):
    """DSS #11 - did the accepted Rent Opportunities moves save what they promised?

    One row per accepted transfer. Once the transfer is completed, the origin's
    average recorded daily rent for the week before is compared with the week after
    (the destination's rent change is netted off); `promised` is the sum of the
    estimates stored when it was accepted. A move is marked "unclear" when the
    origin's tonnage changed by more than half the move's size for some other
    reason, since the rent change can't then be put down to the move alone.
    Statuses: measuring (not completed / too soon), measured, unclear, unavailable
    (no recorded rent to compare)."""
    from .models import OrderTimeline, RentSuggestion

    today = timezone.localdate()
    accepted = list(RentSuggestion.objects.filter(decision='Accepted').select_related(
        'shipment', 'origin_warehouse', 'destination_warehouse'))
    dismissed = RentSuggestion.objects.filter(decision='Dismissed').count()

    groups = {}
    for r in accepted:
        groups.setdefault(r.shipment_id or -r.pk, []).append(r)

    moves, pending, cancelled = [], 0, 0
    for recs in groups.values():
        first = recs[0]
        shp = first.shipment
        if shp is not None and shp.status == 'Cancelled':
            cancelled += 1
            continue
        move = {
            'shipment': shp,
            'origin': first.origin_warehouse,
            'destination': first.destination_warehouse,
            'batches': [r.batch_number for r in recs],
            'move_mt': sum(float(r.move_mt) for r in recs),
            'promised_daily': sum(float(r.est_daily_saving) for r in recs),
            'accepted_at': first.decided_at,
            'moved_on': None, 'measured_daily': None, 'status': 'measuring', 'note': '',
        }
        if shp is None or shp.status != 'Completed':
            pending += 1
            move['note'] = 'Waiting for the transfer to complete'
            moves.append(move)
            continue

        done = (OrderTimeline.objects.filter(shipment=shp, action__startswith='Receiving finalized')
                .order_by('-timestamp').first())
        moved_on = timezone.localtime(done.timestamp).date() if done else timezone.localtime(shp.updated_at).date()
        move['moved_on'] = moved_on
        days_after = min(RESULT_WINDOW_DAYS, (today - moved_on).days)
        if days_after < RESULT_MIN_DAYS_AFTER or move['origin'] is None:
            move['note'] = f'Needs {RESULT_MIN_DAYS_AFTER} days after the move'
            moves.append(move)
            continue

        one = _dt.timedelta(days=1)
        before_start, after_end = moved_on - _dt.timedelta(days=RESULT_WINDOW_DAYS), moved_on + _dt.timedelta(days=days_after)
        o_before = _window_avg(_recorded_days(move['origin'].id, before_start, moved_on - one))
        o_after = _window_avg(_recorded_days(move['origin'].id, moved_on + one, after_end))
        if o_before is None or o_after is None:
            move['status'], move['note'] = 'unavailable', 'No recorded rent to compare'
            moves.append(move)
            continue

        measured = o_before[0] - o_after[0]
        d = move['destination']
        if d is not None:
            d_before = _window_avg(_recorded_days(d.id, before_start, moved_on - one))
            d_after = _window_avg(_recorded_days(d.id, moved_on + one, after_end))
            if d_before and d_after:
                measured -= d_after[0] - d_before[0]
        move['measured_daily'] = round(measured, 2)

        other_change = (o_before[1] - o_after[1]) - move['move_mt']
        if abs(other_change) > 0.5 * move['move_mt']:
            move['status'] = 'unclear'
            move['note'] = f"Other stock changed by {abs(other_change):.0f} MT"
        else:
            move['status'] = 'measured'
        moves.append(move)

    moves.sort(key=lambda m: m['moved_on'] or m['accepted_at'].date(), reverse=True)
    counted = [m for m in moves if m['status'] == 'measured']
    return {
        'accepted': len(groups) - cancelled,
        'dismissed': dismissed,
        'pending': pending,
        'measured_count': len(counted),
        'measured_daily_total': round(sum(m['measured_daily'] for m in counted), 2),
        'promised_daily_total': round(sum(m['promised_daily'] for m in counted), 2),
        'moves': moves[:limit],
        'window_days': RESULT_WINDOW_DAYS,
    }


def open_batch_rent_expr():
    """ORM expression: Sum (batch tonnage x effective rate) across a
    warehouse's currently-open batches - Active OR Quarantined (same statuses as
    used_mt_expr(): a Quarantined batch still occupies space and still costs
    rent even though it isn't "usable" stock).

    "Effective rate" = the batch's own rental_rate_per_mt if it has one
    (a genuine per-PO negotiated rate, locked in permanently at receipt),
    else the warehouse's CURRENT rental_cost_per_mt (live-tracked - a batch
    that just used the warehouse's fallback rate should reflect a later
    correction/edit to that rate, not be stranded at a stale snapshot)."""
    from django.db.models import Case, When, F, Value, DecimalField, Q
    from django.db.models.functions import Coalesce

    open_q = Q(batches__status__in=['Active', 'Quarantined'])
    effective_rate = Coalesce(F('batches__rental_rate_per_mt'), F('rental_cost_per_mt'))
    return Coalesce(
        Sum(Case(
            When(open_q & Q(batches__material__isnull=False),
                 then=F('batches__quantity') * F('batches__material__weight_mt_per_unit') * effective_rate),
            When(open_q & Q(batches__product__isnull=False),
                 then=F('batches__quantity') * F('batches__product__weight_mt_per_unit') * effective_rate),
            default=Value(0), output_field=DecimalField(max_digits=14, decimal_places=4),
        )),
        Value(0, output_field=DecimalField(max_digits=14, decimal_places=4)),
    )


def warehouse_rent_burn(warehouse_ids=None):
    """Per-warehouse true daily rent burn - the single source of truth for
    rental cost, replacing the flat "used_mt * warehouse.rental_cost_per_mt"
    calculations that used to live in views.dashboard_view and
    views.facility_management_view. Internal=0 and Overall=capacity*rate are
    unchanged; Usage is now batch-aware (sums each open batch's own
    locked-in rate, via open_batch_rent_expr(), instead of applying one flat
    rate to the warehouse's current total)."""
    from .models import Warehouse

    qs = Warehouse.objects.all()
    if warehouse_ids is not None:
        qs = qs.filter(id__in=warehouse_ids)
    rows = []
    for w in qs.annotate(used_mt=used_mt_expr(), quarantined_mt=quarantined_mt_expr(), batch_rent=open_batch_rent_expr()).order_by('name'):
        used_mt = float(w.used_mt or 0)
        if w.ownership_type == 'Internal':
            daily_cost, billing_mode = 0.0, 'Internal'
        elif w.rental_billing_method == 'Overall':
            daily_cost, billing_mode = float(w.total_capacity_mt * w.rental_cost_per_mt), 'Overall Capacity'
        else:
            daily_cost, billing_mode = float(w.batch_rent or 0), 'Usage'
        rows.append({
            'warehouse_id': w.id,
            'name': w.name,
            'ownership_type': w.ownership_type,
            'billing_mode': billing_mode,
            'used_mt': round(used_mt, 3),
            'quarantined_mt': round(float(w.quarantined_mt or 0), 3),
            'capacity_mt': float(w.total_capacity_mt),
            'daily_cost': round(daily_cost, 2),
        })
    return rows


def _linreg(points):
    """Ordinary least-squares (slope, intercept) for [(x, y), ...]. slope=None if degenerate."""
    n = len(points)
    if n < 2:
        return None, None
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sxx = sum(p[0] * p[0] for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None, None
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept


def capacity_forecast():
    """Per warehouse, projects when it reaches 100% from its snapshot history.
    Worst first - see §8.8 for the row shape and status bands."""
    from .models import Warehouse, WarehouseUtilizationSnapshot

    snaps = {}
    for s in (WarehouseUtilizationSnapshot.objects
              .order_by('warehouse_id', 'snapshot_date')
              .values('warehouse_id', 'snapshot_date', 'utilization_percent')):
        snaps.setdefault(s['warehouse_id'], []).append(
            (s['snapshot_date'], float(s['utilization_percent']))
        )

    rows = []
    for w in Warehouse.objects.order_by('name'):
        hist = snaps.get(w.id, [])
        base = {
            "warehouse_id": w.id, "name": w.name,
            "snapshot_count": len(hist),
            "first_date": hist[0][0] if hist else None,
            "latest_date": hist[-1][0] if hist else None,
            "current_percent": round(hist[-1][1], 2) if hist else None,
            "weekly_rate_pp": None, "days_to_full": None, "projected_full_date": None,
        }
        if len(hist) < _MIN_SNAPSHOTS:
            base["status"] = "no_data"
            rows.append(base)
            continue

        d0 = hist[0][0]
        slope, _intercept = _linreg([((d - d0).days, y) for d, y in hist])
        current = hist[-1][1]
        base["weekly_rate_pp"] = round(slope * 7, 2) if slope is not None else None

        if slope is None or slope <= _FLAT_SLOPE_PP:
            base["status"] = "stable" if current < 95 else "critical"
        else:
            days_to_full = max(0.0, (100.0 - current) / slope)
            base["days_to_full"] = round(days_to_full, 1)
            if days_to_full <= _CAPACITY_HORIZON_DAYS:
                base["projected_full_date"] = hist[-1][0] + _dt.timedelta(days=round(days_to_full))
            if current >= 95 or days_to_full <= 14:
                base["status"] = "critical"
            elif days_to_full <= 60:
                base["status"] = "watch"
            else:
                base["status"] = "ok"
        rows.append(base)

    rows.sort(key=lambda r: (
        _CAPACITY_RANK[r["status"]],
        r["days_to_full"] if r["days_to_full"] is not None else 10 ** 6,
        -(r["current_percent"] or 0),
        r["name"].lower(),
    ))
    return rows


def _daily_usage_rates(material_ids, product_ids, today):
    """How fast stock leaves on its own, per unit per day: materials from the last
    DEFAULT_WINDOW_DAYS of production consumption, products from the last 180
    days of sales orders (excluding Draft/Rejected)."""
    from .models import SalesOrderDetail

    rates = {('material', mid): r for mid, r in
             consumption_rates(material_ids, window_days=DEFAULT_WINDOW_DAYS, end=today).items()}
    if product_ids:
        window = 180
        for r in (SalesOrderDetail.objects
                  .filter(product_id__in=product_ids,
                          sales_order__order_date__gt=today - _dt.timedelta(days=window),
                          sales_order__order_date__lte=today)
                  .exclude(sales_order__status__in=_TREND_EXCLUDED_SO_STATUSES)
                  .values('product_id').annotate(q=Sum('quantity_ordered'))):
            if r['q']:
                rates[('product', r['product_id'])] = r['q'] / Decimal(window)
    return rates


def _days_batch_would_stay(batch, rates, fefo_ahead, horizon):
    """(days, reason) this batch's free stock would sit where it is if left alone:
    the earlier of its expiry and when it'll be used up - drawn oldest-first
    (FEFO), so the free stock in batches ahead of it goes first - capped at
    `horizon`. reason is 'expires', 'used up' or 'horizon'."""
    options = [(horizon, 'horizon')]
    if batch.days_until_expiry is not None:
        options.append((max(batch.days_until_expiry, 0), 'expires'))
    key = ('material', batch.material_id) if batch.material_id else ('product', batch.product_id)
    rate = rates.get(key)
    if rate and rate > 0 and batch.status == 'Active':
        used_up = (fefo_ahead.get(batch.id, Decimal('0')) + batch.available_quantity) / rate
        options.append((float(used_up), 'used up'))
    days, reason = min(options, key=lambda o: o[0])
    return max(float(days), 0.0), reason


def _fefo_ahead(batches):
    """{batch_id: free quantity in same-item Active batches that will be used before it}."""
    from .models import Batch

    ahead = {}
    items = {(b.material_id, b.product_id) for b in batches}
    for material_id, product_id in items:
        running = Decimal('0')
        qs = Batch.objects.filter(status='Active', material_id=material_id, product_id=product_id)
        for b in qs.order_by('expiry_date', 'manufacturing_date', 'id'):
            ahead[b.id] = running
            running += max(b.quantity - b.allocated_quantity, Decimal('0'))
    return ahead


def incoming_transfer_mt():
    """{warehouse_id: MT} of stock on internal transfers that are heading to a
    warehouse but haven't been received yet - it isn't stored there yet, but it
    will take up the space, so it isn't free."""
    from .models import ShipmentItem

    incoming = {}
    items = (ShipmentItem.objects
             .filter(shipment__direction='Transfer', shipment__destination_warehouse__isnull=False)
             .exclude(shipment__status__in=('Completed', 'Cancelled'))
             .select_related('shipment', 'material', 'product'))
    for it in items:
        unit_mt = it.material.weight_mt_per_unit if it.material else (it.product.weight_mt_per_unit if it.product else 0)
        wid = it.shipment.destination_warehouse_id
        incoming[wid] = incoming.get(wid, 0.0) + float(it.quantity * unit_mt)
    return incoming


def rent_reduction_opportunities():
    """DSS: for rented (Usage-billed) warehouses capacity_forecast() flags
    critical/watch, suggests batches to relocate into our own warehouses and
    estimates the rent that would stop accruing - per day and in total.

    Destinations are Internal warehouses that can store stock (location_type
    Storage or Both - a manufacturing-only plant is never suggested), each with
    its OWN spare capacity, used up as it's taken - so two flagged warehouses can
    never both claim the same free space.

    Only each batch's unallocated portion counts as movable (the allocated part is
    already committed to an outgoing SO/production run).

    Total saving = daily saving x the days the batch would otherwise stay: the
    earlier of expiry and being used up (FEFO, at the current usage/sales rate),
    capped at dss_saving_horizon_days. Batches saving less than
    dss_min_total_saving_rm in total are left out and counted per warehouse.
    Free space goes first to the batches that save the most per MT.

    A destination's free space is its capacity less what's stored and what's
    already on the way there (incoming_transfer_mt), so accepting a suggestion
    reserves its room. Batches someone dismissed stay out until their snooze
    (RentSuggestion.snoozed_until) ends."""
    from .models import Warehouse, Batch, RentSuggestion
    from .settings_store import get_setting

    forecast_rows = {r['warehouse_id']: r for r in capacity_forecast()}
    flagged = [wid for wid, r in forecast_rows.items() if r['status'] in ('critical', 'watch')]
    if not flagged:
        return []

    util_by_wh = {r['warehouse_id']: r for r in warehouse_utilization()}
    incoming = incoming_transfer_mt()
    spare_by_dest = {}
    for w in Warehouse.objects.filter(ownership_type='Internal', location_type__in=Warehouse.STORAGE_TYPES):
        u = util_by_wh.get(w.id)
        spare = float(w.total_capacity_mt) - (u['used_mt'] if u else 0.0) - incoming.get(w.id, 0.0)
        if spare > 0:
            spare_by_dest[w.id] = {'warehouse_id': w.id, 'name': w.name, 'spare_mt': round(spare, 3)}
    destination_options = sorted(spare_by_dest.values(), key=lambda x: -x['spare_mt'])
    if not spare_by_dest:
        return []
    remaining = {wid: d['spare_mt'] for wid, d in spare_by_dest.items()}

    today = timezone.localdate()
    horizon = int(get_setting('dss_saving_horizon_days'))
    min_saving = float(get_setting('dss_min_total_saving_rm'))
    snooze_days = int(get_setting('dss_dismiss_snooze_days'))
    snoozed = set(RentSuggestion.objects.filter(
        decision='Dismissed', snoozed_until__gte=today, batch__isnull=False
    ).values_list('batch_id', flat=True))

    # Gather every movable batch across all flagged rented warehouses first ...
    origins = {}
    batches = []
    for w in Warehouse.objects.filter(id__in=flagged):
        if w.ownership_type == 'Internal' or w.rental_billing_method != 'Usage':
            continue  # nothing to save moving off a free or flat-Overall-billed warehouse
        origins[w.id] = {'warehouse': w, 'candidates': [], 'excluded_low_saving': 0, 'dismissed_hidden': 0}
        for b in (Batch.objects.filter(warehouse=w, status__in=['Active', 'Quarantined'])
                  .select_related('material', 'product')):
            if float(b.available_weight_mt) > 0:
                if b.id in snoozed:
                    origins[w.id]['dismissed_hidden'] += 1
                    continue
                batches.append((b, w))

    rates = _daily_usage_rates({b.material_id for b, _ in batches if b.material_id},
                               {b.product_id for b, _ in batches if b.product_id}, today)
    ahead = _fefo_ahead([b for b, _ in batches])

    pool = []
    for b, w in batches:
        # Effective rate = the batch's own locked-in rate if it has one, else this
        # warehouse's current standing rate (same fallback as open_batch_rent_expr()).
        rate = float(b.rental_rate_per_mt if b.rental_rate_per_mt is not None else w.rental_cost_per_mt)
        stay_days, stay_reason = _days_batch_would_stay(b, rates, ahead, horizon)
        mt = float(b.available_weight_mt)
        if mt * rate * stay_days < min_saving:
            origins[w.id]['excluded_low_saving'] += 1
            continue  # leaves soon / too small - not worth relocating
        pool.append((rate * stay_days, rate, stay_days, stay_reason, mt, b, w.id))

    # ... then place them best saving per MT first (free space is the limit),
    # drawing each destination's space down as it's used.
    pool.sort(key=lambda p: (-p[0], -p[4], p[5].batch_number))
    for per_mt, rate, stay_days, stay_reason, mt, b, origin_id in pool:
        dest_id = max(remaining, key=lambda d: remaining[d])
        if remaining[dest_id] <= 0:
            break  # every destination is full
        take_mt = min(mt, remaining[dest_id])
        total = take_mt * per_mt
        if total < min_saving:
            origins[origin_id]['excluded_low_saving'] += 1
            continue  # only a sliver of space left for it
        remaining[dest_id] -= take_mt
        origins[origin_id]['candidates'].append({
            'batch_id': b.id,
            'batch_number': b.batch_number,
            'item': (b.material.name if b.material else b.product.name),
            'mt': round(mt, 3),
            'move_mt': round(take_mt, 3),
            'rate_per_mt': rate,
            'daily_saving': round(take_mt * rate, 2),
            'stay_days': round(stay_days),
            'stay_reason': stay_reason,
            'total_saving': round(total, 2),
            'destination_id': dest_id,
            'destination': spare_by_dest[dest_id]['name'],
        })

    opportunities = []
    for wid, o in origins.items():
        if o['candidates'] or o['excluded_low_saving'] or o['dismissed_hidden']:
            opportunities.append({
                'warehouse_id': wid,
                'name': o['warehouse'].name,
                'status': forecast_rows[wid]['status'],
                'candidate_batches': o['candidates'],
                'total_daily_saving': round(sum(c['daily_saving'] for c in o['candidates']), 2),
                'total_saving': round(sum(c['total_saving'] for c in o['candidates']), 2),
                'destination_options': destination_options,
                'excluded_low_saving': o['excluded_low_saving'],
                'dismissed_hidden': o['dismissed_hidden'],
                'snooze_days': snooze_days,
                'min_total_saving': min_saving,
            })
    opportunities.sort(key=lambda o: -o['total_saving'])
    return opportunities


# --------------------------------------------------------------------------------
# Phase 4 - Logistics: in-flight shipment risk
# --------------------------------------------------------------------------------
# First function to see every direction (Inbound/Outbound/Transfer) - previously
# Shipment only surfaced indirectly inside sales_order_delivery_risk/supplier_reliability.

_LOGISTICS_RANK = {
    "discrepant": 0, "overdue": 1, "at_risk": 2, "stalled": 3, "pending": 4, "on_track": 5,
}
# Shipment statuses still "in flight" - Completed/Cancelled are terminal and excluded.
_OPEN_SHIPMENT_STATUSES = (
    "Draft", "Logistics Review", "Pending Approval", "Preparing",
    "Dispatched", "Arrived", "Delayed", "Discrepant",
)


def shipment_logistics():
    """Per in-flight Shipment (any direction), worst first. See §8.10 for the
    row shape and risk bands."""
    from .models import Shipment
    from .settings_store import get_setting

    today = timezone.localdate()
    at_risk_window = get_setting("logistics_at_risk_window_days")
    stall_days = get_setting("logistics_stall_days")

    ships = (
        Shipment.objects.filter(status__in=_OPEN_SHIPMENT_STATUSES)
        .select_related("purchase_order", "sales_order", "origin_warehouse", "destination_warehouse")
    )

    rows = []
    for s in ships:
        overdue_by_date = (
            s.status in ("Dispatched", "Arrived")
            and s.expected_eta_date is not None
            and s.actual_arrival_date is None
            and s.expected_eta_date < today
        )
        if s.has_discrepancy or s.status == "Discrepant":
            risk = "discrepant"
        elif s.status == "Delayed" or overdue_by_date:
            risk = "overdue"
        elif (s.status == "Dispatched" and s.expected_eta_date is not None
              and s.actual_arrival_date is None
              and 0 <= (s.expected_eta_date - today).days <= at_risk_window):
            risk = "at_risk"
        elif (s.status == "Arrived" and s.actual_arrival_date is not None
              and (today - s.actual_arrival_date).days >= stall_days):
            risk = "stalled"
        elif s.status in ("Draft", "Logistics Review", "Pending Approval", "Preparing") and not s.dispatch_date:
            risk = "pending"
        else:
            risk = "on_track"

        rows.append({
            "shipment_id": s.id,
            "tracking_number": s.tracking_number,
            "direction": s.direction,
            "status": s.status,
            "is_transfer": s.direction == "Transfer",
            "so_number": s.sales_order.so_number if s.sales_order_id else None,
            "po_number": s.purchase_order.po_number if s.purchase_order_id else None,
            "origin": s.origin_warehouse.name if s.origin_warehouse_id else (s.external_origin or None),
            "destination": s.destination_warehouse.name if s.destination_warehouse_id else (
                s.client_address.strip().splitlines()[0] if s.client_address else None
            ),
            "dispatch_date": s.dispatch_date,
            "expected_eta_date": s.expected_eta_date,
            "actual_arrival_date": s.actual_arrival_date,
            "has_discrepancy": s.has_discrepancy,
            "risk": risk,
        })

    rows.sort(key=lambda r: (
        _LOGISTICS_RANK[r["risk"]],
        r["expected_eta_date"] or r["dispatch_date"] or _dt.date.max,
        r["tracking_number"],
    ))
    return rows


# --------------------------------------------------------------------------------
# Phase 4 - Personal checklist: "my open jobs"
# --------------------------------------------------------------------------------
# One user's own records, not a company-wide roll-up. Shipment has no creator
# field, so last_edited_by is used as a labelled proxy (ownership='touched').

_SO_CLOSED_STATUSES = ("Delivered", "Rejected")
_PO_CLOSED_STATUSES = ("Completed", "Rejected")
_RUN_CLOSED_STATUSES = ("Completed", "Cancelled")
_SHIPMENT_CLOSED_STATUSES = ("Completed", "Cancelled")


def my_open_jobs(user):
    """One user's own open/in-process records across every domain, oldest
    first. See §8.11 for the row shape and how `context` is populated."""
    from .models import SalesOrder, PurchaseOrder, ProductionRun, StockAudit, Shipment

    today = timezone.localdate()

    def _age(d):
        return (today - d).days if d else None

    rows = []

    so_risk_by_number = {r["so_number"]: r for r in sales_order_delivery_risk()}
    for so in (SalesOrder.objects.filter(created_by=user)
               .exclude(status__in=_SO_CLOSED_STATUSES)):
        ctx = None
        risk = so_risk_by_number.get(so.so_number)
        if risk and risk["risk"] in ("late", "at_risk"):
            ctx = {"risk": risk["risk"], "days_slack": risk["days_slack"]}
        rows.append({
            "kind": "Sales Order", "reference": so.so_number, "url_name": "so_detail",
            "pk": so.id, "status": so.status, "opened_on": so.order_date,
            "age_days": _age(so.order_date), "ownership": "created", "context": ctx,
        })

    supplier_by_name = {r["supplier_name"]: r for r in supplier_reliability()}
    for po in (PurchaseOrder.objects.filter(created_by=user)
               .exclude(status__in=_PO_CLOSED_STATUSES).select_related("supplier")):
        ctx = None
        supplier_row = supplier_by_name.get(po.supplier.name if po.supplier_id else po.supplier_name)
        if supplier_row and supplier_row["rating"] in ("poor", "watch"):
            ctx = {"rating": supplier_row["rating"], "on_time_rate": supplier_row["on_time_rate"]}
        rows.append({
            "kind": "Purchase Order", "reference": po.po_number, "url_name": "po_detail",
            "pk": po.id, "status": po.status, "opened_on": po.order_date,
            "age_days": _age(po.order_date), "ownership": "created", "context": ctx,
        })

    for run in (ProductionRun.objects.filter(created_by=user)
                .exclude(status__in=_RUN_CLOSED_STATUSES)):
        opened = run.start_time.date() if run.start_time else None
        rows.append({
            "kind": "Production Run", "reference": run.run_number,
            "url_name": "production_run_detail", "pk": run.id, "status": run.status,
            "opened_on": opened, "age_days": _age(opened), "ownership": "created", "context": None,
        })

    for audit in (StockAudit.objects.filter(auditor=user, status="Pending")
                  .select_related("batch")):
        opened = audit.audit_date.date() if audit.audit_date else None
        rows.append({
            # stock_audit is a list page (no per-record detail view), so pk is
            # None here - the template links to the list, not one record.
            "kind": "Stock Audit", "reference": f"Batch {audit.batch.batch_number}" if audit.batch_id else f"Audit #{audit.id}",
            "url_name": "stock_audit", "pk": None, "status": audit.status,
            "opened_on": opened, "age_days": _age(opened), "ownership": "created", "context": None,
        })

    for ship in (Shipment.objects.filter(last_edited_by=user)
                 .exclude(status__in=_SHIPMENT_CLOSED_STATUSES)):
        opened = ship.dispatch_date
        rows.append({
            "kind": "Shipment", "reference": ship.tracking_number, "url_name": "shipment_detail",
            "pk": ship.id, "status": ship.status, "opened_on": opened,
            "age_days": _age(opened), "ownership": "touched", "context": None,
        })

    rows.sort(key=lambda r: (
        -(r["age_days"] if r["age_days"] is not None else -1),
        r["kind"], r["reference"],
    ))
    return rows


# --------------------------------------------------------------------------------
# Phase 5 - Product sales trend
# --------------------------------------------------------------------------------
# Per product, monthly sales volume (and revenue, where pricing is on file) over
# a trailing window, classified rising/declining/flat - the only genuinely
# time-series view of Sales, as opposed to sales_order_delivery_risk()'s
# point-in-time snapshot of currently-open orders.

_PRODUCT_TREND_RANK = {"declining": 0, "rising": 1, "flat": 2, "new": 3, "insufficient_data": 4}

TREND_WINDOW_MONTHS = 6
# Need sales activity in at least this many months to classify a trend at all.
_MIN_MONTHS_FOR_TREND = 3
# +/- this % change (recent months' average vs earlier months') counts as a
# real trend rather than noise.
_TREND_SIGNIFICANT_PCT = 15.0
# SO statuses that never became a real commitment - excluded from the trend.
_TREND_EXCLUDED_SO_STATUSES = ("Draft", "Rejected")


def _trailing_month_keys(end, window_months):
    """window_months "YYYY-MM" keys ending on end's month, oldest first."""
    y, m = end.year, end.month
    keys = []
    for _ in range(window_months):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    keys.reverse()
    return keys


def product_sales_trend(*, window_months=TREND_WINDOW_MONTHS, end=None):
    """
    Per product, monthly quantity_ordered (and revenue, if unit_price is on
    every line that month) over the trailing window, worst (declining) first.

    Row keys: product_id, sku, name, monthly_qty (list, oldest first),
    monthly_revenue (list, or None if any month has an unpriced line),
    total_qty, total_revenue (or None), recent_avg_qty, earlier_avg_qty,
    pct_change (recent vs earlier average; None if not computable),
    months_with_sales, status
    ('declining' | 'rising' | 'flat' | 'new' | 'insufficient_data').

      declining/rising - recent-average vs earlier-average differs by at least
                          _TREND_SIGNIFICANT_PCT
      flat              - real history, but change is within that band
      new               - no sales in the earlier months, some in the recent ones
      insufficient_data - sales activity in fewer than _MIN_MONTHS_FOR_TREND months
    """
    from .models import SalesOrderDetail

    end = end or timezone.localdate()
    month_keys = _trailing_month_keys(end, window_months)
    earliest = _dt.date(*(int(p) for p in month_keys[0].split("-")), 1)
    recent_n = max(1, window_months // 2)

    details = (
        SalesOrderDetail.objects.filter(sales_order__order_date__gte=earliest)
        .exclude(sales_order__status__in=_TREND_EXCLUDED_SO_STATUSES)
        .values_list("product_id", "product__sku", "product__name",
                     "sales_order__order_date", "quantity_ordered", "unit_price")
    )

    buckets = {}
    for product_id, sku, name, order_date, qty, unit_price in details:
        key = order_date.strftime("%Y-%m")
        if key not in month_keys:
            continue
        b = buckets.setdefault(product_id, {
            "sku": sku, "name": name,
            "qty": {k: Decimal("0") for k in month_keys},
            "rev": {k: Decimal("0") for k in month_keys},
            "priced": {k: True for k in month_keys},
        })
        qty = qty or Decimal("0")
        b["qty"][key] += qty
        if unit_price is not None:
            b["rev"][key] += qty * unit_price
        else:
            b["priced"][key] = False

    rows = []
    for product_id, b in buckets.items():
        monthly_qty = [float(b["qty"][k]) for k in month_keys]
        has_full_pricing = all(b["priced"][k] for k in month_keys if b["qty"][k] > 0)
        monthly_revenue = [float(b["rev"][k]) for k in month_keys] if has_full_pricing else None

        months_with_sales = sum(1 for v in monthly_qty if v > 0)
        recent = monthly_qty[-recent_n:]
        earlier = monthly_qty[:-recent_n]
        recent_avg = sum(recent) / len(recent) if recent else 0.0
        earlier_avg = sum(earlier) / len(earlier) if earlier else 0.0

        if months_with_sales < _MIN_MONTHS_FOR_TREND:
            status, pct_change = "insufficient_data", None
        elif earlier_avg == 0:
            status, pct_change = ("new", None) if recent_avg > 0 else ("insufficient_data", None)
        else:
            pct_change = (recent_avg - earlier_avg) / earlier_avg * 100.0
            if pct_change >= _TREND_SIGNIFICANT_PCT:
                status = "rising"
            elif pct_change <= -_TREND_SIGNIFICANT_PCT:
                status = "declining"
            else:
                status = "flat"

        rows.append({
            "product_id": product_id,
            "sku": b["sku"],
            "name": b["name"],
            "monthly_qty": [round(v, 2) for v in monthly_qty],
            "monthly_revenue": [round(v, 2) for v in monthly_revenue] if monthly_revenue is not None else None,
            "total_qty": round(sum(monthly_qty), 2),
            "total_revenue": round(sum(monthly_revenue), 2) if monthly_revenue is not None else None,
            "recent_avg_qty": round(recent_avg, 2),
            "earlier_avg_qty": round(earlier_avg, 2),
            "pct_change": round(pct_change, 1) if pct_change is not None else None,
            "months_with_sales": months_with_sales,
            "status": status,
        })

    def _sort_key(r):
        if r["status"] == "declining":
            magnitude = r["pct_change"]
        elif r["status"] == "rising":
            magnitude = -r["pct_change"]
        else:
            magnitude = 0.0
        return (_PRODUCT_TREND_RANK[r["status"]], magnitude, -r["total_qty"], r["name"].lower())

    rows.sort(key=_sort_key)
    return rows
