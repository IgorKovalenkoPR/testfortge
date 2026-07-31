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

import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Integer, String,
                        Text, UniqueConstraint,
                        create_engine, event, func, select, text, update)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
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

        # Configure SQLite for safer concurrent access:
        #   * ``foreign_keys=ON`` — off by default in SQLite; needed so
        #     cascade-delete actually fires.
        #   * ``journal_mode=WAL`` — snapshot writer + runner worker stop
        #     blocking each other on read.
        #   * ``synchronous=NORMAL`` — WAL-safe durability with much less
        #     fsync cost than FULL.
        #   * ``busy_timeout=5000`` — rare write-write collisions wait
        #     quietly for up to 5s instead of raising ``database is
        #     locked`` immediately.
        @event.listens_for(eng, "connect")
        def _configure_sqlite_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=5000")
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


def _assert_prod_safety(url: str) -> None:
    """Refuse to boot a production process backed by SQLite.

    SQLite is fine for unit tests and local dev, but in production the
    snapshot writer + detached ``runner_worker`` issue concurrent writes
    from multiple gunicorn workers and will deadlock under load.
    ``FLASK_DEBUG=1`` is our "this is local" signal; anything else is
    assumed to be a real deployment and must use Postgres.

    Escape hatch for solo-VM self-hosters:
    ``TESTFORTGE_ALLOW_SQLITE_PROD=1`` downgrades the hard raise to a
    loud warning.
    """
    in_debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    if not in_debug and url.startswith("sqlite"):
        msg = ("TestForTge starting with SQLite in non-debug mode. "
               "SQLite OK for unit tests and local dev, NOT production: "
               "concurrent writes from gunicorn workers + detached "
               "runner_worker will deadlock under load. Set "
               "DATABASE_URL=postgresql://... or FLASK_DEBUG=1.")
        if os.environ.get("TESTFORTGE_ALLOW_SQLITE_PROD") == "1":
            log.warning(msg + " Continuing because TESTFORTGE_ALLOW_SQLITE_PROD=1.")
        else:
            raise RuntimeError(msg)


def init_db() -> None:
    """Create the engine + tables. Safe to call multiple times.

    **Atomic on failure.** The module globals ``_engine`` / ``_Session``
    are published only after ``create_all`` (the first real connection)
    succeeds. If the database is unreachable at boot — e.g. Render's
    free-tier Postgres has expired — this raises with the globals left
    at ``None``, so a later lazy call (via :func:`session_scope` /
    :func:`get_engine`) retries the full setup instead of
    short-circuiting on a half-built engine that never ran ``create_all``.
    """
    global _engine, _Session
    if _engine is not None:
        return
    url = database_url()
    _assert_prod_safety(url)
    log.info("Initialising DB engine: %s",
             "sqlite" if url.startswith("sqlite") else "postgresql")
    engine = _build_engine(url)
    # create_all opens the first connection — this is where an outage
    # surfaces. Keep it off the module globals until it (and the
    # follow-up ALTERs) have gone through.
    Base.metadata.create_all(engine)
    _ensure_walkthrough_columns(engine)
    _engine = engine
    _Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _ensure_walkthrough_columns(engine: Engine) -> None:
    """Idempotent ALTER TABLE for the walkthrough fields added by the
    TFWefloLab integration (PR-2).

    ``Base.metadata.create_all`` only creates *missing* tables — it does
    not touch the schema of existing ones. Projects that booted before
    PR-2 already have a ``test_case`` table without ``url_pattern`` or
    ``trigger`` columns, so reading TestCase rows via the ORM would raise
    ``OperationalError: no such column``. This helper inspects the live
    schema, issues ``ALTER TABLE ADD COLUMN`` for whichever column is
    absent, and exits quietly when everything is already there.

    Safe to run on every boot — both SQLite and Postgres support
    ``ALTER TABLE ADD COLUMN ... DEFAULT '' NOT NULL`` and the operation
    is no-op once the column exists.
    """
    try:
        from sqlalchemy import inspect as _inspect
        insp = _inspect(engine)
        existing = {c["name"] for c in insp.get_columns("test_case")}
    except SQLAlchemyError as exc:
        # Table doesn't exist yet (first-time install) — create_all
        # above will have made it with the columns already present.
        log.debug("walkthrough column probe skipped: %s", exc)
        return

    additions: list[tuple[str, str]] = []
    if "url_pattern" not in existing:
        additions.append((
            "url_pattern",
            "ALTER TABLE test_case ADD COLUMN url_pattern "
            "VARCHAR(200) NOT NULL DEFAULT ''",
        ))
    if "trigger" not in existing:
        # ``trigger`` is a reserved word in Postgres (and a keyword in
        # SQLite) — double-quote the identifier in the literal SQL we
        # issue here. SQLAlchemy's ORM layer already quotes the column
        # automatically when it generates SELECTs, but the manual ALTER
        # below has to quote explicitly.
        additions.append((
            "trigger",
            'ALTER TABLE test_case ADD COLUMN "trigger" '
            "VARCHAR(40) NOT NULL DEFAULT 'manual'",
        ))
    # PR-B (Recorder MVP) column. NOT NULL DEFAULT '' mirrors
    # ``url_pattern`` and ``trigger`` so dataclass callers never see a
    # surprise None. Existing rows back-fill with '' and behave
    # byte-identically — the runner falls back to its heuristic parse of
    # ``test_steps`` when this is empty.
    if "automation_steps_json" not in existing:
        additions.append((
            "automation_steps_json",
            "ALTER TABLE test_case ADD COLUMN automation_steps_json "
            "TEXT NOT NULL DEFAULT ''",
        ))
    # PR-D suite classification — same NOT NULL DEFAULT '' pattern so
    # pre-PR-D rows back-fill to the "unclassified" bucket and the
    # /test-cases filter UI's "All" chip still shows them.
    if "suite" not in existing:
        additions.append((
            "suite",
            "ALTER TABLE test_case ADD COLUMN suite "
            "VARCHAR(20) NOT NULL DEFAULT ''",
        ))
    # PR-3 dual-format columns. Same idempotent probe; existing rows
    # back-fill to "manual", which is the format they were written in.
    if "tc_format" not in existing:
        additions.append((
            "tc_format",
            "ALTER TABLE test_case ADD COLUMN tc_format "
            "VARCHAR(20) NOT NULL DEFAULT 'manual'",
        ))
    if "gherkin" not in existing:
        additions.append((
            "gherkin",
            "ALTER TABLE test_case ADD COLUMN gherkin "
            "TEXT NOT NULL DEFAULT ''",
        ))

    # PR-F (deep-capture telemetry) column on ``session_draft``. Same
    # idempotent probe: a project that booted before PR-F has the table
    # without ``telemetry_json``, and reading SessionDraft via the ORM
    # would raise "no such column". Probed separately because it's a
    # different table; a missing table (fresh install) just skips —
    # ``create_all`` above made it with the column already present.
    sd_additions: list[tuple[str, str]] = []
    try:
        sd_existing = {c["name"] for c in insp.get_columns("session_draft")}
    except SQLAlchemyError as exc:
        log.debug("session_draft column probe skipped: %s", exc)
        sd_existing = None
    if sd_existing is not None and "telemetry_json" not in sd_existing:
        sd_additions.append((
            "telemetry_json",
            "ALTER TABLE session_draft ADD COLUMN telemetry_json "
            "TEXT NOT NULL DEFAULT ''",
        ))

    # PR-2 low-level checklist numbering on ``checklist_item``. Same
    # idempotent probe and the same NOT NULL DEFAULT idiom: a project that
    # booted before PR-2 has the table without these, and reading
    # ChecklistItem through the ORM would raise "no such column".
    cl_additions: list[tuple[str, str]] = []
    try:
        cl_existing = {c["name"] for c in insp.get_columns("checklist_item")}
    except SQLAlchemyError as exc:
        log.debug("checklist_item column probe skipped: %s", exc)
        cl_existing = None
    if cl_existing is not None:
        if "item_num" not in cl_existing:
            cl_additions.append((
                "item_num",
                "ALTER TABLE checklist_item ADD COLUMN item_num "
                "VARCHAR(24) NOT NULL DEFAULT ''",
            ))
        if "depth" not in cl_existing:
            cl_additions.append((
                "depth",
                "ALTER TABLE checklist_item ADD COLUMN depth "
                "INTEGER NOT NULL DEFAULT 2",
            ))

    if not additions and not sd_additions and not cl_additions:
        return

    try:
        with engine.begin() as conn:
            for col_name, sql in additions:
                log.info("walkthrough migration: adding test_case.%s",
                         col_name)
                conn.execute(text(sql))
            for col_name, sql in sd_additions:
                log.info("walkthrough migration: adding session_draft.%s",
                         col_name)
                conn.execute(text(sql))
            for col_name, sql in cl_additions:
                log.info("checklist migration: adding checklist_item.%s",
                         col_name)
                conn.execute(text(sql))
    except SQLAlchemyError as exc:  # pragma: no cover — best-effort
        # If the ALTER fails (concurrent boot race, unsupported dialect)
        # the worker process keeps running; reads via the ORM will then
        # surface a clear OperationalError the operator can act on.
        log.warning("walkthrough migration ALTER failed: %s", exc)


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
    site_profiles = relationship("SiteProfile", back_populates="project",
                                  cascade="all, delete-orphan", passive_deletes=True)
    locators = relationship("Locator", back_populates="project",
                            cascade="all, delete-orphan", passive_deletes=True)
    # PR-D — pending session-review drafts. Cascade so deleting the
    # project also clears unreviewed recordings; passive_deletes lets
    # the DB's ON DELETE CASCADE handle the rows without ORM load.
    session_drafts = relationship("SessionDraft", back_populates="project",
                                   cascade="all, delete-orphan",
                                   passive_deletes=True)


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
    # TFWefloLab walkthrough integration (PR-2): regex/glob URL pattern
    # that lets the walkthrough mode opportunistically fire this TC when
    # it lands on a matching page. Empty string preserves today's
    # behaviour — only the TC-driven runner consults this field, and only
    # when ``trigger`` is set to a walkthrough mode.
    url_pattern: Mapped[str] = mapped_column(String(200), nullable=False,
                                              default="", server_default="")
    # How this TC is fired: ``manual`` (default — only user-driven runs
    # exercise it, byte-identical to today), ``walkthrough_url_match``
    # (walkthrough mode runs it when the current URL matches
    # ``url_pattern``), or ``always`` (walkthrough mode always runs it,
    # regardless of URL match). Default ``manual`` so existing projects
    # never see walkthrough side-effects when the feature flag flips on.
    trigger: Mapped[str] = mapped_column(String(40), nullable=False,
                                          default="manual",
                                          server_default="manual")
    # Recorder integration: serialised list[dict] of AutomationStep payloads
    # captured by ``tfg record`` (PR-B). When non-empty, ``_run_script``
    # prefers these over the heuristic parse of ``test_steps``. Empty
    # string on every pre-Recorder row — matches the NOT NULL DEFAULT ''
    # pattern used by ``url_pattern`` / ``trigger`` so callers never see
    # a surprise None value through the dataclass field.
    automation_steps_json: Mapped[str] = mapped_column(Text, nullable=False,
                                                       default="",
                                                       server_default="")
    # PR-D: test-suite classification — "" (unclassified, default),
    # "Smoke", "Regression", or "E2E". Set by the suite_classifier when a
    # TC is created via the session-review flow; manually editable from
    # the TC editor later. Filtered on /test-cases (chip row) and on
    # /test-execution ("Run only [suite ▼]" knob). Empty string is the
    # default to keep every pre-PR-D TC visible under "All" — the
    # filter UI explicitly handles the empty bucket.
    suite: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="", server_default="")
    # PR-3: dual-format test cases. ``tc_format`` is "manual" | "gherkin";
    # ``gherkin`` holds the .feature Scenario block. Named tc_format rather
    # than format because FORMAT is reserved in several SQL dialects.
    # Same NOT NULL DEFAULT idiom as url_pattern / suite, so every
    # pre-PR-3 row back-fills to the manual format it already was.
    tc_format: Mapped[str] = mapped_column(String(20), nullable=False,
                                            default="manual",
                                            server_default="manual")
    gherkin: Mapped[str] = mapped_column(Text, nullable=False,
                                          default="", server_default="")
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
    # PR-2: hierarchical row number ("1.1", "2.7.1") and its depth. The
    # reference low-level checklist numbers every row and those numbers get
    # cited in bug reports, so they are persisted rather than recomputed —
    # recomputing would renumber siblings when a row is inserted, which
    # checklist_style.yaml explicitly forbids. Empty string / 2 on every
    # pre-PR-2 row, matching the NOT NULL DEFAULT idiom used by
    # TestCase.url_pattern and TestCase.suite.
    item_num: Mapped[str] = mapped_column(String(24), nullable=False,
                                           default="", server_default="")
    depth: Mapped[int] = mapped_column(Integer, nullable=False,
                                        default=2, server_default="2")
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


class AutomationRun(Base):
    """One ingested ``allure-results`` upload.

    Separate from :class:`ExecutionRun` on purpose. An ExecutionRun is
    something this service performed and can attribute per item; an
    AutomationRun happened somewhere else — a laptop, a CI job — and
    arrives as a finished artefact. Merging them would mean every
    ExecutionRun query grew a "but was it really ours" branch, and the
    per-case results here are keyed by the ``@TC-…`` tag rather than by a
    row id we issued.
    """
    __tablename__ = "automation_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True,
                                    autoincrement=True)
    project_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("project.id", ondelete="CASCADE"),
        nullable=True, index=True)
    #: Where it ran, as reported by the uploader: "local", "ci", "unknown".
    origin: Mapped[str] = mapped_column(String(20), nullable=False,
                                         default="unknown")
    #: Free-text label from the uploader — a branch name, a build number.
    label: Mapped[str] = mapped_column(String(200), nullable=False,
                                        default="")
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    broken: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Kept as its own column, never folded into passed or failed: a
    #: skipped scenario is one nobody checked, and both alternatives would
    #: misreport it. See engine.allure_ingest.
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False,
                                              default=0)
    pass_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: The full RunSummary.to_dict(), for the drill-down view.
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: Relative path under STORAGE_ROOT of an uploaded prebuilt
    #: allure-report bundle, when one was supplied.
    report_path: Mapped[str] = mapped_column(String(500), nullable=False,
                                              default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True)


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


class SiteProfile(Base):
    """Stage-2 site-aware generation: per-(project, url) recon result.

    Cached so the second Generate-from-URL hit on the same target does
    not re-spend an LLM call. The same row holds the strategy matrix
    returned by the Strategy Agent — both blobs are JSON so the schema
    survives prompt-shape iteration without a migration.
    """
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    profile: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    strategy: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    project = relationship("Project", back_populates="site_profiles")


class Locator(Base):
    """PR-A multi-locator Page Object: remembers which selector strategy
    actually resolved each recorded element last time, so the runner can
    promote the winning fallback to the front of the chain on the next
    run.

    Schema mirrors the spec in ``docs/plans/recorder_integration.md``
    (PR-A § Files / Modified ``engine/db.py``): one row per
    (project_id, label) pair, ``candidates_json`` carries the ranked
    LocatorCandidate list that the recorder captured, and the two counters
    drive a simple learning loop the runner pings in
    ``try_locator_chain``. Project scoping is enforced by the
    UniqueConstraint so a stray label collision between two unrelated
    projects can't poison either's stats.
    """
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False, index=True)
    # 500 chars — recorder labels can include arbitrarily long button
    # text (e.g. ``text=Subscribe to our weekly newsletter and...``),
    # and the original 200 limit truncated those silently on Postgres.
    # SQLite ignores the length spec, so this is a no-op there.
    label: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    candidates_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_success_strategy: Mapped[str | None] = mapped_column(
        String(40), nullable=True)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, index=True)

    __table_args__ = (
        UniqueConstraint("project_id", "label", name="uq_locator_project_label"),
    )

    project = relationship("Project", back_populates="locators")


class SessionDraft(Base):
    """PR-D — staging row for a recorded session before the operator
    confirms which proposed TCs to keep.

    Lifecycle:
      1. ``tools/tfg_record.py`` finishes capture → segments steps via
         :mod:`engine.session_segmenter` → classifies each segment via
         :mod:`engine.suite_classifier` → INSERTs one row here with the
         full ``ProposedTC`` list serialised as JSON.
      2. CLI prints the review URL ``/test-cases/review-session/<token>``.
      3. Operator opens the URL, picks Save / Skip + suite override per
         segment, hits Save.
      4. Review route consumes the draft (``consumed=True``) and creates
         N ``TestCase`` rows in the active project.

    Drafts auto-expire after 24 h via ``expires_at`` so an abandoned
    recording never lingers indefinitely. The token is a high-entropy
    string the operator can't forge — required because the URL is
    publicly reachable until the session cookie's project_id matches.
    """
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 64 chars: secrets.token_urlsafe(32) → 43-char base64 + headroom.
    token: Mapped[str] = mapped_column(String(64), nullable=False,
                                        unique=True, index=True)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False, index=True)
    # Serialised list[ProposedTC] dicts: [{"summary", "intent",
    # "suggested_suite", "steps": [...AutomationStep dicts...]}].
    proposed_tcs_json: Mapped[str] = mapped_column(Text, nullable=False,
                                                    default="")
    # PR-F — deep-capture telemetry recorded live via the extension's
    # chrome.debugger (CDP) session: {"network": [...], "console": [...],
    # "dom_snapshots": [...], "meta": {...}}. Empty string for CLI-staged
    # sessions and pre-PR-F recordings, so the review page just hides the
    # telemetry panel rather than erroring. NOT NULL DEFAULT '' mirrors
    # the walkthrough columns' back-fill pattern.
    telemetry_json: Mapped[str] = mapped_column(Text, nullable=False,
                                                 default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True)
    # Single-use — once an operator clicks Save, the draft is sealed so
    # a refresh of the review page can't double-insert the TCs.
    consumed: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                            default=False, server_default="0")

    project = relationship("Project", back_populates="session_drafts")


class BrowserControlSession(Base):
    """PR-F Phase 2 — a live browser-control session.

    The recorder extension can also act as a remotely-driven CDP
    executor: an MCP tool (a different process, sharing this DB)
    enqueues :class:`BrowserCommand` rows, the extension long/short-polls
    Flask for them, executes each against the bound tab, and writes the
    result back. This table is the trust anchor — a command is only
    accepted for a token that has a live, unexpired control session, and
    the session is bound to exactly one project so an agent can't drive a
    browser it wasn't authorised against.

    ``last_seen_at`` is bumped on every poll so ``browser_control_status``
    can tell a caller whether the operator's browser is actually attached
    (vs. a stale token whose tab was closed).
    """
    id: Mapped[int] = mapped_column(Integer, primary_key=True,
                                     autoincrement=True)
    token: Mapped[str] = mapped_column(String(64), nullable=False,
                                        unique=True, index=True)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # Sealed on explicit Stop — a sealed session refuses new commands.
    active: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                          default=True, server_default="1")

    project = relationship("Project")


class BrowserCommand(Base):
    """PR-F Phase 2 — one queued browser command + its result.

    Lifecycle: ``pending`` → ``dispatched`` (extension picked it up) →
    ``done`` / ``error`` (result written). The controller (MCP tool)
    enqueues then polls this row for the terminal state. Commands
    auto-expire so an abandoned session's queue can't accrete forever.
    """
    id: Mapped[int] = mapped_column(Integer, primary_key=True,
                                     autoincrement=True)
    command_id: Mapped[str] = mapped_column(String(40), nullable=False,
                                              unique=True, index=True)
    token: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    verb: Mapped[str] = mapped_column(String(32), nullable=False)
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                         default="pending", index=True)
    result_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    done_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True)


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


def get_project_owner(project_id: str) -> str | None:
    """Return the ``owner_sid`` for *project_id*, or ``None`` if the row
    doesn't exist or has no owner recorded (legacy projects created
    before owner_sid was wired up).

    Thin accessor — avoids hydrating the full project dict just to
    perform an authorization check on every project-scoped route.
    Callers must distinguish "missing project" (404) from "NULL owner"
    (allow with warning) on their own; both return ``None`` here.
    """
    if not project_id:
        return None
    try:
        with session_scope() as sess:
            return sess.query(Project.owner_sid).filter(
                Project.id == project_id).scalar()
    except Exception:
        return None


def touch_project(project_id: str) -> bool:
    """Bump ``updated_at`` on a project so list_projects() surfaces it
    at the top of the dropdown. Called by every persistence helper so
    "recent activity" actually means recent activity, not just creation
    time. Returns True when a row was touched, False if the id was bogus
    or the row was missing. Best-effort: never raises — DB outage just
    means the project's updated_at doesn't advance, which is harmless.
    """
    if not project_id:
        return False
    try:
        with session_scope() as sess:
            p = sess.get(Project, project_id)
            if not p:
                return False
            p.updated_at = _utcnow()
            sess.add(p)
            sess.flush()
            return True
    except Exception:
        return False


def update_project(project_id: str, *,
                   name: str | None = None,
                   base_url: str | None = None,
                   description: str | None = None) -> bool:
    """Partial-update a project's metadata. Skips empty / None fields so
    callers only need to pass what changes. Returns ``True`` when a row
    was actually touched, ``False`` if the id was bogus.

    The ``slug`` column is intentionally NOT regenerated when ``name``
    changes — slug is a stable identifier (used to deduplicate uploads
    in :func:`upsert_project`). If a future caller needs a slug-rename,
    add it explicitly there.
    """
    with session_scope() as sess:
        p = sess.get(Project, project_id)
        if not p:
            return False
        touched = False
        if name is not None:
            n = name.strip()
            if n and n != p.name:
                p.name = n
                touched = True
        if base_url is not None:
            bu = base_url.strip()
            if bu != (p.base_url or ""):
                p.base_url = bu or None
                touched = True
        if description is not None:
            d = description.strip()
            if d != (p.description or ""):
                p.description = d or None
                touched = True
        return touched


# Mapping: artefact-kind -> ORM model, so move_artifacts can enumerate
# everything without hard-coding the table list in two places.
_MOVABLE_KINDS: dict[str, type] = {
    "test_cases":         TestCase,
    "checklist_items":    ChecklistItem,
    "bug_reports":        BugReport,
    "estimations":        Estimation,
    "execution_runs":     ExecutionRun,
    "metric_snapshots":   DashboardMetricSnapshot,
    "tedgie_submissions": TedgieSubmission,
}


def move_artifacts(source_project_id: str,
                   target_project_id: str,
                   kinds: list[str] | None = None) -> dict[str, int]:
    """Bulk-move artefacts from *source* to *target*. UPDATE-only — no
    rows are duplicated, no rows are deleted. Returns ``{kind: rows_moved}``
    so callers can build a flash message.

    Both project ids must exist; ``ValueError`` on either invalid id or
    when source and target are the same.

    ``kinds`` defaults to every movable artefact kind. Pass a subset
    (``["test_cases", "checklist_items"]``) when the caller only wants
    to move those.
    """
    if not source_project_id or not target_project_id:
        raise ValueError("both source_project_id and target_project_id are required")
    if source_project_id == target_project_id:
        raise ValueError("source and target must differ")

    selected = kinds or list(_MOVABLE_KINDS.keys())
    moved: dict[str, int] = {}

    with session_scope() as sess:
        # Confirm both projects exist before touching artefact tables.
        if sess.get(Project, source_project_id) is None:
            raise ValueError(f"source project {source_project_id!r} not found")
        if sess.get(Project, target_project_id) is None:
            raise ValueError(f"target project {target_project_id!r} not found")

        for kind in selected:
            model = _MOVABLE_KINDS.get(kind)
            if model is None:
                continue
            n = (sess.query(model)
                 .filter(model.project_id == source_project_id)
                 .update({model.project_id: target_project_id},
                         synchronize_session=False))
            moved[kind] = int(n or 0)

    return moved


def delete_project(project_id: str) -> None:
    with session_scope() as sess:
        p = sess.get(Project, project_id)
        if p:
            sess.delete(p)


# ── Test cases ─────────────────────────────────────────────────────

def save_test_cases(project_id: str, test_cases: list) -> int:
    """Replace all TC for a project with the new list. Returns rows written.

    Bumps ``project.updated_at`` so the picker dropdown shows
    recently-touched projects at the top.
    """
    if not project_id:
        raise ValueError("project_id is required")
    written = 0
    with session_scope() as sess:
        # Bump updated_at on the parent project so list_projects()
        # surfaces this row to the top of the dropdown.
        proj = sess.get(Project, project_id)
        if proj is not None:
            proj.updated_at = _utcnow()
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
                # Sprint 5: round-trip the walkthrough binding fields.
                # PR-2 added the columns; this is the save side wiring
                # so the editor UI's writes actually persist.
                url_pattern=d.get("url_pattern", "") or "",
                trigger=d.get("trigger", "manual") or "manual",
                # PR-B: recorder output. Empty string when the TC was
                # not recorded — coerce None / missing to "" so the NOT
                # NULL constraint is never violated. Stored as a JSON
                # string so the runner decodes on demand without forcing
                # a column type migration later.
                automation_steps_json=d.get("automation_steps_json") or "",
                # PR-D — test-suite classification carried over when a
                # call-site already knows the suite (e.g. the session
                # review POST creates pre-classified rows). Empty string
                # for every other authoring path keeps the column happy
                # under NOT NULL DEFAULT ''.
                suite=d.get("suite", "") or "",
                # PR-3: coerced rather than trusted — a stray value in the
                # column would make every downstream format check
                # ambiguous, and the coercion is one function call.
                tc_format=_coerce_tc_format(d.get("tc_format")),
                gherkin=d.get("gherkin") or "",
            ))
            written += 1
    return written


def create_test_case(project_id: str, tc: dict) -> int | None:
    """PR-D — append a single TC row without wiping the project's pack.

    Different from :func:`save_test_cases` which is wipe-and-replace.
    Used by the session-review POST so each Saved ProposedTC lands as a
    new row instead of clobbering the existing pack. Returns the new
    row's primary key, or None when project_id is empty / project does
    not exist.
    """
    if not project_id:
        return None
    with session_scope() as sess:
        proj = sess.get(Project, project_id)
        if proj is None:
            return None
        row = TestCase(
            project_id=project_id,
            external_id=tc.get("id"),
            section=tc.get("section"),
            section_num=tc.get("section_num"),
            summary=tc.get("summary"),
            preconditions=tc.get("preconditions"),
            test_steps=tc.get("test_steps"),
            test_data=tc.get("test_data"),
            expected_result=tc.get("expected_result"),
            issues=tc.get("issues"),
            comment=tc.get("comment"),
            user_story_id=tc.get("user_story_id"),
            category=tc.get("category"),
            priority=tc.get("priority", "Medium"),
            status=tc.get("status", "Unchecked"),
            testing_type=tc.get("testing_type", "Functional"),
            url_pattern=tc.get("url_pattern", "") or "",
            trigger=tc.get("trigger", "manual") or "manual",
            automation_steps_json=tc.get("automation_steps_json") or "",
            suite=tc.get("suite", "") or "",
        )
        sess.add(row)
        sess.flush()
        proj.updated_at = _utcnow()
        return row.id


_TC_DATACLASS_FIELDS = (
    "id", "section", "section_num", "summary", "preconditions",
    "test_steps", "test_data", "expected_result", "issues", "comment",
    "user_story_id", "category", "priority", "status", "testing_type",
    # Sprint 5: walkthrough binding metadata. Listed last so callers
    # that unpack-by-position keep their existing column order.
    "url_pattern", "trigger",
    # PR-B: recorder output. Listed after walkthrough fields for the
    # same reason — additive columns extend the tuple, never reshuffle.
    "automation_steps_json",
    # PR-D: suite classification. Same append-only pattern — kept last.
    "suite",
    # PR-3: dual-format. Append-only, same reason.
    "tc_format", "gherkin",
)


def _coerce_tc_format(value) -> str:
    """Normalise a submitted / stored format onto ("manual", "gherkin").

    Kept here rather than imported from engine.gherkin so the DB layer has
    no dependency on the generator package — db.py is imported by the MCP
    server and the detached runner worker, which do not need it.
    """
    return "gherkin" if str(value or "").strip().lower() in (
        "gherkin", "bdd", "feature", "automation") else "manual"


def update_tc_automation_steps(project_id: str,
                                tc_external_id: str,
                                steps: list[dict]) -> bool:
    """Write a serialised AutomationStep list onto a TC's ``automation_steps_json``.

    Looks the row up by ``(project_id, external_id)``. Returns ``True``
    on success, ``False`` when no matching row exists (caller decides
    whether that's an error). Passing ``steps=[]`` clears the column —
    the runner then falls back to its heuristic parse of ``test_steps``.

    The list is JSON-serialised here so the call site does not need to
    care about column-type subtleties. We persist a string so a future
    PR can switch to JSON-typed columns without rewriting the helper.
    """
    if not project_id or not tc_external_id:
        return False
    payload = json.dumps(steps, ensure_ascii=False) if steps else ""
    with session_scope() as sess:
        row = sess.execute(
            select(TestCase).where(
                TestCase.project_id == project_id,
                TestCase.external_id == tc_external_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        row.automation_steps_json = payload
        # Bump parent project's updated_at so the picker reflects recency.
        proj = sess.get(Project, project_id)
        if proj is not None:
            proj.updated_at = _utcnow()
        return True


# ── PR-A: Locator registry (Page Object DB) ───────────────────────

def register_locator_candidates(project_id: str, label: str,
                                 candidates: list[dict]) -> int:
    """Upsert a Locator row with the latest ranked candidate list.

    Called by the recorder pipeline after each successful capture. When
    a row already exists, only ``candidates_json`` + ``last_seen`` are
    refreshed — the success / fail counters survive so the historical
    learning signal isn't reset by a fresh recording of the same flow.
    Returns the locator row id, or 0 if inputs are invalid (no project,
    blank label, or non-list candidates).

    Race tolerance: two concurrent first-time registrations for the
    same ``(project_id, label)`` would otherwise both INSERT and lose
    the second to a UNIQUE-constraint violation. We catch
    :class:`IntegrityError` once and retry as an UPDATE — at-most-one
    INSERT, at-least-once UPDATE, idempotent payload.
    """
    if not (project_id and label) or not isinstance(candidates, list):
        return 0
    payload = json.dumps(candidates, ensure_ascii=False)
    for attempt in range(2):
        try:
            with session_scope() as sess:
                row = sess.execute(
                    select(Locator).where(
                        Locator.project_id == project_id,
                        Locator.label == label,
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = Locator(
                        project_id=project_id,
                        label=label,
                        candidates_json=payload,
                    )
                    sess.add(row)
                    sess.flush()
                    return row.id
                # UPDATE existing in-place. Atomic at the statement level
                # — the session_scope() commit serialises with other
                # writers; concurrent updates land in insertion order.
                row.candidates_json = payload
                row.last_seen = _utcnow()
                return row.id
        except IntegrityError:
            # Lost the INSERT race — loop once and treat as UPDATE.
            if attempt == 0:
                continue
            log.warning("register_locator_candidates: IntegrityError "
                        "persisted after retry for project=%s label=%s",
                        project_id, label)
            return 0
    return 0


def record_locator_success(project_id: str, label: str,
                            strategy: str) -> bool:
    """Bump ``success_count`` and stamp ``last_success_strategy`` so the
    next ``try_locator_chain`` walk can short-circuit straight to the
    winner. No-op (returns False) when the row doesn't exist yet — the
    recorder must register candidates first via
    :func:`register_locator_candidates`.

    The strategy string mirrors the LocatorCandidate.strategy taxonomy
    (``testid|id|role|label|text|placeholder|css|xpath``). Garbage in,
    garbage out — the helper trusts the caller.

    Atomic at the SQL statement level — two parallel gunicorn workers
    proving the same TC both bump the counter via a single
    ``UPDATE … SET success_count = success_count + 1`` so neither
    loses its increment. ORM-side ``onupdate`` doesn't fire for raw
    UPDATE statements, so ``last_seen`` is set explicitly.
    """
    if not (project_id and label):
        return False
    values: dict = {
        "success_count": Locator.success_count + 1,
        "last_seen": _utcnow(),
    }
    if strategy:
        values["last_success_strategy"] = strategy
    with session_scope() as sess:
        result = sess.execute(
            update(Locator)
            .where(Locator.project_id == project_id,
                   Locator.label == label)
            .values(**values)
        )
        return result.rowcount > 0


def record_locator_failure(project_id: str, label: str,
                            strategy: str = "all") -> bool:
    """Bump ``fail_count`` after every candidate in the chain failed.

    ``strategy`` defaults to the sentinel ``"all"`` because the runner
    reaches this branch only when none of the alternates resolved; if
    a caller wants to track per-strategy fails (currently no caller
    does), they can pass the failing strategy name and the registry
    keeps the value alongside the counter. No-op when the row is
    absent. Same atomic-UPDATE pattern as ``record_locator_success``.
    """
    if not (project_id and label):
        return False
    with session_scope() as sess:
        result = sess.execute(
            update(Locator)
            .where(Locator.project_id == project_id,
                   Locator.label == label)
            .values(fail_count=Locator.fail_count + 1,
                    last_seen=_utcnow())
        )
        return result.rowcount > 0


def get_locator(project_id: str, label: str) -> dict | None:
    """Return the locator row as a plain dict (with parsed candidates), or
    ``None`` when no row exists. Used by ``engine.locator_registry`` to
    promote the previously-winning strategy to the front of the chain.

    The ``candidates`` key in the returned dict is the decoded JSON list;
    keeping the raw ``candidates_json`` string out of the public shape
    matches the same convention every other DB helper follows.
    """
    if not (project_id and label):
        return None
    with session_scope() as sess:
        row = sess.execute(
            select(Locator).where(
                Locator.project_id == project_id,
                Locator.label == label,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        try:
            cands = json.loads(row.candidates_json or "[]")
            if not isinstance(cands, list):
                cands = []
        except (json.JSONDecodeError, TypeError):
            cands = []
        return {
            "id": row.id,
            "project_id": row.project_id,
            "label": row.label,
            "candidates": cands,
            "last_success_strategy": row.last_success_strategy,
            "success_count": row.success_count,
            "fail_count": row.fail_count,
        }


def list_locators(project_id: str) -> list[dict]:
    """Project-scoped dump of every Locator row. Returns an empty list
    when ``project_id`` is blank. Used by tests + a future Locator
    admin page; not on the hot path of the runner."""
    if not project_id:
        return []
    with session_scope() as sess:
        rows = sess.execute(
            select(Locator).where(Locator.project_id == project_id)
            .order_by(Locator.label.asc())
        ).scalars().all()
        out: list[dict] = []
        for row in rows:
            try:
                cands = json.loads(row.candidates_json or "[]")
                if not isinstance(cands, list):
                    cands = []
            except (json.JSONDecodeError, TypeError):
                cands = []
            out.append({
                "id": row.id,
                "label": row.label,
                "candidates": cands,
                "last_success_strategy": row.last_success_strategy,
                "success_count": row.success_count,
                "fail_count": row.fail_count,
            })
        return out


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
        # Bump project recency for the picker dropdown.
        proj = sess.get(Project, project_id)
        if proj is not None:
            proj.updated_at = _utcnow()
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
                item_num=d.get("item_num") or "",
                depth=int(d.get("depth") or 2),
            ))
            written += 1
    return written


_CL_DATACLASS_FIELDS = (
    "id", "section", "objective", "comments", "user_story_id",
    "category", "priority", "status", "testing_type",
    "item_num", "depth",
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

# PR-H: ``walkthrough`` + ``live_executor`` previously coerced to
# ``manual`` because they weren't in the whitelist — but routes/
# execution.py passes them explicitly when persisting walkthrough
# bugs and the LiveExecutor early-exit bug factory. Including them
# here lets the cross-run dedup query (which filters by source)
# actually find prior walkthrough bugs instead of skipping every
# row as "manual entry".
VALID_BUG_SOURCES = {
    "tedgie", "execution", "manual", "import",
    "walkthrough", "live_executor",
}


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
        # Bump project recency so a fresh bug surfaces the project at
        # the top of the picker dropdown.
        if project_id:
            proj = sess.get(Project, project_id)
            if proj is not None:
                proj.updated_at = _utcnow()
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
              source: str | None = None,
              run_id: int | None = None) -> list[dict]:
    """List bug rows, newest-first.

    ``run_id`` scopes the listing to a single Test Execution run — the
    ``/bug-reports`` run filter uses it so an operator who has hammered
    the same project across many runs can look at just the run they
    care about instead of the whole historical pile. Only bugs the
    runner filed against that run carry a ``run_id``; manually-filed /
    Tedgie bugs have ``run_id IS NULL`` and are therefore excluded when
    a specific run is requested (the caller offers an "All runs" option
    for the unscoped view).
    """
    with session_scope() as sess:
        stmt = select(BugReport).order_by(BugReport.created_at.desc())
        if project_id:
            stmt = stmt.where(BugReport.project_id == project_id)
        if source:
            stmt = stmt.where(BugReport.source == source)
        if run_id is not None:
            stmt = stmt.where(BugReport.run_id == run_id)
        rows = sess.execute(stmt).scalars().all()
        return [_row_to_dict(r) for r in rows]


def count_bugs_by_run(project_id: str) -> dict[int | None, int]:
    """Return ``{run_id: bug_count}`` for a project in one round-trip.

    Powers the per-run bug counts in the ``/bug-reports`` run-filter
    dropdown so each option reads e.g. "Run #42 — 18 bugs" without an
    N+1 query. The ``None`` key (when present) counts manually-filed /
    Tedgie bugs that aren't tied to any run.
    """
    if not project_id:
        return {}
    with session_scope() as sess:
        rows = sess.execute(
            select(BugReport.run_id, func.count(BugReport.id))
            .where(BugReport.project_id == project_id)
            .group_by(BugReport.run_id)
        ).all()
        return {rid: int(cnt) for rid, cnt in rows}


# ── PR-H: cross-run dedup helpers ─────────────────────────────────


def find_bug_id_by_signature(
    project_id: str, dedup_signature: str,
) -> int | None:
    """Return the row id of an existing bug in ``project_id`` whose
    ``extra.dedup_signature`` matches ``dedup_signature``; ``None``
    when no match exists. Used by the walkthrough save-path so a
    rerun on an unchanged site does not pile a duplicate bug onto
    the project for every finding it already recorded.

    Implementation note: SQLAlchemy's portable JSON column doesn't
    expose ``->>`` operators on SQLite (the default in dev), so the
    function does the comparison in Python after pulling the
    project's bug rows. Pagination is unnecessary at the typical
    project scale (hundreds of bugs); if we ever hit four-digit
    backlogs we can migrate the dedup signature into its own indexed
    column without API changes.
    """
    if not (project_id and dedup_signature):
        return None
    with session_scope() as sess:
        stmt = (
            select(BugReport.id, BugReport.extra)
            .where(BugReport.project_id == project_id)
            .where(BugReport.source.in_(("walkthrough", "live_executor")))
        )
        for row_id, extra in sess.execute(stmt).all():
            ext = extra or {}
            if not isinstance(ext, dict):
                continue
            if ext.get("dedup_signature") == dedup_signature:
                return int(row_id)
        return None


def bump_bug_occurrence(bug_id: int) -> int:
    """Increment the ``extra.occurrence_count`` of ``bug_id`` and
    refresh ``updated_at``; return the new count.

    Called by the walkthrough save-path when ``find_bug_id_by_signature``
    finds an existing row — instead of inserting a duplicate, we
    just bump the counter so the bug record reflects "seen 4 times
    across runs". Best-effort: missing extras and unexpected dict
    shapes are tolerated (the column is JSON, not a strict schema).
    """
    with session_scope() as sess:
        row = sess.get(BugReport, bug_id)
        if row is None:
            return 0
        extra = dict(row.extra or {})
        try:
            new_count = int(extra.get("occurrence_count") or 1) + 1
        except (TypeError, ValueError):
            new_count = 2
        extra["occurrence_count"] = new_count
        row.extra = extra
        # SQLAlchemy detects ``onupdate=_utcnow`` automatically when
        # any column changes; touching ``extra`` is enough.
        return new_count


def delete_bugs_for_project(project_id: str) -> int:
    """Hard-delete every bug row attached to ``project_id``; return
    the count deleted. Used by the Reset Project action — the
    operator wants a clean slate after a noisy walkthrough run, and
    SET NULL cascade isn't enough (it leaves orphan rows visible in
    the bug listing). Confirmation lives in the route layer.
    """
    if not project_id:
        return 0
    with session_scope() as sess:
        rows = sess.execute(
            select(BugReport).where(BugReport.project_id == project_id)
        ).scalars().all()
        count = len(rows)
        for r in rows:
            sess.delete(r)
        proj = sess.get(Project, project_id)
        if proj is not None:
            proj.updated_at = _utcnow()
        return count


# ── Bulk bug operations (Sprint 4 task 4.2) ───────────────────────

# Whitelist of action names accepted by :func:`bulk_update_bugs`. Each
# action maps to either a direct column update, an ``extra`` JSON write
# (``assign``), or a row delete. Keeping this list closed means the
# route layer can reject unknown actions with a simple membership test
# before hitting the DB.
ALLOWED_BULK_ACTIONS = (
    "close", "delete", "assign",
    "status", "severity", "priority", "fix_version",
)

# Column map for the four "single-field set" actions. ``fix_version``
# is exposed under that operator-friendly name but stored in the
# ``version`` column to match the existing schema and exporter.
_BULK_COLUMN_MAP = {
    "status":      "status",
    "severity":    "severity",
    "priority":    "priority",
    "fix_version": "version",
}


def _append_audit(prev: str | None, actor: str, msg: str) -> str:
    """Return ``prev`` with one extra audit line appended.

    Format: ``[YYYY-MM-DD HH:MM] actor: msg``. Empty / missing
    ``prev`` produces just the new line so first-ever audits don't
    leak a leading newline.
    """
    stamp = _utcnow().strftime("%Y-%m-%d %H:%M")
    line = f"[{stamp}] {actor or 'unknown'}: {msg}"
    base = (prev or "").rstrip()
    return f"{base}\n{line}" if base else line


def bulk_update_bugs(project_id: str, bug_ids: list[int], *,
                     action: str, value: str | None,
                     actor: str) -> int:
    """Apply ``action`` to every bug in ``bug_ids`` that belongs to
    ``project_id``. Returns the number of rows actually touched.

    Cross-project safety: the ``project_id`` filter is applied to the
    underlying ``DELETE`` / ``UPDATE`` so a stale checkbox value from
    another project cannot leak into the result.

    ``action`` must be in :data:`ALLOWED_BULK_ACTIONS`. ``value`` is
    optional for ``close`` and ``delete``; required for the rest (an
    empty string is allowed for the "clear field" use case). ``actor``
    appears in the audit-trail line appended to each row's ``comment``
    column on non-delete actions.
    """
    if not project_id or not bug_ids or action not in ALLOWED_BULK_ACTIONS:
        return 0
    with session_scope() as sess:
        q = (sess.query(BugReport)
                 .filter(BugReport.project_id == project_id,
                         BugReport.id.in_(bug_ids)))
        if action == "delete":
            return int(q.delete(synchronize_session=False) or 0)

        rows = q.all()
        if not rows:
            return 0

        if action == "close":
            payload = {"status": "Closed"}
            audit_msg = "status -> Closed"
        elif action == "assign":
            payload = None
            audit_msg = f"assignee -> {value or ''}"
        else:
            column = _BULK_COLUMN_MAP[action]
            payload = {column: value}
            audit_msg = f"{action} -> {value}"

        for r in rows:
            if action == "assign":
                # Assignee lives in the JSON ``extra`` column because the
                # SQLAlchemy model doesn't expose it as a first-class
                # field — keeps schema flat while still letting the UI
                # render it. Re-bind the dict so JSON-mutation detection
                # in SQLAlchemy fires reliably across backends.
                extra = dict(r.extra or {})
                extra["assignee"] = value or ""
                r.extra = extra
            elif payload:
                for col, val in payload.items():
                    setattr(r, col, val)
            r.comment = _append_audit(r.comment, actor, audit_msg)
        return len(rows)


# ── Estimation ─────────────────────────────────────────────────────

def save_estimation(project_id: str, input_payload: dict,
                    result_payload: dict, total_hours: float | None = None) -> int:
    if not project_id:
        raise ValueError("project_id is required")
    with session_scope() as sess:
        # Bump project recency for the picker dropdown.
        proj = sess.get(Project, project_id)
        if proj is not None:
            proj.updated_at = _utcnow()
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


def list_estimations_by_owner(owner_sid: str, *,
                              similar_features: int | None = None,
                              tolerance: float = 0.4,
                              limit: int = 25) -> list[dict]:
    """Return past estimations across every project owned by *owner_sid*,
    optionally filtered to peers whose feature_count is within
    ``similar_features * (1 ± tolerance)``.

    Used by the historical-calibration helper so a new estimate gets
    compared only to projects of comparable size.
    """
    if not owner_sid:
        return []
    with session_scope() as sess:
        # Estimations join Project on project_id; filter by owner_sid.
        stmt = (select(Estimation)
                .join(Project, Estimation.project_id == Project.id)
                .where(Project.owner_sid == owner_sid)
                .order_by(Estimation.created_at.desc())
                .limit(limit * 4))  # over-fetch, we'll size-filter below
        rows = sess.execute(stmt).scalars().all()
        out = []
        for r in rows:
            d = _row_to_dict(r)
            if similar_features and similar_features > 0:
                try:
                    fc = int(((d.get("input_payload") or {})
                              .get("features_count")) or 0)
                except (TypeError, ValueError):
                    fc = 0
                if fc <= 0:
                    continue
                lo = similar_features * (1 - tolerance)
                hi = similar_features * (1 + tolerance)
                if not (lo <= fc <= hi):
                    continue
            out.append(d)
            if len(out) >= limit:
                break
        return out


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
        # Bump project recency — a new run is the strongest signal of
        # active work on this project, should be top of the picker.
        proj = sess.get(Project, project_id)
        if proj is not None:
            proj.updated_at = _utcnow()
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


def save_automation_run(project_id: str | None, summary: dict, *,
                        origin: str = "unknown", label: str = "",
                        report_path: str = "") -> int:
    """Persist one ingested ``allure-results`` upload. Returns the row id."""
    with session_scope() as sess:
        if project_id:
            proj = sess.get(Project, project_id)
            if proj is not None:
                proj.updated_at = _utcnow()
        row = AutomationRun(
            project_id=project_id or None,
            origin=(origin or "unknown")[:20],
            label=(label or "")[:200],
            total=int(summary.get("total") or 0),
            passed=int(summary.get("passed") or 0),
            failed=int(summary.get("failed") or 0),
            broken=int(summary.get("broken") or 0),
            skipped=int(summary.get("skipped") or 0),
            duration_ms=int(summary.get("duration_ms") or 0),
            pass_rate=summary.get("pass_rate"),
            summary=summary,
            report_path=(report_path or "")[:500],
        )
        sess.add(row)
        sess.flush()
        return int(row.id)


def list_automation_runs(project_id: str | None = None,
                         limit: int = 30) -> list[dict]:
    """Most recent automation runs, newest first."""
    with session_scope() as sess:
        stmt = select(AutomationRun)
        if project_id:
            stmt = stmt.where(AutomationRun.project_id == project_id)
        rows = sess.execute(
            stmt.order_by(AutomationRun.created_at.desc(),
                          AutomationRun.id.desc()).limit(limit)
        ).scalars().all()
        return [_row_to_dict(r) for r in rows]


def get_automation_run(run_id: int) -> dict | None:
    with session_scope() as sess:
        row = sess.get(AutomationRun, run_id)
        return _row_to_dict(row) if row is not None else None


def latest_automation_run(project_id: str | None = None) -> dict | None:
    runs = list_automation_runs(project_id, limit=1)
    return runs[0] if runs else None


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


# ── Site profiles (Stage 2: site-aware generation) ─────────────────

def save_site_profile(project_id: str, url: str, profile: dict,
                      strategy: dict | None = None) -> int:
    """Upsert the recon profile for (project_id, url).

    A second Generate-from-URL on the same target overwrites the row
    rather than inserting a duplicate — the LLM call is the costly
    bit, the row itself is cheap. ``strategy`` is optional so callers
    can save the profile first and amend the strategy afterwards.
    """
    with session_scope() as sess:
        existing = sess.execute(
            select(SiteProfile).where(
                SiteProfile.project_id == project_id,
                SiteProfile.url == url,
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.profile = profile or {}
            if strategy is not None:
                existing.strategy = strategy
            sess.flush()
            return existing.id
        row = SiteProfile(
            project_id=project_id,
            url=url,
            profile=profile or {},
            strategy=strategy,
        )
        sess.add(row)
        sess.flush()
        return row.id


def load_site_profile_by_url(project_id: str, url: str) -> dict | None:
    with session_scope() as sess:
        row = sess.execute(
            select(SiteProfile).where(
                SiteProfile.project_id == project_id,
                SiteProfile.url == url,
            )
        ).scalar_one_or_none()
        return _row_to_dict(row) if row else None


def list_site_profiles(project_id: str, limit: int = 50) -> list[dict]:
    with session_scope() as sess:
        rows = sess.execute(
            select(SiteProfile)
            .where(SiteProfile.project_id == project_id)
            .order_by(SiteProfile.updated_at.desc()).limit(limit)
        ).scalars().all()
        return [_row_to_dict(r) for r in rows]


# ── Aggregate counts (for /metrics) ────────────────────────────────

# ── PR-D: SessionDraft (review-staging for recorded sessions) ─────


# Time-to-live for a recorded session before the draft self-evicts.
# 24 h matches the plan's spec — long enough for an operator to finish
# coffee + review, short enough that an abandoned recording doesn't
# linger forever. Bumped via the env var only for tests / dev demos.
SESSION_DRAFT_TTL_HOURS = int(
    os.environ.get("SESSION_DRAFT_TTL_HOURS", "24"))

# PR-F Phase 2 — browser-control TTLs. A control session lives up to an
# hour (a long QA session), an individual command 10 minutes (plenty for
# a slow page + the extension's poll interval). Both env-tunable so tests
# and demos can shorten them.
BROWSER_CONTROL_TTL_MINUTES = int(
    os.environ.get("BROWSER_CONTROL_TTL_MINUTES", "60"))
BROWSER_COMMAND_TTL_MINUTES = int(
    os.environ.get("BROWSER_COMMAND_TTL_MINUTES", "10"))
# A control session is "live" (browser attached) if it polled within
# this window. Beyond it, browser_control_status reports stale.
BROWSER_CONTROL_LIVE_SECONDS = int(
    os.environ.get("BROWSER_CONTROL_LIVE_SECONDS", "20"))


def create_session_draft(project_id: str, token: str,
                           proposed_tcs: list[dict],
                           telemetry: dict | None = None) -> int | None:
    """Persist a new draft for later review. Returns the row id.

    ``token`` is the operator-facing handle that appears in the review
    URL — caller mints it (``secrets.token_urlsafe(32)``) so it never
    leaks through the DB random source. ``proposed_tcs`` is the list of
    ``ProposedTC`` dicts that ``session_segmenter`` produced + the
    ``suggested_suite`` field from ``suite_classifier``.

    ``telemetry`` (PR-F) is the optional deep-capture blob the extension
    records via CDP: ``{"network", "console", "dom_snapshots", "meta"}``.
    ``None`` / empty leaves the column '' and the review page hides the
    telemetry panel. The caller is responsible for capping sizes before
    it reaches here.

    Returns ``None`` when the project doesn't exist — keeps the call
    site free of try/except for the FK-violation case.
    """
    if not (project_id and token and isinstance(proposed_tcs, list)):
        return None
    tele_json = ""
    if telemetry:
        try:
            tele_json = json.dumps(telemetry, ensure_ascii=False)
        except (TypeError, ValueError):
            tele_json = ""
    with session_scope() as sess:
        proj = sess.get(Project, project_id)
        if proj is None:
            return None
        now = _utcnow()
        ttl = timedelta(hours=max(1, SESSION_DRAFT_TTL_HOURS))
        row = SessionDraft(
            token=token,
            project_id=project_id,
            proposed_tcs_json=json.dumps(proposed_tcs, ensure_ascii=False),
            telemetry_json=tele_json,
            created_at=now,
            expires_at=now + ttl,
            consumed=False,
        )
        sess.add(row)
        sess.flush()
        return row.id


def get_session_draft(token: str) -> dict | None:
    """Fetch a draft by its public token.

    Returns ``None`` when:
      * token is empty,
      * row doesn't exist,
      * row already consumed,
      * row has expired (the cleanup pass purges them lazily on read).

    The "already consumed" branch returns None to keep the review URL
    single-use even if the operator refreshes the page after Save —
    they get a fresh 404 instead of a confusing double-create.
    """
    if not token:
        return None
    with session_scope() as sess:
        row = sess.execute(
            select(SessionDraft).where(SessionDraft.token == token)
        ).scalar_one_or_none()
        if row is None or row.consumed:
            return None
        # SQLite drops tzinfo on round-trip; Postgres preserves it.
        # Normalise both sides to naive UTC before comparing so the
        # check works under either backend.
        if row.expires_at:
            now = _utcnow()
            exp = row.expires_at
            if exp.tzinfo is None and now.tzinfo is not None:
                now = now.replace(tzinfo=None)
            elif exp.tzinfo is not None and now.tzinfo is None:
                exp = exp.replace(tzinfo=None)
            if exp < now:
                # Lazy cleanup so a never-reviewed draft doesn't
                # survive past its TTL just because no one visited
                # the URL.
                sess.delete(row)
                return None
        try:
            proposed = json.loads(row.proposed_tcs_json or "[]")
        except (ValueError, TypeError):
            proposed = []
        # PR-F — decode the deep-capture blob. getattr guards the window
        # between deploy and migration where the ORM object may lack the
        # attribute (defensive; the ALTER runs on boot so this is rare).
        try:
            tele_raw = getattr(row, "telemetry_json", "") or ""
            telemetry = json.loads(tele_raw) if tele_raw else None
        except (ValueError, TypeError):
            telemetry = None
        return {
            "id": row.id,
            "token": row.token,
            "project_id": row.project_id,
            "proposed_tcs": proposed if isinstance(proposed, list) else [],
            "telemetry": telemetry if isinstance(telemetry, dict) else None,
            "created_at": row.created_at.isoformat() if row.created_at else "",
            "expires_at": row.expires_at.isoformat() if row.expires_at else "",
        }


def list_pending_session_drafts(project_id: str) -> list[dict]:
    """Every still-reviewable draft for a project, newest first.

    "Reviewable" = not consumed and not past ``expires_at``. Used by the
    Test Cases page to surface a "Pending recording sessions" banner so a
    recording whose review tab got closed isn't silently lost. Each item
    carries the token (for the review URL), timestamps, and a count of
    proposed TCs so the banner can label the link without deserialising
    every step. Expired rows are purged lazily on read, mirroring
    :func:`get_session_draft`.
    """
    if not project_id:
        return []
    out: list[dict] = []
    with session_scope() as sess:
        rows = sess.execute(
            select(SessionDraft)
            .where(SessionDraft.project_id == project_id)
            .where(SessionDraft.consumed == False)  # noqa: E712
            .order_by(SessionDraft.created_at.desc())
        ).scalars().all()
        now = _utcnow()
        for row in rows:
            exp = row.expires_at
            if exp is not None:
                this_now = now
                if exp.tzinfo is None and this_now.tzinfo is not None:
                    this_now = this_now.replace(tzinfo=None)
                elif exp.tzinfo is not None and this_now.tzinfo is None:
                    exp = exp.replace(tzinfo=None)
                if exp < this_now:
                    sess.delete(row)
                    continue
            try:
                proposed = json.loads(row.proposed_tcs_json or "[]")
            except (ValueError, TypeError):
                proposed = []
            out.append({
                "token": row.token,
                "tc_count": len(proposed) if isinstance(proposed, list) else 0,
                "created_at": row.created_at.isoformat() if row.created_at else "",
                "expires_at": row.expires_at.isoformat() if row.expires_at else "",
            })
    return out


def consume_session_draft(token: str) -> bool:
    """Mark the draft as consumed so a refresh of the review URL can't
    double-insert TCs. Returns False when the row is missing / already
    consumed — caller surfaces that as 404."""
    if not token:
        return False
    with session_scope() as sess:
        row = sess.execute(
            select(SessionDraft).where(SessionDraft.token == token)
        ).scalar_one_or_none()
        if row is None or row.consumed:
            return False
        row.consumed = True
        return True


def purge_expired_session_drafts() -> int:
    """Sweeper for the snapshot worker (or a manual admin call). Deletes
    every row past its ``expires_at``. Returns count removed."""
    with session_scope() as sess:
        # Load every row then filter in Python — keeps the tz-naive
        # vs tz-aware comparison consistent across SQLite + Postgres
        # without going through the engine-level inspection rabbit
        # hole. The table is small (one row per active recording
        # session, 24h TTL) so the over-fetch is benign.
        rows = sess.execute(select(SessionDraft)).scalars().all()
        removed = 0
        now = _utcnow()
        for r in rows:
            exp = r.expires_at
            if exp is None:
                continue
            this_now = now
            if exp.tzinfo is None and this_now.tzinfo is not None:
                this_now = this_now.replace(tzinfo=None)
            elif exp.tzinfo is not None and this_now.tzinfo is None:
                exp = exp.replace(tzinfo=None)
            if exp < this_now:
                sess.delete(r)
                removed += 1
        return removed


# ── PR-F Phase 2 — browser control queue ──────────────────────────
#
# A DB-backed command queue is the right cross-process channel here: the
# MCP server (the controller) is a separate process from Flask (which the
# extension polls), and both share this database. The controller enqueues
# a command + polls its row for the result; the extension polls Flask for
# pending commands + writes results back. No sockets, no shared memory.

def _dt_is_past(dt, now=None) -> bool:
    """tz-safe 'is dt strictly in the past'. SQLite drops tzinfo on
    round-trip while Postgres keeps it, so normalise both sides."""
    if dt is None:
        return False
    now = now or _utcnow()
    if dt.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    elif dt.tzinfo is not None and now.tzinfo is None:
        dt = dt.replace(tzinfo=None)
    return dt < now


def _seconds_since(dt) -> float | None:
    """Seconds elapsed since ``dt`` (tz-safe), or None when dt is None."""
    if dt is None:
        return None
    now = _utcnow()
    a, b = dt, now
    if a.tzinfo is None and b.tzinfo is not None:
        b = b.replace(tzinfo=None)
    elif a.tzinfo is not None and b.tzinfo is None:
        a = a.replace(tzinfo=None)
    return (b - a).total_seconds()


def create_browser_control_session(project_id: str, token: str) -> int | None:
    """Register a live control session bound to a project. Returns row id,
    or None when the project doesn't exist / args are empty."""
    if not (project_id and token):
        return None
    with session_scope() as sess:
        if sess.get(Project, project_id) is None:
            return None
        now = _utcnow()
        ttl = timedelta(minutes=max(1, BROWSER_CONTROL_TTL_MINUTES))
        row = BrowserControlSession(
            token=token, project_id=project_id,
            created_at=now, expires_at=now + ttl,
            last_seen_at=now, active=True)
        sess.add(row)
        sess.flush()
        return row.id


def get_browser_control_session(token: str) -> dict | None:
    """Resolve a control token → session dict, or None when missing /
    sealed / expired. Includes a computed ``live`` flag (did the browser
    poll within BROWSER_CONTROL_LIVE_SECONDS)."""
    if not token:
        return None
    with session_scope() as sess:
        row = sess.execute(select(BrowserControlSession).where(
            BrowserControlSession.token == token)).scalar_one_or_none()
        if row is None or not row.active:
            return None
        if _dt_is_past(row.expires_at):
            sess.delete(row)
            return None
        age = _seconds_since(row.last_seen_at)
        live = age is not None and age <= max(1, BROWSER_CONTROL_LIVE_SECONDS)
        return {
            "token": row.token,
            "project_id": row.project_id,
            "live": bool(live),
            "last_seen_seconds": age,
            "created_at": row.created_at.isoformat() if row.created_at else "",
            "expires_at": row.expires_at.isoformat() if row.expires_at else "",
        }


def touch_browser_control_session(token: str) -> bool:
    """Bump ``last_seen_at`` — called on every extension poll so the
    controller can tell the browser is still attached. Returns False when
    the session is gone / sealed / expired."""
    if not token:
        return False
    with session_scope() as sess:
        row = sess.execute(select(BrowserControlSession).where(
            BrowserControlSession.token == token)).scalar_one_or_none()
        if row is None or not row.active or _dt_is_past(row.expires_at):
            return False
        row.last_seen_at = _utcnow()
        return True


def stop_browser_control_session(token: str) -> bool:
    """Seal the session (Stop pressed / operator left) and drop its
    still-pending commands so a late poll can't replay them."""
    if not token:
        return False
    with session_scope() as sess:
        row = sess.execute(select(BrowserControlSession).where(
            BrowserControlSession.token == token)).scalar_one_or_none()
        if row is None:
            return False
        row.active = False
        pending = sess.execute(select(BrowserCommand).where(
            BrowserCommand.token == token,
            BrowserCommand.status == "pending")).scalars().all()
        for c in pending:
            c.status = "error"
            c.error = "session_stopped"
            c.done_at = _utcnow()
        return True


# Structured verbs the executor understands. No ``eval`` — arbitrary JS
# is deliberately out of scope (see the Phase 2 design decision).
BROWSER_COMMAND_VERBS = frozenset({
    "navigate", "read_page", "click", "fill", "wait",
})


def enqueue_browser_command(token: str, verb: str,
                             params: dict | None = None) -> str | None:
    """Queue a command for a live control session. Returns a fresh
    ``command_id`` the caller polls on, or None when the token is invalid
    / sealed / expired or the verb is unknown."""
    if not token or verb not in BROWSER_COMMAND_VERBS:
        return None
    try:
        params_json = json.dumps(params or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    with session_scope() as sess:
        sess_row = sess.execute(select(BrowserControlSession).where(
            BrowserControlSession.token == token)).scalar_one_or_none()
        if (sess_row is None or not sess_row.active
                or _dt_is_past(sess_row.expires_at)):
            return None
        now = _utcnow()
        cmd_id = _uuid()
        row = BrowserCommand(
            command_id=cmd_id, token=token, verb=verb,
            params_json=params_json, status="pending",
            created_at=now,
            expires_at=now + timedelta(
                minutes=max(1, BROWSER_COMMAND_TTL_MINUTES)))
        sess.add(row)
        sess.flush()
        return cmd_id


def dequeue_browser_command(token: str) -> dict | None:
    """Atomically hand the oldest pending, unexpired command for a token
    to the caller (marks it ``dispatched``). Returns None when the queue
    is empty. Called by the extension's poll endpoint."""
    if not token:
        return None
    with session_scope() as sess:
        rows = sess.execute(
            select(BrowserCommand)
            .where(BrowserCommand.token == token,
                   BrowserCommand.status == "pending")
            .order_by(BrowserCommand.created_at.asc())
        ).scalars().all()
        for row in rows:
            if _dt_is_past(row.expires_at):
                row.status = "error"
                row.error = "expired"
                row.done_at = _utcnow()
                continue
            row.status = "dispatched"
            row.dispatched_at = _utcnow()
            return {
                "command_id": row.command_id,
                "verb": row.verb,
                "params": json.loads(row.params_json or "{}"),
            }
        return None


def complete_browser_command(command_id: str, ok: bool,
                              result: dict | None = None,
                              error: str = "") -> bool:
    """Write a command's terminal result. Returns False when the command
    is unknown or already terminal (so a duplicate result POST is a
    no-op, not a corruption)."""
    if not command_id:
        return False
    try:
        result_json = json.dumps(result or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        result_json = "{}"
    with session_scope() as sess:
        row = sess.execute(select(BrowserCommand).where(
            BrowserCommand.command_id == command_id)).scalar_one_or_none()
        if row is None or row.status in ("done", "error"):
            return False
        row.status = "done" if ok else "error"
        row.result_json = result_json if ok else "{}"
        row.error = "" if ok else (str(error) or "command_failed")[:500]
        row.done_at = _utcnow()
        return True


def get_browser_command(command_id: str) -> dict | None:
    """Fetch a command's current state — the controller polls this until
    ``status`` is terminal."""
    if not command_id:
        return None
    with session_scope() as sess:
        row = sess.execute(select(BrowserCommand).where(
            BrowserCommand.command_id == command_id)).scalar_one_or_none()
        if row is None:
            return None
        try:
            result = json.loads(row.result_json or "{}")
        except (ValueError, TypeError):
            result = {}
        return {
            "command_id": row.command_id,
            "token": row.token,
            "verb": row.verb,
            "status": row.status,
            "result": result,
            "error": row.error or "",
        }


def purge_expired_browser_control() -> int:
    """Sweep expired control sessions + commands. Returns rows removed."""
    removed = 0
    with session_scope() as sess:
        for model in (BrowserCommand, BrowserControlSession):
            rows = sess.execute(select(model)).scalars().all()
            for r in rows:
                if _dt_is_past(r.expires_at):
                    sess.delete(r)
                    removed += 1
    return removed


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
    "DashboardMetricSnapshot", "TedgieSubmission", "SiteProfile",
    "Locator", "SessionDraft",
    # projects
    "upsert_project", "update_project", "list_projects", "get_project",
    "delete_project", "move_artifacts",
    # test cases
    "save_test_cases", "load_test_cases", "create_test_case",
    # checklist
    "save_checklist", "load_checklist",
    # bugs
    "save_bug", "list_bugs", "count_bugs_by_run", "VALID_BUG_SOURCES",
    # estimation
    "save_estimation", "list_estimations", "list_estimations_by_owner",
    "latest_estimation",
    # execution
    "start_execution_run", "finish_execution_run", "save_case_result",
    "list_execution_runs",
    # dashboard
    "save_metric_snapshot", "list_metric_snapshots",
    # tedgie
    "save_tedgie_submission", "list_tedgie_submissions",
    # site profiles
    "save_site_profile", "load_site_profile_by_url", "list_site_profiles",
    # PR-A locator registry
    "register_locator_candidates", "record_locator_success",
    "record_locator_failure", "get_locator", "list_locators",
    # PR-D session-review drafts
    "create_session_draft", "get_session_draft",
    "list_pending_session_drafts",
    "consume_session_draft", "purge_expired_session_drafts",
    "SESSION_DRAFT_TTL_HOURS",
    # PR-F Phase 2 browser control
    "BrowserControlSession", "BrowserCommand", "BROWSER_COMMAND_VERBS",
    "create_browser_control_session", "get_browser_control_session",
    "touch_browser_control_session", "stop_browser_control_session",
    "enqueue_browser_command", "dequeue_browser_command",
    "complete_browser_command", "get_browser_command",
    "purge_expired_browser_control",
    # aggregates
    "count_records",
]
