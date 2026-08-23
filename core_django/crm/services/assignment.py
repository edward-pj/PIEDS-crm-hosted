"""Bulk assignment and round-robin distribution of contacts to team members."""

from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from crm.models import CampaignMailing, Contact, ContactAudit


@dataclass
class AssignmentResult:
    assigned: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (email, reason)


@dataclass
class DistributionPlan:
    """What a distribution WOULD do. Nothing is written to produce one.

    Shown before committing because a lead splitting 500 contacts eight ways is
    making a decision they cannot easily undo -- reassigning afterwards is
    refused for anyone already mid-conversation, and rightly so.
    """

    per_member: dict = field(default_factory=dict)   # member -> [contact]
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.per_member.values())


def _audit(contact, actor, old, new):
    """Assignment is a contact mutation and belongs in the same trail as the rest.

    It was the ONLY one that wrote no audit row, which meant "who gave this to
    me, and when" -- the question actually asked when a handover goes wrong --
    was the one question the audit log could not answer.
    """
    ContactAudit.objects.create(
        contact=contact,
        actor=actor,
        field="assigned_to",
        old_value=(old.name if old else "")[:2000],
        new_value=(new.name if new else "")[:2000],
    )


@transaction.atomic
def bulk_assign(contact_ids, member, *, actor=None, force: bool = False) -> AssignmentResult:
    """Assign contacts to `member`.

    Reassigning a contact who already has mailings is refused unless `force`:
    the existing owner may be mid-conversation, and silently moving the contact
    would strand that thread with no owner watching for the reply.
    """
    if not member.is_active:
        raise ValidationError(f"{member.name} is not an active team member.")

    result = AssignmentResult()
    # `of=("self",)` locks only `contacts`. Required, not tidiness:
    # `assigned_to` is nullable, so select_related makes it a LEFT OUTER JOIN,
    # and Postgres refuses FOR UPDATE across one. Locking the member rows would
    # be wrong anyway -- we are not changing them.
    contacts = list(
        Contact.objects.select_for_update(of=("self",))
        .select_related("assigned_to")
        .filter(id__in=contact_ids)
    )

    # One query for the whole batch instead of `contact.mailings.exists()` per
    # contact. Invisible at ten contacts; over the Supabase pooler at five
    # hundred it is five hundred round trips inside one transaction.
    mailed = set(
        CampaignMailing.objects
        .filter(contact_id__in=[c.id for c in contacts])
        .values_list("contact_id", flat=True)
    )

    for contact in contacts:
        if contact.assigned_to_id == member.id:
            result.skipped.append((contact.email, "already assigned to this member"))
            continue

        if not force and contact.assigned_to_id and contact.id in mailed:
            result.skipped.append(
                (contact.email, "has mail history under another member; use force to override")
            )
            continue

        previous = contact.assigned_to
        contact.assigned_to = member
        contact.assigned_at = timezone.now()
        contact.save(update_fields=["assigned_to", "assigned_at", "updated_at"])
        _audit(contact, actor or member, previous, member)
        result.assigned += 1

    return result


@transaction.atomic
def bulk_unassign(contact_ids, *, actor, force: bool = False) -> AssignmentResult:
    """Return contacts to the unassigned pool.

    Was a blanket `.update()` with no ownership check, no mail-history guard and
    no audit -- so it could silently strip a contact from someone mid-thread,
    and leave nothing behind saying who did it. It is the exact inverse of
    `bulk_assign` and had none of its protections.
    """
    result = AssignmentResult()
    # `of=("self",)` locks only `contacts`. Required, not tidiness:
    # `assigned_to` is nullable, so select_related makes it a LEFT OUTER JOIN,
    # and Postgres refuses FOR UPDATE across one. Locking the member rows would
    # be wrong anyway -- we are not changing them.
    contacts = list(
        Contact.objects.select_for_update(of=("self",))
        .select_related("assigned_to")
        .filter(id__in=contact_ids)
    )
    mailed = set(
        CampaignMailing.objects
        .filter(contact_id__in=[c.id for c in contacts])
        .values_list("contact_id", flat=True)
    )

    for contact in contacts:
        if contact.assigned_to_id is None:
            result.skipped.append((contact.email, "already unassigned"))
            continue
        if not force and contact.id in mailed:
            result.skipped.append(
                (contact.email, "has mail history; use force to unassign anyway")
            )
            continue

        previous = contact.assigned_to
        contact.assigned_to = None
        contact.assigned_at = None
        contact.save(update_fields=["assigned_to", "assigned_at", "updated_at"])
        _audit(contact, actor, previous, None)
        result.assigned += 1

    return result


def plan_distribution(contacts, members, *, root_campaign=None) -> DistributionPlan:
    """Deal `contacts` round-robin across `members`. Writes nothing.

    Round-robin by position rather than by current load, deliberately: a lead
    who has just filtered to "fintech in Bangalore" wants that slice split
    evenly between the people working it, not topped up against last month's
    totals. Balancing by load is a different feature and would surprise anyone
    who counted the rows before pressing the button.

    `root_campaign` is what makes this the assignment-time half of the send-time
    guarantee: a contact the team has already reached under that campaign is not
    worth anyone's slot, so it is skipped here rather than being assigned and
    then refused by uniq_root_campaign_contact days later.
    """
    plan = DistributionPlan()
    members = list(members)
    if not members:
        raise ValidationError(
            "No members to distribute to. Add people to the team first."
        )

    plan.per_member = {m: [] for m in members}

    already = set()
    if root_campaign is not None:
        already = set(
            CampaignMailing.objects
            .filter(root_campaign=root_campaign.parent or root_campaign)
            .values_list("contact_id", flat=True)
        )

    index = 0
    for contact in contacts:
        if contact.id in already:
            plan.skipped.append((contact.email, "already mailed for this campaign"))
            continue
        if contact.is_archived:
            plan.skipped.append((contact.email, "archived"))
            continue

        plan.per_member[members[index % len(members)]].append(contact)
        index += 1

    return plan


@transaction.atomic
def distribute(contacts, members, *, actor, root_campaign=None, force=False) -> DistributionPlan:
    """Plan and commit in one transaction.

    Wraps `bulk_assign` rather than writing rows itself, so the reassign guard
    and the audit trail apply exactly as they do to a hand assignment. One
    transaction for the whole distribution: a lead who presses this once must
    not end up with three of eight members loaded because the connection dropped
    halfway.
    """
    plan = plan_distribution(contacts, members, root_campaign=root_campaign)

    for member, assigned in plan.per_member.items():
        if not assigned:
            continue
        result = bulk_assign(
            [c.id for c in assigned], member, actor=actor, force=force
        )
        plan.skipped.extend(result.skipped)

    return plan
