"""Claim / report: the transactional core of the whole system.

This is the code that guarantees a prospect is never mailed twice. It moved here
from the local agent when the agent stopped touching Postgres directly -- the
row lock and the unique constraint only mean something next to the database.

Read the ordering comments before changing anything.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone

from crm.models import Campaign, CampaignMailing, Contact
from shared.enums import (
    BLOCKED_LIFECYCLES,
    CampaignStatus,
    ContactLifecycle,
    MailingStatus,
)

from .render import MissingVariables, render

# Outcome codes shared with the API and both UIs.
OK = "OK"
ALREADY_MAILED = "ALREADY_MAILED"
NOT_ASSIGNED = "NOT_ASSIGNED"
MISSING_VARS = "MISSING_VARS"
CAP_REACHED = "CAP_REACHED"
ARCHIVED = "ARCHIVED"
BLOCKED = "BLOCKED"
SENT = "SENT"
FAILED = "FAILED"

#: Gmail's per-account quota is real; tripping it throttles the whole mailbox
#: for hours. Enforced server-side so it counts across every device a member uses.
DAILY_SEND_CAP = 400

#: CC/BCC apply to EVERY mail in a batch, so ten copied addresses on a 200-mail
#: send is two thousand extra deliveries. Small enough to keep that a decision
#: rather than an accident -- and a pasted contact list will not fit through it.
MAX_COPY_ADDRESSES = 10


class CampaignNotSendable(Exception):
    pass


class InvalidCopyAddresses(Exception):
    """A CC/BCC list the server refuses. Reported before anything is claimed."""


def parse_copy_addresses(raw) -> str:
    """Normalise a comma-separated CC/BCC list, or refuse it.

    Returns the cleaned string to store and send. Lives here beside claim_batch
    rather than in the API layer so preflight and claim cannot disagree about
    which addresses are acceptable.
    """
    if not raw:
        return ""
    if not isinstance(raw, str):
        raise InvalidCopyAddresses("cc and bcc must be comma-separated strings.")

    addresses = [part.strip() for part in raw.split(",") if part.strip()]
    if len(addresses) > MAX_COPY_ADDRESSES:
        raise InvalidCopyAddresses(
            f"At most {MAX_COPY_ADDRESSES} addresses; got {len(addresses)}. "
            "Remember these are copied on every mail in the batch."
        )

    for address in addresses:
        try:
            validate_email(address)
        except DjangoValidationError:
            raise InvalidCopyAddresses(f"{address!r} is not a valid email address.")

    return ", ".join(addresses)


@dataclass
class Skipped:
    contact_id: str
    email: str
    name: str
    reason: str
    #: The outcome code, so the agent does not have to parse English to colour
    #: a row. It used to match on substrings of `reason`, which meant rewording
    #: a message silently turned "already mailed" into "failed" in the UI.
    code: str = ALREADY_MAILED


@dataclass
class Claimed:
    mailing_id: str
    contact_id: str
    to: str
    name: str
    subject: str
    body: str
    #: The HTML alternative. The agent sends it alongside `body` as
    #: multipart/alternative; empty means send plain text only.
    body_html: str = ""
    #: The rest of the envelope, decided here rather than on the laptop. The
    #: agent sets no recipient the server has not already recorded.
    from_name: str = ""
    cc: str = ""
    bcc: str = ""


def load_sendable_campaign(campaign_id) -> Campaign:
    try:
        campaign = Campaign.objects.select_related("parent").get(id=campaign_id)
    except (Campaign.DoesNotExist, DjangoValidationError, ValueError, TypeError):
        raise CampaignNotSendable(f"No campaign {campaign_id}")

    # Both the sub-campaign AND its root must be active. Checking only the
    # sub-campaign would mean pausing "Ignite" left fifteen sub-campaigns
    # happily draining their queues -- and the emergency brake is the whole
    # reason status exists.
    for c in {campaign.id: campaign, **({campaign.parent.id: campaign.parent}
                                        if campaign.parent else {})}.values():
        if c.status != CampaignStatus.ACTIVE.value:
            raise CampaignNotSendable(
                f"Campaign {c.title!r} is {c.status}; only "
                f"{CampaignStatus.ACTIVE.value} campaigns can be mailed."
            )
    return campaign


def sent_last_24h(member) -> int:
    since = timezone.now() - timedelta(hours=24)
    return CampaignMailing.objects.filter(
        sent_by=member, status=MailingStatus.SENT.value, sent_at__gte=since
    ).count()


def unmailable_reason(contact) -> tuple[str, str] | None:
    """Why this contact must not be mailed, as (outcome_code, human reason).

    Called from BOTH preflight and claim_batch so the dry run can never disagree
    with the real thing about who is sendable.
    """
    if contact.is_archived:
        return ARCHIVED, "archived"
    if contact.lifecycle in BLOCKED_LIFECYCLES:
        return BLOCKED, f"marked {contact.get_lifecycle_display().lower()}"
    return None


def preflight(campaign, member, contact_ids) -> list[dict]:
    """Dry run. Writes nothing; tells the user exactly what will happen."""
    # FAILED is deliberately absent: those are re-claimable now, so counting
    # them as "already mailed" would make the dry run disagree with the send.
    # Scoped to the ROOT, not to this campaign: a contact another member has
    # already mailed under their own sub-campaign is not sendable, and a dry run
    # that says otherwise is worse than no dry run.
    root = campaign.parent or campaign
    already = set(
        CampaignMailing.objects.filter(
            root_campaign=root,
            contact_id__in=contact_ids,
            status__in=[MailingStatus.SENT.value, MailingStatus.DRAFT.value],
        ).values_list("contact_id", flat=True)
    )

    results = []
    for contact in Contact.objects.filter(id__in=contact_ids):
        base = {
            "contact_id": str(contact.id),
            "email": contact.email,
            "name": contact.full_name,
        }
        blocked = unmailable_reason(contact)

        if contact.assigned_to_id != member.id:
            results.append({**base, "status": NOT_ASSIGNED, "detail": "not assigned to you"})
        elif blocked:
            results.append({**base, "status": blocked[0], "detail": blocked[1]})
        elif contact.id in already:
            results.append({**base, "status": ALREADY_MAILED, "detail": "already has a mailing"})
        else:
            try:
                render(campaign, contact)
                results.append({**base, "status": OK, "detail": ""})
            except MissingVariables as exc:
                results.append({**base, "status": MISSING_VARS, "detail": str(exc)})
    return results


def claim_batch(campaign, member, contact_ids, *, cc="", bcc="") -> tuple[list[Claimed], list[Skipped]]:
    """Reserve mailings for an agent to send.

    One transaction PER CONTACT, each committed before the agent is told about
    it. That ordering is the point: by the time a mail can leave the building,
    a durable DRAFT row already records the attempt. A crash can therefore
    leave an ambiguous DRAFT (resolvable) but never a sent mail with no record
    (unrecoverable).
    """
    claimed: list[Claimed] = []
    skipped: list[Skipped] = []

    # Raises before any row is written: a malformed CC must fail the whole
    # request, not leave half a batch claimed with the copy silently dropped.
    cc = parse_copy_addresses(cc)
    bcc = parse_copy_addresses(bcc)
    from_name = member.display_name
    root = campaign.parent or campaign

    budget = DAILY_SEND_CAP - sent_last_24h(member)

    for contact_id in contact_ids:
        if budget <= 0:
            skipped.append(
                Skipped(str(contact_id), "", "",
                        f"daily cap of {DAILY_SEND_CAP} reached", CAP_REACHED)
            )
            continue

        try:
            with transaction.atomic():
                # Lock the contact for the duration of the DB work only. The
                # Gmail round trip happens later, on the agent, with no lock
                # held -- otherwise one slow send would serialize the whole team.
                contact = Contact.objects.select_for_update().get(id=contact_id)

                if contact.assigned_to_id != member.id:
                    skipped.append(
                        Skipped(str(contact.id), contact.email, contact.full_name,
                                "not assigned to you", NOT_ASSIGNED)
                    )
                    continue

                # Re-checked here under the lock rather than trusted from the
                # agent's stale list: someone may have archived this contact
                # between the page loading and the send being pressed.
                blocked = unmailable_reason(contact)
                if blocked:
                    skipped.append(
                        Skipped(str(contact.id), contact.email, contact.full_name,
                                blocked[1], blocked[0])
                    )
                    continue

                try:
                    rendered = render(campaign, contact)
                except MissingVariables as exc:
                    skipped.append(
                        Skipped(str(contact.id), contact.email, contact.full_name,
                                str(exc), MISSING_VARS)
                    )
                    continue

                # A previous attempt that we KNOW did not reach anyone is
                # re-used rather than refused. Without this a failed mail is
                # unfixable: the row blocks every future claim, so the contact
                # can never be mailed for this campaign again, and the operator
                # sees "already has a mailing" for someone who never got one.
                #
                # Only FAILED qualifies. SENT is the guarantee itself and is
                # never touched. DRAFT means "claimed, outcome unknown" -- it
                # may be sitting in a prospect's inbox with the report lost, so
                # it must go through reconcile against Gmail first. That is the
                # whole reason DRAFT and FAILED are different states.
                #
                # Scoped to the ROOT, not to this campaign. That is the fix for
                # the duplicate-mail bug: without it, a row under Aarav's
                # sub-campaign is invisible to Kabir's claim and he mails the
                # same prospect again under his own footer.
                existing = (
                    CampaignMailing.objects
                    .select_for_update()
                    .filter(root_campaign=root, contact=contact)
                    .first()
                )

                if existing and existing.status != MailingStatus.FAILED.value:
                    # Who it belongs to changes what the operator should do, so
                    # say which. "Someone else already mailed them" is a fact
                    # about the team; "you already mailed them" is a fact about
                    # you, and showing the wrong one gets a bug filed.
                    theirs = existing.sent_by_id != member.id
                    if existing.status == MailingStatus.SENT.value:
                        reason = (
                            f"already mailed by {existing.sent_by.name} for this "
                            f"campaign" if theirs else "already has a mailing (sent)"
                        )
                    else:
                        reason = (
                            f"claimed by {existing.sent_by.name} and unresolved"
                            if theirs else
                            "already has a mailing (claimed but unresolved; "
                            "run Resolve stranded drafts)"
                        )
                    skipped.append(
                        Skipped(str(contact.id), contact.email, contact.full_name,
                                reason, ALREADY_MAILED)
                    )
                    continue

                if existing:
                    # A FAILED row is taken over rather than left for whoever
                    # first tried it. A mail nobody received is not really
                    # theirs, and refusing here would mean one member's transient
                    # Gmail error silently removed a prospect from the team's
                    # reachable pool until that same member retried.
                    #
                    # `campaign` is rewritten too, so the mail goes out with THIS
                    # member's footer rather than the original sender's.
                    #
                    # Re-render rather than reuse the old snapshot: the template
                    # or the contact may have been fixed since it failed, and
                    # that fix is usually WHY someone is retrying.
                    existing.status = MailingStatus.DRAFT.value
                    existing.campaign = campaign
                    existing.sent_by = member
                    existing.rendered_subject = rendered.subject
                    existing.rendered_body = rendered.body
                    existing.rendered_body_html = rendered.body_html
                    existing.from_name = from_name
                    existing.cc = cc
                    existing.bcc = bcc
                    existing.error_detail = ""
                    existing.mail_message_id = ""
                    existing.mail_thread_id = ""
                    existing.save()
                    mailing = existing
                else:
                    # The unique constraint still fires here for a genuine race.
                    # This is the entire idempotency guarantee -- a double-click
                    # or two agents racing collide at the database rather than
                    # putting a second copy in a prospect's inbox.
                    mailing = CampaignMailing.objects.create(
                        campaign=campaign,
                        contact=contact,
                        sent_by=member,
                        status=MailingStatus.DRAFT.value,
                        rendered_subject=rendered.subject,
                        rendered_body=rendered.body,
                        rendered_body_html=rendered.body_html,
                        from_name=from_name,
                        cc=cc,
                        bcc=bcc,
                    )
        except Contact.DoesNotExist:
            skipped.append(
                Skipped(str(contact_id), "", "", "contact no longer exists", FAILED)
            )
            continue
        except IntegrityError as exc:
            # A genuine race: two claims for the same contact landed at once and
            # the database refused the second. That is the guarantee working.
            #
            # WHICH constraint fired says something different to the operator,
            # so read it off psycopg3's diagnostics rather than reporting one
            # sentence for both. "Someone else on your team already mailed this
            # contact" is a different fact from "you already claimed them", and
            # a member shown the wrong one files a bug.
            constraint = getattr(
                getattr(exc.__cause__, "diag", None), "constraint_name", ""
            ) or ""
            if constraint == "uniq_root_campaign_contact":
                reason = "a teammate claimed this contact for the same campaign first"
            else:
                reason = "already has a mailing"

            contact = Contact.objects.filter(id=contact_id).first()
            skipped.append(
                Skipped(str(contact_id),
                        contact.email if contact else "",
                        contact.full_name if contact else "",
                        reason, ALREADY_MAILED)
            )
            continue

        budget -= 1
        claimed.append(
            Claimed(
                mailing_id=str(mailing.id),
                contact_id=str(contact.id),
                to=contact.email,
                name=contact.full_name,
                subject=rendered.subject,
                body=rendered.body,
                body_html=rendered.body_html,
                from_name=from_name,
                cc=cc,
                bcc=bcc,
            )
        )

    return claimed, skipped


@transaction.atomic
def record_result(mailing_id, member, *, status, message_id="", thread_id="", error="") -> dict:
    """Record what the agent's Gmail call actually did."""
    try:
        mailing = CampaignMailing.objects.select_for_update().get(id=mailing_id)
    except (CampaignMailing.DoesNotExist, DjangoValidationError, ValueError, TypeError):
        return {"status": FAILED, "detail": "no such mailing"}

    if mailing.sent_by_id != member.id:
        return {"status": NOT_ASSIGNED, "detail": "not your mailing"}

    # Only a DRAFT is awaiting a result. Re-reporting a settled mailing is a
    # replayed request, not a new send -- ignore it rather than overwriting.
    if mailing.status != MailingStatus.DRAFT.value:
        return {"status": mailing.status.upper(), "detail": "already settled"}

    now = timezone.now()

    if status == "sent":
        mailing.status = MailingStatus.SENT.value
        mailing.mail_message_id = message_id or ""
        mailing.mail_thread_id = thread_id or ""
        mailing.sent_at = now
        mailing.error_detail = ""
        mailing.save()

        Contact.objects.filter(id=mailing.contact_id).update(
            last_contacted_by=member, last_contacted_at=now, updated_at=now
        )

        # The "changes the moment they mail" rule. Scoped to NEW deliberately:
        # a contact someone has already marked REPLIED must never be dragged
        # backwards to CONTACTED by a later campaign.
        Contact.objects.filter(
            id=mailing.contact_id, lifecycle=ContactLifecycle.NEW.value
        ).update(lifecycle=ContactLifecycle.CONTACTED.value, updated_at=now)

        return {"status": SENT, "detail": thread_id}

    mailing.status = MailingStatus.FAILED.value
    mailing.error_detail = (error or "unknown error")[:2000]
    mailing.save(update_fields=["status", "error_detail", "updated_at"])
    return {"status": FAILED, "detail": mailing.error_detail}


def stranded_drafts(member):
    """DRAFT rows left by an agent that died between claim and report."""
    return (
        CampaignMailing.objects.filter(sent_by=member, status=MailingStatus.DRAFT.value)
        .select_related("contact", "campaign")
        .order_by("created_at")
    )


@transaction.atomic
def reset_for_retry(mailing_id, member) -> bool:
    """Put a FAILED mailing back into DRAFT so it can be claimed again.

    Retry works by UPDATING this row. Inserting a second one is impossible --
    the unique constraint forbids it -- which is exactly what makes retrying safe.
    """
    try:
        mailing = CampaignMailing.objects.select_for_update().get(
            id=mailing_id, sent_by=member, status=MailingStatus.FAILED.value
        )
    except CampaignMailing.DoesNotExist:
        return False
    mailing.status = MailingStatus.DRAFT.value
    mailing.error_detail = ""
    mailing.save(update_fields=["status", "error_detail", "updated_at"])
    return True
