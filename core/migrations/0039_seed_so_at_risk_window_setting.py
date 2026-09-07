"""
Seed the SystemSetting row for so_at_risk_window_days (added to
core.settings_store.REGISTRY in Phase 1's delivery-risk board).

get_setting() already falls back to the registry default without a row, so this
only makes it visible/auditable in the in-site settings editor and admin.
Idempotent; reverse removes just this key.
"""
from django.db import migrations

KEY = "so_at_risk_window_days"


def seed(apps, schema_editor):
    from core.settings_store import REGISTRY

    SystemSetting = apps.get_model("core", "SystemSetting")
    if KEY not in REGISTRY:
        return
    default, value_type, description = REGISTRY[KEY]
    SystemSetting.objects.update_or_create(
        key=KEY,
        defaults={"value": str(default), "value_type": value_type, "description": description},
    )


def unseed(apps, schema_editor):
    SystemSetting = apps.get_model("core", "SystemSetting")
    SystemSetting.objects.filter(key=KEY).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0038_seed_settings_backfill_completed_date"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
