from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from datetime import timedelta

from django.utils import timezone
from django.views.decorators.http import require_POST

from shared.enums import (
    BLOCKED_LIFECYCLES,
    TERMINAL_SCHEDULE_STATUSES,
    CampaignStatus,
    ContactLifecycle,
    MailingStatus,
    ScheduleStatus,
)

from .forms import (
    BulkEditForm,
    CampaignForm,
    ContactForm,
    CsvUploadForm,
    FooterForm,
    NoteForm,
)
from .models import (
    Campaign,
    CampaignMailing,
    Contact,
    GmailCredential,
    ScheduledSend,
    TeamMember,
)
from .services import assignment, importer
from .services import mailing
from .services import gmail as gmail_svc
from .services import runner
from .services import gmail_oauth
from .services import secrets as token_store
from .services import permissions
from .services import teams as team_svc
from .services import scheduling as schedule_svc
from .services import campaigns as campaign_svc
from .services import contacts as contact_svc
from .services.permissions import (
    assignable_members,
    can_edit_contact,
    can_hard_delete,
    is_lead,
    lead_required,
    member_required,
)
from .services.render import MissingVariables
from .services.render import render as render_mail   # not django.shortcuts.render

#: Preview payloads are held in the session between the upload and confirm
#: steps. Small enough for a session cookie backend and avoids a temp table.
IMPORT_SESSION_KEY = "pending_import"

#: After this long, a DRAFT mailing is stuck rather than sending. A batch paces
#: itself at a couple of seconds per mail, so anything older than this was
#: abandoned by an agent that stopped.
STRANDED_AFTER_MINUTES = 15


def _base(request, **extra):
    return {"member": request.member, "is_lead": is_lead(request.member), **extra}


# ---------------------------------------------------------------- dashboard

@member_required
def home(request):
    # Roots, counted across every sub-campaign -- see campaign_list for why.
    funnels = (
        Campaign.objects.filter(parent__isnull=True).annotate(
            sent=Count(
                "root_mailings",
                filter=Q(root_mailings__status=MailingStatus.SENT.value),
            ),
            failed=Count(
                "root_mailings",
                filter=Q(root_mailings__status=MailingStatus.FAILED.value),
            ),
            drafted=Count(
                "root_mailings",
                filter=Q(root_mailings__status=MailingStatus.DRAFT.value),
            ),
        )
        .order_by("-created_at")[:6]
    )
    recent_failures = (
        CampaignMailing.objects.filter(status=MailingStatus.FAILED.value)
        .select_related("contact", "campaign", "sent_by")[:8]
    )
    # What this member actually has to do, for the team's active campaigns.
    # The landing page answering "what is my work" rather than "how is the team
    # doing" is the whole point of the migration -- a member should sign in and
    # see their queue, not a dashboard they have to interpret.
    my_queue = []
    for root in Campaign.objects.filter(
        status=CampaignStatus.ACTIVE.value, parent__isnull=True
    ).order_by("title")[:5]:
        pending = _send_queue(request.member, root).count()
        if pending:
            my_queue.append({"campaign": root, "pending": pending})

    return render(request, "crm/home.html", _base(
        request,
        my_queue=my_queue,
        my_lifecycles=contact_svc.lifecycle_counts(request.member.assigned_contacts.all()),
        gmail_connected=gmail_svc.has_usable_credential(request.member),
        my_contacts=request.member.assigned_contacts.count(),
        total_contacts=Contact.objects.count(),
        unassigned=Contact.objects.filter(assigned_to__isnull=True).count(),
        sent_total=CampaignMailing.objects.filter(status=MailingStatus.SENT.value).count(),
        funnels=funnels,
        recent_failures=recent_failures,
    ))


# ----------------------------------------------------------------- contacts

def _filtered_contacts(request):
    """Shared filtering for the contact list and the assignment screen."""
    qs = Contact.objects.select_related("assigned_to").order_by("company", "first_name")

    q = request.GET.get("q", "").strip()
    company = request.GET.get("company", "").strip()
    assignee = request.GET.get("assignee", "").strip()
    tag = request.GET.get("tag", "").strip()
    lifecycle = request.GET.get("lifecycle", "").strip()
    archived = request.GET.get("archived", "").strip()

    # Archived contacts are hidden everywhere unless explicitly asked for. They
    # are also refused by claim_batch, so this is presentation, not safety.
    qs = qs.filter(is_archived=True) if archived == "1" else qs.filter(is_archived=False)

    if q:
        qs = qs.filter(
            Q(first_name__icontains=q) | Q(last_name__icontains=q)
            | Q(email__icontains=q) | Q(company__icontains=q)
        )
    if company:
        qs = qs.filter(company=company)
    if tag:
        qs = qs.filter(tags__contains=[tag])
    if lifecycle:
        qs = qs.filter(lifecycle=lifecycle)
    if assignee == "unassigned":
        qs = qs.filter(assigned_to__isnull=True)
    elif assignee == "mine":
        qs = qs.filter(assigned_to=request.member)
    elif assignee:
        qs = qs.filter(assigned_to_id=assignee)

    return qs, {
        "q": q, "company": company, "assignee": assignee,
        "tag": tag, "lifecycle": lifecycle, "archived": archived,
    }


def _filter_context(request):
    qs, filters = _filtered_contacts(request)
    return qs, {
        "filters": filters,
        "companies": Contact.objects.filter(is_archived=False).order_by("company")
                            .values_list("company", flat=True).distinct(),
        "all_tags": contact_svc.all_tags(),
        "lifecycles": ContactLifecycle.choices(),
        # Who this lead may assign to -- their own teams' members. See
        # services/permissions.py::assignable_members.
        "members": assignable_members(request.member)
                   .annotate(load=Count("assigned_contacts")),
    }


@member_required
def contact_list(request):
    qs, ctx = _filter_context(request)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "crm/contact_list.html", _base(
        request,
        page=page,
        total=qs.count(),
        bulk_form=BulkEditForm(actor=request.member),
        **ctx,
    ))


@member_required
def contact_detail(request, pk):
    contact = get_object_or_404(
        Contact.objects.select_related("assigned_to", "last_contacted_by"), pk=pk
    )

    if request.method == "POST":
        form = NoteForm(request.POST)
        if form.is_valid():
            note = form.save(commit=False)
            note.contact = contact
            note.author = request.member
            note.save()
            messages.success(request, "Note added.")
            return redirect("crm:contact_detail", pk=pk)
    else:
        form = NoteForm()

    return render(request, "crm/contact_detail.html", _base(
        request,
        contact=contact,
        form=form,
        notes=contact.notes.select_related("author"),
        mailings=contact.mailings.select_related("campaign", "sent_by"),
        audits=contact.audits.select_related("actor")[:50],
        can_edit=can_edit_contact(request.member, contact),
        can_delete=can_hard_delete(request.member, contact),
    ))


# ------------------------------------------------------------- contact CRUD
# @member_required, not @lead_required: a 2025 member may edit the contacts
# assigned to them. The per-object check is inside, via can_edit_contact.

@member_required
def contact_new(request):
    if request.method == "POST":
        form = ContactForm(request.POST, actor=request.member)
        if form.is_valid():
            data = dict(form.cleaned_data)
            data["tags"] = data.pop("tags_raw")
            try:
                contact = contact_svc.create(data, request.member)
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
            else:
                messages.success(request, f"Added {contact.full_name}.")
                return redirect("crm:contact_detail", pk=contact.pk)
    else:
        form = ContactForm(actor=request.member)

    return render(request, "crm/contact_form.html", _base(request, form=form, contact=None))


@member_required
def contact_edit(request, pk):
    contact = get_object_or_404(Contact, pk=pk)
    if not can_edit_contact(request.member, contact):
        raise PermissionDenied(
            "You can only edit contacts assigned to you. Ask a lead to reassign it."
        )

    if request.method == "POST":
        form = ContactForm(request.POST, instance=contact, actor=request.member)
        if form.is_valid():
            data = dict(form.cleaned_data)
            data["tags"] = data.pop("tags_raw")
            try:
                contact_svc.update(contact, data, request.member)
            except (ValidationError, PermissionDenied) as exc:
                messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
            else:
                messages.success(request, "Saved.")
                return redirect("crm:contact_detail", pk=contact.pk)
    else:
        form = ContactForm(instance=contact, actor=request.member)

    return render(request, "crm/contact_form.html", _base(request, form=form, contact=contact))


@member_required
@require_POST
def contact_archive(request, pk):
    contact = get_object_or_404(Contact, pk=pk)
    archive = request.POST.get("archived") != "0"
    try:
        contact_svc.set_archived(contact, request.member, archived=archive)
    except PermissionDenied as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"{contact.full_name} archived — it will no longer receive mail."
            if archive else f"{contact.full_name} restored.",
        )
    return redirect(request.POST.get("next") or "crm:contact_list")


@lead_required
@require_POST
def contact_delete(request, pk):
    contact = get_object_or_404(Contact, pk=pk)
    try:
        email = contact_svc.hard_delete(contact, request.member)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("crm:contact_detail", pk=pk)
    except PermissionDenied as exc:
        messages.error(request, str(exc))
        return redirect("crm:contact_detail", pk=pk)

    messages.success(request, f"Deleted {email} permanently.")
    return redirect("crm:contact_list")


@member_required
@require_POST
def contact_bulk_edit(request):
    contact_ids = request.POST.getlist("contact_ids")
    redirect_to = request.POST.get("next") or "crm:contact_list"

    if not contact_ids:
        messages.error(request, "No contacts selected.")
        return redirect(redirect_to)

    form = BulkEditForm(request.POST, actor=request.member)
    if not form.is_valid():
        messages.error(request, "Check the bulk-edit fields.")
        return redirect(redirect_to)

    try:
        result = contact_svc.bulk_edit(
            contact_ids,
            request.member,
            company=form.cleaned_data.get("company"),
            designation=form.cleaned_data.get("designation"),
            tags_add=form.cleaned_data.get("tags_add"),
            tags_remove=form.cleaned_data.get("tags_remove"),
            lifecycle=form.cleaned_data.get("lifecycle"),
        )
    except PermissionDenied as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    if result.updated:
        messages.success(request, f"Updated {result.updated} contact(s).")
    if result.skipped:
        messages.error(
            request,
            f"Skipped {len(result.skipped)} not assigned to you.",
        )
    if not result.updated and not result.skipped:
        messages.info(request, "Nothing to change — the values already matched.")
    return redirect(redirect_to)


# --------------------------------------------------------------- assignment

@lead_required
def assign(request):
    qs, ctx = _filter_context(request)
    # annotate, NOT `{{ c.mailings.count }}` in the template: that was one extra
    # query per row -- 100 round trips to a hosted database, ~11s per render.
    # A page that slow is not just unpleasant, it broke selection outright (the
    # background refresh landed mid-click and rolled the ticks back).
    qs = qs.annotate(mailed=Count("mailings", distinct=True))
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    return render(request, "crm/assign.html", _base(request, page=page, total=qs.count(), **ctx))


def _report_skipped(request, skipped):
    """Surface refusals rather than dropping them silently.

    bulk_assign refuses to move a contact that already has mail history under
    someone else. The lead needs to decide, and can re-post with force.
    """
    if not skipped:
        return
    detail = "; ".join(f"{email} ({reason})" for email, reason in skipped[:5])
    more = f" …and {len(skipped) - 5} more" if len(skipped) > 5 else ""
    messages.error(request, f"Skipped {len(skipped)}: {detail}{more}")


@lead_required
@require_POST
def assign_apply(request):
    contact_ids = request.POST.getlist("contact_ids")
    action = request.POST.get("action")
    redirect_to = request.POST.get("next") or "crm:assign"

    if not contact_ids:
        messages.error(request, "No contacts selected.")
        return redirect(redirect_to)

    force = request.POST.get("force") == "1"

    if action == "unassign":
        result = assignment.bulk_unassign(
            contact_ids, actor=request.member, force=force
        )
        messages.success(request, f"Unassigned {result.assigned} contact(s).")
        _report_skipped(request, result.skipped)
        return redirect(redirect_to)

    # Resolved through assignable_members, not TeamMember.objects: a lead may
    # only hand work to their own teams' members, and a posted member id from
    # another cohort must find nothing rather than being honoured.
    member = assignable_members(request.member).filter(
        pk=request.POST.get("member")
    ).first()
    if not member:
        messages.error(request, "Pick a member of one of your teams to assign to.")
        return redirect(redirect_to)

    try:
        result = assignment.bulk_assign(
            contact_ids, member, actor=request.member, force=force
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(redirect_to)

    if result.assigned:
        messages.success(request, f"Assigned {result.assigned} contact(s) to {member.name}.")

    # bulk_assign refuses to move a contact that already has mail history under
    # someone else. Surface those rather than dropping them silently -- the
    # lead needs to decide, and can re-post with force.
    _report_skipped(request, result.skipped)
    return redirect(redirect_to)


# ------------------------------------------------------------------- import

@lead_required
def contact_import(request):
    preview = None

    if request.method == "POST":
        form = CsvUploadForm(request.POST, request.FILES)
        if form.is_valid():
            preview = importer.parse(form.cleaned_data["file"].read())
            if preview.ok:
                request.session[IMPORT_SESSION_KEY] = [
                    r.data for r in preview.importable
                ]
            else:
                messages.error(request, preview.header_error)
    else:
        form = CsvUploadForm()

    return render(request, "crm/contact_import.html", _base(
        request, form=form, preview=preview,
        NEW=importer.NEW, DUPLICATE=importer.DUPLICATE, INVALID=importer.INVALID,
    ))


@lead_required
@require_POST
def contact_import_confirm(request):
    rows = request.session.pop(IMPORT_SESSION_KEY, None)
    if not rows:
        messages.error(request, "Nothing pending — upload the file again.")
        return redirect("crm:contact_import")

    created = Contact.objects.bulk_create(
        [Contact(**data, created_by=request.member) for data in rows]
    )
    messages.success(request, f"Imported {len(created)} contact(s).")
    return redirect("crm:contact_list")


# ---------------------------------------------------------------- campaigns

@member_required
def campaign_list(request):
    # Roots only, and the totals count mail sent under EVERY sub-campaign of
    # each -- `root_mailings`, not `mailings`. Listing sub-campaigns here would
    # show fifteen near-identical rows per campaign, and counting only
    # `mailings` would report zero for a campaign the team had worked all week.
    campaigns = Campaign.objects.filter(parent__isnull=True).annotate(
        sent=Count(
            "root_mailings",
            filter=Q(root_mailings__status=MailingStatus.SENT.value),
        ),
        failed=Count(
            "root_mailings",
            filter=Q(root_mailings__status=MailingStatus.FAILED.value),
        ),
    )
    return render(request, "crm/campaign_list.html", _base(request, campaigns=campaigns))


@member_required
def campaign_detail(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)

    # For a root, this is the whole team's mail under it -- every member's
    # sub-campaign included. `campaign.mailings` would show only what was sent
    # under the root directly, which after Phase 2 is usually nothing at all,
    # and the page would report a busy campaign as idle.
    mailings = (
        CampaignMailing.objects
        .filter(root_campaign=campaign.parent or campaign)
        .select_related("contact", "sent_by", "campaign")
        .order_by("-created_at")
    )

    counts = {
        status: mailings.filter(status=status).count()
        for status in (MailingStatus.SENT.value, MailingStatus.DRAFT.value,
                       MailingStatus.FAILED.value)
    }

    # A DRAFT more than a few minutes old is not "in flight" -- nothing is going
    # to happen to it on its own, and until it is resolved that contact cannot
    # be mailed for this campaign at all. Showing it as activity is how an
    # interrupted batch reads as a working one.
    stranded = mailings.filter(
        status=MailingStatus.DRAFT.value,
        created_at__lt=timezone.now() - timedelta(minutes=STRANDED_AFTER_MINUTES),
    ).count()
    counts["stranded"] = stranded
    counts["in_flight"] = counts[MailingStatus.DRAFT.value] - stranded

    # Render against a real assigned contact so the preview shows what a
    # recipient actually receives, not the raw template.
    sample = Contact.objects.filter(assigned_to__isnull=False).first()
    preview, preview_error = None, None
    if sample:
        try:
            preview = render_mail(campaign, sample)
        except MissingVariables as exc:
            preview_error = str(exc)

    return render(request, "crm/campaign_detail.html", _base(
        request, campaign=campaign, counts=counts,
        mailings=Paginator(mailings, 100).get_page(request.GET.get("page")),
        assigned_pool=Contact.objects.filter(assigned_to__isnull=False).count(),
        preview=preview, preview_error=preview_error, sample=sample,
        stranded_members=(
            mailings.filter(status=MailingStatus.DRAFT.value)
            .values_list("sent_by__name", flat=True).distinct()
            if stranded else []
        ),
        transitions=campaign_svc.ALLOWED_TRANSITIONS[CampaignStatus(campaign.status)],
    ))


@lead_required
def campaign_edit(request, pk=None):
    campaign = get_object_or_404(Campaign, pk=pk) if pk else None

    if request.method == "POST":
        form = CampaignForm(request.POST, instance=campaign)
        if form.is_valid():
            obj = form.save(commit=False)
            if campaign is None:
                obj.created_by = request.member
            obj.save()
            messages.success(request, "Campaign saved.")
            return redirect("crm:campaign_detail", pk=obj.pk)
    else:
        form = CampaignForm(instance=campaign)

    return render(request, "crm/campaign_form.html", _base(
        request, form=form, campaign=campaign,
    ))


@lead_required
@require_POST
def campaign_transition(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    try:
        campaign_svc.transition(campaign, request.POST.get("status"))
        messages.success(request, f"Campaign is now {campaign.status}.")
    except (ValidationError, ValueError) as exc:
        detail = "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
        messages.error(request, detail)
    return redirect("crm:campaign_detail", pk=pk)


# ------------------------------------------------------------ scheduled sends

@member_required
def schedule_list(request):
    """Every scheduled send, across the whole team.

    This page exists because the executor is somebody's laptop or container. A
    job that quietly never ran -- nothing was awake, the campaign got paused --
    is invisible from the agent that queued it and dead obvious here. `missed`
    and `failed` are surfaced first for exactly that reason.
    """
    jobs = ScheduledSend.objects.select_related("campaign", "member", "created_by")

    status = request.GET.get("status", "open")
    if status == "open":
        jobs = jobs.exclude(status__in=TERMINAL_SCHEDULE_STATUSES)
    elif status and status != "all":
        jobs = jobs.filter(status=status)

    needs_attention = ScheduledSend.objects.filter(
        status__in=[ScheduleStatus.MISSED.value, ScheduleStatus.FAILED.value]
    ).select_related("campaign", "member")[:10]

    return render(request, "crm/schedule_list.html", _base(
        request,
        page=Paginator(jobs, 50).get_page(request.GET.get("page")),
        needs_attention=needs_attention,
        statuses=ScheduleStatus.choices(),
        current_status=status,
    ))


@lead_required
@require_POST
def schedule_cancel(request, pk):
    """Call off a queued send. Lead-only: it may be someone else's job."""
    try:
        job = schedule_svc.cancel(pk)
    except schedule_svc.NotSchedulable as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"Cancelled the {job.campaign.title!r} send for {job.member.name}. "
            "Anything already sent stays sent.",
        )
    return redirect("crm:schedule_list")


# ------------------------------------------------------------------ members

@lead_required
def member_list(request):
    """Who is on the team, what they are carrying, and whether they can send.

    The API-token half of this page is gone with the agent it existed for. A
    lead handing out a token that reaches nothing is worse than no button, and
    a token surface with no consumer is attack surface with no upside.
    """
    return render(request, "crm/member_list.html", _base(
        request,
        members=TeamMember.objects.annotate(
            load=Count("assigned_contacts", distinct=True),
            sent=Count("mailings", filter=Q(mailings__status=MailingStatus.SENT.value),
                       distinct=True),
        ),
    ))


@lead_required
@require_POST
def member_sender_name(request, pk):
    """Set what a member's recipients see in the From line.

    Lead-only: this is the name a cold prospect judges the mail by, so it is not
    something an individual member changes on their own laptop. Blank clears it
    and falls back to the member's real name.
    """
    member = get_object_or_404(TeamMember, pk=pk)
    member.sender_name = (request.POST.get("sender_name") or "").strip()[:120]
    member.save(update_fields=["sender_name", "updated_at"])
    messages.success(
        request, f"{member.name} now sends as “{member.display_name}”."
    )
    return redirect("crm:member_list")


# ------------------------------------------------------------------- gmail
# Connecting Gmail is deliberately its own screen rather than part of signing
# in. See services/gmail_oauth.py for why the two consents are separate.


@member_required
def gmail_settings(request):
    credential = GmailCredential.objects.filter(member=request.member).first()
    return render(request, "crm/gmail_settings.html", _base(
        request,
        credential=credential,
        connected=bool(credential and credential.is_usable),
        google_configured=bool(
            settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET
        ),
        key_configured=token_store.is_configured(),
        scopes=gmail_svc.SCOPES,
    ))


@member_required
@require_POST
def gmail_connect(request):
    if not (settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET):
        messages.error(request, "Google OAuth is not configured on this deployment.")
        return redirect("crm:gmail_settings")

    # Checked before sending anyone to Google: without a key we could complete
    # the whole consent dance and then be unable to store the result, which
    # would look to the member like Google had refused them.
    if not token_store.is_configured():
        messages.error(
            request,
            "GMAIL_TOKEN_KEY is not set on this deployment, so a Gmail grant "
            "cannot be stored securely. Ask a lead to set it.",
        )
        return redirect("crm:gmail_settings")

    return redirect(gmail_oauth.authorization_url(request, request.member))


@member_required
@require_POST
def gmail_disconnect(request):
    if gmail_oauth.disconnect(request.member):
        messages.success(
            request,
            "Gmail disconnected. Access was also revoked with Google, so the "
            "stored token no longer works anywhere.",
        )
    else:
        messages.info(request, "Gmail was not connected.")
    return redirect("crm:gmail_settings")


# -------------------------------------------------------------------- send
# The screen the whole migration is for: sign in, see what you have to mail,
# press one button. Replaces local_agent/templates/index.html.


#: How many rows the Send screen draws. The table is for picking a subset; the
#: whole-queue path is "Send all", which never touches it. Kept well under
#: Django's DATA_UPLOAD_MAX_NUMBER_FIELDS (1000) so a full manual selection
#: still posts rather than erroring.
SEND_TABLE_LIMIT = 500


def _send_queue(member, campaign):
    """This member's contacts that are still mailable for this campaign.

    Excludes anyone already SENT or DRAFT rather than filtering in Python: the
    pool is the whole team's and a member's assignment can be hundreds of rows.
    Scoped to the ROOT campaign, so a contact a teammate has already reached
    never appears in this member's queue at all.
    FAILED is deliberately NOT excluded -- claim_batch reuses a failed row, so
    those are genuinely still sendable and hiding them makes a retry impossible
    from this screen.
    """
    already = CampaignMailing.objects.filter(
        root_campaign=campaign.parent or campaign,
        status__in=[MailingStatus.SENT.value, MailingStatus.DRAFT.value],
    ).values_list("contact_id", flat=True)

    return (
        member.assigned_contacts
        .filter(is_archived=False)
        .exclude(lifecycle__in=BLOCKED_LIFECYCLES)
        .exclude(id__in=already)
        .order_by("company", "first_name")
    )


@member_required
def send(request):
    # ROOTS only. A member picks the team's campaign; which sub-campaign their
    # mail goes out under is bookkeeping, and offering fifteen near-identical
    # titles would invite sending under somebody else's footer.
    campaigns = Campaign.objects.filter(
        status=CampaignStatus.ACTIVE.value, parent__isnull=True
    ).order_by("title")

    campaign_id = request.POST.get("campaign") or request.GET.get("campaign")
    campaign = None
    if campaign_id:
        campaign = campaigns.filter(pk=campaign_id).first()
    elif campaigns.count() == 1:
        campaign = campaigns.first()

    queue = _send_queue(request.member, campaign) if campaign else Contact.objects.none()
    preflight = None
    my_footer_text = ""
    if campaign:
        my_footer_text = campaign_svc.sub_campaign_for(
            campaign, request.member
        ).footer

    if request.method == "POST" and campaign:
        action = request.POST.get("action")

        # "Send all" resolves the queue on the server rather than trusting a
        # form to carry it. Two reasons, and the second is not cosmetic:
        #
        #   - It is the difference between one click and eight hundred. A member
        #     whose whole assigned list is the thing they want to mail should not
        #     have to select it, and the screen truncates its table anyway.
        #   - Django's DATA_UPLOAD_MAX_NUMBER_FIELDS is 1000. Posting a checkbox
        #     per contact means a member with a four-figure list gets a
        #     TooManyFieldsSent error instead of a send -- and that ceiling
        #     arrives exactly when the feature starts being useful.
        #
        # _send_queue is the same function that produced the count on the page,
        # so "Send all 800" cannot disagree with what 800 meant.
        if action in ("send_all", "preflight_all"):
            selected = [str(pk) for pk in queue.values_list("id", flat=True)]
            action = "send" if action == "send_all" else "preflight"
        else:
            selected = request.POST.getlist("contact_ids")

        if not selected:
            messages.error(request, "Select at least one contact.")

        elif action == "preflight":
            # Writes nothing. Worth its own button because the alternative is
            # discovering a missing {{ company }} on the fourteenth mail.
            preflight = mailing.preflight(campaign, request.member, selected)

        elif action == "send":
            if not gmail_svc.has_usable_credential(request.member):
                messages.error(
                    request,
                    "Connect Gmail before sending — the CRM sends from your own "
                    "mailbox and has no permission to yet.",
                )
                return redirect("crm:gmail_settings")

            # Created here rather than at assignment time: a member should not
            # need setting up before they can send, and doing it lazily means no
            # bookkeeping when someone joins mid-campaign. The mail then goes
            # out with their footer and, crucially, claims the contact against
            # the ROOT so no teammate can mail them again.
            sub = campaign_svc.sub_campaign_for(campaign, request.member)

            try:
                job = schedule_svc.create(
                    campaign_id=sub.id,
                    member=request.member,
                    contact_ids=selected,
                    scheduled_at=timezone.now(),
                    cc=request.POST.get("cc", ""),
                    bcc=request.POST.get("bcc", ""),
                )
            except (schedule_svc.NotSchedulable, mailing.InvalidCopyAddresses) as exc:
                messages.error(request, str(exc))
            else:
                # Queued rather than sent inside this request, deliberately. A
                # free instance can be reaped mid-request, and a send that dies
                # halfway leaves claimed contacts stranded. Queued work survives
                # the tab being closed, the laptop being shut, and the container
                # being restarted.
                messages.success(
                    request,
                    f"{job.total} mail(s) queued. Nothing sends on its own yet "
                    f"— press “Send queued mail now” below to send them from "
                    f"your Gmail.",
                )
                return redirect("crm:schedule_list")

    return render(request, "crm/send.html", _base(
        request,
        campaigns=campaigns,
        campaign=campaign,
        # A picker now, not the send path -- "Send all" never reads this list.
        # Still bounded: rendering four figures of table rows is its own problem.
        queue=queue[:SEND_TABLE_LIMIT],
        queue_total=queue.count() if campaign else 0,
        table_limit=SEND_TABLE_LIMIT,
        preflight=preflight,
        my_footer_text=my_footer_text,
        gmail_connected=gmail_svc.has_usable_credential(request.member),
    ))


@member_required
def my_footer(request, pk):
    """Edit your own footer on one root campaign.

    The sub-campaign is created on demand: a member should not have to be
    "set up" before they can personalise their sign-off, and creating it lazily
    means no bookkeeping when someone joins mid-campaign.
    """
    root = get_object_or_404(Campaign, pk=pk, parent__isnull=True)
    sub = campaign_svc.sub_campaign_for(root, request.member)

    form = FooterForm(
        request.POST or None, instance=sub, is_lead=is_lead(request.member)
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Footer saved.")
        return redirect("crm:campaign_detail", pk=root.pk)

    # Rendered against a real contact so the preview is what a recipient gets,
    # not the template. Falls back to the raw footer when there is nobody to
    # render against yet.
    sample = Contact.objects.filter(assigned_to=request.member).first()
    preview, preview_error = None, None
    if sample:
        try:
            preview = render_mail(sub, sample)
        except MissingVariables as exc:
            preview_error = str(exc)

    return render(request, "crm/footer_form.html", _base(
        request, form=form, root=root, sub=sub,
        preview=preview, preview_error=preview_error, sample=sample,
    ))


# ------------------------------------------------------------------- teams


@member_required
def team_list(request):
    return render(request, "crm/team_list.html", _base(
        request,
        teams=permissions.teams_of(request.member).prefetch_related(
            "memberships__member"
        ),
        led=set(permissions.led_teams(request.member).values_list("id", flat=True)),
    ))


@lead_required
def team_detail(request, pk):
    team = get_object_or_404(permissions.led_teams(request.member), pk=pk)
    return render(request, "crm/team_detail.html", _base(
        request,
        team=team,
        memberships=team.memberships.select_related("member").order_by(
            "-role", "member__name"
        ),
        campaigns=Campaign.objects.filter(team=team, parent__isnull=True),
    ))


@lead_required
@require_POST
def team_rotate_code(request, pk):
    team = get_object_or_404(permissions.led_teams(request.member), pk=pk)
    code = team_svc.rotate_join_code(team, request.member)
    messages.success(
        request,
        f"New join code: {code}. The old one stopped working immediately.",
    )
    return redirect("crm:team_detail", pk=team.pk)


@lead_required
@require_POST
def team_set_role(request, pk):
    team = get_object_or_404(permissions.led_teams(request.member), pk=pk)
    member = get_object_or_404(TeamMember, pk=request.POST.get("member"))
    try:
        team_svc.set_role(team, member, request.POST.get("role", ""), actor=request.member)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, f"{member.name} is now a {request.POST.get('role')}.")
    return redirect("crm:team_detail", pk=team.pk)


@lead_required
def team_distribute(request, pk):
    """Split a filtered slice of the pool across the team, round-robin.

    Two steps on purpose: preview, then commit. Reassignment is refused for
    anyone already mid-conversation, so a distribution is not something a lead
    can casually undo -- they should see the split before it happens.
    """
    team = get_object_or_404(permissions.led_teams(request.member), pk=pk)
    # `team_members`, not `members`: _filter_context already puts a `members`
    # key in the context for the filter dropdown, and shadowing it would make
    # the "assigned to" filter list the wrong people.
    team_members = list(
        TeamMember.objects.filter(
            memberships__team=team, memberships__is_active=True, is_active=True
        ).distinct().order_by("name")
    )

    qs, ctx = _filter_context(request)
    root = None
    if request.POST.get("campaign") or request.GET.get("campaign"):
        root = Campaign.objects.filter(
            pk=request.POST.get("campaign") or request.GET.get("campaign"),
            parent__isnull=True,
        ).first()

    plan, committed = None, False
    if request.method == "POST" and team_members:
        contacts = list(qs[:1000])
        try:
            if request.POST.get("action") == "commit":
                plan = assignment.distribute(
                    contacts, team_members, actor=request.member, root_campaign=root
                )
                committed = True
                messages.success(
                    request,
                    f"Distributed {plan.total} contact(s) across "
                    f"{len(team_members)} member(s).",
                )
            else:
                plan = assignment.plan_distribution(
                    contacts, team_members, root_campaign=root
                )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))

    return render(request, "crm/team_distribute.html", _base(
        request, **ctx,
        team=team, team_members=team_members, plan=plan, committed=committed,
        root=root,
        campaigns=Campaign.objects.filter(
            team=team, parent__isnull=True, status=CampaignStatus.ACTIVE.value
        ),
        pool_size=qs.count(),
    ))


@member_required
@require_POST
def run_queue(request):
    """Drain the queued sends now, from the browser.

    **This is a stand-in for the scheduler, not the scheduler.** The hosted plan
    has no shell, so `manage.py run_tick` cannot be run on the deployed instance
    at all — without this button, queued mail would have no way to leave the
    building. When the tick endpoint and an external pinger land, this stays as
    the manual override.

    `@member_required`, not `@lead_required`, and that is the point. While the
    scheduler is deferred this button IS the send path: gating it on a role
    meant a member pressed Send, watched their own mail sit in `queued`, and
    waited for a lead to log in and press a second button on a different screen.
    Nobody reads that as "the queue is fine", they read it as "sending is
    broken" — and they were half right, because nothing was going to send.

    Widening it grants no power a member did not already have. `tick()` sends
    each job through `sendable_members()`, using **that job's owner's** Gmail
    credential; there is no path by which pressing this makes mail leave your
    mailbox under someone else's name, or someone else's mailbox under yours.
    What a member gains is the ability to execute work the team has already
    queued and approved. What a lead keeps is the brake: cancelling a job, and
    pausing the root campaign, both still stop it.

    Safe to press twice, and safe for two people to press at once, which is why
    it needs no lock: `claim_due` leases with `select_for_update(skip_locked)`
    so a second run sees nothing rather than blocking, and even if both somehow
    reached the same job, `uniq_root_campaign_contact` still stands between them
    and a prospect's inbox. The lease is a scheduling convenience; the
    constraint is the guarantee.

    Bounded by TICK_MAX_SECONDS so it cannot hold a worker indefinitely. If it
    stops on budget it says so, and pressing again continues from the cursor.
    """
    report = runner.tick()

    if report.jobs == 0:
        messages.info(request, "Nothing was due to send.")
    else:
        messages.success(
            request,
            f"Sent {report.sent}, skipped {report.skipped}, across "
            f"{report.jobs} job(s).",
        )
    if report.stopped_early:
        messages.info(
            request, "Stopped on the time budget — press again to continue."
        )
    for error in report.errors[:3]:
        messages.error(request, error)

    return redirect("crm:schedule_list")
