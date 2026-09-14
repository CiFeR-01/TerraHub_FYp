"""
Generate the Tier 3 "AI Copilot" category briefings from the current analytics
signals.

Collects the notable Tier 1/2 signals for one or all of the six domain
categories (materials, products, sales, purchase, logistics, warehouse - see
core/briefing.py::CATEGORY_SIGNAL_BUILDERS), sends them to Claude once per
category, and stores each result as an OpsBriefing row. category_briefing_view
renders the latest one per category - these are what the six tabs on the
"AI Copilot" pages open.

Schedule it like snapshot_utilization - once a day (or weekly with --period
weekly). It never exits non-zero for an API/config problem: the failure is
recorded on the row and the previous good briefing keeps showing.

The personal checklist ("My Open Jobs") is deliberately NOT produced by this
command - it is one person's own data, generated on demand by that person's
own click on /assistant/briefing/ (core/briefing.py::generate_my_checklist),
never on a schedule for "no particular user".

    python manage.py generate_ops_briefing
    python manage.py generate_ops_briefing --category materials
    python manage.py generate_ops_briefing --category all --period weekly
    python manage.py generate_ops_briefing --category logistics --dry-run   # print signals, no API call
"""
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
