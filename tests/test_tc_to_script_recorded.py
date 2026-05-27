"""PR-B/3 tests — tc_to_script prefers automation_steps_json over text parse.

Pins the contract:

* When ``automation_steps_json`` is populated, the runner sees the
  recorded steps verbatim. No "Navigate to <base_url>" injection, no
  heuristic ``parse_manual_step`` pass.
* When ``automation_steps_json`` is empty / missing / malformed, behaviour
  matches the pre-PR-B world byte-for-byte (regression guard for every
  legacy TC).
* ``expected_result`` still becomes a trailing ``expect_text`` step in
  both authoring paths — the runner's pass/fail surface stays uniform.
"""
from __future__ import annotations

import json

from engine.automation_qa import tc_to_script, _decode_recorded_steps


_RECORDED_LOGIN = [
    {"action": "goto", "target": "https://app.example.com/login",
     "value": "", "raw": "page.goto(...)", "comment": ""},
    {"action": "fill", "target": "label=Email",
     "value": "user@x.test", "raw": "...fill(...)", "comment": ""},
    {"action": "click", "target": 'role=button[name="Sign in"]',
     "value": "", "raw": "...click()", "comment": ""},
]


class TestTcToScriptWithRecorded:
    def test_uses_recorded_steps_verbatim(self):
        tc = {
            "id": "TC-001",
            "summary": "Login flow",
            "test_steps": "1. Click something that should be ignored",
            "expected_result": "",
            "automation_steps_json": json.dumps(_RECORDED_LOGIN),
        }
        script = tc_to_script(tc, base_url="https://app.example.com")
        assert len(script.steps) == 3
        assert script.steps[0].action == "goto"
        assert script.steps[0].target == "https://app.example.com/login"
        assert script.steps[1].action == "fill"
        assert script.steps[1].target == "label=Email"
        assert script.steps[2].target == 'role=button[name="Sign in"]'

    def test_no_navigate_injection_when_recorded(self):
        """The heuristic path injects a 'Navigate to <base_url>' step when
        the first line is plain text. Recorded path must NOT do that —
        codegen output already has its own goto, and even when it does
        not, we trust the recording."""
        recorded_without_goto = [
            {"action": "click", "target": "label=Settings", "value": "",
             "raw": "...", "comment": ""},
        ]
        tc = {
            "id": "TC-002", "summary": "X",
            "test_steps": "1. Step",
            "automation_steps_json": json.dumps(recorded_without_goto),
        }
        script = tc_to_script(tc, base_url="https://app.example.com")
        # Single recorded step + no synthetic expect_text (empty expected_result).
        assert len(script.steps) == 1
        assert script.steps[0].action == "click"

    def test_expected_result_still_appends_expect_text(self):
        tc = {
            "id": "TC-003", "summary": "X",
            "test_steps": "",
            "expected_result": 'User sees "Welcome back"',
            "automation_steps_json": json.dumps(_RECORDED_LOGIN),
        }
        script = tc_to_script(tc, base_url="")
        # 3 recorded + 1 synthetic expect_text.
        assert len(script.steps) == 4
        assert script.steps[-1].action == "expect_text"
        assert "Welcome back" in script.steps[-1].value


class TestTcToScriptFallback:
    def test_empty_automation_steps_json_falls_back_to_text(self):
        tc = {
            "id": "TC-LEGACY-1", "summary": "Login",
            "test_steps": "1. Click the 'Login' button\n"
                          "2. Enter 'a@b.test' into Email field",
            "expected_result": "User is logged in",
            "automation_steps_json": "",
        }
        script = tc_to_script(tc, base_url="https://site.com")
        # Legacy path: prepends Navigate, parses 2 lines, appends expect_text.
        assert script.steps[0].action == "goto"
        assert script.steps[0].target == "https://site.com"
        assert any(s.action == "click" for s in script.steps)
        assert any(s.action == "fill" for s in script.steps)
        assert script.steps[-1].action == "expect_text"

    def test_missing_automation_steps_json_key_falls_back(self):
        """A pre-PR-B TC dict has no automation_steps_json key at all.
        Behaviour must be byte-identical to the legacy code path."""
        tc = {
            "id": "TC-LEGACY-2", "summary": "X",
            "test_steps": "1. Click 'Submit'",
            "expected_result": "Form persisted",
        }
        script = tc_to_script(tc, base_url="https://site.com")
        # goto + click + expect_text
        assert len(script.steps) == 3
        assert script.steps[0].action == "goto"
        assert script.steps[1].action == "click"
        assert script.steps[2].action == "expect_text"

    def test_malformed_json_falls_back(self):
        tc = {
            "id": "TC-CORRUPT", "summary": "X",
            "test_steps": "1. Click 'Login'",
            "automation_steps_json": "{not really json[",
        }
        script = tc_to_script(tc, base_url="https://site.com")
        # Fell back to text parse → goto + click (no expect_text, no
        # expected_result on this TC).
        assert script.steps[0].action == "goto"
        assert any(s.action == "click" for s in script.steps)

    def test_wrong_shape_falls_back(self):
        """A JSON object instead of a list — defensive against schema drift."""
        tc = {
            "id": "TC-SHAPE", "summary": "X",
            "test_steps": "1. Click 'Save'",
            "automation_steps_json": json.dumps({"action": "click"}),
        }
        script = tc_to_script(tc, base_url="https://site.com")
        assert script.steps[0].action == "goto"  # legacy injection
        assert any(s.action == "click" for s in script.steps)


class TestDecodeRecordedStepsDirectly:
    def test_skips_items_without_action(self):
        payload = json.dumps([
            {"action": "click", "target": "label=X"},
            {"target": "label=Y"},          # no action — dropped
            {"action": "", "target": "z"},   # blank action — dropped
            {"action": "fill", "value": "v"},
        ])
        steps = _decode_recorded_steps(payload)
        assert [s.action for s in steps] == ["click", "fill"]

    def test_coerces_non_string_fields(self):
        payload = json.dumps([
            {"action": "fill", "target": "x", "value": 42, "raw": None},
        ])
        steps = _decode_recorded_steps(payload)
        assert steps[0].value == "42"
        assert steps[0].raw == ""

    def test_empty_string_returns_empty_list(self):
        assert _decode_recorded_steps("") == []

    def test_non_list_payload_returns_empty_list(self):
        assert _decode_recorded_steps(json.dumps({"x": 1})) == []
