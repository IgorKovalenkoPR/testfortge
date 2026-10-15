"""TestForTge — platform search endpoints (ADR 0003 §4.8, task T5).

  * GET  /api/search          — user:  hybrid search, no LLM; ``quick=1`` is
                                       the typeahead (titles and ids only)
  * GET  /api/hints           — user:  contextual hints for a page, no LLM
  * GET  /api/search/status   — admin: capabilities, counts, stale scopes
  * POST /api/search/reindex  — admin: queue a reindex on the JobQueue

All four answer **404** while ``PLATFORM_SEARCH`` is effectively off (a
hidden feature, not a 403 — ADR §4.10).

And, behind ``PLATFORM_SEARCH_AI`` (which requires ``PLATFORM_SEARCH``;
404 while effectively off) — task T6, ADR §4.6:

  * POST /api/ask             — user:  the AI answer, ``application/x-ndjson``
  * POST /api/ask/feedback    — user:  thumbs up/down on an answer

``POST /api/ask`` body: ``{"q", "project_id"?, "scope"?: "project"|"org",
"lang"?: "en"|"ua"|"ru", "history"?: [{"q", "a"}]}`` (``project`` is an
alias of ``project_id``; tenancy exactly as ``/api/search``). One JSON object
per line, in this order::

  {"type":"meta","mode":"hybrid-py","cached":false,"grounded":true,
   "lang":"en","items":["TC-001","BUG-15"],
   "cards":[{"n":1,"ref","kind","title","url","snippet","project","score"}],
   "model":"claude-haiku-...","model_tier":"haiku",
   "answer_key":"<sha256>|null","prompt_version":"ask-v3"}
  {"type":"text","text":"Login fails with a 500 [[BUG-15]] ..."}   (0..n)
  {"type":"error","code":"not_grounded|budget|rate_limited|no_key|upstream",
   "message":"...","reason":"org_day|user_minute|user_day"}  (0..1; reason
                                                     only with rate_limited)
  {"type":"chips","chips":[{"id","label","q",...,"source":"ai"?}]} (always)
  {"type":"done","cached":false,"usage":{"input_tokens":N,"output_tokens":N},
   "citations":["BUG-15"],"answer_key":"<sha256>|null"}       (always last)

``text`` deltas are already cleaned: a ``[[ref]]`` that was not retrieved is
removed, and the model's ``SUGGESTIONS:`` line never appears — it arrives as
``source: "ai"`` chips. A cache hit replays the same events with
``cached: true`` and zero usage.

Tenancy (ADR §4.2) is decided here and enforced in
:mod:`engine.search.retrieve`: the project ids a caller may search are
``visible_projects()`` (org membership with ``ORG_MODE`` on, the browser's
own projects otherwise). Naming a project narrows that, and a project the
caller may not open is a **403** — the same verdict
``project_access_with_meta`` gives every project route. There is no
parameter that widens the list.

CSRF: the one POST is a session-authenticated JSON write, so it keeps
Flask-WTF's protection and the browser sends ``X-CSRFToken`` from the page's
meta tag — the convention the editors, the chat and the model switcher
follow. It is not exempt.
"""
from __future__ import annotations

from typing import Any

from flask import Flask, abort, jsonify, request, session

from engine import permissions as _perm
from engine.log import get_logger

log = get_logger(__name__)

#: ``kinds=`` accepts these (anything else is ignored, not an error).
KNOWN_KINDS = ("test_case", "checklist_item", "bug", "run", "case_result",
               "estimation", "guide", "knowledge", "istqb")
MAX_QUERY_CHARS = 300


def _flag_on() -> bool:
    from engine import features
    return features.effective("PLATFORM_SEARCH")


def _require_flag() -> None:
    if not _flag_on():
        abort(404)


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _visible() -> tuple[list[str], dict[str, str]]:
    """``(ids, {id: name})`` of the projects the caller may see."""
    from routes._shared import visible_projects
    rows = visible_projects() or []
    ids = [str(p["id"]) for p in rows if p.get("id")]
    names = {str(p["id"]): str(p.get("name") or p.get("project_name") or "")
             for p in rows if p.get("id")}
    return ids, names


def _project_gate(project_id: str):
    """``(None, meta)`` when the caller may open *project_id*, else
    ``(response, None)``."""
    from routes._shared import project_access_with_meta
    verdict, meta = project_access_with_meta(project_id)
    if verdict == "ok" and meta:
        return None, meta
    if verdict == "malformed":
        return (jsonify({"error": "bad_project_id"}), 400), None
    # Missing and forbidden answer alike: a foreign id must not be
    # distinguishable from a non-existent one by its status code.
    return (jsonify({"error": "forbidden",
                     "message": "You do not have access to this project."}),
            403), None


def _kinds(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    out = [k.strip() for k in raw.split(",") if k.strip() in KNOWN_KINDS]
    return sorted(set(out)) or None


def _int(raw: str | None, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(raw))) if raw not in (None, "") \
            else default
    except (TypeError, ValueError):
        return default


def _scope_for_request(args=None) -> tuple[Any, list[str], dict[str, str],
                                           str, str | None]:
    """Resolve ``(error_response, allowed, names, scope, project_filter)``.

    * ``project_id`` (alias ``project``) → that project only, after the
      access gate;
    * with ``scope=org`` as well → every visible project, the named one as
      a *filter* (relaxed, and said so, when it alone empties the result);
    * ``scope=project`` without an id → the caller's active project;
    * otherwise every visible project.
    """
    args = request.args if args is None else args
    ids, names = _visible()
    pid = args.get("project_id") or args.get("project") or ""
    pid = pid.strip() if isinstance(pid, str) else ""
    scope = args.get("scope") or ""
    scope = scope.strip().lower() if isinstance(scope, str) else ""
    if not pid and scope == "project":
        from routes._shared import resolve_active_project
        try:
            pid = resolve_active_project(pin=False) or ""
        except Exception:   # noqa: BLE001
            pid = ""
    if pid:
        err, meta = _project_gate(pid)
        if err is not None:
            return err, [], {}, "project", None
        names.setdefault(pid, str(meta.get("name") or ""))
        if scope == "org":
            allowed = sorted(set(ids) | {pid})
            return None, allowed, names, "org", pid
        return None, [pid], names, "project", None
    return None, ids, names, "org", None


def _ai_flag_on() -> bool:
    from engine import features
    return features.effective("PLATFORM_SEARCH_AI")


def _ai_available() -> bool:
    """``PLATFORM_SEARCH_AI`` effectively on and a key (the org's own, else
    the platform's) to answer with."""
    if not _ai_flag_on():
        return False
    try:
        from engine import llm_keys
        key, _source = llm_keys.resolve_key(_perm.current_org_id())
    except Exception:   # noqa: BLE001
        return False
    return bool((key or "").strip())


def _stale(allowed: list[str]) -> list[str]:
    from engine.search import hooks
    stale = []
    from engine.search.retrieve import MAX_LAG_CHECKS
    for pid in allowed[:MAX_LAG_CHECKS]:
        if hooks.check_lag(pid):
            stale.append(pid)
    return stale


def register(app: Flask) -> None:
    @app.route("/api/search", methods=["GET"])
    @_perm.require_role("user")
    def api_search():
        """Hybrid search over what the caller may see (ADR §4.5, §4.8)."""
        _require_flag()
        import time
        from engine.search import retrieve

        t0 = time.perf_counter()
        q = (request.args.get("q") or "").strip()[:MAX_QUERY_CHARS]
        err, allowed, names, scope, project_filter = _scope_for_request()
        if err is not None:
            return err
        kinds = _kinds(request.args.get("kinds"))
        quick = _truthy(request.args.get("quick"))
        if not q:
            return jsonify({"query": {"raw": "", "normalised": ""},
                            "mode": None, "results": [], "groups": {},
                            "group_order": [], "chips": [], "stale": False,
                            "quick": quick, "scope": scope,
                            "took_ms": 0.0})
        if quick:
            # Typeahead: never queues a reindex, never explains (ADR §4.8).
            body = retrieve.quick_search(
                q, allowed, kinds=kinds,
                limit=_int(request.args.get("limit"), retrieve.QUICK_LIMIT,
                           1, 20),
                per_group=_int(request.args.get("per_group"),
                               retrieve.QUICK_PER_GROUP, 1, 10),
                project_names=names)
            body["scope"] = scope
            return jsonify(body)

        filters = {f: request.args.get(f).strip()
                   for f in retrieve.FACET_FILTERS
                   if (request.args.get(f) or "").strip()}
        stale = _stale(allowed)
        params = retrieve.SearchParams(
            q=q, allowed=allowed, kinds=kinds, filters=filters,
            project_filter=project_filter,
            linked=(request.args.get("linked") or "").strip()[:60] or None,
            limit=_int(request.args.get("limit"), 20, 1, 100),
            explain=request.args.get("explain", "1") != "0",
            istqb=request.args.get("istqb", "1") != "0",
            scope=scope, project_names=names)
        body = retrieve.search(params)
        body["stale"] = bool(stale)
        body["stale_projects"] = stale
        body["scope"] = scope
        body["quick"] = False
        body["took_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        log.info("search q_len=%d scope=%s mode=%s results=%d took_ms=%.1f",
                 len(q), scope, body.get("mode"), len(body["results"]),
                 body["took_ms"])
        return jsonify(body)

    @app.route("/api/hints", methods=["GET"])
    @_perm.require_role("user")
    def api_hints():
        """Deterministic tips + Guide links for a page; similar items for a
        context document the caller may see."""
        _require_flag()
        from engine.search import hints

        page = (request.args.get("page") or "").strip()[:80]
        lang = (request.args.get("lang") or session.get("lang") or "en")
        context = (request.args.get("context") or "").strip()[:100] or None
        pid = (request.args.get("project_id") or "").strip() or None
        allowed = None
        if pid:
            err, _meta = _project_gate(pid)
            if err is not None:
                return err
            allowed = [pid]
        return jsonify(hints.hints_for(page, lang=lang, project_id=pid,
                                       context=context, allowed=allowed))

    @app.route("/api/search/status", methods=["GET"])
    @_perm.require_role("admin")
    def api_search_status():
        """Index state for the caller's projects (ADR §4.8). Re-detects the
        capabilities, so an extension enabled after boot shows up."""
        _require_flag()
        from sqlalchemy import func, select

        from engine import db as _db
        from engine.db import SearchDoc, SearchScopeState
        from engine.search import caps, detect_caps, hooks, provider_name
        from engine.search.embed import NotConfigured, get_embedder
        from engine.search.retrieve import _scope_clause

        detect_caps(_db.get_engine())
        ids, _names = _visible()
        try:
            vec_version = get_embedder().version
        except NotConfigured:
            vec_version = None
        with _db.session_scope() as sess:
            docs = int(sess.execute(select(func.count(SearchDoc.id)).where(
                _scope_clause(ids))).scalar() or 0)
            vectors = int(sess.execute(select(func.count(SearchDoc.id)).where(
                _scope_clause(ids)).where(
                    SearchDoc.vec_blob.is_not(None),
                    SearchDoc.vec_version == vec_version)).scalar() or 0)
            states = sess.query(SearchScopeState).filter(
                SearchScopeState.scope_key.in_(ids + ["global"])).all()
            last = max((s.indexed_at for s in states if s.indexed_at),
                       default=None)
            errors = {s.scope_key: s.last_error for s in states
                      if s.last_error}
        from engine.search.indexer import is_stale
        stale = [pid for pid in ids if is_stale(pid)]
        pending = sorted(set(hooks.pending()) & set(ids))
        return jsonify({
            "fts": bool(caps.get("fts")), "pgvector": bool(caps.get("pgvector")),
            "pgvector_version": caps.get("pgvector_version"),
            "backend": caps.get("backend"),
            "provider": provider_name(), "vec_version": vec_version,
            "docs": docs, "vectors": vectors,
            "projects": len(ids), "pending_projects": len(pending),
            "last_indexed_at": last.isoformat() if last else None,
            "stale_projects": stale, "errors": errors,
            "ai_available": _ai_available(),
        })

    @app.route("/api/search/reindex", methods=["POST"])
    @_perm.require_role("admin")
    def api_search_reindex():
        """Queue a reindex of one project, every visible project, or the
        global corpora (``{"project_id": null|"…", "rebuild": false,
        "global": false}``). Returns ``{"job_id"}`` (202)."""
        _require_flag()
        from engine.job_queue import get_queue

        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "bad_request"}), 400
        pid = (body.get("project_id") or "").strip() if isinstance(
            body.get("project_id"), str) else ""
        rebuild = bool(body.get("rebuild"))
        with_global = bool(body.get("global"))
        if pid:
            err, _meta = _project_gate(pid)
            if err is not None:
                return err
            targets = [pid]
        else:
            targets, _names = _visible()

        def _job(project_ids: list[str], rebuild: bool, with_global: bool):
            from engine.search import indexer
            out: dict[str, Any] = {"projects": {}}
            if with_global:
                out["global"] = indexer.index_global(force=True,
                                                     rebuild=rebuild)
            for p in project_ids:
                out["projects"][p] = indexer.reindex_project(p,
                                                             rebuild=rebuild)
            return out

        try:
            job_id = get_queue().submit(
                "search_index", _job, list(targets), rebuild, with_global,
                meta={"projects": len(targets), "global": with_global,
                      "user_id": _perm.current_user_id()})
        except RuntimeError as exc:
            return jsonify({"error": "unavailable", "message": str(exc)}), 503
        return jsonify({"job_id": job_id, "projects": len(targets),
                        "global": with_global}), 202


    @app.route("/api/ask", methods=["POST"])
    @_perm.require_role("user")
    def api_ask():
        """The AI answer over the search results, streamed as NDJSON (see
        the module docstring for the contract)."""
        if not _ai_flag_on():
            abort(404)
        import json

        from flask import Response, stream_with_context

        from engine.search import answer
        from routes._shared import get_session_id

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "bad_request"}), 400
        q = body.get("q")
        q = q.strip()[:MAX_QUERY_CHARS] if isinstance(q, str) else ""
        if not q:
            return jsonify({"error": "empty_query"}), 400
        err, allowed, names, scope, project_filter = _scope_for_request(body)
        if err is not None:
            return err
        lang = body.get("lang") if body.get("lang") in answer.LANG_NAMES \
            else None
        req = answer.AskRequest(
            q=q, allowed=allowed, names=names, scope=scope,
            project_filter=project_filter,
            project_id=(allowed[0] if scope == "project" and allowed
                        else project_filter),
            lang=lang, history=body.get("history") or [],
            org_id=_perm.current_org_id(), user_id=_perm.current_user_id(),
            sid=get_session_id())

        def generate():
            started = False
            try:
                for event in answer.ask_events(req):
                    started = True
                    yield json.dumps(event, ensure_ascii=False) + "\n"
            except GeneratorExit:
                raise
            except Exception as exc:   # noqa: BLE001 — never a broken stream
                log.warning("ask failed: %s: %s", type(exc).__name__, exc)
                tail = [] if started else [
                    {"type": "meta", "mode": None, "cached": False,
                     "grounded": False, "items": [], "cards": [],
                     "answer_key": None}]
                tail += [{"type": "error", "code": "upstream",
                          "message": answer.MESSAGES["upstream"]},
                         {"type": "chips", "chips": []},
                         {"type": "done", "cached": False,
                          "usage": {"input_tokens": 0, "output_tokens": 0},
                          "citations": [], "answer_key": None}]
                for event in tail:
                    yield json.dumps(event, ensure_ascii=False) + "\n"

        return Response(stream_with_context(generate()),
                        mimetype="application/x-ndjson",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    @app.route("/api/ask/feedback", methods=["POST"])
    @_perm.require_role("user")
    def api_ask_feedback():
        """``{"answer_key", "helpful": bool, "comment"?}`` → counts the vote
        on the caller's own cached answer and records it (with the comment,
        cut to 500 characters) in the audit log. A key that belongs to
        another tenant is a 404, like one that does not exist."""
        if not _ai_flag_on():
            abort(404)
        import re as _re

        from engine import db as _db
        from engine.search import answer
        from routes._shared import get_session_id

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "bad_request"}), 400
        key = body.get("answer_key")
        helpful = body.get("helpful")
        if not (isinstance(key, str) and _re.fullmatch(r"[0-9a-f]{64}", key)) \
                or not isinstance(helpful, bool):
            return jsonify({"error": "bad_request"}), 400
        comment = body.get("comment")
        comment = " ".join(comment.split())[:500] \
            if isinstance(comment, str) else ""
        org_id = _perm.current_org_id()
        user_id = _perm.current_user_id()
        scope = answer.tenant_scope(org_id, user_id, get_session_id())
        owner = _db.rag_answer_owner(key)
        if owner is not None and owner != scope:
            return jsonify({"error": "not_found"}), 404
        counted = _db.rate_rag_answer(key, scope=scope, helpful=helpful)
        _db.append_audit(entity="search_answer", action="feedback",
                         user_id=user_id, org_id=org_id, entity_id=key,
                         diff={"helpful": helpful,
                               **({"comment": comment} if comment else {})})
        return jsonify({"ok": True, "counted": counted})


__all__ = ["register", "KNOWN_KINDS"]
