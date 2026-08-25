"""What happens when a batch is interrupted, and how it is recovered.

This file exists because of a real incident. On 19 Aug two members selected
402 and 109 contacts; the agent claimed every one up front, then the Windows
laptops' connections died partway through (`WinError 10053`). 511 contacts were
left with committed DRAFT rows and no mail. Because a claimed contact could not
be re-claimed, and nothing in the product called `reset_for_retry`, those mails
could not be sent again at all. The CRM showed 511 "in flight" and the operator
saw activity that would never finish.

Three tests here pin the three fixes:
  - `test_a_failed_mailing_can_be_claimed_again` -- recovery is possible at all
  - `test_an_interrupted_batch_strands_only_one_chunk` -- damage is bounded
  - `test_a_sent_mailing_is_never_reclaimed` -- the fix did not weaken the
    guarantee it is built on
"""

import pytest
from django.utils import timezone

from crm.models import Campaign, CampaignMailing, Contact, TeamMember
from crm.services import mailing as svc
from crm.services import sending as send_svc
from shared.enums import CampaignStatus, MailingStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def member():
    return TeamMember.objects.create(
        name="Kabir", bits_email="kabir@pilani.bits-pilani.ac.in", batch="2025"
    )


@pytest.fixture
def campaign(member):
    return Campaign.objects.create(
        title="Outreach", mail_sub="Hi {{ first_name }}", mail_body="Hello {{ first_name }}.",
        var_list=["first_name"], status=CampaignStatus.ACTIVE.value, created_by=member,
    )


def make_contact(member, n=0, **kwargs):
    fields = dict(
        first_name=f"Rohan{n}", last_name="Iyer", email=f"rohan{n}@example.com",
        company="Acme", designation="CTO", assigned_to=member, created_by=member,
    )
    fields.update(kwargs)
    return Contact.objects.create(**fields)


@pytest.fixture
def contact(member):
    return make_contact(member)


# ------------------------------------------------------- recovery is possible

def test_a_failed_mailing_can_be_claimed_again(campaign, member, contact):
    """The core of the incident: a mail that never reached anyone must be
    sendable again, or the contact is lost to that campaign forever."""
    claimed, _ = svc.claim_batch(campaign, member, [contact.id])
    svc.record_result(claimed[0].mailing_id, member, status="failed", error="connection died")

    again, skipped = svc.claim_batch(campaign, member, [contact.id])
    assert len(again) == 1 and not skipped
    assert CampaignMailing.objects.filter(campaign=campaign, contact=contact).count() == 1


def test_a_retry_re_renders_rather_than_reusing_the_old_snapshot(campaign, member, contact):
    """Someone retrying has usually just fixed the thing that broke."""
    claimed, _ = svc.claim_batch(campaign, member, [contact.id])
    svc.record_result(claimed[0].mailing_id, member, status="failed", error="boom")

    contact.first_name = "Rohit"
    contact.save()
    again, _ = svc.claim_batch(campaign, member, [contact.id])

    assert "Rohit" in again[0].body
    mailing = CampaignMailing.objects.get(campaign=campaign, contact=contact)
    assert "Rohit" in mailing.rendered_body
    assert mailing.error_detail == ""          # the old failure is cleared


def test_a_retry_reuses_the_row_so_history_is_one_line(campaign, member, contact):
    claimed, _ = svc.claim_batch(campaign, member, [contact.id])
    first_id = claimed[0].mailing_id
    svc.record_result(first_id, member, status="failed", error="boom")

    again, _ = svc.claim_batch(campaign, member, [contact.id])
    assert again[0].mailing_id == first_id


# ------------------------------------------------- the guarantee is untouched

def test_a_sent_mailing_is_never_reclaimed(campaign, member, contact):
    """The whole system rests on this. Making failures retryable must not have
    opened a door to mailing someone twice."""
    claimed, _ = svc.claim_batch(campaign, member, [contact.id])
    svc.record_result(claimed[0].mailing_id, member, status="sent", message_id="m1", thread_id="t1")

    again, skipped = svc.claim_batch(campaign, member, [contact.id])
    assert again == []
    assert skipped[0].code == svc.ALREADY_MAILED
    assert "sent" in skipped[0].reason


def test_a_stranded_draft_is_not_silently_resent(campaign, member, contact):
    """A DRAFT may already be in someone's inbox with the report lost. It has to
    go through reconcile against Gmail, not be assumed undelivered."""
    svc.claim_batch(campaign, member, [contact.id])

    again, skipped = svc.claim_batch(campaign, member, [contact.id])
    assert again == []
    assert skipped[0].code == svc.ALREADY_MAILED
    assert "Resolve stranded drafts" in skipped[0].reason


def test_the_skip_reason_carries_a_code_not_just_prose(campaign, member):
    """The agent used to infer status by matching English substrings, so
    rewording a message silently turned 'already mailed' into 'failed'."""
    stranger = TeamMember.objects.create(
        name="Ishita", bits_email="ishita@pilani.bits-pilani.ac.in", batch="2025"
    )
    theirs = make_contact(stranger, 5)

    _, skipped = svc.claim_batch(campaign, member, [theirs.id])
    assert skipped[0].code == svc.NOT_ASSIGNED


# --------------------------------------------------------- damage is bounded

class FlakyGmail:
    """Sends `ok_count` mails, then behaves like a dropped connection."""

    def __init__(self, ok_count):
        self.ok_count = ok_count
        self.sent = []

    def send(self, *, to, subject, body, **kwargs):
        if len(self.sent) >= self.ok_count:
            raise ConnectionAbortedError("[WinError 10053] connection aborted")
        self.sent.append(to)

        class R:
            message_id = f"m{len(self.sent)}"
            thread_id = f"t{len(self.sent)}"
        return R()


# NOTE: there used to be a `DirectApi` shim here, standing in for the HTTP
# surface the laptop agent's send_batch spoke to. The sender now calls
# claim_batch and record_result in-process, so the shim is gone and these tests
# exercise the real path with nothing in between. What they assert is unchanged:
# the incident they encode is about ORDERING and CHUNKING, neither of which
# moved when the code did.


def test_an_interrupted_batch_strands_only_one_chunk(campaign, member):
    """The incident in miniature.

    25 contacts, the connection dies after 3 mails. With the old
    claim-everything-up-front behaviour all 25 would hold DRAFT rows. Chunked,
    only the chunk in flight is affected.
    """
    contacts = [make_contact(member, i) for i in range(25)]
    gmail = FlakyGmail(ok_count=3)

    outcomes = list(send_svc.send_batch(
        member, campaign, [c.id for c in contacts],
        gmail=gmail, delay=0, chunk_size=10,
    ))

    assert len(gmail.sent) == 3
    assert sum(1 for o in outcomes if o.status == send_svc.SENT) == 3

    stranded = CampaignMailing.objects.filter(
        campaign=campaign, status=MailingStatus.DRAFT.value
    ).count()
    # Only the 10-contact chunk that was in flight can be affected, and the
    # failures inside it were reported, so in practice nothing is stranded.
    assert stranded <= 10, f"{stranded} contacts stranded -- the whole point was to bound this"

    # Everything the connection touched after the break is FAILED, which is
    # re-claimable. Nothing is stuck.
    failed = CampaignMailing.objects.filter(
        campaign=campaign, status=MailingStatus.FAILED.value
    ).count()
    assert failed >= 1


def test_the_whole_batch_is_recoverable_after_an_interruption(campaign, member):
    """After the connection comes back, sending again must actually send."""
    contacts = [make_contact(member, i) for i in range(25)]

    list(send_svc.send_batch(
        member, campaign, [c.id for c in contacts],
        gmail=FlakyGmail(ok_count=3), delay=0, chunk_size=10,
    ))

    # Resolve whatever was left mid-flight, as "Resolve stranded drafts" would.
    for m in CampaignMailing.objects.filter(campaign=campaign, status=MailingStatus.DRAFT.value):
        svc.record_result(m.id, member, status="failed", error="stranded")

    healthy = FlakyGmail(ok_count=99)
    list(send_svc.send_batch(
        member, campaign, [c.id for c in contacts],
        gmail=healthy, delay=0, chunk_size=10,
    ))

    sent = CampaignMailing.objects.filter(
        campaign=campaign, status=MailingStatus.SENT.value
    ).count()
    assert sent == 25, f"only {sent}/25 recovered"
    # And nobody was mailed twice.
    assert len(healthy.sent) == 22 and len(set(healthy.sent)) == 22


def test_a_claim_failure_strands_nothing(campaign, member, monkeypatch):
    """If the database is unreachable the chunk was never claimed, so there is
    nothing to recover -- the run just stops and says so."""
    contacts = [make_contact(member, i) for i in range(5)]

    def dead_claim(*a, **k):
        raise ConnectionError("[WinError 10053] connection aborted")

    monkeypatch.setattr(send_svc.mailing, "claim_batch", dead_claim)

    outcomes = list(send_svc.send_batch(
        member, campaign, [c.id for c in contacts],
        gmail=FlakyGmail(99), delay=0, chunk_size=10,
    ))

    assert all(o.status == send_svc.FAILED for o in outcomes)
    assert "could not reserve" in outcomes[0].detail
    assert CampaignMailing.objects.count() == 0
