"""PR-B/7 tests — Record button block in templates/test_cases.html.

Pins the contract:

* The Recorder ``<details>`` block renders per-TC only when the host has
  RECORDER_ENABLED=1.
* The pre-filled CLI command carries the active project_id + TC external
  id so the tester can copy it without editing project metadata.
* TCs with recorded steps (``automation_steps_json`` non-empty) surface
  a "Has recorded steps ✓" badge — testers see at a glance which TCs
  already have a capture.
"""
from __future__ import annotations

import json
import os
from unittest import mock

import pytest

from engine import db


@pytest.fixture
def seeded_session(client):
    """Plant a project + two TCs (one with recording, one without) in
    the session so the /test-cases GET renders both rows."""
    pid = db.upsert_project(
        name=f"recorder-ui-{os.urandom(4).hex()}",
        base_url="https://app.example.com",
    )
    db.save_test_cases(pid, [
        {"id": "TC-PLAIN", "section": "Login", "section_num": 1,
         "summary": "Sign in", "preconditions": "",
         "test_steps": "1. Open\n2. Submit", "test_data": "",
         "expected_result": "Welcome", "issues": "", "comment": "",
         "user_story_id": "US-1", "category": "Positive",
         "priority": "High", "status": "Unchecked",
         "testing_type": "Functional", "url_pattern": "",
         "trigger": "manual"},
        {"id": "TC-RECORDED", "section": "Checkout", "section_num": 2,
         "summary": "Pay", "preconditions": "",
         "test_steps": "1. Checkout", "test_data": "",
         "expected_result": "Order created", "issues": "", "comment": "",
         "user_story_id": "US-2", "category": "Positive",
         "priority": "High", "status": "Unchecked",
         "testing_type": "Functional", "url_pattern": "",
         "trigger": "manual"},
    ])
    db.update_tc_automation_steps(pid, "TC-RECORDED", [
        {"action": "goto", "target": "https://app.example.com/cart",
         "value": "", "raw": "page.goto(...)", "comment": ""}
    ])
    with client.session_transaction() as s:
        s["project_id"] = pid
        s["test_cases_data"] = db.load_test_cases(pid)
        s["_session_active_since"] = 9_999_999_999
    yield pid
    db.delete_project(pid)


class TestRecorderBlockVisibility:
    def test_renders_when_flag_on(self, client, seeded_session):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.get("/test-cases")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "tc-recorder-edit" in body
        assert "Record steps" in body
        # CLI command pre-fills the TC ids.
        assert "--tc TC-PLAIN" in body
        assert "--tc TC-RECORDED" in body

    def test_hidden_when_flag_off(self, client, seeded_session):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "0"}):
            resp = client.get("/test-cases")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "tc-recorder-edit" not in body
        assert "Record steps" not in body

    def test_hidden_when_flag_unset(self, client, seeded_session):
        # Strip the env var entirely — default state must hide the
        # block so a host that never opted in stays clean.
        env = {k: v for k, v in os.environ.items() if k != "RECORDER_ENABLED"}
        with mock.patch.dict(os.environ, env, clear=True):
            # Also re-set FLASK_DEBUG so init_db's guard still passes.
            os.environ["FLASK_DEBUG"] = "1"
            resp = client.get("/test-cases")
        assert resp.status_code == 200
        assert "tc-recorder-edit" not in resp.get_data(as_text=True)


class TestRecordedStepsBadge:
    def test_badge_shown_when_steps_present(self, client, seeded_session):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.get("/test-cases")
        body = resp.get_data(as_text=True)
        # Sanity: the badge must appear exactly once across the entire
        # response — once for TC-RECORDED, never for TC-PLAIN.
        assert body.count("Has recorded steps") == 1
        # And that one occurrence must sit between the TC-RECORDED
        # card opening and the next TC card (or end of section).
        badge_idx = body.index("Has recorded steps")
        tc_recorded_idx = body.index('id="TC-RECORDED"')
        tc_plain_idx = body.index('id="TC-PLAIN"')
        # Both cards exist; whichever appears later defines the card
        # boundary the badge must fall before.
        next_card_idx = max(tc_recorded_idx, tc_plain_idx)
        if next_card_idx == tc_recorded_idx:
            # TC-RECORDED renders second — the badge must come after
            # its anchor and before EOF.
            assert badge_idx > tc_recorded_idx
        else:
            # TC-RECORDED renders first — badge must sit between its
            # anchor and the next TC's anchor.
            assert tc_recorded_idx < badge_idx < tc_plain_idx


class TestCliCommandPrefill:
    def test_command_includes_project_id(self, client, seeded_session):
        pid = seeded_session
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.get("/test-cases")
        body = resp.get_data(as_text=True)
        assert f"--project {pid}" in body
        # And the JS data-attribute carries the same so the click
        # handler's dynamic-rebuild path produces the same command.
        assert f'data-project="{pid}"' in body
