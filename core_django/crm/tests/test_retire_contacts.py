"""The safety properties of `retire_contacts`, which are the whole point of it.

This command is a loaded gun pointed at the contact pool: one missing argument
and an unfiltered queryset would take out every prospect the team has. Every
test here pins a refusal rather than a feature -- what it declines to do without
a selector, without --apply, and while a scheduled send still names the rows.
"""

import uuid

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from crm.models import Campaign, CampaignMailing, Contact, ScheduledSend
from crm.tests.conftest import make_lead, make_member
from shared.enums import CampaignStatus, MailingStatus, ScheduleStatus


def run(*args, **kwargs):
    """Call the command, handing back everything it printed."""
    from io import StringIO

    out = StringIO()
    call_command("retire_contacts", *args, stdout=out, stderr=out, **kwargs)
    return out.getvalue()


@pytest.fixture
def member(team):
    return make_member(team)


@pytest.fixture
def lead(team):
    return make_lead(team)


AS = ("--as", "aarav@pilani.bits-pilani.ac.in")


@pytest.fixture
def fixtures(db):
    """Ten plus-addressed test contacts, tagged the way the CSV tags them."""
    return [
        Contact.objects.create(
            first_name=f"Test{i}",
            email=f"f20250882+t{i:03d}@pilani.bits-pilani.ac.in",
            company="Dummy Dynamics",
            tags=["testdata"],
        )
        for i in range(1, 11)
    ]


@pytest.fixture
def real_contact(db):
    """A live prospect that no selector in these tests may ever touch."""
    return Contact.objects.create(
        first_name="Viren", last_name="Suthar",
        email="viren@razorpay.com", company="Razorpay", tags=["fintech"],
    )


# --- refusing to run at all -----------------------------------------------

def test_no_selector_is_refused(db, fixtures, real_contact):
    """The failure mode this guard exists for: a forgotten argument."""
    with pytest.raises(CommandError, match="no selector"):
        run("--apply", "--delete", "--yes")

    assert Contact.objects.count() == 11


def test_selectors_are_anded_not_ored(db, fixtures, real_contact):
    """Two selectors narrow the match. If they ORed, the real contact would be in it."""
    out = run("--email-prefix", "f20250882+t", "--company", "Nowhere Ltd")
    assert "Nothing matched" in out


def test_a_selector_matching_nothing_writes_nothing(db, fixtures, lead):
    out = run("--email-prefix", "nobody+", "--apply", *AS)
    assert "Nothing matched" in out
    assert Contact.objects.filter(is_archived=True).count() == 0


# --- the dry run ----------------------------------------------------------

def test_dry_run_is_the_default(db, fixtures):
    out = run("--email-prefix", "f20250882+t")

    assert "10 contact(s) matched" in out
    assert "Dry run. Nothing was written." in out
    assert Contact.objects.filter(is_archived=True).count() == 0


def test_dry_run_names_the_rows_it_would_touch(db, fixtures):
    out = run("--tag", "testdata")
    assert "f20250882+t001@pilani.bits-pilani.ac.in" in out


# --- archive --------------------------------------------------------------

def test_archive_leaves_the_rows_and_their_history(db, fixtures, real_contact, member, team, lead):
    campaign = Campaign.objects.create(
        title="Sponsorship", mail_sub="Hi", mail_body="Hello",
        status=CampaignStatus.ACTIVE.value, team=team,
    )
    CampaignMailing.objects.create(
        campaign=campaign, root_campaign=campaign, contact=fixtures[0],
        sent_by=member, status=MailingStatus.SENT.value,
    )

    run("--email-prefix", "f20250882+t", "--apply", *AS)

    assert Contact.objects.filter(is_archived=True).count() == 10
    # The row and its mail record both survive -- that is the difference
    # between this mode and --delete.
    assert Contact.objects.filter(email__startswith="f20250882+t").count() == 10
    assert CampaignMailing.objects.count() == 1


def test_archive_does_not_touch_anything_outside_the_selector(db, fixtures, real_contact, lead):
    run("--email-prefix", "f20250882+t", "--apply", *AS)

    real_contact.refresh_from_db()
    assert real_contact.is_archived is False


def test_archive_writes_an_audit_row_per_contact(db, fixtures, lead):
    """A bulk update would be faster and would lose this."""
    run("--tag", "testdata", "--apply", *AS)

    audits = fixtures[0].audits.filter(field="is_archived")
    assert audits.count() == 1
    assert audits.first().new_value == "True"


# --- delete ---------------------------------------------------------------

def test_delete_removes_the_rows_and_their_mailings(db, fixtures, member, team):
    campaign = Campaign.objects.create(
        title="Sponsorship", mail_sub="Hi", mail_body="Hello",
        status=CampaignStatus.ACTIVE.value, team=team,
    )
    for c in fixtures:
        CampaignMailing.objects.create(
            campaign=campaign, root_campaign=campaign, contact=c,
            sent_by=member, status=MailingStatus.SENT.value,
        )

    run("--email-prefix", "f20250882+t", "--apply", "--delete", "--yes")

    assert Contact.objects.filter(email__startswith="f20250882+t").count() == 0
    assert CampaignMailing.objects.count() == 0


def test_delete_warns_that_mail_history_goes_with_it(db, fixtures, member, team):
    campaign = Campaign.objects.create(
        title="Sponsorship", mail_sub="Hi", mail_body="Hello",
        status=CampaignStatus.ACTIVE.value, team=team,
    )
    CampaignMailing.objects.create(
        campaign=campaign, root_campaign=campaign, contact=fixtures[0],
        sent_by=member, status=MailingStatus.SENT.value,
    )

    out = run("--email-prefix", "f20250882+t", "--delete")
    assert "1 CampaignMailing row(s) will be destroyed" in out


def test_delete_is_refused_while_a_live_job_still_names_them(db, fixtures, member, team):
    """The dangling-id crash, pinned.

    claim_batch resolves ids with an unguarded `.get(id=...)`. A deleted id left
    in a PENDING job takes the next tick down with it, so the command must
    refuse rather than produce that.
    """
    campaign = Campaign.objects.create(
        title="Sponsorship", mail_sub="Hi", mail_body="Hello",
        status=CampaignStatus.ACTIVE.value, team=team,
    )
    ScheduledSend.objects.create(
        campaign=campaign, member=member,
        contact_ids=[fixtures[3].id],
        scheduled_at="2026-09-01T10:00:00Z",
        status=ScheduleStatus.PENDING.value,
    )

    with pytest.raises(CommandError, match="scheduled sends"):
        run("--email-prefix", "f20250882+t", "--apply", "--delete", "--yes")

    assert Contact.objects.filter(email__startswith="f20250882+t").count() == 10


def test_a_finished_job_does_not_block_a_delete(db, fixtures, member, team):
    """A DONE job is history. Its ids are a record, not a lookup waiting to happen."""
    campaign = Campaign.objects.create(
        title="Sponsorship", mail_sub="Hi", mail_body="Hello",
        status=CampaignStatus.ACTIVE.value, team=team,
    )
    ScheduledSend.objects.create(
        campaign=campaign, member=member,
        contact_ids=[fixtures[3].id],
        scheduled_at="2026-09-01T10:00:00Z",
        status=ScheduleStatus.DONE.value,
    )

    run("--email-prefix", "f20250882+t", "--apply", "--delete", "--yes")
    assert Contact.objects.filter(email__startswith="f20250882+t").count() == 0


def test_archive_is_allowed_while_a_live_job_names_them(db, fixtures, member, team, lead):
    """The guard is specific to --delete: archiving with a job in flight is safe,
    because the row survives for claim_batch to skip politely."""
    campaign = Campaign.objects.create(
        title="Sponsorship", mail_sub="Hi", mail_body="Hello",
        status=CampaignStatus.ACTIVE.value, team=team,
    )
    ScheduledSend.objects.create(
        campaign=campaign, member=member,
        contact_ids=[fixtures[3].id, uuid.uuid4()],
        scheduled_at="2026-09-01T10:00:00Z",
        status=ScheduleStatus.PENDING.value,
    )

    run("--email-prefix", "f20250882+t", "--apply", *AS)
    assert Contact.objects.filter(is_archived=True).count() == 10


def test_delete_without_yes_asks_for_the_count(db, fixtures, monkeypatch):
    """Typing the number back means having read the report."""
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "nope")

    with pytest.raises(CommandError, match="Not confirmed"):
        run("--email-prefix", "f20250882+t", "--apply", "--delete")

    assert "10" in asked[0]
    assert Contact.objects.filter(email__startswith="f20250882+t").count() == 10


def test_delete_proceeds_when_the_count_is_typed_back(db, fixtures, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "10")

    run("--email-prefix", "f20250882+t", "--apply", "--delete")
    assert Contact.objects.filter(email__startswith="f20250882+t").count() == 0


# --- the address shape this was written for -------------------------------

def test_the_plus_prefix_does_not_match_the_bare_address(db, lead):
    """The member's own mailbox must survive a selector aimed at its plus tags.

    `f20250882+t` and `f20250882@` differ by one character, and getting this
    wrong archives the address the team actually mails from.
    """
    own = Contact.objects.create(
        first_name="Pratham", email="f20250882@pilani.bits-pilani.ac.in",
        company="PIEDS",
    )
    Contact.objects.create(
        first_name="Test", email="f20250882+t001@pilani.bits-pilani.ac.in",
        company="Dummy Dynamics",
    )

    run("--email-prefix", "f20250882+t", "--apply", *AS)

    own.refresh_from_db()
    assert own.is_archived is False
    assert Contact.objects.filter(is_archived=True).count() == 1


# --- who it runs as -------------------------------------------------------

def test_archive_without_an_actor_is_refused(db, fixtures, lead):
    """`set_archived` runs a real permission check; a nameless run cannot pass it,
    and the audit row it would write would name nobody."""
    with pytest.raises(CommandError, match="needs --as"):
        run("--email-prefix", "f20250882+t", "--apply")

    assert Contact.objects.filter(is_archived=True).count() == 0


def test_the_refusal_names_the_leads_it_would_accept(db, fixtures, lead):
    with pytest.raises(CommandError, match="aarav@pilani.bits-pilani.ac.in"):
        run("--email-prefix", "f20250882+t", "--apply")


def test_archiving_as_a_non_lead_is_refused(db, fixtures, member):
    """Only a lead may edit a contact that is not assigned to them."""
    with pytest.raises(CommandError, match="not a lead"):
        run("--email-prefix", "f20250882+t", "--apply",
            "--as", "kabir@pilani.bits-pilani.ac.in")

    assert Contact.objects.filter(is_archived=True).count() == 0


def test_the_audit_row_names_the_lead_who_ran_it(db, fixtures, lead):
    run("--tag", "testdata", "--apply", *AS)

    audit = fixtures[0].audits.filter(field="is_archived").first()
    assert audit.actor_id == lead.id


def test_the_prefix_is_anchored_not_a_substring_search(db, lead):
    """--email-prefix means the START of the address.

    A substring match reads as harmlessly more generous until an address that
    merely contains the fragment gets archived with the batch. Forwarding and
    alias addresses carry other people's usernames in the middle of them, which
    is exactly where an unanchored match finds them.
    """
    passenger = Contact.objects.create(
        first_name="Alias", email="ops+f20250882+t001@razorpay.com",
        company="Razorpay",
    )
    target = Contact.objects.create(
        first_name="Test", email="f20250882+t001@pilani.bits-pilani.ac.in",
        company="Dummy Dynamics",
    )

    run("--email-prefix", "f20250882+t", "--apply", *AS)

    passenger.refresh_from_db()
    target.refresh_from_db()
    assert passenger.is_archived is False
    assert target.is_archived is True
