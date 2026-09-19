"""
Backfill daily_rent_cost on existing WarehouseUtilizationSnapshot rows as an
ESTIMATE (rent_estimated=True): the day's recorded tonnage x the warehouse's
current rate. Past rates and per-batch negotiated rates aren't known, so this is
an approximation for the rent-history chart, not a record. Mirrors
core.analytics.estimate_snapshot_rent(). Reverse clears the estimates.
"""
from decimal import Decimal

from django.db import migrations


def backfill(apps, schema_editor):
    Snapshot = apps.get_model("core", "WarehouseUtilizationSnapshot")
    rows = list(Snapshot.objects.filter(daily_rent_cost__isnull=True).select_related("warehouse"))
    for s in rows:
        w = s.warehouse
        if w.ownership_type == "Internal":
            cost, mode = Decimal("0"), "Internal"
        elif w.rental_billing_method == "Overall":
            cost, mode = w.total_capacity_mt * w.rental_cost_per_mt, "Overall Capacity"
        else:
            cost, mode = s.used_mt * w.rental_cost_per_mt, "Usage"
        s.daily_rent_cost = cost.quantize(Decimal("0.01"))
        s.billing_mode = mode
        s.rent_estimated = True
    Snapshot.objects.bulk_update(rows, ["daily_rent_cost", "billing_mode", "rent_estimated"], batch_size=500)
    print(f"\n  snapshot rent backfill: estimated {len(rows)} row(s)")


def clear(apps, schema_editor):
    Snapshot = apps.get_model("core", "WarehouseUtilizationSnapshot")
    Snapshot.objects.filter(rent_estimated=True).update(daily_rent_cost=None, billing_mode="", rent_estimated=False)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0057_snapshot_rent_fields"),
    ]

    operations = [
        migrations.RunPython(backfill, clear),
    ]
