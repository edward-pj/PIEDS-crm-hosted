"""Executing queued sends, server-side.

This is `local_agent/services/schedule_runner.py` with the polling loop removed.
The laptop version woke itself every 60 seconds; a hosted free instance cannot,
because it is asleep between requests and only a request can wake it. So the
work happens *inside* a request instead, and something external decides how
often that request arrives.

That is less of a change than it sounds. `api/views.py::schedule_claim` already
ran the housekeeping on the polling path, with the comment "the housekeeping
runs as often as the thing it cleans up, with no extra process to keep alive".
Hosting only changes who polls.

**Phase 1 ships the executor; the door and the lock arrive with the scheduler
phase.** Today `tick()` is reached only by `manage.py run_tick`, which is enough
to drain the queue on a machine someone is looking at. What it deliberately does
NOT yet do -- and must, before this runs on a platform with several workers --
is take the advisory lock that stops two overlapping ticks, sweep expired leases
and missed jobs, and run the follow-up rules. Until then, run one tick at a time.

An in-process background thread was considered and rejected: `AppConfig.ready()`
runs in every gunicorn worker (so three workers means three schedulers racing),
and also under `migrate`, `collectstatic`, `shell` and pytest -- a live scheduler
inside the test suite, avoidable only by sniffing `sys.argv`. It would hold a
pooler connection permanently against a deliberate `CONN_MAX_AGE=0`. And it does
not even solve the problem: the instance sleeps and takes the thread with it, so
the thread only runs when something is already keeping the service awake.
"""

import logging
import socket
from dataclasses import dataclass, field
from datetime import timedelta

from django.utils import timezone

from crm.models import GmailCredential, TeamMember

from . import scheduling as schedule_svc
from .gmail import GmailAuthError, GmailClient
from .sending import CAP_REACHED, SENT, send_batch

log = logging.getLogger(__name__)

#: Jobs leased per member per tick.
CLAIM_LIMIT = 5

#: A tick executes inside an HTTP request, so it must finish well inside any
#: proxy timeout and must not monopolise a worker. Both are ceilings, not
#: targets: a tick that runs out of budget simply stops, and the next one
#: continues from the cursor.
TICK_MAX_MAILS = 40
TICK_MAX_SECONDS = 45

#: The most one member may take out of a single tick's budget.
#:
#: Without it the first member in the iteration order consumes the whole 40 and
#: everybody else waits for a tick they can have to themselves. At 800 mails a
#: head that is not a rounding error: it is the difference between the last
#: member's first mail going out three hours after the first member's, and
#: everyone progressing together. Four members per tick, and the rotation in
#: sendable_members() decides which four.
PER_MEMBER_TICK_MAILS = 10


@dataclass
class TickReport:
    """What one tick did. Returned as JSON so a failing pinger means something."""

    started_at: str = ""
    members: int = 0
    jobs: int = 0
    sent: int = 0
    skipped: int = 0
    errors: list = field(default_factory=list)
    stopped_early: bool = False

    def dict(self):
        return {
            "started_at": self.started_at,
            "members": self.members,
            "jobs": self.jobs,
            "sent": self.sent,
            "skipped": self.skipped,
            "errors": self.errors[:10],
            "stopped_early": self.stopped_early,
        }


def executor_id() -> str:
    """Who holds the lease. Only ever read by a human reading the CRM."""
    return f"server:{socket.gethostname()}"


def sendable_members(now=None):
    """Members the server can actually send as, rotated so nobody is always last.

    Anyone without a live Gmail grant is skipped rather than leased and failed:
    leasing a job we cannot execute burns the attempts counter and fills
    `last_error` with the same sentence every tick, which buries the real
    failures under noise.

    The rotation exists because `TeamMember.Meta.ordering` is `["name"]` and a
    tick stops at `TICK_MAX_MAILS`. Alphabetical order plus a hard budget means
    Aarav's queue drains completely before Kabir's is touched at all -- fine at
    40 mails a day, and three hours of skew at 800.

    Derived from wall-clock minutes rather than stored, so there is no column to
    migrate and nothing to get out of step across two gunicorn workers. It is
    NOT a strict round-robin: somebody connecting Gmail mid-day shifts every
    offset by one. Over an hour the distribution is even, which is all this
    needs to be.
    """
    now = now or timezone.now()
    member_ids = (
        GmailCredential.objects
        .filter(revoked_at__isnull=True)
        .values_list("member_id", flat=True)
    )
    members = list(TeamMember.objects.filter(id__in=list(member_ids), is_active=True))
    if not members:
        return members
    offset = int(now.timestamp() // 60) % len(members)
    return members[offset:] + members[:offset]


def run_job(member, job, *, gmail, budget, deadline) -> dict:
    """Send one claimed job's slice. Returns what to report back.

    Counts `attempted`, not just sent: the cursor advances by that, and a
    contact permanently skipped (unassigned, archived, do_not_contact) must move
    it too, or the job never finishes.

    `CAP_REACHED` is the exception, and getting it wrong lost mail. It is the
    only refusal that is TEMPORARY -- the daily cap is a rate limit, and the
    contact is still owed a mail once the rolling window frees. Counting it as
    an attempt marched the cursor past everyone the quota had refused and
    reported the job `done`, silently, with nothing sent to them ever. So it is
    reported separately and deliberately kept out of `attempted`.
    """
    contact_ids = job.next_slice(job.batch_size or None)
    if not contact_ids:
        return {"attempted": 0, "sent": 0, "skipped": 0, "cap_blocked": 0, "error": ""}

    sent = skipped = cap_blocked = 0
    resolved = 0
    errors: list[str] = []

    for outcome in send_batch(
        member, job.campaign, contact_ids,
        cc=job.cc, bcc=job.bcc,
        gmail=gmail, max_mails=budget, deadline=deadline,
    ):
        if outcome.status == CAP_REACHED:
            cap_blocked += 1
            continue
        resolved += 1
        if outcome.status == SENT:
            sent += 1
        else:
            skipped += 1
            if outcome.detail:
                errors.append(f"{outcome.email}: {outcome.detail}")

    if cap_blocked:
        # First, not appended: it explains why the job stopped, which is what
        # somebody reading the schedule page needs before any individual error.
        errors.insert(0, (
            f"paused on the daily send cap; {cap_blocked} contact(s) still "
            f"queued and will go out once the 24-hour window frees"
        ))

    return {
        # `resolved`, not `len(contact_ids)`: a bounded run may stop partway
        # through the slice, and advancing the cursor past contacts nobody
        # attempted would silently drop them from the job forever.
        "attempted": resolved,
        "sent": sent,
        "skipped": skipped,
        "cap_blocked": cap_blocked,
        # Only a sample: a batch of 200 bad addresses should not post 200 lines
        # of prose into a text column someone has to read.
        "error": "; ".join(errors[:5])[:2000],
    }


def run_for_member(member, report, *, deadline, now=None, gmail=None) -> None:
    """Drain what this member has due, within the tick's budget.

    `now` is the tick's own moment, threaded through rather than re-read here.
    One tick is one instant: claim_due's due-date and back-off arithmetic must
    agree with the deadline this run is being held to, and with what the other
    members in the same tick saw.
    """
    now = now or timezone.now()
    try:
        client = GmailClient(member) if gmail is None else gmail
        # Proves the token really belongs to this member before anything is
        # leased. Cached on the row, so this is free after the first call.
        client.verify_identity()
    except GmailAuthError as exc:
        # Not an error worth shouting about every tick: the member has to
        # reconnect, and the CRM already tells them so on their own page.
        log.info("skipping %s: %s", member.bits_email, exc)
        return

    jobs = schedule_svc.claim_due(
        member, agent_id=executor_id(), limit=CLAIM_LIMIT, now=now
    )
    mine = 0
    for job in jobs:
        report.jobs += 1
        # Whichever runs out first: the tick's budget or this member's share of
        # it. The second is what stops one queue starving the rest.
        remaining = min(
            TICK_MAX_MAILS - report.sent,
            PER_MEMBER_TICK_MAILS - mine,
        )

        try:
            result = run_job(
                member, job, gmail=client, budget=max(0, remaining), deadline=deadline
            )
        except Exception as exc:                                # noqa: BLE001
            # A job-level failure: the credential died mid-batch, the database
            # went away. Individual bad mails never reach here -- send_batch
            # isolates those and reports them per contact.
            log.exception("scheduled send %s failed", job.id)
            detail = f"{type(exc).__name__}: {exc}"
            report.errors.append(f"job {job.id}: {detail}")
            schedule_svc.mark_failed(job.id, member, detail)
            continue

        report.sent += result["sent"]
        report.skipped += result["skipped"]
        mine += result["sent"]
        if result["error"]:
            report.errors.append(f"job {job.id}: {result['error']}")

        schedule_svc.record_progress(
            job.id, member,
            attempted=result["attempted"], sent=result["sent"],
            skipped=result["skipped"], cap_blocked=result["cap_blocked"],
            error=result["error"], now=now,
        )

        if timezone.now() >= deadline or report.sent >= TICK_MAX_MAILS:
            report.stopped_early = True
            return
        if mine >= PER_MEMBER_TICK_MAILS:
            # This member has had their share. Not stopped_early -- the tick
            # itself is fine and moves on to the next member.
            return


def tick(*, now=None, max_seconds=TICK_MAX_SECONDS, gmail_for=None) -> TickReport:
    """One pass over everything that is due.

    Every failure mode is per-member and per-job: one broken credential must not
    stop the others, and must not stop the next tick either.

    `gmail_for` is a hook for tests -- a callable taking a member and returning
    a client. Production passes nothing.
    """
    now = now or timezone.now()
    deadline = now + timedelta(seconds=max_seconds)
    report = TickReport(started_at=now.isoformat())

    for member in sendable_members(now):
        if timezone.now() >= deadline or report.sent >= TICK_MAX_MAILS:
            report.stopped_early = True
            break

        report.members += 1
        try:
            run_for_member(
                member, report, deadline=deadline, now=now,
                gmail=gmail_for(member) if gmail_for else None,
            )
        except Exception as exc:                                # noqa: BLE001
            log.exception("tick failed for %s", member.bits_email)
            report.errors.append(f"{member.bits_email}: {type(exc).__name__}: {exc}")

    return report
