"""Cold-start recovery for /test-cases and /checklist.

Free-tier Render sleeps the service after ~15 min, and
``SESSION_TYPE=filesystem`` sits on an ephemeral disk, so every cold
start wipes the session store. ``app.before_request`` additionally clears
``GENERATED_KEYS`` whenever the session predates the current boot.

Both pages therefore rendered their empty state after a restart and the
operator saw their work apparently vanish — while the pack was in
Postgres the whole time, written by ``_persist_test_cases`` on every
generate. /test-execution already restored from the DB; these two did
not.

These tests assert the pack comes back, which is what "working product on
the free plan" means in practice.
"""
from __future__ import annotations

import uuid

import pytest

from engine import db as _db
from routes._shared import GENERATED_KEYS, SERVER_START_TIME


TC_ROWS = [
    {"id": "SC1_001", "section": "Employees grid", "section_num": 1,
     "summary": 'Verify that User can open the Employee details page',
     "preconditions": "Employees are created",
     "test_steps": "1. Go to HR module -> Employees grid\n2. Click the row",
     "test_data": "", "expected_result": "The details page is opened.",
     "category": "Positive", "priority": "High", "status": "Unchecked"},
    {"id": "SC2_001", "section": "Employee creation", "section_num": 2,
     "summary": "Verify that User cannot save without required fields",
     "preconditions": "",
     "test_steps": "1. Go to the grid\n2. Click Save",
     "test_data": "", "expected_result": "A warning is displayed.",
     "category": "Negative", "priority": "High", "status": "Unchecked"},
]

CL_ROWS = [
    {"id": "FUNC_001", "section": "Functional",
     "objective": "Verify that the grid loads", "comments": "",
     "category": "Positive", "priority": "High", "status": "Unchecked"},
]


@pytest.fixture
def project_with_pack():
    """A project whose TC + CL packs are persisted, like after a run."""
    pid = _db.upsert_project(f"ColdStart_{uuid.uuid4().hex[:8]}")
    _db.save_test_cases(pid, TC_ROWS)
    _db.save_checklist(pid, CL_ROWS)
    return pid


def _simulate_cold_start(client, pid: str) -> None:
    """Keep the project selected but drop every generated key.

    This is exactly the state ``before_request`` leaves behind after a
    restart, and the state a wiped filesystem session store produces.
    """
    with client.session_transaction() as sess:
        sess["project_id"] = pid
        for key in GENERATED_KEYS:
            sess.pop(key, None)


class TestTestCasesColdStart:
    def test_pack_is_restored_from_postgres(self, client,
                                            project_with_pack):
        _simulate_cold_start(client, project_with_pack)
        resp = client.get("/test-cases")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # The empty state must NOT be what the operator sees.
        assert "Verify that User can open the Employee details page" in body
        assert "Verify that User cannot save without required fields" in body

    def test_restore_is_announced_not_silent(self, client,
                                             project_with_pack):
        # Traceability and user stories are session-only derivations, so
        # a silent restore would look like a second, different bug.
        _simulate_cold_start(client, project_with_pack)
        body = client.get("/test-cases").get_data(as_text=True)
        assert "Restored" in body and "2" in body

    def test_restored_rows_land_back_in_the_session(self, client,
                                                    project_with_pack):
        _simulate_cold_start(client, project_with_pack)
        body = client.get("/test-cases").get_data(as_text=True)
        # The recovered pack has to reach the *page*. Asserting on
        # session["test_cases_data"] pinned this to WORKSPACE_DB_FIRST=0,
        # where that key is a mirror; the page is the user-visible contract
        # in both positions (E3.3).
        # The fixture's ids are SC1_001 / SC2_001, not "TC-".
        assert "SC1_001" in body and "SC2_001" in body

    def test_session_data_wins_over_the_db(self, client, project_with_pack,
                                           monkeypatch):
        """A fresh in-session pack must not be clobbered by an old one.

        ``_session_active_since`` has to be stamped with the current
        SERVER_START_TIME, otherwise ``before_request`` treats the
        session as predating this boot and purges GENERATED_KEYS — which
        is the very wipe this module exists to recover from.

        Pinned to ``WORKSPACE_DB_FIRST=0``, and unlike the others in this
        module that is not incidental: "the session wins" *is* the old
        contract, and the flag exists to invert it. The test documents the
        behaviour that still ships, and retires with the mirror in E3.4
        rather than being rewritten.
        """
        monkeypatch.delenv("WORKSPACE_DB_FIRST", raising=False)
        with client.session_transaction() as sess:
            sess["project_id"] = project_with_pack
            sess["_session_active_since"] = SERVER_START_TIME
            sess["test_cases_data"] = [dict(TC_ROWS[0],
                                            summary="Verify that FRESH wins")]
        body = client.get("/test-cases").get_data(as_text=True)
        assert "Verify that FRESH wins" in body
        assert "Restored" not in body

    def test_no_project_selected_still_renders_the_empty_state(self, client):
        with client.session_transaction() as sess:
            sess.pop("project_id", None)
            for key in GENERATED_KEYS:
                sess.pop(key, None)
        resp = client.get("/test-cases")
        assert resp.status_code == 200

    def test_db_failure_does_not_break_the_page(self, client, monkeypatch,
                                                project_with_pack):
        _simulate_cold_start(client, project_with_pack)

        def _boom(_pid):
            raise RuntimeError("database is suspended")

        monkeypatch.setattr(_db, "load_test_cases", _boom)
        resp = client.get("/test-cases")
        # Degrades to the empty state rather than a 500 — the free
        # Postgres plan expires roughly monthly, so this path runs.
        assert resp.status_code == 200


class TestChecklistColdStart:
    def test_pack_is_restored_from_postgres(self, client,
                                            project_with_pack):
        _simulate_cold_start(client, project_with_pack)
        body = client.get("/checklist").get_data(as_text=True)
        assert "Verify that the grid loads" in body

    def test_restore_is_announced(self, client, project_with_pack):
        _simulate_cold_start(client, project_with_pack)
        body = client.get("/checklist").get_data(as_text=True)
        assert "Restored" in body

    def test_db_failure_does_not_break_the_page(self, client, monkeypatch,
                                                project_with_pack):
        _simulate_cold_start(client, project_with_pack)

        def _boom(_pid):
            raise RuntimeError("database is suspended")

        monkeypatch.setattr(_db, "load_checklist", _boom)
        assert client.get("/checklist").status_code == 200


class TestSessionStoreWipedEntirely:
    """The harder cold start: the session store itself is gone.

    A Render restart wipes the filesystem session store, so
    ``session["project_id"]`` is empty — not just the generated keys. The
    signed cookie survives (SECRET_KEY is stable per render.yaml), so the
    project is still findable in Postgres by owner_sid, which is what the
    project picker does via ``ensure_active_project``.

    Hydration originally read ``session["project_id"]`` directly and so
    bailed out in precisely this case: on prod the picker showed
    "LLM-VERIFY 2026-07-30 (48 TC)" while the page rendered its empty
    state. Reproduced 2026-07-30.

    The project is created through the app here, not via _db directly, so
    it carries whatever owner_sid the real request flow assigns — the
    thing the recovery depends on.
    """

    @staticmethod
    def _project_via_app(client) -> str:
        """Create a project the way the picker does, return its id."""
        resp = client.post("/projects/db/create",
                           data={"project_name":
                                 f"Wiped_{uuid.uuid4().hex[:8]}"},
                           follow_redirects=True)
        assert resp.status_code == 200, resp.status_code
        with client.session_transaction() as sess:
            pid = sess.get("project_id")
        assert pid, "creating a project must set session['project_id']"
        return pid

    def test_pack_is_restored_with_no_project_id_in_session(self, client):
        pid = self._project_via_app(client)
        _db.save_test_cases(pid, TC_ROWS)

        # Wipe the session the way a restart does — including project_id.
        with client.session_transaction() as sess:
            sess.pop("project_id", None)
            for key in GENERATED_KEYS:
                sess.pop(key, None)

        body = client.get("/test-cases").get_data(as_text=True)
        assert "Verify that User can open the Employee details page" in body, (
            "hydration must resolve the project from Postgres by owner_sid, "
            "not from session['project_id'] — that key is empty in exactly "
            "the cold start this recovery exists for")

    def test_pack_info_probe_also_resolves_the_project(self, client):
        pid = self._project_via_app(client)
        _db.save_test_cases(pid, TC_ROWS)
        with client.session_transaction() as sess:
            sess.pop("project_id", None)

        body = client.get("/api/pack-info?kind=tc").get_json()
        assert body["count"] == len(TC_ROWS)
        assert body["project"] == pid

    def test_resolver_does_not_create_a_project_on_a_read_path(self, client,
                                                              sign_in):
        """ensure_active_project() auto-creates; the resolver must not."""
        pid = self._project_via_app(client)
        with client.session_transaction() as sess:
            sess.pop("project_id", None)
        # A GET that finds nothing must still not mint a project. Point the
        # cookie at a session with no owned projects by using a fresh client.
        fresh = client.application.test_client()
        sign_in(fresh)
        before = fresh.get("/test-cases").status_code
        assert before == 200
        with fresh.session_transaction() as sess:
            assert not sess.get("project_id"), (
                "a plain GET /test-cases created a project as a side "
                "effect; hydration must use resolve_active_project()")


class TestExplicitClearIsRespected:
    """Recovery must not undo a deliberate "New session".

    Caught by tests/test_functional.py::TestNewSession while building the
    fix above: hydration restored the pack from Postgres on the very next
    GET, so /new-session appeared to do nothing. Cold-start recovery and
    an explicit clear look identical from the session's point of view —
    both leave it without a pack — so /new-session stamps the clear with
    the current boot to tell them apart.
    """

    def _seed(self, client) -> str:
        resp = client.post("/projects/db/create",
                           data={"project_name":
                                 f"Cleared_{uuid.uuid4().hex[:8]}"},
                           follow_redirects=True)
        assert resp.status_code == 200
        with client.session_transaction() as sess:
            pid = sess["project_id"]
        _db.save_test_cases(pid, TC_ROWS)
        return pid

    def test_new_session_is_not_undone_by_hydration(self, client):
        self._seed(client)
        client.post("/new-session", follow_redirects=True)
        body = client.get("/test-cases").get_data(as_text=True)
        assert "Verify that User can open the Employee details page" \
            not in body, ("hydration resurrected a pack the user "
                          "explicitly cleared via /new-session")

    def test_picking_a_project_again_re_enables_recovery(self, client):
        pid = self._seed(client)
        client.post("/new-session", follow_redirects=True)
        # Choosing a project is a fresh intent — it supersedes the clear.
        client.post(f"/projects/db/select/{pid}", follow_redirects=True)
        body = client.get("/test-cases").get_data(as_text=True)
        assert "Verify that User can open the Employee details page" in body

    def test_a_restart_after_a_clear_re_enables_recovery(self, client):
        """The marker is session state, so a restart drops it with the rest."""
        self._seed(client)
        client.post("/new-session", follow_redirects=True)
        with client.session_transaction() as sess:
            # Simulate the wipe: the marker goes too.
            sess.pop("_pack_cleared_boot", None)
            sess.pop("project_id", None)
        body = client.get("/test-cases").get_data(as_text=True)
        assert "Verify that User can open the Employee details page" in body


# ── The org era, which the recovery path never learned about ─────────

class TestRecoveryUnderOrgMode:
    """Recovery is giving a project back, not handing one out.

    The staging report on 2026-08-28 was a page whose header named an
    active project next to a "Start session recording" button answering
    "no_active_project". The first fix read that as the endpoints being
    wrong and pointed both resolvers at ``visible_projects``, so they
    would return the first project the caller could *see*.

    That was the wrong layer, and its own premise was false. It rested on
    "a team's project has no owner_sid", copied out of
    ``visible_projects``' docstring — but every path that creates a
    project writes one: both routes in ``routes/projects.py`` pass
    ``owner_sid=get_session_id()``, and so does ``ensure_active_project``'s
    auto-create. The test that pinned the premise built its own project
    without an owner_sid and then asserted it had none, so it pinned the
    fixture rather than the product.

    The header named nothing. ``_project_picker.html`` marks no option
    selected when there is no active id, and the browser then displays the
    first — the select box implying a choice the server had not made.
    Adopting that project made the endpoints agree with the display and
    broke the isolation rule instead: with the whole suite run under
    ORG_MODE it cost 20 tests, among them a page rendering another
    project's ``SC1_001`` and a pack count of 23 where the session had no
    project at all.

    So: the caller's own project comes back; a colleague's never does.
    """

    @pytest.fixture(autouse=True)
    def _org_mode(self, monkeypatch):
        monkeypatch.setenv("AUTH_ENABLED", "1")
        monkeypatch.setenv("ORG_MODE", "1")
        _db.init_db()

    @staticmethod
    def _member(org=None, role="admin"):
        import secrets
        from engine import auth as _auth

        if org is None:
            org = _db.create_organization(f"Team {secrets.token_hex(4)}")
        uid = _db.create_user(
            f"member-{secrets.token_hex(5)}@example.test",
            password_hash=_auth.hash_password("a passphrase here"))
        _db.add_org_member(org, uid, role)
        return org, uid

    @staticmethod
    def _sign_in(client, org, uid, *, project_id=None):
        from engine import permissions as _perm
        with client.session_transaction() as sess:
            sess.clear()
            sess[_perm.SESSION_USER_KEY] = uid
            sess[_perm.SESSION_ORG_KEY] = org
            sess["_session_active_since"] = 9_999_999_999
            if project_id:
                sess["project_id"] = project_id

    def _team_with_a_project(self, client):
        """A signed-in admin who has created a project *through the
        product*, then lost their session — the cold start this file is
        about.

        Created through the route on purpose: a project built directly
        with ``upsert_project`` can be given any shape the test likes,
        and that is exactly how the earlier version of this class talked
        itself into a false premise.
        """
        import secrets
        org, uid = self._member()
        self._sign_in(client, org, uid)
        name = f"team-proj-{secrets.token_hex(4)}"
        resp = client.post("/projects/db/create",
                           data={"project_name": name, "next": "/dashboard"})
        assert resp.status_code in (200, 302), resp.status_code
        pid = next((p["id"] for p in (_db.list_projects_for_org(org) or [])
                    if p.get("name") == name), None)
        assert pid, "the route did not create the project"
        # The session wipe. The SID cookie survives it; the store does not.
        with client.session_transaction() as sess:
            sess.pop("project_id", None)
            sess.pop("project_setup", None)
        return org, uid, pid

    def test_a_project_made_through_the_product_carries_an_owner_sid(
            self, client):
        """The premise, taken from the product instead of from a fixture.

        If project creation ever stops writing ``owner_sid``, recovery
        silently stops working and every test below would still pass —
        they would find nothing to recover and nothing to refuse.
        """
        org, uid, pid = self._team_with_a_project(client)
        row = _db.get_project(pid)
        assert row and row.get("owner_sid"), row
        assert row.get("org_id") == org

    def test_resolve_gives_back_the_callers_own_project(self, client):
        from routes._shared import resolve_active_project

        org, uid, pid = self._team_with_a_project(client)
        with client.session_transaction() as sess:
            assert resolve_active_project(sess) == pid

    def test_resolve_does_not_hand_over_a_colleagues_project(self, app,
                                                             client):
        """The isolation rule, at the layer that nearly broke it.

        A second member of the same organisation can *see* the admin's
        project — that is what a shared workspace means — and must still
        be shown the empty state until they pick it. Silently pinning it
        is how a run on project A comes to render project B.

        A second ``test_client``, not a second sign-in on the first: the
        scope is ``owner_sid``, which comes from the ``_tfg_sid_v1``
        *browser* cookie. Re-signing in on the same client keeps that
        cookie and would make this test assert something the code does not
        do — see the class note about pinning fixtures.
        """
        from routes._shared import resolve_active_project, visible_projects

        org, uid, pid = self._team_with_a_project(client)
        _, colleague = self._member(org=org, role="user")

        with app.test_client() as other:
            self._sign_in(other, org, colleague)
            with other.session_transaction() as sess:
                assert pid in {p["id"] for p in visible_projects(sess)}, (
                    "premise: the colleague can see it in the picker")
                assert resolve_active_project(sess) == ""

    def test_the_scope_is_the_browser_and_this_says_so_out_loud(self,
                                                                client):
        """A limitation, pinned so nobody mistakes it for a guarantee.

        ``owner_sid`` identifies the browser, not the person: it is read
        from a cookie that outlives sign-out. Two colleagues sharing one
        workstation therefore share a recovery scope, and the second to
        sign in can be handed the first one's project.

        This is not new — it is what the code did before the resolver was
        briefly widened, and it is out of scope for a regression fix — but
        an assertion is the only honest place to record it. Closing it
        needs a per-user link on the project, which the table does not
        have.
        """
        from routes._shared import resolve_active_project

        org, uid, pid = self._team_with_a_project(client)
        _, colleague = self._member(org=org, role="user")
        self._sign_in(client, org, colleague)   # same browser, new person

        with client.session_transaction() as sess:
            assert resolve_active_project(sess) == pid, (
                "if this now returns '', recovery has been scoped to the "
                "user and the note above is out of date — delete it")

    def test_ensure_does_not_auto_create_a_second_project(self, client):
        """``ensure_active_project`` falls through to auto-create when the
        lookup finds nothing, so a failed recovery is not merely a wrong
        answer: the team gets a fresh empty "Untitled project" and their
        pack looks lost while sitting in Postgres.
        """
        from routes._shared import ensure_active_project

        org, uid, pid = self._team_with_a_project(client)
        before = len(_db.list_projects_for_org(org) or [])
        with client.session_transaction() as sess:
            got = ensure_active_project(sess)
        assert got == pid
        assert len(_db.list_projects_for_org(org) or []) == before, (
            "a second project was created for a team that already had one")

    def test_ensure_does_not_pin_a_colleagues_project(self, app, client):
        """The louder half: this function writes.

        Adopting a visible project here would pin a colleague's project
        into the caller's session by the mere act of opening a page, with
        nothing on screen recording that a choice had been made.
        """
        from routes._shared import ensure_active_project

        org, uid, pid = self._team_with_a_project(client)
        _, colleague = self._member(org=org, role="user")

        with app.test_client() as other:
            self._sign_in(other, org, colleague)
            with other.session_transaction() as sess:
                got = ensure_active_project(sess)
                assert got != pid, "a colleague's project was pinned"
                assert sess.get("project_id") != pid

    def test_the_recorder_endpoint_agrees_with_the_page(self, client):
        # The symptom as the operator met it, end to end.
        import os
        from unittest import mock

        org, uid, pid = self._team_with_a_project(client)
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post("/api/recorder-session/start", json={})
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json().get("project_id") == pid

    def test_the_picker_does_not_let_the_first_option_imply_a_choice(
            self, app, client):
        """The defect the staging report was actually describing.

        With projects listed and none active, a <select> displays its
        first option, and that is the browser's default rather than the
        server's answer. The page must say so.
        """
        org, uid, pid = self._team_with_a_project(client)
        _, colleague = self._member(org=org, role="user")

        with app.test_client() as other:
            self._sign_in(other, org, colleague)
            body = other.get("/test-cases").get_data(as_text=True)
        assert "No project selected" in body, (
            "the picker still lets the first option pose as the active "
            "project")

