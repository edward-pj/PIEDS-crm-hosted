"""The claim/report contract, tested at the service layer.

These were `test_mailing_api.py`, driven through the token API the laptop agent
used. The API is gone with the agent; the rules are not. Only the tests that
were genuinely ABOUT the API — bearer-token auth, HTTP status codes — were
dropped. Everything here is behaviour that still exists and would otherwise have
lost its coverage with the transport it happened to be tested through.

`record_result`'s guards matter most. It is the function that decides a mail
went out, and it is reachable twice for one mailing whenever a report is
retried — so "already settled" and "not your mailing" are the two answers
standing between a replay and a corrupted record.
"""

import pytest
from django.utils import timezone

from crm.models import Campaign, CampaignMailing, Contact, TeamMember
from crm.services import mailing as svc
from shared.enums import CampaignStatus, ContactLifecycle, MailingStatus

pytestmark = pytest.mark.django_db


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
        title="Outreach", mail_sub="Hi {{ first_name }}",
        mail_body="About {{ company }}", var_list=["first_name", "company"],
        status=CampaignStatus.ACTIVE.value, created_by=member,
    )


@pytest.fixture
def contact(member):
    return Contact.objects.create(
        first_name="Rohan", last_name="Iyer", email="rohan@example.com",
        company="Zerodha", assigned_to=member,
    )


def claim_one(campaign, member, contact):
    claimed, skipped = svc.claim_batch(campaign, member, [contact.id])
    assert not skipped, skipped
    return claimed[0]


class TestClaim:
    def test_a_claim_commits_a_draft_with_the_rendered_content(
        self, campaign, member, contact
    ):
        """The DRAFT is committed BEFORE anything can be sent. That ordering is
        the guarantee: a crash can leave an ambiguous DRAFT, never a sent mail
        with no record."""
        item = claim_one(campaign, member, contact)

        row = CampaignMailing.objects.get(id=item.mailing_id)
        assert row.status == MailingStatus.DRAFT.value
        assert row.rendered_subject == "Hi Rohan"
        assert "Zerodha" in row.rendered_body
        assert row.rendered_body_html.startswith("<!doctype html>")

    def test_a_blank_required_variable_blocks_the_claim(self, campaign, member):
        """"Hi , we loved what you're building at ." must never go out. A
        contact missing data the template needs is skipped, not mailed."""
        blank = Contact.objects.create(
            first_name="Rohan", email="blank@example.com", company="",
            assigned_to=member,
        )

        claimed, skipped = svc.claim_batch(campaign, member, [blank.id])

        assert claimed == []
        assert skipped[0].code == svc.MISSING_VARS
        assert "company" in skipped[0].reason
        assert not CampaignMailing.objects.filter(contact=blank).exists()

    def test_a_non_active_campaign_cannot_be_loaded_for_sending(self, campaign):
        campaign.status = CampaignStatus.PAUSED.value
        campaign.save()

        with pytest.raises(svc.CampaignNotSendable, match="paused"):
            svc.load_sendable_campaign(campaign.id)

    def test_an_unknown_campaign_is_refused_rather_than_500ing(self):
        with pytest.raises(svc.CampaignNotSendable):
            svc.load_sendable_campaign("not-a-uuid")


class TestRecordResult:
    def test_sent_updates_the_mailing_and_the_contact(
        self, campaign, member, contact
    ):
        item = claim_one(campaign, member, contact)

        out = svc.record_result(
            item.mailing_id, member, status="sent",
            message_id="m1", thread_id="t1",
        )

        assert out["status"] == svc.SENT
        row = CampaignMailing.objects.get(id=item.mailing_id)
        assert row.status == MailingStatus.SENT.value
        assert (row.mail_message_id, row.mail_thread_id) == ("m1", "t1")
        assert row.sent_at is not None

        contact.refresh_from_db()
        assert contact.lifecycle == ContactLifecycle.CONTACTED.value
        assert contact.last_contacted_by_id == member.id

    def test_failed_records_the_error_and_leaves_it_retryable(
        self, campaign, member, contact
    ):
        item = claim_one(campaign, member, contact)

        svc.record_result(item.mailing_id, member, status="failed",
                          error="quota exceeded")

        row = CampaignMailing.objects.get(id=item.mailing_id)
        assert row.status == MailingStatus.FAILED.value
        assert "quota exceeded" in row.error_detail
        # FAILED, not DRAFT: re-claimable, because we know it reached nobody.
        again, skipped = svc.claim_batch(campaign, member, [contact.id])
        assert len(again) == 1 and not skipped

    def test_a_replayed_report_does_not_overwrite_the_first(
        self, campaign, member, contact
    ):
        """A retried report is a replayed request, not a second send. Letting it
        through would rewrite history — worse, a 'failed' replay after a real
        'sent' would mark a delivered mail as never sent and invite mailing the
        prospect a second time."""
        item = claim_one(campaign, member, contact)
        svc.record_result(item.mailing_id, member, status="sent",
                          message_id="m1", thread_id="t1")

        out = svc.record_result(item.mailing_id, member, status="failed",
                                error="late report")

        assert out["detail"] == "already settled"
        row = CampaignMailing.objects.get(id=item.mailing_id)
        assert row.status == MailingStatus.SENT.value
        assert row.mail_message_id == "m1"
        assert row.error_detail == ""

    def test_one_member_cannot_settle_anothers_mailing(
        self, campaign, member, other_member, contact
    ):
        """`sent_by` is the audit trail and the daily cap. If anyone could report
        anyone's mailing, both would be suggestions."""
        item = claim_one(campaign, member, contact)

        out = svc.record_result(item.mailing_id, other_member, status="sent",
                                message_id="m1", thread_id="t1")

        assert out["status"] == svc.NOT_ASSIGNED
        assert CampaignMailing.objects.get(
            id=item.mailing_id
        ).status == MailingStatus.DRAFT.value

    def test_an_unknown_mailing_is_refused_rather_than_500ing(self, member):
        out = svc.record_result(
            "11111111-1111-1111-1111-111111111111", member, status="sent"
        )
        assert out["status"] == svc.FAILED
        assert "no such mailing" in out["detail"]

    def test_anything_that_is_not_sent_counts_as_failed(
        self, campaign, member, contact
    ):
        """Fail closed. An unrecognised status must not be read as success."""
        item = claim_one(campaign, member, contact)

        svc.record_result(item.mailing_id, member, status="banana")

        assert CampaignMailing.objects.get(
            id=item.mailing_id
        ).status == MailingStatus.FAILED.value


class TestStrandedDrafts:
    def test_a_claimed_but_unreported_mailing_is_listed(
        self, campaign, member, contact
    ):
        item = claim_one(campaign, member, contact)

        stranded = list(svc.stranded_drafts(member))
        assert [str(m.id) for m in stranded] == [str(item.mailing_id)]

    def test_a_settled_mailing_is_not(self, campaign, member, contact):
        item = claim_one(campaign, member, contact)
        svc.record_result(item.mailing_id, member, status="sent",
                          message_id="m1", thread_id="t1")

        assert list(svc.stranded_drafts(member)) == []

    def test_it_is_scoped_to_the_member(
        self, campaign, member, other_member, contact
    ):
        claim_one(campaign, member, contact)
        assert list(svc.stranded_drafts(other_member)) == []


class TestDailyCap:
    def test_the_cap_is_counted_across_every_device(self, campaign, member):
        """Enforced server-side on purpose: a per-laptop cap would be no cap at
        all, and tripping Gmail's quota throttles the mailbox for hours."""
        now = timezone.now()
        for i in range(svc.DAILY_SEND_CAP):
            c = Contact.objects.create(
                first_name=f"P{i}", email=f"p{i}@example.com", company="Acme",
                assigned_to=member,
            )
            CampaignMailing.objects.create(
                campaign=campaign, contact=c, sent_by=member,
                status=MailingStatus.SENT.value, sent_at=now,
            )

        fresh = Contact.objects.create(
            first_name="One", email="more@example.com", company="Acme",
            assigned_to=member,
        )
        claimed, skipped = svc.claim_batch(campaign, member, [fresh.id])

        assert claimed == []
        assert skipped[0].code == svc.CAP_REACHED


class TestPreflight:
    def test_it_writes_nothing(self, campaign, member, contact):
        rows = svc.preflight(campaign, member, [contact.id])

        assert rows[0]["status"] == svc.OK
        assert CampaignMailing.objects.count() == 0

    def test_it_reports_the_same_refusals_the_claim_would(
        self, campaign, member, other_member
    ):
        theirs = Contact.objects.create(
            first_name="Someone", email="theirs@example.com", company="Acme",
            assigned_to=other_member,
        )

        rows = svc.preflight(campaign, member, [theirs.id])
        assert rows[0]["status"] == svc.NOT_ASSIGNED
