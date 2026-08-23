"""The front door. Kept out of views.py because everything there is already
past the gate -- these four views are the only ones a stranger can reach.
"""

from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .services import auth as auth_svc
from .services import gmail_oauth


def login_page(request):
    if auth_svc.current_member(request):
        return redirect("crm:home")

    return render(request, "crm/login.html", {
        "google_enabled": auth_svc.google_enabled(),
        "hosted_domain": auth_svc.hosted_domain(),
    })


def google_login(request):
    if not auth_svc.google_enabled():
        messages.error(
            request,
            "Google sign-in is not configured on this deployment. Set "
            "GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET.",
        )
        return redirect("login")

    return redirect(auth_svc.google_authorization_url(request))


def google_callback(request):
    try:
        member = auth_svc.member_from_google_callback(request)
    except auth_svc.GoogleAuthError as exc:
        messages.error(request, str(exc))
        return redirect("login")

    auth_svc.login_member(request, member)
    return redirect("crm:home")


@require_POST
def logout_view(request):
    auth_svc.logout_member(request)
    return redirect("login")


def gmail_callback(request):
    """Where Google returns after a member grants mailbox access.

    Top-level rather than under the crm namespace because it is a registered
    redirect URI in Google Cloud Console: the path is part of the app's public
    contract with Google and must not move when the CRM's URLs are reorganised.

    A failure here leaves the member signed in with a message. Gmail access is
    an additional capability, not a condition of using the CRM.
    """
    member = auth_svc.current_member(request)
    if member is None:
        return redirect("login")

    try:
        credential = gmail_oauth.complete(request, member)
    except gmail_oauth.GmailConsentError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"Gmail connected as {credential.google_email}. Mail you send from "
            f"the CRM will now go out from that mailbox.",
        )
    return redirect("crm:gmail_settings")
