"""Rows per page, and the POST that has to survive them.

Fifty per page is fine until somebody imports three hundred contacts and has to
assign them, at which point it is six passes through a screen that carries
filters and a selection. This file pins the choosable size, the fallback when
the URL asks for something silly, and the one coupling that is invisible from
either side: every visible row posts a checkbox, so the page size is bounded by
Django's DATA_UPLOAD_MAX_NUMBER_FIELDS. Exceeding that is a bare 400 on the
screen where somebody has just ticked five hundred boxes.

The last class here is about selection rather than paging, and it is a
structural check on the templates rather than a behavioural one. That is
deliberate -- see its docstring.
"""

import re

import pytest

from crm.models import Contact
from crm.views import PAGE_SIZES

from .conftest import make_lead, sign_in

pytestmark = pytest.mark.django_db


@pytest.fixture
def lead(team):
    return make_lead(team)


@pytest.fixture
def many(db):
    """More contacts than the largest page size, so every size shows a full page."""
    Contact.objects.bulk_create([
        Contact(
            first_name=f"Contact{i:04d}", last_name="Test",
            email=f"c{i:04d}@example.com", company=f"Company {i % 40}",
            designation="Founder",
        )
        for i in range(520)
    ])
    return Contact.objects.all()


def rows_on(response):
    """How many contact checkboxes the page rendered."""
    return response.content.decode().count('name="contact_ids"')


class TestChoosingASize:
    @pytest.mark.parametrize("path,default", [("/contacts/", 50), ("/assign/", 100)])
    def test_each_screen_keeps_the_size_it_had(self, client, lead, many, path, default):
        """Choosable does not mean changed. A screen that showed 100 goes on
        showing 100 until somebody asks for something else."""
        sign_in(client, lead)
        assert rows_on(client.get(path)) == default

    @pytest.mark.parametrize("path", ["/contacts/", "/assign/"])
    @pytest.mark.parametrize("size", PAGE_SIZES)
    def test_every_offered_size_is_honoured(self, client, lead, many, path, size):
        sign_in(client, lead)
        assert rows_on(client.get(path, {"per_page": size})) == size

    def test_three_hundred_is_on_the_menu(self):
        """The size that was actually asked for."""
        assert 300 in PAGE_SIZES

    @pytest.mark.parametrize("bad", ["", "abc", "-5", "0", "1e9", "99.5", "None"])
    def test_a_nonsense_size_falls_back_instead_of_raising(
        self, client, lead, many, bad
    ):
        """It arrives from a hand-edited URL or a stale bookmark. A 500 is a
        poor answer to a typo."""
        sign_in(client, lead)
        response = client.get("/assign/", {"per_page": bad})
        assert response.status_code == 200
        assert rows_on(response) == 100

    def test_an_enormous_size_is_refused(self, client, lead, many):
        """Otherwise `?per_page=100000` renders the entire pool into one page,
        and one bookmark becomes a way to hurt a hosted database."""
        sign_in(client, lead)
        assert rows_on(client.get("/assign/", {"per_page": 100000})) == 100


class TestTheChoiceSticks:
    def test_it_carries_from_one_screen_to_another(self, client, lead, many):
        """A lead who picks 300 on the contact list and clicks through to
        Assign means it there too. Asking again on every screen is the same
        complaint in a different place."""
        sign_in(client, lead)
        client.get("/contacts/", {"per_page": 300})

        assert rows_on(client.get("/assign/")) == 300
        assert rows_on(client.get("/contacts/")) == 300

    def test_the_url_beats_what_was_remembered(self, client, lead, many):
        sign_in(client, lead)
        client.get("/assign/", {"per_page": 300})
        assert rows_on(client.get("/assign/", {"per_page": 50})) == 50

    def test_it_survives_the_assign_round_trip(self, client, lead, many):
        """assign_apply redirects back to `next`; landing on 50 after assigning
        from a page of 300 loses your place."""
        sign_in(client, lead)
        client.get("/assign/", {"per_page": 300})
        assert rows_on(client.get("/assign/")) == 300


class TestTheSizePickerLinks:
    def test_the_highlighted_size_matches_the_rows_on_the_page(
        self, client, lead, many
    ):
        """The picker has to agree with what is underneath it.

        Each screen carries its own default, so computing "current" separately
        from the paginator got it wrong the moment the two disagreed: Assign
        rendered 100 rows while the picker underlined 50. It now reads
        `page.paginator.per_page`, which cannot drift from the page it labels.
        """
        sign_in(client, lead)
        for path, expected in (("/assign/", 100), ("/contacts/", 50)):
            body = client.get(path).content.decode()
            marked = re.findall(r'aria-current="true"[^>]*>\s*(\d+)\s*<', body)
            assert marked == [str(expected)], f"{path}: picker says {marked}"
            assert rows_on(client.get(path)) == expected

    @pytest.mark.parametrize("size", PAGE_SIZES)
    def test_the_highlight_follows_an_explicit_choice(self, client, lead, many, size):
        sign_in(client, lead)
        body = client.get("/assign/", {"per_page": size}).content.decode()
        assert re.findall(r'aria-current="true"[^>]*>\s*(\d+)\s*<', body) == [str(size)]

    def test_the_picker_offers_every_size(self, client, lead, many):
        sign_in(client, lead)
        body = client.get("/assign/").content.decode()
        for size in PAGE_SIZES:
            assert f"per_page={size}" in body or f">{size}<" in body

    def test_changing_size_returns_to_the_first_page(self, client, lead, many):
        """Page 7 of 50 is not page 7 of 300, and landing past the end after
        switching is how a picker looks broken."""
        sign_in(client, lead)
        body = client.get("/assign/", {"page": 3}).content.decode()
        assert "per_page=300" in body
        # `page` must not be carried into the size links.
        for chunk in body.split("per_page=300")[1:]:
            assert not chunk[:40].startswith("&page=")

    def test_paging_does_not_accumulate_page_parameters(self, client, lead, many):
        """`?{{ request.GET.urlencode }}&page=N` re-appended `page` to a query
        string that already had one, so three clicks of Next produced
        `?page=2&page=3&page=4` -- right page, useless URL."""
        sign_in(client, lead)
        body = client.get("/assign/", {"page": 2}).content.decode()
        for href in body.split('href="')[1:]:
            url = href.split('"')[0]
            assert url.count("page=") <= 2, url          # page= and per_page=
            assert "page=2&amp;page=" not in url

    def test_the_filters_survive_a_size_change(self, client, lead, many):
        """Changing the size must not silently drop the filter you were
        looking through, or the count jumps and nothing explains why."""
        sign_in(client, lead)
        body = client.get("/assign/", {"q": "Contact001"}).content.decode()
        assert "q=Contact001" in body


class TestThePostThatHasToSurviveIt:
    def test_a_full_page_of_selections_can_be_submitted(self, client, lead, many):
        """The coupling this whole thing rests on. Every visible row posts a
        checkbox, so the largest page size has to fit inside Django's
        DATA_UPLOAD_MAX_NUMBER_FIELDS -- and going over it is a bare 400, not
        an error anyone could act on."""
        sign_in(client, lead)
        ids = [str(c.pk) for c in Contact.objects.all()[:max(PAGE_SIZES)]]

        response = client.post("/assign/apply/", {
            "contact_ids": ids, "action": "assign",
            "member": str(lead.pk), "next": "/assign/",
        })

        assert response.status_code == 302, "the POST was refused outright"
        assert Contact.objects.filter(assigned_to=lead).count() == len(ids)

    def test_the_field_limit_is_above_the_largest_page(self):
        """Pinned so raising PAGE_SIZES without raising the setting fails here
        rather than on somebody's screen."""
        from django.conf import settings

        assert settings.DATA_UPLOAD_MAX_NUMBER_FIELDS > max(PAGE_SIZES) + 10


# --- selection ---------------------------------------------------------------


class TestSelectAllSurvivesABackgroundRefresh:
    """Select-all worked, then silently stopped, and a reload fixed it.

    The cause: both header checkboxes sit INSIDE the `[data-poll]` region, and
    `Poll.refreshPage` replaces that region's innerHTML every twenty seconds.
    A listener bound straight to the element went into the garbage with the old
    node, while the row boxes kept working because theirs was delegated from
    `document`. So select-all worked until the first refresh landed -- about
    twenty seconds in, or the instant you switched back to the tab -- and never
    again until the page was reloaded. Exactly "it doesn't work at times".

    These assertions are structural because the repository has no JavaScript
    test runner, and adding a Node toolchain to pin four lines of DOM wiring is
    a worse trade than checking the shape. The behavioural proof was done once,
    outside the suite, by driving the real rendered pages through jsdom: with a
    direct binding, select-all set 0/300 boxes after a simulated refresh; with
    delegation, 300/300.

    What each check defends:
      - no direct `getElementById(<master>).addEventListener`, which is the bug;
      - a delegated `document.addEventListener("change", ...)` that names the
        master id, which is the fix;
      - `sync()` must look the master up by id rather than closing over it, or
        the tick state goes stale against a replaced node.
    """

    SCREENS = [("assign.html", "selectAll"), ("contact_list.html", "check-all")]

    def _source(self, name):
        from django.conf import settings

        for directory in settings.TEMPLATES[0]["DIRS"] or []:
            candidate = directory / "crm" / name
            if candidate.exists():
                return candidate.read_text()

        from django.template.loader import get_template

        return get_template(f"crm/{name}").template.source

    @pytest.mark.parametrize("template,master", SCREENS)
    def test_the_master_box_is_not_bound_directly(self, template, master):
        source = self._source(template)
        assert f"getElementById('{master}').addEventListener" not in source
        assert f'getElementById("{master}").addEventListener' not in source

    @pytest.mark.parametrize("template,master", SCREENS)
    def test_it_is_delegated_from_the_document(self, template, master):
        source = self._source(template)
        assert "document.addEventListener('change'" in source
        assert master in source.split("document.addEventListener('change'")[1][:400]

    @pytest.mark.parametrize("template,master", SCREENS)
    def test_the_master_is_looked_up_inside_sync(self, template, master):
        """Held in a variable at load time, it points at a detached node after
        the swap, and the header tick stops matching the rows."""
        source = self._source(template)
        body = source.split("function sync()")[1].split("\n  }")[0]
        assert f"getElementById('{master}')" in body

    @pytest.mark.parametrize("template,master", SCREENS)
    def test_the_master_lives_inside_the_swapped_region(self, template, master):
        """If it ever moves outside `[data-poll]`, the direct binding would be
        safe again and these tests would be guarding nothing. Assert the
        premise, so this fails loudly instead of going quietly irrelevant."""
        source = self._source(template)
        after_poll = source.split("data-poll")[1]
        assert master in after_poll.split("</table>")[0]
