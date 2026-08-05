"""TestFortge — the one way to read a project's work (E3.2).

Every module currently reaches for ``session["test_cases_data"]`` and
friends, falling back to Postgres when the session turns out to be empty.
That pattern is why two people cannot share a project: the session is one
browser, so it cannot be the source of truth for a team's work. ADR 0001
(``docs/plans/adr/0001-project-workspace-source-of-truth.md``) is the
decision this module implements.

What it is
----------
A read-through repository over the artefacts that have a home in the
database — test cases, checklist items, bug reports, estimations and
execution runs. Callers ask for a *project*, never for a session key::

    # before
    tcs = reconstruct_test_cases(session.get("test_cases_data", []))

    # after
    tcs = reconstruct_test_cases(workspace.test_cases(project_id))

Caching is per request and lives in ``flask.g``. Between requests there is
no cache, deliberately: a cache that outlives the request is a second
source of truth, which is the thing being removed.

The transition
--------------
Reads honour ``WORKSPACE_DB_FIRST``:

* **off** (default) — session first, database second. Byte-for-byte what
  the modules do today, so moving a module onto this repository is a pure
  refactor with no behaviour change to review.
* **on** — the database answers, and the session is not consulted for
  artefacts at all.

One flag, one switch, one place to look when the answer is wrong. E3.3 and
E3.4 move the modules; this module is what they move onto.

What is deliberately *not* here
-------------------------------
``raw_requirements``, ``user_stories``, ``traceability_data``,
``custom_prompt`` and ``execution_results`` stay in the session, because
**there is no table for them**. They are not an oversight in this task —
they need schema first, and pretending otherwise would mean a repository
that silently loses half of what a user typed. Named in
``SESSION_ONLY_KEYS`` below so the gap is greppable rather than folklore.
"""
from __future__ import annotations

from typing import Any, Callable

from engine.log import get_logger

log = get_logger(__name__)

#: Artefact kinds this repository owns, and the session key each one
#: shadowed while the session was the source of truth.
KINDS: dict[str, str] = {
    "test_cases": "test_cases_data",
    "checklist": "checklist_data",
    "bugs": "bug_reports_data",
    "estimation": "estimation_result",
    "runs": "test_runs",
}

#: Session keys with no table behind them. Reading these through the
#: repository would be a lie, so it refuses to; they remain the session's
#: business until somebody gives them a schema.
#:
#: This is the honest boundary of E3.2. ``raw_requirements`` in particular
#: means a cold start still loses the *input* a pack was generated from,
#: even once every generated artefact survives.
SESSION_ONLY_KEYS: tuple[str, ...] = (
    "raw_requirements", "user_stories", "traceability_data",
    "custom_prompt", "execution_results", "project_setup",
)


def db_first() -> bool:
    """True when the database is the source of truth for artefacts."""
    from engine import features
    return features.is_enabled("WORKSPACE_DB_FIRST")


# ── Per-request cache ─────────────────────────────────────────────

_CACHE_ATTR = "_workspace_cache"


def _cache() -> dict | None:
    """The current request's cache, or ``None`` outside a request.

    ``None`` rather than a module-level dict on purpose. The detached
    ``runner_worker`` subprocess and the CLI both call in here with no
    request context, and a process-lifetime cache there would serve one
    run's test cases to the next.
    """
    try:
        from flask import g, has_request_context
        if not has_request_context():
            return None
        store = getattr(g, _CACHE_ATTR, None)
        if store is None:
            store = {}
            setattr(g, _CACHE_ATTR, store)
        return store
    except Exception:      # pragma: no cover — no Flask at all
        return None


def _cached(project_id: str, kind: str, produce: Callable[[], Any]) -> Any:
    store = _cache()
    if store is None:
        return produce()
    key = (project_id, kind)
    if key not in store:
        store[key] = produce()
    return store[key]


def invalidate(project_id: str | None = None,
               kind: str | None = None) -> None:
    """Drop cached reads so the next one sees a write.

    Called by every write helper below. A caller that writes through
    ``engine.db`` directly has to call this itself — which is the argument
    for writing through this module instead.
    """
    store = _cache()
    if store is None:
        return
    if project_id is None and kind is None:
        store.clear()
        return
    for key in [k for k in store
                if (project_id is None or k[0] == project_id)
                and (kind is None or k[1] == kind)]:
        store.pop(key, None)


# ── Session shim (transition only) ────────────────────────────────

def _session_value(kind: str):
    """What the session holds for *kind*, or ``None``.

    Returns ``None`` both when there is no request context and when the
    key is absent or empty, so callers cannot tell "no session" from
    "empty session" — they should not care, and conflating them is what
    lets this whole shim be deleted in one commit once the flag is on
    everywhere.
    """
    session_key = KINDS.get(kind)
    if not session_key:
        return None
    try:
        from flask import has_request_context, session
        if not has_request_context():
            return None
        value = session.get(session_key)
        return value or None
    except Exception:      # pragma: no cover
        return None


def _read(project_id: str, kind: str, from_db: Callable[[], Any]):
    """Resolve one artefact kind, honouring the transition flag."""
    if not project_id:
        # No project means nothing in the database can be scoped, so the
        # session is the only possible answer — including when db_first is
        # on. This is the anonymous / pre-project flow, and it keeps
        # working.
        return _session_value(kind) or _empty_for(kind)

    def _produce():
        if not db_first():
            in_session = _session_value(kind)
            if in_session is not None:
                return in_session
        try:
            return from_db()
        except Exception as exc:
            # A database blip must not blank a page that had data. Fall
            # back to whatever the session still holds and say so.
            log.warning("workspace: %s read failed for %s: %s",
                        kind, project_id[:8], exc)
            return _session_value(kind) or _empty_for(kind)

    return _cached(project_id, kind, _produce)


def _empty_for(kind: str):
    return None if kind == "estimation" else []


# ── Row shaping ───────────────────────────────────────────────────
#
# Test cases and checklist items are already shaped for their dataclasses
# by ``engine.db.load_*``. Bugs and runs were not: their mapping lived
# inside ``routes/bugs.py`` and inline in ``routes/projects.py``, which is
# why switching a project used to lose run history — one of the two copies
# simply did not know about a field. Both live here now, in the module
# whose contract is "artefacts in the shape the app consumes", and the
# route modules delegate. Engine does not import routes; that direction is
# the layering violation, and it is how the duplication started.

def bug_row_to_dict(row: dict) -> dict:
    """A ``engine.db.list_bugs`` row → the flat shape ``dict_to_bug`` takes.

    Three mappings worth knowing: the integer row id becomes ``db_id``
    while ``external_id`` becomes the displayed ``id``; ``extra`` is
    unpacked so a JSON blob written before a column existed cannot shadow
    the first-class field that replaced it.
    """
    extra = row.get("extra") or {}
    created = row.get("created_at")
    if not isinstance(created, str):
        created = created.isoformat() if created else ""
    return {
        "id": row.get("external_id") or f"BUG-{int(row.get('id') or 0):03d}",
        "db_id": int(row.get("id") or 0),
        "title": row.get("title") or "",
        "severity": row.get("severity") or "Minor",
        "priority": row.get("priority") or "Medium",
        "status": row.get("status") or "Open",
        "environment": row.get("environment") or "",
        "preconditions": row.get("preconditions")
                         or extra.get("preconditions", ""),
        "attachment": row.get("attachment") or "",
        "bug_area": row.get("bug_area") or extra.get("bug_area", "")
                    or "Functional",
        "steps_to_reproduce": row.get("steps_to_reproduce") or "",
        "actual_result": row.get("actual_result") or "",
        "expected_result": row.get("expected_result") or "",
        "frequency": extra.get("frequency", "Always"),
        "affects_version": row.get("version") or "",
        "found_in_build": extra.get("found_in_build", ""),
        "attachments": extra.get("attachments") or [],
        "linked_item_id": extra.get("linked_item_id", ""),
        "linked_item_type": extra.get("linked_item_type", ""),
        "reporter": row.get("reporter") or "",
        "assignee": row.get("assignee") or extra.get("assignee", ""),
        "created_at": created,
        "component": extra.get("component", ""),
        "labels": extra.get("labels") or [],
        "comment": row.get("comment") or "",
    }


def run_row_to_dict(row: dict) -> dict:
    """An ``engine.db.list_execution_runs`` row → the template's run shape.

    Every key the template iterates has to be present, including the empty
    ones: a missing key is an ``UndefinedError`` mid-render, which is a 500
    on a page that had been working.
    """
    env = row.get("env_payload") or {}
    started = row.get("started_at")
    if not isinstance(started, str):
        started = started.isoformat() if started else ""
    return {
        # Filled by ``runs()`` from execution_case_result. Empty here so a
        # caller that only needs a run's metadata does not pay for its
        # items.
        "results": [],
        "run_id": row.get("id") or row.get("run_id"),
        "db_run_id": row.get("id"),
        "source": env.get("source", ""),
        # How the run was executed: "tc_driven", "walkthrough" or "live".
        # /bug-reports filters on it, so a run whose mode is unknown cannot
        # be filtered — which is what happened for every run read from the
        # database until E3.4 started storing it.
        "mode": env.get("mode", ""),
        # Merged into the payload after the run finished — see
        # engine.db.merge_run_env.
        "walkthrough_findings": env.get("walkthrough_findings") or [],
        "walkthrough_tc_bindings": env.get("walkthrough_tc_bindings") or [],
        "tester_id": env.get("tester_id", ""),
        "tester_name": env.get("tester_name", ""),
        "environment": env.get("environment", ""),
        "env_type": env.get("env_type", ""),
        "testing_types": ", ".join(env.get("testing_types") or []),
        "stats": row.get("stats") or {},
        # Filled by ``runs()`` from count_bugs_by_run. Hardcoding it to 0
        # here is what made every run read from the database claim it had
        # filed no bugs, on a page whose whole job is to show that number.
        "bug_count": 0,
        "site_url": "",
        "base_url": row.get("base_url", ""),
        "headless": row.get("browser_visibility") == "headless",
        "record_video": bool(row.get("record_video")),
        "automation_used": True,
        "created_at": started,
    }


# ── Reads ─────────────────────────────────────────────────────────

def test_cases(project_id: str | None) -> list[dict]:
    """The project's test cases, shaped for ``TestCase(**d)``."""
    from engine import db as _db
    return _read(project_id or "", "test_cases",
                 lambda: _db.load_test_cases(project_id) or [])


def edit_metadata(project_id: str | None,
                  kind: str = "test_cases") -> dict[str, dict]:
    """Per-row version and provenance, keyed by public id (E4.3).

    Separate from the pack because the pack is shaped for the in-session
    dataclass, which has no version field — see ``db.load_edit_metadata``.
    A template renders the two side by side: the pack for the content, this
    for what the editor needs to send back.

    Empty when the workspace is still session-first: without a row in the
    database there is no version to hold, and an editor cannot be reached in
    that configuration anyway (``EDITORS_ENABLED`` requires
    ``WORKSPACE_DB_FIRST``).

    Not routed through ``_read``: that function's job is to choose between
    the session and the database, and it answers a failed read with the
    session's copy or an empty *list*. There is no session copy of a row
    version, and a list here would break the template that indexes this by
    id. So the fallback is an empty mapping, which renders the fields
    read-only — the honest outcome when the versions cannot be read.
    """
    from engine import db as _db
    if not project_id or not db_first():
        return {}

    def _produce() -> dict[str, dict]:
        try:
            return _db.load_edit_metadata(project_id, kind) or {}
        except Exception as exc:
            log.warning("workspace: edit metadata read failed for %s: %s",
                        project_id[:8], exc)
            return {}

    return _cached(project_id, f"edit_metadata:{kind}", _produce)


def checklist(project_id: str | None) -> list[dict]:
    """The project's checklist items, shaped for ``ChecklistItem(**d)``."""
    from engine import db as _db
    return _read(project_id or "", "checklist",
                 lambda: _db.load_checklist(project_id) or [])


def bugs(project_id: str | None, *, run_id: int | None = None) -> list[dict]:
    """The project's bug reports, newest first.

    ``run_id`` scopes to one execution run and **bypasses the session
    shim**: run scoping is a database-era feature, and the session cache
    is not run-scoped, so answering from it would return every bug while
    claiming to return one run's.
    """
    from engine import db as _db
    if run_id is not None:
        if not project_id:
            return []
        return _cached(project_id, f"bugs:{run_id}",
                       lambda: [bug_row_to_dict(r) for r in
                        (_db.list_bugs(project_id, run_id=run_id) or [])])

    return _read(project_id or "", "bugs",
                 lambda: [bug_row_to_dict(r)
                          for r in (_db.list_bugs(project_id) or [])])


def latest_estimation(project_id: str | None) -> dict | None:
    """The most recent estimation's result payload, or ``None``."""
    from engine import db as _db

    def _from_db():
        rows = _db.list_estimations(project_id, limit=1) or []
        if not rows:
            return None
        row = rows[0] or {}
        payload = row.get("result_payload") or row.get("result") or {}
        return payload if isinstance(payload, dict) and payload else None

    return _read(project_id or "", "estimation", _from_db)


def case_result_to_dict(row: dict) -> dict:
    """An ``execution_case_result`` row → the per-item shape the run views use.

    ``duration_ms`` is deliberately absent: the table has no column for it,
    so a run read back from the database cannot report how long an item
    took. The template already guards with ``| default(0, true)`` so this
    degrades to a zero rather than an error — but it is a real gap, not a
    rounding one, and closing it needs a migration. Same for ``video`` and
    ``screenshots``, which are disk paths the row does not carry.

    ``source`` used to be guessed here, from whether the row had notes.
    That produced a plausible wrong answer — the worst kind in a report —
    so E3.4 gave the table a column instead.
    """
    return {
        "item_id": row.get("case_external_id") or "",
        "status": row.get("status") or "",
        "kind": row.get("case_kind") or "test_case",
        "source": row.get("source") or "auto",
        # The display id, and "" when there is none — matching the shape
        # every consumer already assumes. The integer FK stays available
        # separately for anything that needs to join on it.
        "bug_id": row.get("bug_external_id") or "",
        "bug_row_id": row.get("bug_report_id"),
        "comment": row.get("notes") or "",
        "evidence_path": row.get("evidence_path") or "",
    }


def runs(project_id: str | None, *, limit: int = 20,
         with_results: bool = True) -> list[dict]:
    """Execution runs, newest last, shaped as the templates expect.

    ``with_results`` fills each run's per-item results from
    ``execution_case_result`` in **one** grouped query. It defaults to True
    because the run-history template reads ``run.results`` for its item
    count and duration column: shipping runs with a permanently empty
    ``results`` list was the E3.4 bug that made a run read from the
    database look like a run that had executed nothing.
    """
    from engine import db as _db

    def _from_db():
        rows = _db.list_execution_runs(project_id, limit=limit) or []
        shaped = [run_row_to_dict(r) for r in rows][-limit:]
        if not (with_results and shaped):
            return shaped
        try:
            grouped = _db.list_case_results_for_runs(
                [r["db_run_id"] for r in shaped if r.get("db_run_id")])
        except Exception as exc:      # pragma: no cover — best-effort
            log.warning("workspace: case results unavailable: %s", exc)
            return shaped
        for run in shaped:
            for item in grouped.get(run.get("db_run_id"), []):
                run["results"].append(case_result_to_dict(item))

        # The authoritative count, from the bug table's own run_id — not
        # derived from the results above, because a bug can be filed for a
        # run without a per-item result behind it (an infrastructure
        # summary bug, for one). One grouped query for the whole list.
        try:
            per_run = _db.count_bugs_by_run(project_id) or {}
        except Exception as exc:      # pragma: no cover — best-effort
            log.warning("workspace: bug counts unavailable: %s", exc)
            return shaped
        for run in shaped:
            run["bug_count"] = int(per_run.get(run.get("db_run_id"), 0) or 0)
        return shaped

    return _read(project_id or "", "runs", _from_db)


def counts(project_id: str | None) -> dict[str, int]:
    """How much of each artefact the project has.

    Cheap enough to call from a template: everything comes out of the same
    per-request cache the page's other reads populate.
    """
    est = latest_estimation(project_id)
    return {
        "test_cases": len(test_cases(project_id)),
        "checklist": len(checklist(project_id)),
        "bugs": len(bugs(project_id)),
        "runs": len(runs(project_id)),
        "estimations": 1 if est else 0,
    }


# ── Writes ────────────────────────────────────────────────────────
#
# Thin wrappers whose only job is to invalidate the cache afterwards. They
# exist so a caller never has to remember to, and so that when E3.5 adds
# optimistic locking there is one place per artefact to add it.

def pack_versions(project_id: str | None) -> dict[str, int]:
    """Each pack's current version, for a caller about to write one back.

    Read it *before* mutating and hand it to the matching ``save_*``.

    Deliberately not cached per request: the point is to notice a change
    made after this request started, and a cached value would report the
    version the page was rendered with — precisely the stale answer the
    guard exists to catch.
    """
    from engine import db as _db
    if not project_id:
        return {"test_cases": 0, "checklist": 0}
    return _db.pack_versions(project_id)


def save_test_cases(project_id: str, rows: list[dict], *,
                    expected_version: int | None = None,
                    protect_edits: bool = False) -> int:
    """Write the pack. Raises ``engine.db.WriteConflict`` on a stale write.

    The cache is invalidated on success only. Dropping it after a conflict
    would discard a reader's copy of a pack that did not change, making the
    next read look as though the refused write had partly landed.
    """
    from engine import db as _db
    written = _db.save_test_cases(project_id, rows,
                                  expected_version=expected_version,
                                  protect_edits=protect_edits)
    invalidate(project_id, "test_cases")
    return written


def save_checklist(project_id: str, rows: list[dict], *,
                   expected_version: int | None = None,
                   protect_edits: bool = False) -> int:
    """As :func:`save_test_cases`, for the checklist."""
    from engine import db as _db
    written = _db.save_checklist(project_id, rows,
                                 expected_version=expected_version,
                                 protect_edits=protect_edits)
    invalidate(project_id, "checklist")
    return written


def save_bug(project_id: str | None, bug: dict, source: str = "manual") -> int:
    from engine import db as _db
    bug_id = _db.save_bug(project_id, bug, source=source)
    invalidate(project_id, None)   # bug counts feed several cached reads
    return bug_id


def save_estimation(project_id: str, input_payload: dict,
                    result_payload: dict) -> int:
    from engine import db as _db
    row_id = _db.save_estimation(project_id, input_payload, result_payload)
    invalidate(project_id, "estimation")
    return row_id


__all__ = [
    "KINDS", "SESSION_ONLY_KEYS", "db_first",
    "test_cases", "checklist", "bugs", "latest_estimation", "runs", "counts",
    "bug_row_to_dict", "run_row_to_dict", "case_result_to_dict",
    "save_test_cases", "save_checklist", "save_bug", "save_estimation",
    "pack_versions",
    "invalidate",
]
