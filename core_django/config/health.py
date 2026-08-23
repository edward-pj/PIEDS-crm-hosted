"""The platform's liveness probe.

Deliberately the dullest view in the codebase: no authentication, no session, no
database query. Each of those would be a reason for the check to fail while the
process is perfectly healthy -- and a health check that fails on a slow Supabase
pooler causes the platform to restart a container that had nothing wrong with
it, turning a transient database blip into an outage.

"Is this process able to answer HTTP?" is the only question it answers, and the
only one the platform is asking. Whether the database is reachable is answered
once at boot by `manage.py check_db`, which refuses to serve at all if it is not.
"""

from django.http import JsonResponse


def healthz(request):
    return JsonResponse({"ok": True})
