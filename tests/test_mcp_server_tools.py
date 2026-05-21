"""MCP server tool tests — both the v1 read surface and the v1.5
``create_bug_report`` / ``trigger_test_execution`` write tools.

We import the tool functions directly from :mod:`mcp_server.server`.
The functions are wrapped by FastMCP's ``@mcp.tool()`` decorator but
remain callable as ordinary Python — the decorator registers them in
the MCP routing table without rebinding the symbol. That keeps the
tests fast (no JSON-RPC plumbing) and lets us assert on the plain
return dicts.

DB writes go to whatever SQLite file ``engine.db`` resolves to under
``FLASK_DEBUG=1`` (set by :file:`tests/conftest.py`). Each test seeds
its own project so we don't depend on ordering with the wider suite.
"""

from __future__ import annotations

import json
import os
from unittest import mock

import pytest

from engine import db
from mcp_server import server as mcp_server


# ── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
def project_id():
    """A fresh project per test — distinct slug so upsert never collides."""
    name = f"mcp-test-{os.urandom(4).hex()}"
    return db.upsert_project(name=name, base_url="https://example.test/")


# ── create_bug_report ───────────────────────────────────────────


class TestCreateBugReport:
    def test_minimal_bug_persists_with_defaults(self, project_id):
        out = mcp_server.create_bug_report(
            title="Login button does nothing on submit",
            project_id=project_id,
        )
        assert isinstance(out["db_id"], int) and out["db_id"] > 0
        assert out["title"] == "Login button does nothing on submit"
        # Defaults the tool fills in:
        assert out["severity"] == "Major"
        assert out["priority"] == "High"
        assert out["status"] == "Open"
        assert out["source"] == "manual"

        # Round-trip — the bug appears in list_bugs for the project.
        rows = db.list_bugs(project_id=project_id)
        titles = [r.get("title") for r in rows]
        assert "Login button does nothing on submit" in titles

    def test_empty_title_raises(self):
        with pytest.raises(ValueError, match="title.*required"):
            mcp_server.create_bug_report(title="")
        with pytest.raises(ValueError, match="title.*required"):
            mcp_server.create_bug_report(title="   ")

    def test_invalid_source_falls_back_to_manual(self, project_id):
        # ``foobar`` is not in VALID_BUG_SOURCES; the tool must silently
        # downgrade rather than raise — same behaviour as db.save_bug.
        out = mcp_server.create_bug_report(
            title="Source-coercion test",
            project_id=project_id,
            source="foobar",
        )
        assert out["source"] == "manual"
        rows = db.list_bugs(project_id=project_id, source="manual")
        assert any(r.get("title") == "Source-coercion test" for r in rows)

    def test_project_less_bug_is_allowed(self):
        # Mirrors the Tedgie path: BugReport.project_id is nullable.
        out = mcp_server.create_bug_report(
            title="Tedgie-style projectless bug",
            project_id=None,
            source="tedgie",
        )
        assert out["project_id"] is None
        assert out["source"] == "tedgie"

    def test_extra_dict_lands_in_json_column(self, project_id):
        out = mcp_server.create_bug_report(
            title="Bug with assignee + labels",
            project_id=project_id,
            extra={"assignee": "qa-bot", "labels": ["regression", "p1"]},
        )
        rows = db.list_bugs(project_id=project_id)
        row = next(r for r in rows if r["id"] == out["db_id"])
        assert row.get("extra") == {
            "assignee": "qa-bot",
            "labels": ["regression", "p1"],
        }

    def test_related_case_id_persists_when_pointing_at_real_tc(
        self, project_id
    ):
        # SQLite's FK pragma is on for this app, so we can't pass a
        # throwaway id — seed a real TC and link to its integer pk.
        db.save_test_cases(project_id, [{
            "id": "TC-LINK-001",
            "section": "Smoke",
            "summary": "Linkable",
            "test_steps": "1. Visit /",
            "expected_result": "ok",
            "priority": "High",
            "status": "Open",
            "testing_type": "Functional",
        }])
        with db.session_scope() as sess:
            from sqlalchemy import select
            tc_pk = sess.execute(
                select(db.TestCase.id).where(
                    db.TestCase.project_id == project_id,
                    db.TestCase.external_id == "TC-LINK-001",
                )
            ).scalar_one()

        out = mcp_server.create_bug_report(
            title="Bug linked to a real TC",
            project_id=project_id,
            related_case_id=str(tc_pk),
        )
        rows = db.list_bugs(project_id=project_id)
        row = next(r for r in rows if r["id"] == out["db_id"])
        assert row.get("related_case_id") == tc_pk


# ── trigger_test_execution ──────────────────────────────────────


class TestTriggerTestExecution:
    """Subprocess.Popen is patched so the test never spawns a real
    runner_worker — we only assert on the config payload written to
    disk and the args the tool would have spawned with."""

    def _seed_tcs(self, pid: str, count: int = 2) -> list[dict]:
        tcs = [
            {
                "id": f"TC-{i:03d}",
                "section": "Smoke",
                "summary": f"Smoke #{i}",
                "preconditions": "",
                "test_steps": "1. Visit /",
                "test_data": "",
                "expected_result": "Page loads.",
                "priority": "High",
                "status": "Open",
                "testing_type": "Functional",
                "url_pattern": "",
                "trigger": "manual",
            }
            for i in range(1, count + 1)
        ]
        db.save_test_cases(pid, tcs)
        return tcs

    def test_missing_project_id_raises(self):
        with pytest.raises(ValueError, match="project_id is required"):
            mcp_server.trigger_test_execution(project_id="")

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError, match="unknown mode"):
            mcp_server.trigger_test_execution(
                project_id="abc", mode="lol-random"
            )

    def test_tc_driven_requires_base_url(self, project_id):
        self._seed_tcs(project_id)
        with pytest.raises(ValueError, match="base_url is required"):
            mcp_server.trigger_test_execution(
                project_id=project_id, base_url=""
            )

    def test_tc_driven_with_no_matching_tcs_raises(self, project_id):
        # Project exists but has zero TCs.
        with pytest.raises(ValueError, match="no test cases matched"):
            mcp_server.trigger_test_execution(
                project_id=project_id,
                base_url="https://example.test/",
            )

    def test_tc_driven_dispatch_writes_config_and_spawns_worker(
        self, project_id, tmp_path, monkeypatch
    ):
        self._seed_tcs(project_id, count=3)

        # Sandbox STORAGE_ROOT so the test never pollutes the real
        # storage dir. Both the tool module and the job_queue helper
        # must see the override — patch the symbol the tool already
        # imported.
        sandbox = str(tmp_path)
        monkeypatch.setattr(mcp_server, "STORAGE_ROOT", sandbox)

        fake_proc = mock.Mock()
        fake_proc.pid = 12345
        popen_mock = mock.Mock(return_value=fake_proc)
        monkeypatch.setattr(mcp_server.subprocess, "Popen", popen_mock)

        out = mcp_server.trigger_test_execution(
            project_id=project_id,
            base_url="https://example.test/",
            env_types=["web"],
        )
        assert out["pid"] == 12345
        assert out["mode"] == "tc_driven"
        assert out["items"] == 3
        assert out["env_types"] == ["web"]
        assert out["config_id"]  # non-empty
        assert os.path.isfile(out["config_path"])

        # Inspect the persisted config — runner_worker reads exactly
        # this file on boot, so the keys must match what it expects.
        with open(out["config_path"], encoding="utf-8") as f:
            cfg = json.load(f)
        assert cfg["project_id"] == project_id
        assert cfg["mode"] == "tc_driven"
        assert cfg["base_url"] == "https://example.test/"
        assert cfg["session_id"] == mcp_server.MCP_SESSION_ID
        assert cfg["headless"] is True
        assert len(cfg["items_data"]) == 3
        assert cfg["selected_ids"] == ["TC-001", "TC-002", "TC-003"]
        assert cfg["envs"] == {"web": {"environment": "web"}}
        assert cfg["walkthrough"] == {}

        # And Popen was called with the right module + the config path.
        args, kwargs = popen_mock.call_args
        cmd = args[0]
        assert cmd[-1] == out["config_path"]
        assert cmd[-3:-1] == ["-m", "engine.runner_worker"]
        assert kwargs["start_new_session"] is True

    def test_test_case_ids_filter_subsets_items_data(
        self, project_id, tmp_path, monkeypatch
    ):
        self._seed_tcs(project_id, count=4)
        monkeypatch.setattr(mcp_server, "STORAGE_ROOT", str(tmp_path))
        monkeypatch.setattr(
            mcp_server.subprocess, "Popen", mock.Mock(return_value=mock.Mock(pid=1))
        )

        out = mcp_server.trigger_test_execution(
            project_id=project_id,
            base_url="https://example.test/",
            test_case_ids=["TC-002", "TC-004"],
        )
        with open(out["config_path"], encoding="utf-8") as f:
            cfg = json.load(f)
        ids = sorted(it["id"] for it in cfg["items_data"])
        assert ids == ["TC-002", "TC-004"]
        assert out["items"] == 2

    def test_walkthrough_mode_packs_walkthrough_block(
        self, project_id, tmp_path, monkeypatch
    ):
        # Walkthrough mode does NOT pull from db.load_test_cases — the
        # runner reads start_urls + max_pages from the config block,
        # not items_data — so an empty TC project is fine.
        monkeypatch.setattr(mcp_server, "STORAGE_ROOT", str(tmp_path))
        monkeypatch.setattr(
            mcp_server.subprocess, "Popen", mock.Mock(return_value=mock.Mock(pid=42))
        )

        out = mcp_server.trigger_test_execution(
            project_id=project_id,
            base_url="https://walkthrough.test/",
            mode="walkthrough",
            walkthrough_config={"max_pages": 4},
        )
        assert out["mode"] == "walkthrough"
        with open(out["config_path"], encoding="utf-8") as f:
            cfg = json.load(f)
        assert cfg["mode"] == "walkthrough"
        wt = cfg["walkthrough"]
        assert wt["start_urls"] == ["https://walkthrough.test/"]
        assert wt["max_pages"] == 4
        # Even with no inline TCs the runner expects the key present.
        assert wt["test_cases"] == []

    def test_walkthrough_mode_without_any_url_raises(self, project_id):
        with pytest.raises(ValueError, match="start_urls"):
            mcp_server.trigger_test_execution(
                project_id=project_id,
                base_url="",
                mode="walkthrough",
            )

    def test_concurrency_cap_raises_when_saturated(
        self, project_id, tmp_path, monkeypatch
    ):
        self._seed_tcs(project_id)
        monkeypatch.setattr(mcp_server, "STORAGE_ROOT", str(tmp_path))
        # Saturate the cap by faking the count helper.
        monkeypatch.setattr(
            mcp_server,
            "count_active_subprocess_runs",
            lambda *_a, **_kw: mcp_server.MCP_MAX_CONCURRENT_RUNS,
        )
        popen_mock = mock.Mock()
        monkeypatch.setattr(mcp_server.subprocess, "Popen", popen_mock)

        with pytest.raises(RuntimeError, match="cap is"):
            mcp_server.trigger_test_execution(
                project_id=project_id,
                base_url="https://example.test/",
            )
        # And no subprocess was spawned because the cap fired first.
        assert popen_mock.call_count == 0
