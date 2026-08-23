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
        "campaign_mailings",
        "uniq_root_campaign_contact",
        "The TEAM-WIDE idempotency guarantee: one mail per (root campaign, "
        "contact). Without it two members' sub-campaigns can each mail the same "
        "prospect under the same banner.",
    ),
    (
        "contacts",
        "contacts_tags_gin",
        "GIN index backing tag filtering. Missing it is slow, not unsafe.",
    ),
]

#: Invariants that no index can express. Each is (label, SQL, why) where the SQL
#: must return zero rows.
REQUIRED_INVARIANTS = [
    (
        "campaign_mailings.root_campaign_id is NOT NULL",
        """SELECT 1 FROM information_schema.columns
           WHERE table_name = 'campaign_mailings'
             AND column_name = 'root_campaign_id'
             AND is_nullable = 'YES'""",
        "Postgres unique indexes treat NULLs as DISTINCT, so a nullable root "
        "makes uniq_root_campaign_contact vacuous: two rows with a null root "
        "and the same contact would both insert. A nullable root is not a "
        "weaker guarantee, it is no guarantee.",
    ),
    (
        "no mailing without a root",
        "SELECT 1 FROM campaign_mailings WHERE root_campaign_id IS NULL LIMIT 1",
        "Rows predating the backfill are unprotected by the constraint. Run "
        "migration 0011.",
    ),
    (
        "campaigns are at most two levels deep",
        """SELECT 1 FROM campaigns child
           JOIN campaigns parent ON child.parent_id = parent.id
           WHERE parent.parent_id IS NOT NULL LIMIT 1""",
        "A three-level campaign makes 'which footer applies' ambiguous and "
        "breaks the root derivation in CampaignMailing.save().",
    ),
    (
        "every root_campaign matches its campaign's root",
        """SELECT 1 FROM campaign_mailings m
           JOIN campaigns c ON m.campaign_id = c.id
           WHERE m.root_campaign_id IS DISTINCT FROM COALESCE(c.parent_id, c.id)
           LIMIT 1""",
        "A mailing rooted somewhere other than its campaign's actual root. This "
        "is what a botched reparent looks like, and the constraint will not "
        "catch it until the NEXT insert.",
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

            for label, sql, why in REQUIRED_INVARIANTS:
                cur.execute(sql)
                if cur.fetchone():
                    self.stdout.write(self.style.ERROR(f"  VIOLATED {label}"))
                    failures.append(f"{label} — {why}")
                else:
                    self.stdout.write(self.style.SUCCESS(f"  ok     {label}"))

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
