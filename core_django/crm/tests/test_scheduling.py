"""Scheduled sends: the queue, the lease, and the rules around them.

The load-bearing tests here are `test_a_leased_job_is_invisible_to_a_second_agent`
and `test_a_job_never_crosses_to_another_member`. The first is what stops two
agents racing through the same batch; the second is what stops a mail leaving
the wrong mailbox with a false sent_by.

Nothing here sleeps. Every timing rule takes `now` as a parameter precisely so
a six-hour grace window is a six-line test.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from crm.models import (
    Campaign,
    CampaignMailing,
    Contact,
    GmailCredential,
    ScheduledSend,
    TeamMember,
)
from crm.services import gmail as gmail_svc
from crm.services import runner
from crm.services import scheduling as svc
from crm.services import secrets as token_store
from shared.enums import CampaignStatus, ContactLifecycle, MailingStatus, ScheduleStatus

from .conftest import make_lead

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _window_off_by_default(settings):
    """Most tests here are about the queue, not the clock.

    Leaving the real 09:00-19:00 default in place would make them pass or fail
    depending on what time of day the suite happens to run -- which is exactly
    the kind of test that erodes trust in a suite. Tests that are ABOUT the
    window ask for the `window` fixture and get it back.
    """
    settings.SCHEDULE_WINDOW_START = 0
    settings.SCHEDULE_WINDOW_END = 0
    settings.SCHEDULE_WINDOW_DAYS = [0, 1, 2, 3, 4, 5, 6]
    settings.SCHEDULE_GRACE_HOURS = 6


@pytest.fixture
def window(settings):
    """The real defaults: 09:00-19:00 every day, six hours of grace."""
    settings.SCHEDULE_WINDOW_START = 9
    settings.SCHEDULE_WINDOW_END = 19
    settings.SCHEDULE_WINDOW_DAYS = [0, 1, 2, 3, 4, 5, 6]
    settings.SCHEDULE_GRACE_HOURS = 6
    return settings


@pytest.fixture
def member():
    return TeamMember.objects.create(
        name="Kabir", bits_email="kabir@pilani.bits-pilani.ac.in", batch="2025"
    )


@pytest.fixture
def other_member():
    return TeamMember.objects.create(
        name="Ishita", bits_email="ishita@pilani.bits-pilani.ac.in", batch="2025"
    )


@pytest.fixture
def campaign(member):
    return Campaign.objects.create(
        title="Outreach",
        mail_sub="Hello {{ first_name }}",
        mail_body="Hi {{ first_name }}.",
        var_list=["first_name"],
        status=CampaignStatus.ACTIVE.value,
        created_by=member,
    )


def make_contact(member, n=0, **kwargs):
    fields = dict(
        first_name=f"Rohan{n}", last_name="Iyer", email=f"rohan{n}@example.com",
        company="Acme", designation="CTO", assigned_to=member, created_by=member,
    )
    fields.update(kwargs)
    return Contact.objects.create(**fields)


@pytest.fixture
def contact(member):
    return make_contact(member)


def schedule(campaign, member, contacts, *, when=None, **kwargs):
    """Queue a job, optionally one that is already due.

    `create` refuses a time in the past on purpose, so a due job is made by
    creating a future one and then moving the clock hand back on the row --
    which is what actually happens in production anyway. Loosening the
    validation for the convenience of the tests would test different code from
    the one that runs.
    """
    job = svc.create(
        campaign_id=campaign.id,
        member=member,
        contact_ids=[c.id for c in contacts],
        scheduled_at=timezone.now() + timedelta(hours=1),
        **kwargs,
    )
    if when is not None:
        ScheduledSend.objects.filter(id=job.id).update(scheduled_at=when)
        job.refresh_from_db()
    return job


def due_now():
    """A moment safely inside the past, beyond the clock-skew tolerance."""
    return timezone.now() - timedelta(minutes=5)


# ------------------------------------------------------------------ creating

def test_create_stores_the_selection_and_starts_pending(campaign, member, contact):
    job = schedule(campaign, member, [contact])

    assert job.status == ScheduleStatus.PENDING.value
    assert job.contact_ids == [str(contact.id)]
    assert job.cursor == 0 and job.total == 1 and job.remaining == 1


def test_a_time_in_the_past_is_refused(campaign, member, contact):
    with pytest.raises(svc.NotSchedulable):
        svc.create(
            campaign_id=campaign.id, member=member, contact_ids=[contact.id],
            scheduled_at=timezone.now() - timedelta(hours=2),
        )


def test_an_empty_selection_is_refused(campaign, member):
    with pytest.raises(svc.NotSchedulable):
        svc.create(
            campaign_id=campaign.id, member=member, contact_ids=[],
            scheduled_at=timezone.now() + timedelta(hours=1),
        )


def test_a_non_active_campaign_cannot_be_scheduled(campaign, member, contact):
    campaign.status = CampaignStatus.PAUSED.value
    campaign.save()
    with pytest.raises(svc.NotSchedulable):
        svc.create(
            campaign_id=campaign.id, member=member, contact_ids=[contact.id],
            scheduled_at=timezone.now() + timedelta(hours=1),
        )


def test_a_bad_cc_is_refused_at_creation(campaign, member, contact):
    """Same validator as the manual send path -- one definition of a valid CC."""
    with pytest.raises(svc.NotSchedulable):
        svc.create(
            campaign_id=campaign.id, member=member, contact_ids=[contact.id],
            scheduled_at=timezone.now() + timedelta(hours=1), cc="not-an-email",
        )


def test_duplicate_contacts_are_collapsed_but_order_is_kept(campaign, member):
    a, b = make_contact(member, 1), make_contact(member, 2)
    job = svc.create(
        campaign_id=campaign.id, member=member,
        contact_ids=[a.id, b.id, a.id],
        scheduled_at=timezone.now() + timedelta(hours=1),
    )
    assert job.contact_ids == [str(a.id), str(b.id)]


# ------------------------------------------------------------------ claiming

def test_a_job_is_not_claimable_before_it_is_due(campaign, member, contact):
    schedule(campaign, member, [contact], when=timezone.now() + timedelta(hours=3))
    assert svc.claim_due(member) == []


def test_a_due_job_is_claimed_and_leased(campaign, member, contact):
    job = schedule(campaign, member, [contact], when=due_now())

    claimed = svc.claim_due(member, agent_id="always-on")
    assert [j.id for j in claimed] == [job.id]

    job.refresh_from_db()
    assert job.status == ScheduleStatus.RUNNING.value
    assert job.leased_by == "always-on"
    assert job.lease_expires_at > timezone.now()
    assert job.attempts == 1 and job.started_at is not None


def test_a_leased_job_is_invisible_to_a_second_agent(campaign, member, contact):
    """Two of this member's agents polling at once must not both run the batch."""
    schedule(campaign, member, [contact], when=due_now())

    assert len(svc.claim_due(member, agent_id="laptop")) == 1
    assert svc.claim_due(member, agent_id="always-on") == []


def test_a_job_never_crosses_to_another_member(campaign, member, other_member, contact):
    """An agent authenticates as one member; sending someone else's job would
    leave the wrong mailbox and record a false sent_by."""
    schedule(campaign, member, [contact], when=due_now())
    assert svc.claim_due(other_member) == []


def test_a_paused_campaign_holds_the_job_rather_than_sending_it(campaign, member, contact):
    job = schedule(campaign, member, [contact], when=due_now())
    campaign.status = CampaignStatus.PAUSED.value
    campaign.save()

    assert svc.claim_due(member) == []
    job.refresh_from_db()
    assert job.status == ScheduleStatus.HELD.value
    assert "paused" in job.last_error


def test_a_held_job_resumes_once_the_campaign_is_active_again(campaign, member, contact):
    job = schedule(campaign, member, [contact], when=due_now())
    campaign.status = CampaignStatus.PAUSED.value
    campaign.save()
    svc.claim_due(member)

    campaign.status = CampaignStatus.ACTIVE.value
    campaign.save()
    assert len(svc.claim_due(member)) == 1
    job.refresh_from_db()
    assert job.status == ScheduleStatus.RUNNING.value


# -------------------------------------------------------------------- leases

def test_an_expired_lease_is_swept_back_to_pending(campaign, member, contact):
    """A laptop that closed its lid mid-batch must not strand the job."""
    job = schedule(campaign, member, [contact], when=due_now())
    svc.claim_due(member)

    ScheduledSend.objects.filter(id=job.id).update(
        lease_expires_at=timezone.now() - timedelta(minutes=1)
    )
    assert svc.sweep_expired_leases() == 1

    job.refresh_from_db()
    assert job.status == ScheduleStatus.PENDING.value
    assert job.leased_by == ""
    assert len(svc.claim_due(member)) == 1


def test_a_live_lease_is_left_alone(campaign, member, contact):
    schedule(campaign, member, [contact], when=due_now())
    svc.claim_due(member)
    assert svc.sweep_expired_leases() == 0


def test_heartbeat_extends_a_lease(campaign, member, contact):
    job = schedule(campaign, member, [contact], when=due_now())
    svc.claim_due(member)
    job.refresh_from_db()
    before = job.lease_expires_at

    svc.heartbeat(job.id, member, now=timezone.now() + timedelta(minutes=2))
    job.refresh_from_db()
    assert job.lease_expires_at > before


# ------------------------------------------------------------------ progress

def test_progress_advances_the_cursor_and_finishes_the_job(campaign, member):
    contacts = [make_contact(member, i) for i in range(3)]
    job = schedule(campaign, member, contacts, when=due_now())
    svc.claim_due(member)

    out = svc.record_progress(job.id, member, attempted=3, sent=2, skipped=1)
    assert out["status"] == ScheduleStatus.DONE.value

    job.refresh_from_db()
    assert (job.cursor, job.sent_count, job.skipped_count) == (3, 2, 1)
    assert job.finished_at is not None and job.remaining == 0


def test_the_cursor_advances_past_permanently_skipped_contacts(campaign, member):
    """A do_not_contact contact never gets a mailing row. If progress were
    measured by "contacts still lacking a mailing", the job would never end."""
    good = make_contact(member, 1)
    blocked = make_contact(member, 2, lifecycle=ContactLifecycle.DO_NOT_CONTACT.value)
    job = schedule(campaign, member, [good, blocked], when=due_now())
    svc.claim_due(member)

    svc.record_progress(job.id, member, attempted=2, sent=1, skipped=1)
    job.refresh_from_db()

    assert job.status == ScheduleStatus.DONE.value
    assert CampaignMailing.objects.filter(contact=blocked).count() == 0


def test_a_partial_slice_returns_the_job_to_the_queue(campaign, member):
    contacts = [make_contact(member, i) for i in range(5)]
    job = schedule(campaign, member, contacts, when=due_now())
    svc.claim_due(member)

    svc.record_progress(job.id, member, attempted=2, sent=2, skipped=0)
    job.refresh_from_db()

    assert job.status == ScheduleStatus.PENDING.value
    assert job.cursor == 2 and job.remaining == 3
    assert job.next_slice() == [str(c.id) for c in contacts[2:]]


def test_progress_from_another_member_is_refused(campaign, member, other_member, contact):
    job = schedule(campaign, member, [contact], when=due_now())
    out = svc.record_progress(job.id, other_member, attempted=1, sent=1, skipped=0)
    assert out["status"] == "unknown"


def test_mark_failed_is_terminal(campaign, member, contact):
    job = schedule(campaign, member, [contact], when=due_now())
    svc.mark_failed(job.id, member, "gmail auth died")

    job.refresh_from_db()
    assert job.status == ScheduleStatus.FAILED.value
    assert job.is_terminal and "gmail" in job.last_error
    assert svc.claim_due(member) == []


# ------------------------------------------------------------- human control

def test_cancel_stops_a_pending_job(campaign, member, contact):
    job = schedule(campaign, member, [contact])
    svc.cancel(job.id, member=member)

    job.refresh_from_db()
    assert job.status == ScheduleStatus.CANCELLED.value
    assert svc.claim_due(member) == []


def test_cancelling_mid_flight_stops_the_next_slice(campaign, member):
    """Mail already sent stays sent; the job simply does not continue."""
    contacts = [make_contact(member, i) for i in range(4)]
    job = schedule(campaign, member, contacts, when=due_now())
    svc.claim_due(member)
    svc.cancel(job.id, member=member)

    out = svc.record_progress(job.id, member, attempted=2, sent=2, skipped=0)
    assert out["status"] == ScheduleStatus.CANCELLED.value

    job.refresh_from_db()
    assert job.status == ScheduleStatus.CANCELLED.value


def test_a_terminal_job_cannot_be_cancelled_twice(campaign, member, contact):
    job = schedule(campaign, member, [contact])
    svc.cancel(job.id, member=member)
    with pytest.raises(svc.NotSchedulable):
        svc.cancel(job.id, member=member)


def test_reschedule_moves_the_time_and_clears_a_hold(campaign, member, contact):
    job = schedule(campaign, member, [contact], when=due_now())
    campaign.status = CampaignStatus.PAUSED.value
    campaign.save()
    svc.claim_due(member)
    job.refresh_from_db()
    assert job.status == ScheduleStatus.HELD.value

    later = timezone.now() + timedelta(days=1)
    campaign.status = CampaignStatus.ACTIVE.value
    campaign.save()
    svc.reschedule(job.id, later, member=member)

    job.refresh_from_db()
    assert job.status == ScheduleStatus.PENDING.value
    assert abs((job.scheduled_at - later).total_seconds()) < 1
    assert job.last_error == ""


def test_a_running_job_must_be_cancelled_not_rescheduled(campaign, member, contact):
    job = schedule(campaign, member, [contact], when=due_now())
    svc.claim_due(member)
    with pytest.raises(svc.NotSchedulable):
        svc.reschedule(job.id, timezone.now() + timedelta(days=1), member=member)


# ------------------------------------------------- the runner, end to end
# These replace a block of tests that drove the same code through the token API
# the laptop agent used. The API is gone with the agent; the behaviour it
# exercised -- lease, send, advance the cursor, finish -- is not, and now runs
# in-process through services/runner.py. Testing it there is closer to what
# actually happens.


class _FakeGmail:
    def __init__(self):
        self.sent = []

    def verify_identity(self):
        return "ok"

    def send(self, *, to, subject, body, body_html="", from_name="", cc="", bcc=""):
        self.sent.append(to)
        return gmail_svc.SendResult(
            message_id=f"m{len(self.sent)}", thread_id=f"t{len(self.sent)}"
        )

    def find_message_to(self, address, subject):
        return None


@pytest.fixture
def connected(member):
    """A member the server can actually send as."""
    ciphertext, version = token_store.encrypt("refresh")
    GmailCredential.objects.create(
        member=member, google_email=member.bits_email,
        refresh_token_encrypted=ciphertext, key_version=version,
        granted_scopes=list(gmail_svc.SCOPES),
        identity_verified_at=timezone.now(),
    )
    return member


def test_a_due_job_is_leased_sent_and_finished(campaign, connected, contact):
    job = schedule(campaign, connected, [contact], when=due_now())
    gmail = _FakeGmail()

    report = runner.tick(gmail_for=lambda m: gmail)

    assert report.sent == 1
    assert gmail.sent == [contact.email]
    job.refresh_from_db()
    assert job.status == ScheduleStatus.DONE.value
    assert job.cursor == job.total


def test_a_job_that_is_not_due_is_left_alone(campaign, connected, contact):
    job = schedule(campaign, connected, [contact],
                   when=timezone.now() + timedelta(hours=3))

    report = runner.tick(gmail_for=lambda m: _FakeGmail())

    assert report.sent == 0
    job.refresh_from_db()
    assert job.status == ScheduleStatus.PENDING.value
    assert job.attempts == 0


def test_a_paused_campaign_holds_the_job_rather_than_sending_it(
    campaign, connected, contact
):
    """Pausing is the documented emergency brake. It has to stop a send that was
    queued before someone pulled it."""
    job = schedule(campaign, connected, [contact], when=due_now())
    campaign.status = CampaignStatus.PAUSED.value
    campaign.save()

    gmail = _FakeGmail()
    runner.tick(gmail_for=lambda m: gmail)

    assert gmail.sent == []
    job.refresh_from_db()
    assert job.status == ScheduleStatus.HELD.value
    assert "paused" in job.last_error


def test_a_drip_sends_one_batch_per_tick(campaign, connected):
    contacts = [make_contact(connected, i) for i in range(4)]
    job = schedule(campaign, connected, contacts, when=due_now(),
                   batch_size=2, interval_minutes=30)

    gmail = _FakeGmail()
    runner.tick(gmail_for=lambda m: gmail)

    assert len(gmail.sent) == 2
    job.refresh_from_db()
    assert job.cursor == 2
    assert job.status == ScheduleStatus.PENDING.value


def test_the_cursor_advances_past_contacts_that_were_skipped(campaign, connected):
    """Otherwise a permanently unmailable contact stalls the job forever."""
    good = make_contact(connected, 1)
    blocked = make_contact(connected, 2)
    blocked.lifecycle = ContactLifecycle.DO_NOT_CONTACT.value
    blocked.save()

    job = schedule(campaign, connected, [blocked, good], when=due_now())
    runner.tick(gmail_for=lambda m: _FakeGmail())

    job.refresh_from_db()
    assert job.status == ScheduleStatus.DONE.value
    assert job.sent_count == 1
    assert job.skipped_count == 1


# ------------------------------------------------------- the CRM schedule page

@pytest.fixture
def lead(team):
    return make_lead(team)


def sign_in(client, who):
    session = client.session
    session["member_id"] = str(who.id)
    session.save()
    return client


def test_the_page_lists_open_jobs_and_hides_finished_ones(client, lead, campaign, member, contact):
    """Default view is work still outstanding; finished jobs are on ?status=all.

    Asserted on the rendered status classes rather than on primary keys: a
    terminal job renders no cancel form, so its pk never reaches the HTML.
    """
    schedule(campaign, member, [contact])
    done = schedule(campaign, member, [make_contact(member, 9)])
    ScheduledSend.objects.filter(id=done.id).update(status=ScheduleStatus.DONE.value)

    body = sign_in(client, lead).get(reverse("crm:schedule_list")).content.decode()
    assert "s-PENDING" in body
    assert "s-DONE" not in body

    all_body = client.get(reverse("crm:schedule_list"), {"status": "all"}).content.decode()
    assert "s-DONE" in all_body and "s-PENDING" in all_body


def test_missed_jobs_are_called_out_at_the_top(client, lead, campaign, member, contact):
    """The whole point of this page: mail that never went out must be loud."""
    job = schedule(campaign, member, [contact])
    ScheduledSend.objects.filter(id=job.id).update(
        status=ScheduleStatus.MISSED.value, last_error="nothing executed it"
    )

    body = sign_in(client, lead).get(reverse("crm:schedule_list")).content.decode()
    assert "need attention" in body
    assert "nothing executed it" in body


def test_a_lead_can_cancel_anyone_s_job(client, lead, campaign, member, contact):
    job = schedule(campaign, member, [contact])
    r = sign_in(client, lead).post(reverse("crm:schedule_cancel", args=[job.pk]))

    assert r.status_code == 302
    job.refresh_from_db()
    assert job.status == ScheduleStatus.CANCELLED.value


def test_a_member_may_look_but_not_cancel(client, campaign, member, contact):
    """Cancelling someone else's send is a lead decision -- the job belongs to
    another person's mailbox and may be half-sent."""
    job = schedule(campaign, member, [contact])
    sign_in(client, member)

    assert client.get(reverse("crm:schedule_list")).status_code == 200

    r = client.post(reverse("crm:schedule_cancel", args=[job.pk]))
    assert r.status_code in (302, 403)
    job.refresh_from_db()
    assert job.status == ScheduleStatus.PENDING.value


# ----------------------------------------------- the sending window and grace

def at(hour, minute=0, day=18):
    """A moment on 18 Aug 2026 in the project timezone (IST)."""
    from datetime import datetime
    return timezone.make_aware(datetime(2026, 8, day, hour, minute))


def test_the_window_is_evaluated_in_local_time_not_utc(window):
    """09:00 IST is 03:30 UTC. Judging the hour in UTC would close the window
    over the entire Indian working morning."""
    assert svc.in_window(at(9, 30)) is True
    assert svc.in_window(at(18, 59)) is True
    assert svc.in_window(at(19, 0)) is False
    assert svc.in_window(at(3, 0)) is False


def test_next_open_slot_moves_a_late_night_job_to_the_morning(window):
    assert svc.next_open_slot(at(22, 30)) == at(9, 0, day=19)
    assert svc.next_open_slot(at(6, 0)) == at(9, 0)
    assert svc.next_open_slot(at(11, 0)) == at(11, 0)     # already open


def test_a_closed_weekend_pushes_to_monday(window):
    window.SCHEDULE_WINDOW_DAYS = [0, 1, 2, 3, 4]
    saturday = at(11, 0, day=22)
    assert saturday.weekday() == 5
    assert svc.next_open_slot(saturday) == at(9, 0, day=24)   # Monday


def test_an_equal_start_and_end_disables_the_window():
    """The autouse fixture already disables it; this pins the behaviour."""
    assert svc.in_window(at(3, 0)) is True
    assert svc.next_open_slot(at(3, 0)) == at(3, 0)


def test_grace_runs_from_when_the_job_was_first_allowed_to_run(window, campaign, member, contact):
    """A 22:00 job deferred to 09:00 must not be 'missed' for the hours it was
    never permitted to use."""
    job = schedule(campaign, member, [contact], when=at(22, 0))

    assert svc.deliver_after(job) == at(9, 0, day=19)
    assert svc.deadline(job) == at(15, 0, day=19)

    assert svc.is_missed(job, now=at(8, 0, day=19)) is False    # 10h "late", fine
    assert svc.is_missed(job, now=at(14, 0, day=19)) is False   # inside grace
    assert svc.is_missed(job, now=at(16, 0, day=19)) is True    # past it


def test_a_job_due_inside_quiet_hours_is_held_not_sent(window, campaign, member, contact):
    job = schedule(campaign, member, [contact], when=at(22, 0))
    ok, reason = svc.is_runnable(job, now=at(22, 30))
    assert ok is False and "window" in reason


def test_sweep_missed_marks_and_explains(window, campaign, member, contact):
    job = schedule(campaign, member, [contact], when=at(9, 30))

    assert svc.sweep_missed(now=at(12, 0)) == 0       # still inside grace
    assert svc.sweep_missed(now=at(17, 0)) == 1       # past 15:30

    job.refresh_from_db()
    assert job.status == ScheduleStatus.MISSED.value
    assert "nothing executed this" in job.last_error
    assert job.finished_at is not None


def test_a_missed_job_is_never_claimed_afterwards(window, campaign, member, contact):
    schedule(campaign, member, [contact], when=at(9, 30))
    svc.sweep_missed(now=at(17, 0))
    assert svc.claim_due(member, now=at(17, 1)) == []


def test_a_running_job_is_not_swept_out_from_under_its_agent(campaign, member, contact):
    """Marking a live batch missed would leave a half-sent job looking abandoned."""
    job = schedule(campaign, member, [contact], when=due_now())
    svc.claim_due(member)
    assert svc.sweep_missed(now=timezone.now() + timedelta(days=2)) == 0
    job.refresh_from_db()
    assert job.status == ScheduleStatus.RUNNING.value


# ------------------------------------------------------------ drip / throttle

def test_a_drip_hands_out_one_batch_at_a_time(campaign, member):
    contacts = [make_contact(member, i) for i in range(5)]
    job = svc.create(
        campaign_id=campaign.id, member=member, contact_ids=[c.id for c in contacts],
        scheduled_at=timezone.now() + timedelta(hours=1),
        batch_size=2, interval_minutes=30,
    )
    ScheduledSend.objects.filter(id=job.id).update(scheduled_at=due_now())

    claimed = svc.claim_due(member)
    assert claimed[0].next_slice(claimed[0].batch_size) == [str(c.id) for c in contacts[:2]]


def test_a_drip_waits_its_interval_before_the_next_batch(campaign, member):
    contacts = [make_contact(member, i) for i in range(5)]
    job = svc.create(
        campaign_id=campaign.id, member=member, contact_ids=[c.id for c in contacts],
        scheduled_at=timezone.now() + timedelta(hours=1),
        batch_size=2, interval_minutes=30,
    )
    ScheduledSend.objects.filter(id=job.id).update(scheduled_at=due_now())
    svc.claim_due(member)
    svc.record_progress(job.id, member, attempted=2, sent=2, skipped=0)

    job.refresh_from_db()
    assert job.status == ScheduleStatus.PENDING.value
    assert job.next_run_at is not None
    assert svc.claim_due(member) == []                       # too soon

    # Half an hour on, the next batch is fair game.
    later = timezone.now() + timedelta(minutes=31)
    assert len(svc.claim_due(member, now=later)) == 1


def test_a_drip_terminates_after_the_last_batch(campaign, member):
    contacts = [make_contact(member, i) for i in range(3)]
    job = svc.create(
        campaign_id=campaign.id, member=member, contact_ids=[c.id for c in contacts],
        scheduled_at=timezone.now() + timedelta(hours=1),
        batch_size=2, interval_minutes=1,
    )
    ScheduledSend.objects.filter(id=job.id).update(scheduled_at=due_now())

    svc.claim_due(member)
    svc.record_progress(job.id, member, attempted=2, sent=2, skipped=0)
    svc.claim_due(member, now=timezone.now() + timedelta(minutes=2))
    svc.record_progress(job.id, member, attempted=1, sent=1, skipped=0)

    job.refresh_from_db()
    assert job.status == ScheduleStatus.DONE.value
    assert job.next_run_at is None and job.sent_count == 3


def test_a_batch_size_without_an_interval_is_refused(campaign, member, contact):
    """Otherwise it would drain the list on consecutive ticks -- the opposite
    of dripping, with none of the safety."""
    with pytest.raises(svc.NotSchedulable):
        svc.create(
            campaign_id=campaign.id, member=member, contact_ids=[contact.id],
            scheduled_at=timezone.now() + timedelta(hours=1), batch_size=10,
        )


def test_a_drip_in_flight_is_not_declared_missed(window, campaign, member):
    """Its deadline moves with each batch: a job spreading over two days is not
    six hours late on day two."""
    contacts = [make_contact(member, i) for i in range(4)]
    job = svc.create(
        campaign_id=campaign.id, member=member, contact_ids=[c.id for c in contacts],
        scheduled_at=timezone.now() + timedelta(hours=1), batch_size=2, interval_minutes=60,
    )
    # Wind the clock back on the row: it began at 10:00 and has one batch done,
    # so the next is owed at 11:00.
    ScheduledSend.objects.filter(id=job.id).update(
        scheduled_at=at(10, 0), next_run_at=at(11, 0)
    )
    job.refresh_from_db()

    assert svc.is_missed(job, now=at(12, 0)) is False
    assert svc.is_missed(job, now=at(18, 0)) is True          # 11:00 + 6h


def test_the_scheduler_stands_down_when_the_daily_cap_is_spent(campaign, member, contact, monkeypatch):
    """Leasing jobs the cap will refuse burns attempts and reads as failure
    rather than as a quota."""
    schedule(campaign, member, [contact], when=due_now())
    monkeypatch.setattr(svc, "remaining_quota", lambda m: 0)
    assert svc.claim_due(member) == []
