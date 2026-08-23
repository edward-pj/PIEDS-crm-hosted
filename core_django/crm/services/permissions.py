"""The one place that defines what a "lead" is.

A lead is someone with the `lead` role on an active team. It used to be
`member.batch == "2024"` -- a literal that had to be edited every year, could
not express "lead of this team but not that one", and made the permission system
a fact about when somebody was admitted to university.

`is_lead(member)` keeps its one-argument signature on purpose. It answers "is
this person a lead of anything", which is the right question for the ~22
decorator call sites, the template context and the contact-level rules, and
keeping it means none of them changed when the rule underneath did. Questions
that are genuinely about one team get their own function -- see
`assignable_members` -- rather than a second argument that every caller would
have to start passing.
"""

from functools import wraps

from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

#: Cached on the resolved member instance, not in a module-level dict. `is_lead`
#: is called by `lead_required` on every guarded request AND again by `_base`
#: for the template context, so without this a page costs two extra queries for
#: an answer that cannot change within one request. Per-instance means it dies
#: with the request, which is what makes it safe: a role change takes effect on
#: the member's next page load, not at their next login.
_LEAD_CACHE_ATTR = "_is_lead_cached"


def get_member(request):
    """Resolve the signed-in TeamMember, or None.

    Identity is a session key, not a Django `User` -- see services/auth.py.
    """
    from .auth import current_member

    return current_member(request)


def is_lead(member) -> bool:
    """Whether `member` leads any active team.

    `team__is_active=True` is deliberate rather than incidental: a lead of a
    defunct team must not keep global powers over the contact pool forever.
    """
    if not member or not member.is_active:
        return False

    cached = getattr(member, _LEAD_CACHE_ATTR, None)
    if cached is not None:
        return cached

    answer = member.memberships.filter(
        role="lead", is_active=True, team__is_active=True
    ).exists()
    setattr(member, _LEAD_CACHE_ATTR, answer)
    return answer


def led_teams(member):
    """The active teams `member` leads."""
    from crm.models import Team

    if not member or not member.is_active:
        return Team.objects.none()
    return Team.objects.filter(
        is_active=True, memberships__member=member,
        memberships__role="lead", memberships__is_active=True,
    ).distinct()


def teams_of(member):
    """Every active team `member` belongs to, whatever their role."""
    from crm.models import Team

    if not member or not member.is_active:
        return Team.objects.none()
    return Team.objects.filter(
        is_active=True, memberships__member=member, memberships__is_active=True
    ).distinct()


def assignable_members(actor):
    """Who `actor` may assign contacts to: the members of the teams they lead.

    A separate function rather than `is_lead(member, team)`, because this is the
    question the assign screen, the contact form and the round-robin actually
    ask -- and answering it directly means the contact-level rules below keep
    working verbatim. Replaces the unfiltered
    `TeamMember.objects.filter(is_active=True)` those three used before, which
    would let a lead of one cohort assign work to another cohort's members.
    """
    from crm.models import TeamMember

    if not is_lead(actor):
        return TeamMember.objects.none()

    return TeamMember.objects.filter(
        is_active=True,
        memberships__is_active=True,
        memberships__team__in=led_teams(actor),
    ).distinct()


def lead_required(view_func):
    """Restrict a view to active lead-batch members."""

    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        member = get_member(request)
        # Nobody signed in is a wrong turn -- send them to the front door.
        # Signed in but not a lead is a real refusal, and says so.
        if member is None:
            return redirect("login")
        if not is_lead(member):
            raise PermissionDenied(
                "Only team leads may access this page. Ask a lead to change "
                "your role if you should have access."
            )
        request.member = member
        return view_func(request, *args, **kwargs)

    return _wrapped


def member_required(view_func):
    """Restrict a view to any active team member."""

    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        member = get_member(request)
        if member is None:
            return redirect("login")
        request.member = member
        return view_func(request, *args, **kwargs)

    return _wrapped


# --- contact-level rules --------------------------------------------------
# Leads own the whole pool. Everyone else owns exactly what is assigned to
# them: they are the person actually in conversation with that prospect, so
# they are the one who knows when a designation is wrong -- but a stale row
# in someone else's list is not theirs to touch.


def can_edit_contact(member, contact) -> bool:
    """Whether `member` may change this contact's data at all."""
    if not member or not member.is_active:
        return False
    return is_lead(member) or contact.assigned_to_id == member.id


def can_set_lifecycle(member) -> bool:
    """Lifecycle is funnel truth the whole team reads. Leads only.

    Non-leads still move it implicitly by sending -- that path is in
    services/mailing.py and is the only automatic transition.
    """
    return is_lead(member)


def can_hard_delete(member, contact) -> bool:
    """Permanent deletion: leads only, and only for a contact never mailed.

    CampaignMailing.contact is on_delete=PROTECT, so a mailed contact cannot be
    removed from the table at all. Checking here turns an IntegrityError 500
    into a sentence the user can act on. Archiving is the answer for the rest.
    """
    return is_lead(member) and not contact.mailings.exists()


def editable_contacts(member):
    """The queryset `member` is permitted to mutate.

    Bulk operations filter through this rather than trusting posted IDs, so a
    member submitting a hand-crafted list silently affects nothing outside it.
    """
    from crm.models import Contact

    qs = Contact.objects.all()
    return qs if is_lead(member) else qs.filter(assigned_to=member)
