"""
TestFortge — Main Flask Application

Scope: Requirements → Test Cases / Checklists / Estimation / Execution /
Automation / Bug Reports, plus an interactive QA assistant.

This module is the composition root: it configures Flask, wires security
middleware (CSRF, CSP, session isolation) and then delegates every route
to sub-modules under :mod:`routes` via :func:`routes.register_all`.

Runtime configuration — including the SECRET_KEY, upload size cap, cookie
hardening and CSRF wiring — lives in :mod:`config`. Uploads are bounded
by MAX_CONTENT_LENGTH; payloads exceeding it return HTTP 413.
"""

import os
import re
import mimetypes

# Force-register the canonical MIME types for static assets BEFORE Flask
# spins up. On Windows the `mimetypes` module reads from the registry and
# we have seen `.js` mapped to `text/plain` on some boxes, which makes
# Chrome block our app.js with a strict-MIME error and break the chat /
# Back-to-Top widgets. Registering them at import time fixes that
# regardless of the host's registry state.
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")

import markupsafe
import secrets

from flask import Flask, Response, g, jsonify, request, session
from flask_session import Session
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError, generate_csrf
from flask_compress import Compress

import config as _config
from engine import basic_auth
from engine import db as _db
from engine import features
from engine import server_session
from engine import session_timeout
from engine.i18n import get_lang
from engine.log import get_logger
from routes import register_all
from routes._shared import GENERATED_KEYS, SERVER_START_TIME

log = get_logger("testfortge")

app = Flask(__name__)
_config.apply(app)
# Bring up the persistence layer immediately after config. `init_db` is
# idempotent and creates the schema if missing — it's the only DDL hook
# we run at boot.
#
# A DB outage at boot must NOT crash the process. Render's free-tier
# Postgres expires ~every 30 days ("Suspended by Render"); if we re-raise
# here the gunicorn worker dies, /healthz never answers, and Render parks
# the whole site on the "Application loading" interstitial indefinitely.
# Instead we log loudly and boot anyway: /healthz (liveness, DB-free)
# stays green so traffic is routed, non-DB routes work, and DB-backed
# routes retry init_db lazily and degrade to a 500 until the DB returns.
# /readyz reports the real DB status for monitoring.
try:
    _db.init_db()
except Exception:  # pragma: no cover — surface DB outage but stay up
    log.exception(
        "Database initialisation failed at boot — starting in degraded "
        "mode (DB-backed routes will retry lazily; see /readyz).")


def _start_snapshot_catchup_thread() -> None:
    """Daemon thread that fills gaps in ``DashboardMetricSnapshot``.

    Once a day it iterates the project list and writes a snapshot for
    every project that hasn't had one in the last 23 h. Keeps the
    trend chart from going flat-line whenever a project is healthy
    enough that nobody happens to load the dashboard.

    Gated by ``TESTFORTGE_SNAPSHOT_WORKER`` (default ON). Setting it to
    "0" disables the thread — the recommended posture when you've
    scaled gunicorn beyond ``--workers 1`` so the snapshot pass runs
    in a single external cron instead of N parallel times. See
    ``README.md`` for the operations note.

    Defensive: every failure path is swallowed and logged so this
    daemon can never bring the Flask process down. The runner-worker
    and dashboard-load triggers are the primary snapshot sources;
    this thread only catches the long-tail "idle project" case.
    """
    import os as _os
    if _os.environ.get("TESTFORTGE_SNAPSHOT_WORKER", "1") != "1":
        log.info("Daily metric-snapshot thread disabled "
                 "(TESTFORTGE_SNAPSHOT_WORKER != 1).")
        return

    import threading as _threading
    import time as _time

    def _loop() -> None:
        # Sleep first — there's no point snapshotting right at boot
        # when the dashboard-load + runner-worker paths already cover
        # active projects. 24 h between passes is the dial.
        SLEEP_SEC = 24 * 60 * 60
        STALE_SEC = 23 * 60 * 60
        while True:
            try:
                _time.sleep(SLEEP_SEC)
            except Exception:
                return
            try:
                projects = _db.list_projects()
            except Exception as exc:  # pragma: no cover — DB hiccup
                log.warning("snapshot catch-up: list_projects failed: %s", exc)
                continue
            from engine.test_metrics_generator import snapshot_metrics_from_db
            now = _time.time()
            for p in projects or []:
                pid = p.get("id") if isinstance(p, dict) else None
                if not pid:
                    continue
                try:
                    recent = _db.list_metric_snapshots(pid, limit=1)
                except Exception as exc:
                    log.warning("snapshot catch-up: list_snapshots(%s) failed: %s",
                                pid, exc)
                    continue
                # Skip if there's already a fresh snapshot. ``captured_at``
                # is an ISO string in UTC — parse defensively.
                if recent:
                    ts_str = (recent[0].get("captured_at") or "") if isinstance(recent[0], dict) else ""
                    try:
                        from datetime import datetime as _dt
                        ts = _dt.fromisoformat(ts_str)
                        if (now - ts.timestamp()) < STALE_SEC:
                            continue
                    except Exception:
                        # Unparseable timestamp — treat as stale and snapshot.
                        pass
                try:
                    snapshot_metrics_from_db(pid)
                except Exception as exc:
                    log.warning("snapshot catch-up: snapshot(%s) failed: %s",
                                pid, exc)

    t = _threading.Thread(target=_loop, name="snapshot-catchup",
                          daemon=True)
    t.start()
    log.info("Daily metric-snapshot thread started "
             "(TESTFORTGE_SNAPSHOT_WORKER=1).")


_start_snapshot_catchup_thread()
Session(app)
# …then, when SESSION_BACKEND=db, replace what Flask-Session just
# installed with the Postgres-backed interface (E0.2). Order matters:
# Session(app) sets app.session_interface, so this has to run after it.
# The filesystem store it displaces loses every session whenever the dyno
# restarts, which on the free tier is several times a day.
server_session.install(app)

# Idle and absolute session timeouts (E1.5). Registered here, and this
# early, for two ordering reasons — ``before_request`` hooks run in the
# order they were registered:
#
#   * before the cold-start hook further down, which writes
#     ``_session_active_since`` into the session. Once that has run, a
#     session that arrived empty is indistinguishable from one holding a
#     single framework key — and telling those apart is how the sign-in
#     page knows to say "we lost your session" rather than "you were
#     idle". On the free plan the first is the common case.
#   * before the route-policy hook (registered from ``routes/__init__.py``,
#     much later), so an expired session reads as anonymous by the time
#     the policy decides whether the caller may be where they are.
session_timeout.install(app)

# A flag that is set but neutered by a missing prerequisite is the one
# failure mode nobody can see from the outside: the dashboard says ON and
# the app behaves as OFF. Say so once, at boot, in the container log.
for _flag_warning in features.misconfigurations():
    log.warning("feature flags: %s", _flag_warning)

# Gzip/Brotli responses — static assets + JSON payloads benefit most.
Compress(app)
# CSRF protection for every state-changing request. Individual endpoints
# that are genuinely callable cross-origin (none today) can use the
# ``@csrf.exempt`` decorator; the default posture is "protect".
csrf = CSRFProtect(app)
SESSION_DIR = app.config["SESSION_FILE_DIR"]

# HTTP Basic Auth gate. No-op unless TESTFORTGE_BASIC_USER and
# TESTFORTGE_BASIC_PASSWORD are both set in the environment, in which
# case the gate is registered as the very first ``before_request`` hook
# so unauthenticated visitors never reach the session / i18n machinery.
#
# Whether it actually guards anything is decided per request, from
# BASIC_GATE_ENABLED (E1.8) — so retiring the shared password is a
# dashboard edit rather than a deploy. The line below always says which of
# the four states this instance is in, including the one where the gate was
# asked to stand down and refused because nothing would be left behind it.
basic_auth.install(app)
log.info("%s", basic_auth.status())
if basic_auth.standing_down_refused():
    # Louder than info: the operator asked for something and did not get
    # it, and the reason is a second setting they have to change first.
    log.error("%s", basic_auth.status())


# ── /metrics exposure warning (Sprint 4 task 4.5) ─────────────────
#
# When the deployment is fronted by HTTPS (``BEHIND_HTTPS=1``) but
# neither Basic Auth nor the ops-endpoint token is set, ``/metrics``
# is publicly reachable. The endpoint exposes infrastructure-level
# counts only (no user data), but it is still operator-grade telemetry
# we do not want indexed. Emit a single warning at boot so the issue
# surfaces in container logs before the first scrape.
if (os.environ.get("BEHIND_HTTPS") == "1"
        and not os.environ.get("TESTFORTGE_BASIC_USER")
        and not (os.environ.get("OPS_ENDPOINTS_TOKEN") or "").strip()):
    log.warning(
        "SECURITY: BEHIND_HTTPS=1 but no Basic Auth user and no "
        "OPS_ENDPOINTS_TOKEN — /metrics is publicly reachable. "
        "Set TESTFORTGE_BASIC_USER+PASSWORD or OPS_ENDPOINTS_TOKEN, "
        "or restrict /metrics at the reverse proxy."
    )


# ── Error handlers ───────────────────────────────────────────────

def _wants_json() -> bool:
    """True when the caller is a fetch/XHR rather than a browser form post.

    Used by the CSRF handler so a JSON client gets a JSON body it can
    actually branch on. Before this, every CSRF rejection came back as
    ``text/plain``, the page scripts called ``r.json()`` on it
    unconditionally, the resulting SyntaxError landed in their network
    ``.catch()``, and the UI reported "Could not reach the server" — the
    opposite of what happened, with a Retry button that re-posted the
    same dead token forever.
    """
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


@app.errorhandler(CSRFError)
def _handle_csrf_error(e):
    log.warning("CSRF rejected: %s", e.description)
    message = ("Your session expired. Reload the page and try again.")
    if _wants_json():
        # ``reload_required`` tells the client that retrying the same
        # request is pointless — it needs a fresh token first.
        return jsonify({
            "error": "csrf",
            "message": message,
            "reload_required": True,
        }), 400
    return Response(
        message,
        status=400,
        content_type="text/plain; charset=utf-8",
    )


# Jinja global, not a context variable: macros imported without
# ``with context`` cannot see context variables, and the inline-edit macros
# (templates/_inline_edit.html) need this gate. Registered as a callable so
# it is evaluated per render rather than captured at boot.
app.jinja_env.globals["editors_enabled"] = lambda: (
    features.effective("EDITORS_ENABLED"))


def _template_edit_metadata(kind: str = "test_cases") -> dict:
    """``{"TC-004": {"row_version": 3, …}}`` for the page being rendered.

    A Jinja global rather than a value passed to ``render_template``: the
    test-cases page alone renders from seven call sites, and a context key
    missing from one of them is an editor that silently disappears on that
    path. Called from the template, so it costs a query only on pages that
    actually edit — and the repository's per-request cache makes the repeat
    calls inside a loop free.
    """
    try:
        from engine import workspace
        from routes._shared import resolve_active_project
        if not features.effective("EDITORS_ENABLED"):
            return {}
        return workspace.edit_metadata(resolve_active_project(pin=False), kind)
    except Exception as exc:  # pragma: no cover — never break a render
        log.debug("edit metadata unavailable for %s: %s", kind, exc)
        return {}


app.jinja_env.globals["edit_metadata"] = _template_edit_metadata


def _template_bug_status_options(current: str) -> list:
    """The statuses this person may move this bug to, from where it is (E4.5).

    Filtered by role as well as by transition, so the control cannot offer a
    move the server will refuse. Hiding an option is UX, not a permission —
    ``engine.editable``'s field guard checks again on the way in.
    """
    try:
        from engine import bug_workflow, permissions
        allowed = bug_workflow.allowed_from(current)
        return [status for status in allowed
                if bug_workflow.role_required(status) == "user"
                or permissions.has_role(bug_workflow.role_required(status))]
    except Exception as exc:  # pragma: no cover — never break a render
        log.debug("bug status options unavailable: %s", exc)
        from engine.bug_report import BUG_STATUSES
        return list(BUG_STATUSES)


app.jinja_env.globals["bug_status_options"] = _template_bug_status_options


def _template_estimation_edit_state():
    """The editable state of this project's latest estimation (E4.6).

    Falsy when there is nothing to edit — no project, no estimation, or
    editing switched off — so the template can gate the whole panel on it.
    """
    try:
        if not features.effective("EDITORS_ENABLED"):
            return None
        from engine import estimation_edit
        from routes._shared import resolve_active_project
        return estimation_edit.get(resolve_active_project(pin=False))
    except Exception as exc:  # pragma: no cover — never break a render
        log.debug("estimation edit state unavailable: %s", exc)
        return None


def _template_estimation_inputs():
    """The editable drivers, with their labels and guard rails."""
    try:
        from engine import estimation_edit
        return estimation_edit.INPUTS
    except Exception:  # pragma: no cover
        return {}


app.jinja_env.globals["estimation_edit_state"] = (
    _template_estimation_edit_state)
app.jinja_env.globals["estimation_inputs"] = _template_estimation_inputs


def _template_import_targets(kind: str) -> list:
    """Fields a user may map a column onto (E4.8), mapped ones first."""
    try:
        from engine import import_preview
        return import_preview.analyse(kind, []).targets()
    except Exception as exc:  # pragma: no cover — never break a render
        log.debug("import targets unavailable for %s: %s", kind, exc)
        return []


app.jinja_env.globals["import_targets"] = _template_import_targets


@app.route("/api/csrf-token", methods=["GET"])
def api_csrf_token():
    """Mint a CSRF token for the current session.

    Lets a page recover from an expired session without a manual reload:
    the client re-fetches a token and replays the submit once. This is a
    routine occurrence rather than an edge case on the free Render plan —
    the service sleeps after ~15 minutes and ``SESSION_TYPE=filesystem``
    lives on an ephemeral disk, so every cold start invalidates the token
    held by any tab that was already open (see render.yaml's free-tier
    notes).

    GET, so the global CSRFProtect gate does not apply. The token is
    scoped to the caller's own session cookie, so handing it out reveals
    nothing another origin could use — and the Basic-Auth gate, when
    enabled, still fronts this like every other route.
    """
    return jsonify({"token": generate_csrf()})


@app.errorhandler(413)
def _handle_too_large(e):
    limit_mb = int(app.config["MAX_CONTENT_LENGTH"] / (1024 * 1024))
    return Response(
        f"Request body too large (limit: {limit_mb} MB).",
        status=413,
        content_type="text/plain; charset=utf-8",
    )


# ── Template context + CSRF injection ────────────────────────────

# Expose the CSRF token generator to every template so forms can drop in
# ``{{ csrf_token() }}`` without importing anything.
@app.context_processor
def _inject_csrf():
    return {"csrf_token": generate_csrf}


@app.context_processor
def inject_globals():
    # ``recorder_enabled`` gates the PR-B "🎬 Record" surface in
    # templates/test_cases.html. Driven by the same env var the CLI
    # and the MCP tool consult, so a host is either fully in the
    # pilot or fully out of it. Cheap call — string compare per
    # request, no I/O.
    recorder_enabled = os.environ.get("RECORDER_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on")
    # ``pending_drafts`` powers the "Pending recording sessions" banner in
    # templates/test_cases.html — a recording whose review tab got closed
    # before Save would otherwise be invisible until its 24 h TTL lapsed.
    # Only worth a query when the pilot flag is on and a project is
    # active; the lookup is a single indexed read on a tiny table. A
    # failure here must never 500 the page, so swallow and fall back to
    # an empty list (banner just doesn't render).
    pending_drafts: list = []
    if recorder_enabled:
        pid = session.get("project_id")
        if pid:
            try:
                pending_drafts = _db.list_pending_session_drafts(pid)
            except Exception:
                pending_drafts = []
    return {"t": g.t, "lang": g.lang, "recorder_enabled": recorder_enabled,
            "pending_drafts": pending_drafts}


@app.template_filter('fromjson')
def fromjson_filter(s):
    """Decode a JSON string for template iteration.

    Used by the PR-C recorder step editor in templates/test_cases.html
    to walk a TC's ``automation_steps_json`` and render the per-step
    Action/Assert dropdown. Returns ``[]`` on missing / malformed
    payload so a corrupt recording never crashes the page.
    """
    if not s:
        return []
    try:
        import json as _json
        out = _json.loads(s)
    except (ValueError, TypeError):
        return []
    return out if isinstance(out, list) else []


@app.template_filter('nl2br')
def nl2br_filter(s):
    if not s:
        return s
    return markupsafe.Markup(str(markupsafe.escape(s)).replace('\n', '<br>\n'))


@app.template_filter('tc_step_list')
def tc_step_list_filter(blob):
    """A test case's steps as a list, for the steps editor (E4.3).

    The column is one newline-joined blob; the editor needs positions it can
    move and delete. Parsed through :mod:`engine.tc_steps` — the same module
    the server applies the operations with — so "step 3" means the same
    thing on both sides of the request.
    """
    try:
        from engine import tc_steps
        return tc_steps.parse(blob)
    except Exception as exc:  # pragma: no cover — never break a render
        log.debug("tc_step_list failed: %s", exc)
        return []


@app.template_filter('gherkin_view')
def gherkin_view_filter(tc):
    """The Given/When/Then view of a test case, derived on render.

    A filter rather than a stored field on purpose: the manual columns are
    the source of truth, so the BDD view is computed from whatever they say
    right now. Storing it would let the two drift, with the stale copy
    being what the runner executes. See :mod:`engine.gherkin`.
    """
    try:
        from engine import gherkin
        return gherkin.ensure_gherkin(tc)
    except Exception as exc:  # pragma: no cover — never break a render
        log.debug("gherkin_view failed for %r: %s",
                  getattr(tc, "id", "?"), exc)
        return ""


@app.template_filter('safe_display')
def safe_display_filter(s):
    """Strip prompt-injection-style lines from text rendered to other
    operators. Sprint 4 task 4.4 — see :mod:`engine.sanitize`.

    The DB keeps the original verbatim; only the display surface is
    filtered so a bug title pasted by one operator cannot smuggle an
    instruction line into another operator's view.
    """
    from engine.sanitize import strip_display
    return strip_display(s)


@app.template_filter('bug_field')
def bug_field_filter(s):
    """Render a free-text bug-report field, auto-splitting numbered lists.

    Tedgie's chat bug-form (and any pasted text) often arrives like
    ``"1. Foo 2. Bar 3. Baz"`` — a single line with inline enumeration.
    Steps to Reproduce already gets a ``<pre>`` block that respects
    newlines, but Actual Result / Expected Result / Comment used to
    render as flowing prose, which made enumerated lists hard to read.

    This filter:
    * Returns the value untouched (escaped) for short single-item text
      (so a normal sentence still flows).
    * Splits on ``" N. "`` boundaries when 2+ numbered items are
      detected — including the case where the source had no newlines.
    * Preserves original newlines if the writer already split items.
    * Wraps multi-item content in a ``<pre class="steps">`` block so
      it visually matches Steps to Reproduce.
    """
    if not s:
        return "—"
    text = str(s).strip()
    # Detect inline-numbered enumerations: "1. ... 2. ... 3. ..."
    inline = re.search(r"(?:^|\s)\d+\.\s+\S", text)
    has_multiple = len(re.findall(r"(?:^|[\s\n])\d+\.\s+", text)) >= 2
    has_newlines = "\n" in text
    if not (has_newlines or (inline and has_multiple)):
        return text  # ordinary prose, render as-is

    # Split into items keyed by leading "N. "
    if has_multiple:
        # Insert a line break before every " N. " marker. The lookbehind
        # ``(?<=\D)`` plus the ``\s+`` ensures we don't trip on
        # decimals like "1.2.3" (no whitespace between digits) or
        # version strings like "v1.5.0" (the second digit-block has no
        # trailing space). The lookahead requires a space after the
        # dot — that excludes "1.2." substrings inside semver text.
        normalised = re.sub(r"(?<=\D)\s+(\d+\.\s)", r"\n\1", text)
    else:
        normalised = text

    escaped = markupsafe.escape(normalised)
    return markupsafe.Markup(
        f'<pre class="steps">{escaped}</pre>'
    )


# ── Security headers + session hygiene ───────────────────────────

@app.before_request
def _mint_csp_nonce():
    # One nonce per request, exposed to templates via a context
    # processor. Inline <script> blocks carry nonce="{{ csp_nonce }}"
    # so script-src can drop 'unsafe-inline'.
    g.csp_nonce = secrets.token_urlsafe(16)


@app.context_processor
def _inject_csp_nonce():
    return {"csp_nonce": getattr(g, "csp_nonce", "")}


@app.after_request
def _maybe_set_persistent_sid_cookie(resp):
    """PR-L: mint the persistent ``_tfg_sid_v1`` cookie on the way
    out when the browser doesn't already have it and the current
    session has an established SID worth preserving across Render
    redeploys.

    The cookie is signed by ``SECRET_KEY`` (which Render persists
    across redeploys), so the SID survives the filesystem-session
    wipe that happens on every redeploy. Without this hook, every
    redeploy would create a new SID, orphaning the user's projects
    in Postgres under their previous SID and showing a fresh
    "Untitled project" in the dropdown.

    Idempotent: called on every response. Skips when the cookie
    already exists OR the session has no SID yet to promote.
    """
    try:
        from routes._shared import (
            needs_persistent_sid_cookie, set_persistent_sid_cookie,
        )
        if needs_persistent_sid_cookie():
            set_persistent_sid_cookie(resp)
    except Exception:
        # Never let the cookie hook 500 the response — the SID
        # fallback in ``get_session_id`` keeps the app functional
        # even when the persistent cookie isn't set.
        pass
    return resp


@app.after_request
def _apply_security_headers(resp):
    nonce = getattr(g, "csp_nonce", "") or secrets.token_urlsafe(16)
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        f"script-src 'self' 'nonce-{nonce}' https://unpkg.com; "
        "connect-src 'self'; "
        "frame-ancestors 'none'",
    )
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy",
                            "geolocation=(), microphone=(), camera=()")
    # Cross-origin isolation. X-Frame-Options: DENY already stops this app
    # being framed; COOP is the other direction — it severs the window
    # reference a page *we* open (or that opens us) would otherwise keep,
    # which is what makes a tabnabbing or cross-window-scripting attempt
    # cheap. `same-origin-allow-popups` rather than `same-origin` because
    # the Google sign-in flow opens a popup and needs to talk back to it.
    resp.headers.setdefault("Cross-Origin-Opener-Policy",
                            "same-origin-allow-popups")
    # HSTS — only when we know TLS terminates in front of us
    # (``BEHIND_HTTPS=1``). Emitting it over plain HTTP (e.g. local dev)
    # would wrongly pin the loopback host to HTTPS. Two years + preload
    # matches the hstspreload.org submission requirements; includeSubDomains
    # is safe because the whole *.onrender.com host is HTTPS-only.
    if _config.BEHIND_HTTPS:
        resp.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=63072000; includeSubDomains; preload")
    return resp


@app.before_request
def before_request():
    # Invalidate generated data persisted from a previous server run.
    # Filesystem sessions persist across restarts; we want a clean slate
    # so users never see stale data from a previous launch.
    last_active = session.get("_session_active_since", 0)
    if last_active < SERVER_START_TIME:
        for key in GENERATED_KEYS:
            session.pop(key, None)
        session["_session_active_since"] = SERVER_START_TIME

    lang = request.args.get("lang") or session.get("lang", "en")
    if lang not in ("en", "ua"):
        lang = "en"
    if request.args.get("lang"):
        session["lang"] = lang
    g.lang = lang
    g.t = get_lang(lang)


# ── Route registration ───────────────────────────────────────────

register_all(app)


if __name__ == "__main__":
    # Debug mode is gated on the FLASK_DEBUG env var (loaded by config.py).
    # Never hard-code ``debug=True`` — the Werkzeug debugger exposes RCE
    # via the debugger PIN if it reaches a production host.
    #
    # Bind host: when TESTFORTGE_HOST is set we honour it (e.g. ``0.0.0.0``
    # to expose on the LAN). Default stays loopback-only so a fresh clone
    # never leaks to the network without an explicit opt-in. The Basic
    # Auth gate (``engine.basic_auth``) is the second line of defence
    # whenever the host is not 127.0.0.1.
    host = os.environ.get("TESTFORTGE_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("TESTFORTGE_PORT", "5000"))
    app.run(host=host, port=port, debug=_config.DEBUG)
