# TerraHub - System Documentation

Welcome to **TerraHub**, a next-generation geospatial intelligence and environmental workspace application built with Django and Python. This platform acts as a secure console hub for environmental data feeds, telemetry synchronization, and spatial indexing.

---

## 1. System Overview

TerraHub is designed to coordinate, analyze, and render geological, atmospheric, and telemetry data.
Key systems established in this initialization:
- **Custom User Model**: Supporting customizable user credentials and extensible role access flags (`CustomUser`).
- **Core Workspace Layout**: Fully authenticated console entry and stateful dashboard view.
- **Premium Responsive Visual Layout**: Sleek obsidian dark theme with futuristic glows, transitions, glassmorphic panels, and dynamic system logs layout.

---

## 2. Architecture & Technology Stack

- **Backend Framework**: Django 6.0.4
- **Language**: Python 3.14
- **Database**: SQLite (Local development default, extensible to PostgreSQL)
- **Frontend / Styling**: Vanilla HTML5, CSS3, Google Fonts (Outfit, Plus Jakarta Sans), semantic structures, and ambient keyframe animations.

---

## 3. Directory Layout

```text
d:\TerraHub
├── .gitignore
├── .venv/                      # Local Python Virtual Environment
├── db.sqlite3                  # SQLite Database
├── manage.py                   # Django Management Tool
├── requirements.txt            # System Dependencies list
├── SYSTEM_DOCUMENTATION.md     # System Architecture & Guides
├── TerraHub/                   # Django Project Configuration Module
│   ├── __init__.py
│   ├── asgi.py
│   ├── settings.py             # Core Project Configuration settings
│   ├── urls.py                 # Global Project URLs Routing config
│   └── wsgi.py
├── core/                       # Core Django Application
│   ├── migrations/             # Database Migration logs
│   ├── __init__.py
│   ├── admin.py
│   ├── apps.py
│   ├── db_tracker.py           # Database Query Tracking & Diagnostics
│   ├── models.py               # Database Schema Definitions
│   ├── urls.py                 # Application specific URLs mapping
│   └── views.py                # Core Controller Views
└── templates/                  # Frontend HTML Master Templates
    ├── base.html               # Shared premium base skeleton & styles
    ├── home.html               # Public Landing page
    ├── login.html              # Custom dark-themed access portal
    ├── dashboard.html          # Standard app user dashboard placeholder
    └── system.html             # Secure administrative system control console
```

---

## 4. Routing Table

| Path | Name | Controller View / Class | Description | Authentication |
| :--- | :--- | :--- | :--- | :--- |
| `/` | `home` | `core.views.home_view` | Public landing page / portal entryway | Public |
| `/login/` | `login` | `django.contrib.auth.views.LoginView` | Styled glassmorphic authentication page | Public |
| `/logout/` | `logout` | `django.contrib.auth.views.LogoutView` | Clears active session & redirects home | Active User Session |
| `/dashboard/` | `dashboard` | `core.views.dashboard_view` | Main user application dashboard placeholder | Authenticated Only |
| `/system/` | `system` | `core.views.system_view` | Secure system telemetry console workspace | Authenticated Only |
| `/system/db-logs/` | `db_logs_api` | `core.views.db_logs_api_view` | Returns database query logs & connection status as JSON | Authenticated Only |
| `/system/db-logs/clear/` | `db_clear_logs` | `core.views.db_clear_logs_view` | Clears in-memory database query logs buffer | Authenticated Only |
| `/system/db-logs/test/` | `db_test_op` | `core.views.db_test_op_view` | Triggers dummy read/write queries for diagnostic validation | Authenticated Only |
| `/warehouse/<int:pk>/edit/` | `warehouse_edit` | `core.views.warehouse_edit_view` | Secure administrative facility management edit portal | Authenticated Only |


---

## 5. Development & Setup Guide

### 5.1. Virtual Environment Setup
Ensure you have Python 3.14 installed on your system. Navigate to the project root and spin up the environment:
```powershell
# Create virtual environment
python -m venv .venv

# Activate virtual environment
.venv\Scripts\Activate.ps1

# Install requirements
pip install -r requirements.txt
```

### 5.2. Database Migrations
Generate application-specific and default framework database structures:
```powershell
# Create core app migrations
python manage.py makemigrations core

# Execute migrations onto SQLite db.sqlite3
python manage.py migrate
```

### 5.3. Superuser Creation
Create an administrative account non-interactively or interactively:
```powershell
# Environment Variable Method
$env:DJANGO_SUPERUSER_PASSWORD="admin"
python manage.py createsuperuser --noinput --username=admin --email=admin@terrahub.local
```

### 5.4. Start Development Server
Deploy the development pipeline server:
```powershell
python manage.py runserver
```
The application will be accessible at `http://127.0.0.1:8000/`.

---

## 6. Live Database Diagnostics & Query Logging Console

TerraHub includes an integrated real-time database connection diagnostics helper and SQL query tracker log dashboard built directly into the **System Control Console** (`/system/`).

### 6.1. DB Diagnostics (Connection Health & Latency)
- **Automatic Status Check**: Dynamically checks database connectivity using `connection.ensure_connection()` and runs a benchmark query (`SELECT 1`) to calculate latency.
- **Environment Context Identification**: Detects whether settings are configured to use the live database (PostgreSQL via `django.db.backends.postgresql` backend) or a local development database (SQLite via `django.db.backends.sqlite3` backend).
- **On-Demand Health Testing**: Users can test the latency and active status directly using the "Test Latency & Status" interactive AJAX trigger on the page.

### 6.2. SQL Read/Write Console
- **Query Interception**: Implemented in [db_tracker.py](file:///d:/TerraHub/core/db_tracker.py) using a custom `db_query_logging_wrapper` registered via the `connection_created` signal on Django initialization.
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
