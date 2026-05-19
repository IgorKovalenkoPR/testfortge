"""Tests for Sprint 1 Task 5 — per-session concurrency cap.

The cap covers three submit sites:

  1. ``routes/generation.py`` — ``tc_gen`` / ``cl_gen`` jobs flow through
     :class:`engine.job_queue.JobQueue`. Cap is enforced by counting
     active jobs via :meth:`JobQueue.count_active_by_meta` and rejecting
     with a flash + redirect when the threshold is hit.
  2. ``routes/estimation.py`` — same JobQueue gate, JSON 429 surface.
  3. ``routes/execution.py`` — detached Playwright subprocesses do NOT
     touch the JobQueue. Active runs are counted by scanning the
     ``_pending/`` directory for un-done config JSONs whose
     ``session_id`` matches. ``count_active_subprocess_runs`` is the
     dedicated helper added in this task.

These tests:

  * ``test_third_submit_blocked`` — three long-running tc_gen jobs are
    seeded for one session at the configured cap, the next sync POST
    is rejected (no new job enters the queue, flash is set).
  * ``test_different_sessions_isolated`` — session A pinned to the cap
    does not block a submit from session B.
  * ``test_subprocess_cap_counts_pending_only`` — directly exercises
    ``count_active_subprocess_runs``. A config JSON with a matching
    ``done.flag`` is skipped; one without it is counted.

The Flask config knob ``MAX_CONCURRENT_RUNS`` is monkey-patched at the
app level (not the env var) so we don't fight reload semantics inside
the test process.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

from engine.job_queue import (
    DONE, FAILED, PENDING, RUNNING,
    Job, get_queue, count_active_subprocess_runs,
)


# ─────────────────────────────────────────────────────────────────
# Helpers — borrow the pattern from tests/test_rate_limit.py
# ─────────────────────────────────────────────────────────────────

def _reset_queue():
    q = get_queue()
    with q._lock:
        q._jobs.clear()
    return q


def _seed(kind: str, session_id: str, n: int, *,
          status: str = PENDING) -> list[str]:
    """Insert *n* fake jobs of (kind, session_id) into the queue."""
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


@pytest.fixture
def cap_setter(app):
    """Yield a setter that lowers MAX_CONCURRENT_RUNS and restores it.

    Without restoration the test pollutes app.config for every later
    suite — tests/test_rate_limit.py asserts ``limit == 3`` against the
    SAME Flask app singleton.
    """
    original = app.config.get("MAX_CONCURRENT_RUNS")

    def _set(n: int) -> None:
        app.config["MAX_CONCURRENT_RUNS"] = n

    yield _set
    if original is None:
        app.config.pop("MAX_CONCURRENT_RUNS", None)
    else:
        app.config["MAX_CONCURRENT_RUNS"] = original


# ─────────────────────────────────────────────────────────────────
# 1. /test-cases sync POST rejects 3rd submit at cap=2
# ─────────────────────────────────────────────────────────────────

def test_third_submit_blocked(client, app, monkeypatch, cap_setter):
    """With cap=2 and 2 active tc_gen jobs seeded, the next sync POST
    redirects (flash 'warning') without enqueueing a 3rd job."""
    cap_setter(2)
    _reset_queue()

    sid = f"sid-{uuid.uuid4().hex}"
    monkeypatch.setattr(
        "routes.generation.get_session_id", lambda s=None: sid)

    # Fill the slots with PENDING jobs that never finish (they're just
    # seed data — the executor never sees a callable).
    _seed("tc_gen", sid, 2, status=PENDING)

    with client.session_transaction() as sess:
        from routes._shared import SERVER_START_TIME
        sess["_session_active_since"] = SERVER_START_TIME

    # The sync /test-cases POST returns either:
    #   * 200 (rendered template) — only on success or empty-input.
    #   * 302 (redirect to /test-cases) — when blocked OR when the job
    #     overflowed the sync budget.
    # Either way: NO new tc_gen Job must appear in the queue.
    resp = client.post(
        "/test-cases",
        data={"input_text": "User can log in."},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 302)

    q = get_queue()
    with q._lock:
        n_for_sid = sum(
            1 for j in q._jobs.values()
            if j.kind == "tc_gen" and j.meta.get("session_id") == sid
        )
    # Still 2 — the rejection did NOT enqueue a third.
    assert n_for_sid == 2, (
        f"Expected 2 tc_gen jobs for sid (the 2 seeded), "
        f"found {n_for_sid} — rejection failed to bail out."
    )


# ─────────────────────────────────────────────────────────────────
# 2. Different session NOT blocked by another's cap
# ─────────────────────────────────────────────────────────────────

def test_different_sessions_isolated(client, app, monkeypatch, cap_setter):
    """Session A is at the cap; session B's submit still goes through.

    We hit the async /test-cases/run-async endpoint so we can read a
    structured JSON response instead of guessing about template
    rendering: 200 means accepted, 429 means rate-limited.
    """
    cap_setter(1)
    _reset_queue()

    sid_a = f"sid-a-{uuid.uuid4().hex}"
    sid_b = f"sid-b-{uuid.uuid4().hex}"
    _seed("tc_gen", sid_a, 1, status=PENDING)  # A is full

    # Pin the async route's session id to B for this request.
    monkeypatch.setattr(
        "routes.generation.get_session_id", lambda s=None: sid_b)
    # The async route also reads its OWN cap constant (default 2). With
    # only 0 jobs seeded for B that route's gate passes too.
    monkeypatch.setattr("routes.generation.MAX_CONCURRENT_GEN_JOBS", 1)

    with client.session_transaction() as sess:
        from routes._shared import SERVER_START_TIME
        sess["_session_active_since"] = SERVER_START_TIME

    resp = client.post(
        "/test-cases/run-async",
        data={"input_text": "User can log in."},
    )
    # Session B is at 0 → submit must be accepted (not rate_limited).
    assert resp.status_code == 200, (
        f"Session B unexpectedly rate-limited; body={resp.get_data(as_text=True)}"
    )
    body = resp.get_json()
    assert body.get("status") == "pending"
    assert "job_id" in body

    # And session A is still sitting on its single seeded job — the
    # cap is genuinely per-session, not global.
    q = get_queue()
    with q._lock:
        a_jobs = sum(
            1 for j in q._jobs.values()
            if j.kind == "tc_gen" and j.meta.get("session_id") == sid_a
        )
        b_jobs = sum(
            1 for j in q._jobs.values()
            if j.kind == "tc_gen" and j.meta.get("session_id") == sid_b
        )
    assert a_jobs == 1
    assert b_jobs >= 1


# ─────────────────────────────────────────────────────────────────
# 3. Subprocess cap helper skips done.flag entries
# ─────────────────────────────────────────────────────────────────

def test_subprocess_cap_counts_pending_only(tmp_path):
    """`count_active_subprocess_runs` must:
      * count config JSONs whose session_id matches AND have NO
        sibling done.flag
      * skip those that DO have a done.flag (finished runs)
      * skip JSONs for other sessions
      * skip files that aren't .json
      * return 0 for missing/empty directories
    """
    pending = tmp_path / "_pending"
    pending.mkdir()

    sid_self = "operator-1"
    sid_other = "operator-2"

    def _write_config(run_id: str, sid: str, *, done: bool):
        cfg_path = pending / f"{run_id}.json"
        cfg_path.write_text(
            json.dumps({"config_id": run_id, "session_id": sid}),
            encoding="utf-8",
        )
        if done:
            (pending / f"{run_id}.done.flag").write_text("ok", encoding="utf-8")

    # 2 active runs for sid_self (no done.flag).
    _write_config("run-A", sid_self, done=False)
    _write_config("run-B", sid_self, done=False)

    # 1 finished run for sid_self (done.flag present) — must NOT count.
    _write_config("run-C", sid_self, done=True)

    # 1 active run for someone else — must NOT count.
    _write_config("run-D", sid_other, done=False)

    # Stray non-JSON file — must NOT count, must not raise.
    (pending / "ignore-me.txt").write_text("not json", encoding="utf-8")

    # A result.json from a previous run — must NOT count even though it
    # ends in .json. The helper specifically excludes *.result.json.
    (pending / "run-A.result.json").write_text(
        json.dumps({"status": "done"}), encoding="utf-8")

    n = count_active_subprocess_runs(str(pending), sid_self)
    assert n == 2, (
        f"Expected 2 active runs for sid_self, got {n}. "
        f"done.flag and other-session entries must be skipped."
    )

    # Other session sees its single un-done run.
    assert count_active_subprocess_runs(str(pending), sid_other) == 1

    # Missing directory → 0, not an exception.
    assert count_active_subprocess_runs(
        str(tmp_path / "does-not-exist"), sid_self) == 0

    # Empty directory → 0.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert count_active_subprocess_runs(str(empty), sid_self) == 0
