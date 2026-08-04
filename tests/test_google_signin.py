"""Google sign-in — engine/oauth.py + routes/auth.py (E1.4).

Authlib owns state, nonce, PKCE and the id_token signature, so the tests
here cover the part it cannot decide: **which local account a set of
Google claims is allowed to become.** That decision is
``engine.oauth.decide``, written as a pure function precisely so it can be
tested exhaustively without a browser or a network.

The single most important case is
``test_an_unverified_email_never_links_to_an_existing_account``. Without
that check, anyone who can add an arbitrary unverified address to a Google
account they control signs in as the TestFortge user who owns it.
"""

import secrets

import pytest

from engine import db as _db
from engine import oauth as _oauth
from routes.auth import new_invite_token


@pytest.fixture(autouse=True)
def _db_ready():
    _db.init_db()


@pytest.fixture(autouse=True)
def _no_google_creds(monkeypatch):
    """Default to unconfigured; tests that need it opt in."""
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)


def _email() -> str:
    return f"g-{secrets.token_hex(6)}@example.com"


def _sub() -> str:
    return f"1{secrets.randbelow(10**18)}"


def _claims(email: str, *, verified: bool = True, sub: str | None = None,
            **extra) -> dict:
    out = {"sub": sub or _sub(), "email": email, "email_verified": verified}
    out.update(extra)
    return out


# ── Configuration ─────────────────────────────────────────────────

class TestConfiguration:
    def test_unconfigured_by_default(self):
        assert _oauth.is_configured() is False

    def test_both_halves_are_required(self, monkeypatch):
        # A client id with no secret would render the button and fail at
        # the callback — worse than not offering it.
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "abc.apps.googleusercontent.com")
        assert _oauth.is_configured() is False
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "shh")
        assert _oauth.is_configured() is True

    def test_scope_asks_for_nothing_extra(self):
        # Every additional scope is data we then become responsible for.
        assert set(_oauth.GOOGLE_SCOPE.split()) == {"openid", "email", "profile"}

    def test_discovery_is_used_rather_than_hardcoded_endpoints(self):
        # So signing-key rotation is handled for us.
        assert _oauth.GOOGLE_METADATA_URL.endswith(
            "/.well-known/openid-configuration")


# ── The decision ──────────────────────────────────────────────────

class TestReturningUser:
    def test_a_known_identity_signs_in(self):
        uid = _db.create_user(_email())
        sub = _sub()
        _db.link_identity(uid, "google", sub)
        decision = _oauth.decide(_claims(_email(), sub=sub))
        assert decision.action == "sign_in"
        assert decision.user_id == uid

    def test_identity_is_keyed_on_sub_not_email(self):
        # Google documents sub as stable and email as changeable. Keying on
        # email means a user who renames their Google account arrives as a
        # stranger and gets refused.
        uid = _db.create_user(_email())
        sub = _sub()
        _db.link_identity(uid, "google", sub)
        renamed = _oauth.decide(_claims("brand-new-address@example.com",
                                        sub=sub))
        assert renamed.action == "sign_in" and renamed.user_id == uid

    def test_a_deactivated_user_is_refused(self):
        uid = _db.create_user(_email())
        sub = _sub()
        _db.link_identity(uid, "google", sub)
        _db.set_user_active(uid, False)
        assert _oauth.decide(_claims(_email(), sub=sub)).action == "refuse"


class TestLinkingByEmail:
    def test_a_verified_address_links_to_an_existing_account(self):
        email = _email()
        uid = _db.create_user(email)
        decision = _oauth.decide(_claims(email))
        assert decision.action == "link" and decision.user_id == uid

    def test_an_unverified_email_never_links_to_an_existing_account(self):
        # THE attack. Google will assert
        #   {email: victim@customer.com, email_verified: false}
        # for an address anyone can add to an account they control. Without
        # this check, that is a password-free login as the victim.
        email = _email()
        _db.create_user(email)
        decision = _oauth.decide(_claims(email, verified=False))
        assert decision.action == "refuse"
        assert decision.reason == "email_not_verified"

    def test_an_unverified_email_is_refused_even_with_an_invite(self):
        # The invite proves someone wanted *that address* on the team; it
        # does not prove this Google account owns it.
        org = _db.create_organization("Team")
        email, token = _email(), new_invite_token()
        _db.create_invite(org, email, "user", token)
        decision = _oauth.decide(_claims(email, verified=False), token)
        assert decision.action == "refuse"

    def test_email_case_does_not_prevent_a_match(self):
        email = _email()
        uid = _db.create_user(email)
        decision = _oauth.decide(_claims(email.upper()))
        assert decision.action == "link" and decision.user_id == uid

    def test_missing_email_claim_is_refused(self):
        decision = _oauth.decide({"sub": _sub(), "email_verified": True})
        assert decision.action == "refuse"
        assert decision.reason == "no_email_claim"

    def test_a_missing_subject_is_refused(self):
        # Should be impossible for a validated id_token, but an empty
        # subject would match the first identity row with an empty
        # subject, so refuse rather than query.
        assert _oauth.decide(_claims(_email(), sub="")).action == "refuse"


class TestInviteOnly:
    def test_a_stranger_with_no_invite_is_refused(self):
        # Google sign-in is a sign-in mechanism, not a side door around the
        # invite-only decision.
        decision = _oauth.decide(_claims(_email()))
        assert decision.action == "refuse"
        assert decision.reason == "no_account_no_invite"

    def test_a_live_invite_provisions_the_account(self):
        org = _db.create_organization("Team")
        email, token = _email(), new_invite_token()
        _db.create_invite(org, email, "user", token)
        decision = _oauth.decide(_claims(email), token)
        assert decision.action == "provision"
        assert decision.email == email
        assert decision.invite_token == token

    def test_an_invite_for_a_different_address_is_refused(self):
        # Otherwise anyone holding a link could join as whoever they liked,
        # simply by signing in with their own Google account.
        org = _db.create_organization("Team")
        token = new_invite_token()
        _db.create_invite(org, "invited@example.com", "admin", token)
        decision = _oauth.decide(_claims("someone-else@example.com"), token)
        assert decision.action == "refuse"
        assert decision.reason == "invite_email_mismatch"

    def test_an_expired_invite_does_not_provision(self):
        from datetime import datetime, timedelta, timezone
        org = _db.create_organization("Team")
        email, token = _email(), new_invite_token()
        _db.create_invite(org, email, "user", token)
        with _db.session_scope() as sess:
            sess.get(_db.Invite, token).expires_at = \
                datetime.now(timezone.utc) - timedelta(minutes=1)
        assert _oauth.decide(_claims(email), token).action == "refuse"

    def test_a_forged_token_does_not_provision(self):
        assert _oauth.decide(_claims(_email()),
                             new_invite_token()).action == "refuse"

    def test_an_existing_account_wins_over_an_invite(self):
        # Link, not provision: creating a second account for an address
        # that already has one would split someone's work in two.
        org = _db.create_organization("Team")
        email = _email()
        uid = _db.create_user(email)
        token = new_invite_token()
        _db.create_invite(org, email, "user", token)
        decision = _oauth.decide(_claims(email), token)
        assert decision.action == "link" and decision.user_id == uid


class TestRefusalMessage:
    def test_one_message_covers_every_refusal(self):
        # The reasons distinguish "no account here" from "your address is
        # unverified", and the first is an account-enumeration oracle that
        # needs no password at all.
        assert "invite-only" in _oauth.GENERIC_REFUSAL.lower()
        for reason in ("no_account_no_invite", "email_not_verified",
                       "invite_email_mismatch"):
            assert reason not in _oauth.GENERIC_REFUSAL


class TestDisplayName:
    def test_a_name_is_taken_from_the_claims(self):
        assert _oauth.display_name_from({"name": "Ada Lovelace"}) == \
            "Ada Lovelace"

    def test_given_name_is_the_fallback(self):
        assert _oauth.display_name_from({"given_name": "Ada"}) == "Ada"

    def test_absent_name_is_none(self):
        assert _oauth.display_name_from({"sub": "1"}) is None

    def test_an_absurd_name_is_truncated(self):
        # The column is 120 chars; a claim is attacker-influenced input.
        assert len(_oauth.display_name_from({"name": "x" * 500})) == 120


# ── The HTTP surface ──────────────────────────────────────────────

class TestRoutes:
    @pytest.fixture(autouse=True)
    def _auth_on(self, monkeypatch):
        monkeypatch.setenv("AUTH_ENABLED", "1")

    def test_the_button_is_hidden_when_unconfigured(self, client):
        body = client.get("/auth/login").data
        assert b"Continue with Google" not in body

    def test_starting_the_flow_is_refused_when_unconfigured(self, client):
        # Rather than 500ing on a missing client id.
        resp = client.get("/auth/google")
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["Location"]

    def test_the_callback_is_inert_when_unconfigured(self, client):
        resp = client.get("/auth/google/callback")
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["Location"]

    def test_a_failed_exchange_does_not_sign_anyone_in(self, client,
                                                       monkeypatch):
        """A mismatched state, a replayed code, or a cancelled consent
        screen all land here, and none of them may authenticate."""
        import routes.auth as _routes_auth

        class _Google:
            @staticmethod
            def authorize_access_token():
                raise ValueError("mismatching_state: CSRF Warning!")

        class _FakeOAuth:
            google = _Google()

        monkeypatch.setattr(_routes_auth._oauth, "build_oauth",
                            lambda app: _FakeOAuth())
        # Rebuild the app so register() picks up the fake.
        from flask import Flask
        app = Flask(__name__, template_folder="../templates",
                    static_folder="../static")
        app.secret_key = "t"
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False

        @app.route("/", endpoint="index")
        def _index():
            return "home"

        _routes_auth.register(app)
        c = app.test_client()
        resp = c.get("/auth/google/callback")
        assert resp.status_code == 302
        assert c.get("/auth/me").get_json()["authenticated"] is False
