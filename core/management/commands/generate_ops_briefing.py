"""
Generate the Tier 3 "AI Ops Briefing" from the current analytics signals.

Collects the notable Tier 1/2 signals (core/briefing.collect_signals), sends them
to Claude once, and stores the result as an OpsBriefing row. ops_briefing_view
renders the latest one - it is what the "Digital Assistant (AI)" sidebar link
opens.

Schedule it like snapshot_utilization - once a day (or weekly with --period
weekly). It never exits non-zero for an API/config problem: the failure is
recorded on the row and the previous good briefing keeps showing.

    python manage.py generate_ops_briefing
    python manage.py generate_ops_briefing --period weekly
    python manage.py generate_ops_briefing --dry-run     # print signals, no API call
"""
import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Generate the AI Ops Briefing (Tier 3) from current analytics signals."

    def add_arguments(self, parser):
        parser.add_argument(
            "--period", choices=["daily", "weekly"], default="daily",
            help="Briefing cadence label stored on the row (default: daily).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Collect and print the signal payload without calling the API or saving.",
        )

    def handle(self, *args, **options):
        from core.briefing import collect_signals, generate_briefing

        if options["dry_run"]:
            signals = collect_signals()
            self.stdout.write(json.dumps(signals, indent=2, sort_keys=True, default=str))
            self.stdout.write(self.style.WARNING(
                f"Dry run - {signals['signal_count']} notable signal(s); nothing sent or saved."
            ))
            return

        b = generate_briefing(period=options["period"])

        if b.status == "ok":
            self.stdout.write(self.style.SUCCESS(
                f"Briefing #{b.id} [{b.model_id}] {b.input_tokens or 0}+{b.output_tokens or 0} tok: {b.headline}"
            ))
        elif b.status == "empty":
            self.stdout.write(self.style.SUCCESS(f"Briefing #{b.id}: no notable signals - nothing to brief."))
        elif b.status == "skipped":
            self.stdout.write(self.style.WARNING(f"Briefing #{b.id}: skipped ({b.error_detail})"))
        else:
            self.stderr.write(self.style.ERROR(f"Briefing #{b.id}: failed - {b.error_detail}"))
