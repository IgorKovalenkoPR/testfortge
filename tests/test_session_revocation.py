"""Ending somebody's sessions did nothing on the backend production runs.

``engine.db.delete_sessions_for_user`` deleted ``ServerSession`` rows. Those
rows exist only under ``SESSION_BACKEND=db``; anything else keeps
Flask-Session's filesystem store, which has no user column and therefore no
rows to delete. ``render.yaml`` sets ``filesystem`` on the ``testfortge``
service, the test suite uses it too, and ``engine.server_session.install``
documents the db backend as an explicit opt-in — so the default path is the
one that mattered, and on it the call returned 0 and every session survived.

Three callers depend on it, and each says in its own comment what it is for:

* ``routes/auth.py`` — the email password reset. "Leaving the intruder's
  cookie working would make the reset ceremony." It did.
* ``routes/members.py`` — an admin setting a member's password, and removing
  a member from the team ("their sessions die with their access").
* ``engine.permissions.logout_user(everywhere=True)`` — sign out on all
  devices.

The fix is a cut-off on the account rather than a sweep over a store: the
session already carries the instant it was signed in, and ``current_user``
already re-reads the account row on every request, so nothing here costs a
query that was not already being made. Deleting the rows stays — a cookie
that resolves to nothing is better than one refused after it resolves — but
it is no longer the only thing standing between a reset and an intruder.

Every test below runs on the **default** backend, which is the whole point:
the previous version of this behaviour passed under ``SESSION_BACKEND=db``.
"""
import secrets

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import permissions as _perm
from engine import session_timeout as _timeout


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    _db.init_db()


@pytest.fixture
def account():
    """One signed-up member of one team."""
    org = _db.create_organization(f"Org {secrets.token_hex(4)}")
    uid = _db.create_user(f"u-{secrets.token_hex(5)}@example.com",
                          password_hash=_auth.hash_password(
                              "a perfectly good passphrase"))
    _db.add_org_member(org, uid, "user")
    return {"org": org, "uid": uid}


def _signed_in(app, account, *, at=None):
    """A client holding a session stamped the way a real sign-in stamps it.

    The stamp is the part that matters — ``login_user`` writes it through
    ``session_timeout.stamp`` on every one of the four sign-in paths, and it
    is what the cut-off is compared against.
    """
    from datetime import datetime, timezone
    c = app.test_client()
    moment = (at or datetime.now(timezone.utc)).timestamp()
    with c.session_transaction() as sess:
        sess[_perm.SESSION_USER_KEY] = account["uid"]
        sess[_perm.SESSION_ORG_KEY] = account["org"]
        sess[_timeout.AUTH_AT_KEY] = moment
        sess[_timeout.SEEN_AT_KEY] = moment
    return c


def _is_signed_in(client) -> bool:
    """Asked of the product, not of the session dict.

    ``/auth/me`` is the endpoint the UI itself uses to decide whether to
    render a sign-in link, and it is open — so a caller whose session has
    been revoked reaches it and is told "nobody" rather than being
    redirected, which would be indistinguishable from a dozen other
    redirects.
    """
    return bool(client.get("/auth/me").get_json()["authenticated"])


class TestTheControl:
    """A session nobody revoked keeps working — asserted first, because
    every test below would also pass if the check refused everybody."""

    def test_a_signed_in_session_works(self, app, account):
        assert _is_signed_in(_signed_in(app, account))

    def test_it_still_works_after_somebody_else_is_revoked(self, app, account):
        other = _db.create_user(f"o-{secrets.token_hex(5)}@example.com")
        c = _signed_in(app, account)
        _db.delete_sessions_for_user(other)
        assert _is_signed_in(c)

    def test_signing_in_again_after_a_revocation_works(self, app, account):
        """The cut-off is in the past by then, which is the property that
        keeps a password reset usable by the person who asked for it."""
        _db.delete_sessions_for_user(account["uid"])
        assert _is_signed_in(_signed_in(app, account))


class TestRevocation:

    def test_the_session_stops_working(self, app, account):
        c = _signed_in(app, account)
        assert _is_signed_in(c)
        _db.delete_sessions_for_user(account["uid"])
        assert not _is_signed_in(c)

    def test_the_account_records_the_cut_off(self, app, account):
        assert _db.get_user(account["uid"])["sessions_valid_from"] is None
        _db.delete_sessions_for_user(account["uid"])
        assert _db.get_user(account["uid"])["sessions_valid_from"] is not None

    def test_a_gated_page_refuses_too(self, app, account):
        """``/auth/me`` is open, so on its own it proves only that the
        identity resolver said no. This is the refusal that matters."""
        c = _signed_in(app, account)
        assert c.get("/test-cases").status_code == 200
        _db.delete_sessions_for_user(account["uid"])
        assert c.get("/test-cases").status_code in (302, 401)

    def test_an_unstamped_session_is_refused(self, app, account):
        """Deliberately the opposite of ``session_timeout.classify``.

        There, a missing stamp means "predates the deploy that added
        timeouts" and must not sign everybody out on one release. Here it
        means the session cannot show it was created after the revocation,
        and a revocation that keeps sessions it cannot vouch for has not
        revoked anything. It only ever applies to an account somebody
        actually revoked — which the control above is what proves.
        """
        c = app.test_client()
        with c.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = account["uid"]
            sess[_perm.SESSION_ORG_KEY] = account["org"]
        assert _is_signed_in(c)
        _db.delete_sessions_for_user(account["uid"])
        assert not _is_signed_in(c)

    def test_an_unreadable_stamp_is_refused(self, app, account):
        c = app.test_client()
        with c.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = account["uid"]
            sess[_perm.SESSION_ORG_KEY] = account["org"]
            sess[_timeout.AUTH_AT_KEY] = "not a timestamp"
        _db.delete_sessions_for_user(account["uid"])
        assert not _is_signed_in(c)


class TestTheCallersThatDependOnIt:
    """Each of the three, through the surface that performs it — because the
    property is theirs, and a unit test of the sweep proves none of them."""

    def test_a_password_reset_ends_the_intruders_session(self, app, account):
        """The reset link path, from the token to the new password.

        The intruder's client is the one signed in *before* the reset; the
        person who owns the inbox is whoever posts the form.
        """
        intruder = _signed_in(app, account)
        assert _is_signed_in(intruder)

        token = secrets.token_urlsafe(32)
        assert _db.create_auth_token("reset", account["uid"],
                                     _db.get_user(account["uid"])["email"],
                                     token)
        victim = app.test_client()
        resp = victim.post(f"/auth/reset/{token}",
                           data={"password": "a different good passphrase",
                                 "password_confirm": "a different good passphrase"},
                           follow_redirects=False)
        assert resp.status_code in (200, 302), resp.status_code
        assert _db.get_user(account["uid"])["sessions_valid_from"] is not None
        assert not _is_signed_in(intruder)

    def test_an_admin_setting_a_password_ends_their_sessions(self, app,
                                                            account):
        admin = _db.create_user(f"a-{secrets.token_hex(5)}@example.com",
                                password_hash=_auth.hash_password(
                                    "a perfectly good passphrase"))
        _db.add_org_member(account["org"], admin, "admin")
        theirs = _signed_in(app, account)

        c = app.test_client()
        with c.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = admin
            sess[_perm.SESSION_ORG_KEY] = account["org"]
        c.post(f"/org/members/{account['uid']}/password",
               data={"password": "a different good passphrase",
                     "password_confirm": "a different good passphrase"})
        assert not _is_signed_in(theirs)

    def test_removing_a_member_ends_their_sessions(self, app, account):
        admin = _db.create_user(f"a-{secrets.token_hex(5)}@example.com",
                                password_hash=_auth.hash_password(
                                    "a perfectly good passphrase"))
        _db.add_org_member(account["org"], admin, "admin")
        theirs = _signed_in(app, account)

        c = app.test_client()
        with c.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = admin
            sess[_perm.SESSION_ORG_KEY] = account["org"]
        c.post(f"/org/members/{account['uid']}/remove")
        assert not _is_signed_in(theirs)

    def test_sign_out_everywhere_ends_the_other_device(self, app, account):
        phone = _signed_in(app, account)
        laptop = _signed_in(app, account)
        laptop.post("/auth/logout", data={"everywhere": "1"})
        assert not _is_signed_in(phone)


class TestTheComparisonItself:
    """``_revoked_before`` on its own, for the answers that are hard to
    stage through a request and easy to get wrong."""

    def test_no_cut_off_is_not_a_revocation(self):
        assert _perm._revoked_before(None) is False

    def test_outside_a_request_context_it_refuses_nothing(self):
        """Called from a background job, a CLI, the snapshot thread. There
        is no session there to have been revoked, and answering "revoked"
        would make every one of them fail on an account somebody reset."""
        from datetime import datetime, timezone
        assert _perm._revoked_before(datetime.now(timezone.utc)) is False

    def test_an_unreadable_cut_off_refuses_the_session(self, app):
        """Fail closed on this one, unlike the two above: a cut-off that
        cannot be read is a revocation that happened."""
        with app.test_request_context("/"):
            from flask import session
            session[_timeout.AUTH_AT_KEY] = 1.0
            assert _perm._revoked_before("not a date") is True

    def test_a_naive_cut_off_is_read_as_utc(self, app):
        """SQLite stores ``TIMESTAMP`` without an offset, so the value comes
        back naive while the session stamp is a UTC epoch. Comparing the two
        without saying so raises ``TypeError`` — which, in a function whose
        callers treat an exception as "honour the pin", would have made the
        whole check a no-op on the engine most deployments run.
        """
        from datetime import datetime, timedelta, timezone
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        with app.test_request_context("/"):
            from flask import session
            session[_timeout.AUTH_AT_KEY] = past.timestamp()
            naive_future = (datetime.now(timezone.utc)
                            + timedelta(hours=1)).replace(tzinfo=None)
            assert _perm._revoked_before(naive_future) is True
            naive_past = (past - timedelta(hours=1)).replace(tzinfo=None)
            assert _perm._revoked_before(naive_past) is False

    def test_an_unreadable_stamp_refuses(self, app):
        with app.test_request_context("/"):
            from flask import session
            session[_timeout.AUTH_AT_KEY] = "not a timestamp"
            from datetime import datetime, timezone
            assert _perm._revoked_before(datetime.now(timezone.utc)) is True


class TestTheMigration:
    """An existing database is where revocation matters, so the column has
    to arrive there and not only in ``create_all``."""

    def test_the_column_is_declared_as_a_migration(self):
        pairs = {(table, column)
                 for table, column, _ in _db._EDITABLE_COLUMN_MIGRATIONS}
        assert ("app_user", "sessions_valid_from") in pairs

    def test_never_revoked_does_not_read_as_revoked_at_the_epoch(self, app,
                                                                 account):
        """The reason the column is nullable with no default: a ``NOT NULL
        DEFAULT`` would have stamped every existing account and signed the
        whole instance out on the deploy that added it."""
        assert _db.get_user(account["uid"])["sessions_valid_from"] is None
        assert _is_signed_in(_signed_in(app, account))
