"""The front door.

The load-bearing rule here used to be that batch 2025 could not sign in by name.
It is now stronger and simpler: **nobody** can. There is one door, it is Google,
and it verifies a signed token. A password-free "pick your name" form on a public
hostname is not a convenience, it is an unauthenticated login as any lead, so the
test that mattered most in this file is now the one asserting the route is gone.
"""

import pytest
from django.urls import NoReverseMatch, reverse

from crm.models import TeamMember
from crm.services import auth as auth_svc

pytestmark = pytest.mark.django_db


@pytest.fixture
def lead():
    return TeamMember.objects.create(
        name="Aarav", bits_email="aarav@pilani.bits-pilani.ac.in", batch="2024"
    )


@pytest.fixture
def member():
    return TeamMember.objects.create(
        name="Kabir", bits_email="kabir@pilani.bits-pilani.ac.in", batch="2025"
    )


def sign_in(client, member):
    """Establish a session without going through a door.

    Every remaining door is Google's, and mocking a signed id_token to test
    logout would be testing the mock. The session key IS the identity -- see
    services/auth.py::login_member -- so setting it is the honest shortcut.
    """
    session = client.session
    session[auth_svc.SESSION_KEY] = str(member.id)
    session.save()


class TestTheNameDoorIsGone:
    """Tripwire. If any of these fail, an unauthenticated login has come back."""

    def test_the_route_does_not_exist(self):
        with pytest.raises(NoReverseMatch):
            reverse("login_by_name")

    def test_posting_to_the_old_path_signs_nobody_in(self, client, lead):
        r = client.post("/login/name/", {"member_id": str(lead.id)})
        assert r.status_code == 404
        assert auth_svc.SESSION_KEY not in client.session

    def test_no_member_ids_are_rendered_into_the_login_page(self, client, lead, member):
        """The UUIDs were the whole vulnerability: they were the credential."""
        body = client.get(reverse("login")).content.decode()
        assert str(lead.id) not in body
        assert str(member.id) not in body
        assert "Aarav" not in body


class TestSession:
    def test_a_stranger_is_sent_to_the_login_page(self, client):
        r = client.get(reverse("crm:home"))
        assert r.status_code == 302
        assert r["Location"] == reverse("login")

    def test_logout_clears_the_session(self, client, lead):
        sign_in(client, lead)
        client.post(reverse("logout"))
        assert auth_svc.SESSION_KEY not in client.session

    def test_deactivating_a_member_ends_their_session(self, client, lead):
        """Takes effect on the next request, not at their next login."""
        sign_in(client, lead)
        TeamMember.objects.filter(pk=lead.pk).update(is_active=False)

        assert client.get(reverse("crm:home")).status_code == 302


class TestHealthz:
    def test_it_answers_without_a_session(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    def test_it_is_exempt_from_the_https_redirect(self, client, settings):
        """Render's health check arrives over plain HTTP with no
        X-Forwarded-Proto. A 301 here marks every deploy unhealthy."""
        settings.SECURE_SSL_REDIRECT = True
        settings.SECURE_REDIRECT_EXEMPT = [r"^healthz$"]

        assert client.get("/healthz").status_code == 200
        # And the exemption is narrow -- everything else still redirects.
        assert client.get(reverse("login")).status_code == 301


class TestGoogleDoor:
    def test_it_is_offered_when_configured(self, client, settings, lead):
        settings.GOOGLE_OAUTH_CLIENT_ID = "id"
        settings.GOOGLE_OAUTH_CLIENT_SECRET = "secret"
        assert "Sign in with your BITS Google account" in (
            client.get(reverse("login")).content.decode()
        )

    def test_unconfigured_says_so_instead_of_erroring(self, client, settings, lead):
        settings.GOOGLE_OAUTH_CLIENT_ID = ""
        settings.GOOGLE_OAUTH_CLIENT_SECRET = ""

        assert "Not configured on this deployment" in (
            client.get(reverse("login")).content.decode()
        )
        # And the route itself redirects rather than raising.
        assert client.get(reverse("google_login")).status_code == 302

    def test_a_forged_callback_signs_nobody_in(self, client, settings, member):
        """No state in the session means the response did not come from a flow
        we started."""
        settings.GOOGLE_OAUTH_CLIENT_ID = "id"
        settings.GOOGLE_OAUTH_CLIENT_SECRET = "secret"

        r = client.get(reverse("google_callback"), {"state": "made-up", "code": "x"})
        assert r.status_code == 302
        assert auth_svc.SESSION_KEY not in client.session
