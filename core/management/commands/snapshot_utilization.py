"""
Record today's utilization for every warehouse into WarehouseUtilizationSnapshot.

Idempotent per day (unique on warehouse + snapshot_date) - safe to run more than
once. Schedule it once a day; capacity_forecast() needs ~7 days of history before
its projections are meaningful.

    python manage.py snapshot_utilization
"""
from django.core.management.base import BaseCommand

from core.analytics import snapshot_warehouse_utilization


class Command(BaseCommand):
    help = "Snapshot each warehouse's current utilization_percent (idempotent per day)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date", help="Override the snapshot date (YYYY-MM-DD); defaults to today."
        )

    def handle(self, *args, **options):
        snap_date = None
        if options.get("date"):
            from datetime import date
            snap_date = date.fromisoformat(options["date"])

        count, snap_date = snapshot_warehouse_utilization(snap_date)
        self.stdout.write(self.style.SUCCESS(
            f"Snapshotted {count} warehouse(s) for {snap_date}."
        ))
