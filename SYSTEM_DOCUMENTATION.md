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
