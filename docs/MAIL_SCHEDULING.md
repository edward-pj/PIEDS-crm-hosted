# Scheduled sending — developer guide

Branch: `mail_schedule`. Built in six phases, each independently shippable and testable.

## Table of contents

1. [Why this is harder than it looks](#1-why-this-is-harder-than-it-looks)
2. [Architecture](#2-architecture)
3. [The state machine](#3-the-state-machine)
4. [The lease protocol](#4-the-lease-protocol)
5. [Timing arithmetic](#5-timing-arithmetic)
6. [Phase 0 — branch and vocabulary](#phase-0--branch-and-vocabulary)
7. [Phase 1 — one-off scheduled sends](#phase-1--one-off-scheduled-sends)
8. [Phase 2 — cancel, reschedule, visibility](#phase-2--cancel-reschedule-visibility)
9. [Phase 3 — quiet hours and the grace window](#phase-3--quiet-hours-and-the-grace-window)
10. [Phase 4 — drip and throttle](#phase-4--drip-and-throttle)
11. [Phase 5 — follow-up sequences](#phase-5--follow-up-sequences)
12. [Phase 6 — ops and docs](#phase-6--ops-and-docs)
13. [Runbook](#13-runbook)

---

## 1. Why this is harder than it looks

**The Gmail API has no `sendAt`.** Gmail's "Schedule send" button is a feature of the Gmail *web
client*, not of the API. `users.messages.send` takes a raw RFC 822 message and sends it
immediately. There is no parameter, no header, and no draft trick that makes Google hold a message
until Tuesday.

Everything below follows from that one fact. A scheduled send needs **a process that is awake at
the scheduled moment and holds that member's Gmail OAuth token**.

> ### ⚠️ This section described the pre-hosting design. It has been superseded.
>
> Everything below this box was written when the CRM ran on each member's laptop. The reasoning is
> preserved because the *constraint* has not changed — Gmail still has no `sendAt` — but the answer
> has. **The server now holds encrypted per-member refresh tokens and sends directly**
> (`crm/services/gmail.py`, `crm/services/sending.py`, `crm/services/runner.py`), and
> `local_agent/` is being retired.
>
> The argument below — that the CRM *cannot* be that process, because the server never holds Gmail
> credentials — was correct and load-bearing at the time. It was given up deliberately, not
> overlooked. The trade: a member can now sign in, press Send, and close the tab, and a scheduled
> send fires whether or not anyone's laptop is on. What it costs is that a compromised server can
> impersonate a member. What partly replaces it is in `crm/services/secrets.py` (tokens encrypted at
> rest, so a database leak alone yields nothing), the identity binding in
> `gmail.py::verify_identity`, and the fact that a member can revoke access from their own Google
> account at any time.
>
> Read §2–§5 for the state machine, the lease protocol and the timing arithmetic: all of that is
> unchanged and still accurate. Read §1's "what we are not doing" table as history.

So the executor is an agent. The only question is *which* agent, and whether it happens to be
running. That is the subject of §2.

### What we are not doing, and why

*As judged before hosting. The first row is still true; the second and third still hold. The fourth
and fifth are now about how the server sends, not whether it does.*

| Option | Why not |
|---|---|
| `sendAt` on the Gmail API | Does not exist. Still true, and still the reason any of this exists. |
| Server sends via SMTP / SendGrid / SES | Mail would stop coming from a real human mailbox. Deliverability and reply-handling both depend on that (README §1). **Still rejected** — the server sends *through each member's own Gmail*, which is a different thing entirely. |
| Gmail drafts + a Google Apps Script | A second codebase in a second language, with its own auth and its own failure modes, to schedule mail we already know how to send. Still rejected. |
| Service account with domain-wide delegation | Needs Workspace admin over `pilani.bits-pilani.ac.in`. We do not have it. Per-member OAuth consent is what replaces it, and it needs no admin. |
| A cron job on the CRM host that shells into an agent | ~~That *is* the always-on agent~~ — superseded. There is no agent to shell into. The equivalent today is an external pinger calling one endpoint; the free hosting tier has no cron of its own. |

---

## 2. Architecture

```
  ┌──────────────┐   POST /schedules            ┌──────────────────┐
  │ member's     │ ───────────────────────────▶ │  Django CRM      │
  │ laptop agent │                              │  (Supabase)      │
  └──────────────┘                              │                  │
                                                │  scheduled_sends │
  ┌──────────────┐   POST /schedules/claim      │                  │
  │ always-on    │ ◀──── leases due jobs ─────▶ │  campaign_mailings
  │ agent        │       every 60s              └──────────────────┘
  │ (Docker)     │
  └──────┬───────┘
         │ Gmail API (that member's token)
         ▼
     recipients
```

The always-on agent is the existing `agent` service in `docker-compose.yml` (profile `agent`),
which already mounts a token directory and refuses to start unless its API token's owner matches
its Gmail account. Phase 1 gives it a background poller; nothing about its identity model changes.

### One agent, one member

`AGENT_API_TOKEN` identifies exactly one `TeamMember`, and `local_agent/main.py`'s lifespan aborts
startup if the Gmail session disagrees with it. An agent may therefore only execute schedules
belonging to **its own member** — anything else would put a mail in a prospect's inbox from the
wrong mailbox and record a false `sent_by`.

Consequences to be honest about:

- One always-on agent gives reliable scheduling for **one account**.
- To cover several members, run one container per member, each with its own `AGENT_API_TOKEN` and
  `AGENT_TOKEN_DIR`. See the [runbook](#13-runbook).
- A member with no always-on agent can still schedule; their job simply waits for their laptop
  agent, subject to the grace window (§5). This is a real limitation, not a bug, and the CRM
  surfaces it (Phase 2).

---

## 3. The state machine

Defined in `shared/enums.py::ScheduleStatus`, which both apps import — the file's own docstring
requires that these strings never drift.

```
                    ┌──────────── cancel ───────────┐
                    ▼                               │
  create ──▶ PENDING ──── due + allowed ──▶ RUNNING ──▶ DONE
                │  ▲                          │
       due but  │  │ lease expired            │ job-level error
       blocked  │  │ (crash recovery)         ▼
                ▼  │                       FAILED
              HELD ┘
                │
                │ grace window closed
                ▼
             MISSED
```

- **PENDING** — scheduled, not yet due, or bounced back by a lease sweep.
- **RUNNING** — leased by an agent this minute. Not a promise it will finish.
- **HELD** — due, but not allowed to run: the campaign left `active`, or we are inside quiet hours.
  Re-evaluated every tick.
- **DONE** — every contact in the job was resolved: sent, or permanently skipped.
- **CANCELLED / MISSED / FAILED** — terminal, listed in `TERMINAL_SCHEDULE_STATUSES`.

`MISSED` exists so that "nothing was listening" is a visible outcome rather than a mail arriving at
3am, four hours after the moment it referred to.

---

## 4. The lease protocol

Two agents authenticated as the same member (a laptop and the always-on one) can poll at the same
instant. Claiming is one transaction, mirroring `claim_batch` in `services/mailing.py`:

```sql
SELECT * FROM scheduled_sends
 WHERE status = 'pending' AND member_id = :me AND scheduled_at <= :now
 FOR UPDATE SKIP LOCKED;
-- then, in the same transaction:
UPDATE ... SET status='running', leased_by=:agent, lease_expires_at = :now + interval '5 minutes';
```

`SKIP LOCKED` means a second poller sees nothing rather than blocking. While a batch runs the agent
heartbeats to extend the lease; if it dies, the lease expires and a sweep returns the job to
`PENDING`, exactly as `stranded_drafts` recovers a half-sent batch today.

**The lease is a scheduling optimisation, never the safety mechanism.** Even if two agents somehow
executed the same job, `UniqueConstraint(campaign, contact)` still makes a second mail to the same
prospect impossible. That constraint remains the only thing standing between us and a duplicate
send, and nothing here is allowed to weaken it.

---

## 5. Timing arithmetic

`settings.TIME_ZONE` is `Asia/Kolkata` and `USE_TZ` is on: the UI speaks IST, storage is UTC. Times
cross the wire as ISO 8601 with an offset and are parsed with `django.utils.dateparse`.

Everything derives from two configured values — quiet hours and the grace window:

```
deliver_after = max(scheduled_at, next_open_slot(scheduled_at))
deadline      = deliver_after + SCHEDULE_GRACE_HOURS

now <  deliver_after                  ->  PENDING   (not yet)
      deliver_after <= now <= deadline ->  claimable
now >  deadline                       ->  MISSED
inside quiet hours, or campaign not active
                                      ->  HELD      (re-check next tick)
```

Grace is measured from `deliver_after`, **not** from `scheduled_at`. A job set for 22:00 under
09:00–19:00 quiet hours is deferred to 09:00 the next morning; measuring from `scheduled_at` would
declare it missed for being eleven hours late when it was never permitted to run.

All of this lives in `services/scheduling.py` and takes `now` as a parameter (defaulting to
`timezone.now()`), so quiet hours, grace and drip intervals are testable without waiting six hours.

---

## ✅ Phase 0 — branch and vocabulary

- [x] `git switch -c mail_schedule`
- [x] `ScheduleStatus` + `TERMINAL_SCHEDULE_STATUSES` in `shared/enums.py`
- [x] This document

## ✅ Phase 1 — one-off scheduled sends

The MVP: pick contacts, press **Schedule…**, choose a time, and the always-on agent sends it then.

**Model** — `ScheduledSend(TimeStampedModel)`, `db_table = "scheduled_sends"`:

| Field | Notes |
|---|---|
| `campaign` | FK `PROTECT` |
| `member` | FK `PROTECT` — whose Gmail sends it, and the only agent allowed to execute it |
| `created_by` | FK `SET_NULL` |
| `contact_ids` | `ArrayField(UUIDField())` — snapshot of the selection |
| `cursor` | index of the next contact to attempt |
| `scheduled_at` | tz-aware, indexed |
| `status` | `ScheduleStatus`, indexed |
| `cc` / `bcc` | validated by the existing `mailing.parse_copy_addresses` |
| `leased_by` / `lease_expires_at` | crash recovery (§4) |
| `sent_count` / `skipped_count` / `attempts` | progress |
| `last_error` / `started_at` / `finished_at` | |

A **cursor**, not "contacts without a mailing": a contact who is permanently skipped (unassigned,
archived, `do_not_contact`) never gets a `CampaignMailing` row, so an existence check would leave
the job running forever. A cursor always terminates.

**Service** — `core_django/crm/services/scheduling.py`: `create`, `cancel`, `reschedule`,
`claim_due`, `record_progress`, `sweep_expired_leases`, `resolve_status`. Execution reuses
`mailing.claim_batch(...)` untouched: scheduling decides *when* and *for whom*, never *how a mail is
built*.

**API** — `POST /schedules`, `GET /schedules`, `POST /schedules/claim`,
`POST /schedules/<id>/progress`, `POST /schedules/<id>/cancel`.

**Agent** — a background asyncio task in `main.py`'s lifespan polls every 60s and runs claimed jobs
through the existing `send_svc.send_batch`. An `asyncio.Lock` shared with `/api/send` keeps a
scheduled batch and a manual one from interleaving mid-Gmail-call.

**Tests** — `core_django/crm/tests/test_scheduling.py`: due-window boundaries, a leased job is
invisible to a second claimer, an expired lease is swept back, cancel beats execution, member A's
job is never handed to member B, and the cursor terminates past permanently-skipped contacts.

## ✅ Phase 2 — cancel, reschedule, visibility

Agent: a **Scheduled** panel with cancel and change-time. CRM: `/schedules/` showing every member's
jobs with `missed`/`failed` surfaced loudly — a job that silently never ran on someone's laptop is
precisely the failure the shared CRM exists to make visible. Cancelling a `RUNNING` job stops it
before the *next* contact; mail already sent stays sent and its rows stand.

## ✅ Phase 3 — quiet hours and the grace window

Implements §5. Settings: `SCHEDULE_QUIET_START` / `SCHEDULE_QUIET_END` (default 09:00–19:00 IST),
`SCHEDULE_QUIET_DAYS`, `SCHEDULE_GRACE_HOURS` (default 6). A campaign paused between scheduling and
execution becomes `HELD`, then `MISSED` at the deadline: pausing is the documented emergency brake
(README §5) and must stop a scheduled send visibly, not by silent deletion.

## ✅ Phase 4 — drip and throttle

Per job: `batch_size`, `interval_minutes`, optional `per_day`, optional jitter so gaps are not
machine-regular. Each tick takes `contact_ids[cursor : cursor + batch_size]` and advances.

Two existing limits stay authoritative and are never overridden: `DAILY_SEND_CAP = 400`, enforced
server-side across every device a member uses, and `AGENT_SEND_DELAY_SECONDS` for intra-batch
pacing. A drip that hits the cap parks until the 24-hour window rolls, exactly as `claim_batch`
already reports `CAP_REACHED`.

## ✅ Phase 5 — follow-up sequences

`FollowUpRule`: parent campaign → follow-up campaign, `delay_days`, condition `no_reply`.

Reply detection reuses what exists: `CampaignMailing.mail_thread_id` is captured on every send, and
`GmailClient` already holds `gmail.readonly` and already queries Gmail during `reconcile`. A new
agent job fetches each thread and asks whether it contains a message from the contact after
`sent_at`. A reply cancels pending follow-ups; silence past `delay_days` creates a `ScheduledSend`
for the follow-up campaign, and everything downstream is Phase 1 machinery.

⚠️ **This touches a deliberate invariant.** `shared/enums.py` states that NEW→CONTACTED is the
*only* automatic lifecycle transition, because inferring funnel state is guesswork. A real reply in
a thread is evidence rather than a guess — but the rule was chosen on purpose, so the
auto-`REPLIED` transition is **opt-in per rule**, and that docstring and README §6 are updated in
the same commit that introduces it.

## ✅ Phase 6 — ops and docs

Runbook below; README updated (§5 data model, §10 API, §11 services, change log, known gaps);
`.env.example` carries the window, grace and poll settings.

Settings reference:

| Variable | Default | Meaning |
|---|---|---|
| `SCHEDULE_WINDOW_START` / `_END` | `9` / `19` | Delivery window, in `TIME_ZONE`. Equal values disable it. |
| `SCHEDULE_WINDOW_DAYS` | `0,1,2,3,4,5,6` | Weekdays mail may go out; Monday is 0. |
| `SCHEDULE_GRACE_HOURS` | `6` | How late a job may still send before it is `missed`. |
| `AGENT_SCHEDULE_POLL_SECONDS` | `60` | How often an agent asks for due work. |
| `AGENT_SCHEDULE_CLAIM_LIMIT` | `5` | Jobs leased per tick. |
| `AGENT_ID` | hostname:pid | Who holds a lease, for humans reading the CRM. |

---

## 13. Runbook

### Starting the always-on agent

```bash
# once: issue that member a token and grant Gmail consent on this host
docker compose exec crm python core_django/manage.py issue_token \
    kabir@pilani.bits-pilani.ac.in --label always-on

# then, with AGENT_API_TOKEN and AGENT_MEMBER_EMAIL in .env
docker compose --profile agent up -d
docker compose logs -f agent
```

Within a minute the log says:

```
scheduler polling every 60s as <hostname>:<pid>
```

If it does not, nothing is scheduled — that line is the whole feature working.
`GET /health` on port 8111 answers even when the poller is wedged, so check the log, not the port.

### One agent per member

An agent may only execute schedules for the member its token belongs to (§2). To cover several
people, run one container each — same image, different token and token directory:

```yaml
  agent-kabir:
    extends: { service: agent }
    container_name: ignite_agent_kabir
    environment:
      AGENT_API_TOKEN: ${KABIR_AGENT_TOKEN}
      AGENT_MEMBER_EMAIL: kabir@pilani.bits-pilani.ac.in
    volumes: [ "~/.ignite_crm/kabir:/tokens" ]
    ports: [ "8112:8111" ]
```

Each needs its own Gmail consent granted once on that host, and its own port.

### What `missed` means

Nothing was awake to run the job before its grace window closed (§5). The mail **did not go out**.
The `/schedules/` page lists these above the table for exactly this reason.

To send it after all: open the job, confirm the campaign is still `active` and the content still
makes sense, then queue a fresh schedule for the same contacts. A `missed` job is terminal on
purpose — silently reviving one hours later is how a prospect gets a mail about an event that has
already happened.

### Draining the queue before a deploy

```bash
# what is still outstanding
docker compose exec crm python core_django/manage.py shell -c \
  "from crm.models import ScheduledSend; from shared.enums import TERMINAL_SCHEDULE_STATUSES; \
   print(ScheduledSend.objects.exclude(status__in=TERMINAL_SCHEDULE_STATUSES).count())"
```

A restart mid-batch is safe: the lease expires, the job returns to `PENDING`, and the unique
constraint means the contacts already done are skipped. The only cost is up to five minutes of
delay. There is no need to drain anything — but knowing the number tells you what to expect in the
logs afterwards.

### After a code change

**The agent does not hot-reload.** `docker compose up -d --build agent`, or restart the host
process. A change to `local_agent/` with no restart looks exactly like a broken feature, and has
already once been mistaken for one.

### When a scheduled send did not arrive

In order of likelihood:

1. **No agent was running** for that member → the job is `missed` or still `pending`. The log is
   the proof; `/schedules/` is the summary.
2. **Outside the sending window** → status is `held`, and `last_error` names the window and the
   time it will be released.
3. **Campaign was paused** → `held`, then `missed` at the deadline. Pausing is the emergency brake
   and it is doing its job.
4. **Daily cap spent** → the scheduler stands down entirely until the 24-hour window rolls.
5. **The contact was skipped** → `skipped_count` moved, not `sent_count`. Reasons are the ordinary
   ones: reassigned, archived, `do_not_contact`, already mailed for that campaign.
