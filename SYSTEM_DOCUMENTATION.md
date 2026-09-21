# TerraHub - System Documentation

TerraHub is a Django-based manufacturing and warehouse operations platform. It tracks materials and finished-goods inventory across warehouses, drives production runs from bills of materials (recipes), manages purchase and sales orders, generates shipments with FEFO-based batch allocation, and provides QA and approvals workflows on top of full batch/lot traceability.

---

## 1. System Overview

Core capabilities:
- **Custom User Model** with per-location access restrictions. Roles are Django **Groups** (Django admin > Groups); code checks permissions only - per-model ones plus the app-wide ones on `Capability` - via `core/permissions.py`, so roles are added or reshaped without code changes.
- **Catalog Management**: materials and products, with CSV import/export and per-product recipes (bills of materials).
- **Warehouse & Inventory**: multi-warehouse, multi-location inventory with full batch/lot tracking, stock audits, and a public batch lookup/print-label flow.
- **Manufacturing**: production runs consuming materials per recipe, with material allocation and yield tracking.
- **Order Management**: purchase orders (inbound) and sales orders (outbound), each with line-item detail and an approvals inbox.
- **Shipments**: transfer and outbound shipments with pick lists, generated from sales-order allocation using FEFO (first-expired-first-out) batch selection.
- **Stock Allocation**: a shared allocation engine (`StockAllocation`) used by both sales orders and production runs to reserve batch quantities.
- **QA Dashboard** and **Registry Ledger** for traceability and audit history (`RegistryLog`, `OrderTimeline`).
- **System Console**: authenticated live database diagnostics and SQL query-logging dashboard.

---

## 2. Architecture & Technology Stack

- **Backend Framework**: Django 6.0.4
- **Language**: Python 3.14
- **Database**: SQLite for local development; PostgreSQL in production via `psycopg2-binary` / `dj-database-url`
- **Deployment**: `gunicorn` + `whitenoise` (see `Procfile`, `runtime.txt`)
- **Frontend / Styling**: Vanilla HTML5, CSS3, Google Fonts (Outfit, Plus Jakarta Sans), server-rendered Django templates

---

## 3. Directory Layout

```text
D:\TerraHub
├── .gitignore
├── .env / .env.example         # Environment configuration
├── manage.py                   # Django management tool
├── requirements.txt            # System dependencies
├── runtime.txt / Procfile      # Deployment configuration
├── README.md                   # Project overview (mirrors this file)
├── SYSTEM_DOCUMENTATION.md     # System architecture & guides
├── TerraHub/                   # Django project configuration module
│   ├── __init__.py
│   ├── asgi.py / wsgi.py
│   ├── settings.py             # Core project configuration
│   └── urls.py                 # Root URL routing (admin/ + core.urls)
├── core/                       # Core Django application
│   ├── migrations/             # Database migration history
│   ├── models.py               # Domain models (see §5)
│   ├── views.py                # Controller views for all modules
│   ├── analytics.py            # Analytics & forecasting computation layer (see §8)
│   ├── settings_store.py       # Typed access to admin-editable operational settings
│   ├── management/commands/    # Scheduled jobs (daily snapshots, AI briefing)
│   ├── urls.py                 # Application URL routing (see §4)
│   ├── utils.py                # Allocation engine (FEFO), stock helpers
│   ├── decorators.py           # permission_or_redirect view decorator
│   ├── permissions.py          # Central permission helpers (approvers, admin override, ...)
│   ├── context_processors.py   # Template context (nav, notifications, etc.)
│   ├── db_tracker.py           # DB query interception & diagnostics
│   ├── admin.py                # Django admin registrations
│   └── tests.py                # Test suite
├── static/
│   ├── css/style.css
│   └── img/landing-dashboard.png  # Static Operations Overview screenshot used on the landing page
└── templates/                  # Server-rendered HTML templates
    ├── base.html                    # Shared layout/theme
    ├── home.html / login.html       # Public entry points
    ├── dashboard.html / profile.html / system.html / user_management.html
    ├── product_list.html / product_detail.html
    ├── material_list.html / material_form.html
    ├── warehouse_list.html / warehouse_form.html / warehouse_inventory.html
    ├── batch_detail.html / batch_public_info.html / batch_print_label.html
    ├── stock_audit.html / registry_ledger.html
    ├── po_list.html / po_detail.html
    ├── so_list.html / so_detail.html / so_allocate.html
    ├── shipments.html / shipment_detail.html / shipment_pick_list.html
    ├── manufacturing.html / production_run_detail.html / production_allocate.html
    ├── qa_dashboard.html / approvals_inbox.html
    └── partials/recipe_studio_modal.html
```

---

## 4. Routing Table

| Path | Name | Controller View | Description |
| :--- | :--- | :--- | :--- |
| `/` | `home` | `home_view` | Public landing page (hero, the three project objectives, traceability flow, decision support). `?batch=<number>` is the "Trace a batch" box: redirects to that batch's public page, or shows a not-found note. Signed-in users are redirected to `/dashboard/` |
| `/login/` | `login` | `LoginView` | Authentication |
| `/logout/` | `logout` | `LogoutView` | Clears session, redirects home |
| `/dashboard/` | `dashboard` | `dashboard_view` | Main authenticated dashboard |
| `/profile/` | `profile` | `profile_view` | User profile |
| `/system/` | `system` | `system_view` | System telemetry console |
| `/system/settings/` | `system_settings` | `system_settings_view` | Operational settings editor (superuser; see §8) |
| `/system/users/` | `user_management` | `user_management_view` | User/role administration |
| `/system/db-logs/` | `db_logs_api` | `db_logs_api_view` | DB query logs & connection status (JSON) |
| `/system/db-logs/clear/` | `db_clear_logs` | `db_clear_logs_view` | Clears in-memory query log buffer |
| `/system/db-logs/test/` | `db_test_op` | `db_test_op_view` | Triggers dummy read/write for diagnostics |
| `/warehouse/inventory/` | `warehouse_inventory` | `warehouse_inventory_view` | Inventory across warehouses |
| `/warehouse/batch/<batch_number>/` | `batch_detail` | `batch_detail_view` | Batch/lot detail & traceability |
| `/warehouse/batch/<batch_number>/print/` | `batch_print_label` | `batch_print_label_view` | Printable batch label |
| `/public/batch/<batch_number>/` | `batch_public_info` | `batch_public_info_view` | Public batch lookup |
| `/warehouse/facilities/` | `warehouse_list` | `facility_management_view` | Warehouse facility list |
| `/warehouse/create/` | `warehouse_create` | `warehouse_create_view` | Create warehouse |
| `/warehouse/<pk>/edit/` | `warehouse_edit` | `warehouse_edit_view` | Edit warehouse |
| `/warehouse/stock-audit/` | `stock_audit` | `stock_audit_view` | Stock audit workflow |
| `/warehouse/registry/` | `registry` | `registry_ledger_view` | Registry/audit ledger |
| `/catalog/products/` | `product_list` | `product_list_view` | Product catalog |
| `/catalog/products/<pk>/` | `product_detail` | `product_detail_view` | Product detail |
| `/catalog/products/export/` | `export_products_csv` | `export_products_csv` | Export products (CSV) |
| `/catalog/products/template/` | `export_product_template` | `export_product_template` | Product import template |
| `/catalog/products/import/` | `import_products` | `import_products` | Bulk import products |
| `/catalog/products/sales-trend/` | `product_sales_trend` | `views_analytics.product_sales_trend_view` | Monthly sales-volume trend per product — "Analytics → Demand" (see §8.12) |
| `/catalog/products/<id>/recipe/get/` | `get_product_recipe_api` | `get_product_recipe_api` | Fetch product recipe (JSON) |
| `/catalog/products/recipe/save/` | `save_product_recipe_api` | `save_product_recipe_api` | Save product recipe (JSON) |
| `/catalog/materials/` | `material_list` | `material_list_view` | Material catalog |
| `/catalog/materials/<pk>/edit/` | `material_edit` | `material_edit_view` | Edit material |
| `/catalog/materials/export/` | `export_materials_csv` | `export_materials_csv` | Export materials (CSV) |
| `/catalog/materials/template/` | `export_material_template` | `export_material_template` | Material import template |
| `/catalog/materials/import/` | `import_materials` | `import_materials` | Bulk import materials |
| `/catalog/recipes/export/` | `export_recipes_csv` | `export_product_recipes_csv` | Export product recipes (CSV) |
| `/operations/sales-orders/` | `so_list` | `sales_order_list_view` | Sales order list |
| `/operations/orders/so/<pk>/` | `so_detail` | `so_detail_view` | Sales order detail |
| `/operations/orders/so/<pk>/allocate/` | `so_allocate` | `so_allocate_view` | FEFO batch allocation for SO |
| `/operations/orders/so/<pk>/create_shipment/` | `so_create_shipment` | `so_create_shipment_view` | Create shipment from SO |
| `/operations/purchase-orders/` | `po_list` | `purchase_order_list_view` | Purchase order list |
| `/operations/orders/po/<pk>/` | `po_detail` | `po_detail_view` | Purchase order detail |
| `/operations/shipments/` | `shipments` | `shipments_view` | Shipment list |
| `/operations/shipments/<pk>/` | `shipment_detail` | `shipment_detail_view` | Shipment detail |
| `/operations/shipments/<pk>/picklist/` | `shipment_pick_list` | `shipment_pick_list_view` | Shipment pick list |
| `/operations/manufacture/` | `readiness` | `manufacturing_view` | Manufacturing readiness dashboard |
| `/operations/manufacture/run/<pk>/` | `production_run_detail` | `production_run_detail_view` | Production run detail |
| `/operations/manufacture/run/<pk>/allocate/` | `production_run_allocate` | `production_run_allocate_view` | Material allocation for a run |
| `/operations/qa/` | `qa_dashboard` | `qa_dashboard_view` | QA dashboard |
| `/operations/approvals/` | `approvals_inbox` | `approvals_inbox_view` | Pending approvals inbox |
| `/operations/notifications/read/` | `mark_notifications_read` | `mark_notifications_read` | Mark notifications read |
| `/catalog/suppliers/scorecard/` | `supplier_scorecard` | `views_analytics.supplier_scorecard_view` | Supplier reliability scorecard — "Scorecard" tab of the Suppliers hub (see §8) |
| `/operations/sales-orders/risk/` | `so_delivery_risk` | `views_analytics.sales_order_delivery_risk_view` | Sales-order delivery-risk board — "Delivery Risk" tab of the Sales Orders hub (see §8) |
| `/warehouse/stock-audit/accuracy/` | `audit_accuracy` | `views_analytics.audit_accuracy_view` | Stock-audit accuracy roll-up — "Accuracy" tab of the Stock Audit hub (see §8) |
| `/operations/manufacture/yield/` | `production_yield` | `views_analytics.production_yield_view` | Production yield variance — "Yield" tab of the Manufacturing hub (see §8) |
| `/warehouse/forecast/` | `forecast` | `views_analytics.forecast_view` | Consumption-rate stockout & reorder forecast (see §8) |
| `/warehouse/capacity/` | `capacity_forecast` | `views_analytics.capacity_forecast_view` | Warehouse capacity runway — daily-snapshot trend (see §8) |
| `/assistant/briefing/` | `ops_briefing` | `views_analytics.ops_briefing_view` | My Open Jobs — the personal checklist; *Overview → AI Copilot* (see §8.11) |
| `/assistant/briefing/<category>/` | `category_briefing` | `views_analytics.category_briefing_view` | One of the six domain briefings (materials/products/sales/purchase/logistics/warehouse) — Claude narration of that category's Tier 1/2 signals (see §8.9) |
| `/operations/shipments/logistics/` | `shipment_logistics` | `views_analytics.shipment_logistics_view` | In-flight shipment risk roll-up — "Analytics → Supply Chain" (see §8.10) |
| `/admin/` | — | Django admin | Django admin site |

---

## 5. Domain Model

Defined in `core/models.py`:

- **Access**: `CustomUser` (+ Django `Group`), `Capability` (table-less holder of app-wide permissions)
- **Facilities**: `Warehouse`, `WarehouseLocation`
- **Catalog**: `Material`, `Product`, `ProductRecipe`
- **Manufacturing**: `ProductionRun`, `RunMaterialUsage`, `ProductionConsumption`
- **Inventory**: `Batch` (lot-level tracking with expiry/manufacturing dates, quantity, allocated quantity)
- **Purchasing**: `PurchaseOrder`, `PurchaseOrderDetail`
- **Sales**: `SalesOrder`, `SalesOrderDetail`
- **Fulfillment**: `Shipment`, `ShipmentItem`
- **Quality & Audit**: `StockAudit`, `RegistryLog` (movement ledger; `RegistryLog.material` FK is the machine-readable key behind the analytics engine — see §8), `OrderTimeline`
- **Allocation**: `StockAllocation` (shared reservation engine used by sales orders and production runs, resolved via FEFO in `core/utils.py`)
- **Messaging**: `Notification`
- **Configuration**: `SystemSetting` (key/value store for admin-editable operational tunables; read via `core/settings_store.py` — see §8)
- **Analytics snapshots**: `WarehouseUtilizationSnapshot` (one row per warehouse per day, written by `manage.py snapshot_utilization`; see §8)
- **AI Copilot**: `OpsBriefing` (one row per category-briefing run or per-user checklist run — stored Claude output plus its input `signals_json` and token counts, keyed by `category`; see §8.9/§8.11)

---

## 6. Development & Setup Guide

### 6.1. Virtual Environment Setup
Requires Python 3.14.
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 6.2. Environment Configuration
Copy `.env.example` to `.env` and fill in local values (database URL, secret key, etc.).

### 6.3. Database Migrations
```powershell
python manage.py makemigrations core
python manage.py migrate
```

### 6.4. Superuser Creation
```powershell
$env:DJANGO_SUPERUSER_PASSWORD="admin"
python manage.py createsuperuser --noinput --username=admin --email=admin@terrahub.local
```

### 6.5. Start Development Server
```powershell
python manage.py runserver
```
The application will be accessible at `http://127.0.0.1:8000/`.

### 6.6. Tests
```powershell
python manage.py test
```

---

## 7. Live Database Diagnostics & Query Logging Console

TerraHub includes an integrated real-time database connection diagnostics helper and SQL query tracker log dashboard built directly into the **System Control Console** (`/system/`).

### 7.1. DB Diagnostics (Connection Health & Latency)
- **Automatic Status Check**: Dynamically checks database connectivity using `connection.ensure_connection()` and runs a benchmark query (`SELECT 1`) to calculate latency.
- **Environment Context Identification**: Detects whether settings are configured to use the live database (PostgreSQL via `django.db.backends.postgresql` backend) or a local development database (SQLite via `django.db.backends.sqlite3` backend).
- **On-Demand Health Testing**: Users can test the latency and active status directly using the "Test Latency & Status" interactive AJAX trigger on the page.

### 7.2. SQL Read/Write Console
- **Query Interception**: Implemented in [core/db_tracker.py](core/db_tracker.py) using a custom `db_query_logging_wrapper` registered via the `connection_created` signal on Django initialization.
- **Classification Badges**: Automatically parses SQL commands to identify operation type:
  - `READ` for `SELECT` queries
  - `WRITE` for `INSERT`, `UPDATE`, and `DELETE` queries
  - `TRANSACTION` for `BEGIN`, `COMMIT`, and `ROLLBACK` commands
- **In-Memory Buffering**: Logs are stored in a thread-safe, size-limited Python `deque` (maximum 100 entries) to guarantee safety and avoid memory expansion or disk write overhead.
- **Tabbed Interactive UI console**:
  - Toggles between the **System Console** (mock commands and registry ledger events) and the **SQL DB queries** console.
  - **Autorefresh Toggle**: Initiates an AJAX polling query (every 2 seconds) to `/system/db-logs/` to update database query logs dynamically.
  - **Clear Console**: Empties the in-memory log buffer via `/system/db-logs/clear/` JSON POST.
  - **Read/Write Debug Operations**: Simple interactive test triggers that run a safe `SELECT` (reading user details) or `INSERT` (creating a dummy notification record) query, showing immediately in the console.

---

## 8. Analytics & Forecasting Engine

A layered analytics capability built on data the platform already records. All
computation lives in **`core/analytics.py`** as pure functions (no request /
response, no side effects) so it can be called identically from web views,
management commands, the shell, and tests. Scheduled jobs live in
`core/management/commands/`. A phase-by-phase build log is kept in
`ANALYTICS_CHANGELOG.md`.

Three tiers:

| Tier | Nature | Examples |
| :--- | :--- | :--- |
| **1** | Roll-ups of stored data (ORM aggregation, no new math) | supplier reliability, sales-order delivery risk, audit-accuracy trend, production yield variance |
| **2** | Statistical forecasting by plain arithmetic (no ML library) | consumption-rate stockout ETA, reorder-by date, warehouse capacity runway |
| **3** | LLM reasoning over the Tier 1/2 signals, organised by domain | six category briefings + a personal "My Open Jobs" checklist — the **AI Copilot** |

**Navigation.** The eight Tier 1/2 views live in the **Analytics** sidebar section
(second, after Overview), collapsed to one sidebar link per domain category —
Inventory, Supply Chain, Demand, Operations (`core/context_processors.py`'s
`ANALYTICS_CATEGORIES`) — each landing on the first report in that category the
user can see. The category's other reports (Inventory: Stockout & Reorder +
Inventory Capacity; Supply Chain: Supplier/Delivery/Logistics Performance;
Demand: Demand & Sales Trends alone; Operations: Yield Performance + Inventory
Audit Accuracy) are reached as a pill tab strip on the page itself
(`templates/analytics/partials/_analytics_tabs.html`, computed per-request by
`views_analytics._analytics_tabs`), not as separate sidebar entries — the strip
only renders when a category has more than one report the user can see. Every
domain hub page — Suppliers, Sales Orders, Stock Tally, Manufacture, Materials
Hub, Products Catalog, Facility Management, Logistics Tracker — also links into
its analytic via a single header call-out
(`templates/partials/_insight_link.html`). The sidebar highlights the category
link for every report inside it (via `alias_url_names`). The Tier 3
**AI Copilot** — six domain-scoped briefings (Materials, Products, Sales,
Purchase, Logistics, Warehouse) plus the personal "My Open Jobs" checklist that
narrate these signals — sits on the **AI Copilot** link in Overview, not in
Analytics; the six briefings share one page with the same tab-strip pattern
(`/assistant/briefing/<category>/`), and the checklist is `/assistant/briefing/`
itself.

### 8.1. The RegistryLog material spine  *(Phase 0 — implemented)*

`RegistryLog` is the append-only ledger of every physical stock movement
(`Inbound`, `Outbound`, `Consumed_For_Manufacturing`, `Produced`, `Adjusted`,
`Spoiled_Disposal`, `QA_Extension`). Historically its only item reference was the
free-text `item_name` (e.g. `"MAP (Run RUN-2010)"`).

`RegistryLog.material` (nullable FK → `Material`, `related_name='registry_logs'`)
adds a machine-readable key. It is stamped at write time wherever a specific raw
material moves; it is left **null** for movements that are not material-specific
(finished-goods `Produced`, bulk-import summaries, outbound shipments of finished
products). Migration `0035` adds the column (guarded with `ADD COLUMN IF NOT
EXISTS` — see the changelog for why); migration `0036` backfills history.

**Backfill algorithm** (`resolve_material_from_label(item_name, by_name)`):
build `{material.name.lower(): Material}`, then for each null-material ledger row
whose action is stock-affecting, match `item_name` against these shapes and take
the first hit — `<name> (Run …)`, `<name> (Batch …)` (± ` - force closed`),
`Manual Receipt of <name>`, `Updated Material '<name>' (SKU: …)`,
`Batch <b> (<sku> - <name>)`, or an exact `<name>`. No match ⇒ leave null
(conservative: a null row is excluded from rates, never mis-attributed).

### 8.2. Consumption rate  *(Phase 0 — implemented)*

```
daily_consumption(material, window_days=30, end=today) -> { date: Decimal }
    Σ quantity_changed grouped by calendar day, from
    material.registry_logs where action_type = 'Consumed_For_Manufacturing'
    and  end - window_days  <  timestamp::date  <=  end
    (days with no consumption are omitted)

consumption_rate(material, window_days=30, end=today) -> Decimal
    total = Σ daily_consumption(...).values()
    return 0            if total <= 0 or window_days <= 0
    return total / window_days
```

The denominator is the **whole window**, not the number of active days, so idle
days correctly drag the burn rate down. The result is the input to the Phase 2
stockout forecast (`on_hand / rate → days of cover`) that will replace the
current flat degradation threshold in `dashboard_view`
(`days_remaining <= 30 or < material.safe_storage_days`).

### 8.3. Supplier reliability scorecard  *(Phase 1 — implemented)*

`supplier_reliability(since=None, until=None)` → `views_analytics.supplier_scorecard_view`
→ `/catalog/suppliers/scorecard/` (nav: *Analytics → Supply Chain*, tab: Supplier Performance; Suppliers links to it via a header call-out).

Considers POs in status `Pending` / `Partially Received` / `Completed`, optionally
bounded by `order_date`. Grouped by the `supplier` FK, with free-text
`supplier_name`-only POs rolled up under their name.

```
fill_rate     = Σ quantity_received / Σ quantity_ordered      (line items; may exceed 1.0)
arrival(po)   = po.completed_date
                 ?? max linked Shipment.actual_arrival_date
                 ?? max manufacturing_date of batches received against the po
due(po)       = po.expected_delivery_date                          -> not estimated
                 ?? po.order_date + lead                           -> estimated
                    lead = max SupplierMaterial.lead_time_days over the po's
                           materials for its supplier,
                           else get_setting("po_default_lead_time_days")
assessable    = arrival(po) and due(po) both exist
on_time_rate  = count(arrival <= due) / count(assessable)
avg_delay_days= mean( (arrival - due).days )                       (signed, +ve = late)
estimated_share = count(assessable with estimated due) / count(assessable)
rating        = good   if on_time_rate >= .9 and fill_rate >= .98
                poor   if on_time_rate <  .7 or  fill_rate <  .9
                watch  otherwise      (missing on_time_rate counts as 1.0)
```

Rows are returned worst-first: `(rating rank, on_time_rate, fill_rate, name)`.
Rows whose score leans on an estimated due date are tagged `EST` in the UI and
their estimated share is shown, so committed-date and estimated rates are not
blended silently.

`po_default_lead_time_days` (default 14) is one `SystemSetting`. Edit it in-site at
**System Console → Operational Settings** (`/system/settings/`, superuser-only) —
a form generated from the registry; the raw Django admin (`/admin/core/systemsetting/`)
is a fallback. `core/settings_store.py` holds the registry of such tunables
(`REGISTRY`, keyed to `(default, type, description)`), `cast_value()`, and
`get_setting(key)` which returns the typed DB override or the default. A data
migration seeds one row per registry entry so they are all editable from day one.

### 8.4. Sales-order delivery risk  *(Phase 1 — implemented)*

`sales_order_delivery_risk()` → `views_analytics.sales_order_delivery_risk_view`
→ `/operations/sales-orders/risk/` (nav: *Analytics → Supply Chain*, tab: Delivery Performance; Sales Orders links to it via a header call-out).

```
arrival(so)   = max outbound Shipment.actual_arrival_date          -> is_actual
                 ?? max outbound Shipment.expected_eta_date        -> ETA
                 ?? None
shipped_frac  = Σ quantity_shipped / Σ quantity_ordered
risk          = no_deadline   if fulfillment_deadline is None
                on_track      if arrival and arrival <= deadline
                late          if arrival > deadline and (is_actual or shipped_frac >= 1)
                at_risk       if arrival > deadline otherwise
                late          if no shipment and deadline < today
                at_risk       if no shipment and 0 <= days_left <= so_at_risk_window_days
                              and status in (Pending, Awaiting Acknowledgement, In Production)
                on_track      otherwise
days_slack    = (deadline - (arrival or today)).days               (negative = behind)
```

Open = status in Pending / Awaiting Acknowledgement / In Production / Ready to
Ship / Partially Shipped / Shipped. Sorted `(risk rank, days_slack, so_number)`.
`so_at_risk_window_days` (default 7) is a `SystemSetting` (see below).

### 8.5. Stock-audit accuracy  *(Phase 1 — implemented)*

`audit_accuracy(since=None)` → `views_analytics.audit_accuracy_view` →
`/warehouse/stock-audit/accuracy/` (nav: *Analytics → Operations*, tab: Inventory Audit Accuracy; Stock Tally links to it via a header call-out).

`variance = actual_quantity − expected_quantity` per `StockAudit`. Rolled up
`by_warehouse` and `by_item` (`accuracy_rate` = zero-variance share, `shrinkage` =
Σ negative, `overage` = Σ positive, `net_variance`, `mean_abs_variance`,
`chronic_shrinkage` when net < 0 over ≥ 3 audits), plus a monthly `trend`
(count / mean |variance| / net). Sorted lowest accuracy, then largest mean |var|.

### 8.6. Production yield variance  *(Phase 1 — implemented)*

`production_yield_variance()` → `views_analytics.production_yield_view` →
`/operations/manufacture/yield/` (nav: *Analytics → Operations*, tab: Yield Performance; Manufacture links to it via a header call-out).

For `status='Completed'` runs with an `actual_yield`, grouped `by_product` and
`by_supervisor`:

```
yield_variance_pct   = mean( (actual_yield - expected_yield) / expected_yield * 100 )
material_overuse_pct = mean( RunMaterialUsage.variance_pct )   over the run's usages
rating               = good   if yield_variance_pct >= -2 and material_overuse_pct <= 5
                       poor   if yield_variance_pct <  -10 or material_overuse_pct >  15
                       watch  otherwise
```

Sorted `(rating rank, yield_variance_pct, -material_overuse_pct, name)`.

### 8.7. Stockout & reorder forecast  *(Phase 2a — implemented)*

`stockout_forecast(window_days=30)` → `views_analytics.forecast_view` →
`/warehouse/forecast/` (nav: *Analytics → Inventory*, tab: Stockout & Reorder; Materials Hub links to it via a header call-out).

```
burn/day    = Σ Consumed_For_Manufacturing qty over window / window_days   (consumption_rates())
available   = Σ active Batch.quantity - Σ Batch.allocated_quantity  (per material)
arrivals    = open PO lines (Pending / Partially Received): outstanding qty on the
              PO's expected_delivery_date ?? order_date + lead time (same rule as the
              supplier scorecard, _expected_delivery); overdue dates count as today
days_cover  = available / burn/day, then walk arrivals in date order: an arrival
              on or before the current run-out adds qty / burn/day; a later one
              doesn't (it can't prevent the stockout -> on_order_late)
              (None if burn/day == 0, or cover > 3650)
stockout    = today + days_cover
lead_time   = max SupplierMaterial.lead_time_days for the material
              ?? po_default_lead_time_days setting          (-> lead_time_estimated)
reorder_by  = stockout - lead_time
days_until_reorder = days_cover - lead_time
status      = critical     if days_cover <= 0 (out, nothing arriving today), or days_until_reorder < 0
              reorder_now  if days_until_reorder <= 2
              watch        if days_until_reorder <= 14
              ok           otherwise
              no_usage     if burn/day == 0
```

`on_order` is the open-PO outstanding total, split into `on_order_counted`
(lands before run-out, included in days_cover) and `on_order_late`; also
`days_cover_on_hand` (stock alone), `next_po_number/date`, `po_overdue`. Rows
sorted `(status rank, reorder_by, -burn/day, name)`. `consumption_rates()` is the
batched sibling of Phase 0's `consumption_rate()` — one grouped query for many
materials.

### 8.8. Capacity runway  *(Phase 2b — implemented)*

`capacity_forecast()` → `views_analytics.capacity_forecast_view` →
`/warehouse/capacity/` (nav: *Analytics → Inventory*, tab: Inventory Capacity; Facility Management
links to it via a header call-out).

One `WarehouseUtilizationSnapshot` per warehouse per day (idempotent on
`(warehouse, snapshot_date)`) holding the day's occupancy (`used_mt`, Active +
Quarantined via `analytics.used_mt_expr()`, and `quarantined_mt`) and rent
(`daily_rent_cost`, `billing_mode`, from `warehouse_rent_burn()`).

**No cron.** `core/signals.py` rewrites the affected warehouses' row for *today*
(Malaysia time, `timezone.localdate()`) after any commit that saves a `Batch`
(incl. moves between warehouses), a `Warehouse`, or a Material/Product unit
weight — the last write of the day is that day's figure; a day with no row means
nothing changed. Bulk `QuerySet.update()` skips signals, so the Capacity and Rent
Opportunities pages call `ensure_today_snapshots()` to fill any missing row for
today. `manage.py snapshot_utilization` remains for manual use. (The old Railway
cron service for this command can be removed.)

Rows written before daily rent was recorded (and rows the seed/simulation
scripts write directly) have their rent estimated as that day's tonnage × the
warehouse's current rate — stored by migration 0058 with `rent_estimated=True`,
or computed on read by `estimate_snapshot_rent()`. `rent_history(days)` turns the
rows into per-warehouse daily series (carrying values forward over days with no
row) for the Rent History chart on Rent Opportunities.

```
slope, _  = least-squares fit of utilization_percent over the snapshot dates
weekly_rate_pp = slope × 7
days_to_full   = (100 − current) / slope           (slope in pp/day)
projected_full = latest_snapshot_date + days_to_full
status = no_data   if < 3 snapshots
         stable    if slope <= 0.02 pp/day (and current < 95)
         critical  if current >= 95%, or days_to_full <= 14
         watch     if days_to_full <= 60
         ok         otherwise
```

Sorted `(status rank, days_to_full, -current, name)`. The UI shows a "collecting
data" banner until a warehouse has 7 snapshots.

### 8.9. AI Copilot — category briefings  *(Phase 3 — implemented; split into categories in Phase 4)*

`core/briefing.py` → `generate_ops_briefing` command / `category_briefing_view` →
`/assistant/briefing/<category>/` for `category` in `materials`, `products`,
`sales`, `purchase`, `logistics`, `warehouse` (nav: *Overview → AI Copilot*,
tab strip across the top of the page). Phase 3 shipped one company-wide
briefing mixing all six domains; Phase 4 split it so each domain reads on its
own, and added the previously-missing Logistics signal (§8.10).

Two steps per category, **one stateless LLM call**, no agent loop and no tools:

1. **`collect_signals(category, window_days=180)`** looks up
   `CATEGORY_SIGNAL_BUILDERS[category]` and calls only that category's
   functions, keeping the rows worth a manager's attention:
   materials → `stockout_forecast` (`critical|reorder_now|watch`); products →
   `production_yield_variance` (`poor|watch`); sales →
   `sales_order_delivery_risk` (`late|at_risk`); purchase →
   `supplier_reliability` (`poor|watch`); logistics → `shipment_logistics`
   (`discrepant|overdue|at_risk|stalled`); warehouse → `capacity_forecast`
   (`critical|watch`) **and** `audit_accuracy` (chronic shrinkage or accuracy
   < 0.8, + worst items). Each section is capped at 12 rows and reduced to the
   few fields the narration needs (name + figures, dates as ISO strings). The
   result is one JSON-serialisable dict with a `signal_count`.

2. **`generate_briefing(category, period, user)`** renders that dict as text
   and makes a single `anthropic` `client.messages.create()` call with a
   category-specific system prompt (`_system_prompt(category)`, built from
   `_BRIEFING_PROMPT_TEMPLATE` + `CATEGORY_INTROS[category]`) — same discipline
   as before: "use ONLY the figures in the payload; never invent a
   number/name/date". The model gets **no database access and no tools** —
   only the pre-computed dict for its one category — so it can narrate the
   numbers but cannot fabricate them. The reply (headline + 3–6 bullet lines +
   optional `Watch:` line) is stored verbatim.

**Outlook (estimate) — the one deliberate exception.** For Materials and
Warehouse, the payload also carries real history, not just a current-state
number: `burn_trend_weekly` (six weekly average burn figures from
`analytics.daily_consumption()`, via `_weekly_burn_trend()`) and
`utilization_trend` (the last 8 `WarehouseUtilizationSnapshot` points). Only
when a trend field is present may the model append one final section,
`Outlook (estimate):` — 1–3 hedged lines ("likely", "could", "may"), each
naming what it's based on, never a new invented number or date. This is the
only place in the whole briefing system where the model is allowed to say
something not already a fact in the payload, and the prompt, the parsing
(`_parse_briefing_body` → `(points, watch, outlook)`), and the template all
keep it visibly separate (a dashed purple box, "estimate, not a fact"). No
trend field exists yet for Sales/Purchase/Products/Logistics, so the same
prompt naturally omits Outlook there — adding it to another category is
purely a matter of computing and attaching that category's own trend field;
no prompt or template change is needed.

**`OpsBriefing`** row per run: `category` (see §8.11 for `my_checklist`),
`period`, `status` (`ok|empty|skipped|error`), `headline`, `body_text`,
`signals_json`, `signal_count`, `model_id`, `input_tokens`/`output_tokens`
(cost trace), `error_detail`, `generated_by` (null for the scheduled job).
Each category page renders the latest `ok` row for its own `category`; a newer
failed run shows a "last good briefing" notice. Pre-Phase-4 rows carry
`category='legacy'` and are invisible to every page.

Config in `settings_store.REGISTRY` (editable at `/system/settings/`):
`ops_briefing_enabled` (bool; off → `skipped` run, no API call, shared by every
category and the checklist) and `ops_briefing_model` (default `claude-opus-5`;
`claude-sonnet-5`/`claude-haiku-4-5` are cheaper for this job). The API key is
the **`ANTHROPIC_API_KEY` environment variable** — a secret, deliberately not a
`SystemSetting`.

`generate_briefing()` never raises for a config/API/network problem — it
records the failure on the row so the scheduled job stays green. **Like
`snapshot_utilization`, this command has no scheduler wired** — run it daily
(`python manage.py generate_ops_briefing --category all`, or `--period weekly`,
or one category at a time) via Heroku Scheduler, cron, or a scheduled GitHub
Action (on Railway: a second Cron Schedule service, same pattern as
`snapshot_utilization` in §8.8 — remember to give it the `ANTHROPIC_API_KEY`
env var too). Admin/Manager can also trigger a run for one category from its
page. `--dry-run` prints the signal payload(s) without calling the API.

### 8.10. Shipment logistics  *(Phase 4 — implemented)*

`analytics.shipment_logistics()` → `views_analytics.shipment_logistics_view` →
`/operations/shipments/logistics/` (nav: *Analytics → Supply Chain*, tab: Logistics
Performance). Also feeds the Logistics category briefing (§8.9).

Before this, `Shipment` was only read indirectly — the outbound leg inside
`sales_order_delivery_risk()`, the inbound leg inside `supplier_reliability()`.
Neither sees a `Transfer` shipment (warehouse-to-warehouse, no SO or PO at
all). This function looks at every in-flight shipment regardless of direction:

```
scope = Shipment.status not in (Completed, Cancelled)
risk = discrepant  if has_discrepancy or status == 'Discrepant'
       overdue     if status == 'Delayed', or (Dispatched/Arrived and
                    expected_eta_date already past with no actual_arrival_date)
       at_risk     if status == 'Dispatched', not arrived, and
                    expected_eta_date within logistics_at_risk_window_days
       stalled     if status == 'Arrived' for >= logistics_stall_days without
                    being marked Completed
       pending     if pre-dispatch (Draft/Logistics Review/Pending
                    Approval/Preparing) with no dispatch_date yet
       on_track    otherwise
```

Sorted `(risk rank, expected_eta_date or dispatch_date, tracking_number)` —
worst first. `logistics_at_risk_window_days` (default 3) and
`logistics_stall_days` (default 5) are `SystemSetting`s, editable at
`/system/settings/`.

### 8.11. Personal checklist — "My Open Jobs"  *(Phase 4 — implemented)*

`analytics.my_open_jobs(user)` + `core/briefing.py::generate_my_checklist()` →
`ops_briefing_view` → `/assistant/briefing/` (nav: *Overview → AI Copilot*,
the checklist tab). This is the target of the formerly inert "Digital
Assistant (AI)" link, now renamed **AI Copilot**.

Not a company-wide roll-up — one person's own records, still open or in
process, across every domain:

- `SalesOrder` / `PurchaseOrder` / `ProductionRun` — `created_by=user`, closed
  statuses excluded (`Delivered`/`Rejected`, `Completed`/`Rejected`,
  `Completed`/`Cancelled` respectively — each model's real ownership field).
- `StockAudit` — `auditor=user`, `status='Pending'` (still unresolved).
- `Shipment` — has **no creator field at all**, so `last_edited_by=user` is
  used as an honest, clearly-labelled proxy (`ownership='touched'`, rendered
  "(last touched by you)" rather than implying authorship).

Each row optionally carries a `context` dict — a small, already-computed
cross-reference into the same Tier 1 functions above (a Sales Order's own
`sales_order_delivery_risk()` row when it's `late`/`at_risk`; a Purchase
Order's supplier's own `supplier_reliability()` row when `poor`/`watch`) — so
the checklist can point out that an open item is *also* flagged elsewhere,
without computing anything new.

Two layers, matching the "grounded, then narrated" pattern used everywhere
else in this feature:

1. **The live table** — always rendered straight from `my_open_jobs(user)`,
   no API dependency, no cost. This is the accountable source of truth.
2. **`generate_my_checklist(user)`** — one Claude call over that same list
   (via `_checklist_signals(user)`), with `CHECKLIST_SYSTEM_PROMPT` telling the
   model it may re-order, group, and suggest a next action per item, using
   `context` when present, but must refer to every item only by the real
   identifier already in the payload and must never invent one. Stored as
   `OpsBriefing(category='my_checklist', generated_by=user)` — always
   attributed to a specific person, unlike the category rows.

Generation is **on-demand only**, by the person it's for (any authenticated
user, not just Admin/Manager — it is only ever their own data and their own
click) — never scheduled, since a per-user daily job would multiply the API
cost by the number of active users. `generate_ops_briefing` explicitly never
produces `my_checklist` rows.

### 8.12. Product sales trend  *(Phase 5 — implemented)*

`analytics.product_sales_trend()` → `views_analytics.product_sales_trend_view`
→ `/catalog/products/sales-trend/` (nav: *Analytics → Demand*; the category's
only report, so no tab strip).
Also feeds the Sales category briefing (§8.9) and is the trend field that
unlocks its Outlook section.

The only genuinely time-series view in Sales — `sales_order_delivery_risk()`
is a point-in-time snapshot of currently-open orders; this looks at ordered
quantity across **all** real orders (Draft/Rejected excluded) over a trailing
6-month window, bucketed by month, split into two halves:

```
earlier_avg = mean of the first half of the window's monthly quantities
recent_avg  = mean of the second half
pct_change  = (recent_avg − earlier_avg) / earlier_avg × 100
status = insufficient_data  if sales activity in fewer than 3 of the 6 months
         new                if earlier_avg == 0 and recent_avg > 0
         declining          if pct_change <= −15%
         rising             if pct_change >= +15%
         flat               otherwise
```

Sorted declining-first (then rising, by largest swing within each group).
Revenue (`monthly_revenue`/`total_revenue`) is computed alongside quantity but
only populated for a product when every line in every active month has a
`unit_price` on file — otherwise it's `None` rather than a silently-wrong
partial total.

`_product_trend_signals()` in `core/briefing.py` feeds the `declining`/`rising`
rows (with their full `monthly_qty_trend` series) into the Sales briefing —
the same real history a manager would want to know is behind the Outlook
(estimate) section described in §8.9.
