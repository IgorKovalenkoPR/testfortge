"""TestFortge — the Web Recorder: its extension, its endpoints, its review.

  * GET  /recorder/extension.zip            — the Chrome MV3 build
  * POST /api/recorder-session/start        — mint a one-shot capture token
  * POST /api/recorder-session/finish       — accept the captured steps
  * POST /api/browser/poll                  — extension pulls its next command
  * POST /api/browser/result                — extension posts the outcome
  * GET  /test-cases/review-session/<token> — confirm a captured session
  * POST /test-cases/review-session/<token> — save it as a suite
  * POST /test-cases/review-session/<token>/discard

Extracted from ``routes/generation.py``, which had grown to 2 708 lines
carrying two unrelated jobs: turning requirements into test cases, and
recording a browser. This is the second one, and it is a genuinely
separate surface rather than a slice taken for size:

* **Different caller.** A Chrome extension's service worker, running in
  the *system under test's* origin, not a page this product rendered.
  Hence the CORS helpers, the preflight handling, and JSON-only replies.
* **Different authentication.** A one-shot recorder token and the
  browser-control token, not a session cookie — which is why four of
  these endpoints are ``csrf.exempt`` and listed in
  ``engine.route_policy.OPEN``, and why the exemption travels with them
  (see the note above ``register``).
* **Different lifecycle.** Off by default behind two flags, and the
  capture → segment → confirm pipeline has nothing to say to the
  generation flow.

Measured before the cut: of everything ``routes/generation.py`` defines,
the eight routes below borrowed exactly one helper — ``mirror_pack``,
imported here from ``._shared`` directly. The traffic ran one way.

``recorder_enabled`` goes the other way: ``generation.py`` still asks it,
because ``/test-cases/<id>/automation-step-kind`` is part of the
generation flow and gated on the same pilot flag. Public here rather than
underscored there, since it now has callers in two modules.

**The same gate is also spelled out in ``mcp_server/server.py``** — the
identical four lines reading ``RECORDER_ENABLED``. Left alone on purpose:
that runs in its own process and importing a Flask routes module into it
would be a worse coupling than the duplication. Named here so the second
copy is a known one rather than a discovered one.
"""

from __future__ import annotations

import os

from flask import (Flask, flash, g, jsonify, redirect, render_template,
                   request, url_for)

from engine import db as _db
from engine import permissions as _perm
from engine import workspace as _workspace
from engine.log import get_logger

from ._shared import mirror_pack as _mirror, resolve_active_project

_log = get_logger(__name__)


def recorder_enabled() -> bool:
    """Match the same env-var gate the recorder CLI + MCP tool use."""
    return os.environ.get("RECORDER_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on")


def browser_control_enabled() -> bool:
    """PR-F Phase 2 — separate gate for the active-driver channel. Off by
    default; the poll/result endpoints 403 until an operator opts in with
    BROWSER_CONTROL_ENABLED=1, so enabling the recorder doesn't silently
    expose a remote-drive surface."""
    return os.environ.get("BROWSER_CONTROL_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on")
# ── PR-E browser-extension helpers ─────────────────────────────
#
# In-memory ``token → {project_id, created_at}`` mapping for active
# extension recordings. Lives only in this worker process; a restart
# loses every in-flight session, and the extension surfaces that as a
# "session expired — restart from TestForTge" toast. Persisting to DB
# would require a second migration and bring no real recovery benefit
# (recording was abandoned anyway), so we deliberately keep it RAM-only.
_RECORDER_SESSIONS: dict[str, dict] = {}

import time as _time


def _purge_oldest_recorder_session() -> None:
    """Drop the single oldest entry — bounded LRU. Called only when
    the dict exceeds the soft cap (1000 entries), so a misbehaving
    integration can't accumulate sessions unboundedly."""
    if not _RECORDER_SESSIONS:
        return
    oldest_token = min(
        _RECORDER_SESSIONS,
        key=lambda t: _RECORDER_SESSIONS[t].get("created_at", 0),
    )
    _RECORDER_SESSIONS.pop(oldest_token, None)


# PR-F — server-side sanitiser for the extension's deep-capture blob.
# Never trust the extension's own caps: a buggy or hostile client could
# POST an unbounded telemetry object. We re-cap every count + string
# length here so one recording can't bloat the SessionDraft row or the
# review page, and coerce types defensively against schema drift.
_TELE_NET_CAP = 500
_TELE_CONSOLE_CAP = 500
_TELE_SNAPSHOT_CAP = 25
_TELE_STR_CAP = 2000


def _sanitise_recorder_telemetry(raw) -> dict | None:
    """Coerce + cap the ``telemetry`` field from /finish. Returns a
    normalised dict, or ``None`` when there's nothing worth storing."""
    if not isinstance(raw, dict):
        return None

    def _s(v, cap=_TELE_STR_CAP):
        return str(v if v is not None else "")[:cap]

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    def _clip(seq, cap):
        return seq[-cap:] if isinstance(seq, list) else []

    net = []
    for item in _clip(raw.get("network"), _TELE_NET_CAP):
        if not isinstance(item, dict):
            continue
        ok = item.get("ok")
        net.append({
            "method": _s(item.get("method"), 12),
            "url": _s(item.get("url"), 500),
            "type": _s(item.get("type"), 40),
            "status": _int(item.get("status")),
            "ok": (bool(ok) if ok is not None else None),
            "mime": _s(item.get("mime"), 80),
            "error": _s(item.get("error"), 200),
            "redirects": _int(item.get("redirects")),
        })

    con = []
    for item in _clip(raw.get("console"), _TELE_CONSOLE_CAP):
        if not isinstance(item, dict):
            continue
        con.append({
            "level": _s(item.get("level"), 16),
            "text": _s(item.get("text")),
            "source": _s(item.get("source"), 24),
            "url": _s(item.get("url"), 500),
        })

    snaps = []
    for item in _clip(raw.get("dom_snapshots"), _TELE_SNAPSHOT_CAP):
        if not isinstance(item, dict):
            continue
        inter_raw = item.get("interactive")
        inter = []
        for e in (inter_raw[:80] if isinstance(inter_raw, list) else []):
            if not isinstance(e, dict):
                continue
            inter.append({
                "tag": _s(e.get("tag"), 20),
                "role": _s(e.get("role"), 40),
                "name": _s(e.get("name"), 120),
                "text": _s(e.get("text"), 120),
                "locator": _s(e.get("locator"), 300),
                "label": _s(e.get("label"), 200),
            })
        snaps.append({
            "url": _s(item.get("url"), 500),
            "title": _s(item.get("title"), 200),
            "text_digest": _s(item.get("text_digest")),
            "interactive": inter,
            "element_count": len(inter),
        })

    meta_raw = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    meta = {
        "debugger_ok": bool(meta_raw.get("debugger_ok")),
        "debugger_error": _s(meta_raw.get("debugger_error"), 200),
    }

    # Nothing captured AND no debugger-status to report → don't store a
    # row-bloating empty blob. But if the debugger failed to attach we
    # DO keep the meta so the review page can explain the thin panel.
    if not (net or con or snaps or meta["debugger_error"]):
        return None

    con_errors = sum(1 for c in con if c["level"] in ("error", "assert"))
    net_fails = sum(1 for n in net
                     if n["ok"] is False or (n["status"] and n["status"] >= 400))
    return {
        "network": net,
        "console": con,
        "dom_snapshots": snaps,
        "meta": meta,
        "counts": {
            "network": len(net),
            "console": len(con),
            "console_errors": con_errors,
            "network_failures": net_fails,
            "dom_snapshots": len(snaps),
        },
    }


def _recorder_cors_headers() -> dict:
    """CORS headers for the recorder API endpoints.

    The extension's content-script runs in the SUT's origin (whatever
    site the operator is recording against). Since we can't pre-list
    every SUT, we accept ``*``.

    The sentence that used to end this paragraph — "the endpoints carry
    their own auth (the per-session token from /start) so a public origin
    can still only act on a project it was authorised against" — was true
    of ``/finish`` and false of ``/start``, which is the endpoint that
    *issues* the authorisation and had none of its own. ``/start`` now
    requires a session role, so the claim holds for both.

    The wildcard stays on both. On ``/start`` it grants nothing now: ``*``
    forbids credentials, so a cross-origin fetch cannot carry the cookie the
    role gate needs, and SameSite=Lax would not send it anyway. Removing it
    would mean unpicking the shared preflight for no reachable gain.
    """
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "600",
    }


def _recorder_cors_preflight():
    """Empty 204 response for the CORS preflight OPTIONS request."""
    from flask import make_response
    resp = make_response("", 204)
    for k, v in _recorder_cors_headers().items():
        resp.headers[k] = v
    return resp


def _json_with_cors(body: dict, status: int = 200):
    """``jsonify`` + recorder CORS headers in one call."""
    resp = jsonify(body)
    resp.status_code = status
    for k, v in _recorder_cors_headers().items():
        resp.headers[k] = v
    return resp


# ── PR-D session-review helpers ────────────────────────────────


def _parse_review_form(form, proposed_count: int) -> list[dict]:
    """Translate the review template's <form> fields into the same
    shape the JSON body uses, so the POST handler stays one code-path.

    Per row the form ships ``save_<i>=on``, ``suite_<i>=Smoke|...``,
    and ``summary_<i>=...``. Unchecked rows just omit ``save_<i>``.
    """
    out: list[dict] = []
    for i in range(proposed_count):
        if not form.get(f"save_{i}"):
            continue
        out.append({
            "idx":              i,
            "suite":            form.get(f"suite_{i}") or "",
            "summary_override": form.get(f"summary_{i}") or "",
        })
    return out


def _next_section_num(project_id: str) -> int:
    """Find the next free integer section_num for a new TC. Recorded
    flows land in a synthetic ``Section N: Recorded session`` group
    so the operator sees them clustered in /test-cases."""
    tcs = _db.load_test_cases(project_id)
    used = []
    for t in tcs:
        try:
            used.append(int(t.get("section_num") or 0))
        except (TypeError, ValueError):
            continue
    return (max(used) + 1) if used else 1


def _mint_external_id(project_id: str) -> str:
    """Mint a fresh external_id for a recorded TC. Format
    ``REC_<n>`` where n is one past the highest existing REC_ id —
    keeps recorded TCs visually distinct from generated TCs (TC-001,
    SC1_002, ...) without colliding with them."""
    tcs = _db.load_test_cases(project_id)
    highest = 0
    for t in tcs:
        ext = str(t.get("id") or "")
        if ext.startswith("REC_"):
            try:
                highest = max(highest, int(ext[4:]))
            except (TypeError, ValueError):
                continue
    return f"REC_{highest + 1:03d}"


def _human_steps_preview(steps: list[dict]) -> str:
    """Render the recorded steps as numbered text so the legacy
    ``test_steps`` field reads naturally. The runner prefers the
    JSON column anyway; this is purely for the editor view."""
    lines: list[str] = []
    for i, s in enumerate(steps or [], start=1):
        action = (s.get("action") or "").lower()
        target = (s.get("target") or "")[:80]
        value = (s.get("value") or "")[:40]
        kind = (s.get("kind") or "").lower()
        atype = (s.get("assertion_type") or "").lower()
        if kind == "assertion":
            verb = {
                "visible": "Assert visible",
                "text":    "Assert text",
                "url":     "Assert URL",
            }.get(atype, "Assert")
            tail = (
                f" {target!r}" if target else "" if atype != "text"
                else (f" {value!r}" if value else "")
            )
            lines.append(f"{i}. {verb}{tail}")
            continue
        verb_map = {
            "goto":   "Navigate to",
            "click":  "Click",
            "fill":   "Fill",
            "select": "Select",
            "check":  "Check",
            "uncheck": "Uncheck",
            "press":  "Press key",
        }
        verb = verb_map.get(action, action.title() or "Step")
        bits = [verb]
        if target:
            bits.append(target)
        if value and action in ("fill", "select", "press"):
            bits.append(f"= {value!r}")
        lines.append(f"{i}. " + " ".join(bits))
    return "\n".join(lines)


# Stage 2 — site-aware path. Used when the input contains a URL: we
# run crawl → recon → strategy → generate_from_strategy and persist
# both Test Cases and Checklist for the active project. The caller
# decides which surface to render — both buckets are saved either way
# so the sibling page picks up the work without a second click.

#: Cached extension archive, keyed on the folder's newest mtime. The
#: folder is ~114 KB and changes only on deploy, so rebuilding it per
#: download is pure waste; keying on mtime rather than caching forever
#: means a redeploy is picked up without a restart.
_EXTENSION_CACHE: dict[str, tuple[bytes, str]] = {}

#: Never shipped to a tester: editor droppings and OS metadata that would
#: make Chrome's "Load unpacked" complain about unexpected files.
_EXTENSION_SKIP = ("__pycache__", ".DS_Store", "Thumbs.db", ".gitkeep")


def _extension_archive(root: str) -> tuple[bytes, str]:
    """Zip the extension folder. Returns ``(bytes, mtime_stamp)``.

    Paths inside the archive are rooted at ``testfortge-recorder/`` so
    unzipping produces one folder to point Chrome at, rather than
    scattering manifest.json and friends into the tester's Downloads.
    """
    import io as _io
    import zipfile

    files: list[tuple[str, str]] = []
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXTENSION_SKIP
                       and not d.startswith(".")]
        for name in sorted(filenames):
            if name in _EXTENSION_SKIP or name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            files.append((full, f"testfortge-recorder/{rel}"))
            try:
                newest = max(newest, os.path.getmtime(full))
            except OSError:      # pragma: no cover — raced with a deploy
                pass

    stamp = f"{newest:.0f}-{len(files)}"
    cached = _EXTENSION_CACHE.get(stamp)
    if cached is not None:
        return cached

    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for full, arcname in files:
            zf.write(full, arcname)
    payload = buf.getvalue()
    # One entry only: the stamp changes on deploy and the old bytes are
    # dead weight in a 512 MB dyno.
    _EXTENSION_CACHE.clear()
    _EXTENSION_CACHE[stamp] = (payload, stamp)
    return payload, stamp


def register(app: Flask) -> None:
    """Attach the recorder surface.

    ``csrf.exempt`` is called from inside this function, on the view
    objects defined in it. Flask-WTF keys an exemption on
    ``f"{fn.__module__}.{fn.__name__}"``, so moving a view to another
    module *would* silently drop its exemption if the exempting call had
    stayed behind — a 400 in production and green tests everywhere else.
    It did not stay behind: the calls moved with the views, so the name
    they record is this module's. Not taken on trust —
    ``tests/test_csrf_on_every_post.py`` derives the check from the URL
    map with ``WTF_CSRF_ENABLED=True`` and would fail on a lost one.
    """
    # ── PR-E: browser-extension recorder endpoints ─────────────────
    #
    # The extension posts here from the SUT's tab (cross-origin). Both
    # routes are JSON-only, CORS-enabled for any origin (the extension's
    # content-script runs in the SUT's origin which we cannot predict),
    # and gated on RECORDER_ENABLED so the surface stays invisible when
    # the pilot flag is off. The /start endpoint mints a one-shot token
    # bound to the active project; /finish accepts the captured step
    # list and reuses the PR-D segmenter → classifier → SessionDraft
    # pipeline.

    @app.route("/recorder/extension.zip", methods=["GET"])
    def recorder_extension_zip():
        """Serve the recorder extension as a zip the tester can unpack.

        Why this route exists at all: the install instructions said
        "select the ``extension/`` folder from your TestForTge checkout",
        and a tester who only has the web app has no checkout. The
        extension is not optional decoration — without it the "Start
        session recording" button mints a token, opens a tab, and nothing
        ever reads it, because the capture happens in the extension's
        content-script. So the one documented way to make that button
        work required being a developer.

        What this does NOT do, and the UI says so: Chrome cannot install
        a zip. The tester still unpacks it and uses Developer mode →
        Load unpacked. A .crx would be installable, but Chrome refuses
        CRX files that did not come from the Web Store, so it would trade
        an honest two-step install for one that silently fails. This
        removes the checkout, not the pilot.

        Gated on RECORDER_ENABLED like every other recorder surface, so a
        host outside the pilot does not serve a download for a feature it
        has switched off.
        """
        if not recorder_enabled():
            return jsonify({"error": "recorder_disabled"}), 403

        root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "extension")
        if not os.path.isdir(root):
            # A deployment that shipped without the folder. 404 rather
            # than an empty archive: a zip with nothing in it would look
            # like a broken extension rather than a missing one.
            _log.warning("extension folder not found at %s", root)
            return jsonify({"error": "extension_not_bundled"}), 404

        payload, stamp = _extension_archive(root)
        from flask import send_file
        import io as _io2
        response = send_file(
            _io2.BytesIO(payload), mimetype="application/zip",
            as_attachment=True, download_name="testfortge-recorder.zip",
            max_age=0)
        # The tester re-downloads after an update, and a cached copy of
        # yesterday's extension is the kind of bug nobody thinks to
        # suspect. ETag off the folder's newest mtime.
        response.set_etag(f"ext-{stamp}")
        return response

    @app.route("/api/recorder-session/start", methods=["POST", "OPTIONS"])
    @_perm.require_role("user")
    def api_recorder_session_start():
        """Mint a fresh recording token for the active project.

        **Session-authenticated, unlike its sibling.** This route sat in
        ``route_policy.OPEN`` with the reason "extension token auth", and it
        is the one route on the recorder surface with no token to check —
        it is the route that *mints* the token. The extension does not call
        it either: ``extension/popup.js`` says so out loud and routes Start
        through the page precisely because the project comes from the
        session cookie a cross-site fetch could not carry. So the only
        caller is a signed-in, same-origin page, and until this decorator
        the consequence was measured, not supposed: an anonymous caller who
        knew a project id got a token for it, posted steps to ``/finish``,
        and saved a test case into that organisation's project through the
        review flow. Four requests, no credentials. See
        ``tests/test_recorder_token_scope.py``.

        Called from the /test-cases trigger button. The token returned
        is appended to the SUT URL as ``#testfortge-recorder-token=<t>``
        so the extension's content-script can pick it up on the next
        page load without needing the operator to copy-paste anything.

        Body (JSON, optional):
          ``{"project_id": "<pid>"}`` — overrides the session's active
          project. Falls back to ``session['project_id']`` when omitted
          (the usual path from the TestForTge UI button). The docstring
          said ``active_project_id`` until E3.3; that has never been a
          session key, only a template variable derived from this one.

        Returns ``{token, project_id, finish_url, review_url_template}``
        on success. 403 when RECORDER_ENABLED is off, 400 when neither
        the body nor session carries a project_id.
        """
        if request.method == "OPTIONS":
            return _recorder_cors_preflight()
        if not recorder_enabled():
            return _json_with_cors({"error": "recorder_disabled"}, 403)
        payload = request.get_json(silent=True) or {}
        # resolve_active_project(), not session["project_id"] — the same
        # correction line 1595 already carries, in a route written
        # without it. The session key is empty on any request whose
        # session did not set it: a fresh sign-in, or the free plan
        # wiping the filesystem session store on restart. The project
        # itself is in Postgres and the picker renders it correctly, so
        # the page showed an active project while this endpoint answered
        # "no_active_project" — found by walking the recorder end to end
        # on staging, which nothing had done before.
        pid = (payload.get("project_id") or "").strip()
        if pid:
            # A caller-named project is a caller-named *write capability*,
            # so it is checked rather than taken. Nothing sends it today —
            # the page posts an empty body — but it is documented above, and
            # a documented parameter that skips the gate every other
            # project-scoped write honours is the gap itself.
            from ._shared import belongs_to_another_org
            if belongs_to_another_org(pid) or _db.get_project(pid) is None:
                return _json_with_cors({"error": "unknown_project"}, 404)
        else:
            pid = resolve_active_project()
        if not pid:
            return _json_with_cors({"error": "no_active_project"}, 400)
        # Token is the same shape as PR-D's draft tokens — secrets-grade
        # URL-safe base64 so the value is opaque and can't be guessed.
        import secrets as _secrets
        token = _secrets.token_urlsafe(32)
        # In-memory mapping token → (pid, created_at). The extension's
        # /finish call resolves the project off this. We deliberately
        # do NOT persist to DB — these tokens are short-lived (max
        # session lifetime), don't survive a worker restart, and a
        # restart-orphaned recording just expires gracefully (extension
        # gets 404 on finish and the operator re-starts). Persisting
        # would require a second migration and add no real safety.
        _RECORDER_SESSIONS[token] = {
            "project_id": pid,
            "created_at": _time.time(),
        }
        # Cap the mapping at 1000 entries — far above any realistic
        # concurrent-session count, but keeps a runaway integration
        # from filling worker memory indefinitely.
        if len(_RECORDER_SESSIONS) > 1000:
            _purge_oldest_recorder_session()
        base = request.host_url.rstrip("/")
        return _json_with_cors({
            "token": token,
            "project_id": pid,
            "finish_url": f"{base}/api/recorder-session/finish",
            "review_url_template": f"{base}/test-cases/review-session/{{token}}",
        })

    @app.route("/api/recorder-session/finish", methods=["POST", "OPTIONS"])
    def api_recorder_session_finish():
        """Accept captured steps from the extension, stage as a draft.

        Body (JSON, required):
          ``{"token": "<from /start>", "steps": [<AutomationStep dict>, ...]}``

        Pipeline mirrors PR-D's CLI ``_finish_review_mode``:

          1. Resolve token → project_id (404 if unknown / expired).
          2. Decode steps via ``AutomationStep(**dict)`` defensively.
          3. ``session_segmenter.segment()`` → list[ProposedTC].
          4. ``db.create_session_draft()`` writes the draft row.
          5. Return ``{review_url}`` for the extension to open.

        We consume the token (delete from the in-memory map) regardless
        of segmenter outcome so a stuck recording can't be replayed by
        a hostile or buggy client.
        """
        if request.method == "OPTIONS":
            return _recorder_cors_preflight()
        if not recorder_enabled():
            return _json_with_cors({"error": "recorder_disabled"}, 403)
        payload = request.get_json(silent=True) or {}
        token = (payload.get("token") or "").strip()
        steps_raw = payload.get("steps") or []
        if not token or not isinstance(steps_raw, list):
            return _json_with_cors({"error": "bad_request"}, 400)
        meta = _RECORDER_SESSIONS.pop(token, None)
        if not meta:
            return _json_with_cors({"error": "unknown_token"}, 404)
        pid = meta["project_id"]

        # Decode steps. Each item is a dict with the AutomationStep
        # field shape — be defensive against the extension sending
        # partial / extra keys. _decode_recorded_steps already handles
        # this exact pattern for PR-D's review-flow.
        from engine.automation_qa import _decode_recorded_steps
        import json as _json
        steps = _decode_recorded_steps(_json.dumps(steps_raw))
        if not steps:
            return _json_with_cors({
                "error": "no_valid_steps",
                "received": len(steps_raw),
            }, 400)

        # Reuse PR-D pipeline verbatim.
        from engine.session_segmenter import segment
        proposed = [p.to_dict() for p in segment(steps)]
        if not proposed:
            return _json_with_cors({
                "error": "segmenter_returned_empty",
            }, 500)

        # PR-F — decode the optional deep-capture blob. Sanitised +
        # capped server-side so a busy or hostile client can't bloat the
        # row. ``None`` when the extension sent nothing (older extension,
        # or debugger never attached and had no error to report).
        telemetry = _sanitise_recorder_telemetry(payload.get("telemetry"))

        # Use the same draft-token shape PR-D uses so the review URL
        # is indistinguishable from a CLI-staged session. Generated
        # fresh per finish — the recorder token from /start is never
        # written to DB.
        import secrets as _secrets
        draft_token = _secrets.token_urlsafe(32)
        row_id = _db.create_session_draft(pid, draft_token, proposed,
                                           telemetry=telemetry)
        if row_id is None:
            return _json_with_cors({"error": "draft_persist_failed"}, 500)

        base = request.host_url.rstrip("/")
        counts = (telemetry or {}).get("counts", {})
        return _json_with_cors({
            "ok": True,
            "review_url": f"{base}/test-cases/review-session/{draft_token}",
            "proposed_tc_count": len(proposed),
            "telemetry_counts": counts,
        })

    # Both /api/recorder-session/{start,finish} carry their own auth
    # (the per-session token from /start; the active project_id binding
    # in session). The global CSRFProtect gate can't apply because:
    #   * /finish is called from the extension's service worker from
    #     the SUT's origin — there's no TestForTge session cookie or
    #     csrf_token in that context.
    #   * /start is called from the modal on /test-cases via fetch(),
    #     but for consistency with /finish we exempt both rather than
    #     thread a CSRF header through the modal-JS handler.
    # Same pattern routes/debug.py uses for /debug/walkthrough.
    _ext = app.extensions.get("csrf") if hasattr(app, "extensions") else None
    if _ext is not None:
        for _fn in (api_recorder_session_start,
                     api_recorder_session_finish):
            try:
                _ext.exempt(_fn)
            except Exception as exc:  # pragma: no cover — defensive
                _log.debug("recorder-session csrf.exempt skipped: %s", exc)

    # ── PR-F Phase 2: browser-control channel (extension ↔ Flask) ──
    #
    # Only two endpoints live here — the ones the EXTENSION calls:
    #   * /poll   — extension pulls its next queued command
    #   * /result — extension posts a command's result
    # The controller side (mint a session, enqueue a command, read the
    # result) is the MCP server talking to the shared DB directly, so it
    # needs no HTTP endpoint here. Both are CSRF-exempt + CORS-open for
    # the same reason the recorder endpoints are: the extension calls
    # them cross-origin from whatever SUT the operator is driving, with
    # no TestForTge cookie. The control token in the body is the auth.

    @app.route("/api/browser/poll", methods=["POST", "OPTIONS"])
    def api_browser_poll():
        """Extension pulls its next command. Body: ``{"token": "<ctl>"}``.
        Returns ``{"command": {command_id, verb, params} | null}``. Also
        bumps the session's liveness so the controller can see the browser
        is attached. Short-poll (returns immediately) to avoid holding a
        sync worker — the extension re-polls on a short interval."""
        if request.method == "OPTIONS":
            return _recorder_cors_preflight()
        if not browser_control_enabled():
            return _json_with_cors({"error": "control_disabled"}, 403)
        payload = request.get_json(silent=True) or {}
        token = (payload.get("token") or "").strip()
        if not token:
            return _json_with_cors({"error": "bad_request"}, 400)
        # Liveness first — a poll for a stale/sealed token returns 404 so
        # the extension knows to tear its loop down.
        if not _db.touch_browser_control_session(token):
            return _json_with_cors({"error": "unknown_or_stopped"}, 404)
        cmd = _db.dequeue_browser_command(token)
        return _json_with_cors({"command": cmd})

    @app.route("/api/browser/result", methods=["POST", "OPTIONS"])
    def api_browser_result():
        """Extension reports a command's outcome. Body:
        ``{"command_id", "ok": bool, "result": {...}, "error": "..."}``."""
        if request.method == "OPTIONS":
            return _recorder_cors_preflight()
        if not browser_control_enabled():
            return _json_with_cors({"error": "control_disabled"}, 403)
        payload = request.get_json(silent=True) or {}
        command_id = (payload.get("command_id") or "").strip()
        if not command_id:
            return _json_with_cors({"error": "bad_request"}, 400)
        ok = bool(payload.get("ok"))
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        error = str(payload.get("error") or "")
        wrote = _db.complete_browser_command(command_id, ok, result, error)
        if not wrote:
            return _json_with_cors({"error": "unknown_or_terminal"}, 404)
        return _json_with_cors({"ok": True})

    if _ext is not None:
        for _fn in (api_browser_poll, api_browser_result):
            try:
                _ext.exempt(_fn)
            except Exception as exc:  # pragma: no cover — defensive
                _log.debug("browser-control csrf.exempt skipped: %s", exc)

    # ── PR-D: session-review route (CLI staging → operator confirm) ─

    @app.route("/test-cases/review-session/<token>", methods=["GET"])
    def test_cases_review_session(token: str):
        """GET — render the review screen.

        Looks the draft up by token, validates the active session's
        project_id matches the draft's, and shows N proposed-TC cards
        with summary, step preview, suggested-suite dropdown, and a
        per-card Save / Skip checkbox.

        Token failures (missing / expired / consumed) → 404 with a
        friendly message rather than a generic error so the operator
        understands they're past the 24-h window or already saved.
        """
        if not recorder_enabled():
            return render_template(
                "review_session.html",
                draft=None,
                error_message=g.t.get(
                    "review_session_pilot_off",
                    "Recorder pilot is not enabled on this host "
                    "(RECORDER_ENABLED=0).",
                ),
            ), 403
        draft = _db.get_session_draft(token)
        if draft is None:
            return render_template(
                "review_session.html",
                draft=None,
                error_message=g.t.get(
                    "review_session_not_found",
                    "This review link is expired, already used, or "
                    "never existed. Recordings stage for 24 hours; "
                    "re-run the CLI to capture again.",
                ),
            ), 404
        # Project guard — the link is unguessable but we still scope
        # the rendering to the currently-active project. An operator
        # juggling multiple projects shouldn't accidentally land TCs
        # into the wrong one because they followed a stale link.
        active_pid = resolve_active_project()
        if active_pid and active_pid != draft["project_id"]:
            return render_template(
                "review_session.html",
                draft=None,
                error_message=g.t.get(
                    "review_session_wrong_project",
                    "This review link belongs to a different project. "
                    "Switch projects in the picker and reopen the link.",
                ),
            ), 403
        return render_template("review_session.html",
                                draft=draft,
                                token=token,
                                error_message=None)

    @app.route("/test-cases/review-session/<token>/discard",
               methods=["POST"])
    def test_cases_review_session_discard(token: str):
        """Throw a recording away, for real.

        The control offering this was an ``<a href="/test-cases">`` — it
        navigated away and discarded nothing, while its label said
        "Cancel — discard recording". The draft stayed in the pending
        banner, still openable and still savable by anyone holding the
        link, for the whole 24 h TTL. An operator who pressed it and saw
        the recording still listed had no way to read that except as the
        product not working.

        Sealing rather than deleting: ``consume_session_draft`` is the
        same call the save path makes, so a discarded recording behaves
        exactly like a saved one — gone from the banner, refusing a
        replay of the review URL — and the row remains for the sweeper
        and for anyone asking what happened to a capture.
        """
        if not recorder_enabled():
            return jsonify({"error": "recorder_disabled"}), 403

        draft = _db.get_session_draft(token)
        if draft is None:
            # Already discarded, already saved, or expired. Not an error
            # worth a page: the operator wanted it gone and it is gone.
            flash("That recording is no longer pending.", "info")
            return redirect(url_for("test_cases_page"))

        # Same scope check as the save path: a draft belongs to the
        # project it was captured in, and a stale link must not reach
        # into whichever project happens to be active now.
        active_pid = resolve_active_project()
        if active_pid and active_pid != draft["project_id"]:
            return jsonify({"error": "wrong_project"}), 403

        _db.consume_session_draft(token)
        from engine import permissions as _perm
        _db.append_audit(entity="session_draft", action="discard",
                         org_id=_perm.current_org_id(),
                         user_id=_perm.current_user_id(),
                         diff={"project_id": draft["project_id"]})
        flash("Recording discarded. Nothing was added to the pack.",
              "success")
        return redirect(url_for("test_cases_page"))

    @app.route("/test-cases/review-session/<token>", methods=["POST"])
    def test_cases_review_session_save(token: str):
        """POST — consume the draft and create the selected ProposedTCs
        as real TestCase rows.

        Body shape (form-encoded or JSON):
          ``{"selected": [{"idx": 0, "suite": "Smoke",
                            "summary_override": "..."}, ...]}``

        Out-of-range / unknown-suite entries are rejected with 400 so
        a tampered POST cannot smuggle weird values into the DB.
        ``consume_session_draft`` then seals the row so a refresh of
        the GET cannot double-insert.
        """
        if not recorder_enabled():
            return jsonify({"error": "recorder_disabled"}), 403

        draft = _db.get_session_draft(token)
        if draft is None:
            return jsonify({"error": "draft_not_found"}), 404

        # A *mismatch* guard, not an ownership guard, and the difference is
        # load-bearing: ``active_pid`` is empty for a caller who has no
        # project, so the condition never fires for one — which is every
        # anonymous caller, and this route is deliberately open ("the token
        # IS the credential", for a browser that may never sign in).
        # Ownership therefore lives upstream, where the recording token is
        # minted: /api/recorder-session/start is role-gated and checks a
        # caller-named project, so a draft can only name a project its
        # creator was entitled to. Do not read this line as the boundary.
        active_pid = resolve_active_project()
        if active_pid and active_pid != draft["project_id"]:
            return jsonify({"error": "wrong_project"}), 403
        pid = draft["project_id"]

        # JSON body or form-encoded — accept both so the template can
        # POST via plain <form> if JS is disabled.
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            selected = payload.get("selected") or []
        else:
            selected = _parse_review_form(request.form,
                                           len(draft["proposed_tcs"]))

        if not isinstance(selected, list) or not selected:
            return jsonify({"error": "no_selection"}), 400

        from engine.suite_classifier import VALID_SUITES

        # Build the TC rows in order. Wrong-index / wrong-suite → 400.
        created_ids: list[int] = []
        created_external_ids: list[str] = []
        proposed = draft["proposed_tcs"]
        next_section_num = _next_section_num(pid)
        import json as _json
        for entry in selected:
            try:
                idx = int(entry.get("idx"))
            except (TypeError, ValueError):
                return jsonify({"error": "invalid_idx"}), 400
            if idx < 0 or idx >= len(proposed):
                return jsonify({"error": "idx_out_of_range",
                                "idx": idx}), 400
            suite = str(entry.get("suite") or "").strip()
            if suite and suite not in VALID_SUITES:
                return jsonify({"error": "invalid_suite",
                                "suite": suite}), 400
            pt = proposed[idx]
            summary_override = (entry.get("summary_override") or "").strip()
            summary = summary_override or pt.get("summary", "") or "Recorded flow"
            steps_dicts = pt.get("steps") or []
            new_ext = _mint_external_id(pid)
            tc = {
                "id": new_ext,
                "section": f"Section {next_section_num}: Recorded session",
                "section_num": next_section_num,
                "summary": summary,
                "preconditions": "",
                "test_steps": _human_steps_preview(steps_dicts),
                "test_data": "",
                "expected_result": pt.get("intent", "") or "",
                "issues": "",
                "comment": "Generated from recorded session "
                           f"(draft {token[:8]}…).",
                "user_story_id": "",
                "category": "Positive",
                "priority": "Medium",
                "status": "Unchecked",
                "testing_type": "Functional",
                "url_pattern": "",
                "trigger": "manual",
                "automation_steps_json": _json.dumps(
                    steps_dicts, ensure_ascii=False),
                "suite": suite or pt.get("suggested_suite", ""),
            }
            new_id = _db.create_test_case(pid, tc)
            if new_id is None:
                return jsonify({"error": "create_failed",
                                "idx": idx}), 500
            created_ids.append(new_id)
            created_external_ids.append(new_ext)
            next_section_num += 1

        # Seal the draft so a refresh of the GET doesn't double-insert.
        _db.consume_session_draft(token)

        # Surface the additions on /test-cases immediately, without a
        # manual project switch. The rows were just written above, so this
        # is a cache refresh rather than a save — and a no-op once the
        # database is the source of truth.
        _workspace.invalidate(pid, "test_cases")
        _mirror("test_cases_data", _workspace.test_cases(pid))

        return jsonify({
            "ok": True,
            "created_count": len(created_ids),
            "created_external_ids": created_external_ids,
            "redirect_url": url_for("test_cases_page"),
        })

    # ── Async generation pipeline ────────────────────────────────
    # The sync /test-cases and /checklist POST handlers can block for
    # 30–90 s on a busy LLM, which surfaces in the UI as a frozen page.
    # The pair below splits that into:
    #   POST /test-cases/run-async    — submits the job, returns
    #                                   {"job_id": ..., "status": "pending"}
    #   GET  /test-cases/status/<id>  — polled by the modal; reports
    #                                   pending / running / done / failed,
    #                                   and on done writes the result back
    #                                   into session so a redirect to
    #                                   /test-cases renders normally.
    # Same pair exists for /checklist further below.


__all__ = ["register", "recorder_enabled", "browser_control_enabled"]
