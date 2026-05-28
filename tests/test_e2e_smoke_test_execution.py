"""End-to-end smoke for Test Execution module post-PR-C.

Boots the Flask app, plants a small project with one recorded TC, and
hits every page that touches the Test Execution surface in both
RECORDER_ENABLED=0 (default) and =1 (pilot) states. Purpose: confirm
nothing regresses with the Guide + PR-C edits — not a substitute for
the operator's real-Chromium acceptance pass.

Covered routes:
  * /guide
  * /test-cases (recorder block hidden vs shown; step kind dropdown
    rendered iff TC has a recording)
  * /test-execution (run form)
  * /test-execution/live (live mirror page)
  * /bug-reports (Stage 4 surface — confirms PR-C didn't break the
    sibling that consumes Test Execution output)
  * POST /test-cases/<id>/automation-step-kind (the PR-C editor route)
"""
from __future__ import annotations

import os
from unittest import mock

import pytest

from engine import db


@pytest.fixture
def seeded_project(client):
    """Plant a project + 2 TCs (one recorded, one plain) and bind the
    session so /test-cases renders the pack."""
    pid = db.upsert_project(
        name=f"e2e-smoke-{os.urandom(4).hex()}",
        base_url="https://app.example.com",
    )
    db.save_test_cases(pid, [
        {"id": "TC-PLAIN", "section": "Login", "section_num": 1,
         "summary": "Sign in (text-authored)", "preconditions": "",
         "test_steps": "1. Open\n2. Click Submit", "test_data": "",
         "expected_result": "Welcome", "issues": "", "comment": "",
         "user_story_id": "US-1", "category": "Positive",
         "priority": "High", "status": "Unchecked",
         "testing_type": "Functional", "url_pattern": "",
         "trigger": "manual"},
        {"id": "TC-REC", "section": "Login", "section_num": 1,
         "summary": "Sign in (recorded)", "preconditions": "",
         "test_steps": "1. Open\n2. Click Submit", "test_data": "",
         "expected_result": "Welcome", "issues": "", "comment": "",
         "user_story_id": "US-1", "category": "Positive",
         "priority": "High", "status": "Unchecked",
         "testing_type": "Functional", "url_pattern": "",
         "trigger": "manual"},
    ])
    db.update_tc_automation_steps(pid, "TC-REC", [
        {"action": "goto", "target": "https://app.example.com/login",
         "value": "", "raw": "page.goto(...)", "comment": ""},
        {"action": "click",
         "target": 'role=button[name="Sign in"]',
         "value": "", "raw": "click signin",
         "comment": "",
         "target_alternates": ["text=Sign in"],
         "locator_label": "role=button:Sign in"},
        {"action": "expect_visible",
         "target": 'role=heading[name="Welcome"]',
         "value": "", "raw": "expect welcome",
         "comment": "", "kind": "assertion",
         "assertion_type": "visible"},
    ])
    with client.session_transaction() as s:
        s["project_id"] = pid
        s["active_project_id"] = pid
        s["test_cases_data"] = db.load_test_cases(pid)
        s["_session_active_since"] = 9_999_999_999
    yield pid
    db.delete_project(pid)


class TestGuidePage:
    def test_guide_renders_pr_c_assertion_content(self, client):
        """Guide must surface the PR-C assertion authoring path or the
        operator has no docs reference for the dropdown."""
        resp = client.get("/guide")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # PR-C explainer landed in the test-execution template block.
        assert "Capturing assertions" in body or "Assert visible" in body
        assert "Recording test steps" in body
        assert "RECORDER_ENABLED" in body
        # Per-step dropdown is documented.
        assert "Editing kind" in body or "per-step dropdown" in body
        # Multi-locator survival note.
        assert ("multi-locator" in body.lower()
                or "Page Object DB" in body)


class TestCasesPageE2E:
    def test_default_renders_without_recorder_surface(self, client,
                                                       seeded_project):
        """RECORDER_ENABLED off → no recorder UI on /test-cases. This
        guards the feature flag default — a misconfigured host must
        not leak the pilot panel to general users."""
        env = {k: v for k, v in os.environ.items()
               if k != "RECORDER_ENABLED"}
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ["FLASK_DEBUG"] = "1"
            resp = client.get("/test-cases")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # No recorder details block rendered.
        assert "tc-recorder-edit" not in body
        # No actual <select> element with the PR-C dropdown marker.
        # (The JS function body still references the selector string
        # for symmetry with recorderInit() — empty NodeList is fine.)
        assert "<select data-step-kind-select" not in body
        assert "data-step-editor=" not in body
        # The pack itself still renders.
        assert "TC-PLAIN" in body
        assert "TC-REC" in body

    def test_flag_on_renders_recorder_panel_and_dropdown(self, client,
                                                         seeded_project):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.get("/test-cases")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # Recorder pilot surface present.
        assert "tc-recorder-edit" in body
        # TC-REC has a recording so the kind dropdown materialises;
        # TC-PLAIN has no recording so it doesn't.
        assert 'data-step-editor="TC-REC"' in body
        assert 'data-step-editor="TC-PLAIN"' not in body
        # All 4 dropdown options present at least once.
        assert ">Action<" in body
        assert ">Assert visible<" in body
        assert ">Assert text<" in body
        assert ">Assert URL<" in body
        # Existing assertion step is pre-selected.
        assert 'value="assertion/visible"\n' in body \
            or 'value="assertion/visible" selected' in body \
            or 'selected>Assert visible' in body


class TestExecutionPageE2E:
    def test_test_execution_page_boots(self, client, seeded_project):
        """The Test Execution form must boot regardless of recorder
        flag — recordings flow through transparently."""
        resp = client.get("/test-execution")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # Run-mode + environment knobs the operator relies on.
        assert "Run Test Execution" in body or "test-execution" in body

    def test_test_execution_live_page_boots(self, client, seeded_project):
        resp = client.get("/test-execution/live")
        assert resp.status_code == 200


class TestBugReportsSibling:
    def test_bug_reports_page_renders(self, client, seeded_project):
        """Stage 4 sibling consumes Test Execution output. PR-C must
        not have broken its route."""
        resp = client.get("/bug-reports")
        assert resp.status_code == 200


class TestKindEditorRouteSmoke:
    """End-to-end of the PR-C POST endpoint: round-trip a single step
    flip and confirm the DB is updated."""
    def test_post_flip_to_assertion_and_back(self, client, seeded_project):
        pid = seeded_project
        # 1) Flip action step → assertion/text
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post(
                "/test-cases/TC-REC/automation-step-kind",
                json={"index": 1, "kind": "assertion",
                       "assertion_type": "text"},
            )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

        # 2) Confirm persistence.
        import json as _json
        tcs = db.load_test_cases(pid)
        tc = next(t for t in tcs if t["id"] == "TC-REC")
        steps = _json.loads(tc["automation_steps_json"])
        assert steps[1]["kind"] == "assertion"
        assert steps[1]["assertion_type"] == "text"

        # 3) Flip back to action.
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post(
                "/test-cases/TC-REC/automation-step-kind",
                json={"index": 1, "kind": "action"},
            )
        assert resp.status_code == 200

        tcs = db.load_test_cases(pid)
        tc = next(t for t in tcs if t["id"] == "TC-REC")
        steps = _json.loads(tc["automation_steps_json"])
        assert steps[1]["kind"] == "action"
        assert steps[1]["assertion_type"] == ""

    def test_post_blocked_when_flag_off_default(self, client, seeded_project):
        """Default deployment (no RECORDER_ENABLED): editor POST must
        refuse so a misconfigured host can't accept assertion edits
        when the rest of the recorder surface is invisible."""
        env = {k: v for k, v in os.environ.items()
               if k != "RECORDER_ENABLED"}
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ["FLASK_DEBUG"] = "1"
            resp = client.post(
                "/test-cases/TC-REC/automation-step-kind",
                json={"index": 0, "kind": "action"},
            )
        assert resp.status_code == 403
