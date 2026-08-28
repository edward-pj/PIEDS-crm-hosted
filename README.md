# Ignite CRM — PIEDS Mass Mailing System

A shared contact pool and mass-mailing system for PIEDS, BITS Pilani's technology
business incubator. One hosted CRM on one URL: a member signs in with their BITS
Google account, connects Gmail once, and sees the contacts they personally have
to mail — already assigned, already deduplicated, with their own footer on the
team's shared message. Mail leaves their real mailbox, so replies come back to
them and nothing is delivered as a bulk sender.

> **Being migrated from a two-process design.** Until recently this was a hosted
> CRM plus `local_agent/`, a FastAPI app each member ran on their own laptop to
> hold their Gmail credentials. Sending has moved server-side and the agent is
> being retired; sections still describing it are marked where they are now
> historical.

---

## Table of contents

1. [What this is](#1-what-this-is)
2. [Architecture](#2-architecture)
3. [The guarantees](#3-the-guarantees)
4. [Who can do what](#4-who-can-do-what)
5. [Data model](#5-data-model)
6. [Lifecycle and tags](#6-lifecycle-and-tags)
7. [The send protocol](#7-the-send-protocol)
8. [Staying in sync](#8-staying-in-sync)
9. [Screens](#9-screens)
10. [HTTP API — removed](#10-http-api--removed)
11. [Services — where the rules live](#11-services--where-the-rules-live)
12. [Setup](#12-setup)
13. [Running it](#13-running-it)
14. [Supabase — the master database](#14-supabase--the-master-database)
15. [Google setup](#15-google-setup)
16. [Deploying](#16-deploying)
17. [Tests](#17-tests)
18. [Environment variables](#18-environment-variables)
19. [Management commands](#19-management-commands)
20. [File layout](#20-file-layout)
21. [Operational playbook](#21-operational-playbook)
22. [Change log](#22-change-log)
23. [Known gaps](#23-known-gaps)

---

## 1. What this is

One Django app, one database, one URL.

**`core_django/`** owns the schema, the contact pool, campaigns, assignment,
every safety rule — and now the sending too. A member signs in with their BITS
Google account, grants Gmail access once, and the server sends *as them* using a
refresh token it holds encrypted.

**Mail still leaves each member's own mailbox**, which was always the point and
has not changed. A central sending address would land in spam at volume, and the
person whose name is on the mail is the person who will get the reply.

---

## 2. Architecture

```
                                      hosted (Render, one service)
   member's browser          ┌──────────────────────────────────┐
   ─────────────────────────►│ core_django (Django + gunicorn)  │──► Supabase
        Google sign-in       │                                  │    Postgres
                             │ holds: DB credentials            │    (the master)
                             │ holds: each member's Gmail       │
                             │        refresh token, ENCRYPTED  │
                             └──────────────┬───────────────────┘
                                            │
                                            └──► Gmail API, as the member
```

### What changed, and what it cost

This used to be **two** processes: the CRM, plus `local_agent/` — a FastAPI app
each member ran on their own laptop, holding their Gmail OAuth and no database
credentials. The server deliberately held no Gmail credentials, so it *could not*
impersonate anyone even if compromised.

**That property was given up on purpose**, and it is the largest security
decision in this repository. It is what makes the product work as one hosted URL:
a member signs in, presses Send, and closes the tab. Scheduled sends fire whether
or not anyone's laptop is on. The alternative was fifteen people each installing
Python, obtaining a client secret, and running `uvicorn`.

What replaces it, honestly stated:

- Refresh tokens are **encrypted at rest** (`services/secrets.py`) with a key
  that lives only in the hosting platform's environment, never in the database.
  A database leak on its own therefore yields nothing usable. It is **not**
  defence against an application compromise, and `secrets.py` says so.
- `sent_by` stays trustworthy a different way: the granting Google account is
  compared to the member's `bits_email` at grant time and re-verified against
  Gmail's own `getProfile` after any credential change
  (`gmail.py::verify_identity`).
- A member can **revoke access from their own Google account page** at any time,
  with no cooperation from us. The CRM then says "Gmail not connected" instead of
  failing silently.
- Every send still writes a durable `CampaignMailing` row naming the sender, and
  every contact mutation still writes a `ContactAudit` row.

**The database is still reached by exactly one process.** The row lock, the
unique constraints and the DRAFT-before-send ordering all live next to the data
they protect, and port 5432 never faces the internet.

---

## 3. The guarantees

### 3.1 A prospect is never mailed twice for the same campaign

```sql
UNIQUE (campaign_id, contact_id)  -- on campaign_mailings
```

Not application logic — a **database constraint**. A double-click, a retry, two
agents racing, or a crash mid-batch all collide there rather than putting a
second copy in someone's inbox. Everything else in this system is convenience.

Verify it exists on any host with `manage.py check_db`.

### 3.2 A sent mail always has a record

The DRAFT row is committed **before** the Gmail call is made. A crash can
therefore leave an ambiguous DRAFT — visible and resolvable — but never a sent
mail with no record, which would be unrecoverable. The ordering survived the
move server-side unchanged; see §7.

### 3.3 `sent_by` is trustworthy

The agent refuses to start unless two independent identities agree: the API
token's owner (what the CRM thinks) and the Gmail session (which mailbox you can
actually send from). Nobody can send from their own account and have it
attributed to someone else.

### 3.4 We never mail a half-rendered template

A contact missing a variable the template needs is **skipped**, not mailed with a
gap. Nobody receives "Hi , we loved what you're building at ."

### 3.5 Nobody edits data they don't own

Ordinary members can only change contacts assigned to them. Enforced per-object
in `services/permissions.py` and again in `services/contacts.py` — twice, because
the API accepts JSON, and JSON does not respect a form's field list.

---

## 4. Who can do what

A **lead** is someone with the `lead` role on an active team
(`crm/models.py::TeamMembership`), read in exactly one place:
`services/permissions.py::is_lead`.

It used to be `member.batch == "2024"`, a literal in `shared/enums.py` that had
to be edited every year, could not express "lead of this team but not that one",
and made the permission system a fact about when somebody was admitted to
university. **`TeamMember.batch` is now a display field and grants nothing.**

`is_lead(member)` keeps its one-argument signature deliberately — it answers "is
this person a lead of anything", which is the right question for the ~22
decorator call sites and the contact-level rules, so none of them changed when
the rule underneath did. Questions genuinely about one team get their own
function, `assignable_members(actor)`, rather than a second argument every
caller would have to start passing.

Two details that are deliberate rather than incidental:

- **A lead of a *deactivated* team keeps nothing.** `is_lead` filters on
  `team__is_active=True`, so last year's leads do not hold the contact pool
  forever.
- **The answer is cached on the resolved member instance**, not globally.
  `lead_required` calls `is_lead` on every guarded request and `_base` calls it
  again for the template context, so without the cache every page costs two
  extra queries. Per-instance means it dies with the request — a role change
  takes effect on the member's next page load.

| | lead | member |
|---|---|---|
| **Signs in by** | **Google, BITS domain only** | **Google, BITS domain only** |
| **Becomes one by** | being promoted by another lead | a team join code |
| See the whole pool | ✅ | ✅ |
| Edit a contact | anyone's | **only their own assigned** |
| Archive / restore | anyone's | only their own |
| Add a contact | assigns to anyone | assigns to themselves, forced |
| Bulk edit | anyone's | only their own |
| Delete permanently | ✅ (never-mailed only) | ❌ |
| Set `lifecycle` by hand | ✅ | ❌ — the server moves it |
| Assign contacts to members | ✅ | ❌ |
| Create / edit a campaign body | ✅ | ❌ (read-only) |
| Edit their own footer | ✅ | ✅ — the only campaign field a member owns |
| Create a team, rotate its join code | ✅ | ❌ |
| Change someone's role, remove them | ✅ | ❌ |
| Distribute the pool across a team | ✅ | ❌ |
| Import CSV | ✅ | ❌ |
| Drain the send queue (§13.3) | ✅ | ❌ |
| Connect their own Gmail | ✅ | ✅ |
| Send mail | ✅ | ✅ |

Everyone edits through the same screens. There is one surface now — the hosted
CRM — so "which of the two apps am I looking at" is no longer a question anyone
has to answer.

### 4.1 One door

There is no password anywhere in the CRM. `services/auth.py` holds the only
door, and identity is a session key holding a `TeamMember` id — Django's `User`
model is consulted only by `/admin/`.

**Everyone signs in with Google**, restricted to the BITS hosted domain and
matched against `TeamMember.bits_email`. The check is on Google's signed
`id_token` and its `hd` claim, not on the address string, so a personal Gmail
cannot present itself as a BITS one. Members must prove that identity anyway in
order to send, so this reuses a proof they already have to give rather than
inventing a second one.

**There used to be a second door, and it is gone.** Batch 2024 picked a name
from a dropdown with no password, justified on the grounds that every lead ran
the CRM on their own laptop, so the only person who could reach the form was the
person holding the machine. That was true then and is simply false now: on a
public hostname the form is reachable by anyone, and the leads' UUIDs — the only
credential it asked for — were rendered into the page as `<option value>`.

It was deleted rather than hidden behind a setting, because a setting leaves the
code one misconfigured environment variable away from an unauthenticated login
as any lead. `test_login.py::TestTheNameDoorIsGone` is the tripwire: it asserts
the route does not resolve, that `POST /login/name/` is a 404, and that no member
UUID appears anywhere in the login page. If any of those fail, the hole is back.

**Adding a new person is a join code.** A lead reads the code out; the new joiner
signs in with their BITS Google account and enters it. Both halves are required
and neither is sufficient: **Google decides *who*, the code decides *which
team*.** There is deliberately no field to type an address into on `/join/` —
that would be a field an attacker could type into — so the address is always the
one Google signed, carried through the session.

Codes are 10 characters from a 28-symbol alphabet (~48 bits) that omits O/0,
I/1/L and U/V, because a code dictated across a room and typed back wrong is a
support request. `/join/` allows 10 attempts per session; a lead can issue a new
code from the team page at any time and the old one dies immediately, which is
the answer to a code that has been overheard or screenshotted.

**Why members can edit contacts at all.** They are the ones actually in conversation
with their prospects, so they are the first to learn that a designation changed
or a name was misspelt. Making them file a request to a lead guarantees the pool
stays wrong. Scoping it to their own list means a stale row in someone else's
list is still not theirs to touch.

---

## 5. Data model

Everything inherits `TimeStampedModel`: UUID primary key, `created_at`,
`updated_at`. UUIDs rather than sequential integers so a contact id in a URL
leaks nothing about pool size.

### `team_members`
| Field | Type | Notes |
|---|---|---|
| `name` | Char(120) | |
| `bits_email` | Email | unique, validated as a BITS address |
| `phone` | Char(10) | regex-validated, optional |
| `linkedin` | URL | optional |
| `sender_name` | Char(120) | what recipients see in the From line; blank falls back to `name` |
| `batch` | Char(4) | indexed; **display only** — permissions come from `team_memberships` |
| `is_active` | Bool | |
| `user` | OneToOne → Django `User` | `SET_NULL`; null for everyone who never touches `/admin/` |

### `teams`
| Field | Type | Notes |
|---|---|---|
| `name` | Char(120) | |
| `join_code` | Char(32) | unique, indexed — 10 chars generated, rotatable, see §4.1 |
| `default_footer` | Text | seeds a member's footer when their sub-campaign is created |
| `is_active` | Bool | a deactivated team's leads keep nothing (§4) |
| `created_by` | FK → TeamMember | `SET_NULL` |

### `team_memberships`
| Field | Type | Notes |
|---|---|---|
| `team` / `member` | FK / FK | `CASCADE`; **`UNIQUE(team, member)`** |
| `role` | Char(16) | `lead` or `member` — the entire permission system |
| `joined_at` | DateTime | |
| `is_active` | Bool | removal deactivates rather than deletes, so the audit trail survives |

### `gmail_credentials`
One row per member, `OneToOne → TeamMember`. This is what replaced the laptop
agent's `token.json`.

| Field | Type | Notes |
|---|---|---|
| `refresh_token_encrypted` | Binary | Fernet, key from `GMAIL_TOKEN_KEY` |
| `access_token_encrypted` / `access_token_expires_at` | Binary / DateTime | cached, or every mail costs an extra round trip to Google |
| `key_version` | SmallInt | which `GMAIL_TOKEN_KEY` encrypted this row — makes rotation resumable |
| `granted_scopes` | JSON list | what Google *actually* granted; a member can untick a scope |
| `google_email` | Email | must match `bits_email`; the grant is refused otherwise |
| `granted_at` / `last_refreshed_at` | DateTime | |
| `identity_verified_at` | DateTime | cleared on reconnect; forces one `getProfile` before the next send |
| `last_error` / `revoked_at` | Text / DateTime | what the "Gmail not connected" banner reads |

**Be exact about what the encryption buys.** The key lives in the same
environment as the app, so an application compromise yields every token
regardless of it. It protects against **database disclosure only** — a leaked
Supabase dump, a mis-scoped backup — which is a real threat and not the same as
"tokens are safe".

### `api_tokens`

**Vestigial.** The table still exists; nothing reads it. It authenticated the
laptop agent's HTTP API, which is gone (§10). Migration `0015` set `revoked_at`
on every live row, so a token still sitting on somebody's laptop reaches nothing
even if a route were reintroduced by accident. It is kept rather than dropped
because a dropped table is the one migration with no cheap rollback; it goes in
a later cleanup.

### `contacts`
| Field | Type | Notes |
|---|---|---|
| `first_name` / `last_name` | Char(80) | |
| `email` | Email | **unique — the dedupe key for CSV import** |
| `phone_no` | Char(10) | regex-validated |
| `linkedin` | URL | |
| `company` | Char(160) | indexed |
| `designation` | Char(160) | |
| `assigned_to` | FK → TeamMember | `SET_NULL`, indexed |
| `assigned_at` | DateTime | |
| `last_contacted_by` / `last_contacted_at` | FK / DateTime | written on every send |
| **`lifecycle`** | Char(16) | indexed, **server-owned** — see §6 |
| **`tags`** | Postgres array of Char(40) | GIN-indexed, free-form |
| **`is_archived`** | Bool | indexed |
| **`archived_at` / `archived_by`** | DateTime / FK | |
| **`created_by`** | FK → TeamMember | who added it |

Indexes: `(assigned_to, company)`, `(is_archived, lifecycle)`, GIN on `tags`.

### `contact_notes`
Append-only free text: `contact` (CASCADE), `author`, `body`. A separate table
rather than a column so we keep who wrote what and when.

### `contact_audits`
| Field | Type |
|---|---|
| `contact` | FK, `CASCADE` |
| `actor` | FK → TeamMember, `SET_NULL` |
| `field` / `old_value` / `new_value` | Char(40) / Text / Text |

One row per field changed. Written **only** by `services/contacts.py`, so a new
view cannot mutate a contact without leaving a trace.

### `campaigns`
| Field | Type | Notes |
|---|---|---|
| `title` | Char(200) | |
| `mail_sub` / `mail_body` | Char(300) / Text | support `{{ variable }}` and `[words](url)` links |
| `is_html` | Bool | body is raw HTML — see [§5.1](#51-links-and-html-in-a-body) |
| `var_list` | JSON | declared variables, cross-checked against the template |
| `status` | Char(16) | indexed — `draft → active → paused → completed → archived` |
| `created_by` | FK | |

**Only `active` campaigns can be mailed.** Flipping a *root* to `paused` stops
every sub-campaign under it, mid-batch, for everyone. That is the emergency
brake, and §5.0 explains why it has to be checked on the root as well as on the
sub-campaign.

### `scheduled_sends` — mail queued for later

| Field | Type | Notes |
|---|---|---|
| `campaign` / `member` | FK `PROTECT` | `member` is whose Gmail sends it — and `claim_due` is per member, so nothing else can |
| `contact_ids` | UUID array | snapshot of the selection |
| `cursor` | int | index of the next contact to attempt |
| `scheduled_at` | DateTime | indexed; the due query runs every 60s |
| `status` | Char(12) | `pending` / `running` / `held` / `done` / `cancelled` / `missed` / `failed` |
| `cc` / `bcc` | Char | validated by the same `parse_copy_addresses` as a manual send |
| `batch_size` / `interval_minutes` / `next_run_at` | | drip; 0 sends the lot at once |
| `leased_by` / `lease_expires_at` | | crash recovery |
| `sent_count` / `skipped_count` / `attempts` / `last_error` | | progress |

Progress is a **cursor**, not "contacts that still lack a mailing": a permanently skipped contact
never gets a mailing row, so that query would leave a job running forever.

### `follow_up_rules` — chasing silence

| Field | Type | Notes |
|---|---|---|
| `campaign` → `follow_up` | FK | unique together; a campaign cannot follow up on itself |
| `delay_days` | int | days of silence before the follow-up is queued |
| `mark_replied` | Bool | **opt-in**: also move the contact to `replied` when a reply is seen |
| `is_active` | Bool | |

`campaign_mailings` gains `replied_at`, `reply_checked_at` and `followed_up_at` to support this.

### `campaign_mailings` — the unit of idempotency
| Field | Type | Notes |
|---|---|---|
| `campaign` / `contact` / `sent_by` | FK | **all `PROTECT`** |
| `root_campaign` | FK, **NOT NULL** | the root of `campaign`, derived in `save()`. What makes the guarantee team-wide. |
| `mail_thread_id` / `mail_message_id` | Char(120) | from Gmail |
| `status` | Char(8) | `draft` / `sent` / `failed` |
| `rendered_subject` / `rendered_body` | Text | **snapshot of what actually went out** |
| `rendered_body_html` | Text | the HTML alternative — what most recipients actually see |
| `from_name` / `cc` / `bcc` | Char | the rest of the envelope, snapshotted for the same reason |
| `error_detail` | Text | |
| `sent_at` | DateTime | |

```
constraints: UNIQUE(root_campaign, contact)  name="uniq_root_campaign_contact"
             UNIQUE(campaign, contact)       name="uniq_campaign_contact"
indexes:     (campaign, status), (sent_by, sent_at)
```

**Why there are two.** `uniq_campaign_contact` was the original guarantee and it
had a hole: it is scoped to ONE campaign. Every member had their own campaign so
they could have their own footer — so once Aarav had mailed a company under his,
Kabir's campaign held no row for that contact and he could mail the same prospect
again under a different banner. `uniq_root_campaign_contact` closes it: one mail
per contact per **root** campaign, across every member's sub-campaign at once.

The old one is kept even though the new one implies it. It costs an index and it
is what `check_db` has always asserted; a redundant index is far cheaper than a
weakened guarantee.

**`NOT NULL` on `root_campaign` is load-bearing, not tidiness.** Postgres unique
indexes treat NULLs as distinct, so with a nullable column two rows with a null
root and the same contact would *both* insert. A nullable root is not a weaker
guarantee — it is no guarantee. `check_db` asserts the nullability directly
against `information_schema`, not just the index.

Four more deliberate choices here:

- **`rendered_*` snapshots.** Campaign templates change. Without these we could
  never answer "what did we actually send this person?"
- **`PROTECT` on `contact`.** A contact that has ever been mailed cannot be
  deleted — that would destroy the record. This is why archiving exists.
- **`root_campaign` is derived in `save()`, not passed by callers.** Forgetting
  to pass it would not fail loudly; it would insert a NULL root, which the unique
  index ignores. A silently unprotected row is exactly what the field exists to
  prevent, so it is not left to discipline.
- **`related_name="root_mailings"`, not `mailings`.** That name belongs to
  `campaign`, and every funnel aggregate depends on it meaning that. Note the
  flip side: a campaign page counting `campaign.mailings` now reports almost
  nothing, because mail goes out under sub-campaigns — the dashboard, the
  campaign list and the campaign detail page all count `root_mailings`.

### 5.0 Campaign hierarchy: roots and sub-campaigns

Campaigns are **exactly two levels deep**.

A **root** campaign owns the subject, the body, the variables and the status —
what the team is saying, and whether it may be said at all. A **sub-campaign**
belongs to one member, has one root, and owns exactly one thing: that member's
`footer`. Nothing else about it is editable, because everything else is the
team's message rather than the sender's.

```
Ignite  (root: subject, body, status, follow-up rules)
├── Ignite — Kabir   (footer: "Kabir Rao | PIEDS")
├── Ignite — Ishita  (footer: "Ishita Nair | PIEDS")
└── Ignite — Aarav   (footer: ...)
```

- Sub-campaigns are created **on demand**, the first time a member sends or
  edits their footer. Nobody has to be "set up", and a member joining mid-campaign
  needs no bookkeeping.
- A sub-campaign carries **no copy** of the subject or body. `render()` reads both
  from the root; duplicating them would create a second copy that goes quietly
  stale the moment a lead edits the root.
- **Status lives on the root.** Pausing "Ignite" stops every member's queue at
  once — `load_sendable_campaign` and `scheduling.is_runnable` both check the
  root as well as the sub-campaign. Without that, the emergency brake would stop
  one queue out of fifteen.
- **A follow-up campaign must be a sibling root, never a child.** Follow-ups work
  by mailing the same contact under a second campaign, which uniqueness forbids
  within one root — so a follow-up rooted under the campaign it chases could
  never queue anything, silently, forever. `FollowUpRule.clean()` refuses it. As a
  bonus, "Ignite" and "Ignite follow-up" being two roots makes follow-up dedupe
  team-wide for free.
- **Migrations must never create a parent link.** The backfill in `0011` is safe
  precisely because every pre-existing campaign is its own root, making
  `root_campaign_id := campaign_id` the identity map on a pair already guaranteed
  unique. Reparenting two existing campaigns under one root would collapse every
  contact they both mailed onto one `(root, contact)` pair — so
  `campaigns.set_parent()` runs a collision query and refuses first, naming the
  contacts. There is no database-level way to catch this at reparent time: the
  constraint fires on the *next* insert, long after the damage.

**Footers carry no `{{ }}` placeholders**, and that restriction protects
something. `validate_template` demands *set equality* between `var_list` and the
placeholders actually used — a member's footer saying `{{ company }}` would be
"undeclared" against a `var_list` on a root they cannot edit, forcing that
equality down to a subset check and destroying the typo detector the function
exists for. A footer is a signature.

`footer_is_html` is **lead-only**, for the reason in §5.1: `richtext.py` ships
without an HTML sanitiser on the explicit grounds that raw HTML is written only
by leads. Members get the markdown-lite subset, which already handles links.

### 5.1 Links and HTML in a body

Every mail goes out as `multipart/alternative`: a `text/plain` part and a
`text/html` part built from the same body, so a client that refuses HTML still
gets something readable. `services/richtext.py` owns both conversions.

**Links on chosen words** work in any campaign. Write

```
Want to [book a call](https://cal.com/pieds)?
```

and the recipient sees *book a call* as a link, with the plain part reading
`book a call (https://cal.com/pieds)`. The campaign form has an **Insert link**
button that wraps whatever you have selected. Only `http://` and `https://` are
accepted — `javascript:` and `data:` are rejected at save, not at send.

**Raw HTML** is opt-in per campaign via the **Body contains HTML** checkbox, for
footers, dividers, and inline styling:

```html
<p>Hi {{ first_name }},</p>
<hr>
<footer style="color:#888;font-size:12px">PIEDS, BITS Pilani</footer>
```

With it **off** (the default) the body is escaped and blank lines become
spacing, so a `<` or `&` in ordinary prose is safe. With it **on** you own the
markup — write your own `<br>` or `<p>`, because line breaks are no longer
inserted for you — and the plain-text part is generated by stripping the tags.

The escaping guarantee is what lets this project ship without an HTML sanitiser.
In the default mode nothing untrusted reaches the output. In HTML mode the trust
moves rather than vanishing: campaign editing is lead-only, the CRM previews the
result in a `sandbox=""` iframe, and `<script>` and inline event handlers are
refused at save — they would only make the preview lie, since every mail client
strips them anyway.

### 5.2 Sender name, CC and BCC

**Sender name.** A cold mail from `f20251097@pilani.bits-pilani.ac.in` is far
less likely to be opened than one from *Pratham Jain*. Each member has a
`sender_name`, edited by a lead in the **Sends as** column of the Team page, and
blank falls back to their real name. It is resolved per claim rather than cached
anywhere, so editing it takes effect on the very next mail with nothing to
restart. `GmailClient.send` builds the header with `email.utils.formataddr`,
which quotes and encodes names that need it.

> If a recipient still sees the wrong name, check the Gmail account's own
> "Send mail as" setting — Gmail can override the header we set.

**CC / BCC** are entered on the Send screen and apply to **every mail in that
batch** — ten copied addresses on a 200-mail send is two thousand extra
deliveries, and a CC is visible to each prospect. They are not applied where
they are typed: they are passed into `claim_batch`, which validates every
address, caps the list at `MAX_COPY_ADDRESSES` (10) and stores them on each
`campaign_mailings` row before anything is sent. A malformed address fails the
whole claim before a single row is written, and the stored copy is what the
sender reads — so the record of who was copied is the same object the mail was
built from, not a parallel one. Preflight echoes back the addresses the server
accepted.

### 5.3 Scheduled sending

Full detail in **`docs/MAIL_SCHEDULING.md`**. The short version, because one
constraint shapes everything:

**The Gmail API has no `sendAt`.** Gmail's "Schedule send" is a feature of the
web client, not the API. There is no way to hand Google a future time and walk
away, so a scheduled mail requires a process that is *awake at that moment
holding that member's Gmail token*. The server is now that process (§2), which
is the whole reason the migration was worth doing.

So the server owns the queue, every rule **and** the credentials. A due job is
picked up by `services/runner.py::tick()`, which leases it, sends it, and
records progress. `scheduling.claim_due(member, …)` stays **per member**
deliberately even though the server has every credential: the `member` filter is
what guarantees a job sends from the mailbox it was queued against, and `sent_by`
stops meaning anything without it. The tick loops over members rather than
widening that query.

> **The sending window ships disabled.** `SCHEDULE_WINDOW_START == _END == 0`,
> so mail goes out whenever it is queued, at any hour. It defaulted to
> 09:00–19:00 and that surprised people: a send pressed at 20:00 went `HELD`
> rather than out, which reads as "the button did nothing". The machinery is
> intact — set `SCHEDULE_WINDOW_START=9` and `SCHEDULE_WINDOW_END=19` to turn it
> back on, which is worth doing before the first large campaign to real
> prospects.

> **Read this before queueing anything.** `tick()` is not yet on a timer. A due
> job waits until someone runs it — the **Send queued mail now** button on the
> Schedules page, or `manage.py run_tick`. See [§13.3](#133-draining-the-send-queue),
> and [§23](#23-known-gaps) for what is left to make it hands-off.

Everything else is built on that: a **sending window** so nothing arrives at 3am
(shipped **off** — see below),
a **grace period** after which a job is `missed` rather than stale, **drip** to
spread a batch, and **follow-ups** that chase silence using the Gmail thread the
original mail created.

---

## 6. Lifecycle and tags

Two separate things, deliberately kept apart.

### `lifecycle` — server-owned funnel state

```
new ──(first confirmed send)──► contacted
                                    │
                       (set by hand by a lead)
                                    ▼
                    replied · bounced · do_not_contact
```

**The only automatic transition is `new → contacted`**, applied in
`services/mailing.py::record_result()` the instant a send is confirmed. This is
what "changes the moment they mail" means, and it happens inside the same
transaction that records the send.

It is scoped to contacts sitting at `new`:

```python
Contact.objects.filter(
    id=mailing.contact_id, lifecycle=ContactLifecycle.NEW.value
).update(lifecycle=ContactLifecycle.CONTACTED.value, updated_at=now)
```

**Why the filter matters.** Without it, a second campaign would silently
overwrite a `replied` that someone set by hand after an actual conversation —
destroying the one piece of information a human added. A later campaign must
never drag a contact backwards.

`replied`, `bounced` and `do_not_contact` stay manual. Inferring "bounced" from
an SMTP error string is guesswork we would later have to un-guess.

### `tags` — free-form labels

`fintech`, `priority`, `iit-b`, `warm-intro`. Lowercased and de-duplicated on the
way in, so `Fintech` and `FINTECH` cannot become two separate filter facets for
the same idea. Editable by a lead or by the assigned owner. Filterable on
`/contacts/` and `/assign/`, and stored as a real Postgres array so
`tags__contains=["fintech"]` uses the GIN index.

### Blocked states actually block

`is_archived`, `do_not_contact` and `bounced` are **refused by `claim_batch`**
under the row lock — not merely hidden from a list:

```python
def unmailable_reason(contact) -> tuple[str, str] | None:
    if contact.is_archived:
        return ARCHIVED, "archived"
    if contact.lifecycle in BLOCKED_LIFECYCLES:
        return BLOCKED, f"marked {contact.get_lifecycle_display().lower()}"
    return None
```

`preflight()` calls the **same helper**, so the dry run cannot disagree with the
real thing about who is sendable. The check runs under the lock rather than
trusting the posted list, because someone may have archived the contact between
the page loading and Send being pressed.

---

## 7. The send protocol

### The claim → send → report loop

```
services/sending.py::send_batch, ten contacts at a time:

1. mailing.claim_batch(campaign, member, chunk)

2. ONE TRANSACTION PER CONTACT:
     SELECT ... FOR UPDATE the contact          ← row lock acquired
     assigned to this member?         no → skip NOT_ASSIGNED
     archived / blocked lifecycle?   yes → skip ARCHIVED | BLOCKED
     render(root body + this member's footer)
                                    fail → skip MISSING_VARS
     INSERT CampaignMailing(DRAFT)         ← both unique constraints fire here
     COMMIT                                 ← lock released, DRAFT durable
   ────────────────────────────────────────────────────────────────────
3. GmailClient.send(), as the member                ← NO locks held anywhere

4. mailing.record_result(mailing_id, member, ...)
     status="sent"   + message_id, thread_id  → SENT  + lifecycle flip
     status="failed" + error                  → FAILED + error_detail
```

This was four steps over HTTP when a laptop agent did the sending. Removing the
transport removed a network hop, not a layer of logic: the ordering below is
unchanged, and it is the ordering — not where the code runs — that is the
guarantee.

### Why this exact ordering

- **DRAFT commits before the mail leaves.** The reverse — send first, record
  after — could lose the record of a mail that actually went out. That is
  unrecoverable; an orphaned DRAFT is merely annoying.
- **The lock is released at step 2's commit**, before the multi-second Gmail
  round trip. Holding it across the network would serialize the entire team
  behind one slow send.
- **Retry UPDATEs the existing row.** Inserting a second one is physically
  impossible. That is precisely what makes pressing Send twice safe.
- **One transaction per contact**, not one per batch. A single bad contact
  doesn't roll back the other 199.

### Stranded drafts

A `draft` is **not a mail in flight**. It means an agent reserved that contact
and the server never heard back. Nothing will happen to it on its own, and until
it is resolved that contact **cannot be mailed for that campaign again** — the
unique constraint that prevents double-sending also prevents re-sending.

`services/sending.py::reconcile` resolves them by asking Gmail which ones
actually went out, using `GmailClient.find_message_to()`. Anything that did is
recorded as sent; anything that did not becomes `failed`, and a failed mailing
**can** be claimed again — so re-selecting those contacts and pressing Send
simply works. It never re-sends blindly, because a DRAFT may already be sitting
in a prospect's inbox with only the report lost.

`manage.py stranded_drafts` reports the backlog and names whose mailbox each one
belongs to — reconciliation runs against that member's Gmail credential, so the
answer comes from the mailbox the mail would have left.

### Daily cap

`DAILY_SEND_CAP = 400`, enforced inside `claim_batch` by counting
`sent_last_24h(member)`. Server-side deliberately — it counts across every device
a member uses, and across the whole team's shared instance.
Gmail's real per-account quota, once tripped, throttles the whole mailbox for
hours.

### Outcome codes

Shared by the API and both UIs (`services/mailing.py`):

| Code | Meaning |
|---|---|
| `OK` | sendable |
| `ALREADY_MAILED` | a mailing already exists for this (campaign, contact) |
| `NOT_ASSIGNED` | not assigned to the caller |
| `MISSING_VARS` | template needs a field this contact leaves blank |
| `ARCHIVED` | contact is archived |
| `BLOCKED` | lifecycle is `do_not_contact` or `bounced` |
| `CAP_REACHED` | daily cap exhausted |
| `SENT` / `FAILED` | result of the actual send |

---

## 8. Staying in sync

One master database now has the whole team writing to it from two apps, so an
open tab goes stale within seconds of someone else's edit.

Both apps poll every **20 seconds** and on tab focus
(`shared/static/basecoat/poll.js`). Deliberately not websockets: that would mean
a second auth system and a Supabase key in every browser, to save nineteen
seconds.

Behaviours that make it usable rather than annoying:

- **Pauses during a send.** The NDJSON result stream is writing per-row statuses;
  a refresh mid-stream would rebuild the table underneath it.
- **Pauses while an edit dialog is open.** Otherwise a refresh wipes fields
  someone is halfway through typing.
- **Preserves checkbox selections** across a refresh. Losing a 40-contact
  selection to a background poll would make the feature worse than not having it.
- **Stops when the tab is hidden**, and refreshes immediately on focus.
- **Shows "updated 12s ago"**, so a frozen tab looks frozen.
- **Fails silently.** A failed poll logs to console and waits for the next one;
  it never interrupts anyone with an alert.

Event listeners on polled tables are **delegated**, not bound per row — a refresh
replaces the rows and would otherwise take the listeners with them.

---

## 9. Screens

### Django CRM — `http://localhost:8000`

| Route | Screen | Who |
|---|---|---|
| `/` | Dashboard — pool size, per-campaign funnel, recent failures | any member |
| `/contacts/` | Searchable list; filter by company, assignee, **tag, stage, archived**; bulk edit | any member |
| `/contacts/new/` | Add one contact by hand | any member |
| `/contacts/<id>/` | Detail — mail history, notes, **change log** | any member |
| `/contacts/<id>/edit/` | Edit | owner or lead |
| `/contacts/<id>/archive/` | Archive / restore | owner or lead |
| `/contacts/<id>/delete/` | Permanent delete | **lead**, never-mailed only |
| `/contacts/bulk-edit/` | Apply one change to many | any member (scoped) |
| `/contacts/import/` | CSV upload → preview → commit | **lead** |
| `/assign/` | Bulk-assign contacts to members | **lead** |
| `/campaigns/` | List with sent/failed counts | any member |
| `/campaigns/<id>/` | Funnel, live preview, status transitions | any member |
| `/campaigns/new/` · `/campaigns/<id>/edit/` | Template editor: placeholder validation, **Insert link**, HTML toggle | **lead** |
| `/schedules/` | Every scheduled send; `missed`/`failed` called out; **Send queued mail now** for any member, lead-only cancel | member |
| `/send/` | Your queue for a campaign → dry run → **Send** | any member |
| `/campaigns/<id>/footer/` | Your own sign-off on a campaign | any member |
| `/teams/` | The teams you are on | any member |
| `/teams/<id>/` | Join code, roles, campaigns | **lead** |
| `/teams/<id>/distribute/` | Round-robin a filtered slice across the team | **lead** |
| `/settings/gmail/` | Connect / disconnect your Gmail | any member |
| `/members/` | Team load, **Sends as** names, Gmail status | **lead** |
| `/login/` | Sign in with Google | anyone |
| `/join/` | Enter a team join code (after Google sign-in) | verified BITS account |
| `/healthz` | Liveness probe — no auth, no DB query | anyone |
| `/admin/` | Django admin | superuser (password auth, separate) |

---

## 10. HTTP API — removed

There is no `/api/v1/` any more. It existed so a laptop agent could claim
mailings, report results and lease scheduled sends over HTTP; the server now
does all of that in-process (`services/sending.py`, `services/runner.py`).

**Deleting it was a safety decision, not tidying.** Leaving `mailings/claim`
reachable while the server also sends means two independent senders can hold the
same DRAFT row — and `record_result`'s "already settled" guard would then turn a
real double-send into a silently ignored report. A token-authenticated surface
with no consumer is attack surface with no upside.

The `ApiToken` table is kept for now as a record of who was issued what, and
migration `0015` revoked every live token. Its UI, its `manage.py issue_token`
command and all of its routes are gone. Drop the table in a later cleanup —
that is the one migration with no cheap rollback.

## 11. Services — where the rules live

Every rule lives in `core_django/crm/services/`. Change behaviour here and it
takes effect for everyone immediately — there is nothing on anyone's laptop to
update, which is the main practical dividend of retiring the agent.

| Module | Contents |
|---|---|
| `mailing.py` | `claim_batch`, `record_result`, `preflight`, `unmailable_reason`, `stranded_drafts`, `reset_for_retry`, `sent_last_24h`, `load_sendable_campaign`, `parse_copy_addresses`, `DAILY_SEND_CAP`, `MAX_COPY_ADDRESSES` |
| `contacts.py` | `create`, `update`, `set_archived`, `hard_delete`, `bulk_edit`, `clean_tags`, `lifecycle_counts`, `all_tags`, `EDITABLE_FIELDS`, `LEAD_ONLY_FIELDS` |
| `permissions.py` | `is_lead`, `lead_required`, `member_required`, `can_edit_contact`, `can_set_lifecycle`, `can_hard_delete`, `editable_contacts` |
| `assignment.py` | `bulk_assign` (with the reassign guard), `bulk_unassign` |
| `campaigns.py` | `validate_template`, `transition`, `extract_placeholders`, `ALLOWED_VARIABLES` |
| `importer.py` | `parse` → `ImportPreview`, `commit` |
| `render.py` | `render`, `contact_context`, `MissingVariables` |
| `richtext.py` | `to_html`, `to_plain`, `validate_links`, `validate_markup`, `extract_links`, `LINK_RE` |
| `scheduling.py` | `create`, `claim_due`, `record_progress`, `cancel`, `reschedule`, `sweep_expired_leases`, `sweep_missed`, `in_window`, `next_open_slot`, `deliver_after`, `deadline` |
| `followups.py` | `threads_to_check`, `record_reply_scan`, `queue_follow_ups`, `run_all_rules`, `cancel_pending_for` |
| `auth.py` | `login_member`, `current_member`, `name_login_allowed`, `member_from_google_callback`, `SESSION_KEY` |

Two guards worth knowing about:

**The reassign guard** (`bulk_assign`) refuses to move a contact that already has
mailings under another member, unless `force=True`. The existing owner may be
mid-conversation, and silently moving the contact would strand that thread with
nobody watching for the reply. Skipped contacts surface in the UI with a
"reassign anyway" option.

**The delete guard** (`hard_delete`) refuses a contact with mail history. `PROTECT`
would reject it at the database anyway — but as a 500, not as an explanation.

Template variables available: `first_name`, `last_name`, `full_name`, `email`,
`company`, `designation`. `validate_template()` cross-checks every `{{ … }}`
against that set **and** against the campaign's declared `var_list`, so
`{{ compnay }}` fails at save rather than at send.

---

## 12. Setup

### 12.1 Docker — the whole CRM in one command

```bash
git clone <repo> && cd ignite_crm
cp .env.example .env               # Google/Gmail values can wait until §15

docker compose up --build
```

`.env.example` ships with `COMPOSE_PROFILES=localdb`, which is what starts the
local Postgres container. Keep that line for local development; drop it when you
move to a hosted database (§14).

That is everything: Postgres 16, migrations, `check_db`, `seed_dev`, and
gunicorn on <http://localhost:8000>. Nothing is installed on the host — no venv,
no Python version to match. One container, because there is only one process
now. See §13.1 for what it actually does.

Requires only Docker. If another project already owns port 5432, run
`PG_HOST_PORT=5442 docker compose up --build` — that changes the *host* port
only; nothing inside the stack notices.

### 12.2 Native — for working on the code

```bash
git clone <repo> && cd ignite_crm

python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # runtime deps + pytest
cp .env.example .env               # then edit it — the suite needs DJANGO_DEBUG=True

docker compose up -d               # Postgres 16 on :5432

cd core_django
../.venv/bin/python manage.py migrate
../.venv/bin/python manage.py check_db          # verify the constraint landed
../.venv/bin/python manage.py seed_dev          # dev data
../.venv/bin/python manage.py createsuperuser   # optional, for /admin/ only
```

`seed_dev` creates one team (`PIEDS Outreach`, join code `DEVCODE123`) and four
members on it — `aarav`, `diya` (leads) and `kabir`, `ishita` (members) — plus
~50 contacts with assorted tags and
lifecycles, and one active campaign. **Everyone signs in with Google**, so
configure `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` (§15.1) before
expecting to get in — there is no name-dropdown fallback any more, and §4.1
explains why it was deleted rather than kept for local convenience.

---

## 13. Running it

### 13.1 Docker

```bash
docker compose up --build          # postgres + CRM   → http://localhost:8000
```

One image (`./Dockerfile`), one service. It used to ship two apps and let compose
pick between them with an entrypoint; the sending agent is gone, so the image's
`CMD` now carries the full gunicorn argv and compose overrides nothing. That
matters: it means compose and the hosting platform start the container
identically, which is the only way "it works in compose" means anything.

`docker/entrypoint-crm.sh` runs before gunicorn and, in order: prints the
database it is about to use, blocks on `pg_isready`, migrates *if the database is
local*, then **`check_db` — and refuses to boot if it fails.** A container that
came up without `uniq_campaign_contact` could double-mail a prospect, so not
starting is the correct outcome. `seed_dev` runs last, under the same
local-only rule. Both rules are spelt out just below.

| Variable | Default in compose | |
|---|---|---|
| `COMPOSE_PROFILES` | `localdb` in `.env.example` | runs the local Postgres container; drop it once `DATABASE_URL_DOCKER` points at Supabase |
| `PG_HOST_PORT` | `5432` | host-side Postgres port; change it on a collision |
| `RUN_MIGRATIONS` | *auto* | `true` only if the database is local — see below |
| `SEED_DEV` | *auto* | same rule |
| `DJANGO_DEBUG` | `True` | `False` turns on `SECURE_SSL_REDIRECT`, which bounces plain-http localhost to https |
| `DATABASE_URL_DOCKER` | `…@postgres:5432/…` | point it at Supabase to skip the local Postgres |

#### Migrating and seeding decide themselves

Both default to **whether `DATABASE_URL` names a local database** (`localhost`,
`127.0.0.1`, `::1`, or the `postgres` service), rather than to a flag somebody
has to remember to flip:

| | local Postgres | shared hosted database |
|---|---|---|
| `migrate` | every boot | **skipped** — set `RUN_MIGRATIONS=true` on the one machine that owns the schema |
| `seed_dev` | every boot | **skipped** — and `seed_dev` itself refuses a non-local host without `--force` |
| `check_db` | **always** | **always** |

Skipping migrations is safe precisely because `check_db` is not skipped: a
container pointed at a database whose schema was never built exits non-zero with
`MISSING campaign_mailings.uniq_campaign_contact` rather than serving a CRM that
can double-mail. The guard is in `seed_dev.py` as well as the entrypoint,
because someone typing the command by hand deserves the same protection.

`DJANGO_ALLOWED_HOSTS` gets `crm` **appended**, not defaulted — Django validates
the `Host` header, and a `.env` listing only `localhost` must not be able to drop
the compose service name.

### 13.2 Native

Two terminals, from the repo root:

```bash
# 1. database
docker compose up -d postgres

# 2. the CRM                                    → http://localhost:8000
cd core_django && ../.venv/bin/python manage.py runserver 8000
```

`seed_dev` creates the team `PIEDS Outreach` with join code `DEVCODE123`. Sign
in at <http://localhost:8000/login/> with a BITS Google account — set
`GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` first, or the door says
so rather than degrading to something weaker.

| Seeded member | Role | Sees |
|---|---|---|
| Aarav, Diya | lead | everything |
| Kabir, Ishita | member | 403 on `/assign/`, `/contacts/import/`, `/campaigns/new/`, `/teams/<id>/` |

### 13.3 Draining the send queue

**Pressing Send does not send.** It commits a `ScheduledSend` due now and
returns, deliberately: a Render instance can be reaped mid-request, so streaming
a 200-mail batch inside one HTTP response makes "the tab was closed" a data
question. Queueing makes it a non-event and reuses the lease, drip and recovery
machinery that scheduled sends already had.

Something must then drain the queue. Two doors, one code path
(`services/runner.py::tick()`):

| | |
|---|---|
| **Send queued mail now** on `/schedules/` | **any member**, POST — what the team actually uses |
| `manage.py run_tick` | the same function from a shell, for local runs |

A tick is bounded — `TICK_MAX_MAILS` (40) and `TICK_MAX_SECONDS` (45) — so it
fits inside a request. Press it again for more. It is safe to press twice at
once: `claim_due` uses `SELECT … FOR UPDATE SKIP LOCKED`, and
`uniq_root_campaign_contact` is the real guarantee underneath regardless.

> **This is the one thing that is not hands-off yet.** The automatic scheduler —
> an authenticated `/internal/tick` endpoint, a `pg_try_advisory_lock`, and an
> external pinger — is designed in `docs/MAIL_SCHEDULING.md` and deliberately
> **deferred**. Nothing about it changes the queue, the lease protocol or
> `tick()` itself; it adds a door and a lock in front of an executor that
> already exists and is already tested. Until it lands, mail leaves the building
> when *somebody* presses the button — any member, not just a lead. That widening
> is not a convenience: while the scheduler is deferred this button **is** the
> send path, and gating it on a role meant a member pressed Send, watched their
> mail sit in `queued`, and had no way to move it. It grants no new power —
> `tick()` sends each job with its own owner's Gmail credential — and leads keep
> the brake (cancel a job, pause the root). See [§23](#23-known-gaps).

---

## 14. Supabase — the master database

Supabase replaces the Docker Postgres. It changes nothing about the
architecture: Django is the only process that connects to it, and it always
was.

### 14.1 Use the session pooler

From **Project Settings → Database → Connection string → Session pooler**:

```
postgres://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
```

Three ways to get this wrong, worst first:

| Don't | Why |
|---|---|
| Port **6543** (transaction pooler) | `mailing.py` and `assignment.py` depend on `SELECT … FOR UPDATE`; psycopg3's prepared statements break there. If you must, also set `DB_TRANSACTION_POOLER=True`, which sets `prepare_threshold=None` and disables server-side cursors. |
| `db.<ref>.supabase.co` direct | IPv6-only without the paid IPv4 add-on. |
| Omitting `sslmode=require` | Every contact and token travels the public internet in the clear. |

`DB_CONN_MAX_AGE` defaults to `0`. Persistent connections eat pooler slots on the
free tier faster than traffic does.

### 14.2 What changes in `.env`

```diff
- COMPOSE_PROFILES=localdb                    # stop running a database nobody uses
+ DATABASE_URL_DOCKER=postgres://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
```

Nothing else. `RUN_MIGRATIONS` and `SEED_DEV` notice the host is not local and
switch themselves off (§13.1) — **on the deployed instance, which owns the
schema, set `RUN_MIGRATIONS=true` explicitly.** Forgetting it is the single most
likely deploy failure, and it presents as a broken app rather than a missing
variable: migrations are skipped, then the unconditional `check_db` refuses to
serve. See §16.

### 14.3 Cutover

```bash
cd core_django

# 1. build the schema on the far side
DATABASE_URL="<session-pooler-url>" ../.venv/bin/python manage.py migrate

# 2. VERIFY — migrations "succeeding" is not proof the constraint landed
DATABASE_URL="<session-pooler-url>" ../.venv/bin/python manage.py check_db

# 3. move existing data
pg_dump --data-only --no-owner "postgres://ignite:ignite@localhost:5432/ignite_crm" \
  | psql "<session-pooler-url>"

# 4. re-verify after the data load
DATABASE_URL="<session-pooler-url>" ../.venv/bin/python manage.py check_db
```

**Step 2 is not optional.** The entire no-double-mail guarantee is one index.

### 14.4 RLS

Django connects as `postgres`, which **bypasses RLS**. That is deliberate: Django
is the only client, and permissions are enforced in `services/permissions.py`.

Two consequences to respect:
- Never expose the Supabase `anon` or `service_role` keys to any frontend.
- Do not add a second direct-to-Postgres client without revisiting this decision.
  If a browser ever talks to Supabase directly, every rule in this repo is bypassed.

### 14.5 Tests never touch it

`config/settings.py` forces local Docker Postgres whenever pytest is running,
regardless of `DATABASE_URL`:

```python
RUNNING_TESTS = "pytest" in sys.modules or "test" in sys.argv
```

Django's test runner CREATEs and DROPs its database. Pointing that at production
would be unrecoverable. The guard lives in settings rather than `conftest.py`
because pytest-django calls `django.setup()` before root conftest files are
imported — an override there would be too late.

The pytest header prints which host it chose:

```
ignite: tests pinned to localhost:5432/ignite_crm (never the hosted database)
```

Verified by running the suite with `DATABASE_URL` pointed at a fake Supabase
host: all 317 tests still pass against localhost.

---

## 15. Google setup

**One OAuth client, one Google Cloud project.** There used to be two — a Desktop
client for the laptop agent's Gmail consent and a Web client for sign-in — and
mixing them up was the most likely thing to go wrong. The Desktop client is no
longer used at all and can be deleted once the cutover is proven.

The single **Web application** client does both jobs, through two separate
consent steps:

| Step | Scopes | When |
|---|---|---|
| Sign-in | `openid`, `userinfo.email`, `userinfo.profile` | every login |
| Gmail | `gmail.send`, `gmail.readonly` | once, at `/settings/gmail/` |

They are deliberately separate. A person should be able to sign in and look
around before handing over their mailbox, and a declined Gmail consent must not
lock them out of the CRM.

### 15.1 Creating the client

1. **Credentials → Create OAuth client ID → Web application**.
2. Authorised redirect URIs — **both**, for every hostname the app answers on:
   ```
   http://localhost:8000/login/google/callback/
   http://localhost:8000/settings/gmail/callback/
   https://<your-domain>/login/google/callback/
   https://<your-domain>/settings/gmail/callback/
   ```
3. Enable the **Gmail API** on the project.
4. Put the id and secret in the environment as `GOOGLE_OAUTH_CLIENT_ID` /
   `GOOGLE_OAUTH_CLIENT_SECRET`.

Leave them blank and both doors are disabled with a message rather than a stack
trace — but **nobody can sign in at all**. There is no second door.

### 15.2 The consent screen — set user type to Internal

This is the setting that decides whether the whole thing keeps working, and it is
worth getting right the first time.

| User type | Refresh tokens | Verification | Cap |
|---|---|---|---|
| **Internal** (Workspace org) | **never expire** | none needed | none |
| External + In production | persist | needed for `gmail.readonly` eventually | 100 unverified |
| External + **Testing** | **die after 7 days** | — | 100 |

**Set user type to Internal** if the Cloud project belongs to the
`pilani.bits-pilani.ac.in` Google Workspace organisation. Internal apps skip
verification entirely, have no user cap, and — the one that matters — their
refresh tokens do not expire. That last point removes the single largest
operational risk in this migration: an **External** app left in **Testing**
revokes every refresh token after **7 days**, so sending silently stops a week
after launch and nobody knows why.

Internal also permanently removes a future problem: `gmail.readonly` is a
*restricted* scope, which for an External app would eventually require a paid
third-party security assessment.

Three things to check, in this order:

1. **Does the Cloud project sit under the BITS Workspace org?** "Internal" only
   appears as an option if it does. If student accounts cannot create projects in
   the org, the project lands under *No organization* and the option is greyed
   out. This is the one that decides everything else.
2. **Has the Workspace admin blocked third-party Gmail access?** Education orgs
   commonly restrict it. You would see `access_denied` / "This app is blocked" on
   the first consent, so test with one account early.
3. Refresh tokens still die after **6 months of inactivity**, and there is a
   50-tokens-per-user-per-client cap. Neither matters at this usage, but the code
   does not assume a token lives forever — a dead credential is recorded on the
   row and the member is asked to reconnect.

### 15.3 What each member does

Once, in a browser: sign in, open **Gmail** in the sidebar, press **Connect
Gmail**, and grant both permissions. That is the whole setup — no Python, no
client secret, no `uvicorn`.

Granting from the wrong Google account is refused by name: the account that
grants is compared against the member's `bits_email`, because `sent_by` is only
meaningful if the token really belongs to the person it names.

---

## 16. Deploying

One **Render** web service on the free plan, Docker runtime, health check path
`/healthz`. `render.yaml` at the repo root is the blueprint; every value below is
either in it or marked `sync: false` for you to paste in.

### Why one service

Render's free plan allows **750 instance-hours per workspace per calendar
month** against a ~730-hour month. That budget covers **exactly one** always-awake
service — there is no room for a separate worker, which is why sending runs
inside the web process rather than beside it. Free plans also have **no cron
jobs** and **no shell**, which is why the queue is drained from a button in the
UI rather than a scheduled command.

### Environment

| Variable | Value |
|---|---|
| `DATABASE_URL` | Supabase **session pooler, port 5432**, `?sslmode=require` |
| `GMAIL_TOKEN_KEY` | `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` |
| `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | the Web client from §15 |
| `DJANGO_SECRET_KEY` | generated; the app **refuses to boot** on the shipped dev key |
| `DJANGO_ALLOWED_HOSTS` | `crm.example.com,.onrender.com` — a leading dot is Django's subdomain wildcard |
| `DJANGO_DEBUG` | `False` |
| `RUN_MIGRATIONS` | **`true`** — see below |
| `DB_CONN_MAX_AGE` | `0` |
| `WEB_CONCURRENCY` | `2` |

### The five things that will actually bite

1. **`RUN_MIGRATIONS=true` is not optional and is the most likely omission.**
   `docker/entrypoint-crm.sh` derives it from whether the database looks local,
   reads Supabase as shared, and therefore *skips* migrations — the rule that
   stops five laptops racing each other also stops your one server, and it has no
   way to tell the difference. The unconditional `check_db` then fails the boot.
   That failure is correct behaviour and will look exactly like a broken deploy.

2. **`GMAIL_TOKEN_KEY` must be set**, or `check_db` refuses to start. Deliberate:
   a CRM that boots and then cannot send is worse than one that will not boot.
   Generate a *different* key from your local one, and keep it in Render's
   environment — never in the database, since protecting against a database leak
   is the entire point of it.

3. **Never port 6543.** That is Supabase's transaction pooler, which silently
   breaks `SELECT … FOR UPDATE`. `check_db` refuses it outright.

4. **Deploying with `DEBUG=True` breaks Google sign-in in a confusing way.**
   `SECURE_PROXY_SSL_HEADER` is only set when `DEBUG=False`, so behind Render's
   TLS proxy `request.build_absolute_uri` produces an `http://` redirect URI that
   Google rejects. It presents as "Google is broken", not as a settings mistake.

5. **Both redirect URIs, for every hostname.** `/login/google/callback/` *and*
   `/settings/gmail/callback/`, on the `.onrender.com` host as well as your
   custom domain.

With `DEBUG=False`, `settings.py` enables `SECURE_SSL_REDIRECT` (exempting
`/healthz`, or the platform health check gets a 301 and marks every deploy
unhealthy), `SECURE_PROXY_SSL_HEADER`, secure cookies, one-year HSTS with
subdomains, `X_FRAME_OPTIONS=DENY`, `CSRF_TRUSTED_ORIGINS`, and
`CompressedManifestStaticFilesStorage`. Static files are built into the image by
`collectstatic` and served by whitenoise — no nginx.

The entrypoint is the release step: it waits for Postgres, migrates, and runs
`check_db` before gunicorn binds.

### Not yet automatic

**Queued mail does not send on its own.** Somebody presses **Send queued mail
now** on `/schedules/` — any member may, and it is safe to press twice. The automatic scheduler — an authenticated tick endpoint, a
session-level advisory lock, and an external pinger every 1–2 minutes — is
designed but not deployed; see `docs/MAIL_SCHEDULING.md`. The ping interval is a
*throughput* setting, not just a keep-alive one, so read that before choosing it.

---

## 17. Tests

```bash
.venv/bin/python -m pytest          # needs docker compose up
```

**317 tests**, all passing:

| File | Count | Covers |
|---|---|---|
| `test_scheduling.py` | 49 | the queue, the lease, the sending window, grace, drip, `tick()` |
| `test_teams.py` | 36 | join codes, roles, `assignable_members`, distribution, audit |
| `test_campaign_hierarchy.py` | 36 | roots and sub-campaigns, footers, root-scoped dedupe |
| `test_contacts_crud.py` | 32 | edit scoping, lifecycle rules, archive/delete, audit, HTTP layer |
| `test_gmail_credentials.py` | 29 | encryption, key rotation, connect/disconnect, identity binding |
| `test_campaign_headers.py` | 23 | sender name, CC/BCC validation and snapshot, HTML bodies |
| `test_campaign_links.py` | 17 | link syntax, escaping, scheme rejection, both body parts |
| `test_followups.py` | 17 | reply detection, who gets chased, the lifecycle opt-in |
| `test_mailing.py` | 16 | claim/report, preflight, stranded drafts, the daily cap |
| `test_constraints.py` | 16 | both unique constraints, `NOT NULL`, status transitions |
| `test_login.py` | 11 | the one door, and that the deleted one stays deleted (§4.1) |
| `test_send_recovery.py` | 9 | the 19 Aug incident: chunking, retry, no double-send |
| `test_bootstrap.py` | 14 | `bootstrap_team` — the first team, and every re-run of it |
| `test_oauth_pkce.py` | 6 | the PKCE verifier surviving between two requests |
| `test_run_queue_permission.py` | 6 | that draining the queue stays open to every member |

`test_mailing.py` used to be `test_mailing_api.py`, driven through the token API
the laptop agent spoke. Only the tests genuinely *about* the API — bearer-token
auth, HTTP status codes — were dropped; every rule they happened to exercise
through it was rewritten against the service functions directly, which is where
the rules always lived.

The single most important test:

```python
# test_campaign_hierarchy.py::TestTheDuplicateMailBug
def test_the_second_member_is_told_who_reached_them(self, root, contact, kabir, ishita):
    kabir_c  = campaign_svc.sub_campaign_for(root, kabir)
    mailing.claim_batch(kabir_c, kabir, [contact.id])

    ishita_c = campaign_svc.sub_campaign_for(root, ishita)
    claimed, skipped = mailing.claim_batch(ishita_c, ishita, [contact.id])

    assert claimed == []
    assert skipped[0].code == mailing.ALREADY_MAILED
    assert "Kabir" in skipped[0].reason        # named, not a bare "already mailed"
```

Its sibling `test_two_members_cannot_both_mail_one_contact` pins the same fact
one layer down, at the database: the second `CampaignMailing.objects.create`
raises `IntegrityError` and exactly one row survives. Two tests because the
constraint is the guarantee and the claim is only the polite way of hitting it —
if the service ever stops checking, the database must still refuse.

If that ever fails, the system can put two copies of the same cold mail in a
prospect's inbox under two different banners — which is the exact bug this
migration existed to kill. Everything else is negotiable.

Other guarantees pinned by tests: a non-lead gets `PermissionDenied` editing
someone else's contact; a posted `lifecycle` is dropped for non-leads while the
rest of the edit still applies; `bulk_edit` silently skips contacts outside the
caller's list; a mailed contact refuses hard delete with a message rather than a
500; archived and `do_not_contact` contacts are skipped by `claim_batch` with
`ARCHIVED`/`BLOCKED`; `record_result(sent)` moves `new → contacted` but leaves
`replied` alone; a failed send moves nothing; every mutation writes an audit row
naming its actor.

---

## 18. Environment variables

### CRM (`core_django`)
| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | local Docker Postgres | Supabase session pooler in production |
| `IGNITE_TEST_DATABASE_URL` | local Docker Postgres | only used under pytest |
| `DJANGO_SECRET_KEY` | insecure dev key | **must be set in production** |
| `DJANGO_DEBUG` | `False` | |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | |
| `DB_CONN_MAX_AGE` | `0` | persistent connections; keep 0 behind a pooler |
| `GOOGLE_OAUTH_CLIENT_ID` | — | **Web** client; blank disables sign-in entirely |
| `GOOGLE_OAUTH_CLIENT_SECRET` | — | pairs with the above |
| `GOOGLE_OAUTH_HOSTED_DOMAIN` | `pilani.bits-pilani.ac.in` | checked against the signed `hd` claim |
| `DB_TRANSACTION_POOLER` | `False` | only for port 6543 |

### Sending
| Variable | Default | Notes |
|---|---|---|
| `GMAIL_TOKEN_KEY` | — | **required.** Fernet key encrypting every refresh token. `check_db` fails the boot without it. Rotate as `2:<new>,1:<old>` |
| `GMAIL_SEND_DELAY_SECONDS` | `0` | pause between messages in one run |
| `GMAIL_TOKEN_EXPIRY_SKEW_SECONDS` | `120` | slack when deciding a cached access token is still usable |

### Hosting
| Variable | Default | Notes |
|---|---|---|
| `PORT` | `8000` | injected by the platform; the container binds it |
| `WEB_CONCURRENCY` | `2` | gunicorn workers. Three does not fit 512 MB beside the Google client |
| `WEB_THREADS` | `4` | |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | derived | needed only if `ALLOWED_HOSTS` is `*` |
| `DJANGO_LOG_LEVEL` | `INFO` | logs go to stdout, which is what platforms capture |
| `DATA_UPLOAD_MAX_MEMORY_SIZE` | 2.5 MB | CSV import buffers into the DB-backed session |

Gitignored and never committed: `.env`.

---

## 19. Management commands

```bash
cd core_django

../.venv/bin/python manage.py bootstrap_team you@pilani.bits-pilani.ac.in \
    --team "Ignite 26" --name "Your Name"  # the first team and its first lead
../.venv/bin/python manage.py check_db     # verify the live DB is safe to send from
../.venv/bin/python manage.py run_tick     # drain the queued sends once
../.venv/bin/python manage.py stranded_drafts   # list, and optionally settle, half-sent mail
../.venv/bin/python manage.py seed_dev     # dev fixtures (team + join code) — local only
../.venv/bin/python manage.py migrate
../.venv/bin/python manage.py makemigrations
```

### `bootstrap_team` — the one a fresh deployment cannot skip

Signing in requires a `TeamMember`; becoming one requires a join code; a join
code requires a `Team`. **Nothing in the app creates the first link in that
chain**, so a correctly deployed CRM with an empty database has no way for
anyone — including you — to get in.

That is deliberate rather than an oversight. A migration that invented a team
would have to invent a join code nobody could ever be told;
`0014_seed_default_team` writes the literal `ROTATE-ME` in plain sight for
exactly that reason, and only for databases that already had members.

**Render's free plan has no shell**, so run this from your own machine with
`DATABASE_URL` pointed at Supabase. It is reachable from anywhere; that is the
whole point of it being the master, and nothing else about your local setup has
to be working.

```bash
cd core_django
DATABASE_URL="<supabase-session-pooler-url>" \
  ../.venv/bin/python manage.py bootstrap_team you@pilani.bits-pilani.ac.in \
      --team "Ignite 26" --name "Your Name" --self-contact
```

It prints a join code. Read it out; everyone else joins through `/join/` in a
browser and never touches a command line. `--self-contact` also creates a
prospect who is *you*, so the first live send lands in your own inbox.

Re-running is safe and expected: the team and the member are updated rather than
duplicated, an existing member is promoted to lead rather than refused, and a
join code people are already using is **not** rotated silently — pass
`--rotate-code`, or use the Team page.

`check_db` asserts the indexes, the hierarchy invariants and the token key,
that `SELECT … FOR UPDATE` actually works over this connection, and that you are
not on the transaction pooler. It exits non-zero on failure, so it can be a
release gate. Sample output:

```
database : localhost:5432/ignite_crm
server   : PostgreSQL 16.14
  ok     campaign_mailings.uniq_campaign_contact
  ok     campaign_mailings.uniq_root_campaign_contact
  ok     contacts.contacts_tags_gin
  ok     SELECT ... FOR UPDATE

All checks passed.
```

---

## 20. File layout

```
ignite_crm/
├── conftest.py                    reports which DB tests chose
├── Dockerfile                     ONE image, both apps — see §13.1
├── docker-compose.yml             postgres + crm, and agent behind a profile
├── docker/
│   ├── entrypoint-crm.sh          wait → migrate → check_db → seed → serve
│   └── entrypoint-agent.sh        fail fast on a missing token
├── pytest.ini
├── requirements.txt          runtime only — what the image installs
├── requirements-dev.txt      the above plus pytest
├── .env.example
│
├── shared/                        enums and vendored assets
│   ├── enums.py                   CampaignStatus, MailingStatus, ContactLifecycle,
│   │                              BLOCKED_LIFECYCLES   (LEAD_BATCH is gone — §4)
│   └── static/basecoat/
│       ├── basecoat.css  (213 KB) components, from basecoat-css@1.0.2
│       ├── basecoat.js   (43 KB)  dialog, select, dropdown, toast, tabs
│       ├── app.css                hand-written layout + lifecycle badge colours
│       ├── poll.js                20s refresh
│       └── VENDORED.md            provenance
│
├── core_django/
│   ├── config/settings.py         DB config, Supabase notes, the test guard
│   └── crm/
│       ├── auth_views.py          the login page and the two doors
│       ├── models.py              canonical schema — the constraint lives here
│       ├── validators.py          BITS email, phone, batch
│       ├── forms.py               ContactForm, BulkEditForm, CampaignForm, …
│       ├── views.py  urls.py      every screen
│       ├── admin.py
│       ├── services/              ← every rule (see §11)
│       │     gmail.py             send as a member; the ported Gmail client
│       │     gmail_oauth.py       the mailbox consent step
│       │     secrets.py           refresh-token encryption + key versions
│       │     sending.py           claim → send → record, chunked
│       │     runner.py            one pass over everything due
│       │     teams.py             join codes, roles
│       ├── management/commands/   bootstrap_team.py, check_db.py, run_tick.py,
│       │                          seed_dev.py, stranded_drafts.py
│       ├── migrations/            0001 … 0015_revoke_api_tokens
│       ├── templates/crm/         22 templates
│       └── tests/                 317 tests, incl. conftest.py
│
└── render.yaml                    the hosting blueprint
```

`local_agent/` and `crm/api/` are gone: sending moved into the server, and the
token API that existed to serve a laptop was deleted rather than left reachable.

---

## 21. Operational playbook

**Someone is mailing the wrong people — stop everything.**
Set the campaign to `paused` on `/campaigns/<id>/`. Every agent stops at the next
claim; no restart needed.

**A prospect asks never to be contacted again.**
Set their lifecycle to `do_not_contact` (lead). `claim_batch` refuses them
permanently, across all campaigns.

**A member left the team.**
Revoke their tokens on `/members/`, set `is_active = False`. Their agent stops
working immediately; their mail history is preserved.

**A token leaked.**
Revoke it on `/members/` and issue a new one. Only the hash is stored, so nothing
else is exposed. `last_used_at` shows whether it was used after the leak.

**Someone deleted the wrong thing.**
Contacts that have been mailed cannot be deleted at all. For everything else,
`/contacts/<id>/` shows the full change log with actor and timestamp.

**An agent crashed mid-batch.**
Restart it and press **Resolve stranded drafts**. It asks Gmail what actually
went out and settles each row. It never re-sends blindly.

**The CRM is down but mail must go out.**
It can't, and that's the design. The agent cannot claim, so it cannot send. This
is preferable to sending without a record.

---

## 22. Change log

### Phase 5 — scheduled sending (branch `mail_schedule`)

**Schema — migrations `0006`, `0007`, `0008`**
- `ScheduledSend`: the queue. Campaign, member, a snapshot of the selection, a
  cursor, a lease, and drip settings.
- `FollowUpRule`; `replied_at` / `reply_checked_at` / `followed_up_at` on
  `campaign_mailings`.

**The feature**
- Schedule a send from the agent for any future time; an always-on agent in
  Docker executes it. See `docs/MAIL_SCHEDULING.md` for why it has to work that
  way — the Gmail API has no `sendAt`.
- A sending window (09:00–19:00 IST by default) and a six-hour grace period,
  after which a job is `missed` rather than arriving at 3am.
- Drip: 20 contacts every 30 minutes instead of 200 at once.
- Follow-ups: chase whoever did not reply after N days, with reply detection
  reading the Gmail thread the original mail created.
- `/schedules/` in the CRM shows every member's queue and leads with anything
  `missed` or `failed`.
- 65 new tests; 181 total.

**One documented rule changed.** `shared/enums.py` said NEW→CONTACTED was the
only automatic lifecycle transition. Follow-ups add CONTACTED→REPLIED, opt-in
per rule, justified by a reply being observed rather than inferred. Neither
transition can override a state a human chose.

### Phase 4 — what the mail actually looks like

**Schema — migrations `0004`, `0005`**
- `CampaignMailing` gains `rendered_body_html`, `from_name`, `cc`, `bcc` — the
  snapshot now covers the whole envelope, not just the text body.
- `Campaign.is_html`; `TeamMember.sender_name`.

**Mail**
- Every send is now `multipart/alternative`. New `services/richtext.py` converts
  one body into both parts; `[words](url)` puts a link on chosen words, with an
  **Insert link** button in the campaign form.
- **Body contains HTML** (opt-in per campaign) passes raw markup through for
  footers and styling; `<script>` and inline handlers are refused at save.
- The From line shows a name: `TeamMember.sender_name`, edited by a lead on the
  Team page, applied with `email.utils.formataddr`.
- CC/BCC on the agent's send screen, validated and recorded server-side, capped
  at 10 addresses, echoed back by preflight.
- 40 new tests (`test_campaign_links.py`, `test_campaign_headers.py`); 116 total.

### Phase 3 — Docker, and passwordless sign-in

- One root `Dockerfile` holding both apps; `core_django/Dockerfile` removed.
  `docker/entrypoint-crm.sh` migrates, runs `check_db`, and **refuses to serve if
  the constraint is missing**. See §13.1.
- `manage.py issue_token` for bootstrapping an agent before anyone can log in.
- `whitenoise` and `gunicorn` added to `requirements.txt` — `whitenoise` was
  already in `MIDDLEWARE` and missing from the file.
- **Passwords removed from the CRM.** New `services/auth.py` and `auth_views.py`:
  batch 2024 picks a name, batch 2025 signs in with Google on the BITS domain.
  Identity is now a session key holding a `TeamMember` id; `TeamMember.user` and
  Django's `User` survive only for `/admin/`.
- `member_required` / `lead_required` now redirect a stranger to `/login/` and
  keep raising `PermissionDenied` for a real refusal.
- 11 new tests in `test_login.py`; 76 total.
- `migrate` and `seed_dev` now key off whether the database is local, so a
  laptop pointed at the shared pool cannot reseed it or race a migration.
  `check_db` still runs unconditionally and still fails the boot.
- The local `postgres` container moved behind the `localdb` profile, and `crm`
  dropped its `depends_on` in favour of the entrypoint's own wait — which works
  for a hosted database too, where there is no container to depend on.

### Phase 2 — shared editing, lifecycle/tags, Supabase (commit `1aaf2df`)

**Schema — migration `0003`**
- `Contact` gains `lifecycle`, `tags` (Postgres array + GIN index), `is_archived`,
  `archived_at`, `archived_by`, `created_by`
- New `ContactAudit` model: one row per field change, naming the actor
- New index `(is_archived, lifecycle)`

**Rules**
- `record_result()` flips `new → contacted` on a confirmed send, scoped so a
  later campaign never drags `replied` backwards
- `claim_batch` and `preflight` refuse archived and `do_not_contact`/`bounced`
  contacts via a shared `unmailable_reason()` helper, checked under the row lock
- New outcome codes `ARCHIVED` and `BLOCKED`
- `can_edit_contact`, `can_set_lifecycle`, `can_hard_delete`, `editable_contacts`
  in `permissions.py`
- New `services/contacts.py`: every mutation funnels through it, writing audit rows
- `hard_delete` refuses mailed contacts with an explanation instead of a 500
- Tags lowercased and de-duplicated on the way in

**Surfaces**
- Django: `/contacts/new`, `/contacts/<id>/edit`, `/archive`, `/delete`,
  `/bulk-edit`; tag/stage/archived filters; change log on the detail page
- Agent: inline row editor, richer contact payload, live quota
- API: `POST /contacts/new`, `PATCH /contacts/<id>`
- `poll.js` shared by both apps
- Lifecycle badge colours in `app.css`, light and dark
- CSV import accepts a semicolon-separated `tags` column

**Infrastructure**
- `settings.py` documents the session-pooler requirement and pins tests to local
  Postgres regardless of `DATABASE_URL`
- New `manage.py check_db`
- `conftest.py` reports the chosen database in the pytest header

**Tests** — 33 new, 65 total.

**Fixed along the way**
- The agent's quota badge was captured once at FastAPI startup and stale for the
  whole process lifetime; now polled
- Checkbox listeners on `/assign/` were bound per row and would have been lost on
  a polled refresh; now delegated
- A mangled fragment in this README's "Running the CRM" section

### Phase 1 — the base system (commit `6a5b206`)

Four-table schema with the unique constraint; claim/report protocol moved
server-side; token auth; the 15 Django screens; the local agent as a thin client;
Basecoat UI vendored for both apps; 32 tests.

---

## 23. Known gaps

**The Gmail path is only partly proven.** Real sends have gone out, including
HTML bodies with links. But the tests exercise `FakeGmail` mocks, so anything
Gmail itself decides is unverified — in particular whether it honours our From
display name or substitutes the account's own "Send mail as" name, and how
CC/BCC behave in a real batch. **Test any change to the send path with a batch
of one to your own address before pointing it at real prospects.**

**Nothing drains the send queue automatically.** This is the largest gap and
the one to read first. Pressing Send commits a `ScheduledSend` due now; a
scheduled send and a follow-up do the same. All of them then wait until someone
runs `tick()` — the **Send queued mail now** button or `manage.py run_tick`
(§13.3). Queued mail is not lost and not sent twice; it simply does not move on
its own, and a job whose grace window closes first is marked `missed`.

What is missing is small and deliberately scoped: a `GET /internal/tick`
authenticated with `secrets.compare_digest` against `TICK_SECRET`, a
session-level `pg_try_advisory_lock` around the call, and an external pinger
(Render's free plan has no cron). The executor it would sit in front of already
exists and is already tested. `docs/MAIL_SCHEDULING.md` has the full design,
including why an in-process thread from `AppConfig.ready()` was rejected.

**The ping interval will be a throughput setting, not just a keep-alive one.**
When that lands: a tick is capped at `TICK_MAX_MAILS` (40), so a 1-minute ping
is ~2,000 mails/hour team-wide and a 10-minute ping is ~240. Ten minutes is the
interval you would pick if you were only thinking about keeping the instance
awake, and it would quietly turn a launch blast into most of a day. Say the new
shape out loud before the first big campaign rather than letting the team
discover it.

**Reply detection reads the thread, not the meaning.** A follow-up is cancelled
by any message from the prospect's address in the thread, including "wrong
person" or an out-of-office. That is the right trade — chasing someone who
replied is far worse than not chasing someone who bounced a holiday
autoresponder — but it is not sentiment analysis.

**Dev credentials are in the repo.** `docker-compose.yml` uses `ignite:ignite`.
Harmless on localhost, but visible in a public org repo.

**~~The name door trusts the network.~~** *Closed.* Batch-2024 sign-in was a
dropdown and a button, a deliberate trade for a tool every lead ran on their own
laptop. The route, the view, the service functions and the form are all deleted;
see §4.1.

**~~The dashboard shows no lifecycle funnel.~~** *Closed.* A member's landing
page is now their own queue, and `lifecycle_counts()` finally has a caller.

**The server can now impersonate a member, and that is a real loss.** It holds
every refresh token; an application compromise — not merely a database leak —
yields the ability to send as anyone on the team. Encryption at rest does not
help with that case and §5's `gmail_credentials` note says so plainly. What
stands in its place is narrower: `google_email` is checked against `bits_email`
at grant time and re-verified against Gmail's own `getProfile` after any
credential change, every send writes a durable `sent_by`, and a member can
revoke the grant from their own Google account page without asking us. This was
the price of a hosted CRM and it was paid knowingly; it is listed here so nobody
rediscovers it as a surprise.

**The consent screen's Internal user type is doing real work.** It is what
removes the 7-day refresh-token expiry, the 100-user cap and the verification
requirement (§15.2). It also means **only `@pilani.bits-pilani.ac.in` accounts
can ever connect** — which is exactly what we want, and worth knowing before
somebody tries to onboard an external collaborator.

**`bounced` is never set automatically.** Nothing reads bounce notifications;
a lead sets it by hand. Inferring it from SMTP error strings was judged worse
than leaving it manual.

**Polling is not instant.** Up to 20 seconds of staleness by design. The database
is always correct immediately; only the screens lag.

**No rate limit on `claim` beyond the daily cap.** A member could claim 400 in
one burst. Gmail's own throttling is the backstop.

**`api_tokens` is a table nothing reads.** Kept rather than dropped, because a
dropped table is the one migration with no cheap rollback. It should go in a
later cleanup (§5).
