"""
Backfill Batch.rental_rate_per_mt for currently-open batches (status Active or
Quarantined) that predate the field, using each batch's warehouse's standing
rental_cost_per_mt as a one-time snapshot.

Without this, warehouse_rent_burn() would read near-zero on day one, since it
deliberately excludes NULL-rate batches rather than guessing a historical rate
for batches received before rate lock-in existed.

Does NOT backfill closed_date/Depleted status for old zero-quantity batches -
those already contribute 0 to rent (0 MT x rate), so it's a cosmetic reporting
gap only, not a correctness issue.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    Batch = apps.get_model("core", "Batch")

    open_batches = (Batch.objects
                     .filter(status__in=["Active", "Quarantined"],
                             warehouse__isnull=False,
                             rental_rate_per_mt__isnull=True)
                     .select_related("warehouse")
                     .order_by("warehouse_id"))

    by_warehouse = {}
    for b in open_batches:
        by_warehouse.setdefault(b.warehouse_id, []).append(b)

    for warehouse_id, batches in by_warehouse.items():
        rate = batches[0].warehouse.rental_cost_per_mt
        for b in batches:
            b.rental_rate_per_mt = rate
        Batch.objects.bulk_update(batches, ["rental_rate_per_mt"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0045_batch_rental_fields"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
