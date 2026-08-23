"""Shared fixtures and helpers for the CRM test suite.

There was no conftest here until teams landed, and every one of the nine test
files defined its own `member` / `lead` / `campaign` / `contact` fixtures. That
was survivable while a lead was `member.batch == "2024"` -- a fact carried on
the row itself, so constructing one took a single `create()`. It stopped being
survivable the moment being a lead became a fact about a *membership*: the same
one-line change would otherwise have had to be repeated in nine places, and
missing one would present as a PermissionDenied in an unrelated test.

`make_lead` and `make_member` are the two things worth sharing. Fixtures stay in
the files that use them, because their names and addresses are load-bearing in
assertions ("Aarav" appears in expected output), and hoisting them would make
those tests read as if they came from nowhere.
"""

import pytest

from crm.models import Team, TeamMember, TeamMembership


@pytest.fixture
def team(db):
    """The team every fixture below belongs to.

    Mirrors what migration 0014 does to a live database: one team, everyone on
    it. A test that needs two teams builds the second itself.
    """
    return Team.objects.create(name="PIEDS Outreach", join_code="TESTCODE01")


def make_lead(team, *, name="Aarav", email="aarav@pilani.bits-pilani.ac.in",
              batch="2024", **kwargs):
    """A member who leads `team`.

    Use this rather than `TeamMember.objects.create(batch="2024")`. The batch is
    now a display field and grants nothing -- a member created without a lead
    membership is not a lead, however their batch reads.
    """
    member = TeamMember.objects.create(
        name=name, bits_email=email, batch=batch, **kwargs
    )
    TeamMembership.objects.create(team=team, member=member, role="lead")
    return member


def make_member(team, *, name="Kabir", email="kabir@pilani.bits-pilani.ac.in",
                batch="2025", **kwargs):
    """An ordinary member of `team`."""
    member = TeamMember.objects.create(
        name=name, bits_email=email, batch=batch, **kwargs
    )
    TeamMembership.objects.create(team=team, member=member, role="member")
    return member


def sign_in(client, member):
    """Establish a session for `member` without going through a door.

    The session key IS the identity (services/auth.py::login_member), so setting
    it is the honest shortcut; the alternative is mocking a signed Google
    id_token, which would be testing the mock.
    """
    from crm.services import auth as auth_svc

    session = client.session
    session[auth_svc.SESSION_KEY] = str(member.id)
    session.save()
    return client
