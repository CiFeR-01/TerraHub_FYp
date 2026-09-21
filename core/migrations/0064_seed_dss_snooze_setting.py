"""
Seed the SystemSetting row for dss_dismiss_snooze_days so it shows in the in-site
settings editor. get_setting() falls back to the registry default without a row.
Idempotent; reverse removes just this key.
"""
from django.db import migrations

KEY = "dss_dismiss_snooze_days"


def seed(apps, schema_editor):
    from core.settings_store import REGISTRY

    SystemSetting = apps.get_model("core", "SystemSetting")
    default, value_type, description = REGISTRY[KEY]
    SystemSetting.objects.get_or_create(
        key=KEY,
        defaults={"value": str(default), "value_type": value_type, "description": description},
    )


def unseed(apps, schema_editor):
    SystemSetting = apps.get_model("core", "SystemSetting")
    SystemSetting.objects.filter(key=KEY).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0063_rentsuggestion"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
