# Making the queue drain itself

Everything below is done **once**, by hand, after `TICK_SECRET` is set on Render.
None of it lives in the repo, because none of it runs on this machine.

The shape: something outside calls `GET /internal/tick` on a timer, and that
request *is* the tick. Render's free plan has no cron and the instance sleeps
after fifteen minutes of silence, so only a request can wake this process —
which is why a background thread was rejected and why the pinger cannot be
self-hosted.

---

## 0. What runs, and why there are two of them

| | Role |
|---|---|
| **Supabase `pg_cron` + `pg_net`** | The driver. Every minute during the window. Async — it fires the request and does not wait, so a 25-second tick never blocks the cron worker |
| **cron-job.org** | The watchdog. Every 15 minutes. It exists **only** to email you when the endpoint stops answering, which `pg_net` cannot do |

Both hitting the same URL is safe and intended: `tick()` takes a session-level
advisory lock, and the loser gets `{"locked": true}` and a 200.

Supabase is the driver rather than cron-job.org for one reason — it is not a new
trust boundary. The database already holds every contact; giving it the tick
secret adds nothing. Handing a third party a credential that starts your sending
does.

---

## 1. Render

Two environment variables, then redeploy:

```
TICK_SECRET=<paste the output of the command below>
DAILY_SEND_CAP=800
```

Generate the secret locally — do not invent one by hand:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Confirm it took: the deploy log's `check_db` section must print
`ok     TICK_SECRET is set`. If it prints the `WARN` line instead, the route is
answering 503 and nothing will send on a timer.

---

## 2. Supabase

SQL Editor, once:

```sql
create extension if not exists pg_cron;
create extension if not exists pg_net;
```

Then the job. **Times are UTC** — `pg_cron` does not know about Asia/Kolkata, and
10:00–17:00 IST is 04:30–11:30 UTC:

```sql
select cron.schedule(
  'ignite-crm-tick',
  '* 4-11 * * *',
  $$
  select net.http_get(
    url     := 'https://<your-render-host>/internal/tick',
    headers := jsonb_build_object('X-Tick-Secret', '<your TICK_SECRET>')
  );
  $$
);
```

`4-11` is deliberately slightly wider than the window: it starts at 04:00 UTC
(09:30 IST) and ends after 11:59 UTC (17:29 IST). Half an hour of slack at each
end costs nothing and avoids arguing with daylight-saving arithmetic that India
does not have but the cron expression does not know that.

**Check it is running** — `pg_net` is fire-and-forget, so the only evidence is
its response table:

```sql
select status_code, created
from net._http_response
order by created desc
limit 10;
```

Expect `200`. A `403` means the secret does not match; a `503` means Render has
no `TICK_SECRET`. Rows here expire after six hours.

To change the window later, unschedule and re-add:

```sql
select cron.unschedule('ignite-crm-tick');
```

---

## 3. cron-job.org

| Field | Value |
|---|---|
| URL | `https://<your-render-host>/internal/tick` |
| Schedule | every 15 minutes, 04:00–12:00 UTC |
| Header | `X-Tick-Secret: <your TICK_SECRET>` |
| Notifications | **on failure** |

Fifteen minutes, not one: this is an alarm, not the driver. At one-minute
intervals it would double every tick for no benefit and burn its own quota.

The 30-second free-tier cut-off is why `TICK_MAX_SECONDS` is 25. If you raise
that setting, raise it knowing this alarm starts reporting failures on healthy
ticks — at which point point it at `/healthz` instead and accept that it no
longer proves the *tick* works, only that the service is up.

---

## 4. Proving it works

In order. Each one rules out a different thing.

**The endpoint, from your laptop:**

```bash
curl -s -H "X-Tick-Secret: $TICK_SECRET" https://<host>/internal/tick | python3 -m json.tool
```

Expect a body with `sent`, `members`, `elapsed_seconds`, `locked`. Then confirm
it is actually guarded — this must be `403`:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://<host>/internal/tick
```

**End to end, which is the only proof that matters.** Queue a real send to your
own address, close the laptop, and do not press anything. It should arrive
within a minute or two during the window, and the job should reach `done` on its
own. If you press the button to check, you have not tested the pinger.

**After a week**, look at two numbers:

- Render usage should read roughly **7 h/day**. Much more means the pinger is
  running outside its window; a 750-hour monthly budget disappears that way.
- `elapsed_seconds` on a busy tick. If it is near 25 and `stopped_early` is
  true, seconds are your binding constraint and `TICK_MAX_SECONDS` is the knob.
  If it is well under 25 and `stopped_early` is still true, `TICK_MAX_MAILS` is.

---

## What this does not do

- **Follow-ups and reply detection still do not run on a timer.** Left out
  deliberately; nothing here changes when they are added.
- **Outside the window, nothing sends by itself.** A send queued at 18:00 sits
  `PENDING` until 10:00. That is why `SCHEDULE_GRACE_HOURS` is 20 — at 6 the
  sweep marked every evening's queue `MISSED` before dawn. Anyone can still
  force it immediately with **Send queued mail now**.
- **Nothing here touches deliverability.** No unsubscribe header, no send-rate
  ramp, no domain authentication. At five or more members sending 800/day the
  domain crosses Google's 5,000/day bulk-sender threshold, which has its own
  requirements. That was an explicit decision, recorded so it stays visible.
