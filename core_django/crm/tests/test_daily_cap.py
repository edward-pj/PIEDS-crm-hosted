"""The daily send cap, and the mail it used to swallow.

`DAILY_SEND_CAP` is a rate limit, not a filter: a contact refused today is
still owed a mail tomorrow. The scheduler did not know that. `claim_batch`
reports a cap-blocked contact as a `CAP_REACHED` skip, `send_batch` yielded an
Outcome for it, `run_job` counted every yielded Outcome as *attempted*, and
`record_progress` advances the cursor by `attempted` -- so the job walked
straight past everyone it had not sent to and reported `done`.

That was survivable at 400 a day when nobody queued more than they could send.
It is not survivable at 800, where a member's whole assigned list is expected to
exceed one day's quota by design.

The distinction these tests pin: `unassigned`, `archived` and `do_not_contact`
are permanent, so the cursor MUST move past them or the job never finishes.
`CAP_REACHED` is temporary, so the cursor must NOT.
"""

import pytest
from datetime import timedelta
from django.utils import timezone

from crm.models import Campaign, CampaignMailing, Contact
from crm.services import mailing, runner, scheduling as schedule_svc
from crm.tests.conftest import make_member
from crm.tests.test_gmail_credentials import FakeGmail, connect
from shared.enums import CampaignStatus, MailingStatus, ScheduleStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def sender(team):
    member = make_member(team)
    connect(member)
    return member


@pytest.fixture
def campaign(team):
    return Campaign.objects.create(
        title="Ignite", mail_sub="Hello", mail_body="About your work",
        status=CampaignStatus.ACTIVE.value, team=team,
    )


def contacts_for(member, count, *, start=0):
    return [
        Contact.objects.create(
            first_name=f"C{i}", email=f"c{i}@example.com",
            company="Acme", assigned_to=member,
        )
        for i in range(start, start + count)
    ]


def queue(campaign, member, contacts):
    return schedule_svc.create(
        campaign_id=campaign.id, member=member,
        contact_ids=[str(c.id) for c in contacts],
        scheduled_at=timezone.now(),
    )


def drain(gmail, times=8):
    for _ in range(times):
        runner.tick(gmail_for=lambda _m: gmail)


class TestTheCapDoesNotSwallowMail:
    """The regression. Before the fix this job reported done with 15 unsent."""

    def test_a_job_larger_than_the_quota_stops_rather_than_completing(
        self, sender, campaign, monkeypatch
    ):
        monkeypatch.setattr(mailing, "DAILY_SEND_CAP", 5)
        job = queue(campaign, sender, contacts_for(sender, 20))

        gmail = FakeGmail()
        drain(gmail)

        job.refresh_from_db()
        assert len(gmail.sent) == 5
        assert job.cursor == 5, "the cursor walked past contacts nobody mailed"
        assert job.status == ScheduleStatus.PENDING.value, (
            f"job is {job.status}; a cap is a pause, not a completion"
        )
        assert job.remaining == 15

    def test_the_unsent_contacts_have_no_mailing_row_at_all(
        self, sender, campaign, monkeypatch
    ):
        """Not a FAILED row, not a skipped row -- nothing. They were never
        touched, so they stay claimable by tomorrow's tick."""
        monkeypatch.setattr(mailing, "DAILY_SEND_CAP", 5)
        queue(campaign, sender, contacts_for(sender, 20))

        drain(FakeGmail())

        assert CampaignMailing.objects.count() == 5

    def test_it_resumes_once_the_rolling_window_frees(
        self, sender, campaign, monkeypatch
    ):
        """The whole point of not advancing the cursor."""
        monkeypatch.setattr(mailing, "DAILY_SEND_CAP", 5)
        job = queue(campaign, sender, contacts_for(sender, 12))

        gmail = FakeGmail()
        drain(gmail)
        assert len(gmail.sent) == 5

        # Age the first five out of the 24-hour window.
        CampaignMailing.objects.update(sent_at=timezone.now() - timedelta(hours=25))
        job.refresh_from_db()
        job.next_run_at = None          # skip the cap back-off, not what we test
        job.save(update_fields=["next_run_at"])

        drain(gmail)

        job.refresh_from_db()
        assert len(gmail.sent) == 10
        assert job.cursor == 10
        assert job.status == ScheduleStatus.PENDING.value

    def test_the_member_is_told_why_it_stopped(self, sender, campaign, monkeypatch):
        monkeypatch.setattr(mailing, "DAILY_SEND_CAP", 5)
        job = queue(campaign, sender, contacts_for(sender, 20))

        drain(FakeGmail())

        job.refresh_from_db()
        assert "cap" in job.last_error.lower()


class TestPermanentSkipsStillAdvance:
    """The behaviour the cap fix must not break.

    runner.run_job's docstring is explicit: a contact skipped for good has to
    move the cursor or the job never finishes. Only CAP_REACHED changed.
    """

    def test_an_unassigned_contact_advances_the_cursor(self, sender, campaign, team):
        stranger = make_member(team, name="Ishita", email="ishita@pilani.bits-pilani.ac.in")
        mine = contacts_for(sender, 2)
        theirs = Contact.objects.create(
            first_name="Not", email="not@mine.com", company="X", assigned_to=stranger
        )
        job = queue(campaign, sender, [*mine, theirs])

        drain(FakeGmail())

        job.refresh_from_db()
        assert job.cursor == 3
        assert job.status == ScheduleStatus.DONE.value

    def test_an_archived_contact_advances_the_cursor(self, sender, campaign):
        good = contacts_for(sender, 2)
        gone = Contact.objects.create(
            first_name="Gone", email="gone@x.com", company="X",
            assigned_to=sender, is_archived=True,
        )
        job = queue(campaign, sender, [*good, gone])

        drain(FakeGmail())

        job.refresh_from_db()
        assert job.cursor == 3
        assert job.status == ScheduleStatus.DONE.value


class TestACapPausedJobIsNotDeclaredMissed:
    """`sweep_missed` says "nothing executed this within Nh". A job pausing on
    its owner's quota has plainly been executed, and will resume on its own."""

    def test_it_survives_the_sweep(self, sender, campaign, monkeypatch, settings):
        """The real shape: the pinger's window closes at 17:00 and the job waits
        overnight for quota. At the shipped 20-hour grace it must still be there
        in the morning."""
        settings.SCHEDULE_GRACE_HOURS = 20
        monkeypatch.setattr(mailing, "DAILY_SEND_CAP", 5)
        job = queue(campaign, sender, contacts_for(sender, 20))

        drain(FakeGmail())

        schedule_svc.sweep_missed(now=timezone.now() + timedelta(hours=10))

        job.refresh_from_db()
        assert job.status == ScheduleStatus.PENDING.value

    def test_the_backoff_moves_the_clock_on_every_tick(
        self, sender, campaign, monkeypatch
    ):
        """Not just once. A fully capped member is never leased at all, so
        without claim_due pushing next_run_at the deadline would freeze at the
        first pause and the job would age into MISSED anyway."""
        monkeypatch.setattr(mailing, "DAILY_SEND_CAP", 5)
        job = queue(campaign, sender, contacts_for(sender, 20))

        drain(FakeGmail(), times=1)
        job.refresh_from_db()
        first = job.next_run_at
        assert first is not None

        # Past the back-off, so the job is a candidate again -- and still
        # capped, so it gets pushed a second time.
        runner.tick(now=first + timedelta(minutes=1),
                    gmail_for=lambda _m: FakeGmail())

        job.refresh_from_db()
        assert job.next_run_at > first

    def test_a_job_nothing_ever_touched_is_still_marked_missed(
        self, sender, campaign, settings
    ):
        """The sweep still does its job. This is the tripwire for over-fixing."""
        settings.SCHEDULE_GRACE_HOURS = 1
        job = queue(campaign, sender, contacts_for(sender, 3))

        schedule_svc.sweep_missed(now=timezone.now() + timedelta(hours=2))

        job.refresh_from_db()
        assert job.status == ScheduleStatus.MISSED.value


class TestTheCapIsConfigurable:
    def test_it_reads_the_setting(self, settings):
        """800/day is an operational decision, not a code constant. It has to
        be changeable from the hosting dashboard without a redeploy."""
        assert mailing.DAILY_SEND_CAP == settings.DAILY_SEND_CAP
