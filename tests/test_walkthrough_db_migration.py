"""TFWefloLab PR-2 — TestCase ``url_pattern`` + ``trigger`` columns.

PR-2 extends the ``test_case`` table with two columns the walkthrough
matcher reads. The DB layer has no Alembic — schema is owned by
``Base.metadata.create_all`` plus a small idempotent migration helper
:func:`engine.db._ensure_walkthrough_columns` that issues ALTER TABLE
for projects that booted on the pre-PR-2 schema.

Coverage:

* Fresh DB (``init_db`` from cold) carries both columns with the
  documented defaults.
* Pre-existing test_case table missing the new columns gets ALTERed
  on the next ``init_db`` boot.
* A ``manual``-triggered TC stays out of the walkthrough binding by
  default — backward compat for projects that imported TCs before
  PR-2 landed.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Point ``DATABASE_URL`` at a tmp SQLite + reset the engine
    between tests so each case sees a clean schema."""
    db_path = tmp_path / "tfg.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("FLASK_DEBUG", "1")
    import engine.db as _db
    monkeypatch.setattr(_db, "_engine", None)
    monkeypatch.setattr(_db, "_Session", None)
    yield
    # Cleanup: reset module globals so the next test's monkeypatch
    # creates a fresh engine.
    monkeypatch.setattr(_db, "_engine", None)
    monkeypatch.setattr(_db, "_Session", None)


class TestFreshSchema:
    def test_test_case_table_has_walkthrough_columns(self):
        from engine.db import init_db, get_engine
        from sqlalchemy import inspect
        init_db()
        cols = {c["name"] for c in
                inspect(get_engine()).get_columns("test_case")}
        assert "url_pattern" in cols
        assert "trigger" in cols

    def test_defaults_are_manual_trigger_and_empty_pattern(self):
        from engine.db import (init_db, session_scope,
                                TestCase, Project)
        init_db()
        with session_scope() as s:
            p = Project(name="P", slug="p")
            s.add(p)
            s.flush()
            tc = TestCase(project_id=p.id,
                          summary="Smoke test", priority="Medium")
            s.add(tc)
            s.flush()
            assert tc.url_pattern == ""
            assert tc.trigger == "manual"


class TestIdempotentMigration:
    def test_alter_table_adds_columns_on_legacy_schema(
            self, monkeypatch, tmp_path):
        """Simulate an older deploy: create the test_case table
        WITHOUT url_pattern / trigger, then call init_db and verify
        the migration helper added them without dropping data."""
        from sqlalchemy import create_engine, text, inspect

        db_path = tmp_path / "legacy.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

        eng = create_engine(f"sqlite:///{db_path}")
        with eng.begin() as conn:
            conn.execute(text("""
                CREATE TABLE project (
                    id VARCHAR(32) PRIMARY KEY,
                    name VARCHAR(200) NOT NULL,
                    slug VARCHAR(220) NOT NULL,
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """))
            # Pre-PR-2 test_case schema: every column the model carried
            # before walkthrough integration, missing only url_pattern
            # and trigger. The migration helper must add those two
            # without touching the rest.
            conn.execute(text("""
                CREATE TABLE test_case (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id VARCHAR(32) NOT NULL,
                    external_id VARCHAR(40),
                    section VARCHAR(200),
                    section_num VARCHAR(40),
                    summary TEXT,
                    preconditions TEXT,
                    test_steps TEXT,
                    test_data TEXT,
                    expected_result TEXT,
                    issues TEXT,
                    comment TEXT,
                    user_story_id VARCHAR(40),
                    category VARCHAR(60),
                    priority VARCHAR(20),
                    status VARCHAR(20),
                    testing_type VARCHAR(40),
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """))
            conn.execute(text(
                "INSERT INTO project (id, name, slug) "
                "VALUES ('p1', 'P', 'p')"
            ))
            conn.execute(text(
                "INSERT INTO test_case (project_id, summary) "
                "VALUES ('p1', 'legacy row')"
            ))
        eng.dispose()

        # Reset module state and re-initialise — migration runs.
        import engine.db as _db
        monkeypatch.setattr(_db, "_engine", None)
        monkeypatch.setattr(_db, "_Session", None)
        _db.init_db()

        cols = {c["name"] for c in
                inspect(_db.get_engine()).get_columns("test_case")}
        assert "url_pattern" in cols
        assert "trigger" in cols

        # Legacy data is preserved + back-filled with the column
        # defaults the model declares.
        with _db.session_scope() as s:
            from engine.db import TestCase
            row = s.query(TestCase).first()
            assert row.summary == "legacy row"
            assert row.url_pattern == ""
            assert row.trigger == "manual"

    def test_migration_is_idempotent(self):
        """Calling init_db twice on a fresh DB must NOT raise — the
        column-presence probe short-circuits on the second pass."""
        from engine.db import init_db
        init_db()
        # Manually allow re-init by zeroing the cached engine.
        import engine.db as _db
        _db._engine = None
        _db._Session = None
        init_db()  # would raise "duplicate column name" if not gated
