"""Who is using the CRM, and how they proved it.

**One door: Google, restricted to the BITS domain.** Everyone authenticates the
same way, and the identity is verified against Google's signed `id_token` and
its `hd` claim rather than against a string anyone can type.

There used to be a second door -- batch 2024 picked a name from a dropdown, no
password -- justified on the grounds that every lead ran the CRM on their own
laptop, so the only person who could reach the form was the person holding the
machine. That argument was sound then and is simply false now: on a public
hostname the form is reachable by anyone on the internet, the leads' UUIDs were
rendered into the page as `<option value>`, and picking a name made you a lead.
It was deleted rather than hidden behind a setting, because a setting leaves the
code one misconfigured environment variable away from exactly that.

The replacement for "a new person needs access" is the join code, not a
dropdown. Until that lands, a lead adds members through /admin/.

Identity is a session key holding a TeamMember id. Django's `User` model is no
longer consulted for the CRM at all; it survives only for `/admin/`.
"""

import os

from django.conf import settings
from django.urls import reverse

from crm.models import TeamMember

#: The session key. A TeamMember UUID as a string.
SESSION_KEY = "member_id"

#: Where Google is redirected back to, and where we stash the CSRF state.
STATE_SESSION_KEY = "google_oauth_state"

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


class GoogleAuthError(RuntimeError):
    """Raised with a message that is safe to show the person signing in."""


class UnknownMember(GoogleAuthError):
    """A verified BITS account with no TeamMember yet.

    Deliberately NOT a refusal. It carries the proven identity forward to the
    join screen, so the code is the only thing still to establish -- the address
    has already been verified against Google's signed token and the `hd` claim,
    and asking for it again would invite typing a different one.
    """

    def __init__(self, email: str, display_name: str = ""):
        self.email = email
        self.display_name = display_name
        super().__init__(
            f"{email} is not on a team yet. Enter your team's join code to continue."
        )


# --- session ---------------------------------------------------------------


def login_member(request, member: TeamMember) -> None:
    # Cycling the session key on login means a session id captured before
    # sign-in cannot be replayed afterwards.
    request.session.cycle_key()
    request.session[SESSION_KEY] = str(member.id)


def logout_member(request) -> None:
    request.session.pop(SESSION_KEY, None)
    request.session.pop(STATE_SESSION_KEY, None)


def current_member(request) -> TeamMember | None:
    """Resolve the signed-in TeamMember, or None.

    Deactivating a member takes effect on their next request rather than at
    their next login, which is what "their agent stops working immediately" in
    the playbook has to mean for the browser too.
    """
    member_id = request.session.get(SESSION_KEY)
    if not member_id:
        return None

    member = TeamMember.objects.filter(pk=member_id, is_active=True).first()
    if member is None:
        request.session.pop(SESSION_KEY, None)
    return member


# --- google ----------------------------------------------------------------


def google_enabled() -> bool:
    return bool(settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET)


def hosted_domain() -> str:
    """The domain shown on the login page. Display only -- the check is on `hd`."""
    return settings.GOOGLE_OAUTH_HOSTED_DOMAIN or ""


def _flow(request):
    from google_auth_oauthlib.flow import Flow

    # The redirect URI is built from the incoming request, so it is the public
    # https:// URL in production and http://localhost:8000/... in development.
    # oauthlib rejects plain http, hence the DEBUG-only relaxation; Google
    # returns scopes in its own order, hence the other.
    #
    # `build_absolute_uri` reads the scheme from SECURE_PROXY_SSL_HEADER, which
    # settings.py only sets when DEBUG is off. Deploying behind a TLS proxy with
    # DEBUG=True therefore produces an http:// redirect URI that Google rejects.
    if settings.DEBUG:
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

    return Flow.from_client_config(
        {
            "web": {
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            }
        },
        scopes=SCOPES,
        redirect_uri=request.build_absolute_uri(reverse("google_callback")),
    )


def google_authorization_url(request) -> str:
    flow = _flow(request)
    url, state = flow.authorization_url(
        access_type="online",
        include_granted_scopes="true",
        prompt="select_account",
        # Asks Google to show only BITS accounts. It is a hint, not a control:
        # the real check is on the verified `hd` claim below.
        hd=settings.GOOGLE_OAUTH_HOSTED_DOMAIN or None,
    )
    request.session[STATE_SESSION_KEY] = state
    return url


def member_from_google_callback(request) -> TeamMember:
    """Exchange the code, verify the token, and map it to a TeamMember.

    Every failure here is a refusal to sign anyone in, with ONE exception: a
    verified address on the hosted domain that has no member row raises
    `UnknownMember`, which the view turns into the join-code screen rather than
    a dead end. Membership is still not automatic -- a code is required -- but
    it no longer needs a lead at a keyboard for each person, which is what the
    deleted name-picker door used to (badly) provide.
    """
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    expected_state = request.session.pop(STATE_SESSION_KEY, None)
    if not expected_state or request.GET.get("state") != expected_state:
        raise GoogleAuthError("Sign-in state did not match. Please try again.")

    if request.GET.get("error"):
        raise GoogleAuthError("Google sign-in was cancelled.")

    flow = _flow(request)
    try:
        flow.fetch_token(code=request.GET.get("code"))
    except Exception as exc:                                   # noqa: BLE001
        raise GoogleAuthError(f"Could not complete Google sign-in: {exc}")

    # The id_token is signed by Google and carries the email; verifying it is
    # what makes this trustworthy rather than the access token, which only says
    # a call succeeded.
    claims = google_id_token.verify_oauth2_token(
        flow.credentials.id_token,
        google_requests.Request(),
        settings.GOOGLE_OAUTH_CLIENT_ID,
    )

    if not claims.get("email_verified"):
        raise GoogleAuthError("That Google account has no verified email address.")

    domain = settings.GOOGLE_OAUTH_HOSTED_DOMAIN
    if domain and claims.get("hd") != domain:
        raise GoogleAuthError(f"Sign in with your @{domain} account, not a personal one.")

    email = claims["email"].lower()
    member = TeamMember.objects.filter(bits_email__iexact=email, is_active=True).first()
    if member is None:
        # A verified BITS address with no member row is a new joiner, not an
        # intruder -- the `hd` check above is what makes that safe to say, and
        # it is the thing standing between a personal Gmail and the pool.
        # They are sent to /join/, where a valid code creates the member.
        raise UnknownMember(email, claims.get("name") or "")
    return member
