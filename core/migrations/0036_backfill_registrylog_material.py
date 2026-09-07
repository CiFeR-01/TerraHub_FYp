"""
Best-effort backfill of RegistryLog.material for history written before the FK
existed. The item_name -> Material parser lives in core.analytics
(resolve_material_from_label) so it can be unit-tested; it is a pure string
function with no model dependency, safe to call from a migration.

Rows that cannot be resolved (batch-only labels, bulk-import summaries, finished
goods) are left null. Prints a matched/skipped count. Reverse is a no-op.
"""
from django.db import migrations

from core.analytics import resolve_material_from_label

RELEVANT_ACTIONS = {
    "Consumed_For_Manufacturing",
    "Inbound",
    "Outbound",
    "Adjusted",
    "Spoiled_Disposal",
    "QA_Extension",
}


def backfill(apps, schema_editor):
    RegistryLog = apps.get_model("core", "RegistryLog")
    Material = apps.get_model("core", "Material")

    by_name = {}
    for mat in Material.objects.all():
        by_name.setdefault((mat.name or "").strip().lower(), mat)
    if not by_name:
        return

    matched = 0
    skipped = 0
    to_update = []
    for log in RegistryLog.objects.filter(material__isnull=True).iterator():
        if log.action_type not in RELEVANT_ACTIONS:
            skipped += 1
            continue
        mat = resolve_material_from_label(log.item_name, by_name)
        if mat is None:
            skipped += 1
            continue
        log.material_id = mat.id
        to_update.append(log)
        matched += 1
        if len(to_update) >= 500:
            RegistryLog.objects.bulk_update(to_update, ["material"])
            to_update.clear()
    if to_update:
        RegistryLog.objects.bulk_update(to_update, ["material"])

    print(f"\n  RegistryLog.material backfill: matched {matched}, skipped {skipped}")


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0035_registrylog_material"),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
