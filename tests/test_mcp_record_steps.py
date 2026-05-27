"""PR-B/4 tests — mcp_server.server.record_steps_attach.

Pins the contract:

* Gated by RECORDER_ENABLED — production deployments stay off until
  pilot graduates.
* Validates project_id / tc_external_id / steps shape and refuses
  malformed input.
* Defensive cleaner drops items without an action (matches the
  parser's behaviour so the round-trip is consistent end-to-end).
* Round-trips through ``update_tc_automation_steps`` — the runner
  picks up the recording on its next pass without any extra plumbing.
"""
from __future__ import annotations

import json
import os
from unittest import mock

import pytest

from engine import db
from mcp_server import server as mcp_server


@pytest.fixture
def recorder_on():
    """Flip RECORDER_ENABLED on for the test, restore afterwards."""
    with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
        yield


@pytest.fixture
def project_with_tc():
    pid = db.upsert_project(
        name=f"recorder-mcp-{os.urandom(4).hex()}",
        base_url="https://app.example.com",
    )
    db.save_test_cases(pid, [{
        "id": "TC-001", "section": "Login", "section_num": 1,
        "summary": "Sign in", "preconditions": "",
        "test_steps": "1. Open\n2. Submit", "test_data": "",
        "expected_result": "Welcome page",
        "issues": "", "comment": "", "user_story_id": "US-1",
        "category": "Positive", "priority": "High",
        "status": "Unchecked", "testing_type": "Functional",
        "url_pattern": "", "trigger": "manual",
    }])
    yield pid, "TC-001"
    db.delete_project(pid)


SAMPLE_STEPS = [
    {"action": "goto", "target": "https://app.example.com/login",
     "value": "", "raw": "page.goto(...)", "comment": ""},
    {"action": "fill", "target": "label=Email",
     "value": "user@x.test", "raw": "...fill(...)", "comment": ""},
]


class TestFeatureFlag:
    def test_refuses_when_flag_off(self, project_with_tc):
        # Default environ has RECORDER_ENABLED unset → off.
        pid, tc_id = project_with_tc
        with pytest.raises(RuntimeError, match="RECORDER_ENABLED"):
            mcp_server.record_steps_attach(pid, tc_id, SAMPLE_STEPS)

    def test_accepts_when_flag_on(self, recorder_on, project_with_tc):
        pid, tc_id = project_with_tc
        out = mcp_server.record_steps_attach(pid, tc_id, SAMPLE_STEPS)
        assert out["ok"] is True
        assert out["steps_count"] == 2


class TestSuccessPath:
    def test_round_trip_to_db(self, recorder_on, project_with_tc):
        pid, tc_id = project_with_tc
        mcp_server.record_steps_attach(pid, tc_id, SAMPLE_STEPS)
        loaded = db.load_test_cases(pid)
        payload = loaded[0]["automation_steps_json"]
        assert payload, "expected automation_steps_json to be populated"
        decoded = json.loads(payload)
        assert len(decoded) == 2
        assert decoded[0]["action"] == "goto"
        assert decoded[1]["action"] == "fill"

    def test_empty_list_clears_recording(self, recorder_on, project_with_tc):
        pid, tc_id = project_with_tc
        # Attach, then clear.
        mcp_server.record_steps_attach(pid, tc_id, SAMPLE_STEPS)
        out = mcp_server.record_steps_attach(pid, tc_id, [])
        assert out["ok"] is True
        assert out["steps_count"] == 0
        loaded = db.load_test_cases(pid)
        assert loaded[0]["automation_steps_json"] == ""


class TestDefensiveCleaning:
    def test_drops_items_without_action(self, recorder_on, project_with_tc):
        pid, tc_id = project_with_tc
        out = mcp_server.record_steps_attach(pid, tc_id, [
            {"action": "click", "target": "label=X"},
            {"target": "label=Y"},              # no action — dropped
            {"action": "", "target": "z"},      # blank — dropped
            {"action": "fill", "value": "v"},
            "not a dict",                       # type mismatch — dropped
        ])
        assert out["steps_count"] == 2
        loaded = db.load_test_cases(pid)
        decoded = json.loads(loaded[0]["automation_steps_json"])
        assert [s["action"] for s in decoded] == ["click", "fill"]

    def test_coerces_non_string_fields(self, recorder_on, project_with_tc):
        pid, tc_id = project_with_tc
        mcp_server.record_steps_attach(pid, tc_id, [
            {"action": "fill", "target": "x", "value": 42, "raw": None,
             "comment": False},
        ])
        decoded = json.loads(db.load_test_cases(pid)[0]["automation_steps_json"])
        assert decoded[0]["value"] == "42"
        assert decoded[0]["raw"] == ""
        assert decoded[0]["comment"] == ""


class TestValidationFailures:
    def test_missing_project_id_raises(self, recorder_on):
        with pytest.raises(ValueError, match="project_id"):
            mcp_server.record_steps_attach("", "TC-001", SAMPLE_STEPS)

    def test_missing_tc_id_raises(self, recorder_on, project_with_tc):
        pid, _ = project_with_tc
        with pytest.raises(ValueError, match="tc_external_id"):
            mcp_server.record_steps_attach(pid, "", SAMPLE_STEPS)

    def test_non_list_steps_raises(self, recorder_on, project_with_tc):
        pid, tc_id = project_with_tc
        with pytest.raises(ValueError, match="steps"):
            mcp_server.record_steps_attach(pid, tc_id, "not a list")

    def test_unknown_tc_returns_not_found(self, recorder_on, project_with_tc):
        pid, _ = project_with_tc
        out = mcp_server.record_steps_attach(pid, "TC-DOES-NOT-EXIST",
                                              SAMPLE_STEPS)
        assert out["ok"] is False
        assert out["reason"] == "tc_not_found"
