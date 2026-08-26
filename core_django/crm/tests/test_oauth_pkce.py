"""The PKCE verifier has to survive between two requests.

This file exists because of a live failure. Both OAuth flows build a `Flow`,
call `authorization_url()`, and then build a *second* `Flow` in the callback to
exchange the code. `authorization_url()` quietly generates a PKCE code verifier
and sends only its SHA-256 challenge to Google; the verifier lives on that first
Flow object and nothing else. The callback's fresh Flow therefore had
`code_verifier = None`, sent no verifier, and Google refused every single
exchange with:

    (invalid_grant) Missing code verifier

Nothing caught it. The suite mocked above the OAuth layer, so the one thing that
had to travel between the two requests was never exercised. These tests drive
the real `google_auth_oauthlib` Flow and assert the round trip: the challenge in
the redirect URL must be the SHA-256 of the verifier that later reaches
`fetch_token`.

The obvious alternative fix -- `autogenerate_code_verifier=False` -- was
rejected. PKCE binds the authorization code to the flow that started it, so an
intercepted callback URL is useless without the session. Turning it off to make
an error go away would trade a real protection for a smaller diff.
"""

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest

from crm.models import TeamMember
from crm.services import auth as auth_svc
from crm.services import gmail_oauth

pytestmark = pytest.mark.django_db


def challenge_for(verifier: str) -> str:
    """The transformation Google will apply to check the verifier."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


@pytest.fixture(autouse=True)
def oauth_configured(settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = "test-client-id.apps.googleusercontent.com"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "test-secret"


@pytest.fixture
def member():
    return TeamMember.objects.create(
        name="Kabir", bits_email="kabir@pilani.bits-pilani.ac.in", batch="2025"
    )


@pytest.fixture
def request_with_session(rf):
    """A real request carrying a real (in-memory) session."""
    from django.contrib.sessions.backends.db import SessionStore

    def build(path="/login/google/", **params):
        request = rf.get(path, params)
        request.session = SessionStore()
        return request

    return build


class Captured:
    """Stands in for the network. Records what the exchange would have sent."""

    def __init__(self):
        self.code_verifier = "<never called>"

    def install(self, monkeypatch):
        from google_auth_oauthlib.flow import Flow

        captured = self

        def fake_fetch_token(flow_self, **kwargs):
            captured.code_verifier = flow_self.code_verifier
            raise RuntimeError("stop here -- the token exchange is not under test")

        monkeypatch.setattr(Flow, "fetch_token", fake_fetch_token)


# --------------------------------------------------------------- sign-in flow

class TestSignInFlow:
    def test_the_url_carries_a_challenge_and_the_session_keeps_the_verifier(
        self, request_with_session
    ):
        request = request_with_session()

        url = auth_svc.google_authorization_url(request)
        query = parse_qs(urlparse(url).query)

        verifier = request.session[auth_svc.VERIFIER_SESSION_KEY]
        assert verifier, "nothing to present at the token exchange"
        assert query["code_challenge_method"] == ["S256"]
        assert query["code_challenge"] == [challenge_for(verifier)]

    def test_the_callback_presents_the_verifier_the_url_promised(
        self, request_with_session, monkeypatch
    ):
        """THE test. Two separate requests, two separate Flow objects, and the
        verifier has to cross the gap between them."""
        first = request_with_session()
        url = auth_svc.google_authorization_url(first)
        challenge = parse_qs(urlparse(url).query)["code_challenge"][0]

        captured = Captured()
        captured.install(monkeypatch)

        second = request_with_session(
            "/login/google/callback/",
            state=first.session[auth_svc.STATE_SESSION_KEY],
            code="an-authorization-code",
        )
        # The session survives the redirect; the Flow object does not.
        second.session = first.session

        with pytest.raises(auth_svc.GoogleAuthError):
            auth_svc.member_from_google_callback(second)

        assert captured.code_verifier, "no verifier reached fetch_token"
        assert challenge_for(captured.code_verifier) == challenge

    def test_the_verifier_does_not_linger_in_the_session(
        self, request_with_session, monkeypatch
    ):
        """One-shot, like the state. A verifier left behind would be reused by
        the next attempt, against a challenge Google never saw."""
        first = request_with_session()
        auth_svc.google_authorization_url(first)

        Captured().install(monkeypatch)
        second = request_with_session(
            "/login/google/callback/",
            state=first.session[auth_svc.STATE_SESSION_KEY],
            code="c",
        )
        second.session = first.session

        with pytest.raises(auth_svc.GoogleAuthError):
            auth_svc.member_from_google_callback(second)

        assert auth_svc.VERIFIER_SESSION_KEY not in second.session


# ----------------------------------------------------------- gmail grant flow

class TestGmailGrantFlow:
    def test_the_url_carries_a_challenge_and_the_session_keeps_the_verifier(
        self, request_with_session, member
    ):
        request = request_with_session("/settings/gmail/connect/")

        url = gmail_oauth.authorization_url(request, member)
        query = parse_qs(urlparse(url).query)

        verifier = request.session[gmail_oauth.VERIFIER_SESSION_KEY]
        assert verifier
        assert query["code_challenge"] == [challenge_for(verifier)]
        # The two properties this flow exists for, asserted here so a refactor
        # cannot quietly drop them and leave one-hour credentials behind.
        assert query["access_type"] == ["offline"]
        assert query["prompt"] == ["consent"]

    def test_the_callback_presents_the_verifier_the_url_promised(
        self, request_with_session, member, monkeypatch
    ):
        first = request_with_session("/settings/gmail/connect/")
        url = gmail_oauth.authorization_url(first, member)
        challenge = parse_qs(urlparse(url).query)["code_challenge"][0]

        captured = Captured()
        captured.install(monkeypatch)

        second = request_with_session(
            "/settings/gmail/callback/",
            state=first.session[gmail_oauth.STATE_SESSION_KEY],
            code="an-authorization-code",
        )
        second.session = first.session

        with pytest.raises(gmail_oauth.GmailConsentError):
            gmail_oauth.complete(second, member)

        assert captured.code_verifier, "no verifier reached fetch_token"
        assert challenge_for(captured.code_verifier) == challenge


class TestTheTwoFlowsDoNotCollide:
    def test_a_gmail_grant_does_not_clobber_a_half_finished_sign_in(
        self, request_with_session, member
    ):
        """Distinct session keys, deliberately. Sharing one would make an
        interleaved sign-in and Gmail grant fail each other."""
        request = request_with_session()
        auth_svc.google_authorization_url(request)
        sign_in_verifier = request.session[auth_svc.VERIFIER_SESSION_KEY]

        gmail_oauth.authorization_url(request, member)

        assert request.session[auth_svc.VERIFIER_SESSION_KEY] == sign_in_verifier
        assert request.session[gmail_oauth.VERIFIER_SESSION_KEY] != sign_in_verifier
