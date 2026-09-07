"""
Seed the SystemSetting rows from core.settings_store.REGISTRY, and backfill
PurchaseOrder.completed_date for POs that are already 'Completed'.

completed_date backfill: latest manufacturing_date among batches received against
the PO (that is the receipt date apply_po_material_receipt stamps). POs with no
received batches are left null - the scorecard still falls back to other signals.

Both directions are safe to re-run; reverse only removes the seeded setting rows.
"""
from django.db import migrations

from core.settings_store import REGISTRY


def seed_settings(apps, schema_editor):
    SystemSetting = apps.get_model("core", "SystemSetting")
    for key, (default, value_type, description) in REGISTRY.items():
        SystemSetting.objects.update_or_create(
            key=key,
            defaults={
                "value": str(default),
                "value_type": value_type,
                "description": description,
            },
        )


def unseed_settings(apps, schema_editor):
    SystemSetting = apps.get_model("core", "SystemSetting")
    SystemSetting.objects.filter(key__in=list(REGISTRY)).delete()


def backfill_completed_date(apps, schema_editor):
    PurchaseOrder = apps.get_model("core", "PurchaseOrder")
    filled = 0
    for po in PurchaseOrder.objects.filter(status="Completed", completed_date__isnull=True):
        dates = [
            b.manufacturing_date
            for b in po.received_batches.all()
            if b.manufacturing_date is not None
        ]
        if dates:
            po.completed_date = max(dates)
            po.save(update_fields=["completed_date"])
            filled += 1
    print(f"\n  PurchaseOrder.completed_date backfill: filled {filled}")


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0037_purchaseorder_completed_date_systemsetting"),
    ]

    operations = [
        migrations.RunPython(seed_settings, unseed_settings),
        migrations.RunPython(backfill_completed_date, noop),
    ]
