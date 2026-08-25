from django.contrib import admin
from django.urls import include, path

from crm import auth_views

from . import health

urlpatterns = [
    # Django's own auth, for superusers only. The CRM does not use it.
    path("admin/", admin.site.urls),

    # Unauthenticated by design; see config/health.py.
    path("healthz", health.healthz, name="healthz"),

    path("login/", auth_views.login_page, name="login"),
    # NOTE: there is deliberately no password-free "pick your name" route here.
    # It was removed when the CRM moved to a public hostname -- see the module
    # docstring in crm/services/auth.py. Do not reintroduce it;
    # tests/test_login.py::test_the_name_login_route_is_gone will fail if you do.
    path("login/google/", auth_views.google_login, name="google_login"),
    path("login/google/callback/", auth_views.google_callback, name="google_callback"),
    path("join/", auth_views.join, name="join"),
    path("logout/", auth_views.logout_view, name="logout"),

    # A registered redirect URI in Google Cloud Console. Kept at the top level,
    # outside the crm namespace, because moving it breaks every member's Gmail
    # connection until someone updates the console.
    path("settings/gmail/callback/", auth_views.gmail_callback, name="gmail_callback"),
    # NOTE: `/api/v1/` is deliberately gone. It existed so a laptop agent could
    # claim mailings and lease scheduled sends over HTTP; the server now does
    # both in-process. Leaving it reachable would be actively dangerous rather
    # than merely dead: two independent senders could hold the same DRAFT row,
    # and record_result's "already settled" guard would turn a real double-send
    # into a silently ignored report.
    path("", include("crm.urls")),
]
