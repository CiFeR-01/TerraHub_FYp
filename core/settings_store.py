"""
core/settings_store.py - typed access to editable operational settings.

Tunables that ops staff change without a deploy live in the SystemSetting table
(edited via Django admin). Every setting is registered here with its default,
type, and a human description; a data migration seeds a row per entry so they are
all visible in admin from the start.

Callers use get_setting("key") and get a correctly-typed value: the DB override
if a row exists and parses, otherwise the registered default. Read the value
once per request and pass it down - do not call get_setting() inside a loop.
"""
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
    "logistics_stall_days": (
        5,
        "int",
        "A shipment that has been 'Arrived' for at least this many days without "
        "being marked Completed is flagged 'stalled' on the Logistics Risk "
        "board / Logistics briefing.",
    ),
}


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
    if key not in REGISTRY:
        raise KeyError(f"Unknown system setting: {key!r}")
    default, value_type, _ = REGISTRY[key]

    from .models import SystemSetting

    row = SystemSetting.objects.filter(key=key).values_list("value", "value_type").first()
    if row is None:
        return default
    cast = cast_value(row[0], row[1] or value_type)
    return default if cast is None else cast
