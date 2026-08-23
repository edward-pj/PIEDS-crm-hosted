"""Verify the live database actually enforces what the code assumes.

Run this after every migration against a new host -- especially the first one
against Supabase. Migrations "succeeding" is not proof the constraint landed,
and the entire no-double-mail guarantee is one index.

    ../.venv/bin/python manage.py check_db
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

#: (table, index name, why it matters)
REQUIRED_INDEXES = [
    (
        "campaign_mailings",
        "uniq_campaign_contact",
        "THE idempotency guarantee: one mail per (campaign, contact). Without "
        "it, a retry or two racing agents can mail a prospect twice.",
    ),
    (
        "contacts",
        "contacts_tags_gin",
        "GIN index backing tag filtering. Missing it is slow, not unsafe.",
    ),
]


class Command(BaseCommand):
    help = "Verify constraints and connection settings on the live database."

    def handle(self, *args, **options):
        db = settings.DATABASES["default"]
        host, port = db.get("HOST") or "localhost", db.get("PORT") or 5432
        self.stdout.write(f"database : {host}:{port}/{db.get('NAME')}")

        failures = []

        with connection.cursor() as cur:
            cur.execute("SELECT version()")
            self.stdout.write(f"server   : {cur.fetchone()[0].split(',')[0]}")

            for table, index, why in REQUIRED_INDEXES:
                cur.execute(
                    "SELECT 1 FROM pg_indexes WHERE tablename = %s AND indexname = %s",
                    [table, index],
                )
                if cur.fetchone():
                    self.stdout.write(self.style.SUCCESS(f"  ok     {table}.{index}"))
                else:
                    self.stdout.write(self.style.ERROR(f"  MISSING {table}.{index}"))
                    failures.append(f"{table}.{index} — {why}")

            # A transaction-mode pooler silently breaks SELECT ... FOR UPDATE
            # semantics the send path relies on. Surface the port so a bad
            # connection string is caught here rather than during a live send.
            if str(port) == "6543" and not getattr(settings, "DISABLE_SERVER_SIDE_CURSORS", False):
                failures.append(
                    "Port 6543 is Supabase's TRANSACTION pooler. Either switch to the "
                    "session pooler on 5432, or set DB_TRANSACTION_POOLER=True."
                )

            # Prove the lock the send path depends on is actually available.
            try:
                cur.execute("SELECT id FROM contacts LIMIT 1 FOR UPDATE")
                cur.fetchall()
                self.stdout.write(self.style.SUCCESS("  ok     SELECT ... FOR UPDATE"))
            except Exception as exc:                       # noqa: BLE001
                failures.append(f"SELECT ... FOR UPDATE failed: {exc}")

        # The server now holds Gmail refresh tokens, so a missing or broken
        # encryption key is a boot-time failure like any other. Without this
        # check, check_db passes cleanly and then every single send fails on a
        # key that was never set -- which reads as "Gmail is broken", not as
        # "an environment variable is missing".
        try:
            from crm.services import secrets as token_store

            if not token_store.is_configured():
                failures.append(
                    "GMAIL_TOKEN_KEY is not set. Refresh tokens cannot be stored "
                    "or read, so nobody can send. Generate one:  python -c "
                    "'from cryptography.fernet import Fernet; "
                    "print(Fernet.generate_key().decode())'"
                )
            else:
                token_store.self_test()
                self.stdout.write(
                    self.style.SUCCESS("  ok     GMAIL_TOKEN_KEY round-trips")
                )
        except Exception as exc:                           # noqa: BLE001
            failures.append(f"GMAIL_TOKEN_KEY is unusable: {exc}")

        if failures:
            raise CommandError(
                "Database is not safe to send from:\n  - " + "\n  - ".join(failures)
            )

        self.stdout.write(self.style.SUCCESS("\nAll checks passed."))
