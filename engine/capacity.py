"""TestForTge — how much of the free plan is left (E0.11 / E0.12).

Two limits decide whether this deployment keeps working, and neither is
visible from inside the product today:

* **0.5 GB of database**, shared by every organisation on the instance;
* **cold starts**, because the free web service sleeps after ~15 idle
  minutes and the next visitor waits 30–60 seconds. Measured on production
  2026-08-06: ``curl /readyz`` answered 200 after **45 seconds**.

This module measures both. It does not fix either — one is a quota surface
(E0.12) and the other is a scheduled ping (E0.11,
``.github/workflows/keepalive.yml``).

**Not an argument for migrating.** The owner decided on 2026-08-06 that the
database stays on Render free: the deployment is a demo, and a monthly
reset is cheaper than the move. So the job here is to make the ceiling
legible and let a team stay under it — never to imply the answer is a
bigger plan. Wording in the UI follows from that.

Per-organisation footprint is an **estimate**, and says so
--------------------------------------------------------
Postgres can report the size of a table; it cannot report the share of a
table belonging to one organisation, because nothing here is partitioned by
org. The honest alternatives were: report only instance-wide bytes (true,
and useless for "which team should tidy up"), or report per-org **row
counts** multiplied by the per-row sizes ``docs/plans/cost_model.md`` §3
already measured. The second is what this does, and every surface that
shows it calls it an estimate — a number presented as exact, that is not,
is worse than a range.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from engine.log import get_logger

log = get_logger(__name__)

#: The free plan's hard ceiling, in bytes.
FREE_DB_LIMIT_BYTES = 512 * 1024 * 1024

#: Bytes per row, from cost_model.md §3 — measured against real data, not
#: guessed from column types. Used only for the per-org estimate; the
#: instance-wide figure comes from the database itself.
ROW_BYTES = {
    "test_cases": 1_500,
    "checklist_items": 400,
    "bug_reports": 2_000,
    "execution_results": 300,
    "audit_rows": 500,
}

#: Warn an organisation past this share of its allowance.
WARN_RATIO = 0.75

#: Audit entity recorded once per process start, so the cold-start rate is
#: measurable rather than estimated. One row per boot is a few dozen a day
#: on the free plan and answers a question nothing else can.
BOOT_ENTITY = "service"
BOOT_ACTION = "start"


def org_quota_rows() -> int:
    """Rows one organisation may hold before the UI warns.

    A row budget rather than a byte budget, because rows are what a team
    can act on: "you have 4,000 test cases" is a sentence somebody can do
    something about, and "you have 41 MB" is not.

    The default divides the 0.5 GB ceiling by the average artefact and
    leaves half the database for everything else — sessions, indexes, the
    audit trail, and the other organisations on the instance.
    """
    raw = (os.environ.get("ORG_QUOTA_ROWS") or "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            log.warning("ORG_QUOTA_ROWS=%r is not an integer — using the "
                        "default.", raw)
    return 50_000


# ── The database as a whole ──────────────────────────────────────────

@dataclass(frozen=True)
class DatabaseUsage:
    """Instance-wide size. ``bytes_used`` is None when it cannot be read."""

    bytes_used: int | None = None
    limit_bytes: int = FREE_DB_LIMIT_BYTES
    engine: str = ""

    @property
    def ratio(self) -> float:
        if not self.bytes_used or not self.limit_bytes:
            return 0.0
        return self.bytes_used / self.limit_bytes

    @property
    def over_warn(self) -> bool:
        return self.ratio >= WARN_RATIO

    @property
    def readable(self) -> str:
        return human_bytes(self.bytes_used) if self.bytes_used else "unknown"


def human_bytes(size: int | None) -> str:
    if not size:
        return "0 B"
    step = 1024.0
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < step or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} GB"      # pragma: no cover — unreachable


def database_usage() -> DatabaseUsage:
    """How much of the database is used, asked of the database.

    Postgres answers with ``pg_database_size`` — the query the runbook
    (``docs/runbooks/database-on-a-free-plan.md`` §3.1) tells an operator to
    run by hand. SQLite has no equivalent function, so the file is measured
    instead; that path is what the test suite exercises, and it is also the
    honest answer for a developer checkout.
    """
    from sqlalchemy import text

    from engine import db as _db

    url = ""
    try:
        url = _db.database_url()
    except Exception:      # pragma: no cover — config not loaded
        pass
    engine_name = "sqlite" if url.startswith("sqlite") else "postgresql"

    if engine_name == "sqlite":
        return DatabaseUsage(bytes_used=_sqlite_bytes(url), engine=engine_name)

    try:
        with _db.session_scope() as sess:
            size = sess.execute(
                text("SELECT pg_database_size(current_database())")).scalar()
        return DatabaseUsage(bytes_used=int(size or 0), engine=engine_name)
    except Exception as exc:
        # An unreadable size must not take a page down. "unknown" is a
        # legitimate answer and the UI renders it as one.
        log.warning("database size unavailable: %s", exc)
        return DatabaseUsage(bytes_used=None, engine=engine_name)


def _sqlite_bytes(url: str) -> int | None:
    path = url.split("sqlite:///", 1)[-1] if "sqlite:///" in url else ""
    if not path or path == ":memory:":
        return None
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(path + suffix)
        except OSError:
            continue
    return total or None


# ── One organisation's share ─────────────────────────────────────────

@dataclass(frozen=True)
class OrgUsage:
    """What one organisation is holding. Row counts are exact; bytes are
    an estimate — see the module docstring."""

    org_id: str = ""
    projects: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    quota_rows: int = 0

    @property
    def rows(self) -> int:
        return sum(self.counts.values())

    @property
    def estimated_bytes(self) -> int:
        return sum(ROW_BYTES.get(kind, 0) * count
                   for kind, count in self.counts.items())

    @property
    def ratio(self) -> float:
        if not self.quota_rows:
            return 0.0
        return self.rows / self.quota_rows

    @property
    def over_warn(self) -> bool:
        return self.ratio >= WARN_RATIO

    @property
    def over_quota(self) -> bool:
        return bool(self.quota_rows) and self.rows >= self.quota_rows

    @property
    def biggest(self) -> str:
        """Which artefact to tidy first, or "" when there is nothing.

        By estimated bytes rather than by count: 200 execution results are
        more rows than 100 bug reports and a great deal less database.
        """
        if not self.counts:
            return ""
        return max(self.counts,
                   key=lambda k: ROW_BYTES.get(k, 0) * self.counts[k])


def org_usage(org_id: str) -> OrgUsage | None:
    """Count what *org_id* holds. ``None`` when it cannot be read."""
    if not org_id:
        return None

    from sqlalchemy import func, select

    from engine import db as _db

    try:
        with _db.session_scope() as sess:
            project_ids = [
                row for row in sess.execute(
                    select(_db.Project.id)
                    .where(_db.Project.org_id == org_id)).scalars().all()]

            counts = {"test_cases": 0, "checklist_items": 0,
                      "bug_reports": 0, "execution_results": 0,
                      "audit_rows": 0}
            if project_ids:
                for key, model in (
                        ("test_cases", _db.TestCase),
                        ("checklist_items", _db.ChecklistItem),
                        ("bug_reports", _db.BugReport),
                ):
                    counts[key] = int(sess.execute(
                        select(func.count()).select_from(model)
                        .where(model.project_id.in_(project_ids))
                    ).scalar() or 0)
                counts["execution_results"] = int(sess.execute(
                    select(func.count())
                    .select_from(_db.ExecutionCaseResult)
                    .join(_db.ExecutionRun,
                          _db.ExecutionCaseResult.run_id == _db.ExecutionRun.id)
                    .where(_db.ExecutionRun.project_id.in_(project_ids))
                ).scalar() or 0)

            counts["audit_rows"] = int(sess.execute(
                select(func.count()).select_from(_db.AuditLog)
                .where(_db.AuditLog.org_id == org_id)).scalar() or 0)

        return OrgUsage(org_id=org_id, projects=len(project_ids),
                        counts=counts, quota_rows=org_quota_rows())
    except Exception as exc:
        log.warning("org usage unavailable for %s: %s", org_id[:8], exc)
        return None


# ── Cold starts ──────────────────────────────────────────────────────

def record_boot() -> None:
    """Note that this process started. Never raises.

    One row per boot, which is what makes the cold-start rate a
    *measurement* rather than a claim. On the free plan the service sleeps
    after ~15 idle minutes, so the count over a day is the number of times
    somebody waited — and that is the number worth putting in front of
    whoever decides whether keep-alive is worth setting up.
    """
    try:
        from engine import db as _db
        _db.append_audit(entity=BOOT_ENTITY, action=BOOT_ACTION)
    except Exception as exc:      # pragma: no cover — boot must not fail
        log.debug("boot not recorded: %s", exc)


def cold_starts(hours: int = 24) -> int | None:
    """How many times this service started in the last *hours*."""
    try:
        from engine import db as _db
        return _db.count_audit_since(
            entity=BOOT_ENTITY, action=BOOT_ACTION,
            since=datetime.now(timezone.utc) - timedelta(hours=max(1, hours)))
    except Exception as exc:
        log.warning("cold-start count unavailable: %s", exc)
        return None


def availability() -> dict:
    """What to tell an operator about waking up.

    No claim about whether keep-alive is configured: it lives in a GitHub
    Actions schedule, and the application cannot see that. What it *can*
    see is the consequence — how often it has restarted — which is the
    better thing to report anyway, because it is true whatever the cause.
    """
    starts = cold_starts(24)
    return {
        "cold_starts_24h": starts,
        "sleeps_after_minutes": 15,
        "cold_start_seconds": "30–60",
        # Two a day is a service that is used and sleeps overnight. Ten is
        # somebody waiting ten times.
        "noticeable": bool(starts and starts >= 5),
    }


__all__ = [
    "FREE_DB_LIMIT_BYTES", "ROW_BYTES", "WARN_RATIO",
    "BOOT_ENTITY", "BOOT_ACTION",
    "DatabaseUsage", "OrgUsage",
    "human_bytes", "database_usage", "org_quota_rows", "org_usage",
    "record_boot", "cold_starts", "availability",
]
