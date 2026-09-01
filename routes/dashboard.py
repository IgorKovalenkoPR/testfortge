"""TestFortge — Dashboard route.

Renders the landing page with saved-project list and live session
metrics (test cases, checklist, execution ratios, bug severity,
environments covered).

Also exposes the ``/metrics/history`` JSON endpoint that powers the
trend chart on ``/test-metrics`` — see Sprint 3 task 3.3.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import (Flask, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)

from engine import db as _db
from engine import permissions as _perm
from engine.log import get_logger
from engine.test_metrics_generator import compute_session_metrics

log = get_logger(__name__)

from ._shared import (get_session_id, kpi_value, kpi_defect_density,
                      pack_bugs, pack_checklist, pack_runs,
                      pack_test_cases, visible_projects)


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
        # Per-project, not per-browser. Computing these from the
        # caller's session is what made "project metrics" mean
        # "metrics of whoever is looking" — empty after a restart and
        # different in a second tab.
        tc_data=pack_test_cases(),
        cl_data=pack_checklist(),
        test_runs=pack_runs(),
        bugs_data=pack_bugs(),
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


# E7.5 — snapshots are no longer taken here.
#
# They used to be written opportunistically on dashboard load, throttled by a
# module-level dict of "last time per project". Three things were wrong with
# that, and only the third is obvious:
#
#   * the throttle is per *process*. gunicorn runs several workers, so "once
#     per hour" was once per hour per worker, and on the free plan a restart
#     reset it — the trend series was denser at busy times and absent at
#     quiet ones, which is exactly backwards for a trend;
#   * a project nobody opened was never sampled at all, so the series had
#     holes precisely where a team stopped looking;
#   * it made rendering a page a write.
#
# ``app.py`` already runs a daily snapshot worker
# (``TESTFORTGE_SNAPSHOT_WORKER``, on by default) that walks every project.
# That is the one writer now: an even sample, independent of who was looking.


def _owner() -> str:
    """Who this dashboard's layout belongs to (E7.2).

    A signed-in user id when there is one, the session id otherwise — the
    same two-era treatment the project list gets, so a preference survives
    turning ``AUTH_ENABLED`` on.
    """
    try:
        from engine import permissions as _perm
        user_id = _perm.current_user_id()
        if user_id:
            return str(user_id)
    except Exception:      # pragma: no cover — auth off or unavailable
        pass
    return get_session_id(session)


def register(app: Flask) -> None:
    @app.route("/")
    def index():
        from engine import dashboard_config as _cfg
        from engine import dashboard_metrics as _dm

        from engine import features as _features

        project_id = session.get("project_id") or ""
        filters = _dm.Filters.from_request(request.args)
        # Behind DASHBOARD_V2, like every other epic in this programme — the
        # flag was declared in E0.3 for exactly this and read nowhere until
        # now. ``features`` already makes it conditional on
        # WORKSPACE_DB_FIRST: aggregating in SQL means the database is the
        # source of truth, so it cannot come on before that does.
        #
        # Off, the page is byte-for-byte what shipped before: the session
        # aggregator over the repository's packs.
        v2 = bool(_features.effective("DASHBOARD_V2"))
        # E7.1: counted by the database. The old path loaded every case,
        # item, bug and run into Python to produce eighteen integers.
        metrics = (_dm.aggregate(project_id, filters) if v2 and project_id
                   else _compute_dashboard_metrics())
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
        owner = _owner()
        return render_template(
            "index.html",
            projects=projects,
            active_project_id=project_id,
            metrics=metrics,
            automation=_latest_automation(project_id),
            # E7.4 / E7.3 / E7.2
            dash_v2=v2,
            dash_filters=filters,
            dash_options=_dm.options(project_id) if filters.active or project_id
            else _dm.Options(),
            dash_periods=list(_dm.PERIODS),
            dash_kpis=_cfg.evaluate(metrics, _cfg.targets(project_id)),
            dash_targets=_cfg.targets(project_id),
            dash_widgets=_cfg.widgets(owner),
            dash_widget_labels=_cfg.WIDGET_LABELS,
            dash_all_widgets=list(_cfg.DEFAULT_WIDGETS),
        )

    @app.route("/dashboard/layout", methods=["POST"])
    def dashboard_layout():
        """Save which widgets this person sees, and in what order (E7.2)."""
        from engine import dashboard_config as _cfg
        _cfg.set_widgets(_owner(), request.form.getlist("widgets"))
        flash(g.t.get("dash_layout_saved", "Dashboard layout saved."),
              "success")
        return redirect(url_for("index"))

    @app.route("/dashboard/targets", methods=["POST"])
    @_perm.require_role("admin")
    def dashboard_targets():
        """Set this project's KPI targets (E7.3).

        Admin-only on purpose: a target is a team agreement, and a dashboard
        where two people see different colours for the same number is worse
        than one with no colours at all.
        """
        from engine import dashboard_config as _cfg
        project_id = session.get("project_id") or ""
        if not project_id:
            flash(g.t.get("dash_no_project",
                          "Pick a project before setting targets."), "error")
            return redirect(url_for("index"))
        values = {kpi.key: request.form.get(f"target_{kpi.key}")
                  for kpi in _cfg.KPIS}
        try:
            _cfg.set_targets(project_id, values)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        flash(g.t.get("dash_targets_saved", "KPI targets saved."), "success")
        return redirect(url_for("index"))

    @app.route("/dashboard/export.csv", methods=["GET"])
    def dashboard_export_csv():
        """The numbers on screen, as a file (E7.6).

        Exports what the current filter shows, not the whole project — a
        download that silently ignores the filter the user set is a download
        they will paste into a report as if it matched.
        """
        import csv
        import io

        from flask import Response

        from engine import dashboard_config as _cfg
        from engine import dashboard_metrics as _dm

        from engine import features as _features

        project_id = session.get("project_id") or ""
        filters = _dm.Filters.from_request(request.args)
        v2 = bool(_features.effective("DASHBOARD_V2"))
        metrics = (_dm.aggregate(project_id, filters) if v2 and project_id
                   else _compute_dashboard_metrics())
        targets = _cfg.targets(project_id)

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["metric", "value", "target", "status"])
        for row in _cfg.evaluate(metrics, targets):
            writer.writerow([row["label"], row["value"],
                             "" if row["target"] is None else row["target"],
                             row["status"]])
        writer.writerow([])
        for label, mapping in (("Test cases by category",
                                metrics.get("tc_by_category")),
                               ("Test cases by priority",
                                metrics.get("tc_by_priority")),
                               ("Checklist by category",
                                metrics.get("cl_by_category")),
                               ("Bugs by severity",
                                metrics.get("bug_by_severity")),
                               ("Bugs by status",
                                metrics.get("bug_by_status"))):
            writer.writerow([label])
            # Sorted by the key *as a string*. The aggregators normalise
            # their buckets — ``engine.dashboard_metrics._counts`` folds
            # ``None`` and ``""`` into "Unspecified", and the session
            # aggregator now does the same — but this is the line that
            # turns a stray one into a 500 rather than a slightly odd row,
            # and an export that cannot be produced is worse than an export
            # with an "Other" in it.
            for key, count in sorted((mapping or {}).items(),
                                     key=lambda kv: str(kv[0])):
                writer.writerow(["", key, count])
            writer.writerow([])
        if filters.active:
            writer.writerow(["Filtered by",
                             ", ".join(f"{k}={v}" for k, v
                                       in filters.as_query().items())])

        return Response(
            buffer.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition":
                     "attachment; filename=dashboard-metrics.csv"})

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
