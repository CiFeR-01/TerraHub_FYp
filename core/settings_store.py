"""Typed access to editable settings in the SystemSetting table (registered here with defaults).
get_setting("key") returns the DB value if it parses, else the default; read once per request, not in a loop."""
from __future__ import annotations

# key -> (default, type, description)
REGISTRY: dict[str, tuple[object, str, str]] = {
    "po_default_lead_time_days": (
        14,
        "int",
        "Assumed supplier lead time (days from order date) used as the expected "
        "delivery date on the supplier scorecard when a PO has no expected date "
        "set and no per-supplier/material lead time on file.",
    ),
    "so_at_risk_window_days": (
        7,
        "int",
        "A sales order with no shipment yet and a fulfillment deadline this many "
        "days out (or nearer) is flagged 'at risk' on the delivery-risk board.",
    ),
    "ops_briefing_enabled": (
        True,
        "bool",
        "When off, the generate_ops_briefing command records a 'skipped' briefing "
        "instead of calling the Claude API. Turn off to pause the paid daily call "
        "without a deploy.",
    ),
    "ops_briefing_model": (
        "claude-opus-5",
        "str",
        "Claude model id the AI Copilot (Tier 3) briefings and checklist are "
        "generated with. claude-opus-5 is the default; claude-sonnet-5 or "
        "claude-haiku-4-5 are cheaper for this structured-summarisation job.",
    ),
    "logistics_at_risk_window_days": (
        3,
        "int",
        "A Dispatched shipment with an expected_eta_date this many days out "
        "(or nearer) and not yet arrived is flagged 'at risk' on the Logistics "
        "Risk board / Logistics briefing.",
    ),
    "qa_hold_new_finished_goods": (
        False,
        "bool",
        "When on, finished-goods batches from completed production runs start "
        "Quarantined and must be released on the QA dashboard before they can be "
        "allocated or shipped (a sales order's batch is reserved on release).",
    ),
    "dss_min_total_saving_rm": (
        100.0,
        "float",
        "Rent Opportunities only suggests moving a batch if the rent it would save "
        "over the time it would otherwise stay (until used up or expired) is at "
        "least this much (RM).",
    ),
    "dss_dismiss_snooze_days": (
        30,
        "int",
        "How many days a batch stays out of the Rent Opportunities suggestions "
        "after someone dismisses it.",
    ),
    "dss_saving_horizon_days": (
        365,
        "int",
        "Longest period (days) Rent Opportunities counts rent savings over for a "
        "batch that isn't expected to be used up or expire sooner.",
    ),
    "capacity_trend_window_days": (
        60,
        "int",
        "Inventory Capacity fits each warehouse's fill trend over the most recent "
        "this-many days only, so one old step change (a big shipment, a clear-out) "
        "stops skewing the projection once it is this far behind. 0 = use all history.",
    ),
    "stockout_reorder_now_days": (
        2,
        "int",
        "Stockout & Reorder Forecast: a material whose reorder-by date is this many "
        "days away (or nearer, or already past) is 'Action Required'.",
    ),
    "stockout_watch_days": (
        14,
        "int",
        "Stockout & Reorder Forecast: a material whose reorder-by date is beyond the "
        "'reorder now' band but within this many days is 'Watch'; later is 'Healthy'.",
    ),
    "capacity_critical_percent": (
        95.0,
        "float",
        "Inventory Capacity: a warehouse at or above this utilization (%) is 'Critical' "
        "whatever its trend.",
    ),
    "capacity_critical_days": (
        14,
        "int",
        "Inventory Capacity: a warehouse projected to reach 100% within this many days "
        "is 'Critical'.",
    ),
    "capacity_watch_days": (
        60,
        "int",
        "Inventory Capacity: a warehouse projected to reach 100% within this many days "
        "(but not 'Critical') is 'Watch'; further out is 'Filling'.",
    ),
    "capacity_flat_slope_pp": (
        0.02,
        "float",
        "Inventory Capacity: a fill trend flatter than this many percentage points per "
        "day counts as not filling ('Stable').",
    ),
    "supplier_good_on_time_pct": (
        90.0,
        "float",
        "Supplier Performance: a supplier is 'On track' only with at least this on-time "
        "delivery rate (%) and the fill rate below.",
    ),
    "supplier_good_fill_pct": (
        98.0,
        "float",
        "Supplier Performance: minimum fill rate (%) for 'On track' (with the on-time rate above).",
    ),
    "supplier_poor_on_time_pct": (
        70.0,
        "float",
        "Supplier Performance: a supplier below this on-time delivery rate (%) is 'Poor'.",
    ),
    "supplier_poor_fill_pct": (
        90.0,
        "float",
        "Supplier Performance: a supplier below this fill rate (%) is 'Poor'.",
    ),
    "trend_significant_pct": (
        15.0,
        "float",
        "Demand & Sales Trends: a product is 'Rising' or 'Declining' when its recent "
        "average monthly quantity differs from the earlier average by at least this "
        "many percent; within the band it is 'Flat'.",
    ),
    "logistics_stall_days": (
        5,
        "int",
        "A shipment that has been 'Arrived' for at least this many days without "
        "being marked Completed is flagged 'stalled' on the Logistics Risk "
        "board / Logistics briefing.",
    ),
    "qa_near_expiry_days": (
        30,
        "int",
        "QA & Spoilage: batches expiring within this many days are listed as near expiry "
        "(also the dashboard's degrading-batches panel).",
    ),
    "qa_expiry_warning_days": (
        7,
        "int",
        "QA & Spoilage: QA is notified once when a batch is this many days (or fewer) from expiry.",
    ),
    "qa_auto_hold_expired": (
        True,
        "bool",
        "When on, a batch that passes its expiry date is put on QA hold automatically "
        "(Quarantined, with its reservations released) and QA is notified.",
    ),
    "qa_quarantine_alert_days": (
        14,
        "int",
        "QA & Spoilage: QA is reminded about a batch that has been on hold this many days.",
    ),
    "qa_extension_cap_pct": (
        25.0,
        "float",
        "Shelf-life extensions on a batch may add up to this share (%) of its original shelf life "
        "without approval; anything beyond goes to the Action Center.",
    ),
    "qa_extension_max_days": (
        730,
        "int",
        "Longest single shelf-life extension (days) anyone can request, even with approval. At most 730 (2 years).",
    ),
    "qa_writeoff_approval_rm": (
        5000.0,
        "float",
        "A write-off worth at least this much (RM, at cost or selling price) goes to the Action Center "
        "for approval before stock is removed. 0 = every write-off needs approval.",
    ),
    "qa_writeoff_undo_days": (
        7,
        "int",
        "A manager or admin can undo a write-off for this many days afterwards. 0 = write-offs can't be undone.",
    ),
    "qa_release_needs_second_person": (
        False,
        "bool",
        "When on, the person who put a batch on hold can't also release it (four-eyes rule).",
    ),
}


# Sign-in and session settings. Their defaults come from environment variables (TerraHub/settings.py), so a row here
# overrides the environment. Kept out of REGISTRY because old seed migrations write every REGISTRY default into the table.
SECURITY_REGISTRY: dict[str, tuple[object, str, str]] = {
    "login_fail_limit": (
        lambda: _django_settings().LOGIN_FAIL_LIMIT,
        "int",
        "Failed sign-ins allowed for one username from one computer before it is locked out "
        "(any computers together get four times this). Default comes from LOGIN_FAIL_LIMIT.",
    ),
    "login_lockout_minutes": (
        lambda: _django_settings().LOGIN_LOCKOUT_SECONDS // 60,
        "int",
        "How long a locked-out username has to wait before trying again. "
        "Admins can unlock someone sooner from the Users page. Default comes from LOGIN_LOCKOUT_MINUTES.",
    ),
    "session_idle_timeout_minutes": (
        lambda: _django_settings().SESSION_IDLE_TIMEOUT // 60,
        "int",
        "Signed-in users are signed out after this many minutes without using the app. "
        "It can be shortened here; it can't go above SESSION_IDLE_TIMEOUT_MINUTES, which sets the default.",
    ),
}

# Smallest and largest allowed value; a callable bound is read from the environment when checked.
LIMITS = {
    "login_fail_limit": (3, 50),
    "login_lockout_minutes": (1, 24 * 60),
    "session_idle_timeout_minutes": (5, lambda: _django_settings().SESSION_IDLE_TIMEOUT // 60),
    "qa_near_expiry_days": (1, 365),
    "qa_expiry_warning_days": (0, 365),
    "qa_quarantine_alert_days": (1, 365),
    "qa_extension_cap_pct": (0, 500),
    "qa_extension_max_days": (1, 730),
    "qa_writeoff_approval_rm": (0, None),
    "qa_writeoff_undo_days": (0, 365),
}

ALL_SETTINGS = {**SECURITY_REGISTRY, **REGISTRY}

# How the settings page groups them, in order. Every key in ALL_SETTINGS appears exactly once.
SECTIONS = [
    ("Security & sign-in", ["login_fail_limit", "login_lockout_minutes", "session_idle_timeout_minutes"]),
    ("Purchasing & sales", ["po_default_lead_time_days", "so_at_risk_window_days"]),
    ("Logistics", ["logistics_at_risk_window_days", "logistics_stall_days"]),
    ("Quality", ["qa_hold_new_finished_goods", "qa_near_expiry_days", "qa_expiry_warning_days", "qa_auto_hold_expired",
                 "qa_quarantine_alert_days", "qa_extension_cap_pct", "qa_extension_max_days", "qa_writeoff_approval_rm",
                 "qa_writeoff_undo_days", "qa_release_needs_second_person"]),
    ("Stockout & reorder", ["stockout_reorder_now_days", "stockout_watch_days"]),
    ("Inventory capacity", ["capacity_trend_window_days", "capacity_critical_percent", "capacity_critical_days",
                            "capacity_watch_days", "capacity_flat_slope_pp"]),
    ("Suppliers", ["supplier_good_on_time_pct", "supplier_good_fill_pct", "supplier_poor_on_time_pct",
                   "supplier_poor_fill_pct"]),
    ("Demand trends", ["trend_significant_pct"]),
    ("Rent opportunities", ["dss_min_total_saving_rm", "dss_dismiss_snooze_days", "dss_saving_horizon_days"]),
    ("AI Copilot", ["ops_briefing_enabled", "ops_briefing_model"]),
]


def _django_settings():
    from django.conf import settings
    return settings


def default_value(key):
    default = ALL_SETTINGS[key][0]
    return default() if callable(default) else default


def limits(key):
    """(min, max) for `key`, either end None when unbounded."""
    low, high = LIMITS.get(key, (None, None))
    return (low() if callable(low) else low), (high() if callable(high) else high)


def cast_value(raw, value_type):
    """Coerce a raw string to ``value_type``; return None if it does not parse."""
    try:
        if value_type == "int":
            return int(raw)
        if value_type == "float":
            return float(raw)
        if value_type == "bool":
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        return str(raw)
    except (TypeError, ValueError):
        return None


def get_setting(key):
    """Typed value for ``key``: DB override if present and valid, else the default."""
    if key not in ALL_SETTINGS:
        raise KeyError(f"Unknown system setting: {key!r}")
    _, value_type, _ = ALL_SETTINGS[key]
    default = default_value(key)

    from .models import SystemSetting

    row = SystemSetting.objects.filter(key=key).values_list("value", "value_type").first()
    if row is None:
        return default
    cast = cast_value(row[0], row[1] or value_type)
    return default if cast is None else cast
