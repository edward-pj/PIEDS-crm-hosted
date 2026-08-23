"""Server-side Gmail: token storage, identity binding, and the send loop.

The code being ported here had ZERO test coverage on the laptop
(`local_agent/tests/` is empty), and it is the part of the system that can put
mail in a stranger's inbox under someone else's name. So these tests care about
three things in particular:

1. A refresh token is never stored, logged, or rendered in the clear.
2. `sent_by` cannot become a lie -- a token belonging to one person must not be
   usable as another.
3. The claim/send/record ordering survives the port, including the 511-contact
   incident that `CLAIM_CHUNK` exists to prevent.
"""

import pytest
from cryptography.fernet import Fernet
from django.urls import reverse
from django.utils import timezone

from crm.models import Campaign, CampaignMailing, Contact, GmailCredential, TeamMember
from crm.services import gmail as gmail_svc
from crm.services import runner, secrets as token_store, sending
from crm.services import auth as auth_svc
from shared.enums import CampaignStatus, MailingStatus

pytestmark = pytest.mark.django_db

KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def token_key(settings):
    settings.GMAIL_TOKEN_KEY = KEY_A
    return KEY_A


@pytest.fixture(autouse=True)
def always_in_the_send_window(settings):
    """Neutralise quiet hours.

    Without this the tick tests pass in the afternoon and fail after 19:00 IST,
    because a job due outside the delivery window is correctly parked as HELD.
    A test whose result depends on what time it is run is worse than no test.
    Setting start == end disables the window; see scheduling.py::_window.
    """
    settings.SCHEDULE_WINDOW_START = 0
    settings.SCHEDULE_WINDOW_END = 0
    settings.SCHEDULE_WINDOW_DAYS = [0, 1, 2, 3, 4, 5, 6]


@pytest.fixture
def member():
    return TeamMember.objects.create(
        name="Kabir", bits_email="kabir@pilani.bits-pilani.ac.in", batch="2025"
    )


@pytest.fixture
def contact():
    return Contact.objects.create(
        first_name="Rohan", last_name="Iyer", email="rohan@example.com",
        company="Zerodha",
    )


@pytest.fixture
def campaign(member):
    return Campaign.objects.create(
        title="Ignite", mail_sub="Hello {{ first_name }}",
        mail_body="About {{ company }}", var_list=["first_name", "company"],
        status=CampaignStatus.ACTIVE.value, created_by=member,
    )


def connect(member, *, refresh="refresh-token-value", scopes=None, revoked=None):
    ciphertext, version = token_store.encrypt(refresh)
    return GmailCredential.objects.create(
        member=member,
        google_email=member.bits_email,
        refresh_token_encrypted=ciphertext,
        key_version=version,
        granted_scopes=scopes if scopes is not None else list(gmail_svc.SCOPES),
        identity_verified_at=timezone.now(),
        revoked_at=revoked,
    )


class FakeGmail:
    """Stands in for GmailClient. Records what it was asked to send."""

    def __init__(self, fail_on=None, raise_auth=False):
        self.sent = []
        self.fail_on = fail_on or set()
        self.raise_auth = raise_auth

    def verify_identity(self):
        if self.raise_auth:
            raise gmail_svc.GmailAuthError("token belongs to someone else")
        return "ok"

    def send(self, *, to, subject, body, body_html="", from_name="", cc="", bcc=""):
        if self.raise_auth:
            raise gmail_svc.GmailAuthError("credential died mid-batch")
        if to in self.fail_on:
            raise RuntimeError("Gmail said no")
        self.sent.append({"to": to, "subject": subject, "from_name": from_name})
        return gmail_svc.SendResult(
            message_id=f"m-{len(self.sent)}", thread_id=f"t-{len(self.sent)}"
        )

    def find_message_to(self, address, subject):
        return None


# --- token storage ---------------------------------------------------------


class TestTokenStorage:
    def test_a_refresh_token_round_trips(self):
        blob, version = token_store.encrypt("secret-token")
        assert token_store.decrypt(blob, version) == "secret-token"

    def test_the_stored_bytes_do_not_contain_the_token(self, member):
        """The whole point. A database dump must not yield a usable credential."""
        row = connect(member, refresh="super-secret-refresh")
        row.refresh_from_db()

        raw = bytes(row.refresh_token_encrypted)
        assert b"super-secret-refresh" not in raw
        assert token_store.decrypt(raw, row.key_version) == "super-secret-refresh"

    def test_a_row_records_which_key_encrypted_it(self, settings):
        """Rotation is impossible without this, and impossible to add later."""
        settings.GMAIL_TOKEN_KEY = f"1:{KEY_A}"
        blob_a, version_a = token_store.encrypt("old")
        assert version_a == 1

        settings.GMAIL_TOKEN_KEY = f"2:{KEY_B},1:{KEY_A}"
        blob_b, version_b = token_store.encrypt("new")
        assert version_b == 2

        # Both remain readable, each with its own key.
        assert token_store.decrypt(blob_a, 1) == "old"
        assert token_store.decrypt(blob_b, 2) == "new"

    def test_dropping_a_key_that_rows_still_use_fails_loudly(self, settings):
        settings.GMAIL_TOKEN_KEY = f"1:{KEY_A}"
        blob, version = token_store.encrypt("old")

        settings.GMAIL_TOKEN_KEY = f"2:{KEY_B}"
        with pytest.raises(token_store.TokenKeyError, match="No Gmail token key"):
            token_store.decrypt(blob, version)

    def test_mixing_versioned_and_bare_keys_is_refused(self, settings):
        settings.GMAIL_TOKEN_KEY = f"{KEY_A},2:{KEY_B}"
        with pytest.raises(token_store.TokenKeyError, match="mixes bare and versioned"):
            token_store.current_version()

    def test_no_key_configured_is_a_clear_error(self, settings):
        settings.GMAIL_TOKEN_KEY = ""
        assert token_store.is_configured() is False
        with pytest.raises(token_store.TokenKeyError, match="not set"):
            token_store.encrypt("x")


# --- what counts as connected ---------------------------------------------


class TestConnectionState:
    def test_no_row_means_not_connected(self, member):
        assert gmail_svc.has_usable_credential(member) is False
        assert member.gmail_connected is False

    def test_a_revoked_row_means_not_connected(self, member):
        connect(member, revoked=timezone.now())
        member.refresh_from_db()
        assert gmail_svc.has_usable_credential(member) is False
        assert member.gmail_connected is False

    def test_a_partial_grant_is_not_usable(self, member):
        """A member can untick a scope on the consent screen."""
        connect(member, scopes=["https://www.googleapis.com/auth/gmail.send"])
        assert gmail_svc.has_usable_credential(member) is False

    def test_a_full_grant_is_usable(self, member):
        connect(member)
        member.refresh_from_db()
        assert gmail_svc.has_usable_credential(member) is True
        assert member.gmail_connected is True

    def test_sending_without_a_credential_refuses_by_name(self, member):
        with pytest.raises(gmail_svc.GmailNotConnected, match="has not connected"):
            gmail_svc.credential_for(member)


# --- the send loop ---------------------------------------------------------


class TestSendBatch:
    def test_a_send_records_a_sent_mailing(self, member, campaign, contact):
        contact.assigned_to = member
        contact.save()
        gmail = FakeGmail()

        outcomes = list(sending.send_batch(
            member, campaign, [contact.id], gmail=gmail
        ))

        assert [o.status for o in outcomes] == [sending.SENT]
        assert gmail.sent[0]["to"] == "rohan@example.com"

        row = CampaignMailing.objects.get(campaign=campaign, contact=contact)
        assert row.status == MailingStatus.SENT.value
        assert row.sent_by_id == member.id
        assert row.mail_thread_id == "t-1"

    def test_a_gmail_failure_settles_the_row_rather_than_stranding_it(
        self, member, campaign, contact
    ):
        contact.assigned_to = member
        contact.save()

        outcomes = list(sending.send_batch(
            member, campaign, [contact.id],
            gmail=FakeGmail(fail_on={"rohan@example.com"}),
        ))

        assert outcomes[0].status == sending.FAILED
        row = CampaignMailing.objects.get(campaign=campaign, contact=contact)
        # FAILED, not DRAFT: a DRAFT would block this contact forever until
        # someone ran reconcile, for a mail we know never went out.
        assert row.status == MailingStatus.FAILED.value

    def test_a_dead_credential_settles_its_chunk_and_stops(self, member, campaign):
        """Two properties at once, and both were wrong on the first attempt.

        A dead token must not strand the rest of the chunk it already claimed --
        a DRAFT row blocks its contact until someone runs reconcile, for mail
        that provably never left. And it must not burn the contacts it had not
        reached yet, or one expired credential costs a member their whole queue.
        """
        contacts = [
            Contact.objects.create(
                first_name=f"P{i}", email=f"p{i}@example.com",
                company="Acme", assigned_to=member,
            )
            for i in range(25)
        ]

        outcomes = list(sending.send_batch(
            member, campaign, [c.id for c in contacts],
            gmail=FakeGmail(raise_auth=True), chunk_size=10,
        ))

        assert {o.status for o in outcomes} == {sending.FAILED}
        # Exactly the first chunk: claimed, settled, and re-claimable.
        assert len(outcomes) == 10
        assert CampaignMailing.objects.filter(
            status=MailingStatus.FAILED.value
        ).count() == 10
        # Nothing left ambiguous.
        assert CampaignMailing.objects.filter(
            status=MailingStatus.DRAFT.value
        ).count() == 0
        # The other fifteen were never claimed, so they are untouched.
        assert CampaignMailing.objects.count() == 10

    def test_sending_twice_yields_no_second_mail(self, member, campaign, contact):
        """The guarantee, through the new code path."""
        contact.assigned_to = member
        contact.save()
        gmail = FakeGmail()

        list(sending.send_batch(member, campaign, [contact.id], gmail=gmail))
        second = list(sending.send_batch(member, campaign, [contact.id], gmail=gmail))

        assert second[0].status == sending.ALREADY_MAILED
        assert len(gmail.sent) == 1
        assert CampaignMailing.objects.filter(contact=contact).count() == 1

    def test_it_claims_in_chunks_rather_than_all_at_once(self, member, campaign):
        """The 19 Aug incident: one large claim stranded 511 contacts.

        Fails if someone "simplifies" send_batch into a single claim -- the
        interruption below would then leave every remaining contact DRAFT.
        """
        contacts = [
            Contact.objects.create(
                first_name=f"P{i}", email=f"p{i}@example.com",
                company="Acme", assigned_to=member,
            )
            for i in range(25)
        ]

        gen = sending.send_batch(
            member, campaign, [c.id for c in contacts],
            gmail=FakeGmail(), chunk_size=sending.CLAIM_CHUNK,
        )
        # Consume one outcome, then abandon the generator as a crash would.
        next(gen)
        gen.close()

        touched = CampaignMailing.objects.count()
        assert touched <= sending.CLAIM_CHUNK, (
            f"{touched} contacts were claimed up front; a crash here strands "
            f"all of them"
        )

    def test_a_budget_stops_between_chunks_never_mid_chunk(self, member, campaign):
        """Anything claimed must be resolved in the same run."""
        contacts = [
            Contact.objects.create(
                first_name=f"P{i}", email=f"p{i}@example.com",
                company="Acme", assigned_to=member,
            )
            for i in range(30)
        ]

        outcomes = list(sending.send_batch(
            member, campaign, [c.id for c in contacts],
            gmail=FakeGmail(), max_mails=5, chunk_size=10,
        ))

        assert len(outcomes) == 10          # the whole first chunk, then stop
        assert CampaignMailing.objects.filter(
            status=MailingStatus.DRAFT.value
        ).count() == 0


# --- the runner ------------------------------------------------------------


class TestTick:
    def test_a_queued_job_is_sent(self, member, campaign, contact):
        from crm.services import scheduling as schedule_svc

        contact.assigned_to = member
        contact.save()
        connect(member)
        schedule_svc.create(
            campaign_id=campaign.id, member=member,
            contact_ids=[str(contact.id)], scheduled_at=timezone.now(),
        )

        gmail = FakeGmail()
        report = runner.tick(gmail_for=lambda m: gmail)

        assert report.sent == 1
        assert len(gmail.sent) == 1
        assert CampaignMailing.objects.get(contact=contact).status == (
            MailingStatus.SENT.value
        )

    def test_a_member_without_gmail_is_skipped_not_failed(
        self, member, campaign, contact
    ):
        """Leasing a job we cannot execute burns the attempts counter and fills
        last_error with the same sentence every tick."""
        from crm.services import scheduling as schedule_svc

        contact.assigned_to = member
        contact.save()
        job = schedule_svc.create(
            campaign_id=campaign.id, member=member,
            contact_ids=[str(contact.id)], scheduled_at=timezone.now(),
        )

        report = runner.tick()

        assert report.members == 0
        assert report.jobs == 0
        job.refresh_from_db()
        assert job.attempts == 0


# --- the browser-facing surface -------------------------------------------


class TestGmailSettingsPage:
    def _sign_in(self, client, member):
        session = client.session
        session[auth_svc.SESSION_KEY] = str(member.id)
        session.save()

    def test_it_offers_a_connect_button_when_disconnected(
        self, client, member, settings
    ):
        settings.GOOGLE_OAUTH_CLIENT_ID = "id"
        settings.GOOGLE_OAUTH_CLIENT_SECRET = "secret"
        self._sign_in(client, member)

        body = client.get(reverse("crm:gmail_settings")).content.decode()
        assert "Connect Gmail" in body

    def test_an_unconfigured_deployment_says_so_rather_than_offering_a_dead_button(
        self, client, member, settings
    ):
        settings.GOOGLE_OAUTH_CLIENT_ID = ""
        settings.GOOGLE_OAUTH_CLIENT_SECRET = ""
        self._sign_in(client, member)

        body = client.get(reverse("crm:gmail_settings")).content.decode()
        assert "Not available on this deployment" in body

    def test_a_connected_page_never_renders_the_token(self, client, member):
        self._sign_in(client, member)
        connect(member, refresh="super-secret-refresh")

        body = client.get(reverse("crm:gmail_settings")).content.decode()
        assert "super-secret-refresh" not in body
        assert member.bits_email in body

    def test_a_stranger_cannot_reach_it(self, client):
        r = client.get(reverse("crm:gmail_settings"))
        assert r.status_code == 302

    def test_connecting_without_a_key_configured_refuses_early(
        self, client, member, settings
    ):
        """Better than completing the whole consent dance and then failing to
        store the result, which reads to the member as Google refusing them."""
        settings.GMAIL_TOKEN_KEY = ""
        settings.GOOGLE_OAUTH_CLIENT_ID = "id"
        settings.GOOGLE_OAUTH_CLIENT_SECRET = "secret"
        self._sign_in(client, member)

        r = client.post(reverse("crm:gmail_connect"))
        assert r.status_code == 302
        assert GmailCredential.objects.count() == 0


class TestSendPage:
    def _sign_in(self, client, member):
        session = client.session
        session[auth_svc.SESSION_KEY] = str(member.id)
        session.save()

    def test_it_lists_only_my_unmailed_contacts(self, client, member, campaign, contact):
        other = TeamMember.objects.create(
            name="Aarav", bits_email="aarav@pilani.bits-pilani.ac.in", batch="2024"
        )
        theirs = Contact.objects.create(
            first_name="Someone", email="someone@example.com",
            company="Elsewhere", assigned_to=other,
        )
        contact.assigned_to = member
        contact.save()
        self._sign_in(client, member)

        body = client.get(
            reverse("crm:send"), {"campaign": str(campaign.id)}
        ).content.decode()

        assert contact.email in body
        assert theirs.email not in body

    def test_an_already_mailed_contact_disappears_from_the_queue(
        self, client, member, campaign, contact
    ):
        contact.assigned_to = member
        contact.save()
        CampaignMailing.objects.create(
            campaign=campaign, contact=contact, sent_by=member,
            status=MailingStatus.SENT.value,
        )
        self._sign_in(client, member)

        body = client.get(
            reverse("crm:send"), {"campaign": str(campaign.id)}
        ).content.decode()
        assert contact.email not in body

    def test_pressing_send_without_gmail_refuses_and_queues_nothing(
        self, client, member, campaign, contact
    ):
        from crm.models import ScheduledSend

        contact.assigned_to = member
        contact.save()
        self._sign_in(client, member)

        r = client.post(reverse("crm:send"), {
            "campaign": str(campaign.id),
            "contact_ids": [str(contact.id)],
            "action": "send",
        })

        assert r.status_code == 302
        assert ScheduledSend.objects.count() == 0

    def test_pressing_send_queues_a_job(self, client, member, campaign, contact):
        from crm.models import ScheduledSend

        contact.assigned_to = member
        contact.save()
        connect(member)
        self._sign_in(client, member)

        client.post(reverse("crm:send"), {
            "campaign": str(campaign.id),
            "contact_ids": [str(contact.id)],
            "action": "send",
        })

        job = ScheduledSend.objects.get()
        assert job.member_id == member.id
        assert job.total == 1
        # Nothing is sent inside the request: the instance can be reaped
        # mid-request and a half-finished send strands claimed contacts.
        assert CampaignMailing.objects.count() == 0

    def test_a_dry_run_writes_nothing(self, client, member, campaign, contact):
        contact.assigned_to = member
        contact.save()
        self._sign_in(client, member)

        body = client.post(reverse("crm:send"), {
            "campaign": str(campaign.id),
            "contact_ids": [str(contact.id)],
            "action": "preflight",
        }).content.decode()

        assert "Dry run" in body
        assert CampaignMailing.objects.count() == 0
