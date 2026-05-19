"""
TestFortge — runtime configuration.

All security-sensitive settings are sourced here so the rest of the
application can remain declarative. Loads a ``.env`` file if present
(via :mod:`python-dotenv`) and exposes :func:`apply` which mutates the
Flask app config in place.

Guiding principles
------------------
* **SECRET_KEY** must come from the environment. If it is missing we
  refuse to start in non-debug mode, and emit a loud warning in debug.
* Cookies default to the safest modern values (HttpOnly + SameSite=Lax;
  Secure is enabled automatically when the env says we run behind HTTPS).
* Upload size is bounded (``MAX_CONTENT_LENGTH``) to prevent OOM /
  filesystem exhaustion attacks.
* Paths for uploads / storage / sessions live inside the project root by
  default but can be overridden per-env for production.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()  # safe no-op if .env is absent
except ImportError:  # pragma: no cover — dotenv is a hard dep, but be defensive
    pass


# ── Paths ─────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent

UPLOAD_FOLDER = Path(os.environ.get("UPLOAD_FOLDER",
                                      PROJECT_ROOT / "uploads"))
STORAGE_FOLDER = Path(os.environ.get("STORAGE_FOLDER",
                                      PROJECT_ROOT / "storage"))
SESSION_DIR = Path(os.environ.get("SESSION_FILE_DIR",
                                    PROJECT_ROOT / "flask_session"))

for _p in (UPLOAD_FOLDER, STORAGE_FOLDER, SESSION_DIR):
    _p.mkdir(parents=True, exist_ok=True)


# ── Feature flags ─────────────────────────────────────────────────
DEBUG = os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes"}
BEHIND_HTTPS = os.environ.get("BEHIND_HTTPS", "").lower() in {"1", "true", "yes"}

# SSRF allowlist opt-out. When ``SSRF_ALLOWLIST_BYPASS=1`` is set in the
# environment, :func:`engine.security.require_safe_url` becomes a no-op
# so the engine can target operator-controlled staging boxes on
# RFC1918 / loopback addresses (e.g. ``http://192.168.1.10:3000``).
# Default is OFF — production deploys keep the allowlist active to
# prevent SSRF to cloud metadata / internal services. Read fresh from
# the env at each guard call (see engine.security), so we don't capture
# the value here; this constant just exists for ops visibility.
SSRF_ALLOWLIST_BYPASS = os.environ.get(
    "SSRF_ALLOWLIST_BYPASS", "").strip() == "1"


# ── Secrets ───────────────────────────────────────────────────────
def _resolve_secret_key() -> str:
    """Return a secret key, raising if we're in production without one.

    We *never* hard-code a production fallback. In debug mode, if the
    user hasn't provided SECRET_KEY, we mint a random ephemeral one
    (sessions die with the process) and log a warning so it's visible.
    """
    key = os.environ.get("SECRET_KEY", "").strip()
    if key:
        return key
    if DEBUG:
        import warnings
        warnings.warn(
            "SECRET_KEY not set — generated an ephemeral key for this run. "
            "Sessions will be invalidated on restart. Add SECRET_KEY=... "
            "to your .env for stable sessions.",
            RuntimeWarning,
            stacklevel=2,
        )
        return secrets.token_urlsafe(48)
    raise RuntimeError(
        "SECRET_KEY environment variable is required in production. "
        "Generate one with: python -c \"import secrets; "
        "print(secrets.token_urlsafe(48))\""
    )


SECRET_KEY = _resolve_secret_key()


# ── Limits ────────────────────────────────────────────────────────
# Max request body size: 64 MB. Individual file uploads (specs, docs,
# screenshots, short videos) fit well under this. Exceeding it returns
# HTTP 413 instead of OOMing the worker.
MAX_CONTENT_LENGTH = int(os.environ.get(
    "MAX_CONTENT_LENGTH", 64 * 1024 * 1024))

# /chat payload cap (chars). Anything bigger is almost certainly abuse.
CHAT_MESSAGE_MAX_CHARS = int(os.environ.get(
    "CHAT_MESSAGE_MAX_CHARS", 4000))

# /chat history cap (entries). Already enforced before but centralised.
CHAT_HISTORY_MAX_ENTRIES = int(os.environ.get(
    "CHAT_HISTORY_MAX_ENTRIES", 40))

# Estimation bounds — user input clamped to these ranges.
EST_MAX_ADDITIONAL_PLATFORMS = 30
EST_MAX_MINUTES_PER_TC = 120
EST_MAX_BUFFER_PERCENT = 200


# ── Applying to a Flask app ──────────────────────────────────────
def apply(app) -> None:
    """Populate the given Flask ``app`` with the hardened configuration."""
    app.secret_key = SECRET_KEY
    app.config.update(
        # Paths
        UPLOAD_FOLDER=str(UPLOAD_FOLDER),
        STORAGE_FOLDER=str(STORAGE_FOLDER),

        # Request limits
        MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,

        # Server-side filesystem session
        SESSION_TYPE="filesystem",
        SESSION_FILE_DIR=str(SESSION_DIR),
        SESSION_PERMANENT=False,
        SESSION_FILE_THRESHOLD=500,

        # Cookie hardening
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=BEHIND_HTTPS,

        # CSRF hardening (Flask-WTF reads these from app.config).
        # The test suite flips ``TESTING=True`` which also disables CSRF
        # via the conditional below; production defaults remain strict.
        WTF_CSRF_TIME_LIMIT=None,  # CSRF lives as long as the session
        WTF_CSRF_SSL_STRICT=BEHIND_HTTPS,
        WTF_CSRF_CHECK_DEFAULT=True,

        # Expose our own bounds to templates / routes
        CHAT_MESSAGE_MAX_CHARS=CHAT_MESSAGE_MAX_CHARS,
        CHAT_HISTORY_MAX_ENTRIES=CHAT_HISTORY_MAX_ENTRIES,
        EST_MAX_ADDITIONAL_PLATFORMS=EST_MAX_ADDITIONAL_PLATFORMS,
        EST_MAX_MINUTES_PER_TC=EST_MAX_MINUTES_PER_TC,
        EST_MAX_BUFFER_PERCENT=EST_MAX_BUFFER_PERCENT,
    )
