"""Keeping the search index current — ADR 0003 §4.4, layers 1 and 2.

Three layers that do not trust each other:

1. **ORM capture** (this module). ``after_flush`` collects the project id
   of every new, changed or deleted indexable row into ``session.info``;
   ``do_orm_execute`` does the same for bulk ``query.update()`` /
   ``query.delete()`` statements, whose rows the flush never sees, by
   reading ``project_id = :x`` out of the WHERE clause (and out of the SET
   clause, so ``move_artifacts`` marks the target project too).
   ``after_commit`` hands the ids to a per-project **debouncer**: a burst of
   writes to one project (a wipe-and-replace of 300 test cases) becomes one
   ``reindex_project`` job on the :mod:`engine.job_queue`, not 300.
   ``after_rollback`` drops what a failed transaction collected.
2. **Lag check** (:func:`check_lag`). Compares the project's
   ``updated_at`` with ``search_scope_state.indexed_at`` and queues a
   reindex when the index is behind. This is the net under layer 1: a write
   path no event can see (raw SQL, a WHERE clause this module cannot parse,
   a job lost when the process restarted) still bumps ``updated_at`` through
   ``touch_project`` or the pack writers.
3. **Content hash** (:mod:`engine.search.indexer`). Every reindex is a full
   pass over the scope; unchanged documents are skipped by hash, so layers 1
   and 2 may over-schedule freely.

With ``PLATFORM_SEARCH`` off the listeners stay registered and do nothing —
turning the flag on needs no restart (ADR §4.10).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Iterable

from engine.log import get_logger

log = get_logger(__name__)

_INFO_KEY = "search_reindex_pids"

#: Default debounce per project, seconds (ADR §4.4: 3 s).
DEFAULT_DEBOUNCE_S = 3.0


def debounce_seconds() -> float:
    raw = (os.environ.get("SEARCH_INDEX_DEBOUNCE_S") or "").strip()
    try:
        return max(0.0, float(raw)) if raw else DEFAULT_DEBOUNCE_S
    except ValueError:
        return DEFAULT_DEBOUNCE_S


def enabled() -> bool:
    try:
        from engine import features
        return features.effective("PLATFORM_SEARCH")
    except Exception:   # noqa: BLE001 — never break a write over search
        return False


# ── Dispatch: the debounced reindex job ───────────────────────────────

def _run_reindex(project_id: str) -> dict:
    from engine.search import indexer
    return indexer.reindex_project(project_id)


def _submit(project_id: str) -> None:
    """Put one reindex on the JobQueue. Never raises."""
    if not enabled():
        return
    try:
        from engine.job_queue import get_queue
        get_queue().submit("search_index", _run_reindex, project_id,
                           meta={"project_id": project_id})
    except Exception as exc:   # noqa: BLE001 — shutting down, etc.
        log.warning("search: could not queue reindex of %s: %s",
                    (project_id or "")[:8], exc)


class Debouncer:
    """Per-key trailing debounce on ``threading.Timer``.

    ``touch(key)`` (re)sets the key's deadline to now + delay. One timer per
    key; when it fires early because the deadline moved, it re-arms for the
    remainder instead of dispatching. ``drain()`` dispatches everything
    pending synchronously and cancels the timers — for tests and shutdown.
    """

    def __init__(self, dispatch: Callable[[str], Any],
                 delay: Callable[[], float] = debounce_seconds) -> None:
        self._dispatch = dispatch
        self._delay = delay
        self._due: dict[str, float] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _arm(self, key: str, seconds: float) -> None:
        t = threading.Timer(max(0.0, seconds), self._fire, args=(key,))
        t.daemon = True
        t.name = f"search-debounce-{key[:8]}"
        self._timers[key] = t
        t.start()

    def touch(self, key: str) -> None:
        delay = self._delay()
        with self._lock:
            self._due[key] = time.monotonic() + delay
            if key not in self._timers:
                self._arm(key, delay)

    def _fire(self, key: str) -> None:
        with self._lock:
            due = self._due.get(key)
            if due is None:
                self._timers.pop(key, None)
                return
            remaining = due - time.monotonic()
            if remaining > 0.005:
                self._arm(key, remaining)
                return
            self._due.pop(key, None)
            self._timers.pop(key, None)
        try:
            self._dispatch(key)
        except Exception as exc:   # noqa: BLE001
            log.warning("search: debounced dispatch of %s failed: %s",
                        key[:8], exc)

    def pending(self) -> set[str]:
        with self._lock:
            return set(self._due)

    def cancel(self) -> None:
        with self._lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()
            self._due.clear()

    def drain(self, dispatch: Callable[[str], Any] | None = None) -> list[str]:
        """Dispatch every pending key now, in this thread. Returns them."""
        with self._lock:
            keys = sorted(self._due)
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()
            self._due.clear()
        fn = dispatch or self._dispatch
        for k in keys:
            fn(k)
        return keys


_debouncer = Debouncer(lambda pid: _submit(pid))


def schedule(project_ids: Iterable[str | None]) -> None:
    """Queue a debounced reindex for each project id (``None`` skipped)."""
    if not enabled():
        return
    for pid in project_ids:
        if pid:
            _debouncer.touch(str(pid))


def pending() -> set[str]:
    return _debouncer.pending()


def drain(*, sync: bool = True) -> list[str]:
    """Run every pending reindex now. ``sync`` runs it in this thread
    (tests); otherwise it goes on the JobQueue without the remaining
    debounce."""
    return _debouncer.drain(_run_reindex if sync else _submit)


def reset() -> None:
    """Forget everything pending (tests)."""
    _debouncer.cancel()


# ── Layer 2: the lag check ────────────────────────────────────────────

def check_lag(project_id: str) -> bool:
    """True when the project's index is behind its rows; a reindex has then
    been scheduled. ``/api/search`` reports this as ``stale: true``.
    Never raises; with the flag off it reports False and does nothing."""
    if not project_id or not enabled():
        return False
    try:
        from engine.search import indexer
        stale = indexer.is_stale(project_id)
    except Exception as exc:   # noqa: BLE001
        log.debug("search lag check failed for %s: %s", project_id[:8], exc)
        return False
    if stale:
        schedule([project_id])
    return stale


# ── Layer 1: ORM capture ──────────────────────────────────────────────

_MODELS: dict[str, Any] = {}


def _models() -> dict[str, Any]:
    if not _MODELS:
        from engine import db as _db
        _MODELS.update({
            "pid": (_db.TestCase, _db.ChecklistItem, _db.BugReport,
                    _db.ExecutionRun, _db.Estimation),
            "case": _db.ExecutionCaseResult,
            "run": _db.ExecutionRun,
            "project": _db.Project,
        })
    return _MODELS


def _pids_of(session) -> set:
    return session.info.setdefault(_INFO_KEY, set())


def _history_values(obj, attr: str) -> list:
    """Current and previous values of *attr* without triggering a load."""
    from sqlalchemy import inspect as sa_inspect
    out = []
    current = obj.__dict__.get(attr)
    if current is not None:
        out.append(current)
    try:
        hist = sa_inspect(obj).attrs[attr].history
        out.extend(v for v in (hist.deleted or ()) if v is not None)
    except Exception:   # noqa: BLE001
        pass
    return out


def _after_flush(session, _flush_context) -> None:
    if not enabled():
        return
    try:
        m = _models()
        pids = _pids_of(session)
        run_ids: set = set()
        for bucket in (session.new, session.dirty, session.deleted):
            for obj in bucket:
                if isinstance(obj, m["pid"]):
                    pids.update(_history_values(obj, "project_id"))
                elif isinstance(obj, m["case"]):
                    run_ids.update(_history_values(obj, "run_id"))
                elif isinstance(obj, m["project"]):
                    pid = obj.__dict__.get("id")
                    if obj in session.deleted:
                        pids.add(pid)
                    else:
                        from sqlalchemy import inspect as sa_inspect
                        try:
                            if sa_inspect(obj).attrs["org_id"].history \
                                    .has_changes():
                                pids.add(pid)
                        except Exception:   # noqa: BLE001
                            pass
        if run_ids:
            from sqlalchemy import select
            run = m["run"]
            rows = session.connection().execute(
                select(run.project_id).where(run.id.in_(list(run_ids))))
            pids.update(r[0] for r in rows if r[0])
        pids.discard(None)
    except Exception as exc:   # noqa: BLE001 — never break a write
        log.debug("search after_flush capture failed: %s", exc)


def _bound_values(clause) -> list:
    """The literal values bound to ``project_id`` in *clause*:
    ``project_id = :x`` and ``project_id IN (…)``."""
    from sqlalchemy.sql import visitors
    from sqlalchemy.sql.elements import BinaryExpression, BindParameter
    out: list = []
    if clause is None:
        return out
    for el in visitors.iterate(clause):
        if not isinstance(el, BinaryExpression):
            continue
        left, right = el.left, el.right
        if getattr(left, "key", None) != "project_id":
            continue
        if isinstance(right, BindParameter):
            val = right.effective_value
            if isinstance(val, (list, tuple, set)):
                out.extend(val)
            elif val is not None:
                out.append(val)
    return out


def _do_orm_execute(state) -> None:
    if not (state.is_update or state.is_delete) or not enabled():
        return
    try:
        m = _models()
        mapper = state.bind_mapper
        cls = getattr(mapper, "class_", None)
        if cls is None or not (issubclass(cls, m["pid"])):
            if cls is not None and issubclass(cls, m["case"]):
                log.debug("search: bulk case-result statement — left to the "
                          "lag check")
            return
        stmt = state.statement
        pids = _pids_of(state.session)
        pids.update(_bound_values(getattr(stmt, "whereclause", None)))
        values = getattr(stmt, "_values", None) or {}
        for col, val in dict(values).items():
            if getattr(col, "key", col) == "project_id":
                v = getattr(val, "effective_value", val)
                if v is not None:
                    pids.add(v)
        pids.discard(None)
    except Exception as exc:   # noqa: BLE001
        log.debug("search do_orm_execute capture failed: %s", exc)


def _after_commit(session) -> None:
    pids = session.info.pop(_INFO_KEY, None)
    if pids:
        schedule(pids)


def _after_rollback(session) -> None:
    session.info.pop(_INFO_KEY, None)


_INSTALLED = False
_INSTALL_LOCK = threading.Lock()


def install() -> None:
    """Register the listeners on every ``Session``. Idempotent."""
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        from sqlalchemy import event
        from sqlalchemy.orm import Session
        event.listen(Session, "after_flush", _after_flush)
        event.listen(Session, "do_orm_execute", _do_orm_execute)
        event.listen(Session, "after_commit", _after_commit)
        event.listen(Session, "after_rollback", _after_rollback)
        _INSTALLED = True


def installed() -> bool:
    return _INSTALLED


# ── Boot: the global corpora, in the background ───────────────────────

def start_boot_indexing(delay: float | None = None) -> threading.Thread | None:
    """Index the global corpora on a daemon thread after *delay* seconds
    (``SEARCH_BOOT_INDEX_DELAY_S``, default 15). Returns the thread, or
    None when the flag is off. Never blocks boot and never raises; an
    unchanged corpus is a fingerprint compare and nothing else."""
    if not enabled():
        log.info("search: PLATFORM_SEARCH is off — no boot indexing")
        return None
    if delay is None:
        try:
            delay = float(os.environ.get("SEARCH_BOOT_INDEX_DELAY_S") or 15)
        except ValueError:
            delay = 15.0

    def _run() -> None:
        try:
            time.sleep(max(0.0, delay))
            from engine.search import indexer
            res = indexer.index_global()
            log.info("search: global corpora at boot: %s", res)
        except Exception as exc:   # noqa: BLE001 — never fatal
            log.warning("search: global indexing at boot failed: %s", exc)

    t = threading.Thread(target=_run, name="search-boot-index", daemon=True)
    t.start()
    return t


__all__ = ["install", "installed", "schedule", "pending", "drain", "reset",
           "check_lag", "start_boot_indexing", "Debouncer",
           "debounce_seconds", "enabled"]
