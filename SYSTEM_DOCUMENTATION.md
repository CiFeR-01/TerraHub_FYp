# TerraHub - System Documentation

TerraHub is a Django-based manufacturing and warehouse operations platform. It tracks materials and finished-goods inventory across warehouses, drives production runs from bills of materials (recipes), manages purchase and sales orders, generates shipments with FEFO-based batch allocation, and provides QA and approvals workflows on top of full batch/lot traceability.

---

## 1. System Overview

Core capabilities:
- **Custom User Model** with role-based access (`CustomUser`, `Role`) and per-location access restrictions.
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
│   ├── urls.py                 # Application URL routing (see §4)
│   ├── utils.py                # Allocation engine (FEFO), stock helpers
│   ├── decorators.py           # Role/permission decorators
│   ├── context_processors.py   # Template context (nav, notifications, etc.)
│   ├── db_tracker.py           # DB query interception & diagnostics
│   ├── admin.py                # Django admin registrations
│   └── tests.py                # Test suite
├── static/
│   └── css/style.css
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
| `/` | `home` | `home_view` | Public landing page |
| `/login/` | `login` | `LoginView` | Authentication |
| `/logout/` | `logout` | `LogoutView` | Clears session, redirects home |
| `/dashboard/` | `dashboard` | `dashboard_view` | Main authenticated dashboard |
| `/profile/` | `profile` | `profile_view` | User profile |
| `/system/` | `system` | `system_view` | System telemetry console |
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
| `/admin/` | — | Django admin | Django admin site |

---

## 5. Domain Model

Defined in `core/models.py`:

- **Access**: `Role`, `CustomUser`
- **Facilities**: `Warehouse`, `WarehouseLocation`
- **Catalog**: `Material`, `Product`, `ProductRecipe`
- **Manufacturing**: `ProductionRun`, `RunMaterialUsage`, `ProductionConsumption`
- **Inventory**: `Batch` (lot-level tracking with expiry/manufacturing dates, quantity, allocated quantity)
- **Purchasing**: `PurchaseOrder`, `PurchaseOrderDetail`
- **Sales**: `SalesOrder`, `SalesOrderDetail`
- **Fulfillment**: `Shipment`, `ShipmentItem`
- **Quality & Audit**: `StockAudit`, `RegistryLog`, `OrderTimeline`
- **Allocation**: `StockAllocation` (shared reservation engine used by sales orders and production runs, resolved via FEFO in `core/utils.py`)
- **Messaging**: `Notification`

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
