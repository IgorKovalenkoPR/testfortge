"""Unit tests for Automation QA converter and metrics."""
from engine.automation_qa import (
    parse_manual_step, tc_to_script, _detect_action,
)
from engine.automation_report import compute_automation_metrics, detect_flaky


def test_detect_action_click():
    assert _detect_action("Click the 'Login' button") == "click"

def test_detect_action_fill():
    assert _detect_action("Enter 'foo@bar.com' into Email field") == "fill"

def test_detect_action_navigate():
    assert _detect_action("Navigate to https://example.com") == "goto"

def test_parse_fill_extracts_value():
    step = parse_manual_step("2. Enter 'alice@test.com' into Email field")
    assert step.action == "fill"
    assert step.value == "alice@test.com"

def test_parse_click_extracts_target():
    step = parse_manual_step("3. Click the 'Submit' button")
    assert step.action == "click"
    assert "submit" in step.target.lower()

def test_tc_to_script_prepends_goto():
    tc = {"id": "TC_001", "summary": "Verify login",
          "test_steps": "1. Click Login\n2. Enter 'x' into email",
          "expected_result": "User is logged in"}
    s = tc_to_script(tc, base_url="https://site.com")
    assert s.steps[0].action == "goto"
    assert s.steps[-1].action == "expect_text"

def test_metrics_coverage():
    m = compute_automation_metrics(
        {"total": 10, "passed": 7, "failed": 2, "blocked": 1, "duration_ms": 5000},
        total_tc=20,
    )
    assert m["automation_coverage"] == 50.0
    assert m["pass_rate"] == 70.0

def test_flaky_detection():
    hist = [
        {"scripts": [{"tc_id": "TC_001", "status": "passed"}]},
        {"scripts": [{"tc_id": "TC_001", "status": "failed"}]},
    ]
    assert "TC_001" in detect_flaky(hist)
