"""Template-level footguns that render as text instead of failing.

Django's `{# ... #}` comment is SINGLE-LINE. A `{#` whose `#}` is on a later
line is not a comment at all -- the template engine emits the whole block
verbatim, so developer commentary is rendered into the page and shown to users.
It raises nothing, logs nothing, and the tests pass, because the page is still
valid HTML with some extra prose in it.

This repository's docstrings and comments are its design record, so its
templates carry a lot of explanation, and fifteen of those blocks had silently
become page copy. One of them put "A checkbox reads as a checkbox: control
first, label beside it." above the tick box on the footer screen.

`{% comment %} ... {% endcomment %}` is the multi-line form and is safe.
"""

import pathlib

import pytest
from django.conf import settings
from django.template.loader import get_template

TEMPLATE_ROOT = pathlib.Path(settings.BASE_DIR) / "crm" / "templates"


def templates():
    return sorted(TEMPLATE_ROOT.rglob("*.html"))


def test_there_are_templates_to_check():
    """Guards the guard: a bad root would make every test below vacuous."""
    assert len(templates()) > 5, TEMPLATE_ROOT


@pytest.mark.parametrize(
    "path", templates(), ids=lambda p: p.name
)
def test_no_multi_line_hash_comment(path):
    """Each `{#` must be closed on its own line, or it is not a comment."""
    offenders = [
        (i, line.strip())
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if "{#" in line and "#}" not in line
    ]
    assert not offenders, (
        f"{path.name}: {{# #}} is single-line only, so these render as visible "
        f"text. Use {{% comment %}} ... {{% endcomment %}}: {offenders}"
    )


@pytest.mark.parametrize("path", templates(), ids=lambda p: p.name)
def test_comment_blocks_are_closed(path):
    source = path.read_text()
    assert source.count("{% comment %}") == source.count("{% endcomment %}"), path.name


@pytest.mark.parametrize("path", templates(), ids=lambda p: p.name)
def test_every_template_still_loads(path):
    """A syntax error in a template is only found when someone opens the page."""
    get_template(str(path.relative_to(TEMPLATE_ROOT)))
