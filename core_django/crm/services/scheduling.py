"""Scheduled sends: when a campaign goes out, and who executes it.

This module answers exactly two questions -- *when* may a job run, and *which
agent* may run it. It never touches how a mail is built; that stays in
render.py, and is resolved at execution time against whatever the campaign and
the contact say then. Scheduling a send is not a way to freeze a template.

The reason any of this exists: the Gmail API has no `sendAt`. A future send
needs a process awake at that moment holding the member's Gmail token, and the
CRM deliberately holds no Gmail credentials. So the server keeps the queue and
the rules, and an agent asks "anything due for me?" -- see docs/MAIL_SCHEDULING.md.

Every function that cares about time takes `now` as a parameter. Quiet hours, a
six-hour grace window and drip intervals are otherwise untestable without
actually waiting six hours.
"""

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from crm.models import Campaign, ScheduledSend
from shared.enums import TERMINAL_SCHEDULE_STATUSES, CampaignStatus, ScheduleStatus

from .mailing import InvalidCopyAddresses, parse_copy_addresses

#: How long an agent may hold a job before another may take it. Long enough for
#: a slow batch to finish a slice, short enough that a laptop closing its lid
#: does not strand a send until someone notices.
LEASE_MINUTES = 5

#: How long to wait before re-leasing a job that stopped on its owner's daily
#: send cap. Long enough that the scheduler stops picking up a job it cannot
#: advance; short enough that the queue resumes promptly once quota frees.
CAP_RETRY_MINUTES = 30

#: Refuse a schedule set for the past. A little slack absorbs clock skew between
#: a laptop and the server without letting anyone schedule yesterday.
PAST_TOLERANCE_SECONDS = 60


class NotSchedulable(Exception):
    """The job cannot be created or run as asked. Message is user-facing."""


def remaining_quota(member) -> int:
    """How many more mails this member may send in the next 24h.

    Read from mailing.py rather than re-derived: one definition of the cap, and
    it already counts across every device the member uses.
    """
    from .mailing import DAILY_SEND_CAP, sent_last_24h
    return DAILY_SEND_CAP - sent_last_24h(member)


# ------------------------------------------------------------------ creating

def create(*, campaign_id, member, contact_ids, scheduled_at, cc="", bcc="",
           batch_size=0, interval_minutes=0, created_by=None, now=None) -> ScheduledSend:
    """Queue a send. Validates everything that can be known up front.

    Deliberately NOT validated here: whether each contact is still assigned,
    unarchived and mailable. That is re-checked under a row lock at execution
    time by claim_batch, and a contact can change hands between now and then --
    so checking twice would only produce a reassuring answer with no shelf life.
    """
    now = now or timezone.now()

    if not contact_ids:
        raise NotSchedulable("Select at least one contact to schedule.")

    if scheduled_at is None:
        raise NotSchedulable("A scheduled send needs a date and time.")
    if timezone.is_naive(scheduled_at):
        scheduled_at = timezone.make_aware(scheduled_at)
    if scheduled_at < now - timedelta(seconds=PAST_TOLERANCE_SECONDS):
        raise NotSchedulable("That time has already passed. Pick a future time.")

    try:
        campaign = Campaign.objects.get(id=campaign_id)
    except (Campaign.DoesNotExist, DjangoValidationError, ValueError, TypeError):
        raise NotSchedulable("No such campaign.")

    # Checked now for a clear error, and AGAIN at execution: a campaign paused
    # between scheduling and sending must not go out. See is_runnable().
    if campaign.status != CampaignStatus.ACTIVE.value:
        raise NotSchedulable(
            f"Campaign {campaign.title!r} is {campaign.status}; only "
            f"{CampaignStatus.ACTIVE.value} campaigns can be scheduled."
        )

    try:
        cc = parse_copy_addresses(cc)
        bcc = parse_copy_addresses(bcc)
    except InvalidCopyAddresses as exc:
        raise NotSchedulable(str(exc))

    # De-duplicate while preserving the order the operator chose; the cursor
    # walks this list, so a repeated id would just burn a tick.
    seen, ordered = set(), []
    for cid in contact_ids:
        key = str(cid)
        if key not in seen:
            seen.add(key)
            ordered.append(key)

    batch_size = max(0, int(batch_size or 0))
    interval_minutes = max(0, int(interval_minutes or 0))
    if batch_size and not interval_minutes:
        # A batch size with no interval would drain the whole list on
        # consecutive ticks, which is the opposite of dripping.
        raise NotSchedulable("A drip needs an interval between batches.")

    return ScheduledSend.objects.create(
        campaign=campaign,
        member=member,
        created_by=created_by or member,
        contact_ids=ordered,
        scheduled_at=scheduled_at,
        cc=cc,
        bcc=bcc,
        batch_size=batch_size,
        interval_minutes=interval_minutes,
        status=ScheduleStatus.PENDING.value,
    )


# ------------------------------------------------------------------- timing

def _window():
    # Fallbacks match settings.py: 0 == 0 means no window, send whenever.
    return (
        int(getattr(settings, "SCHEDULE_WINDOW_START", 0)),
        int(getattr(settings, "SCHEDULE_WINDOW_END", 0)),
        set(getattr(settings, "SCHEDULE_WINDOW_DAYS", range(7))),
    )


def grace_hours() -> int:
    return int(getattr(settings, "SCHEDULE_GRACE_HOURS", 6))


def in_window(moment) -> bool:
    """Is `moment` inside the hours we are willing to mail people?

    Evaluated in the project timezone, not UTC: the window means "9am where the
    recipient reads it", and the stored value is UTC.
    """
    start, end, days = _window()
    if start == end:
        return True                                  # window disabled

    local = timezone.localtime(moment)
    if local.weekday() not in days:
        return False
    return start <= local.hour < end


def next_open_slot(moment):
    """The first moment from `moment` onwards that we are willing to send.

    Walks forward a day at a time rather than doing calendar arithmetic in one
    expression: a closed weekend plus a window that starts tomorrow is fiddly
    enough that the obvious loop is the honest implementation. Bounded at 14
    days so a misconfigured empty window cannot spin.
    """
    start, end, days = _window()
    if start == end:
        return moment
    if in_window(moment):
        return moment

    local = timezone.localtime(moment)
    for _ in range(14):
        if local.weekday() in days and local.hour < start:
            candidate = local.replace(hour=start, minute=0, second=0, microsecond=0)
        else:
            # After the window closed, or a closed day: try the start of the next.
            nxt = local + timedelta(days=1)
            candidate = nxt.replace(hour=start, minute=0, second=0, microsecond=0)
        if candidate.weekday() in days:
            return candidate.astimezone(moment.tzinfo)
        local = candidate
    return moment                                    # window is never open; do not stall


def deliver_after(job, now=None):
    """The earliest moment this job's NEXT slice is allowed to go out.

    Usually the scheduled time. When that lands outside the sending window it
    is pushed to the next open slot -- and the grace window is measured from
    THIS value rather than from scheduled_at, so a job deferred overnight is not
    declared missed for a lateness it was never permitted to avoid.

    A drip in progress uses next_run_at instead, so its deadline moves with it:
    a job spreading 200 mails over two days is not "six hours late" on day two.
    """
    return next_open_slot(job.next_run_at or job.scheduled_at)


def deadline(job, hours=None, now=None):
    hours = grace_hours() if hours is None else hours
    return deliver_after(job, now) + timedelta(hours=hours)


def is_runnable(job, now=None) -> tuple[bool, str]:
    """May this job execute right now? Returns (yes, human reason if not).

    Re-checks the campaign state on every tick, not just at creation. Pausing a
    campaign is the documented emergency brake for the whole system; it has to
    stop a send that was queued before someone pulled it.
    """
    now = now or timezone.now()

    if job.status in TERMINAL_SCHEDULE_STATUSES:
        return False, f"already {job.status}"

    allowed_from = deliver_after(job, now)
    if now < allowed_from:
        # Distinguish "you scheduled it for later" from "we moved it": the
        # second is the system overruling the operator, and they should be able
        # to read that off the schedule page rather than infer it.
        if allowed_from > job.scheduled_at:
            start, end, _ = _window()
            return False, (
                f"outside the sending window ({start:02d}:00-{end:02d}:00); "
                f"held until {timezone.localtime(allowed_from):%d %b %H:%M}"
            )
        return False, "not due yet"
    if not in_window(now):
        start, end, _ = _window()
        return False, f"outside the sending window ({start:02d}:00-{end:02d}:00)"
    if job.campaign.status != CampaignStatus.ACTIVE.value:
        return False, f"campaign is {job.campaign.status}"
    # The root as well. Pausing "Ignite" has to stop every member's
    # sub-campaign, or the emergency brake stops one queue out of fifteen.
    root = job.campaign.parent
    if root is not None and root.status != CampaignStatus.ACTIVE.value:
        return False, f"campaign {root.title!r} is {root.status}"
    return True, ""


def is_missed(job, now=None) -> bool:
    """Past the point where sending this would do more harm than good."""
    now = now or timezone.now()
    if job.status in TERMINAL_SCHEDULE_STATUSES:
        return False
    return now > deadline(job, now=now)


@transaction.atomic
def sweep_missed(now=None) -> int:
    """Mark jobs nothing executed in time.

    Deliberately a state change and not a silent drop: "we did not send this"
    is information somebody needs, and the CRM's schedule page leads with it.
    """
    now = now or timezone.now()
    stale = (
        ScheduledSend.objects
        .exclude(status__in=TERMINAL_SCHEDULE_STATUSES)
        .exclude(status=ScheduleStatus.RUNNING.value)
        .select_related("campaign")
    )
    marked = 0
    for job in stale:
        if not is_missed(job, now):
            continue
        job.status = ScheduleStatus.MISSED.value
        job.finished_at = now
        job.last_error = (
            f"nothing executed this within {grace_hours()}h of "
            f"{timezone.localtime(deliver_after(job, now)):%d %b %H:%M}"
        )
        job.save(update_fields=["status", "finished_at", "last_error", "updated_at"])
        marked += 1
    return marked


# ------------------------------------------------------------------ claiming

@transaction.atomic
def claim_due(member, agent_id="", limit=5, now=None) -> list[ScheduledSend]:
    """Lease this member's due jobs for one agent to execute.

    `select_for_update(skip_locked=True)`: a second agent polling the same
    instant sees nothing rather than blocking behind us. Filtering on `member`
    is not an optimisation -- an agent authenticates as exactly one member and
    may only ever send from that mailbox.

    The lease is a scheduling convenience, NOT the safety mechanism. Even two
    agents running the same job cannot double-mail anyone: uniq_campaign_contact
    still stands between them and a prospect's inbox.
    """
    now = now or timezone.now()

    candidates = (
        ScheduledSend.objects
        # `of=("self",)` locks only scheduled_sends. Two reasons, and the
        # second is not optional: we have no business locking campaign rows
        # here, and `campaign__parent` is nullable, so select_related makes it a
        # LEFT OUTER JOIN -- and Postgres refuses FOR UPDATE across one.
        .select_for_update(skip_locked=True, of=("self",))
        .select_related("campaign", "campaign__parent")
        .filter(
            member=member,
            status__in=[ScheduleStatus.PENDING.value, ScheduleStatus.HELD.value],
            scheduled_at__lte=now,
        )
        # A drip mid-flight waits for its interval; everything else is eligible
        # the moment it is due.
        .filter(Q(next_run_at__isnull=True) | Q(next_run_at__lte=now))
        .order_by("scheduled_at")[:limit]
    )

    # The daily cap is enforced by claim_batch per contact, but a scheduler that
    # ignores it would lease job after job only to have every mail refused, burn
    # the attempts counter, and look like a failure rather than a quota.
    if remaining_quota(member) <= 0:
        # Push the due jobs out rather than merely walking away. deliver_after()
        # reads next_run_at, so a job left untouched here keeps a deadline
        # measured from a moment we ourselves refused to let it run -- and the
        # missed-sweep declares a healthy queue MISSED for waiting on our own
        # quota. Moving the clock is what makes "wait for the window" honest.
        ScheduledSend.objects.filter(id__in=[job.id for job in candidates]).update(
            next_run_at=now + timedelta(minutes=CAP_RETRY_MINUTES), updated_at=now
        )
        return []

    claimed = []
    for job in candidates:
        ok, reason = is_runnable(job, now)
        if not ok:
            # Due but not allowed: park it as HELD and say why. The next tick
            # re-evaluates -- an unpaused campaign resumes on its own.
            if job.status != ScheduleStatus.HELD.value:
                job.status = ScheduleStatus.HELD.value
            job.last_error = reason
            job.save(update_fields=["status", "last_error", "updated_at"])
            continue

        job.status = ScheduleStatus.RUNNING.value
        job.leased_by = agent_id[:80]
        job.lease_expires_at = now + timedelta(minutes=LEASE_MINUTES)
        job.attempts += 1
        job.last_error = ""
        if job.started_at is None:
            job.started_at = now
        job.save(update_fields=[
            "status", "leased_by", "lease_expires_at", "attempts",
            "last_error", "started_at", "updated_at",
        ])
        claimed.append(job)

    return claimed


@transaction.atomic
def heartbeat(job_id, member, now=None) -> bool:
    """Extend a lease while a long batch is still running."""
    now = now or timezone.now()
    updated = ScheduledSend.objects.filter(
        id=job_id, member=member, status=ScheduleStatus.RUNNING.value
    ).update(
        lease_expires_at=now + timedelta(minutes=LEASE_MINUTES), updated_at=now
    )
    return bool(updated)


@transaction.atomic
def sweep_expired_leases(now=None) -> int:
    """Return jobs whose executor died mid-batch to the queue.

    Same reasoning as stranded_drafts in mailing.py: a crash must leave state
    that is recoverable and obvious, never a job silently lost. Anything already
    sent stays sent -- the cursor and the unique constraint see to that.
    """
    now = now or timezone.now()
    return ScheduledSend.objects.filter(
        status=ScheduleStatus.RUNNING.value, lease_expires_at__lt=now
    ).update(
        status=ScheduleStatus.PENDING.value,
        leased_by="",
        lease_expires_at=None,
        last_error="executor stopped mid-batch; requeued",
        updated_at=now,
    )


# ------------------------------------------------------------------ progress

@transaction.atomic
def record_progress(job_id, member, *, attempted, sent, skipped, error="",
                    cap_blocked=0, now=None) -> dict:
    """Advance the cursor after an executor has sent a slice.

    `attempted` is how many contacts the executor got through, sent or not. The
    cursor advances by that, NOT by `sent`: a contact who was skipped for good
    (unassigned, archived, do_not_contact) must not be retried forever.

    `cap_blocked` is counted separately and is NOT part of `attempted`, because
    the daily cap is a rate limit rather than a verdict -- see runner.run_job.
    Those contacts keep their place in the queue.
    """
    now = now or timezone.now()

    try:
        job = ScheduledSend.objects.select_for_update().select_related("campaign").get(
            id=job_id, member=member
        )
    except (ScheduledSend.DoesNotExist, DjangoValidationError, ValueError, TypeError):
        return {"status": "unknown", "detail": "no such scheduled send"}

    # A human cancelled it while the batch was in flight. Honour that and stop.
    if job.status == ScheduleStatus.CANCELLED.value:
        return {"status": job.status, "detail": "cancelled mid-flight; stopping"}

    job.cursor = min(job.total, job.cursor + max(0, int(attempted)))
    job.sent_count += max(0, int(sent))
    job.skipped_count += max(0, int(skipped))
    job.last_error = (error or "")[:2000]

    if job.cursor >= job.total:
        job.status = ScheduleStatus.DONE.value
        job.finished_at = now
        job.leased_by = ""
        job.lease_expires_at = None
        job.next_run_at = None
    else:
        # More to do on a later tick. Back to PENDING so any agent of this
        # member's can pick up the next slice.
        job.status = ScheduleStatus.PENDING.value
        job.lease_expires_at = None
        job.leased_by = ""
        if job.interval_minutes:
            job.next_run_at = now + timedelta(minutes=job.interval_minutes)
        elif cap_blocked:
            # Back off, and reuse the drip's own field to do it. Two things fall
            # out of that, and the second is the one that matters: claim_due
            # stops leasing a job it cannot advance, AND deliver_after() reads
            # next_run_at -- so the missed-sweep's clock moves with the job.
            # Without this, a queue longer than one day's quota is declared
            # MISSED for a lateness the cap made unavoidable, which is exactly
            # the shape of the bug this whole change exists to fix.
            job.next_run_at = now + timedelta(minutes=CAP_RETRY_MINUTES)

    job.save()
    return {"status": job.status, "cursor": job.cursor, "remaining": job.remaining}


@transaction.atomic
def mark_failed(job_id, member, error, now=None) -> None:
    """The job itself broke -- not one bad mail, which send_batch absorbs."""
    now = now or timezone.now()
    ScheduledSend.objects.filter(id=job_id, member=member).exclude(
        status__in=TERMINAL_SCHEDULE_STATUSES
    ).update(
        status=ScheduleStatus.FAILED.value,
        last_error=(error or "unknown error")[:2000],
        finished_at=now,
        leased_by="",
        lease_expires_at=None,
        updated_at=now,
    )


# --------------------------------------------------------------- human edits

@transaction.atomic
def cancel(job_id, *, member=None, actor=None) -> ScheduledSend:
    """Call a job off.

    A RUNNING job is cancelled too: record_progress sees the status on its next
    report and stops before the NEXT contact. Mail already sent stays sent and
    its CampaignMailing rows stand -- there is no unsending, and pretending
    otherwise in the UI would be a lie.
    """
    qs = ScheduledSend.objects.select_for_update()
    if member is not None:
        qs = qs.filter(member=member)
    try:
        job = qs.get(id=job_id)
    except (ScheduledSend.DoesNotExist, DjangoValidationError, ValueError, TypeError):
        raise NotSchedulable("No such scheduled send.")

    if job.status in TERMINAL_SCHEDULE_STATUSES:
        raise NotSchedulable(f"That send is already {job.status}.")

    job.status = ScheduleStatus.CANCELLED.value
    job.finished_at = timezone.now()
    job.leased_by = ""
    job.lease_expires_at = None
    job.save(update_fields=[
        "status", "finished_at", "leased_by", "lease_expires_at", "updated_at",
    ])
    return job


@transaction.atomic
def reschedule(job_id, new_time, *, member=None, now=None) -> ScheduledSend:
    now = now or timezone.now()

    qs = ScheduledSend.objects.select_for_update()
    if member is not None:
        qs = qs.filter(member=member)
    try:
        job = qs.get(id=job_id)
    except (ScheduledSend.DoesNotExist, DjangoValidationError, ValueError, TypeError):
        raise NotSchedulable("No such scheduled send.")

    if job.status in TERMINAL_SCHEDULE_STATUSES:
        raise NotSchedulable(f"That send is already {job.status}; create a new one.")
    if job.status == ScheduleStatus.RUNNING.value:
        raise NotSchedulable("That send is going out right now; cancel it instead.")

    if timezone.is_naive(new_time):
        new_time = timezone.make_aware(new_time)
    if new_time < now - timedelta(seconds=PAST_TOLERANCE_SECONDS):
        raise NotSchedulable("That time has already passed. Pick a future time.")

    job.scheduled_at = new_time
    # A held job gets a fresh chance: whatever blocked it may be long resolved.
    job.status = ScheduleStatus.PENDING.value
    job.last_error = ""
    job.save(update_fields=["scheduled_at", "status", "last_error", "updated_at"])
    return job


def as_json(job) -> dict:
    """Shared shape for the API and both UIs."""
    return {
        "id": str(job.id),
        "campaign_id": str(job.campaign_id),
        "campaign": job.campaign.title,
        "member": job.member.name,
        "scheduled_at": job.scheduled_at.isoformat(),
        "status": job.status,
        "total": job.total,
        "cursor": job.cursor,
        "remaining": job.remaining,
        "sent_count": job.sent_count,
        "skipped_count": job.skipped_count,
        "cc": job.cc,
        "bcc": job.bcc,
        "batch_size": job.batch_size,
        "interval_minutes": job.interval_minutes,
        "next_run_at": job.next_run_at.isoformat() if job.next_run_at else None,
        "last_error": job.last_error,
        "created_at": job.created_at.isoformat(),
    }
