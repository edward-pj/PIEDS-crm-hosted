"""Granting the server permission to send as a member.

**A second, separate consent step from signing in**, and that separation is
deliberate. Sign-in (services/auth.py) asks only for identity; this asks for a
member's mailbox. Bundling them would mean a person cannot look at the CRM
without first handing over Gmail, and a failed or declined Gmail consent would
lock them out of the application entirely rather than leaving them signed in
with one thing left to do.

The two flows share a client ID but nothing else: different scopes, different
redirect URI, different session state key.

What makes this yield a *refresh* token rather than only an access token:

    access_type="offline"   -- ask for a refresh token at all
    prompt="consent"        -- ask again even if they have already granted it

Without `prompt="consent"` Google returns a refresh token only on the very first
grant for a client/user pair, and returns none on every subsequent one. A member
who reconnects after we lost their row would then get an access token good for
an hour and nothing durable -- and it would look like it worked.
"""

import os

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from crm.models import GmailCredential

from . import secrets as token_store
from .gmail import SCOPES

#: Where the CSRF state for THIS flow lives. Distinct from auth.py's key, or a
#: member signing in while a Gmail grant is half-finished clobbers one with the
#: other and both fail with "state did not match".
STATE_SESSION_KEY = "gmail_oauth_state"

#: The PKCE code verifier for the in-flight grant. Separate key from auth.py's
#: for the same reason the state key is separate: a member connecting Gmail
#: while a sign-in is half-finished must not clobber one flow with the other.
#: See auth.py for why this has to survive in the session at all.
VERIFIER_SESSION_KEY = "gmail_oauth_verifier"


class GmailConsentError(RuntimeError):
    """Raised with a message that is safe to show the member."""


def _flow(request):
    from google_auth_oauthlib.flow import Flow

    if settings.DEBUG:
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    # Google returns granted scopes in its own order, and adds `openid` of its
    # own accord when the account is a Workspace one. Without this, oauthlib
    # raises "Scope has changed" on a grant that is perfectly fine.
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
        redirect_uri=request.build_absolute_uri(reverse("gmail_callback")),
    )


def authorization_url(request, member) -> str:
    flow = _flow(request)
    url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
        # Pre-fills the account chooser with the member's own address. A hint
        # only -- the real check is comparing the granted account below.
        login_hint=member.bits_email,
        hd=settings.GOOGLE_OAUTH_HOSTED_DOMAIN or None,
    )
    request.session[STATE_SESSION_KEY] = state
    request.session[VERIFIER_SESSION_KEY] = flow.code_verifier
    return url


def _granted_email(credentials) -> str:
    """Which Google account actually granted this, per Gmail itself.

    Read from `users.getProfile` rather than from the id_token, because this
    flow does not request `openid` and there may not be one. It is also the
    stronger fact: it is the account the send will actually go out from.
    """
    from googleapiclient.discovery import build

    service = build("gmail", "v1", credentials=credentials,
                    cache_discovery=False, static_discovery=True)
    return service.users().getProfile(userId="me").execute()["emailAddress"].lower()


def complete(request, member) -> GmailCredential:
    """Exchange the code and store the grant. Raises rather than half-storing."""
    expected_state = request.session.pop(STATE_SESSION_KEY, None)
    code_verifier = request.session.pop(VERIFIER_SESSION_KEY, None)
    if not expected_state or request.GET.get("state") != expected_state:
        raise GmailConsentError("Gmail authorisation state did not match. Try again.")

    if request.GET.get("error"):
        raise GmailConsentError(
            "Gmail access was not granted. Nothing was changed; you can still "
            "use the rest of the CRM, but you will not be able to send."
        )

    flow = _flow(request)
    flow.code_verifier = code_verifier
    try:
        flow.fetch_token(code=request.GET.get("code"))
    except Exception as exc:                                    # noqa: BLE001
        raise GmailConsentError(f"Could not complete Gmail authorisation: {exc}")

    credentials = flow.credentials

    if not credentials.refresh_token:
        # Reachable if `prompt="consent"` is ever dropped, or if Google declines
        # to reissue. Storing the row anyway would leave a credential that works
        # for one hour and then fails forever, which is far worse than refusing.
        raise GmailConsentError(
            "Google did not return a long-lived token. Remove 'Ignite CRM' from "
            "your Google account's third-party access list and try again."
        )

    granted = set(credentials.scopes or [])
    missing = [s for s in SCOPES if s not in granted]
    if missing:
        raise GmailConsentError(
            "Gmail access was granted only in part -- both permissions are "
            "needed (sending, and reading your own sent mail to detect replies). "
            "Please accept all of them."
        )

    try:
        google_email = _granted_email(credentials)
    except Exception as exc:                                    # noqa: BLE001
        raise GmailConsentError(f"Could not confirm which account granted access: {exc}")

    # The identity binding. Without it a member could connect somebody else's
    # mailbox and every `sent_by` on their mail would be a lie -- and `sent_by`
    # is what the audit trail, the daily cap and reply detection all key on.
    if google_email != member.bits_email.lower():
        raise GmailConsentError(
            f"You signed in to the CRM as {member.bits_email} but granted Gmail "
            f"access for {google_email}. Sign in to Google as {member.bits_email} "
            f"and try again."
        )

    ciphertext, key_version = token_store.encrypt(credentials.refresh_token)

    row, _ = GmailCredential.objects.update_or_create(
        member=member,
        defaults={
            "google_email": google_email,
            "refresh_token_encrypted": ciphertext,
            "key_version": key_version,
            "granted_scopes": sorted(granted),
            "granted_at": timezone.now(),
            # Just proved by _granted_email above, so the first send does not
            # have to prove it again.
            "identity_verified_at": timezone.now(),
            "last_error": "",
            "revoked_at": None,
            # A reconnect must not leave the previous grant's access token
            # behind: it belongs to the old authorisation and may already be
            # dead.
            "access_token_encrypted": None,
            "access_token_key_version": None,
            "access_token_expires_at": None,
            "last_refreshed_at": None,
        },
    )
    return row


def disconnect(member) -> bool:
    """Mark the member's grant revoked, and ask Google to drop it too.

    Local revocation is what stops the CRM sending; the call to Google is what
    stops the token working at all, including from a database backup taken
    before now. It is attempted but not required -- a member who has clicked
    Disconnect must end up disconnected here even if Google is unreachable.
    """
    row = GmailCredential.objects.filter(member=member).first()
    if row is None or row.revoked_at is not None:
        return False

    try:
        import requests as http

        http.post(
            "https://oauth2.googleapis.com/revoke",
            params={"token": token_store.decrypt(row.refresh_token_encrypted,
                                                 row.key_version)},
            headers={"content-type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
    except Exception:                                           # noqa: BLE001
        pass

    row.revoked_at = timezone.now()
    row.last_error = ""
    row.access_token_encrypted = None
    row.access_token_key_version = None
    row.access_token_expires_at = None
    row.save(update_fields=[
        "revoked_at", "last_error", "access_token_encrypted",
        "access_token_key_version", "access_token_expires_at", "updated_at",
    ])
    return True
