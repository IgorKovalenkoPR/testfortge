"""Sprint-1 Task 2 — owner_sid authorization on project routes.

Verifies that cross-tenant access to project-scoped routes returns 403
instead of leaking / mutating another user's data. Legacy projects with
NULL owner_sid stay accessible (with a log.info trace) — backfill of
those rows is deferred to Sprint 2 by design.

Pattern lifted from tests/test_rate_limit.py: monkeypatch
``routes.projects.get_session_id`` so the route's view of the caller's
session id is deterministic, regardless of what flask-session minted
for the test client.
"""

from __future__ import annotations

import uuid

from engine import db as _db


def _new_sid(label: str = "sid") -> str:
    return f"{label}-{uuid.uuid4().hex}"


def _seed_project(owner_sid: str | None, name_prefix: str = "auth-test") -> str:
    """Create a project owned by *owner_sid* (or NULL) and return its id."""
    name = f"{name_prefix}-{uuid.uuid4().hex[:8]}"
    return _db.upsert_project(name=name, owner_sid=owner_sid)


def _pin_sid(monkeypatch, sid: str) -> None:
    """Make get_session_id() deterministic inside the projects route module."""
    monkeypatch.setattr("routes.projects.get_session_id",
                        lambda s=None: sid)


class TestProjectOwnerAuth:
    def test_load_project_other_owner_returns_403(self, client, monkeypatch):
        """A logged-in attacker cannot read another session's project."""
        victim_sid = _new_sid("victim")
        attacker_sid = _new_sid("attacker")
        pid = _seed_project(owner_sid=victim_sid)
        try:
            _pin_sid(monkeypatch, attacker_sid)
            resp = client.get(f"/load-project/{pid}", follow_redirects=False)
            assert resp.status_code == 403, (
                f"expected 403, got {resp.status_code}: "
                f"{resp.get_data(as_text=True)[:200]}"
            )
        finally:
            _db.delete_project(pid)

    def test_delete_project_other_owner_returns_403(self, client, monkeypatch):
        """A cross-tenant POST /delete-project must 403 and leave the row
        in place."""
        victim_sid = _new_sid("victim")
        attacker_sid = _new_sid("attacker")
        pid = _seed_project(owner_sid=victim_sid)
        try:
            _pin_sid(monkeypatch, attacker_sid)
            resp = client.post(f"/delete-project/{pid}",
                               follow_redirects=False)
            assert resp.status_code == 403, (
                f"expected 403, got {resp.status_code}"
            )
            # Row is still there — auth gate prevented the destructive call.
            assert _db.get_project(pid) is not None, (
                "victim's project was deleted despite 403 — auth gate failed"
            )
        finally:
            _db.delete_project(pid)

    def test_legacy_null_owner_allows_access(self, client, monkeypatch, caplog):
        """Projects predating owner_sid (NULL) stay reachable — backfill
        is Sprint 2. The route emits a log.info so the gap is visible in
        ops dashboards."""
        # upsert_project requires owner_sid to dedupe the slug, so we
        # NULL it out post-insert to mimic a legacy row.
        pid = _seed_project(owner_sid=_new_sid("temp"),
                            name_prefix="legacy-null")
        try:
            from sqlalchemy import update
            from engine.db import Project, session_scope
            with session_scope() as sess:
                sess.execute(
                    update(Project).where(Project.id == pid)
                    .values(owner_sid=None)
                )

            attacker_sid = _new_sid("any")
            _pin_sid(monkeypatch, attacker_sid)
            import logging
            with caplog.at_level(logging.INFO, logger="routes.projects"):
                resp = client.get(f"/load-project/{pid}",
                                  follow_redirects=False)
            # 302 redirect to /index with a flash — NOT 403.
            assert resp.status_code in (200, 302), (
                f"NULL-owner project should be accessible, got "
                f"{resp.status_code}"
            )
            # The legacy-compat log message must show up so ops can spot
            # rows that still need backfilling.
            assert any("NULL owner_sid" in rec.getMessage()
                       for rec in caplog.records), (
                "missing legacy-compat log line for NULL owner_sid project"
            )
        finally:
            _db.delete_project(pid)

    def test_move_artifacts_blocks_cross_owner_target(self, client,
                                                     monkeypatch):
        """The mover owns the source but tries to dump its artefacts into
        a victim's target project. Source check passes, target check
        must 403 — otherwise an attacker can shovel garbage into a
        victim's workspace."""
        attacker_sid = _new_sid("attacker")
        victim_sid = _new_sid("victim")
        source_pid = _seed_project(owner_sid=attacker_sid,
                                   name_prefix="src")
        target_pid = _seed_project(owner_sid=victim_sid,
                                   name_prefix="tgt-victim")
        try:
            _pin_sid(monkeypatch, attacker_sid)
            resp = client.post(
                "/projects/db/move-artifacts",
                data={"source_project_id": source_pid,
                      "target_project_id": target_pid},
                follow_redirects=False,
            )
            assert resp.status_code == 403, (
                f"expected 403 on cross-owner target, got "
                f"{resp.status_code}"
            )
        finally:
            _db.delete_project(source_pid)
            _db.delete_project(target_pid)
