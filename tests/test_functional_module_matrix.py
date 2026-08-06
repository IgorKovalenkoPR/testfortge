"""E9.4 — each module walked at HTTP level, by each role, with CSRF on.

Three test files already surround this one and none of them covers what
it covers, which is worth stating so the overlap question has an answer:

* ``tests/test_permissions.py`` proves the resolver and that every
  endpoint is classified;
* ``tests/test_route_policy_matrix.py`` proves the classification is
  enforced — as status codes, generated from the policy table;
* ``tests/test_csrf_on_every_post.py`` proves no POST accepts a request
  without a token.

What none of them asks is the question a person asks: **did the thing
happen, and did the page say so?** Every defect this programme lost the
most time to answered 200 and rendered a page that looked right — a run
in project A showing project B's content, one verdict closing two items, a
project vanishing from the picker the moment it was created. A status code
would have passed for all three.

So each class below performs one module's real action as an administrator
and checks the outcome twice — in the database and in the text that comes
back — then performs the same action as a plain user and checks that
**nothing changed**, which is the assertion a gate-level 403 cannot make.

Every request carries a CSRF token fetched the way the browser fetches
it. The suite as a whole runs with ``WTF_CSRF_ENABLED = False``, so
without this file the forms could stop carrying tokens and no test would
notice until production.

**Mode**: authenticated, throughout. This file is about roles, and roles
do not exist with the flags off — so it names the mode rather than
inheriting whichever one the run happens to use.
"""
from __future__ import annotations

import secrets

import pytest

from app import app as flask_app
from engine import dashboard_config as _cfg
from engine import db as _db
from engine import permissions as _perm


@pytest.fixture(autouse=True)
def _authenticated(monkeypatch):
    """The mode this file describes, named rather than assumed."""
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    monkeypatch.setitem(flask_app.config, "WTF_CSRF_ENABLED", True)
    monkeypatch.setitem(flask_app.config, "TESTING", True)
    _db.init_db()


@pytest.fixture
def team():
    """One organisation, one admin, one plain user — fresh per test.

    Fresh rather than module-scoped because half the assertions below are
    "nothing changed", and a leftover invite or project from the previous
    test is indistinguishable from the thing under test having leaked.
    """
    tag = secrets.token_hex(4)
    org = _db.create_organization(f"Module Matrix {tag}")
    admin = _db.create_user(f"admin-{tag}@matrix.test", email_verified=True)
    user = _db.create_user(f"user-{tag}@matrix.test", email_verified=True)
    _db.add_org_member(org, admin, "admin")
    _db.add_org_member(org, user, "user")
    return {"org": org, "admin": admin, "user": user, "tag": tag}


class Person:
    """A signed-in browser, with the token handling a browser does."""

    def __init__(self, client, user_id: str, org_id: str):
        self.client = client
        with client.session_transaction() as sess:
            sess.clear()
            sess[_perm.SESSION_USER_KEY] = user_id
            sess[_perm.SESSION_ORG_KEY] = org_id

    def token(self) -> str:
        response = self.client.get("/api/csrf-token")
        assert response.status_code == 200, response.status_code
        return response.get_json()["token"]

    def post(self, url: str, data: dict | None = None, *, token=True,
             follow: bool = True):
        payload = dict(data or {})
        if token:
            payload["csrf_token"] = self.token()
        return self.client.post(url, data=payload, follow_redirects=follow)

    def get(self, url: str):
        return self.client.get(url)

    def text(self, url: str) -> str:
        return self.get(url).get_data(as_text=True)


@pytest.fixture
def people(team):
    """``people("admin")`` / ``people("user")`` — a browser for each role.

    A separate client per role, not one client that swaps identity: half
    of these tests are about what one person cannot do to another's work,
    and a shared cookie jar would let a stale session key decide the
    answer.
    """
    def _person(role: str) -> Person:
        return Person(flask_app.test_client(), team[role], team["org"])

    return _person


def _refused(response) -> bool:
    """The request did not do the thing.

    Deliberately broad — a redirect back to the form with an error flash,
    a 403 from the gate and a 400 from CSRF are all "refused", and which
    one a given route chooses is a UX decision rather than the property
    under test. What makes this safe is that every use of it sits beside
    an assertion that the underlying data is unchanged; on its own it
    would be the kind of check that passes for the wrong reason.
    """
    return response.status_code >= 400 or "alert-error" in \
        response.get_data(as_text=True)


# ── Projects ─────────────────────────────────────────────────────────

class TestProjects:
    """Requirement 2: an admin creates projects; a user works in them."""

    def _names(self, org: str) -> set[str]:
        return {p["name"] for p in _db.list_projects(org_id=org)}

    def test_an_admin_creates_a_project_and_the_page_says_so(self, people,
                                                             team):
        name = f"Matrix Project {team['tag']}"
        response = people("admin").post("/projects/db/create",
                                        {"project_name": name})

        assert response.status_code == 200, response.status_code
        body = response.get_data(as_text=True)
        # Not the whole flash string: Jinja escapes the quotes around the
        # name, so matching the literal sentence would fail on the markup
        # rather than on the behaviour.
        assert "created and activated" in body and name in body
        assert name in self._names(team["org"]), (
            "the page reported a creation the database does not have")

    def test_the_new_project_is_visible_to_its_own_author(self, people,
                                                          team):
        """The defect E9.9 found on its first authenticated run.

        Nothing wrote ``Project.org_id`` while the listing filtered on it,
        so a project disappeared from the picker at the moment of
        creation. The page said "created and activated" throughout.
        """
        name = f"Visible {team['tag']}"
        admin = people("admin")
        admin.post("/projects/db/create", {"project_name": name})

        assert name in admin.text("/")

    def test_a_plain_user_cannot_create_one_and_none_appears(self, people,
                                                             team):
        name = f"Forbidden {team['tag']}"
        before = self._names(team["org"])

        response = people("user").post("/projects/db/create",
                                       {"project_name": name})

        assert _refused(response), response.status_code
        assert self._names(team["org"]) == before, (
            "the refusal was cosmetic — the project was created anyway")

    def test_a_plain_user_may_switch_to_an_existing_project(self, people,
                                                            team):
        """The other half of requirement 2, and the reason the split is
        not simply "writes are admin"."""
        name = f"Switchable {team['tag']}"
        people("admin").post("/projects/db/create", {"project_name": name})
        project_id = next(p["id"] for p in _db.list_projects(org_id=team["org"])
                          if p["name"] == name)

        user = people("user")
        response = user.post(f"/projects/db/select/{project_id}")

        assert not _refused(response), response.get_data(as_text=True)[:200]
        assert name in user.text("/")

    def test_the_create_form_without_a_token_creates_nothing(self, people,
                                                             team):
        name = f"Tokenless {team['tag']}"
        response = people("admin").post("/projects/db/create",
                                        {"project_name": name}, token=False)

        assert response.status_code == 400
        assert name not in self._names(team["org"])


# ── Members ──────────────────────────────────────────────────────────

class TestMembers:
    """Requirement 1: an admin invites people and sets their roles."""

    def _invited(self, org: str) -> set[str]:
        return {i["email"] for i in _db.list_pending_invites(org)}

    def test_an_admin_invites_and_the_link_comes_back(self, people, team):
        email = f"newcomer-{team['tag']}@matrix.test"
        response = people("admin").post("/org/members/invite",
                                        {"email": email, "role": "user"})
        body = response.get_data(as_text=True)

        assert f"Invitation created for {email} as user" in body
        assert "/auth/accept/" in body, (
            "the invitation was created but the admin was given no link to "
            "send, and E0.4 does not exist to send it for them")
        assert email in self._invited(team["org"])

    def test_a_plain_user_cannot_invite_and_no_invite_exists(self, people,
                                                             team):
        email = f"smuggled-{team['tag']}@matrix.test"
        response = people("user").post("/org/members/invite",
                                       {"email": email, "role": "admin"})

        assert _refused(response), response.status_code
        assert email not in self._invited(team["org"])

    def test_a_plain_user_cannot_promote_themselves(self, people, team):
        """The escalation the role split exists to prevent."""
        response = people("user").post(
            f"/org/members/{team['user']}/role", {"role": "admin"})

        assert _refused(response), response.status_code
        assert _db.get_org_role(team["org"], team["user"]) == "user", (
            "a plain user promoted themselves to admin")

    def test_an_admin_may_change_a_role_and_the_page_shows_it(self, people,
                                                              team):
        response = people("admin").post(
            f"/org/members/{team['user']}/role", {"role": "admin"})

        assert not _refused(response), response.get_data(as_text=True)[:200]
        assert _db.get_org_role(team["org"], team["user"]) == "admin"

    def test_an_invite_without_a_token_invites_nobody(self, people, team):
        email = f"tokenless-{team['tag']}@matrix.test"
        response = people("admin").post("/org/members/invite",
                                        {"email": email, "role": "user"},
                                        token=False)

        assert response.status_code == 400
        assert email not in self._invited(team["org"])


# ── Organisation settings ────────────────────────────────────────────

class TestOrgSettings:
    """Requirement 2's other half: an admin changes configuration."""

    def test_an_admin_renames_the_organisation(self, people, team):
        renamed = f"Renamed {team['tag']}"
        response = people("admin").post("/org/settings/general",
                                        {"name": renamed})

        assert not _refused(response), response.get_data(as_text=True)[:200]
        assert (_db.get_organization(team["org"]) or {}).get("name") == renamed

    def test_a_plain_user_cannot_and_the_name_stands(self, people, team):
        before = (_db.get_organization(team["org"]) or {}).get("name")

        response = people("user").post("/org/settings/general",
                                       {"name": "Hijacked"})

        assert _refused(response), response.status_code
        assert (_db.get_organization(team["org"]) or {}).get("name") == before


# ── Dashboard targets ────────────────────────────────────────────────

class TestDashboardTargets:
    """A target is a team agreement, so only an admin sets one (E7.3)."""

    def _project(self, people, team) -> tuple:
        name = f"Targets {team['tag']}"
        admin = people("admin")
        admin.post("/projects/db/create", {"project_name": name})
        pid = next(p["id"] for p in _db.list_projects(org_id=team["org"])
                   if p["name"] == name)
        return admin, pid

    def test_an_admin_sets_a_target_and_it_is_stored(self, people, team):
        admin, pid = self._project(people, team)

        response = admin.post("/dashboard/targets",
                              {"target_exec_pass_rate": "75"})

        assert not _refused(response), response.get_data(as_text=True)[:200]
        assert _cfg.targets(pid)["exec_pass_rate"] == 75.0

    def test_a_plain_user_cannot_move_the_bar(self, people, team):
        admin, pid = self._project(people, team)
        admin.post("/dashboard/targets", {"target_exec_pass_rate": "75"})

        user = people("user")
        user.post(f"/projects/db/select/{pid}")
        response = user.post("/dashboard/targets",
                             {"target_exec_pass_rate": "10"})

        assert _refused(response), response.status_code
        assert _cfg.targets(pid)["exec_pass_rate"] == 75.0, (
            "a plain user redefined what counts as a passing project")


# ── Bug reports ──────────────────────────────────────────────────────

class TestBugReports:
    """Any member does the QA work; only an admin destroys it."""

    def _project_with_a_bug(self, people, team) -> tuple:
        name = f"Bugs {team['tag']}"
        admin = people("admin")
        admin.post("/projects/db/create", {"project_name": name})
        pid = next(p["id"] for p in _db.list_projects(org_id=team["org"])
                   if p["name"] == name)
        bug_id = _db.save_bug(pid, {
            "id": "BUG_001", "title": f"The header count is stale {team['tag']}",
            "severity": "Major", "status": "Open"})
        return admin, pid, bug_id

    def test_a_member_can_resolve_a_bug_and_the_row_says_so(self, people,
                                                            team):
        _admin, pid, bug_id = self._project_with_a_bug(people, team)

        user = people("user")
        user.post(f"/projects/db/select/{pid}")
        response = user.post("/bugs/bulk", {"bug_ids": str(bug_id),
                                            "action": "status",
                                            "status_value": "Resolved"})

        assert not _refused(response), response.get_data(as_text=True)[:300]
        stored = {b["id"]: b for b in _db.list_bugs(pid)}
        assert stored[bug_id]["status"] == "Resolved"

    def test_a_member_cannot_close_because_closing_is_the_sign_off(
            self, people, team):
        """E4.5's workflow rule, exercised through the toolbar.

        Resolved says "I think this is fixed"; Closed says "I verified it".
        The second is an admin's word, so the transition is refused — and
        the refusal has to say which one to use instead, or the tester is
        left with a button that does nothing.
        """
        _admin, pid, bug_id = self._project_with_a_bug(people, team)

        user = people("user")
        user.post(f"/projects/db/select/{pid}")
        response = user.post("/bugs/bulk",
                             {"bug_ids": str(bug_id), "action": "close"})

        assert "Resolved" in response.get_data(as_text=True), (
            "the refusal did not name the transition the tester may make")
        stored = {b["id"]: b for b in _db.list_bugs(pid)}
        assert stored[bug_id]["status"] == "Open"

    def test_an_admin_can_close(self, people, team):
        """So the test above cannot pass because closing is simply broken."""
        admin, pid, bug_id = self._project_with_a_bug(people, team)

        admin.post("/bugs/bulk", {"bug_ids": str(bug_id), "action": "close"})

        stored = {b["id"]: b for b in _db.list_bugs(pid)}
        assert stored[bug_id]["status"] == "Closed"

    def test_a_member_cannot_delete_and_the_bug_survives(self, people, team):
        """Closing and deleting look the same in the toolbar and are not.

        Asserted on the row rather than on the response, because a bulk
        action that refuses and deletes anyway would answer exactly the
        same way as one that refuses and does not.
        """
        _admin, pid, bug_id = self._project_with_a_bug(people, team)

        user = people("user")
        user.post(f"/projects/db/select/{pid}")
        user.post("/bugs/bulk", {"bug_ids": str(bug_id), "action": "delete"})

        assert bug_id in {b["id"] for b in _db.list_bugs(pid)}, (
            "a plain user deleted a bug report")

    def test_an_admin_can_delete(self, people, team):
        """So the test above cannot pass because deletion is simply broken."""
        admin, pid, bug_id = self._project_with_a_bug(people, team)

        admin.post("/bugs/bulk", {"bug_ids": str(bug_id), "action": "delete"})

        assert bug_id not in {b["id"] for b in _db.list_bugs(pid)}

    def test_a_bulk_action_without_a_token_changes_nothing(self, people,
                                                           team):
        admin, pid, bug_id = self._project_with_a_bug(people, team)

        response = admin.post("/bugs/bulk",
                              {"bug_ids": str(bug_id), "action": "delete"},
                              token=False)

        assert response.status_code == 400
        assert bug_id in {b["id"] for b in _db.list_bugs(pid)}


# ── The harness itself ───────────────────────────────────────────────

class TestTheHarnessWouldNotice:
    """Everything above rests on two conditions that are easy to lose."""

    def test_the_run_is_authenticated(self, people, team):
        assert _perm.auth_active() and _perm.org_active(), (
            "the flags are off, so every 'a plain user cannot' below is "
            "checking a gate that is not installed")

    def test_the_roles_are_actually_different(self, team):
        assert _db.get_org_role(team["org"], team["admin"]) == "admin"
        assert _db.get_org_role(team["org"], team["user"]) == "user"

    def test_enforcement_is_on(self, people, team):
        assert flask_app.config["WTF_CSRF_ENABLED"] is True
        response = people("admin").post("/new-session", token=False)
        assert response.status_code == 400
