"""TestForTge — the first administrator, and why the invite flow needs one.

Measured on the live deployment 2026-08-10, while checking how the owner
would sign in after enabling authentication:

* an account is created in exactly two places, and both consume an
  invitation — ``/auth/accept/<token>`` and the Google callback (which
  ``engine.oauth.decide`` refuses without one);
* an invitation is issued by an **admin**, at ``/org/invite``;
* ``db.create_organization`` is called from no route at all;
* and the Basic-gate interlock (``engine.basic_auth``) deliberately keeps
  the gate up while ``AUTH_ENABLED=0``, so "just turn the gate off" is not
  a way in either.

Together: **turning authentication on for a fresh database locks everybody
out, permanently.** The whole invitation machinery has no origin. It is the
same shape this programme kept finding — a mechanism whose first caller was
never written — and it is invisible from the code, because every individual
piece is correct.

This module is that first caller.

What it does, and what it refuses to do
---------------------------------------
Given ``BOOTSTRAP_ADMIN_EMAIL`` and ``BOOTSTRAP_ADMIN_PASSWORD``, and a
database with **no users at all**, it creates one verified account, one
organisation, and makes the account its admin. Then it writes an audit
record, because minting an administrator is the most privileged thing this
codebase can do without a human in the loop.

It refuses in every other case, and each refusal is a log line rather than
an exception: this runs at boot, and a misconfigured variable must not be
the reason the service will not start.

Why "no users" and not "first boot ever"
----------------------------------------
Because the deployment this runs on loses its database roughly monthly —
Render's free Postgres expires and is recreated (see
docs/runbooks/database-on-a-free-plan.md). An instance that re-locks itself
after every reset is not a usable instance, so the condition is the state of
the database, not a one-shot marker. The cost of that choice is stated
plainly in the runbook: while the variables are set, an empty database will
always acquire this administrator. Whoever can read those variables already
has the Render dashboard, so it grants no access that did not exist.
"""
from __future__ import annotations

import os

from engine import auth as _auth
from engine import db as _db
from engine.log import get_logger

log = get_logger(__name__)

EMAIL_ENV = "BOOTSTRAP_ADMIN_EMAIL"
PASSWORD_ENV = "BOOTSTRAP_ADMIN_PASSWORD"
ORG_ENV = "BOOTSTRAP_ORG_NAME"

DEFAULT_ORG_NAME = "My team"


def _clean(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def claim_first_admin() -> str:
    """Create the first administrator if there is none. Returns its id.

    Returns ``""`` for every non-action — not configured, already has
    users, password refused — so a caller can log the outcome without
    having to distinguish failures it cannot fix anyway.
    """
    email = _clean(EMAIL_ENV)
    password = _clean(PASSWORD_ENV)

    if not email and not password:
        return ""                      # not configured: the normal case

    # Half-configured is worth a line of its own. Silence here is how an
    # operator spends an afternoon wondering why the account never appeared.
    if not email or not password:
        log.warning(
            "%s/%s: only one of the two is set, so no administrator was "
            "created. Both are needed.", EMAIL_ENV, PASSWORD_ENV)
        return ""

    try:
        existing = _db.count_users()
    except Exception:                   # pragma: no cover — DB outage at boot
        log.exception("could not count users, so the first-admin bootstrap "
                      "did nothing. The service is starting anyway.")
        return ""

    if existing:
        # Not an error: this is what every boot after the first one looks
        # like. Logged at info so the variable's presence is explainable.
        log.info("first-admin bootstrap: %d account(s) already exist, "
                 "doing nothing.", existing)
        return ""

    # Hash first. A weak password must not produce an admin — and the
    # product's own rule is the one that applies, not a looser one for
    # operators.
    try:
        password_hash = _auth.hash_password(password, email=email)
    except Exception as exc:
        log.error("first-admin bootstrap refused the password in %s: %s "
                  "Fix the variable and redeploy; no account was created.",
                  PASSWORD_ENV, exc)
        return ""

    user_id = _db.create_user(
        email,
        display_name=email.split("@", 1)[0] or None,
        password_hash=password_hash,
        # There is nobody to send a confirmation link to yet, and the
        # address came from the person who owns the deployment.
        email_verified=True,
    )
    if not user_id:
        # ``create_user`` returns None when the address is taken, which can
        # only mean another worker won the race a moment ago.
        log.info("first-admin bootstrap: the address is already registered, "
                 "so another process created it first.")
        return ""

    org_id = _db.create_organization(_clean(ORG_ENV) or DEFAULT_ORG_NAME)
    if org_id:
        _db.add_org_member(org_id, user_id, "admin")
    else:
        # The account still works with ORG_MODE off, and saying so is more
        # useful than a bare failure.
        log.error("first-admin bootstrap created the account but no "
                  "organisation; with ORG_MODE on this user will see no "
                  "team.")

    _db.append_audit(entity="user", action="bootstrap_admin",
                     user_id=user_id, org_id=org_id or None,
                     entity_id=user_id,
                     diff={"email": email, "org": org_id or ""})

    log.warning(
        "first-admin bootstrap created %s as an admin of %r. This runs only "
        "while the database has no users; it will do nothing on the next "
        "boot. Remove %s once you can sign in.",
        email, _clean(ORG_ENV) or DEFAULT_ORG_NAME, PASSWORD_ENV)
    return user_id


__all__ = ["claim_first_admin", "EMAIL_ENV", "PASSWORD_ENV", "ORG_ENV"]
