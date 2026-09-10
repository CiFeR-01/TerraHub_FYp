"""
core/analytics.py — computation layer for TerraHub's analytics & forecasting.

Pure functions over the ORM: no request/response objects, no side effects. Meant to
be called from web views (core/views_analytics.py), management commands (daily
snapshots, the AI ops briefing), the shell, and tests.

Roadmap (see SYSTEM_DOCUMENTATION.md section 8):
  Tier 1 - roll-ups of data already stored: supplier reliability, sales-order
           delivery risk, audit accuracy, production yield variance.
  Tier 2 - statistical forecasting by plain arithmetic: consumption-rate stockout
           ETA, reorder-by date, warehouse capacity runway.
  Tier 3 - an LLM ops briefing that narrates the Tier 1/2 signals.

Delivered so far:
  Phase 0 - RegistryLog.material spine + consumption_rate() / daily_consumption().
  Phase 1 - supplier_reliability() scorecard, with a lead-time-derived expected
            delivery date fallback (po_default_lead_time_days system setting);
            sales_order_delivery_risk() board; audit_accuracy();
            production_yield_variance().
  Phase 2a - stockout_forecast(): consumption-rate days-of-cover + reorder-by date.
  Phase 2b - capacity_forecast(): linear-fit of WarehouseUtilizationSnapshot -> date
             each warehouse crosses 100%.
"""
from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

# RegistryLog.action_type value written every time materials are drawn into a
# production run (see core/utils.py :: consume_materials_for_run).
CONSUMPTION_ACTION = "Consumed_For_Manufacturing"

# Trailing window used for burn-rate calculations unless the caller overrides it.
DEFAULT_WINDOW_DAYS = 30


# --------------------------------------------------------------------------------
# RegistryLog.item_name -> Material resolution
# --------------------------------------------------------------------------------
# Used by migration 0036 to backfill RegistryLog.material for history written
# before the FK existed, and available for any later reconciliation pass. Kept
# here (not in the migration) so it is importable and unit-tested.

_UPDATED_MATERIAL_RE = re.compile(r"Updated Material '(.+?)'")
_AUDIT_RESOLVE_RE = re.compile(r"^Batch \S+ \(.+? - (.+?)\)$")


def resolve_material_from_label(item_name, by_name):
    """
    Map a free-text RegistryLog.item_name back to a material, using ``by_name``:
    a dict keyed by ``material.name.strip().lower()`` whose values are whatever the
    caller wants back (a Material instance, an id, an sku...).

    Recognises the label shapes the pre-Phase-0 code produced:
      "<name> (Run <run>)"                      - Consumed_For_Manufacturing
      "<name> (Batch <batch>)" [" - force closed"] - Inbound / Outbound
      "Manual Receipt of <name>"                - Adjusted (manual receive)
      "Updated Material '<name>' (SKU: <sku>)"  - Adjusted (material edit)
      "Batch <batch> (<sku> - <name>)"          - Adjusted (audit resolve)
      "<name>"                                  - exact match

    Returns the mapped value, or ``None`` when nothing matches (batch-only labels,
    bulk-import summaries, finished-goods movements).
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
    Total quantity of ``material`` consumed for manufacturing per calendar day over
    the trailing ``window_days`` ending on ``end`` (default: today).

    Returns ``{date: Decimal}``. Days with no consumption are absent from the dict;
    callers that need a dense series should zero-fill themselves.

    Reads ``RegistryLog`` rows via the ``material`` FK added in Phase 0, so only
    movements that were tagged with a material are counted. Rows left untagged by
    the backfill (older history the parser could not resolve) are silently skipped
    - this makes the rate conservative rather than wrong.
    """
    end = end or timezone.now().date()
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
    """
    Mean daily consumption of ``material`` over the window: total consumed divided
    by the full ``window_days`` (not just the days that had activity), so idle days
    correctly pull the burn rate down.

    Returns a non-negative ``Decimal``. Returns ``0`` when there is no consumption
    in the window or ``window_days`` is not positive.
    """
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
# Rolls PurchaseOrder / PurchaseOrderDetail / linked inbound Shipment data up per
# supplier. Nothing new is recorded - this is aggregation of data the PO and
# logistics flows already capture.

# PO statuses that represent a real, approved order with fulfilment activity worth
# measuring. Draft / Pending Approval / Rejected carry no fulfilment signal.
SCORECARD_PO_STATUSES = ("Pending", "Partially Received", "Completed")
_OPEN_PO_STATUSES = ("Pending", "Partially Received")

_RATING_RANK = {"poor": 0, "watch": 1, "good": 2, "n/a": 3}


def _po_actual_arrival(po):
    """
    Best available "goods actually arrived" date for a PO:
      1. po.completed_date (stamped when receipts flip the PO to 'Completed');
      2. else latest actual_arrival_date across linked inbound shipments;
      3. else latest manufacturing_date across batches received against the PO;
      4. else None (timeliness cannot be assessed).
    """
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
    """
    ``(expected_date, is_estimated)`` for a PO.

      * ``po.expected_delivery_date`` if set                    -> (date, False)
      * else ``po.order_date + lead``                           -> (date, True)
        where ``lead`` is the largest ``SupplierMaterial.lead_time_days`` across
        the PO's line-item materials for its supplier, or ``default_lead_days``
        when none is on file.
      * ``(None, False)`` if there is no basis at all.
    """
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
    Per-supplier fulfilment scorecard, worst performer first.

    ``since`` / ``until`` (``date``, optional) bound the POs considered by
    ``order_date``.

    Each row is a dict:
      supplier_id, supplier_name, is_active,
      po_count, open_count, completed_count,
      ordered_qty, received_qty, fill_rate      (received / ordered; None if none ordered)
      assessable_count, on_time_count, late_count,
      on_time_rate                              (on_time / assessable; None if none assessable)
      avg_delay_days                            (signed mean; +ve = late; None if none assessable)
      estimated_count, estimated_share          (assessable POs whose expected date was derived
                                                 from a lead time rather than set explicitly)
      last_delivery                             (date or None)
      rating                                    ('good' | 'watch' | 'poor' | 'n/a')

    A PO's expected date is its ``expected_delivery_date`` when set, otherwise
    ``order_date`` plus a lead time (per-supplier/material if on file, else the
    editable ``po_default_lead_time_days`` system setting). Its arrival is
    ``completed_date`` / linked-shipment arrival / receipt date.

    Grouped by the ``supplier`` FK where set; POs with only a free-text
    ``supplier_name`` roll up under that name (case-insensitively).
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
# Per open sales order, does the projected arrival beat the fulfilment deadline?
# Uses fulfillment_deadline vs. linked outbound Shipment ETAs and production state.

# SO statuses that are still "in flight" and worth risk-tracking. Draft / Pending
# Approval / Rejected have no commitment yet; Delivered is done.
_OPEN_SO_STATUSES = (
    "Pending", "Awaiting Acknowledgement", "In Production",
    "Ready to Ship", "Partially Shipped", "Shipped",
)
# While an order is still pre-shipment, these statuses mean production/prep is the
# thing standing between it and its deadline.
_PRE_SHIPMENT_SO_STATUSES = ("Pending", "Awaiting Acknowledgement", "In Production")

_RISK_RANK = {"late": 0, "at_risk": 1, "on_track": 2, "no_deadline": 3}


def _so_projected_arrival(so):
    """
    ``(date, is_actual)`` best estimate of when the client receives the order, from
    its outbound shipments: an ``actual_arrival_date`` if any shipment has landed,
    else the latest ``expected_eta_date``. ``(None, False)`` if nothing has shipped.
    """
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
    """
    Delivery-risk row per open sales order, worst first.

    Row keys: so_id, so_number, client_name, status, deadline, arrival,
    arrival_is_actual, shipped_fraction, days_slack (deadline - arrival/today;
    negative = behind), production_summary (str or None), risk
    ('late' | 'at_risk' | 'on_track' | 'no_deadline').
    """
    from .models import SalesOrder
    from .settings_store import get_setting

    at_risk_window = get_setting("so_at_risk_window_days")
    today = timezone.now().date()

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
# Rolls StockAudit.variance (actual - expected) up per warehouse and per item, plus
# a monthly trend, to surface where counts chronically drift.


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
    """
    Stock-count accuracy from ``StockAudit``.

    Returns ``{"by_warehouse": [...], "by_item": [...], "trend": [...]}``.
    ``by_warehouse`` / ``by_item`` rows: name, audit_count, exact_count,
    accuracy_rate (exact / total; None if no audits), net_variance (signed sum,
    +ve = found more than expected), shrinkage (sum of negative variances),
    overage (sum of positive), mean_abs_variance, chronic_shrinkage (bool).
    ``trend`` rows: bucket 'YYYY-MM', audit_count, mean_abs_variance,
    net_variance - chronological.

    Worst first: lowest accuracy_rate, then largest mean_abs_variance.
    """
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
# expected_yield vs actual_yield and RunMaterialUsage.variance_pct, per product and
# per supervisor, to flag recipes / runs that consistently overconsume or under-yield.

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
    """
    Yield performance of completed production runs, grouped by target product and by
    supervisor. Worst first.

    Row keys: name, run_count, expected_total, actual_total,
    mean_yield_pct (actual/expected, %), mean_yield_variance_pct (signed, %),
    mean_material_overuse_pct (mean RunMaterialUsage.variance_pct; +ve = overused),
    rating ('good' | 'watch' | 'poor' | 'n/a').
    """
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


# --------------------------------------------------------------------------------
# Tier 2 - Consumption-rate stockout forecast
# --------------------------------------------------------------------------------
# Turns the RegistryLog consumption history into a forward-looking "runs out in N
# days" estimate per material, plus a "reorder by" date once lead time is folded
# in. Pure arithmetic - a moving average, not a model.

_STOCKOUT_RANK = {"critical": 0, "reorder_now": 1, "watch": 2, "ok": 3, "no_usage": 4}

# Days-until-reorder bands: <= _REORDER_NOW_DAYS is "reorder now", <= _REORDER_WATCH_DAYS is "watch".
_REORDER_NOW_DAYS = 2
_REORDER_WATCH_DAYS = 14

# Beyond this many days of cover we don't bother projecting a stockout date - the
# burn rate is so low the estimate is noise.
_FORECAST_HORIZON_DAYS = 3650


def consumption_rates(material_ids, *, window_days=DEFAULT_WINDOW_DAYS, end=None):
    """
    Mean daily consumption for many materials in one query.

    Returns ``{material_id: Decimal}`` (rate per calendar day over the full
    window). Materials with no consumption in the window are absent.
    """
    ids = list(material_ids)
    if not ids or window_days <= 0:
        return {}
    from .models import RegistryLog

    end = end or timezone.now().date()
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


def stockout_forecast(*, window_days=DEFAULT_WINDOW_DAYS, end=None):
    """
    Per active material: on-hand vs. burn rate -> days of cover, projected stockout
    date, and a reorder-by date (stockout minus lead time). Most urgent first.

    Row keys: material_id, sku, name, unit, on_hand, allocated, available,
    on_order, daily_rate, days_cover, stockout_date, lead_time_days,
    lead_time_estimated (bool), reorder_by_date, safe_storage_days, status
    ('critical' | 'reorder_now' | 'watch' | 'ok' | 'no_usage').

      critical    - days_cover < lead time: cannot be replenished in time
      reorder_now - reorder-by date is today or past
      watch       - reorder-by date within the next 14 days
      ok          - further out
      no_usage    - no consumption in the window; nothing to forecast
    """
    from django.db.models import F, Max
    from .models import Material, Batch, SupplierMaterial, PurchaseOrderDetail
    from .settings_store import get_setting

    today = end or timezone.now().date()
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
    on_order = {
        r["material_id"]: (r["oo"] or Decimal("0"))
        for r in PurchaseOrderDetail.objects.filter(
            material_id__in=ids,
            purchase_order__status__in=["Pending", "Partially Received"],
        ).values("material_id").annotate(oo=Sum(F("quantity_ordered") - F("quantity_received")))
    }
    rates = consumption_rates(ids, window_days=window_days, end=today)
    default_lead = get_setting("po_default_lead_time_days")

    rows = []
    for m in materials:
        oh, al = on_hand.get(m.id, (Decimal("0"), Decimal("0")))
        available = oh - al
        rate = rates.get(m.id)
        lt = lead_map.get(m.id)
        lead_estimated = lt is None
        lead_days = int(lt) if lt is not None else int(default_lead)

        if not rate or rate <= 0:
            status = "no_usage"
            days_cover = None
            stockout_date = None
            reorder_by = None
        elif available <= 0:
            # already out - reorder is overdue regardless of rate
            days_cover = 0.0
            stockout_date = today
            reorder_by = today - _dt.timedelta(days=lead_days)
            status = "critical"
        else:
            days_cover = float(available) / float(rate)
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
                if days_until_reorder < 0:
                    status = "critical"
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
            "on_order": float(on_order.get(m.id, Decimal("0"))),
            "daily_rate": float(rate) if rate else 0.0,
            "days_cover": days_cover,
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
# Snapshots utilization_percent per warehouse daily (management command
# snapshot_utilization), then linear-fits the trend to project when each
# warehouse crosses 100%.

_CAPACITY_RANK = {"critical": 0, "watch": 1, "ok": 2, "stable": 3, "no_data": 4}

# Below this many snapshots there is no trend to fit.
_MIN_SNAPSHOTS = 3
# Slopes flatter than this (percentage points per day) count as "not filling".
_FLAT_SLOPE_PP = 0.02
_CAPACITY_HORIZON_DAYS = 3650


def used_mt_expr():
    """
    ORM expression for a Warehouse's active-stock tonnage: Σ over active batches of
    quantity × the material's or product's weight_mt_per_unit. Shared by
    dashboard_view and warehouse_utilization() so the number is defined once.
    """
    from django.db.models import Case, When, F, Value, DecimalField
    from django.db.models.functions import Coalesce

    return Coalesce(
        Sum(Case(
            When(batches__status='Active', batches__material__isnull=False,
                 then=F('batches__quantity') * F('batches__material__weight_mt_per_unit')),
            When(batches__status='Active', batches__product__isnull=False,
                 then=F('batches__quantity') * F('batches__product__weight_mt_per_unit')),
            default=Value(0), output_field=DecimalField(),
        )),
        Value(0, output_field=DecimalField()),
    )


def warehouse_utilization():
    """
    Current point-in-time utilization per warehouse:
    ``[{warehouse_id, name, used_mt, capacity_mt, utilization_percent}, ...]``.
    """
    from .models import Warehouse

    rows = []
    for w in Warehouse.objects.annotate(used_mt=used_mt_expr()).order_by('name'):
        used = float(w.used_mt or 0)
        cap = float(w.total_capacity_mt or 0)
        rows.append({
            "warehouse_id": w.id,
            "name": w.name,
            "used_mt": round(used, 3),
            "capacity_mt": round(cap, 3),
            "utilization_percent": round(used / cap * 100, 2) if cap > 0 else 0.0,
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
    """
    Per warehouse, fit utilization_percent over its snapshot history and project
    when it reaches 100%. Most urgent first.

    Row keys: warehouse_id, name, snapshot_count, first_date, latest_date,
    current_percent, weekly_rate_pp (slope × 7; +ve = filling), days_to_full,
    projected_full_date, status
    ('critical' | 'watch' | 'ok' | 'stable' | 'no_data').

      critical  - current >= 95%, or projected full within 14 days
      watch     - projected full within 60 days
      ok        - filling, but further out
      stable    - flat or emptying
      no_data   - fewer than 3 snapshots
    """
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
