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

import markupsafe
from flask import Flask, Response, g, request, session
from flask_session import Session
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError, generate_csrf
from flask_compress import Compress

import config as _config
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
    # CSP allows self + inline styles (the current templates still rely on
    # a couple of inline ``style=`` attributes and the Lucide icon
    # initialiser). Tighten further once all inline styles are removed.
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "script-src 'self' https://unpkg.com; "
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
    app.run(debug=_config.DEBUG, port=5000)
