"""Follow-ups: chasing silence without chasing people who answered.

The test that matters most is `test_a_replied_contact_is_never_followed_up`.
Mailing someone a "just following up!" after they have already written back is
the single most embarrassing thing this feature can do, and the only thing
standing between us and it is reply detection being believed.

Second most important: `test_lifecycle_is_left_alone_unless_the_rule_opts_in`.
shared/enums.py documents NEW -> CONTACTED as the *only* automatic lifecycle
transition. This feature adds a second one, so it has to stay opt-in.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from crm.models import (
    Campaign, CampaignMailing, Contact, FollowUpRule, ScheduledSend, TeamMember,
)
from crm.services import followups as svc
from shared.enums import CampaignStatus, ContactLifecycle, MailingStatus, ScheduleStatus

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _window_off(settings):
    """Clock-independence, as in test_scheduling.py."""
    settings.SCHEDULE_WINDOW_START = 0
    settings.SCHEDULE_WINDOW_END = 0
    settings.SCHEDULE_GRACE_HOURS = 6


@pytest.fixture
def member():
    return TeamMember.objects.create(
        name="Kabir", bits_email="kabir@pilani.bits-pilani.ac.in", batch="2025"
    )


def make_campaign(member, title):
    return Campaign.objects.create(
        title=title, mail_sub="Hi {{ first_name }}", mail_body="Hello {{ first_name }}.",
        var_list=["first_name"], status=CampaignStatus.ACTIVE.value, created_by=member,
    )


@pytest.fixture
def first(member):
    return make_campaign(member, "Outreach")


@pytest.fixture
def second(member):
    return make_campaign(member, "Follow-up")


@pytest.fixture
def rule(first, second, member):
    return FollowUpRule.objects.create(
        campaign=first, follow_up=second, delay_days=3, created_by=member
    )


def make_contact(member, n=0, **kwargs):
    fields = dict(
        first_name=f"Rohan{n}", last_name="Iyer", email=f"rohan{n}@example.com",
        company="Acme", designation="CTO", assigned_to=member, created_by=member,
    )
    fields.update(kwargs)
    return Contact.objects.create(**fields)


def sent_mailing(campaign, contact, member, *, days_ago=5, **kwargs):
    when = timezone.now() - timedelta(days=days_ago)
    fields = dict(
        campaign=campaign, contact=contact, sent_by=member,
        status=MailingStatus.SENT.value, mail_thread_id=f"t-{contact.pk}",
        rendered_subject="Hi", rendered_body="Hello", sent_at=when,
    )
    fields.update(kwargs)
    return CampaignMailing.objects.create(**fields)


# ------------------------------------------------------------ who is chased

def test_silence_past_the_delay_is_followed_up(rule, first, second, member):
    contact = make_contact(member)
    sent_mailing(first, contact, member, days_ago=5)

    jobs = svc.queue_follow_ups(rule)
    assert len(jobs) == 1
    # The SENDER'S sub-campaign of the follow-up root, not the root itself.
    # Queueing the root would send every follow-up with the wrong footer -- or
    # none -- so a follow-up would arrive signed by somebody the prospect has
    # never heard from.
    assert jobs[0].campaign.parent == second
    assert jobs[0].campaign.owner == member
    assert jobs[0].contact_ids == [str(contact.id)]
    assert jobs[0].member == member


def test_someone_mailed_yesterday_is_left_alone(rule, first, member):
    """delay_days is three; a day of silence is not silence."""
    sent_mailing(first, make_contact(member), member, days_ago=1)
    assert svc.queue_follow_ups(rule) == []


def test_a_replied_contact_is_never_followed_up(rule, first, member):
    """The one thing this feature must never do."""
    contact = make_contact(member)
    sent_mailing(first, contact, member, days_ago=9, replied_at=timezone.now())

    assert svc.queue_follow_ups(rule) == []
    assert not ScheduledSend.objects.exists()


def test_nobody_is_followed_up_twice(rule, first, member):
    contact = make_contact(member)
    sent_mailing(first, contact, member, days_ago=5)

    assert len(svc.queue_follow_ups(rule)) == 1
    assert svc.queue_follow_ups(rule) == []          # followed_up_at now stamped


def test_a_contact_already_mailed_the_follow_up_is_skipped(rule, first, second, member):
    """uniq_campaign_contact would refuse it anyway; filtering here keeps the
    job honest about its own size instead of reporting a batch of skips."""
    contact = make_contact(member)
    sent_mailing(first, contact, member, days_ago=5)
    sent_mailing(second, contact, member, days_ago=1)

    assert svc.queue_follow_ups(rule) == []


def test_an_inactive_rule_does_nothing(rule, first, member):
    sent_mailing(first, make_contact(member), member, days_ago=5)
    rule.is_active = False
    rule.save()
    assert svc.queue_follow_ups(rule) == []


def test_a_paused_follow_up_campaign_queues_nothing(rule, first, second, member):
    """Pausing is the emergency brake; it has to stop follow-ups too."""
    sent_mailing(first, make_contact(member), member, days_ago=5)
    second.status = CampaignStatus.PAUSED.value
    second.save()
    assert svc.queue_follow_ups(rule) == []


def test_each_original_sender_gets_their_own_job(rule, first, second, member):
    """A follow-up must leave the same mailbox as the mail it is chasing, or it
    arrives from a stranger with no thread behind it."""
    other = TeamMember.objects.create(
        name="Ishita", bits_email="ishita@pilani.bits-pilani.ac.in", batch="2025"
    )
    a, b = make_contact(member, 1), make_contact(other, 2)
    sent_mailing(first, a, member, days_ago=5)
    sent_mailing(first, b, other, days_ago=5)

    jobs = svc.queue_follow_ups(rule)
    assert {j.member for j in jobs} == {member, other}
    assert all(len(j.contact_ids) == 1 for j in jobs)
    # Each job under its own sender's sub-campaign, so each follow-up carries
    # the footer of the person whose thread it continues. This assertion is the
    # tripwire: without it the sub-campaign resolution can be dropped from
    # queue_follow_ups and every test here still passes.
    assert {j.campaign.owner for j in jobs} == {member, other}
    assert all(j.campaign.parent_id == second.id for j in jobs)


# --------------------------------------------------------- reply bookkeeping

def test_recording_a_reply_marks_the_mailing(rule, first, member):
    contact = make_contact(member)
    mailing = sent_mailing(first, contact, member, days_ago=1)

    assert svc.record_reply_scan(mailing.id, member, replied=True)["status"] == "replied"
    mailing.refresh_from_db()
    assert mailing.replied_at is not None and mailing.reply_checked_at is not None


def test_a_no_reply_scan_only_stamps_the_check(rule, first, member):
    mailing = sent_mailing(first, make_contact(member), member, days_ago=1)

    assert svc.record_reply_scan(mailing.id, member, replied=False)["status"] == "no_reply"
    mailing.refresh_from_db()
    assert mailing.replied_at is None and mailing.reply_checked_at is not None


def test_lifecycle_is_left_alone_unless_the_rule_opts_in(rule, first, member):
    """shared/enums.py documents NEW -> CONTACTED as the only automatic
    transition. Adding a second one silently would make that docstring a lie."""
    contact = make_contact(member, lifecycle=ContactLifecycle.CONTACTED.value)
    mailing = sent_mailing(first, contact, member, days_ago=1)

    svc.record_reply_scan(mailing.id, member, replied=True)
    contact.refresh_from_db()
    assert contact.lifecycle == ContactLifecycle.CONTACTED.value

    rule.mark_replied = True
    rule.save()
    other = make_contact(member, 2, lifecycle=ContactLifecycle.CONTACTED.value)
    svc.record_reply_scan(sent_mailing(first, other, member, days_ago=1).id, member, replied=True)
    other.refresh_from_db()
    assert other.lifecycle == ContactLifecycle.REPLIED.value


def test_a_hand_set_do_not_contact_outranks_a_detected_reply(rule, first, member):
    """A human decision must never be undone by a background scan."""
    rule.mark_replied = True
    rule.save()
    contact = make_contact(member, lifecycle=ContactLifecycle.DO_NOT_CONTACT.value)

    svc.record_reply_scan(sent_mailing(first, contact, member, days_ago=1).id, member, replied=True)
    contact.refresh_from_db()
    assert contact.lifecycle == ContactLifecycle.DO_NOT_CONTACT.value


def test_a_late_reply_pulls_the_contact_out_of_a_queued_follow_up(rule, first, second, member):
    """The window between queueing a follow-up and sending it is the one place
    we could still chase someone who has answered."""
    a, b = make_contact(member, 1), make_contact(member, 2)
    sent_mailing(first, a, member, days_ago=5)
    sent_mailing(first, b, member, days_ago=5)

    job = svc.queue_follow_ups(rule)[0]
    assert len(job.contact_ids) == 2

    svc.cancel_pending_for(a.id, second.id)
    job.refresh_from_db()
    # str() on both sides: the column hands back UUID objects, not the strings
    # that went in.
    assert [str(c) for c in job.contact_ids] == [str(b.id)]


def test_only_unsent_contacts_are_pulled_from_a_running_job(rule, first, second, member):
    """Rewriting the part of a job already sent would be a lie about history."""
    a, b = make_contact(member, 1), make_contact(member, 2)
    sent_mailing(first, a, member, days_ago=5)
    sent_mailing(first, b, member, days_ago=5)

    job = svc.queue_follow_ups(rule)[0]
    ScheduledSend.objects.filter(id=job.id).update(cursor=1)   # first one already sent

    svc.cancel_pending_for(job.contact_ids[0], second.id)      # already sent
    job.refresh_from_db()
    assert len(job.contact_ids) == 2                            # untouched


# --------------------------------------------------------------- scan window

def test_threads_are_only_watched_for_a_month(rule, first, member):
    """A prospect silent for a month is not about to answer, and every check
    costs a Gmail call."""
    fresh = sent_mailing(first, make_contact(member, 1), member, days_ago=2)
    stale = sent_mailing(first, make_contact(member, 2), member,
                         days_ago=svc.REPLY_WATCH_DAYS + 5)

    watched = {m.id for m in svc.threads_to_check(member)}
    assert fresh.id in watched and stale.id not in watched


def test_threads_without_a_live_rule_are_not_scanned(first, member):
    """No rule means nobody asked; spending Gmail quota on it is waste."""
    sent_mailing(first, make_contact(member), member, days_ago=2)
    assert list(svc.threads_to_check(member)) == []


def test_an_already_replied_thread_is_dropped_from_the_scan(rule, first, member):
    sent_mailing(first, make_contact(member), member, days_ago=2, replied_at=timezone.now())
    assert list(svc.threads_to_check(member)) == []
