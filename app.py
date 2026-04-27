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
from flask import Flask, Response, g, request, session
from flask_session import Session
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError, generate_csrf
from flask_compress import Compress

import config as _config
from engine import basic_auth
from engine.i18n import get_lang
from engine.log import get_logger
from routes import register_all
from routes._shared import GENERATED_KEYS, SERVER_START_TIME

log = get_logger("testfortge")

app = Flask(__name__)
_config.apply(app)
Session(app)
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
basic_auth.install(app)
if basic_auth.is_enabled():
    log.info("HTTP Basic Auth gate is active.")


# ── Error handlers ───────────────────────────────────────────────

@app.errorhandler(CSRFError)
def _handle_csrf_error(e):
    log.warning("CSRF rejected: %s", e.description)
    return Response(
        "CSRF token missing or invalid. Reload the page and try again.",
        status=400,
        content_type="text/plain; charset=utf-8",
    )


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
    return {"t": g.t, "lang": g.lang}


@app.template_filter('nl2br')
def nl2br_filter(s):
    if not s:
        return s
    return markupsafe.Markup(str(markupsafe.escape(s)).replace('\n', '<br>\n'))


# ── Security headers + session hygiene ───────────────────────────

@app.after_request
def _apply_security_headers(resp):
    # CSP allows self + inline styles AND inline scripts. The templates rely
    # on inline <script> blocks (drag-drop, tabs, run-detail toggles, the
    # Lucide icon initialiser) and a number of inline ``onclick=`` handlers.
    # Without 'unsafe-inline' on script-src those silently fail under modern
    # browsers — leaving Drag & Drop / Upload / metric tabs / Test Execution
    # toggles non-functional. Tighten with nonces once every inline script
    # and onclick has been migrated to an external file.
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com; "
        "connect-src 'self'; "
        "frame-ancestors 'none'",
    )
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy",
                            "geolocation=(), microphone=(), camera=()")
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
