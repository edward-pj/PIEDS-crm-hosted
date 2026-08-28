"""The one door an external scheduler is allowed through.

Render's free plan has no cron and the instance sleeps after fifteen minutes of
silence, so **only a request can wake this process**. Everything periodic
therefore happens inside a request that something outside makes on a timer: a
Supabase `pg_cron` job calling `net.http_get` every minute, with cron-job.org
pointed at the same URL purely so somebody is told when it stops answering.

This is not a new pattern in the codebase, it is the old one restored. The
retired `api/views.py::schedule_claim` already ran the sweeps on the polling
path, with the comment "the housekeeping runs as often as the thing it cleans
up, with no extra process to keep alive". Hosting only changed who polls.

**Deliberately not an `ApiToken`.** Those existed so a laptop could authenticate
as a member; the whole tree was deleted in Phase 5 and reviving it would put a
second sender back on the network. A pinger is not a team member and holds no
member's authority -- it starts work that members already queued and approved.
A shared secret is the honest shape for that, and it is compared with
`secrets.compare_digest` so the comparison does not leak its own answer.
"""

import logging
import secrets

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from crm.services import runner

log = logging.getLogger(__name__)

#: Sent by the pinger. A header, never a query parameter: Render logs the full
#: request line, so `?key=...` writes the credential into the log of every
#: request that carries it.
SECRET_HEADER = "HTTP_X_TICK_SECRET"


def tick_enabled() -> bool:
    """Whether this deployment has a tick secret at all.

    Same shape as `auth.google_login_allowed()`: an unset secret disables the
    route rather than defaulting it open. A blank `compare_digest` against a
    blank header would otherwise authenticate anybody who sends no header at
    all, which is the failure you would never notice.
    """
    return bool(getattr(settings, "TICK_SECRET", ""))


@csrf_exempt
@require_GET
def tick(request):
    """Drain whatever is due. Returns the TickReport as JSON.

    JSON rather than an empty 200 so the pinger's failure detection means
    something: "answered 200 with sent=0 for six hours" and "has not answered
    at all" are different problems with different fixes.
    """
    if not tick_enabled():
        return JsonResponse(
            {"error": "tick endpoint is not configured on this deployment"},
            status=503,
        )

    presented = request.META.get(SECRET_HEADER, "")
    if not secrets.compare_digest(presented, settings.TICK_SECRET):
        # No detail. "Wrong secret" and "no secret" are the same answer to
        # whoever is asking, and distinguishing them only helps them.
        log.warning("rejected tick from %s", request.META.get("REMOTE_ADDR", "?"))
        return JsonResponse({"error": "forbidden"}, status=403)

    report = runner.tick()
    if report.locked:
        # Normal. Two pingers plus a button all reach this function, and the
        # lock is what makes that safe rather than a double-send.
        return JsonResponse(report.dict(), status=200)

    log.info(
        "tick sent=%s skipped=%s jobs=%s members=%s",
        report.sent, report.skipped, report.jobs, report.members,
    )
    return JsonResponse(report.dict(), status=200)
