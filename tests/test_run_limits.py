"""
Fair use for browser runs (E5.5).

The acceptance criterion is the shape of the failure, not the limit itself:
exceeding it must produce a comprehensible refusal rather than a 500 — or,
worse, what actually happened before, which was two Chromiums on half a
gigabyte and both of them OOM-killed. A killed run stops with no verdict and
no explanation, so the tester's evidence is "the page just stopped".

Two behaviours here are easy to get wrong and expensive when wrong:

* a **stale** open run must stop counting, because an OOM kill leaves
  ``finished_at`` NULL — nothing gets the chance to write it — and counting
  it forever would wedge the project with SQL as the only recovery;
* the **manual walk must never be limited**. It is a person reading a page.
  Limiting it would be a bug that looks like a policy.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from engine import db as _db
from engine import run_limits
from engine.testcase_generator import ChecklistItem, TestCase
from routes._shared import SERVER_START_TIME, cl_to_dict, tc_to_dict


def _now():
    return datetime.now(timezone.utc)


def _run(run_id=1, mode="tc_driven", *, minutes_ago=0):
    return {"id": run_id,
            "started_at": (_now() - timedelta(minutes=minutes_ago)).isoformat(),
            "env_payload": {"mode": mode}}


class TestConfiguration:
    def test_the_default_is_one(self, monkeypatch):
        monkeypatch.delenv("TESTFORTGE_MAX_CONCURRENT_RUNS", raising=False)
        assert run_limits.max_concurrent() == 1

    def test_it_is_read_at_call_time(self, monkeypatch):
        # So a deployment can raise it without a code change, and so a test
        # can set it per case instead of reloading the module.
        monkeypatch.setenv("TESTFORTGE_MAX_CONCURRENT_RUNS", "3")
        assert run_limits.max_concurrent() == 3

    def test_nonsense_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("TESTFORTGE_MAX_CONCURRENT_RUNS", "lots")
        assert run_limits.max_concurrent() == 1

    def test_zero_means_no_limit_rather_than_no_runs(self, monkeypatch):
        # Nobody configures "no runs at all"; they might well mean the other
        # thing, and a service that refuses every run reads as broken.
        monkeypatch.setenv("TESTFORTGE_MAX_CONCURRENT_RUNS", "0")
        assert run_limits.max_concurrent() > 1000


class TestStaleRunsStopCounting:
    def test_a_fresh_run_counts(self, monkeypatch):
        monkeypatch.delenv("TESTFORTGE_RUN_STALE_MINUTES", raising=False)
        counted, stale = run_limits.split_by_age([_run(minutes_ago=2)])
        assert len(counted) == 1 and not stale

    def test_an_old_run_does_not(self, monkeypatch):
        monkeypatch.delenv("TESTFORTGE_RUN_STALE_MINUTES", raising=False)
        counted, stale = run_limits.split_by_age([_run(minutes_ago=120)])
        assert not counted and len(stale) == 1

    def test_the_window_is_configurable(self, monkeypatch):
        monkeypatch.setenv("TESTFORTGE_RUN_STALE_MINUTES", "5")
        counted, stale = run_limits.split_by_age([_run(minutes_ago=10)])
        assert not counted and len(stale) == 1

    def test_a_run_with_no_timestamp_counts(self):
        # A missing started_at is not evidence of age. Treating it as stale
        # would let one broken write disable the limit.
        counted, stale = run_limits.split_by_age([{"id": 1, "env_payload": {}}])
        assert len(counted) == 1 and not stale

    def test_a_naive_timestamp_is_read_as_utc(self):
        stamp = datetime.now(timezone.utc) - timedelta(minutes=1)
        naive = stamp.replace(tzinfo=None).isoformat()
        counted, _ = run_limits.split_by_age(
            [{"id": 1, "started_at": naive, "env_payload": {}}])
        assert len(counted) == 1

    def test_an_unparseable_timestamp_counts(self):
        counted, stale = run_limits.split_by_age(
            [{"id": 1, "started_at": "yesterday-ish", "env_payload": {}}])
        assert len(counted) == 1 and not stale


class TestTheRefusalExplainsItself:
    def test_it_names_the_run_holding_the_slot(self):
        d = run_limits.Decision(allowed=False, limit=1,
                                active=[_run(run_id=42, minutes_ago=3)])
        message = d.message()
        assert "#42" in message
        assert "tc_driven" in message

    def test_it_says_what_to_do(self):
        d = run_limits.Decision(allowed=False, limit=1, active=[_run()])
        assert "Runs" in d.message() or "Wait" in d.message()

    def test_it_says_why_the_limit_exists(self):
        # "Try again later" without a reason is indistinguishable from a bug.
        assert "memory" in run_limits.Decision(
            allowed=False, limit=1, active=[_run()]).message()

    def test_it_mentions_ignored_stale_runs(self):
        # Otherwise somebody wonders why an old run is not blocking, and the
        # other reading is that the limit is broken.
        d = run_limits.Decision(allowed=False, limit=1, active=[_run()],
                                stale=[_run(run_id=7, minutes_ago=999)])
        assert "stale" in d.message()

    def test_an_allowed_decision_says_nothing(self):
        assert run_limits.Decision(allowed=True).message() == ""

    def test_a_higher_limit_reads_differently(self):
        d = run_limits.Decision(allowed=False, limit=3,
                                active=[_run(1), _run(2), _run(3)])
        assert "3 browser runs" in d.message()
        assert "the limit is 3" in d.message()


class TestWhatCountsAsABrowserRun:
    def test_a_manual_walk_does_not(self, request, monkeypatch):
        monkeypatch.delenv("TESTFORTGE_MAX_CONCURRENT_RUNS", raising=False)
        pid = _db.upsert_project(f"limit-manual-{request.node.name}")
        _db.start_execution_run(pid, {"mode": "manual", "manual_queue": []})
        _db.start_execution_run(pid, {"mode": "manual", "manual_queue": []})
        assert run_limits.check([pid]).allowed

    @pytest.mark.parametrize("mode", ["tc_driven", "walkthrough", "live"])
    def test_every_browser_mode_does(self, request, monkeypatch, mode):
        monkeypatch.delenv("TESTFORTGE_MAX_CONCURRENT_RUNS", raising=False)
        pid = _db.upsert_project(f"limit-{mode}-{request.node.name}")
        _db.start_execution_run(pid, {"mode": mode})
        assert not run_limits.check([pid]).allowed

    def test_a_run_with_no_mode_counts(self, request, monkeypatch):
        # Predates the field. Under-counting risks the OOM this exists to
        # prevent; over-counting costs a wait.
        monkeypatch.delenv("TESTFORTGE_MAX_CONCURRENT_RUNS", raising=False)
        pid = _db.upsert_project(f"limit-nomode-{request.node.name}")
        _db.start_execution_run(pid, {})
        assert not run_limits.check([pid]).allowed

    def test_a_finished_run_does_not(self, request, monkeypatch):
        monkeypatch.delenv("TESTFORTGE_MAX_CONCURRENT_RUNS", raising=False)
        pid = _db.upsert_project(f"limit-done-{request.node.name}")
        run_id = _db.start_execution_run(pid, {"mode": "tc_driven"})
        _db.finish_execution_run(run_id, status="completed")
        assert run_limits.check([pid]).allowed


class TestTheScopeIsWiderThanOneProject:
    def test_a_run_in_a_sibling_project_counts(self, request, monkeypatch):
        # Otherwise the limit is bypassable by switching project, and the
        # bypass is an OOM rather than an error message.
        monkeypatch.delenv("TESTFORTGE_MAX_CONCURRENT_RUNS", raising=False)
        a = _db.upsert_project(f"limit-a-{request.node.name}")
        b = _db.upsert_project(f"limit-b-{request.node.name}")
        _db.start_execution_run(a, {"mode": "tc_driven"})
        assert not run_limits.check([a, b]).allowed

    def test_no_projects_means_no_limit_to_apply(self):
        assert run_limits.check([]).allowed

    def test_the_limit_counts_across_the_whole_scope(self, request,
                                                    monkeypatch):
        monkeypatch.setenv("TESTFORTGE_MAX_CONCURRENT_RUNS", "2")
        a = _db.upsert_project(f"limit-c-{request.node.name}")
        b = _db.upsert_project(f"limit-d-{request.node.name}")
        _db.start_execution_run(a, {"mode": "tc_driven"})
        assert run_limits.check([a, b]).allowed
        _db.start_execution_run(b, {"mode": "live"})
        assert not run_limits.check([a, b]).allowed


class TestTheRouteRefusesPolitely:
    """The acceptance criterion: a comprehensible queue, not a 500."""

    @pytest.fixture
    def project(self, client, request, fresh_org, make_project):
        # fresh_org, because the limit's scope IS the organisation: sharing
        # one with the rest of the suite means an open run left behind by
        # another file counts here, and the test fails for something that
        # happened elsewhere.
        #
        # make_project rather than upsert_project for the same reason in
        # reverse: a project outside the caller's organisation is correctly
        # invisible to the check, and the test would measure nothing.
        pid = make_project(f"limit-route-{request.node.name}",
                           **({"org_id": fresh_org} if fresh_org else {}))
        _db.save_test_cases(pid, [tc_to_dict(TestCase(
            id="TC_001", section="S", section_num=1, summary="Verify it",
            preconditions="", test_steps="1. do it", test_data="",
            expected_result="e", priority="High", category="Positive"))])
        _db.save_checklist(pid, [cl_to_dict(ChecklistItem(
            id="HDR_001", section="Header", objective="Verify the logo",
            item_num="1.1", priority="High", category="Positive"))])
        with client.session_transaction() as sess:
            sess["_session_active_since"] = SERVER_START_TIME
            sess["project_id"] = pid
            sess["project_setup"] = {"project_name": "limit"}
            sess.pop("test_cases_data", None)
            sess.pop("checklist_data", None)
        return pid

    def test_a_second_browser_run_is_refused_with_an_explanation(
            self, client, project, monkeypatch):
        monkeypatch.setenv("TESTFORTGE_MAX_CONCURRENT_RUNS", "1")
        blocker = _db.start_execution_run(project, {"mode": "tc_driven"})
        resp = client.post("/test-execution",
                           data={"run_mode": "tc_driven",
                                 "selected_items": ["TC_001"]},
                           follow_redirects=True)
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # ``#id (mode)`` is how run_limits.py writes it. The bare
        # ``f"#{blocker}"`` also matches `&#39;` and a CSS colour, so it
        # could pass without the message naming this run at all.
        assert f"#{blocker} (tc_driven)" in body
        assert "already in progress" in body

    def test_the_manual_walk_is_not_refused(self, client, project,
                                            monkeypatch):
        monkeypatch.setenv("TESTFORTGE_MAX_CONCURRENT_RUNS", "1")
        _db.start_execution_run(project, {"mode": "tc_driven"})
        resp = client.post("/test-execution", data={"run_mode": "manual"},
                           follow_redirects=False)
        # 307 to the manual surface: the limit is above that line on
        # purpose, because a person reading a page costs nothing.
        assert resp.status_code == 307

    def test_a_stale_run_does_not_block_the_route(self, client, project,
                                                 monkeypatch):
        monkeypatch.setenv("TESTFORTGE_MAX_CONCURRENT_RUNS", "1")
        monkeypatch.setenv("TESTFORTGE_RUN_STALE_MINUTES", "1")
        run_id = _db.start_execution_run(project, {"mode": "tc_driven"})
        # Age it past the window, the way an OOM kill would leave it.
        with _db.session_scope() as sess:
            row = sess.get(_db.ExecutionRun, run_id)
            row.started_at = datetime.now(timezone.utc) - timedelta(hours=3)
        resp = client.post("/test-execution",
                           data={"run_mode": "tc_driven",
                                 "selected_items": ["TC_001"]},
                           follow_redirects=True)
        assert "already in progress" not in resp.get_data(as_text=True)
