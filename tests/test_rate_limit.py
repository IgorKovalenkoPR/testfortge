"""Tests for per-session concurrency rate-limiting on async endpoints.

The rate limit is a concurrency cap (N *simultaneously active* jobs per
session), not a leaky bucket — finished jobs don't count, different
sessions don't share the limit. These tests hit three contracts:

  1. ``JobQueue.count_active_by_meta`` correctly counts pending/running
     jobs filtered by an arbitrary meta key, and ignores finished ones.
  2. ``/automation/run-async`` returns 429 + Retry-After when the caller
     already has MAX_CONCURRENT_JOBS_PER_SESSION jobs in flight, and the
     rejected submit does not touch the queue.
  3. ``/estimation/run-async`` behaves the same way and is isolated from
     automation's count (separate kind → separate limit).

We avoid waiting on real work by pre-seeding the queue dict directly
with PENDING Job objects — that way the test never spins up Playwright
or a site crawl. The singleton queue is cleared of any cross-test
residue at the start of each test via ``_jobs.clear()``.
"""

from __future__ import annotations

import uuid

import pytest

from engine.job_queue import (
    DONE, FAILED, PENDING, RUNNING,
    Job, get_queue,
)


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _reset_queue():
    """Wipe the singleton queue's job dict.

    The queue is a module-level singleton shared across the whole test
    session, so individual tests must own their slice of state. We only
    clear the dict — the executor stays alive so `get_queue()` keeps
    returning the same object.
    """
    q = get_queue()
    with q._lock:
        q._jobs.clear()
    return q


def _seed(kind: str, session_id: str, n: int, *, status: str = PENDING) -> list[str]:
    """Insert *n* fake jobs for (kind, session_id) into the queue."""
    q = get_queue()
    ids: list[str] = []
    with q._lock:
        for _ in range(n):
            j = Job(
                id=uuid.uuid4().hex,
                kind=kind,
                status=status,
                meta={"session_id": session_id},
            )
            q._jobs[j.id] = j
            ids.append(j.id)
    return ids


# ─────────────────────────────────────────────────────────────────
# Unit: count_active_by_meta
# ─────────────────────────────────────────────────────────────────

class TestCountActiveByMeta:
    def test_counts_only_matching_kind_and_meta(self):
        _reset_queue()
        _seed("automation", "sid-A", 2, status=PENDING)
        _seed("automation", "sid-B", 5, status=PENDING)
        _seed("estimation", "sid-A", 3, status=PENDING)
        q = get_queue()
        assert q.count_active_by_meta("automation", "session_id", "sid-A") == 2
        assert q.count_active_by_meta("automation", "session_id", "sid-B") == 5
        assert q.count_active_by_meta("estimation", "session_id", "sid-A") == 3
        assert q.count_active_by_meta("automation", "session_id", "ghost") == 0

    def test_excludes_finished_jobs(self):
        _reset_queue()
        _seed("automation", "sid-X", 2, status=PENDING)
        _seed("automation", "sid-X", 1, status=RUNNING)
        _seed("automation", "sid-X", 7, status=DONE)    # should be ignored
        _seed("automation", "sid-X", 4, status=FAILED)  # should be ignored
        assert (get_queue().count_active_by_meta(
            "automation", "session_id", "sid-X") == 3)


# ─────────────────────────────────────────────────────────────────
# HTTP: /automation/run-async rate limit
# ─────────────────────────────────────────────────────────────────

_DUMMY_TC = [{
    "id": "TC-1",
    "summary": "verify that x",
    "steps": ["step 1"],
    "expected_result": "ok",
}]


class TestAutomationRateLimit:
    def _pin_sid(self, monkeypatch, sid: str):
        """Make get_session_id deterministic inside the route handler."""
        monkeypatch.setattr(
            "routes.automation.get_session_id", lambda s=None: sid)

    def test_429_when_at_limit(self, client, monkeypatch):
        _reset_queue()
        sid = f"sid-{uuid.uuid4().hex}"
        self._pin_sid(monkeypatch, sid)
        _seed("automation", sid, 3)  # default limit is 3

        with client.session_transaction() as sess:
            # The before_request handler wipes sessions whose _session_active_since
            # doesn't match SERVER_START_TIME — set it so our seed survives.
            from routes._shared import SERVER_START_TIME
            sess["_session_active_since"] = SERVER_START_TIME
            sess["test_cases_data"] = _DUMMY_TC

        resp = client.post("/automation/run-async",
                           data={"base_url": "https://example.com"})
        assert resp.status_code == 429
        payload = resp.get_json()
        assert payload["error"] == "rate_limited"
        assert payload["active"] == 3
        assert payload["limit"] == 3
        assert resp.headers.get("Retry-After") == "30"

        # Critical: rejection must NOT have enqueued the new job.
        q = get_queue()
        with q._lock:
            assert sum(1 for j in q._jobs.values()
                       if j.kind == "automation"
                       and j.meta.get("session_id") == sid) == 3

    def test_below_limit_still_accepted(self, client, monkeypatch):
        """With 2 active jobs (< 3) the next submit goes through."""
        _reset_queue()
        sid = f"sid-{uuid.uuid4().hex}"
        self._pin_sid(monkeypatch, sid)
        _seed("automation", sid, 2)

        # Stub the AutomationRunner so we don't touch Playwright.
        class _FakeRunner:
            def __init__(self, *a, **kw): pass
            def run(self, scripts):
                from engine.automation_report import AutomationReport
                return AutomationReport(results=[], passed=0, failed=0,
                                        blocked=0, total=0)

        monkeypatch.setattr("routes.automation.AutomationRunner", _FakeRunner)

        with client.session_transaction() as sess:
            # The before_request handler wipes sessions whose _session_active_since
            # doesn't match SERVER_START_TIME — set it so our seed survives.
            from routes._shared import SERVER_START_TIME
            sess["_session_active_since"] = SERVER_START_TIME
            sess["test_cases_data"] = _DUMMY_TC

        resp = client.post("/automation/run-async",
                           data={"base_url": "https://example.com"})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        payload = resp.get_json()
        assert "job_id" in payload
        assert payload["status"] == "pending"

    def test_different_sessions_do_not_share_limit(self, client, monkeypatch):
        """Seed 3 jobs for session A. Session B can still submit."""
        _reset_queue()
        sid_a = f"sid-a-{uuid.uuid4().hex}"
        sid_b = f"sid-b-{uuid.uuid4().hex}"
        _seed("automation", sid_a, 3)  # A is full

        # Stub runner (session B's submit will go through).
        class _FakeRunner:
            def __init__(self, *a, **kw): pass
            def run(self, scripts):
                from engine.automation_report import AutomationReport
                return AutomationReport(results=[], passed=0, failed=0,
                                        blocked=0, total=0)

        monkeypatch.setattr("routes.automation.AutomationRunner", _FakeRunner)
        monkeypatch.setattr(
            "routes.automation.get_session_id", lambda s=None: sid_b)

        with client.session_transaction() as sess:
            # The before_request handler wipes sessions whose _session_active_since
            # doesn't match SERVER_START_TIME — set it so our seed survives.
            from routes._shared import SERVER_START_TIME
            sess["_session_active_since"] = SERVER_START_TIME
            sess["test_cases_data"] = _DUMMY_TC

        resp = client.post("/automation/run-async",
                           data={"base_url": "https://example.com"})
        assert resp.status_code == 200, resp.get_data(as_text=True)

    def test_finished_jobs_do_not_count(self, client, monkeypatch):
        """Seed 5 DONE jobs for the session. New submit is accepted."""
        _reset_queue()
        sid = f"sid-{uuid.uuid4().hex}"
        self._pin_sid(monkeypatch, sid)
        _seed("automation", sid, 5, status=DONE)

        class _FakeRunner:
            def __init__(self, *a, **kw): pass
            def run(self, scripts):
                from engine.automation_report import AutomationReport
                return AutomationReport(results=[], passed=0, failed=0,
                                        blocked=0, total=0)

        monkeypatch.setattr("routes.automation.AutomationRunner", _FakeRunner)

        with client.session_transaction() as sess:
            # The before_request handler wipes sessions whose _session_active_since
            # doesn't match SERVER_START_TIME — set it so our seed survives.
            from routes._shared import SERVER_START_TIME
            sess["_session_active_since"] = SERVER_START_TIME
            sess["test_cases_data"] = _DUMMY_TC

        resp = client.post("/automation/run-async",
                           data={"base_url": "https://example.com"})
        assert resp.status_code == 200, resp.get_data(as_text=True)


# ─────────────────────────────────────────────────────────────────
# HTTP: /estimation/run-async rate limit
# ─────────────────────────────────────────────────────────────────

class TestEstimationRateLimit:
    def test_429_when_at_limit(self, client, monkeypatch):
        _reset_queue()
        sid = f"sid-{uuid.uuid4().hex}"
        monkeypatch.setattr(
            "routes.estimation.get_session_id", lambda s=None: sid)
        _seed("estimation", sid, 3)

        resp = client.post("/estimation/run-async",
                           data={"url": "https://example.com"})
        assert resp.status_code == 429
        payload = resp.get_json()
        assert payload["error"] == "rate_limited"
        assert payload["active"] == 3
        assert payload["limit"] == 3
        assert resp.headers.get("Retry-After") == "15"

    def test_kind_isolation_automation_does_not_block_estimation(
            self, client, monkeypatch):
        """3 active automation jobs must NOT rate-limit estimation."""
        _reset_queue()
        sid = f"sid-{uuid.uuid4().hex}"
        _seed("automation", sid, 3)  # fills automation slot
        monkeypatch.setattr(
            "routes.estimation.get_session_id", lambda s=None: sid)

        # Stub crawl_site so we don't hit the network.
        class _FakeAnalysis:
            features = []
            pages = []
        monkeypatch.setattr(
            "routes.estimation.crawl_site", lambda u, **kw: _FakeAnalysis())
        monkeypatch.setattr(
            "routes.estimation.features_from_site_analysis",
            lambda a: [{"name": "stub"}])
        # compute_estimation returns something asdict-able — easiest is
        # to let the worker raise inside the thread; the endpoint has
        # already returned 200 with the job_id by then.

        resp = client.post("/estimation/run-async",
                           data={"url": "https://example.com"})
        assert resp.status_code == 200, resp.get_data(as_text=True)
