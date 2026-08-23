"""Campaign lifecycle rules, imposed at the service layer (not on the model).

The model stores `status` as a plain char field with choices; nothing stops a
bad value being written by a careless `.save()`. Every intentional change goes
through `transition()`, which is where the actual rules live.
"""

import re

from django.core.exceptions import ValidationError

from shared.enums import CampaignStatus

#: What may follow what. Terminal states have no outgoing edges except archive.
ALLOWED_TRANSITIONS = {
    CampaignStatus.DRAFT: {CampaignStatus.ACTIVE, CampaignStatus.ARCHIVED},
    CampaignStatus.ACTIVE: {CampaignStatus.PAUSED, CampaignStatus.COMPLETED},
    CampaignStatus.PAUSED: {CampaignStatus.ACTIVE, CampaignStatus.COMPLETED},
    CampaignStatus.COMPLETED: {CampaignStatus.ARCHIVED},
    CampaignStatus.ARCHIVED: set(),
}

#: Matches {{ var }} / {{var}} in subject and body.
PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

#: Contact fields a template is allowed to interpolate.
ALLOWED_VARIABLES = {
    "first_name",
    "last_name",
    "full_name",
    "email",
    "company",
    "designation",
}


def extract_placeholders(campaign) -> set[str]:
    """Placeholders in the message. Read from the root -- see validate_footer."""
    root = campaign.parent or campaign
    text = f"{root.mail_sub}\n{root.mail_body}"
    return set(PLACEHOLDER_RE.findall(text))


def validate_footer(text) -> None:
    """A footer may not interpolate contact data. Raises ValidationError.

    Not an arbitrary restriction -- it protects `validate_template` below, which
    demands SET EQUALITY between `var_list` and the placeholders actually used.
    A member's footer saying `{{ company }}` would be "undeclared" against a
    `var_list` on a root campaign that member cannot edit, so allowing it would
    force that equality down to a subset check and destroy the typo detector
    the function exists for. A footer is a signature; it has no business
    interpolating a prospect's data.
    """
    used = PLACEHOLDER_RE.findall(text or "")
    if used:
        raise ValidationError(
            "A footer cannot use {{ variables }} — it is your sign-off, and the "
            "same text goes to everyone. Found: "
            + ", ".join(sorted(set(used)))
        )


def sub_campaign_for(root, member):
    """This member's sub-campaign of `root`, created on demand.

    A sub-campaign carries NO copy of the subject or body. render() reads both
    from the root, and duplicating them here would create a second copy that
    goes quietly stale the moment a lead edits the root -- so every member would
    keep mailing whatever the wording was on the day they were first assigned.

    Created ACTIVE because status lives on the root: pausing "Ignite" is the
    emergency brake for every member at once (see scheduling.is_runnable), and
    a per-member status would give fifteen more places to forget.
    """
    from crm.models import Campaign

    root = root.parent or root
    existing = Campaign.objects.filter(parent=root, owner=member).first()
    if existing is not None:
        return existing

    return Campaign.objects.create(
        title=f"{root.title} — {member.name}",
        mail_sub="",
        mail_body="",
        var_list=[],
        status=CampaignStatus.ACTIVE.value,
        parent=root,
        owner=member,
        created_by=member,
    )


class ReparentCollision(ValidationError):
    """Reparenting would make two mailings collide on (root, contact)."""


def set_parent(campaign, parent, owner=None) -> None:
    """Root `campaign` under `parent` as `owner`'s sub-campaign, or refuse.

    `owner` is required whenever `parent` is: becoming a sub-campaign IS
    becoming somebody's, and the `campaign_parent_and_owner_agree` check
    constraint refuses the half-state. Making it a parameter turns that into a
    readable error instead of an IntegrityError from the database.

    **This check is the only thing standing between a reorganisation and a
    permanently broken campaign.** `uniq_root_campaign_contact` is enforced on
    INSERT, so a bad reparent does not fail here -- it fails on the next send,
    long after the person who did it has moved on, and it presents as "this
    contact was already mailed" for a contact that was mailed under a different
    campaign entirely.

    The migrations are safe precisely because they create no hierarchy. Anything
    that creates hierarchy afterwards has to run this first.
    """
    from crm.models import Campaign, CampaignMailing

    if parent is None:
        campaign.parent = None
        campaign.owner = None
        campaign.save(update_fields=["parent", "owner", "updated_at"])
        return

    if owner is None:
        raise ValidationError(
            "A sub-campaign belongs to one member. Pass the owner as well as "
            "the parent."
        )

    if parent.parent_id is not None:
        raise ValidationError(
            f"{parent.title!r} is itself a sub-campaign. Campaigns are two levels "
            f"deep: a root, and one sub-campaign per member."
        )
    if parent.id == campaign.id:
        raise ValidationError("A campaign cannot be its own parent.")
    if Campaign.objects.filter(parent=campaign).exists():
        raise ValidationError(
            f"{campaign.title!r} already has sub-campaigns of its own, so it "
            f"cannot become one. Move its sub-campaigns first."
        )

    # Which contacts would end up with two rows under the same root?
    mine = CampaignMailing.objects.filter(campaign=campaign).values_list(
        "contact_id", flat=True
    )
    clashing = (
        CampaignMailing.objects
        .filter(root_campaign=parent, contact_id__in=list(mine))
        .select_related("contact")
        .values_list("contact__email", flat=True)[:10]
    )
    clashing = list(clashing)
    if clashing:
        raise ReparentCollision(
            f"Cannot move {campaign.title!r} under {parent.title!r}: "
            f"{len(clashing)} contact(s) have already been mailed under both, "
            f"and one contact may only be mailed once per root campaign. "
            f"First few: " + ", ".join(clashing)
        )

    campaign.parent = parent
    campaign.owner = owner
    campaign.save(update_fields=["parent", "owner", "updated_at"])


def validate_template(campaign) -> None:
    """Every placeholder must be declared in var_list AND be a real field.

    Run before allowing DRAFT -> ACTIVE. Catching a typo like {{ compnay }}
    here is the difference between a clean campaign and 200 mails that say
    "Dear {{ first_name }}".
    """
    root = campaign.parent or campaign
    used = extract_placeholders(root)
    declared = set(root.var_list or [])

    unknown = used - ALLOWED_VARIABLES
    if unknown:
        raise ValidationError(
            "Template uses variables that are not Contact fields: "
            + ", ".join(sorted(unknown))
            + ". Allowed: "
            + ", ".join(sorted(ALLOWED_VARIABLES))
        )

    undeclared = used - declared
    if undeclared:
        raise ValidationError(
            "Template uses variables missing from var_list: " + ", ".join(sorted(undeclared))
        )

    unused = declared - used
    if unused:
        raise ValidationError(
            "var_list declares variables the template never uses: "
            + ", ".join(sorted(unused))
        )


def transition(campaign, to_status) -> None:
    """Move a campaign to a new status, or raise ValidationError."""
    current = CampaignStatus(campaign.status)
    target = CampaignStatus(to_status)

    if current == target:
        return

    if target not in ALLOWED_TRANSITIONS[current]:
        allowed = ", ".join(sorted(s.value for s in ALLOWED_TRANSITIONS[current])) or "nothing"
        raise ValidationError(
            f"Cannot move campaign from {current.value} to {target.value}. "
            f"Allowed from {current.value}: {allowed}."
        )

    # Going live is the only transition that lets mail leave the building, so
    # it is the only one that gets a template check.
    if target == CampaignStatus.ACTIVE:
        validate_template(campaign)

    campaign.status = target.value
    campaign.save(update_fields=["status", "updated_at"])
