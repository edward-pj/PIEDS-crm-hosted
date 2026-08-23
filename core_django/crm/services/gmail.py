"""Sending mail as a member, from the server.

A port of `local_agent/gmail/client.py`. Message construction, reply detection
and draft reconciliation moved across unchanged -- they were never the part that
depended on running beside a browser. **Only credential loading is different**,
and that difference is the whole reason the local agent existed:

    InstalledAppFlow.run_local_server(port=0, prompt="consent")

binds a localhost socket and opens a browser on the machine running the code.
On a server there is no browser and no one sitting at it. Here the refresh token
is fetched from the database instead (granted once, in the member's own browser,
by services/gmail_oauth.py) and exchanged for an access token over plain HTTPS.

Everything else about the send path is deliberately identical, because the
message bytes are what a prospect actually receives and there was no reason to
risk changing them at the same time as changing where the code runs.
"""

import base64
import logging
from dataclasses import dataclass
from datetime import timedelta
from datetime import timezone as dt_timezone
from email.message import EmailMessage
from email.utils import formataddr

from django.conf import settings
from django.utils import timezone

from crm.models import GmailCredential

from . import secrets as token_store

log = logging.getLogger(__name__)

#: Both are needed. `gmail.send` does the sending; `gmail.readonly` backs reply
#: detection (services/followups.py) and stranded-draft reconciliation, neither
#: of which can be done from the send scope alone.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]


class GmailAuthError(RuntimeError):
    """The member's Gmail authorisation is missing, revoked, or wrong.

    Distinct from a send failure on purpose: this one is fixed by the member
    reconnecting Gmail, and no amount of retrying will help without that.
    """


class GmailNotConnected(GmailAuthError):
    """This member has never connected Gmail, or has disconnected it."""


@dataclass
class SendResult:
    message_id: str
    thread_id: str


# --- credentials -----------------------------------------------------------


def credential_for(member) -> GmailCredential:
    row = GmailCredential.objects.filter(member=member).first()
    if row is None or not row.is_usable:
        raise GmailNotConnected(
            f"{member.name} has not connected Gmail. Open Settings > Gmail and "
            f"grant access before sending."
        )
    return row


def has_usable_credential(member) -> bool:
    row = GmailCredential.objects.filter(member=member).first()
    return bool(row and row.is_usable and row.has_scopes(SCOPES))


def _cached_access_token(row: GmailCredential) -> str | None:
    """The stored access token, if it will still be valid on arrival.

    The skew matters: a token with four seconds left passes a naive expiry check
    and then fails the request it was fetched for, which surfaces as a random
    401 on one mail in a batch.
    """
    if not row.access_token_encrypted or not row.access_token_expires_at:
        return None

    skew = timedelta(seconds=settings.GMAIL_TOKEN_EXPIRY_SKEW_SECONDS)
    if row.access_token_expires_at - skew <= timezone.now():
        return None

    return token_store.decrypt(row.access_token_encrypted, row.access_token_key_version)


def _store_access_token(row: GmailCredential, creds) -> None:
    if not creds.token or not creds.expiry:
        return

    ciphertext, version = token_store.encrypt(creds.token)
    row.access_token_encrypted = ciphertext
    row.access_token_key_version = version
    # google-auth returns a naive UTC datetime. Storing it naive under USE_TZ
    # would compare wrongly against timezone.now() for the life of the row --
    # `django.utils.timezone.utc` was removed in Django 5, so this is the
    # stdlib one.
    row.access_token_expires_at = (
        timezone.make_aware(creds.expiry, dt_timezone.utc)
        if timezone.is_naive(creds.expiry) else creds.expiry
    )
    row.last_refreshed_at = timezone.now()
    row.last_error = ""
    row.save(update_fields=[
        "access_token_encrypted", "access_token_key_version",
        "access_token_expires_at", "last_refreshed_at", "last_error", "updated_at",
    ])


def _mark_broken(row: GmailCredential, detail: str, *, revoked: bool) -> None:
    row.last_error = detail[:2000]
    fields = ["last_error", "updated_at"]
    if revoked and row.revoked_at is None:
        row.revoked_at = timezone.now()
        fields.append("revoked_at")
    row.save(update_fields=fields)


def load_credentials(member):
    """Build google-auth Credentials for `member` with no browser involved."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    row = credential_for(member)

    if not settings.GOOGLE_OAUTH_CLIENT_ID or not settings.GOOGLE_OAUTH_CLIENT_SECRET:
        raise GmailAuthError(
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set on "
            "this deployment, so stored refresh tokens cannot be exchanged."
        )

    refresh_token = token_store.decrypt(row.refresh_token_encrypted, row.key_version)

    creds = Credentials(
        token=_cached_access_token(row),
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=list(row.granted_scopes or SCOPES),
        expiry=(
            row.access_token_expires_at.replace(tzinfo=None)
            if row.access_token_expires_at else None
        ),
    )

    if creds.valid:
        return creds

    try:
        creds.refresh(Request())
    except Exception as exc:                                   # noqa: BLE001
        # A refresh failure is nearly always terminal: the member revoked the
        # app from their Google account, an admin blocked it, or the grant
        # expired. Recording it and marking the row revoked is what turns
        # "their mail silently stopped" into "reconnect Gmail", which is the
        # only action that actually fixes it.
        detail = f"{type(exc).__name__}: {exc}"
        _mark_broken(row, detail, revoked=True)
        raise GmailNotConnected(
            f"Google refused to refresh {member.name}'s Gmail authorisation "
            f"({detail}). They need to reconnect Gmail."
        ) from exc

    _store_access_token(row, creds)
    return creds


# --- client ----------------------------------------------------------------


class GmailClient:
    """Sends as one member. Construct per member, reuse within a run."""

    def __init__(self, member):
        self.member = member
        self.member_email = member.bits_email
        self._service = None
        self._authenticated_email: str | None = None

    @property
    def service(self):
        if self._service is None:
            from googleapiclient.discovery import build

            creds = load_credentials(self.member)
            # static_discovery=True uses the discovery document bundled with the
            # library instead of fetching one per process. On a small instance
            # where every worker would otherwise hold its own copy, that is the
            # difference between a warm start and a cold HTTPS round trip.
            self._service = build(
                "gmail", "v1", credentials=creds,
                cache_discovery=False, static_discovery=True,
            )
        return self._service

    def authenticated_email(self) -> str:
        """The address Google says we are actually acting as."""
        if self._authenticated_email is None:
            profile = self.service.users().getProfile(userId="me").execute()
            self._authenticated_email = profile["emailAddress"].lower()
        return self._authenticated_email

    def verify_identity(self) -> str:
        """Refuse to run if the Gmail account isn't the configured member.

        `campaign_mailings.sent_by` is only trustworthy because of this check.
        On the laptop agent it ran on every startup; here it runs once per
        credential and the result is cached on the row, because calling Gmail
        before every message would cost a round trip per mail for a fact that
        cannot change without the credential changing.
        """
        row = credential_for(self.member)
        if row.identity_verified_at and row.identity_verified_at >= row.granted_at:
            return row.google_email

        actual = self.authenticated_email()
        expected = self.member_email.lower()
        if actual != expected:
            _mark_broken(
                row,
                f"token authenticates as {actual}, expected {expected}",
                revoked=True,
            )
            raise GmailAuthError(
                f"The connected Gmail account is {actual!r} but this member is "
                f"{expected!r}. Disconnect Gmail and reconnect with the correct "
                f"account."
            )

        row.google_email = actual
        row.identity_verified_at = timezone.now()
        row.save(update_fields=["google_email", "identity_verified_at", "updated_at"])
        return actual

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        body_html: str = "",
        from_name: str = "",
        cc: str = "",
        bcc: str = "",
    ) -> SendResult:
        message = EmailMessage()
        message["To"] = to

        # `formataddr`, not an f-string: it quotes a name containing a comma or
        # a period and RFC 2047-encodes a non-ASCII one. Hand-built From headers
        # are how mail ends up displaying as `"Rao"" <addr>` in some clients.
        # An empty name yields the bare address, unchanged from before.
        message["From"] = formataddr((from_name, self.member_email)) if from_name \
            else self.member_email
        message["Subject"] = subject

        # Both are plain headers. Gmail strips Bcc before delivery, so the
        # recipients never learn about each other.
        if cc:
            message["Cc"] = cc
        if bcc:
            message["Bcc"] = bcc

        # Plain text first, then the HTML alternative. That ordering is what
        # multipart/alternative means -- last part wins in clients that render
        # HTML, and the text part is the fallback for those that do not. The
        # server built both from the same body (see services/richtext.py), so
        # they always say the same thing.
        message.set_content(body)
        if body_html:
            message.add_alternative(body_html, subtype="html")

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent = self.service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return SendResult(message_id=sent["id"], thread_id=sent["threadId"])

    def thread_has_reply_from(self, thread_id: str, address: str) -> bool:
        """Did `address` write back in this thread?

        Evidence, not inference: we look for a message in the thread whose From
        is the prospect. That rules out our own follow-ups, anyone we CC'd, and
        Gmail's own "message blocked" notices, all of which land in the same
        thread and none of which mean the prospect replied.

        A thread that has vanished (deleted, or the id is stale) reads as "no
        reply" rather than raising: the caller is a background scan, and one
        bad row must not stop it looking at the rest.
        """
        try:
            thread = (
                self.service.users().threads()
                .get(userId="me", id=thread_id, format="metadata",
                     metadataHeaders=["From"])
                .execute()
            )
        except Exception:                              # noqa: BLE001
            return False

        wanted = (address or "").strip().lower()
        if not wanted:
            return False

        for message in thread.get("messages", []):
            headers = message.get("payload", {}).get("headers", [])
            sender = next(
                (h.get("value", "") for h in headers if h.get("name", "").lower() == "from"),
                "",
            )
            # The header is "Name <addr>", so a substring test on the bare
            # address is the reliable read.
            if wanted in sender.lower():
                return True
        return False

    def find_message_to(self, address: str, subject: str) -> SendResult | None:
        """Used by reconcile to resolve a stranded DRAFT row.

        Answers "did this mail actually go out before we crashed?"
        """
        query = f'in:sent to:{address} subject:"{subject}"'
        resp = (
            self.service.users()
            .messages()
            .list(userId="me", q=query, maxResults=1)
            .execute()
        )
        messages = resp.get("messages", [])
        if not messages:
            return None
        return SendResult(message_id=messages[0]["id"], thread_id=messages[0]["threadId"])
