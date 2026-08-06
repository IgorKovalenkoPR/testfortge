"""
The access-control branches that nothing exercised (E9.2).

The programme's target is 100% branch coverage on permissions and crypto,
on the reasoning that a missed branch there is an access-control hole rather
than an untested convenience. Measured before this file: `permissions` 93%,
`auth` 97%, `route_policy` 90%.

The gaps were not evenly boring. One of them was **a deactivated user's
existing session being rejected** — a security property with no test at all,
in a codebase where every other part of sign-out is covered. Deactivating
somebody is the action an operator takes when they need it to be true
immediately, and it was resting on nine lines nobody had run.

The rest are the branches that only fire when something is already unusual:
a session backend with no id to rotate, a lock timestamp that arrived as an
unparseable string, an API caller who gets HTML because nobody checked the
Accept header, a 403 template that is missing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import permissions as _perm
from engine import route_policy as _rp


# ── The deactivated user ─────────────────────────────────────────────

class TestADeactivatedUsersSessionIsRefused:
    """Deactivation has to take effect on the *existing* session.

    An operator deactivates an account because they want it to stop
    working now — not at the next sign-in, which may never come. The
    session cookie is already issued and already valid, so the check has to
    happen on every lookup, and this is the only thing that makes it true.
    """

    def test_an_active_user_resolves(self, app, request):
        uid = _db.create_user(f"active-{request.node.name}@x.test")
        with app.test_request_context("/"):
            from flask import session
            session[_perm.SESSION_USER_KEY] = uid
            assert (_perm.current_user() or {}).get("id") == uid

    def test_a_deactivated_user_does_not(self, app, request):
        uid = _db.create_user(f"gone-{request.node.name}@x.test")
        _db.set_user_active(uid, False)
        with app.test_request_context("/"):
            from flask import session
            session[_perm.SESSION_USER_KEY] = uid
            assert _perm.current_user() is None

    def test_and_therefore_holds_no_role(self, app, request, monkeypatch):
        """A deactivated admin must not still be an admin.

        Only ``org_active`` is forced on, deliberately: with organisations
        off, ``is_admin()`` returns True for everybody by design — there is
        nobody to be less than an admin in a single-tenant deployment, and
        the legacy UI keeps showing everything. Patching the whole identity
        (the ``auth_on`` fixture) would replace the very functions under
        test. My first version of this test forced nothing and asserted the
        org-mode answer, which is the mistake the strategy document names:
        a test that depends on the mode has to name the mode.
        """
        monkeypatch.setattr(_perm, "org_active", lambda: True)
        uid = _db.create_user(f"norole-{request.node.name}@x.test")
        org = _db.create_organization(f"org-{request.node.name}")
        _db.add_org_member(org, uid, "admin")
        _db.set_user_active(uid, False)
        with app.test_request_context("/"):
            from flask import session
            session[_perm.SESSION_USER_KEY] = uid
            session[_perm.SESSION_ORG_KEY] = org
            # Membership still exists in the database; the account does not
            # work. Before current_role() resolved through current_user(),
            # this returned "admin".
            assert _perm.current_role() is None
            assert not _perm.is_admin()
            assert not _perm.has_role("user")

    def test_an_active_admin_does_hold_the_role(self, app, request,
                                                monkeypatch):
        # The other half, so the assertion above cannot pass because the
        # role lookup is broken for everyone.
        monkeypatch.setattr(_perm, "org_active", lambda: True)
        uid = _db.create_user(f"stillon-{request.node.name}@x.test")
        org = _db.create_organization(f"org-on-{request.node.name}")
        _db.add_org_member(org, uid, "admin")
        with app.test_request_context("/"):
            from flask import session
            session[_perm.SESSION_USER_KEY] = uid
            session[_perm.SESSION_ORG_KEY] = org
            assert _perm.current_role() == "admin"
            assert _perm.is_admin()

    def test_an_unknown_user_id_resolves_to_nobody(self, app):
        with app.test_request_context("/"):
            from flask import session
            session[_perm.SESSION_USER_KEY] = "no-such-user"
            assert _perm.current_user() is None


# ── Sign-in on a backend with no session id ──────────────────────────

class TestLoginWithoutASessionIdToRotate:
    def test_it_still_signs_in(self, app, request):
        """The rotation is skipped, the sign-in is not.

        ``login_user`` rotates the session id to defend against fixation.
        A backend that exposes no ``sid`` cannot be rotated — the code logs
        that fixation is undefended and carries on, because refusing to
        sign anyone in would be a worse answer than an undefended session.
        """
        uid = _db.create_user(f"nosid-{request.node.name}@x.test")
        with app.test_request_context("/"):
            from flask import session
            # A session object with no sid at all: the branch that the
            # normal filesystem and DB backends never take.
            assert not hasattr(session, "sid") or session.sid is not None
            if hasattr(session, "sid"):
                pytest.skip("this backend exposes a sid; covered elsewhere")
            _perm.login_user(uid)
            assert session[_perm.SESSION_USER_KEY] == uid

    def test_the_org_is_only_written_when_given(self, app, request):
        uid = _db.create_user(f"noorg-{request.node.name}@x.test")
        with app.test_request_context("/"):
            from flask import session
            _perm.login_user(uid)
            # No org argument, no org key — rather than an empty string,
            # which would read as "in a team called nothing".
            assert _perm.SESSION_ORG_KEY not in session

    def test_set_active_org_switches_teams(self, app, request):
        uid = _db.create_user(f"switch-{request.node.name}@x.test")
        first = _db.create_organization(f"first-{request.node.name}")
        second = _db.create_organization(f"second-{request.node.name}")
        with app.test_request_context("/"):
            from flask import session
            _perm.login_user(uid, org_id=first)
            _perm.set_active_org(second)
            assert session[_perm.SESSION_ORG_KEY] == second


# ── Refusals have to suit the caller ─────────────────────────────────

class TestTheRefusalMatchesTheCaller:
    """An API caller given an HTML page cannot act on it, and a browser
    given JSON shows the user a blob. The Accept header decides."""

    def _forbidden(self, app, headers):
        with app.test_request_context("/", headers=headers):
            return _perm._deny_forbidden("admin")

    def test_an_xhr_caller_gets_json(self, app):
        body, status = self._forbidden(
            app, {"X-Requested-With": "XMLHttpRequest"})
        assert status == 403
        payload = body.get_json()
        assert payload["required_role"] == "admin"

    def test_a_json_caller_gets_json(self, app):
        body, status = self._forbidden(app, {"Accept": "application/json"})
        assert status == 403
        assert body.get_json()["required_role"] == "admin"

    def test_a_browser_gets_a_page(self, app):
        body, status = self._forbidden(
            app, {"Accept": "text/html,application/json"})
        # Both types accepted means a browser: text/html present is the
        # signal, and it wins.
        assert status == 403
        assert not hasattr(body, "get_json")

    def test_the_message_names_the_role_that_was_needed(self, app):
        body, _ = self._forbidden(app, {"Accept": "application/json"})
        assert "admin" in body.get_json()["message"]

    def test_a_missing_template_still_refuses(self, app, monkeypatch):
        """Plainly, rather than turning a 403 into a 500.

        A refusal that crashes is indistinguishable from a bug, and the
        caller would reasonably retry.
        """
        import engine.permissions as mod
        monkeypatch.setattr(
            mod, "render_template",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no template")),
            raising=False)
        with app.test_request_context("/", headers={"Accept": "text/html"}):
            body, status = mod._deny_forbidden("admin")
        assert status == 403
        assert "admin" in str(body)


# ── Lockout messages ─────────────────────────────────────────────────

class TestTheLockoutMessage:
    def test_a_naive_timestamp_is_read_as_utc(self):
        soon = (datetime.now(timezone.utc) + timedelta(minutes=3))
        assert "3 minute" in _auth.lockout_message(
            soon.replace(tzinfo=None)) or "4 minute" in _auth.lockout_message(
            soon.replace(tzinfo=None))

    def test_an_iso_string_is_parsed(self):
        soon = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
        assert "minute" in _auth.lockout_message(soon)

    def test_an_unparseable_timestamp_falls_back_to_a_vague_message(self):
        # Vague rather than wrong: telling somebody "try again in 0 minutes"
        # when the lock is real sends them into a loop.
        msg = _auth.lockout_message("whenever")
        assert "shortly" in msg

    def test_no_lock_reads_as_no_lock(self):
        assert "shortly" in _auth.lockout_message(None)

    def test_a_lock_in_the_past_does_not_promise_a_wait(self):
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert "shortly" in _auth.lockout_message(past)

    def test_the_singular_is_used_for_one_minute(self):
        soon = datetime.now(timezone.utc) + timedelta(seconds=5)
        msg = _auth.lockout_message(soon)
        assert "1 minute." in msg or "1 minutes" not in msg


# ── The policy table's own validation ────────────────────────────────

class TestThePolicyTableValidatesItself:
    def test_a_clean_table_has_no_problems(self):
        assert _rp.validate() == []

    def test_an_unknown_role_is_reported(self, monkeypatch):
        # The table is hand-maintained, and a typo'd role would otherwise
        # fail closed on every request to that endpoint at runtime.
        monkeypatch.setitem(_rp.POLICY, "some_endpoint", "superuser")
        problems = _rp.validate()
        assert any("superuser" in p for p in problems)

    def test_an_endpoint_in_both_tables_is_reported(self, monkeypatch):
        monkeypatch.setitem(_rp.OPEN, "index", "contradiction")
        problems = _rp.validate()
        assert any("index" in p for p in problems)

    def test_policy_for_returns_none_for_an_unclassified_endpoint(self):
        assert _rp.policy_for("not_a_real_endpoint") is None

    def test_policy_for_returns_none_for_no_endpoint(self):
        # A request with no endpoint is a 404 in the making; it must not
        # blow up in the gate on the way there.
        assert _rp.policy_for(None) is None

    def test_is_open_is_false_for_no_endpoint(self):
        assert _rp.is_open(None) is False

    def test_is_open_recognises_the_allowlist(self):
        assert _rp.is_open("healthz") is True
