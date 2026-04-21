"""Tests for Wave D operations endpoints (/healthz and /metrics).

Covers:
  * /healthz returns 200 with all checks true in the normal test setup
  * /healthz returns 503 when a required dir is missing/unwritable
  * /metrics shape — job_queue.by_status has all four states keyed
  * /metrics reflects submitted jobs of different kinds
"""

import os
import tempfile

import pytest


class TestHealthz:
    def test_ok_when_dirs_writable(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert all(data["checks"].values())
        assert data["uptime_seconds"] >= 0

    def test_degraded_when_session_dir_missing(self, app, client):
        original = app.config.get("SESSION_FILE_DIR")
        # Point to a path that does not exist
        app.config["SESSION_FILE_DIR"] = os.path.join(
            tempfile.gettempdir(), "this_dir_should_not_exist_12345xyz")
        try:
            resp = client.get("/healthz")
            assert resp.status_code == 503
            data = resp.get_json()
            assert data["status"] == "degraded"
            assert data["checks"]["session_dir_writable"] is False
        finally:
            app.config["SESSION_FILE_DIR"] = original


class TestMetrics:
    def test_shape(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "uptime_seconds" in data
        jq = data["job_queue"]
        # All four lifecycle states keyed, even if zero
        assert set(jq["by_status"].keys()) >= {
            "pending", "running", "done", "failed"}
        assert "in_flight" in jq
        assert "limits" in data

    def test_reflects_submitted_jobs(self, client):
        from engine.job_queue import get_queue, DONE
        import time
        jid = get_queue().submit("automation", lambda: "x")
        # Wait briefly for the worker to finish — /metrics counts
        # terminal jobs the same way.
        deadline = time.time() + 2
        while time.time() < deadline:
            j = get_queue().get(jid)
            if j and j.status == DONE:
                break
            time.sleep(0.02)
        resp = client.get("/metrics")
        data = resp.get_json()
        assert data["job_queue"]["by_kind"].get("automation", 0) >= 1


class TestJSONLogFormat:
    """The JSON formatter must produce a single-line JSON doc per record."""

    def test_json_formatter_shape(self):
        import json
        import logging
        from engine.log import _JSONFormatter

        rec = logging.LogRecord(
            name="testfortge.unit", level=logging.INFO, pathname=__file__,
            lineno=1, msg="hello %s", args=("world",), exc_info=None,
        )
        out = _JSONFormatter().format(rec)
        doc = json.loads(out)
        assert doc["level"] == "INFO"
        assert doc["logger"] == "testfortge.unit"
        assert doc["message"] == "hello world"
        assert "ts" in doc

    def test_json_formatter_includes_exc_info(self):
        import json
        import logging
        import sys
        from engine.log import _JSONFormatter

        try:
            raise ValueError("nope")
        except ValueError:
            exc_info = sys.exc_info()

        rec = logging.LogRecord(
            name="t", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="boom", args=(), exc_info=exc_info,
        )
        out = _JSONFormatter().format(rec)
        doc = json.loads(out)
        assert "ValueError: nope" in doc["exc_info"]
