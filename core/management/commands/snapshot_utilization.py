"""
Record today's occupancy and rent for every warehouse into WarehouseUtilizationSnapshot.

No longer needs scheduling: core/signals.py rewrites a warehouse's row for today
whenever its stock or settings change, and the Rent Opportunities / Capacity pages
fill in any missing row for today. Kept for manual use (e.g. after a bulk import
that bypassed the signals, or --date to write a specific day).

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
