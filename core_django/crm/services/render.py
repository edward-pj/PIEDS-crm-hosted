"""Campaign template -> concrete subject and body for one contact.

Moved server-side from the local agent: the server owns the campaign template,
so an agent can never alter the wording of what goes out under its member's
name. Shares PLACEHOLDER_RE / ALLOWED_VARIABLES with campaigns.py so the
validation shown in the campaign form and the substitution done at send time
can never disagree.
"""

from dataclasses import dataclass

from .campaigns import ALLOWED_VARIABLES, PLACEHOLDER_RE
from .richtext import to_fragment, to_plain, wrap_document

#: RFC 3676 signature delimiter: "-- " on its own line. Mail clients recognise
#: it and collapse or grey what follows, which is exactly what a footer is.
PLAIN_FOOTER_SEPARATOR = "\n\n-- \n"

#: The HTML equivalent. A rule rather than a "--" so it reads as a divider.
HTML_FOOTER_SEPARATOR = (
    '\n<br>\n<hr style="border:none;border-top:1px solid #ddd;margin:16px 0">\n'
)


class MissingVariables(Exception):
    """A contact lacks data the template requires. Do not mail them."""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__("missing/blank variables: " + ", ".join(missing))


@dataclass
class Rendered:
    """What one contact actually receives.

    Both bodies are carried, not one: the mail goes out as multipart/alternative
    so a client that refuses HTML still gets `body`.
    """

    subject: str
    body: str
    body_html: str = ""


def contact_context(contact) -> dict[str, str]:
    return {
        "first_name": contact.first_name or "",
        "last_name": contact.last_name or "",
        "full_name": contact.full_name,
        "email": contact.email or "",
        "company": contact.company or "",
        "designation": contact.designation or "",
    }


def render(campaign, contact) -> Rendered:
    """Substitute every placeholder, or refuse.

    We raise rather than substituting an empty string: "Hi ," reads worse than
    a skipped contact, and a blank company in a subject line advertises that the
    mail was blasted. Callers surface these as MISSING_VARS so the data can be
    fixed before sending.

    **The message comes from the root, the footer from `campaign` itself.** A
    sub-campaign owns nothing but its footer, so passing one here yields the
    team's wording with that member's sign-off. Passing a root yields the same
    thing with no footer, which is what a root campaign mailed directly is.

    Footers deliberately carry no placeholders -- see `validate_footer` in
    campaigns.py for why that restriction earns its keep.
    """
    root = campaign.parent or campaign

    ctx = contact_context(contact)
    used = set(PLACEHOLDER_RE.findall(f"{root.mail_sub}\n{root.mail_body}"))

    unknown = used - ALLOWED_VARIABLES
    if unknown:
        raise MissingVariables(sorted(unknown))

    missing = sorted(v for v in used if not ctx.get(v, "").strip())
    if missing:
        raise MissingVariables(missing)

    def substitute(text: str) -> str:
        return PLACEHOLDER_RE.sub(lambda m: ctx[m.group(1)], text)

    # Substitute first, convert second. In the default mode that means every
    # contact value passes through the escaping in to_fragment, so a company
    # name with an `&` in it cannot break the markup. The cost is that a contact
    # field literally containing `[x](y)` would turn into a link -- accepted.
    #
    # With `is_html` on, that escaping is gone and a contact field containing
    # markup is trusted. Acceptable: contacts are typed in by the same team that
    # writes the campaigns, and the CSV importer is lead-only.
    raw = bool(getattr(root, "is_html", False))
    body = substitute(root.mail_body)

    footer = (getattr(campaign, "footer", "") or "").strip()
    footer_raw = bool(getattr(campaign, "footer_is_html", False))

    plain = to_plain(body, raw=raw)
    # Each part is converted with ITS OWN raw flag and only the results are
    # joined. Converting the joined text instead would escape a raw-HTML footer
    # into visible tags whenever the body happened to be plain text -- and
    # joining two to_html() results would nest two complete HTML documents,
    # which is why to_fragment and wrap_document exist separately.
    fragment = to_fragment(body, raw=raw)

    if footer:
        plain += PLAIN_FOOTER_SEPARATOR + to_plain(footer, raw=footer_raw)
        fragment += HTML_FOOTER_SEPARATOR + to_fragment(footer, raw=footer_raw)

    return Rendered(
        subject=substitute(root.mail_sub),
        body=plain,
        body_html=wrap_document(fragment),
    )
