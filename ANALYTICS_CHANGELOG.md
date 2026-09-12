# Analytics & Forecasting — Change Log

Running record of the analytics build-out (Tiers 1–3). Newest phase first. Each
entry says **what changed**, **how it works**, and **what is deliberately left for
later**. Algorithms are documented in `SYSTEM_DOCUMENTATION.md` §8.

---

## Phase 0 — RegistryLog material spine  ·  2026-08-31

**Goal:** make `RegistryLog` queryable by raw material so consumption-rate /
stockout / capacity analytics have a clean, structural key instead of parsing
free text. (Decision: option **B** from the design discussion — add an FK — over
option A (string parsing at query time) or C (derive from `RunMaterialUsage`).)

### Changed

| File | Change |
| :--- | :--- |
| `core/models.py` | `RegistryLog.material` — new `ForeignKey('Material', null=True, blank=True, on_delete=SET_NULL, related_name='registry_logs')`. `item_name` stays as the human label; this FK is the machine key. |
| `core/migrations/0035_registrylog_material.py` | Adds the column. Wrapped in `SeparateDatabaseAndState`: DB side uses `ADD COLUMN IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS` because an abandoned branch had already added `material_id` (and an orphan `product_id`) to `core_registrylog` on the shared dev database. No-op where the column exists, real add on a clean DB. |
| `core/migrations/0036_backfill_registrylog_material.py` | Data migration. Best-effort backfill of `material` for pre-existing rows by parsing `item_name`. Reverse is a no-op. |
| `core/analytics.py` | **New module** — the computation layer (pure functions, no request/response). Ships `resolve_material_from_label()`, `daily_consumption()`, `consumption_rate()`. |
| `core/utils.py` | Added `material=` to 3 `RegistryLog.objects.create` calls: consumption (`consume_materials_for_run`), outbound deduction (`deduct_stock_from_allocation`), PO receipt (`apply_po_material_receipt`). |
| `core/views.py` | Added `material=` to 6 `RegistryLog.objects.create` calls: manual receive, stock-audit resolve, material edit, QA expiry extension, spoilage disposal, transfer-received inbound, force-closed outbound. Manual receive gained a `log_material` local (only one branch has a material in scope). |
| `core/management/commands/` | **New package** (empty `__init__.py`s) — home for Phase 2/3 scheduled jobs. |
| `core/tests.py` | +14 tests: `RegistryLogMaterialSpineTests` (FK persists, day/material bucketing, window bounds, rate math, non-consumption actions ignored) and `RegistryLogBackfillResolverTests` (every `item_name` shape the parser recognises). |

### How it works

**The FK.** Every place stock physically moves for a specific material now stamps
`RegistryLog.material`. Movements that are not material-specific stay null on
purpose: finished-goods `Produced` rows, bulk-import summary rows, shipment
"reopened" audit rows, and outbound shipments of *products* (finished goods,
which have no `Material` record).

**Backfill (migration 0036).** Builds `{material.name.lower(): Material}` once,
then for each null-material row whose `action_type` is in
`{Consumed_For_Manufacturing, Inbound, Outbound, Adjusted, Spoiled_Disposal,
QA_Extension}` runs `resolve_material_from_label()`:

| `item_name` shape | Producer |
| :--- | :--- |
| `<name> (Run <run>)` | consumption |
| `<name> (Batch <batch>)` (± ` - force closed`) | inbound / outbound |
| `Manual Receipt of <name>` | manual receive |
| `Updated Material '<name>' (SKU: <sku>)` | material edit |
| `Batch <batch> (<sku> - <name>)` | audit resolve |
| exact `<name>` | fallback |

Unresolved rows are left null (conservative — a null row is simply excluded from
rates, never mis-attributed). `bulk_update` in batches of 500. Prints
`matched X, skipped Y`.

*First run on the dev DB:* **matched 5, skipped 4** — the 4 skips were 1
finished-good `Produced` row and 3 outbound shipments of finished-goods products
(no material exists to match). Material-relevant coverage: **5/5**, including
both `Consumed_For_Manufacturing` rows.

**`consumption_rate(material, window_days=30, end=None)`.** `daily_consumption()`
sums `quantity_changed` from `material.registry_logs` where
`action_type='Consumed_For_Manufacturing'` and `timestamp__date` is within
`(end - window_days, end]`, grouped by day. `consumption_rate` = `total /
window_days` — divided by the **full window**, not by active days, so idle days
correctly pull the burn rate down. Returns a non-negative `Decimal`; `0` when
there is no consumption in the window.

### Verification

- `python manage.py makemigrations --check` → no changes; `python manage.py check`
  → clean.
- `python manage.py migrate core` → `0035 OK`, `0036 OK (matched 5, skipped 4)`.
- `python manage.py test core` → **38 passed** (was 24).

### Not done in Phase 0 (deferred)

- **`RegistryLog.notes`** — `core/views.py` `batch_detail_view` (`update_batch`
  action) passes `notes=` to `RegistryLog.objects.create`, but the model has no
  such field, so that path currently raises `TypeError`. Left untouched to keep
  this migration to a single concern; that call site is also the one skipped
  `material=` addition. Fix separately (add the field or drop the kwarg).
- **Orphan columns** — `core_registrylog.product_id` (+ its FK constraint) exists
  on the dev DB from the abandoned branch and is not in Django state. Harmless
  (always null, ignored by the ORM). Clean up with a dedicated state/DB migration
  if desired.
- No views, URLs, templates, or nav changes yet — those start in Phase 1.

---

## Phase 1 — Tier 1 roll-up widgets  ·  **complete**

Supplier reliability scorecard · SO delivery-risk · audit-accuracy trend ·
production yield variance — all shipped as tabs on their respective hub pages.
None of it is predictive; it is the descriptive/diagnostic foundation the Tier 2
forecasts build on.

### 1e — Audit accuracy + production yield variance  ·  2026-09-01

The last two Tier 1 widgets.

#### Changed

| File | Change |
| :--- | :--- |
| `core/analytics.py` | `audit_accuracy(since=None)` — `StockAudit.variance` rolled up `by_warehouse`, `by_item`, and a monthly `trend`; worst count-accuracy first; `chronic_shrinkage` flag (net negative over 3+ audits). `production_yield_variance()` — completed runs grouped `by_product` / `by_supervisor`: mean yield-vs-plan, signed yield variance %, mean `RunMaterialUsage.variance_pct` (material overuse), good/watch/poor rating. |
| `core/views_analytics.py` | `audit_accuracy_view` (window selector + KPI summary), `production_yield_view` (KPI summary). Both pass a `sections` list so one template renders the by-X tables. |
| `templates/analytics/audit_accuracy.html`, `templates/analytics/production_yield.html` | **New.** |
| `templates/partials/_audit_tabs.html`, `templates/partials/_manufacturing_tabs.html` | **New** tab strips: Ledger / Accuracy, Readiness / Yield. |
| `templates/stock_audit.html`, `templates/manufacturing.html` | Include the tab strip. |
| `core/urls.py` | `+ warehouse/stock-audit/accuracy/` (`audit_accuracy`), `+ operations/manufacture/yield/` (`production_yield`). |
| `core/context_processors.py` | `Stock Tally (Audit)` and `Manufacture` nav items gain `alias_url_names` so they stay active on the new tabs. |
| `core/tests.py` | +15 tests (`AuditAccuracyTests`, `ProductionYieldVarianceTests`): exact/shrink/overage split, warehouse & item grouping, chronic-shrinkage flag, monthly trend, window filter, under-yield & material-overuse ratings, supervisor grouping incl. Unassigned, exclusion of incomplete runs, sort order, both views render. |

#### How it works

**Audit accuracy.** `variance = actual − expected` per `StockAudit`. Per warehouse
and per item: `accuracy_rate = exact / total` (exact = zero variance),
`shrinkage = Σ negative`, `overage = Σ positive`, `net_variance`,
`mean_abs_variance`. Monthly `trend` = count / mean |variance| / net per
`YYYY-MM`. Sorted lowest accuracy, then largest mean |variance|.

**Yield variance.** For `status='Completed'` runs with an `actual_yield`:
`yield_var% = (actual − expected) / expected × 100` (per run, then meaned per
group); `material_overuse% = mean(RunMaterialUsage.variance_pct)` over the run's
usages. Rating: good if yield var ≥ −2% and overuse ≤ 5%; poor if yield var
< −10% or overuse > 15%; watch between. Sorted `(rating, yield var, -overuse)`.

#### Verification

- `manage.py check` clean; `manage.py test core` → **89 passed** (was 74).
- Live: `/operations/manufacture/yield/` shows 4 completed runs across 3 products,
  all "On plan" (dev data has matching expected/actual yields). `audit_accuracy`
  renders its empty state — the dev DB has no `StockAudit` rows. Tab strips
  present on both host pages; nav items stay highlighted.

### 1f — Click-to-sort on the analytics tables  ·  2026-09-01

Default order stays "worst-first"; every column heading is now a sort toggle.

| File | Change |
| :--- | :--- |
| `templates/partials/_sortable_tables.html` | **New** — ~60 lines of dependency-free JS. Finds `table.sortable`, makes `th[data-sort]` clickable (↕ / ▲ / ▼), sorts `tbody` rows by that column, toggles direction, blanks/`—` always last, colspan "empty state" rows left in place. Idempotent, self-initialising. |
| `templates/analytics/supplier_scorecard.html`, `so_delivery_risk.html`, `audit_accuracy.html`, `production_yield.html` | `class="sortable"` on each table; `data-sort="text\|num\|date"` on every heading; `data-sort-value` on cells whose text carries extra tokens (rating badges → severity rank 0–3; on-time `%` with `(n/n) EST`; dates as `Y-m-d`; count cells with `(n open)`; nullable numbers). Footnote gains "click any column heading to re-sort". Each template includes the partial once. |

Verified via `javascript_tool`: numeric, date, text and severity-rank sorts all
order correctly and reverse on second click; no console errors; `manage.py test
core` still **89 passed** (view tests render the templates).

### 1g — Click-to-sort on every list page  ·  2026-09-01

Extended the sort behaviour to all index tables, with a self-configuring script.

| File | Change |
| :--- | :--- |
| `templates/partials/_sortable_tables.html` | Rewritten to **auto-configure**: type per column is detected from the cells (number / date / text), columns whose header is "Action(s)" / "QA Actions" or contains "Timeline" are left non-sortable, and no per-`<th>` markup is needed. Explicit `data-sort` / `data-sort-value` still override (the analytics pages are unchanged). Parsers handle `RM1,234.50`, `Aug. 22, 2026`, `2026-09-01 03:27:20` (to the second), and mangled signs like `--10.00`. |
| `templates/base.html` | Includes the partial once, so it covers every page. |
| `templates/analytics/*.html` | Dropped their individual includes (now inherited from base). |
| `material_list`, `product_list`, `client_list`, `supplier_list`, `po_list`, `so_list`, `shipments`, `user_management`, `registry_ledger`, `qa_dashboard` (×2 tables), `warehouse_inventory` | `class="sortable"` added to the data table. |

**Not included:** `manufacturing.html` (production runs are server-paginated — a
client sort would only reorder the visible page); `dashboard.html` (the parallel
"Executive Dashboard" restyle owns it); detail-page sub-tables and modal
import-preview tables (low value / dynamic).

**Pre-existing quirk noted:** `registry_ledger.html` renders some negative
quantities as `--10.00` (it prepends `-` to an already-negative value). The sort
tolerates it (first number wins); the display bug itself is out of scope here.

Verified on `po_list` (dates + skipped Timeline/Actions), `registry_ledger`
(second-precision timestamps, mangled-sign quantities), `materials` (currency),
and the supplier scorecard (no regression). No console errors; `manage.py test
core` → **89 passed**.

### 1d — Sales-order delivery risk  ·  2026-08-31

**Goal:** per open sales order, will it arrive by its `fulfillment_deadline`?

#### Changed

| File | Change |
| :--- | :--- |
| `core/analytics.py` | New `sales_order_delivery_risk()` + `_so_projected_arrival`, `_classify_so_risk`. Rows worst-first: `late` / `at_risk` / `on_track` / `no_deadline`. |
| `core/settings_store.py` | New tunable `so_at_risk_window_days` (default 7). |
| `core/migrations/0039_*` | Seeds the `so_at_risk_window_days` row. |
| `core/views_analytics.py` | `sales_order_delivery_risk_view` — board + `?risk=` filter + KPI summary. |
| `templates/analytics/so_delivery_risk.html` | **New** — KPI tiles, filter chips, worst-first table (SO → detail link, status, risk badge, deadline, projected arrival, slack, production summary), scoring footnote. |
| `templates/partials/_so_tabs.html` | **New** — Orders / Delivery Risk tab strip. |
| `templates/so_list.html` | Includes the tab strip. |
| `core/urls.py` | `+ operations/sales-orders/risk/` (`so_delivery_risk`). |
| `core/context_processors.py` | `Sales Orders` nav item gains `alias_url_names: ['so_delivery_risk']` so it stays highlighted on the board. |
| `core/tests.py` | +13 tests (`SalesOrderDeliveryRiskTests`): every classification branch, exclusions, sort order, production summary, the window setting changing a verdict, view + `?risk=` filter, tab on the list page. |

#### How it works

```
arrival(so)   = max outbound Shipment.actual_arrival_date          -> is_actual
                 ?? max outbound Shipment.expected_eta_date        -> ETA
                 ?? None (nothing shipped)
shipped_frac  = Σ quantity_shipped / Σ quantity_ordered

risk:
  no_deadline   fulfillment_deadline is None
  with arrival:
    on_track    arrival <= deadline
    late        arrival > deadline and (is_actual or shipped_frac >= 1)
    at_risk     arrival > deadline otherwise
  no shipment yet:
    late        deadline < today
    at_risk     0 <= (deadline - today).days <= so_at_risk_window_days
                and status in (Pending, Awaiting Acknowledgement, In Production)
    on_track    otherwise

days_slack = (deadline - (arrival or today)).days      # negative = behind
```

Open = status in Pending / Awaiting Acknowledgement / In Production / Ready to
Ship / Partially Shipped / Shipped. Sorted `(risk rank, days_slack, so_number)`.

#### Verification

- `makemigrations --check` clean; `migrate` → `0039 OK`; `manage.py test core` →
  **74 passed** (was 61).
- Live at `/operations/sales-orders/risk/`: 4 open orders — SO-1003 (Ready to
  Ship, deadline 6 days ago, nothing shipped) → **Late −6d**; SO-1001 (Shipped,
  ETA one day inside deadline) → **On track +1d**; two Pending orders with no
  deadline → **No deadline**. No console errors; tab strip on both pages.

### 1a — Supplier reliability scorecard  ·  2026-08-31

**Goal:** roll up on-time-delivery and order-fill performance per supplier from
data the PO / logistics flows already record. No schema change.

#### Changed

| File | Change |
| :--- | :--- |
| `core/analytics.py` | New `supplier_reliability(since=None, until=None)` + helpers `_po_actual_arrival`, `_rate_supplier`. Returns a list of per-supplier dicts, worst first. |
| `core/views_analytics.py` | **New module** — thin controllers for analytics pages. `supplier_scorecard_view` + `_parse_window` helper; `WINDOW_CHOICES = (30, 90, 180, 365, 0)`. |
| `templates/analytics/supplier_scorecard.html` | **New** — KPI tiles + worst-first table, window selector, scoring-method footnote. Reuses existing `kpi-item-card` / `modern-table` / `table-card-wrapper` styles. |
| `core/urls.py` | `from . import views, views_analytics`; new route `analytics/suppliers/` → `supplier_scorecard` in a marked "Analytics & Forecasting" block. |
| `core/context_processors.py` | Nav item **Supplier Scorecard** added to the **Catalog & Directory** group, right after Suppliers, gated on `core.view_supplier`. (Design doc floated "Overview"; placed next to Suppliers instead — more discoverable, matches the permission model that group uses.) |
| `core/tests.py` | +8 tests (`SupplierScorecardTests`): good/poor rating, worst-first sort, batch-date arrival fallback, `order_date` window filter, Draft PO exclusion, view renders / requires login. |

#### How it works

Considers POs with status `Pending`, `Partially Received`, or `Completed`
(Draft / Pending Approval / Rejected carry no fulfilment signal), optionally
bounded by `order_date` via `since`/`until`. Groups by the `supplier` FK where
set; POs with only a free-text `supplier_name` roll up under that name (shown
with an "unlinked" tag).

Per supplier:

- **fill_rate** = Σ `quantity_received` / Σ `quantity_ordered` across the
  supplier's PO line items. Can exceed 100 % (over-receipt) — shown verbatim.
- **actual arrival** per PO (`_po_actual_arrival`): latest linked inbound
  `Shipment.actual_arrival_date`; else latest `manufacturing_date` among batches
  received against the PO; else None.
- **on_time_rate** = on-time / assessable, where a PO is *assessable* only if it
  has both an `expected_delivery_date` and a derived arrival; *on time* iff
  `arrival <= expected_delivery_date`.
- **avg_delay_days** = signed mean of `(arrival − expected)` days (+ve = late).
- **rating**: `good` if on-time ≥ 90 % and fill ≥ 98 %; `poor` if on-time < 70 %
  or fill < 90 %; `watch` otherwise; `n/a` if nothing measurable. A missing
  on-time rate is treated as 1.0 for rating so a supplier with no
  `expected_delivery_date` data is judged on fill alone.
- Sorted by `(rating rank, on_time_rate, fill_rate, name)` — worst first.

#### Verification

- `manage.py check` clean; `manage.py test core` → **46 passed** (was 38).
- Rendered against live dev data at `/analytics/suppliers/`: 2 suppliers surface
  ("tnt" unlinked, "Northline Chemicals Sdn Bhd"), styled consistently, nav entry
  active, no console errors.

### 1b — Editable lead time + completed date  ·  2026-08-31

**Goal:** make the on-time metric work now, before POs carry an
`expected_delivery_date`, by deriving a due date from a lead time — and let ops
tune that lead time without a deploy.

#### Changed

| File | Change |
| :--- | :--- |
| `core/models.py` | `PurchaseOrder.completed_date` (`DateField`, null) — authoritative "arrived" date, stamped when receipts flip a PO to `Completed`. New `SystemSetting` model — key/value/`value_type`/description, `updated_by`. |
| `core/settings_store.py` | **New module.** `REGISTRY` of tunables `{key: (default, type, description)}` + `get_setting(key)` returning the typed DB override or the registered default. First entry: `po_default_lead_time_days = 14`. |
| `core/admin.py` | `SystemSettingAdmin` — `list_editable` on `value`, everything else read-only, add/delete disabled (rows come from the seed migration), `updated_by` stamped on save. |
| `core/utils.py` | `apply_po_material_receipt` stamps `completed_date = date.today()` the first time cumulative receipts reach the ordered quantity. |
| `core/migrations/0037_*` | Adds `completed_date` + creates `SystemSetting`. |
| `core/migrations/0038_*` | Seeds `SystemSetting` rows from `settings_store.REGISTRY`; backfills `completed_date` on existing `Completed` POs from their latest received-batch date. |
| `core/analytics.py` | `_po_actual_arrival` now prefers `completed_date`. New `_expected_delivery(po, lead_map, default_lead)` → `(date, is_estimated)`: real `expected_delivery_date`, else `order_date + lead` (max `SupplierMaterial.lead_time_days` across the PO's materials, else the setting). `_supplier_lead_map()` bulk-loads lead times once. Rows gain `estimated_count` / `estimated_share`. |
| `core/views_analytics.py` | Summary gains `assessable_pos`, `estimated_pos`, `estimated_share`. |
| `templates/analytics/supplier_scorecard.html` | `EST` tag on rows scored against a derived due date; "N of M POs scored on an estimated due date" under the on-time KPI; rewritten scoring footnote. |
| `core/tests.py` | +9 tests: `SystemSettingTests` (default / typed override / invalid-value fallback / unknown-key / seeded) and `SupplierScorecardExpectedDateTests` (default-lead estimate, per-supplier lead-time override, explicit date not flagged, `completed_date` as the arrival signal). |

#### How it works

```
arrival(po)   = po.completed_date
                 ?? max linked Shipment.actual_arrival_date
                 ?? max manufacturing_date of received batches
due(po)       = po.expected_delivery_date                         -> not estimated
                 ?? po.order_date + lead                          -> estimated
                    lead = max SupplierMaterial.lead_time_days over the PO's
                           line-item materials for its supplier,
                           else get_setting("po_default_lead_time_days")
```

A PO is assessable once it has an arrival and any due date. Rows report how many
of their assessable POs used an estimated due date; the UI tags those `EST` and
the KPI shows the estimated share, so a committed-date rate and an estimated one
are never silently blended.

**`po_default_lead_time_days`** is edited at **Admin › Core › System settings**.
`get_setting()` reads the typed value (falling back to the `14` default if the
row is missing or unparseable). Read once per `supplier_reliability()` call, not
per PO.

#### Verification

- `makemigrations --check` clean; `manage.py test core` → **55 passed** (was 46).
- `migrate` → `0037 OK`, `0038 OK` (`completed_date` backfill filled 3).
- Live at `/analytics/suppliers/`: on-time rate now shows **100% (n/n) EST** for
  both suppliers with "3 of 3 POs scored on an estimated due date" under the KPI;
  avg delay ≈ −13d (arrived well inside the 14-day assumed lead). Tightens as
  real expected dates / per-supplier lead times are entered or the default is
  tuned.

### 1c — In-site settings page + Suppliers hub tabs  ·  2026-08-31

UX feedback: the setting was only reachable via raw Django admin, and the
scorecard was a stray top-level nav item.

#### Changed

| File | Change |
| :--- | :--- |
| `core/views.py` | New `system_settings_view` — superuser-only in-site editor for `settings_store.REGISTRY`, GET renders a generated form, POST validates each field via `cast_value` and upserts `SystemSetting` with `updated_by`. |
| `core/settings_store.py` | `_cast` → public `cast_value` (now used by the view too). |
| `templates/system_settings.html` | **New** — one row per tunable: label, key, description, default, type-aware input, `CUSTOM` tag when overridden, "last changed by" once a human edits it. |
| `templates/system.html` | "⚙ Operational Settings" button in the console header. |
| `core/urls.py` | `+ system/settings/` (`system_settings`). Scorecard moved `analytics/suppliers/` → **`catalog/suppliers/scorecard/`**. |
| `core/context_processors.py` | Nav: **System Console** and **System Settings** moved out of Overview into the **Admin** group (both superuser-only; the Admin group now shows for superusers even without an Admin/Manager role). Removed the standalone `Supplier Scorecard` item. `Suppliers` now stays highlighted on the scorecard via a new `alias_url_names` key on nav items. |
| `templates/partials/_supplier_tabs.html` | **New** — Directory / Scorecard tab strip, active from `request.resolver_match.url_name`. |
| `templates/supplier_list.html` | h1 "Supplier Directory" → "Suppliers"; includes the tab strip. |
| `templates/analytics/supplier_scorecard.html` | Breadcrumb Dashboard / Suppliers / Scorecard; h1 "Suppliers"; includes the tab strip. |
| `core/tests.py` | +6 tests: `SupplierHubNavTests` (tabs render, scorecard URL path) and `SystemSettingsViewTests` (superuser gate, GET lists settings, POST updates + stamps `updated_by`, POST rejects invalid). |

#### Result

- **Suppliers** is now a hub: one nav entry, `Directory` and `Scorecard` tabs.
  `/analytics/` stays reserved for the cross-cutting Tier 2/3 pages (forecast,
  briefing).
- **System Console → Operational Settings** (`/system/settings/`) edits
  `po_default_lead_time_days` (and every future tunable) with validation and an
  audit of who changed it. Django admin still works as a fallback.
- `manage.py test core` → **61 passed** (was 55).

## Phase 2 — Tier 2 forecasting  ·  **complete**

Consumption-rate stockout ETA + reorder-by **(done)** · warehouse capacity runway
**(done)**.

### 2b — Capacity runway  ·  2026-09-08

The last Tier 2 piece, and the first with a data pipeline: `utilization_percent`
was computed on the fly in `dashboard_view` and thrown away; now it's snapshotted
daily and linear-fit to a "date this warehouse fills up".

| File | Change |
| :--- | :--- |
| `core/models.py` | New `WarehouseUtilizationSnapshot` — `warehouse` FK, `snapshot_date`, `used_mt`, `capacity_mt`, `utilization_percent`; unique on `(warehouse, snapshot_date)`. Migration `0040`. |
| `core/analytics.py` | `used_mt_expr()` — the active-stock-tonnage ORM expression, extracted so `dashboard_view` and the snapshot share one definition. `warehouse_utilization()` — current per-warehouse utilization. `_linreg()` — plain least-squares. `capacity_forecast()` — per warehouse: fit `utilization_percent` over its snapshots, `days_to_full = (100 − current) / slope`, projected full date, status `critical` / `watch` / `ok` / `stable` / `no_data`. |
| `core/views.py` | `dashboard_view` now calls `analytics.used_mt_expr()` instead of an inline `Coalesce(Sum(Case(...)))`. |
| `core/management/commands/snapshot_utilization.py` | **New** — writes one row per warehouse per day (idempotent; `--date` override for backfill). |
| `core/views_analytics.py` | `capacity_forecast_view` — KPI summary + `empty` / `building` (< 7 days) flags. |
| `templates/analytics/capacity_forecast.html` | **New** — "no snapshots yet" and "collecting data" banners, KPI tiles, sortable table (warehouse, status, utilization, weekly rate in pp, days to full, projected full date, snapshot count), method footnote. |
| `core/urls.py` | `+ warehouse/capacity/` (`capacity_forecast`). |
| `core/context_processors.py` | Nav: **Capacity Runway** in Insights, after Stockout Forecast (gated `core.view_warehouse`). |
| `templates/warehouse_list.html` | Header call-out link into it (`_insight_link.html`). |
| `core/admin.py` | `WarehouseUtilizationSnapshot` registered (list + date hierarchy). |
| `core/tests.py` | +10 tests (`CapacityForecastTests`): weighted utilization, command idempotency, `no_data` floor, rising trend → projected date + `watch`, fast fill → `critical`, flat → `stable`, view empty / building states, auth, facility call-out. |

#### Scheduling

`snapshot_utilization` must run **once a day**. It has no scheduler wired — pick one:

- **Heroku Scheduler** add-on → daily job `python manage.py snapshot_utilization`
- **cron** → `0 2 * * * cd /app && python manage.py snapshot_utilization`
- **GitHub Actions** → a `schedule:` workflow hitting the same command

`capacity_forecast()` returns `no_data` until a warehouse has 3 snapshots and the
UI flags "collecting data" until 7. Backfill isn't possible (no utilization
history exists) — the runway starts the day the job first runs.

#### Verification

- `manage.py check` clean; `migrate` → `0040 OK`; `manage.py test core` → **114 passed**.
- Command run against dev data: `Snapshotted 4 warehouse(s)`.
- Live at `/warehouse/capacity/`: with a seeded 7-day rising trend, Main Assembly
  Plant showed **Critical · 88.0% · +30 pp/wk · 3d · 11 Sep 2026**; other
  warehouses "No data" (1 snapshot). Sidebar highlights only `Insights → Capacity
  Runway`; no console errors. Synthetic snapshots then cleared — dev DB holds one
  honest day.

### Sidebar — "Insights" section  ·  2026-09-01

Analytics were scattered as tabs on five parent pages. Gathered them under a new
**Insights** sidebar section (position 2, after Overview).

**Layout: option 2 — the call-out link.** Each domain page (Suppliers, Sales
Orders, Stock Tally, Manufacture, Materials Hub) shows a single header link into
its analytic; the analytics pages are clean standalone destinations under
Insights. One unambiguous sidebar highlight per page — no cross-highlighting.

| File | Change |
| :--- | :--- |
| `core/context_processors.py` | New `insights` nav group — Stockout Forecast, Supplier Scorecard, Delivery Risk, Yield Variance, Audit Accuracy (each gated by the same permission as its parent). New `NAV_ICONS['insights']` (line-chart glyph). "Stockout Forecast" removed from Inventory & Facilities. All five `alias_url_names` on the parent nav items removed (the `alias_url_names` mechanism itself is kept as an extension point). |
| `templates/partials/_insight_link.html` | **New** — the shared header link (`{% include ... with link_url=… link_label=… %}`), a small chart-glyph button. |
| `templates/supplier_list.html`, `so_list.html`, `stock_audit.html`, `manufacturing.html`, `material_list.html` | Their tab-strip include swapped for the `_insight_link.html` include. |
| `templates/analytics/*.html` (5) | Tab-strip include removed. Breadcrumb → `Dashboard / <Parent> / <Analytic>` (parent linked, for the way back); `<h1>` is now the analytic's own name (e.g. "Delivery Risk", "Stockout & Reorder Forecast"). |
| `templates/partials/_supplier_tabs.html`, `_so_tabs.html`, `_audit_tabs.html`, `_manufacturing_tabs.html`, `_material_tabs.html` | **Deleted** — unused. |
| `core/tests.py` | Two nav tests reworded (`test_directory_page_links_to_scorecard`, `test_so_list_links_to_delivery_risk`) to assert the call-out link instead of the tab strip. |

Verified live: on Sales Orders the header shows "Delivery risk board →" and the
sidebar highlights only `Operations → Sales Orders`; on the Delivery Risk page
there's no tab strip and the sidebar highlights only `Insights → Delivery Risk`;
same shape on the forecast and scorecard pages. `manage.py test core` → **101 passed**.

### 2a — Stockout & reorder forecast  ·  2026-09-01

The first genuinely predictive view — a moving-average burn rate turned into
"runs out in N days" and a "reorder by" date. No new infrastructure: it reads the
`RegistryLog` consumption history that Phase 0's `material` FK made queryable.

| File | Change |
| :--- | :--- |
| `core/analytics.py` | `consumption_rates(ids, window_days)` — batched burn rate for many materials in one query. `stockout_forecast(window_days=30)` — per active material: available (on-hand − allocated) ÷ burn → `days_cover`; `stockout_date` = today + cover; `reorder_by_date` = stockout − lead time (max `SupplierMaterial.lead_time_days`, else `po_default_lead_time_days`, flagged `lead_time_estimated`); `on_order` from open POs (shown, not netted); status `critical` / `reorder_now` / `watch` / `ok` / `no_usage` from `days_until_reorder = days_cover − lead_time` (bands: <0, ≤2, ≤14). Cover beyond 10 years → no projected date, `ok`. Available ≤ 0 → `critical`. Worst first. |
| `core/views_analytics.py` | `forecast_view` — burn-rate window selector (14/30/60/90), status filter, KPI summary. |
| `templates/analytics/forecast.html` | **New** — KPI tiles, filter chips, sortable table (Material → edit link, status badge, available, burn/day, days cover, runs out, reorder by + EST tag, on order), method footnote. |
| `core/urls.py` | `+ warehouse/forecast/` (`forecast`). |
| `core/context_processors.py` | Nav: **Stockout Forecast** under Inventory & Facilities (gated `core.view_material`). |
| `core/tests.py` | +12 tests (`StockoutForecastTests`): no-usage, burn-rate/days-cover math, critical/watch/ok bands, already-out, estimated-lead flag + default, allocated reduces available, on-order aggregation, sort order, view + `?status=` filter + auth. |

#### Verification

- `manage.py check` clean; `manage.py test core` → **101 passed** (was 89).
- Live at `/warehouse/forecast/`: 22 materials, 20 "No usage" (dev DB has almost
  no consumption history), 2 "OK" (trace consumption, cover past the 10-year
  horizon). Nav entry active; table auto-sorts; no console errors. The status
  bands are exercised by the tests rather than the demo data.

## Phase 3 — Tier 3 AI ops briefing  ·  2026-09-10

**Goal:** turn the Tier 1/2 signals into a plain-English briefing a manager reads
in 20 seconds, and wire the long-dead `Digital Assistant (AI)` nav link to it.
The LLM does **only** narration — no numeric prediction, no DB access, no tools,
no agent loop. One stateless `messages.create` call over a pre-computed dict.

### Changed

| File | Change |
| :--- | :--- |
| `core/briefing.py` | **New module.** `collect_signals(window_days=180)` — calls the six analytics functions, keeps only notable rows (`critical`/`reorder_now`/`watch` stockout, `critical`/`watch` capacity, `poor`/`watch` supplier, `late`/`at_risk` SO, chronic-shrinkage or <0.8-accuracy warehouses + worst items, `poor`/`watch` yield), caps each section at 12 rows, reduces to name + figures (dates as ISO strings) → one JSON-serialisable dict with `signal_count`. `render_prompt()` — one-line frame + `json.dumps(indent=2)`. `generate_briefing(period, user)` — single `anthropic` `client.messages.create()` with `BRIEFING_SYSTEM_PROMPT` ("use ONLY the figures in the payload"); parses headline + body from the plain-text reply; persists an `OpsBriefing`. Never raises for a config/API/network problem — records `status='error'` on the row. Import of `anthropic` is guarded (`None` if not installed). |
| `core/models.py` | **New `OpsBriefing`** — `generated_at`, `period` (`daily`/`weekly`), `status` (`ok`/`empty`/`skipped`/`error`), `headline`, `body_text`, `signals_json`, `signal_count`, `model_id`, `input_tokens`/`output_tokens`, `error_detail`, `generated_by` (null for the scheduled job). `ordering = ['-generated_at']`. |
| `core/management/commands/generate_ops_briefing.py` | **New.** `--period daily\|weekly`, `--dry-run` (prints the signal payload, no API call, nothing saved). Exit code stays 0 on a recorded failure so a scheduler stays green; `error` prints to stderr. |
| `core/views_analytics.py` | `ops_briefing_view` — renders the latest `status='ok'` row (bullet points + `Watch:` line parsed from `body_text`), a "last good briefing" notice when a newer run failed, per-section back-links to the Insights pages, a recent-runs table, and (Admin/Manager only) a POST "Generate now" button. `os` / `messages` / `redirect` / `get_setting` imports added. |
| `core/urls.py` | `+ assistant/briefing/` → `ops_briefing`. |
| `core/context_processors.py` | `Digital Assistant (AI)` overview item: `url_name` `None` → `'ops_briefing'` (the link is finally live). Comment in the Insights block updated — the briefing lives in Overview, not Insights. |
| `core/settings_store.py` | `REGISTRY` += `ops_briefing_enabled` (bool, default `True`; off → `skipped` run, no API call) and `ops_briefing_model` (str, default `claude-opus-5`). |
| `core/admin.py` | `OpsBriefing` registered read-only (`has_add_permission` → False), `date_hierarchy='generated_at'`, token/status/model columns. |
| `core/migrations/0041_opsbriefing.py` | `CreateModel`. |
| `core/migrations/0042_seed_ops_briefing_settings.py` | Seeds the two new `SystemSetting` rows from `REGISTRY` (idempotent; reverse deletes just those keys). |
| `requirements.txt` | `+ anthropic>=0.40.0`. |
| `templates/analytics/ops_briefing.html` | **New.** Breadcrumb `Dashboard / Digital Assistant (AI)`; "not configured" / "paused" / stale-run banners; briefing card (headline, bullets, Watch line, "drawn from" chips linking each Insights page, model + token footnote); generate form; recent-runs table; method footnote. |
| `core/tests.py` | +17 tests (`OpsBriefingTests`) with a fake `anthropic` module: notable-only signal collection, empty-payload short-circuit (no API call), missing-key / package-missing / disabled → recorded `error`/`skipped` not raised, successful generation stores body + tokens + model, API exception caught, `model_id` from the setting, command `--dry-run` / real run, view auth + empty state + rendered briefing + back-links, generate-button gating, and the once-dead nav link now resolving. |

### How it works

`ANTHROPIC_API_KEY` is read from the **environment** (a secret — deliberately not
a `SystemSetting`). The model sees only `signals_json`, so it can restate the
numbers but cannot invent them; every line of a briefing traces back to a figure
on an Insights page. Model choice and on/off are `SystemSetting`s so cost can be
tuned (`claude-sonnet-5` / `claude-haiku-4-5`) or the paid daily call paused
without a deploy. One run ≈ 4K in + ~1K out tokens ≈ $0.05/day on `claude-opus-5`.

### Deliberately left for later

- **No scheduler.** Like `snapshot_utilization`, `generate_ops_briefing` must be
  wired to a daily job at deploy time (Heroku Scheduler / cron / GitHub Action).
  Until then the page shows "No briefing yet"; Admin/Manager can generate on
  demand.
- **Prompt caching / adaptive thinking** not used — the system prompt is well
  under the cache floor and the task is simple summarisation.
- **Phase 3b (not built)** — a constrained natural-language query box routing a
  question to one of the existing analytics functions (never free-form SQL).

### Verification

- `manage.py makemigrations` → `0041`; `migrate` → `0041`, `0042` OK.
  `manage.py check` clean. `manage.py test core` → **131 passed** (was 114).
- `generate_ops_briefing --dry-run` against dev data: 3 notable signals
  (1 late SO, 1 low-accuracy warehouse + item). Real run with no key →
  `Briefing #1: failed - The 'anthropic' package is not installed` (recorded,
  exit 0); test rows cleared.
- Live at `/assistant/briefing/`: header, "Not configured" banner, empty state,
  disabled "Generate now" button; sidebar highlights only
  *Overview → Digital Assistant (AI)* and the link resolves to
  `/assistant/briefing/`; no console errors.
