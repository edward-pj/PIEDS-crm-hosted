"""Create the first team and its first lead, on whatever database is configured.

**This is the one command a fresh deployment cannot do without.** Everything in
the CRM is reachable only after signing in, signing in requires a `TeamMember`,
becoming a `TeamMember` requires a join code, and a join code requires a `Team`.
Nothing in the app creates the first link in that chain, deliberately: a
migration that invents a team would have to invent a join code nobody could ever
be told (see `0014_seed_default_team`, which writes `ROTATE-ME` in plain sight
for exactly that reason and only for databases that already had members).

So the chain is started from outside, once:

    ../.venv/bin/python manage.py bootstrap_team \
        you@pilani.bits-pilani.ac.in --team "PIEDS Outreach" --name "Your Name"

**Render's free plan has no shell**, so run this from your own machine with
`DATABASE_URL` pointed at the production database. Supabase is reachable from
anywhere; that is the whole point of it being the master. Nothing else about
running the app locally needs to be true.

After it prints a join code, everyone else joins through `/join/` in the browser
and never touches a command line.

Re-runnable: the team and the member are updated rather than duplicated, and an
existing member is promoted to lead rather than refused. It will not rotate a
join code that already exists -- rotating one silently would lock out everyone
mid-onboarding. Use the Team page for that, or `--rotate-code`.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from crm.models import Team, TeamMember, TeamMembership, TeamRole


class Command(BaseCommand):
    help = "Create the first team and its founding lead. Prints the join code."

    def add_arguments(self, parser):
        parser.add_argument(
            "email",
            help="The founding lead. Must be the Google account you can sign in as.",
        )
        parser.add_argument("--team", default="PIEDS Outreach", help="Team name.")
        parser.add_argument("--name", default="", help="Display name. Defaults from the email.")
        parser.add_argument(
            "--rotate-code",
            action="store_true",
            help="Issue a new join code even if the team already has one. "
                 "Kills the old one immediately.",
        )
        parser.add_argument(
            "--self-contact",
            action="store_true",
            help="Also create a contact addressed to this same person, assigned "
                 "to them, so the first live send lands in your own inbox.",
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        email = opts["email"].strip().lower()
        if "@" not in email:
            raise CommandError(f"{email!r} is not an email address.")

        # Not merely tidiness: sign-in checks Google's `hd` claim against
        # GOOGLE_OAUTH_HOSTED_DOMAIN, so a member seeded with a non-BITS address
        # can never sign in at all. Better to refuse here than to create a row
        # that looks right and locks somebody out.
        if not email.endswith("bits-pilani.ac.in"):
            raise CommandError(
                f"{email} is not a BITS address. Google sign-in checks the hosted "
                "domain, so this account could never sign in."
            )


        team, team_created = Team.objects.get_or_create(
            name=opts["team"],
            defaults={"join_code": Team.generate_join_code(), "is_active": True},
        )
        if not team_created and (opts["rotate_code"] or team.join_code == "ROTATE-ME"):
            team.join_code = Team.generate_join_code()
            team.save(update_fields=["join_code", "updated_at"])
        if not team.is_active:
            team.is_active = True
            team.save(update_fields=["is_active", "updated_at"])

        self.stdout.write(
            f"team     : {team.name} ({'created' if team_created else 'existing'})"
        )

        # `name` is only written when it was actually supplied, or when the
        # member is new. A re-run without --name must not overwrite a real name
        # with a slug derived from a roll-number address.
        member, member_created = TeamMember.objects.get_or_create(
            bits_email=email,
            defaults={
                "name": opts["name"] or email.split("@")[0].replace(".", " ").title(),
                "is_active": True,
            },
        )
        changed = []
        if opts["name"] and member.name != opts["name"]:
            member.name, _ = opts["name"], changed.append("name")
        if not member.is_active:
            member.is_active, _ = True, changed.append("is_active")
        if changed:
            member.save(update_fields=[*changed, "updated_at"])
        state = "created" if member_created else ("updated" if changed else "existing")
        self.stdout.write(f"lead     : {member.name} <{member.bits_email}> ({state})")

        membership, _ = TeamMembership.objects.update_or_create(
            team=team, member=member,
            defaults={"role": TeamRole.LEAD.value, "is_active": True},
        )
        self.stdout.write(f"role     : {membership.role} on {team.name}")

        if opts["self_contact"]:
            self._self_contact(member, member.name, email)

        self.stdout.write(self.style.SUCCESS(f"\njoin code: {team.join_code}\n"))
        self.stdout.write(
            "Read that out to the rest of the team. They sign in with their BITS\n"
            "Google account at /login/ and enter it at /join/. Rotate it from the\n"
            "Team page once everyone is in -- a code that has been screenshotted\n"
            "is a code anyone can use."
        )
        self.stdout.write(
            self.style.WARNING(
                "\nYou still have to connect Gmail at /settings/gmail/ before you "
                "can send anything."
            )
        )

    def _self_contact(self, member, name, email):
        """A prospect who is you, so the first live send is provably safe."""
        from crm.models import Contact
        from shared.enums import ContactLifecycle

        parts = name.split()
        contact, _ = Contact.objects.update_or_create(
            email=email,
            defaults={
                "first_name": parts[0],
                "last_name": " ".join(parts[1:]) or "Test",
                "company": "PIEDS",
                "designation": "Test Recipient",
                "assigned_to": member,
                "created_by": member,
                "tags": ["test"],
                # Explicitly reset: a re-run after a successful send would
                # otherwise leave it 'contacted' and the next test send would be
                # refused as already mailed -- which is the constraint working,
                # but looks like a broken command.
                "lifecycle": ContactLifecycle.NEW.value,
                "is_archived": False,
            },
        )
        self.stdout.write(f"contact  : {contact.full_name} <{contact.email}> assigned to self")
