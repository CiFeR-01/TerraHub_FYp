"""
core/briefing.py - Tier 3 "AI Ops Briefing".

Two steps, no agent loop:

  collect_signals()   - call the Tier 1/2 functions in core/analytics.py and keep
                        only the rows that are actually worth a manager's
                        attention (critical / watch / poor / at-risk). Assemble
                        one compact, JSON-serialisable dict. Pure computation.

  generate_briefing() - render that dict as text, make a single
                        client.messages.create() call to Claude with a tightly
                        constrained system prompt ("use only the numbers
                        provided"), and store the result as an OpsBriefing row.

The model never touches the database and gets no tools: it sees only the
pre-computed signal dict, so it can narrate the numbers but cannot invent them.
Every figure in a briefing traces back to signals_json and to the analytics page
it came from.

Config (core/settings_store.py, editable at /system/settings/ without a deploy):
  ops_briefing_enabled  - False -> record a 'skipped' briefing, skip the API call.
  ops_briefing_model    - Claude model id (default claude-opus-5).

The API key is read from the ANTHROPIC_API_KEY environment variable - it is a
secret and is deliberately NOT a SystemSetting.
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

# How far back the reliability / accuracy roll-ups look for this briefing.
SIGNAL_WINDOW_DAYS = 180
# Hard cap on rows per section so the prompt stays small and cheap.
MAX_ROWS_PER_SECTION = 12
# Response ceiling. The briefing text itself is short (~400-600 tokens), but
# reasoning-capable models (claude-opus-5) spend thinking tokens against this
# budget first - too low and the whole budget is consumed before any text block
# is emitted. Keep generous headroom.
MAX_OUTPUT_TOKENS = 4000

BRIEFING_SYSTEM_PROMPT = """\
You write the operations briefing for TerraHub, a warehouse and manufacturing
management system. You are given a JSON object of pre-computed signals: materials
near stock-out, warehouses near capacity, unreliable suppliers, at-risk sales
orders, stock-count drift, and production yield problems. Every number in it has
already been calculated and verified by the system.

Rules:
- Use ONLY the figures in the payload. Never invent, estimate, average, or
  extrapolate a number, name, date, or count that is not present.
- Lead with the single most urgent item.
- Be specific: name the material, warehouse, supplier, or order and quote its
  figure (days of cover, projected-full date, on-time rate, days of slack).
- If a section of the payload is empty, treat that area as clear - do not
  speculate about it.
- No preamble, no sign-off. Recommendations are limited to "reorder now",
  "expedite", or "investigate". Operational voice, present tense.

Output as plain text:
- First line: a one-sentence headline (no "Headline:" label, no markdown).
- Then 3 to 6 lines, each starting with "- ", most urgent first.
- Optional last line starting with "Watch: " for things that are not urgent yet.
"""


# ---------------------------------------------------------------------------
# Signal collection
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


def _stockout_signals():
    rows = analytics.stockout_forecast(window_days=analytics.DEFAULT_WINDOW_DAYS)
    notable = [r for r in rows if r["status"] in ("critical", "reorder_now", "watch")]
    return [
        {
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
        for r in notable[:MAX_ROWS_PER_SECTION]
    ]


def _capacity_signals():
    rows = analytics.capacity_forecast()
    notable = [r for r in rows if r["status"] in ("critical", "watch")]
    return [
        {
            "warehouse": r["name"],
            "status": r["status"],
            "current_percent": _num(r["current_percent"], 1),
            "weekly_rate_pp": _num(r["weekly_rate_pp"]),
            "days_to_full": _num(r["days_to_full"], 0),
            "projected_full_date": _iso(r["projected_full_date"]),
            "snapshot_count": r["snapshot_count"],
        }
        for r in notable[:MAX_ROWS_PER_SECTION]
    ]


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


def _delivery_signals():
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


def _yield_signals():
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


def collect_signals(*, window_days=SIGNAL_WINDOW_DAYS, end=None):
    """
    Assemble the notable-only analytics signals into one JSON-serialisable dict.

    Only rows a manager would act on are kept (critical / reorder_now / watch /
    poor / late / at_risk / chronic drift). An empty section means "nothing
    notable there", which the prompt treats as "clear".
    """
    today = end or timezone.now().date()
    since = today - _dt.timedelta(days=window_days)

    stockout = _stockout_signals()
    capacity = _capacity_signals()
    suppliers = _supplier_signals(since)
    delivery = _delivery_signals()
    audit = _audit_signals(since)
    yield_ = _yield_signals()

    signal_count = (
        len(stockout) + len(capacity) + len(suppliers) + len(delivery)
        + len(audit["by_warehouse"]) + len(audit["by_item"]) + len(yield_)
    )

    return {
        "generated_for": today.isoformat(),
        "lookback_days": window_days,
        "signal_count": signal_count,
        "stockout_forecast": stockout,
        "capacity_runway": capacity,
        "supplier_reliability": suppliers,
        "sales_order_delivery_risk": delivery,
        "stock_audit_accuracy": audit,
        "production_yield_variance": yield_,
    }


def _signal_count(signals):
    return signals.get("signal_count", 0)


def render_prompt(signals, *, period="daily"):
    """The user-turn text: a one-line frame plus the pretty-printed signal JSON."""
    header = (
        f"Signals for {signals.get('generated_for')} "
        f"({period} briefing, {_signal_count(signals)} notable). "
        "Write the briefing.\n\n"
    )
    return header + json.dumps(signals, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def _split_headline(text):
    """(headline, body) from the model's plain-text output."""
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        return "", ""
    headline = lines[0].lstrip("#-*• ").strip()[:255]
    body = "\n".join(lines[1:]).strip()
    return headline, body


def generate_briefing(*, period="daily", user=None, window_days=SIGNAL_WINDOW_DAYS):
    """
    Collect signals, call Claude once, and persist an OpsBriefing row. Never
    raises for an API/config problem - the failure is recorded on the row so the
    scheduled job stays green and the last good briefing keeps showing.

    Returns the saved OpsBriefing.
    """
    from .models import OpsBriefing

    signals = collect_signals(window_days=window_days)
    count = _signal_count(signals)
    base = dict(period=period, signals_json=signals, signal_count=count, generated_by=user)

    if count == 0:
        return OpsBriefing.objects.create(
            status="empty", headline="All clear - no notable operations signals.", **base
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

    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=model_id,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=BRIEFING_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": render_prompt(signals, period=period)}],
        )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        ).strip()
        stop_reason = getattr(resp, "stop_reason", None)
        in_tok = getattr(resp.usage, "input_tokens", None)
        out_tok = getattr(resp.usage, "output_tokens", None)

        if not text:
            # Usually means max_tokens was consumed by reasoning before any text
            # block was emitted - record it rather than saving a blank "ok".
            detail = "Model returned no text block"
            if stop_reason:
                detail += f" (stop_reason={stop_reason})"
            if stop_reason == "max_tokens":
                detail += "; raise MAX_OUTPUT_TOKENS or use a lighter model."
            return OpsBriefing.objects.create(
                status="error", model_id=getattr(resp, "model", model_id) or model_id,
                input_tokens=in_tok, output_tokens=out_tok,
                error_detail=detail[:500], **base,
            )

        headline, body = _split_headline(text)
        return OpsBriefing.objects.create(
            status="ok",
            headline=headline or "Operations briefing",
            body_text=body or text,
            model_id=getattr(resp, "model", model_id) or model_id,
            input_tokens=in_tok,
            output_tokens=out_tok,
            **base,
        )
    except Exception as exc:  # noqa: BLE001 - any API/network error is recorded, not raised
        return OpsBriefing.objects.create(
            status="error", model_id=model_id,
            error_detail=f"{type(exc).__name__}: {exc}"[:500],
            **base,
        )
