"""Smoke tests for the Wave C.4 async route pair.

Covers enough to catch regressions in:
  * URL map wiring — all four new endpoints are reachable
  * /automation/run-async preconditions (no test cases → 400)
  * /automation/status returns 404 for unknown ids
  * /estimation/run-async precondition (missing URL → 400)
  * /estimation/status returns 404 for unknown ids
  * /test-cases/run-async + /test-cases/status — full end-to-end path
    (regression: a polling pile-up + slow DB write inside the polling
    endpoint left the modal frozen on prod with no result rendered)
  * /checklist/run-async + /checklist/status — same end-to-end check
"""

import pytest


class TestAutomationAsyncWiring:
    def test_run_async_rejects_without_test_cases(self, client):
        resp = client.post("/automation/run-async",
                           data={"base_url": "https://example.com"})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "no_test_cases"

    def test_status_unknown_returns_404(self, client):
        resp = client.get("/automation/status/deadbeefdeadbeefdeadbeef")
        assert resp.status_code == 404
        assert resp.get_json() == {"error": "not_found"}

    def test_status_rejects_cross_kind_id(self, client):
        from engine.job_queue import get_queue
        jid = get_queue().submit("estimation", lambda: {"x": 1})
        resp = client.get(f"/automation/status/{jid}")
        assert resp.status_code == 404


class TestEstimationAsyncWiring:
    def test_run_async_requires_url(self, client):
        resp = client.post("/estimation/run-async", data={})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "no_url"

    def test_status_unknown_returns_404(self, client):
        resp = client.get("/estimation/status/deadbeefdeadbeefdeadbeef")
        assert resp.status_code == 404

    def test_status_rejects_cross_kind_id(self, client):
        from engine.job_queue import get_queue
        jid = get_queue().submit("automation", lambda: {"x": 1})
        resp = client.get(f"/estimation/status/{jid}")
        assert resp.status_code == 404


class TestURLMapIntact:
    def test_expected_endpoints_present(self, app):
        endpoints = {r.endpoint for r in app.url_map.iter_rules()}
        expected = {
            "index", "new_session", "save_project",
            "load_project", "delete_project",
            "test_cases_page", "checklist_page", "export",
            "test_execution_page", "test_execution_generate_account",
            "create_bug_report", "bug_reports_page", "export_bug_reports",
            "automation_page", "automation_generate_account",
            "automation_run", "automation_asset",
            "estimation_page", "estimation_run", "estimation_export",
            "chat_route", "chat_history_route", "chat_reset_route",
            "automation_run_async", "automation_status",
            "estimation_run_async", "estimation_status",
            "healthz", "metrics",
        }
        missing = expected - endpoints
        assert not missing, f"missing endpoints: {missing}"


class TestGenerationAsyncEndToEnd:
    """End-to-end smoke for the test-case + checklist async pair.

    Regression target: a bug reported on prod where the modal showed
    'Drafting user stories...' indefinitely and no cases ever appeared.
    Root cause was a pile-up of stalled /status polls plus a slow DB
    write inside the polling endpoint; the fix moved persistence into
    the worker and made /status return a redirect_url so the client
    never has to guess where to go next."""

    def _wait_done(self, client, status_url, max_polls=60):
        import time
        last = None
        for _ in range(max_polls):
            r = client.get(status_url)
            last = r.get_json()
            if last.get("status") in ("done", "failed"):
                return last
            time.sleep(0.1)
        raise AssertionError(f"job did not finish: last={last}")

    def test_test_cases_async_full_flow(self, client):
        resp = client.post("/test-cases/run-async",
                           data={"input_text": "User must log in with email."})
        assert resp.status_code == 200
        job_id = resp.get_json()["job_id"]

        final = self._wait_done(client, f"/test-cases/status/{job_id}")
        assert final["status"] == "done"
        assert final.get("redirect_url", "").endswith("/test-cases")

        with client.session_transaction() as s:
            assert s.get("test_cases_data"), "test cases not in session"

        page = client.get("/test-cases")
        assert page.status_code == 200
        assert b'class="stat-card"' in page.data, "stat cards missing"

    def test_checklist_async_full_flow(self, client):
        resp = client.post("/checklist/run-async",
                           data={"input_text": "User must register an account."})
        assert resp.status_code == 200
        job_id = resp.get_json()["job_id"]

        final = self._wait_done(client, f"/checklist/status/{job_id}")
        assert final["status"] == "done"
        assert final.get("redirect_url", "").endswith("/checklist")

        with client.session_transaction() as s:
            assert s.get("checklist_data"), "checklist not in session"

    def test_status_404_carries_machine_readable_error(self, client):
        r = client.get("/test-cases/status/deadbeefdeadbeefdeadbeef")
        assert r.status_code == 404
        assert r.get_json() == {"error": "not_found"}

    def test_status_running_does_not_carry_redirect_url(self, client):
        from engine.job_queue import get_queue
        import threading
        gate = threading.Event()

        def slow_worker():
            gate.wait(timeout=5.0)
            return {"tc_dicts": []}

        jid = get_queue().submit("tc_gen", slow_worker,
                                 meta={"session_id": "test"})
        try:
            r = client.get(f"/test-cases/status/{jid}")
            payload = r.get_json()
            assert payload["status"] in ("pending", "running")
            assert "redirect_url" not in payload
        finally:
            gate.set()
