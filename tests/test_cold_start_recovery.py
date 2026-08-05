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
