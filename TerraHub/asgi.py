"""ASGI entry point for TerraHub (exposes `application`)."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'TerraHub.settings')

application = get_asgi_application()
