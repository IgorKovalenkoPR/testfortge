"""TestFortge — password authentication (E1.2).

Argon2id via ``argon2-cffi``. The parameters are the library's defaults,
which track the RFC 9106 recommendations; they are not tuned here because
a hand-picked cost parameter ages badly and this module is not where
someone should be guessing at memory budgets.

What this module is careful about, and why each one matters
----------------------------------------------------------
**Account enumeration.** ``/login`` and ``/reset`` must not reveal whether
an address has an account. Two leaks, not one: the *message* (handled by
the routes, which say the same thing either way) and the *timing* — a
missing user returns in microseconds while a real one spends ~50 ms in
Argon2, which is trivially measurable over a network. :func:`verify_login`
therefore hashes a dummy password when the user does not exist, so both
paths cost the same.

**Brute force.** A counter in memory is not a lockout on a dyno that
sleeps every fifteen minutes, so ``failed_logins`` and ``locked_until``
live on the user row.

**Timing-safe comparison** is Argon2's job, not ours — ``verify()`` is
constant-time with respect to the hash.

**Rehashing.** ``argon2-cffi`` can tell us when a stored hash used weaker
parameters than the current policy. We upgrade it on the next successful
login, so raising the cost later does not leave old accounts behind.

What is deliberately *not* here: password composition rules. Requiring a
digit and a symbol measurably pushes people toward ``Password1!`` and is
no longer recommended by NIST. Length is the requirement.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from engine.log import get_logger

log = get_logger(__name__)

#: Minimum password length. 12 rather than 8: with no composition rules,
#: length is the only thing carrying the entropy.
MIN_PASSWORD_LEN = 12

#: Maximum. Argon2 hashes whatever it is given, so an unbounded field is a
#: cheap way to make the server do megabytes of work per request.
MAX_PASSWORD_LEN = 128

#: Failed attempts before the account is temporarily locked.
MAX_FAILED_LOGINS = 5

#: How long the lock lasts. Long enough to make online guessing pointless,
#: short enough that a locked-out colleague is not blocked for the day.
LOCKOUT_MINUTES = 15

#: A syntactically valid hash of a value nobody can log in with, used to
#: burn the same CPU as a real verification when the account is absent.
#: Computed once, lazily.
_DUMMY_HASH: str | None = None


class AuthError(RuntimeError):
    """Base for authentication failures that a route should surface."""


class PasswordPolicyError(AuthError):
    """The proposed password does not meet policy. Message is user-facing."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hasher():
    from argon2 import PasswordHasher
    return PasswordHasher()


# ── Hashing ───────────────────────────────────────────────────────

def validate_password(password: str, *, email: str | None = None) -> None:
    """Raise :class:`PasswordPolicyError` if *password* is unacceptable.

    Messages are written to be shown to the person typing, so they say
    what to do rather than what rule was violated.
    """
    pwd = password or ""
    if len(pwd) < MIN_PASSWORD_LEN:
        raise PasswordPolicyError(
            f"Use at least {MIN_PASSWORD_LEN} characters. A short phrase of "
            f"a few words works well and is easier to remember than a "
            f"scrambled word."
        )
    if len(pwd) > MAX_PASSWORD_LEN:
        raise PasswordPolicyError(
            f"That is longer than {MAX_PASSWORD_LEN} characters."
        )
    if pwd.strip() == "":
        raise PasswordPolicyError("The password cannot be only whitespace.")
    if email and pwd.strip().lower() == (email or "").strip().lower():
        raise PasswordPolicyError(
            "The password cannot be your email address."
        )


def hash_password(password: str, *, email: str | None = None) -> str:
    """Validate then hash. Raises :class:`PasswordPolicyError` on policy."""
    validate_password(password, email=email)
    return _hasher().hash(password)


def _dummy_hash() -> str:
    """A real Argon2 hash to verify against when no user exists."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = _hasher().hash("timing-equaliser-not-a-real-password")
    return _DUMMY_HASH


def _burn_equivalent_time() -> None:
    """Spend the same CPU a real verification would.

    Without this, "no such account" answers in microseconds and "wrong
    password" takes ~50 ms — enough of a difference to enumerate every
    address in a customer's domain from the outside.
    """
    try:
        from argon2.exceptions import VerificationError
        try:
            _hasher().verify(_dummy_hash(), "wrong")
        except VerificationError:
            pass
    except Exception:  # pragma: no cover — never let this raise
        pass


# ── Login ─────────────────────────────────────────────────────────

class LoginResult:
    """Outcome of a login attempt.

    ``user`` is populated only on success. ``reason`` is for the log and
    for tests — a route must **not** show it to the caller, because
    "no such account" and "wrong password" are different reasons and
    telling them apart is the enumeration leak.
    """

    __slots__ = ("ok", "user", "reason", "locked_until")

    def __init__(self, ok: bool, user: dict | None = None,
                 reason: str = "", locked_until: datetime | None = None):
        self.ok = ok
        self.user = user
        self.reason = reason
        self.locked_until = locked_until

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"LoginResult(ok={self.ok}, reason={self.reason!r})"


def _lock_remaining(locked_until) -> timedelta | None:
    if locked_until is None:
        return None
    when = locked_until
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when)
        except ValueError:
            return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    remaining = when - _utcnow()
    return remaining if remaining.total_seconds() > 0 else None


def verify_login(email: str, password: str) -> LoginResult:
    """Check a password, applying and updating the lockout counters.

    Never raises for a bad credential — the caller branches on
    ``result.ok``. Every failure path costs roughly the same wall-clock
    time as a success.
    """
    from engine import db as _db

    email = _db.normalize_email(email)
    row = _db.get_user_by_email(email) if email else None

    if row is None:
        _burn_equivalent_time()
        return LoginResult(False, reason="no_such_user")

    if not row.get("is_active", True):
        _burn_equivalent_time()
        return LoginResult(False, reason="inactive")

    remaining = _lock_remaining(row.get("locked_until"))
    if remaining is not None:
        # Do not spend Argon2 time on a locked account — that is the one
        # place where a fast answer leaks nothing, because the caller
        # already knows the account exists (they just got locked out of
        # it) and refusing cheaply is what makes the lock a defence.
        return LoginResult(False, reason="locked",
                           locked_until=row.get("locked_until"))

    stored = row.get("password_hash")
    if not stored:
        # Google-only account. Same opaque failure as a wrong password:
        # revealing "this address exists but signs in with Google" tells
        # an attacker which provider to phish.
        _burn_equivalent_time()
        return LoginResult(False, reason="no_password")

    from argon2.exceptions import (InvalidHashError, VerifyMismatchError,
                                   VerificationError)
    hasher = _hasher()
    try:
        hasher.verify(stored, password or "")
    except (VerifyMismatchError, VerificationError):
        _register_failure(row["id"])
        return LoginResult(False, reason="bad_password")
    except InvalidHashError:
        # A corrupt or foreign hash. Not the user's fault and not
        # something they can fix, so fail closed and make it visible.
        log.error("user %s has an unreadable password hash", row["id"][:8])
        return LoginResult(False, reason="bad_hash")

    # Success. Upgrade the hash if the cost parameters have moved on.
    try:
        if hasher.check_needs_rehash(stored):
            _db.set_password_hash(row["id"], hasher.hash(password))
            log.info("rehashed password for user %s at current parameters",
                     row["id"][:8])
    except Exception as exc:  # pragma: no cover — never fail a good login
        log.warning("rehash skipped for %s: %s", row["id"][:8], exc)

    _db.clear_login_failures(row["id"])
    return LoginResult(True, user=_db.get_user(row["id"]))


def _register_failure(user_id: str) -> None:
    """Count a failure and lock the account once the threshold is hit."""
    from engine import db as _db
    try:
        count = _db.bump_login_failure(user_id)
        if count >= MAX_FAILED_LOGINS:
            until = _utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
            _db.lock_user(user_id, until)
            log.warning("user %s locked until %s after %d failed attempts",
                        user_id[:8], until.isoformat(), count)
    except Exception as exc:  # pragma: no cover — best-effort
        log.warning("failed-login bookkeeping failed for %s: %s",
                    user_id[:8], exc)


def lockout_message(locked_until) -> str:
    """A user-facing sentence for a locked account.

    Safe to show: the person triggered the lock themselves, so it reveals
    nothing they did not already know.
    """
    remaining = _lock_remaining(locked_until)
    if remaining is None:
        return "Too many attempts. Try again shortly."
    minutes = max(1, int(remaining.total_seconds() // 60) + 1)
    return (f"Too many failed attempts. Try again in about "
            f"{minutes} minute{'s' if minutes != 1 else ''}.")


#: The single message both "no such account" and "wrong password" produce.
#: One constant so the two paths cannot drift apart in a later edit — the
#: drift is the enumeration bug, and it is the kind that gets reintroduced
#: by someone improving an error message in good faith.
GENERIC_LOGIN_FAILURE = "That email and password do not match an account."


__all__ = [
    "MIN_PASSWORD_LEN", "MAX_PASSWORD_LEN", "MAX_FAILED_LOGINS",
    "LOCKOUT_MINUTES", "GENERIC_LOGIN_FAILURE",
    "AuthError", "PasswordPolicyError", "LoginResult",
    "validate_password", "hash_password", "verify_login",
    "lockout_message",
]
