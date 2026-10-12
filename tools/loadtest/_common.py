"""Names and guards shared by ``seed_loadtest`` and ``cleanup_loadtest``.

Everything that decides *which rows belong to the load test* lives here, in
one place, so the seed and the cleanup can never disagree about it — and so
the unit tests exercise the very predicate the cleanup trusts.

The rule is deliberately narrow: an organisation is ours only when its
**name is exactly** one of :data:`ORG_NAMES`; a user is ours only when their
email matches :data:`EMAIL_RE` **and** they belong to no organisation
outside :data:`ORG_NAMES`. A project is ours only when its ``org_id`` is one
of our organisations' ids. Nothing is ever selected by a ``LIKE`` on a name.
"""
from __future__ import annotations

import os
import re

#: The primary tenant every load-test account lives in.
ORG_NAME = "LOADTEST"
#: A second tenant, used only by the smoke test's tenancy check.
ORG_NAME_B = "LOADTEST-B"
#: The only organisation names the seed creates and the cleanup touches.
ORG_NAMES: tuple[str, ...] = (ORG_NAME, ORG_NAME_B)

#: The project the load test browses, searches and edits (2,000 TCs ...).
BIG_PROJECT = "LOADTEST big"
#: Generation writes a whole pack (wipe-and-replace), so it gets a project
#: of its own — running it in BIG_PROJECT would delete the seeded 2,000 TCs.
GEN_PROJECT = "LOADTEST gen scratch"
#: The second tenant's project (tenancy check).
B_PROJECT = "LOADTEST-B tenancy"

#: Tokens that occur in exactly one tenant — the tenancy check searches for
#: the other tenant's token and must get nothing back.
TENANT_TOKEN_A = "zephyrquartz"
TENANT_TOKEN_B = "obsidianlark"

#: Email domain for seeded accounts. ``.invalid`` is reserved (RFC 2606):
#: no mail can ever be delivered to it, so an invite/reset email the app
#: might send for these accounts goes nowhere.
EMAIL_DOMAIN = "loadtest.testforge.invalid"
EMAIL_RE = re.compile(r"^lt-(?:admin|user-\d{2,3}|b-user)@"
                      + re.escape(EMAIL_DOMAIN) + r"$")


def admin_email() -> str:
    return f"lt-admin@{EMAIL_DOMAIN}"


def user_email(i: int) -> str:
    return f"lt-user-{i:02d}@{EMAIL_DOMAIN}"


def b_user_email() -> str:
    return f"lt-b-user@{EMAIL_DOMAIN}"


class RefusedError(RuntimeError):
    """A guard refused to act. The message says why, for an operator."""


def check_org_name(name: str) -> str:
    """Return *name* when it is one of ours, else raise :class:`RefusedError`.

    Exact match only: ``loadtest``, ``LOADTEST `` or ``LOADTEST-prod`` are
    all refused, because a near miss is precisely the case a guard exists
    for.
    """
    if not isinstance(name, str) or name not in ORG_NAMES:
        raise RefusedError(
            f"refusing to act on organisation {name!r}: only "
            f"{', '.join(ORG_NAMES)} (exact names) are load-test tenants")
    return name


def is_loadtest_email(email: str | None) -> bool:
    return bool(email) and bool(EMAIL_RE.match(str(email).strip().lower()))


def password_from_env(var: str = "LOADTEST_PASSWORD") -> str:
    """The shared test-account password. Never defaulted, never printed."""
    pwd = os.environ.get(var) or ""
    if not pwd:
        raise RefusedError(
            f"{var} is not set. Export a password of 12+ characters for the "
            f"seeded test accounts (it is hashed, never printed).")
    return pwd
