"""
Record today's utilization for every warehouse into WarehouseUtilizationSnapshot.

Idempotent per day (unique on warehouse + snapshot_date) - safe to run more than
once. Schedule it once a day; capacity_forecast() needs ~7 days of history before
its projections are meaningful.

    python manage.py snapshot_utilization
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.analytics import warehouse_utilization
from core.models import WarehouseUtilizationSnapshot


class Command(BaseCommand):
    help = "Snapshot each warehouse's current utilization_percent (idempotent per day)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date", help="Override the snapshot date (YYYY-MM-DD); defaults to today."
        )

    def handle(self, *args, **options):
        if options.get("date"):
            from datetime import date
            snap_date = date.fromisoformat(options["date"])
        else:
            snap_date = timezone.now().date()

        rows = warehouse_utilization()
        for r in rows:
            WarehouseUtilizationSnapshot.objects.update_or_create(
                warehouse_id=r["warehouse_id"],
                snapshot_date=snap_date,
                defaults={
                    "used_mt": r["used_mt"],
                    "capacity_mt": r["capacity_mt"],
                    "utilization_percent": r["utilization_percent"],
                },
            )
        self.stdout.write(self.style.SUCCESS(
            f"Snapshotted {len(rows)} warehouse(s) for {snap_date}."
        ))
