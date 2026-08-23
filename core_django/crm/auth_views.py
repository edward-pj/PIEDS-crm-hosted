"""The front door. Kept out of views.py because everything there is already
past the gate -- these four views are the only ones a stranger can reach.
"""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .services import auth as auth_svc
from .services import gmail_oauth
from .services import teams as team_svc


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


#: Where the verified-but-unknown identity waits while they enter a join code.
#: The address is NEVER read from a form field -- it is the one Google signed.
PENDING_JOIN_KEY = "pending_join"


def google_callback(request):
    try:
        member = auth_svc.member_from_google_callback(request)
    except auth_svc.UnknownMember as exc:
        # Verified BITS address, no member row yet. Carry the proven identity
        # to the join screen rather than refusing: they are a new joiner, and
        # the alternative is a lead adding every person by hand in /admin/.
        request.session[PENDING_JOIN_KEY] = {
            "email": exc.email, "name": exc.display_name,
        }
        return redirect("join")
    except auth_svc.GoogleAuthError as exc:
        messages.error(request, str(exc))
        return redirect("login")

    auth_svc.login_member(request, member)
    return redirect("crm:home")


def join(request):
    """Turn a verified BITS identity plus a join code into a team member.

    Both halves are required and neither is sufficient. Google proves WHO; the
    code decides WHICH TEAM. Reaching this page without the first is a dead end
    by design -- there is no field to type an address into, because a field
    would be a field an attacker could type into.
    """
    pending = request.session.get(PENDING_JOIN_KEY)
    if not pending:
        messages.error(request, "Sign in with your BITS Google account first.")
        return redirect("login")

    if request.method == "POST":
        if team_svc.attempts_exhausted(request.session):
            messages.error(
                request,
                "Too many attempts. Close the browser and start again, or ask "
                "a lead to check the code.",
            )
            return redirect("login")
        team_svc.record_attempt(request.session)

        try:
            member = team_svc.join(
                code=request.POST.get("code", ""),
                email=pending["email"],
                display_name=pending.get("name", ""),
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            request.session.pop(PENDING_JOIN_KEY, None)
            request.session.pop(team_svc.JOIN_ATTEMPTS_SESSION_KEY, None)
            auth_svc.login_member(request, member)
            messages.success(request, f"Welcome, {member.name}.")
            return redirect("crm:home")

    return render(request, "crm/join.html", {"email": pending["email"]})


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
