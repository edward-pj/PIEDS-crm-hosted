"""Every mutation of a Contact, in one place.

Both the Django CRM and the local agent's inline editor call these functions
rather than touching the model, for two reasons:

1. The permission rule lives once, in services/permissions.py, and is applied
   here -- so adding a new view cannot accidentally create an unguarded write path.
2. Every field change leaves a ContactAudit row. Fifteen people now edit one
   shared pool from two apps; without the trail, a bad bulk edit is unpickable.

Fields the caller must never be able to set directly (`lifecycle` for non-leads,
`created_by`, the archive stamps) are stripped here, not merely omitted from the
form -- the API accepts JSON, and JSON does not respect a form's field list.
"""

from dataclasses import dataclass, field

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from crm.models import Contact, ContactAudit
from shared.enums import ContactLifecycle

from .permissions import (
    can_edit_contact,
    can_hard_delete,
    can_set_lifecycle,
    editable_contacts,
    is_lead,
)

#: Fields a user may change through a form or the API. Anything outside this set
#: is server-owned; see the module docstring.
EDITABLE_FIELDS = frozenset({
    "first_name", "last_name", "email", "phone_no",
    "linkedin", "company", "designation", "tags",
})

#: Only a lead may set these by hand. The automatic NEW -> CONTACTED transition
#: lives in services/mailing.py::record_result.
#:
#: `assigned_to` is here because the edit form has always OFFERED it to leads
#: while `update()` silently dropped it -- the page said "Saved." and nothing
#: changed. Wiring it through is the honest fix; the alternative was removing a
#: control leads reasonably expect. It routes through assignment.bulk_assign
#: below rather than being set directly, so the reassign guard and the audit row
#: apply exactly as they do on the bulk screen.
LEAD_ONLY_FIELDS = frozenset({"lifecycle", "assigned_to"})


@dataclass
class BulkEditResult:
    updated: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (email, reason)


def _as_text(value) -> str:
    """Render a field value for the audit log."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def _audit(contact, actor, field_name, old, new):
    ContactAudit.objects.create(
        contact=contact,
        actor=actor,
        field=field_name,
        old_value=_as_text(old)[:2000],
        new_value=_as_text(new)[:2000],
    )


def _permitted_fields(actor) -> frozenset:
    return EDITABLE_FIELDS | (LEAD_ONLY_FIELDS if can_set_lifecycle(actor) else frozenset())


def clean_tags(raw) -> list[str]:
    """Normalise tags from a form string or a JSON list.

    Lowercased and de-duplicated while preserving order, so 'Fintech' and
    'fintech' cannot become two different filter facets for the same idea.
    """
    if raw is None:
        return []
    items = raw.split(",") if isinstance(raw, str) else list(raw)

    seen, out = set(), []
    for item in items:
        tag = str(item).strip().lower()[:40]
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


@transaction.atomic
def create(data: dict, actor) -> Contact:
    """Add a single contact by hand.

    A non-lead always gets the contact assigned to themselves -- forced, not
    defaulted, so posting someone else's member id does nothing.
    """
    fields = {k: v for k, v in data.items() if k in _permitted_fields(actor)}
    fields["tags"] = clean_tags(fields.get("tags"))

    contact = Contact(**fields, created_by=actor)

    if is_lead(actor) and data.get("assigned_to"):
        contact.assigned_to = data["assigned_to"]
    else:
        contact.assigned_to = actor
    contact.assigned_at = timezone.now()

    contact.full_clean()
    contact.save()

    _audit(contact, actor, "created", "", contact.email)
    return contact


@transaction.atomic
def update(contact: Contact, data: dict, actor) -> Contact:
    """Change fields on an existing contact, recording each change."""
    if not can_edit_contact(actor, contact):
        raise PermissionDenied(
            "You can only edit contacts assigned to you. Ask a lead to reassign it."
        )

    allowed = _permitted_fields(actor)
    changes = []

    # Reassignment is not an ordinary field write: it has a guard (a contact
    # mid-conversation must not move silently) and its own audit row. Handled
    # first and separately so the loop below stays a plain field diff.
    if "assigned_to" in allowed and "assigned_to" in data:
        new_owner = data.get("assigned_to")
        if new_owner is not None and new_owner.id != contact.assigned_to_id:
            from . import assignment as assignment_svc

            result = assignment_svc.bulk_assign(
                [contact.id], new_owner, actor=actor,
                force=data.get("force_reassign", False),
            )
            if result.skipped:
                raise ValidationError(
                    f"Could not reassign: {result.skipped[0][1]}."
                )
            contact.refresh_from_db()

    for name, value in data.items():
        if name not in allowed or name == "assigned_to":
            continue  # silently dropped -- lifecycle for a non-lead lands here
        if name == "tags":
            value = clean_tags(value)

        old = getattr(contact, name)
        if old == value:
            continue

        setattr(contact, name, value)
        changes.append((name, old, value))

    if not changes:
        return contact

    contact.full_clean()
    contact.save(update_fields=[name for name, _, _ in changes] + ["updated_at"])

    for name, old, new in changes:
        _audit(contact, actor, name, old, new)

    return contact


@transaction.atomic
def set_archived(contact: Contact, actor, *, archived: bool) -> Contact:
    """Archive or restore. Archived contacts are refused by claim_batch."""
    if not can_edit_contact(actor, contact):
        raise PermissionDenied("You can only archive contacts assigned to you.")

    if contact.is_archived == archived:
        return contact

    contact.is_archived = archived
    contact.archived_at = timezone.now() if archived else None
    contact.archived_by = actor if archived else None
    contact.save(update_fields=["is_archived", "archived_at", "archived_by", "updated_at"])

    _audit(contact, actor, "is_archived", not archived, archived)
    return contact


@transaction.atomic
def hard_delete(contact: Contact, actor) -> str:
    """Permanently remove a contact. Returns the email, for the flash message.

    Refuses anything with mail history: CampaignMailing.contact is PROTECT, so
    the database would reject it anyway -- but as a 500, not as an explanation.
    """
    if not is_lead(actor):
        raise PermissionDenied("Only leads may permanently delete a contact.")

    if contact.mailings.exists():
        raise ValidationError(
            f"{contact.email} has been mailed and cannot be deleted -- that would "
            f"destroy the record of what we sent. Archive it instead."
        )

    email = contact.email
    contact.delete()  # cascades notes and audits
    return email


@transaction.atomic
def bulk_edit(
    contact_ids,
    actor,
    *,
    company=None,
    designation=None,
    tags_add=None,
    tags_remove=None,
    lifecycle=None,
    assigned_to=None,
) -> BulkEditResult:
    """Apply the same change to many contacts. Blank arguments mean "leave alone".

    Scoped through `editable_contacts(actor)`, so a member posting IDs outside
    their own list changes nothing rather than erroring -- the ones they do own
    still go through.
    """
    result = BulkEditResult()

    if lifecycle and not can_set_lifecycle(actor):
        raise PermissionDenied("Only leads may set lifecycle in bulk.")
    if assigned_to is not None and not is_lead(actor):
        raise PermissionDenied("Only leads may reassign contacts.")

    tags_add = clean_tags(tags_add)
    tags_remove = set(clean_tags(tags_remove))

    # IDs arrive as strings from a form POST and as UUIDs from internal callers;
    # compare as strings so neither shape silently reports everything skipped.
    permitted = set(
        editable_contacts(actor).filter(id__in=contact_ids).values_list("id", flat=True)
    )
    permitted_str = {str(p) for p in permitted}
    for cid in contact_ids:
        if str(cid) not in permitted_str:
            result.skipped.append((str(cid), "not yours to edit"))

    contacts = Contact.objects.select_for_update().filter(id__in=permitted)

    for contact in contacts:
        changes = []

        if company:
            changes.append(("company", contact.company, company))
            contact.company = company
        if designation:
            changes.append(("designation", contact.designation, designation))
            contact.designation = designation
        if lifecycle:
            changes.append(("lifecycle", contact.lifecycle, lifecycle))
            contact.lifecycle = lifecycle
        if assigned_to is not None:
            changes.append(("assigned_to", contact.assigned_to, assigned_to))
            contact.assigned_to = assigned_to
            contact.assigned_at = timezone.now()

        if tags_add or tags_remove:
            old_tags = list(contact.tags)
            new_tags = [t for t in old_tags if t not in tags_remove]
            new_tags += [t for t in tags_add if t not in new_tags]
            if new_tags != old_tags:
                changes.append(("tags", old_tags, new_tags))
                contact.tags = new_tags

        if not changes:
            continue

        contact.save()
        for name, old, new in changes:
            _audit(contact, actor, name, old, new)
        result.updated += 1

    return result


def lifecycle_counts(qs=None) -> dict:
    """Funnel breakdown for the dashboard, in one query."""
    from django.db.models import Count

    qs = Contact.objects.all() if qs is None else qs
    rows = qs.values("lifecycle").annotate(n=Count("id"))
    counts = {m.value: 0 for m in ContactLifecycle}
    counts.update({r["lifecycle"]: r["n"] for r in rows})
    return counts


def all_tags() -> list[str]:
    """Every tag currently in use, for filter dropdowns."""
    seen = set()
    for tags in Contact.objects.exclude(tags=[]).values_list("tags", flat=True):
        seen.update(tags)
    return sorted(seen)
