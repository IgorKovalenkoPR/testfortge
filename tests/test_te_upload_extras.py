"""Test Execution upload UX — drag-and-drop anchors, auto-run flag,
pack-status badge. Regression target: feature #15."""
import io
import pytest


class TestPackBadge:
    def test_badge_visible_when_pack_loaded(self, client):
        with client.session_transaction() as s:
            s["test_cases_data"] = [{
                "id":"TC1","section":"A","section_num":1,"summary":"s",
                "preconditions":"","test_steps":"","test_data":"",
                "expected_result":"","issues":"","comment":"",
                "user_story_id":"","category":"Positive","priority":"High",
                "status":"Unchecked","testing_type":"Functional",
            }] * 7
            s["_session_active_since"] = 9_999_999_999
        body = client.get("/test-execution?lang=en").get_data(as_text=True)
        assert "pack-status-badge" in body
        assert "<strong>7</strong>" in body
        assert "Pack loaded" in body
        assert "Clear pack" in body

    def test_badge_hidden_when_empty(self, client):
        body = client.get("/test-execution?lang=en").get_data(as_text=True)
        assert "pack-status-badge" not in body


class TestUploadDragAndDrop:
    def test_dnd_zones_rendered_on_empty_state(self, client):
        body = client.get("/test-execution?lang=en").get_data(as_text=True)
        assert 'data-te-upload="tc"' in body
        assert 'data-te-upload="cl"' in body
        assert 'class="file-upload-area te-drop-zone"' in body
        # Bug-fix: auto_run was removed (operator wants Run-only
        # via the existing button, not auto-triggered post-upload).
        assert 'name="auto_run"' not in body


class TestUploadFromExecutionLandsHome:
    """After the auto-run-on-upload feature was reverted (operator
    asked for upload-only behaviour, Run stays manual), every upload
    POST from /test-execution must land back on /test-execution and
    must NOT produce a test_run side-effect. The auto_run form field
    was removed; sending it via API now has no effect."""

    def _tc_csv(self):
        return b"ID,Section,Summary,Steps,Expected\nTC-1,A,Empty,1.x,Err\n"

    def _cl_csv(self):
        return b"ID,Section,Objective\nCL-1,A,Sanity\n"

    def test_upload_tc_from_execution_lands_home(self, client):
        r = client.post(
            "/test-cases/upload",
            data={"upload_file": (io.BytesIO(self._tc_csv()), "p.csv"),
                  "upload_mode": "replace"},
            headers={"Referer": "http://localhost/test-execution"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["Location"] == "/test-execution"
        with client.session_transaction() as s:
            assert not s.get("test_runs"), "upload must not auto-run"

    def test_upload_cl_from_execution_lands_home(self, client):
        r = client.post(
            "/checklist/upload",
            data={"upload_file": (io.BytesIO(self._cl_csv()), "p.csv"),
                  "upload_mode": "replace"},
            headers={"Referer": "http://localhost/test-execution"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["Location"] == "/test-execution"
        with client.session_transaction() as s:
            assert not s.get("test_runs"), "upload must not auto-run"

    def test_legacy_auto_run_query_param_is_ignored(self, client):
        """Even if a stale client posts auto_run=1, the route now
        ignores it — the server-side auto-run path was decoupled."""
        r = client.post(
            "/test-cases/upload",
            data={"upload_file": (io.BytesIO(self._tc_csv()), "p.csv"),
                  "upload_mode": "replace",
                  "auto_run": "1"},
            headers={"Referer": "http://localhost/test-execution"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["Location"] == "/test-execution"
        with client.session_transaction() as s:
            assert not s.get("test_runs"), "auto_run=1 must be a no-op"


class TestClearPackButton:
    """Operator video: clicking Clear pack on /test-execution returned
    405 because /new-session is POST-only and the button was a plain
    GET <a>. Fix wraps it in an inline POST form."""

    def test_clear_pack_renders_as_post_form(self, client):
        with client.session_transaction() as s:
            s["test_cases_data"] = [{"id":"TC-1","section":"X","section_num":1,
                "summary":"s","preconditions":"","test_steps":"",
                "test_data":"","expected_result":"","issues":"",
                "comment":"","user_story_id":"","category":"Positive",
                "priority":"High","status":"Unchecked","testing_type":"Functional"}]
            s["_session_active_since"] = 9_999_999_999
        body = client.get("/test-execution?lang=en").get_data(as_text=True)
        # The Clear pack control is a POST <form>, not a <a href>.
        assert 'action="/new-session"' in body
        # Must include CSRF input.
        assert 'name="csrf_token"' in body
        # The <a href="/new-session"> form is gone.
        assert 'href="/new-session"' not in body

    def test_clear_pack_post_succeeds(self, client):
        """POST /new-session must respond cleanly (302) — no 405."""
        with client.session_transaction() as s:
            s["test_cases_data"] = [{"id":"TC-1","section":"X","section_num":1,
                "summary":"s","preconditions":"","test_steps":"",
                "test_data":"","expected_result":"","issues":"",
                "comment":"","user_story_id":"","category":"Positive",
                "priority":"High","status":"Unchecked","testing_type":"Functional"}]
            s["_session_active_since"] = 9_999_999_999
        r = client.post("/new-session", follow_redirects=False)
        assert r.status_code == 302
        with client.session_transaction() as s:
            assert not s.get("test_cases_data")

    def test_get_new_session_returns_405(self, client):
        """Defensive — confirm the GET route really would 405. If the
        route ever switches to allowing GET we want to know so this
        test reminds us to re-evaluate the form-vs-anchor decision."""
        r = client.get("/new-session", follow_redirects=False)
        assert r.status_code == 405
