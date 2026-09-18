"""
Seed the SystemSetting row for qa_hold_new_finished_goods (off by default).

get_setting() falls back to the registry default without a row, so this only makes
it visible in the in-site settings editor. Idempotent; reverse removes just this key.
"""
from django.db import migrations

KEY = "qa_hold_new_finished_goods"


def seed(apps, schema_editor):
    from core.settings_store import REGISTRY

    SystemSetting = apps.get_model("core", "SystemSetting")
    if KEY not in REGISTRY:
        return
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
        ("core", "0054_recipe_quantity_4dp"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
