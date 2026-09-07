"""
core/views_analytics.py - HTTP controllers for the analytics & forecasting pages.

Deliberately thin: parse request params, call core/analytics.py, render a
template under templates/analytics/. All real computation lives in analytics.py so
it stays testable and reusable by management commands. See
SYSTEM_DOCUMENTATION.md section 8 and ANALYTICS_CHANGELOG.md.
"""
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from . import analytics

# Trailing windows offered in the UI. 0 == all time.
WINDOW_CHOICES = (30, 90, 180, 365, 0)
_DEFAULT_WINDOW = 180


def _parse_window(request, default=_DEFAULT_WINDOW):
    try:
        w = int(request.GET.get("window", default))
    except (TypeError, ValueError):
        return default
    return w if w in WINDOW_CHOICES else default


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

    return render(request, "analytics/supplier_scorecard.html", {
        "rows": rows,
        "summary": summary,
        "window_days": window_days,
        "window_choices": WINDOW_CHOICES,
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
    return render(request, "analytics/audit_accuracy.html", {
        "sections": [
            {"title": "By warehouse", "label": "Warehouse", "rows": data["by_warehouse"]},
            {"title": "By item", "label": "Item", "rows": data["by_item"]},
        ],
        "trend": data["trend"],
        "summary": summary,
        "window_days": window_days,
        "window_choices": WINDOW_CHOICES,
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
    return render(request, "analytics/production_yield.html", {
        "sections": [
            {"title": "By product", "label": "Product", "rows": data["by_product"]},
            {"title": "By supervisor", "label": "Supervisor", "rows": data["by_supervisor"]},
        ],
        "summary": summary,
    })


# Burn-rate lookback windows for the stockout forecast (days). No "all time" here
# - a rate is total / window, so the divisor must be a real span.
FORECAST_WINDOWS = (14, 30, 60, 90)


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
    valid = ("critical", "reorder_now", "watch", "ok", "no_usage")
    shown = [r for r in rows if r["status"] == status_filter] if status_filter in valid else rows
    if status_filter not in valid:
        status_filter = ""

    summary = {
        "material_count": len(rows),
        "critical": sum(1 for r in rows if r["status"] == "critical"),
        "reorder_now": sum(1 for r in rows if r["status"] == "reorder_now"),
        "watch": sum(1 for r in rows if r["status"] == "watch"),
        "no_usage": sum(1 for r in rows if r["status"] == "no_usage"),
        "estimated_lead": sum(1 for r in rows if r["lead_time_estimated"] and r["status"] != "no_usage"),
    }
    return render(request, "analytics/forecast.html", {
        "rows": shown,
        "summary": summary,
        "window": window,
        "window_choices": FORECAST_WINDOWS,
        "status_filter": status_filter,
        "status_choices": [
            ("", "All"),
            ("critical", "Critical"),
            ("reorder_now", "Reorder now"),
            ("watch", "Watch"),
            ("ok", "OK"),
            ("no_usage", "No usage"),
        ],
    })
