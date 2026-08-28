"""The scheduler's door, and the lock behind it.

`GET /internal/tick` is the only route on this deployment that is not
authenticated as a person. It exists because Render's free plan has no cron and
the instance sleeps between requests, so only a request can wake the process.

Two things are being pinned. The auth is the obvious one. The lock is the
important one: two gunicorn workers plus two pingers plus a button all reach
`runner.tick()`, and without a lock a Supabase cron and a cron-job.org cron
landing in the same second run two ticks that both lease and both send.
"""

import json

import pytest
from django.urls import reverse

from crm.models import TeamMember
from crm.services import runner
from crm.tests.conftest import make_member

pytestmark = pytest.mark.django_db

SECRET = "test-tick-secret-value"


@pytest.fixture
def configured(settings):
    settings.TICK_SECRET = SECRET
    return settings


def get(client, secret=None):
    headers = {"HTTP_X_TICK_SECRET": secret} if secret is not None else {}
    return client.get(reverse("internal_tick"), **headers)


class TestTheDoor:
    def test_the_right_secret_runs_a_tick(self, client, configured):
        response = get(client, SECRET)

        assert response.status_code == 200
        body = json.loads(response.content)
        assert body["sent"] == 0
        assert "started_at" in body

    def test_no_header_is_refused(self, client, configured):
        assert get(client).status_code == 403

    def test_a_wrong_secret_is_refused(self, client, configured):
        assert get(client, "not-it").status_code == 403

    def test_an_empty_header_is_refused(self, client, configured):
        """The one that would be silent. A blank compare_digest against a blank
        setting authenticates everybody who sends nothing."""
        assert get(client, "").status_code == 403

    def test_an_unconfigured_deployment_disables_the_route(self, client, settings):
        settings.TICK_SECRET = ""

        response = get(client, "")

        assert response.status_code == 503
        assert "not configured" in json.loads(response.content)["error"]

    def test_it_refuses_to_be_configured_open(self, client, settings):
        """Blank secret must not become a blank-header login."""
        settings.TICK_SECRET = ""
        assert get(client).status_code == 503
        assert get(client, "anything").status_code == 503

    def test_post_is_refused(self, client, configured):
        response = client.post(
            reverse("internal_tick"), **{"HTTP_X_TICK_SECRET": SECRET}
        )
        assert response.status_code == 405

    def test_the_secret_is_not_echoed_back(self, client, configured):
        assert SECRET.encode() not in get(client, SECRET).content
        assert SECRET.encode() not in get(client, "wrong").content


class TestTheReportIsUseful:
    """A pinger that cannot tell a working tick from a stalled one is a
    keep-alive, not a scheduler."""

    def test_it_reports_the_housekeeping_it_did(self, client, configured):
        body = json.loads(get(client, SECRET).content)

        assert "leases_recovered" in body
        assert "marked_missed" in body
        assert "locked" in body
        assert "stopped_early" in body

    def test_a_member_with_no_gmail_is_not_counted_as_sendable(
        self, client, configured, team
    ):
        make_member(team)

        body = json.loads(get(client, SECRET).content)

        assert body["members"] == 0


class TestTheLock:
    """Session-level, so a second CONNECTION is refused. Re-entrant within one
    connection by design -- the hazard is two workers, not one process."""

    def test_a_second_connection_cannot_run_at_the_same_time(self, configured):
        from django.db import connections

        holder = connections.create_connection("default")
        try:
            with holder.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_lock(%s)", [runner.TICK_LOCK_KEY]
                )
                assert cur.fetchone()[0] is True, "could not stage the lock"

                report = runner.tick()

                assert report.locked is True
                assert report.sent == 0
                assert report.members == 0
        finally:
            with holder.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", [runner.TICK_LOCK_KEY])
            holder.close()

    def test_the_endpoint_says_so_rather_than_erroring(self, client, configured):
        """A locked tick is a 200. cron-job.org alerts on non-200, and "the
        other pinger got there first" is not a failure worth waking anyone."""
        from django.db import connections

        holder = connections.create_connection("default")
        try:
            with holder.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", [runner.TICK_LOCK_KEY])
                cur.fetchone()

                response = get(client, SECRET)

            assert response.status_code == 200
            assert json.loads(response.content)["locked"] is True
        finally:
            with holder.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", [runner.TICK_LOCK_KEY])
            holder.close()

    def test_the_lock_is_released_afterwards(self, configured):
        """A tick that kept its lock would stop every later tick forever."""
        runner.tick()

        from django.db import connections

        other = connections.create_connection("default")
        try:
            with other.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", [runner.TICK_LOCK_KEY])
                assert cur.fetchone()[0] is True, "the previous tick held on to it"
                cur.execute("SELECT pg_advisory_unlock(%s)", [runner.TICK_LOCK_KEY])
        finally:
            other.close()

    def test_it_is_released_even_when_the_tick_raises(self, configured, monkeypatch):
        monkeypatch.setattr(
            runner, "sendable_members",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        with pytest.raises(RuntimeError):
            runner.tick()

        from django.db import connections

        other = connections.create_connection("default")
        try:
            with other.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", [runner.TICK_LOCK_KEY])
                assert cur.fetchone()[0] is True, "a crashed tick kept the lock"
                cur.execute("SELECT pg_advisory_unlock(%s)", [runner.TICK_LOCK_KEY])
        finally:
            other.close()
