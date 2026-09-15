"""The Runs register, and the way out of a wedged run.

``/test-execution/runs`` listed a run's mode and start time and nothing
about what it found, and it offered an action only for a manual walk. For
every automated run it was a dead end: no link to the results (the only
route to those was the dispatching tab's own auto-redirect, so closing the
tab lost the run), no verdict counts, and — the one the operator was
actually blocked by — no way to end a run that had stopped being real.

The page also hid a tester's own runs from them. Its "assigned to me" scope
filtered on ``assignee_id``, which only the manual walk ever writes, so a
non-admin who dispatched an automated run saw "Nothing is assigned to you
in this project yet" on the page that exists to show it to them.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import db as _db                       # noqa: E402
from routes._shared import SERVER_START_TIME       # noqa: E402


def _activate(client, pid: str) -> None:
    with client.session_transaction() as sess:
        sess["_session_active_since"] = SERVER_START_TIME
        sess["project_id"] = pid


def _seed_run(pid: str, identity: dict, **payload) -> int:
    """A run seeded the way a real dispatch writes one.

    ``started_by`` matters and is easy to leave out. With authentication on
    the register scopes to the caller, so a row without it is correctly
    hidden — and a test that omits it then asserts against a page that is
    behaving. The dispatcher stamps it (``routes/execution.py``,
    ``_perm_uid``); seeding has to as well, or these tests pass with the
    flags off and fail in the mode the product ships in.
    """
    payload.setdefault("mode", "walkthrough")
    payload["started_by"] = identity.get("user_id") or ""
    return _db.start_execution_run(pid, payload)


class TestAnOpenRunCanBeEnded:
    """The remedy the refusal message names has to exist."""

    def test_cancel_closes_the_run_and_frees_the_slot(
            self, client, make_project, monkeypatch):
        monkeypatch.setenv("TESTFORTGE_MAX_CONCURRENT_RUNS", "1")
        pid = make_project("cancel-frees-slot")
        _activate(client, pid)
        run_id = _db.start_execution_run(pid, {"mode": "walkthrough"})

        from engine import run_limits
        assert not run_limits.check([pid]).allowed, "precondition"

        resp = client.post(f"/test-execution/runs/{run_id}/cancel",
                           follow_redirects=True)
        assert resp.status_code == 200

        row = _db.get_execution_run(run_id)
        assert row["finished_at"] is not None
        assert row["status"] == "cancelled"
        assert run_limits.check([pid]).allowed, (
            "cancelling a run has to release the slot, or it is a button "
            "that changes a label and nothing else")

    def test_cancelling_a_closed_run_says_so_rather_than_lying(
            self, client, make_project):
        pid = make_project("cancel-already-closed")
        _activate(client, pid)
        run_id = _db.start_execution_run(pid, {"mode": "walkthrough"})
        _db.finish_execution_run(run_id, status="completed")

        resp = client.post(f"/test-execution/runs/{run_id}/cancel",
                           follow_redirects=True)
        assert resp.status_code == 200
        assert _db.get_execution_run(run_id)["status"] == "completed", (
            "a cancel must not overwrite the verdict a finished run "
            "already reached")

    def test_a_run_in_another_project_is_not_cancellable(
            self, client, make_project):
        """Same scope rule as every other run route: a different project
        active is the accidental case, and it is the one that damages
        data."""
        mine = make_project("cancel-scope-mine")
        theirs = make_project("cancel-scope-theirs")
        _activate(client, mine)
        run_id = _db.start_execution_run(theirs, {"mode": "walkthrough"})

        resp = client.post(f"/test-execution/runs/{run_id}/cancel")
        assert resp.status_code == 404
        assert _db.get_execution_run(run_id)["finished_at"] is None


class TestTheRegisterIsNotADeadEnd:

    def test_an_open_run_offers_a_cancel(self, client, make_project,
                                         suite_identity):
        pid = make_project("register-cancel-button")
        _activate(client, pid)
        run_id = _seed_run(pid, suite_identity)

        body = client.get("/test-execution/runs").get_data(as_text=True)
        assert f"/test-execution/runs/{run_id}/cancel" in body, (
            "an open run with no control is a run the operator can only "
            "clear with SQL")

    def test_a_closed_run_offers_none(self, client, make_project,
                                      suite_identity):
        pid = make_project("register-no-cancel-when-closed")
        _activate(client, pid)
        run_id = _seed_run(pid, suite_identity)
        _db.finish_execution_run(run_id, status="completed")

        body = client.get("/test-execution/runs").get_data(as_text=True)
        assert f"/test-execution/runs/{run_id}/cancel" not in body

    def test_an_automated_run_links_to_its_results(self, client,
                                                   make_project,
                                                   suite_identity):
        pid = make_project("register-results-link")
        _activate(client, pid)
        _seed_run(pid, suite_identity, mode="tc_driven",
                  config_id="20260915_090700_abc123")

        body = client.get("/test-execution/runs").get_data(as_text=True)
        assert "/test-execution/results/20260915_090700_abc123" in body, (
            "without this the only route to a finished automated run was "
            "the dispatching tab's auto-redirect")

    def test_the_mode_column_uses_the_name_the_operator_chose(
            self, client, make_project, suite_identity):
        """``live`` is the executor's internal name and appears nowhere in
        the UI the operator used to start the run."""
        pid = make_project("register-mode-label")
        _activate(client, pid)
        _seed_run(pid, suite_identity, mode="live")

        body = client.get("/test-execution/runs").get_data(as_text=True)
        assert ">live<" not in body
        assert "Automated" in body

    def test_verdict_counts_are_shown_for_a_finished_run(self, client,
                                                         make_project,
                                                         suite_identity):
        pid = make_project("register-counts")
        _activate(client, pid)
        run_id = _seed_run(pid, suite_identity, mode="tc_driven")
        _db.finish_execution_run(run_id, status="completed",
                                 stats={"passed": 7, "failed": 2,
                                        "blocked": 1})

        body = client.get("/test-execution/runs").get_data(as_text=True)
        assert "7P" in body and "2F" in body and "1B" in body, (
            "the register answered 'when did this run start' and never "
            "'what did it find'")


class TestTheCancelFormWorksWithCsrfOn:
    """The suite disables CSRF, production does not.

    This repository has shipped a POST endpoint that passed every test and
    answered 400 in production more than once — a form that renders no
    token, or an endpoint that needs an exemption it did not get. The
    Cancel button is the operator's only way out of a wedged run, so a 400
    here would put them back where they started, with a page that looks
    like it has a control.
    """

    def test_the_form_renders_a_token_and_the_post_is_accepted(
            self, client, make_project, monkeypatch, suite_identity):
        import re
        monkeypatch.setitem(client.application.config,
                            "WTF_CSRF_ENABLED", True)
        pid = make_project("cancel-csrf-on")
        _activate(client, pid)
        run_id = _seed_run(pid, suite_identity)

        body = client.get("/test-execution/runs").get_data(as_text=True)
        assert f"/test-execution/runs/{run_id}/cancel" in body
        token = re.search(r'name="csrf_token" value="([^"]+)"', body)
        assert token, "the cancel form renders no CSRF token"

        resp = client.post(f"/test-execution/runs/{run_id}/cancel",
                           data={"csrf_token": token.group(1)})
        assert resp.status_code == 302, resp.get_data(as_text=True)[:400]
        assert _db.get_execution_run(run_id)["finished_at"]

    def test_a_post_without_a_token_is_refused(
            self, client, make_project, monkeypatch, suite_identity):
        """The other half: the guard has to actually be on, or the test
        above proves only that a token was rendered."""
        monkeypatch.setitem(client.application.config,
                            "WTF_CSRF_ENABLED", True)
        pid = make_project("cancel-csrf-off-token")
        _activate(client, pid)
        run_id = _seed_run(pid, suite_identity)

        assert client.post(
            f"/test-execution/runs/{run_id}/cancel").status_code == 400
        assert not _db.get_execution_run(run_id)["finished_at"]
