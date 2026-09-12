"""
Seed the SystemSetting rows for the Phase 3 AI Ops Briefing tunables
(ops_briefing_enabled, ops_briefing_model), added to
core.settings_store.REGISTRY.

get_setting() already falls back to the registry default without a row; this just
makes them visible/editable in the in-site settings editor and admin from the
start. Idempotent; reverse removes only these keys.
"""
from django.db import migrations

KEYS = ("ops_briefing_enabled", "ops_briefing_model")


def seed(apps, schema_editor):
    from core.settings_store import REGISTRY

    SystemSetting = apps.get_model("core", "SystemSetting")
    for key in KEYS:
        if key not in REGISTRY:
            continue
        default, value_type, description = REGISTRY[key]
        SystemSetting.objects.update_or_create(
            key=key,
            defaults={"value": str(default), "value_type": value_type, "description": description},
        )


def unseed(apps, schema_editor):
    SystemSetting = apps.get_model("core", "SystemSetting")
    SystemSetting.objects.filter(key__in=KEYS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0041_opsbriefing"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
