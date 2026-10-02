"""Generate the Tier 3 "AI Copilot" category briefings from the current analytics signals.

For one or all categories (core/briefing.py::CATEGORY_SIGNAL_BUILDERS) it sends the signals to Claude once and stores an OpsBriefing row.
Schedule daily (or weekly with --period weekly). API/config failures are recorded on the row, never a non-zero exit.
The personal checklist is generated on demand by the user, not by this command.

    python manage.py generate_ops_briefing [--category materials|all] [--period weekly] [--dry-run]"""
import json

from django.core.management.base import BaseCommand

_ALL_CATEGORIES = ("materials", "products", "sales", "purchase", "logistics", "warehouse")


class Command(BaseCommand):
    help = "Generate the AI Copilot category briefings (Tier 3) from current analytics signals."

    def add_arguments(self, parser):
        parser.add_argument(
            "--category", choices=[*_ALL_CATEGORIES, "all"], default="all",
            help="Which category briefing(s) to generate (default: all).",
        )
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

        categories = list(_ALL_CATEGORIES) if options["category"] == "all" else [options["category"]]

        if options["dry_run"]:
            for category in categories:
                signals = collect_signals(category)
                self.stdout.write(self.style.WARNING(f"--- {category} ---"))
                self.stdout.write(json.dumps(signals, indent=2, sort_keys=True, default=str))
                self.stdout.write(self.style.WARNING(
                    f"{category}: {signals['signal_count']} notable signal(s); nothing sent or saved."
                ))
            return

        for category in categories:
            b = generate_briefing(category=category, period=options["period"])
            if b.status == "ok":
                self.stdout.write(self.style.SUCCESS(
                    f"{category}: briefing #{b.id} [{b.model_id}] {b.input_tokens or 0}+{b.output_tokens or 0} tok: {b.headline}"
                ))
            elif b.status == "empty":
                self.stdout.write(self.style.SUCCESS(f"{category}: briefing #{b.id} - no notable signals."))
            elif b.status == "skipped":
                self.stdout.write(self.style.WARNING(f"{category}: briefing #{b.id} skipped ({b.error_detail})"))
            else:
                self.stderr.write(self.style.ERROR(f"{category}: briefing #{b.id} failed - {b.error_detail}"))
