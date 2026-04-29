"""
TestForTge — Persistence layer (SQLAlchemy 2.0).

Backs the platform with a relational store for everything users care about:
projects, test cases, checklists, bug reports, estimation history, test
execution runs (and per-case results), Tedgie submissions and dashboard
metric snapshots.

Engine selection
----------------
Honours a single ``DATABASE_URL`` env var. In production Render injects
the connection string of the Postgres add-on; locally we fall back to a
SQLite file under ``STORAGE_FOLDER/testfortge.db`` so devs don't need a
running Postgres to run the app.

* ``postgres://`` URLs are auto-rewritten to ``postgresql+psycopg2://``
  (SQLAlchemy 2.x dropped the legacy alias).
* ``TESTFORTGE_DB`` is honoured as a path override (relative or
  absolute), useful for unit tests that want a throwaway file.

Public API mirrors what every other module needs — see the ``__all__``
block at the bottom for the full surface. Each helper opens its own
short-lived session so callers don't have to think about transactions.
"""
from __future__ import annotations

import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import (DateTime, Float, ForeignKey, Integer, String, Text,
                        create_engine, event, func, select)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import (DeclarativeBase, Mapped, Session, declared_attr,
                            mapped_column, relationship, sessionmaker)
from sqlalchemy.types import JSON

from engine.log import get_logger

log = get_logger(__name__)

# ── Engine / session factory (lazy) ────────────────────────────────

_engine: Engine | None = None
_Session: sessionmaker | None = None


def _normalize_url(raw: str) -> str:
    """Adapt legacy / Render-style URLs for SQLAlchemy 2.x."""
    if not raw:
        return raw
    # Render & Heroku still hand out ``postgres://`` — SQLAlchemy 2 wants
    # ``postgresql+psycopg2://``. Accept either prefix.
    if raw.startswith("postgres://"):
        return "postgresql+psycopg2://" + raw[len("postgres://"):]
    if raw.startswith("postgresql://"):
        return "postgresql+psycopg2://" + raw[len("postgresql://"):]
    return raw


def database_url() -> str:
    """Resolve the active SQLAlchemy URL.

    Priority:
      1. ``DATABASE_URL`` (Render-style) — used in production.
      2. ``TESTFORTGE_DB`` — explicit local path override.
      3. ``STORAGE_FOLDER/testfortge.db`` — convenient local fallback.
    """
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if url:
        return _normalize_url(url)

    explicit = (os.environ.get("TESTFORTGE_DB") or "").strip()
    if explicit:
        return f"sqlite:///{explicit}"

    storage = os.environ.get("STORAGE_FOLDER") or ""
    if storage and Path(storage).is_dir():
        sqlite_path = Path(storage) / "testfortge.db"
    else:
        sqlite_path = Path(__file__).resolve().parent.parent / "storage" / "testfortge.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{sqlite_path}"


def _build_engine(url: str) -> Engine:
    """Create a tuned engine for the chosen backend."""
    if url.startswith("sqlite"):
        # ``check_same_thread=False`` so Flask threads can share the
        # connection pool. ``pool_pre_ping`` is a no-op for sqlite but
        # cheap and harmless.
        eng = create_engine(
            url,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
            future=True,
        )

        # Foreign keys are off by default in SQLite; turn them on
        # for every connection so cascade-delete actually fires.
        @event.listens_for(eng, "connect")
        def _enable_sqlite_fk(dbapi_conn, _):  # pragma: no cover
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return eng

    # Postgres: stricter pooling, recycle every 30 min so Render's idle
    # connection killer doesn't hand us a dead socket.
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
    )


def init_db() -> None:
    """Create the engine + tables. Safe to call multiple times."""
    global _engine, _Session
    if _engine is not None:
        return
    url = database_url()
    log.info("Initialising DB engine: %s",
             "sqlite" if url.startswith("sqlite") else "postgresql")
    _engine = _build_engine(url)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    Base.metadata.create_all(_engine)


def get_engine() -> Engine:
    if _engine is None:
        init_db()
    assert _engine is not None
    return _engine


@contextmanager
def session_scope() -> Iterable[Session]:
    """Per-call transactional session. Commits on success, rolls back
    on exception, always closes."""
    if _Session is None:
        init_db()
    assert _Session is not None
    sess = _Session()
    try:
        yield sess
        sess.commit()
    except SQLAlchemyError:
        sess.rollback()
        raise
    finally:
        sess.close()


def ping() -> bool:
    """Lightweight health check — returns True if a SELECT 1 succeeds."""
    try:
        with session_scope() as sess:
            sess.execute(select(1))
        return True
    except SQLAlchemyError as exc:
        log.warning("DB ping failed: %s", exc)
        return False


# ── Models ─────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    """Common base — every table inherits ``id`` and audit timestamps."""

    @declared_attr.directive
    def __tablename__(cls) -> str:
        # CamelCase → snake_case; preserves single-word names too.
        return re.sub(r"(?<!^)(?=[A-Z])", "_", cls.__name__).lower()


def _uuid() -> str:
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(220), nullable=False, unique=True, index=True)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_sid: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  default=_utcnow, onupdate=_utcnow)

    test_cases = relationship("TestCase", back_populates="project",
                              cascade="all, delete-orphan", passive_deletes=True)
    checklist_items = relationship("ChecklistItem", back_populates="project",
                                   cascade="all, delete-orphan", passive_deletes=True)
    bugs = relationship("BugReport", back_populates="project",
                        cascade="all, delete-orphan", passive_deletes=True)
    estimations = relationship("Estimation", back_populates="project",
                               cascade="all, delete-orphan", passive_deletes=True)
    runs = relationship("ExecutionRun", back_populates="project",
                        cascade="all, delete-orphan", passive_deletes=True)
    metric_snapshots = relationship("DashboardMetricSnapshot", back_populates="project",
                                    cascade="all, delete-orphan", passive_deletes=True)
    tedgie_submissions = relationship("TedgieSubmission", back_populates="project",
                                      cascade="all, delete-orphan", passive_deletes=True)


class TestCase(Base):
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False, index=True)
    external_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    section: Mapped[str | None] = mapped_column(String(200), nullable=True)
    section_num: Mapped[str | None] = mapped_column(String(40), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    preconditions: Mapped[str | None] = mapped_column(Text, nullable=True)
    test_steps: Mapped[str | None] = mapped_column(Text, nullable=True)
    test_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    issues: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_story_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    priority: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    testing_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  default=_utcnow, onupdate=_utcnow)

    project = relationship("Project", back_populates="test_cases")


class ChecklistItem(Base):
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False, index=True)
    external_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    section: Mapped[str | None] = mapped_column(String(200), nullable=True)
    objective: Mapped[str | None] = mapped_column(Text, nullable=True)
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_story_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    priority: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    testing_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  default=_utcnow, onupdate=_utcnow)

    project = relationship("Project", back_populates="checklist_items")


class BugReport(Base):
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Tedgie may submit a bug before any project is selected — keep nullable.
    project_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("project.id", ondelete="SET NULL"),
        nullable=True, index=True)
    external_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    severity: Mapped[str | None] = mapped_column(String(20), nullable=True)
    priority: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    environment: Mapped[str | None] = mapped_column(String(200), nullable=True)
    browser: Mapped[str | None] = mapped_column(String(80), nullable=True)
    os: Mapped[str | None] = mapped_column(String(80), nullable=True)
    version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    steps_to_reproduce: Mapped[str | None] = mapped_column(Text, nullable=True)
    actual_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    reporter: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual",
                                         index=True)
    related_case_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("test_case.id", ondelete="SET NULL"), nullable=True)
    run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("execution_run.id", ondelete="SET NULL"),
        nullable=True, index=True)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  default=_utcnow, onupdate=_utcnow)

    project = relationship("Project", back_populates="bugs")


class Estimation(Base):
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False, index=True)
    input_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    total_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True)

    project = relationship("Project", back_populates="estimations")


class ExecutionRun(Base):
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False, index=True)
    env_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    browser_visibility: Mapped[str | None] = mapped_column(String(40), nullable=True)
    record_video: Mapped[bool] = mapped_column(default=False)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running",
                                         index=True)
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    project = relationship("Project", back_populates="runs")
    case_results = relationship("ExecutionCaseResult", back_populates="run",
                                cascade="all, delete-orphan", passive_deletes=True)


class ExecutionCaseResult(Base):
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("execution_run.id", ondelete="CASCADE"),
        nullable=False, index=True)
    case_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("test_case.id", ondelete="SET NULL"), nullable=True)
    case_external_id: Mapped[str | None] = mapped_column(String(40), nullable=True,
                                                          index=True)
    case_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="test_case")
    status: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    evidence_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    bug_report_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bug_report.id", ondelete="SET NULL"), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    run = relationship("ExecutionRun", back_populates="case_results")


class DashboardMetricSnapshot(Base):
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False, index=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True)
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    project = relationship("Project", back_populates="metric_snapshots")


class TedgieSubmission(Base):
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("project.id", ondelete="SET NULL"),
        nullable=True, index=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    classified_into_bug_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bug_report.id", ondelete="SET NULL"), nullable=True)
    reporter: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True)

    project = relationship("Project", back_populates="tedgie_submissions")


# ── Helpers (slug, dict serialisation) ─────────────────────────────

def _slugify(name: str, fallback: str = "project") -> str:
    """Lowercase, hyphen-separated, ASCII-only slug."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return (s or fallback)[:200]


def _row_to_dict(obj: Base) -> dict:
    """SQLAlchemy ORM row → plain dict (with ISO-formatted datetimes)."""
    out: dict[str, Any] = {}
    for col in obj.__table__.columns:  # type: ignore[attr-defined]
        val = getattr(obj, col.name)
        if isinstance(val, datetime):
            val = val.isoformat()
        out[col.name] = val
    return out


# ── Projects ───────────────────────────────────────────────────────

def upsert_project(name: str, base_url: str | None = None,
                   description: str | None = None,
                   owner_sid: str | None = None) -> str:
    """Create-or-return-existing by (owner_sid, slug)."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Project name is required")
    slug = _slugify(name)
    # Make slug unique-per-owner by appending the sid prefix when present.
    if owner_sid:
        slug = f"{slug}-{owner_sid[:8]}"

    with session_scope() as sess:
        existing = sess.execute(
            select(Project).where(Project.slug == slug)
        ).scalar_one_or_none()
        if existing:
            existing.name = name  # name can drift even if slug is stable
            if base_url is not None:
                existing.base_url = base_url
            if description is not None:
                existing.description = description
            return existing.id

        proj = Project(
            id=_uuid(),
            name=name,
            slug=slug,
            base_url=base_url,
            description=description,
            owner_sid=owner_sid,
        )
        sess.add(proj)
        sess.flush()
        return proj.id


def list_projects(owner_sid: str | None = None) -> list[dict]:
    with session_scope() as sess:
        stmt = select(Project).order_by(Project.updated_at.desc())
        if owner_sid:
            stmt = stmt.where(Project.owner_sid == owner_sid)
        rows = sess.execute(stmt).scalars().all()

        # Pull aggregate counts per project in a single round-trip.
        counts_tc = dict(sess.execute(
            select(TestCase.project_id, func.count(TestCase.id))
            .group_by(TestCase.project_id)
        ).all())
        counts_cl = dict(sess.execute(
            select(ChecklistItem.project_id, func.count(ChecklistItem.id))
            .group_by(ChecklistItem.project_id)
        ).all())
        counts_bug = dict(sess.execute(
            select(BugReport.project_id, func.count(BugReport.id))
            .group_by(BugReport.project_id)
        ).all())

        out = []
        for p in rows:
            d = _row_to_dict(p)
            d["test_cases_count"] = counts_tc.get(p.id, 0)
            d["checklist_count"] = counts_cl.get(p.id, 0)
            d["bug_count"] = counts_bug.get(p.id, 0)
            # Back-compat keys for templates that used the old folder model.
            d["folder"] = p.id
            d["project_name"] = p.name
            d["saved_at"] = d["updated_at"]
            out.append(d)
        return out


def get_project(project_id: str) -> dict | None:
    with session_scope() as sess:
        p = sess.get(Project, project_id)
        return _row_to_dict(p) if p else None


def delete_project(project_id: str) -> None:
    with session_scope() as sess:
        p = sess.get(Project, project_id)
        if p:
            sess.delete(p)


# ── Test cases ─────────────────────────────────────────────────────

def save_test_cases(project_id: str, test_cases: list) -> int:
    """Replace all TC for a project with the new list. Returns rows written."""
    if not project_id:
        raise ValueError("project_id is required")
    written = 0
    with session_scope() as sess:
        # Wipe-and-replace keeps semantics simple — caller treats DB as
        # the single source of truth for the *current* TC set.
        sess.query(TestCase).filter(TestCase.project_id == project_id).delete()
        for tc in test_cases or []:
            d = tc if isinstance(tc, dict) else getattr(tc, "__dict__", {})
            sess.add(TestCase(
                project_id=project_id,
                external_id=d.get("id"),
                section=d.get("section"),
                section_num=d.get("section_num"),
                summary=d.get("summary"),
                preconditions=d.get("preconditions"),
                test_steps=d.get("test_steps"),
                test_data=d.get("test_data"),
                expected_result=d.get("expected_result"),
                issues=d.get("issues"),
                comment=d.get("comment"),
                user_story_id=d.get("user_story_id"),
                category=d.get("category"),
                priority=d.get("priority"),
                status=d.get("status"),
                testing_type=d.get("testing_type", "Functional"),
            ))
            written += 1
    return written


_TC_DATACLASS_FIELDS = (
    "id", "section", "section_num", "summary", "preconditions",
    "test_steps", "test_data", "expected_result", "issues", "comment",
    "user_story_id", "category", "priority", "status", "testing_type",
)


def load_test_cases(project_id: str) -> list[dict]:
    """Return TC rows shaped for the in-session dataclass.

    Strips DB-only fields (project_id, created_at, ...) so callers can
    feed the dicts straight into ``TestCase(**d)`` via
    ``reconstruct_test_cases``."""
    if not project_id:
        return []
    with session_scope() as sess:
        rows = sess.execute(
            select(TestCase).where(TestCase.project_id == project_id)
            .order_by(TestCase.id.asc())
        ).scalars().all()
        out = []
        for tc in rows:
            d = _row_to_dict(tc)
            d["id"] = d.pop("external_id") or f"TC-{tc.id:03d}"
            # Coerce section_num — DB stores it as str, dataclass wants int.
            sn = d.get("section_num")
            try:
                d["section_num"] = int(sn) if sn not in (None, "") else 0
            except (TypeError, ValueError):
                d["section_num"] = 0
            # Drop DB-only keys.
            out.append({k: d.get(k, "") for k in _TC_DATACLASS_FIELDS})
        return out


# ── Checklist ──────────────────────────────────────────────────────

def save_checklist(project_id: str, items: list) -> int:
    if not project_id:
        raise ValueError("project_id is required")
    written = 0
    with session_scope() as sess:
        sess.query(ChecklistItem).filter(ChecklistItem.project_id == project_id).delete()
        for cl in items or []:
            d = cl if isinstance(cl, dict) else getattr(cl, "__dict__", {})
            sess.add(ChecklistItem(
                project_id=project_id,
                external_id=d.get("id"),
                section=d.get("section"),
                objective=d.get("objective"),
                comments=d.get("comments"),
                user_story_id=d.get("user_story_id"),
                category=d.get("category"),
                priority=d.get("priority"),
                status=d.get("status"),
                testing_type=d.get("testing_type", "Functional"),
            ))
            written += 1
    return written


_CL_DATACLASS_FIELDS = (
    "id", "section", "objective", "comments", "user_story_id",
    "category", "priority", "status", "testing_type",
)


def load_checklist(project_id: str) -> list[dict]:
    """Return CL rows shaped for the in-session dataclass."""
    if not project_id:
        return []
    with session_scope() as sess:
        rows = sess.execute(
            select(ChecklistItem).where(ChecklistItem.project_id == project_id)
            .order_by(ChecklistItem.id.asc())
        ).scalars().all()
        out = []
        for cl in rows:
            d = _row_to_dict(cl)
            d["id"] = d.pop("external_id") or f"CL-{cl.id:03d}"
            out.append({k: d.get(k, "") for k in _CL_DATACLASS_FIELDS})
        return out


# ── Bug reports ────────────────────────────────────────────────────

VALID_BUG_SOURCES = {"tedgie", "execution", "manual", "import"}


def save_bug(project_id: str | None, bug: dict, source: str = "manual") -> int:
    """Persist a bug report. Returns the row id (auto-assigned)."""
    if source not in VALID_BUG_SOURCES:
        source = "manual"
    bug = bug or {}
    extra_keys = {k: v for k, v in bug.items() if k not in (
        "id", "title", "severity", "priority", "status",
        "environment", "browser", "os", "version",
        "steps_to_reproduce", "actual_result", "expected_result",
        "comment", "reporter", "related_case_id", "run_id",
    )}
    with session_scope() as sess:
        row = BugReport(
            project_id=project_id,
            external_id=bug.get("id"),
            title=bug.get("title") or bug.get("summary"),
            severity=bug.get("severity"),
            priority=bug.get("priority"),
            status=bug.get("status") or "Open",
            environment=bug.get("environment"),
            browser=bug.get("browser"),
            os=bug.get("os"),
            version=bug.get("version"),
            steps_to_reproduce=bug.get("steps_to_reproduce"),
            actual_result=bug.get("actual_result"),
            expected_result=bug.get("expected_result"),
            comment=bug.get("comment"),
            reporter=bug.get("reporter"),
            source=source,
            related_case_id=bug.get("related_case_id"),
            run_id=bug.get("run_id"),
            extra=extra_keys or None,
        )
        sess.add(row)
        sess.flush()
        return row.id


def list_bugs(project_id: str | None = None,
              source: str | None = None) -> list[dict]:
    with session_scope() as sess:
        stmt = select(BugReport).order_by(BugReport.created_at.desc())
        if project_id:
            stmt = stmt.where(BugReport.project_id == project_id)
        if source:
            stmt = stmt.where(BugReport.source == source)
        rows = sess.execute(stmt).scalars().all()
        return [_row_to_dict(r) for r in rows]


# ── Estimation ─────────────────────────────────────────────────────

def save_estimation(project_id: str, input_payload: dict,
                    result_payload: dict, total_hours: float | None = None) -> int:
    if not project_id:
        raise ValueError("project_id is required")
    with session_scope() as sess:
        row = Estimation(
            project_id=project_id,
            input_payload=input_payload or {},
            result_payload=result_payload or {},
            total_hours=total_hours,
        )
        sess.add(row)
        sess.flush()
        return row.id


def list_estimations(project_id: str, limit: int = 50) -> list[dict]:
    with session_scope() as sess:
        rows = sess.execute(
            select(Estimation).where(Estimation.project_id == project_id)
            .order_by(Estimation.created_at.desc()).limit(limit)
        ).scalars().all()
        return [_row_to_dict(r) for r in rows]


def latest_estimation(project_id: str) -> dict | None:
    if not project_id:
        return None
    with session_scope() as sess:
        row = sess.execute(
            select(Estimation).where(Estimation.project_id == project_id)
            .order_by(Estimation.created_at.desc()).limit(1)
        ).scalar_one_or_none()
        return _row_to_dict(row) if row else None


# ── Execution runs ─────────────────────────────────────────────────

def start_execution_run(project_id: str, env_payload: dict,
                        browser_visibility: str | None = None,
                        record_video: bool = False,
                        base_url: str | None = None) -> int:
    if not project_id:
        raise ValueError("project_id is required")
    with session_scope() as sess:
        row = ExecutionRun(
            project_id=project_id,
            env_payload=env_payload or {},
            browser_visibility=browser_visibility,
            record_video=bool(record_video),
            base_url=base_url,
            status="running",
        )
        sess.add(row)
        sess.flush()
        return row.id


def finish_execution_run(run_id: int, status: str = "completed",
                         stats: dict | None = None) -> None:
    with session_scope() as sess:
        row = sess.get(ExecutionRun, run_id)
        if not row:
            return
        row.status = status
        row.stats = stats or {}
        row.finished_at = _utcnow()


def save_case_result(run_id: int, *, case_external_id: str | None = None,
                     case_id: int | None = None, case_kind: str = "test_case",
                     status: str | None = None, evidence_path: str | None = None,
                     bug_report_id: int | None = None,
                     notes: str | None = None) -> int:
    with session_scope() as sess:
        row = ExecutionCaseResult(
            run_id=run_id,
            case_id=case_id,
            case_external_id=case_external_id,
            case_kind=case_kind,
            status=status,
            evidence_path=evidence_path,
            bug_report_id=bug_report_id,
            notes=notes,
        )
        sess.add(row)
        sess.flush()
        return row.id


def list_execution_runs(project_id: str, limit: int = 50) -> list[dict]:
    with session_scope() as sess:
        rows = sess.execute(
            select(ExecutionRun).where(ExecutionRun.project_id == project_id)
            .order_by(ExecutionRun.started_at.desc()).limit(limit)
        ).scalars().all()
        return [_row_to_dict(r) for r in rows]


# ── Dashboard metrics ──────────────────────────────────────────────

def save_metric_snapshot(project_id: str, metrics: dict) -> int:
    if not project_id:
        raise ValueError("project_id is required")
    with session_scope() as sess:
        row = DashboardMetricSnapshot(
            project_id=project_id,
            metrics=metrics or {},
        )
        sess.add(row)
        sess.flush()
        return row.id


def list_metric_snapshots(project_id: str, limit: int = 30) -> list[dict]:
    with session_scope() as sess:
        rows = sess.execute(
            select(DashboardMetricSnapshot)
            .where(DashboardMetricSnapshot.project_id == project_id)
            .order_by(DashboardMetricSnapshot.captured_at.desc()).limit(limit)
        ).scalars().all()
        return [_row_to_dict(r) for r in rows]


# ── Tedgie submissions ─────────────────────────────────────────────

def save_tedgie_submission(project_id: str | None, raw_payload: dict,
                           reporter: str | None = None,
                           classified_into_bug_id: int | None = None) -> int:
    with session_scope() as sess:
        row = TedgieSubmission(
            project_id=project_id,
            raw_payload=raw_payload or {},
            reporter=reporter,
            classified_into_bug_id=classified_into_bug_id,
        )
        sess.add(row)
        sess.flush()
        return row.id


def list_tedgie_submissions(project_id: str | None = None,
                            limit: int = 100) -> list[dict]:
    with session_scope() as sess:
        stmt = select(TedgieSubmission).order_by(
            TedgieSubmission.created_at.desc()).limit(limit)
        if project_id:
            stmt = stmt.where(TedgieSubmission.project_id == project_id)
        rows = sess.execute(stmt).scalars().all()
        return [_row_to_dict(r) for r in rows]


# ── Aggregate counts (for /metrics) ────────────────────────────────

def count_records() -> dict:
    """Cheap top-level counts — used by /metrics and admin checks."""
    with session_scope() as sess:
        return {
            "projects": int(sess.execute(select(func.count(Project.id))).scalar() or 0),
            "test_cases": int(sess.execute(select(func.count(TestCase.id))).scalar() or 0),
            "checklist_items": int(sess.execute(select(func.count(ChecklistItem.id))).scalar() or 0),
            "bug_reports": int(sess.execute(select(func.count(BugReport.id))).scalar() or 0),
            "estimations": int(sess.execute(select(func.count(Estimation.id))).scalar() or 0),
            "execution_runs": int(sess.execute(select(func.count(ExecutionRun.id))).scalar() or 0),
            "metric_snapshots": int(sess.execute(select(func.count(DashboardMetricSnapshot.id))).scalar() or 0),
            "tedgie_submissions": int(sess.execute(select(func.count(TedgieSubmission.id))).scalar() or 0),
        }


__all__ = [
    # bootstrap
    "init_db", "get_engine", "session_scope", "ping", "database_url",
    # models (exposed for advanced callers / migrations)
    "Base", "Project", "TestCase", "ChecklistItem", "BugReport",
    "Estimation", "ExecutionRun", "ExecutionCaseResult",
    "DashboardMetricSnapshot", "TedgieSubmission",
    # projects
    "upsert_project", "list_projects", "get_project", "delete_project",
    # test cases
    "save_test_cases", "load_test_cases",
    # checklist
    "save_checklist", "load_checklist",
    # bugs
    "save_bug", "list_bugs", "VALID_BUG_SOURCES",
    # estimation
    "save_estimation", "list_estimations", "latest_estimation",
    # execution
    "start_execution_run", "finish_execution_run", "save_case_result",
    "list_execution_runs",
    # dashboard
    "save_metric_snapshot", "list_metric_snapshots",
    # tedgie
    "save_tedgie_submission", "list_tedgie_submissions",
    # aggregates
    "count_records",
]
