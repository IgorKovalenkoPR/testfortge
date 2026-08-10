"""The first administrator — the missing first caller of the invite flow.

Found 2026-08-10 by asking a question the test suite never had to ask: with
``AUTH_ENABLED=1`` on a fresh deployment, how does the owner sign in?

* an account is created only by claiming an invitation (``/auth/accept``, or
  the Google callback, which ``engine.oauth.decide`` refuses without one);
* an invitation is issued only by an admin;
* ``db.create_organization`` is reachable from no route;
* and the Basic-gate interlock keeps the gate up while ``AUTH_ENABLED=0``,
  so "turn the gate off instead" is not a way in either.

So enabling authentication on an empty database locks everybody out
permanently. Every piece is individually correct, which is exactly why no
test caught it: the suite always created its users through ``engine.db``,
the way a fixture can and an operator cannot.

The assertion that matters
--------------------------
Not "a row appeared in the users table" — that is the code path. The test
this file exists for signs in **through the real login route** with the
password from the environment variable, because that is the thing the owner
will do, and the only claim worth making.
"""
from __future__ import annotations

import secrets

import pytest

from app import app as flask_app
from engine import auth as _auth
from engine import bootstrap
from engine import db as _db
from engine import permissions as _perm


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A database with no users at all — the state the bootstrap is for.

    A private file, not the suite's scratch database: every other test
    creates users in it, and "no users at all" is the one precondition this
    module cannot share.
    """
    monkeypatch.setenv("FLASK_DEBUG", "1")
    monkeypatch.setenv("TESTFORTGE_DB", str(tmp_path / "bootstrap.db"))
    _db._engine = None            # force a rebuild against the new path
    _db.init_db()
    assert _db.count_users() == 0, "the fixture is not a fresh database"
    yield
    _db._engine = None
    _db.init_db()                 # hand the suite's own database back


@pytest.fixture
def configured(monkeypatch):
    """The two variables set, with a password the product would accept."""
    email = f"owner-{secrets.token_hex(4)}@example.test"
    password = "a phrase of several words"
    monkeypatch.setenv(bootstrap.EMAIL_ENV, email)
    monkeypatch.setenv(bootstrap.PASSWORD_ENV, password)
    monkeypatch.delenv(bootstrap.ORG_ENV, raising=False)
    return {"email": email, "password": password}


class TestItCreatesAnAdminWhoCanActuallySignIn:

    def test_the_account_signs_in_through_the_real_login_route(
            self, fresh_db, configured, monkeypatch):
        """The whole point, and the only assertion that proves it.

        Reads the flash and the session rather than the status code: a
        failed sign-in also renders 200, which is how every defect in this
        programme looked from the outside.
        """
        user_id = bootstrap.claim_first_admin()
        assert user_id

        monkeypatch.setitem(flask_app.config, "TESTING", True)
        monkeypatch.setitem(flask_app.config, "WTF_CSRF_ENABLED", False)
        monkeypatch.setattr(_perm, "auth_active", lambda: True)
        client = flask_app.test_client()

        response = client.post("/auth/login", data={
            "email": configured["email"],
            "password": configured["password"],
        }, follow_redirects=False)

        assert response.status_code in (302, 303), (
            f"the sign-in did not redirect, so it did not succeed: "
            f"{response.status_code}")
        with client.session_transaction() as sess:
            assert sess.get(_perm.SESSION_USER_KEY) == user_id, (
                "the session carries no user id, so the password minted by "
                "the bootstrap does not open the door it exists for")

    def test_it_is_an_admin_of_an_organisation(self, fresh_db, configured):
        user_id = bootstrap.claim_first_admin()
        orgs = _db.list_orgs_for_user(user_id) \
            if hasattr(_db, "list_orgs_for_user") else []
        if orgs:
            org_id = orgs[0]["id"] if isinstance(orgs[0], dict) else orgs[0]
        else:                      # fall back to the audit record's org
            org_id = ""
            for row in _db.list_audit(limit=20) or []:
                if row.get("action") == "bootstrap_admin":
                    org_id = (row.get("diff") or {}).get("org") or ""
                    break
        assert org_id, "no organisation was recorded for the first admin"
        assert _db.count_org_admins(org_id) == 1

    def test_the_address_is_verified(self, fresh_db, configured):
        """Nobody can send a confirmation link yet, and an unconfirmed
        banner on the first account is a dead end rather than a nudge."""
        user_id = bootstrap.claim_first_admin()
        assert _db.get_user(user_id)["email_verified"] is True

    def test_it_is_recorded_in_the_audit_trail(self, fresh_db, configured):
        """Minting an administrator is the most privileged thing this
        codebase does without a human in the loop."""
        bootstrap.claim_first_admin()
        actions = [row.get("action") for row in _db.list_audit(limit=20) or []]
        assert "bootstrap_admin" in actions


class TestItRefusesEveryOtherCase:

    def test_it_does_nothing_when_a_user_already_exists(
            self, fresh_db, configured):
        first = bootstrap.claim_first_admin()
        assert first
        again = bootstrap.claim_first_admin()
        assert again == "", "it minted a second administrator"
        assert _db.count_users() == 1

    def test_it_does_nothing_when_unconfigured(self, fresh_db, monkeypatch):
        monkeypatch.delenv(bootstrap.EMAIL_ENV, raising=False)
        monkeypatch.delenv(bootstrap.PASSWORD_ENV, raising=False)
        assert bootstrap.claim_first_admin() == ""
        assert _db.count_users() == 0

    def test_half_configured_creates_nothing(self, fresh_db, monkeypatch):
        """And it is a warning rather than silence: an operator who set one
        variable is waiting for an account that will never appear."""
        monkeypatch.setenv(bootstrap.EMAIL_ENV, "owner@example.test")
        monkeypatch.delenv(bootstrap.PASSWORD_ENV, raising=False)
        assert bootstrap.claim_first_admin() == ""
        assert _db.count_users() == 0

    def test_a_weak_password_creates_no_admin(self, fresh_db, monkeypatch):
        """The product's own rule applies to the operator too. An admin
        with a four-character password is worse than no admin, because the
        second state is obvious and the first is not."""
        monkeypatch.setenv(bootstrap.EMAIL_ENV, "owner@example.test")
        monkeypatch.setenv(bootstrap.PASSWORD_ENV, "12345")
        assert bootstrap.claim_first_admin() == ""
        assert _db.count_users() == 0, (
            "a password the login form would reject produced an account")

    def test_the_password_rule_is_the_products_own(self):
        """Guards the previous test against drifting apart from the policy
        it claims to reuse: if MIN_PASSWORD_LEN drops to 4, "12345" stops
        being weak and that test starts passing vacuously."""
        assert _auth.MIN_PASSWORD_LEN > 5

    def test_it_never_raises(self, fresh_db, monkeypatch):
        """It runs at boot. A misconfigured variable must not be the reason
        the service will not start — app.py wraps it too, and this checks
        the module itself keeps that contract."""
        monkeypatch.setenv(bootstrap.EMAIL_ENV, "not-an-email")
        monkeypatch.setenv(bootstrap.PASSWORD_ENV, "a phrase of several words")
        bootstrap.claim_first_admin()      # must not raise


class TestTheDeploymentDeclaresIt:

    def test_render_declares_both_variables(self):
        """A value set in the dashboard and absent from the blueprint is
        deleted by the next Manual Sync — the failure mode E0.6 exists for.
        ``BOOTSTRAP_ADMIN_PASSWORD`` is a credential, so it must be
        ``sync: false`` rather than carry a value here."""
        import pathlib
        import yaml
        blueprint = yaml.safe_load(
            (pathlib.Path(__file__).resolve().parent.parent
             / "render.yaml").read_text(encoding="utf-8"))
        web = next(s for s in blueprint["services"]
                   if s.get("type") == "web")
        env = {e["key"]: e for e in web.get("envVars", []) if "key" in e}
        for name in (bootstrap.EMAIL_ENV, bootstrap.PASSWORD_ENV):
            assert name in env, f"render.yaml does not declare {name}"
        assert env[bootstrap.PASSWORD_ENV].get("sync") is False, (
            "the bootstrap password must be sync: false — a credential does "
            "not belong in a file in git")
