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


def test_two_threads_initialising_at_once_build_one_schema(tmp_path,
                                                           monkeypatch):
    """``init_db`` is lazy — ``get_engine`` / ``session_scope`` call it when
    ``_engine`` is None — so two threads arriving together both ran
    ``create_all`` against the same new file, and the loser raised
    ``table test_case already exists``. Seen on CI, 2026-10-12, as an ERROR
    in the fixture above: a background thread left by an earlier test
    reached the database while the fixture was building its own.

    The interleaving is forced, not hoped for: the first ``create_all``
    waits until the second thread has had every chance to start its own.
    """
    monkeypatch.setenv("FLASK_DEBUG", "1")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'race.db'}")
    monkeypatch.delenv("TESTFORTGE_DB", raising=False)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_Session", None)

    real_create_all = db.Base.metadata.create_all
    calls: list[int] = []
    second_started = threading.Event()

    def slow_create_all(*args, **kwargs):
        calls.append(threading.get_ident())
        if len(calls) == 1:
            second_started.wait(timeout=5)
            # Give the second thread time to reach create_all if nothing
            # stops it; with the lock it is parked on init_db instead.
            threading.Event().wait(0.3)
        return real_create_all(*args, **kwargs)

    monkeypatch.setattr(db.Base.metadata, "create_all", slow_create_all)

    errors: list[BaseException] = []

    def first():
        try:
            db.init_db()
        except BaseException as exc:   # noqa: BLE001 — surfaced below
            errors.append(exc)

    def second():
        second_started.set()
        try:
            db.init_db()
        except BaseException as exc:   # noqa: BLE001 — surfaced below
            errors.append(exc)

    t1 = threading.Thread(target=first)
    t1.start()
    while not calls:                  # first is inside create_all
        threading.Event().wait(0.01)
    t2 = threading.Thread(target=second)
    t2.start()
    t1.join(10)
    t2.join(10)
    try:
        assert not errors, f"concurrent init raised: {errors!r}"
        assert len(calls) == 1, f"create_all ran {len(calls)} times"
        assert db._engine is not None and db._Session is not None
    finally:
        if db._engine is not None:
            db._engine.dispose()
