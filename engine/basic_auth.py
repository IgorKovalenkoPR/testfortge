"""
TestFortge — HTTP Basic Auth gate, and how it stands down (E1.8).

Single-tenant shared password protection for the whole app. Configured via
two env vars:

* ``TESTFORTGE_BASIC_USER``     — the username colleagues type
* ``TESTFORTGE_BASIC_PASSWORD`` — the matching password

This was the *only* authentication the platform had before E1, which is
why it fronts everything. E1 replaced it with real accounts, and E1.8 is
the retirement: the gate now also answers to ``BASIC_GATE_ENABLED``, so
dropping it is one dashboard edit and putting it back is the same edit.

Comparisons use ``hmac.compare_digest`` to avoid timing leaks.

It refuses to stand down onto an open instance
---------------------------------------------
``BASIC_GATE_ENABLED=0`` with ``AUTH_ENABLED=0`` asks for every door to be
unlocked at once: no shared password and no accounts either. That is not a
configuration anyone wants and it is one keystroke away from the one they
do want, so :func:`is_enabled` **keeps the gate up** and logs an error
naming the combination. Fail closed, the same way an unclassified route in
``engine.route_policy`` is refused rather than served.

The exemptions come from the route table, not from an env var
------------------------------------------------------------
There are two gates in front of this app — this one and the session policy
in ``engine.route_policy`` — and until E1.8 each had its own hand-written
list of what to let through. This one's list lived in
``TESTFORTGE_BASIC_PUBLIC_PATHS`` and defaulted to ``/healthz,/readyz``,
which meant that on production, with the gate up, **every
token-authenticated machine caller got a 401 from the perimeter before its
own credential was ever read**: the Chrome extension could not start a
recording, and CI could not post an Allure bundle. Nothing said so,
because a 401 from a gate looks like a 401 from an endpoint.

So the allowlist is now ``route_policy.MACHINE`` — declared once, beside
the policy table it has to agree with, and validated against it at boot.
The env var still works and is still additive, for a path nobody
anticipated.
"""
from __future__ import annotations

import base64
import hmac
import os
from typing import Tuple

from flask import Response, request

from engine.log import get_logger

log = get_logger(__name__)

_REALM = "TestFortge"

# ── Config loading ─────────────────────────────────────────────────

def _split_paths(raw: str) -> Tuple[str, ...]:
    return tuple(p.strip() for p in raw.split(",") if p.strip())


_USER = os.environ.get("TESTFORTGE_BASIC_USER", "").strip()
_PASSWORD = os.environ.get("TESTFORTGE_BASIC_PASSWORD", "").strip()
_PUBLIC_PATHS = _split_paths(
    os.environ.get("TESTFORTGE_BASIC_PUBLIC_PATHS", "/healthz,/readyz")
)


def credentials_configured() -> bool:
    """True when a shared password exists at all.

    Read at import, like the credentials themselves. Separate from
    :func:`is_enabled` because "there is no password" and "there is one and
    we are choosing not to use it" are different states that want different
    words in the boot log.
    """
    return bool(_USER and _PASSWORD)


def _gate_wanted() -> bool:
    """Whether the operator has asked for the gate. Read per request."""
    from engine import features
    return features.is_enabled("BASIC_GATE_ENABLED")


def _accounts_exist() -> bool:
    from engine import features
    return features.is_enabled("AUTH_ENABLED")


def standing_down_refused() -> bool:
    """True when the interlock is overriding the operator's request.

    The single definition of the interlock: :func:`is_enabled` is written in
    terms of this rather than repeating the condition, so the gate cannot
    end up down while the log says it was kept up. Mutating one of two
    copies is exactly the kind of edit that leaves those two disagreeing and
    only one of them tested.
    """
    return (credentials_configured() and not _gate_wanted()
            and not _accounts_exist())


def is_enabled() -> bool:
    """Whether the gate is actually guarding this instance right now.

    Wanted, or kept up against the operator's wishes because there would be
    nothing behind it.
    """
    if not credentials_configured():
        return False
    return _gate_wanted() or standing_down_refused()


def status() -> str:
    """One line for the boot log — what the perimeter is doing and why."""
    if not credentials_configured():
        return ("HTTP Basic gate is not configured "
                "(TESTFORTGE_BASIC_USER / _PASSWORD unset).")
    if standing_down_refused():
        return ("HTTP Basic gate is STAYING UP: BASIC_GATE_ENABLED=0 was "
                "requested but AUTH_ENABLED is off, so dropping it would "
                "leave this instance with no authentication at all. Turn "
                "AUTH_ENABLED on first.")
    if is_enabled():
        return "HTTP Basic Auth gate is active."
    return ("HTTP Basic gate has stood down (BASIC_GATE_ENABLED=0); real "
            "accounts are the only way in.")


def _is_public(path: str) -> bool:
    """Paths exempted by the env var. Additive to :data:`MACHINE`."""
    for rule in _PUBLIC_PATHS:
        if rule.endswith("*"):
            if path.startswith(rule[:-1]):
                return True
        elif path == rule:
            return True
    return False


def _is_machine_endpoint(endpoint: str | None) -> bool:
    """True for a caller that carries its own credential.

    Keyed on the **endpoint**, not the path: an endpoint name is exact,
    while a path list has to be kept in step with every rule that gains a
    prefix or a converter.
    """
    if not endpoint:
        return False
    try:
        from engine import route_policy
        return endpoint in route_policy.MACHINE
    except Exception:      # pragma: no cover — import cycle at boot
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
    """Wire the gate in, if a shared password exists at all.

    Registered whenever credentials are configured, and the *decision* is
    made per request. That is what makes ``BASIC_GATE_ENABLED`` a live
    switch rather than a redeploy: with the hook conditional on the flag at
    import time, flipping it would do nothing until the process restarted —
    and a perimeter you cannot verify without a deploy is one nobody tests.
    """
    if not credentials_configured():
        return

    @app.before_request
    def _basic_auth_gate():
        # Tests run with `TESTING=True` and a Flask test_client; treat
        # them as already-authenticated so existing fixtures don't all
        # need to inject an Authorization header. The file that checks
        # this gate turns TESTING off on purpose.
        if app.config.get("TESTING"):
            return None
        if not is_enabled():
            return None
        # Callers with their own credential, and the ops probes. See
        # route_policy.MACHINE for why this is not an env var.
        if _is_machine_endpoint(request.endpoint):
            return None
        if _is_public(request.path):
            return None
        header = request.headers.get("Authorization", "")
        creds = _decode_credentials(header)
        if creds is None or not _credentials_match(*creds):
            return _challenge()
        return None


__all__ = [
    "credentials_configured", "is_enabled", "standing_down_refused",
    "status", "install",
]
