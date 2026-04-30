"""TestFortge — Dashboard route.

Renders the landing page with saved-project list and live session
metrics (test cases, checklist, execution ratios, bug severity,
environments covered).
"""

from __future__ import annotations

from flask import Flask, render_template, session

from engine import db as _db

from ._shared import get_session_id


def _compute_dashboard_metrics() -> dict:
    """Compute test metrics from current session data for the dashboard.

    Aggregates:
      - Test case / checklist counts + category breakdown
      - Execution status ratios (Passed / Failed / Blocked)
      - Bug severity and priority distributions
      - Environments covered by test runs
    """
    tc_data = session.get("test_cases_data", [])
    cl_data = session.get("checklist_data", [])
    test_runs = session.get("test_runs", [])
    bugs_data = session.get("bug_reports_data", [])

    # ── Test cases breakdown ──────────────────────────────────
    tc_total = len(tc_data)
    tc_by_category: dict[str, int] = {}
    tc_by_priority: dict[str, int] = {}
    for tc in tc_data:
        cat = tc.get("category", "Other")
        tc_by_category[cat] = tc_by_category.get(cat, 0) + 1
        pri = tc.get("priority", "Medium")
        tc_by_priority[pri] = tc_by_priority.get(pri, 0) + 1

    # ── Checklist breakdown ───────────────────────────────────
    cl_total = len(cl_data)
    cl_by_category: dict[str, int] = {}
    cl_by_priority: dict[str, int] = {}
    for cl in cl_data:
        cat = cl.get("category", "Other")
        cl_by_category[cat] = cl_by_category.get(cat, 0) + 1
        pri = cl.get("priority", "Medium")
        cl_by_priority[pri] = cl_by_priority.get(pri, 0) + 1

    # ── Execution status ratios ───────────────────────────────
    exec_passed = exec_failed = exec_blocked = 0
    for run in test_runs:
        stats = run.get("stats", {})
        exec_passed += stats.get("passed", 0)
        exec_failed += stats.get("failed", 0)
        exec_blocked += stats.get("blocked", 0)
    exec_total = exec_passed + exec_failed + exec_blocked
    exec_pass_rate = round(exec_passed / exec_total * 100, 1) if exec_total else 0

    # ── Bug severity distribution ─────────────────────────────
    bug_total = len(bugs_data)
    bug_by_severity: dict[str, int] = {}
    bug_by_priority: dict[str, int] = {}
    bug_by_status: dict[str, int] = {}
    for bug in bugs_data:
        sev = bug.get("severity", "Minor")
        bug_by_severity[sev] = bug_by_severity.get(sev, 0) + 1
        pri = bug.get("priority", "Medium")
        bug_by_priority[pri] = bug_by_priority.get(pri, 0) + 1
        st = bug.get("status", "Open")
        bug_by_status[st] = bug_by_status.get(st, 0) + 1

    # ── Environments covered ──────────────────────────────────
    environments = []
    seen_envs: set[str] = set()
    for run in test_runs:
        env = run.get("environment", "")
        if env and env not in seen_envs:
            seen_envs.add(env)
            parts = [p.strip() for p in env.split("/")]
            environments.append({
                "full": env,
                "platform": parts[0] if len(parts) > 0 else "",
                "browser": parts[1] if len(parts) > 1 else "",
                "device": parts[2] if len(parts) > 2 else "",
                "screen": parts[3] if len(parts) > 3 else "",
                "runs": sum(1 for r in test_runs if r.get("environment") == env),
            })

    has_data = bool(tc_total or cl_total or test_runs or bugs_data)

    return {
        "has_data": has_data,
        "tc_total": tc_total,
        "tc_by_category": tc_by_category,
        "tc_by_priority": tc_by_priority,
        "cl_total": cl_total,
        "cl_by_category": cl_by_category,
        "cl_by_priority": cl_by_priority,
        "exec_total": exec_total,
        "exec_passed": exec_passed,
        "exec_failed": exec_failed,
        "exec_blocked": exec_blocked,
        "exec_pass_rate": exec_pass_rate,
        "runs_count": len(test_runs),
        "bug_total": bug_total,
        "bug_by_severity": bug_by_severity,
        "bug_by_priority": bug_by_priority,
        "bug_by_status": bug_by_status,
        "environments": environments,
    }


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
        # Scope the project list by session-id so different browser
        # sessions don't see each other's projects on the same DB.
        try:
            projects = _db.list_projects(owner_sid=get_session_id())
        except Exception:  # pragma: no cover — surface UI even with a sad DB
            projects = []
        # Persist a metric snapshot when the active project actually has
        # data — gives us a points-in-time series for trend charts later.
        _maybe_snapshot_metrics(session.get("project_id") or "", metrics)
        return render_template("index.html",
                               projects=projects,
                               active_project_id=session.get("project_id"),
                               metrics=metrics)


__all__ = ["register"]
