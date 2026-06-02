"""PR-D — SessionDraft DB helpers + TestCase.suite column."""
from __future__ import annotations

import os
from datetime import timedelta

import pytest

from engine import db


@pytest.fixture
def project(app):
    db.init_db()
    pid = db.upsert_project(
        name=f"sd-{os.urandom(4).hex()}",
        base_url="https://app.test",
        owner_sid="test",
    )
    yield pid
    db.delete_project(pid)


class TestSessionDraftLifecycle:
    def test_create_get_consume_round_trip(self, project):
        token = "tok_abc_xyz_001"
        proposed = [
            {"summary": "Flow A", "intent": "x",
             "suggested_suite": "Smoke", "rationale": "ok",
             "steps": [{"action": "click", "target": "role=button"}]},
        ]
        row_id = db.create_session_draft(project, token, proposed)
        assert row_id is not None

        draft = db.get_session_draft(token)
        assert draft is not None
        assert draft["project_id"] == project
        assert draft["proposed_tcs"][0]["summary"] == "Flow A"

        ok = db.consume_session_draft(token)
        assert ok is True

        # Subsequent fetch returns None — single-use guarantee.
        assert db.get_session_draft(token) is None

    def test_missing_token_returns_none(self, project):
        assert db.get_session_draft("nonexistent-token") is None
        assert db.consume_session_draft("nonexistent-token") is False

    def test_missing_project_id_returns_none(self):
        assert db.create_session_draft("", "tok", []) is None

    def test_unknown_project_id_returns_none(self):
        assert db.create_session_draft("ffffffff" * 4, "tok", []) is None

    def test_invalid_input_types(self, project):
        # proposed_tcs not a list → reject
        assert db.create_session_draft(project, "tok", "not-a-list") is None  # type: ignore[arg-type]
        # empty token → reject
        assert db.create_session_draft(project, "", []) is None


class TestExpiry:
    def test_expired_draft_returns_none_and_lazy_deletes(self, project):
        token = "tok_expired"
        db.create_session_draft(project, token, [])
        # Manually backdate expires_at so the lazy-purge fires on read.
        from engine.db import SessionDraft, _utcnow, session_scope
        with session_scope() as sess:
            from sqlalchemy import select
            row = sess.execute(
                select(SessionDraft).where(SessionDraft.token == token)
            ).scalar_one()
            row.expires_at = _utcnow() - timedelta(hours=1)
        # Read → None + row removed.
        assert db.get_session_draft(token) is None
        # And it's purged — second consume is also None.
        assert db.consume_session_draft(token) is False

    def test_purge_expired_sweeps_old_rows(self, project):
        from engine.db import SessionDraft, _utcnow, session_scope
        from sqlalchemy import select
        # Plant 2 rows: one expired, one fresh.
        db.create_session_draft(project, "tok_fresh", [])
        db.create_session_draft(project, "tok_old", [])
        with session_scope() as sess:
            row = sess.execute(
                select(SessionDraft).where(SessionDraft.token == "tok_old")
            ).scalar_one()
            row.expires_at = _utcnow() - timedelta(hours=1)

        removed = db.purge_expired_session_drafts()
        assert removed == 1
        # Fresh one survives.
        assert db.get_session_draft("tok_fresh") is not None


class TestSuiteColumn:
    def test_default_empty_suite(self, project):
        db.save_test_cases(project, [
            {"id": "TC-001", "section": "s", "section_num": 1,
             "summary": "x", "test_steps": "1. nope",
             "expected_result": "", "category": "Positive",
             "priority": "Medium", "status": "Unchecked",
             "testing_type": "Functional"},
        ])
        rows = db.load_test_cases(project)
        assert rows[0]["suite"] == ""

    def test_create_test_case_persists_suite(self, project):
        new_id = db.create_test_case(project, {
            "id": "REC_001", "summary": "Login",
            "test_steps": "1. login", "expected_result": "ok",
            "suite": "Regression", "automation_steps_json": "[]",
        })
        assert new_id is not None
        rows = db.load_test_cases(project)
        rec = next(r for r in rows if r["id"] == "REC_001")
        assert rec["suite"] == "Regression"

    def test_create_test_case_appends_does_not_wipe(self, project):
        db.save_test_cases(project, [
            {"id": "TC-EXISTING", "section": "x", "section_num": 1,
             "summary": "kept", "test_steps": "1. x",
             "expected_result": "", "testing_type": "Functional"},
        ])
        db.create_test_case(project, {
            "id": "REC_001", "summary": "new",
            "test_steps": "1. new", "expected_result": "",
            "suite": "Smoke", "testing_type": "Functional",
        })
        rows = db.load_test_cases(project)
        ids = {r["id"] for r in rows}
        # Both old and new coexist.
        assert "TC-EXISTING" in ids and "REC_001" in ids
