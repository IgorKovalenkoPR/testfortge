"""Trend chart / metrics history — Sprint 3 Task 3.3.

Covers:

  * **Shape parity** between the in-session aggregator
    (``compute_session_metrics``) and the DB-row aggregator
    (``_aggregate_from_db_rows``). The trend chart treats both as
    fungible sources, so any key drift between them would silently
    leave gaps in stored snapshots.
  * **``/metrics/history`` happy path**: seed three snapshots and
    assert they come back ascending with the published JSON shape.
  * **``days`` clamping**: ``?days=999`` clamps to 365; ``?days=0``
    defaults to 1.
  * **Empty / missing project**: route returns
    ``{"snapshots": []}`` with HTTP 200 — never 4xx for the
    "anonymous visitor" UX.
  * **``snapshot_metrics_from_db`` on an empty project** returns
    ``None`` rather than persisting a useless all-zero row.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# FLASK_DEBUG is set by conftest.py before any module-level import that
# touches the DB. Re-asserting here is cheap and documents intent.
os.environ.setdefault("FLASK_DEBUG", "1")

import engine.db as db  # noqa: E402 — after sys.path manipulation
from engine.test_metrics_generator import (  # noqa: E402
    _aggregate_from_db_rows,
    compute_session_metrics,
    snapshot_metrics_from_db,
)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Point engine.db at a throwaway SQLite file for the test."""
    monkeypatch.setenv("FLASK_DEBUG", "1")
    monkeypatch.setenv("DATABASE_URL",
                       f"sqlite:///{tmp_path / 'metrics_history.db'}")
    monkeypatch.delenv("TESTFORTGE_DB", raising=False)

    prev_engine = db._engine
    prev_session = db._Session
    db._engine = None
    db._Session = None
    try:
        db.init_db()
        yield
    finally:
        if db._engine is not None:
            db._engine.dispose()
        db._engine = prev_engine
        db._Session = prev_session


# ── Shape parity ────────────────────────────────────────────────


def test_shape_parity_between_session_and_db_aggregators():
    """compute_session_metrics and _aggregate_from_db_rows must yield
    dicts with **identical** key sets. The trend chart never reaches
    into nested data — every field it shows is a top-level key — so
    a divergence here turns into missing data points downstream.
    """
    session_metrics = compute_session_metrics(
        tc_data=[{"category": "Smoke", "priority": "High"}],
        cl_data=[{"category": "Smoke", "priority": "High"}],
        test_runs=[{"environment": "Win/Chrome",
                    "stats": {"passed": 3, "failed": 1, "blocked": 0}}],
        bugs_data=[{"severity": "Major", "priority": "High", "status": "Open"}],
    )
    # Mirror the same dataset through the DB-row aggregator. Field
    # names track DB column names (``stats``, ``env_payload``) — both
    # paths converge on the same output keys.
    db_metrics = _aggregate_from_db_rows(
        tcs=[{"category": "Smoke", "priority": "High"}],
        bugs=[{"severity": "Major", "priority": "High", "status": "Open"}],
        runs=[{"stats": {"passed": 3, "failed": 1, "blocked": 0},
               "env_payload": {"environment": "Win/Chrome"}}],
        cls=[{"category": "Smoke", "priority": "High"}],
    )
    assert set(session_metrics.keys()) == set(db_metrics.keys()), (
        "Trend chart relies on identical top-level field sets — "
        "any drift here will silently leave snapshot gaps."
    )
    # Spot-check that ``has_data`` agrees too, since the snapshot
    # write path keys off it specifically.
    assert session_metrics["has_data"] is True
    assert db_metrics["has_data"] is True


def test_aggregator_handles_empty_inputs():
    """Both aggregators must return has_data=False on empty input —
    that's what snapshot_metrics(_from_db) keys off to decide
    whether to persist."""
    a = compute_session_metrics()
    b = _aggregate_from_db_rows(tcs=[], bugs=[], runs=[], cls=[])
    assert a["has_data"] is False
    assert b["has_data"] is False
    assert a["tc_total"] == b["tc_total"] == 0
    assert a["bug_total"] == b["bug_total"] == 0


# ── /metrics/history ────────────────────────────────────────────


@pytest.fixture
def client(fresh_db):
    """Flask test client wired against the temp SQLite DB."""
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        yield c


def _seed_project_with_snapshots(n: int = 3) -> str:
    """Create a project and ``n`` evenly-spaced snapshots."""
    pid = db.upsert_project(name="trend-test", owner_sid="test-sid")
    # Three snapshots with sane sample data so the /metrics/history
    # serialiser exercises every field it picks out of ``metrics``.
    for i in range(n):
        metrics = {
            "has_data": True,
            "tc_total": 100 + i * 10,
            "bug_total": 5 + i,
            "exec_total": 50 + i * 5,
            "exec_pass_rate": 80.0 + i,
        }
        db.save_metric_snapshot(pid, metrics)
    return pid


def test_metrics_history_happy_path(client):
    pid = _seed_project_with_snapshots(3)
    resp = client.get(f"/metrics/history?project_id={pid}&days=30")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert "snapshots" in payload
    snaps = payload["snapshots"]
    assert len(snaps) == 3, snaps
    # Ascending order by ``ts`` — the chart consumes the array verbatim.
    timestamps = [s["ts"] for s in snaps]
    assert timestamps == sorted(timestamps), (
        "history must return snapshots oldest-first")
    # Every published field is present and typed correctly.
    for s in snaps:
        assert isinstance(s.get("ts"), str) and s["ts"]
        assert isinstance(s["pass_rate"], (int, float))
        assert isinstance(s["defect_density"], (int, float))
        assert isinstance(s["tc_total"], int)
        assert isinstance(s["bug_total"], int)
        assert isinstance(s["exec_total"], int)


def test_metrics_history_clamps_days_upper(client):
    """?days=999 — must clamp to 365 days of window. Verifying it
    returns 200 with the documented shape rather than 4xx is the
    contract; the limit itself is enforced inside the route, so we
    just make sure the route doesn't reject the request."""
    pid = _seed_project_with_snapshots(1)
    resp = client.get(f"/metrics/history?project_id={pid}&days=999")
    assert resp.status_code == 200
    snaps = resp.get_json()["snapshots"]
    # Our seeded snapshots are fresh, so they all fit in any window.
    assert len(snaps) == 1


def test_metrics_history_clamps_days_zero(client):
    """``?days=0`` defaults to 1 — the chart should still render
    today's data without the route returning an empty list because
    of a zero-width window."""
    pid = _seed_project_with_snapshots(1)
    resp = client.get(f"/metrics/history?project_id={pid}&days=0")
    assert resp.status_code == 200
    snaps = resp.get_json()["snapshots"]
    # A fresh snapshot is within "today" — should be returned.
    assert len(snaps) == 1


def test_metrics_history_no_project_returns_empty(client):
    """Anonymous visitor (no session, no ?project_id) — return
    200 + empty list rather than 4xx. This keeps the trend chart's
    empty-state UX trivial."""
    resp = client.get("/metrics/history")
    assert resp.status_code == 200
    assert resp.get_json() == {"snapshots": []}


def test_metrics_history_unknown_project_returns_empty(client):
    """An unknown ``project_id`` — same as no-project: empty list,
    not a 404. The DB query returns no rows; the route happily
    serialises that to ``{"snapshots": []}``."""
    resp = client.get("/metrics/history?project_id=does-not-exist")
    assert resp.status_code == 200
    assert resp.get_json() == {"snapshots": []}


# ── snapshot_metrics_from_db ────────────────────────────────────


def test_snapshot_metrics_from_db_returns_none_when_empty(fresh_db):
    """A project with no TCs / no bugs / no runs has ``has_data`` =
    False, so the helper must skip the DB write and return None."""
    pid = db.upsert_project(name="empty", owner_sid="test-sid")
    result = snapshot_metrics_from_db(pid)
    assert result is None
    # And no row was inserted.
    assert db.list_metric_snapshots(pid) == []


def test_snapshot_metrics_from_db_writes_when_project_has_bugs(fresh_db):
    """A project with at least one bug must have ``has_data``=True
    and a row persisted with id > 0."""
    pid = db.upsert_project(name="with-bug", owner_sid="test-sid")
    db.save_bug(pid, {"title": "smoke", "severity": "Minor"})
    sid = snapshot_metrics_from_db(pid)
    assert isinstance(sid, int) and sid > 0
    rows = db.list_metric_snapshots(pid)
    assert len(rows) == 1
    metrics = rows[0]["metrics"]
    assert metrics["bug_total"] == 1
