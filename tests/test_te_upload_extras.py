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
        assert 'name="auto_run"' in body


class TestAutoRunPassthrough:
    def _csv(self):
        return b"ID,Section,Summary,Steps,Expected\nTC-1,A,Empty,1.x,Err\n"

    def test_upload_with_auto_run_redirects_back_with_flag(self, client):
        r = client.post(
            "/test-cases/upload",
            data={"upload_file": (io.BytesIO(self._csv()), "p.csv"),
                  "upload_mode": "replace",
                  "auto_run": "1"},
            headers={"Referer": "http://localhost/test-execution"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/test-execution?auto_run=1")

    def test_upload_without_auto_run_redirects_clean(self, client):
        r = client.post(
            "/test-cases/upload",
            data={"upload_file": (io.BytesIO(self._csv()), "p.csv"),
                  "upload_mode": "replace"},
            headers={"Referer": "http://localhost/test-execution"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["Location"] == "/test-execution"

    def test_checklist_upload_with_auto_run(self, client):
        csv = b"ID,Section,Objective\nCL-1,A,Sanity\n"
        r = client.post(
            "/checklist/upload",
            data={"upload_file": (io.BytesIO(csv), "p.csv"),
                  "upload_mode": "replace",
                  "auto_run": "1"},
            headers={"Referer": "http://localhost/test-execution"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/test-execution?auto_run=1")
