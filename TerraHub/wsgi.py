"""WSGI entry point for TerraHub (exposes `application`)."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'TerraHub.settings')

application = get_wsgi_application()
