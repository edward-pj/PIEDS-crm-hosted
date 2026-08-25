"""Shared editing of the master contact pool.

Two batches now write to the same rows from two different apps. These tests pin
down the two rules that make that safe:

  * a 2025 member may only change contacts assigned to them
  * `lifecycle` is server-owned -- it moves on a confirmed send, never backwards,
    and a non-lead cannot set it by hand

Plus the two states that must actually refuse mail rather than merely being
hidden from a list.
"""


import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from crm.models import Campaign, CampaignMailing, Contact, ContactAudit, TeamMember
from crm.services import auth as auth_svc
from crm.services import permissions
from crm.services import contacts as contact_svc
from crm.services import mailing as mailing_svc
from shared.enums import CampaignStatus, ContactLifecycle, MailingStatus

from .conftest import make_lead

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def lead(team):
    """A lead is now a ROLE on a team, not a batch. `make_lead` is the only
    correct way to build one -- see tests/conftest.py."""
    user = User.objects.create_user("aarav", password="x")
    return make_lead(team, user=user)


@pytest.fixture
def member():
    user = User.objects.create_user("kabir", password="x")
    return TeamMember.objects.create(
        name="Kabir", bits_email="kabir@pilani.bits-pilani.ac.in",
        batch="2025", user=user,
    )


@pytest.fixture
def other():
    user = User.objects.create_user("ishita", password="x")
    return TeamMember.objects.create(
        name="Ishita", bits_email="ishita@pilani.bits-pilani.ac.in",
        batch="2025", user=user,
    )


@pytest.fixture
def mine(member):
    return Contact.objects.create(
        first_name="Rohan", last_name="Iyer", email="rohan@example.com",
        company="Zerodha", designation="CTO", assigned_to=member,
    )


@pytest.fixture
def theirs(other):
    return Contact.objects.create(
        first_name="Meera", email="meera@example.com",
        company="Razorpay", designation="VP", assigned_to=other,
    )


@pytest.fixture
def campaign(lead):
    return Campaign.objects.create(
        title="Outreach",
        mail_sub="{{ company }} x PIEDS",
        mail_body="Hi {{ first_name }}, about your work as {{ designation }}.",
        var_list=["company", "first_name", "designation"],
        status=CampaignStatus.ACTIVE.value,
        created_by=lead,
    )


# ------------------------------------------------------- scoping: the core

class TestEditScope:
    def test_member_may_edit_own_contact(self, member, mine):
        contact_svc.update(mine, {"designation": "Founder"}, member)
        mine.refresh_from_db()
        assert mine.designation == "Founder"

    def test_member_may_not_edit_someone_elses(self, member, theirs):
        with pytest.raises(PermissionDenied):
            contact_svc.update(theirs, {"designation": "hijacked"}, member)
        theirs.refresh_from_db()
        assert theirs.designation == "VP"

    def test_lead_may_edit_anyones(self, lead, theirs):
        contact_svc.update(theirs, {"designation": "Director"}, lead)
        theirs.refresh_from_db()
        assert theirs.designation == "Director"

    def test_created_contact_is_forced_onto_its_creator(self, member, other):
        """Posting someone else's member id must not park work on them."""
        contact = contact_svc.create(
            {"first_name": "Neha", "email": "neha@example.com",
             "company": "Swiggy", "assigned_to": other},
            member,
        )
        assert contact.assigned_to_id == member.id
        assert contact.created_by_id == member.id

    def test_lead_may_choose_the_assignee_on_create(self, lead, member):
        contact = contact_svc.create(
            {"first_name": "Neha", "email": "neha@example.com",
             "company": "Swiggy", "assigned_to": member},
            lead,
        )
        assert contact.assigned_to_id == member.id


class TestLifecycleIsServerOwned:
    def test_member_cannot_set_lifecycle_by_hand(self, member, mine):
        contact_svc.update(
            mine, {"lifecycle": ContactLifecycle.REPLIED.value, "company": "Zerodha Ltd"}, member
        )
        mine.refresh_from_db()
        # The permitted field went through; the forbidden one was dropped.
        assert mine.company == "Zerodha Ltd"
        assert mine.lifecycle == ContactLifecycle.NEW.value

    def test_lead_can_set_lifecycle_by_hand(self, lead, mine):
        contact_svc.update(mine, {"lifecycle": ContactLifecycle.REPLIED.value}, lead)
        mine.refresh_from_db()
        assert mine.lifecycle == ContactLifecycle.REPLIED.value


# -------------------------------------------------------------- bulk edit

class TestBulkEdit:
    def test_tags_are_added_and_removed(self, member, mine):
        mine.tags = ["old", "keep"]
        mine.save()

        contact_svc.bulk_edit(
            [mine.id], member, tags_add="fintech, Priority", tags_remove="old"
        )
        mine.refresh_from_db()
        # Lowercased on the way in, so one idea cannot become two facets.
        assert mine.tags == ["keep", "fintech", "priority"]

    def test_skips_contacts_that_are_not_yours(self, member, mine, theirs):
        result = contact_svc.bulk_edit([mine.id, theirs.id], member, company="Acme")

        assert result.updated == 1
        assert len(result.skipped) == 1
        mine.refresh_from_db()
        theirs.refresh_from_db()
        assert mine.company == "Acme"
        assert theirs.company == "Razorpay"      # untouched

    def test_member_cannot_bulk_set_lifecycle(self, member, mine):
        with pytest.raises(PermissionDenied):
            contact_svc.bulk_edit([mine.id], member, lifecycle=ContactLifecycle.REPLIED.value)


# ------------------------------------------------------ archive vs delete

class TestArchiveAndDelete:
    def test_archiving_marks_the_contact_unmailable(self, member, mine):
        contact_svc.set_archived(mine, member, archived=True)
        mine.refresh_from_db()
        assert mine.is_archived
        assert mine.archived_by_id == member.id
        assert not mine.is_mailable

    def test_member_cannot_archive_someone_elses(self, member, theirs):
        with pytest.raises(PermissionDenied):
            contact_svc.set_archived(theirs, member, archived=True)

    def test_unmailed_contact_can_be_deleted(self, lead, mine):
        contact_svc.hard_delete(mine, lead)
        assert not Contact.objects.filter(pk=mine.pk).exists()

    def test_mailed_contact_is_refused_with_an_explanation(self, lead, member, mine, campaign):
        CampaignMailing.objects.create(
            campaign=campaign, contact=mine, sent_by=member,
            status=MailingStatus.SENT.value,
        )
        # PROTECT would raise IntegrityError; we want a sentence instead.
        with pytest.raises(ValidationError) as exc:
            contact_svc.hard_delete(mine, lead)

        assert "archive" in " ".join(exc.value.messages).lower()
        assert Contact.objects.filter(pk=mine.pk).exists()

    def test_member_cannot_hard_delete(self, member, mine):
        with pytest.raises(PermissionDenied):
            contact_svc.hard_delete(mine, member)


# --------------------------------------------- blocked states refuse mail

class TestBlockedStatesRefuseMail:
    def test_archived_contact_is_not_claimed(self, member, mine, campaign):
        contact_svc.set_archived(mine, member, archived=True)

        claimed, skipped = mailing_svc.claim_batch(campaign, member, [mine.id])

        assert claimed == []
        assert "archived" in skipped[0].reason
        assert not CampaignMailing.objects.filter(contact=mine).exists()

    def test_do_not_contact_is_not_claimed(self, lead, member, mine, campaign):
        contact_svc.update(mine, {"lifecycle": ContactLifecycle.DO_NOT_CONTACT.value}, lead)

        claimed, skipped = mailing_svc.claim_batch(campaign, member, [mine.id])

        assert claimed == []
        assert "do not contact" in skipped[0].reason.lower()
        assert not CampaignMailing.objects.filter(contact=mine).exists()

    def test_preflight_reports_the_same_refusal_as_claim(self, member, mine, campaign):
        """A dry run that disagreed with the real thing would be worse than none."""
        contact_svc.set_archived(mine, member, archived=True)

        results = mailing_svc.preflight(campaign, member, [mine.id])
        assert results[0]["status"] == mailing_svc.ARCHIVED


# ------------------------------------------ the automatic lifecycle flip

class TestLifecycleFlipOnSend:
    def _claim(self, campaign, member, contact):
        claimed, _ = mailing_svc.claim_batch(campaign, member, [contact.id])
        return claimed[0]

    def test_new_becomes_contacted(self, member, mine, campaign):
        claim = self._claim(campaign, member, mine)
        mailing_svc.record_result(
            claim.mailing_id, member, status="sent", message_id="m1", thread_id="t1"
        )
        mine.refresh_from_db()
        assert mine.lifecycle == ContactLifecycle.CONTACTED.value

    def test_replied_is_never_dragged_backwards(self, lead, member, mine, campaign):
        """A later campaign must not undo a human's read of the situation."""
        contact_svc.update(mine, {"lifecycle": ContactLifecycle.REPLIED.value}, lead)

        claim = self._claim(campaign, member, mine)
        mailing_svc.record_result(
            claim.mailing_id, member, status="sent", message_id="m1", thread_id="t1"
        )
        mine.refresh_from_db()
        assert mine.lifecycle == ContactLifecycle.REPLIED.value

    def test_a_failed_send_does_not_move_the_lifecycle(self, member, mine, campaign):
        claim = self._claim(campaign, member, mine)
        mailing_svc.record_result(claim.mailing_id, member, status="failed", error="smtp down")
        mine.refresh_from_db()
        assert mine.lifecycle == ContactLifecycle.NEW.value


# ------------------------------------------------------------ audit trail

class TestAuditTrail:
    def test_every_change_names_its_actor(self, member, mine):
        contact_svc.update(mine, {"company": "Zerodha Ltd", "designation": "Founder"}, member)

        audits = ContactAudit.objects.filter(contact=mine)
        assert audits.count() == 2
        assert {a.field for a in audits} == {"company", "designation"}
        assert all(a.actor_id == member.id for a in audits)

        company = audits.get(field="company")
        assert company.old_value == "Zerodha"
        assert company.new_value == "Zerodha Ltd"

    def test_a_no_op_edit_writes_nothing(self, member, mine):
        contact_svc.update(mine, {"company": "Zerodha"}, member)
        assert not ContactAudit.objects.filter(contact=mine, field="company").exists()

    def test_archiving_is_recorded(self, member, mine):
        contact_svc.set_archived(mine, member, archived=True)
        assert ContactAudit.objects.filter(contact=mine, field="is_archived").exists()


# --------------------------------------------------------- the HTTP layer

def signin(client, member):
    """Establish a CRM session. Identity is a session key, not a Django User."""
    session = client.session
    session[auth_svc.SESSION_KEY] = str(member.id)
    session.save()


class TestWebViews:
    def test_member_gets_403_editing_someone_elses(self, client, member, theirs):
        signin(client, member)
        r = client.get(reverse("crm:contact_edit", args=[theirs.pk]))
        assert r.status_code == 403

    def test_member_can_open_their_own(self, client, member, mine):
        signin(client, member)
        r = client.get(reverse("crm:contact_edit", args=[mine.pk]))
        assert r.status_code == 200

    def test_lead_can_open_anyones(self, client, lead, theirs):
        signin(client, lead)
        assert client.get(reverse("crm:contact_edit", args=[theirs.pk])).status_code == 200

    def test_member_cannot_reach_the_delete_route(self, client, member, mine):
        signin(client, member)
        r = client.post(reverse("crm:contact_delete", args=[mine.pk]))
        assert r.status_code == 403
        assert Contact.objects.filter(pk=mine.pk).exists()

    def test_archived_contacts_are_hidden_from_the_list_by_default(
        self, client, member, mine
    ):
        signin(client, member)
        contact_svc.set_archived(mine, member, archived=True)

        assert mine.email not in client.get(reverse("crm:contact_list")).content.decode()
        shown = client.get(reverse("crm:contact_list"), {"archived": "1"}).content.decode()
        assert mine.email in shown


class TestEditPermissionsAtTheServiceLayer:
    """These used to go through the token API, which the laptop agent used.

    The API is gone with the agent, but the RULES it exercised are not -- they
    are enforced in services/contacts.py, which is where they always were. The
    API tested them at one remove; these test them directly, which is if
    anything a better place for them.
    """

    def test_a_member_can_edit_their_own_contact(self, member, mine):
        contact_svc.update(mine, {"designation": "Founder", "tags": ["fintech"]}, member)

        mine.refresh_from_db()
        assert mine.designation == "Founder"
        assert mine.tags == ["fintech"]

    def test_a_member_cannot_edit_someone_elses(self, member, theirs):
        with pytest.raises(PermissionDenied):
            contact_svc.update(theirs, {"designation": "hijacked"}, member)

        theirs.refresh_from_db()
        assert theirs.designation == "VP"

    def test_a_member_may_still_edit_a_contact_they_archived(self, member, mine):
        """Archiving hides a contact from lists and refuses mail to them; it
        does not hand ownership back. Un-archiving has to stay possible for the
        person who did it -- see TestBlockedStatesRefuseMail for the half that
        actually stops mail."""
        contact_svc.set_archived(mine, member, archived=True)
        assert mine in permissions.editable_contacts(member)
