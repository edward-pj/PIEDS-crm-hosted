"""Writing mail markup: raw HTML, the links inside it, images, and the previews.

This file exists because of one production defect. A member pasted the email
signature they use every day into their footer -- a table, a few spans, a
mailto: link -- and every mail they sent went out with the tags visible as
text. Not because the markup was wrong, and not because anything rejected it:
`footer_is_html` was lead-only, so the checkbox that would have rendered it was
simply absent from their form, and no message anywhere said so.

Two things had to change and both are pinned here. The checkbox is now offered
to everybody, and `validate_markup` became a real gate rather than a role -- so
the tests that matter most are the ones proving the gate still refuses what the
role used to (TestTheGateThatReplacedTheRole).

The rest covers what "I cannot see the edits I want to do" also meant: there
was no way to render markup without saving it and sending with it, and a member
with no contacts assigned yet got no preview at all.
"""

import pytest

from crm.forms import CampaignForm, FooterForm
from crm.models import Campaign, Contact
from crm.services import campaigns as campaign_svc
from crm.services import richtext as rt
from crm.services.render import render

from .conftest import make_lead, make_member, sign_in

pytestmark = pytest.mark.django_db


@pytest.fixture
def lead(team):
    return make_lead(team)


@pytest.fixture
def kabir(team):
    return make_member(team)


@pytest.fixture
def root(lead):
    return Campaign.objects.create(
        title="Ignite 26",
        mail_sub="Hello {{ first_name }}",
        mail_body="We run an incubator at BITS.",
        var_list=["first_name"],
        status="active",
        created_by=lead,
    )


@pytest.fixture
def contact(kabir):
    return Contact.objects.create(
        first_name="Rohan", last_name="Mehta", email="rohan@example.com",
        company="Zerodha", designation="Founder", assigned_to=kabir,
    )


# --- the gate that replaced the role ---------------------------------------


class TestTheGateThatReplacedTheRole:
    """`footer_is_html` was lead-only because richtext shipped no sanitiser.

    Opening it to members is only defensible if these still fail closed. If any
    of them can be saved, the change was a straight downgrade.
    """

    @pytest.mark.parametrize("markup", [
        "<script>alert(1)</script>",
        '<div onclick="alert(1)">x</div>',
        '<a href="javascript:alert(1)">x</a>',
        '<a href="vbscript:msgbox">x</a>',
        '<iframe src="https://evil.test"></iframe>',
        '<object data="https://evil.test"></object>',
        '<form action="https://evil.test"><input name="p"></form>',
        '<base href="https://evil.test/">',
        '<img src="data:text/html;base64,PHNjcmlwdD4=">',
        '<div style="width:expression(alert(1))">x</div>',
    ])
    def test_it_is_refused(self, markup):
        assert rt.validate_markup(markup), f"{markup} was accepted"

    def test_the_message_names_the_tag(self):
        """"Invalid HTML" is not actionable; "<iframe> is not allowed" is."""
        assert "iframe" in rt.validate_markup("<iframe src='https://x.test'>")[0]

    def test_one_message_per_tag_however_many_times_it_appears(self):
        """Five <iframe>s are one thing to fix, not five identical red lines."""
        problems = rt.validate_markup("<iframe></iframe>" * 5)
        assert len([p for p in problems if "iframe" in p]) == 1

    def test_the_same_link_is_judged_the_same_way_in_both_syntaxes(self):
        """`[x](javascript:1)` was refused and `<a href="javascript:1">` was
        not -- one link, written two ways, held to two different standards."""
        assert rt.validate_links("[x](javascript:alert(1))")
        assert rt.validate_markup('<a href="javascript:alert(1)">x</a>')

    def test_none_of_it_applies_when_the_html_box_is_off(self):
        """Without the checkbox the text is escaped, so a tag is literal
        characters. Refusing it there would block a body that says "use <b> for
        bold" -- prose, not markup."""
        data = {"title": "T", "mail_sub": "Hi", "mail_body": "<script>x</script>",
                "var_list_raw": ""}
        assert CampaignForm(data=data).is_valid()
        assert not CampaignForm(data={**data, "is_html": "on"}).is_valid()


# --- what a real signature needs -------------------------------------------


class TestARealSignatureIsAccepted:
    """The gate is worthless if it also refuses the thing people actually write."""

    SIGNATURE = (
        '<div style="font-family: Arial, sans-serif; font-size:13px; color:#333">\n'
        "  <strong>Anushreya</strong><br>\n"
        "  <span style=\"color:#555\">Partnership Associate</span><br>\n"
        '  <a href="mailto:f20251539@pilani.bits-pilani.ac.in">University</a>\n'
        '  | <a href="https://www.linkedin.com/in/anushreya/">LinkedIn</a>\n'
        '  | <a href="tel:+918822818959">Phone</a><br>\n'
        "  <!-- colour bar -->\n"
        '  <table cellpadding="0" cellspacing="0" border="0" style="width:230px">\n'
        '    <tr><td style="background-color:#f7a01d"></td></tr>\n'
        "  </table>\n"
        '  <img src="https://pieds.in/logo.png" alt="PIEDS" width="120">\n'
        "  <span>Birla Institute of Technology &amp; Science, Pilani</span>\n"
        "</div>"
    )

    def test_no_part_of_it_is_refused(self):
        assert rt.validate_markup(self.SIGNATURE) == []

    def test_mailto_and_tel_are_allowed_in_raw_html(self):
        """Wider than the markdown syntax on purpose. A pasted signature is
        exactly where these live, and refusing them rejects nearly all of them."""
        assert rt.validate_markup('<a href="mailto:x@y.com">m</a>') == []
        assert rt.validate_markup('<a href="tel:+919999999999">t</a>') == []

    def test_a_hash_href_is_left_alone(self):
        """The conventional "goes nowhere on purpose" placeholder."""
        assert rt.validate_markup('<a href="#">x</a>') == []

    def test_a_colon_in_a_query_string_is_not_a_scheme(self):
        assert rt.validate_markup('<img src="https://x.test/l.png?w=1:2">') == []


class TestImages:
    """"Embeddings": the two ways a logo legitimately reaches an inbox."""

    def test_an_https_image_is_allowed(self):
        assert rt.validate_markup('<img src="https://pieds.in/logo.png">') == []

    def test_an_embedded_image_is_allowed(self):
        """data:image/ cannot carry script; every other data: type can, which is
        why the prefix is checked and not merely the scheme."""
        assert rt.validate_markup('<img src="data:image/png;base64,iVBORw0K">') == []
        assert rt.validate_markup('<img src="data:image/gif;base64,R0lGOD">') == []

    def test_a_cid_image_is_allowed(self):
        assert rt.validate_markup('<img src="cid:logo123">') == []

    def test_a_relative_image_is_refused_with_the_reason(self):
        """The commonest way a signature loses its logo: `logo.png` works in the
        editor it was copied from and resolves for nobody in a mail."""
        problems = rt.validate_markup('<img src="logo.png">')
        assert problems and "relative" in problems[0]

    def test_an_image_survives_to_the_recipient(self, root, contact, kabir):
        sub = campaign_svc.sub_campaign_for(root, kabir)
        sub.footer = '<img src="https://pieds.in/logo.png" alt="PIEDS">'
        sub.footer_is_html = True
        sub.save()

        assert '<img src="https://pieds.in/logo.png"' in render(sub, contact).body_html


# --- the plain-text alternative --------------------------------------------


class TestThePlainTextHalf:
    """Half the promise of multipart/alternative, and the half nobody looks at."""

    def test_an_anchor_keeps_its_url(self):
        assert rt.to_plain('<a href="https://pieds.in">PIEDS</a>', raw=True) == (
            "PIEDS (https://pieds.in)"
        )

    def test_a_mailto_shows_the_address_not_the_scheme(self):
        assert rt.to_plain('<a href="mailto:a@b.com">Mail</a>', raw=True) == (
            "Mail (a@b.com)"
        )

    def test_a_self_describing_link_is_not_repeated(self):
        """"https://pieds.in (https://pieds.in)" reads as a bug."""
        assert rt.to_plain('<a href="https://pieds.in">https://pieds.in</a>',
                           raw=True) == "https://pieds.in"

    def test_single_quoted_and_unquoted_hrefs_are_handled(self):
        assert "https://a.test" in rt.to_plain("<a href='https://a.test'>x</a>", raw=True)
        assert "https://b.test" in rt.to_plain("<a href=https://b.test>x</a>", raw=True)

    def test_a_signature_does_not_arrive_as_a_column_of_blank_lines(self):
        """One stripped block element leaves one blank line. A dozen of them
        leaves the name at the bottom of a screenful of nothing."""
        markup = "<div><p>a</p></div>\n\n\n\n<div><p>b</p></div>"
        assert "\n\n\n" not in rt.to_plain(markup, raw=True)

    def test_the_non_raw_path_is_untouched(self):
        """Plain bodies are not markup; `3 < 5` is arithmetic, not a tag."""
        assert rt.to_plain("3 < 5 &amp; true") == "3 < 5 &amp; true"


# --- seeing it before sending it -------------------------------------------


class TestTheFooterPreview:
    def test_preview_renders_without_saving(self, client, root, kabir):
        """The button that did not exist. Finding out what a tag did used to
        mean saving it and sending with it."""
        sign_in(client, kabir)
        response = client.post(f"/campaigns/{root.id}/footer/", {
            "footer": "<b>Kabir</b>", "footer_is_html": "on", "action": "preview",
        })

        assert response.status_code == 200, "preview must not redirect"
        assert Campaign.objects.get(parent=root, owner=kabir).footer == "", \
            "preview wrote to the database"

        # The srcdoc attribute carries a whole document, so its markup is
        # escaped exactly once on the way in. Escaped TWICE is the signature of
        # the bug this screen is about: it means the footer went through the
        # plain-text path and `<b>` will reach the inbox as visible characters.
        body = response.content.decode()
        assert "&lt;b&gt;Kabir&lt;/b&gt;" in body
        assert "&amp;lt;b&amp;gt;" not in body, "the footer was escaped as plain text"

    def test_preview_still_reports_errors(self, client, root, kabir):
        sign_in(client, kabir)
        response = client.post(f"/campaigns/{root.id}/footer/", {
            "footer": "<iframe></iframe>", "footer_is_html": "on",
            "action": "preview",
        })
        assert "iframe" in response.content.decode()

    def test_a_member_with_no_contacts_still_gets_a_preview(self, client, root, kabir):
        """Which is precisely the member who has just joined and is setting
        their signature up. This screen used to show them nothing at all."""
        assert not Contact.objects.exists()
        sign_in(client, kabir)

        body = client.get(f"/campaigns/{root.id}/footer/").content.decode()
        assert "Sample data" in body
        assert "srcdoc" in body

    def test_a_real_contact_is_preferred_over_the_stand_in(
        self, client, root, contact, kabir
    ):
        sign_in(client, kabir)
        body = client.get(f"/campaigns/{root.id}/footer/").content.decode()
        assert "Rohan Mehta" in body and "Sample data" not in body

    def test_the_stand_in_is_never_written_to_the_pool(self, client, root, kabir):
        """A fictional prospect created to power a preview is the kind of test
        data still sitting in the contact list a year later."""
        sign_in(client, kabir)
        client.get(f"/campaigns/{root.id}/footer/")
        assert Contact.objects.count() == 0

    def test_the_html_checkbox_is_on_a_members_form(self, client, root, kabir):
        """The whole defect, at the level it was actually visible."""
        sign_in(client, kabir)
        assert "footer_is_html" in client.get(
            f"/campaigns/{root.id}/footer/"
        ).content.decode()


class TestTheCampaignPreview:
    def test_preview_renders_without_saving(self, client, lead):
        sign_in(client, lead)
        response = client.post("/campaigns/new/", {
            "title": "Draft", "mail_sub": "Hi {{ first_name }}",
            "mail_body": "<p>Hi {{ first_name }}</p>", "var_list_raw": "first_name",
            "is_html": "on", "action": "preview",
        })

        assert response.status_code == 200, "preview must not redirect"
        assert not Campaign.objects.filter(title="Draft").exists()
        assert "Preview" in response.content.decode()

    def test_saving_still_saves(self, client, lead):
        sign_in(client, lead)
        response = client.post("/campaigns/new/", {
            "title": "Real", "mail_sub": "Hi {{ first_name }}",
            "mail_body": "Hi {{ first_name }}", "var_list_raw": "first_name",
        })

        assert response.status_code == 302
        assert Campaign.objects.filter(title="Real").exists()

    def test_the_preview_shows_the_unsaved_edit(self, client, lead, root, contact):
        """Previewing what is stored rather than what was typed would make the
        button useless for the one job it has."""
        sign_in(client, lead)
        response = client.post(f"/campaigns/{root.id}/edit/", {
            "title": root.title, "mail_sub": "Hello {{ first_name }}",
            "mail_body": "A COMPLETELY NEW BODY", "var_list_raw": "first_name",
            "action": "preview",
        })

        assert "A COMPLETELY NEW BODY" in response.content.decode()
        root.refresh_from_db()
        assert root.mail_body == "We run an incubator at BITS."


# --- the form collects every problem ---------------------------------------


def test_a_footer_reports_all_its_bad_links_at_once(root, lead):
    """`raise` inside the loop reported one per save, so a signature with three
    took three round trips -- each of which looked like a fresh failure."""
    sub = campaign_svc.sub_campaign_for(root, lead)
    form = FooterForm(
        data={"footer": "[a](htp://x) and [b](ftp://y)"}, instance=sub
    )

    assert not form.is_valid()
    assert len(form.errors["footer"]) == 2


# --- forgetting to tick the box --------------------------------------------


class TestTheForgottenCheckbox:
    """The defect will happen again the first time somebody simply forgets.

    The checkbox being present is only half a fix: it still has to be ticked,
    and a footer full of tags with it unticked produces exactly the mail this
    whole change exists to stop.
    """

    def test_a_pasted_signature_without_the_box_is_refused(self, root, kabir):
        sub = campaign_svc.sub_campaign_for(root, kabir)
        form = FooterForm(
            data={"footer": "<strong>Kabir</strong><br>PIEDS"}, instance=sub
        )

        assert not form.is_valid()
        # On the checkbox, not the textarea: the checkbox is where the fix is.
        assert "footer_is_html" in form.errors
        assert "visible text" in form.errors["footer_is_html"][0]

    def test_ticking_the_box_makes_it_save(self, root, kabir):
        sub = campaign_svc.sub_campaign_for(root, kabir)
        form = FooterForm(
            data={"footer": "<strong>Kabir</strong>", "footer_is_html": "on"},
            instance=sub,
        )
        assert form.is_valid(), form.errors

    def test_ordinary_prose_is_not_mistaken_for_markup(self, root, kabir):
        """"under <5 lakh" is arithmetic and an address in angle brackets is an
        address. Checking for real tag names rather than a bare `<` is what
        keeps this guard from blocking plain-text footers."""
        sub = campaign_svc.sub_campaign_for(root, kabir)
        for prose in ("Regards,\nKabir Rao\nPIEDS, BITS Pilani",
                      "Cheques under <5 lakh only",
                      "reach me at <kabir at pieds dot in>",
                      "3 < 5 and 5 > 3"):
            form = FooterForm(data={"footer": prose}, instance=sub)
            assert form.is_valid(), f"{prose!r} was refused: {form.errors}"

    def test_looks_like_html_is_about_tags_not_angle_brackets(self):
        assert rt.looks_like_html("<span>x</span>")
        assert rt.looks_like_html("<TABLE><TR><TD>x</TD></TR></TABLE>")
        assert not rt.looks_like_html("under <5 lakh")
        assert not rt.looks_like_html("Regards, Kabir")
        assert not rt.looks_like_html("")
