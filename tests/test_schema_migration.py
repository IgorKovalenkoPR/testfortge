"""The additive column migrations, exercised rather than assumed.

Four PRs in this series added columns to live tables:

    checklist_item   item_num, depth                      (low-level checklist)
    test_case        tc_format, gherkin                   (dual-format TCs)
    bug_report       bug_area, preconditions,
                     attachment, assignee                 (the bug sheet)
    automation_run   (whole table)                        (Allure ingest)

Each is a hand-written ``ALTER TABLE`` in ``_ensure_walkthrough_columns``
sitting beside a separately-declared SQLAlchemy model. Those two can
disagree — the model says the column exists, the ALTER never adds it, and
a deployment that upgrades in place raises "no such column" on the first
read while a fresh install passes every test.

So these tests simulate the upgrade rather than the install: build the
schema, DROP the new columns, run the migration, and check what came back.
SQLite has supported ``DROP COLUMN`` since 3.35, which is what makes the
simulation possible.

Every test that touches the schema is parameterised over **both** engines.
SQLite runs always; PostgreSQL runs when ``TFG_TEST_POSTGRES_URL`` points
at a throwaway database, which CI supplies from a service container. That
matters because the ALTERs are hand-written SQL and production is
Postgres: the two engines differ on reserved words, on whether
``ADD COLUMN … NOT NULL DEFAULT`` rewrites the table, and on type names. A
migration verified only on SQLite is verified on the wrong engine.
"""
from __future__ import annotations

import os
import re
import sqlite3

import pytest
from sqlalchemy import inspect, text

from engine import db as _db


#: One Postgres service container, many test files. ``--dist loadgroup`` in
#: CI (see .github/workflows/tests.yml) keeps every test carrying this mark
#: inside a single xdist worker, because M-1's per-process isolation gives
#: each worker its own *SQLite* scratch database and cannot give it its own
#: Postgres server. Two workers dropping and recreating tables in one
#: database is the collision M-1 removed, in the one place the fix does not
#: reach. Guarded by tests/test_suite_isolation.py.
pytestmark = pytest.mark.xdist_group("postgres")
#: (table, column, expected default for a back-filled row)
ADDED_COLUMNS: tuple[tuple[str, str, object], ...] = (
    ("checklist_item", "item_num", ""),
    ("checklist_item", "depth", 2),
    ("test_case", "tc_format", "manual"),
    ("test_case", "gherkin", ""),
    ("test_case", "url_pattern", ""),
    ("test_case", "trigger", "manual"),
    ("test_case", "automation_steps_json", ""),
    ("test_case", "suite", ""),
    ("bug_report", "bug_area", "Functional"),
    ("bug_report", "preconditions", None),
    ("bug_report", "attachment", None),
    ("bug_report", "assignee", None),
)


def _columns(table: str) -> set[str]:
    insp = inspect(_db.get_engine())
    try:
        return {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return set()


#: Point this at a THROWAWAY PostgreSQL database to run the whole file
#: against the engine production actually uses. CI sets it from a service
#: container; locally, any scratch database works:
#:
#:     TFG_TEST_POSTGRES_URL=postgresql+psycopg2://u:p@localhost/tfg_test
#:
#: Everything below is destructive — it drops columns and tables — so the
#: URL must never point at anything whose contents matter.
POSTGRES_URL = os.environ.get("TFG_TEST_POSTGRES_URL", "").strip()

#: SQLite gained ``DROP COLUMN`` in 3.35. Older builds can still run the
#: fresh-install and portability checks.
_CAN_DROP_COLUMN = sqlite3.sqlite_version_info >= (3, 35)


def _params() -> list:
    out = [pytest.param("sqlite", id="sqlite")]
    out.append(pytest.param(
        "postgres", id="postgres",
        marks=pytest.mark.skipif(
            not POSTGRES_URL,
            reason="set TFG_TEST_POSTGRES_URL to a throwaway database")))
    return out


@pytest.fixture(params=_params())
def fresh_engine(request, tmp_path, monkeypatch):
    """A brand-new database with the full schema, on each backend.

    Parameterised over SQLite and PostgreSQL because the ALTERs are
    hand-written SQL and production runs Postgres. A migration verified
    only on SQLite is a migration verified on the wrong engine — the two
    differ on reserved words, on whether ``ADD COLUMN … NOT NULL DEFAULT``
    rewrites the table, and on type names.

    These tests DROP columns and tables. If the swap below ever fails to
    take, they would do that to the shared development database instead —
    which is exactly what happened the first time this file ran in the
    full suite: six unrelated tests started failing with "no such table:
    session_draft", four files later, with nothing pointing back here.

    So isolation is asserted, not assumed. A destructive test that cannot
    prove it owns its database must fail loudly and locally rather than
    quietly corrupt everyone else's.
    """
    monkeypatch.setenv("FLASK_DEBUG", "1")
    if request.param == "postgres":
        url = POSTGRES_URL
        marker = url.rsplit("/", 1)[-1].split("?")[0]
    else:
        db_path = tmp_path / "mig.db"
        url = f"sqlite:///{db_path}"
        marker = str(db_path)

    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(_db, "_engine", None, raising=False)
    monkeypatch.setattr(_db, "_Session", None, raising=False)

    if request.param == "postgres":
        # Start from nothing: a leftover schema from a previous run would
        # make "the migration created this" indistinguishable from "it was
        # already there".
        from sqlalchemy import create_engine
        probe = create_engine(url)
        _db.Base.metadata.drop_all(probe)
        probe.dispose()

    _db.init_db()
    engine = _db.get_engine()
    assert marker in str(engine.url), (
        f"refusing to run destructive migration tests against "
        f"{engine.url} — the database swap did not take")

    yield engine

    if request.param == "postgres":
        _db.Base.metadata.drop_all(engine)
    # Tear the engine down so the next test rebuilds against the real URL
    # rather than inheriting this throwaway one. Both globals: resetting
    # only ``_engine`` leaves the stale sessionmaker bound here, which is
    # the split brain that caused the cross-file failures above.
    engine.dispose()
    monkeypatch.setattr(_db, "_engine", None, raising=False)
    monkeypatch.setattr(_db, "_Session", None, raising=False)


def _drop_column(conn, table: str, column: str) -> None:
    """``DROP COLUMN``, quoting the identifier for both engines."""
    conn.execute(text(f'ALTER TABLE {table} DROP COLUMN "{column}"'))


class TestFreshInstall:
    def test_create_all_produces_every_added_column(self, fresh_engine):
        for table, column, _default in ADDED_COLUMNS:
            assert column in _columns(table), f"{table}.{column}"

    def test_automation_run_table_exists(self, fresh_engine):
        assert _columns("automation_run"), "automation_run was not created"
        for column in ("project_id", "origin", "label", "total", "passed",
                       "failed", "broken", "skipped", "duration_ms",
                       "pass_rate", "summary", "report_path"):
            assert column in _columns("automation_run"), column


class TestUpgradeInPlace:
    """The path a deployment actually takes: an existing DB, new code."""

    @pytest.mark.skipif(not _CAN_DROP_COLUMN,
                        reason="SQLite < 3.35 has no DROP COLUMN")
    def test_dropped_columns_are_restored(self, fresh_engine):
        # Simulate the pre-PR schema.
        with fresh_engine.begin() as conn:
            for table, column, _default in ADDED_COLUMNS:
                _drop_column(conn, table, column)
        for table, column, _default in ADDED_COLUMNS:
            assert column not in _columns(table), \
                f"{table}.{column} survived the drop — test is not testing"

        _db._ensure_walkthrough_columns(fresh_engine)

        missing = [f"{t}.{c}" for t, c, _ in ADDED_COLUMNS
                   if c not in _columns(t)]
        assert not missing, missing

    @pytest.mark.skipif(not _CAN_DROP_COLUMN,
                        reason="SQLite < 3.35 has no DROP COLUMN")
    def test_existing_rows_back_fill_to_the_declared_default(self,
                                                             fresh_engine):
        pid = _db.upsert_project("migration-backfill")
        _db.save_test_cases(pid, [{
            "id": "SC1_001", "section": "S", "section_num": 1,
            "summary": "Verify that x", "preconditions": "",
            "test_steps": "1. Go", "test_data": "",
            "expected_result": "y"}])
        _db.save_checklist(pid, [{"id": "HDR_001", "section": "Header",
                                  "objective": "Verify that y"}])
        _db.save_bug(pid, {"title": "Existing bug"})

        with fresh_engine.begin() as conn:
            for table, column, _default in ADDED_COLUMNS:
                _drop_column(conn, table, column)
        _db._ensure_walkthrough_columns(fresh_engine)

        # A row written before the migration must read back with the
        # declared default, not NULL where the model says NOT NULL.
        with fresh_engine.begin() as conn:
            for table, column, default in ADDED_COLUMNS:
                if default is None:
                    continue
                value = conn.execute(
                    text(f"SELECT {column} FROM {table} LIMIT 1")).scalar()
                assert value == default, f"{table}.{column} = {value!r}"

    @pytest.mark.skipif(not _CAN_DROP_COLUMN,
                        reason="SQLite < 3.35 has no DROP COLUMN")
    def test_the_orm_can_read_a_migrated_row(self, fresh_engine):
        """The failure mode this whole file exists for.

        A column the model declares and the migration forgets raises
        "no such column" on the first ORM read — after deploy, on a
        customer's data, not in CI.
        """
        pid = _db.upsert_project("migration-orm-read")
        _db.save_bug(pid, {"title": "Before"})
        with fresh_engine.begin() as conn:
            for table, column, _default in ADDED_COLUMNS:
                _drop_column(conn, table, column)
        _db._ensure_walkthrough_columns(fresh_engine)

        rows = _db.list_bugs(pid)
        assert rows and rows[0]["bug_area"] == "Functional"
        assert _db.load_test_cases(pid) == []
        assert _db.load_checklist(pid) == []

    def test_running_the_migration_twice_is_a_no_op(self, fresh_engine):
        # Two gunicorn workers boot at once; both call init_db.
        before = {t: _columns(t) for t, _c, _d in ADDED_COLUMNS}
        _db._ensure_walkthrough_columns(fresh_engine)
        _db._ensure_walkthrough_columns(fresh_engine)
        after = {t: _columns(t) for t, _c, _d in ADDED_COLUMNS}
        assert before == after

    def test_a_missing_table_does_not_raise(self, fresh_engine):
        # A partially-created DB must not stop the app from booting.
        with fresh_engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS session_draft"))
        _db._ensure_walkthrough_columns(fresh_engine)   # must not raise


class TestMigrationCoversTheModel:
    """The guard that catches the NEXT forgotten column.

    ``ADDED_COLUMNS`` above is hand-maintained and shares the migration's
    weakness — someone can add a column to the model and forget both. This
    derives the requirement from the model instead: a column declared with
    a ``server_default`` exists precisely because pre-existing rows need a
    value, which means an in-place upgrade MUST add it.
    """

    def _migrated_columns(self) -> dict[str, set[str]]:
        import inspect as _inspect
        # Every migration helper, not just the walkthrough one. The matcher
        # read a single function for as long as there was a single function;
        # by E4.1 there were five, and a column added by any of the others
        # would have been reported as un-migrated (or, worse, a genuinely
        # missing migration would have been reported as present because the
        # column lived in a table this test does not check).
        src = chr(10).join(
            _inspect.getsource(getattr(_db, name))
            for name in dir(_db)
            if name.startswith("_ensure_") and callable(getattr(_db, name))
        )
        # …plus the module-level statement tables the helpers execute, which
        # are deliberately literal so both an operator's grep and this
        # matcher can find them.
        src += chr(10) + chr(10).join(
            str(item) for name in dir(_db)
            if name.endswith("_MIGRATIONS")
            for item in (getattr(_db, name) or ())
        )
        out: dict[str, set[str]] = {}
        # The quoted form matters: ``trigger`` is a reserved word in
        # Postgres and is added as ADD COLUMN "trigger". A matcher that
        # only understood bare identifiers reported it as missing.
        for m in re.finditer(r'ALTER TABLE (\w+) ADD COLUMN "?(\w+)"?', src):
            out.setdefault(m.group(1), set()).add(m.group(2))
        return out

    def test_every_defaulted_model_column_is_migrated(self):
        migrated = self._migrated_columns()
        gaps: list[str] = []
        for model in (_db.TestCase, _db.ChecklistItem, _db.BugReport):
            table = model.__tablename__
            for column in model.__table__.columns:
                if column.server_default is None:
                    continue
                if column.name not in migrated.get(table, set()):
                    gaps.append(f"{table}.{column.name}")
        assert not gaps, (
            f"declared with a server_default but never added to an existing "
            f"database — an in-place upgrade will raise 'no such column' on "
            f"the first ORM read: {gaps}")

    def test_the_matcher_understands_quoted_identifiers(self):
        # Guards the guard: a regex that missed "trigger" would report a
        # false gap and, worse, hide a real one behind the noise.
        assert "trigger" in self._migrated_columns().get("test_case", set())


class TestDdlPortability:
    """Production is Postgres; this environment had none available.

    These check the DDL avoids the constructs that differ between the two
    dialects. They are not a substitute for running the migration against
    a real Postgres — they are what can be verified without one.
    """

    def _ddl(self) -> list[str]:
        import inspect as _inspect
        src = _inspect.getsource(_db._ensure_walkthrough_columns)
        return re.findall(r'"(ALTER TABLE [^"]+)"', src) + \
            re.findall(r'"(ALTER TABLE [^"]+)"\s*\n\s*"([^"]+)"', src) and \
            re.findall(r'ALTER TABLE [^"]*', src)

    def test_every_added_column_has_a_constant_default(self):
        """SQLite refuses a non-constant default on ADD COLUMN.

        ``DEFAULT now()`` or ``DEFAULT (…)`` would work on Postgres and
        fail on SQLite — the reverse of the usual portability trap, and it
        would only surface on a dev machine.
        """
        import inspect as _inspect
        src = _inspect.getsource(_db._ensure_walkthrough_columns)
        for match in re.finditer(r"DEFAULT\s+([^\"',]+)", src):
            value = match.group(1).strip()
            assert not value.endswith("("), f"non-constant default: {value}"
            assert value.lower() not in ("now()", "current_timestamp"), value

    def test_no_sqlite_only_or_postgres_only_syntax(self):
        import inspect as _inspect
        src = _inspect.getsource(_db._ensure_walkthrough_columns)
        statements = re.findall(r"ALTER TABLE .*?(?=\"|$)", src, re.S)
        blob = " ".join(statements).lower()
        # ``IF NOT EXISTS`` on ADD COLUMN is Postgres-only; the code probes
        # the column list instead, which is why it works on both.
        assert "add column if not exists" not in blob
        # ``AUTOINCREMENT`` and ``SERIAL`` are each single-dialect.
        assert "autoincrement" not in blob
        assert "serial" not in blob

    def test_added_columns_use_portable_types(self):
        import inspect as _inspect
        src = _inspect.getsource(_db._ensure_walkthrough_columns)
        for match in re.finditer(
                r"ADD COLUMN \w+ (\w+(?:\(\d+\))?)", src):
            declared = match.group(1).upper()
            base = re.sub(r"\(\d+\)$", "", declared)
            assert base in ("TEXT", "VARCHAR", "INTEGER", "BOOLEAN",
                            "FLOAT", "REAL"), declared


# ── Platform search schema (ADR 0003 §4.1, §4.12, §7.2) ───────────────

def _is_pg(engine) -> bool:
    return engine.dialect.name == "postgresql"


def _pg_scalar(engine, sql: str):
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar()


def _vector_column_type(engine):
    return _pg_scalar(
        engine,
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = to_regclass('search_vector') "
        "AND attname = 'embedding' AND NOT attisdropped")


class TestSearchSchema:
    """``search_doc`` / ``search_scope_state`` on both engines, plus the
    Postgres-only FTS column and the pgvector table, with and without the
    extension.

    CI's service image is ``pgvector/pgvector:pg16``, so the extension is
    *available* there; the no-extension path is produced by dropping it and
    pointing the CREATE EXTENSION statement at one that does not exist —
    the same "statement fails inside the migration" a managed database
    without the privilege gives, on the real engine.
    """

    def test_tables_exist_with_the_adr_columns(self, fresh_engine):
        cols = _columns("search_doc")
        for column in ("id", "doc_key", "scope", "org_id", "project_id",
                       "kind", "ref", "chunk_no", "title", "body", "lang",
                       "lex_title", "lex_body", "url", "facets",
                       "content_hash", "vec_version", "vec_blob",
                       "source_updated_at", "indexed_at"):
            assert column in cols, column
        for column in ("scope_key", "version", "indexed_at", "doc_count",
                       "provider", "vec_version", "last_error", "vocab_json"):
            assert column in _columns("search_scope_state"), column

    def test_caps_are_published_at_boot(self, fresh_engine):
        from engine import search
        assert search.caps["checked"] is True
        assert search.caps["backend"] == fresh_engine.dialect.name
        assert search.caps["provider"] == "hash"
        if _is_pg(fresh_engine):
            assert search.caps["fts"] is True
            assert "tsv" in _columns("search_doc")
        else:
            assert search.caps["fts"] is False
            assert search.caps["pgvector"] is False
            assert "tsv" not in _columns("search_doc")

    def test_running_it_twice_is_a_no_op(self, fresh_engine):
        from engine import search
        before = (_columns("search_doc"), dict(search.caps))
        _db._ensure_search_schema(fresh_engine)
        _db._ensure_search_schema(fresh_engine)
        assert (_columns("search_doc"), dict(search.caps)) == before

    def test_detect_caps_agrees_with_what_the_migration_published(
            self, fresh_engine):
        from engine import search
        published = dict(search.caps)
        detected = search.detect_caps(fresh_engine, publish=False)
        for key in ("fts", "pgvector", "pgvector_version", "backend"):
            assert detected[key] == published[key], key

    def test_a_doc_round_trips_and_cascades_with_its_project(self,
                                                             fresh_engine):
        from engine.search.embed import from_blob, get_embedder, to_blob
        vec = get_embedder("hash").embed("не можу увійти")
        pid = _db.upsert_project("search-cascade")
        with _db.session_scope() as sess:
            sess.add(_db.SearchDoc(
                doc_key=f"project:{pid}:bug:BUG-1:0", scope="project",
                project_id=pid, kind="bug", ref="BUG-1", title="Login fails",
                lang="mixed", lex_title="login fail", lex_body="вхід login",
                facets={"status": "open"}, vec_version="hash-v1.1-288",
                vec_blob=to_blob(vec)))
        with _db.session_scope() as sess:
            row = sess.query(_db.SearchDoc).one()
            assert from_blob(row.vec_blob) == vec
            assert row.facets == {"status": "open"}
        if _is_pg(fresh_engine):
            # The generated tsvector is filled from the lexical columns.
            assert _pg_scalar(
                fresh_engine,
                "SELECT count(*) FROM search_doc "
                "WHERE tsv @@ to_tsquery('simple', 'login')") == 1
        _db.delete_project(pid)
        with _db.session_scope() as sess:
            assert sess.query(_db.SearchDoc).count() == 0

    def test_doc_key_is_unique(self, fresh_engine):
        from sqlalchemy.exc import IntegrityError
        with _db.session_scope() as sess:
            sess.add(_db.SearchDoc(doc_key="global:-:guide:x:0",
                                   scope="global", kind="guide", ref="x"))
        with pytest.raises(IntegrityError):
            with _db.session_scope() as sess:
                sess.add(_db.SearchDoc(doc_key="global:-:guide:x:0",
                                       scope="global", kind="guide", ref="x"))

    # -- Postgres with the extension -----------------------------------

    def test_with_the_extension_search_vector_and_hnsw_exist(
            self, fresh_engine):
        if not _is_pg(fresh_engine):
            pytest.skip("pgvector is Postgres-only")
        from engine import search
        if not _pg_scalar(fresh_engine, "SELECT count(*) FROM "
                          "pg_available_extensions WHERE name = 'vector'"):
            pytest.skip("this Postgres has no pgvector build available "
                        "(CI uses pgvector/pgvector:pg16)")
        assert search.caps["pgvector"] is True
        assert search.caps["pgvector_version"]
        assert _vector_column_type(fresh_engine) == "vector(288)"
        assert _pg_scalar(fresh_engine,
                          "SELECT count(*) FROM pg_indexes WHERE "
                          "indexname = 'ix_search_vector_hnsw'") == 1

    def test_a_wrong_dimension_vector_table_is_rebuilt(self, fresh_engine):
        from engine import search
        if not _is_pg(fresh_engine) or not search.caps["pgvector"]:
            pytest.skip("needs Postgres with pgvector")
        with fresh_engine.begin() as conn:
            conn.execute(text("DROP TABLE search_vector"))
            conn.execute(text(
                "CREATE TABLE search_vector (doc_id INTEGER PRIMARY KEY, "
                "project_id VARCHAR(32), embedding vector(3))"))
        _db._ensure_search_schema(fresh_engine)
        assert _vector_column_type(fresh_engine) == "vector(288)"
        assert search.caps["pgvector"] is True

    def test_drop_all_is_not_blocked_by_search_vector(self, fresh_engine):
        """search_vector is raw DDL with a FK into search_doc; without the
        before_drop hook Postgres refuses ``drop_all`` with
        DependentObjectsStillExist — and every destructive test file's
        teardown would fail with it."""
        _db.Base.metadata.drop_all(fresh_engine)
        assert not _columns("search_doc")
        if _is_pg(fresh_engine):
            assert not _pg_scalar(
                fresh_engine, "SELECT to_regclass('search_vector') IS NOT NULL")
        _db.Base.metadata.create_all(fresh_engine)

    # -- Postgres without the extension --------------------------------

    def test_without_the_extension_boot_survives_and_reports_lexical(
            self, fresh_engine, monkeypatch):
        if not _is_pg(fresh_engine):
            pytest.skip("the extension path is Postgres-only")
        from engine import search
        with fresh_engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS search_vector"))
            conn.execute(text("DROP EXTENSION IF EXISTS vector CASCADE"))
        monkeypatch.setattr(_db, "_PGVECTOR_CREATE_SQL",
                            "CREATE EXTENSION IF NOT EXISTS "
                            "tfg_no_such_extension")

        _db._ensure_search_schema(fresh_engine)          # must not raise

        assert search.caps["pgvector"] is False
        assert search.caps["pgvector_version"] is None
        assert search.caps["fts"] is True               # FTS needs no extension
        assert not _pg_scalar(
            fresh_engine, "SELECT to_regclass('search_vector') IS NOT NULL")
        # The failed CREATE EXTENSION ran in a savepoint: the schema is still
        # usable for ordinary writes.
        pid = _db.upsert_project("search-no-extension")
        with _db.session_scope() as sess:
            sess.add(_db.SearchDoc(doc_key=f"project:{pid}:bug:BUG-2:0",
                                   scope="project", project_id=pid,
                                   kind="bug", ref="BUG-2",
                                   lex_title="payment"))
        assert search.detect_caps(fresh_engine, publish=False)[
            "pgvector"] is False


def _sqlalchemy_error():
    from sqlalchemy.exc import OperationalError
    return OperationalError("SELECT 1", {}, Exception("permission denied"))


class _BrokenConn:
    def __enter__(self):
        raise _sqlalchemy_error()

    def __exit__(self, *exc):
        return False


class _BrokenPostgres:
    """An engine that claims to be Postgres and fails every statement."""

    class dialect:  # noqa: N801 — mimics Engine.dialect
        name = "postgresql"

    def begin(self):
        return _BrokenConn()

    def connect(self):
        return _BrokenConn()


class TestSearchSchemaNeverFailsTheBoot:
    """Engine-free, so it runs on every machine, including one with no
    Postgres server: a Postgres whose every statement fails (no privileges,
    a connection dropped mid-boot) must leave init_db standing and report
    the lexical-only capability record."""

    def test_a_failing_postgres_is_survived(self, monkeypatch):
        import sqlalchemy
        from engine import search

        def _refuse(_engine):
            raise _sqlalchemy_error()

        monkeypatch.setattr(sqlalchemy, "inspect", _refuse)
        try:
            _db._ensure_search_schema(_BrokenPostgres())  # must not raise
            assert search.caps["backend"] == "postgresql"
            assert search.caps["fts"] is False
            assert search.caps["pgvector"] is False
            assert search.detect_caps(_BrokenPostgres())["pgvector"] is False
        finally:
            search.reset_caps()

    def test_sqlite_reports_no_fts_and_no_pgvector(self, tmp_path):
        from sqlalchemy import create_engine
        from engine import search
        engine = create_engine(f"sqlite:///{tmp_path / 'caps.db'}")
        try:
            _db.Base.metadata.create_all(engine)
            _db._ensure_search_schema(engine)
            assert (search.caps["fts"], search.caps["pgvector"],
                    search.caps["backend"]) == (False, False, "sqlite")
            assert search.detect_caps(engine, publish=False)[
                "pgvector"] is False
        finally:
            engine.dispose()
            search.reset_caps()
