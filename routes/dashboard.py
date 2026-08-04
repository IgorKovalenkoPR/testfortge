"""TestFortge — Dashboard route.

Renders the landing page with saved-project list and live session
metrics (test cases, checklist, execution ratios, bug severity,
environments covered).

Also exposes the ``/metrics/history`` JSON endpoint that powers the
trend chart on ``/test-metrics`` — see Sprint 3 task 3.3.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, render_template, request, session

from engine import db as _db
from engine.log import get_logger
from engine.test_metrics_generator import compute_session_metrics

log = get_logger(__name__)

from ._shared import (get_session_id, kpi_value, kpi_defect_density,
                      visible_projects)


def _compute_dashboard_metrics() -> dict:
    """Backwards-compatible wrapper — delegates to the pure aggregator.

    The bulk of the logic lives in
    ``engine.test_metrics_generator.compute_session_metrics`` so the
    detached ``runner_worker`` subprocess (which has no Flask
    ``session``) can reuse it via ``snapshot_metrics_from_db``. This
    wrapper stays here so existing template renderers and the
    opportunistic snapshot trigger below keep working untouched.
    """
    return compute_session_metrics(
        tc_data=session.get("test_cases_data", []),
        cl_data=session.get("checklist_data", []),
        test_runs=session.get("test_runs", []),
        bugs_data=session.get("bug_reports_data", []),
    )


def _latest_automation(project_id: str) -> dict | None:
    """The most recent ingested Allure run, for the Dashboard card.

    Kept out of ``compute_session_metrics`` deliberately: that function is
    pure over session data so the detached runner_worker can reuse it, and
    an automation run lives only in the DB.
    """
    if not project_id:
        return None
    try:
        return _db.latest_automation_run(project_id)
    except Exception as exc:  # pragma: no cover — never break the dashboard
        log.debug("dashboard: automation lookup failed: %s", exc)
        return None


# Module-local cache so we don't pound DB every dashboard load.
_LAST_SNAPSHOT_AT: dict[str, float] = {}
_SNAPSHOT_THROTTLE_SEC = 3600  # one snapshot per project per hour


def _maybe_snapshot_metrics(project_id: str, metrics: dict) -> None:
    """Write a dashboard_metric_snapshot at most once per project per hour.

    Skipped silently when there's no active project or when no artefact
    data has been generated yet (the dashboard is empty)."""
    import time as _time
    if not project_id or not metrics or not metrics.get("has_data"):
        return
    last = _LAST_SNAPSHOT_AT.get(project_id, 0.0)
    if _time.time() - last < _SNAPSHOT_THROTTLE_SEC:
        return
    try:
        _db.save_metric_snapshot(project_id, metrics)
        _LAST_SNAPSHOT_AT[project_id] = _time.time()
    except Exception as exc:  # pragma: no cover — best-effort
        from engine.log import get_logger
        get_logger(__name__).warning("metric snapshot failed: %s", exc)


def register(app: Flask) -> None:
    @app.route("/")
    def index():
        metrics = _compute_dashboard_metrics()
        # Scoped by organisation when ORG_MODE is on, and by session-id
        # otherwise — the same two eras the access gate honours. Sharing one
        # helper with the project picker is the point: the two lists
        # disagreeing about what exists is how a user ends up looking at a
        # project the sidebar says is not there.
        try:
            projects = visible_projects(session)
        except Exception:  # pragma: no cover — surface UI even with a sad DB
            projects = []
        # Persist a metric snapshot when the active project actually has
        # data — gives us a points-in-time series for trend charts later.
        _maybe_snapshot_metrics(session.get("project_id") or "", metrics)
        return render_template("index.html",
                               projects=projects,
                               active_project_id=session.get("project_id"),
                               metrics=metrics,
                               automation=_latest_automation(
                                   session.get("project_id") or ""))

    @app.route("/metrics/history", methods=["GET"])
    def metrics_history_route():
        """Return a rolling window of ``DashboardMetricSnapshot`` rows.

        Query params:
          - ``project_id`` — defaults to ``session["project_id"]``.
            When neither is set we return an empty list rather than 4xx,
            because the trend chart's empty-state path is the
            "anonymous visitor lands on /test-metrics" UX (no friction).
          - ``days`` — clamped to [1, 365]. Default 30.

        Response shape::

            {
              "snapshots": [
                {"ts": "2026-05-19T12:00:00+00:00",
                 "pass_rate": 0.92, "defect_density": 0.03,
                 "tc_total": 120, "bug_total": 8, "exec_total": 35},
                ...
              ]
            }

        Order: ascending by ``ts`` so the chart can feed the array
        straight into uPlot without re-sorting on the client.
        """
        pid = (request.args.get("project_id")
               or session.get("project_id") or "").strip()
        if not pid:
            return jsonify({"snapshots": []})
        # Clamp ``days`` to [1, 365]. A "?days=0" request defaults to
        # 1 day — the chart never receives a zero-width window.
        try:
            days_raw = int(request.args.get("days", 30))
        except (TypeError, ValueError):
            days_raw = 30
        days = max(1, min(days_raw, 365)) if days_raw > 0 else 1
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        # Pull a generous limit — 4× ``days`` covers up to ~4
        # snapshots/day comfortably without ever truncating the visible
        # window. The route filters by ``captured_at >= cutoff`` below.
        try:
            rows = _db.list_metric_snapshots(pid, limit=max(days * 4, 30))
        except Exception:  # pragma: no cover — keep the page alive on DB hiccups
            rows = []
        cutoff_iso = cutoff.isoformat()
        out: list[dict] = []
        for r in rows:
            ts = r.get("captured_at") or ""
            if ts and ts < cutoff_iso:
                continue
            m = r.get("metrics") or {}
            out.append({
                "ts": ts,
                "pass_rate": kpi_value(m, "exec_pass_rate"),
                "defect_density": kpi_defect_density(m),
                "tc_total": int(kpi_value(m, "tc_total")),
                "bug_total": int(kpi_value(m, "bug_total")),
                "exec_total": int(kpi_value(m, "exec_total")),
            })
        # list_metric_snapshots returns desc; the chart needs ascending.
        out.reverse()
        return jsonify({"snapshots": out})


__all__ = ["register"]
