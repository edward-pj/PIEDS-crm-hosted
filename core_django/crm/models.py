"""Canonical schema for Ignite CRM.

This module is the SINGLE SOURCE OF TRUTH for the database, and Django is the
only process that ever connects to it. The FastAPI local agent holds no models
and no credentials -- it reaches this data exclusively over the HTTP API in
`crm/api/`, which is what lets members run it on their own laptops.
"""

import hashlib
import secrets
import uuid

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.utils import timezone

from shared.enums import (
    BLOCKED_LIFECYCLES,
    TERMINAL_SCHEDULE_STATUSES,
    CampaignStatus,
    ContactLifecycle,
    MailingStatus,
    ScheduleStatus,
)

from .validators import batch_validator, phone_validator, validate_bits_email


class TimeStampedModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class TeamMember(TimeStampedModel):
    """A PIEDS team member who sends mail from their own Gmail account."""

    name = models.CharField(max_length=120)
    bits_email = models.EmailField(
        unique=True,
        validators=[validate_bits_email],
        help_text="Must match the Gmail account used by the local sending agent.",
    )
    phone = models.CharField(max_length=10, validators=[phone_validator], blank=True)
    linkedin = models.URLField(blank=True)
    sender_name = models.CharField(
        max_length=120,
        blank=True,
        help_text=(
            "Shown as the sender in the recipient's inbox. Blank uses the member's name. "
            "A bare address in a cold mail is far less likely to be opened."
        ),
    )
    batch = models.CharField(
        max_length=4,
        validators=[batch_validator],
        db_index=True,
        help_text="Admission year. Drives permissions -- see services/permissions.py.",
    )
    is_active = models.BooleanField(default=True)

    # Links this member to a Django login for the central admin UI. Null for
    # members who only ever run the local agent and never log into Django.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="team_member",
    )

    class Meta:
        db_table = "team_members"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.batch})"

    @property
    def display_name(self) -> str:
        """What a recipient sees in the From line."""
        return self.sender_name or self.name

    @property
    def gmail_connected(self) -> bool:
        """Whether this member has a live Gmail grant.

        A reverse OneToOne raises rather than returning None when absent, and
        "has never connected" is the common case, so the exception is the normal
        path here -- not an error worth logging.
        """
        try:
            return self.gmail_credential.is_usable
        except GmailCredential.DoesNotExist:
            return False


class Team(TimeStampedModel):
    """A cohort that works one contact pool together.

    Replaces the admission year as the unit of permission. `LEAD_BATCH = "2024"`
    was the entire permission system: a hard-coded literal that had to be edited
    every year, could not express "lead of this campaign but not that one", and
    said nothing at all about which members belong together.

    The join code is what makes a team self-serve. A lead creates the team, reads
    the code out once, and everyone else is in -- with no lead needed at a
    keyboard to add each person, and no password-free door on a public hostname.
    """

    name = models.CharField(max_length=120)

    #: Handed out once, verbally or in a group chat, then rotated. Unique and
    #: indexed because /join/ looks a member up by it on every attempt.
    join_code = models.CharField(max_length=32, unique=True, db_index=True)

    #: Seeded into each new sub-campaign's footer, so a member who never edits
    #: theirs still signs off as somebody rather than as nobody.
    default_footer = models.TextField(blank=True)

    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        TeamMember, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="teams_created",
    )

    class Meta:
        db_table = "teams"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @staticmethod
    def generate_join_code() -> str:
        """A code that survives being read aloud.

        `secrets`, not `random`: this is the only credential standing between a
        BITS address and the contact pool.

        The alphabet omits O/0, I/1/L and U/V -- a code dictated across a room
        and typed back wrong is a support request, and every ambiguous pair
        removed is one fewer. 10 characters from a 28-symbol alphabet is ~48
        bits, which is not guessable at the rate /join/ permits.
        """
        alphabet = "ABCDEFGHJKMNPQRSTWXYZ23456789"
        return "".join(secrets.choice(alphabet) for _ in range(10))


class TeamRole(models.TextChoices):
    LEAD = "lead", "Lead"
    MEMBER = "member", "Member"


class TeamMembership(TimeStampedModel):
    """Who is on a team, and what they may do there.

    The role lives here rather than on TeamMember because it is a fact about a
    person *in a team*, not about the person. Someone can lead next year's
    cohort while still being an ordinary member of this one -- which the batch
    field could never express.
    """

    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="memberships")
    member = models.ForeignKey(
        TeamMember, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(
        max_length=8, choices=TeamRole.choices, default=TeamRole.MEMBER,
        db_index=True,
    )
    joined_at = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "team_memberships"
        ordering = ["team", "member"]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "member"], name="uniq_team_member"
            ),
        ]

    def __str__(self):
        return f"{self.member.name} — {self.role} of {self.team.name}"


class GmailCredential(TimeStampedModel):
    """A member's authorisation for this server to send mail as them.

    This is the single biggest change to the security model in the codebase.
    Previously the CRM deliberately held no Gmail credentials -- the sending
    agent ran on the member's own laptop, and the server therefore *could not*
    impersonate anyone even if it were compromised. Hosting gives that up: a
    member logs in, sends, and closes the tab, so something awake must hold
    their authorisation.

    What replaces the old guarantee:

    - The refresh token is encrypted at rest (see services/secrets.py), so a
      database dump on its own yields nothing usable.
    - `google_email` records which Google account actually granted this, checked
      against the member's `bits_email` at grant time and re-verified against
      Gmail's own `getProfile` before the first send after any credential
      change. `CampaignMailing.sent_by` is only honest because of that check.
    - A member can revoke this from their own Google account page at any time,
      with no cooperation from us. When they do, the refresh fails, `last_error`
      is recorded, and the CRM says "Gmail not connected" instead of silently
      dropping their mail on the floor.

    One row per member: a second Gmail account for the same person would make
    `sent_by` ambiguous, which is the one thing that must never happen.
    """

    member = models.OneToOneField(
        TeamMember, on_delete=models.CASCADE, related_name="gmail_credential"
    )

    #: The account Google says granted this. NOT assumed equal to bits_email --
    #: it is compared with it, and a mismatch refuses the grant.
    google_email = models.EmailField()

    #: Fernet ciphertext. Never log, never render, never put in an API response.
    refresh_token_encrypted = models.BinaryField()
    key_version = models.PositiveSmallIntegerField(
        help_text="Which GMAIL_TOKEN_KEY version encrypted this row."
    )

    #: Cached so a send does not pay a round trip to Google per message.
    #: `Credentials(token=None)` is never `valid`, so without this every single
    #: mail refreshes first -- which inside a bounded request budget is felt
    #: immediately. Encrypted too: it is a live credential for its lifetime.
    access_token_encrypted = models.BinaryField(null=True, blank=True)
    access_token_key_version = models.PositiveSmallIntegerField(null=True, blank=True)
    access_token_expires_at = models.DateTimeField(null=True, blank=True)

    #: What Google actually granted, which is not always what we asked for --
    #: a member can untick a scope on the consent screen. Checked before a send
    #: rather than discovered as a 403 halfway through a batch.
    granted_scopes = models.JSONField(default=list, blank=True)

    granted_at = models.DateTimeField(default=timezone.now)
    last_refreshed_at = models.DateTimeField(null=True, blank=True)

    #: When `getProfile` last confirmed the token really belongs to
    #: `google_email`. Cleared whenever the credential changes.
    identity_verified_at = models.DateTimeField(null=True, blank=True)

    #: The last refusal from Google, kept so the member is told *why* they need
    #: to reconnect. An empty string means the credential is believed good.
    last_error = models.TextField(blank=True)

    #: Set when the member disconnects, or when a refresh proves the grant is
    #: dead. The row is kept rather than deleted: "connected once, then revoked"
    #: and "never connected" are different facts when mail stops going out.
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "gmail_credentials"

    def __str__(self):
        state = "revoked" if self.revoked_at else "active"
        return f"{self.google_email} ({state})"

    @property
    def is_usable(self) -> bool:
        return self.revoked_at is None and bool(self.refresh_token_encrypted)

    def has_scopes(self, required) -> bool:
        return set(required).issubset(set(self.granted_scopes or []))


class ApiToken(TimeStampedModel):
    """Bearer token letting a member's local agent talk to this server.

    Only the hash is stored. The plaintext is shown once at creation and is
    unrecoverable afterwards -- a leaked database should not hand an attacker
    working credentials for every member's agent.
    """

    member = models.ForeignKey(TeamMember, on_delete=models.CASCADE, related_name="api_tokens")
    label = models.CharField(max_length=80, blank=True, help_text="e.g. 'Aarav's MacBook'")
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    key_prefix = models.CharField(max_length=8, help_text="First chars, shown in the UI.")
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "api_tokens"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.key_prefix}… ({self.member.name})"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and self.member.is_active

    @staticmethod
    def hash_key(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def issue(cls, member, label: str = "") -> tuple["ApiToken", str]:
        """Create a token. Returns (token, plaintext) -- show plaintext once."""
        raw = secrets.token_urlsafe(32)
        token = cls.objects.create(
            member=member, label=label, key_hash=cls.hash_key(raw), key_prefix=raw[:8]
        )
        return token, raw

    def revoke(self):
        self.revoked_at = timezone.now()
        self.save(update_fields=["revoked_at", "updated_at"])


class Contact(TimeStampedModel):
    """A prospect. Owned by at most one team member at a time."""

    first_name = models.CharField(max_length=80)
    last_name = models.CharField(max_length=80, blank=True)
    email = models.EmailField(
        unique=True,
        help_text="Unique -- this is the dedupe key for CSV import.",
    )
    phone_no = models.CharField(max_length=10, validators=[phone_validator], blank=True)
    linkedin = models.URLField(blank=True)
    company = models.CharField(max_length=160, db_index=True)
    designation = models.CharField(max_length=160, blank=True)

    assigned_to = models.ForeignKey(
        TeamMember,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_contacts",
    )
    assigned_at = models.DateTimeField(null=True, blank=True)

    last_contacted_by = models.ForeignKey(
        TeamMember,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    last_contacted_at = models.DateTimeField(null=True, blank=True)

    # --- funnel state -----------------------------------------------------
    # SERVER-OWNED. Only services/mailing.py and a lead may write this; the
    # contact form drops the field entirely for non-leads.
    lifecycle = models.CharField(
        max_length=16,
        choices=ContactLifecycle.choices(),
        default=ContactLifecycle.NEW.value,
        db_index=True,
        help_text="Flips to 'contacted' automatically on the first sent mail.",
    )

    # Free-form, editable by a lead or by the member the contact is assigned to.
    # A real Postgres array, so `tags__contains=["fintech"]` uses the GIN index.
    tags = ArrayField(
        models.CharField(max_length=40),
        default=list,
        blank=True,
        help_text="Free-form labels, e.g. fintech, priority, iit-b.",
    )

    # Archive rather than delete: CampaignMailing.contact is PROTECT, so any
    # contact that has ever been mailed cannot be removed from the table at all.
    is_archived = models.BooleanField(default=False, db_index=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.ForeignKey(
        TeamMember, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    created_by = models.ForeignKey(
        TeamMember, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "contacts"
        ordering = ["company", "first_name"]
        indexes = [
            models.Index(fields=["assigned_to", "company"]),
            models.Index(fields=["is_archived", "lifecycle"]),
            GinIndex(fields=["tags"], name="contacts_tags_gin"),
        ]

    def __str__(self):
        return f"{self.full_name} @ {self.company}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_mailable(self) -> bool:
        """Whether this contact may receive mail at all.

        Advisory only -- the binding check is in services/mailing.py::claim_batch,
        which re-tests this under a row lock. Use this for UI, never for safety.
        """
        return not self.is_archived and self.lifecycle not in BLOCKED_LIFECYCLES


class ContactNote(TimeStampedModel):
    """Append-only note on a contact.

    A separate table rather than a text column so we keep who wrote what and
    when -- on a shared contact pool that history is the whole point.
    """

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="notes")
    author = models.ForeignKey(TeamMember, on_delete=models.SET_NULL, null=True, related_name="+")
    body = models.TextField()

    class Meta:
        db_table = "contact_notes"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Note on {self.contact_id} by {self.author_id}"


class ContactAudit(TimeStampedModel):
    """One row per field changed on a contact.

    Fifteen people now edit one shared pool from two different apps. Without
    this, "who changed this company name and when" is unanswerable, and a bad
    bulk edit is impossible to unpick. Written only by services/contacts.py, so
    a new view cannot mutate a contact without leaving a trace.
    """

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="audits")
    actor = models.ForeignKey(TeamMember, on_delete=models.SET_NULL, null=True, related_name="+")
    field = models.CharField(max_length=40)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)

    class Meta:
        db_table = "contact_audits"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["contact", "-created_at"])]

    def __str__(self):
        return f"{self.field}: {self.old_value!r} -> {self.new_value!r}"


class Campaign(TimeStampedModel):
    """A reusable mail template plus its lifecycle state.

    Status transitions are NOT enforced here -- see services/campaigns.py.

    **Campaigns are exactly two levels deep.** A *root* campaign owns the
    subject, the body and the status -- what the team is saying. A *sub-campaign*
    belongs to one member, has one root, and owns exactly one thing: that
    member's footer. Nothing else about a sub-campaign is editable, because
    everything else is the team's message rather than the sender's.

    The reason is a bug this shape exists to make impossible. Each member used
    to get their own top-level campaign so they could have their own footer, and
    `uniq_campaign_contact` is scoped to ONE campaign -- so once Aarav had mailed
    a company under his, Kabir's campaign had no row for that contact and he
    could mail them again under a different banner. Rooting the sub-campaigns
    and adding `uniq_root_campaign_contact` makes the guarantee team-wide, which
    is what it always should have been.

    Depth is exactly two, never a tree: a root has no parent, a sub-campaign's
    parent is always a root. Three levels would make "which footer applies"
    ambiguous, and the constraint below enforces it.
    """

    title = models.CharField(max_length=200)
    mail_sub = models.CharField(max_length=300, help_text="Supports {{ variable }} placeholders.")
    mail_body = models.TextField(help_text="Supports {{ variable }} placeholders.")
    #: Opt-in, not automatic. Turning this on for every campaign would break
    #: any existing body whose prose contains a bare `<` or `&`.
    is_html = models.BooleanField(
        default=False,
        help_text="Write raw HTML in the body -- footers, styling, dividers.",
    )
    var_list = models.JSONField(
        default=list,
        blank=True,
        help_text='Declared variables, e.g. ["first_name", "company", "designation"].',
    )
    status = models.CharField(
        max_length=16,
        choices=CampaignStatus.choices(),
        default=CampaignStatus.DRAFT.value,
        db_index=True,
    )
    created_by = models.ForeignKey(
        TeamMember, on_delete=models.SET_NULL, null=True, related_name="campaigns"
    )

    #: Which team's campaign this is. Only meaningful on a root; a sub-campaign
    #: inherits it. Nullable because campaigns predate teams.
    team = models.ForeignKey(
        "Team", on_delete=models.PROTECT, null=True, blank=True,
        related_name="campaigns",
    )

    #: NULL for a root campaign. PROTECT because deleting a root out from under
    #: its sub-campaigns would orphan every mailing rooted to it.
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True,
        related_name="sub_campaigns",
        help_text="The root campaign this personalises. Blank for a root campaign.",
    )

    #: Whose sub-campaign this is. NULL on a root: a root belongs to the team.
    owner = models.ForeignKey(
        TeamMember, on_delete=models.PROTECT, null=True, blank=True,
        related_name="sub_campaigns",
    )

    #: The ONLY field a sub-campaign owner may edit. Appended to the root's body
    #: at render time; see services/render.py.
    footer = models.TextField(
        blank=True,
        help_text="Your sign-off. Appended to the campaign body in your mail only.",
    )

    #: Independent of the root's `is_html` -- a plain-text body can carry a
    #: styled footer and vice versa, which is why render.py converts the two
    #: parts separately. Lead-only in the form: richtext's no-sanitiser design
    #: rests on raw HTML being written only by leads.
    footer_is_html = models.BooleanField(
        default=False,
        help_text="Write the footer as raw HTML. Leads only.",
    )

    class Meta:
        db_table = "campaigns"
        ordering = ["-created_at"]
        constraints = [
            # One sub-campaign per member per root. Two would make "which
            # footer" ambiguous and would let one member hold two claims on the
            # same contact.
            models.UniqueConstraint(
                fields=["parent", "owner"],
                name="uniq_parent_owner",
                condition=models.Q(parent__isnull=False),
            ),
            # A sub-campaign has an owner; a root does not. Without this a root
            # could acquire an owner and quietly start behaving like a
            # sub-campaign of nothing.
            models.CheckConstraint(
                condition=(
                    models.Q(parent__isnull=True, owner__isnull=True)
                    | models.Q(parent__isnull=False, owner__isnull=False)
                ),
                name="campaign_parent_and_owner_agree",
            ),
        ]

    def __str__(self):
        return f"{self.title} [{self.status}]"

    @property
    def root(self):
        """The campaign that owns the message. Itself, if it is a root."""
        return self.parent or self

    @property
    def is_root(self) -> bool:
        return self.parent_id is None


class CampaignMailing(TimeStampedModel):
    """One mail, to one contact, for one campaign. The unit of idempotency."""

    campaign = models.ForeignKey(Campaign, on_delete=models.PROTECT, related_name="mailings")

    #: The root of `campaign`, denormalised so the database can enforce
    #: uniqueness across every member's sub-campaign at once. Set automatically
    #: in save(); no caller passes it.
    #:
    #: NOT `related_name="mailings"` -- that belongs to `campaign` above, and
    #: every funnel aggregate in views.py depends on it meaning that.
    #: NOT NULL is load-bearing, not tidiness -- see the constraint below.
    root_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="root_mailings",
        db_index=True,
    )

    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name="mailings")
    sent_by = models.ForeignKey(TeamMember, on_delete=models.PROTECT, related_name="mailings")

    mail_thread_id = models.CharField(max_length=120, blank=True)
    mail_message_id = models.CharField(max_length=120, blank=True)

    status = models.CharField(
        max_length=8,
        choices=MailingStatus.choices(),
        default=MailingStatus.DRAFT.value,
    )

    # Snapshot of what actually went out. Campaign templates change over time;
    # without this we could never answer "what did we actually send this person".
    rendered_subject = models.TextField(blank=True)
    rendered_body = models.TextField(blank=True)
    # The HTML alternative, stored because it -- not rendered_body -- is what
    # most recipients see. Blank for mailings sent before HTML bodies existed.
    rendered_body_html = models.TextField(blank=True)

    # The rest of the envelope, snapshotted for the same reason as the body:
    # "who did we actually copy on this" must be answerable months later, and a
    # member's sender name can change between one send and the next.
    from_name = models.CharField(max_length=120, blank=True)
    cc = models.CharField(max_length=500, blank=True)
    bcc = models.CharField(max_length=500, blank=True)

    error_detail = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    # Follow-up bookkeeping. `replied_at` is set only when a reply is actually
    # seen in the Gmail thread; `followed_up_at` stops a rule queueing the same
    # follow-up twice on successive scans.
    replied_at = models.DateTimeField(null=True, blank=True)
    reply_checked_at = models.DateTimeField(null=True, blank=True)
    followed_up_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "campaign_mailings"
        ordering = ["-created_at"]
        constraints = [
            # THE idempotency guarantee, now team-wide. One mail per contact per
            # ROOT campaign, so Aarav's sub-campaign and Kabir's cannot both
            # reach the same prospect under the same banner.
            #
            # This is the constraint that fixes the duplicate-mail bug. It is
            # only worth anything because root_campaign is NOT NULL: Postgres
            # unique indexes treat NULLs as distinct, so two rows with a null
            # root and the same contact would BOTH insert. See check_db.
            models.UniqueConstraint(
                fields=["root_campaign", "contact"],
                name="uniq_root_campaign_contact",
            ),
            # Implied by the one above -- a collision here is a collision there
            # too -- but kept deliberately. It is what check_db has always
            # asserted and what test_constraints.py pins, and a redundant index
            # is far cheaper than a weakened guarantee.
            models.UniqueConstraint(
                fields=["campaign", "contact"], name="uniq_campaign_contact"
            ),
        ]
        indexes = [
            models.Index(fields=["campaign", "status"]),
            models.Index(fields=["sent_by", "sent_at"]),
        ]

    def save(self, *args, **kwargs):
        """Derive `root_campaign` rather than trusting the caller.

        Every call site would otherwise have to remember, and forgetting would
        not fail -- it would insert a NULL root, which the unique index ignores.
        A silently unprotected row is exactly the failure this field exists to
        prevent, so it is not something to leave to discipline.
        """
        if self.root_campaign_id is None and self.campaign_id is not None:
            self.root_campaign_id = self.campaign.parent_id or self.campaign_id
            if "update_fields" in kwargs and kwargs["update_fields"] is not None:
                kwargs["update_fields"] = list(kwargs["update_fields"]) + ["root_campaign"]
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.campaign_id} -> {self.contact_id} [{self.status}]"


class ScheduledSend(TimeStampedModel):
    """A campaign send lined up for later.

    Gmail has no server-side scheduling -- there is no `sendAt` on the API -- so
    this row is not a promise to Google, it is a note to ourselves that some
    agent must pick up at the right moment. See docs/MAIL_SCHEDULING.md.

    It stores WHEN and FOR WHOM. It stores nothing about how a mail is built:
    that stays in services/render.py and is resolved at execution time against
    whatever the campaign and the contact say then, not what they said when the
    job was created. Scheduling a send is not a way to freeze a template.
    """

    campaign = models.ForeignKey(Campaign, on_delete=models.PROTECT, related_name="schedules")

    # Whose Gmail sends this, and therefore the only agent permitted to execute
    # it. An agent authenticates as exactly one member; letting it run someone
    # else's job would send from the wrong mailbox and record a false sent_by.
    member = models.ForeignKey(TeamMember, on_delete=models.PROTECT, related_name="schedules")
    created_by = models.ForeignKey(
        TeamMember, on_delete=models.SET_NULL, null=True, related_name="+"
    )

    #: Snapshot of the selection. Deliberately ids and not a M2M: this is a
    #: record of intent at scheduling time, and claim_batch re-checks every one
    #: of them under a lock anyway (assignment, archived, lifecycle, cap).
    contact_ids = ArrayField(models.UUIDField(), default=list)

    #: Index of the next contact to attempt. Progress is a cursor rather than
    #: "contacts that still lack a mailing" because a permanently skipped
    #: contact -- unassigned, archived, do_not_contact -- never gets a mailing
    #: row at all, and that query would leave the job running forever.
    cursor = models.PositiveIntegerField(default=0)

    scheduled_at = models.DateTimeField(db_index=True)

    # --- drip ------------------------------------------------------------
    # Zero batch_size means "the whole thing at once", which is the one-off
    # send. Anything else spreads the job across ticks: 200 mails leaving one
    # mailbox in one minute is both a deliverability signal and an unrecoverable
    # mistake if the template was wrong.
    batch_size = models.PositiveIntegerField(
        default=0, help_text="Contacts per run. 0 sends the whole selection at once."
    )
    interval_minutes = models.PositiveIntegerField(
        default=0, help_text="Wait between batches. Ignored unless batch_size is set."
    )
    #: When the next slice may go. Set after each batch; null means "now".
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    status = models.CharField(
        max_length=12,
        choices=ScheduleStatus.choices(),
        default=ScheduleStatus.PENDING.value,
        db_index=True,
    )

    # Same envelope the manual send path uses; validated by
    # services/mailing.py::parse_copy_addresses before it ever lands here.
    cc = models.CharField(max_length=500, blank=True)
    bcc = models.CharField(max_length=500, blank=True)

    # The lease. An expired lease on a RUNNING job means its executor died
    # mid-batch; a sweep returns it to PENDING. This is a scheduling
    # convenience, NOT the safety mechanism -- uniq_campaign_contact is what
    # actually makes a double execution harmless.
    leased_by = models.CharField(max_length=80, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)

    sent_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    attempts = models.PositiveIntegerField(default=0)

    last_error = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "scheduled_sends"
        ordering = ["scheduled_at"]
        indexes = [
            # The due query, run every 60s by every agent.
            models.Index(fields=["status", "scheduled_at"]),
            models.Index(fields=["member", "status"]),
        ]

    def __str__(self):
        return f"{self.campaign_id} x{len(self.contact_ids)} @ {self.scheduled_at:%Y-%m-%d %H:%M} [{self.status}]"

    @property
    def total(self) -> int:
        return len(self.contact_ids)

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.cursor)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_SCHEDULE_STATUSES

    def next_slice(self, size=None) -> list:
        """The contacts to attempt on this tick.

        `size=None` means the whole remainder, which is what a one-off send
        wants. Phase 4 passes a batch size to drip instead.
        """
        end = self.total if size is None else min(self.total, self.cursor + size)
        return [str(cid) for cid in self.contact_ids[self.cursor:end]]


class FollowUpRule(TimeStampedModel):
    """Send a second campaign to whoever did not reply to the first.

    Reply detection is real evidence, not inference: every sent mailing records
    its Gmail thread id, and the agent asks Gmail whether that thread gained a
    message from the contact. Compare ContactLifecycle, where guessing "bounced"
    from an SMTP string was deliberately refused -- this is the opposite case,
    an observed fact rather than a reading of tea leaves.
    """

    campaign = models.ForeignKey(
        Campaign, on_delete=models.CASCADE, related_name="follow_up_rules",
        help_text="The campaign whose silence we are following up on.",
    )
    follow_up = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="follows_from",
        help_text="What to send them instead.",
    )
    delay_days = models.PositiveIntegerField(
        default=3, help_text="Days of silence before the follow-up is queued."
    )
    is_active = models.BooleanField(default=True)

    #: Off by default and per rule, because it moves a contact through the
    #: funnel automatically. shared/enums.py documents NEW -> CONTACTED as the
    #: only automatic transition; this is the second, and it is opt-in so that
    #: rule stays a decision rather than an accident.
    mark_replied = models.BooleanField(
        default=False,
        help_text="Also set the contact's lifecycle to Replied when a reply is seen.",
    )

    created_by = models.ForeignKey(
        TeamMember, on_delete=models.SET_NULL, null=True, related_name="+"
    )

    class Meta:
        db_table = "follow_up_rules"
        ordering = ["-created_at"]
        constraints = [
            # One rule per pair: two rules chaining the same campaigns would
            # queue the same follow-up twice.
            models.UniqueConstraint(
                fields=["campaign", "follow_up"], name="uniq_followup_pair"
            ),
        ]

    def __str__(self):
        return f"{self.campaign_id} -> {self.follow_up_id} after {self.delay_days}d"

    def clean(self):
        """A follow-up campaign must be a SIBLING root, never a child.

        Follow-ups work by mailing the same contact under a second campaign.
        `uniq_root_campaign_contact` forbids that within one root -- so a
        follow-up sharing a root with the campaign it chases can never queue
        anything: every job it creates would be 100% skipped, silently, forever.

        Making them sibling roots also earns something. "Ignite" and "Ignite
        follow-up" are two roots, so follow-up dedupe is team-wide for free.
        """
        from django.core.exceptions import ValidationError

        if self.campaign_id and self.campaign_id == self.follow_up_id:
            raise ValidationError("A campaign cannot follow up on itself.")

        if self.campaign_id and self.follow_up_id:
            if self.campaign.root.id == self.follow_up.root.id:
                raise ValidationError(
                    "A follow-up must be a separate root campaign, not part of "
                    "the same one. One contact may only be mailed once per root "
                    "campaign, so a follow-up rooted here could never send "
                    "anything."
                )
