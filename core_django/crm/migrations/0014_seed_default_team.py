"""Put every existing member on one team, preserving exactly the powers they had.

This is what makes the batch -> role flip a **zero-behaviour-change deploy**.
`is_lead()` stops reading `member.batch == "2024"` and starts reading the
membership role; without this migration, that single edit would revoke every
lead's access the moment it shipped, and the first anyone would know is a
PermissionDenied on the assign screen.

The mapping is deliberately the old rule, applied once:

    role = "lead" if member.batch == LEAD_BATCH else "member"

After this runs, `batch` is a display field. It is left in place rather than
dropped -- it is real information about a person, and a dropped column is the
one migration with no cheap rollback.

Idempotent, because a half-applied deploy that gets re-run must not create a
second team and split the cohort in two.
"""

from django.db import migrations

#: Inlined rather than imported from shared.enums. A migration is a historical
#: record: it has to keep meaning the same thing after LEAD_BATCH is deleted
#: from the codebase, which is precisely what the next commit does.
LEAD_BATCH_AT_TIME_OF_WRITING = "2024"

DEFAULT_TEAM_NAME = "PIEDS Outreach"


def seed(apps, schema_editor):
    Team = apps.get_model("crm", "Team")
    TeamMembership = apps.get_model("crm", "TeamMembership")
    TeamMember = apps.get_model("crm", "TeamMember")

    if not TeamMember.objects.exists():
        # A fresh database. Creating an empty team with a join code nobody was
        # told would just be litter; the first lead creates a real one.
        return

    team = Team.objects.filter(name=DEFAULT_TEAM_NAME).first()
    if team is None:
        # Not Team.generate_join_code(): historical models carry no methods.
        # Deliberately not a random code either -- a code generated inside a
        # migration is one nobody can ever be told, so it is written here in
        # plain sight and MUST be rotated from the team page after deploying.
        team = Team.objects.create(
            name=DEFAULT_TEAM_NAME,
            join_code="ROTATE-ME",
            is_active=True,
        )

    existing = set(
        TeamMembership.objects.filter(team=team).values_list("member_id", flat=True)
    )

    TeamMembership.objects.bulk_create([
        TeamMembership(
            team=team,
            member=member,
            role=("lead" if member.batch == LEAD_BATCH_AT_TIME_OF_WRITING
                  else "member"),
            is_active=member.is_active,
        )
        for member in TeamMember.objects.all()
        if member.id not in existing
    ])

    # Every existing campaign belongs to the one team that now exists.
    Campaign = apps.get_model("crm", "Campaign")
    Campaign.objects.filter(team__isnull=True).update(team=team)


def unseed(apps, schema_editor):
    """Reversible, so 0015's permission change can be rolled back through."""
    Team = apps.get_model("crm", "Team")
    Campaign = apps.get_model("crm", "Campaign")

    team = Team.objects.filter(name=DEFAULT_TEAM_NAME).first()
    if team is None:
        return
    Campaign.objects.filter(team=team).update(team=None)
    team.memberships.all().delete()
    team.delete()


class Migration(migrations.Migration):

    dependencies = [("crm", "0013_teams")]

    operations = [migrations.RunPython(seed, unseed)]
