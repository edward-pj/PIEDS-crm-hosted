"""Who may drain the send queue.

While the scheduler is deferred, `POST /schedules/run/` is not an administrative
convenience -- it is the send path. `views.send` queues a `ScheduledSend` and
nothing on the server drains it on a timer, so a member who cannot reach this
route cannot send mail at all, however many times they press Send.

It was `@lead_required` when Phase 5 shipped. That made every member's mail wait
on a lead noticing a queue on a screen the member had already left. These tests
exist so it does not quietly become lead-only again: the first one is the
tripwire, and the rest pin what the widening did *not* change.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from crm.models import ScheduledSend
from crm.tests.conftest import make_lead, make_member, sign_in

pytestmark = pytest.mark.django_db


@pytest.fixture
def lead(team):
    return make_lead(team)


@pytest.fixture
def member(team):
    return make_member(team)


class TestAnyMemberMayDrainTheQueue:
    def test_an_ordinary_member_may_press_it(self, client, member, monkeypatch):
        """The tripwire. A failure here means members cannot send."""
        called = []
        monkeypatch.setattr(
            "crm.views.runner.tick",
            lambda *a, **k: called.append(True) or _empty_report(),
        )

        sign_in(client, member)
        r = client.post(reverse("crm:run_queue"))

        assert r.status_code == 302
        assert r["Location"] == reverse("crm:schedule_list")
        assert called, "the tick did not run for a non-lead"

    def test_a_lead_may_still_press_it(self, client, lead, monkeypatch):
        monkeypatch.setattr("crm.views.runner.tick", lambda *a, **k: _empty_report())

        sign_in(client, lead)
        assert client.post(reverse("crm:run_queue")).status_code == 302

    def test_the_button_is_offered_to_a_non_lead(self, client, member):
        sign_in(client, member)
        body = client.get(reverse("crm:schedule_list")).content.decode()

        assert reverse("crm:run_queue") in body
        assert "Send queued mail now" in body


class TestTheWideningGrantsNothingElse:
    """The route is wider; nothing around it is."""

    def test_a_stranger_still_cannot(self, client):
        r = client.post(reverse("crm:run_queue"))
        assert r.status_code == 302
        assert r["Location"].startswith(reverse("login"))

    def test_get_is_still_refused(self, client, member):
        """State-changing and CSRF-protected: POST only."""
        sign_in(client, member)
        assert client.get(reverse("crm:run_queue")).status_code == 405

    def test_cancelling_someone_elses_job_is_still_lead_only(
        self, client, team, member, lead
    ):
        """The brake stays with leads. Draining a queue the team already
        approved is a different act from stopping one mid-flight."""
        from crm.models import Campaign
        from shared.enums import CampaignStatus

        campaign = Campaign.objects.create(
            title="Ignite", mail_sub="s", mail_body="b",
            status=CampaignStatus.ACTIVE.value, team=team,
        )
        job = ScheduledSend.objects.create(
            campaign=campaign, member=lead, contact_ids=[],
            scheduled_at=timezone.now(),
        )

        sign_in(client, member)
        r = client.post(reverse("crm:schedule_cancel", args=[job.pk]))

        assert r.status_code in (302, 403)
        job.refresh_from_db()
        assert job.status != "cancelled"


def _empty_report():
    from crm.services.runner import TickReport

    return TickReport(started_at="now")
