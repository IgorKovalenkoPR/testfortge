"""Role enforcement — engine/permissions.py (E2.2).

The failure this file exists to prevent is not "a check is wrong" but "a
check is missing". Eighty routes each carrying their own copy of an
``if`` is how a 403 bypass ships: the one that gets it wrong looks
exactly like the seventy-nine that do not. So the tests cover the central
resolver, and then assert that no *new* protected surface can appear
without a declared policy.
"""

import secrets

import pytest
from flask import Flask

from engine import db as _db
from engine import permissions as _perm


@pytest.fixture(autouse=True)
def _db_ready():
    _db.init_db()


def _email() -> str:
    return f"p-{secrets.token_hex(6)}@example.com"


def _org_with(role: str) -> tuple[str, str]:
    org = _db.create_organization(f"Team {secrets.token_hex(4)}")
    uid = _db.create_user(_email())
    _db.add_org_member(org, uid, role)
    return org, uid


def _probe_app() -> Flask:
    """A tiny app with one route per policy."""
    app = Flask(__name__)
    app.secret_key = "test"

    @app.route("/open")
    def open_route():
        return "open"

    @app.route("/signed-in")
    @_perm.require_login
    def signed_in():
        return "signed in"

    @app.route("/any-member")
    @_perm.require_role("user")
    def any_member():
        return "member"

    @app.route("/admins-only")
    @_perm.require_role("admin")
    def admins_only():
        return "admin"

    # Endpoint the unauthenticated redirect targets.
    @app.route("/auth/login", endpoint="auth_login")
    def login():
        return "login page"

    @app.route("/", endpoint="index")
    def index():
        return "home"

    return app


def _as(client, org: str | None, uid: str | None):
    """Put a user + org into the test client's session."""
    with client.session_transaction() as sess:
        if uid:
            sess[_perm.SESSION_USER_KEY] = uid
        if org:
            sess[_perm.SESSION_ORG_KEY] = org


# ── Staged rollout ────────────────────────────────────────────────

class TestFlagsOff:
    """While the flags are off, nothing changes. This is what lets the
    whole programme sit on main instead of a long-lived branch."""

    def test_protected_routes_are_open_without_auth_enabled(self, monkeypatch):
        monkeypatch.delenv("AUTH_ENABLED", raising=False)
        client = _probe_app().test_client()
        assert client.get("/signed-in").status_code == 200
        assert client.get("/admins-only").status_code == 200

    def test_role_checks_are_inert_without_org_mode(self, monkeypatch):
        monkeypatch.setenv("AUTH_ENABLED", "1")
        monkeypatch.delenv("ORG_MODE", raising=False)
        app = _probe_app()
        client = app.test_client()
        uid = _db.create_user(_email())
        _as(client, None, uid)
        # Signed in but in no organisation: with ORG_MODE off there is
        # nobody to be less than an admin, so the legacy single-tenant
        # behaviour holds.
        assert client.get("/admins-only").status_code == 200

    def test_is_admin_is_true_in_legacy_mode(self, monkeypatch):
        monkeypatch.delenv("ORG_MODE", raising=False)
        app = _probe_app()
        with app.test_request_context("/"):
            assert _perm.is_admin() is True


# ── The gate itself ───────────────────────────────────────────────

@pytest.fixture
def full_auth(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")


class TestUnauthenticated:
    def test_a_browser_is_redirected_to_sign_in(self, full_auth):
        client = _probe_app().test_client()
        resp = client.get("/signed-in")
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["Location"]

    def test_a_fetch_gets_401_json(self, full_auth):
        client = _probe_app().test_client()
        resp = client.get("/signed-in",
                          headers={"Accept": "application/json"})
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "auth_required"

    def test_a_role_route_answers_401_not_403_when_anonymous(self, full_auth):
        # 403 would imply "we know who you are and you may not" — the
        # honest answer to an anonymous caller is "sign in first".
        client = _probe_app().test_client()
        resp = client.get("/admins-only",
                          headers={"Accept": "application/json"})
        assert resp.status_code == 401

    def test_open_routes_stay_open(self, full_auth):
        assert _probe_app().test_client().get("/open").status_code == 200


class TestRoleMatrix:
    @pytest.mark.parametrize("role,path,expected", [
        ("user", "/signed-in", 200),
        ("user", "/any-member", 200),
        ("user", "/admins-only", 403),
        ("admin", "/signed-in", 200),
        ("admin", "/any-member", 200),
        ("admin", "/admins-only", 200),
    ])
    def test_each_role_against_each_route(self, full_auth, role, path, expected):
        app = _probe_app()
        client = app.test_client()
        org, uid = _org_with(role)
        _as(client, org, uid)
        resp = client.get(path, headers={"Accept": "application/json"})
        assert resp.status_code == expected

    def test_a_non_member_is_refused_even_though_signed_in(self, full_auth):
        # None means no access. A caller that defaulted it to "user" would
        # be letting a stranger into somebody else's workspace.
        app = _probe_app()
        client = app.test_client()
        org = _db.create_organization(f"Team {secrets.token_hex(4)}")
        outsider = _db.create_user(_email())
        _as(client, org, outsider)
        resp = client.get("/any-member",
                          headers={"Accept": "application/json"})
        assert resp.status_code == 403

    def test_a_deactivated_user_loses_access_on_the_next_request(self, full_auth):
        # Checked per request rather than trusted from the session, so
        # deactivation takes effect now and not whenever the session
        # happens to expire.
        app = _probe_app()
        client = app.test_client()
        org, uid = _org_with("admin")
        _as(client, org, uid)
        assert client.get("/admins-only",
                          headers={"Accept": "application/json"}
                          ).status_code == 200
        _db.set_user_active(uid, False)
        assert client.get("/admins-only",
                          headers={"Accept": "application/json"}
                          ).status_code == 401

    def test_a_role_change_takes_effect_immediately(self, full_auth):
        app = _probe_app()
        client = app.test_client()
        org, uid = _org_with("user")
        _as(client, org, uid)
        assert client.get("/admins-only",
                          headers={"Accept": "application/json"}
                          ).status_code == 403
        _db.add_org_member(org, uid, "admin")
        assert client.get("/admins-only",
                          headers={"Accept": "application/json"}
                          ).status_code == 200

    def test_the_403_body_names_the_role_needed(self, full_auth):
        app = _probe_app()
        client = app.test_client()
        org, uid = _org_with("user")
        _as(client, org, uid)
        body = client.get("/admins-only",
                          headers={"Accept": "application/json"}).get_json()
        assert body["required_role"] == "admin"

    def test_an_unknown_required_role_fails_closed(self, full_auth):
        # A typo in a decorator must not silently grant access to everyone.
        app = Flask(__name__)
        app.secret_key = "t"

        @app.route("/typo")
        @_perm.require_role("admni")
        def typo():
            return "should never be reachable"

        @app.route("/auth/login", endpoint="auth_login")
        def login():
            return "login"

        client = app.test_client()
        org, uid = _org_with("admin")
        _as(client, org, uid)
        resp = client.get("/typo", headers={"Accept": "application/json"})
        assert resp.status_code == 403


class TestSignOutEverywhere:
    def test_other_sessions_are_dropped(self, full_auth, monkeypatch):
        monkeypatch.setenv("SESSION_BACKEND", "db")
        from engine import server_session

        app = _probe_app()
        server_session.install(app)
        org, uid = _org_with("user")

        # Two devices.
        laptop, phone = app.test_client(), app.test_client()
        for c in (laptop, phone):
            with c.session_transaction() as sess:
                sess[_perm.SESSION_USER_KEY] = uid
                sess[_perm.SESSION_ORG_KEY] = org
            assert c.get("/any-member").status_code == 200

        with app.test_request_context("/"):
            from flask import session
            session[_perm.SESSION_USER_KEY] = uid
            _perm.logout_user(everywhere=True)

        # Both device sessions are gone; a signed-out browser is
        # redirected rather than served.
        assert laptop.get("/any-member").status_code == 302
        assert phone.get("/any-member").status_code == 302


# ── Coverage: no protected surface without a declared policy ──────

class TestEveryRouteIsClassified:
    """The real defence against a missing check.

    Every route on the production app is either declared open here — with
    a reason — or must carry a policy from ``engine.permissions``. A new
    route lands with neither and this test fails, which is the only
    mechanism that scales past the point where a human reviews the whole
    URL map.
    """

    #: Routes that are open on purpose, and why.
    #:
    #: Anything not in this set, and not decorated, fails the test. Adding
    #: an entry here is a deliberate act that shows up in review — which
    #: is the point.
    OPEN_ON_PURPOSE = {
        # Ops probes. Monitors call these without credentials by design;
        # /metrics is separately gated by OPS_ENDPOINTS_TOKEN.
        "healthz", "readyz", "metrics",
        # The authentication surface itself.
        "auth_login", "auth_logout", "auth_accept_invite", "auth_me",
        # Static files.
        "static",
        # Token-authenticated machine endpoints. Each carries its own
        # bearer/token check and is csrf-exempt for that reason; a session
        # role gate would be the wrong control for a caller that has no
        # session at all — the browser extension, CI, the MCP service.
        "api_recorder_session_start", "api_recorder_session_finish",
        "api_browser_poll", "api_browser_result",
        "automation_allure_results",
        # The recorder's review page is reached by a one-time token in the
        # URL, from a browser that may never have signed in.
        "test_cases_review_session", "test_cases_review_session_save",
        # The CSRF token endpoint — needed *before* a caller can post
        # anything at all, including a sign-in.
        "api_csrf_token",
    }

    @staticmethod
    def _production_app():
        from app import app as flask_app
        return flask_app

    def test_the_url_map_is_not_empty(self):
        # A green result because the app failed to build would be the
        # least useful kind of green.
        rules = list(self._production_app().url_map.iter_rules())
        assert len(rules) > 50, f"only {len(rules)} rules — did the app build?"

    def test_no_route_is_both_open_and_protected(self):
        app = self._production_app()
        for endpoint in sorted(self.OPEN_ON_PURPOSE):
            view = app.view_functions.get(endpoint)
            if view is None:
                continue
            assert not hasattr(view, "_required_role"), (
                f"{endpoint} is listed as open on purpose but also carries "
                f"a policy decorator — remove it from OPEN_ON_PURPOSE."
            )

    def test_the_open_list_has_no_stale_entries(self):
        # An endpoint that no longer exists sitting in the allowlist is a
        # hole waiting for someone to reuse the name.
        app = self._production_app()
        stale = sorted(e for e in self.OPEN_ON_PURPOSE
                       if e not in app.view_functions)
        assert not stale, (
            f"OPEN_ON_PURPOSE names endpoints that no longer exist: "
            f"{stale}. Remove them — a stale allowlist entry becomes a "
            f"hole the moment the name is reused."
        )

    @pytest.mark.xfail(
        reason="E2.3 has not run yet: the ~80 pre-existing routes still "
               "carry the owner_sid check rather than a role policy. This "
               "test is the acceptance criterion for that task, and is "
               "expected to fail until it lands — it is xfail rather than "
               "skip so it starts passing loudly the moment it is done.",
        strict=False,
    )
    def test_every_route_declares_a_policy(self):
        app = self._production_app()
        undeclared = []
        for rule in app.url_map.iter_rules():
            endpoint = rule.endpoint
            if endpoint in self.OPEN_ON_PURPOSE:
                continue
            view = app.view_functions.get(endpoint)
            if view is None or not hasattr(view, "_required_role"):
                undeclared.append(endpoint)
        assert not undeclared, (
            f"{len(undeclared)} route(s) carry no access policy: "
            f"{sorted(undeclared)[:12]}… Decorate each with "
            f"@require_login or @require_role(...), or add it to "
            f"OPEN_ON_PURPOSE with a reason."
        )
