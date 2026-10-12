"""Index maintenance — ADR 0003 §4.4 layers 1 and 2, §7.3.

What is asserted is the outcome a user would see: after a write and a
drained debounce, ``search_doc`` says what the row says. Where a test is
about *scheduling* it asserts the exact set of projects queued — "one job,
not 300" is a set of size one, not "submit was called".

No test here starts a timer and sleeps hoping it fires in time. The
debounce delay is set to an hour and :func:`hooks.drain` runs what is
pending, in this thread; the one test of the real timer path waits on an
event with a deadline. The debouncer's re-arm logic is driven by calling
its ``_fire`` directly with a deadline the test controls.
"""
from __future__ import annotations

import secrets
import threading
import time

import pytest
from sqlalchemy import text

from engine import db as _db
from engine.db import SearchDoc
from engine.search import hooks, indexer


@pytest.fixture
def search_on(monkeypatch):
    monkeypatch.setenv("PLATFORM_SEARCH", "1")
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("SEARCH_INDEX_DEBOUNCE_S", "3600")
    _db.init_db()
    hooks.install()
    hooks.reset()
    yield
    hooks.reset()


def _project(org_id: str | None = None) -> str:
    return _db.upsert_project(f"hooks {secrets.token_hex(4)}", org_id=org_id)


def _titles(pid: str) -> dict[tuple[str, str], str]:
    with _db.session_scope() as sess:
        return {(r.kind, r.ref): r.title for r in sess.query(SearchDoc)
                .filter(SearchDoc.project_id == pid, SearchDoc.chunk_no == 0)}


TC = {"id": "TC-001", "summary": "Checkout total", "test_steps": "pay",
      "expected_result": "paid"}


def test_hooks_are_installed_at_boot():
    import app  # noqa: F401 — the module conftest already imported
    assert hooks.installed()


# ── layer 1: ORM capture → debounce ──────────────────────────────────

class TestCapture:
    def test_a_300_row_pack_save_is_one_pending_reindex(self, search_on):
        pid = _project()
        hooks.reset()
        _db.save_test_cases(pid, [dict(TC, id=f"TC-{i:03d}",
                                       summary=f"case {i}")
                                  for i in range(300)])
        assert hooks.pending() == {pid}
        assert hooks.drain() == [pid]
        assert len(_titles(pid)) == 300
        assert hooks.pending() == set()

    def test_editable_create_patch_remove_reach_the_index(self, search_on):
        from engine import editable
        pid = _project()
        hooks.drain()
        made = editable.create("test_case", pid, {"summary": "Не можу увійти"})
        assert hooks.pending() == {pid}
        hooks.drain()
        assert _titles(pid)[("test_case", made["id"])].endswith(
            "Не можу увійти")

        editable.patch("test_case", pid, made["id"],
                       {"summary": "Login works again"})
        assert hooks.pending() == {pid}
        hooks.drain()
        assert _titles(pid)[("test_case", made["id"])].endswith(
            "Login works again")

        editable.remove("test_case", pid, made["id"])
        assert hooks.pending() == {pid}
        hooks.drain()
        assert ("test_case", made["id"]) not in _titles(pid)

    def test_a_bulk_delete_the_flush_never_sees_is_caught(self, search_on):
        """``bulk_update_bugs(action='delete')`` is ``query.delete()``: no
        row is loaded, so after_flush has nothing. do_orm_execute reads the
        project id out of the WHERE clause."""
        pid = _project()
        bid = _db.save_bug(pid, {"id": "BUG-1", "title": "Cart total wrong"})
        hooks.drain()
        assert ("bug", "BUG-1") in _titles(pid)
        assert _db.bulk_update_bugs(pid, [bid], action="delete", value=None,
                                    actor="t") == 1
        assert hooks.pending() == {pid}
        hooks.drain()
        assert ("bug", "BUG-1") not in _titles(pid)

    def test_move_artifacts_marks_both_projects(self, search_on):
        src, dst = _project(), _project()
        _db.save_test_cases(src, [TC])
        hooks.drain()
        _db.move_artifacts(src, dst, ["test_cases"])
        assert hooks.pending() == {src, dst}
        hooks.drain()
        assert ("test_case", "TC-001") not in _titles(src)
        assert ("test_case", "TC-001") in _titles(dst)

    def test_a_case_result_marks_its_runs_project(self, search_on):
        pid = _project()
        run_id = _db.start_execution_run(pid, {"mode": "manual"})
        hooks.drain()
        _db.save_case_result(run_id, case_external_id="TC-009",
                             status="Failed", notes="boom")
        assert hooks.pending() == {pid}
        hooks.drain()
        assert ("case_result", f"run:{run_id}:TC-009") in _titles(pid)

    def test_a_project_changing_org_is_marked(self, search_on):
        org = _db.create_organization(f"Org {secrets.token_hex(3)}")
        pid = _project()
        _db.save_test_cases(pid, [TC])
        hooks.drain()
        assert _db.set_project_org(pid, org)
        assert hooks.pending() == {pid}

    def test_touching_a_project_is_not_a_reindex(self, search_on):
        pid = _project()
        hooks.reset()
        assert _db.touch_project(pid)
        assert hooks.pending() == set()

    def test_a_rolled_back_write_schedules_nothing(self, search_on):
        pid = _project()
        hooks.reset()
        with pytest.raises(RuntimeError):
            with _db.session_scope() as sess:
                sess.add(_db.TestCase(project_id=pid, external_id="TC-777",
                                      summary="never committed"))
                sess.flush()                  # after_flush has captured it …
                raise RuntimeError("abort")   # … and the commit never comes
        assert hooks.pending() == set()

    def test_a_rollback_does_not_leak_into_the_sessions_next_commit(
            self, search_on):
        """One long-lived session: flush a tracked row, roll back, then
        commit something untracked. Without after_rollback clearing what
        the flush captured, that commit would schedule the project."""
        pid = _project()
        hooks.reset()
        sess = _db._Session()
        try:
            sess.add(_db.TestCase(project_id=pid, external_id="TC-778",
                                  summary="rolled back"))
            sess.flush()
            sess.rollback()
            sess.get(_db.Project, pid).description = "untracked change"
            sess.commit()
        finally:
            sess.close()
        assert hooks.pending() == set()

    def test_flag_off_captures_nothing_and_writes_nothing(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_SEARCH", "0")
        monkeypatch.setenv("SEARCH_INDEX_DEBOUNCE_S", "3600")
        hooks.install()
        hooks.reset()
        pid = _project()
        _db.save_test_cases(pid, [TC])
        bid = _db.save_bug(pid, {"id": "BUG-1", "title": "x"})
        _db.bulk_update_bugs(pid, [bid], action="delete", value=None,
                             actor="t")
        assert hooks.pending() == set()
        assert indexer.reindex_project(pid)["skipped"]
        assert hooks.check_lag(pid) is False
        with _db.session_scope() as sess:
            assert sess.query(SearchDoc).filter(
                SearchDoc.project_id == pid).count() == 0

    def test_after_commit_goes_to_the_job_queue(self, search_on, monkeypatch):
        """The real path, end to end: a commit, the timer, a JobQueue job.
        Waits on an event with a deadline — no fixed sleep."""
        monkeypatch.setenv("SEARCH_INDEX_DEBOUNCE_S", "0.05")
        done = threading.Event()
        submitted = []

        class FakeQueue:
            def submit(self, kind, func, *args, meta=None, **kw):
                submitted.append((kind, args, meta))
                func(*args, **kw)
                done.set()
                return "job"

        import engine.job_queue as jq
        monkeypatch.setattr(jq, "get_queue", lambda: FakeQueue())
        pid = _project()
        hooks.reset()
        _db.save_test_cases(pid, [TC])
        assert done.wait(10), "debounced reindex never reached the queue"
        assert submitted == [("search_index", (pid,), {"project_id": pid})]
        assert ("test_case", "TC-001") in _titles(pid)


# ── the debouncer itself ─────────────────────────────────────────────

class TestDebouncer:
    def test_a_burst_dispatches_once_and_only_after_the_last_touch(self):
        calls: list[str] = []
        d = hooks.Debouncer(calls.append, delay=lambda: 3600)
        try:
            for _ in range(50):
                d.touch("p1")
            d.touch("p2")
            assert d.pending() == {"p1", "p2"}
            # The timer firing before the deadline re-arms, never dispatches.
            d._fire("p1")
            assert calls == [] and "p1" in d.pending()
            # Deadline reached: exactly one dispatch.
            with d._lock:
                d._due["p1"] = time.monotonic() - 1
            d._fire("p1")
            d._fire("p1")
            assert calls == ["p1"]
            assert d.pending() == {"p2"}
            assert d.drain() == ["p2"]
            assert calls == ["p1", "p2"]
        finally:
            d.cancel()

    def test_a_touch_while_waiting_moves_the_deadline(self):
        calls: list[str] = []
        d = hooks.Debouncer(calls.append, delay=lambda: 3600)
        try:
            d.touch("p")
            with d._lock:
                d._due["p"] = time.monotonic() - 1     # was due …
            d.touch("p")                               # … then touched again
            d._fire("p")
            assert calls == []
        finally:
            d.cancel()


# ── layer 2: the lag check catches what the hooks cannot ────────────

class TestLagCheck:
    def test_a_pack_save_the_hooks_missed_is_caught(self, search_on,
                                                    monkeypatch):
        """The delete+reinsert pack write while capture was not running
        (flag flipped later, a lost job, a restart): updated_at moved, the
        index did not, and the first search finds out."""
        pid = _project()
        _db.save_test_cases(pid, [TC])
        hooks.drain()
        assert not indexer.is_stale(pid)

        monkeypatch.setenv("PLATFORM_SEARCH", "0")
        _db.save_test_cases(pid, [dict(TC, summary="Refund total")])
        monkeypatch.setenv("PLATFORM_SEARCH", "1")
        assert hooks.pending() == set()
        assert _titles(pid)[("test_case", "TC-001")].endswith("Checkout total")

        assert hooks.check_lag(pid) is True
        assert hooks.pending() == {pid}
        hooks.drain()
        assert _titles(pid)[("test_case", "TC-001")].endswith("Refund total")
        assert hooks.check_lag(pid) is False
        assert hooks.pending() == set()

    def test_raw_sql_plus_touch_is_caught(self, search_on):
        """A write no ORM event can see — Core SQL on a bare connection —
        followed by the touch every persistence helper does."""
        pid = _project()
        _db.save_test_cases(pid, [TC])
        hooks.drain()
        with _db.get_engine().begin() as conn:
            conn.execute(text("UPDATE test_case SET summary = 'Raw edit' "
                              "WHERE project_id = :p"), {"p": pid})
        assert hooks.pending() == set()
        _db.touch_project(pid)
        assert hooks.check_lag(pid)
        hooks.drain()
        assert _titles(pid)[("test_case", "TC-001")].endswith("Raw edit")

    def test_a_never_indexed_project_is_stale(self, search_on):
        pid = _project()
        hooks.reset()
        assert hooks.check_lag(pid) is True
        assert hooks.pending() == {pid}

    def test_an_unknown_project_is_not_stale(self, search_on):
        assert hooks.check_lag("0" * 32) is False
        assert hooks.pending() == set()


# ── boot ─────────────────────────────────────────────────────────────

class TestBootIndexing:
    def test_flag_off_starts_nothing(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_SEARCH", "0")
        assert hooks.start_boot_indexing(delay=0) is None

    def test_flag_on_indexes_the_global_corpora_off_thread(
            self, search_on, monkeypatch):
        calls = []
        started_in = []

        def fake_index_global(**kw):
            started_in.append(threading.current_thread().name)
            calls.append(kw)
            return {}

        monkeypatch.setattr(indexer, "index_global", fake_index_global)
        t = hooks.start_boot_indexing(delay=0)
        assert t is not None and t.daemon
        t.join(10)
        assert calls == [{}]
        assert started_in == ["search-boot-index"]

    def test_a_failure_at_boot_is_swallowed(self, search_on, monkeypatch):
        def boom(**kw):
            raise RuntimeError("db down")
        monkeypatch.setattr(indexer, "index_global", boom)
        t = hooks.start_boot_indexing(delay=0)
        t.join(10)
        assert not t.is_alive()


# ── the backfill CLI ─────────────────────────────────────────────────

class TestBackfillCli:
    def test_refuses_with_the_flag_off(self, monkeypatch, capsys):
        from tools import search_backfill
        monkeypatch.setenv("PLATFORM_SEARCH", "0")
        pid = _project()
        _db.save_test_cases(pid, [TC])
        assert search_backfill.main(["--project", pid]) == 2
        assert _titles(pid) == {}

    def test_force_indexes_a_project(self, monkeypatch, capsys):
        from tools import search_backfill
        monkeypatch.setenv("PLATFORM_SEARCH", "0")
        pid = _project()
        _db.save_test_cases(pid, [TC])
        assert search_backfill.main(["--project", pid, "--force",
                                     "--json"]) == 0
        assert ("test_case", "TC-001") in _titles(pid)
        out = capsys.readouterr().out
        assert '"upserted": 1' in out

    def test_nothing_to_do_is_an_argument_error(self):
        from tools import search_backfill
        assert search_backfill.main([]) == 2

    def test_to_pgvector_on_sqlite_copies_nothing(self, search_on):
        from tools import search_backfill
        assert search_backfill.main(["--to-pgvector"]) == 0
