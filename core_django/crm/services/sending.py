"""The send loop, moved from the laptop to the server.

This is `local_agent/services/send.py` with the HTTP hop removed: it calls
`mailing.claim_batch` and `mailing.record_result` directly instead of posting to
`/api/v1/`. Everything the agent did over the network it now does in-process,
against the same functions, in the same order.

**The ordering is the guarantee and it is unchanged.** `claim_batch` commits a
DRAFT row per contact *before* this code is told the contact exists, so by the
time a mail can leave the building a durable record of the attempt already
exists. A crash can leave an ambiguous DRAFT (resolvable against Gmail by
`reconcile`) but never a sent mail with no record.

What genuinely changed, and is worth knowing:

- One process now sends for the whole team, where before there were as many
  processes as laptops. `claim_batch` releases its row lock before the Gmail
  round trip, which used to be a nicety and is now the thing that stops one
  slow send serialising everybody.
- A run is bounded by `max_mails` / `deadline` because it executes inside a
  request on an instance that can be reaped. Nothing is lost when a run stops
  early: the unclaimed contacts were never touched, and the next run picks them
  up. This is why the loop checks its budget between chunks rather than trying
  to finish what it started.
"""

import logging
import time
from dataclasses import asdict, dataclass

from django.conf import settings
from django.utils import timezone

from . import mailing
from .gmail import GmailAuthError, GmailClient

log = logging.getLogger(__name__)

SENT = mailing.SENT
FAILED = mailing.FAILED
ALREADY_MAILED = mailing.ALREADY_MAILED
CAP_REACHED = mailing.CAP_REACHED

#: Contacts reserved per round trip.
#:
#: This number is the difference between a bad afternoon and a wasted week.
#: Claiming a whole 400-contact selection up front commits a DRAFT row for every
#: one of them BEFORE a single mail is sent -- and a batch that size takes over
#: ten minutes to work through. When the connection died mid-batch (it did:
#: WinError 10053 on a Windows laptop, 19 Aug), 511 contacts were left claimed
#: but unsent, and a claimed contact cannot be re-claimed. The mails could not
#: simply be sent again.
#:
#: Claiming ten at a time bounds that damage to ten. Anything left over is
#: recoverable with "Resolve stranded drafts" instead of being a data-repair job.
#:
#: Hosting makes this MORE important, not less: the instance can be reaped
#: mid-run with no warning at all, where a laptop at least had a person watching.
CLAIM_CHUNK = 10


@dataclass
class Outcome:
    contact_id: str
    email: str
    name: str
    status: str
    detail: str = ""

    def dict(self):
        return asdict(self)


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def send_batch(
    member,
    campaign,
    contact_ids,
    *,
    cc: str = "",
    bcc: str = "",
    gmail=None,
    delay=None,
    chunk_size: int = CLAIM_CHUNK,
    max_mails: int | None = None,
    deadline=None,
):
    """Claim, send, report. Yields an Outcome per contact as it resolves.

    `gmail` is injected so tests can pass a fake; production passes nothing and
    gets the member's real client.

    `max_mails` and `deadline` bound the run. Reaching either stops cleanly
    between chunks -- never mid-chunk, because a chunk's contacts are already
    claimed and would be stranded.
    """
    delay = settings.GMAIL_SEND_DELAY_SECONDS if delay is None else delay
    gmail = GmailClient(member) if gmail is None else gmail

    sent_count = 0
    remaining = len(contact_ids)

    for chunk in _chunks(list(contact_ids), max(1, chunk_size)):
        # Checked BEFORE claiming, never after: anything claimed must be
        # resolved in this run or it becomes a stranded draft for no reason.
        if max_mails is not None and sent_count >= max_mails:
            return
        if deadline is not None and timezone.now() >= deadline:
            return

        try:
            claimed, skipped = mailing.claim_batch(
                campaign, member, chunk, cc=cc, bcc=bcc
            )
        except mailing.InvalidCopyAddresses:
            # Raised before any row is written, and it will fail identically for
            # every remaining chunk, so stop rather than emit the same error N
            # times.
            raise
        except Exception as exc:                                # noqa: BLE001
            # Nothing was claimed by a failed claim, so nothing is stranded.
            log.exception("claim failed for %s", member.bits_email)
            for cid in chunk:
                yield Outcome(str(cid), "", "", FAILED, f"could not reserve: {exc}")
            return

        # A cap-blocked contact is the one refusal that is TEMPORARY: the
        # rolling 24-hour window frees it again, and they are still owed a mail.
        # Note it, but do not act on it until this chunk's claimed contacts have
        # been settled -- they hold DRAFT rows, and returning early would strand
        # every one of them for a quota that has nothing to do with them.
        cap_reached = any(skip.code == CAP_REACHED for skip in skipped)

        # Report everything the server refused before touching Gmail.
        for skip in skipped:
            yield Outcome(
                contact_id=skip.contact_id,
                email=skip.email,
                name=skip.name,
                status=skip.code,
                detail=skip.reason,
            )

        for index, item in enumerate(claimed):
            base = {
                "contact_id": item.contact_id,
                "email": item.to,
                "name": item.name,
            }

            try:
                result = gmail.send(
                    to=item.to,
                    subject=item.subject,
                    body=item.body,
                    body_html=item.body_html,
                    # The envelope is the server's word: it validated these
                    # addresses and already recorded them on the DRAFT row.
                    from_name=item.from_name,
                    cc=item.cc,
                    bcc=item.bcc,
                )
            except GmailAuthError as exc:
                # The credential is bad, so every remaining send fails the same
                # way. Stop -- but settle every contact ALREADY CLAIMED in this
                # chunk first, including this one.
                #
                # Returning here without settling them would leave DRAFT rows,
                # and a DRAFT blocks its contact from ever being mailed for this
                # campaign until somebody runs reconcile -- for mail that
                # provably never left the building. FAILED is both true and
                # re-claimable. Contacts in later chunks were never claimed and
                # are untouched, which is what stops one dead token from burning
                # a member's whole queue.
                for pending in claimed[index:]:
                    mailing.record_result(
                        pending.mailing_id, member, status="failed", error=str(exc)
                    )
                    yield Outcome(
                        contact_id=pending.contact_id, email=pending.to,
                        name=pending.name, status=FAILED, detail=str(exc),
                    )
                return
            except Exception as exc:                            # noqa: BLE001
                detail = f"{type(exc).__name__}: {exc}"
                try:
                    mailing.record_result(
                        item.mailing_id, member, status="failed", error=detail
                    )
                except Exception:                               # noqa: BLE001
                    # The mail definitely did not go out; the DRAFT stays and
                    # reconcile will clear it later.
                    log.exception("could not record failure for %s", item.mailing_id)
                yield Outcome(**base, status=FAILED, detail=detail)
            else:
                sent_count += 1
                try:
                    mailing.record_result(
                        item.mailing_id, member, status="sent",
                        message_id=result.message_id, thread_id=result.thread_id,
                    )
                    yield Outcome(**base, status=SENT, detail=result.thread_id)
                except Exception as exc:                        # noqa: BLE001
                    # Worst case: mail sent, database not told. The DRAFT row is
                    # the evidence and reconcile resolves it against Gmail.
                    log.exception("sent but failed to record %s", item.mailing_id)
                    yield Outcome(
                        **base, status=SENT,
                        detail=f"sent, but recording failed ({exc}) — run reconcile",
                    )

            remaining -= 1
            if delay and remaining > 0:
                time.sleep(delay)

        if cap_reached:
            # The budget cannot recover inside one run, so every later chunk
            # would come back refused identically. Stopping here is not just an
            # optimisation: without it the loop rips through the whole remainder
            # in seconds, and the caller counts each refusal as an attempt.
            return


def reconcile(member, *, gmail=None, max_rounds: int = 20) -> list[Outcome]:
    """Resolve DRAFTs stranded by a crash between claim and record.

    Asks Gmail whether each mail actually went out, then records the answer.
    This is what makes the crash window safe rather than merely visible -- and
    it is the ONLY way a stranded draft becomes sendable again, because a draft
    that might already be sitting in a prospect's inbox must never be assumed
    undelivered.

    One unreadable draft is skipped rather than aborting the run: the whole
    point is to clear a backlog.
    """
    gmail = GmailClient(member) if gmail is None else gmail
    outcomes: list[Outcome] = []
    seen: set[str] = set()

    for _ in range(max_rounds):
        drafts = [
            d for d in mailing.stranded_drafts(member)[:100]
            if str(d.id) not in seen
        ]
        if not drafts:
            break

        for draft in drafts:
            seen.add(str(draft.id))
            base = {
                "contact_id": str(draft.contact_id),
                "email": draft.contact.email,
                "name": draft.contact.full_name,
            }
            try:
                found = gmail.find_message_to(
                    draft.contact.email, draft.rendered_subject
                )
                if found:
                    mailing.record_result(
                        draft.id, member, status="sent",
                        message_id=found.message_id, thread_id=found.thread_id,
                    )
                    outcomes.append(
                        Outcome(**base, status=SENT, detail="confirmed in Gmail")
                    )
                else:
                    mailing.record_result(
                        draft.id, member, status="failed",
                        error="stranded draft; no matching sent message",
                    )
                    outcomes.append(
                        Outcome(**base, status=FAILED, detail="marked failed, safe to retry")
                    )
            except Exception as exc:                            # noqa: BLE001
                # Leave it a DRAFT and move on; the next run picks it up again.
                outcomes.append(
                    Outcome(**base, status=FAILED, detail=f"could not resolve: {exc}")
                )

    return outcomes
