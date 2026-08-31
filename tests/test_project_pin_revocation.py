"""A pinned project id was trusted forever; the access gate was not.

Found by walking, not by testing. ``routes.projects._require_project_owner``
re-reads membership on *every* request — that is what makes a project-scoped
form route refuse a stranger. ``session["project_id"]`` was checked once, by
the route that set it, and every surface downstream treated it as authority
from then on. So the two surfaces answered the same question differently the
moment a membership changed, and the difference was not academic: an admin
removed a colleague from the team, the flash said "Removed from the team",
and the removed colleague could still read, rewrite and **delete** that
team's test cases through ``/api/edit/*`` and see them on ``/test-cases``.

It was reachable through the product's own routes, and the reason is a
second, separate gap: ``routes/members.py`` ends the removed person's
sessions, which would have closed the door — except that
``delete_sessions_for_user`` deleted ``ServerSession`` rows, and those exist
only under ``SESSION_BACKEND=db``, while production runs ``filesystem``. So
the sessions lived on. That half is fixed and covered in
``tests/test_session_revocation.py``, and the two fixes are now independent
guards on one door.

Which is why the tests below revoke the membership **directly** rather than
through ``/org/members/<id>/remove``: with the session guard also in place
that route signs her out entirely, and a 302 to the sign-in page would pass
whether the pin was re-checked or not — the test would then be measuring the
other fix. ``test_the_route_refuses_her`` walks the real route and asserts
only the end state, which is what a reader wants to know; everything else
isolates this guard by taking away her *project* access while her session
stays valid. That is not a contrived state, either: it is what any future
revocation path that does not end sessions will look like.

The scenario needs a caller who is still *somebody's* member, because
``require_role`` reads the role in the **active** organisation and a caller
with no team at all is already refused by it. Alice is in two teams and
picks a project in the second — which the gate allows, because membership in
the project's organisation is exactly what it asks for.
"""
import secrets

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import permissions as _perm


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("EDITORS_ENABLED", "1")
    _db.init_db()


SECRET = "SECRET-SUMMARY-XYZ"
SECRET_BUG = "SECRET-BUG-TITLE-XYZ"


@pytest.fixture
def two_teams():
    """Alice in both teams, a project and a test case in the second."""
    pwd = _auth.hash_password("a perfectly good passphrase")
    mine = _db.create_organization(f"Mine {secrets.token_hex(4)}")
    theirs = _db.create_organization(f"Theirs {secrets.token_hex(4)}")
    alice = _db.create_user(f"a-{secrets.token_hex(5)}@example.com",
                            password_hash=pwd)
    boss = _db.create_user(f"b-{secrets.token_hex(5)}@example.com",
                           password_hash=pwd)
    _db.add_org_member(mine, alice, "user")
    _db.add_org_member(theirs, alice, "user")
    _db.add_org_member(theirs, boss, "admin")
    pid = _db.upsert_project(name=f"P-{secrets.token_hex(4)}", org_id=theirs)
    _db.save_test_cases(pid, [{"id": "TC-001", "title": "Their case",
                               "summary": SECRET, "test_steps": "1. do it"}])
    _db.save_bug(pid, {"bug_id": "BUG-001", "title": SECRET_BUG,
                       "severity": "Major", "priority": "High",
                       "status": "Open"})
    return {"mine": mine, "theirs": theirs, "alice": alice, "boss": boss,
            "pid": pid}


def _alice(app, two_teams):
    """Alice, signed into her own team, with the other team's project picked
    through the product's own route — so the pin is one the gate approved."""
    c = app.test_client()
    with c.session_transaction() as sess:
        sess[_perm.SESSION_USER_KEY] = two_teams["alice"]
        sess[_perm.SESSION_ORG_KEY] = two_teams["mine"]
    pid = two_teams["pid"]
    assert c.post("/projects/db/select/" + pid).status_code == 302
    with c.session_transaction() as sess:
        assert sess.get("project_id") == pid, (
            "the premise of every test below: the pin was accepted while "
            "she was a member")
    return c


def _remove_her(app, two_teams):
    """Take away her membership of the other team, and nothing else.

    Not the route — see the module docstring. The route additionally ends her
    session, which would refuse her for the other fix's reason and leave this
    one unmeasured.
    """
    assert _db.remove_org_member(two_teams["theirs"], two_teams["alice"])
    assert _db.get_org_role(two_teams["theirs"], two_teams["alice"]) is None


def _remove_her_through_the_route(app, two_teams):
    """What an admin actually clicks."""
    c = app.test_client()
    with c.session_transaction() as sess:
        sess[_perm.SESSION_USER_KEY] = two_teams["boss"]
        sess[_perm.SESSION_ORG_KEY] = two_teams["theirs"]
    url = "/org/members/" + two_teams["alice"] + "/remove"
    assert c.post(url).status_code == 302
    assert _db.get_org_role(two_teams["theirs"], two_teams["alice"]) is None


class TestWhileStillAMember:
    """The control. Everything below has to keep working for a colleague."""

    def test_she_can_read_it(self, app, two_teams):
        c = _alice(app, two_teams)
        r = c.get("/api/edit/test_case/TC-001")
        assert r.status_code == 200
        assert r.get_json()["item"]["summary"] == SECRET

    def test_she_can_edit_it(self, app, two_teams):
        c = _alice(app, two_teams)
        r = c.patch("/api/edit/test_case/TC-001",
                    json={"changes": {"summary": "Verify a colleague edits"}})
        assert r.status_code == 200
        assert [row["summary"]
                for row in _db.load_test_cases(two_teams["pid"])] == \
            ["Verify a colleague edits"]

    def test_the_page_shows_it(self, app, two_teams):
        c = _alice(app, two_teams)
        assert SECRET in c.get("/test-cases").get_data(as_text=True)


class TestAfterSheIsRemoved:
    """The defect. Each operation, separately, because each was reachable.

    404 rather than 403 on purpose, and not for tidiness: the handler says so
    itself — the same answer whether the row is missing or belongs to another
    project, because distinguishing them confirms an id exists somewhere the
    caller cannot see.
    """

    def test_she_cannot_read_it(self, app, two_teams):
        c = _alice(app, two_teams)
        _remove_her(app, two_teams)
        r = c.get("/api/edit/test_case/TC-001")
        assert r.status_code == 404
        assert SECRET not in r.get_data(as_text=True)

    def test_she_cannot_rewrite_it(self, app, two_teams):
        c = _alice(app, two_teams)
        _remove_her(app, two_teams)
        r = c.patch("/api/edit/test_case/TC-001",
                    json={"changes": {"summary": "Verify a stranger edits"}})
        assert r.status_code == 404
        # The stored row, not the response — a refusal that still wrote is
        # the failure this asserts against.
        assert [row["summary"]
                for row in _db.load_test_cases(two_teams["pid"])] == [SECRET]

    def test_she_cannot_delete_it(self, app, two_teams):
        c = _alice(app, two_teams)
        _remove_her(app, two_teams)
        assert c.delete("/api/edit/test_case/TC-001").status_code == 404
        assert len(_db.load_test_cases(two_teams["pid"])) == 1

    def test_she_cannot_bulk_delete_it(self, app, two_teams):
        """The toolbar's path, which does not name the project either."""
        c = _alice(app, two_teams)
        _remove_her(app, two_teams)
        r = c.post("/api/edit/test_case/bulk-delete", json={"ids": ["TC-001"]})
        assert len(_db.load_test_cases(two_teams["pid"])) == 1, (
            "bulk-delete answered %s: %s"
            % (r.status_code, r.get_data(as_text=True)))

    def test_she_cannot_create_in_their_project(self, app, two_teams):
        c = _alice(app, two_teams)
        _remove_her(app, two_teams)
        c.post("/api/edit/test_case",
               json={"values": {"summary": "Verify a stranger adds a case"}})
        assert len(_db.load_test_cases(two_teams["pid"])) == 1

    def test_the_page_no_longer_shows_it(self, app, two_teams):
        c = _alice(app, two_teams)
        _remove_her(app, two_teams)
        assert SECRET not in c.get("/test-cases").get_data(as_text=True)

    def test_the_route_refuses_her(self, app, two_teams):
        """The whole path an admin walks, with both guards in place.

        Deliberately loose about *which* refusal: the session guard gets
        there first and redirects to sign-in, the pin guard would answer 404,
        and a reader of this file should not have to know which. What must
        never be true is 200 with the row in it.
        """
        c = _alice(app, two_teams)
        _remove_her_through_the_route(app, two_teams)
        r = c.get("/api/edit/test_case/TC-001")
        assert r.status_code in (302, 401, 404), r.status_code
        assert SECRET not in r.get_data(as_text=True)

    def test_the_resolver_itself_stops_naming_it(self, app, two_teams):
        """The layer the fix is in, asserted directly.

        Every surface above reaches the project through this one call, so a
        test per surface proves the symptom and this proves the cause.
        """
        from routes._shared import resolve_active_project
        _alice(app, two_teams)
        _remove_her(app, two_teams)
        with app.test_request_context("/"):
            from flask import session
            session[_perm.SESSION_USER_KEY] = two_teams["alice"]
            session[_perm.SESSION_ORG_KEY] = two_teams["mine"]
            session["project_id"] = two_teams["pid"]
            assert resolve_active_project(pin=False) != two_teams["pid"]


class TestTheOtherResolver:
    """``ensure_active_project`` had the same unchecked fast path.

    A separate class because it is a separate function, and mutating one
    while the other stayed fixed left every test above green — which is the
    only reason these exist. It is the resolver the *older* surfaces use:
    bug reports, estimation, generation, execution. Two of them are walked
    here, one that reads and one that writes.
    """

    def test_the_bug_reports_page_stops_showing_their_bugs(self, app,
                                                           two_teams):
        c = _alice(app, two_teams)
        assert SECRET_BUG in c.get("/bug-reports").get_data(as_text=True), (
            "the control: a colleague sees the team's bugs")
        _remove_her(app, two_teams)
        assert SECRET_BUG not in c.get("/bug-reports").get_data(as_text=True)

    def test_she_cannot_file_a_bug_into_their_project(self, app, two_teams):
        c = _alice(app, two_teams)
        _remove_her(app, two_teams)
        c.post("/create-bug-report",
               data={"title": "Filed by a stranger", "severity": "Major",
                     "priority": "High"})
        titles = [row.get("title") for row in _db.list_bugs(two_teams["pid"])]
        assert "Filed by a stranger" not in titles, titles

    def test_the_resolver_itself_clears_the_pin(self, app, two_teams):
        """It writes, unlike its sibling, so here the key really goes —
        otherwise the picker keeps naming a project every route refuses."""
        from routes._shared import ensure_active_project
        _alice(app, two_teams)
        _remove_her(app, two_teams)
        with app.test_request_context("/"):
            from flask import session
            session[_perm.SESSION_USER_KEY] = two_teams["alice"]
            session[_perm.SESSION_ORG_KEY] = two_teams["mine"]
            session["project_id"] = two_teams["pid"]
            assert ensure_active_project() != two_teams["pid"]


class TestTheRuleItself:
    """``project_access_with_meta`` — the four verdicts.

    ``_require_project_owner`` turns three of them into three different
    answers (400, 404, 403), and only one of the three is exercised by the
    walk above. The fourth, ``"ok"``, has to carry the row: the gate returns
    it, so a verdict that forgot the meta would 500 every project page.
    """

    def test_a_malformed_id_is_not_a_lookup(self, app):
        from routes._shared import project_access_with_meta
        with app.test_request_context("/"):
            assert project_access_with_meta("not-an-id") == ("malformed", None)
            assert project_access_with_meta(None) == ("malformed", None)

    def test_a_missing_project_is_missing_not_forbidden(self, app):
        """The distinction the resolvers depend on: a pin naming a deleted
        project keeps resolving, because turning that into "no active
        project" would change what a dozen pages render for something that
        is not a security question."""
        from routes._shared import project_access_with_meta
        with app.test_request_context("/"):
            assert project_access_with_meta("f" * 32) == ("missing", None)

    def test_a_legacy_project_answers_to_its_own_session(self, app):
        from routes._shared import (get_session_id,
                            project_access_with_meta)
        with app.test_request_context("/"):
            sid = get_session_id()
            pid = _db.upsert_project(name=f"L-{secrets.token_hex(4)}",
                                     owner_sid=sid)
            verdict, meta = project_access_with_meta(pid)
            assert verdict == "ok"
            assert meta["id"] == pid, (
                "the gate returns this row to its caller; a verdict that "
                "dropped it would 500 every project page")

    def test_a_legacy_project_refuses_another_session(self, app):
        from routes._shared import project_access_with_meta
        pid = _db.upsert_project(name=f"L-{secrets.token_hex(4)}",
                                 owner_sid="someone-elses-session")
        with app.test_request_context("/"):
            assert project_access_with_meta(pid) == ("forbidden_owner", None)

    def test_a_database_that_cannot_answer_honours_the_pin(self, app,
                                                           monkeypatch):
        """Fail *open*, deliberately, and only here.

        The check is a re-check: the pin was approved once already. A DB
        blip that made every resolver return "no active project" would take
        the product away from everybody who is entitled to it, to defend
        against a case that needs a membership change to exist at all.
        """
        from engine import db as _db_mod
        from routes import _shared
        pid = _db.upsert_project(name=f"X-{secrets.token_hex(4)}")

        def _boom(*a, **k):
            raise RuntimeError("database is away")

        monkeypatch.setattr(_db_mod, "get_project", _boom)
        with app.test_request_context("/"):
            assert _shared._pin_revoked(pid) is False
