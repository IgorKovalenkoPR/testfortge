"""PR-E — backend tests for /api/recorder-session/{start,finish}.

The extension is plain JS we don't pytest, but the endpoints it hits
have to be rock-solid: bad inputs must not corrupt SessionDrafts, and
the segmenter pipeline must produce a real review URL. These tests
pin both the success path (start → finish → review URL) and the four
big failure modes (flag off, missing project, unknown token, no steps).
"""
from __future__ import annotations

import json
import os
from unittest import mock

import pytest

from engine import db


# Minimal valid AutomationStep dict — same shape content.js sends.
def _make_step(action="click", target='role=button[name="Sign in"]',
                value="", label="role=button:Sign in"):
    return {
        "action": action,
        "target": target,
        "value": value,
        "raw": f'page.locator("{target}").{action}()',
        "comment": "",
        "target_alternates": ["text=Sign in", "css=button.primary"],
        "locator_label": label,
        "kind": "action",
        "assertion_type": "",
    }


@pytest.fixture
def ext_project(client):
    pid = db.upsert_project(
        name=f"ext-api-{os.urandom(4).hex()}",
        base_url="https://app.example.com",
    )
    with client.session_transaction() as s:
        s["project_id"] = pid
        s["active_project_id"] = pid
        s["_session_active_since"] = 9_999_999_999
    yield pid
    db.delete_project(pid)


class TestRecorderSessionStart:
    def test_returns_token_and_finish_url_when_flag_on(self, client, ext_project):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post("/api/recorder-session/start",
                                json={})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert "token" in body and len(body["token"]) >= 30
        assert body["project_id"] == ext_project
        assert body["finish_url"].endswith("/api/recorder-session/finish")
        assert "{token}" in body["review_url_template"]

    def test_returns_403_when_flag_off(self, client, ext_project):
        env = {k: v for k, v in os.environ.items()
                if k != "RECORDER_ENABLED"}
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ["FLASK_DEBUG"] = "1"
            resp = client.post("/api/recorder-session/start",
                                json={})
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "recorder_disabled"

    def test_returns_400_without_active_project(self, client):
        """No active project in session AND no project_id in body —
        must refuse so the extension never gets a token that can't
        be resolved on finish."""
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post("/api/recorder-session/start",
                                json={})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "no_active_project"

    def test_explicit_project_id_in_body_overrides_session(self, client):
        # Plant a project but don't bind it in session — body should win.
        pid = db.upsert_project(
            name=f"ext-body-{os.urandom(4).hex()}",
            base_url="https://x.test")
        try:
            with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
                resp = client.post("/api/recorder-session/start",
                                    json={"project_id": pid})
            assert resp.status_code == 200
            assert resp.get_json()["project_id"] == pid
        finally:
            db.delete_project(pid)

    def test_cors_headers_present(self, client, ext_project):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post("/api/recorder-session/start",
                                json={})
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    def test_preflight_options_returns_204(self, client, ext_project):
        resp = client.open("/api/recorder-session/start", method="OPTIONS")
        assert resp.status_code == 204
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"


class TestRecorderSessionFinish:
    def _start_and_get_token(self, client, ext_project):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post("/api/recorder-session/start",
                                json={"project_id": ext_project})
        assert resp.status_code == 200
        return resp.get_json()["token"]

    def test_full_round_trip_creates_session_draft(self, client, ext_project):
        """Happy path: start → finish with valid steps → review_url
        points at a SessionDraft the GET review-session route can
        actually load."""
        token = self._start_and_get_token(client, ext_project)

        # Stub the LLM segmenter — keep the test pure.
        from engine import session_segmenter as _seg
        with mock.patch.object(_seg, "_call_llm", return_value=[]):
            with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
                resp = client.post(
                    "/api/recorder-session/finish",
                    json={
                        "token": token,
                        "steps": [
                            _make_step(action="goto",
                                        target="https://x.test/login",
                                        label=""),
                            _make_step(),
                        ],
                    },
                )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["ok"] is True
        assert "/test-cases/review-session/" in body["review_url"]
        assert body["proposed_tc_count"] >= 1

        # Confirm the SessionDraft exists.
        draft_token = body["review_url"].rsplit("/", 1)[-1]
        draft = db.get_session_draft(draft_token)
        assert draft is not None
        assert draft["project_id"] == ext_project
        assert len(draft["proposed_tcs"]) >= 1

    def test_unknown_token_returns_404(self, client, ext_project):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post(
                "/api/recorder-session/finish",
                json={"token": "bogus-never-issued",
                       "steps": [_make_step()]},
            )
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "unknown_token"

    def test_empty_steps_rejected(self, client, ext_project):
        token = self._start_and_get_token(client, ext_project)
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post(
                "/api/recorder-session/finish",
                json={"token": token, "steps": []},
            )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "no_valid_steps"

    def test_malformed_step_dropped_silently(self, client, ext_project):
        """_decode_recorded_steps drops items without an action; the
        remaining valid step still creates a draft."""
        token = self._start_and_get_token(client, ext_project)
        from engine import session_segmenter as _seg
        with mock.patch.object(_seg, "_call_llm", return_value=[]):
            with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
                resp = client.post(
                    "/api/recorder-session/finish",
                    json={
                        "token": token,
                        "steps": [
                            {"raw": "garbage with no action"},  # dropped
                            _make_step(),                        # kept
                        ],
                    },
                )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_token_consumed_on_finish(self, client, ext_project):
        """A second finish with the same token must fail — protects
        against duplicate uploads from a buggy extension or a
        replay attack."""
        token = self._start_and_get_token(client, ext_project)
        from engine import session_segmenter as _seg
        with mock.patch.object(_seg, "_call_llm", return_value=[]):
            with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
                first = client.post(
                    "/api/recorder-session/finish",
                    json={"token": token, "steps": [_make_step()]})
                assert first.status_code == 200
                second = client.post(
                    "/api/recorder-session/finish",
                    json={"token": token, "steps": [_make_step()]})
        assert second.status_code == 404
        assert second.get_json()["error"] == "unknown_token"

    def test_returns_403_when_flag_off(self, client, ext_project):
        env = {k: v for k, v in os.environ.items()
                if k != "RECORDER_ENABLED"}
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ["FLASK_DEBUG"] = "1"
            resp = client.post(
                "/api/recorder-session/finish",
                json={"token": "x", "steps": [_make_step()]},
            )
        assert resp.status_code == 403

    def test_cors_headers_on_finish(self, client, ext_project):
        token = self._start_and_get_token(client, ext_project)
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post(
                "/api/recorder-session/finish",
                json={"token": token, "steps": [_make_step()]},
            )
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"


class TestUiTrigger:
    """The /test-cases page must surface the 🎬 Start session recording
    button when RECORDER_ENABLED=1 and hide it when off. Without the
    button, the extension has no entry point and the feature is
    silently dead."""

    def test_button_visible_when_flag_on(self, client, ext_project):
        # Plant a TC so the page renders the testcases tab.
        db.save_test_cases(ext_project, [{
            "id": "TC-001", "section": "Login", "section_num": 1,
            "summary": "Sign in", "preconditions": "",
            "test_steps": "1. Open", "test_data": "",
            "expected_result": "Welcome", "issues": "", "comment": "",
            "user_story_id": "US-1", "category": "Positive",
            "priority": "High", "status": "Unchecked",
            "testing_type": "Functional", "url_pattern": "",
            "trigger": "manual"},
        ])
        with client.session_transaction() as s:
            s["test_cases_data"] = db.load_test_cases(ext_project)
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.get("/test-cases")
        body = resp.get_data(as_text=True)
        assert 'id="ext-recorder-start"' in body
        assert 'id="ext-recorder-modal"' in body
        assert "/api/recorder-session/start" in body

    def test_csrf_protection_does_not_block_endpoints(self, app, ext_project):
        """Regression for PR-E hotfix: production has CSRFProtect on
        every POST, but recorder endpoints must be exempt because:
        * /finish is called cross-origin from the extension (no CSRF
          token possible);
        * /start is called via fetch() from the modal — keeping both
          exempt is consistent with /debug/walkthrough's pattern.

        Test re-enables CSRF (conftest disables it for the rest of the
        suite) and verifies the POSTs still succeed with no token."""
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            with app.test_client() as csrf_client:
                with csrf_client.session_transaction() as s:
                    s["project_id"] = ext_project
                    s["active_project_id"] = ext_project
                    s["_session_active_since"] = 9_999_999_999
                with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
                    # /start — must NOT 400 with CSRF error.
                    resp = csrf_client.post(
                        "/api/recorder-session/start",
                        json={"project_id": ext_project},
                    )
                assert resp.status_code == 200, (
                    f"CSRF blocked /start: {resp.get_data(as_text=True)}")
                token = resp.get_json()["token"]

                # /finish — same expectation.
                from engine import session_segmenter as _seg
                with mock.patch.object(_seg, "_call_llm", return_value=[]):
                    with mock.patch.dict(os.environ,
                                          {"RECORDER_ENABLED": "1"}):
                        resp = csrf_client.post(
                            "/api/recorder-session/finish",
                            json={"token": token,
                                   "steps": [_make_step()]},
                        )
                assert resp.status_code == 200, (
                    f"CSRF blocked /finish: {resp.get_data(as_text=True)}")
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

    def test_button_hidden_when_flag_off(self, client, ext_project):
        db.save_test_cases(ext_project, [{
            "id": "TC-001", "section": "Login", "section_num": 1,
            "summary": "Sign in", "preconditions": "", "test_steps": "",
            "test_data": "", "expected_result": "", "issues": "",
            "comment": "", "user_story_id": "US-1",
            "category": "Positive", "priority": "High",
            "status": "Unchecked", "testing_type": "Functional",
            "url_pattern": "", "trigger": "manual"},
        ])
        with client.session_transaction() as s:
            s["test_cases_data"] = db.load_test_cases(ext_project)
        env = {k: v for k, v in os.environ.items()
                if k != "RECORDER_ENABLED"}
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ["FLASK_DEBUG"] = "1"
            resp = client.get("/test-cases")
        body = resp.get_data(as_text=True)
        assert 'id="ext-recorder-start"' not in body
