"""
core/briefing.py - Tier 3 "AI Copilot": category briefings + the personal checklist.

Two stateless Claude calls, no tool loop, no database access for the model:
category briefings narrate the notable Tier 1/2 signals per domain
(collect_signals/generate_briefing); the personal checklist narrates one
user's own open records (analytics.my_open_jobs/generate_my_checklist).
Both persist an OpsBriefing row. See SYSTEM_DOCUMENTATION.md §8.9-§8.11.

Config (settings_store.py, editable at /system/settings/): ops_briefing_enabled,
ops_briefing_model. The API key is the ANTHROPIC_API_KEY env var - a secret,
deliberately not a SystemSetting.
"""
from __future__ import annotations

import datetime as _dt
import json
import os

from django.utils import timezone

from . import analytics
from .settings_store import get_setting

try:  # the package is only needed when a briefing is actually generated
    import anthropic
except ImportError:  # pragma: no cover - exercised via the "not installed" path
    anthropic = None

# How far back the reliability / accuracy roll-ups look for a category briefing.
SIGNAL_WINDOW_DAYS = 180
# Hard cap on rows per section so the prompt stays small and cheap.
MAX_ROWS_PER_SECTION = 12
# Response ceiling. Keep generous - reasoning models spend thinking tokens
# against this budget before any text, so too low yields an empty reply.
MAX_OUTPUT_TOKENS = 4000


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _num(x, places=2):
    """Round floats for the payload; pass through None / ints unchanged."""
    if x is None:
        return None
    if isinstance(x, float):
        return round(x, places)
    return x


def _iso(d):
    return d.isoformat() if isinstance(d, (_dt.date, _dt.datetime)) else None


def _split_headline(text):
    """(headline, body) from the model's plain-text output."""
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        return "", ""
    headline = lines[0].lstrip("#-*• ").strip()[:255]
    body = "\n".join(lines[1:]).strip()
    return headline, body


def _call_claude(*, model_id, system_prompt, user_content):
    """One client.messages.create() call. Returns (text, model, input_tokens,
    output_tokens, error_detail); error_detail is None on success. Never raises."""
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=model_id,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        ).strip()
        model = getattr(resp, "model", model_id) or model_id
        in_tok = getattr(resp.usage, "input_tokens", None)
        out_tok = getattr(resp.usage, "output_tokens", None)
        if not text:
            # Usually max_tokens was consumed by reasoning before any text block.
            stop_reason = getattr(resp, "stop_reason", None)
            detail = "Model returned no text block"
            if stop_reason:
                detail += f" (stop_reason={stop_reason})"
            if stop_reason == "max_tokens":
                detail += "; raise MAX_OUTPUT_TOKENS or use a lighter model."
            return "", model, in_tok, out_tok, detail[:500]
        return text, model, in_tok, out_tok, None
    except Exception as exc:  # noqa: BLE001 - any API/network error is recorded, not raised
        return "", model_id, None, None, f"{type(exc).__name__}: {exc}"[:500]


# ---------------------------------------------------------------------------
# Category briefings - signal collection
# ---------------------------------------------------------------------------

# Weeks of history handed to Claude for the optional "Outlook (estimate)"
# section - a real trend line, not just a single current-state number.
TREND_WEEKS = 6
# Snapshot points handed to Claude for the same purpose (capacity).
TREND_SNAPSHOTS = 8


def _weekly_burn_trend(material, weeks=TREND_WEEKS):
    """Oldest-to-newest mean daily consumption per week, for the Outlook trend field."""
    daily = analytics.daily_consumption(material, window_days=weeks * 7)
    today = timezone.now().date()
    buckets = []
    for w in range(weeks):
        end = today - _dt.timedelta(days=7 * w)
        start = end - _dt.timedelta(days=7)
        total = sum(float(qty) for day, qty in daily.items() if start < day <= end)
        buckets.append(_num(total / 7, 2))
    buckets.reverse()
    return buckets


def _stockout_signals(since):
    from .models import Material

    rows = analytics.stockout_forecast(window_days=analytics.DEFAULT_WINDOW_DAYS)
    notable = [r for r in rows if r["status"] in ("critical", "reorder_now", "watch")][:MAX_ROWS_PER_SECTION]
    materials = Material.objects.in_bulk([r["material_id"] for r in notable])

    out = []
    for r in notable:
        row = {
            "material": f'{r["sku"]} - {r["name"]}',
            "status": r["status"],
            "available": _num(r["available"]),
            "daily_burn": _num(r["daily_rate"], 3),
            "days_cover": _num(r["days_cover"], 1),
            "stockout_date": _iso(r["stockout_date"]),
            "reorder_by": _iso(r["reorder_by_date"]),
            "lead_time_days": r["lead_time_days"],
            "lead_time_estimated": r["lead_time_estimated"],
            "on_order": _num(r["on_order"]),
        }
        material = materials.get(r["material_id"])
        if material:
            row["burn_trend_weekly"] = _weekly_burn_trend(material)
        out.append(row)
    return out


def _capacity_signals(since):
    from .models import WarehouseUtilizationSnapshot

    rows = analytics.capacity_forecast()
    notable = [r for r in rows if r["status"] in ("critical", "watch")][:MAX_ROWS_PER_SECTION]

    out = []
    for r in notable:
        row = {
            "warehouse": r["name"],
            "status": r["status"],
            "current_percent": _num(r["current_percent"], 1),
            "weekly_rate_pp": _num(r["weekly_rate_pp"]),
            "days_to_full": _num(r["days_to_full"], 0),
            "projected_full_date": _iso(r["projected_full_date"]),
            "snapshot_count": r["snapshot_count"],
        }
        snaps = list(
            WarehouseUtilizationSnapshot.objects.filter(warehouse_id=r["warehouse_id"])
            .order_by("-snapshot_date")[:TREND_SNAPSHOTS]
        )
        if snaps:
            row["utilization_trend"] = [
                {"date": _iso(s.snapshot_date), "percent": _num(float(s.utilization_percent), 1)}
                for s in reversed(snaps)
            ]
        out.append(row)
    return out


def _supplier_signals(since):
    rows = analytics.supplier_reliability(since=since)
    notable = [r for r in rows if r["rating"] in ("poor", "watch")]
    return [
        {
            "supplier": r["supplier_name"],
            "rating": r["rating"],
            "on_time_rate": _num(r["on_time_rate"], 3),
            "fill_rate": _num(r["fill_rate"], 3),
            "avg_delay_days": _num(r["avg_delay_days"], 1),
            "open_pos": r["open_count"],
            "assessable_pos": r["assessable_count"],
            "estimated_share": _num(r["estimated_share"], 2),
            "last_delivery": _iso(r["last_delivery"]),
        }
        for r in notable[:MAX_ROWS_PER_SECTION]
    ]


def _delivery_signals(since):
    rows = analytics.sales_order_delivery_risk()
    notable = [r for r in rows if r["risk"] in ("late", "at_risk")]
    return [
        {
            "sales_order": r["so_number"],
            "client": r["client_name"],
            "risk": r["risk"],
            "status": r["status"],
            "deadline": _iso(r["deadline"]),
            "projected_arrival": _iso(r["arrival"]),
            "arrival_is_actual": r["arrival_is_actual"],
            "days_slack": r["days_slack"],
            "shipped_fraction": _num(r["shipped_fraction"], 2),
            "production": r["production_summary"],
        }
        for r in notable[:MAX_ROWS_PER_SECTION]
    ]


def _product_trend_signals(since):
    rows = analytics.product_sales_trend()
    notable = [r for r in rows if r["status"] in ("declining", "rising")]
    return [
        {
            "product": f'{r["sku"]} - {r["name"]}',
            "status": r["status"],
            "pct_change": r["pct_change"],
            "recent_avg_qty": r["recent_avg_qty"],
            "earlier_avg_qty": r["earlier_avg_qty"],
            "total_qty": r["total_qty"],
            "monthly_qty_trend": r["monthly_qty"],
        }
        for r in notable[:MAX_ROWS_PER_SECTION]
    ]


def _audit_signals(since):
    data = analytics.audit_accuracy(since=since)
    warehouses = [
        {
            "warehouse": r["name"],
            "audits": r["audit_count"],
            "accuracy_rate": _num(r["accuracy_rate"], 3),
            "net_variance": _num(r["net_variance"], 2),
            "shrinkage": _num(r["shrinkage"], 2),
            "mean_abs_variance": _num(r["mean_abs_variance"], 2),
            "chronic_shrinkage": r["chronic_shrinkage"],
        }
        for r in data["by_warehouse"]
        if r["chronic_shrinkage"] or (r["accuracy_rate"] is not None and r["accuracy_rate"] < 0.8)
    ][:MAX_ROWS_PER_SECTION]
    items = [
        {
            "item": r["name"],
            "audits": r["audit_count"],
            "accuracy_rate": _num(r["accuracy_rate"], 3),
            "net_variance": _num(r["net_variance"], 2),
        }
        for r in data["by_item"]
        if r["accuracy_rate"] is not None and r["accuracy_rate"] < 0.8 and r["audit_count"] >= 2
    ][:5]
    return {"by_warehouse": warehouses, "by_item": items}


def _yield_signals(since):
    data = analytics.production_yield_variance()
    notable = [r for r in data["by_product"] if r["rating"] in ("poor", "watch")]
    return [
        {
            "product": r["name"],
            "rating": r["rating"],
            "runs": r["run_count"],
            "mean_yield_pct": _num(r["mean_yield_pct"], 1),
            "mean_yield_variance_pct": _num(r["mean_yield_variance_pct"], 1),
            "mean_material_overuse_pct": _num(r["mean_material_overuse_pct"], 1),
        }
        for r in notable[:MAX_ROWS_PER_SECTION]
    ]


def _logistics_signals(since):
    rows = analytics.shipment_logistics()
    notable = [r for r in rows if r["risk"] in ("discrepant", "overdue", "at_risk", "stalled")]
    return [
        {
            "tracking_number": r["tracking_number"],
            "direction": r["direction"],
            "status": r["status"],
            "sales_order": r["so_number"],
            "purchase_order": r["po_number"],
            "origin": r["origin"],
            "destination": r["destination"],
            "expected_eta": _iso(r["expected_eta_date"]),
            "actual_arrival": _iso(r["actual_arrival_date"]),
            "has_discrepancy": r["has_discrepancy"],
            "risk": r["risk"],
        }
        for r in notable[:MAX_ROWS_PER_SECTION]
    ]


# Which analytics feed which category. Every builder takes (since) - even the
# ones that ignore it - so collect_signals() can call them uniformly.
CATEGORY_SIGNAL_BUILDERS = {
    "materials": [("stockout_forecast", _stockout_signals)],
    "products": [("production_yield_variance", _yield_signals)],
    "sales": [
        ("sales_order_delivery_risk", _delivery_signals),
        ("product_sales_trend", _product_trend_signals),
    ],
    "purchase": [("supplier_reliability", _supplier_signals)],
    "logistics": [("shipment_logistics", _logistics_signals)],
    "warehouse": [
        ("capacity_runway", _capacity_signals),
        ("stock_audit_accuracy", _audit_signals),
    ],
}


def _section_len(section):
    if isinstance(section, dict):
        return sum(len(v) for v in section.values())
    return len(section or [])


def collect_signals(category, *, window_days=SIGNAL_WINDOW_DAYS, end=None):
    """One category's notable-only analytics signals as a JSON-serialisable
    dict. Raises ValueError for an unknown category."""
    if category not in CATEGORY_SIGNAL_BUILDERS:
        raise ValueError(f"Unknown briefing category: {category!r}")

    today = end or timezone.now().date()
    since = today - _dt.timedelta(days=window_days)

    payload = {}
    count = 0
    for key, builder in CATEGORY_SIGNAL_BUILDERS[category]:
        section = builder(since)
        payload[key] = section
        count += _section_len(section)

    return {
        "category": category,
        "generated_for": today.isoformat(),
        "lookback_days": window_days,
        "signal_count": count,
        **payload,
    }


# ---------------------------------------------------------------------------
# Category briefings - prompt + generation
# ---------------------------------------------------------------------------

_BRIEFING_PROMPT_TEMPLATE = """\
You write the {intro}. Every number in the payload has already been calculated
and verified by the system.

Rules:
- Use ONLY the figures in the payload. Never invent, estimate, average, or
  extrapolate a number, name, date, or count that is not present - except in
  the separate Outlook section described below, where that is the point.
- Lead with the single most urgent item.
- Be specific: name the material, warehouse, supplier, order, or shipment and
  quote its figure (days of cover, projected-full date, on-time rate, days of
  slack, ETA).
- If a section of the payload is empty, treat that area as clear - do not
  speculate about it.
- No preamble, no sign-off. Recommendations are limited to "reorder now",
  "expedite", or "investigate". Operational voice, present tense.

Output as plain text:
- First line: a one-sentence headline (no "Headline:" label, no markdown).
- Then 3 to 6 lines, each starting with "- ", most urgent first.
- Optional last line starting with "Watch: " for things that are not urgent yet.

Trend data: some rows carry a field like "burn_trend_weekly",
"utilization_trend", or "monthly_qty_trend" - real historical figures, oldest
first, not a summary. ONLY if such a field is present anywhere in the payload,
you may add one more section at the very end:

Outlook (estimate):
- 1 to 3 lines, each starting with "- ".
- Every line must use a hedge word ("likely", "could", "may", "roughly") -
  never phrase it as a settled fact.
- Every line must name what it's based on (e.g. "burn rate rose in each of the
  last 3 weeks") from a plain read of the trend figures given - not a new
  number you calculate, and never a specific future date or quantity.
- If no trend field is present anywhere in the payload, omit this section
  entirely - never write "Outlook" with nothing to base it on.
"""

CATEGORY_INTROS = {
    "materials": "Materials briefing for TerraHub, covering raw materials near stock-out",
    "products": "Products briefing for TerraHub, covering production yield and material-overuse variance",
    "sales": "Sales briefing for TerraHub, covering at-risk and late sales-order deliveries, and products with a rising or declining sales trend",
    "purchase": "Purchase briefing for TerraHub, covering unreliable suppliers",
    "logistics": "Logistics briefing for TerraHub, covering in-flight shipments - inbound, outbound, and internal transfers - that are discrepant, overdue, at risk, or stalled",
    "warehouse": "Warehouse briefing for TerraHub, covering capacity runway and stock-count accuracy/shrinkage",
}


def _system_prompt(category):
    return _BRIEFING_PROMPT_TEMPLATE.format(intro=CATEGORY_INTROS[category])


def render_prompt(signals, *, period="daily"):
    """The user-turn text: a one-line frame plus the pretty-printed signal JSON."""
    header = (
        f"Signals for {signals.get('generated_for')} "
        f"({signals.get('category')}, {period} briefing, {signals.get('signal_count', 0)} notable). "
        "Write the briefing.\n\n"
    )
    return header + json.dumps(signals, indent=2, sort_keys=True)


def generate_briefing(*, category, period="daily", user=None, window_days=SIGNAL_WINDOW_DAYS):
    """Collect one category's signals, call Claude once, and persist an
    OpsBriefing row. Never raises for an API/config problem - it's recorded
    on the row instead. Returns the saved OpsBriefing."""
    from .models import OpsBriefing

    signals = collect_signals(category, window_days=window_days)
    count = signals["signal_count"]
    base = dict(category=category, period=period, signals_json=signals,
                signal_count=count, generated_by=user)

    if count == 0:
        return OpsBriefing.objects.create(
            status="empty", headline="All clear - no notable signals in this category.", **base
        )

    if not get_setting("ops_briefing_enabled"):
        return OpsBriefing.objects.create(
            status="skipped", error_detail="ops_briefing_enabled is off.", **base
        )

    model_id = get_setting("ops_briefing_model")

    if anthropic is None:
        return OpsBriefing.objects.create(
            status="error", model_id=model_id,
            error_detail="The 'anthropic' package is not installed (pip install anthropic).",
            **base,
        )
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return OpsBriefing.objects.create(
            status="error", model_id=model_id,
            error_detail="ANTHROPIC_API_KEY environment variable is not set.",
            **base,
        )

    text, resolved_model, in_tok, out_tok, error_detail = _call_claude(
        model_id=model_id, system_prompt=_system_prompt(category),
        user_content=render_prompt(signals, period=period),
    )
    if error_detail:
        return OpsBriefing.objects.create(
            status="error", model_id=resolved_model,
            input_tokens=in_tok, output_tokens=out_tok,
            error_detail=error_detail, **base,
        )

    headline, body = _split_headline(text)
    return OpsBriefing.objects.create(
        status="ok",
        headline=headline or f"{category.title()} briefing",
        body_text=body or text,
        model_id=resolved_model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        **base,
    )


# ---------------------------------------------------------------------------
# Personal checklist - "My Open Jobs"
# ---------------------------------------------------------------------------
# Different signal source (analytics.my_open_jobs, not a company-wide roll-up)
# and a different prompt - see CHECKLIST_SYSTEM_PROMPT below.

CHECKLIST_SYSTEM_PROMPT = """\
You are the AI Copilot's personal checklist writer for TerraHub. You are given
one person's own open work items - sales orders, purchase orders, production
runs, stock audits, and shipments they created or are handling - each with its
real reference number, status, and how many days it has been open. Some items
carry a small `context` object (e.g. that sales order is at risk of missing its
deadline, or that purchase order's supplier is unreliable) - use it when
present, it is already verified.

Rules:
- Refer to every item ONLY by the reference/kind already in the payload
  (e.g. "Sales Order SO-1042", "PO-2007", "Shipment TRK-88"). Never invent an
  item, a count, or a number not present in the payload or its `context`.
- Group and prioritise: oldest and highest-risk items first. It is fine to
  group similar items in one line.
- For each item or group, suggest one concrete next action (e.g. "follow up
  with the supplier", "chase approval", "confirm delivery").
- If there is nothing open, say so plainly - do not pad the list.
- No preamble, no sign-off. Direct, encouraging, second person ("you").

Output as plain text:
- First line: a one-sentence headline naming the single most important item.
- Then 3 to 6 lines, each starting with "- ", most urgent/oldest first.
- Optional last line starting with "Watch: " for lower-urgency items.
"""


def _checklist_signals(user):
    items = analytics.my_open_jobs(user)
    trimmed = [
        {
            "kind": it["kind"],
            "reference": it["reference"],
            "status": it["status"],
            "opened_on": _iso(it["opened_on"]),
            "age_days": it["age_days"],
            "ownership": it["ownership"],
            "context": it["context"],
        }
        for it in items[:MAX_ROWS_PER_SECTION * 2]
    ]
    return {
        "category": "my_checklist",
        "generated_for": timezone.now().date().isoformat(),
        "signal_count": len(trimmed),
        "open_items": trimmed,
    }


def render_checklist_prompt(signals):
    header = (
        f"Open items for {signals.get('generated_for')} "
        f"({signals.get('signal_count', 0)} total). Write the checklist.\n\n"
    )
    return header + json.dumps(signals, indent=2, sort_keys=True)


def generate_my_checklist(*, user, window_days=SIGNAL_WINDOW_DAYS):
    """Collect this user's own open items, call Claude once to prioritise/
    annotate them, and persist an OpsBriefing(category='my_checklist',
    generated_by=user) row. window_days is unused - kept for signature symmetry."""
    from .models import OpsBriefing

    signals = _checklist_signals(user)
    count = signals["signal_count"]
    base = dict(category="my_checklist", period="daily", signals_json=signals,
                signal_count=count, generated_by=user)

    if count == 0:
        return OpsBriefing.objects.create(
            status="empty", headline="Nothing open right now.", **base
        )

    if not get_setting("ops_briefing_enabled"):
        return OpsBriefing.objects.create(
            status="skipped", error_detail="ops_briefing_enabled is off.", **base
        )

    model_id = get_setting("ops_briefing_model")

    if anthropic is None:
        return OpsBriefing.objects.create(
            status="error", model_id=model_id,
            error_detail="The 'anthropic' package is not installed (pip install anthropic).",
            **base,
        )
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return OpsBriefing.objects.create(
            status="error", model_id=model_id,
            error_detail="ANTHROPIC_API_KEY environment variable is not set.",
            **base,
        )

    text, resolved_model, in_tok, out_tok, error_detail = _call_claude(
        model_id=model_id, system_prompt=CHECKLIST_SYSTEM_PROMPT,
        user_content=render_checklist_prompt(signals),
    )
    if error_detail:
        return OpsBriefing.objects.create(
            status="error", model_id=resolved_model,
            input_tokens=in_tok, output_tokens=out_tok,
            error_detail=error_detail, **base,
        )

    headline, body = _split_headline(text)
    return OpsBriefing.objects.create(
        status="ok",
        headline=headline or "Your checklist",
        body_text=body or text,
        model_id=resolved_model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        **base,
    )
