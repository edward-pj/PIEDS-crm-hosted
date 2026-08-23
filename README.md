# Ignite CRM — PIEDS Mass Mailing System

A shared contact pool and mass-mailing system for PIEDS, BITS Pilani's technology
business incubator. One master database, a hosted CRM the 2024 batch runs, and a
small app each 2025-batch member runs on their own laptop to send from their own
Gmail.

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
10. [HTTP API](#10-http-api)
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

Two apps, one database, one visual language:

- **`core_django/`** — the CRM. Owns the schema, the contact pool, campaigns,
  assignment, and **every safety rule**. It is the only process that connects to
  the database. **It never sends mail.**
- **`local_agent/`** — a small FastAPI app each team member runs on their laptop.
  Talks to the CRM over HTTPS and sends through *their own* Gmail. Holds no
  database credentials.

The split exists because of one asymmetry: **neither side can send mail alone.**
The server knows who to mail and enforces every rule, but has no mailbox. The
agent has a mailbox but doesn't know who to mail until the server hands it a
claim. That is the whole security model.

---

## 2. Architecture

```
   member's laptop                          hosted
┌────────────────────────┐        ┌──────────────────────────┐
│ local_agent (FastAPI)  │        │ core_django (Django)     │
│ :8111                  │──────► │ :8000                    │──► Supabase
│                        │ HTTPS  │                          │    Postgres
│ holds: Gmail OAuth     │ +Token │ holds: DB credentials    │    (the master)
│ holds: NO db creds     │        │ holds: NO Gmail creds    │
└───────────┬────────────┘        └──────────────────────────┘
            │
            └──► Gmail API — the member's own mailbox, never the server's
```

**Why the agent is local at all.** Mail sent from a central server would come
from one address and land in spam at volume. Each member sending from their own
BITS address is both more deliverable and more honest — the person whose name is
on the mail is the person who will get the reply.

**Why the agent has no database access.** It used to. Moving the send logic
server-side means the row lock, the unique constraint, and the DRAFT-before-send
ordering all live next to the data they protect. Laptops stopped holding
production credentials, and port 5432 never faces the internet.

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

The DRAFT row is committed **before** the agent is told to send. A crash can
therefore leave an ambiguous DRAFT — visible and resolvable — but never a sent
mail with no record, which would be unrecoverable.

### 3.3 `sent_by` is trustworthy

The agent refuses to start unless two independent identities agree: the API
token's owner (what the CRM thinks) and the Gmail session (which mailbox you can
actually send from). Nobody can send from their own account and have it
attributed to someone else.

### 3.4 We never mail a half-rendered template

A contact missing a variable the template needs is **skipped**, not mailed with a
gap. Nobody receives "Hi , we loved what you're building at ."

### 3.5 Nobody edits data they don't own

2025-batch members can only change contacts assigned to them. Enforced per-object
in `services/permissions.py` and again in `services/contacts.py` — twice, because
the API accepts JSON, and JSON does not respect a form's field list.

---

## 4. Who can do what

Defined once, in `shared/enums.py::LEAD_BATCH` (currently `"2024"`) and
`services/permissions.py`. Changing which batch leads next year is a one-line edit.

| | batch 2024 (lead) | batch 2025 |
|---|---|---|
| **Signs in by** | **picking their name** | **Google, BITS domain only** |
| See the whole pool | ✅ | ✅ |
| Edit a contact | anyone's | **only their own assigned** |
| Archive / restore | anyone's | only their own |
| Add a contact | assigns to anyone | assigns to themselves, forced |
| Bulk edit | anyone's | only their own |
| Delete permanently | ✅ (never-mailed only) | ❌ |
| Set `lifecycle` by hand | ✅ | ❌ — the server moves it |
| Assign contacts to members | ✅ | ❌ |
| Create / edit campaigns | ✅ | ❌ (read-only) |
| Issue and revoke API tokens | ✅ | ❌ |
| Import CSV | ✅ | ❌ |
| Send mail | ✅ | ✅ |

Both batches edit from **either surface**: full forms in the Django CRM, and an
inline row editor in the local agent for fixing a detail just before sending.

### 4.1 Two doors, one per batch

There is no password anywhere in the CRM. `services/auth.py` holds both doors,
and identity is a session key holding a `TeamMember` id — Django's `User` model
is consulted only by `/admin/`.

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

**Adding a new person** is a lead's job through `/admin/` until join codes land.
There is no self-service door and no fallback if Google is misconfigured — the
login page says so rather than degrading to something weaker.

**Why 2025 members can edit at all.** They are the ones actually in conversation
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
| `batch` | Char(4) | **indexed — drives all permissions** |
| `is_active` | Bool | |
| `user` | OneToOne → Django `User` | `SET_NULL`; null for members who only run the agent |

### `api_tokens`
| Field | Type | Notes |
|---|---|---|
| `member` | FK → TeamMember | `CASCADE` |
| `label` | Char(80) | e.g. "Aarav's MacBook" |
| `key_hash` | Char(64) | unique, indexed — **SHA-256 only** |
| `key_prefix` | Char(8) | first chars, shown in the UI for identification |
| `last_used_at` | DateTime | stamped on every request, so leads can spot stale tokens |
| `revoked_at` | DateTime | |

The plaintext key is shown **once** at creation and is unrecoverable. A dumped
database yields zero working credentials.

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

**Only `active` campaigns can be mailed.** Flipping one to `paused` in the CRM
stops every agent on every laptop mid-batch. That is the emergency brake.

### `scheduled_sends` — mail queued for later

| Field | Type | Notes |
|---|---|---|
| `campaign` / `member` | FK `PROTECT` | `member` is whose Gmail sends it — **and the only agent allowed to run it** |
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
blank falls back to their real name. It is resolved per claim rather than read
from the agent's startup profile, so editing it takes effect on the next send
with no laptop to restart. `GmailClient.send` builds the header with
`email.utils.formataddr`, which quotes and encodes names that need it.

> If a recipient still sees the wrong name, check the Gmail account's own
> "Send mail as" setting — Gmail can override the header we set.

**CC / BCC** are entered on the local agent's send screen and apply to **every
mail in that batch** — ten copied addresses on a 200-mail send is two thousand
extra deliveries, and a CC is visible to each prospect. The agent does not apply
them itself: they travel to the server with the claim, which validates every
address, caps the list at `MAX_COPY_ADDRESSES` (10), stores them on each
`campaign_mailings` row, and hands them back. A malformed address fails the whole
request before a single row is written. Preflight echoes back the addresses the
server accepted, and the send confirmation names them again.

### 5.3 Scheduled sending

Full detail in **`docs/MAIL_SCHEDULING.md`**. The short version, because one
constraint shapes everything:

**The Gmail API has no `sendAt`.** Gmail's "Schedule send" is a feature of the
web client, not the API. There is no way to hand Google a future time and walk
away, so a scheduled mail requires a process that is *awake at that moment
holding that member's Gmail token*. The CRM cannot be that process — it holds no
Gmail credentials, deliberately (§2).

So the server owns the queue and every rule, and an agent asks "anything due for
me?" every 60 seconds. Run the `agent` compose profile on an always-on host and
09:00 means 09:00; rely on a laptop and it means "whenever that laptop is next
open", bounded by the grace window.

An agent authenticates as exactly one member and may only run *that* member's
jobs — anything else would send from the wrong mailbox and record a false
`sent_by`. One always-on agent therefore covers one account; run one container
per member to cover more.

Everything else is built on that: a **sending window** so nothing arrives at 3am,
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
trusting the agent's list, because someone may have archived the contact between
the page loading and Send being pressed.

---

## 7. The send protocol

### The claim → send → report loop

```
1. agent: POST /api/v1/mailings/claim {campaign_id, contact_ids[]}

2. server, ONE TRANSACTION PER CONTACT:
     SELECT ... FOR UPDATE the contact          ← row lock acquired
     assigned to the caller?          no → skip NOT_ASSIGNED
     archived / blocked lifecycle?   yes → skip ARCHIVED | BLOCKED
     render(campaign, contact)      fail → skip MISSING_VARS
     INSERT CampaignMailing(DRAFT)         ← unique constraint fires on a dupe
     COMMIT                                 ← lock released, DRAFT durable
   ────────────────────────────────────────────────────────────────────
3. agent sends via its own Gmail             ← NO locks held anywhere

4. agent: POST /api/v1/mailings/<id>/result
     {status: "sent",   message_id, thread_id}  → SENT  + lifecycle flip
     {status: "failed", error}                  → FAILED + error_detail
```

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

Resolve them from the member's own agent with **Resolve stranded drafts**, which
asks Gmail which ones actually went out. Anything that did is recorded as sent;
anything that did not becomes `failed`, and a failed mailing **can** be claimed
again — so re-selecting those contacts and pressing Send simply works.

`manage.py stranded_drafts` reports the backlog and names who has to clear it.
Only that member's agent can: the mail left their mailbox, and only they hold the
Gmail credentials to check it.

#### The original design

If the agent dies between steps 2 and 4, DRAFTs are left behind.
`GET /mailings/drafts` lists your own; **Resolve stranded drafts** uses
`GmailClient.find_message_to()` to ask **Gmail itself** whether that mail went
out, and settles the row to `SENT` or `FAILED` accordingly. It never re-sends
blindly.

### Daily cap

`DAILY_SEND_CAP = 400`, enforced inside `claim_batch` by counting
`sent_last_24h(member)`. Server-side deliberately — it counts across every device
a member uses, so nobody evades it by opening the agent on a second laptop.
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
| `/schedules/` | Every scheduled send; `missed`/`failed` called out, lead-only cancel | member |
| `/members/` | Team load, **Sends as** names, issue/revoke API tokens | **lead** |
| `/login/` | Sign in — name list, or Google | anyone |
| `/admin/` | Django admin | superuser (password auth, separate) |

### Local agent — `http://localhost:8111`

Single page: campaign picker → CC/BCC → Verify Gmail → contact table (with tags,
stage badge and a ✎ inline editor per row) → Preflight → **Send** or
**Schedule…**, plus a **Scheduled** panel and **Resolve stranded drafts**. Editing CC/BCC re-locks Send until you preflight again, so
the addresses that go out are always ones the dry run showed you.

---

## 10. HTTP API

All endpoints take `Authorization: Token <key>` and live under `/api/v1/`.

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/me` | identity handshake, live quota |
| `GET` | `/campaigns` | active campaigns only |
| `GET` | `/contacts?campaign_id=` | the caller's assigned contacts, archived excluded |
| `POST` | `/contacts/new` | add a contact, force-assigned to the caller |
| `PATCH` | `/contacts/<id>` | edit, guarded by `can_edit_contact` |
| `POST` | `/mailings/preflight` | dry run, **writes nothing**; echoes back the accepted `cc`/`bcc` |
| `POST` | `/mailings/claim` | reserve DRAFTs, returns the rendered mail and its envelope |
| `POST` | `/mailings/<id>/result` | record sent/failed |
| `GET` | `/mailings/drafts` | stranded DRAFTs to reconcile |
| `GET`/`POST` | `/schedules` | list, or queue a send for later |
| `POST` | `/schedules/claim` | lease due jobs; also sweeps stale leases, missed jobs and follow-ups |
| `POST` | `/schedules/<id>/progress` | report a slice, advance the cursor |
| `POST` | `/schedules/<id>/cancel` · `/reschedule` | |
| `GET` | `/replies/scan` | threads worth re-reading for a reply |
| `POST` | `/replies/<mailing_id>` | report what Gmail said |

Contact payload:

```json
{
  "id": "…", "name": "Rohan Iyer", "first_name": "Rohan", "last_name": "Iyer",
  "email": "rohan@example.com", "phone_no": "", "linkedin": "",
  "company": "Zerodha", "designation": "CTO",
  "tags": ["fintech", "priority"],
  "lifecycle": "new", "lifecycle_label": "New",
  "mailable": true, "already_mailed": false, "last_contacted_at": null
}
```

`preflight` and `claim` both accept optional `cc` and `bcc` — comma-separated
strings, validated server-side (see [§5.2](#52-sender-name-cc-and-bcc)). A
claimed item carries everything needed to build the message and nothing the
agent has to decide:

```json
{
  "mailing_id": "…", "contact_id": "…", "to": "rohan@example.com", "name": "Rohan Iyer",
  "subject": "Zerodha x PIEDS",
  "body": "Hi Rohan, want to book a call (https://cal.com/pieds)?",
  "body_html": "<!doctype html><html><body …>…</body></html>",
  "from_name": "Pratham Jain", "cc": "lead@pieds.in", "bcc": ""
}
```

CORS is not configured and is not needed — the agent is a server-side HTTP
client, not a browser.

### Agent's own routes (`:8111`)

`GET /` (the UI), `/health`, `/auth/verify`, `/api/me`, `/api/campaigns`,
`/api/contacts`, `POST /api/contacts`, `PATCH /api/contacts/{id}`,
`/api/preflight`, `/api/send` (NDJSON stream), `/api/drafts`, `/api/reconcile`.
All thin proxies to the CRM except `/auth/verify` and `/api/send`, which touch
Gmail.

---

## 11. Services — where the rules live

Every rule lives in `core_django/crm/services/`. The agent is a dumb pipe with a
mailbox. Change behaviour here and it takes effect for everyone immediately, with
no laptop to update.

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
cp .env.example .env               # leave the agent section blank for now

docker compose up --build
```

`.env.example` ships with `COMPOSE_PROFILES=localdb`, which is what starts the
local Postgres container. Keep that line for local development; drop it when you
move to a hosted database (§14).

That is everything: Postgres 16, migrations, `check_db`, `seed_dev`, and
gunicorn on <http://localhost:8000>. Nothing is installed on the host — no venv,
no Python version to match. See §13.1 for what it actually does and how to add
the agent.

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

`seed_dev` creates four members — `aarav`, `diya` (batch 2024, leads) and
`kabir`, `ishita` (batch 2025) — plus ~50 contacts with assorted tags and
lifecycles, and one active campaign. The two leads can sign in immediately by
picking their name; the 2025 members need Google configured (§15.1).

---

## 13. Running it

### 13.1 Docker

```bash
docker compose up --build          # postgres + CRM   → http://localhost:8000
```

One image (`./Dockerfile`) contains both apps, because they share `shared/` and
the same dependency set. Compose decides which one a container runs by choosing
an entrypoint. **The split is enforced by environment, not by files:** the agent
container is simply never given `DATABASE_URL`.

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
laptop pointed at a database whose schema was never built exits non-zero with
`MISSING campaign_mailings.uniq_campaign_contact` rather than serving a CRM that
can double-mail. The guard is in `seed_dev.py` as well as the entrypoint,
because someone typing the command by hand deserves the same protection.

`DJANGO_ALLOWED_HOSTS` gets `crm` **appended**, not defaulted — the agent
container reaches the CRM by that hostname and Django validates `Host`, so a
`.env` listing only `localhost` must not be able to drop it.

#### Adding the agent

The agent is behind a profile because it cannot start unprepared: it needs an
API token that only exists once the CRM is up, and a Gmail consent that has to
happen in a real browser.

```bash
# 1. token (the /members/ page is the normal path; this is for bootstrapping)
docker compose exec crm python core_django/manage.py issue_token \
    aarav@pilani.bits-pilani.ac.in --label docker

# 2. paste AGENT_API_TOKEN and AGENT_MEMBER_EMAIL into .env

# 3. Gmail consent — ONCE, on the host, because the OAuth flow opens a browser
#    and binds 127.0.0.1:8080 inside whatever runs it
mkdir -p ~/.ignite_crm && cp client_secret.json ~/.ignite_crm/
.venv/bin/uvicorn local_agent.main:app --port 8111    # press "Verify Gmail", then Ctrl-C

# 4. now the container reuses that cached token
docker compose --profile agent up --build             # → http://localhost:8111
```

`~/.ignite_crm` is mounted at `/tokens`, holding both `client_secret.json` and
the cached `token_*.json`. Step 3 is a one-time cost per member; after it, the
agent is `docker compose --profile agent up` forever.

The agent container's own entrypoint fails fast with an explanation when
`AGENT_API_TOKEN` is empty, rather than letting uvicorn crash-loop on the
lifespan identity check.

### 13.2 Native

Three terminals, from the repo root:

```bash
# 1. database
docker compose up -d postgres

# 2. backend — the CRM                          → http://localhost:8000
cd core_django && ../.venv/bin/python manage.py runserver 8000

# 3. local agent                                → http://localhost:8111
.venv/bin/uvicorn local_agent.main:app --port 8111
```

Log in at <http://localhost:8000/login/>:

| User | Batch | Sees |
|---|---|---|
| Aarav (pick the name) | 2024 lead | everything |
| Kabir (Google sign-in) | 2025 | 403 on `/assign/`, `/contacts/import/`, `/campaigns/new/`, `/members/` |

The agent runs as whichever member `AGENT_API_TOKEN` belongs to.

---

## 14. Supabase — the master database

Supabase replaces the Docker Postgres. It changes nothing about the
architecture: Django remains the only process that connects to it, and laptops
still hold no database credentials.

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

### 14.2 What changes in `.env` on every laptop

```diff
- COMPOSE_PROFILES=localdb                    # stop running a database nobody uses
+ DATABASE_URL_DOCKER=postgres://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
```

Nothing else. `RUN_MIGRATIONS` and `SEED_DEV` notice the host is not local and
switch themselves off (§13.1) — on the one machine that owns the schema, set
`RUN_MIGRATIONS=true`.

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
host: all 76 tests still pass against localhost.

---

## 15. Google setup

**Two OAuth clients, one Google Cloud project.** They are not interchangeable and
mixing them up is the most likely thing to go wrong here:

| | client type | used by | for |
|---|---|---|---|
| `client_secret.json` | **Desktop app** | `local_agent` | sending mail as the member |
| `GOOGLE_OAUTH_CLIENT_ID/SECRET` | **Web application** | `core_django` | batch-2025 sign-in |

### 15.1 Web client — sign-in for batch 2025

1. **Credentials → Create OAuth client ID → Web application**.
2. Authorised redirect URI, exactly:
   `http://localhost:8000/login/google/callback/`
   (add your public URL too if you host the CRM).
3. Put the id and secret in `.env` as `GOOGLE_OAUTH_CLIENT_ID` /
   `GOOGLE_OAUTH_CLIENT_SECRET`.

Leave them blank and the Google door is disabled with a message on the login
page rather than a stack trace — but **no batch-2025 member can sign in to the
CRM until they are set**. They can still send: the agent authenticates with an
API token, not a browser session.

`GOOGLE_OAUTH_HOSTED_DOMAIN` defaults to `pilani.bits-pilani.ac.in` and is
checked against the signed `hd` claim, so a personal Gmail is refused even if
somebody put one in the pool. Sign-in never creates a member — an address with
no active `TeamMember` is turned away.

### 15.2 Desktop client — sending

One-time, per member:

1. **Google Cloud Console** → same project → enable the **Gmail API**.
2. **OAuth consent screen** → External → add each team member as a test user.
3. **Credentials → Create OAuth client ID → Desktop app** → download the JSON as
   `client_secret.json` at the repo root (gitignored).
4. Ask a lead to issue you a token on `/members/`. **It is shown once.**
5. Fill in `.env`: `AGENT_API_BASE_URL`, `AGENT_API_TOKEN`, `AGENT_MEMBER_EMAIL`.
6. Start the agent and press **Verify Gmail** — the consent screen appears once.

Scopes requested: `gmail.send` and `gmail.readonly`. Readonly is needed for
stranded-draft reconciliation — asking Gmail whether a mail actually went out.
Cached tokens live in `~/.ignite_crm`, `chmod 600`.

Each person needs **their own** token. Never share one — the token is what makes
`sent_by` meaningful.

---

## 16. Deploying

```bash
docker build -t ignite-crm .        # from the repo root; `shared/` needs it
```

The image's default command is the CRM, behind the same entrypoint compose uses,
so a deploy verifies the database before it serves:

```bash
docker run -p 8000:8000 --env-file prod.env ignite-crm
```

**Set `RUN_MIGRATIONS=true` in `prod.env`.** A deploy's `DATABASE_URL` is
Supabase, which the entrypoint reads as a shared database and therefore does
*not* migrate by default (§13.1) — the rule that stops five laptops racing each
other also stops your one server, and it has no way to tell the difference.
Without it, `check_db` fails the boot on the first deploy after a migration.

App-level environment variables:

| Variable | Value |
|---|---|
| `DATABASE_URL` | the Supabase session-pooler URL |
| `DJANGO_SECRET_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(50))"` |
| `DJANGO_ALLOWED_HOSTS` | your public hostname |
| `DJANGO_DEBUG` | `False` |

With `DEBUG=False`, `settings.py` automatically enables `SECURE_SSL_REDIRECT`,
`SECURE_PROXY_SSL_HEADER`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`,
one-year HSTS with subdomains, `X_FRAME_OPTIONS=DENY`, `CSRF_TRUSTED_ORIGINS`
derived from `ALLOWED_HOSTS`, and `CompressedManifestStaticFilesStorage`.

The entrypoint is the release step — it migrates (given `RUN_MIGRATIONS=true`)
and runs `check_db` before gunicorn binds. Static files are built into the image
via `collectstatic` and served by whitenoise — no nginx needed.

Then point each member's `AGENT_API_BASE_URL` at the public HTTPS host.

---

## 17. Tests

```bash
.venv/bin/python -m pytest          # needs docker compose up
```

**181 tests**, all passing:

| File | Count | Covers |
|---|---|---|
| `test_constraints.py` | 13 | the unique constraint, permissions, status transitions |
| `test_mailing_api.py` | 19 | token auth, claim/report, preflight, drafts, retry |
| `test_contacts_crud.py` | 33 | edit scoping, lifecycle rules, archive/delete, audit, HTTP layer |
| `test_login.py` | 11 | the two doors — see §4.1 |
| `test_campaign_links.py` | 17 | link syntax, escaping, scheme rejection, both body parts |
| `test_campaign_headers.py` | 23 | sender name, CC/BCC validation and snapshot, HTML bodies |
| `test_scheduling.py` | 48 | the queue, the lease, the sending window, grace, drip |
| `test_followups.py` | 17 | reply detection, who gets chased, the lifecycle opt-in |

The single most important test:

```python
def test_claiming_twice_yields_no_second_mailing(...):
    first  = claim(client, auth, campaign, [contact.id]).json()
    second = claim(client, auth, campaign, [contact.id]).json()
    assert len(first["claimed"]) == 1
    assert second["claimed"] == []
    assert "already has a mailing" in second["skipped"][0]["reason"]
    assert CampaignMailing.objects.filter(contact=contact).count() == 1
```

If that ever fails, the system can put two copies of the same mail in a
prospect's inbox. Everything else is negotiable.

Other guarantees pinned by tests: a 2025 member gets `PermissionDenied` editing
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
| `GOOGLE_OAUTH_CLIENT_ID` | — | **Web** client; blank disables batch-2025 sign-in |
| `GOOGLE_OAUTH_CLIENT_SECRET` | — | pairs with the above |
| `GOOGLE_OAUTH_HOSTED_DOMAIN` | `pilani.bits-pilani.ac.in` | checked against the signed `hd` claim |
| `DB_TRANSACTION_POOLER` | `False` | only for port 6543 |

### Agent (`local_agent`) — no `DATABASE_URL`, by design
| Variable | Notes |
|---|---|
| `AGENT_API_BASE_URL` | the CRM's URL; HTTPS in production |
| `AGENT_API_TOKEN` | issued on `/members/`, shown once |
| `AGENT_MEMBER_EMAIL` | must match both the token owner and the Gmail account |
| `GOOGLE_CLIENT_SECRETS_PATH` | default `./client_secret.json` |
| `AGENT_TOKEN_DIR` | default `~/.ignite_crm` |
| `AGENT_SEND_DELAY_SECONDS` | default `2`; the hard cap is server-side |

Gitignored and never committed: `.env`, `client_secret.json`, `token_*.json`,
`.ignite_crm/`.

---

## 19. Management commands

```bash
cd core_django

../.venv/bin/python manage.py check_db     # verify the live DB is safe to send from
../.venv/bin/python manage.py seed_dev     # dev fixtures
../.venv/bin/python manage.py issue_token <email> --label <name>   # shown once
../.venv/bin/python manage.py migrate
../.venv/bin/python manage.py makemigrations
```

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
├── shared/                        imported by BOTH apps
│   ├── enums.py                   CampaignStatus, MailingStatus, ContactLifecycle,
│   │                              BLOCKED_LIFECYCLES, LEAD_BATCH
│   └── static/basecoat/
│       ├── basecoat.css  (213 KB) components, from basecoat-css@1.0.2
│       ├── basecoat.js   (43 KB)  dialog, select, dropdown, toast, tabs
│       ├── app.css                hand-written layout + lifecycle badge colours
│       ├── poll.js                20s refresh, shared by both apps
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
│       ├── services/              ← every rule (see §11), incl. auth.py
│       ├── api/                   auth.py, views.py, urls.py
│       ├── management/commands/   check_db.py, seed_dev.py, issue_token.py
│       ├── migrations/            0001_initial, 0002_apitoken,
│       │                          0003_contact_lifecycle_tags_archive
│       ├── templates/crm/         14 templates
│       └── tests/                 181 tests
│
└── local_agent/
    ├── main.py                    lifespan identity checks + 12 routes
    ├── api_client.py              httpx wrapper — the only link to the CRM
    ├── config.py
    ├── services/send.py           claim → send → report, + reconcile
    ├── gmail/client.py            OAuth, token storage, identity binding
    └── templates/index.html       the send UI + inline editor
```

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

**The local agent does not hot-reload.** It is run with plain `uvicorn`, no
`--reload`, so a change under `local_agent/` does nothing until the process is
restarted. This has already once looked exactly like a broken feature.

**Scheduled sending is only as reliable as where the agent runs.** Gmail has no
`sendAt`, so a scheduled mail goes out only while an agent is running for that
member (§5.3). One always-on container covers one account; everyone else's
scheduled mail waits for their laptop, and is marked `missed` if the grace
window closes first. That is a deployment property, not a bug to fix in code —
but it is the first thing to check when a scheduled send did not arrive.

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

**`bounced` is never set automatically.** Nothing reads bounce notifications;
a lead sets it by hand. Inferring it from SMTP error strings was judged worse
than leaving it manual.

**Polling is not instant.** Up to 20 seconds of staleness by design. The database
is always correct immediately; only the screens lag.

**No rate limit on `claim` beyond the daily cap.** A member could claim 400 in
one burst. Gmail's own throttling is the backstop.

**The dashboard shows no lifecycle funnel yet.** `services/contacts.py`
provides `lifecycle_counts()`, but no screen calls it.
