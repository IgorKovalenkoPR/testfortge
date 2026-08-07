"""E1.9 — the authentication security set, and the map of who covers what.

E1.9 names six vectors. Four of them already had named negative tests
elsewhere, and this file exists for the two that did not have adequate
ones — plus a registry that makes the whole claim machine-checked instead
of a sentence in a plan document.

The map, asserted below in ``TestTheSixVectorsAreAllCovered``:

===================  ==========================================
vector               named negative test
===================  ==========================================
CSRF                 test_csrf_on_every_post.py — generated from
                     the URL map, so a new POST cannot be missed
session fixation     test_auth_password.py::TestSessionFixation
open redirect        test_auth_password.py::TestLoginRoute::
                     test_next_cannot_redirect_off_site
brute force          test_auth_password.py::TestLockout
timing               here
account enumeration  here
===================  ==========================================

Why timing and enumeration got a file of their own
--------------------------------------------------
Tests for both existed and neither did what its name said.

``test_one_message_covers_both_failure_modes`` inspected the *constant* —
that it is a string containing the word "match" — and never exercised
either branch that is supposed to produce it. A route that returned
different sentences would have passed.

``test_an_unknown_address_returns_the_identical_body`` asserted that the
generic message appears in the unknown-address response. It never compared
the two bodies, so anything *else* that differed — an extra hint, a
different heading — was invisible to the test that promised identity.

``test_an_unknown_address_costs_the_same_time_as_a_wrong_password`` is a
wall-clock measurement with a five-fold tolerance. It stays where it is as
a coarse cross-check, but a timeout-shaped test cannot be the gate on a
zero-flaky-tests policy, so the gate here measures **work rather than
time**: every failing path must spend one Argon2 verification, which is a
fact about what the code did, not about how loaded the machine was.
"""
from __future__ import annotations

import ast
import pathlib
import re
import secrets

import pytest

from engine import auth as _auth
from engine import db as _db

GOOD_PASSWORD = "correct horse battery staple"

TESTS_DIR = pathlib.Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def _db_ready():
    _db.init_db()


@pytest.fixture(autouse=True)
def _auth_on(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")


def _email() -> str:
    return f"vec-{secrets.token_hex(6)}@example.com"


def _user(*, active: bool = True, **kw) -> tuple[str, str]:
    email = _email()
    uid = _db.create_user(
        email, password_hash=_auth.hash_password(GOOD_PASSWORD), **kw)
    if not active:
        # A separate call because ``create_user`` has no ``is_active``
        # parameter — deactivation is an administrative act on an existing
        # account, not a way to create one.
        _db.set_user_active(uid, False)
    return uid, email


# ── Timing: measured as work, not as seconds ─────────────────────────

class _CountingHasher:
    """A ``PasswordHasher`` that records how often it verified.

    The point of the timing defence is that a missing account costs the
    same Argon2 verification a real one does. That is a fact about control
    flow, so counting the verifications tests it exactly, and a busy CI box
    cannot change the answer.
    """

    def __init__(self, real):
        self._real = real
        self.verifications = 0

    def verify(self, stored, candidate):
        self.verifications += 1
        return self._real.verify(stored, candidate)

    def hash(self, password):
        return self._real.hash(password)

    def check_needs_rehash(self, stored):
        return self._real.check_needs_rehash(stored)


@pytest.fixture
def hasher(monkeypatch):
    """Count Argon2 verifications for the duration of one test.

    The dummy hash is warmed first: it is computed lazily and cached in a
    module global, so the very first call in a process would otherwise
    charge one extra ``hash`` to whichever test ran first.
    """
    from argon2 import PasswordHasher

    _auth._dummy_hash()
    counter = _CountingHasher(PasswordHasher())
    monkeypatch.setattr(_auth, "_hasher", lambda: counter)
    return counter


class TestTimingDoesNotRevealWhetherAnAccountExists:
    """Every rejection must cost one verification.

    Without the equaliser, "no such account" returns in microseconds while
    a real account spends ~50 ms in Argon2 — a difference big enough to
    enumerate an entire company's addresses over the network, without a
    single successful login.
    """

    def test_an_unknown_address_still_verifies_a_hash(self, hasher):
        result = _auth.verify_login(_email(), "wrong password here")
        assert result.ok is False
        assert result.reason == "no_such_user"
        assert hasher.verifications == 1, (
            "the unknown-address path returned without doing the work a "
            "real verification does, so its response time identifies "
            "which addresses have accounts")

    def test_a_wrong_password_verifies_a_hash(self, hasher):
        _, email = _user()
        result = _auth.verify_login(email, "wrong password here")
        assert result.reason == "bad_password"
        assert hasher.verifications == 1

    def test_a_deactivated_account_verifies_a_hash(self, hasher):
        _, email = _user(active=False)
        result = _auth.verify_login(email, GOOD_PASSWORD)
        assert result.reason == "inactive"
        assert hasher.verifications == 1, (
            "a deactivated account answered faster than a wrong password, "
            "so the pair of responses says 'this address exists but is "
            "switched off'")

    def test_a_google_only_account_verifies_a_hash(self, hasher):
        email = _email()
        _db.create_user(email, email_verified=True)   # no password_hash
        result = _auth.verify_login(email, "any password at all")
        assert result.reason == "no_password"
        assert hasher.verifications == 1, (
            "an account with no password answered instantly, which tells "
            "an attacker the address exists AND which provider to phish")

    def test_every_rejection_costs_the_same_single_verification(self, hasher):
        """The four paths side by side, which is the property that matters.

        Each assertion above holds on its own; a per-path drift would still
        be a leak, so the comparison is made explicitly.
        """
        _, real = _user()
        _, dead = _user(active=False)
        google_only = _email()
        _db.create_user(google_only, email_verified=True)

        counts = {}
        for label, email, password in (
            ("unknown", _email(), "wrong password here"),
            ("bad_password", real, "wrong password here"),
            ("inactive", dead, GOOD_PASSWORD),
            ("no_password", google_only, "wrong password here"),
        ):
            before = hasher.verifications
            _auth.verify_login(email, password)
            counts[label] = hasher.verifications - before

        assert set(counts.values()) == {1}, counts

    def test_a_locked_account_is_the_one_deliberate_exception(self, hasher):
        """A locked account is refused without spending Argon2 time.

        That is intentional and documented in ``engine/auth.py``: refusing
        cheaply is what makes the lock a defence rather than a way to make
        the server do work.

        **The residual leak, recorded here rather than left implicit.** The
        lockout *message* is deliberately specific, on the reasoning that
        the person who tripped it already knows the account exists. An
        attacker who submits five wrong passwords to a candidate address
        gets that same specific message — for a real account — and the
        generic one for an address with no account. So lockout is an
        enumeration oracle costing five requests per address.

        It is left as it is because both fixes are worse here: showing the
        generic message throws away the one piece of genuinely useful
        feedback on this page, and counting failures against arbitrary
        submitted addresses means writing attacker-supplied strings into a
        0.5 GB database. Recorded so the next person reads a decision
        rather than an oversight — it is the operator's call to revisit.
        """
        uid, email = _user()
        for _ in range(_auth.MAX_FAILED_LOGINS):
            _auth.verify_login(email, "wrong password here")

        before = hasher.verifications
        result = _auth.verify_login(email, "wrong password here")

        assert result.reason == "locked"
        assert hasher.verifications == before, (
            "a locked account is now paying for a verification it refuses "
            "to use — that is work an attacker can ask for for free")
        assert _auth.lockout_message(result.locked_until) != \
            _auth.GENERIC_LOGIN_FAILURE, (
            "if this is now generic, the oracle in this docstring is "
            "closed and the docstring should say so")


# ── Enumeration: the message, the status, and everything else ────────

#: What a frozen CSRF token looks like on the page.
FROZEN_CSRF = "FROZEN-FOR-COMPARISON"


@pytest.fixture
def quiet_page(monkeypatch):
    """Stop the CSRF token from changing between two renders of a page.

    ``base.html`` carries both ``<meta name="csrf-token" content="{{ csrf_token() }}">``
    and a hidden input, and the token is **signed with a timestamp** — so
    two renders that land in different seconds differ, in a part of the
    page with nothing to do with authentication failure.

    Two rounds of measurement went into these six lines, and both are worth
    recording because each looked finished.

    *First*, the volatile parts were regex-normalised. That stripped the
    form input and missed the meta tag: 6 differing runs out of 40.

    *Second*, the token was frozen at ``jinja_env.globals``, which is where
    Flask-WTF installs it — and it had no effect at all, because
    ``csrf_token`` reaches a template here through **three** registrations
    and a context processor beats a Jinja global. Two more differed out of
    60. So all three are frozen, and
    ``TestTheComparisonIsTrustworthy`` asserts the freeze took rather than
    trusting that it did.

    Not by patching ``secrets``, which would also freeze the session ids
    that ``permissions.login_user`` rotates against fixation.
    """
    import app as _app_module
    from flask_wtf import csrf as _wtf_csrf
    from app import app as flask_app

    frozen = lambda *a, **k: FROZEN_CSRF          # noqa: E731
    # All three resolve at call time, so patching the module attribute is
    # what reaches the already-registered processors.
    monkeypatch.setattr(_wtf_csrf, "generate_csrf", frozen)
    monkeypatch.setattr(_app_module, "generate_csrf", frozen)
    monkeypatch.setitem(flask_app.jinja_env.globals, "csrf_token", frozen)
    return True


#: The CSP nonce, which is minted per request on purpose.
#:
#: Regex rather than frozen because every occurrence has this one shape, so
#: the substitution is total — unlike the CSRF token, which reaches the page
#: through two different attributes.
_NONCE = re.compile(r'nonce="[^"]*"')


def _page(response, email: str) -> str:
    """A comparable rendering of a login response.

    Two things are removed and both are legitimately per-response:

    * the CSP nonce;
    * the echoed email — the caller typed it, so showing it back reveals
      nothing they did not already know.

    Everything else must match. Comparing whole pages rather than checking
    for a substring is the difference between this and the test it
    replaces: a stray extra hint on one of the two pages is exactly the
    leak worth catching, and a substring check cannot see it.
    """
    body = _NONCE.sub('nonce="X"', response.get_data(as_text=True))
    return body.replace(email, "ADDRESS-TYPED")


class TestTheComparisonIsTrustworthy:
    """The check whose absence let a flake through.

    Comparing two whole pages only means something if two renderings of the
    *same* page already compare equal. Without that, a difference in the
    volatile parts reads as a leak (a flake, which is what happened) and an
    over-eager normalisation reads as safety (a test that can no longer
    fail, which is worse).
    """

    def test_two_renderings_of_one_failure_are_identical(self, anon_client,
                                                         quiet_page):
        _, email = _user()
        first = anon_client.post("/auth/login",
                                 data={"email": email,
                                       "password": "wrong password here"})
        second = anon_client.post("/auth/login",
                                  data={"email": email,
                                        "password": "wrong password here"})

        assert _page(first, email) == _page(second, email), (
            "the same request rendered two different pages, so every "
            "comparison below can fail for a reason that is not a leak")

    def test_the_freeze_actually_reached_the_page(self, anon_client,
                                                  quiet_page):
        """Deterministic where the test above is statistical.

        Freezing the token at ``jinja_env.globals`` alone changed nothing —
        a context processor wins — and the only symptom was two differing
        runs in sixty. So the sentinel is looked for directly: if a future
        edit moves where ``csrf_token`` comes from, this fails on every run
        instead of one run in thirty.
        """
        _, email = _user()
        body = anon_client.post(
            "/auth/login",
            data={"email": email, "password": "wrong password here"}
        ).get_data(as_text=True)

        assert body.count(FROZEN_CSRF) >= 2, (
            f"the CSRF token is still live in {2 - body.count(FROZEN_CSRF)} "
            f"of its places on the page, so comparisons here will fail "
            f"whenever two renders land in different seconds")

    def test_the_page_still_contains_the_parts_that_matter(self, anon_client,
                                                           quiet_page):
        """Guards the opposite failure: a treatment so aggressive that the
        pages match because there is nothing left of them."""
        _, email = _user()
        body = _page(anon_client.post(
            "/auth/login",
            data={"email": email, "password": "wrong password here"}), email)

        assert _auth.GENERIC_LOGIN_FAILURE in body
        assert "ADDRESS-TYPED" in body, "the echoed address vanished entirely"
        assert len(body) > 2000, f"only {len(body)} characters survived"

    def test_a_deliberate_difference_is_still_detected(self, anon_client,
                                                       quiet_page):
        """The comparison's own teeth, checked directly rather than assumed:
        two pages that differ only in the typed address must not match once
        the addresses are no longer substituted away."""
        _, email = _user()
        ghost = _email()
        known = anon_client.post("/auth/login",
                                 data={"email": email,
                                       "password": "wrong password here"})
        unknown = anon_client.post("/auth/login",
                                   data={"email": ghost,
                                         "password": "wrong password here"})

        assert _page(known, "no-such-substring") != \
            _page(unknown, "no-such-substring")


class TestTheTwoFailuresAreIndistinguishable:
    def test_verify_login_reports_both_as_a_plain_failure(self):
        """The reasons differ internally — they have to, for the log — and
        neither is allowed to reach the caller."""
        _, email = _user()
        wrong = _auth.verify_login(email, "wrong password here")
        unknown = _auth.verify_login(_email(), "wrong password here")

        assert wrong.ok is unknown.ok is False
        assert wrong.reason != unknown.reason, (
            "the two cases are now indistinguishable in the log as well, "
            "which is where the difference is wanted")
        assert wrong.user is unknown.user is None

    def test_the_route_renders_the_same_page_for_both(self, anon_client,
                                                     quiet_page):
        """The assertion the old test's name promised.

        The whole page, not a substring: anything differing fails,
        including a difference nobody thought to predict.
        """
        _, email = _user()
        ghost = _email()

        known = anon_client.post("/auth/login",
                                 data={"email": email,
                                       "password": "wrong password here"})
        unknown = anon_client.post("/auth/login",
                                   data={"email": ghost,
                                         "password": "wrong password here"})

        assert known.status_code == unknown.status_code == 401
        assert _page(known, email) == _page(unknown, ghost), (
            "the page differs between a wrong password and an address with "
            "no account, so the pair of responses enumerates accounts")

    def test_both_carry_the_one_shared_message(self, anon_client):
        _, email = _user()
        for address in (email, _email()):
            response = anon_client.post(
                "/auth/login",
                data={"email": address, "password": "wrong password here"})
            assert _auth.GENERIC_LOGIN_FAILURE.encode() in response.data, \
                address

    def test_a_deactivated_account_looks_like_a_wrong_password(
            self, anon_client, quiet_page):
        """The path most likely to be forgotten: deactivation is an admin
        action, and its failure has to look like every other failure."""
        _, live = _user()
        _, dead = _user(active=False)

        good = anon_client.post("/auth/login",
                                data={"email": live,
                                      "password": "wrong password here"})
        off = anon_client.post("/auth/login",
                               data={"email": dead,
                                     "password": GOOD_PASSWORD})

        assert good.status_code == off.status_code == 401
        assert _page(good, live) == _page(off, dead)

    def test_a_google_only_account_looks_like_a_wrong_password(
            self, anon_client, quiet_page):
        """Revealing "this address exists but signs in with Google" tells an
        attacker exactly which provider to phish."""
        _, normal = _user()
        google_only = _email()
        _db.create_user(google_only, email_verified=True)

        a = anon_client.post("/auth/login",
                             data={"email": normal,
                                   "password": "wrong password here"})
        b = anon_client.post("/auth/login",
                             data={"email": google_only,
                                   "password": "wrong password here"})

        assert a.status_code == b.status_code == 401
        assert _page(a, normal) == _page(b, google_only)

    def test_the_reason_never_reaches_the_response(self, anon_client):
        """The internal vocabulary is for the log. If one of these words
        ever appears on the page, the log's precision has leaked into the
        product."""
        _, email = _user()
        for address in (email, _email()):
            body = anon_client.post(
                "/auth/login",
                data={"email": address,
                      "password": "wrong password here"}
            ).get_data(as_text=True).lower()
            for reason in ("no_such_user", "bad_password", "no_password",
                           "inactive", "bad_hash"):
                assert reason not in body, (address, reason)

    def test_the_shared_message_names_neither_field(self):
        """"Wrong password" and "unknown email" are both leaks even as a
        hint about *which* field to correct."""
        message = _auth.GENERIC_LOGIN_FAILURE.lower()
        for giveaway in ("no account", "not found", "unknown",
                         "incorrect password", "wrong password",
                         "does not exist"):
            assert giveaway not in message, giveaway


# ── The claim itself, machine-checked ────────────────────────────────

#: vector → (file, the test that covers it).
#:
#: Read by AST rather than imported: the tests directory is not a package,
#: and importing a sibling test module for its side effects to assert a
#: name exists is a worse dependency than reading the file.
COVERAGE: dict[str, tuple[str, str]] = {
    "csrf": ("test_csrf_on_every_post.py",
             "test_a_post_without_a_token_is_refused"),
    "session_fixation": ("test_auth_password.py",
                         "test_the_session_id_changes_on_sign_in"),
    "open_redirect": ("test_auth_password.py",
                      "test_next_cannot_redirect_off_site"),
    "brute_force": ("test_auth_password.py",
                    "test_the_account_locks_after_the_threshold"),
    "timing": ("test_auth_security_vectors.py",
               "test_every_rejection_costs_the_same_single_verification"),
    "enumeration": ("test_auth_security_vectors.py",
                    "test_the_route_renders_the_same_page_for_both"),
}


def _test_names(filename: str) -> set[str]:
    path = TESTS_DIR / filename
    assert path.exists(), f"{filename} is gone"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


class TestTheSixVectorsAreAllCovered:
    """E1.9's acceptance criterion, as a test rather than as a claim.

    Without this, deleting or renaming any one of the four tests that live
    in other files would silently make "all six vectors have a named
    negative test" false, and the plan would go on saying it did.
    """

    def test_all_six_are_listed(self):
        assert set(COVERAGE) == {"csrf", "session_fixation", "open_redirect",
                                 "brute_force", "timing", "enumeration"}

    @pytest.mark.parametrize("vector", sorted(COVERAGE))
    def test_the_covering_test_still_exists(self, vector):
        filename, test_name = COVERAGE[vector]
        names = _test_names(filename)
        assert test_name in names, (
            f"E1.9 vector {vector!r} was covered by {filename}::{test_name}, "
            f"which no longer exists. Either restore it or point COVERAGE at "
            f"whatever replaced it — the vector must not become uncovered "
            f"quietly."
        )

    def test_the_wall_clock_timing_check_is_still_there_as_a_cross_check(self):
        """It is not the gate — the work-equivalence tests above are — but
        it measures the thing an attacker actually observes, so it is worth
        keeping alongside them."""
        assert "test_an_unknown_address_costs_the_same_time_as_a_wrong_password" \
            in _test_names("test_auth_password.py")
