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
    path("logout/", auth_views.logout_view, name="logout"),

    # A registered redirect URI in Google Cloud Console. Kept at the top level,
    # outside the crm namespace, because moving it breaks every member's Gmail
    # connection until someone updates the console.
    path("settings/gmail/callback/", auth_views.gmail_callback, name="gmail_callback"),
    path("api/v1/", include("crm.api.urls")),
    path("", include("crm.urls")),
]
