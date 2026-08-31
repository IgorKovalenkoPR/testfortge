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
                        create_engine, delete, event, func, select, text,
                        update)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import (DeclarativeBase, Mapped, Session, declared_attr,
                            mapped_column, relationship, sessionmaker)
from sqlalchemy.types import JSON

from engine import public_ids as _public_ids
from engine.log import get_logger

log = get_logger(__name__)

# ── Engine / session factory (lazy) ────────────────────────────────

_engine: Engine | None = None
_Session: sessionmaker | None = None


#: ``scheme://`` at the front of a URL.
_HAS_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)

#: ``user:password@host[:port]/database`` with no scheme — what the Neon and
#: Render consoles show *after* the ``postgresql://`` part, and therefore what
#: ends up in the variable when somebody selects the visible text by hand.
_SCHEMELESS_DSN = re.compile(
    r"^[^\s:/@]+:[^\s@/]*@[^\s:/@]+(:\d+)?/[^\s?]+", re.IGNORECASE)


def redact_url(raw: str) -> str:
    """A connection string safe to put in a log line.

    Everything between the scheme and ``@`` becomes ``***``. This exists
    because of a real leak: on 2026-08-12 a ``DATABASE_URL`` pasted without
    its scheme made SQLAlchemy raise ``ArgumentError``, whose message
    **contains the string it could not parse**. The message went to the log
    verbatim, so the database password was sitting in Render's log viewer —
    and in a screenshot of it. A credential in a log is worse than the
    outage that produced it: the outage ends when the value is fixed, the
    log line stays until the password is rotated.
    """
    if not raw:
        return "(empty)"
    value = raw.strip()
    head, sep, tail = value.partition("@")
    if not sep:
        # No credentials to hide; still keep query parameters out of logs.
        return value.split("?", 1)[0]
    scheme = _HAS_SCHEME.match(head)
    prefix = scheme.group(0) if scheme else ""
    return f"{prefix}***@{tail.split('?', 1)[0]}"


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
    # A scheme-less ``user:pass@host/db``: unambiguous here, because the only
    # non-SQLite backend this app supports is Postgres. Repaired rather than
    # refused, and the warning names what was assumed — the alternative
    # measured itself on 2026-08-12: every page answered 500, /healthz kept
    # answering 200 because it is deliberately DB-free, so Render reported
    # the service Live while nothing worked.
    if not _HAS_SCHEME.match(raw) and _SCHEMELESS_DSN.match(raw):
        log.warning(
            "DATABASE_URL has no scheme; assuming postgresql:// for %s. "
            "The consoles show the string with 'postgresql://' in front — "
            "add it to the variable so this stops being a guess.",
            redact_url(raw))
        return "postgresql+psycopg2://" + raw
    return raw


def _reject_unusable_url(url: str, raw: str) -> None:
    """Refuse a URL SQLAlchemy cannot parse, in operator language.

    Raised **before** ``create_engine`` on purpose: SQLAlchemy's own
    ``ArgumentError`` quotes the whole string, password included, and any
    traceback of it lands in the log. Everything here is redacted.
    """
    if url.startswith("sqlite"):
        return
    value = raw.strip()
    if value.lower().startswith(("psql ", "psql'", 'psql"')):
        raise RuntimeError(
            "DATABASE_URL looks like a psql command rather than a "
            "connection string. Copy only the postgresql://… URI, without "
            "the psql prefix.")
    if value[:1] in {"'", '"'}:
        raise RuntimeError(
            "DATABASE_URL is wrapped in quotes. The dashboard stores the "
            "value literally, so the quotes become part of the host name — "
            "paste the URI on its own.")
    # The scheme is checked on the **normalised** url, not on the raw value:
    # a scheme-less `user:pass@host/db` was already repaired by
    # `_normalize_url`, and the first version of this function tested `raw`
    # here — which refused the very value the repair had just made usable.
    # Caught by running it, not by reading it.
    if not _HAS_SCHEME.match(url):
        raise RuntimeError(
            f"DATABASE_URL has no scheme and does not look like a "
            f"connection string: it begins {value.split(':', 1)[0]!r}. A "
            f"Postgres URI starts with 'postgresql://'.")
    from sqlalchemy.engine import url as _sa_url
    try:
        _sa_url.make_url(url)
    except Exception as exc:
        raise RuntimeError(
            f"DATABASE_URL could not be parsed as a connection string "
            f"({type(exc).__name__}). Redacted value: {redact_url(raw)}"
        ) from None


def database_url() -> str:
    """Resolve the active SQLAlchemy URL.

    Priority:
      1. ``DATABASE_URL`` (Render-style) — used in production.
      2. ``TESTFORTGE_DB`` — explicit local path override.
      3. ``STORAGE_FOLDER/testfortge.db`` — convenient local fallback.
    """
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if url:
        normalised = _normalize_url(url)
        _reject_unusable_url(normalised, url)
        return normalised

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
    _ensure_project_org_column(engine)
    _ensure_invite_emailed_column(engine)
    _ensure_case_result_source_column(engine)
    _ensure_pack_version_columns(engine)
    _ensure_editable_columns(engine)
    # Repair before constraining: the index cannot be created over rows that
    # already collide (E4.4a).
    _renumber_duplicate_public_ids(engine)
    _ensure_public_id_unique_indexes(engine)
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

    # PR-6 bug-sheet columns. Same idempotent probe and NOT NULL DEFAULT
    # idiom; every pre-PR-6 row back-fills to Functional, which is what an
    # un-triaged bug effectively was.
    bug_additions: list[tuple[str, str]] = []
    try:
        bug_existing = {c["name"] for c in insp.get_columns("bug_report")}
    except SQLAlchemyError as exc:
        log.debug("bug_report column probe skipped: %s", exc)
        bug_existing = None
    if bug_existing is not None:
        for col, ddl in (
            ("preconditions",
             "ALTER TABLE bug_report ADD COLUMN preconditions TEXT"),
            ("attachment",
             "ALTER TABLE bug_report ADD COLUMN attachment VARCHAR(500)"),
            ("assignee",
             "ALTER TABLE bug_report ADD COLUMN assignee VARCHAR(120)"),
            ("bug_area",
             "ALTER TABLE bug_report ADD COLUMN bug_area VARCHAR(20) "
             "NOT NULL DEFAULT 'Functional'"),
        ):
            if col not in bug_existing:
                bug_additions.append((col, ddl))

    if not additions and not sd_additions and not cl_additions             and not bug_additions:
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
            for col_name, sql in bug_additions:
                log.info("bug-sheet migration: adding bug_report.%s",
                         col_name)
                conn.execute(text(sql))
    except SQLAlchemyError as exc:  # pragma: no cover — best-effort
        # If the ALTER fails (concurrent boot race, unsupported dialect)
        # the worker process keeps running; reads via the ORM will then
        # surface a clear OperationalError the operator can act on.
        log.warning("walkthrough migration ALTER failed: %s", exc)


#: Editing metadata migrations, as literal statements.
#:
#: Spelled out rather than built with an f-string over a table list, for two
#: reasons. An operator debugging "no such column: row_version" greps for the
#: column name and has to land here. And ``tests/test_schema_migration.py``
#: verifies that every column declared with a ``server_default`` is actually
#: added to an existing database — it does that by reading these statements,
#: which it cannot do if the identifiers only exist at runtime.
_EDITABLE_COLUMN_MIGRATIONS = (
    ("test_case", "row_version",
     "ALTER TABLE test_case ADD COLUMN row_version INTEGER NOT NULL DEFAULT 1"),
    ("test_case", "ai_generated",
     "ALTER TABLE test_case ADD COLUMN ai_generated BOOLEAN NOT NULL DEFAULT '1'"),
    ("test_case", "edited_by",
     "ALTER TABLE test_case ADD COLUMN edited_by VARCHAR(32)"),
    ("test_case", "edited_at",
     "ALTER TABLE test_case ADD COLUMN edited_at "
     "TIMESTAMP WITH TIME ZONE"),
    ("checklist_item", "row_version",
     "ALTER TABLE checklist_item ADD COLUMN row_version INTEGER NOT NULL DEFAULT 1"),
    ("checklist_item", "ai_generated",
     "ALTER TABLE checklist_item ADD COLUMN ai_generated BOOLEAN NOT NULL DEFAULT '1'"),
    ("checklist_item", "edited_by",
     "ALTER TABLE checklist_item ADD COLUMN edited_by VARCHAR(32)"),
    ("checklist_item", "edited_at",
     "ALTER TABLE checklist_item ADD COLUMN edited_at "
     "TIMESTAMP WITH TIME ZONE"),
    ("bug_report", "row_version",
     "ALTER TABLE bug_report ADD COLUMN row_version INTEGER NOT NULL DEFAULT 1"),
    ("bug_report", "ai_generated",
     "ALTER TABLE bug_report ADD COLUMN ai_generated BOOLEAN NOT NULL DEFAULT '1'"),
    ("bug_report", "edited_by",
     "ALTER TABLE bug_report ADD COLUMN edited_by VARCHAR(32)"),
    ("bug_report", "edited_at",
     "ALTER TABLE bug_report ADD COLUMN edited_at "
     "TIMESTAMP WITH TIME ZONE"),
    # ── Estimation (E4.6) ─────────────────────────────────────────
    #
    # The same four, plus one the other entities do not need.
    # ``original_payload`` keeps the result the generator produced, because
    # an estimate is the one artefact whose *previous* value is the
    # interesting one: "the model said 120 h, the lead says 148 h" is a
    # conversation with a client, and E4.6's diff is what makes it possible.
    # The other editors can show a before/after from the audit row; here the
    # before is a whole computed structure, and reconstructing it from a
    # diff would be a second implementation of the estimator.
    ("estimation", "row_version",
     "ALTER TABLE estimation ADD COLUMN row_version INTEGER NOT NULL DEFAULT 1"),
    ("estimation", "ai_generated",
     "ALTER TABLE estimation ADD COLUMN ai_generated BOOLEAN NOT NULL DEFAULT '1'"),
    ("estimation", "edited_by",
     "ALTER TABLE estimation ADD COLUMN edited_by VARCHAR(32)"),
    ("estimation", "edited_at",
     "ALTER TABLE estimation ADD COLUMN edited_at "
     "TIMESTAMP WITH TIME ZONE"),
    ("estimation", "original_payload",
     "ALTER TABLE estimation ADD COLUMN original_payload TEXT"),
    # ── Dashboard (E7.3) ──────────────────────────────────────────
    #
    # ``JSON``, not ``TEXT``, and the difference is Postgres-only. The model
    # declares ``JSON``; SQLAlchemy's JSON type decodes for itself on SQLite
    # and leaves it to the driver on Postgres, where psycopg2 only parses
    # columns whose declared type really is json. An upgraded instance would
    # therefore hand ``get_project_setting`` the *string* ``'{}'`` and the
    # dashboard would raise ``AttributeError: 'str' object has no attribute
    # 'get'`` — on upgraded deployments only, never on a fresh install.
    ("project", "settings",
     "ALTER TABLE project ADD COLUMN settings JSON NOT NULL DEFAULT '{}'"),
    # ``TIMESTAMP WITH TIME ZONE`` on the five datetime columns, matching
    # the ``DateTime(timezone=True)`` the models declare. A bare
    # ``TIMESTAMP`` is ``timestamp without time zone`` on Postgres, so the
    # same column ended up aware on a fresh install and naive on an upgraded
    # one, and ``_row_to_dict`` then serialised it with an offset on one
    # deployment and without on the other. Same class as the boolean and
    # JSON traps above, in a third form; ``tests/test_migration_populated_
    # copy.py`` now guards all three. Instances that already ran the old
    # statement keep the naive column — the ALTER is skipped once the
    # column exists — which is cosmetic today because nothing compares
    # these values, only writes and serialises them.
    #
    # ── Session revocation ────────────────────────────────────────
    #
    # Not editing metadata like the rest of this table, but the same
    # mechanism: an idempotent ADD COLUMN on an existing database. It has to
    # be here rather than left to ``create_all``, because every deployment
    # that already has accounts is exactly the one where revocation matters.
    # Nullable and with no default on purpose — "never revoked" is the
    # normal state and must not read as "revoked at the epoch".
    ("app_user", "sessions_valid_from",
     "ALTER TABLE app_user ADD COLUMN sessions_valid_from "
     "TIMESTAMP WITH TIME ZONE"),
)


def _ensure_editable_columns(engine: Engine) -> None:
    """Idempotent ALTERs for the editing metadata — E4.1.

    ``row_version`` starts at 1 and ``ai_generated`` at true for every
    existing row, and both are the honest reading of the data: nothing has
    been edited yet, because until E4 there was no way to edit it, so every
    row present is generator output a regeneration may legitimately replace.

    Every default above is written **quoted**, matching what
    ``create_all`` emits for the same column. That is not style: Postgres
    reads a bare ``DEFAULT 1`` on a boolean column as an integer and
    refuses the statement, while ``DEFAULT '1'`` is a boolean literal both
    engines accept. Since the failure is caught and logged below, the
    column would simply never appear, and the ORM's next read of
    ``ai_generated`` would fail on an upgraded Postgres instance and
    nowhere else. Exercised by
    ``tests/test_migration_populated_copy.py`` on both engines.
    """
    from sqlalchemy import inspect as _inspect

    present: dict[str, set[str]] = {}
    for table, column, statement in _EDITABLE_COLUMN_MIGRATIONS:
        if table not in present:
            try:
                present[table] = {
                    c["name"] for c in _inspect(engine).get_columns(table)}
            except SQLAlchemyError as exc:
                log.debug("editable-column probe skipped for %s: %s",
                          table, exc)
                present[table] = set()
                continue
        if not present[table] or column in present[table]:
            continue
        try:
            with engine.begin() as conn:
                log.info("editing migration: adding %s.%s", table, column)
                conn.execute(text(statement))
        except SQLAlchemyError as exc:  # pragma: no cover — best-effort
            log.warning("editing migration failed on %s.%s: %s",
                        table, column, exc)


def _ensure_pack_version_columns(engine: Engine) -> None:
    """Idempotent ALTERs for ``project.tc_version`` / ``cl_version`` — E3.5.

    ``create_all`` makes missing tables, never missing columns, so an
    instance that ran before this programme has ``project`` without them and
    the ORM cannot read its own rows.

    Existing projects start at 0, which is right: nobody holds a version for
    them yet, so the first versioned write from any client will conflict
    once and succeed on the reload. Starting them at 0 and having callers
    read the value before writing is what makes that self-correcting rather
    than sticky.
    """
    try:
        from sqlalchemy import inspect as _inspect
        existing = {c["name"] for c in
                    _inspect(engine).get_columns("project")}
    except SQLAlchemyError as exc:
        log.debug("pack-version probe skipped: %s", exc)
        return
    missing = [c for c in ("tc_version", "cl_version") if c not in existing]
    if not missing:
        return
    try:
        with engine.begin() as conn:
            for column in missing:
                log.info("concurrency migration: adding project.%s", column)
                conn.execute(text(
                    f"ALTER TABLE project ADD COLUMN {column} "
                    f"INTEGER NOT NULL DEFAULT 0"))
    except SQLAlchemyError as exc:  # pragma: no cover — best-effort
        log.warning("concurrency migration ALTER failed: %s", exc)


def _renumber_duplicate_public_ids(engine: Engine) -> None:
    """Give already-stored duplicate ids a unique one — E4.4a.

    ``save_*`` now enforces uniqueness on the way in, but rows written before
    that carry duplicates, and the unique index cannot be created over them.
    So the data is repaired first, then the index goes on to keep it that way.

    Renumbering is done per (project, table) and only for ids that actually
    collide: an id that is unique is left alone, because these ids appear in
    exports, in bug reports that cite "failed at CNT_014", and in a client's
    review comments. Within a collision the **first** row by primary key
    keeps the id — it is the one most likely to be the one already cited.

    Row identity is not affected, so a renumbered row keeps its own
    ``row_version`` and ``ai_generated``: the metadata lives on the row, not
    in a table keyed by the public id.

    ``bug_report`` joined the list after E9.7 measured ten people filing at
    once: its ids are minted the same read-then-write way and had never been
    constrained, so any instance that has been in use has duplicates waiting
    for the index below. Its ``project_id`` is nullable — a bug filed
    through Tedgie before a project is chosen has none — hence the
    ``IS NOT NULL`` filter: those rows are outside what a unique index over
    ``(project_id, external_id)`` can constrain on either engine, so
    renumbering them would rewrite ids nobody asked about.
    """
    for table in ("test_case", "checklist_item", "bug_report"):
        try:
            with engine.begin() as conn:
                groups = conn.execute(text(
                    f"SELECT project_id, external_id FROM {table} "
                    f"WHERE external_id IS NOT NULL AND external_id <> '' "
                    f"AND project_id IS NOT NULL "
                    f"GROUP BY project_id, external_id "
                    f"HAVING COUNT(*) > 1"
                )).all()
                if not groups:
                    continue
                # Every id in the project, so a new number cannot land on one
                # that is already in use by a row we are not touching.
                by_project: dict[str, set[str]] = {}
                for project_id, _ in groups:
                    if project_id in by_project:
                        continue
                    by_project[project_id] = {
                        row[0] for row in conn.execute(text(
                            f"SELECT external_id FROM {table} "
                            f"WHERE project_id = :p AND external_id IS NOT NULL"
                        ), {"p": project_id}).all()}

                renamed = 0
                for project_id, external_id in groups:
                    rows = conn.execute(text(
                        f"SELECT id FROM {table} WHERE project_id = :p "
                        f"AND external_id = :e ORDER BY id ASC"
                    ), {"p": project_id, "e": external_id}).all()
                    taken = by_project[project_id]
                    for (row_id,) in rows[1:]:      # the first keeps the id
                        prefix, number = _public_ids.split_id(external_id)
                        if number is None:
                            prefix, number = f"{external_id}_", 0
                        candidate = number + 1
                        new_id = _public_ids.format_id(prefix, candidate)
                        while new_id in taken:
                            candidate += 1
                            new_id = _public_ids.format_id(prefix, candidate)
                        conn.execute(text(
                            f"UPDATE {table} SET external_id = :new "
                            f"WHERE id = :i"), {"new": new_id, "i": row_id})
                        taken.add(new_id)
                        renamed += 1
                        log.info("E4.4a: %s row %s renumbered %s → %s "
                                 "(project %s)", table, row_id, external_id,
                                 new_id, str(project_id)[:8])
                if renamed:
                    log.warning(
                        "E4.4a: renumbered %d duplicate %s id(s). The ids "
                        "themselves changed, so an export or bug report "
                        "citing the old one now points at the row that kept "
                        "it.", renamed, table)
        except SQLAlchemyError as exc:   # pragma: no cover — best effort
            log.warning("could not renumber duplicate %s ids: %s", table, exc)


def _ensure_public_id_unique_indexes(engine: Engine) -> None:
    """Unique ``(project_id, external_id)`` on ``test_case`` — E4.3.

    Creating an item by hand mints its id as "one past the highest" (TC-007).
    Two people pressing the button at the same moment mint the same number,
    and a duplicate public id is not a cosmetic problem: every later edit of
    that id matches two rows, and ``one_or_none()`` raises rather than
    guessing. With this index the collision is an IntegrityError instead,
    which ``engine.editable.create`` retries — the second attempt sees the
    committed row and takes the next number.

    NULL ``external_id`` rows are unaffected: SQLite and Postgres both allow
    repeated NULLs in a unique index, and older generated rows may have one.

    If an instance already holds duplicates the index cannot be created. That
    is logged, loudly and with the offending ids, and the app carries on
    without the guard rather than refusing to boot over historical data.

    ``checklist_item`` is covered too, as of E4.4a. It could not be at first:
    the site-aware generator emitted duplicates — measured on
    ``POST /checklist`` for https://example.com, an 82-item pack containing
    ``CNT_001`` twice — so the index made every checklist save roll back and
    the page rendered empty. Two builders each counted from 1 over their own
    output and the route concatenated the lists.

    That is fixed at the source now: ``save_test_cases`` and
    ``save_checklist`` both run ``public_ids.ensure_unique`` over the pack,
    and ``_renumber_duplicate_public_ids`` repairs rows written before that.
    Both have to hold for this index to be creatable, which is exactly why it
    is worth having — it is the thing that would fail loudly if either
    regressed.

    ``bug_report`` was left out until E9.7, on the grounds that nobody files
    two bugs at the same instant. Ten concurrent testers is exactly the case
    the organisation model invites, and a bug id is the most-cited id in the
    product — "reopening BUG-004" names two findings if two rows answer to
    it. ``save_bug`` retries against this index the way ``editable.create``
    does.
    """
    for table in ("test_case", "checklist_item", "bug_report"):
        index = f"ux_{table}_project_external_id"
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {index} "
                    f"ON {table} (project_id, external_id)"))
        except SQLAlchemyError as exc:
            log.warning(
                "could not create %s: %s. Hand-created %s ids are not "
                "protected against a same-instant collision until the "
                "duplicates below are resolved.", index, exc, table)
            try:
                with engine.begin() as conn:
                    dupes = conn.execute(text(
                        f"SELECT project_id, external_id, COUNT(*) AS n "
                        f"FROM {table} WHERE external_id IS NOT NULL "
                        f"GROUP BY project_id, external_id HAVING n > 1"
                    )).all()
                for project_id, external_id, count in dupes:
                    log.warning("duplicate %s id: project=%s id=%s (%d rows)",
                                table, project_id, external_id, count)
            except SQLAlchemyError:      # pragma: no cover — diagnostics only
                pass


def _ensure_case_result_source_column(engine: Engine) -> None:
    """Idempotent ``ALTER TABLE execution_case_result ADD COLUMN source`` — E3.4.

    ``create_all`` makes missing tables, never missing columns, so an
    instance that ran before this programme has the table without it and
    the ORM would fail to read its own rows.
    """
    try:
        from sqlalchemy import inspect as _inspect
        existing = {c["name"] for c in
                    _inspect(engine).get_columns("execution_case_result")}
    except SQLAlchemyError as exc:
        log.debug("case-result source probe skipped: %s", exc)
        return
    if "source" in existing:
        return
    try:
        with engine.begin() as conn:
            log.info("execution migration: adding execution_case_result.source")
            conn.execute(text(
                "ALTER TABLE execution_case_result ADD COLUMN source "
                "VARCHAR(20) NOT NULL DEFAULT ''"))
    except SQLAlchemyError as exc:  # pragma: no cover — best-effort
        log.warning("execution migration ALTER failed: %s", exc)


def _ensure_project_org_column(engine: Engine) -> None:
    """Idempotent ``ALTER TABLE project ADD COLUMN org_id`` — E2.1.

    Same reason the walkthrough helper above exists: ``create_all`` makes
    missing *tables*, never missing columns, so an instance that booted
    before this programme has a ``project`` table the ORM can no longer
    read once ``Project.org_id`` is declared.

    Added nullable with no default and no foreign key. Nullable because
    every project that exists right now predates organisations and has
    nothing to point at; no FK because SQLite cannot add a constrained
    column to a populated table, so declaring one would give fresh
    installs a check that production does not have — the worst of both,
    since dev would then be unable to reproduce a prod integrity bug.
    Referential integrity for this column is enforced in the helpers
    (:func:`set_project_org`) until a real migration tool arrives.
    """
    try:
        from sqlalchemy import inspect as _inspect
        insp = _inspect(engine)
        existing = {c["name"] for c in insp.get_columns("project")}
    except SQLAlchemyError as exc:
        # No project table yet — fresh install, create_all already made
        # it with org_id present.
        log.debug("project.org_id probe skipped: %s", exc)
        return

    if "org_id" in existing:
        return

    try:
        with engine.begin() as conn:
            log.info("tenancy migration: adding project.org_id")
            conn.execute(text(
                "ALTER TABLE project ADD COLUMN org_id VARCHAR(32)"))
    except SQLAlchemyError as exc:  # pragma: no cover — best-effort
        log.warning("tenancy migration ALTER failed: %s", exc)


def _ensure_invite_emailed_column(engine: Engine) -> None:
    """Idempotent ``ALTER TABLE invite ADD COLUMN emailed_at`` — E0.4.

    Nullable with no default, and NULL is the correct reading for every
    invitation that already exists: they were all handed over by hand,
    because until this epic there was nothing to send them with. So the
    column says something true about history rather than backfilling a
    delivery that never happened — which matters, because it is what
    decides whether an invited address counts as proven.
    """
    try:
        from sqlalchemy import inspect as _inspect
        existing = {c["name"] for c in _inspect(engine).get_columns("invite")}
    except SQLAlchemyError as exc:
        # No invite table yet — fresh install, create_all made it complete.
        log.debug("invite.emailed_at probe skipped: %s", exc)
        return

    if "emailed_at" in existing:
        return

    try:
        with engine.begin() as conn:
            log.info("email migration: adding invite.emailed_at")
            conn.execute(text(
                "ALTER TABLE invite ADD COLUMN emailed_at TIMESTAMP"))
    except SQLAlchemyError as exc:  # pragma: no cover — best-effort
        log.warning("email migration ALTER failed: %s", exc)


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
    # E2.1 — the organisation this project belongs to. Nullable through
    # the whole migration window: every project that exists today was
    # created by an anonymous session and has no org until its owner
    # claims an account (E1.6). Routes must treat NULL as "legacy,
    # owner_sid still governs" rather than "belongs to everyone".
    #
    # No ForeignKey declared here on purpose. The column is added to an
    # existing table by ALTER (see _ensure_project_org_column), and
    # SQLite cannot add a constrained column to a populated table — the
    # constraint would only exist on fresh installs, which is worse than
    # not having it, because it would hide the missing check in dev.
    org_id: Mapped[str | None] = mapped_column(String(32), nullable=True,
                                                index=True)
    # Optimistic-concurrency counters, one per artefact pack (E3.5).
    #
    # ``save_test_cases`` and ``save_checklist`` are wipe-and-replace, so a
    # caller that read a pack, changed one item and wrote it all back would
    # delete whatever a colleague added in between — silently, because
    # replacing a pack looks identical whether or not the copy was stale. A
    # version the caller has to present turns that into a 409.
    #
    # Two counters, not one: a checklist save has no business conflicting
    # with a test-case save. Integers rather than a JSON blob because a
    # counter needs an atomic ``SET v = v + 1 WHERE v = ?``, and that is
    # what makes the check race-free — read-modify-write on JSON could miss
    # a bump and weaken the guard it implements.
    #: Project-level settings — KPI targets today (E7.3). A JSON column
    #: because it is read whole, written whole and never queried across
    #: projects; a table would buy indexing nobody needs.
    settings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict,
                                            server_default="{}")
    tc_version: Mapped[int] = mapped_column(Integer, nullable=False,
                                             default=0, server_default="0")
    cl_version: Mapped[int] = mapped_column(Integer, nullable=False,
                                             default=0, server_default="0")
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
    # ── Editing metadata (E4.1) ───────────────────────────────────
    #
    # ``row_version`` is the per-row half of the optimistic locking E3.5
    # built at pack level. Pack versions are the right unit for a
    # wipe-and-replace save; they are the wrong one for field editing,
    # where two people changing two different items would collide for no
    # reason.
    #
    # ``ai_generated`` is what stops a regeneration silently overwriting
    # someone's work. It starts True because generators are what produce
    # these rows, and a human edit flips it False — see
    # ``engine.editable.patch``. Without it, requirements 4-7 (edit the
    # generated output) turn into a way to lose the edit on the next
    # Generate click.
    row_version: Mapped[int] = mapped_column(Integer, nullable=False,
                                              default=1, server_default="1")
    ai_generated: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                default=True,
                                                server_default="1")
    edited_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
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
    # ── Editing metadata (E4.1) ───────────────────────────────────
    #
    # ``row_version`` is the per-row half of the optimistic locking E3.5
    # built at pack level. Pack versions are the right unit for a
    # wipe-and-replace save; they are the wrong one for field editing,
    # where two people changing two different items would collide for no
    # reason.
    #
    # ``ai_generated`` is what stops a regeneration silently overwriting
    # someone's work. It starts True because generators are what produce
    # these rows, and a human edit flips it False — see
    # ``engine.editable.patch``. Without it, requirements 4-7 (edit the
    # generated output) turn into a way to lose the edit on the next
    # Generate click.
    row_version: Mapped[int] = mapped_column(Integer, nullable=False,
                                              default=1, server_default="1")
    ai_generated: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                default=True,
                                                server_default="1")
    edited_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
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
    # PR-6: the remaining columns of the team's own bug sheet
    # (Training Plan_Horban Yaroslavna.xlsx, "Bugs" tab). Preconditions
    # carry the state and the test data a reproduction needs; Attachment
    # is the evidence link the reference puts on every row; Assignee is
    # who owns the fix.
    preconditions: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment: Mapped[str | None] = mapped_column(String(500), nullable=True)
    assignee: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # WHAT KIND of broken, independent of severity's HOW BADLY. A Critical
    # accessibility defect and a Critical payment defect go to different
    # people. See engine/bug_areas.py.
    bug_area: Mapped[str] = mapped_column(String(20), nullable=False,
                                           default="Functional",
                                           server_default="Functional")
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual",
                                         index=True)
    related_case_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("test_case.id", ondelete="SET NULL"), nullable=True)
    run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("execution_run.id", ondelete="SET NULL"),
        nullable=True, index=True)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # ── Editing metadata (E4.1) ───────────────────────────────────
    #
    # ``row_version`` is the per-row half of the optimistic locking E3.5
    # built at pack level. Pack versions are the right unit for a
    # wipe-and-replace save; they are the wrong one for field editing,
    # where two people changing two different items would collide for no
    # reason.
    #
    # ``ai_generated`` is what stops a regeneration silently overwriting
    # someone's work. It starts True because generators are what produce
    # these rows, and a human edit flips it False — see
    # ``engine.editable.patch``. Without it, requirements 4-7 (edit the
    # generated output) turn into a way to lose the edit on the next
    # Generate click.
    row_version: Mapped[int] = mapped_column(Integer, nullable=False,
                                              default=1, server_default="1")
    ai_generated: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                default=True,
                                                server_default="1")
    edited_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
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
    # ── Editing metadata (E4.6) ───────────────────────────────────
    #
    # The same contract the other three editable entities carry — see
    # TestCase — plus ``original_payload``: the result as the generator
    # computed it, kept so the editor can show "the model said 120 h, you say
    # 148 h". Stored as JSON text rather than a JSON column because it is
    # only ever read and written whole.
    row_version: Mapped[int] = mapped_column(Integer, nullable=False,
                                              default=1, server_default="1")
    ai_generated: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                default=True,
                                                server_default="1")
    edited_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    original_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    # How the verdict was reached: "manual" (a person clicked it),
    # "real_check" / "simulated" (the runner), "auto" (derived).
    #
    # Added in E3.4. Without it a run read back from the database could not
    # say how any of its verdicts were produced — and the first attempt at
    # reading runs from Postgres guessed the value from whether the row had
    # notes, which is exactly the plausible-but-wrong answer that makes a
    # report untrustworthy.
    source: Mapped[str] = mapped_column(String(20), nullable=False,
                                         default="", server_default="")
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


# ── Identity, tenancy, audit (programme epics E0.2 / E1.1 / E2.1) ──
#
# Declared together because they are one dependency cluster: a role
# belongs to a membership, a membership needs a user, and a server-side
# session is what turns a browser into that user for the next request.
# See docs/plans/team_platform_architecture.md §3 for the target shape
# and docs/plans/adr/0001 for why the workspace has to follow.

#: The two roles the owner asked for. ``admin`` creates projects and
#: changes settings; ``user`` does everything else.
ORG_ROLES: tuple[str, ...] = ("admin", "user")

#: Comparable ranks, so a guard can ask for "at least user" without
#: enumerating roles. Kept deliberately small — a third role is a
#: product decision, not a constant somebody adds in passing.
ROLE_RANK: dict[str, int] = {"user": 1, "admin": 2}


def normalize_email(raw: str | None) -> str:
    """Lowercase and strip an email for storage and lookup.

    Uniqueness lives on this normalised form, so ``Bob@Example.com`` and
    ``bob@example.com`` are one account. Without normalising at the only
    door into the table, a user signs up twice and then reports that
    "sign-in with Google made a second empty workspace".

    Note we do *not* touch the local part beyond casing — gmail-style
    dot and ``+tag`` folding is provider-specific, and applying it
    universally would merge two genuinely different addresses.
    """
    return (raw or "").strip().lower()


class ServerSession(Base):
    """One server-side session record — E0.2.

    Replaces the filesystem session store, which lost every logged-in
    user and every in-progress pack whenever the dyno restarted (Render's
    free tier sleeps after ~15 idle minutes, and the filesystem goes with
    it). Rows here outlive the process and are visible to every gunicorn
    worker, which is also what makes E1.5's "sign out on all devices"
    a single DELETE rather than a hunt through a directory.

    ``user_id`` is nullable on purpose: anonymous browsing keeps working
    exactly as it does today, and the column is filled in at sign-in.
    """
    __tablename__ = "server_session"

    sid: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Not a ForeignKey. A session row must survive its user being
    # deleted long enough for the delete to be *audited*; the cleanup is
    # an explicit delete_sessions_for_user() call, not a cascade we
    # cannot observe.
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True,
                                                 index=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)
    accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True)


class User(Base):
    """A human. The identifier the whole platform hangs off.

    Table is ``app_user``, not ``user``: ``user`` is a reserved word in
    Postgres. SQLAlchemy quotes it correctly in generated SQL, but this
    module also issues hand-written ``ALTER TABLE`` statements for
    migrations (see the ``_ensure_*_columns`` helpers), and every one of
    those becomes a quoting trap. Renaming the table costs nothing and
    removes the whole category.

    ``password_hash`` is nullable — a Google-only account has no
    password, and inventing one would be a credential nobody rotates.
    """
    __tablename__ = "app_user"

    id: Mapped[str] = mapped_column(String(32), primary_key=True,
                                     default=_uuid)
    email: Mapped[str] = mapped_column(String(255), nullable=False,
                                        unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(120),
                                                      nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255),
                                                       nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                  default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                             default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # Brute-force counters. Live on the user rather than in a cache so
    # the lockout survives a restart — an in-memory counter on a dyno
    # that sleeps every 15 minutes is not a lockout.
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False,
                                                default=0)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # No session signed in before this instant may act.
    #
    # Ending somebody's sessions used to mean deleting their
    # ``ServerSession`` rows, and those exist only under
    # ``SESSION_BACKEND=db``. On the filesystem backend — the default, and
    # what production runs — there were no rows to delete, so
    # ``delete_sessions_for_user`` returned 0 and every caller that depends
    # on it was ceremony: a password reset left the intruder signed in, and
    # "sign out on all devices" signed nobody out. Measured, not guessed;
    # see ``tests/test_session_revocation.py``.
    #
    # A timestamp on the account works whatever holds the session, because
    # the session carries the instant it was signed in and ``current_user``
    # already re-reads this row on every request.
    sessions_valid_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    identities = relationship("Identity", back_populates="user",
                              cascade="all, delete-orphan",
                              passive_deletes=True)


class Identity(Base):
    """An external sign-in provider bound to a :class:`User`.

    Keyed on ``(provider, subject)`` — the OIDC ``sub`` claim, *not* the
    email. Google documents ``sub`` as the stable identifier and email as
    changeable; keying on email means a user who renames their Google
    account arrives as a stranger.

    ``email`` here is only what the provider last told us, kept for the
    audit trail when an account-linking decision needs explaining.
    """
    id: Mapped[str] = mapped_column(String(32), primary_key=True,
                                     default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)

    user = relationship("User", back_populates="identities")

    __table_args__ = (
        UniqueConstraint("provider", "subject", name="uq_identity_provider_subject"),
    )


class Organization(Base):
    """The team boundary. Projects belong to one; users join via
    :class:`OrgMember`.

    ``settings`` carries the admin-configurable bits (storage target,
    retention window, KPI targets) as JSON rather than a column each,
    because those are product knobs that change every sprint and a
    migration per knob is a tax with no payer.
    """
    id: Mapped[str] = mapped_column(String(32), primary_key=True,
                                     default=_uuid)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(180), nullable=False,
                                       unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)
    settings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class OrgMember(Base):
    """A user's role inside an organisation."""
    id: Mapped[int] = mapped_column(Integer, primary_key=True,
                                     autoincrement=True)
    org_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)
    added_by_user_id: Mapped[str | None] = mapped_column(String(32),
                                                          nullable=True)

    __table_args__ = (
        UniqueConstraint("org_id", "user_id", name="uq_org_member"),
    )


class Invite(Base):
    """A pending invitation to join an organisation at a given role.

    There is deliberately **no** ``UNIQUE (org_id, email)`` constraint,
    though the earlier identity spike proposed one. It would mean a
    person who is invited, joins, and later leaves can never be invited
    back — the used row blocks the seat forever. "One *pending* invite
    per address" is the rule we actually want, and it is enforced in
    :func:`create_invite` (which revokes any live invite for that address
    first) because a partial unique index is Postgres-only and this
    schema also has to build on SQLite.
    """
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    invited_by_user_id: Mapped[str | None] = mapped_column(String(32),
                                                            nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True)
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    #: When this invitation was actually delivered by email (E0.4).
    #:
    #: NULL means it was not — either no provider is configured or the send
    #: failed — and the admin handed the link over some other way. That is
    #: not bookkeeping: it decides whether the address is *proven*. An
    #: invitation that arrived in the inbox can only have been opened by
    #: whoever reads that inbox; a link pasted into a chat proves nothing
    #: about the address at all, and marking such an account
    #: ``email_verified`` would be recording an assumption as a fact.
    #: See :func:`consume_invite`'s caller in ``routes/auth.py``.
    emailed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


#: Purposes a one-time token can serve (E1.3).
AUTH_TOKEN_PURPOSES = ("reset", "verify")

#: How long a password-reset link lives, in minutes.
#:
#: One hour. Short because the link *is* the credential — anyone holding it
#: can take the account — and long enough to survive somebody starting the
#: flow, being interrupted, and coming back to their inbox.
RESET_TTL_MINUTES = 60

#: How long an address-confirmation link lives, in minutes.
#:
#: A day. Longer than a reset because it grants nothing: the holder can
#: confirm an address they already control and nothing else.
VERIFY_TTL_MINUTES = 24 * 60


class AuthToken(Base):
    """A single-use, expiring token for resetting a password or proving an
    address (E1.3).

    Separate from :class:`Invite` on purpose, though the shape is close.
    An invite carries an organisation and a role and creates a membership;
    these carry neither and act on an account that already exists. Folding
    them together would mean a nullable ``org_id`` and a ``role`` that is
    meaningless for two of the three purposes, and every query would have
    to remember which kind of row it was looking at.

    ``sent_at`` is what makes "one email per token" enforceable rather than
    hoped for — see :func:`claim_auth_token_send`.
    """
    __tablename__ = "auth_token"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False,
                                         index=True)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False, index=True)
    #: The address this token was issued for. Held separately from the
    #: user's current address because a ``verify`` token proves *this*
    #: address, and honouring it after the account's address has changed
    #: would confirm something nobody asked about.
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True)
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class OrgSecret(Base):
    """A credential an organisation gave us, encrypted (E0.9).

    Separate table rather than a field in ``Organization.settings``, and
    the reason is not tidiness. ``settings`` is a JSON blob that gets
    read, rendered in an admin form, logged when something goes wrong,
    and included in an export bundle. A ciphertext in there leaks by
    accident eventually. A dedicated table has one reader
    (``engine.llm_keys``), never appears in a ``_row_to_dict`` of
    anything else, and is trivially excluded from exports.

    Only the ciphertext is stored — see ``engine.llm_keys`` for the
    Fernet envelope and why its key is a separate env var from
    ``SECRET_KEY``.
    """
    id: Mapped[int] = mapped_column(Integer, primary_key=True,
                                     autoincrement=True)
    org_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_org_secret"),
    )


class LlmUsage(Base):
    """One LLM call's token counts and cost (E0.7).

    A row per call, not a running total, because the questions that get
    asked are "which module is expensive", "which org", "did that change
    help" — and a counter cannot answer any of them retrospectively.

    Cost is stored as integer **micro-dollars** (1e-6 USD). Floats would
    be fine for one row and wrong for a month of them: summing a hundred
    thousand rounded floats drifts, and this number ends up in front of
    whoever pays.

    ``key_source`` records who paid — ``org`` (the team's own BYOK key) or
    ``platform`` (the operator's). Without it a total cannot be attributed,
    and the whole point of BYOK is that most of the bill is not ours.
    """
    id: Mapped[int] = mapped_column(Integer, primary_key=True,
                                     autoincrement=True)
    org_id: Mapped[str | None] = mapped_column(String(32), nullable=True,
                                                index=True)
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True,
                                                    index=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    key_source: Mapped[str] = mapped_column(String(16), nullable=False,
                                             default="platform")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False,
                                               default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False,
                                                default=0)
    # Cache reads are billed at 10% of the input rate and cache writes at
    # 125%, so they have to be counted separately or the cost is wrong in
    # both directions at once.
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False,
                                                    default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False,
                                                     default=0)
    cost_micros: Mapped[int] = mapped_column(Integer, nullable=False,
                                              default=0)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                          default=_utcnow, index=True)


class AuditLog(Base):
    """Who changed what, when — E2.7.

    Supersedes the trick of appending a line to a bug's ``comment``
    field, which was cheap and worked for one entity but is unfilterable
    and destroys the data it annotates. Every mutation from the editing
    substrate (E4.1) and every role change writes a row here.

    Nullable ``org_id`` / ``project_id`` / ``user_id`` so the table can
    also record events with no project (a sign-in) or no user (a
    system-initiated retention delete).
    """
    id: Mapped[int] = mapped_column(Integer, primary_key=True,
                                     autoincrement=True)
    org_id: Mapped[str | None] = mapped_column(String(32), nullable=True,
                                                index=True)
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True,
                                                    index=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True,
                                                 index=True)
    entity: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    diff: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                          default=_utcnow, index=True)


# ── Identity / tenancy helpers ─────────────────────────────────────

def create_user(email: str, *, display_name: str | None = None,
                password_hash: str | None = None,
                email_verified: bool = False) -> str | None:
    """Create a user, returning the new id — or ``None`` if the email is
    already taken.

    ``None`` rather than an exception because "this address already has
    an account" is an ordinary branch at every call site (sign-up,
    invite-claim, first Google sign-in), not an error. The uniqueness
    check is the DB constraint, caught below, so two concurrent sign-ups
    for the same address cannot both win.
    """
    email = normalize_email(email)
    if not email:
        return None
    with session_scope() as sess:
        row = User(
            email=email,
            display_name=(display_name or "").strip() or None,
            password_hash=password_hash,
            email_verified=bool(email_verified),
        )
        sess.add(row)
        try:
            sess.flush()
        except IntegrityError:
            sess.rollback()
            return None
        return row.id


def get_user(user_id: str | None) -> dict | None:
    if not user_id:
        return None
    with session_scope() as sess:
        row = sess.get(User, user_id)
        return _row_to_dict(row) if row else None


def get_user_by_email(email: str | None) -> dict | None:
    email = normalize_email(email)
    if not email:
        return None
    with session_scope() as sess:
        row = sess.query(User).filter(User.email == email).one_or_none()
        return _row_to_dict(row) if row else None


def set_password_hash(user_id: str, password_hash: str) -> bool:
    """Store a new password hash. Used by sign-up, reset, and the
    on-login rehash when cost parameters change."""
    if not (user_id and password_hash):
        return False
    with session_scope() as sess:
        row = sess.get(User, user_id)
        if row is None:
            return False
        row.password_hash = password_hash
        return True


def mark_email_verified(user_id: str, email: str | None = None) -> bool:
    """Record that this account's address is proven.

    *email* is an optional guard, and the reason it exists is E1.3: a
    ``verify`` token proves the address it was **issued for**. If the
    account's address has changed since the link was sent, honouring the
    link would mark the new, unproven address as confirmed — the one thing
    the flag must never say falsely. Mismatch refuses rather than confirming
    the wrong thing.
    """
    if not user_id:
        return False
    with session_scope() as sess:
        row = sess.get(User, user_id)
        if row is None:
            return False
        if email is not None and \
                normalize_email(row.email) != normalize_email(email):
            log.info("verify refused for %s: the address changed since the "
                     "link was issued", user_id[:8])
            return False
        row.email_verified = True
        return True


def touch_last_login(user_id: str) -> bool:
    if not user_id:
        return False
    with session_scope() as sess:
        row = sess.get(User, user_id)
        if row is None:
            return False
        row.last_login_at = _utcnow()
        return True


def bump_login_failure(user_id: str) -> int:
    """Increment the failed-login counter and return the new value.

    Incremented with a SQL expression rather than read-modify-write. Two
    concurrent wrong-password attempts — which is precisely the shape of
    an attack — would otherwise both read the same value and each write
    n+1, so the counter would advance by one instead of two and the
    lockout threshold would take twice as many attempts to reach.
    """
    if not user_id:
        return 0
    with session_scope() as sess:
        sess.execute(
            update(User)
            .where(User.id == user_id)
            .values(failed_logins=User.failed_logins + 1)
        )
        sess.flush()
        return int(sess.query(User.failed_logins).filter(
            User.id == user_id).scalar() or 0)


def clear_login_failures(user_id: str) -> bool:
    """Reset the counters after a successful login."""
    if not user_id:
        return False
    with session_scope() as sess:
        row = sess.get(User, user_id)
        if row is None:
            return False
        row.failed_logins = 0
        row.locked_until = None
        row.last_login_at = _utcnow()
        return True


def lock_user(user_id: str, until: datetime) -> bool:
    if not (user_id and until):
        return False
    with session_scope() as sess:
        row = sess.get(User, user_id)
        if row is None:
            return False
        row.locked_until = until
        return True


def set_user_active(user_id: str, active: bool) -> bool:
    """Deactivate or reactivate an account.

    Deactivation rather than deletion is the default for a departing
    colleague: their name still has to resolve on every test case they
    wrote and every bug they filed, and a deleted row turns an audit
    trail into a list of orphaned ids.
    """
    if not user_id:
        return False
    with session_scope() as sess:
        row = sess.get(User, user_id)
        if row is None:
            return False
        row.is_active = bool(active)
        return True


def link_identity(user_id: str, provider: str, subject: str,
                  email: str | None = None) -> bool:
    """Bind an external provider identity to an existing user.

    Idempotent: linking the same ``(provider, subject)`` to the same user
    twice is a no-op returning True. Linking it to a *different* user
    returns False — that is an account-takeover attempt or a bug, and
    silently re-pointing the row would hand one person's workspace to
    another.
    """
    if not (user_id and provider and subject):
        return False
    with session_scope() as sess:
        existing = sess.query(Identity).filter(
            Identity.provider == provider,
            Identity.subject == subject,
        ).one_or_none()
        if existing is not None:
            if existing.user_id != user_id:
                log.warning(
                    "identity %s:%s already bound to a different user",
                    provider, subject[:12])
                return False
            return True
        sess.add(Identity(user_id=user_id, provider=provider,
                          subject=subject,
                          email=normalize_email(email) or None))
        try:
            sess.flush()
        except IntegrityError:
            sess.rollback()
            return False
        return True


def get_user_by_identity(provider: str, subject: str) -> dict | None:
    """Look up a user by external identity — the Google sign-in path."""
    if not (provider and subject):
        return None
    with session_scope() as sess:
        ident = sess.query(Identity).filter(
            Identity.provider == provider,
            Identity.subject == subject,
        ).one_or_none()
        if ident is None:
            return None
        row = sess.get(User, ident.user_id)
        return _row_to_dict(row) if row else None


def create_organization(name: str, *, settings: dict | None = None) -> str:
    """Create an org with a unique slug derived from *name*."""
    name = (name or "").strip() or "Team"
    base = _slugify(name, fallback="team")[:160]
    with session_scope() as sess:
        slug = base
        # Two teams may legitimately be called "QA" — disambiguate rather
        # than reject, since the name is a label and the slug is plumbing.
        if sess.query(Organization).filter(
                Organization.slug == slug).one_or_none() is not None:
            slug = f"{base}-{_uuid()[:8]}"
        row = Organization(name=name, slug=slug, settings=settings or {})
        sess.add(row)
        sess.flush()
        return row.id


def get_organization(org_id: str | None) -> dict | None:
    if not org_id:
        return None
    with session_scope() as sess:
        row = sess.get(Organization, org_id)
        return _row_to_dict(row) if row else None


def update_org_settings(org_id: str, patch: dict) -> bool:
    """Merge *patch* into the org's settings blob.

    Merge rather than replace, because the settings blob is shared by
    several unrelated features (storage target, retention window, LLM
    budget, KPI targets). A settings form that PUTs the whole object
    silently drops whatever the form did not render — and the symptom
    lands on a different feature than the one that was edited.
    """
    if not (org_id and isinstance(patch, dict)):
        return False
    with session_scope() as sess:
        row = sess.get(Organization, org_id)
        if row is None:
            return False
        merged = dict(row.settings or {})
        merged.update(patch)
        # Reassign rather than mutate: SQLAlchemy does not track in-place
        # changes to a JSON column, so mutating row.settings would look
        # like it worked and write nothing.
        row.settings = merged
        return True


def add_org_member(org_id: str, user_id: str, role: str, *,
                   added_by_user_id: str | None = None) -> bool:
    """Add or update a membership. Returns False on an unknown role.

    Re-adding an existing member updates their role, which is what the
    members page needs for "change role" — one code path instead of two.
    """
    if role not in ORG_ROLES:
        log.warning("add_org_member: unknown role %r", role)
        return False
    if not (org_id and user_id):
        return False
    with session_scope() as sess:
        existing = sess.query(OrgMember).filter(
            OrgMember.org_id == org_id,
            OrgMember.user_id == user_id,
        ).one_or_none()
        if existing is not None:
            existing.role = role
            return True
        sess.add(OrgMember(org_id=org_id, user_id=user_id, role=role,
                           added_by_user_id=added_by_user_id))
        try:
            sess.flush()
        except IntegrityError:
            sess.rollback()
            return False
        return True


def get_org_role(org_id: str | None, user_id: str | None) -> str | None:
    """The user's role in the org, or ``None`` when they are not a member.

    ``None`` means no access. Callers must not treat it as a default
    role — that is how a stranger becomes a tester.
    """
    if not (org_id and user_id):
        return None
    with session_scope() as sess:
        return sess.query(OrgMember.role).filter(
            OrgMember.org_id == org_id,
            OrgMember.user_id == user_id,
        ).scalar()


def list_org_members(org_id: str) -> list[dict]:
    """Members with their email and display name, admins first."""
    if not org_id:
        return []
    with session_scope() as sess:
        rows = sess.query(OrgMember, User).join(
            User, User.id == OrgMember.user_id,
        ).filter(OrgMember.org_id == org_id).all()
        out = [{
            "user_id": m.user_id,
            "email": u.email,
            "display_name": u.display_name,
            "role": m.role,
            "added_at": m.added_at.isoformat() if m.added_at else "",
            "is_active": bool(u.is_active),
        } for m, u in rows]
        out.sort(key=lambda r: (-ROLE_RANK.get(r["role"], 0), r["email"]))
        return out


def count_users() -> int:
    """How many accounts exist at all.

    The input to the first-admin bootstrap (``engine.bootstrap``): with
    authentication on, an account can only be created by claiming an
    invitation, and an invitation can only be issued by an admin — so a
    database with zero users has no way to acquire its first one. This is
    the cheapest possible question to ask before minting one.
    """
    with session_scope() as sess:
        return int(sess.query(func.count(User.id)).scalar() or 0)


def count_org_admins(org_id: str) -> int:
    """How many admins the org has — the last-admin guard's input.

    Split out as its own query so the guard is a cheap COUNT rather than
    loading the whole member list on every role change.
    """
    if not org_id:
        return 0
    with session_scope() as sess:
        return int(sess.query(func.count(OrgMember.id)).filter(
            OrgMember.org_id == org_id,
            OrgMember.role == "admin",
        ).scalar() or 0)


def change_org_role(org_id: str, user_id: str, role: str) -> bool:
    """Change a member's role, refusing to demote the last admin.

    Separate from :func:`add_org_member` precisely because of that guard.
    ``remove_org_member`` has always had it, but demotion is the same
    failure with a different verb: an org whose only admin becomes a plain
    user has nobody who can create a project, change settings, or promote
    anyone back — and no way to recover without a DBA. The two doors to
    that state need the same lock.
    """
    if role not in ORG_ROLES:
        log.warning("change_org_role: unknown role %r", role)
        return False
    if not (org_id and user_id):
        return False
    with session_scope() as sess:
        row = sess.query(OrgMember).filter(
            OrgMember.org_id == org_id,
            OrgMember.user_id == user_id,
        ).one_or_none()
        if row is None:
            return False
        if row.role == role:
            return True                     # idempotent
        if row.role == "admin" and role != "admin":
            admins = int(sess.query(func.count(OrgMember.id)).filter(
                OrgMember.org_id == org_id,
                OrgMember.role == "admin",
            ).scalar() or 0)
            if admins <= 1:
                return False
        row.role = role
        return True


def remove_org_member(org_id: str, user_id: str) -> bool:
    """Remove a membership, refusing to strand the org without an admin.

    Returns False when the target is the only admin. The caller turns
    that into the 400 + "promote someone first" message; enforcing it
    here means no route can forget.
    """
    if not (org_id and user_id):
        return False
    with session_scope() as sess:
        row = sess.query(OrgMember).filter(
            OrgMember.org_id == org_id,
            OrgMember.user_id == user_id,
        ).one_or_none()
        if row is None:
            return False
        if row.role == "admin":
            admins = int(sess.query(func.count(OrgMember.id)).filter(
                OrgMember.org_id == org_id,
                OrgMember.role == "admin",
            ).scalar() or 0)
            if admins <= 1:
                return False
        sess.delete(row)
        return True


def set_project_org(project_id: str, org_id: str) -> bool:
    """Attach a project to an org, verifying the org exists first.

    Stands in for the foreign key the column cannot carry (see
    :func:`_ensure_project_org_column`) — without this check a typo'd
    org_id would produce a project no membership query can ever reach,
    i.e. silently orphaned data.
    """
    if not (project_id and org_id):
        return False
    with session_scope() as sess:
        if sess.get(Organization, org_id) is None:
            log.warning("set_project_org: no such org %s", org_id[:8])
            return False
        proj = sess.get(Project, project_id)
        if proj is None:
            return False
        proj.org_id = org_id
        return True


def list_projects_for_org(org_id: str) -> list[dict]:
    """The organisation's projects, in the shape templates expect.

    A thin alias over :func:`list_projects` — it exists because the call
    sites read better for it, not because it does anything else. Notably it
    must **not** grow its own query: doing that once already produced rows
    without the ``folder`` / ``saved_at`` back-compat keys and a 500 on the
    dashboard.
    """
    if not org_id:
        return []
    return list_projects(org_id=org_id)


# ── Invites ────────────────────────────────────────────────────────

#: How long an invite stays claimable. Long enough to survive a weekend
#: and a forgotten inbox, short enough that a leaked link in a chat
#: history is not a permanent back door.
INVITE_TTL_HOURS = 168  # 7 days


def create_invite(org_id: str, email: str, role: str, token: str, *,
                  invited_by_user_id: str | None = None,
                  ttl_hours: int | None = None) -> bool:
    """Issue an invite, revoking any still-live invite for that address.

    *token* is minted by the caller (``secrets.token_urlsafe(32)``) so the
    secret never comes from the database's random source and never has to
    be read back out.

    The revoke-first step is what enforces "one pending invite per
    address" in place of a unique constraint the schema deliberately does
    not carry — see :class:`Invite`. It also means re-inviting someone
    invalidates the older link, so a forwarded stale email cannot be used
    to join at a role that was since downgraded.
    """
    email = normalize_email(email)
    if not (org_id and email and token) or role not in ORG_ROLES:
        return False
    now = _utcnow()
    with session_scope() as sess:
        if sess.get(Organization, org_id) is None:
            return False
        live = sess.query(Invite).filter(
            Invite.org_id == org_id,
            Invite.email == email,
            Invite.used_at.is_(None),
            Invite.revoked_at.is_(None),
            Invite.expires_at > now,
        ).all()
        for old in live:
            old.revoked_at = now
        ttl = timedelta(hours=max(1, ttl_hours or INVITE_TTL_HOURS))
        sess.add(Invite(
            token=token, org_id=org_id, email=email, role=role,
            invited_by_user_id=invited_by_user_id,
            created_at=now, expires_at=now + ttl,
        ))
        try:
            sess.flush()
        except IntegrityError:
            sess.rollback()
            return False
        return True


def mark_invite_emailed(token: str) -> bool:
    """Record that this invitation was actually delivered by email (E0.4).

    Called only after the provider accepted the message. The column is what
    decides whether the invited address counts as *proven* when the account
    is created — see ``Invite.emailed_at``.
    """
    if not token:
        return False
    with session_scope() as sess:
        claimed = sess.execute(
            update(Invite)
            .where(Invite.token == token, Invite.emailed_at.is_(None))
            .values(emailed_at=_utcnow())
            .execution_options(synchronize_session=False))
        return bool(claimed.rowcount)


# ── One-time tokens for reset and verify (E1.3) ────────────────────

def create_auth_token(purpose: str, user_id: str, email: str, token: str, *,
                      ttl_minutes: int | None = None) -> bool:
    """Issue a one-time token, revoking any live one of the same purpose.

    *token* is minted by the caller (``secrets.token_urlsafe(32)``), the
    same arrangement :func:`create_invite` uses: the secret never comes from
    the database's random source and never has to be read back out.

    Revoke-first is what makes "one live reset link per person" true, and it
    matters more here than for invites. Asking for a reset twice is what
    people do when the first message is slow, and leaving both links armed
    means the older one — the one more likely to be sitting in a forwarded
    mail or a shared inbox — still opens the account.
    """
    email = normalize_email(email)
    if purpose not in AUTH_TOKEN_PURPOSES or not (user_id and email and token):
        return False
    now = _utcnow()
    default_ttl = (RESET_TTL_MINUTES if purpose == "reset"
                   else VERIFY_TTL_MINUTES)
    with session_scope() as sess:
        if sess.get(User, user_id) is None:
            return False
        sess.execute(
            update(AuthToken)
            .where(AuthToken.user_id == user_id,
                   AuthToken.purpose == purpose,
                   AuthToken.used_at.is_(None),
                   AuthToken.revoked_at.is_(None))
            .values(revoked_at=now)
            .execution_options(synchronize_session=False))
        ttl = timedelta(minutes=max(1, ttl_minutes or default_ttl))
        sess.add(AuthToken(token=token, purpose=purpose, user_id=user_id,
                           email=email, created_at=now,
                           expires_at=now + ttl))
        try:
            sess.flush()
        except IntegrityError:
            sess.rollback()
            return False
        return True


def get_auth_token(token: str, purpose: str | None = None) -> dict | None:
    """The token's row if it is claimable **right now**, else ``None``.

    One answer for never-existed, already-used, revoked and expired. A
    caller cannot tell them apart because there is nothing useful to do
    with the difference and plenty to do with it if you are guessing tokens
    at the endpoint: "expired" confirms the token was real, and a real
    token means a real account.
    """
    if not token:
        return None
    now = _utcnow()
    with session_scope() as sess:
        row = sess.get(AuthToken, token)
        if row is None:
            return None
        if purpose is not None and row.purpose != purpose:
            return None
        if row.used_at is not None or row.revoked_at is not None:
            return None
        expires = row.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires is not None and expires <= now:
            return None
        return _row_to_dict(row)


def claim_auth_token_send(token: str) -> bool:
    """Take the right to send this token's one email. ``False`` if taken.

    The acceptance criterion for E1.3 is that a message is never sent twice
    for one token, and a double-submitted form is the ordinary way that
    happens — somebody clicks "email me a link", nothing appears to move, so
    they click again. Reading ``sent_at`` and then setting it would let both
    requests through under Postgres's READ COMMITTED, where each reads NULL
    before either writes; one conditional UPDATE cannot be interleaved.

    Claimed *before* the provider is called, not after, and the ordering is
    deliberate: a send that happens twice has spent two of a hundred daily
    messages and put a second live credential in an inbox, while a claim
    that is never followed by a send costs the user one retry.
    """
    if not token:
        return False
    with session_scope() as sess:
        claimed = sess.execute(
            update(AuthToken)
            .where(AuthToken.token == token, AuthToken.sent_at.is_(None))
            .values(sent_at=_utcnow())
            .execution_options(synchronize_session=False))
        return bool(claimed.rowcount)


def consume_auth_token(token: str, purpose: str) -> dict | None:
    """Redeem a one-time token. Returns its row, or ``None`` if unclaimable.

    **Claimed by one conditional UPDATE**, the same shape
    :func:`consume_invite` uses and for the same reason: a link that travels
    by email is a link two people can open at the same moment, and a
    read-then-write hands the account to both of them. ``rowcount == 0``
    means somebody else got there first, which is indistinguishable — on
    purpose — from expired and from never having existed.
    """
    if not (token and purpose in AUTH_TOKEN_PURPOSES):
        return None
    now = _utcnow()
    with session_scope() as sess:
        row = sess.get(AuthToken, token)
        if row is None or row.purpose != purpose:
            return None
        claimed = sess.execute(
            update(AuthToken)
            .where(AuthToken.token == token,
                   AuthToken.purpose == purpose,
                   AuthToken.used_at.is_(None),
                   AuthToken.revoked_at.is_(None),
                   AuthToken.expires_at > now)
            .values(used_at=now)
            .execution_options(synchronize_session=False))
        if not claimed.rowcount:
            return None
        return {"token": row.token, "purpose": row.purpose,
                "user_id": row.user_id, "email": row.email}


def revoke_auth_tokens(user_id: str, purpose: str | None = None) -> int:
    """Kill this person's live tokens. Returns how many.

    Called when a password changes: any reset link still in flight was
    issued to whoever asked, and after a successful reset the account is
    already in someone's hands. Leaving a spare key under the mat because
    it was cut before the lock changed is not a thing to do.
    """
    if not user_id:
        return 0
    with session_scope() as sess:
        stmt = (update(AuthToken)
                .where(AuthToken.user_id == user_id,
                       AuthToken.used_at.is_(None),
                       AuthToken.revoked_at.is_(None)))
        if purpose is not None:
            stmt = stmt.where(AuthToken.purpose == purpose)
        result = sess.execute(
            stmt.values(revoked_at=_utcnow())
            .execution_options(synchronize_session=False))
        return int(result.rowcount or 0)


def purge_expired_auth_tokens(*, older_than_hours: int = 24 * 30) -> int:
    """Delete tokens long past use. Returns how many rows went.

    Kept for a month after expiry rather than deleted on expiry, so that
    "this link stopped working" can still be answered from the audit trail
    while the row is small and the table stays bounded.
    """
    cutoff = _utcnow() - timedelta(hours=max(1, older_than_hours))
    with session_scope() as sess:
        result = sess.execute(
            delete(AuthToken).where(AuthToken.expires_at < cutoff))
        return int(result.rowcount or 0)


def get_invite(token: str) -> dict | None:
    """Return a *claimable* invite, or ``None``.

    Used, revoked and expired invites all read as ``None`` — the caller
    gets one answer to "may this token be redeemed" and cannot
    accidentally branch on a stale row's fields.
    """
    if not token:
        return None
    with session_scope() as sess:
        row = sess.get(Invite, token)
        if row is None:
            return None
        if row.used_at is not None or row.revoked_at is not None:
            return None
        if _dt_is_past(row.expires_at):
            return None
        return _row_to_dict(row)


def consume_invite(token: str, user_id: str) -> str | None:
    """Redeem an invite: mark it used and add the membership.

    Returns the org id on success, ``None`` if the token is not
    claimable. Both writes happen in one transaction, so a crash cannot
    leave a burned token with no membership behind it — which would lock
    the invitee out with no way to retry, and would look to the admin
    like the invitation had been accepted.

    **The token is claimed by one conditional UPDATE**, not by reading
    ``used_at`` and then setting it. An invitation link travels by email
    and email gets forwarded, so two people opening it at the same moment
    is ordinary rather than exotic — and read-then-write hands the seat to
    both of them under Postgres's READ COMMITTED, where each transaction
    reads NULL before either writes. SQLite hides that by serialising
    writers, which is precisely why it had to be reasoned about rather
    than tested for locally. ``rowcount == 0`` means somebody else got
    there first, and it is the same shape ``_claim_pack_write`` uses for
    the same reason.

    Claimed *before* the membership is written, so the two orderings agree
    about failure: if the membership insert then fails, the whole
    transaction — claim included — rolls back and the link still works.
    """
    if not (token and user_id):
        return None
    now = _utcnow()
    with session_scope() as sess:
        row = sess.get(Invite, token)
        if row is None or row.role not in ORG_ROLES:
            return None
        org_id, role = row.org_id, row.role
        invited_by = row.invited_by_user_id
        claimed = sess.execute(
            update(Invite)
            .where(Invite.token == token,
                   Invite.used_at.is_(None),
                   Invite.revoked_at.is_(None),
                   Invite.expires_at > now)
            .values(used_at=now)
            .execution_options(synchronize_session=False))
        if not claimed.rowcount:
            # Used, revoked or expired — one answer for all three, as
            # everywhere else this token is inspected.
            return None
        member = sess.query(OrgMember).filter(
            OrgMember.org_id == org_id,
            OrgMember.user_id == user_id,
        ).one_or_none()
        if member is None:
            sess.add(OrgMember(org_id=org_id, user_id=user_id, role=role,
                               added_by_user_id=invited_by))
        else:
            member.role = role
        return org_id


def revoke_invites_for_email(org_id: str, email: str) -> int:
    """Cancel every live invitation for *email* in *org_id*.

    Keyed on the address rather than the token so the members page can
    offer a cancel button without rendering a token that is a bearer
    credential for somebody else's seat. Scoped to one organisation, so an
    admin of one team cannot cancel another team's invitations.

    Returns how many rows were revoked — normally 0 or 1, since
    :func:`create_invite` already revokes older ones, but written as a
    count so a historical duplicate cannot survive the cancel.
    """
    email = normalize_email(email)
    if not (org_id and email):
        return 0
    now = _utcnow()
    with session_scope() as sess:
        rows = sess.query(Invite).filter(
            Invite.org_id == org_id,
            Invite.email == email,
            Invite.used_at.is_(None),
            Invite.revoked_at.is_(None),
            Invite.expires_at > now,
        ).all()
        for row in rows:
            row.revoked_at = now
        return len(rows)


def revoke_invite(token: str) -> bool:
    if not token:
        return False
    with session_scope() as sess:
        row = sess.get(Invite, token)
        if row is None or row.used_at is not None:
            return False
        row.revoked_at = _utcnow()
        return True


def list_pending_invites(org_id: str) -> list[dict]:
    if not org_id:
        return []
    now = _utcnow()
    with session_scope() as sess:
        rows = sess.query(Invite).filter(
            Invite.org_id == org_id,
            Invite.used_at.is_(None),
            Invite.revoked_at.is_(None),
            Invite.expires_at > now,
        ).order_by(Invite.created_at.desc()).all()
        # Token deliberately omitted: this list renders on the members
        # page, and a pending invite's token is a bearer credential for
        # somebody else's seat.
        return [{k: v for k, v in _row_to_dict(r).items() if k != "token"}
                for r in rows]


# ── Org secrets (E0.9) ─────────────────────────────────────────────
#
# Only ``engine.llm_keys`` should call these. The ciphertext is
# deliberately not reachable through any list_* / _row_to_dict path that
# feeds a template, an export or a log line.

def set_org_secret(org_id: str, name: str, ciphertext: str) -> bool:
    if not (org_id and name and ciphertext):
        return False
    with session_scope() as sess:
        if sess.get(Organization, org_id) is None:
            return False
        row = sess.query(OrgSecret).filter(
            OrgSecret.org_id == org_id, OrgSecret.name == name,
        ).one_or_none()
        if row is None:
            sess.add(OrgSecret(org_id=org_id, name=name,
                               ciphertext=ciphertext))
        else:
            row.ciphertext = ciphertext
        return True


def get_org_secret(org_id: str, name: str) -> str | None:
    if not (org_id and name):
        return None
    with session_scope() as sess:
        return sess.query(OrgSecret.ciphertext).filter(
            OrgSecret.org_id == org_id, OrgSecret.name == name,
        ).scalar()


def delete_org_secret(org_id: str, name: str) -> bool:
    if not (org_id and name):
        return False
    with session_scope() as sess:
        row = sess.query(OrgSecret).filter(
            OrgSecret.org_id == org_id, OrgSecret.name == name,
        ).one_or_none()
        if row is None:
            return False
        sess.delete(row)
        return True


def has_org_secret(org_id: str, name: str) -> bool:
    """Whether a secret exists, without decrypting or returning it.

    What the settings page needs in order to render "a key is configured"
    without the ciphertext ever leaving this module.
    """
    return bool(get_org_secret(org_id, name))


# ── LLM usage accounting (E0.7) ────────────────────────────────────

def record_llm_usage(*, kind: str, model: str,
                     org_id: str | None = None,
                     project_id: str | None = None,
                     user_id: str | None = None,
                     key_source: str = "platform",
                     input_tokens: int = 0, output_tokens: int = 0,
                     cache_read_tokens: int = 0,
                     cache_write_tokens: int = 0,
                     cost_micros: int = 0) -> int | None:
    """Record one call. Never raises — metering must not break a feature.

    Keyword-only for the same reason :func:`append_audit` is: five id-ish
    strings and five integers next to each other are trivially
    transposable positionally, and silently mis-attributed spend is worse
    than none.
    """
    if not (kind and model):
        return None
    try:
        with session_scope() as sess:
            row = LlmUsage(
                kind=kind, model=model, org_id=org_id,
                project_id=project_id, user_id=user_id,
                key_source=key_source,
                input_tokens=max(0, int(input_tokens or 0)),
                output_tokens=max(0, int(output_tokens or 0)),
                cache_read_tokens=max(0, int(cache_read_tokens or 0)),
                cache_write_tokens=max(0, int(cache_write_tokens or 0)),
                cost_micros=max(0, int(cost_micros or 0)),
            )
            sess.add(row)
            sess.flush()
            return row.id
    except SQLAlchemyError as exc:
        log.warning("record_llm_usage failed (%s/%s): %s", kind, model, exc)
        return None


def _month_start(now: datetime | None = None) -> datetime:
    now = now or _utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def org_spend_micros(org_id: str, *, since: datetime | None = None,
                     key_source: str | None = "platform") -> int:
    """Spend for *org_id* since *since* (default: start of this month).

    ``key_source`` defaults to ``"platform"`` on purpose. The budget guard
    exists to cap what the *operator* pays; an org spending its own BYOK
    key's money is none of the platform's business and must not be
    throttled. Pass ``None`` to total everything for a usage report.
    """
    if not org_id:
        return 0
    since = since or _month_start()
    try:
        with session_scope() as sess:
            q = sess.query(func.coalesce(func.sum(LlmUsage.cost_micros), 0)) \
                .filter(LlmUsage.org_id == org_id, LlmUsage.at >= since)
            if key_source:
                q = q.filter(LlmUsage.key_source == key_source)
            return int(q.scalar() or 0)
    except SQLAlchemyError as exc:
        # Fail *open*: a metering outage must not lock every org out of
        # generation. The budget is a cost guard, not a security control.
        log.warning("org_spend_micros failed for %s: %s", org_id[:8], exc)
        return 0


def llm_usage_summary(org_id: str, *, since: datetime | None = None) -> dict:
    """Per-kind and per-model totals for an org — the usage report."""
    since = since or _month_start()
    out: dict = {"since": since.isoformat(), "by_kind": {},
                 "by_model": {}, "total_micros": 0, "calls": 0}
    if not org_id:
        return out
    try:
        with session_scope() as sess:
            rows = sess.query(
                LlmUsage.kind, LlmUsage.model,
                func.count(LlmUsage.id), func.sum(LlmUsage.cost_micros),
                func.sum(LlmUsage.input_tokens),
                func.sum(LlmUsage.output_tokens),
            ).filter(
                LlmUsage.org_id == org_id, LlmUsage.at >= since,
            ).group_by(LlmUsage.kind, LlmUsage.model).all()
    except SQLAlchemyError as exc:
        log.warning("llm_usage_summary failed for %s: %s", org_id[:8], exc)
        return out

    for kind, model, calls, cost, tin, tout in rows:
        cost = int(cost or 0)
        calls = int(calls or 0)
        k = out["by_kind"].setdefault(
            kind, {"calls": 0, "cost_micros": 0,
                   "input_tokens": 0, "output_tokens": 0})
        k["calls"] += calls
        k["cost_micros"] += cost
        k["input_tokens"] += int(tin or 0)
        k["output_tokens"] += int(tout or 0)
        m = out["by_model"].setdefault(model, {"calls": 0, "cost_micros": 0})
        m["calls"] += calls
        m["cost_micros"] += cost
        out["total_micros"] += cost
        out["calls"] += calls
    return out


def purge_llm_usage(older_than_days: int = 400) -> int:
    """Drop usage rows past their retention window.

    A row per call adds up, and the free database has a 0.5 GB cap for
    everything (see docs/plans/cost_model.md). 400 days keeps a full
    year of month-on-month comparison and nothing beyond it.
    """
    cutoff = _utcnow() - timedelta(days=max(1, older_than_days))
    with session_scope() as sess:
        rows = sess.query(LlmUsage).filter(LlmUsage.at < cutoff).all()
        for row in rows:
            sess.delete(row)
        return len(rows)


# ── Audit log ──────────────────────────────────────────────────────

def append_audit(*, entity: str, action: str,
                 user_id: str | None = None,
                 org_id: str | None = None,
                 project_id: str | None = None,
                 entity_id: str | None = None,
                 diff: dict | None = None) -> int | None:
    """Record one mutation. Never raises — auditing must not be the
    reason a user's edit fails.

    Keyword-only on purpose: the five id-ish string parameters are
    trivially transposable positionally, and an audit trail that
    attributes edits to the wrong project is worse than none.
    """
    if not (entity and action):
        return None
    try:
        with session_scope() as sess:
            row = AuditLog(entity=entity, action=action, user_id=user_id,
                           org_id=org_id, project_id=project_id,
                           entity_id=str(entity_id) if entity_id else None,
                           diff=diff)
            sess.add(row)
            sess.flush()
            return row.id
    except SQLAlchemyError as exc:
        log.warning("append_audit failed (%s %s): %s", entity, action, exc)
        return None


def list_audit(*, org_id: str | None = None, project_id: str | None = None,
               entity: str | None = None, entity_id: str | None = None,
               limit: int = 100) -> list[dict]:
    """Most-recent-first audit rows for the given scope."""
    limit = max(1, min(int(limit or 100), 1000))
    with session_scope() as sess:
        q = sess.query(AuditLog)
        if org_id:
            q = q.filter(AuditLog.org_id == org_id)
        if project_id:
            q = q.filter(AuditLog.project_id == project_id)
        if entity:
            q = q.filter(AuditLog.entity == entity)
        if entity_id:
            q = q.filter(AuditLog.entity_id == str(entity_id))
        rows = q.order_by(AuditLog.at.desc()).limit(limit).all()
        return [_row_to_dict(r) for r in rows]


def count_audit_since(*, entity: str, action: str, since: datetime) -> int:
    """How many matching events were recorded after *since*.

    Exists for ``engine.mailer``'s daily quota (E0.4). The audit trail is
    the meter as well as the record: a second table counting the same
    events would be one more thing to migrate and one more place for the
    two numbers to disagree, and ``AuditLog.at`` is already indexed.
    """
    if not (entity and action):
        return 0
    with session_scope() as sess:
        return int(sess.execute(
            select(func.count()).select_from(AuditLog)
            .where(AuditLog.entity == entity, AuditLog.action == action,
                   AuditLog.at >= since)
        ).scalar() or 0)


# ── Server-side session store (E0.2) ───────────────────────────────

#: A session used within this window of its deadline gets pushed forward.
#: This is what makes expiry *sliding* — an active user is never logged
#: out mid-task — while keeping the extension out of the write path on
#: every request. Read side owns it so exactly one place decides whether
#: a session is still alive.
SESSION_SLIDE_WITHIN = timedelta(hours=24)

#: How far forward a slide pushes the deadline when the caller does not
#: say. The session interface passes its own configured lifetime; this
#: default only covers direct callers (tests, maintenance scripts).
SESSION_DEFAULT_LIFETIME = timedelta(days=14)


def session_load(sid: str, *, lifetime: timedelta | None = None) -> str | None:
    """Return a live session's payload, or ``None`` if absent/expired.

    Touches ``accessed_at`` so an idle-timeout policy (E1.5) has
    something to read, and slides ``expires_at`` forward when the row is
    within :data:`SESSION_SLIDE_WITHIN` of expiring. Both writes are
    deliberately part of the read: a separate "touch" call is the kind of
    thing one code path forgets, and the symptom — a session that expires
    under an actively working user — is close to impossible to reproduce
    on purpose.
    """
    if not sid:
        return None
    with session_scope() as sess:
        row = sess.get(ServerSession, sid)
        if row is None:
            return None
        if _dt_is_past(row.expires_at):
            # Drop it here rather than waiting for the vacuum, so a
            # replayed cookie cannot resurrect anything.
            sess.delete(row)
            return None
        now = _utcnow()
        row.accessed_at = now
        exp = row.expires_at
        if exp is not None:
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if (exp - now) < SESSION_SLIDE_WITHIN:
                row.expires_at = now + (lifetime or SESSION_DEFAULT_LIFETIME)
        return row.payload


def _apply_session_update(sess, row, payload: str, expires_at: datetime,
                          user_id: str | None, now: datetime) -> None:
    row.payload = payload
    row.expires_at = expires_at
    row.accessed_at = now
    # Only ever set, never cleared, by a save: signing out is an explicit
    # delete, and a background write that happened to carry no user must
    # not silently de-authenticate the tab.
    if user_id:
        row.user_id = user_id


def session_save(sid: str, payload: str, expires_at: datetime, *,
                 user_id: str | None = None) -> bool:
    """Upsert a session row.

    The INSERT is retried as an UPDATE on a uniqueness violation, because
    get-then-insert races itself. One page load fires a dozen parallel
    requests for assets and fetches, all carrying the same brand-new sid;
    each sees no row and each inserts. Observed as
    ``UNIQUE constraint failed: server_session.sid`` on the very first
    page load of a signed-in session, which cost that request its session
    write — and with the session holding the working pack, that is lost
    work rather than a stray log line.
    """
    if not sid:
        return False
    now = _utcnow()
    with session_scope() as sess:
        row = sess.get(ServerSession, sid)
        if row is not None:
            _apply_session_update(sess, row, payload, expires_at, user_id, now)
            return True
        sess.add(ServerSession(sid=sid, payload=payload,
                               user_id=user_id, created_at=now,
                               accessed_at=now, expires_at=expires_at))
        try:
            sess.flush()
        except IntegrityError:
            # Somebody else inserted it between our read and our write.
            # Their row is as good as ours would have been; merge into it.
            sess.rollback()
            row = sess.get(ServerSession, sid)
            if row is None:  # pragma: no cover — vanished again, give up
                log.warning("session %s… could not be written", sid[:8])
                return False
            _apply_session_update(sess, row, payload, expires_at, user_id, now)
        return True


def session_delete(sid: str) -> bool:
    if not sid:
        return False
    with session_scope() as sess:
        row = sess.get(ServerSession, sid)
        if row is None:
            return False
        sess.delete(row)
        return True


def delete_sessions_for_user(user_id: str, *, except_sid: str | None = None) -> int:
    """Invalidate every session belonging to *user_id*.

    This is "sign out on all devices", and the same call a password reset
    must make — a reset that leaves the attacker's existing session alive
    has not recovered the account. ``except_sid`` lets the user who
    initiated it stay signed in where they are.
    """
    if not user_id:
        return 0
    with session_scope() as sess:
        q = sess.query(ServerSession).filter(
            ServerSession.user_id == user_id)
        if except_sid:
            q = q.filter(ServerSession.sid != except_sid)
        rows = q.all()
        for row in rows:
            sess.delete(row)
        # And the account's own cut-off, which is what makes this work on a
        # backend that has no rows to delete. Deleting rows stays: it frees
        # the storage and it stops a replayed cookie from resolving at all,
        # which is a stronger property than being refused after it resolves.
        # ``except_sid`` cannot be honoured by a timestamp, which cannot
        # name one session. No caller needs it to be: the only one that
        # passes it is ``permissions.logout_user``, which has already
        # cleared its own session dict by then — the argument spares it a
        # row it was discarding anyway. A future caller that really must
        # survive its own sweep has to re-stamp its session afterwards.
        user = sess.get(User, user_id)
        if user is not None:
            user.sessions_valid_from = _utcnow()
        return len(rows)


def purge_expired_sessions() -> int:
    """Delete expired session rows. Called from the same periodic sweep
    as the other purge helpers."""
    now = _utcnow()
    with session_scope() as sess:
        rows = sess.query(ServerSession).filter(
            ServerSession.expires_at <= now).all()
        for row in rows:
            sess.delete(row)
        return len(rows)


# ── Helpers (slug, dict serialisation) ─────────────────────────────

class WriteConflict(RuntimeError):
    """The pack changed between the caller reading it and writing it back.

    Carries what was expected and what was found, so a route can tell the
    user how stale their copy is rather than only that it is.
    """

    def __init__(self, kind: str, expected: int, actual: int):
        self.kind = kind
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"{kind} pack was at version {actual}, caller had {expected}")


#: Pack kind → the column holding its version counter.
_PACK_VERSION_COLUMN = {"test_cases": "tc_version", "checklist": "cl_version"}


def pack_versions(project_id: str) -> dict[str, int]:
    """Each artefact pack's current version. Zeroes for an unknown project."""
    out = {kind: 0 for kind in _PACK_VERSION_COLUMN}
    if not project_id:
        return out
    with session_scope() as sess:
        row = sess.get(Project, project_id)
        if row is None:
            return out
        out["test_cases"] = int(row.tc_version or 0)
        out["checklist"] = int(row.cl_version or 0)
        return out


#: The last merge each pack kind performed, for the route to report.
#:
#: A module-level slot rather than a return value because ``save_*`` returns a
#: row count that a dozen callers already read, and changing that signature to
#: carry a report would touch all of them to serve one. Read immediately after
#: the call, in the same request — see ``take_merge_report``.
_LAST_MERGE_REPORT: dict = {}


def _protect_edits(project_id: str, incoming: list, kind: str, policy: str):
    """Apply the regeneration policy. Returns ``(rows, report)``."""
    from engine import regeneration

    loader = load_test_cases if kind == "test_cases" else load_checklist
    try:
        existing = loader(project_id) or []
        metadata = load_edit_metadata(project_id, kind) or {}
    except SQLAlchemyError as exc:      # pragma: no cover — read failure
        # Cannot see the existing pack, so cannot know what to protect.
        # Writing the incoming pack would silently discard edits, so the safe
        # answer is to refuse the write and say why.
        raise RuntimeError(
            f"cannot check {kind} for manual edits before regenerating: {exc}"
        ) from exc
    return regeneration.merge(existing, incoming, metadata, policy=policy)


def take_merge_report(kind: str):
    """The report from the most recent protected write of ``kind``, once.

    Popped rather than read, so a later unprotected write cannot make the
    route repeat a message about a merge that did not happen this time.
    """
    return _LAST_MERGE_REPORT.pop(kind, None)


class UserSetting(Base):
    """One person's preference — dashboard layout today (E7.2).

    Keyed on a free-text ``owner`` rather than a foreign key to ``app_user``,
    because a preference has to work in both eras: a signed-in user id when
    ``AUTH_ENABLED`` is on, and the session id otherwise. A foreign key would
    make the anonymous case impossible to store, which is the case most
    installations are in today.

    Not on ``app_user`` as a JSON column for the same reason.
    """
    owner: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


def get_user_setting(owner: str, key: str):
    """One person's stored preference, or ``None``."""
    if not owner or not key:
        return None
    with session_scope() as sess:
        row = sess.get(UserSetting, (str(owner)[:64], key))
        return (row.value or {}).get("v") if row is not None else None


def set_user_setting(owner: str, key: str, value) -> None:
    """Store it, wrapped so a list or a scalar round-trips through JSON."""
    if not owner or not key:
        return
    owner = str(owner)[:64]
    with session_scope() as sess:
        row = sess.get(UserSetting, (owner, key))
        if row is None:
            sess.add(UserSetting(owner=owner, key=key, value={"v": value}))
        else:
            row.value = {"v": value}


def get_project_setting(project_id: str, key: str):
    """A project-level setting out of ``project.settings`` (E7.3)."""
    if not project_id or not key:
        return None
    with session_scope() as sess:
        row = sess.get(Project, project_id)
        if row is None:
            return None
        return (getattr(row, "settings", None) or {}).get(key)


def set_project_setting(project_id: str, key: str, value) -> None:
    if not project_id or not key:
        return
    with session_scope() as sess:
        row = sess.get(Project, project_id)
        if row is None:
            raise ValueError(f"no such project: {project_id}")
        # Re-bound rather than mutated: SQLAlchemy's change detection on a
        # JSON column does not see an in-place dict update reliably across
        # backends, and a silently unsaved setting is the kind of bug people
        # blame on the browser.
        settings = dict(getattr(row, "settings", None) or {})
        settings[key] = value
        row.settings = settings


def bump_pack_version(project_id: str, kind: str) -> None:
    """Mark a pack as changed because a row was added or deleted (E4.3).

    Field edits deliberately do *not* touch this — that is what the per-row
    version is for. Adding or removing an item is different: a
    wipe-and-replace save that started before it would reinstate a deleted
    case or drop a new one, and the pack version is what turns that into a
    409 instead.
    """
    if not project_id or kind not in _PACK_VERSION_COLUMN:
        return
    with session_scope() as sess:
        _claim_pack_write(sess, project_id, kind, None)


def _claim_pack_write(sess, project_id: str, kind: str,
                      expected_version: int | None) -> None:
    """Bump a pack's version, refusing a stale write.

    One conditional UPDATE, so the check and the bump cannot be interleaved
    by another writer — which is what a read-then-compare gets wrong exactly
    when it matters. ``rowcount == 0`` means somebody else got there first.

    ``expected_version=None`` bumps unconditionally. Deliberate rather than a
    loophole: generating a pack is an intentional replacement, and every
    pre-E3.5 caller passes nothing and has to keep working.
    """
    column = _PACK_VERSION_COLUMN[kind]
    if expected_version is None:
        sess.execute(
            update(Project).where(Project.id == project_id)
            .values(**{column: getattr(Project, column) + 1}))
        return
    result = sess.execute(
        update(Project)
        .where(Project.id == project_id,
               getattr(Project, column) == int(expected_version))
        .values(**{column: getattr(Project, column) + 1}))
    if result.rowcount:
        return
    # Distinguish "someone else wrote" from "no such project": the first is
    # a conflict a reload resolves, the second is not.
    actual = sess.query(getattr(Project, column)).filter(
        Project.id == project_id).scalar()
    if actual is None:
        raise ValueError(f"no such project: {project_id}")
    raise WriteConflict(kind, int(expected_version), int(actual))


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
                   owner_sid: str | None = None,
                   org_id: str | None = None) -> str:
    """Create-or-return-existing by (owner_sid, slug).

    ``org_id`` was missing entirely until the suite was first run with
    ``ORG_MODE=1``, and its absence was not a cosmetic gap: nothing wrote
    the column, ``visible_projects`` lists **only** the caller's
    organisation's projects when org mode is on, and the result was that a
    project became invisible to the person who had just created it. The
    flag is off in production, so nobody had met it — the listing side of
    E2 shipped without the writing side.

    An existing row is adopted rather than left alone when it has no
    organisation yet. That is the migration path: a project created before
    the flag went on belongs to whoever is working in it now, and refusing
    to stamp it would leave it permanently unreachable. A row that already
    names a *different* organisation is never moved here — that would be a
    silent transfer between teams, and it is what
    :func:`adopt_orphan_projects` exists to do deliberately.
    """
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
            if org_id and not existing.org_id:
                existing.org_id = org_id
            return existing.id

        proj = Project(
            id=_uuid(),
            name=name,
            slug=slug,
            base_url=base_url,
            description=description,
            owner_sid=owner_sid,
            org_id=org_id,
        )
        sess.add(proj)
        sess.flush()
        return proj.id


def adopt_orphan_projects(org_id: str, *, only_when_sole_org: bool = True) -> int:
    """Attach projects with no organisation to *org_id*. Returns the count.

    The migration when ``ORG_MODE`` is switched on: every project that
    exists at that moment has ``org_id = NULL``, because nothing ever wrote
    the column, and the moment the flag goes on they all disappear from
    every listing. Doing nothing is therefore not a neutral option.

    ``only_when_sole_org`` refuses the sweep when more than one
    organisation exists, and that refusal is the point. With several teams
    on one deployment there is no way to tell whose an orphan project is,
    and guessing would hand one team's work to another. In that case the
    operator has to say, project by project — a slower answer and the only
    honest one.
    """
    if not org_id:
        return 0
    with session_scope() as sess:
        if only_when_sole_org:
            org_count = sess.execute(
                select(func.count()).select_from(Organization)
            ).scalar() or 0
            if org_count > 1:
                log.warning(
                    "adopt_orphan_projects: %d organisations exist — refusing "
                    "to guess which owns the unassigned projects", org_count)
                return 0
        rows = sess.execute(
            select(Project).where(Project.org_id.is_(None))
        ).scalars().all()
        for row in rows:
            row.org_id = org_id
        if rows:
            log.info("adopted %d unassigned project(s) into org %s",
                     len(rows), org_id[:8])
        return len(rows)


#: How many orphan names :func:`orphan_project_survey` carries back.
#:
#: The count is the number that matters; the names are there so an admin
#: can recognise the work before signing for it, and so the refusal below
#: names what has to be dealt with project by project. Bounded because a
#: deployment with three hundred pre-flag projects would otherwise render
#: three hundred rows into a settings page.
ORPHAN_PREVIEW_LIMIT = 25


def orphan_project_survey() -> dict:
    """What :func:`adopt_orphan_projects` would do, before it does it.

    Exists because that function answers ``0`` to two different questions —
    "there was nothing to adopt" and "I refuse to guess between several
    organisations" — and a screen that renders both as a silent zero tells
    an admin their projects are gone rather than that a decision is
    waiting for them. The sweep's return value is deliberately unchanged;
    this reads the same two facts up front instead.

    Keys: ``count`` of unassigned projects, ``names`` (at most
    :data:`ORPHAN_PREVIEW_LIMIT` of them, newest first), ``organisations``
    in existence, and ``ambiguous`` — true when the sweep will refuse.
    """
    with session_scope() as sess:
        count = sess.execute(
            select(func.count()).select_from(Project)
            .where(Project.org_id.is_(None))
        ).scalar() or 0
        names = list(sess.execute(
            select(Project.name).where(Project.org_id.is_(None))
            .order_by(Project.updated_at.desc())
            .limit(ORPHAN_PREVIEW_LIMIT)
        ).scalars().all())
        orgs = sess.execute(
            select(func.count()).select_from(Organization)
        ).scalar() or 0
    return {
        "count": int(count),
        "names": names,
        "organisations": int(orgs),
        "ambiguous": int(orgs) > 1,
    }


def list_projects(owner_sid: str | None = None,
                  org_id: str | None = None) -> list[dict]:
    """Projects, newest first, with per-project artefact counts.

    ``org_id`` filters by organisation and ``owner_sid`` by anonymous
    session — the two eras of ownership. Both are parameters on this one
    function rather than a second ``list_projects_for_org``, because the
    shaping below is what templates depend on: the first attempt at a
    separate org-scoped query returned raw rows and the dashboard 500'd on
    ``p.saved_at``, a back-compat key only this code path adds.
    """
    with session_scope() as sess:
        stmt = select(Project).order_by(Project.updated_at.desc())
        if org_id:
            stmt = stmt.where(Project.org_id == org_id)
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

#: Columns a wipe-and-replace pack write must carry across, keyed by the
#: row's public id.
#:
#: ``save_test_cases`` and ``save_checklist`` delete and re-insert, which
#: resets every column not present in the incoming dicts — and the incoming
#: dicts come from ``load_*``, which strips to the dataclass fields. So a
#: pack write silently reverted ``ai_generated`` to True and ``row_version``
#: to 1 for rows a human had just edited. That is not a cosmetic loss: it is
#: the flag E4.7's regeneration guard reads, so losing it means the next
#: Generate click quietly discards the edit.
#:
#: Both inline editors and every upload go through a pack write, so without
#: this the provenance E4.1 records would survive until roughly the next
#: click.
_EDIT_METADATA_COLUMNS = ("row_version", "ai_generated", "edited_by",
                          "edited_at")


def _capture_edit_metadata(sess, model, project_id: str) -> dict[str, dict]:
    """Snapshot the edit metadata of a project's rows, by public id."""
    rows = sess.query(model).filter(model.project_id == project_id).all()
    out: dict[str, dict] = {}
    for row in rows:
        key = getattr(row, "external_id", None)
        if not key:
            continue
        out[key] = {c: getattr(row, c, None) for c in _EDIT_METADATA_COLUMNS}
    return out


def _restore_edit_metadata(row, snapshot: dict[str, dict]) -> None:
    """Put a row's previous edit metadata back, if it had any.

    Matched on the public id. What a *regeneration* should do with a
    preserved ``ai_generated=False`` is E4.7's decision, not this
    function's — its job is only to stop the metadata evaporating on an
    ordinary read-modify-write.
    """
    previous = snapshot.get(getattr(row, "external_id", None) or "")
    if not previous:
        return
    for column, value in previous.items():
        if value is not None:
            setattr(row, column, value)


def save_test_cases(project_id: str, test_cases: list, *,
                    expected_version: int | None = None,
                    protect_edits: bool = False,
                    policy: str = "merge") -> int:
    """Replace all TC for a project with the new list. Returns rows written.

    Bumps ``project.updated_at`` so the picker dropdown shows
    recently-touched projects at the top.

    ``expected_version`` opts into optimistic concurrency: pass the value
    :func:`pack_versions` reported when the pack was read, and a write that
    lost a race raises :class:`WriteConflict` instead of quietly deleting
    the winner's rows. Omit it for an intentional replacement.
    """
    if not project_id:
        raise ValueError("project_id is required")
    # E4.7. Opt-in, so every pre-existing caller behaves exactly as before:
    # only the Generate paths ask for it. A reorder or an upload writes the
    # rows it was given.
    if protect_edits:
        test_cases, _LAST_MERGE_REPORT["test_cases"] = _protect_edits(
            project_id, test_cases, "test_cases", policy)
    # Same guarantee as the checklist (E4.4a), and here it is load-bearing:
    # the unique index on (project_id, external_id) turns a duplicate into a
    # rolled-back INSERT, which stores *nothing*. Losing one id to
    # renumbering is a small cost; losing the pack is not.
    _public_ids.ensure_unique(test_cases, fallback_prefix="TC-")
    written = 0
    with session_scope() as sess:
        # Before the delete below, and in the same transaction: a conflict
        # has to abort the whole write, not leave the pack half-replaced.
        _claim_pack_write(sess, project_id, "test_cases", expected_version)
        # Bump updated_at on the parent project so list_projects()
        # surfaces this row to the top of the dropdown.
        proj = sess.get(Project, project_id)
        if proj is not None:
            proj.updated_at = _utcnow()
        # Wipe-and-replace keeps semantics simple — caller treats DB as
        # the single source of truth for the *current* TC set. The edit
        # metadata is carried across explicitly, because a delete would
        # otherwise revert it to the column defaults.
        edits = _capture_edit_metadata(sess, TestCase, project_id)
        sess.query(TestCase).filter(TestCase.project_id == project_id).delete()
        for tc in test_cases or []:
            d = tc if isinstance(tc, dict) else getattr(tc, "__dict__", {})
            row = TestCase(
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
            )
            _restore_edit_metadata(row, edits)
            sess.add(row)
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


def load_edit_metadata(project_id: str, kind: str = "test_cases") -> dict:
    """``{"TC-004": {"row_version": 3, "ai_generated": False, …}}`` (E4.3).

    ``load_test_cases`` strips everything the in-session dataclass does not
    declare, which includes the edit metadata — so a page rendering editable
    fields has no version to send and no way to show which rows a person has
    touched. One query beside the pack rather than a per-row lookup: a
    project with 200 cases would otherwise open 200 connections to render.
    """
    models = {"test_cases": TestCase, "checklist": ChecklistItem,
              "bugs": BugReport}
    model = models.get(kind)
    if not project_id or model is None:
        return {}
    out: dict[str, dict] = {}
    with session_scope() as sess:
        rows = sess.execute(
            select(model.external_id, model.row_version, model.ai_generated,
                   model.edited_by, model.edited_at)
            .where(model.project_id == project_id)
        ).all()
        for external_id, version, ai_generated, edited_by, edited_at in rows:
            if not external_id:
                # A row with no public id cannot be addressed by the edit
                # endpoint anyway (it is keyed on external_id), so offering
                # a version for it would invite a 404.
                continue
            out[str(external_id)] = {
                "row_version": int(version or 1),
                "ai_generated": bool(ai_generated),
                "edited_by": edited_by,
                "edited_at": edited_at.isoformat() if edited_at else None,
            }
    return out


# ── Checklist ──────────────────────────────────────────────────────

def save_checklist(project_id: str, items: list, *,
                   expected_version: int | None = None,
                   protect_edits: bool = False,
                   policy: str = "merge") -> int:
    """Replace all checklist items for a project. Returns rows written.

    ``expected_version`` behaves exactly as in :func:`save_test_cases` —
    see there for why a stale write has to be refused rather than merged.
    """
    if not project_id:
        raise ValueError("project_id is required")
    # E4.7 — see save_test_cases.
    if protect_edits:
        items, _LAST_MERGE_REPORT["checklist"] = _protect_edits(
            project_id, items, "checklist", policy)
    # E4.4a. Two builders each counting from 1 over their own output, with a
    # route that concatenates the lists, produced duplicate public ids —
    # measured: an 82-item pack with CNT_001 twice. Every editor addresses a
    # row by this id, so a duplicate makes the row unaddressable. Enforced
    # here rather than in the builders because eight write paths reach this
    # function, and more builders than that feed them.
    _public_ids.ensure_unique(items, fallback_prefix="CL_")
    written = 0
    with session_scope() as sess:
        _claim_pack_write(sess, project_id, "checklist", expected_version)
        # Bump project recency for the picker dropdown.
        proj = sess.get(Project, project_id)
        if proj is not None:
            proj.updated_at = _utcnow()
        edits = _capture_edit_metadata(sess, ChecklistItem, project_id)
        sess.query(ChecklistItem).filter(
            ChecklistItem.project_id == project_id).delete()
        for cl in items or []:
            d = cl if isinstance(cl, dict) else getattr(cl, "__dict__", {})
            row = ChecklistItem(
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
            )
            _restore_edit_metadata(row, edits)
            sess.add(row)
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


def _resolve_bug_area(bug: dict) -> str:
    """The quality attribute a bug belongs to. See engine.bug_areas."""
    try:
        from engine import bug_areas
        return bug_areas.resolve_area(bug)
    except Exception:  # pragma: no cover — never block a bug write
        return "Functional"


#: How many times :func:`save_bug` may re-mint a colliding public id.
#:
#: Not three, which is what ``editable.create`` uses and what the first
#: version of this copied. The failure is a herd, not a coin flip: ten
#: testers filing at once all read the same highest number, and each
#: successful insert only drains one of them, so a given writer can lose up
#: to nine races in a row. E9.7 measured exactly that — with three attempts
#: one filing in ten was dropped, and dropped *quietly*, because
#: ``routes/bugs._persist_bug`` treats a write failure as best-effort and
#: the page still says "Bug report created successfully".
#:
#: So the bound is set well above the concurrency this product's own
#: organisation model invites. An unconstrained retry loop is not the
#: alternative — a genuinely broken insert has to stop somewhere — but the
#: cost of one more attempt is a single small transaction and the cost of
#: running out is somebody's finding.
BUG_ID_ATTEMPTS = 25


def _next_external_id(sess, model, project_id: str, like: str) -> str:
    """One past the highest number already using *like*'s prefix — E4.4a.

    ``BUG-004`` in a project whose highest is ``BUG-011`` returns
    ``BUG-012``. The prefix is kept rather than normalised because these ids
    are cited by hand, and a row that switched from ``BUG-`` to ``ITEM_``
    when it was renumbered would look like a different artefact.

    An id with no trailing number (``"regression"``) gets one appended, so
    the second occurrence becomes ``regression_001`` instead of colliding
    forever.
    """
    prefix, number = _public_ids.split_id(like)
    if number is None:
        prefix, number = f"{like}_", 0
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    highest = number
    for (value,) in sess.query(model.external_id).filter(
            model.project_id == project_id,
            model.external_id.like(f"{prefix}%")).all():
        match = pattern.match(str(value or ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return _public_ids.format_id(prefix, highest + 1)


def save_bug(project_id: str | None, bug: dict, source: str = "manual") -> int:
    """Persist a bug report. Returns the row id (auto-assigned).

    **Retries once past a public-id collision (E4.4a, extended in E9.7).**
    Every caller mints ``bug["id"]`` as "one past the highest" by reading the
    project's bugs and then writing, which is a read-then-write: two people
    filing at the same moment mint the same number. With
    ``ux_bug_report_project_external_id`` in place that is an
    ``IntegrityError`` rather than two rows answering to ``BUG-004``, and the
    fix is to look again — the second attempt sees the committed row and
    takes the next number. Bounded, so a genuinely broken insert does not
    spin.

    The retry lives here rather than in each of the five callers because
    here is where the write is: a route, the manual walk, the walkthrough
    runner, Tedgie and the MCP tool all arrive through this function, and a
    guard repeated five times is the shape where one copy is wrong.
    """
    if source not in VALID_BUG_SOURCES:
        source = "manual"
    bug = bug or {}
    extra_keys = {k: v for k, v in bug.items() if k not in (
        "id", "title", "severity", "priority", "status",
        "environment", "browser", "os", "version",
        "steps_to_reproduce", "actual_result", "expected_result",
        "comment", "reporter", "related_case_id", "run_id",
        "preconditions", "attachment", "assignee", "bug_area",
    )}
    # Empty string is not an id, and storing it as one would put every
    # id-less bug in a project into a single collision the unique index
    # cannot resolve. NULL is what "this bug has no public id" means.
    external_id = (bug.get("id") or "").strip() or None

    attempts = BUG_ID_ATTEMPTS
    while True:
        try:
            return _insert_bug(project_id, bug, source, extra_keys,
                               external_id)
        except IntegrityError as exc:
            attempts -= 1
            if not (project_id and external_id):
                raise
            if attempts <= 0:
                # Never silently: the caller in routes/bugs.py wraps this
                # in a best-effort try, so an exception that gets this far
                # becomes a bug report the tester was told had been saved.
                log.warning(
                    "gave up minting a bug id in project %s after %d "
                    "attempts — the report was NOT saved: %s",
                    str(project_id)[:8], BUG_ID_ATTEMPTS, exc.orig)
                raise
            with session_scope() as sess:
                external_id = _next_external_id(
                    sess, BugReport, project_id, external_id)
            log.info("bug id collision in project %s, retrying as %s: %s",
                     str(project_id)[:8], external_id, exc.orig)


def _insert_bug(project_id: str | None, bug: dict, source: str,
                extra_keys: dict, external_id: str | None) -> int:
    with session_scope() as sess:
        # Bump project recency so a fresh bug surfaces the project at
        # the top of the picker dropdown.
        if project_id:
            proj = sess.get(Project, project_id)
            if proj is not None:
                proj.updated_at = _utcnow()
        row = BugReport(
            project_id=project_id,
            external_id=external_id,
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
            preconditions=bug.get("preconditions"),
            attachment=bug.get("attachment"),
            assignee=bug.get("assignee"),
            # Derived when the caller did not decide, never overwritten
            # when it did — triage is a judgement and re-deriving it on
            # every write would silently undo it.
            bug_area=_resolve_bug_area(bug),
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


def append_bug_attachment(project_id: str, bug_id: int, key: str) -> bool:
    """Add one storage key to a bug's ``attachments`` list (E4.5a).

    Scoped by ``project_id`` for the reason :func:`bulk_update_bugs` gives:
    a bug id from another project must not be addressable, and applying the
    filter in the query is what makes that true rather than remembered.

    ``attachments`` lives in the ``extra`` JSON blob and is a **list of
    storage keys** — not to be confused with the ``attachment`` column,
    which is a single free-text evidence *link* carried over from the team's
    bug spreadsheet. Two mechanisms, names one letter apart; see
    ``routes/bugs.py::bug_attach`` for why they stay separate.

    The whole dict is reassigned rather than mutated in place: SQLAlchemy
    does not track mutation inside a JSON column, so ``extra["attachments"]
    .append(...)`` writes nothing and reports success.
    """
    if not (project_id and bug_id and key):
        return False
    with session_scope() as sess:
        row = sess.query(BugReport).filter(
            BugReport.project_id == project_id,
            BugReport.id == int(bug_id),
        ).one_or_none()
        if row is None:
            return False
        extra = dict(row.extra or {})
        attachments = list(extra.get("attachments") or [])
        if key in attachments:
            return True
        attachments.append(key)
        extra["attachments"] = attachments
        row.extra = extra
        return True


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
                     source: str | None = None,
                     notes: str | None = None) -> int:
    with session_scope() as sess:
        row = ExecutionCaseResult(
            run_id=run_id,
            case_id=case_id,
            case_external_id=case_external_id,
            case_kind=case_kind,
            status=status,
            source=(source or "")[:20],
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
    """Most recent automation runs, newest first.

    ``project_id=None`` means **every run on this instance**, across every
    project and organisation. That is a trap in the shape of a default: an
    empty string is falsy, so a caller that passes an unresolved active
    project asks for the wide answer while reading like it asked for the
    narrow one. ``/automation`` did exactly that and rendered another
    organisation's run history; ``routes/dashboard.py`` had already grown a
    hand-written ``if not project_id: return None`` against the same edge.
    Both now guard, and any third caller must too — there is no
    ops-wide view that wants this, so if one is ever added it should say so
    at the call site.
    """
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


def list_case_results(run_id: int) -> list[dict]:
    """Per-item results of one run, oldest first.

    The manual runner uses this as its progress ledger: the queue lives in
    the run's ``env_payload`` and "where am I" is derived from which items
    already have a row here. That keeps the walk resumable across a reload,
    a lost tab or a different browser, with no extra state to fall out of
    sync with the results themselves.
    """
    with session_scope() as sess:
        rows = sess.execute(
            select(ExecutionCaseResult)
            .where(ExecutionCaseResult.run_id == run_id)
            .order_by(ExecutionCaseResult.id.asc())
        ).scalars().all()
        return [_row_to_dict(r) for r in rows]


def merge_run_env(run_id: int, patch: dict) -> bool:
    """Merge *patch* into a run's ``env_payload``.

    Merge, not replace: the payload is written at ``start_execution_run``
    with the tester and environment, and enriched afterwards with facts only
    known once the run finished. A caller that PUT the whole object would
    drop whichever half it did not have.
    """
    if not (run_id and isinstance(patch, dict) and patch):
        return False
    with session_scope() as sess:
        row = sess.get(ExecutionRun, run_id)
        if row is None:
            return False
        merged = dict(row.env_payload or {})
        merged.update(patch)
        # Reassigned, not mutated: SQLAlchemy does not track in-place
        # changes to a JSON column, so mutating would look like it worked
        # and write nothing.
        row.env_payload = merged
        return True


def list_case_results_for_runs(run_ids: list[int]) -> dict[int, list[dict]]:
    """Per-item results for several runs at once, grouped by run id.

    One query rather than one per run. The run-history list shows twenty
    runs, and calling :func:`list_case_results` in a loop would be twenty
    round-trips per page load — on a free-tier database with a compute
    quota, that is the difference between a page and a bill.
    """
    ids = [int(r) for r in (run_ids or []) if r]
    if not ids:
        return {}
    out: dict[int, list[dict]] = {rid: [] for rid in ids}
    with session_scope() as sess:
        # Outer-joined to the bug's *display* id. The row carries the
        # integer FK, but every consumer — templates, the results page, the
        # exporters — treats a result's bug_id as the human "BUG-001"
        # string. Returning the FK instead looks like it works until
        # something calls .startswith on it.
        rows = sess.execute(
            select(ExecutionCaseResult, BugReport.external_id)
            .outerjoin(BugReport,
                       BugReport.id == ExecutionCaseResult.bug_report_id)
            .where(ExecutionCaseResult.run_id.in_(ids))
            .order_by(ExecutionCaseResult.id.asc())
        ).all()
        for row, bug_external_id in rows:
            d = _row_to_dict(row)
            d["bug_external_id"] = bug_external_id or ""
            out.setdefault(int(row.run_id), []).append(d)
    return out


def get_execution_run(run_id: int) -> dict | None:
    with session_scope() as sess:
        row = sess.get(ExecutionRun, run_id)
        return _row_to_dict(row) if row is not None else None


def update_case_result(run_id: int, case_external_id: str,
                       case_kind: str | None = None, **fields) -> bool:
    """Overwrite an item's result. Returns ``False`` when it has none yet.

    A tester who mis-clicks Passed has to be able to correct it, and a
    second row for the same item would double-count in the run stats.

    ``case_kind`` narrows the row when given, and it needs to be given
    whenever a run can contain both kinds: the two id spaces are separate
    sequences, so a test case and a checklist item may share a label, and
    without the kind this would correct whichever of the two was written
    last. It stays optional so the automation ingest, whose runs are
    single-kind, is unaffected.
    """
    allowed = {"status", "evidence_path", "bug_report_id", "notes"}
    payload = {k: v for k, v in fields.items() if k in allowed}
    if not payload:
        return False
    with session_scope() as sess:
        where = [ExecutionCaseResult.run_id == run_id,
                 ExecutionCaseResult.case_external_id == case_external_id]
        if case_kind:
            where.append(ExecutionCaseResult.case_kind == case_kind)
        row = sess.execute(
            select(ExecutionCaseResult).where(*where)
            .order_by(ExecutionCaseResult.id.desc())
        ).scalars().first()
        if row is None:
            return False
        for key, value in payload.items():
            setattr(row, key, value)
        return True


def assign_run(run_id: int, assignee_id: str, *,
               tester: str | None = None) -> bool:
    """Hand a run to somebody. Returns False when the run is gone.

    Merges into ``env_payload`` rather than replacing it: the payload also
    holds the manual queue, the environment and the base URL, and a walk
    reassigned mid-flight must not lose the queue it is walking.

    Reassignment exists because a walk can outlive its owner's availability
    — sixty checks span days, and an admin needs a way to move one rather
    than starting it again and losing the verdicts already recorded.
    """
    with session_scope() as sess:
        row = sess.get(ExecutionRun, run_id)
        if row is None:
            return False
        payload = dict(row.env_payload or {})
        payload["assignee_id"] = str(assignee_id or "")
        if tester is not None:
            payload["tester"] = str(tester)
        # Reassigned wholesale: SQLAlchemy tracks JSON columns by identity,
        # so mutating the dict in place would not mark the row dirty and the
        # write would be silently dropped.
        row.env_payload = payload
        return True


def list_open_runs(project_id: str, *, mode: str | None = None,
                   limit: int = 20) -> list[dict]:
    """Runs in this project that were started and never closed.

    A manual walk over sixty checks spans laptop sleeps and hand-offs, and
    the state to resume it was already in the database — but nothing listed
    it, so an interrupted run was reachable only from browser history. That
    is the whole reason this exists: resumable and findable are different
    properties, and only the first one was built.

    ``mode`` filters on ``env_payload['mode']`` in Python rather than SQL
    because the column is JSON and SQLite and Postgres disagree about how
    to index into it — the row counts here are small enough that the
    difference does not matter, and a portable query is worth more.
    """
    with session_scope() as sess:
        rows = sess.execute(
            select(ExecutionRun)
            .where(ExecutionRun.project_id == project_id,
                   ExecutionRun.finished_at.is_(None))
            .order_by(ExecutionRun.started_at.desc()).limit(limit * 3)
        ).scalars().all()
    out = [_row_to_dict(r) for r in rows]
    if mode:
        out = [r for r in out
               if str((r.get("env_payload") or {}).get("mode") or "") == mode]
    return out[:limit]


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
    # the pre-ORG_MODE migration and the preview of it (E1.6)
    "adopt_orphan_projects", "orphan_project_survey",
    "ORPHAN_PREVIEW_LIMIT",
    # test cases
    "save_test_cases", "load_test_cases", "create_test_case",
    # Optimistic concurrency on pack writes (E3.5).
    "WriteConflict", "pack_versions",
    # checklist
    "save_checklist", "load_checklist",
    # bugs
    "save_bug", "list_bugs", "count_bugs_by_run", "VALID_BUG_SOURCES",
    # estimation
    "save_estimation", "list_estimations", "list_estimations_by_owner",
    "latest_estimation",
    # execution
    "start_execution_run", "finish_execution_run", "save_case_result",
    "merge_run_env", "list_case_results_for_runs",
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
    # identity / tenancy / audit (E0.2, E1.1, E2.1, E2.7)
    "ServerSession", "User", "Identity", "Organization", "OrgMember",
    "Invite", "AuditLog", "OrgSecret", "LlmUsage",
    "ORG_ROLES", "ROLE_RANK", "INVITE_TTL_HOURS", "normalize_email",
    "create_user", "get_user", "get_user_by_email",
    "link_identity", "get_user_by_identity",
    "get_organization", "update_org_settings",
    # org secrets — engine.llm_keys is the only intended caller
    "set_org_secret", "get_org_secret", "delete_org_secret",
    "has_org_secret",
    # LLM usage accounting
    "record_llm_usage", "org_spend_micros", "llm_usage_summary",
    "purge_llm_usage",
    "create_organization", "add_org_member", "get_org_role",
    "list_org_members", "count_org_admins", "count_users",
    "remove_org_member",
    "change_org_role", "set_password_hash", "mark_email_verified",
    "touch_last_login", "bump_login_failure", "clear_login_failures",
    "lock_user", "set_user_active",
    "set_project_org", "list_projects_for_org",
    "create_invite", "get_invite", "consume_invite", "revoke_invite",
    "revoke_invites_for_email", "list_pending_invites",
    "mark_invite_emailed",
    # one-time tokens for reset / verify (E1.3)
    "AuthToken", "AUTH_TOKEN_PURPOSES",
    "RESET_TTL_MINUTES", "VERIFY_TTL_MINUTES",
    "create_auth_token", "get_auth_token", "consume_auth_token",
    "claim_auth_token_send", "revoke_auth_tokens",
    "purge_expired_auth_tokens",
    "append_audit", "list_audit", "count_audit_since",
    "session_load", "session_save", "session_delete",
    "delete_sessions_for_user", "purge_expired_sessions",
    # aggregates
    "count_records",
]
