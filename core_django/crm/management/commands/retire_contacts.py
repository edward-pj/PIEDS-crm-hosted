"""Archive -- or, if you insist, destroy -- a batch of contacts matched by filter.

Written for the case it is named after: a test import. `test_contacts_300.csv`
puts 300 rows in the pool addressed to plus-tagged variants of one member's own
mailbox, they get mailed, and then there is no way to get them out again. The
contact page has an Archive button and a Delete button, both one contact at a
time; the bulk-edit bar can set a company or a tag but not this. Three hundred
clicks is not a route, so people leave the test data in the pool instead, and it
shows up in every list and every count from then on.

Two modes, and the difference matters:

  ARCHIVE (default) is reversible and loses nothing. `is_archived` drops the
  contact out of every list -- views.py filters on it -- and `claim_batch`
  re-checks it under a row lock, so an archived contact cannot be mailed even
  by a job that was queued before the archive happened. The send history stays.
  This is what the model was built for; the field's own comment says so.

  --delete is not reversible and does lose something. CampaignMailing.contact
  is PROTECT, so a contact that has ever been mailed cannot leave the table
  while its mailing rows exist -- and those rows ARE the record of what the
  team sent to whom. Deleting them also gives up team-wide dedupe for those
  addresses: uniq_root_campaign_contact is enforced by the presence of the row,
  so once it is gone the same prospect can be mailed again under any campaign.
  For invented addresses that only ever received test mail that is a fair
  trade. For a real prospect it is destroying evidence, and the confirmation
  prompt exists to make you say which of the two this is.

Nothing writes without --apply. The dry run is the default because a selector
is easy to get slightly wrong, and a slightly wrong selector here is the
difference between 300 fixtures and somebody's live prospect list.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Q

from crm.models import CampaignMailing, Contact, ScheduledSend, TeamMember
from crm.services import contacts as contact_svc
from crm.services.permissions import is_lead
from shared.enums import ScheduleStatus

#: A job in one of these states may still be executed, and claim_batch resolves
#: its contact ids with `Contact.objects.get(id=...)` -- an unguarded get. Delete
#: a contact whose id is sitting in one of these arrays and the next tick raises
#: Contact.DoesNotExist mid-batch. Terminal jobs are only history and are fine.
LIVE_SCHEDULE_STATES = (
    ScheduleStatus.PENDING.value,
    ScheduleStatus.RUNNING.value,
    ScheduleStatus.HELD.value,
)

#: Shown before anything is written. Enough rows to recognise the batch, not so
#: many that the confirmation scrolls off the top of the terminal.
PREVIEW_ROWS = 12


class Command(BaseCommand):
    help = (
        "Archive (default) or permanently delete contacts matched by email prefix, "
        "tag or company. Dry run unless --apply is given."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--email-prefix",
            help="Match on the start of the address, e.g. f20250882+t",
        )
        parser.add_argument("--tag", help="Match contacts carrying this tag.")
        parser.add_argument("--company", help="Match this company exactly (case-insensitive).")
        parser.add_argument(
            "--as",
            dest="actor_email",
            help="BITS address of the lead this runs as. Required to archive: it "
                 "is who the audit trail names.",
        )
        parser.add_argument(
            "--delete",
            action="store_true",
            help="Destroy the rows instead of archiving them. Also destroys their "
                 "mail history, which is what makes them deletable at all.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write. Without this the command only reports.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip the typed confirmation --delete --apply otherwise requires.",
        )

    def handle(self, *args, **opts):
        qs = self._select(opts)
        total = qs.count()

        if not total:
            self.stdout.write(self.style.WARNING(
                "Nothing matched. Nothing was written.\n"
                "Check the selector against a contact you can see in the UI -- "
                "--email-prefix matches the START of the address, not any part of it."
            ))
            return

        self._report(qs, total, opts)

        if not opts["apply"]:
            self.stdout.write(self.style.MIGRATE_HEADING(
                "\nDry run. Nothing was written."
            ))
            self.stdout.write(
                "Re-run with --apply once the list above is the list you meant."
            )
            return

        if opts["delete"]:
            self._blocked_by_live_schedules(qs)
            if not opts["yes"]:
                self._confirm(total)
            self._delete(qs, total)
        else:
            self._archive(qs, total, self._actor(opts))

    # --- selection --------------------------------------------------------

    def _select(self, opts):
        """Build the queryset, refusing to run without a selector.

        No selector must never mean "everything". A bare `retire_contacts
        --apply --delete` that fell through to an unfiltered queryset would
        empty the contact pool, and the one keystroke that produces it is
        forgetting the argument.
        """
        filters = Q()
        used = []

        if opts["email_prefix"]:
            prefix = opts["email_prefix"]
            filters &= Q(email__istartswith=prefix)
            used.append(f"email starts with {prefix!r}")
        if opts["tag"]:
            filters &= Q(tags__contains=[opts["tag"]])
            used.append(f"tagged {opts['tag']!r}")
        if opts["company"]:
            filters &= Q(company__iexact=opts["company"])
            used.append(f"company is {opts['company']!r}")

        if not used:
            raise CommandError(
                "Refusing to run with no selector -- that would match every "
                "contact in the pool.\n"
                "Give at least one of --email-prefix, --tag, --company."
            )

        self.stdout.write(self.style.MIGRATE_HEADING("Selector"))
        self.stdout.write("  " + "\n  AND ".join(used) + "\n")
        # Multiple selectors are ANDed on purpose. Two independent facts about
        # the same import agreeing (a plus-tagged address AND the tag the CSV
        # set) is a far better guarantee than either alone.
        return Contact.objects.filter(filters).order_by("email")

    def _actor(self, opts):
        """Resolve --as to a lead, and insist on one.

        `set_archived` runs a real permission check -- `can_edit_contact` --
        which a null actor fails, and rightly: an archive is an edit and the
        audit row it writes is meant to answer "who took these out of the
        pool?". A shell run with nobody's name on it answers that with a blank.
        Deleting takes no actor because it leaves no audit row to sign; there is
        no contact left to hang one on.
        """
        email = opts["actor_email"]
        if not email:
            leads = [
                m.bits_email for m in TeamMember.objects.filter(is_active=True)
                if is_lead(m)
            ]
            raise CommandError(
                "Archiving needs --as <lead-bits-email>: the audit row records "
                "who did it,\nand a run with no name on it records nobody.\n"
                "Leads on this database: " + (", ".join(sorted(leads)) or "none found")
            )

        member = TeamMember.objects.filter(bits_email__iexact=email).first()
        if not member:
            raise CommandError(f"No team member with the address {email!r}.")
        if not is_lead(member):
            raise CommandError(
                f"{member.name} is not a lead, and only a lead may archive a "
                f"contact that is not assigned to them."
            )
        return member

    # --- reporting --------------------------------------------------------

    def _report(self, qs, total, opts):
        rows = qs.annotate(n_mailings=Count("mailings"))
        preview = list(rows[:PREVIEW_ROWS])
        mailed = rows.filter(n_mailings__gt=0).count()
        archived = qs.filter(is_archived=True).count()

        verb = "DELETE" if opts["delete"] else "ARCHIVE"
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"{total} contact(s) matched, and would be {verb}D"
        ))

        self.stdout.write(f"\n{'email':52} {'name':22} {'company':22} {'mails':>5}")
        for c in preview:
            self.stdout.write(
                f"{c.email[:52]:52} {c.full_name[:22]:22} "
                f"{c.company[:22]:22} {c.n_mailings:>5}"
            )
        if total > PREVIEW_ROWS:
            self.stdout.write(f"... and {total - PREVIEW_ROWS} more")

        self.stdout.write("")
        self.stdout.write(f"  already archived : {archived}")
        self.stdout.write(f"  with mail history: {mailed}")

        if opts["delete"] and mailed:
            n = CampaignMailing.objects.filter(contact__in=qs).count()
            self.stdout.write(self.style.WARNING(
                f"\n  {n} CampaignMailing row(s) will be destroyed to make the "
                f"delete possible.\n"
                f"  That is the record of what was sent to these addresses, and "
                f"it also stops\n  being the thing that prevents them being "
                f"mailed twice."
            ))
        elif not opts["delete"] and mailed:
            self.stdout.write(
                "\n  Archiving keeps every one of those mail records. "
                "Nothing is lost here."
            )

    # --- guards -----------------------------------------------------------

    def _blocked_by_live_schedules(self, qs):
        """Refuse to delete a contact some queued job is still going to look up.

        claim_batch reads its selection with an unguarded
        `Contact.objects.select_for_update().get(id=contact_id)`. A deleted id
        left in a PENDING job's `contact_ids` array therefore does not skip --
        it raises, and takes the whole tick down with it. Archiving has no such
        problem: the row is still there, and the archive check skips it politely.
        """
        ids = set(qs.values_list("id", flat=True))
        clashes = []
        for job in ScheduledSend.objects.filter(status__in=LIVE_SCHEDULE_STATES):
            hits = ids.intersection(job.contact_ids or [])
            if hits:
                clashes.append((job, len(hits)))

        if not clashes:
            return

        lines = "\n".join(
            f"  job {job.id} ({job.status}) for {job.member.name}: {n} of them"
            for job, n in clashes
        )
        raise CommandError(
            "Refusing to delete: these contacts are still named in scheduled "
            "sends that have not finished.\n"
            f"{lines}\n\n"
            "Deleting them would leave dangling ids in those jobs, and the next "
            "tick would crash\nlooking one up rather than skipping it. Cancel "
            "the jobs first, or archive instead --\narchiving is safe with a job "
            "in flight, because the row survives to be skipped."
        )

    def _confirm(self, total):
        """A typed confirmation, not a y/n.

        The prompt asks for the count because that is the number the operator
        should have checked. Typing it back means having read the report.
        """
        self.stdout.write(self.style.WARNING(
            f"\nThis permanently destroys {total} contact(s) and their mail "
            f"history. It cannot be undone."
        ))
        answer = input(f"Type the number {total} to proceed, anything else to stop: ")
        if answer.strip() != str(total):
            raise CommandError("Not confirmed. Nothing was written.")

    # --- writes -----------------------------------------------------------

    def _archive(self, qs, total, actor):
        """One `set_archived` per contact rather than a bulk `update`.

        A bulk update would be one query instead of six hundred, and would skip
        the ContactAudit row that records who took the contact out of the pool
        and when. Six hundred queries once, against a permanent gap in the audit
        trail every time somebody wonders where the contacts went, is not a
        close call.
        """
        done = 0
        with transaction.atomic():
            for contact in qs.iterator():
                contact_svc.set_archived(contact, actor, archived=True)
                done += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nArchived {done} of {total} contact(s) as {actor.name}."
        ))
        self.stdout.write(
            "They are out of every list and cannot be mailed again. Their send "
            "history is intact.\nTo bring them back: tick 'archived' in the "
            "contact filters and use Restore, or re-run\nthis with the same "
            "selector once it grows an --unarchive flag."
        )

    def _delete(self, qs, total):
        ids = list(qs.values_list("id", flat=True))
        with transaction.atomic():
            # The PROTECT is on CampaignMailing.contact, so the mailings go
            # first or the delete below raises ProtectedError. Notes and audits
            # are CASCADE and need no help.
            n_mailings, _ = CampaignMailing.objects.filter(contact_id__in=ids).delete()
            Contact.objects.filter(id__in=ids).delete()

        self.stdout.write(self.style.SUCCESS(
            f"\nDeleted {total} contact(s) and {n_mailings} mailing-related row(s)."
        ))
