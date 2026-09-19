"""
Seed the SystemSetting rows for the Rent Opportunities total-saving rule
(dss_min_total_saving_rm, dss_saving_horizon_days) so they show in the in-site
settings editor. get_setting() falls back to the registry defaults without a row.
Idempotent; reverse removes just these keys.
"""
from django.db import migrations

KEYS = ("dss_min_total_saving_rm", "dss_saving_horizon_days")


def seed(apps, schema_editor):
    from core.settings_store import REGISTRY

    SystemSetting = apps.get_model("core", "SystemSetting")
    for key in KEYS:
        if key not in REGISTRY:
            continue
        default, value_type, description = REGISTRY[key]
        SystemSetting.objects.get_or_create(
            key=key,
            defaults={"value": str(default), "value_type": value_type, "description": description},
        )


def unseed(apps, schema_editor):
    SystemSetting = apps.get_model("core", "SystemSetting")
    SystemSetting.objects.filter(key__in=KEYS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0061_remove_legacy_roles"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
