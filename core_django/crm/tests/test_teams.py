"""Teams, join codes, roles, and distribution.

Two things here are worth more than the rest.

`TestJoining` pins the shape of the new front door. The old one was a dropdown
of names with no password; this one requires a Google-verified BITS identity
AND a code. Neither half is sufficient, and the tests say so directly — because
a regression that made either half optional would look like a working login.

`TestDistribution` pins that a lead can only ever hand work to their own team.
The queryset it replaced was `TeamMember.objects.filter(is_active=True)`, which
let a lead of one cohort assign to another cohort's members.
"""

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from crm.models import (
    Campaign,
    CampaignMailing,
    Contact,
    ContactAudit,
    Team,
    TeamMember,
    TeamMembership,
)
from crm.services import assignment
from crm.services import teams as team_svc
from crm.services.permissions import assignable_members, is_lead
from shared.enums import CampaignStatus, MailingStatus

from .conftest import make_lead, make_member, sign_in

pytestmark = pytest.mark.django_db


@pytest.fixture
def lead(team):
    return make_lead(team)


@pytest.fixture
def member(team):
    return make_member(team)


def make_contacts(n, *, assigned_to=None, prefix="p"):
    return [
        Contact.objects.create(
            first_name=f"{prefix}{i}", email=f"{prefix}{i}@example.com",
            company="Acme", assigned_to=assigned_to,
        )
        for i in range(n)
    ]


# --- join codes ------------------------------------------------------------


class TestJoinCodes:
    def test_a_generated_code_avoids_ambiguous_characters(self):
        """A code is read aloud across a room. O/0 and I/1/L are support
        requests waiting to happen."""
        for _ in range(50):
            code = Team.generate_join_code()
            assert len(code) == 10
            assert not (set(code) & set("O0I1LUV"))

    def test_codes_are_not_predictable(self):
        codes = {Team.generate_join_code() for _ in range(200)}
        assert len(codes) == 200

    def test_rotation_invalidates_the_old_code(self, team, lead):
        old = team.join_code
        new = team_svc.rotate_join_code(team, lead)

        assert new != old
        assert team_svc.team_for_code(old) is None
        assert team_svc.team_for_code(new) == team

    def test_an_inactive_team_accepts_nobody(self, team):
        team.is_active = False
        team.save()
        assert team_svc.team_for_code(team.join_code) is None


class TestJoining:
    def test_a_valid_code_creates_the_member_and_membership(self, team):
        member = team_svc.join(
            code=team.join_code,
            email="f20250882@pilani.bits-pilani.ac.in",
            display_name="Nikhil Sharma",
        )

        assert member.name == "Nikhil Sharma"
        assert member.bits_email == "f20250882@pilani.bits-pilani.ac.in"
        # Inferred from the address, and a display field only.
        assert member.batch == "2025"
        assert member.memberships.get(team=team).role == "member"
        # Never a lead by joining. The code is handed around; leadership is not.
        assert is_lead(member) is False

    def test_a_bad_code_creates_nothing(self, team):
        with pytest.raises(ValidationError, match="not valid"):
            team_svc.join(code="WRONGCODE1", email="x@pilani.bits-pilani.ac.in")
        assert TeamMember.objects.count() == 0

    def test_a_non_bits_address_is_refused_even_with_a_good_code(self, team):
        """The domain check upstream is the real control; this is the last line
        of defence if it is ever loosened."""
        with pytest.raises(ValidationError):
            team_svc.join(code=team.join_code, email="someone@gmail.com")
        assert TeamMember.objects.count() == 0

    def test_joining_twice_is_idempotent(self, team):
        first = team_svc.join(code=team.join_code, email="a@pilani.bits-pilani.ac.in")
        second = team_svc.join(code=team.join_code, email="a@pilani.bits-pilani.ac.in")

        assert first.id == second.id
        assert TeamMembership.objects.filter(member=first).count() == 1

    def test_a_deactivated_member_is_reactivated_by_a_valid_code(self, team):
        member = team_svc.join(code=team.join_code, email="a@pilani.bits-pilani.ac.in")
        TeamMember.objects.filter(pk=member.pk).update(is_active=False)

        again = team_svc.join(code=team.join_code, email="a@pilani.bits-pilani.ac.in")
        assert again.is_active is True

    def test_a_second_team_does_not_disturb_the_first(self, team):
        member = team_svc.join(code=team.join_code, email="a@pilani.bits-pilani.ac.in")
        other = Team.objects.create(name="Other", join_code="OTHERCODE1")

        team_svc.join(code=other.join_code, email="a@pilani.bits-pilani.ac.in")
        assert member.memberships.count() == 2


class TestTheJoinScreen:
    def test_it_is_a_dead_end_without_a_verified_identity(self, client):
        """There is deliberately no field to type an address into: a field
        would be a field an attacker could type into."""
        r = client.get(reverse("join"))
        assert r.status_code == 302
        assert r["Location"] == reverse("login")

    def test_a_verified_identity_plus_a_code_signs_you_in(self, client, team):
        from crm.services import auth as auth_svc

        session = client.session
        session["pending_join"] = {
            "email": "f20250882@pilani.bits-pilani.ac.in", "name": "Nikhil",
        }
        session.save()

        r = client.post(reverse("join"), {"code": team.join_code})
        assert r.status_code == 302
        assert auth_svc.SESSION_KEY in client.session
        assert TeamMember.objects.filter(
            bits_email="f20250882@pilani.bits-pilani.ac.in"
        ).exists()

    def test_guessing_is_rate_limited(self, client, team):
        session = client.session
        session["pending_join"] = {"email": "a@pilani.bits-pilani.ac.in", "name": ""}
        session.save()

        for _ in range(team_svc.MAX_JOIN_ATTEMPTS):
            client.post(reverse("join"), {"code": "BADCODE001"})

        r = client.post(reverse("join"), {"code": team.join_code})
        assert r["Location"] == reverse("login")
        assert TeamMember.objects.count() == 0


# --- roles -----------------------------------------------------------------


class TestRoles:
    def test_a_lead_can_promote_someone(self, team, lead, member):
        team_svc.set_role(team, member, "lead", actor=lead)
        assert is_lead(TeamMember.objects.get(pk=member.pk)) is True

    def test_the_last_lead_cannot_be_demoted(self, team, lead):
        """A team with no lead is a team nobody can assign work in, with no
        remaining path to fix it short of /admin/."""
        with pytest.raises(ValidationError, match="only lead"):
            team_svc.set_role(team, lead, "member", actor=lead)

    def test_a_lead_can_step_down_once_someone_else_leads(self, team, lead, member):
        team_svc.set_role(team, member, "lead", actor=lead)
        team_svc.set_role(team, lead, "member", actor=lead)
        assert is_lead(TeamMember.objects.get(pk=lead.pk)) is False

    def test_the_last_lead_cannot_be_removed_either(self, team, lead):
        with pytest.raises(ValidationError, match="only lead"):
            team_svc.remove(team, lead, actor=lead)


class TestAssignableMembers:
    def test_a_lead_sees_their_own_teams_members(self, team, lead, member):
        assert set(assignable_members(lead)) == {lead, member}

    def test_a_lead_cannot_reach_another_teams_members(self, team, lead):
        other = Team.objects.create(name="Other", join_code="OTHERCODE1")
        stranger = make_member(
            other, name="Stranger", email="stranger@pilani.bits-pilani.ac.in"
        )
        assert stranger not in assignable_members(lead)

    def test_a_non_lead_can_assign_to_nobody(self, team, member):
        assert list(assignable_members(member)) == []

    def test_the_assign_screen_refuses_a_posted_outsider(self, client, team, lead):
        """A posted member id from another cohort must find nothing rather than
        being honoured."""
        other = Team.objects.create(name="Other", join_code="OTHERCODE1")
        stranger = make_member(
            other, name="Stranger", email="stranger@pilani.bits-pilani.ac.in"
        )
        contact = make_contacts(1)[0]
        sign_in(client, lead)

        client.post(reverse("crm:assign_apply"), {
            "contact_ids": [str(contact.id)], "member": str(stranger.id),
        })

        contact.refresh_from_db()
        assert contact.assigned_to_id is None


# --- distribution ----------------------------------------------------------


class TestDistribution:
    def test_it_deals_evenly(self, team, lead, member):
        contacts = make_contacts(10)
        plan = assignment.plan_distribution(contacts, [lead, member])

        assert plan.total == 10
        assert len(plan.per_member[lead]) == 5
        assert len(plan.per_member[member]) == 5

    def test_an_odd_number_splits_without_dropping_anyone(self, team, lead, member):
        plan = assignment.plan_distribution(make_contacts(7), [lead, member])
        assert plan.total == 7
        assert sorted(len(v) for v in plan.per_member.values()) == [3, 4]

    def test_planning_writes_nothing(self, team, lead, member):
        contacts = make_contacts(4)
        assignment.plan_distribution(contacts, [lead, member])

        for c in contacts:
            c.refresh_from_db()
            assert c.assigned_to_id is None

    def test_committing_assigns_and_audits(self, team, lead, member):
        contacts = make_contacts(4)
        assignment.distribute(contacts, [lead, member], actor=lead)

        assigned = {c.assigned_to_id for c in Contact.objects.all()}
        assert assigned == {lead.id, member.id}
        # Assignment was the ONE contact mutation that wrote no audit row, so
        # "who gave this to me" was the one question the log could not answer.
        assert ContactAudit.objects.filter(field="assigned_to").count() == 4

    def test_a_contact_the_team_already_mailed_is_skipped(self, team, lead, member):
        """The assignment-time half of the guarantee the constraint enforces at
        send time."""
        root = Campaign.objects.create(
            title="Ignite", mail_sub="s", mail_body="b",
            status=CampaignStatus.ACTIVE.value, team=team,
        )
        contacts = make_contacts(4)
        CampaignMailing.objects.create(
            campaign=root, contact=contacts[0], sent_by=member,
            status=MailingStatus.SENT.value,
        )

        plan = assignment.distribute(
            contacts, [lead, member], actor=lead, root_campaign=root
        )

        assert plan.total == 3
        assert contacts[0].email in [email for email, _ in plan.skipped]

    def test_a_contact_mid_conversation_is_not_silently_moved(self, team, lead, member):
        root = Campaign.objects.create(
            title="Ignite", mail_sub="s", mail_body="b", team=team
        )
        contact = make_contacts(1, assigned_to=member)[0]
        CampaignMailing.objects.create(
            campaign=root, contact=contact, sent_by=member,
            status=MailingStatus.SENT.value,
        )

        result = assignment.bulk_assign([contact.id], lead, actor=lead)

        assert result.assigned == 0
        contact.refresh_from_db()
        assert contact.assigned_to_id == member.id

    def test_distributing_to_nobody_is_refused_rather_than_silent(self, team, lead):
        with pytest.raises(ValidationError, match="No members"):
            assignment.plan_distribution(make_contacts(3), [])


class TestUnassign:
    def test_it_audits(self, team, lead, member):
        contact = make_contacts(1, assigned_to=member)[0]
        assignment.bulk_unassign([contact.id], actor=lead)

        contact.refresh_from_db()
        assert contact.assigned_to_id is None
        assert ContactAudit.objects.filter(field="assigned_to").count() == 1

    def test_it_guards_mail_history_like_assignment_does(self, team, lead, member):
        """It was a blanket .update() with no guard at all -- the exact inverse
        of bulk_assign, with none of its protections."""
        root = Campaign.objects.create(
            title="Ignite", mail_sub="s", mail_body="b", team=team
        )
        contact = make_contacts(1, assigned_to=member)[0]
        CampaignMailing.objects.create(
            campaign=root, contact=contact, sent_by=member,
            status=MailingStatus.SENT.value,
        )

        result = assignment.bulk_unassign([contact.id], actor=lead)
        assert result.assigned == 0
        contact.refresh_from_db()
        assert contact.assigned_to_id == member.id

        forced = assignment.bulk_unassign([contact.id], actor=lead, force=True)
        assert forced.assigned == 1


class TestTheAssignedToField:
    def test_a_lead_can_actually_reassign_from_the_edit_form(self, team, lead, member):
        """The form has always OFFERED this to leads while update() silently
        dropped it -- the page said 'Saved.' and nothing changed."""
        from crm.services import contacts as contact_svc

        contact = make_contacts(1, assigned_to=lead)[0]
        contact_svc.update(contact, {"assigned_to": member}, lead)

        contact.refresh_from_db()
        assert contact.assigned_to_id == member.id

    def test_a_member_still_cannot(self, team, lead, member):
        from crm.services import contacts as contact_svc

        contact = make_contacts(1, assigned_to=member)[0]
        contact_svc.update(contact, {"assigned_to": lead}, member)

        contact.refresh_from_db()
        assert contact.assigned_to_id == member.id


# --- the screens -----------------------------------------------------------


class TestTeamScreens:
    def test_a_member_sees_their_team_but_cannot_manage_it(self, client, team, member):
        sign_in(client, member)

        assert team.name in client.get(reverse("crm:team_list")).content.decode()
        # Django turns PermissionDenied into a 403 rather than propagating it.
        assert client.get(
            reverse("crm:team_detail", args=[team.pk])
        ).status_code == 403

    def test_the_join_code_is_never_shown_to_a_member(self, client, team, member):
        sign_in(client, member)
        assert team.join_code not in client.get(
            reverse("crm:team_list")
        ).content.decode()

    def test_a_lead_sees_the_code(self, client, team, lead):
        sign_in(client, lead)
        assert team.join_code in client.get(
            reverse("crm:team_detail", args=[team.pk])
        ).content.decode()

    def test_a_lead_cannot_manage_a_team_they_do_not_lead(self, client, team, lead):
        other = Team.objects.create(name="Other", join_code="OTHERCODE1")
        sign_in(client, lead)

        assert client.get(
            reverse("crm:team_detail", args=[other.pk])
        ).status_code == 404
