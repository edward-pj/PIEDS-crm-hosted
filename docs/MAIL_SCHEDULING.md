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
> `local_agent/` has been deleted.
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
  ┌──────────────┐   press Send / Schedule      ┌────────────────────┐
  │  a member's  │ ───────────────────────────▶ │  Django CRM        │
  │   browser    │                              │  (Render, one      │
  └──────────────┘                              │   Docker service)  │
                                                │                    │
  ┌──────────────┐   "Send queued mail now"     │  scheduled_sends   │
  │  a lead's    │ ───────────────────────────▶ │  campaign_mailings │
  │   browser    │        (or manage.py         │  gmail_credentials │
  └──────────────┘         run_tick)            └─────────┬──────────┘
                                                          │
                                       services/runner.py::tick()
                                                          │
                                       Gmail API, per member's own token
                                                          ▼
                                                     recipients
```

**One process does everything.** There is no agent, no second app, and nothing
to keep running on anyone's laptop. `services/runner.py::tick()` is the executor:
it sweeps expired leases, sweeps missed jobs, runs the reply scan, runs the
follow-up rules, then for each member with a usable Gmail credential calls
`scheduling.claim_due(member, …)` and sends what it leased.

### Pressing Send does not send

It commits a `ScheduledSend` due now and returns. This is deliberate: a free
Render instance can be reaped mid-request, and gunicorn workers are scarce, so
streaming a 200-mail batch inside one HTTP response makes "the tab was closed" a
data-integrity question. Queueing makes it a non-event, and it means an
immediate send reuses the lease, drip, grace and recovery machinery that
scheduled sends already had rather than growing a second execution path.

### Still one member per job

`claim_due(member, …)` keeps its per-member signature even though the server now
holds every credential and could widen the query. The `member` filter is what
guarantees a job sends from the mailbox it was queued against; `sent_by` stops
meaning anything without it. The tick loops over members instead.

### How it is driven

`tick()` has three doors and one code path:

| Piece | Why |
|---|---|
| `GET /internal/tick`, authenticated with `secrets.compare_digest` against `TICK_SECRET` | the pinger is not a team member, so a shared secret rather than a session or a token row. A header, never a query string: Render logs full request lines |
| session-level `pg_try_advisory_lock` around the call, released in a `finally` | two pings landing on two gunicorn workers means two ticks, and workers share no memory |
| an external pinger every minute | Render's free plan has **no cron**, and a self-ping cannot wake an instance that is already asleep |

Supabase `pg_cron` + `pg_net` is the driver, cron-job.org the failure alarm.
`docs/PINGER_SETUP.md` has the exact SQL and settings. An unset `TICK_SECRET`
disables the route (503) rather than defaulting it open — a blank
`compare_digest` against a blank header would authenticate anybody sending
nothing at all.

**Still not run on a timer:** reply detection and `followups.run_all_rules`.
Left out deliberately rather than forgotten; adding them changes nothing about
the queue, the lock or the lease protocol.

Three things worth knowing:

- **Session-level, not `pg_try_advisory_xact_lock`.** The transactional variant
  needs the whole tick inside one transaction, which is impossible: `claim_batch`
  commits **per contact** on purpose, so that a crash can never leave a sent mail
  with no record. Never wrap `tick()` in `@transaction.atomic`.
- **Advisory locks are broken on Supabase's transaction pooler (:6543)** — a
  session lock taken on one pooled backend is invisible to the next statement,
  with no error, so two ticks simply run at once. `check_db` already refuses port
  6543 for `SELECT … FOR UPDATE`; that check protects the scheduler too.
- **The ping interval is the throughput setting, not the tick size.** A tick is
  capped at `TICK_MAX_MAILS` (40), so 1 minute is ~2,400 mails/hour team-wide
  and 10 minutes is ~240. Ten minutes is what you would pick thinking only about
  keep-alive, and it would quietly turn a launch blast into most of a day.
- **One tick's budget is shared.** `PER_MEMBER_TICK_MAILS` (10) caps any one
  member's share, and `sendable_members()` rotates by wall-clock minute, because
  `TeamMember.Meta.ordering` is `["name"]` and a hard budget otherwise means the
  alphabetically first member drains completely before anyone else is touched.

An in-process thread from `AppConfig.ready()` was considered and **rejected**:
`ready()` runs in every gunicorn worker (so, several schedulers racing), and also
under `migrate`, `collectstatic`, `shell` and **pytest** — a live scheduler
inside the test suite, avoidable only by sniffing `sys.argv`. It holds a pooler
connection permanently against a deliberate `CONN_MAX_AGE=0`. And it does not
even solve the problem: the instance sleeps after 15 minutes of no requests and
the thread dies with it, so it only runs when something external is already
keeping the service awake. On a platform where only a request can wake the
process, a request-driven design is the only honest one.

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
- **RUNNING** — leased by a tick this minute. Not a promise it will finish.
- **HELD** — due, but not allowed to run: the campaign left `active`, or we are inside quiet hours.
  Re-evaluated every tick.
- **DONE** — every contact in the job was resolved: sent, or permanently skipped.
- **CANCELLED / MISSED / FAILED** — terminal, listed in `TERMINAL_SCHEDULE_STATUSES`.

`MISSED` exists so that "nothing was listening" is a visible outcome rather than a mail arriving at
3am, four hours after the moment it referred to.

---

## 4. The lease protocol

Two ticks can overlap — two gunicorn workers, or a lead pressing the button while `run_tick`
is running. Claiming is one transaction, mirroring `claim_batch` in `services/mailing.py`:

```sql
SELECT * FROM scheduled_sends
 WHERE status = 'pending' AND member_id = :me AND scheduled_at <= :now
 FOR UPDATE SKIP LOCKED;
-- then, in the same transaction:
UPDATE ... SET status='running', leased_by=:runner, lease_expires_at = :now + interval '5 minutes';
```

`SKIP LOCKED` means a second tick takes different jobs rather than blocking. While a batch runs
the lease is extended; if the process dies, the lease expires and a sweep returns the job to
`PENDING`, exactly as `stranded_drafts` recovers a half-sent batch today.

**The lease is a scheduling optimisation, never the safety mechanism.** Even if two runners somehow
executed the same job, `uniq_root_campaign_contact` still makes a second mail to the same prospect
impossible — and it is now team-wide, not per campaign, so it also covers two *different* members'
sub-campaigns under one root. That constraint remains the only thing standing between us and a
duplicate send, and nothing here is allowed to weaken it.

> **When the scheduler lands, the 5-minute lease becomes wrong.** With ticks minutes apart, every
> RUNNING job's lease expires between them and the sweep requeues jobs that were never stranded.
> Fix the meaning rather than the number: a tick must **always resolve every job it leases before
> returning**, so "RUNNING at the start of a tick" means precisely "the previous tick died
> mid-slice" — which is what the sweep is for. Then drop `LEASE_MINUTES` to 2.

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

The MVP: pick contacts, press **Schedule…**, choose a time, and the next tick after that time sends it.

**Model** — `ScheduledSend(TimeStampedModel)`, `db_table = "scheduled_sends"`:

| Field | Notes |
|---|---|
| `campaign` | FK `PROTECT` |
| `member` | FK `PROTECT` — whose Gmail sends it; `claim_due` is per member, so nothing else can |
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
`SCHEDULE_QUIET_DAYS`, `SCHEDULE_GRACE_HOURS` (default 20). A campaign paused between scheduling and
execution becomes `HELD`, then `MISSED` at the deadline: pausing is the documented emergency brake
(README §5) and must stop a scheduled send visibly, not by silent deletion.

## ✅ Phase 4 — drip and throttle

Per job: `batch_size`, `interval_minutes`, optional `per_day`, optional jitter so gaps are not
machine-regular. Each tick takes `contact_ids[cursor : cursor + batch_size]` and advances.

Two existing limits stay authoritative and are never overridden: `DAILY_SEND_CAP` (800, from settings), enforced
server-side and counted across everything a member sends, and `GMAIL_SEND_DELAY_SECONDS` for
intra-batch pacing (now `0.0` by default — the old 2-second pause existed for a laptop sitting in a
loop, and inside a bounded tick it only burns budget). A drip that hits the cap parks until the 24-hour window rolls, exactly as `claim_batch`
already reports `CAP_REACHED`.

## ✅ Phase 5 — follow-up sequences

`FollowUpRule`: parent campaign → follow-up campaign, `delay_days`, condition `no_reply`.

Reply detection reuses what exists: `CampaignMailing.mail_thread_id` is captured on every send, and
`GmailClient` already holds `gmail.readonly` and already queries Gmail during `reconcile`. The
reply scan at the top of each tick fetches each thread and asks whether it contains a message from
the contact after `sent_at`. It runs **before** claiming, deliberately: a reply seen now pulls that
contact out of a follow-up going out this very tick. A reply cancels pending follow-ups; silence past `delay_days` creates a `ScheduledSend`
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
| `SCHEDULE_WINDOW_START` / `_END` | `0` / `0` | Delivery window, in `TIME_ZONE`. **Equal values disable it, and that is the shipped default** — mail sends whenever it is queued. Set `9` / `19` to restore a 09:00–19:00 window. |
| `SCHEDULE_WINDOW_DAYS` | `0,1,2,3,4,5,6` | Weekdays mail may go out; Monday is 0. |
| `SCHEDULE_GRACE_HOURS` | `20` | How late a job may still send before it is `missed`. Must exceed the longest gap the pinger's window creates — 17 h for a 10:00–17:00 window. |
| `GMAIL_SEND_DELAY_SECONDS` | `0.0` | Pause between messages inside one batch. |

And, as module constants rather than env vars, because changing them is a decision and not a knob:

| Constant | Value | Meaning |
|---|---|---|
| `runner.CLAIM_LIMIT` | `5` | Jobs leased per member per tick. |
| `runner.TICK_MAX_MAILS` | `40` | Hard cap on one tick, so it fits inside a request. Env-configurable. |
| `runner.TICK_MAX_SECONDS` | `25` | Wall-clock cap on one tick — and under cron-job.org's 30 s cut-off, so the alarm reports truth. Env-configurable. |
| `runner.PER_MEMBER_TICK_MAILS` | `10` | One member's share of a tick, so nobody drains the whole budget. |
| `settings.TICK_SECRET` | — | Shared secret for `/internal/tick`. Blank disables the route. |
| `scheduling.LEASE_MINUTES` | `5` | How long a lease survives before a sweep reclaims it. |
| `sending.CLAIM_CHUNK` | `10` | Contacts claimed at once — see the 19 Aug incident, README §7. |

---

## 13. Runbook

### Making queued mail go out

Normally the pinger does it (§2, and `docs/PINGER_SETUP.md`). The other two doors
are for when you do not want to wait a minute, or are not deployed:

| | |
|---|---|
| `GET /internal/tick` | the pinger's door, every minute during its window |
| **Send queued mail now** on `/schedules/` | any member, POST — the manual override |
| `manage.py run_tick` | the same function from a shell, for local work |

```bash
cd core_django && ../.venv/bin/python manage.py run_tick
```

It prints what it did: leases taken, mails sent, jobs finished, jobs held. A
tick is bounded by `TICK_MAX_MAILS` (40) and `TICK_MAX_SECONDS` (25) so it fits
inside a request — and inside cron-job.org's 30-second cut-off, so the alarm
reports truth. Both are env-configurable; `elapsed_seconds` in the tick's JSON
tells you which one is actually binding.

**Render's free tier has no shell.** On the deployed instance the button and the
endpoint are the only ways in — `manage.py run_tick` cannot be run there at all.

It is safe to run two at once, at three independent levels: the advisory lock
means the second one returns `{"locked": true}` without starting; if it somehow
did start, `claim_due` uses `SELECT … FOR UPDATE SKIP LOCKED` so it would take
different jobs; and underneath both, `uniq_root_campaign_contact` is the real
guarantee that no prospect is mailed twice. The lock is about not wasting work,
never about correctness of the mail itself.

### Everyone connects their own Gmail

A member with no usable `GmailCredential` is skipped by the tick entirely — their
jobs sit `pending` until they connect. `sendable_members()` is the filter, and
`/settings/gmail/` is where they fix it. This is the most likely reason one
person's scheduled mail did not move while everyone else's did.

### What `missed` means

Nothing ran the job before its grace window closed (§5). The mail **did not go
out**. The `/schedules/` page lists these above the table for exactly this
reason.

To send it after all: open the job, confirm the campaign is still `active` and
the content still makes sense, then queue a fresh schedule for the same contacts.
A `missed` job is terminal on purpose — silently reviving one hours later is how
a prospect gets a mail about an event that has already happened.

### Draining the queue before a deploy

```bash
# what is still outstanding
cd core_django && ../.venv/bin/python manage.py shell -c \
  "from crm.models import ScheduledSend; from shared.enums import TERMINAL_SCHEDULE_STATUSES; \
   print(ScheduledSend.objects.exclude(status__in=TERMINAL_SCHEDULE_STATUSES).count())"
```

A restart mid-tick is safe: the lease expires, the job returns to `PENDING`, and
the unique constraint means contacts already done are skipped. The only cost is
delay. There is nothing you *must* drain — but knowing the number tells you what
to expect afterwards.

### When a scheduled send did not arrive

In order of likelihood:

1. **Nobody ran a tick.** The job is still `pending`, or `missed` if its grace
   window closed. This is the common one until the scheduler lands, and it is the
   first thing to check.
2. **That member has not connected Gmail** → skipped by `sendable_members()`,
   job stays `pending`.
3. **Outside the sending window** → status is `held`, and `last_error` names the
   window and the time it will be released.
4. **Campaign was paused** → `held`, then `missed` at the deadline. Note that
   pausing the **root** pauses every sub-campaign under it; that is the emergency
   brake doing its job.
5. **Daily cap spent** → that member stands down entirely until the 24-hour
   window rolls.
6. **The contact was skipped** → `skipped_count` moved, not `sent_count`. Reasons
   are the ordinary ones: reassigned, archived, `do_not_contact`, or already
   mailed by a teammate under that root campaign.
