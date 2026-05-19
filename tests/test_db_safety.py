"""Production-safety guard for SQLite — Sprint 3 Task 3.4.

``init_db()`` refuses to boot when:
  * the resolved URL is SQLite, AND
  * FLASK_DEBUG != "1" (i.e. this looks like a real deployment),

unless the operator explicitly opts in with
``TESTFORTGE_ALLOW_SQLITE_PROD=1``.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.db as db


@pytest.fixture
def reset_engine():
    """Tear down the module-level engine before/after each test so the
    guard runs every time. Other tests share the same module state, so
    we must restore it on the way out.
    """
    prev_engine = db._engine
    prev_session = db._Session
    db._engine = None
    db._Session = None
    yield
    if db._engine is not None:
        db._engine.dispose()
    db._engine = prev_engine
    db._Session = prev_session


def test_sqlite_in_non_debug_mode_raises(reset_engine, tmp_path, monkeypatch):
    """SQLite + FLASK_DEBUG unset (or =0) must hard-raise."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'prod.db'}")
    monkeypatch.delenv("FLASK_DEBUG", raising=False)
    monkeypatch.delenv("TESTFORTGE_ALLOW_SQLITE_PROD", raising=False)
    monkeypatch.delenv("TESTFORTGE_DB", raising=False)

    with pytest.raises(RuntimeError, match="SQLite in non-debug mode"):
        db.init_db()


def test_sqlite_with_flask_debug_zero_raises(reset_engine, tmp_path, monkeypatch):
    """Explicit FLASK_DEBUG=0 is just as bad as unset — must also raise."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'prod.db'}")
    monkeypatch.setenv("FLASK_DEBUG", "0")
    monkeypatch.delenv("TESTFORTGE_ALLOW_SQLITE_PROD", raising=False)
    monkeypatch.delenv("TESTFORTGE_DB", raising=False)

    with pytest.raises(RuntimeError, match="SQLite in non-debug mode"):
        db.init_db()


def test_escape_hatch_allows_boot(reset_engine, tmp_path, monkeypatch, caplog):
    """TESTFORTGE_ALLOW_SQLITE_PROD=1 downgrades raise -> warning."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'opt-in.db'}")
    monkeypatch.delenv("FLASK_DEBUG", raising=False)
    monkeypatch.setenv("TESTFORTGE_ALLOW_SQLITE_PROD", "1")
    monkeypatch.delenv("TESTFORTGE_DB", raising=False)

    # Should NOT raise.
    db.init_db()
    assert db._engine is not None


def test_flask_debug_one_is_fine(reset_engine, tmp_path, monkeypatch):
    """The normal local-dev case — FLASK_DEBUG=1 lets SQLite through."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'dev.db'}")
    monkeypatch.setenv("FLASK_DEBUG", "1")
    monkeypatch.delenv("TESTFORTGE_DB", raising=False)

    db.init_db()
    assert db._engine is not None


def test_postgres_url_bypasses_guard(reset_engine, monkeypatch):
    """A postgres URL must never trip the guard, regardless of debug
    flag. We don't actually need a live Postgres — we only assert the
    safety check itself doesn't raise. ``_assert_prod_safety`` is the
    isolated unit under test here.
    """
    monkeypatch.delenv("FLASK_DEBUG", raising=False)
    # Doesn't have to resolve — we call the inner assertion directly.
    db._assert_prod_safety("postgresql+psycopg2://user:pass@host:5432/dbname")
