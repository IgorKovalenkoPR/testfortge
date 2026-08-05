"""TestFortge — the dashboard's numbers, counted by the database (E7.1).

Requirement 10 asks for metrics a team can work with per project. E3 already
made them per-project rather than per-browser; what was still true is *how*
they were produced: every test case, checklist item, bug and run was loaded
into Python on every dashboard load, and counted there.

That is fine for one person's demo project and wrong for a team's: a project
with 3,000 cases and 900 bugs moves all of it over the wire to compute
eighteen integers, on the free tier, on every page view. This module asks the
database to count instead — ``COUNT(*) … GROUP BY`` — and returns the same
eighteen keys.

The contract is the shape, not the mechanism
--------------------------------------------
``engine.test_metrics_generator.compute_session_metrics`` defines the dict the
template and the trend endpoint read, and its docstring says the field shape is
load-bearing. So this module is checked against it key by key in the tests
rather than trusted to agree. When there is no project — the anonymous,
pre-project flow — the old path still answers, because there is nothing in the
database to count.

Filters (E7.4)
--------------
A dashboard nobody can narrow is a dashboard that answers one question. The
filters are period, run, tester, suite and environment. Runs are filtered in
Python and case results in SQL, on purpose: tester and environment live inside
``execution_run.env_payload`` (a JSON column), and JSON predicates differ
between SQLite and Postgres in exactly the way that produces a query working in
tests and failing in production. A project has tens of runs, not millions, so
reading them and filtering in Python costs nothing and behaves identically on
both backends.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from engine.log import get_logger

log = get_logger(__name__)

#: Periods the UI offers, in days. ``None`` means everything.
PERIODS: dict[str, int | None] = {
    "7d": 7, "30d": 30, "90d": 90, "all": None,
}
DEFAULT_PERIOD = "all"


@dataclass
class Filters:
    """What the dashboard is currently narrowed to.

    Every field empty means "everything", which is what a fresh visit shows.
    """
    period: str = DEFAULT_PERIOD
    run_id: int | None = None
    tester: str = ""
    suite: str = ""
    environment: str = ""

    @classmethod
    def from_request(cls, args) -> "Filters":
        """Read them off a query string, ignoring anything unrecognised."""
        def get(name: str) -> str:
            try:
                return (args.get(name) or "").strip()
            except Exception:      # pragma: no cover — exotic mapping
                return ""

        period = get("period") or DEFAULT_PERIOD
        if period not in PERIODS:
            period = DEFAULT_PERIOD
        raw_run = get("run")
        run_id = None
        if raw_run.isdigit():
            run_id = int(raw_run)
        return cls(period=period, run_id=run_id, tester=get("tester"),
                   suite=get("suite"), environment=get("environment"))

    @property
    def active(self) -> bool:
        return bool(self.run_id or self.tester or self.suite
                    or self.environment
                    or (self.period != DEFAULT_PERIOD))

    def since(self) -> datetime | None:
        days = PERIODS.get(self.period)
        if not days:
            return None
        return datetime.now(timezone.utc) - timedelta(days=days)

    def as_query(self) -> dict:
        """The non-default parts, for building links that keep the filter."""
        out = {}
        if self.period != DEFAULT_PERIOD:
            out["period"] = self.period
        if self.run_id:
            out["run"] = self.run_id
        for name in ("tester", "suite", "environment"):
            value = getattr(self, name)
            if value:
                out[name] = value
        return out


@dataclass
class Options:
    """What the filter controls can offer, from this project's own data.

    Offering a tester who never ran anything here, or a suite nothing is
    tagged with, produces a filter that returns nothing and looks broken.
    """
    testers: list[str] = field(default_factory=list)
    suites: list[str] = field(default_factory=list)
    environments: list[str] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)


def _counts(sess, model, column, project_id: str, *, extra=None) -> dict:
    """``{value: count}`` for one column, counted by the database."""
    from sqlalchemy import func, select

    target = getattr(model, column)
    query = (select(target, func.count()).where(model.project_id == project_id)
             .group_by(target))
    if extra is not None:
        query = query.where(extra)
    out: dict[str, int] = {}
    for value, count in sess.execute(query).all():
        key = (str(value).strip() if value not in (None, "") else "Unspecified")
        out[key] = out.get(key, 0) + int(count or 0)
    return out


def _total(sess, model, project_id: str, *, extra=None) -> int:
    from sqlalchemy import func, select

    query = select(func.count()).select_from(model).where(
        model.project_id == project_id)
    if extra is not None:
        query = query.where(extra)
    return int(sess.execute(query).scalar() or 0)


def options(project_id: str) -> Options:
    """The values this project's filters can take."""
    from engine import db as _db

    if not project_id:
        return Options()
    try:
        with _db.session_scope() as sess:
            from sqlalchemy import select
            suites = sorted({
                str(value).strip()
                for (value,) in sess.execute(
                    select(_db.TestCase.suite).where(
                        _db.TestCase.project_id == project_id).distinct()).all()
                if value and str(value).strip()})
            runs = sess.execute(
                select(_db.ExecutionRun).where(
                    _db.ExecutionRun.project_id == project_id)
                .order_by(_db.ExecutionRun.started_at.desc()).limit(50)
            ).scalars().all()
            testers, environments, run_rows = set(), set(), []
            for run in runs:
                env = dict(run.env_payload or {})
                name = str(env.get("tester_name") or env.get("tester_id")
                           or "").strip()
                if name:
                    testers.add(name)
                where = str(env.get("environment") or "").strip()
                if where:
                    environments.add(where)
                run_rows.append({
                    "id": int(run.id),
                    "started_at": (run.started_at.isoformat()
                                   if run.started_at else ""),
                    "tester": name, "environment": where,
                    "mode": str(env.get("mode") or ""),
                })
            return Options(testers=sorted(testers), suites=suites,
                           environments=sorted(environments), runs=run_rows)
    except Exception as exc:      # pragma: no cover — never break the page
        log.warning("dashboard filter options unavailable: %s", exc)
        return Options()


def _matching_run_ids(sess, project_id: str, filters: Filters):
    """Run ids the filter admits, or ``None`` when it admits all of them.

    ``None`` and ``[]`` mean different things and the caller must not confuse
    them: no filter versus a filter nothing matches.
    """
    from sqlalchemy import select

    from engine import db as _db

    if not (filters.run_id or filters.tester or filters.environment
            or filters.since()):
        return None

    query = select(_db.ExecutionRun).where(
        _db.ExecutionRun.project_id == project_id)
    since = filters.since()
    if since is not None:
        query = query.where(_db.ExecutionRun.started_at >= since)
    if filters.run_id:
        query = query.where(_db.ExecutionRun.id == filters.run_id)

    out = []
    for run in sess.execute(query).scalars().all():
        env = dict(run.env_payload or {})
        if filters.tester:
            name = str(env.get("tester_name") or env.get("tester_id") or "")
            if name.strip() != filters.tester:
                continue
        if filters.environment:
            if str(env.get("environment") or "").strip() != filters.environment:
                continue
        out.append(int(run.id))
    return out


def aggregate(project_id: str, filters: Filters | None = None) -> dict:
    """The dashboard's eighteen numbers, counted by the database.

    Falls back to the session-based aggregator when there is no project, and
    returns its empty shape when the database cannot answer — a dashboard that
    raises is worse than one that says it has nothing.
    """
    from engine.test_metrics_generator import compute_session_metrics

    filters = filters or Filters()
    if not project_id:
        return compute_session_metrics()

    from engine import db as _db

    try:
        with _db.session_scope() as sess:
            return _aggregate(sess, project_id, filters)
    except Exception as exc:
        log.warning("dashboard aggregation failed for %s: %s",
                    project_id[:8], exc)
        return compute_session_metrics()


def _aggregate(sess, project_id: str, filters: Filters) -> dict:
    from sqlalchemy import func, select

    from engine import db as _db

    suite_filter = None
    if filters.suite:
        suite_filter = _db.TestCase.suite == filters.suite

    tc_total = _total(sess, _db.TestCase, project_id, extra=suite_filter)
    tc_by_category = _counts(sess, _db.TestCase, "category", project_id,
                             extra=suite_filter)
    tc_by_priority = _counts(sess, _db.TestCase, "priority", project_id,
                             extra=suite_filter)

    # A suite is a property of a test case, so a suite filter says nothing
    # about checklist items. Reporting the unfiltered count beside filtered
    # test-case counts would be a subtly wrong comparison, so the checklist
    # is reported as zero-scoped when the filter cannot apply to it.
    if filters.suite:
        cl_total, cl_by_category, cl_by_priority = 0, {}, {}
    else:
        cl_total = _total(sess, _db.ChecklistItem, project_id)
        cl_by_category = _counts(sess, _db.ChecklistItem, "category",
                                 project_id)
        cl_by_priority = _counts(sess, _db.ChecklistItem, "priority",
                                 project_id)

    run_ids = _matching_run_ids(sess, project_id, filters)

    if run_ids is not None and not run_ids:
        # A filter nothing matches. Distinct from "no filter" — see
        # _matching_run_ids — and the difference is the whole point: the
        # honest answer here is zero, not everything.
        exec_counts = {}
    else:
        query = (select(_db.ExecutionCaseResult.status, func.count())
                 .join(_db.ExecutionRun,
                       _db.ExecutionCaseResult.run_id == _db.ExecutionRun.id)
                 .where(_db.ExecutionRun.project_id == project_id)
                 .group_by(_db.ExecutionCaseResult.status))
        if run_ids is not None:
            query = query.where(_db.ExecutionCaseResult.run_id.in_(run_ids))
        exec_counts = {}
        for status, count in sess.execute(query).all():
            key = str(status or "").strip() or "Unchecked"
            exec_counts[key] = exec_counts.get(key, 0) + int(count or 0)

    passed = exec_counts.get("Passed", 0)
    failed = exec_counts.get("Failed", 0)
    blocked = exec_counts.get("Blocked", 0)
    # "Passed but" counts as executed and as neither pass nor fail, the same
    # way the session aggregator treats it.
    executed = sum(exec_counts.values())

    runs_count = (len(run_ids) if run_ids is not None
                  else _total(sess, _db.ExecutionRun, project_id))

    bug_extra = None
    if run_ids is not None:
        bug_extra = (_db.BugReport.run_id.in_(run_ids) if run_ids
                     else _db.BugReport.id < 0)
    bug_total = _total(sess, _db.BugReport, project_id, extra=bug_extra)
    bug_by_severity = _counts(sess, _db.BugReport, "severity", project_id,
                              extra=bug_extra)
    bug_by_status = _counts(sess, _db.BugReport, "status", project_id,
                            extra=bug_extra)
    bug_by_priority = _counts(sess, _db.BugReport, "priority", project_id,
                              extra=bug_extra)

    return {
        "has_data": bool(tc_total or cl_total or bug_total or executed),
        "tc_total": tc_total,
        "tc_by_category": tc_by_category,
        "tc_by_priority": tc_by_priority,
        "cl_total": cl_total,
        "cl_by_category": cl_by_category,
        "cl_by_priority": cl_by_priority,
        "exec_total": executed,
        "exec_passed": passed,
        "exec_failed": failed,
        "exec_blocked": blocked,
        "exec_pass_rate": round(passed / executed * 100, 1) if executed else 0.0,
        "runs_count": runs_count,
        "bug_total": bug_total,
        "bug_by_severity": bug_by_severity,
        "bug_by_status": bug_by_status,
        "bug_by_priority": bug_by_priority,
        "environments": _environments(sess, project_id, run_ids),
    }


def _environments(sess, project_id: str, run_ids) -> list:
    """Environment names the runs in scope were executed against."""
    from sqlalchemy import select

    from engine import db as _db

    query = select(_db.ExecutionRun).where(
        _db.ExecutionRun.project_id == project_id)
    if run_ids is not None:
        if not run_ids:
            return []
        query = query.where(_db.ExecutionRun.id.in_(run_ids))
    seen = []
    for run in sess.execute(query).scalars().all():
        env = dict(run.env_payload or {})
        name = str(env.get("environment") or "").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


__all__ = ["DEFAULT_PERIOD", "Filters", "Options", "PERIODS", "aggregate",
           "options"]
