"""What a dispatch says about itself, and what it must not claim.

Three findings from the Execute beta test, each of which produced a
confident wrong answer rather than an error:

* the detached worker's working directory was derived from where artefacts
  live, so every dispatch died at exec on any deployment that puts them on
  a mounted volume — while the POST flashed "✓ Playwright pass dispatched";
* a worker that died before its first write left ``run-status`` answering
  ``queued`` with no upper bound, so the widget painted "● running" for
  ever on a process that had never existed;
* a run with no Base URL executes nothing and invents its verdicts, and it
  was filing those as bug reports — severity, reporter, steps to reproduce
  and all — with nothing on the board saying they were invented.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import db as _db                       # noqa: E402
from routes._shared import SERVER_START_TIME       # noqa: E402


def _activate(client, pid: str) -> None:
    with client.session_transaction() as sess:
        sess["_session_active_since"] = SERVER_START_TIME
        sess["project_id"] = pid


class TestTheWorkerIsSpawnedWhereItCanBeImported:
    """``python -m engine.runner_worker`` resolves ``engine`` from the cwd.

    The spawn sites passed ``dirname(STORAGE_ROOT)``, which is the
    application root only while artefacts sit inside the checkout — the one
    arrangement ``STORAGE_ROOT`` exists to let a deployment change. Point it
    at a volume and every run died with ``No module named 'engine'`` in a
    log nobody reads, with a success flash on screen.
    """

    def test_app_root_is_where_the_packages_are(self):
        from engine.automation_paths import APP_ROOT
        assert os.path.isdir(os.path.join(APP_ROOT, "engine"))
        assert os.path.isdir(os.path.join(APP_ROOT, "routes"))

    def test_app_root_does_not_move_with_the_artefact_directory(self):
        from engine.automation_paths import APP_ROOT, STORAGE_ROOT
        assert APP_ROOT != os.path.dirname(STORAGE_ROOT) or \
            os.path.isdir(os.path.join(os.path.dirname(STORAGE_ROOT),
                                       "engine")), (
            "these are equal only by coincidence; the test suite already "
            "points STORAGE_ROOT elsewhere, which is why the dispatch was "
            "never exercised end to end")

    def test_every_spawn_site_uses_it(self):
        """Three call sites had the same bug; a grep keeps them together."""
        from engine.automation_paths import APP_ROOT
        for path in ("routes/execution.py", "routes/debug.py",
                     "mcp_server/server.py"):
            full = os.path.join(APP_ROOT, path)
            body = open(full, encoding="utf-8").read()
            if "engine.runner_worker" not in body:
                continue
            assert "dirname(STORAGE_ROOT)" not in body \
                and "dirname(storage_root)" not in body, (
                f"{path} still derives the worker's cwd from the artefact "
                f"directory")


class TestADispatchThatNeverStartedIsNotStillRunning:

    def _dispatch_dir(self, client, tmp_path, monkeypatch, pid, age_s):
        from routes.automation import STORAGE_ROOT
        pending = os.path.join(STORAGE_ROOT, "automation_runs", "_pending")
        os.makedirs(pending, exist_ok=True)
        config_id = "neverstarted01"
        run_id = _db.start_execution_run(pid, {"mode": "tc_driven",
                                               "config_id": config_id})
        config_path = os.path.join(pending, f"{config_id}.json")
        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump({"db_run_ids": {"web": run_id}}, fh)
        stamp = time.time() - age_s
        os.utime(config_path, (stamp, stamp))
        return config_id, run_id

    def test_a_fresh_dispatch_is_still_queued(self, client, make_project,
                                              tmp_path, monkeypatch):
        pid = make_project("dispatch-fresh")
        _activate(client, pid)
        config_id, _ = self._dispatch_dir(client, tmp_path, monkeypatch,
                                          pid, age_s=5)
        body = client.get(
            f"/test-execution/run-status/{config_id}").get_json()
        assert body["status"] == "queued"

    def test_an_old_dispatch_that_never_started_is_reported_failed(
            self, client, make_project, tmp_path, monkeypatch):
        pid = make_project("dispatch-never-started")
        _activate(client, pid)
        config_id, run_id = self._dispatch_dir(client, tmp_path, monkeypatch,
                                               pid, age_s=600)

        body = client.get(
            f"/test-execution/run-status/{config_id}").get_json()
        assert body["status"] == "failed", (
            "'queued' had no upper bound, so a worker that died at exec "
            "left the page spinning with no error and no Import button")
        assert "never started" in body["error"]

        assert _db.get_execution_run(run_id)["finished_at"] is not None, (
            "and it went on holding the one-browser-run slot")


class TestASimulatedVerdictSaysSo:
    """A run with no URL opens no browser. It still produces verdicts —
    that is what the deterministic simulator is for, and the pack is
    usually a demo — but a bug report is a claim about the product, and
    these were reaching the board indistinguishable from findings somebody
    had actually seen. One of them read "SQL metacharacters in search are
    not escaped", Major/High, against a site nothing had opened."""

    def _run(self, items):
        from engine.qa_testers import execute_items
        return execute_items(
            items=items, item_type="test_case", tester_id="mid_1",
            environment="Windows 11 / Chrome", testing_types=["Regression"],
            site_url="")

    def _items(self, n=25):
        return [{"id": f"TC_{i:03d}",
                 "summary": f"Verify behaviour number {i}",
                 "section": "Search", "preconditions": "The page is open",
                 "test_steps": "1. Do the thing",
                 "expected_result": "It works",
                 "priority": "High", "category": "Positive"}
                for i in range(1, n + 1)]

    def test_the_verdict_comment_carries_the_marker(self):
        out = self._run(self._items())
        simulated = [r for r in out["results"] if r["source"] == "simulated"]
        assert simulated, "this test is about the simulator; it did not run"
        for r in simulated:
            assert r["comment"].startswith("[simulated]"), (
                "the only record that nothing was executed was a 'sources' "
                "counter no surface rendered")

    def test_every_failed_item_still_carries_a_bug_id(self):
        """The contract the operator asked for stands — the fix is
        labelling, not withholding."""
        out = self._run(self._items())
        failing = [r for r in out["results"]
                   if r["status"] in ("Failed", "Blocked")]
        assert failing, "the simulator produced no failures to check"
        for r in failing:
            assert r["bug_id"], r

    def test_a_bug_born_of_a_simulation_is_labelled_as_one(self):
        out = self._run(self._items())
        bugs = out["bugs"]
        assert bugs, "the simulator produced no bugs to check"
        for bug in bugs:
            assert bug["title"].startswith("[simulated]"), (
                "the board cannot tell this from something a tester saw")
            assert "source:simulated" in bug["labels"], (
                "and nothing can filter them out")

    def test_a_real_check_is_not_labelled(self):
        """The marker has to mean something, so it must not be on
        everything."""
        from engine.qa_testers import execute_items
        out = execute_items(
            items=self._items(3), item_type="test_case", tester_id="mid_1",
            environment="Windows 11 / Chrome", testing_types=["Regression"],
            site_url="", manual_statuses={"TC_001": "Failed"})
        manual = [r for r in out["results"] if r["item_id"] == "TC_001"][0]
        assert manual["source"] == "manual"
        assert not manual["comment"].startswith("[simulated]")
        filed = [b for b in out["bugs"]
                 if not b["title"].startswith("[simulated]")]
        assert filed, "a hand-set verdict must still file an unlabelled bug"


class TestAManualWalkBugCanBeCited:
    """It was filed with ``external_id=NULL`` and the run id in ``extra``,
    so the card showed a blank ID and no run filter could reach it."""

    def test_it_gets_a_public_id_and_a_run(self, client, make_project):
        from routes.execution_manual import _file_bug
        from engine.manual_run import QueueItem

        pid = make_project("manual-bug-citable")
        run_id = _db.start_execution_run(
            pid, {"mode": "manual", "environment": "Windows 11 / Chrome",
                  "tester": "Olena"})
        item = QueueItem(
            kind="test_case", external_id="TC_001",
            summary="Verify the search returns results",
            section="Search", preconditions="The page is open",
            steps=["Open search", "Type a term"],
            expected_result="Results appear", priority="High")

        bug_row = _file_bug({"id": run_id, "project_id": pid,
                             "env_payload": {"environment": "Win/Chrome",
                                             "tester": "Olena"}},
                            item, "Failed", "The list stayed empty.")
        assert bug_row

        bugs = _db.list_bugs(pid)
        stored = [b for b in bugs if b.get("db_id") == bug_row
                  or b.get("id")][0]
        assert stored.get("id"), (
            "a bug with no public id renders a blank ID cell and cannot be "
            "cited in an export or a conversation")
        assert _db.count_bugs_by_run(pid).get(run_id), (
            "and the run filter on /bug-reports listed it under no run")


class TestAWatchedFailureIsAFailure:
    """``reconcile_with_automation`` calls a runner failure genuine only
    when the asset bucket carries a ``failure_step``, and the bucket used to
    record one only for a step whose failure **screenshot existed on disk as
    a non-empty file**. A picture is corroboration; it is not what makes a
    failure real, and it is the part most likely to be missing — the capture
    can fail, retention sweeps artefacts, and an ephemeral disk loses them
    between the worker and the import.

    The cannot-execute guard the 2026-07-15 investigation added is the
    reason the screenshot test was there, and it has to survive: a script
    that ran no step, or whose runner reported "blocked", is still Blocked
    with no bug.
    """

    def _assets(self, steps, status="failed"):
        from engine.runner_worker import _build_automation_assets
        return _build_automation_assets(
            {"scripts": [{"tc_id": "TC_001", "status": status,
                          "steps": steps}]}, storage_root="/nonexistent")

    def test_a_failed_step_with_no_screenshot_is_still_the_failure(self):
        assets = self._assets([{"index": 1, "action": "click",
                                "status": "failed",
                                "comment": "Timed out waiting for the panel"}])
        step = assets["TC_001"]["failure_step"]
        assert step, (
            "with no failure_step the verdict becomes 'Automation could "
            "not execute this item' and no bug is filed")
        assert step["comment"] == "Timed out waiting for the panel"
        assert step["screenshot"] == "", "there was none, and that is fine"

    def test_a_script_that_ran_no_step_is_still_cannot_execute(self):
        assert self._assets([])["TC_001"]["failure_step"] is None

    def test_a_script_whose_steps_all_passed_has_no_failure_step(self):
        assets = self._assets(
            [{"index": 1, "action": "goto", "status": "passed"},
             {"index": 2, "action": "click", "status": "passed"}],
            status="blocked")
        assert assets["TC_001"]["failure_step"] is None, (
            "a runner that reported blocked without a failed step is the "
            "cannot-execute case, and must not file a product defect")

    def test_the_first_failed_step_is_the_one_reported(self):
        assets = self._assets(
            [{"index": 1, "action": "goto", "status": "passed"},
             {"index": 2, "action": "click", "status": "failed",
              "comment": "first"},
             {"index": 3, "action": "fill", "status": "failed",
              "comment": "second"}])
        assert assets["TC_001"]["failure_step"]["comment"] == "first"
