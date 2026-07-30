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
        client.get("/test-cases")
        with client.session_transaction() as sess:
            assert len(sess.get("test_cases_data") or []) == 2

    def test_session_data_wins_over_the_db(self, client,
                                           project_with_pack):
        """A fresh in-session pack must not be clobbered by an old one.

        ``_session_active_since`` has to be stamped with the current
        SERVER_START_TIME, otherwise ``before_request`` treats the
        session as predating this boot and purges GENERATED_KEYS — which
        is the very wipe this module exists to recover from.
        """
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
