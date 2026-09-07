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
