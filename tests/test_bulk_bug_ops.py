"""Sprint 4 task 4.2 — bulk bug operations.

Coverage:

1. Engine helper :func:`engine.db.bulk_update_bugs` honours each
   action (close, status, severity, priority, fix_version, assign,
   delete) and writes the audit-trail line every non-delete time.
2. Cross-project safety: a bulk update against project A never
   touches a row from project B even when its id is in the list.
3. Route ``POST /bugs/bulk``:
   * 302 + flash on success
   * 403 when another session owns the project
   * Invalid action / empty ids redirect with an error flash
4. ``bug.db_id`` is rendered into the bug-card checkbox by
   :func:`bug_reports_page` so the toolbar can select rows.
"""

from __future__ import annotations

import uuid

import pytest
from werkzeug.datastructures import MultiDict

from engine import db as _db


# ── Seed helpers ──────────────────────────────────────────────────


def _new_sid(label: str = "sid") -> str:
    return f"{label}-{uuid.uuid4().hex}"


def _seed_project_with_bugs(owner_sid: str, n: int = 3,
                            external_prefix: str = "BUG") -> tuple[str, list[int]]:
    """Create a project + ``n`` bugs and return (project_id, [bug_db_ids])."""
    pid = _db.upsert_project(name=f"bulk-test-{uuid.uuid4().hex[:8]}",
                             owner_sid=owner_sid)
    ids: list[int] = []
    for i in range(1, n + 1):
        bid = _db.save_bug(pid, {
            "id": f"{external_prefix}-{i:03d}",
            "title": f"Bug {i}",
            "severity": "Major",
            "priority": "High",
            "status": "Open",
            "environment": "Linux / Chrome",
            "steps_to_reproduce": "1. open\n2. click",
            "actual_result": "broken",
            "expected_result": "works",
        })
        ids.append(bid)
    return pid, ids


def _pin_sid(monkeypatch, sid: str) -> None:
    """Pin both projects.get_session_id and execution.get_session_id —
    the bulk route consults both via the owner check chain."""
    monkeypatch.setattr("routes.projects.get_session_id",
                        lambda s=None: sid)
    monkeypatch.setattr("routes.execution.get_session_id",
                        lambda s=None: sid)
    monkeypatch.setattr("routes._shared.get_session_id",
                        lambda s=None: sid)


# ── Engine helper unit tests ──────────────────────────────────────


class TestBulkUpdateEngine:
    def test_close_action_updates_status_and_writes_audit(self):
        sid = _new_sid("alice")
        pid, ids = _seed_project_with_bugs(sid, n=2)

        n = _db.bulk_update_bugs(pid, ids, action="close",
                                 value=None, actor="alice")
        assert n == 2

        rows = {r["id"]: r for r in _db.list_bugs(pid)}
        for bid in ids:
            assert rows[bid]["status"] == "Closed"
            assert "alice: status -> Closed" in (rows[bid]["comment"] or "")

    def test_assign_action_writes_to_extra_json(self):
        sid = _new_sid("alice")
        pid, ids = _seed_project_with_bugs(sid, n=1)

        n = _db.bulk_update_bugs(pid, ids, action="assign",
                                 value="bob@example.com", actor="alice")
        assert n == 1
        row = _db.list_bugs(pid)[0]
        assert (row.get("extra") or {}).get("assignee") == "bob@example.com"
        assert "assignee -> bob@example.com" in (row["comment"] or "")

    def test_severity_action_updates_column(self):
        sid = _new_sid("alice")
        pid, ids = _seed_project_with_bugs(sid, n=1)
        n = _db.bulk_update_bugs(pid, ids, action="severity",
                                 value="Critical", actor="alice")
        assert n == 1
        assert _db.list_bugs(pid)[0]["severity"] == "Critical"

    def test_fix_version_writes_to_version_column(self):
        sid = _new_sid("alice")
        pid, ids = _seed_project_with_bugs(sid, n=1)
        n = _db.bulk_update_bugs(pid, ids, action="fix_version",
                                 value="2.4.0", actor="alice")
        assert n == 1
        # The plan exposes the field as ``fix_version`` to operators
        # but persists it in the existing ``version`` column.
        assert _db.list_bugs(pid)[0]["version"] == "2.4.0"

    def test_delete_action_drops_rows(self):
        sid = _new_sid("alice")
        pid, ids = _seed_project_with_bugs(sid, n=3)
        n = _db.bulk_update_bugs(pid, ids[:2], action="delete",
                                 value=None, actor="alice")
        assert n == 2
        remaining = {r["id"] for r in _db.list_bugs(pid)}
        assert remaining == {ids[2]}

    def test_cross_project_ids_are_ignored(self):
        """Sending an id from project B in a bulk update for project A
        must NOT touch the B row."""
        sid = _new_sid("alice")
        pid_a, ids_a = _seed_project_with_bugs(sid, n=2, external_prefix="A")
        pid_b, ids_b = _seed_project_with_bugs(sid, n=2, external_prefix="B")

        mixed = ids_a + ids_b
        n = _db.bulk_update_bugs(pid_a, mixed, action="close",
                                 value=None, actor="alice")
        assert n == 2  # only the two A-rows changed

        # Project B rows untouched.
        for r in _db.list_bugs(pid_b):
            assert r["status"] == "Open"

    def test_unknown_action_returns_zero(self):
        sid = _new_sid("alice")
        pid, ids = _seed_project_with_bugs(sid, n=1)
        n = _db.bulk_update_bugs(pid, ids, action="exterminate",
                                 value=None, actor="alice")
        assert n == 0

    def test_empty_id_list_returns_zero(self):
        sid = _new_sid("alice")
        pid, _ = _seed_project_with_bugs(sid, n=1)
        n = _db.bulk_update_bugs(pid, [], action="close",
                                 value=None, actor="alice")
        assert n == 0

    def test_audit_lines_accumulate_on_subsequent_runs(self):
        sid = _new_sid("alice")
        pid, ids = _seed_project_with_bugs(sid, n=1)
        _db.bulk_update_bugs(pid, ids, action="severity",
                             value="Critical", actor="alice")
        _db.bulk_update_bugs(pid, ids, action="priority",
                             value="Highest", actor="bob")

        comment = _db.list_bugs(pid)[0]["comment"] or ""
        # Both lines survive; nothing overwrites the prior trail.
        assert "alice: severity -> Critical" in comment
        assert "bob: priority -> Highest" in comment


# ── Route tests ───────────────────────────────────────────────────


class TestBugsBulkRoute:
    def test_close_action_round_trip(self, client, monkeypatch):
        sid = _new_sid("alice")
        pid, ids = _seed_project_with_bugs(sid, n=2)
        _pin_sid(monkeypatch, sid)
        with client.session_transaction() as sess:
            sess["project_id"] = pid

        payload = MultiDict([
            ("bug_ids", str(ids[0])),
            ("bug_ids", str(ids[1])),
            ("action", "close"),
        ])
        resp = client.post("/bugs/bulk", data=payload,
                           follow_redirects=False)
        assert resp.status_code == 302
        assert "/bug-reports" in resp.headers["Location"]

        for r in _db.list_bugs(pid):
            assert r["status"] == "Closed"

    def test_status_action_with_per_action_value(self, client, monkeypatch):
        sid = _new_sid("alice")
        pid, ids = _seed_project_with_bugs(sid, n=1)
        _pin_sid(monkeypatch, sid)
        with client.session_transaction() as sess:
            sess["project_id"] = pid

        # The toolbar's per-action ``<action>_value`` field is what the
        # route prefers. We send both to confirm the namespaced one wins.
        resp = client.post("/bugs/bulk", data={
            "bug_ids": str(ids[0]),
            "action": "status",
            "status_value": "In Progress",
            "value": "garbage-should-be-ignored",
        }, follow_redirects=False)
        assert resp.status_code == 302
        assert _db.list_bugs(pid)[0]["status"] == "In Progress"

    def test_cross_tenant_returns_403(self, client, monkeypatch):
        """A different session impersonating bulk on someone else's
        project must be rejected by the owner gate."""
        owner_sid = _new_sid("owner")
        attacker_sid = _new_sid("attacker")
        pid, ids = _seed_project_with_bugs(owner_sid, n=1)

        _pin_sid(monkeypatch, attacker_sid)
        with client.session_transaction() as sess:
            sess["project_id"] = pid

        resp = client.post("/bugs/bulk", data={
            "bug_ids": str(ids[0]),
            "action": "close",
        }, follow_redirects=False)
        assert resp.status_code == 403
        # Victim rows untouched.
        assert _db.list_bugs(pid)[0]["status"] == "Open"

    def test_invalid_action_redirects_with_error(self, client, monkeypatch):
        sid = _new_sid("alice")
        pid, ids = _seed_project_with_bugs(sid, n=1)
        _pin_sid(monkeypatch, sid)
        with client.session_transaction() as sess:
            sess["project_id"] = pid

        resp = client.post("/bugs/bulk", data={
            "bug_ids": str(ids[0]),
            "action": "exterminate",
        }, follow_redirects=False)
        assert resp.status_code == 302
        assert _db.list_bugs(pid)[0]["status"] == "Open"

    def test_no_ids_redirects_with_error(self, client, monkeypatch):
        sid = _new_sid("alice")
        pid, _ = _seed_project_with_bugs(sid, n=1)
        _pin_sid(monkeypatch, sid)
        with client.session_transaction() as sess:
            sess["project_id"] = pid

        resp = client.post("/bugs/bulk", data={
            "action": "close",
        }, follow_redirects=False)
        assert resp.status_code == 302


# ── Template wiring ───────────────────────────────────────────────


class TestBugReportsRendersDbId:
    def test_bug_card_carries_db_id_checkbox(self, client, monkeypatch):
        sid = _new_sid("alice")
        pid, ids = _seed_project_with_bugs(sid, n=1)
        _pin_sid(monkeypatch, sid)
        with client.session_transaction() as sess:
            sess["project_id"] = pid

        resp = client.get("/bug-reports")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert f'value="{ids[0]}"' in body, (
            "bug card should expose a checkbox carrying the DB row id"
        )
        # Sticky toolbar markup must be present (initially hidden).
        assert 'id="bulk-toolbar"' in body
        assert 'name="bug_ids"' in body
