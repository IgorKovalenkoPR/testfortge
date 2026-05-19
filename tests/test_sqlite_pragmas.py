"""SQLite PRAGMA configuration — Sprint 3 Task 3.4.

Opens a fresh engine against a temp SQLite file and asserts that the
connect listener has applied WAL + safety pragmas on every new
connection. If anyone removes the pragmas (or breaks the event hook),
the assertion-fest below catches it immediately.
"""
from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.db as db


@pytest.fixture
def fresh_sqlite_engine(tmp_path, monkeypatch):
    """Tear down any module-level engine, point env at a temp SQLite
    file, and let init_db() rebuild from scratch. Cleanup restores the
    previous engine handles so other tests still see a working DB.
    """
    monkeypatch.setenv("FLASK_DEBUG", "1")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'pragmas.db'}")
    # Don't let TESTFORTGE_DB or STORAGE_FOLDER override DATABASE_URL.
    monkeypatch.delenv("TESTFORTGE_DB", raising=False)

    prev_engine = db._engine
    prev_session = db._Session
    db._engine = None
    db._Session = None
    try:
        db.init_db()
        yield db.get_engine()
    finally:
        if db._engine is not None:
            db._engine.dispose()
        db._engine = prev_engine
        db._Session = prev_session


def _pragma(eng, name: str):
    """Return the single scalar value of ``PRAGMA <name>``."""
    with eng.connect() as conn:
        return conn.execute(text(f"PRAGMA {name}")).scalar()


def test_journal_mode_is_wal(fresh_sqlite_engine):
    val = _pragma(fresh_sqlite_engine, "journal_mode")
    assert str(val).lower() == "wal", f"expected WAL, got {val!r}"


def test_busy_timeout_5000ms(fresh_sqlite_engine):
    assert int(_pragma(fresh_sqlite_engine, "busy_timeout")) == 5000


def test_foreign_keys_on(fresh_sqlite_engine):
    assert int(_pragma(fresh_sqlite_engine, "foreign_keys")) == 1


def test_synchronous_normal(fresh_sqlite_engine):
    # NORMAL == 1 in SQLite's PRAGMA-as-int representation.
    assert int(_pragma(fresh_sqlite_engine, "synchronous")) == 1


def test_pragmas_applied_on_every_new_connection(fresh_sqlite_engine):
    """The connect listener must fire for *every* new pool connection,
    not just the first. Pull two distinct connections and confirm both
    have WAL set.
    """
    eng = fresh_sqlite_engine
    with eng.connect() as c1:
        m1 = c1.execute(text("PRAGMA journal_mode")).scalar()
    with eng.connect() as c2:
        m2 = c2.execute(text("PRAGMA journal_mode")).scalar()
    assert str(m1).lower() == "wal"
    assert str(m2).lower() == "wal"
