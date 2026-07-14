"""PR-F Phase 2 — browser-control DB queue helpers.

The queue is the cross-process channel between the MCP controller and
the extension executor, so its state machine has to be exact:
pending → dispatched → done/error, single-dispatch, sealed sessions
refuse new work, and expiry never hands out stale commands.
"""
from __future__ import annotations

import os

import pytest

from engine import db


@pytest.fixture
def project(app):
    db.init_db()
    pid = db.upsert_project(
        name=f"bc-{os.urandom(4).hex()}",
        base_url="https://app.test",
        owner_sid="test",
    )
    yield pid
    db.delete_project(pid)


class TestControlSession:
    def test_create_get_touch(self, project):
        assert db.create_browser_control_session(project, "ctl_1") is not None
        s = db.get_browser_control_session("ctl_1")
        assert s is not None
        assert s["project_id"] == project
        # Freshly created → last_seen just set → live.
        assert s["live"] is True
        assert db.touch_browser_control_session("ctl_1") is True

    def test_unknown_token_is_none(self, project):
        assert db.get_browser_control_session("nope") is None
        assert db.touch_browser_control_session("nope") is False

    def test_unknown_project_rejected(self):
        assert db.create_browser_control_session("f" * 32, "ctl_x") is None

    def test_stop_seals_session(self, project):
        db.create_browser_control_session(project, "ctl_stop")
        assert db.stop_browser_control_session("ctl_stop") is True
        # Sealed → not resolvable, and no new commands accepted.
        assert db.get_browser_control_session("ctl_stop") is None
        assert db.enqueue_browser_command("ctl_stop", "navigate",
                                           {"url": "https://x"}) is None


class TestCommandQueue:
    def _session(self, project, tok="ctl_q"):
        db.create_browser_control_session(project, tok)
        return tok

    def test_enqueue_dequeue_complete_round_trip(self, project):
        tok = self._session(project)
        cid = db.enqueue_browser_command(tok, "navigate",
                                         {"url": "https://app.test/x"})
        assert cid is not None
        # Pending until dequeued.
        assert db.get_browser_command(cid)["status"] == "pending"

        popped = db.dequeue_browser_command(tok)
        assert popped["command_id"] == cid
        assert popped["verb"] == "navigate"
        assert popped["params"]["url"] == "https://app.test/x"
        assert db.get_browser_command(cid)["status"] == "dispatched"

        assert db.complete_browser_command(
            cid, True, {"url": "https://app.test/x", "title": "X"}) is True
        done = db.get_browser_command(cid)
        assert done["status"] == "done"
        assert done["result"]["title"] == "X"

    def test_single_dispatch(self, project):
        tok = self._session(project, "ctl_single")
        db.enqueue_browser_command(tok, "wait", {"ms": 100})
        first = db.dequeue_browser_command(tok)
        assert first is not None
        # Second poll finds nothing — the command was already handed out.
        assert db.dequeue_browser_command(tok) is None

    def test_fifo_order(self, project):
        tok = self._session(project, "ctl_fifo")
        c1 = db.enqueue_browser_command(tok, "wait", {"ms": 1})
        c2 = db.enqueue_browser_command(tok, "wait", {"ms": 2})
        assert db.dequeue_browser_command(tok)["command_id"] == c1
        assert db.dequeue_browser_command(tok)["command_id"] == c2

    def test_unknown_verb_rejected(self, project):
        tok = self._session(project, "ctl_verb")
        assert db.enqueue_browser_command(tok, "eval",
                                          {"js": "alert(1)"}) is None

    def test_complete_is_idempotent(self, project):
        tok = self._session(project, "ctl_idem")
        cid = db.enqueue_browser_command(tok, "click", {"ref": "ref_1"})
        db.dequeue_browser_command(tok)
        assert db.complete_browser_command(cid, True, {"clicked": True}) is True
        # Second completion is a no-op (guards against a duplicate POST
        # from a retrying extension clobbering the result).
        assert db.complete_browser_command(cid, False, None, "boom") is False
        assert db.get_browser_command(cid)["status"] == "done"

    def test_expired_command_not_dispatched(self, project):
        tok = self._session(project, "ctl_exp")
        cid = db.enqueue_browser_command(tok, "wait", {"ms": 1})
        # Backdate expiry so the dequeue pass marks it errored instead of
        # handing out a stale command.
        from engine.db import BrowserCommand, _utcnow, session_scope
        from datetime import timedelta
        from sqlalchemy import select
        with session_scope() as sess:
            row = sess.execute(select(BrowserCommand).where(
                BrowserCommand.command_id == cid)).scalar_one()
            row.expires_at = _utcnow() - timedelta(minutes=1)
        assert db.dequeue_browser_command(tok) is None
        assert db.get_browser_command(cid)["status"] == "error"
