"""Concurrent writes against a WAL-mode SQLite DB — Sprint 3 Task 3.4.

5 threads * 10 writes each. Without WAL + busy_timeout this trips
``OperationalError: database is locked`` almost every time. With the
pragmas applied by ``engine.db._configure_sqlite_pragmas`` it should be
clean.
"""
from __future__ import annotations

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.db as db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Point the engine at a temp SQLite file and tear it down after."""
    monkeypatch.setenv("FLASK_DEBUG", "1")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'concurrent.db'}")
    monkeypatch.delenv("TESTFORTGE_DB", raising=False)

    prev_engine = db._engine
    prev_session = db._Session
    db._engine = None
    db._Session = None
    try:
        db.init_db()
        yield
    finally:
        if db._engine is not None:
            db._engine.dispose()
        db._engine = prev_engine
        db._Session = prev_session


def _hammer(thread_idx: int, writes_per_thread: int) -> int:
    """Insert ``writes_per_thread`` projects from one thread."""
    written = 0
    for i in range(writes_per_thread):
        # Unique name per (thread, iteration) — upsert_project derives
        # slug from name, so this also guarantees unique slugs.
        name = f"concurrent-{thread_idx}-{i}-{threading.get_ident()}"
        pid = db.upsert_project(name=name, owner_sid=f"sid-{thread_idx}")
        assert pid
        written += 1
    return written


def test_concurrent_writes_no_database_locked(fresh_db):
    """5 threads * 10 writes — should complete without raising
    ``OperationalError: database is locked``.
    """
    threads = 5
    writes_each = 10
    expected_total = threads * writes_each

    errors: list[BaseException] = []
    with ThreadPoolExecutor(max_workers=threads) as ex:
        futures = [ex.submit(_hammer, i, writes_each) for i in range(threads)]
        total = 0
        for fut in as_completed(futures):
            try:
                total += fut.result()
            except BaseException as exc:  # capture everything, surface below
                errors.append(exc)

    assert not errors, f"concurrent writers raised: {errors!r}"
    assert total == expected_total

    # Sanity: rows actually landed.
    counts = db.count_records()
    assert counts["projects"] >= expected_total
