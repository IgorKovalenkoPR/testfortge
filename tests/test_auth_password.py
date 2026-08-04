"""Password authentication — engine/auth.py + routes/auth.py (E1.2 / E1.5).

Security-critical, so the negative cases outnumber the positive ones. Each
test names the attack it prevents rather than the function it calls, because
in six months the useful question about a failing line here will be "what
was this stopping".
"""

import secrets
import time
from datetime import datetime, timedelta, timezone

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import permissions as _perm
from routes.auth import new_invite_token


@pytest.fixture(autouse=True)
def _db_ready():
    _db.init_db()


@pytest.fixture(autouse=True)
def _auth_on(monkeypatch):
    """Most of these need real accounts switched on."""
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")


def _email() -> str:
    return f"u-{secrets.token_hex(6)}@example.com"


GOOD_PASSWORD = "correct horse battery staple"


def _user(password: str = GOOD_PASSWORD, **kw) -> tuple[str, str]:
    email = _email()
    uid = _db.create_user(email, password_hash=_auth.hash_password(password),
                          **kw)
    return uid, email


# ── Password policy ───────────────────────────────────────────────

class TestPasswordPolicy:
    def test_a_passphrase_is_accepted(self):
        assert _auth.hash_password(GOOD_PASSWORD)

    def test_too_short_is_refused_with_advice(self):
        with pytest.raises(_auth.PasswordPolicyError) as exc:
            _auth.hash_password("short")
        # The message tells the person what to do, not which rule fired.
        assert str(_auth.MIN_PASSWORD_LEN) in str(exc.value)

    def test_absurdly_long_is_refused(self):
        # Argon2 hashes whatever it is handed, so an unbounded field is a
        # cheap way to make the server do megabytes of work per request.
        with pytest.raises(_auth.PasswordPolicyError):
            _auth.hash_password("a" * (_auth.MAX_PASSWORD_LEN + 1))

    def test_whitespace_only_is_refused(self):
        with pytest.raises(_auth.PasswordPolicyError):
            _auth.hash_password(" " * 20)

    def test_the_password_cannot_be_the_email(self):
        email = "averylongemailaddress@example.com"
        with pytest.raises(_auth.PasswordPolicyError):
            _auth.hash_password(email, email=email)

    def test_no_composition_rules(self):
        # Requiring a digit and a symbol pushes people toward Password1!
        # and is no longer recommended. Length carries the entropy.
        assert _auth.hash_password("all lowercase words here")

    def test_the_hash_is_argon2id_and_not_the_password(self):
        h = _auth.hash_password(GOOD_PASSWORD)
        assert h.startswith("$argon2id$")
        assert GOOD_PASSWORD not in h

    def test_two_hashes_of_one_password_differ(self):
        # i.e. it is salted. Identical hashes would let anyone spot shared
        # passwords straight out of a dump of the table.
        assert _auth.hash_password(GOOD_PASSWORD) != \
            _auth.hash_password(GOOD_PASSWORD)


# ── Login ─────────────────────────────────────────────────────────

class TestLogin:
    def test_the_right_password_succeeds(self):
        uid, email = _user()
        result = _auth.verify_login(email, GOOD_PASSWORD)
        assert result.ok and result.user["id"] == uid

    def test_the_wrong_password_fails(self):
        _, email = _user()
        assert _auth.verify_login(email, "not the password at all").ok is False

    def test_an_unknown_address_fails(self):
        assert _auth.verify_login(_email(), GOOD_PASSWORD).ok is False

    def test_email_case_and_padding_do_not_matter(self):
        uid, email = _user()
        result = _auth.verify_login(f"  {email.upper()} ", GOOD_PASSWORD)
        assert result.ok and result.user["id"] == uid

    def test_a_deactivated_account_cannot_sign_in(self):
        uid, email = _user()
        _db.set_user_active(uid, False)
        assert _auth.verify_login(email, GOOD_PASSWORD).ok is False

    def test_a_google_only_account_gets_the_same_opaque_failure(self):
        # Saying "this address signs in with Google" tells an attacker
        # which provider to phish.
        email = _email()
        _db.create_user(email)          # no password_hash
        result = _auth.verify_login(email, GOOD_PASSWORD)
        assert result.ok is False
        assert result.reason == "no_password"

    def test_a_successful_login_stamps_last_login(self):
        uid, email = _user()
        assert _db.get_user(uid)["last_login_at"] is None
        _auth.verify_login(email, GOOD_PASSWORD)
        assert _db.get_user(uid)["last_login_at"] is not None


class TestAccountEnumeration:
    def test_one_message_covers_both_failure_modes(self):
        # A single constant, so the two paths cannot drift apart when
        # somebody improves an error message in good faith later.
        assert isinstance(_auth.GENERIC_LOGIN_FAILURE, str)
        assert "match" in _auth.GENERIC_LOGIN_FAILURE.lower()

    def test_an_unknown_address_costs_the_same_time_as_a_wrong_password(self):
        # The real leak: a missing user returns in microseconds while a real
        # one spends ~50 ms in Argon2, which is measurable over a network.
        _, email = _user()

        def _timed(fn, n=3):
            best = None
            for _ in range(n):
                start = time.perf_counter()
                fn()
                elapsed = time.perf_counter() - start
                best = elapsed if best is None else min(best, elapsed)
            return best

        known = _timed(lambda: _auth.verify_login(email, "wrong password xx"))
        unknown = _timed(lambda: _auth.verify_login(_email(), "wrong pw xx"))
        # Generous bound — this is a coarse smoke check, not a statistical
        # test. Before the dummy-hash equaliser the ratio was ~1000x.
        assert unknown > known / 5, (
            f"unknown-user path took {unknown:.4f}s vs {known:.4f}s for a "
            f"known user — the difference enumerates accounts"
        )


class TestLockout:
    def test_the_account_locks_after_the_threshold(self):
        uid, email = _user()
        for _ in range(_auth.MAX_FAILED_LOGINS):
            _auth.verify_login(email, "wrong")
        result = _auth.verify_login(email, GOOD_PASSWORD)
        assert result.ok is False
        assert result.reason == "locked"

    def test_the_lock_survives_a_restart(self):
        # A counter in memory is not a lockout on a dyno that sleeps every
        # fifteen minutes — so it lives on the row.
        uid, email = _user()
        for _ in range(_auth.MAX_FAILED_LOGINS):
            _auth.verify_login(email, "wrong")
        assert _db.get_user(uid)["locked_until"] is not None

    def test_a_successful_login_resets_the_counter(self):
        uid, email = _user()
        _auth.verify_login(email, "wrong")
        _auth.verify_login(email, "wrong")
        assert _db.get_user(uid)["failed_logins"] == 2
        assert _auth.verify_login(email, GOOD_PASSWORD).ok
        assert _db.get_user(uid)["failed_logins"] == 0

    def test_the_lock_expires(self):
        uid, email = _user()
        _db.lock_user(uid, datetime.now(timezone.utc) - timedelta(minutes=1))
        assert _auth.verify_login(email, GOOD_PASSWORD).ok is True

    def test_the_counter_advances_once_per_attempt(self):
        # Incremented with a SQL expression, not read-modify-write: two
        # concurrent wrong guesses — the shape of an actual attack — would
        # otherwise both read n and write n+1, doubling the attempts needed
        # to reach the threshold.
        uid, email = _user()
        for expected in range(1, 4):
            _auth.verify_login(email, "wrong")
            assert _db.get_user(uid)["failed_logins"] == expected

    def test_the_lockout_message_is_safe_to_show(self):
        # The person locked it themselves, so it reveals nothing new.
        until = datetime.now(timezone.utc) + timedelta(minutes=7)
        msg = _auth.lockout_message(until)
        assert "minute" in msg


class TestRehashOnLogin:
    def test_a_weaker_stored_hash_is_upgraded(self, monkeypatch):
        from argon2 import PasswordHasher
        weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
        email = _email()
        uid = _db.create_user(email, password_hash=weak.hash(GOOD_PASSWORD))
        before = _db.get_user(uid)["password_hash"]

        assert _auth.verify_login(email, GOOD_PASSWORD).ok is True

        after = _db.get_user(uid)["password_hash"]
        assert after != before, "raising the cost left an old account behind"
        # …and the new hash still works.
        assert _auth.verify_login(email, GOOD_PASSWORD).ok is True

    def test_an_unreadable_hash_fails_closed(self):
        email = _email()
        _db.create_user(email, password_hash="not-an-argon2-hash")
        result = _auth.verify_login(email, GOOD_PASSWORD)
        assert result.ok is False and result.reason == "bad_hash"


# ── The HTTP surface ──────────────────────────────────────────────

class TestLoginRoute:
    def test_the_page_renders(self, client):
        assert client.get("/auth/login").status_code == 200

    def test_a_good_password_signs_in(self, client):
        uid, email = _user()
        resp = client.post("/auth/login",
                           data={"email": email, "password": GOOD_PASSWORD})
        assert resp.status_code == 302
        assert client.get("/auth/me").get_json()["authenticated"] is True

    def test_a_bad_password_returns_401_and_the_generic_message(self, client):
        _, email = _user()
        resp = client.post("/auth/login",
                           data={"email": email, "password": "wrong wrong"})
        assert resp.status_code == 401
        assert _auth.GENERIC_LOGIN_FAILURE.encode() in resp.data

    def test_an_unknown_address_returns_the_identical_body(self, client):
        _, email = _user()
        known = client.post("/auth/login",
                            data={"email": email, "password": "wrong wrong"})
        unknown = client.post("/auth/login",
                              data={"email": _email(),
                                    "password": "wrong wrong"})
        assert known.status_code == unknown.status_code == 401
        # The rendered page must not differ — the message is the leak.
        assert _auth.GENERIC_LOGIN_FAILURE.encode() in unknown.data

    def test_signing_out_clears_the_session(self, client):
        _, email = _user()
        client.post("/auth/login",
                    data={"email": email, "password": GOOD_PASSWORD})
        client.post("/auth/logout")
        assert client.get("/auth/me").get_json()["authenticated"] is False

    def test_me_never_returns_the_password_hash(self, client):
        _, email = _user()
        client.post("/auth/login",
                    data={"email": email, "password": GOOD_PASSWORD})
        body = client.get("/auth/me").get_json()
        assert "password_hash" not in body["user"]
        assert "password" not in str(body).lower()

    @pytest.mark.parametrize("hostile", [
        "https://evil.example.com/",
        "//evil.example.com/",
        "http:/\\evil.example.com",
    ])
    def test_next_cannot_redirect_off_site(self, client, hostile):
        # A sign-in page is the most attractive place in any app for an
        # open redirect, because the user has just been asked to trust it.
        _, email = _user()
        resp = client.post("/auth/login",
                           data={"email": email, "password": GOOD_PASSWORD,
                                 "next": hostile})
        assert resp.status_code == 302
        assert "evil.example.com" not in resp.headers["Location"]

    def test_a_same_origin_next_is_honoured(self, client):
        _, email = _user()
        resp = client.post("/auth/login",
                           data={"email": email, "password": GOOD_PASSWORD,
                                 "next": "/estimation"})
        assert resp.headers["Location"].endswith("/estimation")


class TestSessionFixation:
    def test_the_session_id_changes_on_sign_in(self, client, monkeypatch):
        # Without rotation, anyone who can plant a known session cookie
        # before sign-in holds a valid authenticated session afterwards.
        monkeypatch.setenv("SESSION_BACKEND", "db")
        from engine import server_session
        server_session.install(client.application)

        client.get("/auth/login")                 # mint an anonymous session
        before = client.get_cookie("session")
        _, email = _user()
        client.post("/auth/login",
                    data={"email": email, "password": GOOD_PASSWORD})
        after = client.get_cookie("session")
        assert before is not None and after is not None
        assert before.value != after.value

    def test_the_pre_login_session_row_is_not_reused(self, client, monkeypatch):
        monkeypatch.setenv("SESSION_BACKEND", "db")
        from engine import server_session
        server_session.install(client.application)

        client.get("/auth/login")
        stale = client.get_cookie("session").value
        _, email = _user()
        client.post("/auth/login",
                    data={"email": email, "password": GOOD_PASSWORD})
        # Replaying the pre-login cookie must not be authenticated.
        other = client.application.test_client()
        other.set_cookie("session", stale)
        assert other.get("/auth/me").get_json()["authenticated"] is False


# ── Invites ───────────────────────────────────────────────────────

class TestInviteAcceptance:
    def _invited(self, role="user") -> tuple[str, str, str]:
        org = _db.create_organization(f"Team {secrets.token_hex(4)}")
        email, token = _email(), new_invite_token()
        assert _db.create_invite(org, email, role, token)
        return org, email, token

    def test_the_page_renders_for_a_live_invite(self, client):
        _, email, token = self._invited()
        resp = client.get(f"/auth/accept/{token}")
        assert resp.status_code == 200
        assert email.encode() in resp.data

    def test_accepting_creates_the_account_and_the_membership(self, client):
        org, email, token = self._invited(role="admin")
        resp = client.post(f"/auth/accept/{token}",
                           data={"password": GOOD_PASSWORD,
                                 "password_confirm": GOOD_PASSWORD,
                                 "display_name": "New Tester"})
        assert resp.status_code == 302
        user = _db.get_user_by_email(email)
        assert user is not None
        assert _db.get_org_role(org, user["id"]) == "admin"
        # Signed in immediately — a second sign-in form right after
        # choosing a password is friction with no security value.
        assert client.get("/auth/me").get_json()["authenticated"] is True

    def test_the_invited_address_is_treated_as_proven(self, client):
        # Only the link's holder could have opened it, and the free email
        # tier is capped at 100 messages a day — a second confirmation
        # would be ceremony with a cost.
        _, email, token = self._invited()
        client.post(f"/auth/accept/{token}",
                    data={"password": GOOD_PASSWORD,
                          "password_confirm": GOOD_PASSWORD})
        assert _db.get_user_by_email(email)["email_verified"] is True

    def test_mismatched_confirmation_is_refused(self, client):
        _, email, token = self._invited()
        resp = client.post(f"/auth/accept/{token}",
                           data={"password": GOOD_PASSWORD,
                                 "password_confirm": "something else here"})
        assert resp.status_code == 400
        assert _db.get_user_by_email(email) is None

    def test_a_weak_password_is_refused_and_creates_nothing(self, client):
        _, email, token = self._invited()
        resp = client.post(f"/auth/accept/{token}",
                           data={"password": "short", "password_confirm": "short"})
        assert resp.status_code == 400
        assert _db.get_user_by_email(email) is None
        # …and the invite is still claimable, so the person can retry.
        assert _db.get_invite(token) is not None

    def test_an_expired_token_gets_410_and_one_message(self, client):
        _, _, token = self._invited()
        with _db.session_scope() as sess:
            sess.get(_db.Invite, token).expires_at = \
                datetime.now(timezone.utc) - timedelta(minutes=1)
        resp = client.get(f"/auth/accept/{token}")
        assert resp.status_code == 410

    def test_an_unknown_token_is_indistinguishable_from_an_expired_one(self, client):
        # Distinguishing them confirms which tokens were real to anyone
        # spraying guesses at the endpoint.
        resp = client.get(f"/auth/accept/{new_invite_token()}")
        assert resp.status_code == 410

    def test_a_token_cannot_be_claimed_twice(self, client):
        _, _, token = self._invited()
        client.post(f"/auth/accept/{token}",
                    data={"password": GOOD_PASSWORD,
                          "password_confirm": GOOD_PASSWORD})
        again = client.application.test_client()
        assert again.get(f"/auth/accept/{token}").status_code == 410

    def test_an_existing_user_joins_without_setting_a_new_password(self, client):
        # Whoever holds the link must not be able to change the password of
        # an account that already exists.
        uid, email = _user()
        org = _db.create_organization(f"Team {secrets.token_hex(4)}")
        token = new_invite_token()
        _db.create_invite(org, email, "user", token)
        before = _db.get_user(uid)["password_hash"]

        resp = client.post(f"/auth/accept/{token}",
                           data={"password": "attacker chosen password",
                                 "password_confirm": "attacker chosen password"})
        assert resp.status_code == 302
        assert _db.get_user(uid)["password_hash"] == before
        assert _db.get_org_role(org, uid) == "user"
        assert _auth.verify_login(email, GOOD_PASSWORD).ok is True
