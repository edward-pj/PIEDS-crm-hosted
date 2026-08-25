"""The one command a fresh deployment cannot do without.

Signing in requires a `TeamMember`; becoming one requires a join code; a join
code requires a `Team`. Nothing in the app creates the first link in that chain,
so `bootstrap_team` is the only way a new database ever gets its first person in.
If it breaks, a correct deploy is an unusable one -- and it breaks in the place
nobody looks, because everything else about the app is fine.

It is also run against the *production* database from somebody's laptop (Render's
free plan has no shell), which is why the re-run behaviour is tested as carefully
as the first run: the second invocation is the dangerous one.
"""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from crm.models import Contact, Team, TeamMember, TeamMembership
from crm.services import permissions
from shared.enums import ContactLifecycle

pytestmark = pytest.mark.django_db

EMAIL = "kabir@pilani.bits-pilani.ac.in"


def bootstrap(*args, **kwargs):
    call_command("bootstrap_team", *args, **kwargs)


class TestTheFirstRun:
    def test_it_creates_a_team_a_lead_and_a_membership(self):
        bootstrap(EMAIL, team="Ignite 26", name="Kabir Rao")

        team = Team.objects.get(name="Ignite 26")
        member = TeamMember.objects.get(bits_email=EMAIL)
        membership = TeamMembership.objects.get(team=team, member=member)

        assert member.name == "Kabir Rao"
        assert membership.role == "lead"
        assert membership.is_active and team.is_active

    def test_the_lead_is_actually_a_lead(self):
        """The whole point. A membership row with the wrong role would look
        correct in the database and refuse every screen in the app."""
        bootstrap(EMAIL)

        member = TeamMember.objects.get(bits_email=EMAIL)
        assert permissions.is_lead(member)

    def test_the_join_code_is_real_and_rotatable(self):
        bootstrap(EMAIL)

        code = Team.objects.get().join_code
        assert len(code) >= 10
        assert code != "ROTATE-ME"

    def test_a_name_is_derived_when_none_is_given(self):
        bootstrap(EMAIL)
        assert TeamMember.objects.get(bits_email=EMAIL).name == "Kabir"


class TestTheSecondRun:
    """Re-running is expected -- a typo in the name, a second lead, a team that
    already exists. None of it may destroy anything."""

    def test_it_does_not_duplicate_the_team_or_the_membership(self):
        bootstrap(EMAIL, team="Ignite 26")
        bootstrap(EMAIL, team="Ignite 26")

        assert Team.objects.count() == 1
        assert TeamMembership.objects.count() == 1

    def test_it_does_not_rotate_a_code_people_are_already_using(self):
        """Silently rotating would lock out everyone mid-onboarding, and the
        person re-running the command would have no idea they had done it."""
        bootstrap(EMAIL, team="Ignite 26")
        first = Team.objects.get().join_code

        bootstrap(EMAIL, team="Ignite 26")
        assert Team.objects.get().join_code == first

    def test_rotate_code_rotates_it(self):
        bootstrap(EMAIL, team="Ignite 26")
        first = Team.objects.get().join_code

        bootstrap(EMAIL, team="Ignite 26", rotate_code=True)
        assert Team.objects.get().join_code != first

    def test_it_does_not_clobber_a_real_name_with_a_roll_number(self):
        """`f20250882@...` derives to "F20250882". A re-run without --name that
        overwrote "Pratham Jain" with that would be a silent regression in the
        From line of every mail that member sends."""
        bootstrap(EMAIL, name="Kabir Rao")
        bootstrap(EMAIL)

        assert TeamMember.objects.get(bits_email=EMAIL).name == "Kabir Rao"

    def test_it_promotes_an_existing_member_rather_than_refusing(self):
        member = TeamMember.objects.create(name="Kabir", bits_email=EMAIL, batch="2025")
        team = Team.objects.create(name="Ignite 26", join_code="EXISTINGCODE")
        TeamMembership.objects.create(team=team, member=member, role="member")

        bootstrap(EMAIL, team="Ignite 26")

        assert TeamMembership.objects.get(team=team, member=member).role == "lead"

    def test_it_reactivates_a_deactivated_team(self):
        bootstrap(EMAIL, team="Ignite 26")
        Team.objects.update(is_active=False)

        bootstrap(EMAIL, team="Ignite 26")
        assert Team.objects.get().is_active


class TestRefusals:
    def test_a_non_bits_address_is_refused(self):
        """Sign-in checks Google's `hd` claim, so this member could never sign
        in. Creating the row anyway produces something that looks right and
        locks somebody out."""
        with pytest.raises(CommandError, match="not a BITS address"):
            bootstrap("someone@gmail.com")

        assert not TeamMember.objects.exists()

    def test_a_non_address_is_refused(self):
        with pytest.raises(CommandError, match="not an email address"):
            bootstrap("kabir")


class TestSelfContact:
    def test_it_creates_a_prospect_who_is_you(self):
        """So the first live send lands in your own inbox rather than a
        stranger's -- the only honest way to test a path every unit test mocks."""
        bootstrap(EMAIL, name="Kabir Rao", self_contact=True)

        contact = Contact.objects.get(email=EMAIL)
        member = TeamMember.objects.get(bits_email=EMAIL)
        assert contact.assigned_to_id == member.id
        assert contact.lifecycle == ContactLifecycle.NEW.value

    def test_a_re_run_resets_it_to_sendable(self):
        """Otherwise the second test send is refused as already mailed, which is
        the constraint working correctly but reads as a broken command."""
        bootstrap(EMAIL, self_contact=True)
        Contact.objects.update(lifecycle=ContactLifecycle.CONTACTED.value)

        bootstrap(EMAIL, self_contact=True)
        assert Contact.objects.get(email=EMAIL).lifecycle == ContactLifecycle.NEW.value
