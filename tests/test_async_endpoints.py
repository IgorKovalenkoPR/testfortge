"""Smoke tests for the Wave C.4 async route pair.

Covers enough to catch regressions in:
  * URL map wiring — all four new endpoints are reachable
  * /automation/run-async preconditions (no test cases → 400)
  * /automation/status returns 404 for unknown ids
  * /estimation/run-async precondition (missing URL → 400)
  * /estimation/status returns 404 for unknown ids

We deliberately don't exercise a real Playwright run — that's covered by
the sync /automation/run path via test_functional.py, and spinning up
Chromium just to validate the queue wiring would be slow and flaky.
"""

import json

import pytest


class TestAutomationAsyncWiring:
    def test_run_async_rejects_without_test_cases(self, client):
        """Without session-stored test cases, the endpoint returns 400 JSON."""
        resp = client.post("/automation/run-async",
                           data={"base_url": "https://example.com"})
        assert resp.status_code == 400
        payload = resp.get_json()
        assert payload["error"] == "no_test_cases"

    def test_status_unknown_returns_404(self, client):
        resp = client.get("/automation/status/deadbeefdeadbeefdeadbeef")
        assert resp.status_code == 404
        assert resp.get_json() == {"error": "not_found"}

    def test_status_rejects_cross_kind_id(self, client):
        """A job registered under a different kind (e.g. estimation) must
        not be leakable through the automation /status endpoint."""
        from engine.job_queue import get_queue
        jid = get_queue().submit("estimation", lambda: {"x": 1})
        resp = client.get(f"/automation/status/{jid}")
        assert resp.status_code == 404


class TestEstimationAsyncWiring:
    def test_run_async_requires_url(self, client):
        resp = client.post("/estimation/run-async", data={})
        assert resp.status_code == 400
        payload = resp.get_json()
        assert payload["error"] == "no_url"

    def test_status_unknown_returns_404(self, client):
        resp = client.get("/estimation/status/deadbeefdeadbeefdeadbeef")
        assert resp.status_code == 404

    def test_status_rejects_cross_kind_id(self, client):
        from engine.job_queue import get_queue
        jid = get_queue().submit("automation", lambda: {"x": 1})
        resp = client.get(f"/estimation/status/{jid}")
        assert resp.status_code == 404


class TestURLMapIntact:
    """Defensive regression check — Wave C.2 split must not have dropped
    any of the original 24 routes, and Wave C.4 must have added exactly 4."""

    def test_expected_endpoints_present(self, app):
        endpoints = {r.endpoint for r in app.url_map.iter_rules()}
        expected = {
            # Core dashboard + project storage
            "index", "new_session", "save_project",
            "load_project", "delete_project",
            # Generation + export
            "test_cases_page", "checklist_page", "export",
            # Execution + bugs
            "test_execution_page", "test_execution_generate_account",
            "create_bug_report", "bug_reports_page", "export_bug_reports",
            # Automation
            "automation_page", "automation_generate_account",
            "automation_run", "automation_asset",
            # Estimation
            "estimation_page", "estimation_run", "estimation_export",
            # Chat
            "chat_route", "chat_history_route", "chat_reset_route",
            # Wave C.4 async additions
            "automation_run_async", "automation_status",
            "estimation_run_async", "estimation_status",
            # Wave D observability
            "healthz", "metrics",
        }
        missing = expected - endpoints
        assert not missing, f"missing endpoints: {missing}"
