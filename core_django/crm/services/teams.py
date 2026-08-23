"""Teams: joining one, and who is on it.

The join code replaces two things that both had to go. It replaces the
password-free name picker, which was an unauthenticated login as any lead the
moment the CRM had a public hostname. And it replaces "ask a lead to add you in
/admin/", which needs a lead at a keyboard for every person and does not scale
past the first afternoon of a recruitment drive.

What makes a code safe enough to be the only secret: it is never the *whole*
credential. A person must already have proved a `@pilani.bits-pilani.ac.in`
identity to Google, verified against the signed `hd` claim, before the code is
even asked for. The code decides *which team*; Google decides *who*.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from crm.models import Team, TeamMember, TeamMembership, TeamRole

#: Attempts allowed per session before /join/ stops answering. A code is ~48
#: bits, so this is not what makes guessing infeasible -- it is what stops a
#: script grinding away unnoticed, and what makes the attempt visible in the
#: logs as a burst rather than a trickle.
MAX_JOIN_ATTEMPTS = 10

JOIN_ATTEMPTS_SESSION_KEY = "join_attempts"


class JoinError(ValidationError):
    """Shown to the person trying to join. Never says which half was wrong."""


def team_for_code(code: str) -> Team | None:
    return Team.objects.filter(
        join_code__iexact=(code or "").strip(), is_active=True
    ).first()


def record_attempt(session) -> int:
    attempts = int(session.get(JOIN_ATTEMPTS_SESSION_KEY, 0)) + 1
    session[JOIN_ATTEMPTS_SESSION_KEY] = attempts
    return attempts


def attempts_exhausted(session) -> bool:
    return int(session.get(JOIN_ATTEMPTS_SESSION_KEY, 0)) >= MAX_JOIN_ATTEMPTS


@transaction.atomic
def join(*, code: str, email: str, display_name: str = "") -> TeamMember:
    """Create the member and their membership, in one transaction.

    `email` must already have been verified by the Google flow -- this function
    trusts it completely and has no way to check it. It is never taken from a
    form field, and the only caller is the join view, which reads it from the
    session where the OAuth callback put it.
    """
    team = team_for_code(code)
    if team is None:
        raise JoinError("That join code is not valid. Ask your lead for the code.")

    email = (email or "").strip().lower()
    if not email:
        raise JoinError("Sign in with Google before joining a team.")

    member = TeamMember.objects.filter(bits_email__iexact=email).first()
    if member is None:
        member = TeamMember(
            name=display_name.strip() or email.split("@")[0],
            bits_email=email,
            # Inferred from the address, which for BITS encodes the year:
            # f20250882@... A display field only -- it grants nothing now.
            batch=_batch_from_email(email),
        )
        # full_clean, not a bare save: validate_bits_email is the last line of
        # defence if the domain check upstream is ever loosened.
        member.full_clean()
        member.save()
    elif not member.is_active:
        # Reactivating on a valid code is right: somebody deactivated at the end
        # of last year returning with this year's code is the common case, and
        # the alternative is a lead editing rows in /admin/.
        member.is_active = True
        member.save(update_fields=["is_active", "updated_at"])

    membership, created = TeamMembership.objects.get_or_create(
        team=team, member=member,
        defaults={"role": TeamRole.MEMBER, "joined_at": timezone.now()},
    )
    if not created and not membership.is_active:
        membership.is_active = True
        membership.save(update_fields=["is_active", "updated_at"])

    return member


def _batch_from_email(email: str) -> str:
    """`f20250882@pilani...` -> `2025`. Falls back to the current year.

    Wrong-but-harmless is acceptable here in a way it never was before: the
    batch drives no permission any more. Getting it wrong costs a slightly odd
    label on a members page, not access to anything.
    """
    local = email.split("@")[0]
    digits = "".join(c for c in local if c.isdigit())
    if len(digits) >= 4 and digits[:4].startswith("20"):
        return digits[:4]
    return str(timezone.now().year)


@transaction.atomic
def rotate_join_code(team, actor) -> str:
    """Issue a new code, invalidating the old one immediately.

    The whole point of a code that can be read aloud is that it will eventually
    be overheard, screenshotted, or pasted into the wrong group. Rotation is the
    answer to that, so it must be one button and not a database edit.
    """
    team.join_code = Team.generate_join_code()
    team.save(update_fields=["join_code", "updated_at"])
    return team.join_code


@transaction.atomic
def set_role(team, member, role, *, actor) -> TeamMembership:
    """Promote or demote someone on a team."""
    if role not in dict(TeamRole.choices):
        raise ValidationError(f"{role!r} is not a role.")

    membership = TeamMembership.objects.filter(team=team, member=member).first()
    if membership is None:
        raise ValidationError(f"{member.name} is not on {team.name}.")

    if (membership.role == TeamRole.LEAD and role != TeamRole.LEAD
            and not _has_another_lead(team, member)):
        # A team with no lead is a team nobody can assign work in, and no
        # remaining path to fix it short of /admin/. Refusing the last demotion
        # is cheaper than explaining how to undo it.
        raise ValidationError(
            f"{member.name} is the only lead of {team.name}. Promote someone "
            f"else first."
        )

    membership.role = role
    membership.save(update_fields=["role", "updated_at"])
    return membership


def _has_another_lead(team, excluding) -> bool:
    return TeamMembership.objects.filter(
        team=team, role=TeamRole.LEAD, is_active=True
    ).exclude(member=excluding).exists()


@transaction.atomic
def remove(team, member, *, actor) -> None:
    """Deactivate a membership. The row is kept as a record of who was here."""
    if not _has_another_lead(team, member) and TeamMembership.objects.filter(
        team=team, member=member, role=TeamRole.LEAD
    ).exists():
        raise ValidationError(
            f"{member.name} is the only lead of {team.name}. Promote someone "
            f"else first."
        )
    TeamMembership.objects.filter(team=team, member=member).update(
        is_active=False, updated_at=timezone.now()
    )
