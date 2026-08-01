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

**Not covered here: PostgreSQL.** Production runs Postgres and no server
was available in this environment, so the DDL is exercised on SQLite only
and checked for portable syntax by inspection below. A Postgres run before
deploy is still worth doing.
"""
from __future__ import annotations

import re

import pytest
from sqlalchemy import inspect, text

from engine import db as _db


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


@pytest.fixture()
def fresh_engine(tmp_path, monkeypatch):
    """A brand-new SQLite database with the full schema.

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
    db_path = tmp_path / "mig.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr(_db, "_engine", None, raising=False)
    monkeypatch.setattr(_db, "_Session", None, raising=False)
    _db.init_db()

    engine = _db.get_engine()
    assert str(db_path) in str(engine.url), (
        f"refusing to run destructive migration tests against "
        f"{engine.url} — the database swap did not take")

    yield engine

    # Tear the engine down so the next test rebuilds against the real URL
    # rather than inheriting this throwaway one.
    engine.dispose()
    monkeypatch.setattr(_db, "_engine", None, raising=False)
    monkeypatch.setattr(_db, "_Session", None, raising=False)


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

    def test_dropped_columns_are_restored(self, fresh_engine):
        # Simulate the pre-PR schema.
        with fresh_engine.begin() as conn:
            for table, column, _default in ADDED_COLUMNS:
                conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        for table, column, _default in ADDED_COLUMNS:
            assert column not in _columns(table), \
                f"{table}.{column} survived the drop — test is not testing"

        _db._ensure_walkthrough_columns(fresh_engine)

        missing = [f"{t}.{c}" for t, c, _ in ADDED_COLUMNS
                   if c not in _columns(t)]
        assert not missing, missing

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
                conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
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
                conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
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
        src = _inspect.getsource(_db._ensure_walkthrough_columns)
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
