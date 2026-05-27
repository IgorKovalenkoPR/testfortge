"""PR-B/2 tests — engine.db.update_tc_automation_steps round-trip.

Pins the contract:

* The helper finds the TC by ``(project_id, external_id)`` and writes a
  JSON-serialised step list to ``automation_steps_json``.
* An unknown TC returns ``False`` (caller decides if that's an error).
* Empty list clears the column (NULL) so the runner falls back to its
  heuristic parse of ``test_steps``.
* Existing TCs without recorded steps load with ``automation_steps_json=""``
  through ``_TC_DATACLASS_FIELDS`` — backward-compatible for every
  pre-Recorder pack.
"""
from __future__ import annotations

import json

import pytest

from engine import db


@pytest.fixture
def project_with_tc(app):
    """Create a project + one TC, return (project_id, tc_external_id)."""
    db.init_db()
    pid = db.upsert_project(
        name="Recorder pilot project",
        base_url="https://app.example.com",
        owner_sid="test-sid-recorder",
    )
    db.save_test_cases(pid, [{
        "id": "TC-001",
        "section": "Login",
        "section_num": 1,
        "summary": "Sign in happy path",
        "preconditions": "",
        "test_steps": "1. Open /login\n2. Enter credentials\n3. Submit",
        "test_data": "",
        "expected_result": "Welcome page loads",
        "issues": "",
        "comment": "",
        "user_story_id": "US-1",
        "category": "Positive",
        "priority": "High",
        "status": "Unchecked",
        "testing_type": "Functional",
        "url_pattern": "",
        "trigger": "manual",
    }])
    yield pid, "TC-001"
    db.delete_project(pid)


class TestUpdateTcAutomationSteps:
    def test_writes_steps_and_round_trips(self, project_with_tc):
        pid, tc_id = project_with_tc
        steps = [
            {"action": "goto", "target": "https://app.example.com/login",
             "value": "", "raw": "page.goto(...)", "comment": ""},
            {"action": "fill", "target": "label=Email",
             "value": "user@x.test", "raw": "page.get_by_label(...).fill(...)",
             "comment": ""},
        ]
        ok = db.update_tc_automation_steps(pid, tc_id, steps)
        assert ok is True

        loaded = db.load_test_cases(pid)
        assert len(loaded) == 1
        payload = loaded[0].get("automation_steps_json", "")
        assert payload, "expected automation_steps_json to round-trip back"
        decoded = json.loads(payload)
        assert decoded == steps

    def test_unknown_tc_returns_false(self, project_with_tc):
        pid, _ = project_with_tc
        assert db.update_tc_automation_steps(pid, "TC-DOES-NOT-EXIST", [
            {"action": "goto", "target": "https://x.test"}
        ]) is False

    def test_empty_steps_clears_column(self, project_with_tc):
        pid, tc_id = project_with_tc
        # First, write a payload so we have something to clear.
        db.update_tc_automation_steps(pid, tc_id, [
            {"action": "goto", "target": "https://x.test"}
        ])
        # Now clear it.
        ok = db.update_tc_automation_steps(pid, tc_id, [])
        assert ok is True
        loaded = db.load_test_cases(pid)
        # Column is NULL → _TC_DATACLASS_FIELDS resolves it to "".
        assert loaded[0].get("automation_steps_json", "") == ""

    def test_missing_project_id_returns_false(self):
        assert db.update_tc_automation_steps("", "TC-001", []) is False

    def test_missing_tc_id_returns_false(self, project_with_tc):
        pid, _ = project_with_tc
        assert db.update_tc_automation_steps(pid, "", []) is False


class TestPreRecorderBackwardCompat:
    def test_tc_without_recording_loads_with_empty_steps_field(
            self, project_with_tc):
        """A TC saved without recorder data must surface
        ``automation_steps_json=""`` so callers that always read the field
        do not get KeyError or surprise None values."""
        pid, _ = project_with_tc
        loaded = db.load_test_cases(pid)
        assert loaded[0]["automation_steps_json"] == ""

    def test_unicode_preserved_in_payload(self, project_with_tc):
        pid, tc_id = project_with_tc
        steps = [{"action": "fill", "target": "label=Імʼя",
                  "value": "Олександр", "raw": "page.fill('Імʼя', 'Олександр')",
                  "comment": "Ukrainian payload"}]
        db.update_tc_automation_steps(pid, tc_id, steps)
        decoded = json.loads(db.load_test_cases(pid)[0]["automation_steps_json"])
        assert decoded[0]["value"] == "Олександр"
        assert decoded[0]["target"] == "label=Імʼя"
