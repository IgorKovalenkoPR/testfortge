"""The Execute repair — a run has to be able to end.

Measured on staging 2026-09-15: every automated run the operator started
was refused with *"A browser run is already in progress for this team. #4
(walkthrough) started at 2026-09-15 09:07."* Nothing was running. The runs
that had already finished were still open in the database, because the only
writer of ``finished_at`` for an automated run was
``GET /test-execution/results/<id>`` — a page the operator had to reach with
the dispatching tab still open.

Three separate omissions produced that, and each one gets a test here
rather than a comment:

* the detached worker never told the database it had finished, and the
  run ids it was handed were dropped when it assembled ``config_echo``,
  so the import opened a *second* row and closed that one instead;
* every early return in the import abandoned the row it was supposed to
  close, including the one taken when the worker reported a failure;
* there was no way to end a run by hand — no route, no button — so the
  operator's whole remedy was to wait out a thirty-minute window that
  re-armed on the next attempt.

And one that was not about closing at all: the fair-use gate was evaluated
three hundred lines before the handler knew whether the run would launch a
browser, so a checklist run with no URL — which never opens Chromium — was
refused by a message about browser memory. That one lives in
``tests/test_run_limits.py`` beside the rest of the cap.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import db as _db          # noqa: E402
from engine import runner_worker      # noqa: E402


def _age(run_id: int, **delta) -> None:
    """Backdate a run's ``started_at`` so the staleness window applies."""
    with _db.session_scope() as sess:
        row = sess.get(_db.ExecutionRun, run_id)
        row.started_at = datetime.now(timezone.utc) - timedelta(**delta)


# ── The worker closes its own rows ───────────────────────────────────

class TestTheWorkerClosesItsOwnRuns:
    """``finished_at`` must not depend on anybody opening a page."""

    def test_it_closes_every_row_the_dispatcher_opened(self, make_project):
        pid = make_project("worker-closes")
        a = _db.start_execution_run(pid, {"mode": "tc_driven",
                                          "env_type": "web"})
        b = _db.start_execution_run(pid, {"mode": "tc_driven",
                                          "env_type": "mobile_web"})
        closed = runner_worker.close_db_runs(
            {"db_run_ids": {"web": a, "mobile_web": b}},
            "completed", stats={"passed": 3, "failed": 1, "blocked": 0})
        assert closed == 2
        for run_id in (a, b):
            row = _db.get_execution_run(run_id)
            assert row["finished_at"] is not None, (
                f"run {run_id} is still open after the worker finished — "
                f"it goes on holding the one-browser-run slot")
            assert row["status"] == "completed"
        assert _db.get_execution_run(a)["stats"]["passed"] == 3

    def test_a_config_with_no_run_ids_is_not_an_error(self):
        """Configs written before the dispatcher opened rows have no key."""
        assert runner_worker.close_db_runs({}, "completed") == 0
        assert runner_worker.close_db_runs({"db_run_ids": {}}, "failed") == 0

    def test_it_does_not_overwrite_a_verdict_somebody_else_reached(
            self, make_project):
        """A cancel that lands first must survive a late worker.

        ``finish_execution_run`` overwrites whatever it finds, which is
        right for the import — its reconciled stats are the better answer.
        It is wrong here: a run the operator cancelled, re-labelled
        "completed" by a worker that finished afterwards, reads as a run
        nobody cancelled.
        """
        pid = make_project("worker-no-clobber")
        run_id = _db.start_execution_run(pid, {"mode": "tc_driven"})
        _db.close_execution_run_if_open(run_id, status="cancelled")
        assert runner_worker.close_db_runs(
            {"db_run_ids": {"web": run_id}}, "completed") == 0
        assert _db.get_execution_run(run_id)["status"] == "cancelled"

    def test_a_row_that_cannot_be_reached_does_not_crash_the_worker(self):
        """The artefacts on disk are the record; a DB failure is not fatal."""
        assert runner_worker.close_db_runs(
            {"db_run_ids": {"web": 10 ** 9}}, "completed") == 0

    def test_the_terminate_handler_closes_them_too(self, make_project,
                                                   tmp_path):
        """SIGTERM is what a redeploy sends, and it used to leave the row
        open for the next operator to trip over."""
        pid = make_project("worker-sigterm")
        run_id = _db.start_execution_run(pid, {"mode": "walkthrough"})
        runner_worker._write_terminated_artifacts(
            signum=15, config_id="cfg",
            error_path=str(tmp_path / "e.flag"),
            result_path=str(tmp_path / "r.json"),
            done_path=str(tmp_path / "d.flag"),
            config={"db_run_ids": {"web": run_id}},
        )
        row = _db.get_execution_run(run_id)
        assert row["finished_at"] is not None
        assert row["status"] == "terminated"


class TestTheConfigEchoCarriesTheRunIds:
    """The import adopts the dispatcher's rows — it has to be able to see
    them. Every other key it reads comes from ``config_echo``; this one was
    the only one the echo was assembled without, so the adopt lookup found
    nothing, opened a second row, and closed that instead. Two rows per run
    in the register, one of them permanently in progress."""

    def _run_worker(self, tmp_path, monkeypatch, config_extra):
        import engine.live_executor as le

        class _FakeReport:
            run_id = "fake"
            started_at = ""
            finished_at = ""
            base_url = ""
            headless = True
            total = 0
            passed = 0
            failed = 0
            blocked = 0
            duration_ms = 0
            scripts = []

        class _FakeExecutor:
            findings = []
            tc_bindings = []
            early_exit_reason = ""

            def __init__(self, *a, **kw):
                pass

            def run(self, start_urls=None):
                return _FakeReport()

            def dedupe_findings(self):
                return []

        monkeypatch.setattr(le, "LiveExecutor", _FakeExecutor)

        storage = tmp_path / "storage"
        pending = storage / "automation_runs" / "_pending"
        pending.mkdir(parents=True, exist_ok=True)
        config = {
            "storage_root": str(storage),
            "base_url": "https://example.com/",
            "items_data": [],
            "env_types": ["web"],
            "mode": "walkthrough",
            "project_id": "",
        }
        config.update(config_extra)
        config_path = pending / "cfg1.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["runner_worker", str(config_path)])
        assert runner_worker.main() == 0
        return json.loads(
            (pending / "cfg1.result.json").read_text(encoding="utf-8"))

    def test_db_run_ids_survive_into_the_echo(self, tmp_path, monkeypatch):
        payload = self._run_worker(tmp_path, monkeypatch,
                                   {"db_run_ids": {"web": 77}})
        assert payload["config_echo"].get("db_run_ids") == {"web": 77}, (
            "the import reads db_run_ids off config_echo; without it every "
            "successful run orphans the row the dispatcher opened")

    def test_a_finished_worker_closes_the_row_without_anyone_importing(
            self, tmp_path, monkeypatch, make_project):
        pid = make_project("worker-e2e-close")
        run_id = _db.start_execution_run(pid, {"mode": "walkthrough"})
        self._run_worker(tmp_path, monkeypatch,
                         {"db_run_ids": {"web": run_id}})
        assert _db.get_execution_run(run_id)["finished_at"] is not None, (
            "nobody opened /test-execution/results — which is exactly the "
            "case the operator was stuck in")


class TestTheModeDecidesTheEngine:
    """Both automated radio buttons resolved to the same executor.

    ``LiveExecutor`` is a crawler: it runs a case only where
    ``walkthrough_tc_match`` binds one to a URL it landed on, and that
    function never selects a case whose ``trigger`` is ``"manual"`` — the
    default for every case the generator and the editor create. So
    "Automated — Playwright drives the items you select below" selected the
    items, handed them to a crawler, and executed none of them.
    """

    def test_a_default_test_case_is_invisible_to_the_crawler(self):
        from engine.walkthrough_tc_match import match_tcs_for_url
        from engine import editable
        default_trigger = (editable.entity("test_case")
                           .create_defaults.get("trigger"))
        assert default_trigger == "manual", (
            "this test is about the default; if it changed, re-measure")
        tcs = [{"id": "TC_001", "summary": "Verify the form",
                "trigger": default_trigger,
                "url_pattern": "https://example.com/contact"}]
        assert match_tcs_for_url(tcs, "https://example.com/contact") == [], (
            "a crawler cannot be the engine for an item-driven run: it "
            "never selects a case carrying the default trigger")

    def test_tc_driven_no_longer_routes_to_the_crawler(self, tmp_path,
                                                       monkeypatch):
        """The mode the operator reaches first must run their selection."""
        import engine.live_executor as le
        import engine.automation_runner as ar

        used = []

        class _Boom:
            def __init__(self, *a, **kw):
                used.append("live")
                raise AssertionError(
                    "tc_driven reached LiveExecutor — the selected items "
                    "would be crawled for rather than run")

        class _FakeReport:
            run_id = "fake"
            started_at = ""
            finished_at = ""
            base_url = ""
            headless = True
            total = 0
            passed = 0
            failed = 0
            blocked = 0
            duration_ms = 0
            scripts = []

        class _FakeRunner:
            def __init__(self, *a, **kw):
                used.append("automation")

            def run(self, scripts):
                return _FakeReport()

        monkeypatch.setattr(le, "LiveExecutor", _Boom)
        monkeypatch.setattr(ar, "AutomationRunner", _FakeRunner)

        storage = tmp_path / "storage"
        pending = storage / "automation_runs" / "_pending"
        pending.mkdir(parents=True, exist_ok=True)
        config_path = pending / "cfg2.json"
        config_path.write_text(json.dumps({
            "storage_root": str(storage),
            "base_url": "https://example.com/",
            "items_data": [{"id": "TC_001", "summary": "Verify it",
                            "test_steps": "1. Open the page",
                            "expected_result": "It opens"}],
            "env_types": ["web"],
            "mode": "tc_driven",
            "project_id": "",
        }), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["runner_worker", str(config_path)])
        assert runner_worker.main() == 0
        assert used == ["automation"]

        payload = json.loads(
            (pending / "cfg2.result.json").read_text(encoding="utf-8"))
        assert payload["mode"] == "tc_driven"


# ── The reaper ───────────────────────────────────────────────────────

class TestAbandonedRunsAreClosedRatherThanIgnored:
    """``run_limits`` has always skipped runs past the staleness window
    when it counts. Skipping is not closing: the row stayed ``running``, so
    the Runs register advertised a run in progress for a worker that had
    been dead for hours, and the same row was re-examined and re-ignored on
    every later request."""

    def test_a_stale_run_is_closed(self, make_project):
        pid = make_project("reaper-stale")
        run_id = _db.start_execution_run(pid, {"mode": "walkthrough"})
        _age(run_id, hours=4)

        assert _db.close_abandoned_runs([pid], 30) == [run_id]
        closed = _db.get_execution_run(run_id)
        assert closed["finished_at"] is not None
        assert closed["status"] == "abandoned"

    def test_a_fresh_run_is_left_alone(self, make_project):
        pid = make_project("reaper-fresh")
        run_id = _db.start_execution_run(pid, {"mode": "walkthrough"})
        assert _db.close_abandoned_runs([pid], 30) == []
        assert _db.get_execution_run(run_id)["finished_at"] is None

    def test_a_manual_walk_is_never_reaped(self, make_project):
        """A walk is meant to outlive a working day — that is the whole
        point of it being resumable. Ageing one out would close a run the
        tester is coming back to tomorrow."""
        pid = make_project("reaper-manual")
        run_id = _db.start_execution_run(
            pid, {"mode": "manual", "manual_queue": [{"id": "TC_001"}]})
        _age(run_id, days=3)
        assert _db.close_abandoned_runs([pid], 30) == []
        assert _db.get_execution_run(run_id)["finished_at"] is None

    def test_a_closed_run_is_not_touched_again(self, make_project):
        pid = make_project("reaper-closed")
        run_id = _db.start_execution_run(pid, {"mode": "walkthrough"})
        _age(run_id, hours=4)
        _db.finish_execution_run(run_id, status="completed")
        assert _db.close_abandoned_runs([pid], 30) == []
        assert _db.get_execution_run(run_id)["status"] == "completed"
