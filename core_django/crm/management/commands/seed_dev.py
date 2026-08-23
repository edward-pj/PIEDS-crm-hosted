"""Populate a dev database with realistic-looking data.

Idempotent: re-running updates rather than duplicating, so it is safe to call
repeatedly while iterating.
"""

import random

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from crm.models import Campaign, Contact, Team, TeamMember, TeamMembership
from shared.enums import CampaignStatus, ContactLifecycle

#: Seeding writes fixture contacts and an active campaign. Doing that to the
#: database the whole team shares would put invented prospects in a real pool --
#: and `update_or_create` on `title` means it would happily overwrite a live
#: campaign's template. The entrypoint decides this too; the check lives here as
#: well because a person typing the command by hand deserves the same guard.
LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1", "postgres"}

#: (name, email, batch, phone, role). The batch is a display field now; the
#: role is what grants anything.
MEMBERS = [
    ("Aarav Sharma", "aarav@pilani.bits-pilani.ac.in", "2024", "9812345670", "lead"),
    ("Diya Menon", "diya@pilani.bits-pilani.ac.in", "2024", "9812345671", "lead"),
    ("Kabir Rao", "kabir@pilani.bits-pilani.ac.in", "2025", "9812345672", "member"),
    ("Ishita Nair", "ishita@pilani.bits-pilani.ac.in", "2025", "9812345673", "member"),
]

SEED_TEAM_NAME = "PIEDS Outreach"
SEED_JOIN_CODE = "DEVCODE123"

COMPANIES = [
    "Zerodha", "Razorpay", "Postman", "Freshworks", "Zoho", "CRED", "Groww",
    "Meesho", "Innovaccer", "Darwinbox", "Chargebee", "BrowserStack",
]
DESIGNATIONS = ["Founder", "CTO", "VP Engineering", "Head of Partnerships", "Director, Strategy"]
FIRST = ["Rohan", "Ananya", "Vikram", "Sneha", "Arjun", "Meera", "Nikhil", "Priya", "Rahul", "Tara"]
LAST = ["Iyer", "Kapoor", "Reddy", "Bose", "Desai", "Malhotra", "Pillai", "Chawla"]
TAGS = ["fintech", "saas", "deeptech", "priority", "warm-intro", "iit-b", "alum"]


class Command(BaseCommand):
    help = "Seed the dev database with team members, contacts and a campaign."

    def add_arguments(self, parser):
        parser.add_argument("--contacts", type=int, default=50)
        parser.add_argument(
            "--force",
            action="store_true",
            help="Seed even when the database is not local. Almost never right.",
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        host = (settings.DATABASES["default"].get("HOST") or "").lower()
        if host not in LOCAL_HOSTS and not opts["force"]:
            raise CommandError(
                f"Refusing to seed {host!r} -- that is not a local database. "
                f"Seeding writes fixture contacts and overwrites the campaign "
                f"template of the same title. Pass --force if you are certain."
            )

        random.seed(42)

        members = []
        team, _ = Team.objects.update_or_create(
            name=SEED_TEAM_NAME,
            defaults={"join_code": SEED_JOIN_CODE, "is_active": True},
        )

        for name, email, batch, phone, role in MEMBERS:
            member, _ = TeamMember.objects.update_or_create(
                bits_email=email,
                defaults={"name": name, "batch": batch, "phone": phone},
            )
            TeamMembership.objects.update_or_create(
                team=team, member=member,
                defaults={"role": role, "is_active": True},
            )
            members.append(member)

        # No Django Users: everyone signs in with Google, and a new person joins
        # with the team's code. `manage.py createsuperuser` is for /admin/ only.
        leads = [name for name, _e, _b, _p, role in MEMBERS if role == "lead"]
        self.stdout.write(
            f"team members: {len(members)} on {team.name!r} — "
            f"leads: {', '.join(leads)}"
        )
        self.stdout.write(f"join code: {team.join_code}")

        assignees = [m for m in members if m.batch == "2025"]
        created = 0
        for i in range(opts["contacts"]):
            first, last = random.choice(FIRST), random.choice(LAST)
            email = f"{first.lower()}.{last.lower()}{i}@example.com"
            _, was_new = Contact.objects.update_or_create(
                email=email,
                defaults={
                    "first_name": first,
                    "last_name": last,
                    "company": random.choice(COMPANIES),
                    "designation": random.choice(DESIGNATIONS),
                    # Leave a third unassigned so the assignment screen has work to do.
                    "assigned_to": random.choice(assignees + [None]),
                    "tags": random.sample(TAGS, random.randint(0, 2)),
                    # A couple of blocked contacts so the "refuses to mail"
                    # path is visible without having to set one up by hand.
                    "lifecycle": random.choices(
                        [ContactLifecycle.NEW.value,
                         ContactLifecycle.REPLIED.value,
                         ContactLifecycle.DO_NOT_CONTACT.value],
                        weights=[88, 8, 4],
                    )[0],
                },
            )
            created += was_new
        self.stdout.write(f"contacts: {created} new, {Contact.objects.count()} total")

        campaign, _ = Campaign.objects.update_or_create(
            title="PIEDS Incubation Outreach — Spring",
            defaults={
                "mail_sub": "{{ company }} x PIEDS, BITS Pilani",
                "mail_body": (
                    "Hi {{ first_name }},\n\n"
                    "I'm reaching out from PIEDS, the technology business incubator at "
                    "BITS Pilani. Given your work as {{ designation }} at {{ company }}, "
                    "I thought there might be a good fit worth exploring.\n\n"
                    "Would you be open to a short call next week?\n\n"
                    "Best,\nPIEDS Team"
                ),
                "var_list": ["first_name", "company", "designation"],
                "status": CampaignStatus.ACTIVE.value,
                "created_by": members[0],
            },
        )
        self.stdout.write(self.style.SUCCESS(f"campaign ready: {campaign.title}"))
