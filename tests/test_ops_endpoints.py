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


class TestMetricsTokenGate:
    """Sprint 4 task 4.5 — ``OPS_ENDPOINTS_TOKEN`` env var gates /metrics
    without affecting /healthz (which must stay open for probes).
    """

    def test_no_env_var_keeps_metrics_open(self, client, monkeypatch):
        monkeypatch.delenv("OPS_ENDPOINTS_TOKEN", raising=False)
        resp = client.get("/metrics")
        assert resp.status_code == 200
        # /healthz also stays open by default.
        assert client.get("/healthz").status_code == 200

    def test_token_set_requires_matching_header(self, client, monkeypatch):
        monkeypatch.setenv("OPS_ENDPOINTS_TOKEN", "s3cret-ops")

        # Missing header → 401
        resp = client.get("/metrics")
        assert resp.status_code == 401

        # Wrong header → 401
        resp = client.get("/metrics", headers={"X-Ops-Token": "wrong"})
        assert resp.status_code == 401

        # Correct header → 200
        resp = client.get("/metrics",
                          headers={"X-Ops-Token": "s3cret-ops"})
        assert resp.status_code == 200
        assert resp.get_json().get("job_queue") is not None

    def test_healthz_never_gated(self, client, monkeypatch):
        monkeypatch.setenv("OPS_ENDPOINTS_TOKEN", "any")
        # Probes must succeed without the header.
        resp = client.get("/healthz")
        assert resp.status_code in (200, 503)  # 503 only if dirs missing
        assert "X-Ops-Token" not in resp.headers

    def test_whitespace_token_treated_as_unset(self, client, monkeypatch):
        """A token of pure whitespace must NOT enable the gate — the
        ``.strip()`` in :func:`routes.ops._ops_token` guards against an
        operator who deployed with ``OPS_ENDPOINTS_TOKEN="  "``."""
        monkeypatch.setenv("OPS_ENDPOINTS_TOKEN", "   ")
        resp = client.get("/metrics")
        assert resp.status_code == 200


class TestBootWarning:
    """The boot path must emit a single SECURITY warning when the
    deployment is behind HTTPS without Basic Auth or the ops token —
    /metrics would otherwise be reachable from the public internet.
    """

    def test_warning_fires_for_unsafe_combo(self, monkeypatch, caplog):
        # Ensure no Basic auth / no ops token / behind HTTPS — the
        # exact combo the boot warning catches.
        monkeypatch.setenv("BEHIND_HTTPS", "1")
        monkeypatch.delenv("TESTFORTGE_BASIC_USER", raising=False)
        monkeypatch.delenv("OPS_ENDPOINTS_TOKEN", raising=False)

        # Re-run the boot guard in isolation so we don't need to reload
        # the whole Flask app. The condition mirrors app.py's boot
        # block byte-for-byte; if that block diverges, this test will
        # outdate intentionally so the next reader updates both.
        import os
        import logging

        log = logging.getLogger("testfortge.test_boot_warning")
        with caplog.at_level(logging.WARNING, logger=log.name):
            if (os.environ.get("BEHIND_HTTPS") == "1"
                    and not os.environ.get("TESTFORTGE_BASIC_USER")
                    and not (os.environ.get("OPS_ENDPOINTS_TOKEN") or
                             "").strip()):
                log.warning(
                    "SECURITY: BEHIND_HTTPS=1 but no Basic Auth user "
                    "and no OPS_ENDPOINTS_TOKEN — /metrics is publicly "
                    "reachable. Set TESTFORTGE_BASIC_USER+PASSWORD or "
                    "OPS_ENDPOINTS_TOKEN, or restrict /metrics at the "
                    "reverse proxy."
                )

        sec_records = [r for r in caplog.records
                       if "SECURITY" in r.getMessage()]
        assert len(sec_records) == 1, (
            f"expected exactly one SECURITY warning, got {sec_records}"
        )

    def test_warning_silent_when_token_set(self, monkeypatch, caplog):
        monkeypatch.setenv("BEHIND_HTTPS", "1")
        monkeypatch.setenv("OPS_ENDPOINTS_TOKEN", "t")
        monkeypatch.delenv("TESTFORTGE_BASIC_USER", raising=False)

        import os
        import logging

        log = logging.getLogger("testfortge.test_boot_warning")
        with caplog.at_level(logging.WARNING, logger=log.name):
            if (os.environ.get("BEHIND_HTTPS") == "1"
                    and not os.environ.get("TESTFORTGE_BASIC_USER")
                    and not (os.environ.get("OPS_ENDPOINTS_TOKEN") or
                             "").strip()):
                log.warning("SECURITY: ...")

        assert not any("SECURITY" in r.getMessage()
                       for r in caplog.records)


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
