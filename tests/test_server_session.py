"""DB-backed server-side sessions — engine/server_session.py (E0.2).

Two properties are being defended here, and both are things the
filesystem backend could not do:

* a session survives the process that created it (Render's free tier
  restarts the dyno several times a day, and today the Flask session is
  where a project's working state lives);
* every session for one user can be enumerated and dropped in one call,
  which is what "sign out on all devices" and "invalidate sessions on
  password reset" both need.
"""

import json
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask, session as flask_session

from engine import db as _db
from engine import server_session


@pytest.fixture(autouse=True)
def _db_ready():
    _db.init_db()


def _sid() -> str:
    return secrets.token_urlsafe(32)


def _future(hours: int = 24) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


# ── The store itself ──────────────────────────────────────────────

class TestSessionStore:
    def test_save_then_load_round_trips(self):
        sid = _sid()
        assert _db.session_save(sid, '{"a": 1}', _future())
        assert json.loads(_db.session_load(sid)) == {"a": 1}

    def test_unknown_sid_loads_as_none(self):
        # A forged cookie is a dead end, not an error.
        assert _db.session_load(_sid()) is None

    def test_an_expired_row_loads_as_none_and_is_dropped(self):
        sid = _sid()
        _db.session_save(sid, '{"a": 1}', _future(-1))
        assert _db.session_load(sid) is None
        # Deleted on read rather than left for the vacuum, so a replayed
        # cookie cannot resurrect anything.
        with _db.session_scope() as sess:
            assert sess.get(_db.ServerSession, sid) is None

    def test_reading_slides_a_near_expiry_session_forward(self):
        # An actively working user must never be logged out mid-task.
        sid = _sid()
        soon = datetime.now(timezone.utc) + timedelta(hours=1)
        _db.session_save(sid, "{}", soon)
        assert _db.session_load(sid, lifetime=timedelta(days=7)) is not None
        with _db.session_scope() as sess:
            row = sess.get(_db.ServerSession, sid)
            exp = row.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
        assert exp - datetime.now(timezone.utc) > timedelta(days=6)

    def test_reading_a_far_from_expiry_session_leaves_it_alone(self):
        sid = _sid()
        far = datetime.now(timezone.utc) + timedelta(days=10)
        _db.session_save(sid, "{}", far)
        _db.session_load(sid)
        with _db.session_scope() as sess:
            row = sess.get(_db.ServerSession, sid)
            exp = row.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
        assert exp - datetime.now(timezone.utc) < timedelta(days=11)

    def test_delete_removes_the_row(self):
        sid = _sid()
        _db.session_save(sid, "{}", _future())
        assert _db.session_delete(sid) is True
        assert _db.session_load(sid) is None
        assert _db.session_delete(sid) is False

    def test_a_concurrent_insert_of_the_same_sid_does_not_lose_the_write(self):
        """One page load fires a dozen parallel requests carrying the same
        brand-new sid; each sees no row and each inserts.

        Observed for real as ``UNIQUE constraint failed: server_session.sid``
        on the first page load of a signed-in session, which cost that
        request its session write — and with the session holding the working
        pack, that is lost work rather than a stray log line.
        """
        sid = _sid()
        # Simulate the loser of the race: a row appears between the read
        # and the write of the call under test.
        _db.session_save(sid, '{"first": 1}', _future())
        assert _db.session_save(sid, '{"second": 2}', _future()) is True
        assert json.loads(_db.session_load(sid)) == {"second": 2}
        with _db.session_scope() as sess:
            assert sess.query(_db.ServerSession).filter(
                _db.ServerSession.sid == sid).count() == 1

    def test_a_later_save_without_a_user_does_not_deauthenticate(self):
        # A background write that happens to carry no user must not
        # silently sign the tab out.
        sid, uid = _sid(), _db.create_user(f"u-{secrets.token_hex(5)}@e.com")
        _db.session_save(sid, "{}", _future(), user_id=uid)
        _db.session_save(sid, '{"x": 1}', _future())
        with _db.session_scope() as sess:
            assert sess.get(_db.ServerSession, sid).user_id == uid


class TestSignOutEverywhere:
    def test_every_session_for_a_user_is_dropped(self):
        uid = _db.create_user(f"u-{secrets.token_hex(5)}@e.com")
        laptop, phone = _sid(), _sid()
        for sid in (laptop, phone):
            _db.session_save(sid, "{}", _future(), user_id=uid)
        assert _db.delete_sessions_for_user(uid) == 2
        assert _db.session_load(laptop) is None
        assert _db.session_load(phone) is None

    def test_the_initiating_session_can_be_spared(self):
        uid = _db.create_user(f"u-{secrets.token_hex(5)}@e.com")
        here, elsewhere = _sid(), _sid()
        for sid in (here, elsewhere):
            _db.session_save(sid, "{}", _future(), user_id=uid)
        assert _db.delete_sessions_for_user(uid, except_sid=here) == 1
        assert _db.session_load(here) is not None
        assert _db.session_load(elsewhere) is None

    def test_other_users_are_untouched(self):
        a = _db.create_user(f"a-{secrets.token_hex(5)}@e.com")
        b = _db.create_user(f"b-{secrets.token_hex(5)}@e.com")
        sid_a, sid_b = _sid(), _sid()
        _db.session_save(sid_a, "{}", _future(), user_id=a)
        _db.session_save(sid_b, "{}", _future(), user_id=b)
        _db.delete_sessions_for_user(a)
        assert _db.session_load(sid_b) is not None


class TestVacuum:
    def test_expired_rows_are_swept_and_live_ones_kept(self):
        # Nothing in this codebase called any purge_expired_* helper before
        # this programme, so rows accumulated forever. On a free-tier
        # Postgres with a hard size cap that is the database filling up.
        dead, alive = _sid(), _sid()
        _db.session_save(dead, "{}", _future(-2))
        _db.session_save(alive, "{}", _future(48))
        assert _db.purge_expired_sessions() >= 1
        with _db.session_scope() as sess:
            assert sess.get(_db.ServerSession, dead) is None
            assert sess.get(_db.ServerSession, alive) is not None


# ── The Flask interface ───────────────────────────────────────────

def _app(**env) -> Flask:
    """A tiny app wired to the DB session interface."""
    app = Flask(__name__)
    app.secret_key = "test-key"
    app.session_interface = server_session.DbSessionInterface(
        lifetime=timedelta(hours=env.get("hours", 24)))

    @app.route("/set")
    def _set():
        flask_session["who"] = "tester"
        return "ok"

    @app.route("/get")
    def _get():
        return flask_session.get("who", "-")

    @app.route("/clear")
    def _clear():
        flask_session.clear()
        return "ok"

    return app


class TestFlaskInterface:
    def test_a_value_written_in_one_request_is_read_in_the_next(self):
        client = _app().test_client()
        assert client.get("/set").status_code == 200
        assert client.get("/get").data == b"tester"

    def test_the_session_survives_a_process_restart(self):
        # The whole point of E0.2. A brand-new app object with a brand-new
        # (empty) filesystem still finds the session, because it is a row.
        app = _app()
        client = app.test_client()
        client.get("/set")
        cookie = client.get_cookie("session")
        assert cookie is not None

        reborn = _app().test_client()
        reborn.set_cookie("session", cookie.value)
        assert reborn.get("/get").data == b"tester"

    def test_the_cookie_carries_no_session_data(self):
        # Server-side means server-side: the cookie is an opaque key, so
        # nothing in it is readable or forgeable into meaning.
        client = _app().test_client()
        client.get("/set")
        assert b"tester" not in client.get_cookie("session").value.encode()

    def test_an_unmodified_request_writes_no_cookie(self):
        # Otherwise every static asset request re-stamps the session.
        client = _app().test_client()
        client.get("/set")
        resp = client.get("/get")
        assert "Set-Cookie" not in resp.headers

    def test_clearing_the_session_drops_the_row_and_the_cookie(self):
        app = _app()
        client = app.test_client()
        client.get("/set")
        sid = client.get_cookie("session").value
        resp = client.get("/clear")
        assert "Set-Cookie" in resp.headers
        assert _db.session_load(sid) is None

    def test_a_forged_cookie_yields_an_empty_session_not_an_error(self):
        client = _app().test_client()
        client.set_cookie("session", "not-a-real-sid")
        assert client.get("/get").data == b"-"

    def test_an_absurdly_long_cookie_is_refused_before_the_query(self):
        client = _app().test_client()
        client.set_cookie("session", "x" * 400)
        assert client.get("/get").data == b"-"

    def test_payload_is_json_not_pickle(self):
        # These rows will soon hold the authenticated user id; a pickle
        # would make each one a deserialisation gadget.
        client = _app().test_client()
        client.get("/set")
        raw = _db.session_load(client.get_cookie("session").value)
        assert json.loads(raw) == {"who": "tester"}

    def test_a_dead_database_degrades_instead_of_500ing(self, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("database is on fire")

        monkeypatch.setattr(_db, "session_load", _boom)
        client = _app().test_client()
        # Honest empty session beats a stack trace on every page.
        assert client.get("/get").status_code == 200


class TestInstallSwitch:
    def test_install_is_off_unless_asked_for(self, monkeypatch):
        monkeypatch.delenv("SESSION_BACKEND", raising=False)
        app = Flask(__name__)
        before = app.session_interface
        assert server_session.install(app) is False
        assert app.session_interface is before

    def test_install_takes_over_when_session_backend_is_db(self, monkeypatch):
        monkeypatch.setenv("SESSION_BACKEND", "db")
        app = Flask(__name__)
        assert server_session.install(app) is True
        assert isinstance(app.session_interface,
                          server_session.DbSessionInterface)

    def test_lifetime_is_configurable_and_survives_a_bad_value(self, monkeypatch):
        monkeypatch.setenv("SESSION_BACKEND", "db")
        monkeypatch.setenv("SESSION_DB_LIFETIME_HOURS", "48")
        app = Flask(__name__)
        server_session.install(app)
        assert app.session_interface.lifetime == timedelta(hours=48)

        monkeypatch.setenv("SESSION_DB_LIFETIME_HOURS", "soon")
        app2 = Flask(__name__)
        server_session.install(app2)
        assert app2.session_interface.lifetime == timedelta(
            hours=server_session.DEFAULT_LIFETIME_HOURS)
