"""One run must not report three different pass rates.

Found by walking Test Execution, 2026-08-31. A manual run of five cases
was judged Passed / Passed but / Failed / Blocked / Skipped, closed, and
then one verdict was corrected — which the page explicitly invites:

    Close the run to write its totals, or step back through any item to
    correct a verdict — a correction overwrites the old one rather than
    adding a second result.

Afterwards the same run answered:

    run page (stored snapshot)   50.0%
    dashboard                    33.3%
    its own verdicts, by its own rule   25.0%

Two independent causes.

**The snapshot is never refreshed.** ``manual_run_finish`` writes
``ExecutionRun.stats`` once. ``manual_run_verdict`` never looks at the
run's status, so a correction after closing updates the item and leaves
the totals frozen — and ``test_metrics_generator._aggregate_from_db_rows``
reads exactly those totals to compute the dashboard's execution pass
rate. The correction the product asked for never reaches the number
anyone looks at.

**"Passed but" is dropped by the aggregator.** ``engine.manual_run``
defines the rule and documents it: ``EXECUTED_VERDICTS`` covers Passed,
Passed but, Failed and Blocked, and ``pass_rate`` counts Passed plus
Passed but over those. The aggregator sums only ``passed``, ``failed``
and ``blocked``, so a "Passed but" verdict vanishes from the numerator
*and* the denominator. It is not a rounding difference: the verdict
stops existing.
"""
from __future__ import annotations

import uuid

import pytest

from engine import db as _db
from engine import manual_run as mr
from engine.test_metrics_generator import _aggregate_from_db_rows


# ── The aggregator against the rule it is supposed to share ──────

def _stats(passed=0, passed_but=0, failed=0, blocked=0, skipped=0) -> dict:
    total = passed + passed_but + failed + blocked + skipped
    counts = {"Passed": passed, "Passed but": passed_but,
              "Failed": failed, "Blocked": blocked, "Skipped": skipped}
    progress = mr.Progress(total=total, done=total, counts=counts)
    return mr.run_stats(progress)


class TestTheDashboardAgreesWithTheRunPage:
    def test_a_passed_but_verdict_counts_the_way_the_run_page_counts_it(self):
        stats = _stats(passed=0, passed_but=1, failed=2, blocked=1, skipped=1)
        # What the tester was shown while judging.
        assert stats["pass_rate"] == 25.0, stats
        metrics = _aggregate_from_db_rows(
            [], [], [{"stats": stats}], cls=[])
        assert metrics["exec_pass_rate"] == stats["pass_rate"], (
            f"the run page says {stats['pass_rate']}% and the dashboard "
            f"says {metrics['exec_pass_rate']}% about the same run")

    def test_a_passed_but_verdict_is_not_dropped_from_the_total(self):
        stats = _stats(passed=1, passed_but=1, failed=1, blocked=1)
        metrics = _aggregate_from_db_rows([], [], [{"stats": stats}], cls=[])
        assert metrics["exec_total"] == 4, (
            "an executed item disappeared from the execution total: "
            f"{metrics['exec_total']} of 4")

    def test_a_skip_is_still_neither_a_pass_nor_a_failure(self):
        # The other half of the rule, which must not break while fixing
        # the first: a skipped item was never looked at.
        stats = _stats(passed=1, failed=1, skipped=8)
        metrics = _aggregate_from_db_rows([], [], [{"stats": stats}], cls=[])
        assert metrics["exec_total"] == 2, metrics
        assert metrics["exec_pass_rate"] == 50.0, metrics

    def test_runs_without_passed_but_are_unchanged(self):
        # Every run recorded before "Passed but" existed has no such key.
        # The fix must not move their numbers.
        legacy = {"mode": "auto", "total": 4, "executed": 4,
                  "passed": 3, "failed": 1, "blocked": 0}
        metrics = _aggregate_from_db_rows([], [], [{"stats": legacy}], cls=[])
        assert metrics["exec_total"] == 4
        assert metrics["exec_pass_rate"] == 75.0


# ── A correction after closing has to reach the totals ───────────

VERDICTS = [("SC1_001", "Passed"), ("SC1_002", "Passed but"),
            ("SC1_003", "Failed"), ("SC1_004", "Blocked"),
            ("SC1_005", "Skipped")]

TC_ROWS = [
    {"id": ext, "section": "Walked", "section_num": 1,
     "summary": f"Verify that item {n} behaves", "preconditions": "",
     "test_steps": "1. Look at it", "test_data": "",
     "expected_result": "It behaves.", "category": "Positive",
     "priority": "High", "status": "Unchecked"}
    for n, (ext, _v) in enumerate(VERDICTS, start=1)
]



def _start_manual_run(client, pid: str) -> int:
    """Start a manual run and return its id.

    The id comes from the database rather than the redirect target: the
    POST lands on ``/test-execution/manual/start``, so parsing the last
    path segment reads the word "start" as a number.
    """
    before = {int(r["id"]) for r in (_db.list_execution_runs(pid) or [])}
    client.post("/test-execution", data={
        "run_mode": "manual", "source": "test_cases", "cred_mode": "none",
        "selected_items": [ext for ext, _v in VERDICTS],
    }, follow_redirects=True)
    after = {int(r["id"]) for r in (_db.list_execution_runs(pid) or [])}
    fresh = sorted(after - before)
    assert fresh, "starting a manual run created no run row"
    return fresh[-1]


@pytest.fixture()
def closed_run(client):
    """A finished manual run with every item judged, then closed."""
    client.post("/projects/db/create",
                data={"project_name": f"Run stats {uuid.uuid4().hex[:8]}"},
                follow_redirects=True)
    with client.session_transaction() as sess:
        pid = sess.get("project_id") or ""
    assert pid
    _db.save_test_cases(pid, TC_ROWS)

    run_id = _start_manual_run(client, pid)

    for ext, verdict in VERDICTS:
        client.post(f"/test-execution/manual/{run_id}/verdict",
                    data={"external_id": ext, "kind": "test_case",
                          "verdict": verdict, "notes": "walked"},
                    follow_redirects=True)
    client.post(f"/test-execution/manual/{run_id}/finish",
                follow_redirects=True)
    row = _db.get_execution_run(run_id)
    assert (row or {}).get("status") == "completed", row
    return pid, run_id


def _stored(run_id) -> dict:
    return (_db.get_execution_run(run_id) or {}).get("stats") or {}


class TestACorrectionAfterClosing:
    def test_the_stored_totals_follow_the_correction(self, client, closed_run):
        _pid, run_id = closed_run
        assert _stored(run_id).get("pass_rate") == 50.0, _stored(run_id)
        client.post(f"/test-execution/manual/{run_id}/verdict",
                    data={"external_id": "SC1_001", "kind": "test_case",
                          "verdict": "Failed",
                          "notes": "corrected after closing"},
                    follow_redirects=True)
        after = _stored(run_id)
        assert after.get("pass_rate") == 25.0, (
            "the run kept the totals it had before the correction: "
            f"{after}")
        assert after.get("passed") == 0, after
        assert after.get("failed") == 2, after

    def test_the_run_does_not_reopen_itself(self, client, closed_run):
        # Correcting a verdict is not resuming the run. A closed run that
        # silently reopens would drop out of every "finished runs" view.
        _pid, run_id = closed_run
        client.post(f"/test-execution/manual/{run_id}/verdict",
                    data={"external_id": "SC1_001", "kind": "test_case",
                          "verdict": "Failed", "notes": "corrected"},
                    follow_redirects=True)
        assert (_db.get_execution_run(run_id) or {}).get("status") \
            == "completed"

    def test_an_open_run_is_not_closed_by_a_verdict(self, client):
        # The mirror image, and the reason the fix cannot simply call
        # finish on every verdict: a run mid-walk must stay open.
        client.post("/projects/db/create",
                    data={"project_name": f"Mid walk {uuid.uuid4().hex[:8]}"},
                    follow_redirects=True)
        with client.session_transaction() as sess:
            pid = sess.get("project_id") or ""
        _db.save_test_cases(pid, TC_ROWS)
        run_id = _start_manual_run(client, pid)
        client.post(f"/test-execution/manual/{run_id}/verdict",
                    data={"external_id": "SC1_001", "kind": "test_case",
                          "verdict": "Passed", "notes": ""},
                    follow_redirects=True)
        status = (_db.get_execution_run(run_id) or {}).get("status")
        assert status not in ("completed", "partial"), (
            f"a single verdict closed a run that had four items left: "
            f"{status}")
