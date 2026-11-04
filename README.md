# TerraHub

Inventory, production and warehouse management system. Final year project by Hariharan, <university>, 2026.

Built with Django 6.0 (Python 3.14) and PostgreSQL. Modules: purchase orders, sales orders, manufacturing runs, shipments, QA and batch tracking, stock audits, facility rent and the Rent Reduction DSS, approvals, analytics, and an optional AI Copilot.

Full technical documentation: [SYSTEM_DOCUMENTATION.md](SYSTEM_DOCUMENTATION.md).

## Requirements

- Python 3.14
- PostgreSQL 14 or newer (tested on 16)
- Git

## How to run it (Windows, PowerShell)

1. **Get the code**
   ```powershell
   git clone <this repository's URL> TerraHub
   cd TerraHub
   ```
2. **Create a virtual environment and install packages**
   ```powershell
   py -3.14 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
   If PowerShell blocks the activate script, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.
   If `python` opens the Microsoft Store, use `py` or `.\.venv\Scripts\python.exe` instead.
3. **Create an empty database** in PostgreSQL (pgAdmin, or `psql -U postgres`):
   ```sql
   CREATE DATABASE "TerraHub";
   ```
4. **Create the settings file**: copy `.env.example` to `.env` and fill in your PostgreSQL user, password and port.
   ```powershell
   Copy-Item .env.example .env
   ```
   `ANTHROPIC_API_KEY` is optional. Without it, everything works except the AI Copilot briefings.
5. **Build the tables** (also creates the default roles and settings)
   ```powershell
   python manage.py migrate
   ```
   Do not run `makemigrations`.
6. **Create the first admin user**
   ```powershell
   python manage.py createsuperuser
   ```
7. **Start the server**
   ```powershell
   python manage.py runserver
   ```
   Open http://127.0.0.1:8000/ and sign in with the admin user.

On macOS/Linux use `python3 -m venv .venv` and `source .venv/bin/activate`; the other steps are the same.

## First steps in the app

1. Sign in as the admin user.
2. The **Users** page: add users and give each a role (Admin, Manager, Purchasing, Warehouse, Staff (Viewer), ...).
3. The **Roles & Permissions** page: see what each role can do.
4. Add master data: facilities, suppliers, clients, materials, products and recipes.
5. Try a full flow: purchase order > receive > QA release > production run > sales order > shipment.

## Running the tests

```powershell
python manage.py test core
```
The tests create and delete their own test database, so your data is not touched. The PostgreSQL user needs permission to create databases.

## Optional management commands

```powershell
python manage.py qa_sweep               # hold batches past their expiry date
python manage.py snapshot_utilization   # daily facility usage snapshot
python manage.py generate_ops_briefing  # AI Copilot briefing (needs ANTHROPIC_API_KEY)
```

## Deploying (Railway)

During development the system was deployed on [Railway](https://railway.app). `Procfile` runs `collectstatic`, `migrate` and `gunicorn` on every deploy. Set these variables: `SECRET_KEY`, `DEBUG=False`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `DATABASE_URL`, and optionally `ANTHROPIC_API_KEY`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `could not connect to server` | PostgreSQL is not running, or `DB_PORT` / `DB_PASSWORD` in `.env` are wrong |
| `database "TerraHub" does not exist` | do step 3 |
| `SECRET_KEY must be set when DEBUG is False` | add `DEBUG=True` to `.env` for local use, or set `SECRET_KEY` |
| AI Copilot says the API key is not set | expected without `ANTHROPIC_API_KEY` |
