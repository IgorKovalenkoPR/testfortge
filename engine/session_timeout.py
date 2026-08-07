"""TestFortge — idle and absolute session timeouts (E1.5).

The rest of E1.5 shipped with E1.2: ``permissions.login_user`` rotates the
session id against fixation, and ``logout_user(everywhere=True)`` drops a
user's other sessions. What was missing is the pair of clocks — a session
that is never used again, and a session that is used forever.

Two clocks, because they answer different questions
---------------------------------------------------
* **Idle** — "nobody has touched this in a while." Bounds the window in
  which an unlocked laptop or a walked-away-from browser is a live
  session.
* **Absolute** — "this session has existed too long, however busy it has
  been." Bounds what a stolen cookie is worth. An idle timeout alone does
  not: an attacker holding the cookie *is* activity, so they can keep it
  alive indefinitely.

One without the other is a common half-measure, which is why both are
here and why the reason survives to the sign-in page.

What ``_session_active_since`` is not
-------------------------------------
``app.py`` writes a key of that name and it is **not** a session clock —
it holds ``SERVER_START_TIME`` and exists to drop generated artefacts left
by a previous process. It is easy to mistake for one, and reusing it as
one would tie a security control to a cold-start marker.

The distinction this module has to make
---------------------------------------
On the free plan the deployment already loses sessions constantly: the
service sleeps after ~15 idle minutes and ``SESSION_TYPE=filesystem``
lives on an ephemeral disk, so "I have to sign in again" is a routine
event with nothing to do with any timeout. If an expiry said only "sign in
again", the two causes would be indistinguishable, and the honest one —
"we dropped your session, not you" — is the one a user is owed.

So an expiry carries its reason out to the sign-in page
(:data:`REASON_PARAM`), and the sign-in route says a different sentence
for a vanished store. See :func:`explain`.

A consequence worth stating plainly: on today's deployment the *store*
already enforces something far tighter than :data:`IDLE_DEFAULT_MINUTES`,
so the clock that actually bites here is the absolute one. The idle clock
starts mattering with ``SESSION_BACKEND=db``, where rows survive restarts
for two weeks.

Testability
-----------
:func:`classify` is a pure function of ``(stamps, now)``. Nothing here
sleeps, and no test of it needs to: a test hands it a ``now`` twelve hours
on, or writes a stale stamp into the session dict. Real-time sleeps in a
test of a timeout are how a suite acquires a flaky test it cannot delete.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from engine.log import get_logger

log = get_logger(__name__)

#: Session keys owned by this module.
#:
#: ``AUTH_AT`` is stamped once, at sign-in, and never moves — it is the
#: absolute clock's origin. ``SEEN_AT`` moves with activity.
AUTH_AT_KEY = "_auth_at"
SEEN_AT_KEY = "_seen_at"

#: Query parameter carrying the reason to the sign-in page.
REASON_PARAM = "expired"

#: The two reasons, as they appear in that parameter.
IDLE = "idle"
ABSOLUTE = "absolute"

#: Minutes of inactivity before a session ends.
#:
#: Four hours: long enough to survive a meeting, a lunch, or a long
#: generation watched from another tab, and short enough that a browser
#: left open on a shared desk overnight is not a live session in the
#: morning. Overridable with ``SESSION_IDLE_MINUTES``.
IDLE_DEFAULT_MINUTES = 4 * 60

#: Hours a session may live regardless of activity.
#:
#: A day. This is the number that bounds a stolen cookie, since an
#: attacker's own requests keep the idle clock fresh. Overridable with
#: ``SESSION_ABSOLUTE_HOURS``.
ABSOLUTE_DEFAULT_HOURS = 24

#: Don't rewrite the idle stamp more often than this, in seconds.
#:
#: Not a tuning knob so much as a correctness one. Writing ``SEEN_AT``
#: on every request marks the session modified on every request, and one
#: page load here fires roughly a dozen parallel requests — which is the
#: exact shape that made ``db.session_save`` need an
#: IntegrityError-then-UPDATE retry. A minute of granularity is far finer
#: than a four-hour window needs and costs one write per minute of use.
SEEN_WRITE_GRANULARITY_SECONDS = 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _positive_int(env_name: str, default: int) -> int:
    """A positive integer from the environment, or *default*.

    Zero and negatives are refused rather than honoured: read literally,
    ``SESSION_IDLE_MINUTES=0`` means "expire every session immediately",
    which nobody types on purpose and which would lock everyone out of a
    running deployment. A typo should not be able to do that, so it falls
    back and says so.
    """
    raw = (os.environ.get(env_name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer — using %d.",
                    env_name, raw, default)
        return default
    if value <= 0:
        log.warning("%s=%d is not a usable window — using %d. To switch "
                    "timeouts off, set the window very large instead.",
                    env_name, value, default)
        return default
    return value


def idle_seconds() -> int:
    return _positive_int("SESSION_IDLE_MINUTES", IDLE_DEFAULT_MINUTES) * 60


def absolute_seconds() -> int:
    return _positive_int("SESSION_ABSOLUTE_HOURS",
                         ABSOLUTE_DEFAULT_HOURS) * 3600


# ── The decision, as a pure function ─────────────────────────────────

def stamp(session) -> None:
    """Start both clocks. Called from ``permissions.login_user`` only.

    One call site rather than one per sign-in path, for the reason that
    module gives for having a single resolver: there are four ways in
    (password, invite, Google, invite-via-Google) and a per-path stamp is
    a per-path chance to forget one.
    """
    now = _utcnow().timestamp()
    session[AUTH_AT_KEY] = now
    session[SEEN_AT_KEY] = now


def classify(stamps: dict, now: float, *,
             idle: int | None = None,
             absolute: int | None = None) -> str | None:
    """Why this session should end, or ``None`` if it should not.

    *stamps* is anything with ``.get`` — the live session, or a plain dict
    in a test. Absent or unparseable stamps return ``None``: a session that
    predates this module must not be thrown out on the deploy that adds it,
    and a corrupt number is not evidence of an attack. The next sign-in
    stamps it properly.

    Absolute is checked first. When both windows have passed, the honest
    answer is the harder limit, and it is the one whose message ("sessions
    end after a day") does not invite the user to conclude they merely
    stepped away.
    """
    idle_window = idle_seconds() if idle is None else idle
    absolute_window = absolute_seconds() if absolute is None else absolute

    auth_at = _as_timestamp(stamps.get(AUTH_AT_KEY))
    if auth_at is not None and now - auth_at > absolute_window:
        return ABSOLUTE

    seen_at = _as_timestamp(stamps.get(SEEN_AT_KEY))
    if seen_at is not None and now - seen_at > idle_window:
        return IDLE

    return None


def _as_timestamp(value) -> float | None:
    """A POSIX timestamp from whatever survived a round trip, or ``None``.

    JSON-serialised sessions turn a float into a float, but a session
    written by an older build — or hand-edited in a test — can hold a
    string or an ISO date. Coerced rather than trusted, because the
    alternative to a ``None`` here is a ``TypeError`` inside a
    ``before_request`` hook, which is a 500 on every page.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def touch(session, now: float | None = None) -> bool:
    """Move the idle clock forward. True when the session was written.

    Throttled — see :data:`SEEN_WRITE_GRANULARITY_SECONDS`.
    """
    moment = _utcnow().timestamp() if now is None else now
    previous = _as_timestamp(session.get(SEEN_AT_KEY))
    if previous is not None and \
            moment - previous < SEEN_WRITE_GRANULARITY_SECONDS:
        return False
    session[SEEN_AT_KEY] = moment
    return True


# ── Ending the session ───────────────────────────────────────────────

def expire(session, reason: str) -> None:
    """Drop the session and its server-side row.

    The row as well as the dict: an emptied dict that is still addressed by
    the same id leaves the cookie replayable against whatever a later
    request writes there. ``login_user`` deletes the pre-login row for the
    same reason on the way in.
    """
    from engine import db as _db

    sid = getattr(session, "sid", None)
    user_id = session.get("_user_id")
    carried = {k: session.get(k) for k in ("lang",) if session.get(k)}
    session.clear()
    session.update(carried)
    session.modified = True
    if sid:
        try:
            _db.session_delete(sid)
        except Exception as exc:  # pragma: no cover — best-effort
            log.warning("could not delete expired session row: %s", exc)
    log.info("session expired (%s) for user %s", reason,
             (user_id or "?")[:8])


def explain(reason: str | None) -> str | None:
    """The sentence for a sign-in page, or ``None`` for no reason given.

    Written for the person reading it: what happened, and that they did
    nothing wrong. The numbers come from the live configuration rather
    than the prose, so an operator who widens a window does not leave a
    page contradicting it.
    """
    if reason == IDLE:
        hours = idle_seconds() // 3600
        minutes = (idle_seconds() % 3600) // 60
        if hours and minutes:
            window = f"{hours} hours {minutes} minutes"
        elif hours:
            window = f"{hours} hour{'s' if hours != 1 else ''}"
        else:
            window = f"{minutes} minute{'s' if minutes != 1 else ''}"
        return (f"You were signed out after {window} without activity. "
                f"Sign in to carry on — your saved work is untouched.")
    if reason == ABSOLUTE:
        hours = absolute_seconds() // 3600
        return (f"Sessions end after {hours} hours, even active ones, so "
                f"yours has finished. Sign in to carry on — your saved "
                f"work is untouched.")
    return None


#: ``flask.g`` attribute recording that the browser's session cookie named
#: nothing the server had.
_LOST_ON_G = "_session_was_lost"


def session_was_lost() -> bool:
    """True when this request arrived with a cookie the store did not know.

    Recorded by the hook rather than computed on demand, and that is the
    whole difficulty: by the time a view runs, ``app.py``'s own
    ``before_request`` has already written ``_session_active_since`` into
    the session, so "the session is empty" is no longer answerable. The
    hook is registered early enough to see it.

    The obvious alternatives were tried and do not work here. Flask-Session
    never sets ``SessionMixin.new``, so it reads ``False`` for a cookie that
    resolved to nothing; and comparing the cookie value with ``session.sid``
    is true for one backend, false for the other, and depends on library
    internals either way.
    """
    try:
        from flask import g
        return bool(getattr(g, _LOST_ON_G, False))
    except Exception:      # outside a request context
        return False


def valid_reason(raw: str | None) -> str | None:
    """*raw* if it is one of ours, else ``None``.

    The reason arrives in a query parameter, so it is attacker-supplied
    text. Nothing security-relevant hangs off it — the session is already
    gone by the time it is read — but it reaches a template, so it is
    matched against a closed set rather than echoed.
    """
    value = (raw or "").strip()
    return value if value in (IDLE, ABSOLUTE) else None


# ── The hook ─────────────────────────────────────────────────────────

def install(app) -> None:
    """Attach the enforcement hook.

    Registration order carries two requirements, and both are the reason
    this is called where it is rather than beside the other route wiring.
    Flask runs ``before_request`` handlers in the order they were
    registered, so:

    * **before ``app.py``'s own hook**, which writes
      ``_session_active_since`` into the session on a cold start. After
      that has run, an empty session is indistinguishable from one holding
      a single framework key, and :func:`session_was_lost` has nothing to
      measure;
    * **before the route-policy hook**, so an expired session reads as
      anonymous by the time the policy decides whether this caller may be
      here. That one is registered from ``routes/__init__.py``, much later,
      so it is satisfied by construction.
    """
    from flask import g, redirect, request, session, url_for

    @app.before_request
    def _enforce_session_timeouts():
        from engine import permissions as _perm

        # While AUTH_ENABLED is off there is no sign-in to return to, and
        # the app behaves exactly as it did before this programme — no
        # expiry, and no message about one.
        if not _perm.auth_active():
            return None

        if not session.get(_perm.SESSION_USER_KEY):
            # Nobody to sign out. Before leaving, note whether this browser
            # presented a cookie the store could not resolve — the free
            # dyno's nap takes the whole session store with it, and the
            # sign-in page owes that person a different sentence from the
            # one it gives someone who timed out. This is the only moment
            # the difference is visible.
            cookie_name = app.session_interface.get_cookie_name(app)
            if request.cookies.get(cookie_name) and not dict(session):
                setattr(g, _LOST_ON_G, True)
            return None

        reason = classify(session, _utcnow().timestamp())
        if reason is None:
            touch(session)
            return None

        expire(session, reason)

        from engine import route_policy as _route_policy
        endpoint = request.endpoint
        if endpoint is None or _route_policy.is_open(endpoint):
            # The sign-in page, the sign-out post, static files, the health
            # probes. The session is dead either way, but redirecting here
            # would bounce /auth/login to itself forever.
            return None

        if _perm._wants_json():
            from flask import jsonify
            # A distinct code from plain ``auth_required``: a tab polling a
            # progress endpoint can tell "your session ended" from "you
            # were never signed in" and say so instead of silently
            # reloading into a sign-in page.
            return jsonify({
                "error": "session_expired",
                "reason": reason,
                "message": explain(reason),
            }), 401

        return redirect(url_for("auth_login", next=request.full_path,
                                **{REASON_PARAM: reason}))

    log.info("Session timeouts installed: idle %d min, absolute %d h.",
             idle_seconds() // 60, absolute_seconds() // 3600)


__all__ = [
    "AUTH_AT_KEY", "SEEN_AT_KEY", "REASON_PARAM", "IDLE", "ABSOLUTE",
    "IDLE_DEFAULT_MINUTES", "ABSOLUTE_DEFAULT_HOURS",
    "SEEN_WRITE_GRANULARITY_SECONDS",
    "idle_seconds", "absolute_seconds",
    "stamp", "classify", "touch", "expire", "explain", "valid_reason",
    "session_was_lost", "install",
]
