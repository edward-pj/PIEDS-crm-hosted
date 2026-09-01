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
through untouched so an author can paste the email signature they already use.

`footer_is_html` used to be lead-only, on the grounds that this module ships no
sanitiser. That rule did not survive contact with the thing it was guarding: a
member pasted a perfectly ordinary signature -- a table, some spans, a mailto:
link -- and every mail they sent went out with the tags visible as text, because
the checkbox that would have rendered it was not on their form. The restriction
did not prevent unsafe HTML; it prevented *working* HTML, and produced no error
message while doing it.

So the gate is now a gate rather than a role. `validate_markup` below is the
control, it runs for every author, and it rejects -- loudly, at the form, with a
message naming the tag -- the constructs that are actually dangerous or actually
dead in a mail client: script, inline event handlers, the framing and form tags,
`<base>`, and any `href`/`src` that is not a scheme a mail client will open.

Rejecting rather than stripping is deliberate and matches the rest of this
module: an author who is told "<iframe> is not allowed" can fix their signature,
whereas one whose markup is silently rewritten cannot tell what happened. The
two rendering contexts back the gate up rather than relying on it -- the CRM
only ever shows this HTML inside a `sandbox=""` iframe, which cannot run script
even if some construct slipped past, and mail clients strip script themselves.
That is what keeps the preview honest about what an inbox will do.
"""

import html
import re
from html.parser import HTMLParser

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

#: Attributes whose value is a URL, so raw HTML gets the same link checking the
#: markdown syntax has had since links landed. Without this, `[x](javascript:1)`
#: was refused and `<a href="javascript:1">x</a>` sailed through -- the same
#: link, written two ways, judged by two different standards.
URL_ATTRS = frozenset({"href", "src", "background", "action", "poster",
                       "formaction", "cite"})

#: Schemes a hand-written `href` may use. Wider than ALLOWED_SCHEMES on purpose.
#: The markdown syntax is our own invention and we chose to keep it to the web;
#: a pasted signature is exactly where `mailto:` and `tel:` legitimately live,
#: and refusing them would reject essentially every real signature.
HREF_SCHEMES = frozenset({"http", "https", "mailto", "tel"})

#: `src` is narrower: an image cannot usefully be a mailto:. `cid:` is how an
#: inline attachment is referenced, and `data:image/` how a small logo is
#: embedded without one -- neither can carry script. Any OTHER data: type can,
#: which is why the check below tests the prefix and not merely the scheme.
SRC_SCHEMES = frozenset({"http", "https", "cid"})

#: Attributes that point at something to display rather than somewhere to go.
_MEDIA_ATTRS = frozenset({"src", "background", "poster"})

#: Tags a mail client will drop, refuse, or sandbox. Rejected for the same
#: reason as <script>: allowing them means the preview shows something no
#: recipient gets. <base> is the one that is actively dangerous rather than
#: merely useless -- it silently repoints every other URL in the mail.
FORBIDDEN_TAGS = {
    "iframe": "no mail client renders a frame",
    "frame": "no mail client renders a frame",
    "frameset": "no mail client renders a frame",
    "object": "no mail client renders embedded objects",
    "embed": "no mail client renders embedded objects",
    "applet": "no mail client renders embedded objects",
    "form": "mail clients strip forms; a link to a real page is the way to "
            "collect a reply",
    "base": "it silently repoints every other link in the mail",
    "meta": "mail clients strip document metadata",
    "link": "mail clients strip external stylesheets; use style=\"...\" on the "
            "tag itself",
    "svg": "Gmail and Outlook both strip inline SVG; use a PNG",
    "math": "mail clients strip inline MathML",
}

#: CSS that has historically been a script vector. Mail clients strip all three;
#: they are listed so the preview cannot show behaviour an inbox will not.
_CSS_DANGER_RE = re.compile(r"expression\s*\(|-moz-binding|behaviou?r\s*:", re.I)


class _TagCollector(HTMLParser):
    """Every start tag and its attributes, in document order.

    HTMLParser rather than a regex because this feeds *rejection messages*. A
    regex that mistakes `<a href="x">` inside a code sample for a real anchor
    produces an error the author cannot act on, and one that misses a tag split
    across two lines produces silence exactly where a message was needed.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, attrs))

    #: `<img ... />` is a start tag for our purposes; without this it is missed.
    handle_startendtag = handle_starttag


def _tags(text: str) -> list[tuple[str, list[tuple[str, str | None]]]]:
    """Parse `text` as an HTML fragment. Never raises.

    A parse failure must not become a 500 inside form validation: whatever was
    recognised before the failure is still worth checking, and the tag that
    broke the parser is not something we were going to render usefully anyway.
    """
    parser = _TagCollector()
    try:
        parser.feed(text or "")
        parser.close()
    except Exception:       # pragma: no cover - HTMLParser is lenient by design
        pass
    return parser.tags


def _scheme_of(url: str) -> str:
    """The URL's scheme, or "" if it has none.

    Split on the first `/`, `?` or `#` before looking for the colon: in
    `logo.png?w=1:2` the colon is inside a query string, and reading it as a
    scheme would report a nonexistent problem.
    """
    head = re.split(r"[/?#]", url, maxsplit=1)[0]
    return head.split(":", 1)[0].lower() if ":" in head else ""


def extract_html_links(text: str) -> list[tuple[str, str, str]]:
    """Every (tag, attribute, url) that raw HTML points at, in order."""
    found = []
    for tag, attrs in _tags(text):
        for name, value in attrs:
            if name in URL_ATTRS and value and value.strip():
                found.append((tag, name, value.strip()))
    return found


def validate_html_urls(text: str) -> list[str]:
    """Problems with the URLs inside raw HTML -- `href`, `src` and friends.

    Same bargain as `validate_links`: a dead logo or an unopenable link caught
    here is a red line under a text box; caught at send time it is already
    sitting in a prospect's inbox.
    """
    problems: list[str] = []

    for tag, attr, url in extract_html_links(text):
        # Resolved per contact at send time, exactly as in validate_links.
        if "{{" in url:
            continue

        # A bare fragment is the conventional "this link goes nowhere on
        # purpose" placeholder. Dead in a mail, but harmless and deliberate.
        if url == "#":
            continue

        scheme = _scheme_of(url)
        media = attr in _MEDIA_ATTRS

        if not scheme:
            problems.append(
                f'<{tag} {attr}="{url}"> is a relative URL. A mail has no page '
                f"to be relative to, so it will not resolve for anyone -- use a "
                f"full https:// address."
            )
            continue

        if media and scheme == "data":
            if not url.lower().startswith("data:image/"):
                problems.append(
                    f"<{tag} {attr}=...> uses a data: URL that is not an image. "
                    f"Only data:image/... is allowed."
                )
            continue

        allowed = SRC_SCHEMES if media else HREF_SCHEMES
        if scheme not in allowed:
            problems.append(
                f"<{tag} {attr}=...> uses the {scheme}: scheme, which is not "
                f"allowed here. Use " + " or ".join(f"{s}:" for s in sorted(allowed)) + "."
            )

    return problems


#: Tags ordinary enough in a pasted signature that finding one in a footer whose
#: HTML box is OFF means a mistake rather than prose. Deliberately a list of
#: real tag names and not "does it contain a < ": a footer reading "priced at
#: <5 lakh" is not markup, and neither is an address in angle brackets.
_SIGNATURE_TAGS = frozenset({
    "a", "b", "big", "blockquote", "br", "center", "div", "em", "font", "h1",
    "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "li", "ol", "p", "small",
    "span", "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th",
    "thead", "tr", "u", "ul",
})


def looks_like_html(text: str) -> bool:
    """Whether `text` is markup somebody forgot to tick the HTML box for.

    The entire defect this module was reworked for, reduced to a question the
    form can ask. A member pasted a signature, left the box unticked because it
    was not on their form, and mailed the tags to real prospects. The box is on
    their form now -- this is what stops the same mail going out when they
    simply forget to tick it.
    """
    return any(tag in _SIGNATURE_TAGS for tag, _ in _tags(text))


def validate_markup(text: str) -> list[str]:
    """Problems with raw HTML in a body or footer.

    Only consulted when the author ticked the HTML box -- without it the text is
    escaped, so `<script>` there is literal characters and harmless.

    This is the gate that replaced "raw HTML is lead-only"; see the module
    docstring. It rejects rather than strips, so an author is told which tag to
    remove instead of watching their signature quietly change shape.
    """
    problems = []
    text = text or ""

    if _SCRIPT_RE.search(text):
        problems.append(
            "<script> is not allowed: every mail client strips it, so it would "
            "only make the preview lie about what recipients see."
        )
    if _HANDLER_RE.search(text):
        problems.append(
            "Inline event handlers (onclick=, onload=, ...) are not allowed, "
            "for the same reason as <script>."
        )

    # One message per distinct tag, however many times it appears: five
    # <iframe>s are one thing to fix, not five.
    reported: set[str] = set()
    for tag, attrs in _tags(text):
        if tag in FORBIDDEN_TAGS and tag not in reported:
            reported.add(tag)
            problems.append(f"<{tag}> is not allowed: {FORBIDDEN_TAGS[tag]}.")

        for name, value in attrs:
            if name == "style" and value and _CSS_DANGER_RE.search(value):
                problems.append(
                    f"The style= on <{tag}> uses CSS that mail clients strip as "
                    f"a script vector (expression(), behavior:, -moz-binding)."
                )

    problems.extend(validate_html_urls(text))
    return problems


#: `<a href="URL">label</a>`, for the text/plain alternative. Deliberately
#: tolerant of anything between the tags -- `<a><strong>x</strong></a>` is
#: ordinary in a pasted signature, and strip_tags cleans the remains anyway.
_ANCHOR_RE = re.compile(
    r"""<a\b[^>]*?\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))[^>]*>(.*?)</a\s*>""",
    re.I | re.S,
)

#: Any run of three or more newlines. A signature built from block elements
#: leaves one blank line per stripped tag, so the fallback arrives as a column
#: of whitespace with a name at the bottom unless this is collapsed.
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def _anchor_to_text(match: "re.Match[str]") -> str:
    """`<a href="https://x">book</a>` -> `book (https://x)`."""
    url = (match.group(1) or match.group(2) or match.group(3) or "").strip()
    label = html.unescape(strip_tags(match.group(4) or "")).strip()

    # `mailto:someone@x.com (mailto:someone@x.com)` reads as a bug. The address
    # is the useful half; the scheme is machine punctuation.
    if url.lower().startswith("mailto:"):
        url = url[len("mailto:"):]

    if not label:
        return url
    # Already self-describing -- "https://pieds.in (https://pieds.in)" is noise.
    if not url or url in label:
        return label
    return f"{label} ({url})"


def _tidy(text: str) -> str:
    """Trailing spaces off every line, and no run of blank lines longer than one."""
    lines = [line.rstrip() for line in text.splitlines()]
    return _BLANK_RUN_RE.sub("\n\n", "\n".join(lines)).strip("\n")


def to_plain(text: str, raw: bool = False) -> str:
    """`[book a call](https://x)` -> `book a call (https://x)`.

    The text/plain alternative must not be lossy: a client that refuses HTML
    still has to be able to reach the link, so the URL stays visible.

    In raw mode the body is markup, so the tags come out and their entities go
    back to being characters -- a fallback full of `<td>` and `&amp;` reads
    worse than no fallback at all.

    **Anchors get the same treatment as the markdown syntax, and for the same
    reason.** `strip_tags` alone turned `<a href="https://pieds.in">PIEDS</a>`
    into the bare word "PIEDS", so every link in a raw-HTML body or a pasted
    signature was unreachable for exactly the readers this alternative exists to
    serve -- the promise in the paragraph above, quietly broken on the one path
    that did not go through LINK_RE.
    """
    plain = LINK_RE.sub(lambda m: f"{m.group(1).strip()} ({m.group(2)})", text or "")
    if raw:
        plain = _ANCHOR_RE.sub(_anchor_to_text, plain)
        plain = _tidy(html.unescape(strip_tags(plain)))
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
