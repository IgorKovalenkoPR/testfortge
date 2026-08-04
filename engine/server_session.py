"""TestFortge — server-side sessions kept in Postgres (E0.2).

Replaces ``SESSION_TYPE="filesystem"``, which stored each session as a
pickle under ``flask_session/``. That worked for one process on one box
and fails on this deployment in two specific ways:

1. **The filesystem is not persistent.** A free-tier dyno sleeps after
   ~15 idle minutes and comes back with an empty disk. Since the Flask
   session is currently where a project's *working state* lives (see
   ``docs/plans/adr/0001``), a nap looked to the user like "my test cases
   disappeared". The existing code has visible scar tissue from this —
   ``routes/generation.py`` restores packs from Postgres on GET precisely
   because the session had evaporated.
2. **It is per-process.** Two gunicorn workers do not share a session
   directory reliably, and nothing can enumerate "every session for this
   user", which is what signing out of all devices and invalidating
   sessions on password reset both require (E1.5).

Rows live in ``engine.db.ServerSession``.

Why the cookie holds a bare random id and is not signed
------------------------------------------------------
The cookie value is 32 bytes from ``secrets.token_urlsafe`` used purely
as a database key. Signing exists to stop a client forging a *meaningful*
value; here the only thing a forger can produce is a string that is not a
key in the table, which loads as a brand-new empty session. There is
nothing to tamper with, so a signature would add ceremony and a second
failure mode (``SECRET_KEY`` rotation invalidating every session) for no
gain. This is the standard opaque-token pattern; the security property
comes from 256 bits of entropy, not from itsdangerous.

The cookie keeps ``HttpOnly``, ``SameSite`` and ``Secure`` from the app
config exactly as the filesystem backend did.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from flask.sessions import SessionInterface, SessionMixin
from werkzeug.datastructures import CallbackDict

from engine.log import get_logger

log = get_logger(__name__)

#: Cookie value length in bytes before base64 — 32 bytes = 256 bits.
_SID_BYTES = 32

#: How long a session row stays valid without being touched. Also the
#: cookie's Max-Age. Overridable via ``SESSION_DB_LIFETIME_HOURS``.
DEFAULT_LIFETIME_HOURS = 24 * 14

#: Log a warning past this payload size, in bytes.
#:
#: This matters more than it looks. The Flask session currently holds a
#: project's whole working pack — sixty test cases with steps and expected
#: results is comfortably a few hundred kilobytes — and the target free
#: Postgres has a 0.5 GB hard cap for *everything*. A few hundred
#: sessions of that size is a meaningful fraction of the whole database.
#:
#: Not an error, because refusing the write would lose a user's work to
#: protect disk. A warning, so the pressure is visible in the log before
#: it is visible as a full database — and so the E3 workspace refactor,
#: which moves packs out of the session and shrinks these rows to
#: kilobytes, has a number to show for itself.
PAYLOAD_WARN_BYTES = 256 * 1024

#: Probability of sweeping expired rows on any given save. At ~1 in 500
#: writes the vacuum costs nothing measurable but still runs many times a
#: day under real traffic — and needs no background thread, which matters
#: because a sleeping free-tier dyno does not run one anyway.
_VACUUM_ODDS = 500


def _new_sid() -> str:
    return secrets.token_urlsafe(_SID_BYTES)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DbSession(CallbackDict, SessionMixin):
    """A dict that remembers whether it changed, plus its sid."""

    def __init__(self, initial: dict | None = None, sid: str | None = None,
                 new: bool = False):
        def _on_update(_self):
            _self.modified = True

        super().__init__(initial or {}, _on_update)
        self.sid = sid
        self.new = new
        self.modified = False


class DbSessionInterface(SessionInterface):
    """Flask session interface backed by ``engine.db.ServerSession``."""

    session_class = DbSession

    def __init__(self, lifetime: timedelta | None = None):
        self.lifetime = lifetime or timedelta(hours=DEFAULT_LIFETIME_HOURS)

    # ── read ──────────────────────────────────────────────────────

    def open_session(self, app, request) -> DbSession:
        cookie_name = self.get_cookie_name(app)
        sid = request.cookies.get(cookie_name)
        if not sid:
            return self.session_class(sid=_new_sid(), new=True)

        # A cookie longer than any sid we mint is either corruption or
        # someone probing; refuse it before it reaches a query.
        if len(sid) > 128:
            log.warning("session cookie rejected: implausible length %d",
                        len(sid))
            return self.session_class(sid=_new_sid(), new=True)

        from engine import db as _db
        try:
            payload = _db.session_load(sid, lifetime=self.lifetime)
        except Exception as exc:
            # A database blip must not turn every page into a 500. Degrade
            # to an empty session — the user sees a logged-out app, which
            # is honest, rather than a stack trace.
            log.warning("session load failed for sid=%s…: %s", sid[:8], exc)
            return self.session_class(sid=_new_sid(), new=True)

        if payload is None:
            # Unknown or expired sid. Keep the *same* sid rather than
            # minting a new one: a forged cookie then stays a dead end
            # instead of handing the caller a fresh writable session on
            # every request, and an expired-but-returning user keeps one
            # row instead of accumulating one per visit.
            return self.session_class(sid=sid, new=True)

        try:
            data = json.loads(payload) if payload else {}
            if not isinstance(data, dict):
                raise ValueError("session payload is not an object")
        except (ValueError, TypeError) as exc:
            log.warning("session payload unreadable for sid=%s…: %s",
                        sid[:8], exc)
            return self.session_class(sid=sid, new=True)

        return self.session_class(data, sid=sid, new=False)

    # ── write ─────────────────────────────────────────────────────

    def save_session(self, app, session, response) -> None:
        cookie_name = self.get_cookie_name(app)
        domain = self.get_cookie_domain(app)
        path = self.get_cookie_path(app)

        from engine import db as _db

        # Emptied session → drop the row and the cookie. Covers sign-out
        # and any flow that clears everything.
        if not session:
            if session.modified and not session.new:
                try:
                    _db.session_delete(session.sid)
                except Exception as exc:
                    log.warning("session delete failed: %s", exc)
                response.delete_cookie(cookie_name, domain=domain, path=path)
            return

        if not (session.modified or session.new):
            # Nothing changed: no write, no Set-Cookie. Sliding expiry is
            # handled on the read side instead — ``engine.db.session_load``
            # pushes ``expires_at`` forward when a live session is used
            # close to its deadline. Doing it there means one place decides
            # when a session is still alive, and a page that loads twenty
            # static assets does not become twenty UPDATEs.
            return

        expires_at = _utcnow() + self.lifetime
        payload = self._serialise(session)
        if payload is None:
            return

        try:
            _db.session_save(session.sid, payload, expires_at,
                             user_id=session.get("_user_id"))
        except Exception as exc:
            # Losing a session write is bad but not fatal; raising here
            # would turn a successful POST into a 500 after the work was
            # already committed.
            log.warning("session save failed for sid=%s…: %s",
                        session.sid[:8], exc)
            return

        self._maybe_vacuum()

        response.set_cookie(
            cookie_name, session.sid,
            expires=expires_at,
            httponly=self.get_cookie_httponly(app),
            domain=domain, path=path,
            secure=self.get_cookie_secure(app),
            samesite=self.get_cookie_samesite(app),
        )

    # ── internals ─────────────────────────────────────────────────

    def _serialise(self, session) -> str | None:
        """JSON, not pickle.

        The filesystem backend pickled, which meant a session row was a
        deserialisation gadget — and these rows will soon hold the
        authenticated user id. JSON cannot execute anything. The cost is
        that only JSON-native types survive a round trip; everything the
        app currently stores in the session is already dict / list / str /
        int / bool, because it all came from ``*_to_dict`` helpers on the
        way in.
        """
        try:
            payload = json.dumps(dict(session), ensure_ascii=False,
                                 default=self._json_default)
            size = len(payload.encode("utf-8"))
            if size > PAYLOAD_WARN_BYTES:
                # Name the biggest keys — otherwise the warning tells you
                # there is a problem without telling you where it is.
                biggest = sorted(
                    ((k, len(json.dumps(v, ensure_ascii=False,
                                        default=self._json_default)))
                     for k, v in session.items()),
                    key=lambda kv: -kv[1])[:3]
                log.warning(
                    "session payload is %d KB (over the %d KB soft limit) "
                    "for sid=%s… — largest keys: %s",
                    size // 1024, PAYLOAD_WARN_BYTES // 1024,
                    (session.sid or "")[:8],
                    ", ".join(f"{k}={n // 1024}KB" for k, n in biggest))
            return payload
        except (TypeError, ValueError) as exc:
            log.error("session payload not JSON-serialisable — dropping "
                      "write: %s", exc)
            return None

    @staticmethod
    def _json_default(value: Any) -> Any:
        # datetimes turn up in a few session payloads; ISO-format them
        # rather than failing the whole write.
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError(f"{type(value).__name__} is not JSON-serialisable")

    def _maybe_vacuum(self) -> None:
        """Occasionally delete expired session rows.

        Needed here rather than left to a cron: nothing in this codebase
        currently calls any of the ``purge_expired_*`` helpers, so expired
        rows have simply been accumulating. On a free-tier Postgres with a
        hard size cap that is not a tidiness issue — it is the database
        filling up.
        """
        if secrets.randbelow(_VACUUM_ODDS) != 0:
            return
        from engine import db as _db
        try:
            n = _db.purge_expired_sessions()
            if n:
                log.info("session vacuum removed %d expired row(s)", n)
        except Exception as exc:  # pragma: no cover — opportunistic
            log.debug("session vacuum skipped: %s", exc)


def install(app) -> bool:
    """Attach the DB-backed session interface when configured to.

    Returns True when it took over, False when the app keeps whatever
    Flask-Session set up. Controlled by ``SESSION_BACKEND``:

    * ``db`` — use this interface.
    * anything else (default) — leave the filesystem backend alone.

    Kept as an explicit opt-in rather than "on whenever DATABASE_URL is
    Postgres" so the rollout is reversible from the Render dashboard
    without a redeploy, and so the test suite's own filesystem sessions
    are unaffected unless a test asks for this.
    """
    import os
    if (os.environ.get("SESSION_BACKEND", "") or "").strip().lower() != "db":
        return False
    hours = os.environ.get("SESSION_DB_LIFETIME_HOURS", "").strip()
    try:
        lifetime = timedelta(hours=int(hours)) if hours else None
    except ValueError:
        log.warning("SESSION_DB_LIFETIME_HOURS=%r is not an integer — "
                    "using the default.", hours)
        lifetime = None
    app.session_interface = DbSessionInterface(lifetime=lifetime)
    log.info("Server-side sessions: Postgres (SESSION_BACKEND=db).")
    return True


__all__ = ["DbSession", "DbSessionInterface", "install",
           "DEFAULT_LIFETIME_HOURS"]
