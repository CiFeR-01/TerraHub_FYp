"""Daily QA sweep: put expired stock on hold, warn QA about batches nearing expiry and about long holds.
The QA page runs the same sweep when it loads; schedule this (e.g. once a day on Railway) to cover quiet days.
Usage: python manage.py qa_sweep"""
from django.core.management.base import BaseCommand

from core.qa import daily_sweep


class Command(BaseCommand):
    help = "Hold expired batches and send QA expiry / long-hold alerts (safe to run repeatedly)."

    def handle(self, *args, **options):
        counts = daily_sweep()
        self.stdout.write(self.style.SUCCESS(
            f"QA sweep: {counts['auto_held']} expired batch(es) held, {counts['warned']} expiry warning(s), "
            f"{counts['hold_alerts']} long-hold reminder(s)."
        ))
