"""The whole Execute chain, driven end to end.

*"No test run starts at all, neither by test case nor by checklist. It
never even got as far as bug reports."*

Every test in this repository that touched the automated path stopped at a
boundary: the route tests faked ``Popen`` and asserted a row appeared; the
worker tests ran the worker and asserted a file appeared; the results tests
handed the endpoint a payload somebody had written by hand. Nothing joined
them, so the four defects that lived *between* them — the run ids dropped
from ``config_echo``, the row nobody closed, the cwd the worker could not
import itself from, the gate that refused the runs it was not about — were
each invisible to the suite while being individually fatal in the product.

So this drives the real sequence, with the subprocess boundary replaced by
an in-process call to the same ``runner_worker.main()`` the spawn would
have reached:

    POST /test-execution  →  worker  →  run closed  →
    GET /test-execution/results/<id>  →  verdicts  →  /bug-reports

and asserts at each joint that the next one can see what the last one
wrote.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import db as _db                       # noqa: E402
from engine import runner_worker                   # noqa: E402
from routes._shared import SERVER_START_TIME, tc_to_dict   # noqa: E402
from engine.testcase_generator import ChecklistItem, TestCase  # noqa: E402


def _cases(n=6):
    return [tc_to_dict(TestCase(
        id=f"TC_{i:03d}", section="Contact", section_num=1,
        summary=f"Verify the contact form accepts input number {i}",
        preconditions="The page is open",
        test_steps="1. Open the page\n2. Submit the form",
        test_data="", expected_result="The form submits",
        priority="High", category="Positive")) for i in range(1, n + 1)]


class _FailedStep:
    """A step Playwright watched fail, with no screenshot.

    Deliberately without one. A capture can fail (``live_executor`` logs
    that at WARNING because it happens), retention can sweep the artefacts,
    and on an ephemeral disk they can be gone between the worker and the
    import. None of that makes the failure less real, and all of it used to
    turn the verdict into "Automation could not execute this item —
    recorded as Blocked (not a product defect)" with no bug filed.
    """

    index = 1
    action = "click"
    raw = "Submit the form"
    status = "failed"
    duration_ms = 400
    comment = "Timed out waiting for the confirmation panel"
    screenshot_before = ""
    screenshot_after = ""
    screenshot_failure = ""
    console_errors: list = []


class _Script:
    def __init__(self, tc_id, status, steps=()):
        self.tc_id = tc_id
        self.summary = f"Verify the contact form accepts input {tc_id[-1]}"
        self.status = status
        self.duration_ms = 700
        self.video_path = ""
        self.final_url = "https://example.com/contact"
        self.comment = ("Timed out waiting for the confirmation panel"
                        if status == "failed" else "")
        self.steps = list(steps)


class _FakeReport:
    """What a runner hands back. One failure, so the chain has a bug to
    carry all the way to the board."""

    run_id = "e2e"
    started_at = ""
    finished_at = ""
    base_url = "https://example.com/"
    headless = True
    total = 2
    passed = 1
    failed = 1
    blocked = 0
    duration_ms = 1200

    def __init__(self):
        self.scripts = [_Script("TC_001", "failed", [_FailedStep()]),
                        _Script("TC_002", "passed")]


@pytest.fixture
def run_worker_inline(monkeypatch):
    """Replace the detached spawn with a direct call to the same entry
    point, and record the config it was given.

    The spawn itself is covered by ``tests/test_runner_worker.py``; what
    has never been covered is everything downstream of it, because a
    detached subprocess cannot be waited on from a route test.
    """
    import engine.live_executor as le
    import engine.automation_runner as ar
    seen: dict = {}

    class _FakeExecutor:
        findings: list = []
        tc_bindings: list = []
        early_exit_reason = ""

        def __init__(self, *a, **kw):
            pass

        def run(self, start_urls=None):
            return _FakeReport()

        def dedupe_findings(self):
            return []

    class _FakeRunner:
        def __init__(self, *a, **kw):
            pass

        def run(self, scripts):
            return _FakeReport()

    monkeypatch.setattr(le, "LiveExecutor", _FakeExecutor)
    monkeypatch.setattr(ar, "AutomationRunner", _FakeRunner)

    class _InlinePopen:
        pid = 4242

        def __init__(self, argv, **kwargs):
            config_path = argv[-1]
            seen["config_path"] = config_path
            seen["cwd"] = kwargs.get("cwd")
            with open(config_path, encoding="utf-8") as fh:
                seen["config"] = json.load(fh)
            argv_backup = sys.argv
            sys.argv = ["runner_worker", config_path]
            try:
                seen["exit_code"] = runner_worker.main()
            finally:
                sys.argv = argv_backup

    monkeypatch.setattr(subprocess, "Popen", _InlinePopen)
    return seen


@pytest.fixture
def project(client, make_project, request):
    # Named per test: ``upsert_project`` keys on the name, so a shared one
    # accumulates every test's runs and the counts stop meaning anything.
    pid = make_project(f"execute-end-to-end-{request.node.name}")
    _db.save_test_cases(pid, _cases())
    with client.session_transaction() as sess:
        sess["_session_active_since"] = SERVER_START_TIME
        sess["project_id"] = pid
        sess.pop("test_cases_data", None)
        sess.pop("checklist_data", None)
    return pid


FORM = {
    "run_mode": "tc_driven",
    "source": "test_cases",
    "env_type": "web",
    "base_url": "https://example.com/",
    "selected_items": ["TC_001", "TC_002"],
}


class TestTheChainHolds:

    def test_a_dispatch_reaches_the_worker_with_a_usable_cwd(
            self, client, project, run_worker_inline):
        resp = client.post("/test-execution", data=FORM,
                           follow_redirects=True)
        assert resp.status_code == 200
        assert run_worker_inline.get("config_path"), (
            "the POST never reached Popen — the run was refused before "
            "dispatch")

        cwd = run_worker_inline["cwd"]
        assert os.path.isdir(os.path.join(cwd, "engine")), (
            f"the worker would be spawned in {cwd!r}, where "
            f"'python -m engine.runner_worker' cannot resolve the package")

    def test_the_dispatcher_hands_the_worker_its_run_ids(
            self, client, project, run_worker_inline):
        client.post("/test-execution", data=FORM, follow_redirects=True)
        ids = run_worker_inline["config"].get("db_run_ids") or {}
        assert ids, "the dispatch opened no row for the worker to close"
        rows = _db.list_execution_runs(project)
        assert len(rows) == len(ids)

    def test_the_run_is_closed_without_anybody_opening_a_page(
            self, client, project, run_worker_inline):
        """The defect the operator was blocked by. Nothing here visits
        the results page."""
        client.post("/test-execution", data=FORM, follow_redirects=True)
        rows = _db.list_execution_runs(project)
        assert rows and all(r["finished_at"] for r in rows), (
            "a finished run still counts against the one-browser-run cap, "
            "which is 'A browser run is already in progress for this team' "
            "for a run that ended")

    def test_the_next_run_is_therefore_admitted(
            self, client, project, run_worker_inline, monkeypatch):
        monkeypatch.setenv("TESTFORTGE_MAX_CONCURRENT_RUNS", "1")
        first = client.post("/test-execution", data=FORM,
                            follow_redirects=True)
        assert "already in progress" not in first.get_data(as_text=True)
        second = client.post("/test-execution", data=FORM,
                             follow_redirects=True)
        assert "already in progress" not in second.get_data(as_text=True), (
            "each completed run locked the team out for thirty minutes, "
            "and the lock re-armed on the next attempt")

    def test_the_import_adopts_the_row_rather_than_opening_a_second(
            self, client, project, run_worker_inline):
        client.post("/test-execution", data=FORM, follow_redirects=True)
        before = len(_db.list_execution_runs(project))
        config_id = os.path.basename(
            run_worker_inline["config_path"]).removesuffix(".json")

        resp = client.get(f"/test-execution/results/{config_id}",
                          follow_redirects=True)
        assert resp.status_code == 200
        assert len(_db.list_execution_runs(project)) == before, (
            "the import opened its own row because it could not see the "
            "dispatcher's — two rows per run in the register")

    def test_a_watched_failure_reaches_the_bug_board(
            self, client, project, run_worker_inline):
        """*"It never even got as far as bug reports."*

        The last joint in the chain, and the one that stayed broken after
        the run itself was fixed. ``reconcile_with_automation`` calls a
        runner failure genuine only when the asset bucket carries a
        ``failure_step``, and the bucket recorded one only for a step whose
        failure **screenshot existed on disk as a non-empty file**. So a
        step Playwright had just watched fail, without a surviving picture
        of it, was written down as *"Automation could not execute this
        item — recorded as Blocked (not a product defect)"*, the
        simulator's bug for it was dropped, and the board stayed empty
        after a run that had found something.
        """
        client.post("/test-execution", data=FORM, follow_redirects=True)
        config_id = os.path.basename(
            run_worker_inline["config_path"]).removesuffix(".json")
        client.get(f"/test-execution/results/{config_id}",
                   follow_redirects=True)

        run = _db.list_execution_runs(project)[0]
        stats = run.get("stats") or {}
        assert stats.get("failed") == 1, (
            f"the watched failure was not recorded as one: {stats}")
        assert not stats.get("cannot_execute"), (
            "it was reclassified as a cannot-execute condition, which "
            "never files a bug")

        bugs = _db.list_bugs(project)
        assert len(bugs) == 1, f"expected one bug, got {len(bugs)}"
        bug = bugs[0]
        assert bug.get("external_id"), "the bug has no citable id"
        assert bug.get("run_id") == run["id"], (
            "the bug is not linked to the run, so the run filter on "
            "/bug-reports cannot reach it")
        assert "Timed out waiting for the confirmation panel" in str(
            bug.get("actual_result") or ""), (
            "the bug does not carry what the browser actually observed")

        body = client.get("/bug-reports").get_data(as_text=True)
        assert bug["external_id"] in body

    def test_the_register_can_reach_the_results(
            self, client, project, run_worker_inline):
        client.post("/test-execution", data=FORM, follow_redirects=True)
        config_id = os.path.basename(
            run_worker_inline["config_path"]).removesuffix(".json")

        body = client.get("/test-execution/runs").get_data(as_text=True)
        assert f"/test-execution/results/{config_id}" in body, (
            "the only route to a finished automated run was the "
            "dispatching tab's own auto-redirect")


class TestTheChecklistTakesTheSamePath:

    def test_a_checklist_run_with_no_url_is_admitted_and_recorded(
            self, client, make_project):
        """The simulator path. It opens no browser, so the browser-run cap
        has nothing to say about it — and it used to refuse it anyway."""
        from routes._shared import cl_to_dict

        pid = make_project("execute-e2e-checklist")
        _db.save_checklist(pid, [cl_to_dict(ChecklistItem(
            id="HDR_001", section="Header", objective="Verify the logo",
            item_num="1.1", priority="High", category="Positive"))])
        with client.session_transaction() as sess:
            sess["_session_active_since"] = SERVER_START_TIME
            sess["project_id"] = pid
            sess.pop("checklist_data", None)
            sess.pop("test_cases_data", None)

        blocker = _db.start_execution_run(pid, {"mode": "walkthrough"})
        resp = client.post("/test-execution",
                           data={"run_mode": "tc_driven",
                                 "source": "checklist",
                                 "env_type": "web",
                                 "selected_items": ["HDR_001"]},
                           follow_redirects=True)
        assert resp.status_code == 200
        assert "already in progress" not in resp.get_data(as_text=True)

        runs = [r for r in _db.list_execution_runs(pid) if r["id"] != blocker]
        assert runs, "a checklist run left no row in the register"
        assert runs[0]["finished_at"], (
            "the simulator path runs inside the request, so it has no "
            "excuse for leaving the row open")
