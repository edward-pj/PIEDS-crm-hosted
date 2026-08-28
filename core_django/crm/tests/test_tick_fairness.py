"""Sharing one tick's budget across the team.

A tick sends at most `TICK_MAX_MAILS`, and `TeamMember.Meta.ordering` is
`["name"]`. Left alone, those two facts mean the alphabetically first member
takes the whole budget every tick until their queue is empty, and the last
member's first mail goes out hours after the first member's.

Two stateless mechanisms fix it, and both are tested here: a per-member ceiling
on one tick (`PER_MEMBER_TICK_MAILS`) so a tick serves several people, and a
rotation of the iteration order so the same person is not always served first.
"""

import pytest
from datetime import datetime, timedelta, timezone as dt_timezone
from django.utils import timezone

from crm.models import Campaign, Contact
from crm.services import runner, scheduling as schedule_svc, sending
from crm.tests.conftest import make_member
from crm.tests.test_gmail_credentials import FakeGmail, connect
from shared.enums import CampaignStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def campaign(team):
    return Campaign.objects.create(
        title="Ignite", mail_sub="Hello", mail_body="About your work",
        status=CampaignStatus.ACTIVE.value, team=team,
    )


def sender_with_queue(team, campaign, name, count):
    """A member with Gmail connected and `count` contacts queued."""
    member = make_member(
        team, name=name, email=f"{name.lower()}@pilani.bits-pilani.ac.in"
    )
    connect(member)
    contacts = [
        Contact.objects.create(
            first_name=f"{name}{i}", email=f"{name.lower()}{i}@example.com",
            company="Acme", assigned_to=member,
        )
        for i in range(count)
    ]
    schedule_svc.create(
        campaign_id=campaign.id, member=member,
        contact_ids=[str(c.id) for c in contacts],
        scheduled_at=timezone.now(),
    )
    return member


class TestOneTickServesSeveralMembers:
    def test_nobody_takes_the_whole_budget(self, team, campaign):
        """Before the per-member ceiling, Aarav sent 40 and the others zero."""
        for name in ("Aarav", "Kabir", "Zoya"):
            sender_with_queue(team, campaign, name, 30)

        gmail = FakeGmail()
        report = runner.tick(gmail_for=lambda _m: gmail)

        assert report.sent == 30, "three members x PER_MEMBER_TICK_MAILS"
        assert report.members == 3

    def test_each_member_is_held_to_their_share(self, team, campaign):
        for name in ("Aarav", "Kabir", "Zoya"):
            sender_with_queue(team, campaign, name, 30)

        gmail = FakeGmail()
        runner.tick(gmail_for=lambda _m: gmail)

        for prefix in ("aarav", "kabir", "zoya"):
            mine = [m for m in gmail.sent if m["to"].startswith(prefix)]
            assert len(mine) == runner.PER_MEMBER_TICK_MAILS, (
                f"{prefix} got {len(mine)}, not their share"
            )

    def test_the_tick_budget_still_binds_overall(self, team, campaign, monkeypatch):
        """The per-member share is a second ceiling, not a replacement.

        Both budgets are honoured to CLAIM_CHUNK granularity, never to the
        exact mail: send_batch checks its allowance BEFORE claiming a chunk and
        then finishes that chunk, because the ten contacts in it already hold
        DRAFT rows and abandoning them would strand every one. So a budget of 15
        stops somewhere in [15, 25), and that overshoot is the deliberate price
        of never stranding a claim.
        """
        monkeypatch.setattr(runner, "TICK_MAX_MAILS", 15)
        for name in ("Aarav", "Kabir", "Zoya"):
            sender_with_queue(team, campaign, name, 30)

        gmail = FakeGmail()
        report = runner.tick(gmail_for=lambda _m: gmail)

        assert 15 <= report.sent < 15 + sending.CLAIM_CHUNK
        assert report.stopped_early is True
        assert report.members < 3, "it stopped before reaching everybody"


class TestTheOrderRotates:
    def test_consecutive_minutes_lead_with_different_members(self, team, campaign):
        for name in ("Aarav", "Kabir", "Zoya"):
            sender_with_queue(team, campaign, name, 5)

        base = datetime(2026, 8, 28, 10, 0, tzinfo=dt_timezone.utc)
        leaders = [
            runner.sendable_members(base + timedelta(minutes=i))[0].name
            for i in range(3)
        ]

        assert len(set(leaders)) == 3, f"same member led every tick: {leaders}"

    def test_it_is_a_rotation_not_a_shuffle(self, team, campaign):
        """Everyone still appears exactly once, so nobody is dropped."""
        for name in ("Aarav", "Kabir", "Zoya"):
            sender_with_queue(team, campaign, name, 5)

        base = datetime(2026, 8, 28, 10, 0, tzinfo=dt_timezone.utc)
        names = [m.name for m in runner.sendable_members(base)]

        assert sorted(names) == ["Aarav", "Kabir", "Zoya"]

    def test_no_sendable_members_is_not_a_crash(self, team):
        """A modulo by zero is the obvious way to write this wrong."""
        assert runner.sendable_members() == []
