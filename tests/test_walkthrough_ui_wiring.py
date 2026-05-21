"""TFWefloLab integration PR-3 — UI wiring + results endpoint.

PR-3 adds the operator-facing surface on top of the PR-1 scaffold and
PR-2 heuristics:

* Run-mode radio (``tc_driven`` / ``walkthrough``) on the form
* Walkthrough sub-config (max_pages, axe_enabled, tc_binding…)
* TC projection — ``trigger`` filter picks which test_cases the
  walkthrough runner will fire when ``tc_binding=url_pattern``
* Results endpoint reads ``walkthrough_findings_deduped`` from
  ``result.json`` and turns each finding into a bug via
  :func:`engine.bug_report.create_bug_from_walkthrough_finding`
* Bugs land in ``session['bug_reports_data']`` and the per-run
  record gets ``walkthrough_findings`` + ``walkthrough_tc_bindings``
  attached for the findings sub-tab to render against

These tests pin those behaviours so a future refactor of the route
glue doesn't silently lose the walkthrough pipeline.

The detached subprocess is patched out across the suite — we assert
on the JSON config the worker *would* pick up, not on a real run.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys

import pytest


# ── Shared fixtures ──────────────────────────────────────────────


@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    """Point STORAGE_FOLDER at a per-test tmp dir.

    Mirrors the scaffold/heuristics tests so /test-execution writes its
    ``_pending/`` config JSONs somewhere we can introspect after the
    POST returns. Without this the route writes to the dev share and
    cross-test runs contaminate each other.
    """
    from routes import automation as _auto
    monkeypatch.setattr(_auto, "STORAGE_ROOT", str(tmp_path))
    return str(tmp_path)


@pytest.fixture
def patched_subprocess(monkeypatch):
    """Capture the argv the route would Popen, so we can read the
    serialised config_payload without actually spawning a worker."""
    captured: dict = {}

    class _FakePopen:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            captured["kw"] = kw
            self.pid = 9999

    import routes.execution as _exec
    # routes/execution.py imports subprocess inside the POST handler
    # (``import subprocess as _subprocess``), so we patch it on the
    # module subprocess module — the lazy import resolves to the same
    # binding.
    import subprocess as _real_subprocess
    monkeypatch.setattr(_real_subprocess, "Popen", _FakePopen)
    return captured


@pytest.fixture
def session_with_tc_data(client):
    """Pre-populate the Flask session with a small TC pack that
    exercises every ``trigger`` value the walkthrough TC projection
    cares about. Three TCs:

    * ``TC-001`` — ``trigger=manual``           → must be filtered out
    * ``TC-002`` — ``trigger=walkthrough_url_match`` → must pass through
    * ``TC-003`` — ``trigger=always``           → must pass through
    """
    with client.session_transaction() as sess:
        sess["test_cases_data"] = [
            {"id": "TC-001", "summary": "Manual TC",
             "trigger": "manual",
             "url_pattern": "*/checkout/*"},
            {"id": "TC-002", "summary": "URL-bound TC",
             "trigger": "walkthrough_url_match",
             "url_pattern": "*/checkout/*"},
            {"id": "TC-003", "summary": "Always-on TC",
             "trigger": "always", "url_pattern": ""},
        ]
        sess["project_setup"] = {"project_name": "PR-3 fixture"}
        # app.before_request wipes GENERATED_KEYS (test_cases_data etc.)
        # for sessions started before SERVER_START_TIME. Pin the
        # session as "fresh" so the route sees our fixture data
        # instead of an empty pack.
        sess["_session_active_since"] = 9_999_999_999
    return client


# ── 1. POST /test-execution — run_mode parsing ──────────────────


class TestRunModeParsing:
    def test_default_run_mode_is_tc_driven(
            self, session_with_tc_data, tmp_storage, patched_subprocess):
        """When no run_mode is posted (older clients, automation),
        the config_payload mode falls back to tc_driven and no
        walkthrough block is set up — the existing TC path stays
        byte-identical."""
        resp = session_with_tc_data.post(
            "/test-execution",
            data={
                "source": "test_cases",
                "base_url": "https://example.com/",
                "env_type": "web",
                "selected_items": ["TC-001"],
            },
            follow_redirects=False,
        )
        # The route writes to the session-cap dance and may redirect,
        # but the config JSON should exist either way.
        assert resp.status_code in (200, 302, 303)
        cfg_path = patched_subprocess["argv"][3]
        with open(cfg_path) as f:
            cfg = json.load(f)
        assert cfg["mode"] == "tc_driven"
        assert cfg["walkthrough"] == {}
        assert cfg["tc_binding"] == "url_pattern"

    def test_walkthrough_mode_sets_config_block(
            self, session_with_tc_data, tmp_storage, patched_subprocess):
        """run_mode=walkthrough fills the walkthrough block from the
        form's numeric/bool inputs and projects TC data into
        walkthrough.test_cases."""
        resp = session_with_tc_data.post(
            "/test-execution",
            data={
                "source": "test_cases",
                "base_url": "https://example.com/",
                "env_type": "web",
                "run_mode": "walkthrough",
                "walkthrough_max_pages": "9",
                "walkthrough_max_form_fills": "0",
                "walkthrough_device_timeout_ms": "120000",
                "walkthrough_axe_enabled": "1",
                "walkthrough_tc_binding": "url_pattern",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 302, 303), resp.get_data(as_text=True)
        cfg_path = patched_subprocess["argv"][3]
        with open(cfg_path) as f:
            cfg = json.load(f)
        assert cfg["mode"] == "walkthrough"
        wt = cfg["walkthrough"]
        assert wt["max_pages"] == 9
        assert wt["max_form_fills"] == 0
        assert wt["device_timeout_ms"] == 120000
        assert wt["axe_enabled"] is True
        assert wt["start_urls"] == ["https://example.com/"]

    def test_invalid_run_mode_falls_back_to_tc_driven(
            self, session_with_tc_data, tmp_storage, patched_subprocess):
        """An unknown run_mode value (typo, future enum, attacker) is
        sanitised — we never trust untrusted form input to flip the
        worker into an unexpected dispatch branch."""
        resp = session_with_tc_data.post(
            "/test-execution",
            data={
                "source": "test_cases",
                "base_url": "https://example.com/",
                "env_type": "web",
                "run_mode": "exploit",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 302, 303)
        cfg_path = patched_subprocess["argv"][3]
        with open(cfg_path) as f:
            cfg = json.load(f)
        assert cfg["mode"] == "tc_driven"

    def test_invalid_tc_binding_falls_back_to_url_pattern(
            self, session_with_tc_data, tmp_storage, patched_subprocess):
        resp = session_with_tc_data.post(
            "/test-execution",
            data={
                "source": "test_cases",
                "base_url": "https://example.com/",
                "env_type": "web",
                "run_mode": "walkthrough",
                "walkthrough_tc_binding": "ai_predict",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 302, 303)
        cfg_path = patched_subprocess["argv"][3]
        with open(cfg_path) as f:
            cfg = json.load(f)
        assert cfg["tc_binding"] == "url_pattern"

    def test_walkthrough_numeric_floor_clamping(
            self, session_with_tc_data, tmp_storage, patched_subprocess):
        """device_timeout_ms below the 60 s floor gets clamped — a
        20 s device deadline would race the page-load timeout and
        every URL would show as blocked."""
        resp = session_with_tc_data.post(
            "/test-execution",
            data={
                "source": "test_cases",
                "base_url": "https://example.com/",
                "env_type": "web",
                "run_mode": "walkthrough",
                "walkthrough_device_timeout_ms": "5000",
                "walkthrough_max_pages": "0",
            },
            follow_redirects=False,
        )
        cfg_path = patched_subprocess["argv"][3]
        with open(cfg_path) as f:
            cfg = json.load(f)
        assert cfg["walkthrough"]["device_timeout_ms"] >= 60000
        assert cfg["walkthrough"]["max_pages"] >= 1


# ── 2. TC projection — trigger filter ────────────────────────────


class TestTcProjection:
    def test_only_walkthrough_triggers_are_projected(
            self, session_with_tc_data, tmp_storage, patched_subprocess):
        """``trigger=manual`` TCs must never reach the walkthrough
        runner. Only ``walkthrough_url_match`` and ``always`` are
        legitimate fire candidates per
        :mod:`engine.walkthrough_tc_match` rules."""
        session_with_tc_data.post(
            "/test-execution",
            data={
                "source": "test_cases",
                "base_url": "https://example.com/",
                "env_type": "web",
                "run_mode": "walkthrough",
                "walkthrough_tc_binding": "url_pattern",
            },
            follow_redirects=False,
        )
        cfg_path = patched_subprocess["argv"][3]
        with open(cfg_path) as f:
            cfg = json.load(f)
        projected_ids = [tc["id"] for tc in cfg["walkthrough"]["test_cases"]]
        assert "TC-001" not in projected_ids       # trigger=manual filtered
        assert set(projected_ids) == {"TC-002", "TC-003"}

    def test_ignore_binding_skips_projection(
            self, session_with_tc_data, tmp_storage, patched_subprocess):
        """When the operator picks ``tc_binding=ignore`` the runner
        must run heuristics-only, even if TCs in the pack are
        otherwise eligible. PR-3 sometimes prefers to keep the run
        deterministic and not surface TC noise."""
        session_with_tc_data.post(
            "/test-execution",
            data={
                "source": "test_cases",
                "base_url": "https://example.com/",
                "env_type": "web",
                "run_mode": "walkthrough",
                "walkthrough_tc_binding": "ignore",
            },
            follow_redirects=False,
        )
        cfg_path = patched_subprocess["argv"][3]
        with open(cfg_path) as f:
            cfg = json.load(f)
        assert cfg["walkthrough"]["test_cases"] == []


# ── 3. Results endpoint — walkthrough findings → bugs ────────────


class TestResultsWalkthroughPath:
    def _write_payload(self, tmp_storage, run_id, payload):
        pending = os.path.join(tmp_storage, "automation_runs", "_pending")
        os.makedirs(pending, exist_ok=True)
        result_path = os.path.join(pending, f"{run_id}.result.json")
        done_flag = os.path.join(pending, f"{run_id}.done.flag")
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        # The route checks for both result.json and done.flag — write
        # an empty flag to mark completion.
        open(done_flag, "w").close()

    def test_findings_become_bugs_and_attach_to_run_record(
            self, client, tmp_storage):
        """End-to-end through the results endpoint: a result.json with
        two findings + two TC bindings should yield two new bugs in
        the session bug list, and the run_record's
        ``walkthrough_findings`` list should mirror the deduped view."""
        run_id = "20260521_120000_abc123"
        payload = {
            "status": "done",
            "config_id": run_id,
            "mode": "walkthrough",
            "report": {"passed": 0, "failed": 0, "blocked": 0,
                       "run_id": run_id},
            "automation_assets": {},
            "walkthrough_findings": [],
            "walkthrough_findings_deduped": [
                {"severity": "Critical", "defect_class": "broken_image",
                 "area": "Images",
                 "message": "Broken image on hero",
                 "url": "https://example.com/",
                 "element": "img.hero",
                 "screenshot": "",
                 "tc_id": "WALK-IMG-001"},
                {"severity": "Major", "defect_class": "axe_serious",
                 "area": "Accessibility",
                 "message": "Form input missing label",
                 "url": "https://example.com/contact",
                 "element": "input#email",
                 "screenshot": "",
                 "tc_id": "WALK-AXE-001"},
            ],
            "walkthrough_tc_bindings": [
                {"url": "https://example.com/checkout",
                 "tc_id": "TC-002"},
            ],
            "config_echo": {
                "base_url": "https://example.com/",
                "site_url": "https://example.com/",
                "env_types": ["web"],
                "items_data": [],
                "selected_ids": [],
                "manual_statuses": {},
                "manual_bug_refs": {},
                "tester_id": "mid_1",
                "testing_types": ["Regression"],
                "source": "test_cases",
                "item_type": "test_case",
                "headless": True,
                "record_video": False,
                "envs": {"web": {"environment": "Web · Windows 11 / Chrome"}},
            },
        }
        self._write_payload(tmp_storage, run_id, payload)

        resp = client.get(f"/test-execution/results/{run_id}",
                          follow_redirects=False)
        # The endpoint redirects to /test-execution after rendering
        # successfully — the redirect is the success signal, the
        # interesting assertions live in the session.
        assert resp.status_code in (200, 302, 303), resp.get_data(as_text=True)

        with client.session_transaction() as sess:
            bugs = sess.get("bug_reports_data", []) or []
            runs = sess.get("test_runs", []) or []

        wt_bugs = [b for b in bugs
                   if b.get("linked_item_type") == "walkthrough"]
        assert len(wt_bugs) == 2, [b.get("title") for b in wt_bugs]
        wt_ids = {b["linked_item_id"] for b in wt_bugs}
        assert wt_ids == {"WALK-IMG-001", "WALK-AXE-001"}
        # source:walkthrough label is what the bug-reports listing
        # filters on; missing it means the bug looks like a TC-driven
        # one and the operator can't tell them apart.
        for b in wt_bugs:
            assert "source:walkthrough" in (b.get("labels") or [])

        assert runs, "results endpoint should produce a run record"
        last = runs[-1]
        assert last["mode"] == "walkthrough"
        assert len(last["walkthrough_findings"]) == 2
        assert last["walkthrough_tc_bindings"][0]["tc_id"] == "TC-002"
        # Bug count includes the synthesised walkthrough bugs even
        # though execution["bugs"] (TC-driven) is empty.
        assert last["bug_count"] == 2

    def test_tc_driven_results_unchanged(self, client, tmp_storage):
        """The walkthrough findings branch must NOT activate on a
        ``mode=tc_driven`` payload — the existing TC results path is
        byte-identical to pre-PR-3."""
        run_id = "20260521_120000_tcd000"
        payload = {
            "status": "done",
            "config_id": run_id,
            "mode": "tc_driven",
            "report": {"passed": 0, "failed": 0, "blocked": 0,
                       "run_id": run_id},
            "automation_assets": {},
            # Even if findings somehow slip in, mode=tc_driven blocks
            # the conversion path.
            "walkthrough_findings_deduped": [
                {"severity": "Critical", "defect_class": "broken_image",
                 "area": "Images", "message": "should be ignored",
                 "tc_id": "WALK-IMG-999"},
            ],
            "config_echo": {
                "base_url": "",
                "site_url": "",
                "env_types": ["web"],
                "items_data": [],
                "selected_ids": [],
                "tester_id": "mid_1",
                "testing_types": ["Regression"],
                "source": "test_cases",
                "item_type": "test_case",
                "headless": True,
                "record_video": False,
                "envs": {"web": {"environment": "Web"}},
            },
        }
        TestResultsWalkthroughPath()._write_payload(
            tmp_storage, run_id, payload)
        resp = client.get(f"/test-execution/results/{run_id}",
                          follow_redirects=False)
        assert resp.status_code in (200, 302, 303)
        with client.session_transaction() as sess:
            bugs = sess.get("bug_reports_data", []) or []
        assert [b for b in bugs
                if b.get("linked_item_type") == "walkthrough"] == []


# ── 4. Template smoke — radio + subtab render ────────────────────


class TestTemplateSmoke:
    def test_get_test_execution_renders_run_mode_radio(self, client):
        """The Run-mode radio must be in the DOM whenever the form is
        rendered (i.e. when at least one pack is present). Pre-empty
        the session with a minimal TC pack so we reach the form
        branch instead of the empty-state landing card."""
        with client.session_transaction() as s:
            s["test_cases_data"] = [{"id": "TC-1", "summary": "x",
                                      "section": "S", "section_num": 1}]
            s["_session_active_since"] = 9_999_999_999
        resp = client.get("/test-execution")
        assert resp.status_code == 200, resp.status_code
        body = resp.get_data(as_text=True)
        assert 'name="run_mode"' in body
        assert 'value="walkthrough"' in body
        assert 'value="tc_driven"' in body
        # Walkthrough options panel — collapsed by default but
        # must be present so JS can auto-open it.
        assert 'data-te-walkthrough-panel' in body
        assert 'name="walkthrough_max_pages"' in body
        assert 'name="walkthrough_tc_binding"' in body

    def test_findings_subtab_renders_when_run_has_findings(self, client):
        """When session test_runs holds a walkthrough run with
        findings, the run-card must surface the Results | Findings
        sub-tab strip + at least one finding row."""
        with client.session_transaction() as sess:
            sess["test_cases_data"] = [{
                "id": "TC-1", "summary": "x",
                "section": "S", "section_num": 1,
            }]
            sess["_session_active_since"] = 9_999_999_999
            sess["test_runs"] = [{
                "run_id": 99,
                "source": "test_cases",
                "mode": "walkthrough",
                "tester_name": "QA",
                "environment": "Web · Linux / Chrome",
                "testing_types": "Regression",
                "results": [],
                "stats": {"passed": 0, "failed": 0,
                          "blocked": 0, "pass_rate": 0},
                "bug_count": 1,
                "site_url": "https://example.com/",
                "base_url": "https://example.com/",
                "headless": True,
                "record_video": False,
                "automation_used": True,
                "created_at": "2026-05-21T12:00:00",
                "walkthrough_findings": [
                    {"severity": "Critical",
                     "defect_class": "broken_image",
                     "area": "Images",
                     "message": "Hero image broken",
                     "url": "https://example.com/",
                     "element": "img.hero",
                     "screenshot": "",
                     "tc_id": "WALK-IMG-001"},
                ],
                "walkthrough_tc_bindings": [
                    {"tc_id": "TC-002",
                     "url": "https://example.com/checkout"},
                ],
            }]
        resp = client.get("/test-execution")
        assert resp.status_code == 200, resp.status_code
        body = resp.get_data(as_text=True)
        # Sub-tab strip + at least one finding row.
        assert 'data-te-subtabs' in body
        assert 'data-subtab-target="findings-99"' in body
        assert 'Hero image broken' in body
        # TC-binding panel is below the sub-tabs.
        assert 'TC-002' in body
