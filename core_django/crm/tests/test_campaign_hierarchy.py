"""The duplicate-mail bug, and the hierarchy that fixes it.

The bug this file exists for: every member had their own top-level campaign so
they could have their own footer, and `uniq_campaign_contact` is scoped to ONE
campaign. Once Aarav had mailed a company under his, Kabir's campaign held no
row for that contact, so he could mail the same prospect again under a different
banner. `test_constraints.py::test_same_contact_in_a_different_campaign_is_allowed`
asserts that this is still true of two unrelated ROOTS -- which it should be.
What must not be true is that it holds between two members of one campaign.

If `test_two_members_cannot_both_mail_one_contact` ever fails, two cold mails
reach one prospect and everything else in this repo is negotiable.
"""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from crm.models import Campaign, CampaignMailing, Contact, FollowUpRule, TeamMember
from crm.services import campaigns as campaign_svc
from crm.services import mailing
from crm.services.render import render
from shared.enums import CampaignStatus, MailingStatus

from .conftest import make_lead

pytestmark = pytest.mark.django_db


@pytest.fixture
def lead(team):
    return make_lead(team)


@pytest.fixture
def kabir():
    return TeamMember.objects.create(
        name="Kabir", bits_email="kabir@pilani.bits-pilani.ac.in", batch="2025"
    )


@pytest.fixture
def ishita():
    return TeamMember.objects.create(
        name="Ishita", bits_email="ishita@pilani.bits-pilani.ac.in", batch="2025"
    )


@pytest.fixture
def root(lead):
    return Campaign.objects.create(
        title="Ignite", mail_sub="Hello {{ first_name }}",
        mail_body="We run an incubator at {{ company }}.",
        var_list=["first_name", "company"],
        status=CampaignStatus.ACTIVE.value, created_by=lead,
    )


@pytest.fixture
def contact():
    return Contact.objects.create(
        first_name="Rohan", last_name="Iyer", email="rohan@example.com",
        company="Zerodha",
    )


# --- the guarantee ---------------------------------------------------------


class TestTheDuplicateMailBug:
    def test_two_members_cannot_both_mail_one_contact(
        self, root, contact, kabir, ishita
    ):
        """THE test. Two sub-campaigns, one root, one prospect, one mail."""
        kabir_c = campaign_svc.sub_campaign_for(root, kabir)
        ishita_c = campaign_svc.sub_campaign_for(root, ishita)

        CampaignMailing.objects.create(
            campaign=kabir_c, contact=contact, sent_by=kabir,
            status=MailingStatus.SENT.value,
        )

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                CampaignMailing.objects.create(
                    campaign=ishita_c, contact=contact, sent_by=ishita,
                    status=MailingStatus.DRAFT.value,
                )

        assert CampaignMailing.objects.filter(contact=contact).count() == 1

    def test_the_second_member_is_told_who_reached_them(
        self, root, contact, kabir, ishita
    ):
        """Refused at the claim, with the teammate named -- not an IntegrityError
        500, and not a bare 'already has a mailing' that reads as their own."""
        contact.assigned_to = kabir
        contact.save()
        kabir_c = campaign_svc.sub_campaign_for(root, kabir)
        mailing.claim_batch(kabir_c, kabir, [contact.id])

        contact.assigned_to = ishita
        contact.save()
        ishita_c = campaign_svc.sub_campaign_for(root, ishita)
        claimed, skipped = mailing.claim_batch(ishita_c, ishita, [contact.id])

        assert claimed == []
        assert skipped[0].code == mailing.ALREADY_MAILED
        assert "Aarav" not in skipped[0].reason
        assert "Kabir" in skipped[0].reason

    def test_the_dry_run_agrees_with_the_send(self, root, contact, kabir, ishita):
        """A preflight that says OK for a contact the send will refuse is worse
        than no preflight."""
        contact.assigned_to = kabir
        contact.save()
        mailing.claim_batch(campaign_svc.sub_campaign_for(root, kabir), kabir,
                            [contact.id])

        contact.assigned_to = ishita
        contact.save()
        rows = mailing.preflight(
            campaign_svc.sub_campaign_for(root, ishita), ishita, [contact.id]
        )
        assert rows[0]["status"] == mailing.ALREADY_MAILED

    def test_two_unrelated_roots_may_still_reach_one_contact(
        self, root, contact, kabir, lead
    ):
        """Not a regression -- this is what makes follow-ups possible at all."""
        other_root = Campaign.objects.create(
            title="Ignite follow-up", mail_sub="Following up", mail_body="Hello",
            status=CampaignStatus.ACTIVE.value, created_by=lead,
        )
        CampaignMailing.objects.create(campaign=root, contact=contact, sent_by=kabir)
        CampaignMailing.objects.create(
            campaign=other_root, contact=contact, sent_by=kabir
        )
        assert CampaignMailing.objects.filter(contact=contact).count() == 2

    def test_a_failed_mailing_is_taken_over_rather_than_blocking_the_team(
        self, root, contact, kabir, ishita
    ):
        """Otherwise one member's transient Gmail error silently removes a
        prospect from the team's reachable pool until that member retries."""
        contact.assigned_to = kabir
        contact.save()
        claimed, _ = mailing.claim_batch(
            campaign_svc.sub_campaign_for(root, kabir), kabir, [contact.id]
        )
        mailing.record_result(claimed[0].mailing_id, kabir, status="failed",
                              error="quota exceeded")

        contact.assigned_to = ishita
        contact.save()
        ishita_c = campaign_svc.sub_campaign_for(root, ishita)
        claimed, skipped = mailing.claim_batch(ishita_c, ishita, [contact.id])

        assert skipped == []
        assert len(claimed) == 1
        row = CampaignMailing.objects.get(contact=contact)
        # Rewritten to Ishita, campaign included -- so it goes out with HER
        # footer, not the footer of the person whose attempt failed.
        assert row.sent_by_id == ishita.id
        assert row.campaign_id == ishita_c.id
        assert CampaignMailing.objects.count() == 1


class TestRootDerivation:
    def test_a_root_campaign_is_its_own_root(self, root, contact, kabir):
        m = CampaignMailing.objects.create(
            campaign=root, contact=contact, sent_by=kabir
        )
        assert m.root_campaign_id == root.id

    def test_a_sub_campaigns_mailing_roots_to_the_parent(
        self, root, contact, kabir
    ):
        sub = campaign_svc.sub_campaign_for(root, kabir)
        m = CampaignMailing.objects.create(
            campaign=sub, contact=contact, sent_by=kabir
        )
        assert m.root_campaign_id == root.id

    def test_no_caller_has_to_pass_it(self, root, contact, kabir):
        """Derived in save(). Leaving it to call sites would not fail loudly --
        it would insert a NULL root, which the unique index ignores."""
        sub = campaign_svc.sub_campaign_for(root, kabir)
        m = CampaignMailing(campaign=sub, contact=contact, sent_by=kabir)
        m.save()
        assert m.root_campaign_id == root.id


# --- shape rules -----------------------------------------------------------


class TestHierarchyShape:
    def test_one_sub_campaign_per_member_per_root(self, root, kabir):
        first = campaign_svc.sub_campaign_for(root, kabir)
        second = campaign_svc.sub_campaign_for(root, kabir)
        assert first.id == second.id

    def test_a_duplicate_sub_campaign_is_refused_by_the_database(self, root, kabir):
        campaign_svc.sub_campaign_for(root, kabir)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Campaign.objects.create(
                    title="sneaky", mail_sub="", mail_body="",
                    parent=root, owner=kabir,
                )

    def test_a_root_may_not_have_an_owner(self, root, kabir, lead):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Campaign.objects.create(
                    title="confused", mail_sub="x", mail_body="y",
                    parent=None, owner=kabir, created_by=lead,
                )

    def test_three_levels_are_refused(self, root, kabir, ishita):
        sub = campaign_svc.sub_campaign_for(root, kabir)
        grandchild = Campaign.objects.create(
            title="deeper", mail_sub="", mail_body="",
        )
        with pytest.raises(ValidationError, match="two levels deep"):
            campaign_svc.set_parent(grandchild, sub, owner=ishita)


class TestReparenting:
    def test_a_clean_move_is_allowed(self, root, lead):
        loose = Campaign.objects.create(
            title="Loose", mail_sub="s", mail_body="b", created_by=lead
        )
        campaign_svc.set_parent(loose, root, owner=lead)
        loose.refresh_from_db()
        assert loose.parent_id == root.id
        assert loose.owner_id == lead.id

    def test_a_parent_without_an_owner_is_refused_readably(self, root, lead):
        """The check constraint would refuse it anyway; this turns an
        IntegrityError into a sentence."""
        loose = Campaign.objects.create(
            title="Loose", mail_sub="s", mail_body="b", created_by=lead
        )
        with pytest.raises(ValidationError, match="belongs to one member"):
            campaign_svc.set_parent(loose, root)

    def test_a_move_that_would_collide_is_refused_with_the_contacts_named(
        self, root, contact, kabir, lead
    ):
        """The migrations are provably safe only because they create no
        hierarchy. Anything that creates hierarchy afterwards has to check --
        the constraint fires on the NEXT insert, long after the damage."""
        CampaignMailing.objects.create(campaign=root, contact=contact, sent_by=kabir)

        other = Campaign.objects.create(
            title="Other", mail_sub="s", mail_body="b", created_by=lead
        )
        CampaignMailing.objects.create(campaign=other, contact=contact, sent_by=kabir)

        with pytest.raises(campaign_svc.ReparentCollision, match="rohan@example.com"):
            campaign_svc.set_parent(other, root, owner=lead)


# --- rendering -------------------------------------------------------------


class TestFooters:
    def test_a_sub_campaign_sends_the_roots_message(self, root, contact, kabir):
        sub = campaign_svc.sub_campaign_for(root, kabir)
        out = render(sub, contact)
        assert out.subject == "Hello Rohan"
        assert "We run an incubator at Zerodha." in out.body

    def test_each_member_gets_their_own_footer(self, root, contact, kabir, ishita):
        a = campaign_svc.sub_campaign_for(root, kabir)
        a.footer = "Kabir Rao\nPIEDS"
        a.save()
        b = campaign_svc.sub_campaign_for(root, ishita)
        b.footer = "Ishita Nair\nPIEDS"
        b.save()

        assert "Kabir Rao" in render(a, contact).body
        assert "Ishita Nair" not in render(a, contact).body
        assert "Ishita Nair" in render(b, contact).body

    def test_the_footer_reaches_both_the_plain_and_html_parts(
        self, root, contact, kabir
    ):
        """Most recipients see the HTML; a client that refuses it sees the
        plain. A footer in only one of them is a footer half the world misses."""
        sub = campaign_svc.sub_campaign_for(root, kabir)
        sub.footer = "Kabir Rao"
        sub.save()

        out = render(sub, contact)
        assert "Kabir Rao" in out.body
        assert "Kabir Rao" in out.body_html

    def test_an_empty_footer_adds_no_dangling_separator(self, root, contact, kabir):
        sub = campaign_svc.sub_campaign_for(root, kabir)
        out = render(sub, contact)
        assert not out.body.rstrip().endswith("--")
        assert "<hr" not in out.body_html

    def test_the_html_is_one_document_not_two(self, root, contact, kabir):
        """to_html returns a COMPLETE document, so concatenating two of them
        would nest one inside the other. That is why to_fragment exists."""
        sub = campaign_svc.sub_campaign_for(root, kabir)
        sub.footer = "Kabir Rao"
        sub.save()

        html = render(sub, contact).body_html
        assert html.count("<!doctype html>") == 1
        assert html.count("</html>") == 1
        assert html.count("<body") == 1

    def test_a_raw_html_footer_survives_a_plain_text_body(
        self, root, contact, kabir
    ):
        """The reason each part is converted with its OWN raw flag. Converting
        them together would escape this footer into visible tags."""
        sub = campaign_svc.sub_campaign_for(root, kabir)
        sub.footer = '<b>Kabir Rao</b><br><a href="https://pieds.in">PIEDS</a>'
        sub.footer_is_html = True
        sub.save()

        html = render(sub, contact).body_html
        assert "<b>Kabir Rao</b>" in html
        assert "&lt;b&gt;" not in html
        # And the plain-text body is still escaped as usual.
        assert "We run an incubator at Zerodha." in render(sub, contact).body

    def test_a_plain_footer_is_escaped(self, root, contact, kabir):
        sub = campaign_svc.sub_campaign_for(root, kabir)
        sub.footer = "Kabir <script>alert(1)</script>"
        sub.save()

        html = render(sub, contact).body_html
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_a_raw_footer_keeps_its_own_line_breaks(self, root, contact, kabir):
        """The newline conversion moved INSIDE to_fragment for this. Running it
        over the joined text would inject <br> into a raw-HTML footer whose
        author deliberately did not want them."""
        sub = campaign_svc.sub_campaign_for(root, kabir)
        sub.footer = "<p>Kabir</p>\n<p>PIEDS</p>"
        sub.footer_is_html = True
        sub.save()

        html = render(sub, contact).body_html
        assert "<p>Kabir</p>\n<p>PIEDS</p>" in html
        assert "<p>Kabir</p><br>" not in html

    def test_a_footer_may_not_use_placeholders(self):
        """Allowing them would force validate_template's set equality down to a
        subset check, destroying the typo detector it exists for."""
        with pytest.raises(ValidationError, match="cannot use"):
            campaign_svc.validate_footer("Regards, {{ first_name }}")

        campaign_svc.validate_footer("Regards, Kabir")   # no exception


# --- the emergency brake ---------------------------------------------------


class TestPausingTheRoot:
    def test_pausing_the_root_stops_every_sub_campaign(self, root, kabir):
        sub = campaign_svc.sub_campaign_for(root, kabir)
        root.status = CampaignStatus.PAUSED.value
        root.save()

        with pytest.raises(mailing.CampaignNotSendable, match="paused"):
            mailing.load_sendable_campaign(sub.id)

    def test_a_live_root_with_a_paused_sub_campaign_is_also_refused(
        self, root, kabir
    ):
        sub = campaign_svc.sub_campaign_for(root, kabir)
        sub.status = CampaignStatus.PAUSED.value
        sub.save()

        with pytest.raises(mailing.CampaignNotSendable):
            mailing.load_sendable_campaign(sub.id)


class TestFollowUpRules:
    def test_a_follow_up_under_the_same_root_is_refused(self, root, kabir):
        """It could never queue anything: every job would be 100% skipped by
        uniq_root_campaign_contact, silently, forever."""
        sub = campaign_svc.sub_campaign_for(root, kabir)
        rule = FollowUpRule(campaign=root, follow_up=sub, delay_days=3)
        with pytest.raises(ValidationError, match="separate root campaign"):
            rule.clean()

    def test_a_sibling_root_is_allowed(self, root, lead):
        sibling = Campaign.objects.create(
            title="Ignite follow-up", mail_sub="s", mail_body="b", created_by=lead
        )
        FollowUpRule(campaign=root, follow_up=sibling, delay_days=3).clean()


# --- the browser flow ------------------------------------------------------


class TestTheSendScreen:
    def _sign_in(self, client, member):
        from crm.services import auth as auth_svc

        session = client.session
        session[auth_svc.SESSION_KEY] = str(member.id)
        session.save()

    def test_only_root_campaigns_are_offered(self, client, root, kabir):
        """Offering sub-campaigns would invite sending under someone else's
        footer, and would list fifteen near-identical titles."""
        sub = campaign_svc.sub_campaign_for(root, kabir)
        self._sign_in(client, kabir)

        body = client.get("/send/").content.decode()
        assert str(root.id) in body
        assert str(sub.id) not in body

    def test_sending_queues_the_job_under_my_sub_campaign(
        self, client, root, contact, kabir
    ):
        from crm.models import GmailCredential, ScheduledSend
        from crm.services import secrets as token_store

        ciphertext, version = token_store.encrypt("refresh")
        GmailCredential.objects.create(
            member=kabir, google_email=kabir.bits_email,
            refresh_token_encrypted=ciphertext, key_version=version,
            granted_scopes=[
                "https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/gmail.readonly",
            ],
        )
        contact.assigned_to = kabir
        contact.save()
        self._sign_in(client, kabir)

        client.post("/send/", {
            "campaign": str(root.id),
            "contact_ids": [str(contact.id)],
            "action": "send",
        })

        job = ScheduledSend.objects.get()
        assert job.campaign.parent_id == root.id
        assert job.campaign.owner_id == kabir.id

    def test_a_teammates_contact_is_not_in_my_queue(
        self, client, root, contact, kabir, ishita
    ):
        """The assignment-time half of the guarantee the constraint enforces at
        send time. Showing it and then refusing it is the confusion the whole
        hierarchy exists to remove."""
        contact.assigned_to = ishita
        contact.save()
        mailing.claim_batch(
            campaign_svc.sub_campaign_for(root, ishita), ishita, [contact.id]
        )

        contact.assigned_to = kabir
        contact.save()
        self._sign_in(client, kabir)

        body = client.get("/send/", {"campaign": str(root.id)}).content.decode()
        assert contact.email not in body


class TestTheFooterScreen:
    def _sign_in(self, client, member):
        from crm.services import auth as auth_svc

        session = client.session
        session[auth_svc.SESSION_KEY] = str(member.id)
        session.save()

    def test_a_member_can_set_their_own_footer(self, client, root, kabir):
        self._sign_in(client, kabir)
        client.post(f"/campaigns/{root.id}/footer/", {"footer": "Kabir Rao\nPIEDS"})

        sub = Campaign.objects.get(parent=root, owner=kabir)
        assert "Kabir Rao" in sub.footer

    def test_a_member_may_set_raw_html(self, client, root, kabir):
        """The rule this replaced was lead-only, and it did not hold up.

        A member pasted the signature they use every day, the checkbox that
        would have rendered it was not on their form, nothing said so, and every
        mail they sent went out with the tags showing. The restriction blocked
        working HTML rather than unsafe HTML -- richtext.validate_markup is the
        control now, and it runs for everybody.
        """
        self._sign_in(client, kabir)
        client.post(f"/campaigns/{root.id}/footer/", {
            "footer": "<b>Kabir</b>", "footer_is_html": "on",
        })

        sub = Campaign.objects.get(parent=root, owner=kabir)
        assert sub.footer_is_html is True
        assert sub.footer == "<b>Kabir</b>"

    def test_a_members_pasted_signature_renders_instead_of_showing_its_tags(
        self, client, root, contact, kabir
    ):
        """The exact shape found in production, on a real member's footer.

        Trimmed but not simplified: the table, the inline styles, the mailto:
        and the HTML comment are all things a pasted Gmail signature contains,
        and every one of them was arriving in prospects' inboxes as text.
        """
        self._sign_in(client, kabir)
        signature = (
            '<div style="font-family: Arial, sans-serif; color: #333333;">\n'
            "  <strong>Anushreya</strong><br>\n"
            '  <a href="mailto:f20251539@pilani.bits-pilani.ac.in">University</a>\n'
            '  | <a href="https://www.linkedin.com/in/anushreya/">LinkedIn</a><br>\n'
            "  <!-- colour bar -->\n"
            '  <table cellpadding="0" cellspacing="0" border="0" style="width:230px">\n'
            '    <tr><td style="background-color:#f7a01d"></td></tr>\n'
            "  </table>\n"
            "  <span>Birla Institute of Technology &amp; Science, Pilani</span>\n"
            "</div>"
        )
        response = client.post(f"/campaigns/{root.id}/footer/", {
            "footer": signature, "footer_is_html": "on",
        })

        assert response.status_code == 302, "the signature was refused"
        sub = Campaign.objects.get(parent=root, owner=kabir)
        assert sub.footer_is_html is True

        html = render(sub, contact).body_html
        assert "<strong>Anushreya</strong>" in html
        assert "&lt;strong&gt;" not in html, "tags went out as visible text"
        assert "<table" in html and "background-color:#f7a01d" in html

    def test_the_plain_fallback_of_a_signature_keeps_its_links(
        self, root, contact, kabir
    ):
        """strip_tags alone turned `<a href="https://pieds.in">PIEDS</a>` into
        the bare word "PIEDS", so the text/plain alternative -- which exists
        precisely so a client that refuses HTML can still reach the link -- was
        the one path where the link could not be reached."""
        sub = campaign_svc.sub_campaign_for(root, kabir)
        sub.footer = (
            '<a href="https://pieds.in">PIEDS</a><br>\n'
            '<a href="mailto:you@pilani.bits-pilani.ac.in">Email</a>'
        )
        sub.footer_is_html = True
        sub.save()

        plain = render(sub, contact).body
        assert "PIEDS (https://pieds.in)" in plain
        # The scheme is machine punctuation; the address is the useful half.
        assert "Email (you@pilani.bits-pilani.ac.in)" in plain

    def test_a_dangerous_footer_is_refused_whoever_writes_it(self, client, root, kabir):
        """The gate that replaced the role check. If this can be saved, opening
        the checkbox to members was a straight downgrade."""
        self._sign_in(client, kabir)
        for bad in ('<a href="javascript:alert(1)">x</a>',
                    "<script>alert(1)</script>",
                    '<iframe src="https://evil.test"></iframe>',
                    '<div onclick="alert(1)">x</div>'):
            response = client.post(f"/campaigns/{root.id}/footer/", {
                "footer": bad, "footer_is_html": "on",
            })
            assert response.status_code == 200, f"{bad} was accepted"
            assert Campaign.objects.get(parent=root, owner=kabir).footer == ""

    def test_a_lead_may_set_raw_html(self, client, root, lead):
        self._sign_in(client, lead)
        client.post(f"/campaigns/{root.id}/footer/", {
            "footer": "<b>Aarav</b>", "footer_is_html": "on",
        })

        sub = Campaign.objects.get(parent=root, owner=lead)
        assert sub.footer_is_html is True

    def test_a_footer_with_a_placeholder_is_rejected(self, client, root, kabir):
        self._sign_in(client, kabir)
        r = client.post(f"/campaigns/{root.id}/footer/", {
            "footer": "Regards, {{ first_name }}",
        })

        assert r.status_code == 200          # redisplayed with the error
        sub = Campaign.objects.get(parent=root, owner=kabir)
        assert sub.footer == ""

    def test_it_cannot_be_pointed_at_a_sub_campaign(self, client, root, kabir):
        """Otherwise a member could reach someone else's sub-campaign by id and
        rewrite their sign-off."""
        sub = campaign_svc.sub_campaign_for(root, kabir)
        self._sign_in(client, kabir)

        assert client.get(f"/campaigns/{sub.id}/footer/").status_code == 404
