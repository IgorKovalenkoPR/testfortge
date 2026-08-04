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

    Every route on the production app is either open on purpose (with a
    reason recorded in ``engine.route_policy.OPEN``), gated by the policy
    table, or self-enforcing via a decorator. A new route lands with none
    of the three and this fails — the only mechanism that scales past the
    point where a human reviews an 88-entry URL map.
    """

    @staticmethod
    def _production_app():
        from app import app as flask_app
        return flask_app

    @staticmethod
    def _policy():
        from engine import route_policy
        return route_policy

    def test_the_url_map_is_not_empty(self):
        # A green result because the app failed to build would be the
        # least useful kind of green.
        rules = list(self._production_app().url_map.iter_rules())
        assert len(rules) > 50, f"only {len(rules)} rules — did the app build?"

    def test_the_policy_table_is_internally_consistent(self):
        # An endpoint in both tables, or a role that does not exist, would
        # resolve by lookup order rather than by intent.
        assert self._policy().validate() == []

    def test_no_route_is_both_open_and_self_enforcing(self):
        app = self._production_app()
        for endpoint in sorted(self._policy().OPEN):
            view = app.view_functions.get(endpoint)
            if view is None:
                continue
            assert not hasattr(view, "_required_role"), (
                f"{endpoint} is listed as open on purpose but also carries "
                f"a policy decorator — one of the two is a mistake."
            )

    def test_neither_table_has_stale_entries(self):
        # An endpoint that no longer exists becomes a hole the moment the
        # name is reused.
        app = self._production_app()
        policy = self._policy()
        for name, table in (("OPEN", policy.OPEN), ("POLICY", policy.POLICY)):
            stale = sorted(e for e in table if e not in app.view_functions)
            assert not stale, (
                f"engine/route_policy.{name} names endpoints that no longer "
                f"exist: {stale}. Remove them."
            )

    def test_every_open_entry_carries_a_reason(self):
        # A bare allowlist accretes entries nobody can justify or remove.
        for endpoint, reason in self._policy().OPEN.items():
            assert len(reason) > 15, f"{endpoint} has a token reason"

    def test_every_route_declares_a_policy(self):
        """The acceptance criterion for E2.3."""
        undeclared = self._policy().unclassified(self._production_app())
        assert not undeclared, (
            f"{len(undeclared)} route(s) carry no access policy: "
            f"{undeclared[:12]}… Add each to engine/route_policy.POLICY "
            f"or OPEN (with a reason), or decorate it with @require_login "
            f"/ @require_role(...)."
        )

    def test_project_creation_is_admin_only(self):
        # The owner's requirement 2, asserted against the table rather than
        # trusted to a reading of it.
        policy = self._policy().POLICY
        for endpoint in ("db_create_project", "db_rename_project",
                         "delete_project", "db_move_artifacts"):
            assert policy[endpoint] == "admin", endpoint

    def test_saving_current_work_is_not_treated_as_creating_a_project(self):
        # save_project upserts, so it *can* create a row — but it is the
        # "Save current work" button, and while the Flask session is still
        # the source of truth (ADR 0001) gating it would mean a plain
        # user's test cases are lost on the next dyno restart. Requirement
        # 2 withholds creating projects, which is db_create_project above.
        assert self._policy().POLICY["save_project"] == "user"

    def test_doing_the_qa_work_does_not_need_admin(self):
        # The other half of requirement 2: a user works with everything
        # except project creation and configuration.
        policy = self._policy().POLICY
        for endpoint in ("test_cases_page", "checklist_page",
                         "test_execution_page", "bug_reports_page",
                         "estimation_page", "create_bug_report",
                         "manual_run_verdict", "automation_run",
                         "chat_route", "db_select_project"):
            assert policy[endpoint] == "user", endpoint

    def test_the_shell_is_reachable_before_joining_a_team(self):
        # A user whose organisation was deleted must be able to reach the
        # page that explains it, not a 403 on every URL including that one.
        policy = self._policy().POLICY
        assert policy["index"] == "login"
        assert policy["guide_page"] == "login"


class TestUnclassifiedRoutesFailClosed:
    def test_an_unclassified_route_is_refused_rather_than_open(self, full_auth):
        """The direction a mistake fails in.

        A new route that nobody classified must be unreachable, not
        public — the opposite of the usual accident. The coverage test
        above should mean this is never reached in production, but the two
        together are what make "we forgot one" harmless.
        """
        from engine import route_policy

        app = Flask(__name__)
        app.secret_key = "t"

        @app.route("/brand-new-feature")
        def brand_new_feature():
            return "should not be reachable"

        @app.route("/auth/login", endpoint="auth_login")
        def login():
            return "login"

        @app.route("/", endpoint="index")
        def index():
            return "home"

        route_policy.install(app)
        client = app.test_client()
        org, uid = _org_with("admin")
        _as(client, org, uid)
        resp = client.get("/brand-new-feature",
                          headers={"Accept": "application/json"})
        assert resp.status_code == 403

    def test_the_hook_is_inert_while_auth_is_off(self, monkeypatch):
        monkeypatch.delenv("AUTH_ENABLED", raising=False)
        from engine import route_policy

        app = Flask(__name__)
        app.secret_key = "t"

        @app.route("/brand-new-feature")
        def brand_new_feature():
            return "reachable"

        route_policy.install(app)
        assert app.test_client().get("/brand-new-feature").status_code == 200

    def test_an_inconsistent_table_refuses_to_boot(self, monkeypatch):
        # Serving requests under a policy nobody can predict is worse than
        # not serving them.
        from engine import route_policy

        monkeypatch.setitem(route_policy.POLICY, "healthz", "admin")
        app = Flask(__name__)
        app.secret_key = "t"
        with pytest.raises(route_policy.PolicyError):
            route_policy.install(app)

    def test_a_missing_endpoint_does_not_leak_that_it_is_missing(self, full_auth):
        # A 401 on an unknown URL would tell an anonymous prober which
        # paths exist.
        from engine import route_policy

        app = Flask(__name__)
        app.secret_key = "t"

        @app.route("/auth/login", endpoint="auth_login")
        def login():
            return "login"

        route_policy.install(app)
        assert app.test_client().get("/no-such-path").status_code == 404
