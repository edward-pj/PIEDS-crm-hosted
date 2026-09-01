"""One button for the whole queue.

Sending used to mean ticking a box per contact. That is fine for twelve and
impossible for eight hundred, and it fails in a way nobody would guess: Django's
`DATA_UPLOAD_MAX_NUMBER_FIELDS` is 1000, so a member with a four-figure list
gets `TooManyFieldsSent` rather than a send -- an error that arrives exactly
when the feature starts being worth having.

"Send all" posts a single flag and lets the server re-resolve the queue with
`_send_queue`, the same function that produced the count printed on the page.
These tests pin that the two cannot disagree, and that resolving server-side
did not become a way to mail somebody else's contacts.

`TestSendingOnlySome` is the counterweight, and it exists because the first
version of this screen went too far the other way: every row box rendered
`checked` and the selective buttons were folded into a collapsed <details>, so
"mail everyone assigned to me" was what happened if you did nothing, and mailing
two people meant un-ticking several hundred boxes. Both paths have to stay one
click, and neither may be the accidental one.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from crm.models import Campaign, CampaignMailing, Contact, ScheduledSend
from crm.tests.conftest import make_lead, make_member, sign_in
from crm.tests.test_gmail_credentials import connect
from shared.enums import CampaignStatus, MailingStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def sender(team):
    member = make_member(team)
    connect(member)
    return member


@pytest.fixture
def campaign(team):
    return Campaign.objects.create(
        title="Ignite", mail_sub="Hello", mail_body="About your work",
        status=CampaignStatus.ACTIVE.value, team=team,
    )


def assign(member, count, *, prefix="c"):
    return [
        Contact.objects.create(
            first_name=f"C{i}", email=f"{prefix}{i}@example.com",
            company="Acme", assigned_to=member,
        )
        for i in range(count)
    ]


def send_all(client, campaign):
    return client.post(
        reverse("crm:send"), {"campaign": str(campaign.id), "action": "send_all"}
    )


class TestSendAll:
    def test_one_post_queues_the_whole_list(self, client, sender, campaign):
        assign(sender, 30)
        sign_in(client, sender)

        send_all(client, campaign)

        job = ScheduledSend.objects.get()
        assert job.total == 30
        assert job.member == sender

    def test_it_carries_no_contact_ids_in_the_request(self, client, sender, campaign):
        """The whole reason it exists. If this ever needs a field per contact,
        the 1000-field ceiling is back and so is the bug."""
        assign(sender, 30)
        sign_in(client, sender)

        response = client.post(reverse("crm:send"), {
            "campaign": str(campaign.id), "action": "send_all",
        })

        assert response.status_code == 302
        assert ScheduledSend.objects.get().total == 30

    def test_it_covers_contacts_the_table_never_rendered(
        self, client, sender, campaign, monkeypatch
    ):
        """The table is capped; Send all is not. This is the case that made
        `queue[:500]` a silent truncation rather than a display choice."""
        monkeypatch.setattr("crm.views.SEND_TABLE_LIMIT", 5)
        assign(sender, 20)
        sign_in(client, sender)

        send_all(client, campaign)

        assert ScheduledSend.objects.get().total == 20

    def test_it_agrees_with_the_count_on_the_page(self, client, sender, campaign):
        """"Send all 30" must mean the same 30 the header just claimed."""
        assign(sender, 30)
        sign_in(client, sender)

        body = client.get(
            reverse("crm:send"), {"campaign": str(campaign.id)}
        ).content.decode()
        assert "Send all 30" in body

        send_all(client, campaign)
        assert ScheduledSend.objects.get().total == 30


class TestItResolvesOnlyWhatTheMemberMaySend:
    """Resolving server-side is a privilege boundary, not just a convenience."""

    def test_another_member_s_contacts_are_not_included(
        self, client, sender, campaign, team
    ):
        assign(sender, 3)
        other = make_member(team, name="Ishita", email="ishita@pilani.bits-pilani.ac.in")
        assign(other, 7, prefix="theirs")
        sign_in(client, sender)

        send_all(client, campaign)

        assert ScheduledSend.objects.get().total == 3

    def test_already_mailed_contacts_are_not_included(
        self, client, sender, campaign
    ):
        contacts = assign(sender, 3)
        CampaignMailing.objects.create(
            campaign=campaign, contact=contacts[0], sent_by=sender,
            status=MailingStatus.SENT.value, sent_at=timezone.now(),
        )
        sign_in(client, sender)

        send_all(client, campaign)

        assert ScheduledSend.objects.get().total == 2

    def test_an_empty_queue_queues_nothing(self, client, sender, campaign):
        """Re-renders with an error rather than creating a job of zero."""
        sign_in(client, sender)

        response = send_all(client, campaign)

        assert response.status_code == 200
        assert ScheduledSend.objects.count() == 0

    def test_a_member_without_gmail_is_sent_to_connect_it(
        self, client, team, campaign
    ):
        no_gmail = make_member(team, name="Zoya", email="zoya@pilani.bits-pilani.ac.in")
        assign(no_gmail, 3)
        sign_in(client, no_gmail)

        response = send_all(client, campaign)

        assert response["Location"] == reverse("crm:gmail_settings")
        assert ScheduledSend.objects.count() == 0


class TestDryRunAll:
    def test_it_writes_nothing(self, client, sender, campaign):
        assign(sender, 10)
        sign_in(client, sender)

        client.post(reverse("crm:send"), {
            "campaign": str(campaign.id), "action": "preflight_all",
        })

        assert ScheduledSend.objects.count() == 0
        assert CampaignMailing.objects.count() == 0


class TestSelectiveSendStillWorks:
    def test_ticking_three_boxes_queues_three(self, client, sender, campaign):
        contacts = assign(sender, 10)
        sign_in(client, sender)

        client.post(reverse("crm:send"), {
            "campaign": str(campaign.id), "action": "send",
            "contact_ids": [str(c.id) for c in contacts[:3]],
        })

        assert ScheduledSend.objects.get().total == 3


class TestSendingOnlySome:
    """Picking a couple of contacts has to mail exactly those couple.

    The server was never the problem -- `action=send` has always used whatever
    `contact_ids` it was handed. The screen was: boxes shipped pre-ticked, so
    the default state of the form was "everyone", and the buttons that honour a
    selection were hidden behind a <details>.
    """

    def test_two_selected_contacts_queue_two_mails(self, client, sender, campaign):
        contacts = assign(sender, 25)
        sign_in(client, sender)
        picked = contacts[:2]

        client.post(reverse("crm:send"), {
            "campaign": str(campaign.id),
            "action": "send",
            "contact_ids": [str(c.id) for c in picked],
        })

        job = ScheduledSend.objects.get()
        assert job.total == 2
        # contact_ids round-trips through JSON as UUIDs, not strings.
        assert {str(cid) for cid in job.contact_ids} == {str(c.id) for c in picked}

    def test_one_selected_contact_queues_one_mail(self, client, sender, campaign):
        contacts = assign(sender, 25)
        sign_in(client, sender)

        client.post(reverse("crm:send"), {
            "campaign": str(campaign.id), "action": "send",
            "contact_ids": [str(contacts[7].id)],
        })

        job = ScheduledSend.objects.get()
        assert job.total == 1
        assert [str(cid) for cid in job.contact_ids] == [str(contacts[7].id)]

    def test_a_selection_does_not_quietly_become_everyone(
        self, client, sender, campaign
    ):
        """The failure this screen actually produced. Anything that widens a
        two-contact selection to the whole assigned list is the bug."""
        assign(sender, 40)
        sign_in(client, sender)
        chosen = [str(c.id) for c in Contact.objects.filter(assigned_to=sender)[:2]]

        client.post(reverse("crm:send"), {
            "campaign": str(campaign.id), "action": "send", "contact_ids": chosen,
        })

        assert ScheduledSend.objects.get().total == 2

    def test_selecting_nothing_sends_nothing(self, client, sender, campaign):
        assign(sender, 10)
        sign_in(client, sender)

        client.post(reverse("crm:send"), {
            "campaign": str(campaign.id), "action": "send",
        })

        assert not ScheduledSend.objects.exists()

    def test_a_dry_run_of_a_selection_covers_only_that_selection(
        self, client, sender, campaign
    ):
        contacts = assign(sender, 20)
        sign_in(client, sender)

        response = client.post(reverse("crm:send"), {
            "campaign": str(campaign.id), "action": "preflight",
            "contact_ids": [str(c.id) for c in contacts[:3]],
        })

        assert len(response.context["preflight"]) == 3


class TestTheScreenDoesNotPreSelectEveryone:
    """The template half, which is where the defect actually lived."""

    def test_the_row_boxes_start_empty(self, client, sender, campaign):
        """Pre-ticked boxes made "send to everyone" the outcome of doing
        nothing -- the opposite of what a send screen should default to."""
        assign(sender, 5)
        sign_in(client, sender)

        body = client.get(reverse("crm:send"), {"campaign": str(campaign.id)}).content.decode()
        rows = [line for line in body.splitlines() if 'name="contact_ids"' in line]

        assert rows, "no contact rows rendered"
        for row in rows:
            assert "checked" not in row, f"row ships pre-ticked: {row.strip()}"

    def test_both_ways_to_send_are_offered_outside_a_disclosure(
        self, client, sender, campaign
    ):
        """Selective send sat inside a collapsed <details>, so the only visible
        action mailed the whole list. Both are top-level now."""
        assign(sender, 5)
        sign_in(client, sender)
        body = client.get(reverse("crm:send"), {"campaign": str(campaign.id)}).content.decode()

        assert 'value="send"' in body and 'value="send_all"' in body

        # Nothing between a <details> and its </details> may carry the
        # selective actions.
        for chunk in body.split("<details>")[1:]:
            hidden = chunk.split("</details>")[0]
            assert 'value="send"' not in hidden
            assert 'value="send_all"' not in hidden
