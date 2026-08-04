"""TestFortge — who is calling, and what they are allowed to do (E2.2).

One resolver, one set of decorators. The alternative — an ``if`` at each
route — is how a 403 bypass ships: eighty routes, each with its own copy
of the check, and the one that gets it wrong looks exactly like the
seventy-nine that do not. There is a test
(``tests/test_permissions.py::TestEveryRouteIsClassified``) that fails the
build when a new route appears without a declared policy, so "we forgot
one" is a build failure rather than a finding.

Roles, as the owner specified them:

* **admin** — creates projects, changes settings and configuration,
  invites people and changes their roles.
* **user** — everything else: test cases, checklists, runs, bugs,
  estimates, metrics.

Staged rollout
--------------
Both decorators are **no-ops while ``AUTH_ENABLED`` is off**, and role
checks are no-ops while ``ORG_MODE`` is off. That is what lets the whole
programme land on main behind a flag: routes get their annotations now,
and the annotations start biting when identity exists. The alternative is
a long-lived branch, which is worse.

Session keys owned by this module — ``_user_id`` and ``org_id``. Nothing
else may write them; ``engine.server_session`` reads ``_user_id`` to
attribute a session row to a user, and that is the only other consumer.
"""
from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from engine.log import get_logger

log = get_logger(__name__)

SESSION_USER_KEY = "_user_id"
SESSION_ORG_KEY = "org_id"


# ── Feature gates ─────────────────────────────────────────────────

def auth_active() -> bool:
    """True when real accounts are switched on."""
    from engine import features
    return features.is_enabled("AUTH_ENABLED")


def org_active() -> bool:
    """True when organisations and roles are switched on.

    Uses ``effective`` rather than ``is_enabled``: ORG_MODE without
    AUTH_ENABLED has nothing to attach a role to, and reading it as on
    would let a route believe it had a user.
    """
    from engine import features
    return features.effective("ORG_MODE")


# ── Who is calling ────────────────────────────────────────────────

def current_user_id() -> str | None:
    """The signed-in user's id, or ``None``."""
    try:
        from flask import session
        return session.get(SESSION_USER_KEY) or None
    except Exception:      # outside a request context
        return None


def current_user() -> dict | None:
    """The signed-in user's row, or ``None``.

    Re-checks ``is_active`` on every call rather than trusting the
    session. Deactivating an account has to take effect on the next
    request, not whenever the session happens to expire.
    """
    uid = current_user_id()
    if not uid:
        return None
    from engine import db as _db
    row = _db.get_user(uid)
    if row is None:
        return None
    if not row.get("is_active", True):
        log.info("session for deactivated user %s rejected", uid[:8])
        return None
    return row


def current_org_id() -> str | None:
    return _session_get(SESSION_ORG_KEY)


def _session_get(key: str) -> str | None:
    try:
        from flask import session
        return session.get(key) or None
    except Exception:
        return None


def current_role() -> str | None:
    """The caller's role in the active organisation, or ``None``.

    ``None`` means no access. It is never a default role — treating it as
    one is how a stranger becomes a tester.
    """
    uid, org = current_user_id(), current_org_id()
    if not (uid and org):
        return None
    from engine import db as _db
    return _db.get_org_role(org, uid)


def is_admin() -> bool:
    """For templates, so the UI can hide what the server would refuse.

    Hiding is UX. The server-side check is the security boundary, and it
    is the decorators below — never this.
    """
    if not org_active():
        # Without organisations there is nobody to be less than an admin,
        # so the legacy single-tenant UI keeps showing everything.
        return True
    return current_role() == "admin"


def has_role(minimum: str) -> bool:
    """Whether the caller holds at least *minimum* in the active org."""
    from engine import db as _db
    if not org_active():
        return True
    role = current_role()
    if role is None:
        return False
    want = _db.ROLE_RANK.get(minimum)
    if want is None:
        # An unknown requirement fails closed. A typo in a decorator must
        # not silently grant access to everyone.
        log.error("has_role called with unknown role %r — denying", minimum)
        return False
    return _db.ROLE_RANK.get(role, 0) >= want


# ── Sign in / out ─────────────────────────────────────────────────

def login_user(user_id: str, *, org_id: str | None = None) -> None:
    """Bind the current session to *user_id*, rotating the session id.

    The rotation is the security property, not bookkeeping. Without it an
    attacker who can plant a known session cookie before sign-in — a
    shared machine, a fixated id in a link — holds a valid *authenticated*
    session the moment the victim signs in.

    ``session.clear()`` alone does **not** achieve this, which is worth
    stating because it looks like it should: clearing empties the
    dictionary but leaves ``session.sid`` untouched, so the new
    authenticated payload is written straight back to the row the planted
    cookie names. Flask has no "regenerate id" call, so the sid is
    replaced explicitly and the old server-side row is deleted.
    """
    import secrets

    from flask import session
    from engine import db as _db

    old_sid = getattr(session, "sid", None)

    # Preserve the handful of keys that are about the browser, not the
    # identity. Everything else goes, including anything an attacker
    # planted.
    carried = {k: session.get(k) for k in ("lang",) if session.get(k)}
    session.clear()
    session.update(carried)

    # Rotate. Both our interface and Flask-Session's expose ``sid``; if a
    # future backend does not, the assignment is skipped rather than
    # crashing sign-in — but then fixation is not defended, so
    # ``_rotation_supported`` is asserted by the test suite.
    if old_sid is not None:
        try:
            session.sid = secrets.token_urlsafe(32)
        except Exception:  # pragma: no cover — read-only sid
            log.error("session id could not be rotated on login — "
                      "session fixation is NOT defended on this backend")
        else:
            # Kill the pre-login row, so replaying the old cookie lands on
            # nothing instead of on a session that is now signed in.
            try:
                _db.session_delete(old_sid)
            except Exception as exc:  # pragma: no cover — best-effort
                log.warning("could not drop pre-login session row: %s", exc)

    session[SESSION_USER_KEY] = user_id
    if org_id:
        session[SESSION_ORG_KEY] = org_id
    session.modified = True
    _db.touch_last_login(user_id)


def logout_user(*, everywhere: bool = False) -> None:
    """Sign out. With ``everywhere``, drop this user's other sessions too."""
    from flask import session
    uid = current_user_id()
    sid = getattr(session, "sid", None)
    session.clear()
    session.modified = True
    if everywhere and uid:
        from engine import db as _db
        n = _db.delete_sessions_for_user(uid, except_sid=sid)
        log.info("signed user %s out of %d other session(s)", uid[:8], n)


def set_active_org(org_id: str) -> None:
    from flask import session
    session[SESSION_ORG_KEY] = org_id
    session.modified = True


# ── Decorators ────────────────────────────────────────────────────

def require_login(view: Callable) -> Callable:
    """Reject anonymous callers once ``AUTH_ENABLED`` is on."""

    @wraps(view)
    def _wrapped(*args: Any, **kwargs: Any):
        if not auth_active():
            return view(*args, **kwargs)
        if current_user() is None:
            return _deny_unauthenticated()
        return view(*args, **kwargs)

    _wrapped._required_role = "login"       # read by the coverage test
    return _wrapped


def require_role(minimum: str) -> Callable:
    """Require at least *minimum* in the active organisation.

    Implies :func:`require_login` — a role check on an anonymous caller
    would otherwise resolve to "no role" and return 403 where 401 is the
    honest answer.
    """

    def _decorator(view: Callable) -> Callable:
        @wraps(view)
        def _wrapped(*args: Any, **kwargs: Any):
            if not auth_active():
                return view(*args, **kwargs)
            if current_user() is None:
                return _deny_unauthenticated()
            if not has_role(minimum):
                return _deny_forbidden(minimum)
            return view(*args, **kwargs)

        _wrapped._required_role = minimum
        return _wrapped

    return _decorator


def _wants_json() -> bool:
    from flask import request
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def _deny_unauthenticated():
    """401 for a fetch, a redirect to sign-in for a browser.

    The ``next`` parameter is validated on the way back out in
    ``routes/auth.py`` — an unvalidated one is an open redirect, and a
    sign-in page is the most attractive possible place for one.
    """
    from flask import jsonify, redirect, request, url_for
    if _wants_json():
        return jsonify({"error": "auth_required",
                        "message": "Sign in to continue."}), 401
    return redirect(url_for("auth_login", next=request.full_path))


def _deny_forbidden(minimum: str):
    from flask import jsonify, render_template
    if _wants_json():
        return jsonify({
            "error": "forbidden",
            "message": f"This action needs the {minimum} role.",
            "required_role": minimum,
        }), 403
    try:
        return render_template("403.html", required_role=minimum), 403
    except Exception:      # template missing — still refuse, plainly
        return (f"Forbidden — this action needs the {minimum} role.", 403)


def template_context() -> dict:
    """Injected into every template so the UI can match the server."""
    user = current_user()
    return {
        "auth_active": auth_active(),
        "org_active": org_active(),
        "current_user": user,
        "current_role": current_role(),
        "is_admin": is_admin(),
    }


__all__ = [
    "SESSION_USER_KEY", "SESSION_ORG_KEY",
    "auth_active", "org_active",
    "current_user", "current_user_id", "current_org_id", "current_role",
    "is_admin", "has_role",
    "login_user", "logout_user", "set_active_org",
    "require_login", "require_role", "template_context",
]
