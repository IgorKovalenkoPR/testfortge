"""PR-F Phase 2 — browser-control Flask endpoints + MCP tools.

The extension talks to Flask's /api/browser/{poll,result}; the MCP
controller talks to the shared DB directly via the browser_* tools.
These tests pin both halves without a real browser: the endpoints are
exercised with a client, and the MCP tools are exercised with the
extension simulated (instant dequeue+complete) so the blocking await
resolves deterministically.
"""
from __future__ import annotations

import os
from unittest import mock

import pytest

from engine import db
from mcp_server import server as mcp_server


@pytest.fixture
def control_on():
    with mock.patch.dict(os.environ, {"BROWSER_CONTROL_ENABLED": "1"}):
        yield


@pytest.fixture
def bc_project(client):
    pid = db.upsert_project(
        name=f"bcapi-{os.urandom(4).hex()}",
        base_url="https://app.example.com",
    )
    yield pid
    db.delete_project(pid)


def _session(pid, tok="ctl_api"):
    db.create_browser_control_session(pid, tok)
    return tok


class TestPollEndpoint:
    def test_403_when_disabled(self, client, bc_project):
        tok = _session(bc_project, "ctl_off")
        env = {k: v for k, v in os.environ.items()
                if k != "BROWSER_CONTROL_ENABLED"}
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ["FLASK_DEBUG"] = "1"
            resp = client.post("/api/browser/poll", json={"token": tok})
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "control_disabled"

    def test_unknown_token_404(self, client, control_on, bc_project):
        resp = client.post("/api/browser/poll", json={"token": "nope"})
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "unknown_or_stopped"

    def test_empty_queue_returns_null_command(self, client, control_on,
                                              bc_project):
        tok = _session(bc_project, "ctl_empty")
        resp = client.post("/api/browser/poll", json={"token": tok})
        assert resp.status_code == 200
        assert resp.get_json()["command"] is None

    def test_dequeues_and_dispatches(self, client, control_on, bc_project):
        tok = _session(bc_project, "ctl_deq")
        cid = db.enqueue_browser_command(tok, "navigate",
                                         {"url": "https://app.example.com/x"})
        resp = client.post("/api/browser/poll", json={"token": tok})
        assert resp.status_code == 200
        cmd = resp.get_json()["command"]
        assert cmd["command_id"] == cid
        assert cmd["verb"] == "navigate"
        # Marked dispatched — a second poll finds nothing.
        assert db.get_browser_command(cid)["status"] == "dispatched"
        again = client.post("/api/browser/poll", json={"token": tok})
        assert again.get_json()["command"] is None

    def test_poll_touches_liveness(self, client, control_on, bc_project):
        """A poll bumps last_seen so the controller sees the browser as
        live even if it had gone quiet."""
        tok = _session(bc_project, "ctl_live")
        # Backdate last_seen so it would read stale without a touch.
        from engine.db import (BrowserControlSession, _utcnow, session_scope)
        from datetime import timedelta
        from sqlalchemy import select
        with session_scope() as sess:
            row = sess.execute(select(BrowserControlSession).where(
                BrowserControlSession.token == tok)).scalar_one()
            row.last_seen_at = _utcnow() - timedelta(minutes=5)
        assert db.get_browser_control_session(tok)["live"] is False
        client.post("/api/browser/poll", json={"token": tok})
        assert db.get_browser_control_session(tok)["live"] is True

    def test_cors_and_preflight(self, client, control_on, bc_project):
        pre = client.open("/api/browser/poll", method="OPTIONS")
        assert pre.status_code == 204
        assert pre.headers.get("Access-Control-Allow-Origin") == "*"


class TestResultEndpoint:
    def test_completes_command(self, client, control_on, bc_project):
        tok = _session(bc_project, "ctl_res")
        cid = db.enqueue_browser_command(tok, "read_page", {})
        db.dequeue_browser_command(tok)
        resp = client.post("/api/browser/result", json={
            "command_id": cid, "ok": True,
            "result": {"title": "Home", "elements": []},
        })
        assert resp.status_code == 200
        done = db.get_browser_command(cid)
        assert done["status"] == "done"
        assert done["result"]["title"] == "Home"

    def test_error_result(self, client, control_on, bc_project):
        tok = _session(bc_project, "ctl_err")
        cid = db.enqueue_browser_command(tok, "click", {"ref": "ref_9"})
        db.dequeue_browser_command(tok)
        resp = client.post("/api/browser/result", json={
            "command_id": cid, "ok": False, "error": "ref_not_found: ref_9",
        })
        assert resp.status_code == 200
        done = db.get_browser_command(cid)
        assert done["status"] == "error"
        assert "ref_not_found" in done["error"]

    def test_unknown_command_404(self, client, control_on, bc_project):
        resp = client.post("/api/browser/result",
                            json={"command_id": "ghost", "ok": True})
        assert resp.status_code == 404

    def test_csrf_exempt(self, app, bc_project):
        """Both endpoints must survive CSRFProtect (extension can't carry
        a token). conftest disables CSRF; re-enable it here."""
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            tok = _session(bc_project, "ctl_csrf")
            cid = db.enqueue_browser_command(tok, "wait", {"ms": 1})
            with app.test_client() as c:
                with mock.patch.dict(os.environ,
                                      {"BROWSER_CONTROL_ENABLED": "1"}):
                    poll = c.post("/api/browser/poll", json={"token": tok})
                    assert poll.status_code == 200, poll.get_data(as_text=True)
                    res = c.post("/api/browser/result",
                                 json={"command_id": cid, "ok": True,
                                        "result": {}})
                    assert res.status_code == 200, res.get_data(as_text=True)
        finally:
            app.config["WTF_CSRF_ENABLED"] = False


class TestMcpTools:
    def test_control_start_creates_session_and_open_url(self, bc_project):
        out = mcp_server.browser_control_start(bc_project,
                                               "https://app.example.com/login")
        assert out["ok"] is True
        assert out["token"]
        # Handoff URL carries the token in the fragment, never the query.
        assert "testfortge-control-token=" + out["token"] in out["open_url"]
        assert out["open_url"].split("#", 1)[1].startswith("testfortge-control-token")
        # Session is resolvable + live (just created).
        st = mcp_server.browser_control_status(out["token"])
        assert st["ok"] is True and st["live"] is True

    def test_control_start_unknown_project(self):
        out = mcp_server.browser_control_start("f" * 32, "https://x.test")
        assert out["ok"] is False
        assert out["error"] == "unknown_project"

    def test_navigate_requires_http(self, bc_project):
        with pytest.raises(ValueError):
            mcp_server.browser_navigate("tok", "ftp://nope")

    def test_command_not_attached_errors_fast(self, bc_project):
        """When the browser hasn't polled recently (not live), a drive
        command fails fast instead of blocking to timeout."""
        tok = _session(bc_project, "ctl_notlive")
        from engine.db import (BrowserControlSession, _utcnow, session_scope)
        from datetime import timedelta
        from sqlalchemy import select
        with session_scope() as sess:
            row = sess.execute(select(BrowserControlSession).where(
                BrowserControlSession.token == tok)).scalar_one()
            row.last_seen_at = _utcnow() - timedelta(minutes=5)
        out = mcp_server.browser_read_page(tok)
        assert out["ok"] is False
        assert out["error"] == "browser_not_attached"

    def test_navigate_happy_path_with_simulated_extension(self, bc_project,
                                                          monkeypatch):
        """Simulate the extension by completing each command the instant
        it's enqueued, so the tool's blocking await resolves at once."""
        tok = _session(bc_project, "ctl_sim")
        real_enqueue = db.enqueue_browser_command

        def instant_enqueue(token, verb, params=None):
            cid = real_enqueue(token, verb, params)
            if cid:
                db.dequeue_browser_command(token)
                db.complete_browser_command(
                    cid, True, {"echo": verb, "params": params or {}})
            return cid

        monkeypatch.setattr(db, "enqueue_browser_command", instant_enqueue)
        out = mcp_server.browser_navigate(tok, "https://app.example.com/next")
        assert out["ok"] is True
        assert out["result"]["echo"] == "navigate"
        assert out["result"]["params"]["url"] == "https://app.example.com/next"

    def test_control_stop_seals(self, bc_project):
        tok = _session(bc_project, "ctl_stop_tool")
        assert mcp_server.browser_control_stop(tok)["ok"] is True
        st = mcp_server.browser_control_status(tok)
        assert st["active"] is False
