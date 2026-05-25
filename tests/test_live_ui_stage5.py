"""Stage 5 — UI tests for ``templates/test_execution_live.html``.

Stage 3 added ``rss_mb`` / ``memory_budget_mb`` / ``mode`` /
``early_exit_reason`` to the live ``info.json`` payload, but the live
template ignored them. Stage 4 created infra-level Bug Reports for the
``early_exit_reason`` post-run; Stage 5 surfaces the same signals **while
the run is still in progress** so operators don't have to wait for the
redirect to ``/test-execution/results`` to learn the run was cut short.

These tests pin the new DOM contract:

* **Memory pill** — read by JS from ``info.rss_mb`` /
  ``info.memory_budget_mb``; colour-banded green → amber → red. The
  pill element + JS painter must exist in the rendered template.
* **Mode badge** — read by JS from ``info.mode``. Hidden when missing
  so pre-Stage-3 runs don't sprout an empty badge.
* **Early-exit banner** — triggered by either ``status in
  (oom_exit, early_exit)`` or a non-empty ``info.early_exit_reason``.
  OOM gets a red card, wall-clock gets an amber one.
* **Status colours map** — must include the Stage-3 statuses
  (``oom_exit``, ``early_exit``, ``running_tc``) otherwise they fall
  through to the idle pill which is misleading.

JS execution is NOT tested here — the painter functions are pure
DOM-mutation code that pytest can't drive without a headless browser.
Instead we assert on the static markers (data-attrs, JS substring) so
a future refactor that drops a contract piece fails loudly.

The ``/test-execution/live/info`` integration test verifies the route
passes through new fields verbatim, which is what the JS painter
consumes.
"""

from __future__ import annotations

import json
import os

import pytest


# ── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    """Point STORAGE_FOLDER at a per-test tmp dir.

    Mirrors the pattern from ``test_walkthrough_ui_wiring.py``: the
    live-info route reads from ``<storage>/automation_runs/_live/
    info.json`` — we want to control exactly what that file contains
    without contaminating the dev share.
    """
    from routes import automation as _auto
    monkeypatch.setattr(_auto, "STORAGE_ROOT", str(tmp_path))
    return str(tmp_path)


def _write_live_info(storage_root: str, info: dict) -> str:
    live_dir = os.path.join(storage_root, "automation_runs", "_live")
    os.makedirs(live_dir, exist_ok=True)
    path = os.path.join(live_dir, "info.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(info, f)
    return path


# ── 1. Template — new DOM elements must render ──────────────────


class TestLiveTemplateDom:
    """Stage 5's new live-status DOM is present on every render."""

    def test_memory_pill_element_present(self, client):
        resp = client.get("/test-execution/live")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # The pill is initially hidden=true (JS toggles it once the
        # first /info poll returns a real budget); the data-attr is
        # the contract surface.
        assert 'data-te-mem-pill' in body
        assert 'id="status-memory"' in body
        assert 'id="status-memory-text"' in body

    def test_mode_badge_element_present(self, client):
        resp = client.get("/test-execution/live")
        body = resp.get_data(as_text=True)
        assert 'data-te-mode-badge' in body
        assert 'id="status-mode"' in body
        # Default placeholder text is "mode: —" before the first
        # info poll lands.
        assert 'mode:' in body

    def test_early_exit_banner_element_present(self, client):
        resp = client.get("/test-execution/live")
        body = resp.get_data(as_text=True)
        assert 'data-te-early-exit-banner' in body
        assert 'id="te-live-early-exit"' in body
        assert 'id="te-live-early-exit-reason"' in body
        # Banner is initially hidden; JS un-hides it when either
        # ``status`` is a cut-short variant or ``early_exit_reason``
        # is non-empty.
        assert 'id="te-live-early-exit"' in body
        # Import-partial-results link is part of the banner so
        # operators have a one-click recovery path from the live
        # tab.
        assert 'te-live-early-exit-import' in body


# ── 2. JS contract — painter functions + colours map ────────────


class TestLiveScriptContract:
    """The inline ``<script>`` block in the live template carries the
    JS contract these tests pin. We don't run JS (no headless browser
    in the suite) but we DO assert the contract markers are present,
    because a refactor that drops a colours-table entry or renames a
    painter function will silently break the live view in production."""

    def test_status_colours_includes_stage3_states(self, client):
        resp = client.get("/test-execution/live")
        body = resp.get_data(as_text=True)
        # Each new state must have a colours entry so paintStatus's
        # ``colours[status] || colours.idle`` lookup succeeds.
        assert "oom_exit:" in body
        assert "early_exit:" in body
        assert "running_tc:" in body

    def test_painter_functions_exist(self, client):
        resp = client.get("/test-execution/live")
        body = resp.get_data(as_text=True)
        assert "function paintMemoryPill(" in body
        assert "function paintModeBadge(" in body
        assert "function paintEarlyExitBanner(" in body

    def test_live_walk_recognition_substring(self, client):
        """LiveExecutor stamps ``LIVE-PAGE-NNN`` synthetic ids on each
        visited URL. The painter shows ``Walking: <url>`` instead of
        the cryptic ``Test case: LIVE-PAGE-005`` only when the regex
        match fires — pin the regex literal so a future rename of the
        id prefix can't silently break the live label."""
        resp = client.get("/test-execution/live")
        body = resp.get_data(as_text=True)
        assert "/^LIVE-PAGE-/" in body
        assert "'Walking: '" in body


# ── 3. /test-execution/live/info passes Stage-3 fields through ────


class TestLiveInfoPassesNewFields:
    """The JS painter reads info.json fields verbatim — the route must
    not strip them. These tests sit on the route boundary, NOT the JS
    side: we write a synthetic info.json mimicking what LiveExecutor
    would produce, then GET the live-info endpoint and assert the new
    fields survive the round trip."""

    def test_memory_fields_pass_through(self, client, tmp_storage):
        _write_live_info(tmp_storage, {
            "status": "running",
            "run_id": "stg5_mem",
            "rss_mb": 320,
            "memory_budget_mb": 400,
            "ts": 1_700_000_000_000,
        })
        resp = client.get("/test-execution/live/info")
        assert resp.status_code == 200
        info = resp.get_json()
        assert info["rss_mb"] == 320
        assert info["memory_budget_mb"] == 400

    def test_mode_field_passes_through(self, client, tmp_storage):
        _write_live_info(tmp_storage, {
            "status": "running",
            "mode": "live",
            "ts": 1_700_000_000_000,
        })
        info = client.get("/test-execution/live/info").get_json()
        assert info["mode"] == "live"

    def test_oom_exit_status_and_reason_pass_through(
            self, client, tmp_storage):
        """When LiveExecutor's OomGuard trips, it writes a final
        info.json with ``status='oom_exit'`` plus an ``extra`` block
        carrying ``early_exit_reason``. The route must NOT strip
        either — the JS painter trips on both surfaces."""
        _write_live_info(tmp_storage, {
            "status": "oom_exit",
            "early_exit_reason": "oom_budget_exceeded (412 MB > 400 MB)",
            "rss_mb": 412,
            "memory_budget_mb": 400,
            "mode": "live",
            "ts": 1_700_000_000_000,
        })
        info = client.get("/test-execution/live/info").get_json()
        assert info["status"] == "oom_exit"
        assert "412 MB > 400 MB" in info["early_exit_reason"]
        assert info["mode"] == "live"

    def test_wall_clock_status_and_reason_pass_through(
            self, client, tmp_storage):
        _write_live_info(tmp_storage, {
            "status": "early_exit",
            "early_exit_reason": "wall_deadline_exceeded",
            "mode": "live",
            "ts": 1_700_000_000_000,
        })
        info = client.get("/test-execution/live/info").get_json()
        assert info["status"] == "early_exit"
        assert info["early_exit_reason"] == "wall_deadline_exceeded"

    def test_legacy_run_without_new_fields_still_renders(
            self, client, tmp_storage):
        """An older info.json from pre-Stage-3 worker MUST still be
        served without exploding. The JS painter handles missing
        fields gracefully (memPill stays hidden, modeBadge stays
        hidden, early-exit banner stays hidden) — this test pins the
        ROUTE side: no schema enforcement, no 500."""
        _write_live_info(tmp_storage, {
            "status": "running",
            "current_tc": "TC-007",
            "cases_done": 3,
            "cases_total": 10,
            "ts": 1_700_000_000_000,
        })
        resp = client.get("/test-execution/live/info")
        assert resp.status_code == 200
        info = resp.get_json()
        assert info["status"] == "running"
        # Missing-by-design — JS treats absent as zero/empty.
        assert "rss_mb" not in info
        assert "memory_budget_mb" not in info
        assert "mode" not in info
