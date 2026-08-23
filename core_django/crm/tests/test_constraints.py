"""The idempotency guarantee, proven at the database level.

If this file ever fails, the system can send duplicate mail. Treat it as the
most important test in the repo.
"""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from crm.models import Campaign, CampaignMailing, Contact, TeamMember
from crm.services import campaigns as campaign_svc
from crm.services.permissions import is_lead
from shared.enums import CampaignStatus, MailingStatus

from .conftest import make_lead, make_member

pytestmark = pytest.mark.django_db


@pytest.fixture
def member(team):
    return make_lead(team)


@pytest.fixture
def contact():
    return Contact.objects.create(
        first_name="Rohan", last_name="Iyer", email="rohan@example.com", company="Zerodha"
    )


@pytest.fixture
def campaign(member):
    return Campaign.objects.create(
        title="Outreach",
        mail_sub="Hello {{ first_name }}",
        mail_body="About {{ company }}",
        var_list=["first_name", "company"],
        status=CampaignStatus.ACTIVE.value,
        created_by=member,
    )


def test_same_campaign_contact_pair_cannot_be_inserted_twice(campaign, contact, member):
    CampaignMailing.objects.create(
        campaign=campaign, contact=contact, sent_by=member, status=MailingStatus.SENT.value
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            CampaignMailing.objects.create(
                campaign=campaign, contact=contact, sent_by=member,
                status=MailingStatus.DRAFT.value,
            )

    assert CampaignMailing.objects.count() == 1


def test_a_failed_mailing_still_blocks_a_second_insert(campaign, contact, member):
    """Retry must UPDATE the existing row, never INSERT a new one."""
    CampaignMailing.objects.create(
        campaign=campaign, contact=contact, sent_by=member,
        status=MailingStatus.FAILED.value, error_detail="quota exceeded",
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            CampaignMailing.objects.create(
                campaign=campaign, contact=contact, sent_by=member
            )


def test_same_contact_in_a_different_campaign_is_allowed(campaign, contact, member):
    other = Campaign.objects.create(
        title="Second", mail_sub="Hi", mail_body="Body", created_by=member
    )
    CampaignMailing.objects.create(campaign=campaign, contact=contact, sent_by=member)
    CampaignMailing.objects.create(campaign=other, contact=contact, sent_by=member)

    assert CampaignMailing.objects.filter(contact=contact).count() == 2


def test_contact_email_is_unique(contact):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Contact.objects.create(
                first_name="Someone", email=contact.email, company="Elsewhere"
            )


def test_non_bits_email_is_rejected():
    member = TeamMember(name="Outsider", bits_email="hacker@gmail.com", batch="2024")
    with pytest.raises(ValidationError):
        member.full_clean()


class TestPermissions:
    """A lead is a ROLE on an active team. The batch grants nothing."""

    def test_a_lead_role_makes_a_lead(self, member):
        assert is_lead(member) is True

    def test_the_batch_alone_grants_nothing(self, team):
        """The old rule was `batch == "2024"`. This member has that batch and no
        membership, and must not be a lead -- otherwise the flip did not
        actually happen and every graduating year re-opens the hole."""
        stale = TeamMember.objects.create(
            name="Ghost", bits_email="ghost@pilani.bits-pilani.ac.in", batch="2024"
        )
        assert is_lead(stale) is False

    def test_an_ordinary_member_is_not_a_lead(self, team):
        assert is_lead(make_member(team)) is False

    def test_inactive_lead_is_not_a_lead(self, member):
        member.is_active = False
        assert is_lead(member) is False

    def test_a_lead_of_a_defunct_team_keeps_no_powers(self, team, member):
        """Otherwise last year's leads hold the contact pool forever."""
        assert is_lead(member) is True
        team.is_active = False
        team.save()

        # Re-fetched, not refresh_from_db(): the answer is cached on the
        # INSTANCE, and refresh_from_db reloads field values without clearing
        # attributes set on it. That is the intended lifetime -- get_member()
        # resolves a fresh TeamMember per request, so a role change takes effect
        # on the member's next page load. Re-fetching here is what a second
        # request does.
        assert is_lead(TeamMember.objects.get(pk=member.pk)) is False

    def test_a_deactivated_membership_revokes_the_role(self, team, member):
        member.memberships.update(is_active=False)
        assert is_lead(TeamMember.objects.get(pk=member.pk)) is False


class TestCampaignTransitions:
    def test_draft_to_active_is_allowed(self, member):
        c = Campaign.objects.create(
            title="C", mail_sub="Hi {{ first_name }}", mail_body="x",
            var_list=["first_name"], created_by=member,
        )
        campaign_svc.transition(c, CampaignStatus.ACTIVE)
        c.refresh_from_db()
        assert c.status == CampaignStatus.ACTIVE.value

    def test_draft_to_completed_is_rejected(self, member):
        c = Campaign.objects.create(title="C", mail_sub="s", mail_body="b", created_by=member)
        with pytest.raises(ValidationError):
            campaign_svc.transition(c, CampaignStatus.COMPLETED)

    def test_activation_rejects_an_undeclared_placeholder(self, member):
        c = Campaign.objects.create(
            title="C", mail_sub="Hi {{ first_name }}", mail_body="At {{ company }}",
            var_list=["first_name"], created_by=member,   # company not declared
        )
        with pytest.raises(ValidationError, match="missing from var_list"):
            campaign_svc.transition(c, CampaignStatus.ACTIVE)

    def test_activation_rejects_a_typo_variable(self, member):
        c = Campaign.objects.create(
            title="C", mail_sub="Hi {{ compnay }}", mail_body="b",
            var_list=["compnay"], created_by=member,
        )
        with pytest.raises(ValidationError, match="not Contact fields"):
            campaign_svc.transition(c, CampaignStatus.ACTIVE)

    def test_archived_is_terminal(self, member):
        c = Campaign.objects.create(
            title="C", mail_sub="s", mail_body="b",
            status=CampaignStatus.ARCHIVED.value, created_by=member,
        )
        with pytest.raises(ValidationError):
            campaign_svc.transition(c, CampaignStatus.ACTIVE)
