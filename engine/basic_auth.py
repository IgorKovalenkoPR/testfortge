"""
TestFortge — HTTP Basic Auth gate.

Single-tenant shared password protection for the whole app. Configured
via two env vars:

* ``TESTFORTGE_BASIC_USER``     — the username colleagues type
* ``TESTFORTGE_BASIC_PASSWORD`` — the matching password

When *both* are set the gate is active: every request that hits a
non-public path is rejected with HTTP 401 ``WWW-Authenticate: Basic`` if
its ``Authorization`` header is missing or wrong. When either is empty
the gate is a no-op, so local dev keeps working without configuration.

Comparisons use ``hmac.compare_digest`` to avoid timing leaks.

Public paths are an opt-in allowlist — by default ``/healthz`` is open
so external monitors keep working without credentials. Override via the
``TESTFORTGE_BASIC_PUBLIC_PATHS`` env var (comma-separated absolute
paths or ``/prefix*`` globs).
"""
from __future__ import annotations

import base64
import hmac
import os
from typing import Tuple

from flask import Response, request

_REALM = "TestFortge"

# ── Config loading ─────────────────────────────────────────────────

def _split_paths(raw: str) -> Tuple[str, ...]:
    return tuple(p.strip() for p in raw.split(",") if p.strip())


_USER = os.environ.get("TESTFORTGE_BASIC_USER", "").strip()
_PASSWORD = os.environ.get("TESTFORTGE_BASIC_PASSWORD", "").strip()
_PUBLIC_PATHS = _split_paths(
    os.environ.get("TESTFORTGE_BASIC_PUBLIC_PATHS", "/healthz")
)


def is_enabled() -> bool:
    return bool(_USER and _PASSWORD)


def _is_public(path: str) -> bool:
    for rule in _PUBLIC_PATHS:
        if rule.endswith("*"):
            if path.startswith(rule[:-1]):
                return True
        elif path == rule:
            return True
    return False


def _decode_credentials(header: str) -> Tuple[str, str] | None:
    """Parse a ``Basic <b64(user:pass)>`` header into a (user, pwd) tuple."""
    if not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    user, _, pwd = decoded.partition(":")
    return user, pwd


def _credentials_match(user: str, pwd: str) -> bool:
    user_ok = hmac.compare_digest(user.encode("utf-8"), _USER.encode("utf-8"))
    pwd_ok = hmac.compare_digest(pwd.encode("utf-8"), _PASSWORD.encode("utf-8"))
    return user_ok and pwd_ok


def _challenge() -> Response:
    return Response(
        "Authentication required.\n",
        status=401,
        headers={"WWW-Authenticate": f'Basic realm="{_REALM}", charset="UTF-8"'},
        content_type="text/plain; charset=utf-8",
    )


# ── Flask integration ──────────────────────────────────────────────

def install(app) -> None:
    """Wire the auth gate into ``app`` if env vars are configured."""
    if not is_enabled():
        return

    @app.before_request
    def _basic_auth_gate():  # pragma: no cover — exercised via integration
        # Tests run with `TESTING=True` and a Flask test_client; treat
        # them as already-authenticated so existing fixtures don't all
        # need to inject an Authorization header.
        if app.config.get("TESTING"):
            return None
        # Allow unauthenticated probes for liveness so external monitors
        # (Docker HEALTHCHECK, uptime pingers) keep working.
        if _is_public(request.path):
            return None
        header = request.headers.get("Authorization", "")
        creds = _decode_credentials(header)
        if creds is None or not _credentials_match(*creds):
            return _challenge()
        return None
