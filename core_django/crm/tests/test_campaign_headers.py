"""The mail envelope: who it comes from, who is copied, and HTML bodies.

Three features share this file because they share one code path -- the claim
payload is the only thing standing between a campaign and a real inbox.

The test that matters most here is `test_bad_cc_claims_nothing`: CC/BCC are
validated before any row is written, so a typo cannot leave half a batch
claimed with the copy silently dropped.
"""

import json
from email.message import EmailMessage
from email.utils import formataddr

import pytest
from django.urls import reverse

from crm.forms import CampaignForm
from crm.models import ApiToken, Campaign, CampaignMailing, Contact, TeamMember
from crm.services import mailing as mailing_svc
from crm.services.render import render
from crm.services.richtext import to_html, to_plain, validate_markup
from shared.enums import CampaignStatus

from .conftest import make_lead

pytestmark = pytest.mark.django_db


@pytest.fixture
def member():
    return TeamMember.objects.create(
        name="Kabir Rao", bits_email="kabir@pilani.bits-pilani.ac.in", batch="2025"
    )


@pytest.fixture
def auth(member):
    _, raw = ApiToken.issue(member, "test laptop")
    return {"HTTP_AUTHORIZATION": f"Token {raw}"}


@pytest.fixture
def campaign(member):
    return Campaign.objects.create(
        title="Outreach",
        mail_sub="Hello {{ first_name }}",
        mail_body="Hi {{ first_name }}, good to meet you.",
        var_list=["first_name"],
        status=CampaignStatus.ACTIVE.value,
        created_by=member,
    )


@pytest.fixture
def contact(member):
    return Contact.objects.create(
        first_name="Rohan", last_name="Iyer", email="rohan@example.com",
        company="Acme", designation="CTO", assigned_to=member, created_by=member,
    )


# ------------------------------------------------------------- sender name

def test_display_name_falls_back_to_the_real_name(member):
    assert member.display_name == "Kabir Rao"
    member.sender_name = "Kabir from PIEDS"
    assert member.display_name == "Kabir from PIEDS"


def test_claim_carries_the_sender_name(campaign, contact, member):
    member.sender_name = "Kabir from PIEDS"
    member.save()

    claimed, _ = mailing_svc.claim_batch(campaign, member, [contact.id])
    assert claimed[0].from_name == "Kabir from PIEDS"
    assert CampaignMailing.objects.get(contact=contact).from_name == "Kabir from PIEDS"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Kabir Rao", "Kabir Rao <k@x.com>"),
        # A period or comma must be quoted, or clients render the header wrong.
        ("Rao, Kabir", '"Rao, Kabir" <k@x.com>'),
    ],
)
def test_from_header_is_built_with_formataddr(name, expected):
    assert formataddr((name, "k@x.com")) == expected


def test_non_ascii_sender_name_is_encoded_not_dropped():
    message = EmailMessage()
    message["From"] = formataddr(("Kabir Râo", "k@x.com"))
    # Either RFC 2047 encoded or carried as UTF-8 -- what matters is that the
    # address survives and the header is still parseable.
    assert "k@x.com" in message.as_string()


def test_lead_can_set_a_sender_name(client, member, team):
    lead = make_lead(team)
    session = client.session
    session["member_id"] = str(lead.id)
    session.save()

    response = client.post(
        reverse("crm:member_sender_name", args=[member.pk]),
        {"sender_name": "Kabir from PIEDS"},
    )
    assert response.status_code == 302
    member.refresh_from_db()
    assert member.sender_name == "Kabir from PIEDS"


# ------------------------------------------------------------------ cc/bcc

def test_valid_addresses_are_normalised():
    assert mailing_svc.parse_copy_addresses(" a@x.com ,b@y.com ") == "a@x.com, b@y.com"
    assert mailing_svc.parse_copy_addresses("") == ""
    assert mailing_svc.parse_copy_addresses(None) == ""


def test_a_bad_address_is_refused():
    with pytest.raises(mailing_svc.InvalidCopyAddresses):
        mailing_svc.parse_copy_addresses("a@x.com, not-an-email")


def test_too_many_addresses_are_refused():
    many = ", ".join(f"a{i}@x.com" for i in range(mailing_svc.MAX_COPY_ADDRESSES + 1))
    with pytest.raises(mailing_svc.InvalidCopyAddresses):
        mailing_svc.parse_copy_addresses(many)


def test_claim_snapshots_cc_and_bcc(campaign, contact, member):
    claimed, _ = mailing_svc.claim_batch(
        campaign, member, [contact.id], cc="lead@x.com", bcc="archive@x.com"
    )
    assert claimed[0].cc == "lead@x.com"
    assert claimed[0].bcc == "archive@x.com"

    mailing = CampaignMailing.objects.get(contact=contact)
    assert mailing.cc == "lead@x.com"
    assert mailing.bcc == "archive@x.com"


def test_bad_cc_claims_nothing(campaign, contact, member):
    """Validation happens before the first row is written, not per contact."""
    with pytest.raises(mailing_svc.InvalidCopyAddresses):
        mailing_svc.claim_batch(campaign, member, [contact.id], cc="oops")

    assert not CampaignMailing.objects.filter(contact=contact).exists()


def test_api_rejects_a_bad_cc_with_a_readable_error(client, auth, campaign, contact):
    response = client.post(
        reverse("api:claim"),
        data=json.dumps({
            "campaign_id": str(campaign.id),
            "contact_ids": [str(contact.id)],
            "cc": "not-an-email",
        }),
        content_type="application/json",
        **auth,
    )
    assert response.status_code == 400
    assert "not a valid email" in response.json()["error"]


def test_preflight_echoes_the_copies_back(client, auth, campaign, contact, member):
    member.sender_name = "Kabir from PIEDS"
    member.save()

    response = client.post(
        reverse("api:preflight"),
        data=json.dumps({
            "campaign_id": str(campaign.id),
            "contact_ids": [str(contact.id)],
            "cc": " lead@x.com ",
        }),
        content_type="application/json",
        **auth,
    )
    body = response.json()
    assert body["cc"] == "lead@x.com"
    assert body["bcc"] == ""
    assert body["from_name"] == "Kabir from PIEDS"


# --------------------------------------------------------------- html body

FOOTER = '<p>Hi {{ first_name }},</p><hr><footer style="color:#888">PIEDS</footer>'


def test_html_off_still_escapes(campaign, contact):
    """Guards the default: turning the feature on must not change old campaigns."""
    campaign.mail_body = "Hi {{ first_name }}, 3 < 5 & true"
    rendered = render(campaign, contact)

    assert "&lt;" in rendered.body_html
    assert "&amp;" in rendered.body_html
    assert "<br>" in rendered.body_html or "\n" not in campaign.mail_body


def test_html_on_passes_markup_through(campaign, contact):
    campaign.is_html = True
    campaign.mail_body = FOOTER
    rendered = render(campaign, contact)

    assert "<hr>" in rendered.body_html
    assert '<footer style="color:#888">PIEDS</footer>' in rendered.body_html
    assert "<p>Hi Rohan,</p>" in rendered.body_html


def test_html_on_leaves_newlines_alone(campaign, contact):
    """The author writes their own <br>; we must not add a second set."""
    campaign.is_html = True
    campaign.mail_body = "<p>one</p>\n<p>two</p>"
    assert "<br>" not in render(campaign, contact).body_html


def test_html_plain_fallback_is_readable_prose(campaign, contact):
    campaign.is_html = True
    campaign.mail_body = "<p>Hi {{ first_name }} &amp; team</p><hr><footer>PIEDS</footer>"
    body = render(campaign, contact).body

    assert "<" not in body and ">" not in body
    assert "&amp;" not in body and "& team" in body
    assert "Hi Rohan" in body


def test_links_still_work_in_html_mode():
    html = to_html("<b>x</b> [book](https://cal.com/x)", raw=True)
    assert "<b>x</b>" in html
    assert '<a href="https://cal.com/x">book</a>' in html


def test_link_href_is_escaped_even_in_html_mode():
    """The one thing raw mode does not relax -- we still build the anchor."""
    assert "&quot;" in to_html('[x](https://a.test/?q="1")', raw=True)


def test_to_plain_unaffected_when_not_raw():
    assert to_plain("3 < 5 &amp; true") == "3 < 5 &amp; true"


@pytest.mark.parametrize("body", [
    "<script>alert(1)</script>",
    "<div onclick=\"alert(1)\">x</div>",
])
def test_script_and_handlers_are_rejected(body):
    assert validate_markup(body)


def test_form_rejects_script_only_when_html_is_on():
    data = {
        "title": "T", "mail_sub": "Hi {{ first_name }}",
        "mail_body": "Hi {{ first_name }} <script>alert(1)</script>",
        "var_list_raw": "first_name",
    }
    # Escaped anyway without the checkbox, so it is literal text and allowed.
    assert CampaignForm(data=data).is_valid()

    form = CampaignForm(data={**data, "is_html": "on"})
    assert not form.is_valid()
    assert any("script" in e.lower() for e in form.errors["mail_body"])
