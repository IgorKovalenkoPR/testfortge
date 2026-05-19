"""
TestFortge — Test Metrics Generator (TestFort Template)

Computes real QA metrics using industry-standard formulas:
  1. Coverage Completion Table (per device/browser)
  2. Test Execution Summary by Platforms
  3. Issues Summary (by platform, type, status, severity)
  4. KPI calculations (pass rate, defect density, etc.)

Formulas (QA Best Practices):
  - Test Execution Rate = (Executed / Total) * 100
  - Pass Rate = (Passed / Executed) * 100
  - Fail Rate = (Failed / Executed) * 100
  - Defect Density = Total Bugs / Total Test Cases
  - Test Coverage = (Executed / Total Planned) * 100
  - Defect Removal Efficiency = (Bugs Found in QA / Total Known Bugs) * 100

Also exposes dashboard metric helpers used by the trend-history feature
(Sprint 3 task 3.3):
  - ``compute_session_metrics`` — pure variant of the old
    ``routes.dashboard._compute_dashboard_metrics``; reads pre-extracted
    lists rather than ``flask.session`` so it can be called from
    out-of-request code paths (workers, scheduled threads) without
    pulling Flask into engine modules.
  - ``_aggregate_from_db_rows`` — same shape, but built from raw DB
    rows returned by ``engine.db.load_test_cases / load_checklist /
    list_bugs / list_execution_runs``.
  - ``snapshot_metrics`` — in-request entry point that wraps
    ``compute_session_metrics`` and persists the result.
  - ``snapshot_metrics_from_db`` — out-of-request entry point for the
    detached ``runner_worker`` subprocess and the daily catch-up
    thread.
"""

from dataclasses import dataclass, field
import re


@dataclass
class CoverageRow:
    device: str
    overall_checks: int
    remaining: int
    percentage: float  # 0.0 - 1.0


@dataclass
class ExecutionRow:
    device: str
    passed: int
    failed: int
    passed_but: int
    blocked: int


@dataclass
class IssuesSummary:
    by_platform: list[dict] = field(default_factory=list)
    by_type: list[dict] = field(default_factory=list)
    by_status: list[dict] = field(default_factory=list)
    by_severity: list[dict] = field(default_factory=list)


@dataclass
class KPI:
    name: str
    value: str
    formula: str
    status: str = ""  # good, warning, bad


@dataclass
class TestMetrics:
    coverage: list[CoverageRow] = field(default_factory=list)
    execution: list[ExecutionRow] = field(default_factory=list)
    issues: IssuesSummary = field(default_factory=IssuesSummary)
    kpis: list[KPI] = field(default_factory=list)
    total_checks: int = 0
    total_test_cases: int = 0
    total_issues: int = 0


def _parse_extra_lines(lines: list[str]) -> dict:
    """Try to parse numeric data from uploaded content lines."""
    data = {}
    for line in lines:
        lower = line.lower()
        numbers = re.findall(r'\d+', line)
        if not numbers:
            continue
        num = int(numbers[0])
        if any(kw in lower for kw in ["pass", "passed"]):
            data.setdefault("tc_passed", num)
        elif any(kw in lower for kw in ["fail", "failed"]):
            data.setdefault("tc_failed", num)
        elif any(kw in lower for kw in ["block"]):
            data.setdefault("tc_blocked", num)
        elif any(kw in lower for kw in ["total test", "total tc", "test cases"]):
            data.setdefault("total_tc", num)
        elif any(kw in lower for kw in ["critical", "blocker"]):
            data.setdefault("bugs_critical", num)
        elif any(kw in lower for kw in ["major"]):
            data.setdefault("bugs_major", num)
        elif any(kw in lower for kw in ["minor"]):
            data.setdefault("bugs_minor", num)
        elif any(kw in lower for kw in ["bug", "defect", "issue"]):
            data.setdefault("total_bugs", num)
    return data


def generate_test_metrics(
    total_checks: int = 0,
    total_test_cases: int = 0,
    devices: list[str] = None,
    metrics_input: dict = None,
    custom_prompt: str = "",
) -> TestMetrics:
    """Generate test metrics using real input data and QA formulas."""

    if devices is None:
        devices = ["Device/Browser 1", "Device/Browser 2", "Device/Browser 3"]
    if metrics_input is None:
        metrics_input = {}

    # Parse extra data from uploaded files
    extra_lines = metrics_input.get("extra_lines", [])
    if extra_lines:
        parsed = _parse_extra_lines(extra_lines)
        # Merge parsed data (don't overwrite user-provided values)
        for k, v in parsed.items():
            if not metrics_input.get(k):
                metrics_input[k] = v

    # ── Core numbers ──
    tc_total = metrics_input.get("total_tc", total_test_cases) or total_test_cases
    tc_passed = metrics_input.get("tc_passed", 0)
    tc_failed = metrics_input.get("tc_failed", 0)
    tc_blocked = metrics_input.get("tc_blocked", 0)
    tc_passed_but = metrics_input.get("tc_passed_but", 0)
    tc_executed = tc_passed + tc_failed + tc_passed_but + tc_blocked
    tc_remaining = max(0, tc_total - tc_executed)

    total_bugs = metrics_input.get("total_bugs", tc_failed)
    bugs_critical = metrics_input.get("bugs_critical", 0)
    bugs_major = metrics_input.get("bugs_major", 0)
    bugs_minor = metrics_input.get("bugs_minor", 0)
    bugs_low = metrics_input.get("bugs_low", 0)
    if not any([bugs_critical, bugs_major, bugs_minor, bugs_low]) and total_bugs > 0:
        # Distribute bugs by typical ratio
        bugs_critical = max(1, total_bugs // 5)
        bugs_major = max(1, total_bugs // 3)
        bugs_minor = total_bugs - bugs_critical - bugs_major
        bugs_low = 0

    checks_total = total_checks or tc_total

    metrics = TestMetrics()
    metrics.total_checks = checks_total
    metrics.total_test_cases = tc_total
    metrics.total_issues = total_bugs

    # ── Coverage Completion (per device) ──
    for device in devices:
        pct = (tc_executed / checks_total) if checks_total > 0 else 0.0
        remaining = max(0, checks_total - tc_executed)
        metrics.coverage.append(CoverageRow(
            device=device,
            overall_checks=checks_total,
            remaining=remaining,
            percentage=min(pct, 1.0),
        ))

    # ── Test Execution Summary ──
    for i, device in enumerate(devices):
        if i == 0:
            metrics.execution.append(ExecutionRow(
                device=device,
                passed=tc_passed,
                failed=tc_failed,
                passed_but=tc_passed_but,
                blocked=tc_blocked,
            ))
        else:
            # Additional devices: template for manual filling
            metrics.execution.append(ExecutionRow(
                device=device, passed=0, failed=0, passed_but=0, blocked=0,
            ))

    # ── Issues Summary ──
    metrics.issues = IssuesSummary(
        by_platform=[
            {"type": "Web", "count": total_bugs},
            {"type": "Desktop", "count": 0},
            {"type": "Mobile", "count": 0},
            {"type": "All platforms", "count": total_bugs},
        ],
        by_type=[
            {"type": "Functional", "count": tc_failed},
            {"type": "UI", "count": bugs_minor},
            {"type": "UX", "count": 0},
            {"type": "Enhancement", "count": 0},
        ],
        by_status=[
            {"status": "Open", "count": tc_failed},
            {"status": "By design", "count": 0},
            {"status": "Reopen", "count": 0},
            {"status": "Fixed", "count": 0},
            {"status": "Need more info", "count": tc_blocked},
            {"status": "Closed", "count": tc_passed},
            {"status": "Duplicate", "count": 0},
        ],
        by_severity=[
            {"severity": "Blocker", "count": bugs_critical},
            {"severity": "Critical", "count": bugs_critical},
            {"severity": "Major", "count": bugs_major},
            {"severity": "Minor", "count": bugs_minor},
            {"severity": "Low", "count": bugs_low},
        ],
    )

    # ── KPI Calculations (QA Best Practice Formulas) ──
    kpis = []

    # 1. Test Execution Rate
    exec_rate = (tc_executed / tc_total * 100) if tc_total > 0 else 0
    kpis.append(KPI(
        name="Test Execution Rate",
        value=f"{exec_rate:.1f}%",
        formula=f"Executed / Total = {tc_executed} / {tc_total}",
        status="good" if exec_rate >= 80 else ("warning" if exec_rate >= 50 else "bad"),
    ))

    # 2. Pass Rate
    pass_rate = (tc_passed / tc_executed * 100) if tc_executed > 0 else 0
    kpis.append(KPI(
        name="Pass Rate",
        value=f"{pass_rate:.1f}%",
        formula=f"Passed / Executed = {tc_passed} / {tc_executed}",
        status="good" if pass_rate >= 90 else ("warning" if pass_rate >= 70 else "bad"),
    ))

    # 3. Fail Rate
    fail_rate = (tc_failed / tc_executed * 100) if tc_executed > 0 else 0
    kpis.append(KPI(
        name="Fail Rate",
        value=f"{fail_rate:.1f}%",
        formula=f"Failed / Executed = {tc_failed} / {tc_executed}",
        status="good" if fail_rate <= 5 else ("warning" if fail_rate <= 15 else "bad"),
    ))

    # 4. Defect Density
    defect_density = (total_bugs / tc_total) if tc_total > 0 else 0
    kpis.append(KPI(
        name="Defect Density",
        value=f"{defect_density:.2f}",
        formula=f"Total Bugs / Total TCs = {total_bugs} / {tc_total}",
        status="good" if defect_density <= 0.1 else ("warning" if defect_density <= 0.3 else "bad"),
    ))

    # 5. Test Coverage
    coverage = (tc_executed / tc_total * 100) if tc_total > 0 else 0
    kpis.append(KPI(
        name="Test Coverage",
        value=f"{coverage:.1f}%",
        formula=f"Executed / Planned = {tc_executed} / {tc_total}",
        status="good" if coverage >= 80 else ("warning" if coverage >= 50 else "bad"),
    ))

    # 6. Block Rate
    block_rate = (tc_blocked / tc_total * 100) if tc_total > 0 else 0
    kpis.append(KPI(
        name="Block Rate",
        value=f"{block_rate:.1f}%",
        formula=f"Blocked / Total = {tc_blocked} / {tc_total}",
        status="good" if block_rate <= 2 else ("warning" if block_rate <= 10 else "bad"),
    ))

    metrics.kpis = kpis
    return metrics


# ── Dashboard metrics (Sprint 3 task 3.3) ───────────────────────────


def compute_session_metrics(
    tc_data: list | None = None,
    cl_data: list | None = None,
    test_runs: list | None = None,
    bugs_data: list | None = None,
) -> dict:
    """Pure aggregator behind the landing-page KPI dashboard.

    Takes plain lists (already pulled out of ``flask.session`` or any
    other store) and produces the dict that the dashboard template
    consumes. Field shape is **load-bearing** — every key here is read
    from ``templates/index.html`` and from the trend-history JSON
    endpoint, and ``_aggregate_from_db_rows`` mirrors it exactly. If
    you add a field, update both call sites and add the parity check
    in ``tests/test_metrics_history.py``.
    """
    tc_data = tc_data or []
    cl_data = cl_data or []
    test_runs = test_runs or []
    bugs_data = bugs_data or []

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
        stats = run.get("stats", {}) or {}
        exec_passed += stats.get("passed", 0) or 0
        exec_failed += stats.get("failed", 0) or 0
        exec_blocked += stats.get("blocked", 0) or 0
    exec_total = exec_passed + exec_failed + exec_blocked
    exec_pass_rate = round(exec_passed / exec_total * 100, 1) if exec_total else 0

    # ── Bug severity distribution ─────────────────────────────
    bug_total = len(bugs_data)
    bug_by_severity: dict[str, int] = {}
    bug_by_priority: dict[str, int] = {}
    bug_by_status: dict[str, int] = {}
    for bug in bugs_data:
        sev = bug.get("severity", "Minor") or "Minor"
        bug_by_severity[sev] = bug_by_severity.get(sev, 0) + 1
        pri = bug.get("priority", "Medium") or "Medium"
        bug_by_priority[pri] = bug_by_priority.get(pri, 0) + 1
        st = bug.get("status", "Open") or "Open"
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


def _aggregate_from_db_rows(
    tcs: list | None,
    bugs: list | None,
    runs: list | None,
    cls: list | None = None,
) -> dict:
    """Build the dashboard metrics dict from raw DB rows.

    Mirrors ``compute_session_metrics`` exactly (same keys, same dtypes).
    The detached ``runner_worker`` subprocess and the daily catch-up
    thread call this — they have no ``flask.session`` to consult.

    ``ExecutionRun`` rows store status counts under ``stats`` (same
    layout as the in-session runs), so the aggregation logic is
    identical. ``BugReport`` rows use ``severity / priority / status``
    column names that already match the dataclass conventions, so they
    drop in unchanged.

    Stripping any fields here that aren't in ``compute_session_metrics``
    would break the trend chart's "data only contains keys both sides
    set" guarantee. Don't.
    """
    # The DB rows already use the same key names as the session dicts
    # (load_test_cases / load_checklist preserve dataclass field names,
    # list_bugs / list_execution_runs return _row_to_dict output which
    # uses column names). We only have to construct a synthetic
    # "test_runs"-shaped list that includes the ``stats`` payload and
    # an ``environment`` string the aggregator expects.
    tcs = tcs or []
    cls = cls or []
    bugs = bugs or []
    runs = runs or []

    synthetic_runs: list[dict] = []
    for r in runs:
        env_payload = r.get("env_payload") or {}
        # The session-flavoured run dicts carry a flat "environment"
        # string like "Windows/Chrome". DB rows store the structured
        # ``env_payload`` instead. Stitch the same shape so the
        # aggregator counts environments consistently.
        if isinstance(env_payload, dict):
            env_str = env_payload.get("environment") or env_payload.get("label") or ""
            if not env_str:
                platform = env_payload.get("platform", "")
                browser = env_payload.get("browser", "")
                if platform or browser:
                    env_str = f"{platform}/{browser}".strip("/")
        else:
            env_str = ""
        synthetic_runs.append({
            "stats": r.get("stats") or {},
            "environment": env_str,
        })

    return compute_session_metrics(
        tc_data=tcs,
        cl_data=cls,
        test_runs=synthetic_runs,
        bugs_data=bugs,
    )


def snapshot_metrics(project_id: str) -> int | None:
    """Persist a metric snapshot from the current request's session.

    Returns the row id of the persisted snapshot, or ``None`` when
    there's nothing to snapshot (no active project, empty dashboard).
    """
    if not project_id:
        return None
    try:
        from flask import session  # local import — keeps this module Flask-free at import time
    except Exception:
        return None
    metrics = compute_session_metrics(
        tc_data=session.get("test_cases_data", []),
        cl_data=session.get("checklist_data", []),
        test_runs=session.get("test_runs", []),
        bugs_data=session.get("bug_reports_data", []),
    )
    if not metrics.get("has_data"):
        return None
    from engine import db as _db
    return _db.save_metric_snapshot(project_id, metrics)


def snapshot_metrics_from_db(project_id: str) -> int | None:
    """Out-of-request snapshot — pulls everything from the DB.

    Used by ``runner_worker`` (detached subprocess, no Flask context)
    and the daily catch-up thread. Best-effort: returns ``None`` if
    there's no data yet, raises whatever the DB raises if persistence
    blows up (caller handles).
    """
    if not project_id:
        return None
    from engine import db as _db
    tcs = _db.load_test_cases(project_id)
    cls = _db.load_checklist(project_id)
    bugs = _db.list_bugs(project_id)
    runs = _db.list_execution_runs(project_id)
    metrics = _aggregate_from_db_rows(tcs, bugs, runs, cls=cls)
    if not metrics.get("has_data"):
        return None
    return _db.save_metric_snapshot(project_id, metrics)


__all__ = [
    # legacy template helpers
    "CoverageRow", "ExecutionRow", "IssuesSummary", "KPI", "TestMetrics",
    "generate_test_metrics",
    # dashboard / trend-history
    "compute_session_metrics", "_aggregate_from_db_rows",
    "snapshot_metrics", "snapshot_metrics_from_db",
]
