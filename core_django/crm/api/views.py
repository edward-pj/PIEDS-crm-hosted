"""JSON endpoints driving the local sending agents.

The agent holds exactly one thing this server cannot: the member's Gmail
credentials. Everything else -- who owns a contact, what the template says,
whether a mail already went out -- is decided here.
"""

from dataclasses import asdict

from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_POST

from crm.models import Campaign, CampaignMailing, Contact, ScheduledSend
from crm.services import contacts as contact_svc
from crm.services import mailing as mailing_svc
from crm.services import followups as followup_svc
from crm.services import scheduling as schedule_svc
from shared.enums import TERMINAL_SCHEDULE_STATUSES, CampaignStatus, MailingStatus

from .auth import api_token_required, json_error, parse_json


@require_GET
@api_token_required
def me(request):
    """Identity handshake. The agent aborts if this disagrees with its config."""
    member = request.member
    return JsonResponse({
        "id": str(member.id),
        "name": member.name,
        "bits_email": member.bits_email,
        "batch": member.batch,
        "assigned_contacts": member.assigned_contacts.count(),
        "sent_last_24h": mailing_svc.sent_last_24h(member),
        "daily_cap": mailing_svc.DAILY_SEND_CAP,
    })


@require_GET
@api_token_required
def campaigns(request):
    qs = Campaign.objects.filter(status=CampaignStatus.ACTIVE.value)
    return JsonResponse([
        {"id": str(c.id), "title": c.title, "mail_sub": c.mail_sub, "var_list": c.var_list}
        for c in qs
    ], safe=False)


@require_GET
@api_token_required
def contacts(request):
    """The member's assigned contacts, annotated for the selected campaign.

    `already_mailed` lets the UI grey a row out *before* the unique constraint
    has to reject it -- the constraint stays the real guarantee, this is just
    courtesy.
    """
    member = request.member
    campaign_id = request.GET.get("campaign_id")

    mailed = set()
    if campaign_id:
        # Root-scoped, so a contact a teammate has already reached under their
        # own sub-campaign shows as mailed here too. Scoping to this campaign
        # alone would show them as available and then refuse them at send time,
        # which is the exact confusion the grey-out exists to prevent.
        campaign = Campaign.objects.filter(id=campaign_id).first()
        if campaign is not None:
            mailed = set(
                CampaignMailing.objects.filter(
                    root_campaign_id=campaign.parent_id or campaign.id,
                    status__in=[MailingStatus.SENT.value, MailingStatus.DRAFT.value],
                ).values_list("contact_id", flat=True)
            )

    # Archived contacts are excluded outright rather than shown greyed: they are
    # refused by claim_batch anyway, and offering them would only invite a
    # confusing skip at send time.
    qs = (
        Contact.objects.filter(assigned_to=member, is_archived=False)
        .order_by("company", "first_name")
    )
    return JsonResponse([_contact_json(c, mailed) for c in qs], safe=False)


def _contact_json(c, mailed=frozenset()):
    """One contact, with enough detail for the agent's inline edit dialog."""
    return {
        "id": str(c.id),
        "name": c.full_name,
        "first_name": c.first_name,
        "last_name": c.last_name,
        "email": c.email,
        "phone_no": c.phone_no,
        "linkedin": c.linkedin,
        "company": c.company,
        "designation": c.designation,
        "tags": list(c.tags or []),
        "lifecycle": c.lifecycle,
        "lifecycle_label": c.get_lifecycle_display(),
        "mailable": c.is_mailable,
        "already_mailed": c.id in mailed,
        "last_contacted_at": c.last_contacted_at.isoformat() if c.last_contacted_at else None,
    }


@api_token_required
def contact_create(request):
    """Add a contact from the agent. Always assigned to the calling member."""
    if request.method != "POST":
        return json_error("POST required.", status=405)

    payload, error = parse_json(request)
    if error:
        return error

    try:
        contact = contact_svc.create(payload, request.member)
    except DjangoValidationError as exc:
        return json_error("; ".join(_flatten(exc)), status=422)
    except PermissionDenied as exc:
        return json_error(str(exc), status=403)

    return JsonResponse(_contact_json(contact), status=201)


@api_token_required
def contact_update(request, contact_id):
    """Edit a contact from the agent's row dialog.

    Same permission rule as the web form -- a member may only change contacts
    assigned to them, and `lifecycle` is dropped for non-leads by the service.
    """
    if request.method not in ("PATCH", "POST"):
        return json_error("PATCH required.", status=405)

    try:
        contact = Contact.objects.get(id=contact_id)
    except (Contact.DoesNotExist, ValueError, TypeError):
        return json_error("No such contact.", status=404)

    payload, error = parse_json(request)
    if error:
        return error

    try:
        contact = contact_svc.update(contact, payload, request.member)
    except PermissionDenied as exc:
        return json_error(str(exc), status=403)
    except DjangoValidationError as exc:
        return json_error("; ".join(_flatten(exc)), status=422)

    return JsonResponse(_contact_json(contact))


def _flatten(exc) -> list[str]:
    """Django validation errors, field-labelled, as flat strings."""
    if hasattr(exc, "message_dict"):
        return [f"{f}: {' '.join(m)}" for f, m in exc.message_dict.items()]
    return list(exc.messages)


def _campaign_and_ids(request):
    """Shared validation for preflight and claim.

    Returns (campaign, contact_ids, copies, error). `copies` is the validated
    {"cc", "bcc"} pair -- parsed here so a dry run and the real send can never
    disagree about who gets copied.
    """
    payload, error = parse_json(request)
    if error:
        return None, None, None, error

    contact_ids = payload.get("contact_ids") or []
    if not isinstance(contact_ids, list) or not contact_ids:
        return None, None, None, json_error("contact_ids must be a non-empty list.")

    try:
        campaign = mailing_svc.load_sendable_campaign(payload.get("campaign_id"))
    except mailing_svc.CampaignNotSendable as exc:
        return None, None, None, json_error(str(exc))

    try:
        copies = {
            "cc": mailing_svc.parse_copy_addresses(payload.get("cc")),
            "bcc": mailing_svc.parse_copy_addresses(payload.get("bcc")),
        }
    except mailing_svc.InvalidCopyAddresses as exc:
        return None, None, None, json_error(str(exc))

    return campaign, contact_ids, copies, None


@require_POST
@api_token_required
def preflight(request):
    campaign, contact_ids, copies, error = _campaign_and_ids(request)
    if error:
        return error

    member = request.member
    outcomes = mailing_svc.preflight(campaign, member, contact_ids)

    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o["status"]] = counts.get(o["status"], 0) + 1

    # Render the first sendable one so the operator sees a real mail, not a template.
    sample = next((o for o in outcomes if o["status"] == mailing_svc.OK), None)
    preview = None
    if sample:
        contact = Contact.objects.get(id=sample["contact_id"])
        rendered = mailing_svc.render(campaign, contact)
        preview = {
            "to": contact.email,
            "from_name": member.display_name,
            "subject": rendered.subject,
            "body": rendered.body,
            "body_html": rendered.body_html,
        }

    return JsonResponse({
        "campaign": campaign.title,
        "counts": counts,
        # Echoed back validated, so the operator sees exactly who gets copied on
        # every mail BEFORE committing to the send. That is the point of a dry run.
        "cc": copies["cc"],
        "bcc": copies["bcc"],
        "from_name": member.display_name,
        "sendable": counts.get(mailing_svc.OK, 0),
        "remaining_today": mailing_svc.DAILY_SEND_CAP - mailing_svc.sent_last_24h(member),
        "preview": preview,
        "outcomes": outcomes,
    })


@require_POST
@api_token_required
def claim(request):
    """Reserve mailings. Every returned item has a committed DRAFT row."""
    campaign, contact_ids, copies, error = _campaign_and_ids(request)
    if error:
        return error

    claimed, skipped = mailing_svc.claim_batch(
        campaign, request.member, contact_ids, cc=copies["cc"], bcc=copies["bcc"]
    )
    return JsonResponse({
        "claimed": [asdict(c) for c in claimed],
        "skipped": [asdict(s) for s in skipped],
    })


@require_POST
@api_token_required
def report_result(request, mailing_id):
    payload, error = parse_json(request)
    if error:
        return error

    status = payload.get("status")
    if status not in ("sent", "failed"):
        return json_error("status must be 'sent' or 'failed'.")

    return JsonResponse(mailing_svc.record_result(
        mailing_id,
        request.member,
        status=status,
        message_id=payload.get("message_id", ""),
        thread_id=payload.get("thread_id", ""),
        error=payload.get("error", ""),
    ))


@require_GET
@api_token_required
def drafts(request):
    """DRAFTs stranded by an agent that died between claim and report."""
    # Paged, and without the bodies. An interrupted 400-contact batch leaves 400
    # of these, and reconcile only needs the address and subject to ask Gmail
    # "did this go out?" -- shipping 400 rendered mails to answer that was
    # megabytes of response on the connection that had just proved unreliable.
    limit = min(int(request.GET.get("limit", 100) or 100), 500)
    qs = mailing_svc.stranded_drafts(request.member)

    return JsonResponse({
        "total": qs.count(),
        "drafts": [
            {
                "mailing_id": str(m.id),
                "campaign": m.campaign.title,
                "to": m.contact.email,
                "name": m.contact.full_name,
                "subject": m.rendered_subject,
                "created_at": m.created_at.isoformat(),
            }
            for m in qs[:limit]
        ],
    })


# ----------------------------------------------------------- scheduled sends
# The agent cannot schedule anything by itself: it posts an intent, the server
# validates it, stores it, and later hands it back when it is due. Same division
# as the claim protocol -- the laptop supplies Gmail, the server supplies truth.

def _parse_when(raw):
    """ISO 8601 with an offset -> aware datetime, or None."""
    if not raw:
        return None
    when = parse_datetime(raw)
    if when is None:
        return None
    return timezone.make_aware(when) if timezone.is_naive(when) else when


@api_token_required
def schedules(request):
    """GET the caller's jobs, POST a new one."""
    if request.method == "GET":
        qs = (
            ScheduledSend.objects.filter(member=request.member)
            .select_related("campaign", "member")
        )
        status = request.GET.get("status")
        if status == "open":
            qs = qs.exclude(status__in=TERMINAL_SCHEDULE_STATUSES)
        elif status:
            qs = qs.filter(status=status)
        return JsonResponse([schedule_svc.as_json(j) for j in qs[:200]], safe=False)

    if request.method != "POST":
        return json_error("GET or POST only.", status=405)

    payload, error = parse_json(request)
    if error:
        return error

    when = _parse_when(payload.get("scheduled_at"))
    if when is None:
        return json_error("scheduled_at must be an ISO 8601 datetime with an offset.")

    try:
        job = schedule_svc.create(
            campaign_id=payload.get("campaign_id"),
            member=request.member,
            contact_ids=payload.get("contact_ids") or [],
            scheduled_at=when,
            cc=payload.get("cc", ""),
            bcc=payload.get("bcc", ""),
            batch_size=payload.get("batch_size", 0),
            interval_minutes=payload.get("interval_minutes", 0),
        )
    except schedule_svc.NotSchedulable as exc:
        return json_error(str(exc))

    return JsonResponse(schedule_svc.as_json(job), status=201)


@require_POST
@api_token_required
def schedule_claim(request):
    """Lease everything due for this agent's member.

    Sweeping expired leases here rather than on a separate timer: every agent
    polls this endpoint anyway, so the recovery path runs as often as the thing
    it recovers from, with no extra process to keep alive.
    """
    payload, _ = parse_json(request)
    payload = payload or {}

    swept = schedule_svc.sweep_expired_leases()
    # Retiring jobs nothing ran in time belongs here for the same reason as the
    # lease sweep: every agent polls this anyway, so the housekeeping runs as
    # often as the thing it cleans up, with no extra process to keep alive.
    missed = schedule_svc.sweep_missed()
    # Queue any follow-ups that have come due. Same reasoning as the sweeps:
    # every agent polls this anyway, so there is no separate timer to keep alive.
    queued = len(followup_svc.run_all_rules())
    jobs = schedule_svc.claim_due(
        request.member,
        agent_id=str(payload.get("agent_id", ""))[:80],
        limit=int(payload.get("limit", 5) or 5),
    )

    return JsonResponse({
        "requeued_stale": swept,
        "marked_missed": missed,
        "follow_ups_queued": queued,
        "claimed": [
            {**schedule_svc.as_json(j), "contact_ids": j.next_slice(j.batch_size or None)}
            for j in jobs
        ],
    })


@require_POST
@api_token_required
def schedule_progress(request, schedule_id):
    payload, error = parse_json(request)
    if error:
        return error

    if payload.get("failed"):
        schedule_svc.mark_failed(schedule_id, request.member, payload.get("error", ""))
        return JsonResponse({"status": "failed"})

    return JsonResponse(schedule_svc.record_progress(
        schedule_id,
        request.member,
        attempted=payload.get("attempted", 0),
        sent=payload.get("sent", 0),
        skipped=payload.get("skipped", 0),
        error=payload.get("error", ""),
    ))


@require_POST
@api_token_required
def schedule_cancel(request, schedule_id):
    try:
        job = schedule_svc.cancel(schedule_id, member=request.member)
    except schedule_svc.NotSchedulable as exc:
        return json_error(str(exc))
    return JsonResponse(schedule_svc.as_json(job))


@require_POST
@api_token_required
def schedule_reschedule(request, schedule_id):
    payload, error = parse_json(request)
    if error:
        return error

    when = _parse_when(payload.get("scheduled_at"))
    if when is None:
        return json_error("scheduled_at must be an ISO 8601 datetime with an offset.")

    try:
        job = schedule_svc.reschedule(schedule_id, when, member=request.member)
    except schedule_svc.NotSchedulable as exc:
        return json_error(str(exc))
    return JsonResponse(schedule_svc.as_json(job))


# --------------------------------------------------------------- follow-ups
# Only the agent can read Gmail, so only the agent can answer "did they reply?".
# It reports the fact and stops there -- acting on it is the server's job.

@require_GET
@api_token_required
def reply_scan(request):
    """Threads worth re-reading for this member."""
    return JsonResponse([
        {
            "mailing_id": str(m.id),
            "thread_id": m.mail_thread_id,
            "email": m.contact.email,
            "campaign": m.campaign.title,
            "sent_at": m.sent_at.isoformat() if m.sent_at else None,
        }
        for m in followup_svc.threads_to_check(request.member)
    ], safe=False)


@require_POST
@api_token_required
def reply_report(request, mailing_id):
    payload, error = parse_json(request)
    if error:
        return error

    result = followup_svc.record_reply_scan(
        mailing_id, request.member, replied=bool(payload.get("replied"))
    )

    # A reply that lands after the follow-up was queued but before it went out
    # is the one window where we would otherwise chase someone who has already
    # answered. Pull them back out of the queue.
    if result.get("status") == "replied":
        try:
            mailing = CampaignMailing.objects.select_related(
                "campaign", "campaign__parent"
            ).get(id=mailing_id)
        except (CampaignMailing.DoesNotExist, ValueError, TypeError):
            return JsonResponse(result)
        # Rules hang off ROOT campaigns; the mail went out under the sender's
        # sub-campaign. Reading rules off `mailing.campaign` would find none.
        root = mailing.campaign.parent or mailing.campaign
        for rule in root.follow_up_rules.filter(is_active=True):
            result["pulled_from_queue"] = followup_svc.cancel_pending_for(
                mailing.contact_id, rule.follow_up_id
            )

    return JsonResponse(result)
