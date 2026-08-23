"""Execute one pass of the scheduled-send queue.

    ../.venv/bin/python manage.py run_tick

The same `services/runner.py::tick()` that the hosted scheduler will call over
HTTP, reached from a terminal instead. One code path, so what you test locally
is what runs in production.

Run it in a loop during development if you want the old always-on behaviour:

    while true; do python manage.py run_tick; sleep 60; done
"""

import json

from django.core.management.base import BaseCommand

from crm.services import runner


class Command(BaseCommand):
    help = "Run one scheduler tick: send everything currently due."

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-seconds", type=int, default=runner.TICK_MAX_SECONDS,
            help="Stop cleanly after this long. Anything unsent is picked up next tick.",
        )
        parser.add_argument(
            "--json", action="store_true", help="Print the report as JSON."
        )

    def handle(self, *args, **options):
        report = runner.tick(max_seconds=options["max_seconds"])

        if options["json"]:
            self.stdout.write(json.dumps(report.dict(), indent=2))
            return

        self.stdout.write(
            f"members={report.members} jobs={report.jobs} "
            f"sent={report.sent} skipped={report.skipped}"
        )
        if report.stopped_early:
            self.stdout.write(self.style.WARNING(
                "stopped on budget; run again to continue"
            ))
        for error in report.errors[:10]:
            self.stdout.write(self.style.ERROR(f"  {error}"))
        if not report.errors:
            self.stdout.write(self.style.SUCCESS("no errors"))
