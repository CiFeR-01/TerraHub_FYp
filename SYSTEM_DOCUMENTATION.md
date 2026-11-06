# TerraHub - System Documentation

TerraHub is a Django-based manufacturing and warehouse operations platform. It tracks materials and finished-goods inventory across warehouses, drives production runs from bills of materials (recipes), manages purchase and sales orders, generates shipments with FEFO-based batch allocation, and provides QA and approvals workflows on top of full batch/lot traceability.

---

## 1. System Overview

Core capabilities:
- **Custom User Model** with per-location access restrictions. Roles are Django **Groups** (Django admin > Groups); code checks permissions only - per-model ones plus the app-wide ones on `Capability` - via `core/permissions.py`, so roles are added or reshaped without code changes.
- **Catalog Management**: materials, products, suppliers and clients, with CSV import/export and per-product recipes (bills of materials).
- **Warehouse & Inventory**: multi-warehouse, multi-location inventory with full batch/lot tracking, stock audits, and a public batch lookup/print-label flow.
- **Manufacturing**: production runs consuming materials per recipe, with material allocation, yield tracking and printable mixing / assembly sheets.
- **Order Management**: purchase orders (inbound) and sales orders (outbound), each with line-item detail, CSV import/export and an approvals inbox.
- **Shipments**: transfer and outbound shipments with pick lists, generated from sales-order allocation using FEFO (first-expired-first-out) batch selection.
- **Stock Allocation**: a shared allocation engine (`StockAllocation`) used by both sales orders and production runs to reserve batch quantities.
- **QA & Spoilage Control**: holds, inspections, releases, write-offs, supplier returns and shelf-life extensions, with approval limits and a daily expiry sweep (§8.1).
- **Warehouse rental**: rent billed per day or per billing term (e.g. RM/MT per 30 days) for each rented facility, plus a rent-reduction decision-support page (§7.13).
- **Registry Ledger** and **Audit Log** for traceability and history (`RegistryLog`, `OrderTimeline`, `QAEvent`, `UserAuditLog`).

---

## 2. Architecture & Technology Stack

- **Backend Framework**: Django 6.0.8
- **Language**: Python 3.14
- **Database**: PostgreSQL (`psycopg2-binary`). Local connection details come from `.env`; on Railway the injected `DATABASE_URL` overrides them via `dj-database-url`. SQLite is possible by setting `DB_ENGINE=django.db.backends.sqlite3`
- **AI**: the `anthropic` SDK, used only by the AI Copilot briefings (§7.9)
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
├── README.md                   # Project overview and quick start
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
│   ├── views_analytics.py      # Analytics, decision-support and AI Copilot views (see §7)
│   ├── views_qa.py             # QA dashboard and "Send to QA" views (see §8.1)
│   ├── views_roles.py          # Roles page: create roles and tick their permissions
│   ├── analytics.py            # Analytics & forecasting computation layer (see §7)
│   ├── analytics_cache.py      # Short-lived cache for heavy analytics, cleared on data changes
│   ├── briefing.py             # AI Copilot signal collection and Claude calls (see §7.9)
│   ├── qa.py                   # QA actions: holds, inspections, write-offs, extensions (see §8.1)
│   ├── settings_store.py       # Typed access to admin-editable operational settings
│   ├── signals.py              # Keeps capacity snapshots and the analytics cache current
│   ├── account.py              # Temporary passwords and the forced password change
│   ├── login_throttle.py       # Sign-in lockout after repeated failures (see §8.5)
│   ├── errors.py               # Custom 400/403/404/500 pages (see §8.5)
│   ├── validators.py           # Phone and email validators shared by forms
│   ├── management/commands/    # snapshot_utilization, generate_ops_briefing, qa_sweep (see §6.6)
│   ├── urls.py                 # Application URL routing (see §4)
│   ├── utils.py                # Allocation engine (FEFO), stock helpers
│   ├── decorators.py           # permission_or_redirect view decorator
│   ├── permissions.py          # Central permission helpers (approvers, admin override, ...)
│   ├── context_processors.py   # Template context (nav, notifications, etc.)
│   ├── admin.py                # Django admin registrations
│   ├── templatetags/           # Quantity formatting and QA template helpers
│   ├── tests.py                # Test suite
│   └── tests_critical.py       # Tests for the critical stock, order and QA paths
├── static/
│   ├── css/style.css
│   ├── js/                        # Session timeout, contact validation and other page scripts
│   └── img/landing-dashboard.png  # Static Operations Overview screenshot used on the landing page
└── templates/                  # Server-rendered HTML templates
    ├── base.html                    # Shared layout/theme
    ├── home.html / login.html / error.html   # Public entry points and error pages
    ├── dashboard.html / profile.html / password_change.html
    ├── system_settings.html / user_management.html / user_audit_log.html / roles.html
    ├── product_list.html / product_detail.html
    ├── material_list.html / material_detail.html / material_form.html
    ├── supplier_list.html / supplier_detail.html / client_list.html / client_detail.html
    ├── warehouse_list.html / warehouse_form.html / warehouse_inventory.html
    ├── batch_detail.html / batch_public_info.html / batch_print_label.html
    ├── stock_audit.html / registry_ledger.html
    ├── po_list.html / po_detail.html
    ├── so_list.html / so_detail.html / so_allocate.html
    ├── shipments.html / shipment_detail.html / shipment_pick_list.html
    ├── manufacturing.html / production_run_detail.html / production_allocate.html
    ├── production_run_mixing_sheet.html / production_run_assembly_sheet.html
    ├── qa_dashboard.html / approvals_inbox.html
    ├── analytics/                   # Analytics, decision-support and AI Copilot pages (see §7)
    └── partials/                    # Shared pieces: recipe studio, order import, QA modals, pagination, sorting
```

---

## 4. Routing Table

| Path | Name | Controller View | Description |
| :--- | :--- | :--- | :--- |
| `/` | `home` | `home_view` | Public landing page (hero, the three project objectives, traceability flow, decision support). `?batch=<number>` is the "Trace a batch" box: redirects to that batch's public page, or shows a not-found note. Signed-in users are redirected to `/dashboard/` |
| `/login/` | `login` | `LoginView` | Authentication |
| `/logout/` | `logout` | `LogoutView` | Clears session, redirects home |
| `/session/keepalive/` | `session_keepalive` | `session_keepalive_view` | Keepalive ping from the idle-timeout script; returns 401 once the session has expired |
| `/dashboard/` | `dashboard` | `dashboard_view` | Main authenticated dashboard |
| `/profile/` | `profile` | `profile_view` | User profile |
| `/profile/password/` | `password_change` | `password_change_view` | Change own password (forced after an admin reset) |
| `/system/settings/` | `system_settings` | `system_settings_view` | Operational settings editor (superuser; see §7) |
| `/system/users/` | `user_management` | `user_management_view` | User administration and temporary password resets |
| `/system/users/audit/` | `user_audit_log` | `user_audit_log_view` | Audit Log of account, role and settings changes |
| `/system/roles/` | `roles` | `views_roles.roles_view` | Create, rename and delete roles and tick their permissions |
| `/warehouse/inventory/` | `warehouse_inventory` | `warehouse_inventory_view` | Inventory across warehouses |
| `/warehouse/batch/<batch_number>/` | `batch_detail` | `batch_detail_view` | Batch/lot detail & traceability |
| `/warehouse/batch/<batch_number>/print/` | `batch_print_label` | `batch_print_label_view` | Printable batch label |
| `/public/batch/<batch_number>/` | `batch_public_info` | `batch_public_info_view` | Public batch lookup |
| `/warehouse/facilities/` | `warehouse_list` | `facility_management_view` | Warehouse facility list |
| `/warehouse/facilities/new/` | `warehouse_create` | `warehouse_create_view` | Create warehouse |
| `/warehouse/facilities/<pk>/edit/` | `warehouse_edit` | `warehouse_edit_view` | Edit warehouse |
| `/warehouse/stock-audit/` | `stock_audit` | `stock_audit_view` | Stock audit workflow |
| `/warehouse/registry/` | `registry` | `registry_ledger_view` | Registry/audit ledger |
| `/catalog/products/` | `product_list` | `product_list_view` | Product catalog |
| `/catalog/products/<pk>/` | `product_detail` | `product_detail_view` | Product detail — stock KPIs, recipe cost & margin, production-vs-sales chart (sales series = `analytics.product_sales_trend`, §7.12) |
| `/catalog/products/export/` | `export_products_csv` | `export_products_csv` | Export products (CSV) |
| `/catalog/products/template/` | `export_product_template` | `export_product_template` | Product import template |
| `/catalog/products/import/` | `import_products` | `import_products` | Bulk import products |
| `/analytics/sales-trends/` | `product_sales_trend` | `views_analytics.product_sales_trend_view` | Monthly sales-volume trend per product — "Analytics → Demand" (see §7.12) |
| `/catalog/products/<id>/recipe/get/` | `get_product_recipe_api` | `get_product_recipe_api` | Fetch product recipe (JSON) |
| `/catalog/products/recipe/save/` | `save_product_recipe_api` | `save_product_recipe_api` | Save product recipe (JSON) |
| `/catalog/materials/` | `material_list` | `material_list_view` | Material catalog |
| `/catalog/materials/<pk>/` | `material_detail` | `material_detail_view` | Material detail — editable info, batches, stock by warehouse, cover/reorder forecast (`analytics.stockout_forecast`, §7.7), consumption chart (RegistryLog `Consumed_For_Manufacturing`, §7.1–8.2), recipes, suppliers, POs, movements, unrecorded run usage. Edit/archive need `core.change_material` |
| `/catalog/materials/<pk>/edit/` | `material_edit` | `material_edit_view` | Edit material |
| `/catalog/materials/export/` | `export_materials_csv` | `export_materials_csv` | Export materials (CSV) |
| `/catalog/materials/template/` | `export_material_template` | `export_material_template` | Material import template |
| `/catalog/materials/import/` | `import_materials` | `import_materials` | Bulk import materials |
| `/catalog/suppliers/` | `supplier_list` | `supplier_list_view` | Supplier list |
| `/catalog/suppliers/<pk>/` | `supplier_detail` | `supplier_detail_view` | Supplier detail, supplied materials and lead times |
| `/catalog/suppliers/<pk>/edit/` | `supplier_edit` | `supplier_edit_view` | Edit supplier |
| `/catalog/clients/` | `client_list` | `client_list_view` | Client list |
| `/catalog/clients/<pk>/` | `client_detail` | `client_detail_view` | Client detail and orders |
| `/catalog/clients/<pk>/edit/` | `client_edit` | `client_edit_view` | Edit client |
| `/catalog/recipes/export/` | `export_recipes_csv` | `export_product_recipes_csv` | Export product recipes (CSV) |
| `/operations/sales-orders/` | `so_list` | `sales_order_list_view` | Sales order list |
| `/operations/sales-orders/export/` | `export_so_csv` | `export_orders_csv` | Export sales orders (CSV, one row per line item) |
| `/operations/sales-orders/template/` | `export_so_template` | `export_order_template` | Sales order import template |
| `/operations/sales-orders/import/` | `import_so` | `import_orders` | Bulk import sales orders (dry run first; see §8.3) |
| `/operations/sales-orders/<pk>/` | `so_detail` | `so_detail_view` | Sales order detail |
| `/operations/sales-orders/<pk>/allocate/` | `so_allocate` | `so_allocate_view` | FEFO batch allocation for SO |
| `/operations/sales-orders/<pk>/create_shipment/` | `so_create_shipment` | `so_create_shipment_view` | Create shipment from SO |
| `/operations/purchase-orders/` | `po_list` | `purchase_order_list_view` | Purchase order list |
| `/operations/purchase-orders/export/` | `export_po_csv` | `export_orders_csv` | Export purchase orders (CSV, one row per line item) |
| `/operations/purchase-orders/template/` | `export_po_template` | `export_order_template` | Purchase order import template |
| `/operations/purchase-orders/import/` | `import_po` | `import_orders` | Bulk import purchase orders (dry run first; see §8.3) |
| `/operations/purchase-orders/<pk>/` | `po_detail` | `po_detail_view` | Purchase order detail |
| `/operations/shipments/` | `shipments` | `shipments_view` | Shipment list |
| `/operations/shipments/<pk>/` | `shipment_detail` | `shipment_detail_view` | Shipment detail |
| `/operations/shipments/<pk>/picklist/` | `shipment_pick_list` | `shipment_pick_list_view` | Shipment pick list |
| `/operations/manufacture/` | `readiness` | `manufacturing_view` | Manufacturing readiness dashboard |
| `/operations/manufacture/run/<pk>/` | `production_run_detail` | `production_run_detail_view` | Production run detail |
| `/operations/manufacture/run/<pk>/mixing-sheet/` | `production_run_mixing_sheet` | `production_run_mixing_sheet_view` | Printable mixing sheet for the shop floor (§8.4) |
| `/operations/manufacture/run/<pk>/assembly-sheet/` | `production_run_assembly_sheet` | `production_run_assembly_sheet_view` | Printable assembly record of a completed run (§8.4) |
| `/operations/manufacture/run/<pk>/allocate/` | `production_run_allocate` | `production_run_allocate_view` | Material allocation for a run |
| `/operations/qa/` | `qa_dashboard` | `views_qa.qa_dashboard_view` | QA dashboard: holds, requests, inspections, approvals (§8.1) |
| `/operations/qa/request/` | `qa_request_create` | `views_qa.qa_request_create` | "Send to QA" from any page |
| `/analytics/spoilage/` | `spoilage_report` | `views_analytics.spoilage_report_view` | Spoilage & write-offs report (§7.14) |
| `/approvals/` | `approvals_inbox` | `approvals_inbox_view` | Pending approvals inbox |
| `/operations/notifications/read/` | `mark_notifications_read` | `mark_notifications_read` | Mark notifications read |
| `/analytics/supplier-performance/` | `supplier_scorecard` | `views_analytics.supplier_scorecard_view` | Supplier reliability scorecard — "Scorecard" tab of the Suppliers hub (see §7) |
| `/decision-support/delivery/` | `so_delivery_risk` | `views_analytics.sales_order_delivery_risk_view` | Sales-order delivery-risk board — "Delivery Risk" tab of the Sales Orders hub (see §7) |
| `/analytics/audit-accuracy/` | `audit_accuracy` | `views_analytics.audit_accuracy_view` | Stock-audit accuracy roll-up — "Accuracy" tab of the Stock Audit hub (see §7) |
| `/analytics/yield/` | `production_yield` | `views_analytics.production_yield_view` | Production yield variance — "Yield" tab of the Manufacturing hub (see §7) |
| `/decision-support/stockout/` | `forecast` | `views_analytics.forecast_view` | Consumption-rate stockout & reorder forecast (see §7) |
| `/decision-support/capacity/` | `capacity_forecast` | `views_analytics.capacity_forecast_view` | Warehouse capacity runway — daily-snapshot trend (see §7) |
| `/decision-support/rent-reduction/` | `rent_opportunities` | `views_analytics.rent_opportunities_view` | Rent Opportunities — suggested moves out of rented storage (§7.13) |
| `/decision-support/rent-reduction/decide/` | `rent_suggestion_decide` | `views_analytics.rent_suggestion_decide` | Accept (creates Draft transfers) or dismiss ticked suggestions |
| `/assistant/briefing/` | `ops_briefing` | `views_analytics.ops_briefing_view` | My Open Jobs — the personal checklist; *Overview → AI Copilot* (see §7.11) |
| `/assistant/briefing/hub/` | `briefing_hub` | `views_analytics.briefing_hub_view` | AI Copilot landing page: My Open Jobs plus the six briefing cards |
| `/assistant/briefing/tick/<pk>/` | `briefing_tick` | `views_analytics.briefing_tick` | Tick or untick one checklist bullet (JSON) |
| `/assistant/briefing/<category>/` | `category_briefing` | `views_analytics.category_briefing_view` | One of the six domain briefings (materials/products/sales/purchase/logistics/warehouse) — Claude narration of that category's Tier 1/2 signals (see §7.9) |
| `/analytics/logistics-performance/` | `shipment_logistics` | `views_analytics.shipment_logistics_view` | In-flight shipment risk roll-up — "Analytics → Supply Chain" (see §7.10) |
| `/admin/` | — | Django admin | Django admin site |

---

## 5. Domain Model

Defined in `core/models.py`:

- **Access**: `CustomUser` (+ Django `Group`), `Capability` (table-less holder of app-wide permissions)
- **Facilities**: `Warehouse`, `WarehouseLocation`
- **Catalog**: `Material`, `Product`, `ProductRecipe`, `Supplier`, `SupplierMaterial` (lead time per supplier and material), `Client`
- **Manufacturing**: `ProductionRun`, `ProductionRunYieldLog`, `RunMaterialUsage`, `RunExtraMaterial`, `ProductionConsumption`
- **Inventory**: `Batch` (lot-level tracking with expiry/manufacturing dates, quantity, allocated quantity, and an optional locked `rental_rate_per_mt`; NULL follows the warehouse rate live)
- **Purchasing**: `PurchaseOrder`, `PurchaseOrderDetail`
- **Sales**: `SalesOrder`, `SalesOrderDetail`
- **Fulfillment**: `Shipment` (tracking numbers `SHP-1001` onwards), `ShipmentItem`, `ShipmentItemReceipt` (one dated receiving event, so a load can be received in parts)
- **Quality**: `QARequest`, `QAInspection`, `QAApproval`, `QAEvent` (see §8.1)
- **Audit**: `StockAudit`, `UserAuditLog`, `RegistryLog` (movement ledger; `RegistryLog.material` FK is the machine-readable key behind the analytics engine — see §7), `OrderTimeline`
- **Allocation**: `StockAllocation` (shared reservation engine used by sales orders and production runs, resolved via FEFO in `core/utils.py`)
- **Messaging**: `Notification`
- **Configuration**: `SystemSetting` (key/value store for admin-editable operational tunables; read via `core/settings_store.py` — see §7)
- **Analytics snapshots**: `WarehouseUtilizationSnapshot` (one row per warehouse per day, kept current by `core/signals.py`; see §7.8)
- **Decision support**: `RentSuggestion` (an accepted or dismissed Rent Opportunities suggestion; see §7.13)
- **AI Copilot**: `BriefingTick` (one user's tick on one checklist bullet), `OpsBriefing` (one row per category-briefing run or per-user checklist run — stored Claude output plus its input `signals_json` and token counts, keyed by `category`; see §7.9/§7.11)

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
Copy `.env.example` to `.env` and fill in the PostgreSQL connection (`DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`) and `ANTHROPIC_API_KEY` (only needed for the AI Copilot).

### 6.3. Database Migrations
```powershell
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

### 6.6. Scheduled jobs
- `python manage.py qa_sweep`: holds expired batches and sends QA expiry and long-hold alerts. Run it daily; it is safe to run repeatedly.
- `python manage.py generate_ops_briefing --category all`: the AI Copilot briefings (§7.9).
- `python manage.py snapshot_utilization`: a manual capacity snapshot, normally not needed (§7.8).

### 6.7. Tests
```powershell
python manage.py test
```
`core/tests.py` covers the modules broadly; `core/tests_critical.py` covers the stock, order and QA paths where a bug would lose or misstate stock.

---

## 7. Analytics & Forecasting Engine

A layered analytics capability built on data the platform already records. All
computation lives in **`core/analytics.py`** as pure functions (no request /
response, no side effects) so it can be called identically from web views,
management commands, the shell, and tests. Scheduled jobs live in
`core/management/commands/`. Heavy results are cached for 60 seconds by
`core/analytics_cache.py`, and `core/signals.py` clears that cache whenever the
underlying records change.

Three tiers:

| Tier | Nature | Examples |
| :--- | :--- | :--- |
| **1** | Roll-ups of stored data (ORM aggregation, no new math) | supplier reliability, sales-order delivery risk, audit-accuracy trend, production yield variance |
| **2** | Statistical forecasting by plain arithmetic (no ML library) | consumption-rate stockout ETA, reorder-by date, warehouse capacity runway, rent-reduction suggestions |
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

### 7.1. The RegistryLog material spine

`RegistryLog` is the append-only ledger of every physical stock movement
(`Inbound`, `Outbound`, `Consumed_For_Manufacturing`, `Produced`, `Adjusted`,
`Spoiled_Disposal`, `QA_Extension`). Each row carries a free-text `item_name`
(e.g. `"MAP (Run RUN-2010)"`) and a machine-readable key,
`RegistryLog.material` (nullable FK → `Material`, `related_name='registry_logs'`). It is stamped at write time wherever a specific raw
material moves; it is left **null** for movements that are not material-specific
(finished-goods `Produced`, bulk-import summaries, outbound shipments of finished
products). Migration `0035` adds the column (guarded with `ADD COLUMN IF NOT
EXISTS`, so it is safe on a database that already has the column); migration `0036` backfills history.

**Backfill algorithm** (`resolve_material_from_label(item_name, by_name)`):
build `{material.name.lower(): Material}`, then for each null-material ledger row
whose action is stock-affecting, match `item_name` against these shapes and take
the first hit — `<name> (Run …)`, `<name> (Batch …)` (± ` - force closed`),
`Manual Receipt of <name>`, `Updated Material '<name>' (SKU: …)`,
`Batch <b> (<sku> - <name>)`, or an exact `<name>`. No match ⇒ leave null
(conservative: a null row is excluded from rates, never mis-attributed).

### 7.2. Consumption rate

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
days correctly drag the burn rate down. The result is the input to the
stockout forecast (`on_hand / rate → days of cover`, §7.7).

### 7.3. Supplier reliability scorecard

`supplier_reliability(since=None, until=None)` → `views_analytics.supplier_scorecard_view`
→ `/analytics/supplier-performance/` (nav: *Analytics → Supply Chain*, tab: Supplier Performance; Suppliers links to it via a header call-out).

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
**Admin → System Settings** (`/system/settings/`, superuser-only) —
a form generated from the registry; the raw Django admin (`/admin/core/systemsetting/`)
is a fallback. `core/settings_store.py` holds the registry of such tunables
(`REGISTRY`, keyed to `(default, type, description)`), `cast_value()`, and
`get_setting(key)` which returns the typed DB override or the default. A data
migration seeds one row per registry entry so they are all editable from day one.

### 7.4. Sales-order delivery risk

`sales_order_delivery_risk()` → `views_analytics.sales_order_delivery_risk_view`
→ `/decision-support/delivery/` (nav: *Analytics → Supply Chain*, tab: Delivery Performance; Sales Orders links to it via a header call-out).

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

### 7.5. Stock-audit accuracy

`audit_accuracy(since=None)` → `views_analytics.audit_accuracy_view` →
`/analytics/audit-accuracy/` (nav: *Analytics → Operations*, tab: Inventory Audit Accuracy; Stock Tally links to it via a header call-out).

`variance = actual_quantity − expected_quantity` per `StockAudit`. Rolled up
`by_warehouse` and `by_item` (`accuracy_rate` = zero-variance share, `shrinkage` =
Σ negative, `overage` = Σ positive, `net_variance`, `mean_abs_variance`,
`chronic_shrinkage` when net < 0 over ≥ 3 audits), plus a monthly `trend`
(count / mean |variance| / net). Sorted lowest accuracy, then largest mean |var|.

### 7.6. Production yield variance

`production_yield_variance()` → `views_analytics.production_yield_view` →
`/analytics/yield/` (nav: *Analytics → Operations*, tab: Yield Performance; Manufacture links to it via a header call-out).

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

**Unrecorded material usage** — `unrecorded_material_usage(recent_days=7, window_days=28)`, shown as a
section and a KPI tile on the same page. When a run is completed with more of a material poured than was
allocated, the shortfall is traced to a batch or entered as *batch not in records* with a mandatory reason
(`RunExtraMaterial`, `batch` NULL). Nothing is deducted from stock for the latter. Per material:

```
events / qty   = RunExtraMaterial rows with batch NULL, created within recent_days / window_days
share_pct      = qty (window) / Σ RunMaterialUsage.actual_qty of runs Completed within the window * 100  (cap 100)
top_reasons    = the 3 most common reasons (case / spacing merged);  top_people = the 2 people entering most
```

Sorted highest share first (unknown share last), then window quantity, then name.

### 7.7. Stockout & reorder forecast

`stockout_forecast(window_days=30)` → `views_analytics.forecast_view` →
`/decision-support/stockout/` (nav: *Analytics → Inventory*, tab: Stockout & Reorder; Materials Hub links to it via a header call-out).

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
              reorder_now  if days_until_reorder <= stockout_reorder_now_days (2)
              watch        if days_until_reorder <= stockout_watch_days (14)
              ok           otherwise
              no_usage     if burn/day == 0
```

`on_order` is the open-PO outstanding total, split into `on_order_counted`
(lands before run-out, included in days_cover) and `on_order_late`; also
`days_cover_on_hand` (stock alone), `next_po_number/date`, `po_overdue`. Rows
sorted `(status rank, reorder_by, -burn/day, name)`. `consumption_rates()` is the
batched sibling of `consumption_rate()` — one grouped query for many
materials.

### 7.8. Capacity runway

`capacity_forecast()` → `views_analytics.capacity_forecast_view` →
`/decision-support/capacity/` (nav: *Analytics → Inventory*, tab: Inventory Capacity; Facility Management
links to it via a header call-out).

One `WarehouseUtilizationSnapshot` per warehouse per day (idempotent on
`(warehouse, snapshot_date)`) holding the day's occupancy (`used_mt`, Active +
Quarantined via `analytics.used_mt_expr()`, and `quarantined_mt`) and rent
(`daily_rent_cost`, `billing_mode`, from `warehouse_rent_burn()`).

**No cron.** `core/signals.py` rewrites the affected warehouses' row for *today*
(Malaysia time, `timezone.localdate()`) after any commit that saves a `Batch`
(incl. moves between warehouses), a `Warehouse`, or a Material/Product unit
weight — the last write of the day is that day's figure; a day with no row means
nothing changed. The Capacity page also compares live occupancy with the last snapshot and
offers "Run snapshot now" when they differ; page views never write. `manage.py snapshot_utilization`
takes a snapshot on demand.

Rows written before daily rent was recorded (and rows the seed/simulation
scripts write directly) have their rent estimated as that day's tonnage × the
warehouse's current rate — stored by migration 0058 with `rent_estimated=True`,
or computed on read by `estimate_snapshot_rent()`. `rent_history(days)` turns the
rows into per-warehouse daily series (carrying values forward over days with no
row) for the Rent History chart on Rent Opportunities.

```
slope, _  = least-squares fit of utilization_percent over one point per calendar day over the last
            `capacity_trend_window_days` (default 60, 0 = all; minimum 7), or since the first
            snapshot if that is more recent (a day with no row carries the last row forward)
weekly_rate_pp = slope × 7
days_to_full   = (100 − current) / slope           (slope in pp/day)
projected_full = today + days_to_full
status = no_data   if < 3 calendar days of history (first snapshot -> today)
         stable    if slope <= capacity_flat_slope_pp (0.02 pp/day) and current < capacity_critical_percent (95)
         critical  if current >= capacity_critical_percent (95%), or days_to_full <= capacity_critical_days (14)
         watch     if days_to_full <= capacity_watch_days (60)
         ok         otherwise
```

Sorted `(status rank, days_to_full, -current, name)`. The UI shows a "collecting
data" banner until there are 7 calendar days of history.

### 7.9. AI Copilot — category briefings

`core/briefing.py` → `generate_ops_briefing` command / `category_briefing_view` →
`/assistant/briefing/<category>/` for `category` in `materials`, `products`,
`sales`, `purchase`, `logistics`, `warehouse` (nav: *Overview → AI Copilot*,
tab strip across the top of the page). Each domain has its own briefing,
including Logistics (§7.10).

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
keep it visibly separate (a dashed purple box, "estimate, not a fact").
Sales gets the same section from its product sales trend (§7.12). A category
whose payload has no trend field simply has no Outlook section, and giving one
to another category only needs that category's own trend field; no prompt or
template change is needed.

**`OpsBriefing`** row per run: `category` (see §7.11 for `my_checklist`),
`period`, `status` (`ok|empty|skipped|error`), `headline`, `body_text`,
`signals_json`, `signal_count`, `model_id`, `input_tokens`/`output_tokens`
(cost trace), `error_detail`, `generated_by` (null for the scheduled job).
Each category page renders the latest `ok` row for its own `category`; a newer
failed run shows a "last good briefing" notice. Rows from the earlier single
company-wide briefing carry `category='legacy'` and are not shown on any page.

Config in `settings_store.REGISTRY` (editable at `/system/settings/`):
`ops_briefing_enabled` (bool; off → `skipped` run, no API call, shared by every
category and the checklist) and `ops_briefing_model` (default `claude-opus-5`;
`claude-sonnet-5`/`claude-haiku-4-5` are cheaper for this job). The API key is
the **`ANTHROPIC_API_KEY` environment variable** — a secret, deliberately not a
`SystemSetting`.

`generate_briefing()` never raises for a config/API/network problem — it
records the failure on the row so the scheduled job stays green. Run it daily
(`python manage.py generate_ops_briefing --category all`, or `--period weekly`,
or one category at a time) from any scheduler: cron, Heroku Scheduler, a
scheduled GitHub Action, or on Railway a Cron Schedule service with the
`ANTHROPIC_API_KEY` env var set. Admin/Manager can also trigger a run for one category from its
page. `--dry-run` prints the signal payload(s) without calling the API.

### 7.10. Shipment logistics

`analytics.shipment_logistics()` → `views_analytics.shipment_logistics_view` →
`/analytics/logistics-performance/` (nav: *Analytics → Supply Chain*, tab: Logistics
Performance). Also feeds the Logistics category briefing (§7.9).

`sales_order_delivery_risk()` reads the outbound leg of a shipment and
`supplier_reliability()` the inbound leg; this function looks at every in-flight
shipment regardless of direction, including `Transfer` shipments
(warehouse-to-warehouse, no SO or PO):

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

### 7.11. Personal checklist — "My Open Jobs"

`analytics.my_open_jobs(user)` + `core/briefing.py::generate_my_checklist()` →
`ops_briefing_view` → `/assistant/briefing/` (nav: *Overview → AI Copilot*,
the checklist tab).

Not a company-wide roll-up — one person's own records, still open or in
process, across every domain:

- `SalesOrder` / `PurchaseOrder` / `ProductionRun` — `created_by=user`, closed
  statuses excluded (`Delivered`/`Rejected`, `Completed`/`Rejected`,
  `Completed`/`Cancelled` respectively — each model's real ownership field).
- `StockAudit` — `auditor=user`, `status='Pending'` (still unresolved).
- `Shipment` — `last_edited_by=user` (`ownership='touched'`, rendered
  "(last touched by you)").

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

### 7.12. Product sales trend

`analytics.product_sales_trend()` → `views_analytics.product_sales_trend_view`
→ `/analytics/sales-trends/` (nav: *Analytics → Demand*; the category's
only report, so no tab strip).
Also feeds the Sales category briefing (§7.9) and is the trend field that
unlocks its Outlook section.

The only genuinely time-series view in Sales — `sales_order_delivery_risk()`
is a point-in-time snapshot of currently-open orders; this looks at ordered
quantity across **all** real orders (Draft/Rejected/Cancelled excluded) over a trailing
6-month window, bucketed by month, split into two halves:

```
earlier_avg = mean of the first half of the window's monthly quantities
recent_avg  = mean of the second half
pct_change  = (recent_avg − earlier_avg) / earlier_avg × 100
status = insufficient_data  if sales activity in fewer than 3 of the 6 months
         new                if earlier_avg == 0 and recent_avg > 0
         declining          if pct_change <= −trend_significant_pct (15%)
         rising             if pct_change >= +trend_significant_pct (15%)
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
(estimate) section described in §7.9.

### 7.13. Warehouse rent and Rent Opportunities

**How rent is billed.** Each `Warehouse` has an `ownership_type` (`Internal`, `ExternalProvider`, `SupplierStorage`),
a `rental_billing_method` and a `rental_cost_per_mt` charged per billing term of `rental_term_days` days
(1 = a daily rate; 30 = RM per MT for every 30 days or part of 30 days a lot stays, matching the real rental invoices).
`warehouse_rent_burn()` is the single source of truth for rent:

```
Internal  -> 0
Overall   -> total_capacity_mt x rate / term_days                      (fixed, whatever is stored)
Usage     -> sum over open batches of weight_mt x effective rate / term_days
effective rate = Batch.rental_rate_per_mt   if set (locked, e.g. a negotiated PO rate)
                 Warehouse.rental_cost_per_mt otherwise (follows rate changes live)
```

`Batch.rent_term()` gives one lot's position in its billing terms: a term is charged in full once it starts,
so 31 days at 30-day terms is 2 terms. There is no minimum-billing floor.

**Rent Opportunities.** `rent_reduction_opportunities()` → `views_analytics.rent_opportunities_view` →
`/decision-support/rent-reduction/`. For each Usage-billed rented warehouse it suggests batches to move into the
company's own storage warehouses, checking the destination has room (capacity, plus transfers already on the way),
and estimates the rent each move saves until the batch is expected to be used up or expire, capped at
`dss_saving_horizon_days` (365). Moves saving less than `dss_min_total_saving_rm` (RM 100) are left out.
Accepting ticked suggestions creates Draft internal transfers; dismissing one records a reason and keeps that batch out
for `dss_dismiss_snooze_days` (30). Each decision is stored as a `RentSuggestion` with the estimate at the time,
and `rent_results()` later compares the origin warehouse's rent the week before and after each completed move with that estimate. The page also shows the Rent History chart
(`rent_history()`, §7.8).

### 7.14. Spoilage & write-offs

`spoilage_report(since, until)` → `views_analytics.spoilage_report_view` → `/analytics/spoilage/`.
Totals every QA write-off (`QAEvent` with action `disposed`; supplier returns are write-offs with that reason)
and stock lost in transit (`RegistryLog` `Lost_In_Transit`), broken down by reason, material or product,
warehouse and supplier, and shown against inbound quantity so the loss can be read as a share of what came in.

---

## 8. Operations & Controls

### 8.1. QA & spoilage control

All QA actions live in `core/qa.py`; the views only call them, and every action records a `QAEvent` with a reason,
which forms the batch's QA history and the data behind the spoilage report.

- **Holds and releases.** A batch is put on hold (`Quarantined`, its reservations released) by QA or automatically.
  `qa_release_needs_second_person` turns on a four-eyes rule so the person who held a batch can't also release it.
  `qa_hold_new_finished_goods` makes finished goods from completed runs start on hold.
- **Send to QA.** Any page with a batch can raise a `QARequest`, optionally holding the stock at once. QA works the
  request from the Requests tab and records an outcome.
- **Inspections.** `QAInspection` stores the checklist the inspector ticked and the result, including moisture checks.
- **Write-offs and returns.** A write-off worth at least `qa_writeoff_approval_rm` (RM 5,000) waits in the
  Action Center as a `QAApproval` before stock is removed. A manager can undo a write-off for `qa_writeoff_undo_days` (7).
  Reserved stock can't be written off, and the quantity is required.
- **Shelf-life extensions.** Up to `qa_extension_cap_pct` (25%) of the original shelf life can be added without
  approval; more goes to the Action Center. One extension is at most `qa_extension_max_days` (730).
- **Expiry sweep.** `manage.py qa_sweep` (daily) holds expired batches when `qa_auto_hold_expired` is on, warns QA
  `qa_expiry_warning_days` (7) before expiry, and reminds QA about batches on hold for `qa_quarantine_alert_days` (14).
  Batches within `qa_near_expiry_days` (30) are listed as near expiry.

### 8.2. Document numbering

Order, run, SKU, shipment and batch numbers are generated by `core/utils.py::generate_next_code()`: the highest number
among codes of exactly that form, plus one (e.g. `SO-1004`, `SHP-1188`). The prefixes are settings
(`prefix_sales_order`, `prefix_purchase_order`, `prefix_production_run`, `prefix_product_sku`,
`prefix_material_sku`, `prefix_shipment`, `prefix_batch`, `prefix_finished_goods_batch`), editable at
`/system/settings/`. Existing records keep their old prefix; numbering restarts for a new prefix.

### 8.3. CSV import and export

Products, materials, sales orders and purchase orders export to CSV and import from a matching template; recipes export to CSV and are edited in the recipe studio.
The order export puts the import columns first, so an export can be re-imported. Imports run as a **dry run** first
and report errors per order before anything is saved. Imported orders are records of past activity: they do not
move stock, which is entered as batches.

### 8.4. Printed documents

- **Pick list** (`/operations/shipments/<pk>/picklist/`): batches and locations to pick for a shipment.
- **Mixing sheet**: a production run's materials and quantities for the shop floor; bag counts and signatures are
  filled in by hand.
- **Assembly sheet**: the stock record of a completed run, materials used in and finished goods out.
- **Batch label** (`/warehouse/batch/<batch_number>/print/`) with a QR code to the public batch page.

### 8.5. Access and security

- **Roles** are Django Groups edited on the Roles page (`/system/roles/`). Code checks permissions only, never role
  names (`core/permissions.py`). A POST with no recognised action is refused by default (`action_denied`), and
  buttons a user can't use are hidden. Users can also be limited to certain warehouses.
- **Sign-in throttling** (`core/login_throttle.py`) locks a username after `login_fail_limit` failures from one
  computer (four times that across computers) for `login_lockout_minutes`; admins can unlock sooner.
- **Idle timeout.** Users are signed out after `session_idle_timeout_minutes` without activity
  (`static/js/session-timeout.js` with `/session/keepalive/`).
- **Password resets.** An admin issues a temporary password (`core/account.py`) which the user must change at
  first sign-in; an unused one expires after 24 hours.
- **Audit Log.** `UserAuditLog` records account, role and System Settings changes: who did what, to whom and when.
- **Error pages.** `core/errors.py` serves custom 400/403/404/500 pages from a standalone `error.html`, so a 500
  caused by a database outage still renders.
