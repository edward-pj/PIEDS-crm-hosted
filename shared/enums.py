"""Status vocabularies shared by core_django and local_agent.

These strings are persisted in Postgres, so both apps MUST agree on them
character for character. Defining them once here is the only thing that
prevents the Django models and the SQLAlchemy mirror from drifting.

Plain `str` subclasses so `CampaignStatus.ACTIVE == "active"` is True and the
values drop straight into Django `choices` and SQLAlchemy comparisons.
"""

from enum import Enum


class CampaignStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"

    @classmethod
    def choices(cls):
        return [(m.value, m.name.title()) for m in cls]


class MailingStatus(str, Enum):
    DRAFT = "draft"
    SENT = "sent"
    FAILED = "failed"

    @classmethod
    def choices(cls):
        return [(m.value, m.name.title()) for m in cls]


class ScheduleStatus(str, Enum):
    """Where a scheduled send stands.

    The states exist because Gmail has no server-side scheduling: there is no
    `sendAt`, so a future send needs a process awake at that moment holding the
    member's Gmail token. Everything that can go wrong with "was anything
    listening at 9am?" has to be representable, and visibly so -- a scheduled
    mail that quietly never went is worse than one that failed loudly.

    PENDING -> RUNNING -> DONE is the happy path. HELD and MISSED are the two
    that earn their keep: HELD says "due, but not allowed to run yet" (the
    campaign gained the emergency brake, or we are inside quiet hours), and
    MISSED says "nothing executed this before its grace window closed" rather
    than mailing prospects at three in the morning.
    """

    PENDING = "pending"
    RUNNING = "running"
    HELD = "held"
    DONE = "done"
    CANCELLED = "cancelled"
    MISSED = "missed"
    FAILED = "failed"

    @classmethod
    def choices(cls):
        return [(m.value, m.name.title()) for m in cls]


class ContactLifecycle(str, Enum):
    """Where a prospect stands in the outreach funnel.

    NEW -> CONTACTED is applied automatically by services/mailing.py::record_result
    the moment a mail is confirmed sent.

    CONTACTED -> REPLIED is the only other automatic transition, and it is
    OPT-IN per follow-up rule (FollowUpRule.mark_replied, off by default). The
    distinction that earns it: a reply sitting in the Gmail thread is something
    we observed, not something we inferred. Everything else is still set by hand
    on purpose -- reading "bounced" out of an SMTP error string is guesswork we
    would later have to un-guess.

    Neither automatic transition may move a contact backwards, and neither may
    override a state a human chose: DO_NOT_CONTACT and BOUNCED always win.
    """

    NEW = "new"
    CONTACTED = "contacted"
    REPLIED = "replied"
    BOUNCED = "bounced"
    DO_NOT_CONTACT = "do_not_contact"

    @classmethod
    def choices(cls):
        return [(m.value, m.name.replace("_", " ").title()) for m in cls]


#: Campaigns may only be mailed from in this state. Enforced by the local agent
#: before any send, and by the service layer on transition.
SENDABLE_CAMPAIGN_STATUSES = frozenset({CampaignStatus.ACTIVE})

#: Lifecycle states that must NEVER receive mail. Checked in claim_batch, so a
#: contact in one of these is refused at the point of reservation -- not merely
#: hidden from a list somewhere.
BLOCKED_LIFECYCLES = frozenset(
    {ContactLifecycle.DO_NOT_CONTACT.value, ContactLifecycle.BOUNCED.value}
)

#: A scheduled send in one of these is finished with; nothing will execute it
#: again. Anything else is still the scheduler's business.
TERMINAL_SCHEDULE_STATUSES = frozenset({
    ScheduleStatus.DONE.value,
    ScheduleStatus.CANCELLED.value,
    ScheduleStatus.MISSED.value,
    ScheduleStatus.FAILED.value,
})

# NOTE: `LEAD_BATCH = "2024"` used to live here and was the entire permission
# system. It is gone. A lead is now someone with the `lead` role on an active
# team -- see crm/models.py::TeamMembership and services/permissions.py::is_lead.
#
# Left as a comment rather than deleted silently: a literal that decided who
# could touch the contact pool is worth a headstone, and anyone grepping for it
# after reading an old commit should land here rather than on nothing.
