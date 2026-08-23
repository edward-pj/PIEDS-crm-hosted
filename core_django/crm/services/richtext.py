"""The one place that understands campaign link syntax.

A lead writes `[book a call](https://cal.com/x)` in the plain body textarea and
the recipient gets clickable anchor text. Everything about that syntax lives
here -- the form's validation and the send-time conversion both import from
this module, exactly as they both import PLACEHOLDER_RE from campaigns.py, so
"what the form accepted" and "what went out" can never drift apart.

Why a markdown subset rather than a rich-text editor: in the default mode we
generate the HTML ourselves from a syntax we control, so there is nothing
untrusted to sanitise and no sanitiser dependency to keep current. That rests
entirely on to_html() escaping every piece it did not itself write. Do not
weaken it.

RAW MODE (`raw=True`, driven by the campaign's `is_html` checkbox, or a
sub-campaign's `footer_is_html`) deliberately suspends that: the text is passed
through untouched so a lead can write a divider or inline styling. The trust
boundary moves rather than disappearing -- campaign editing is @lead_required
and `footer_is_html` is removed from the form for non-leads for exactly this
reason (see forms.py::FooterForm; a member's footer is always escaped). The only
place the CRM
renders this HTML is the campaign_detail preview, inside a `sandbox=""` iframe
that cannot run script. Mail clients strip script themselves. CampaignForm
additionally refuses `<script>` and inline event handlers, so the sandboxed
preview cannot quietly disagree with what an inbox will do.
"""

import html
import re

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.utils.html import escape, strip_tags

#: `[label](url)`. The label forbids brackets so nesting cannot be attempted,
#: and the URL forbids whitespace so an unclosed paren fails to match rather
#: than swallowing the rest of the mail.
LINK_RE = re.compile(r"\[([^\[\]]+)\]\(\s*(\S+?)\s*\)")

#: mailto: is deliberately absent. Only schemes a mail client will open as a
#: web page are allowed; javascript: and data: are the reason this list exists.
ALLOWED_SCHEMES = ("http", "https")

_validate_url = URLValidator(schemes=list(ALLOWED_SCHEMES))

#: Inlined because mail clients strip <style> blocks. Kept deliberately plain --
#: this is a personal-looking outreach mail, not a newsletter.
_BODY_STYLE = (
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,"
    "Arial,sans-serif;font-size:14px;line-height:1.5;color:#111"
)


def extract_links(text: str) -> list[tuple[str, str]]:
    """Every (label, url) pair in the text, in order."""
    return LINK_RE.findall(text or "")


def validate_links(text: str) -> list[str]:
    """Problems with the links in `text`, as messages fit to show a user.

    Empty list means every link is sendable.
    """
    problems: list[str] = []

    for label, url in extract_links(text):
        if not label.strip():
            problems.append(f"A link has no text to click: [{label}]({url})")

        # Checked before URLValidator so `javascript:alert(1)` reports what is
        # actually wrong with it rather than a generic "enter a valid URL".
        scheme = url.split(":", 1)[0].lower() if ":" in url else ""
        if scheme and scheme not in ALLOWED_SCHEMES:
            problems.append(
                f"{url!r} uses the {scheme}: scheme. Links must start with http:// or https://."
            )
            continue

        # A placeholder standing in for the whole URL is resolved per contact at
        # send time, so there is nothing to validate here yet. render() escapes
        # whatever it substitutes, and a contact field is never a URL scheme we
        # allow, so the worst case is a dead link, not an unsafe one.
        if "{{" in url:
            continue

        try:
            _validate_url(url)
        except ValidationError:
            problems.append(f"{url!r} is not a valid URL.")

    return problems


#: Script and inline event handlers. A mail client strips both, so they can only
#: ever mislead: the campaign preview would show behaviour no recipient gets.
_SCRIPT_RE = re.compile(r"<\s*script\b", re.I)
_HANDLER_RE = re.compile(r"<[^>]*?\son[a-z]+\s*=", re.I | re.S)


def validate_markup(text: str) -> list[str]:
    """Problems with raw HTML in a body. Only consulted when `is_html` is on."""
    problems = []
    if _SCRIPT_RE.search(text or ""):
        problems.append(
            "<script> is not allowed: every mail client strips it, so it would "
            "only make the preview lie about what recipients see."
        )
    if _HANDLER_RE.search(text or ""):
        problems.append(
            "Inline event handlers (onclick=, onload=, ...) are not allowed, "
            "for the same reason as <script>."
        )
    return problems


def to_plain(text: str, raw: bool = False) -> str:
    """`[book a call](https://x)` -> `book a call (https://x)`.

    The text/plain alternative must not be lossy: a client that refuses HTML
    still has to be able to reach the link, so the URL stays visible.

    In raw mode the body is markup, so the tags come out and their entities go
    back to being characters -- a fallback full of `<td>` and `&amp;` reads
    worse than no fallback at all.
    """
    plain = LINK_RE.sub(lambda m: f"{m.group(1).strip()} ({m.group(2)})", text or "")
    if raw:
        plain = html.unescape(strip_tags(plain))
    return plain


def to_fragment(text: str, raw: bool = False) -> str:
    """Convert one piece of body text to an HTML *fragment* -- no document.

    Split out of `to_html` so a body and a footer can be converted SEPARATELY,
    each with its own `raw` flag, and only then joined. Converting them together
    is not an option: a plain-text body would escape a raw-HTML footer into
    visible tags. Joining two `to_html` results is not an option either -- each
    is a complete `<!doctype html>...</html>` document, so the result would be
    one document nested inside another.

    Default mode: every run of ordinary text, every link label, and every href
    is escaped before it is interpolated, and newlines become <br>. That is what
    makes the output safe to preview and to hand to Gmail without a sanitiser.

    Raw mode: ordinary text passes through as the markup it is, and newlines are
    left alone -- an author writing <p> and <br> themselves does not want a
    second set inserted underneath. Link syntax still works in both modes, and
    its href is still escaped either way; there is no reason to hand-write an
    anchor just because the rest of the body is HTML.

    **The newline conversion belongs in here, not in the caller.** It used to run
    over the whole joined body. Joining first and converting after would inject
    <br> into a raw-HTML footer whose author deliberately did not want them --
    which is the entire distinction `raw` exists to express.
    """
    out: list[str] = []
    cursor = 0
    text = text or ""

    def passthrough(chunk: str) -> str:
        return chunk if raw else escape(chunk)

    for match in LINK_RE.finditer(text):
        out.append(passthrough(text[cursor:match.start()]))
        label, url = match.group(1).strip(), match.group(2)
        out.append(f'<a href="{escape(url)}">{passthrough(label)}</a>')
        cursor = match.end()

    out.append(passthrough(text[cursor:]))

    fragment = "".join(out)
    if not raw:
        # Escaping first and converting newlines second: the reverse would let a
        # <br> we just inserted be escaped back into visible text.
        fragment = fragment.replace("\n", "<br>\n")
    return fragment


def wrap_document(fragment: str) -> str:
    """Wrap a fragment (or several, already joined) as a full HTML document."""
    return (
        '<!doctype html><html><body style="' + _BODY_STYLE + '">\n'
        + fragment
        + "\n</body></html>"
    )


def to_html(text: str, raw: bool = False) -> str:
    """Render one body as a full HTML document.

    Kept with its original signature AND byte-identical output: the campaign
    preview, the link tests and the header tests all pin this, and there was no
    reason to change what goes out at the same time as adding footers.
    """
    return wrap_document(to_fragment(text, raw))
