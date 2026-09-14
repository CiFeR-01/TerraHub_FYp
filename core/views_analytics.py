"""
core/views_analytics.py - HTTP controllers for the analytics & forecasting pages.

Deliberately thin: parse request params, call core/analytics.py, render a
template under templates/analytics/. All real computation lives in analytics.py so
it stays testable and reusable by management commands. See
SYSTEM_DOCUMENTATION.md section 8 and ANALYTICS_CHANGELOG.md.
"""
import os
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone

from . import analytics
from .context_processors import ANALYTICS_CATEGORIES, _perm_ok
from .settings_store import get_setting

# Trailing windows offered in the UI. 0 == all time.
WINDOW_CHOICES = (30, 90, 180, 365, 0)
_DEFAULT_WINDOW = 180


def _parse_window(request, default=_DEFAULT_WINDOW):
    try:
        w = int(request.GET.get("window", default))
    except (TypeError, ValueError):
        return default
    return w if w in WINDOW_CHOICES else default


def _analytics_tabs(request, url_name):
    """(category_label, [(url_name, label), ...]) for `url_name`'s Analytics
    category, filtered to what `request.user` can see. Powers the tab strip
    each analytics page shows for its sibling reports in the same category
    (core/context_processors.py's ANALYTICS_CATEGORIES is the source of
    truth; the sidebar collapses each category to one link, this expands it
    back out into tabs on the page itself)."""
    for category_label, items in ANALYTICS_CATEGORIES:
        names = [item[0] for item in items]
        if url_name in names:
            visible = [(u, label) for u, label, perm in items if _perm_ok(request.user, perm)]
            return category_label, visible
    return "", []


@login_required
def supplier_scorecard_view(request):
    window_days = _parse_window(request)
    since = timezone.now().date() - timedelta(days=window_days) if window_days else None
    rows = analytics.supplier_reliability(since=since)

    assessable = [r for r in rows if r["on_time_rate"] is not None]
    assessable_pos = sum(r["assessable_count"] for r in rows)
    estimated_pos = sum(r["estimated_count"] for r in rows)
    summary = {
        "supplier_count": len(rows),
        "poor_count": sum(1 for r in rows if r["rating"] == "poor"),
        "watch_count": sum(1 for r in rows if r["rating"] == "watch"),
        "good_count": sum(1 for r in rows if r["rating"] == "good"),
        "avg_on_time": (
            sum(r["on_time_rate"] for r in assessable) / len(assessable)
            if assessable else None
        ),
        "total_open_po": sum(r["open_count"] for r in rows),
        "assessable_pos": assessable_pos,
        "estimated_pos": estimated_pos,
        "estimated_share": (estimated_pos / assessable_pos) if assessable_pos else None,
    }

    analytics_category, analytics_tabs = _analytics_tabs(request, "supplier_scorecard")
    return render(request, "analytics/supplier_scorecard.html", {
        "rows": rows,
        "summary": summary,
        "window_days": window_days,
        "window_choices": WINDOW_CHOICES,
        "analytics_category": analytics_category,
        "analytics_tabs": analytics_tabs,
        "analytics_active": "supplier_scorecard",
    })


@login_required
def sales_order_delivery_risk_view(request):
    rows = analytics.sales_order_delivery_risk()
    risk_filter = request.GET.get("risk", "").strip()
    if risk_filter in ("late", "at_risk", "on_track", "no_deadline"):
        shown = [r for r in rows if r["risk"] == risk_filter]
    else:
        risk_filter = ""
        shown = rows

    summary = {
        "open_count": len(rows),
        "late_count": sum(1 for r in rows if r["risk"] == "late"),
        "at_risk_count": sum(1 for r in rows if r["risk"] == "at_risk"),
        "on_track_count": sum(1 for r in rows if r["risk"] == "on_track"),
        "no_deadline_count": sum(1 for r in rows if r["risk"] == "no_deadline"),
    }
    analytics_category, analytics_tabs = _analytics_tabs(request, "so_delivery_risk")
    return render(request, "analytics/so_delivery_risk.html", {
        "rows": shown,
        "summary": summary,
        "risk_filter": risk_filter,
        "filters": [
            ("", "All"),
            ("late", "Late"),
            ("at_risk", "At risk"),
            ("on_track", "On track"),
            ("no_deadline", "No deadline"),
        ],
        "analytics_category": analytics_category,
        "analytics_tabs": analytics_tabs,
        "analytics_active": "so_delivery_risk",
    })


@login_required
def audit_accuracy_view(request):
    window_days = _parse_window(request, default=365)
    since = timezone.now().date() - timedelta(days=window_days) if window_days else None
    data = analytics.audit_accuracy(since=since)

    wh = data["by_warehouse"]
    total_audits = sum(r["audit_count"] for r in wh)
    total_net = sum(r["net_variance"] for r in wh)
    total_shrink = sum(r["shrinkage"] for r in wh)
    exact = sum(r["exact_count"] for r in wh)
    summary = {
        "total_audits": total_audits,
        "overall_accuracy": (exact / total_audits) if total_audits else None,
        "net_variance": total_net,
        "shrinkage": total_shrink,
        "chronic_warehouses": sum(1 for r in wh if r["chronic_shrinkage"]),
    }
    analytics_category, analytics_tabs = _analytics_tabs(request, "audit_accuracy")
    return render(request, "analytics/audit_accuracy.html", {
        "sections": [
            {"title": "By warehouse", "label": "Warehouse", "rows": data["by_warehouse"]},
            {"title": "By item", "label": "Item", "rows": data["by_item"]},
        ],
        "trend": data["trend"],
        "summary": summary,
        "window_days": window_days,
        "window_choices": WINDOW_CHOICES,
        "analytics_category": analytics_category,
        "analytics_tabs": analytics_tabs,
        "analytics_active": "audit_accuracy",
    })


@login_required
def production_yield_view(request):
    data = analytics.production_yield_variance()
    prod = data["by_product"]
    rated = [r for r in prod if r["rating"] != "n/a"]
    summary = {
        "product_count": len(prod),
        "poor_count": sum(1 for r in prod if r["rating"] == "poor"),
        "watch_count": sum(1 for r in prod if r["rating"] == "watch"),
        "run_count": sum(r["run_count"] for r in prod),
        "mean_yield_variance_pct": (
            sum(r["mean_yield_variance_pct"] for r in rated) / len(rated) if rated else None
        ),
    }
    analytics_category, analytics_tabs = _analytics_tabs(request, "production_yield")
    return render(request, "analytics/production_yield.html", {
        "sections": [
            {"title": "By product", "label": "Product", "rows": data["by_product"]},
            {"title": "By supervisor", "label": "Supervisor", "rows": data["by_supervisor"]},
        ],
        "summary": summary,
        "analytics_category": analytics_category,
        "analytics_tabs": analytics_tabs,
        "analytics_active": "production_yield",
    })


# Burn-rate lookback windows for the stockout forecast (days). No "all time" here
# - a rate is total / window, so the divisor must be a real span.
FORECAST_WINDOWS = (14, 30, 60, 90)

# UI-facing status buckets (merges the backend's critical/reorder_now into one
# "action required" bucket) -> (display label, badge color, emoji, action label).
_STATUS_DISPLAY = {
    "critical": ("Action Required", "rose", "\U0001F534", "Reorder"),
    "reorder_now": ("Action Required", "rose", "\U0001F534", "Reorder"),
    "watch": ("Watch", "amber", "\U0001F7E0", "Review"),
    "ok": ("Healthy", "emerald", "\U0001F7E2", "Monitor"),
    "no_usage": ("No Usage Data", "slate", "⚪", "—"),
}

# UI-facing status filter values -> the set of raw backend statuses they cover.
_STATUS_FILTER_MAP = {
    "action_required": {"critical", "reorder_now"},
    "watch": {"watch"},
    "healthy": {"ok"},
    "no_data": {"no_usage"},
}

# Cap on how many bars the "Projected Stock Coverage" strip renders.
_COVERAGE_MAX_ROWS = 15


@login_required
def forecast_view(request):
    try:
        window = int(request.GET.get("window", 30))
    except (TypeError, ValueError):
        window = 30
    if window not in FORECAST_WINDOWS:
        window = 30

    rows = analytics.stockout_forecast(window_days=window)

    status_filter = request.GET.get("status", "").strip()
    if status_filter not in _STATUS_FILTER_MAP:
        status_filter = ""
    status_set = _STATUS_FILTER_MAP.get(status_filter)
    shown = [r for r in rows if r["status"] in status_set] if status_set else rows

    query = request.GET.get("q", "").strip()
    if query:
        q_lower = query.lower()
        shown = [
            r for r in shown
            if q_lower in r["sku"].lower() or q_lower in r["name"].lower()
        ]

    for r in shown:
        display_status, display_color, display_emoji, action_label = _STATUS_DISPLAY[r["status"]]
        r["display_status"] = display_status
        r["display_color"] = display_color
        r["display_emoji"] = display_emoji
        r["action_label"] = action_label

    coverage_rows = [r for r in shown if r["days_cover"] is not None][:_COVERAGE_MAX_ROWS]
    max_days = max((r["days_cover"] for r in coverage_rows), default=0)
    if max_days > 0:
        for r in coverage_rows:
            r["coverage_pct"] = min(100, round(r["days_cover"] / max_days * 100))
            r["threshold_pct"] = min(100, round(r["lead_time_days"] / max_days * 100))
    else:
        coverage_rows = []

    summary = {
        "material_count": len(rows),
        "critical": sum(1 for r in rows if r["status"] == "critical"),
        "reorder_now": sum(1 for r in rows if r["status"] == "reorder_now"),
        "watch": sum(1 for r in rows if r["status"] == "watch"),
        "no_usage": sum(1 for r in rows if r["status"] == "no_usage"),
        "estimated_lead": sum(1 for r in rows if r["lead_time_estimated"] and r["status"] != "no_usage"),
    }
    analytics_category, analytics_tabs = _analytics_tabs(request, "forecast")
    return render(request, "analytics/forecast.html", {
        "rows": shown,
        "coverage_rows": coverage_rows,
        "summary": summary,
        "window": window,
        "window_choices": FORECAST_WINDOWS,
        "status_filter": status_filter,
        "query": query,
        "status_choices": [
            ("", "All"),
            ("action_required", "Action Required"),
            ("watch", "Watch"),
            ("healthy", "Healthy"),
            ("no_data", "No Data"),
        ],
        "analytics_category": analytics_category,
        "analytics_tabs": analytics_tabs,
        "analytics_active": "forecast",
    })


@login_required
def capacity_forecast_view(request):
    rows = analytics.capacity_forecast()
    history_days = max((r["snapshot_count"] for r in rows), default=0)
    summary = {
        "warehouse_count": len(rows),
        "critical": sum(1 for r in rows if r["status"] == "critical"),
        "watch": sum(1 for r in rows if r["status"] == "watch"),
        "no_data": sum(1 for r in rows if r["status"] == "no_data"),
        "history_days": history_days,
        "empty": history_days == 0,
        "building": 0 < history_days < 7,
    }
    analytics_category, analytics_tabs = _analytics_tabs(request, "capacity_forecast")
    return render(request, "analytics/capacity_forecast.html", {
        "rows": rows,
        "summary": summary,
        "analytics_category": analytics_category,
        "analytics_tabs": analytics_tabs,
        "analytics_active": "capacity_forecast",
    })


# --------------------------------------------------------------------------------
# Tier 3 - AI Copilot: category briefings + the personal checklist
# --------------------------------------------------------------------------------
# Six company-wide, category-scoped briefings (core/briefing.py) plus one
# personal "My Open Jobs" checklist. The checklist ("AI Copilot" sidebar link)
# is any authenticated user's own data; the category briefings are Admin/
# Manager-gated, same as System Settings-adjacent, cost-sensitive features.

CATEGORY_TABS = (
    ("materials", "Materials"),
    ("products", "Products"),
    ("sales", "Sales"),
    ("purchase", "Purchase"),
    ("logistics", "Logistics"),
    ("warehouse", "Warehouse"),
)
_CATEGORY_LABELS = dict(CATEGORY_TABS)

_CATEGORY_SIGNAL_LINKS = {
    "materials": [("stockout_forecast", "forecast", "Stockout & Reorder Forecast")],
    "products": [("production_yield_variance", "production_yield", "Yield Performance")],
    "sales": [
        ("sales_order_delivery_risk", "so_delivery_risk", "Delivery Performance"),
        ("product_sales_trend", "product_sales_trend", "Demand & Sales Trends"),
    ],
    "purchase": [("supplier_reliability", "supplier_scorecard", "Supplier Performance")],
    "logistics": [("shipment_logistics", "shipment_logistics", "Logistics Performance")],
    "warehouse": [
        ("capacity_runway", "capacity_forecast", "Inventory Capacity"),
        ("stock_audit_accuracy", "audit_accuracy", "Inventory Audit Accuracy"),
    ],
}


def _can_generate_briefing(user):
    return user.is_superuser or getattr(user, "role", None) in ("Admin", "Manager")


def _parse_briefing_body(briefing):
    """Split body_text into (points, watch, outlook) - outlook is the hedged
    "Outlook (estimate)" section, present only when the payload had trend data."""
    if briefing is None:
        return [], "", []
    points, watch, outlook = [], "", []
    mode = "points"
    for line in (briefing.body_text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if low.startswith("outlook"):
            mode = "outlook"
            continue
        if mode == "points" and low.startswith("watch:"):
            watch = s[len("watch:"):].strip()
            continue
        item = s[2:].strip() if s[:2] in ("- ", "* ", "• ") else s
        (outlook if mode == "outlook" else points).append(item)
    return points, watch, outlook


def _signal_sections_for(latest, links):
    sections = []
    if latest:
        sig = latest.signals_json or {}
        for key, url_name, label in links:
            payload = sig.get(key)
            n = sum(len(v) for v in payload.values()) if isinstance(payload, dict) else len(payload or [])
            sections.append({"label": label, "url_name": url_name, "count": n})
    return sections


@login_required
def category_briefing_view(request, category):
    """One of the six domain briefings (Materials/Products/Sales/Purchase/
    Logistics/Warehouse). Admin/Manager can trigger a run; anyone can read."""
    from .models import OpsBriefing

    if category not in _CATEGORY_LABELS:
        raise Http404(f"Unknown briefing category: {category!r}")

    can_generate = _can_generate_briefing(request.user)

    if request.method == "POST":
        if not can_generate:
            messages.error(request, "Only Admin or Manager can generate a briefing.")
            return redirect("category_briefing", category=category)
        from .briefing import generate_briefing
        b = generate_briefing(category=category, period=request.POST.get("period", "daily"), user=request.user)
        if b.status == "ok":
            messages.success(request, f"Briefing generated ({b.model_id}, {b.signal_count} signals).")
        elif b.status == "empty":
            messages.success(request, "Nothing notable to brief right now.")
        elif b.status == "skipped":
            messages.warning(request, f"Briefing skipped: {b.error_detail}")
        else:
            messages.error(request, f"Briefing failed: {b.error_detail}")
        return redirect("category_briefing", category=category)

    latest = OpsBriefing.objects.filter(category=category, status="ok").first()
    last_attempt = OpsBriefing.objects.filter(category=category).first()
    points, watch, outlook = _parse_briefing_body(latest)

    return render(request, "analytics/category_briefing.html", {
        "category": category,
        "category_label": _CATEGORY_LABELS[category],
        "tabs": CATEGORY_TABS,
        "briefing": latest,
        "points": points,
        "watch": watch,
        "outlook": outlook,
        "last_attempt": last_attempt,
        "stale_attempt": last_attempt if (last_attempt and last_attempt != latest) else None,
        "signal_sections": _signal_sections_for(latest, _CATEGORY_SIGNAL_LINKS[category]),
        "recent": OpsBriefing.objects.filter(category=category)[:8],
        "can_generate": can_generate,
        "has_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "model_setting": get_setting("ops_briefing_model"),
        "briefing_enabled": get_setting("ops_briefing_enabled"),
    })


@login_required
def ops_briefing_view(request):
    """
    The personal checklist - "My Open Jobs". The table of live_items is always
    computed straight from the database (no API dependency); the optional
    Claude-prioritised card on top is generated on demand by the user it's
    for - any authenticated user, not just Admin/Manager, since it is only
    ever their own data and their own click.
    """
    from .models import OpsBriefing

    live_items = analytics.my_open_jobs(request.user)

    if request.method == "POST":
        from .briefing import generate_my_checklist
        b = generate_my_checklist(user=request.user)
        if b.status == "ok":
            messages.success(request, f"Checklist generated ({b.model_id}, {b.signal_count} open item(s)).")
        elif b.status == "empty":
            messages.success(request, "Nothing open right now.")
        elif b.status == "skipped":
            messages.warning(request, f"Checklist skipped: {b.error_detail}")
        else:
            messages.error(request, f"Checklist failed: {b.error_detail}")
        return redirect("ops_briefing")

    latest = OpsBriefing.objects.filter(
        category="my_checklist", generated_by=request.user, status="ok"
    ).first()
    last_attempt = OpsBriefing.objects.filter(
        category="my_checklist", generated_by=request.user
    ).first()
    points, watch, outlook = _parse_briefing_body(latest)

    return render(request, "analytics/ops_briefing.html", {
        "tabs": CATEGORY_TABS,
        "live_items": live_items,
        "briefing": latest,
        "points": points,
        "watch": watch,
        "outlook": outlook,
        "last_attempt": last_attempt,
        "stale_attempt": last_attempt if (last_attempt and last_attempt != latest) else None,
        "recent": OpsBriefing.objects.filter(category="my_checklist", generated_by=request.user)[:8],
        "has_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "model_setting": get_setting("ops_briefing_model"),
        "briefing_enabled": get_setting("ops_briefing_enabled"),
    })


@login_required
def shipment_logistics_view(request):
    rows = analytics.shipment_logistics()
    summary = {
        "shipment_count": len(rows),
        "discrepant": sum(1 for r in rows if r["risk"] == "discrepant"),
        "overdue": sum(1 for r in rows if r["risk"] == "overdue"),
        "at_risk": sum(1 for r in rows if r["risk"] == "at_risk"),
        "stalled": sum(1 for r in rows if r["risk"] == "stalled"),
        "pending": sum(1 for r in rows if r["risk"] == "pending"),
    }
    analytics_category, analytics_tabs = _analytics_tabs(request, "shipment_logistics")
    return render(request, "analytics/shipment_logistics.html", {
        "rows": rows,
        "summary": summary,
        "analytics_category": analytics_category,
        "analytics_tabs": analytics_tabs,
        "analytics_active": "shipment_logistics",
    })


@login_required
def product_sales_trend_view(request):
    rows = analytics.product_sales_trend()
    summary = {
        "product_count": len(rows),
        "declining": sum(1 for r in rows if r["status"] == "declining"),
        "rising": sum(1 for r in rows if r["status"] == "rising"),
        "flat": sum(1 for r in rows if r["status"] == "flat"),
        "new": sum(1 for r in rows if r["status"] == "new"),
    }
    analytics_category, analytics_tabs = _analytics_tabs(request, "product_sales_trend")
    return render(request, "analytics/product_sales_trend.html", {
        "rows": rows,
        "summary": summary,
        "window_months": analytics.TREND_WINDOW_MONTHS,
        "analytics_category": analytics_category,
        "analytics_tabs": analytics_tabs,
        "analytics_active": "product_sales_trend",
    })
