"""Stage 3 — engine/live_executor coverage.

Verifies the LiveExecutor against a stubbed Playwright surface so the
tests stay deterministic on CI (no real Chromium). The fake Playwright
hierarchy is borrowed (and extended) from
``tests/test_walkthrough_scaffold.py``.

Coverage:

1. :class:`OomGuard` — active when psutil is importable, the active /
   inactive paths return the right RSS-vs-budget answer, and a missing
   psutil makes the guard a no-op rather than a crash.
2. :func:`discover_links_on_page` — same-origin filter; off-origin
   links are dropped; broken pages return ``[]``.
3. :func:`_key_pages_from_profile` and the seed-URL precedence:
   explicit ``start_urls`` > ``SiteProfile.key_pages`` > ``base_url``.
4. ``LiveExecutor.run()`` — happy path against the fake Playwright
   produces a RunReport with one ``LIVE-PAGE-*`` script per visited
   URL, finds nothing on the empty page, and writes the live-info
   filmstrip mirror under ``_live/``.
5. ``url_pattern`` matching dispatches the right TestCases per URL —
   ``trigger="manual"`` skipped, ``trigger="always"`` fires on every
   page, ``walkthrough_url_match`` fires only where the pattern hits.
6. ``OomGuard`` early-exit: monkeypatch the guard so the second page
   visit reports over-budget; the executor returns the partial scripts
   list and exposes ``early_exit_reason`` for the worker to echo into
   result.json.
7. runner_worker dispatch — without ``LEGACY_EXECUTOR`` even an
   explicit ``mode="walkthrough"`` request goes to the live executor;
   the result.json ``mode`` field reflects this.
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from typing import Any

import pytest

# Reuse the Playwright stubs from the scaffold suite — they cover the
# same surface (goto/screenshot/locator/evaluate/on/keyboard) so the
# heuristic battery treats every page as "nothing matched". Adding a
# new file would mean maintaining two parallel fakes.
from tests.test_walkthrough_scaffold import (  # noqa: E402
    _FakeBrowser,
    _FakeContext,
    _FakeKeyboard,
    _FakeLocator,
    _FakeMouse,
    _FakePage,
    _FakePlaywright,
    _FakePlaywrightCM,
    fake_pw,
    tmp_storage,
)


# ── OomGuard ────────────────────────────────────────────────────────


class TestOomGuard:
    def test_active_when_psutil_available(self):
        from engine.live_executor import OomGuard
        g = OomGuard(400)
        assert g.budget_mb == 400
        assert g.budget_bytes == 400 * 1024 * 1024
        assert g.active is True
        # rss_mb returns a positive integer on this process.
        assert g.rss_mb() > 0
        # Real budget is well above the current RSS — over_budget False.
        assert g.over_budget() is False

    @staticmethod
    def _fake_process(own_mb: int, child_mb: tuple[int, ...] = ()):
        """A process tree with a known shape.

        Children are modelled because Chromium **is** the children — a
        fake with none describes the one world in which the old guard
        looked correct, which is how it stayed wrong.
        """
        def _mem(mb):
            return type("Mem", (), {"rss": mb * 1024 * 1024})()

        class _Child:
            def __init__(self, mb):
                self._mb = mb

            def memory_info(self):
                return _mem(self._mb)

        class _Proc:
            def memory_info(self):
                return _mem(own_mb)

            def children(self, recursive=False):
                return [_Child(mb) for mb in child_mb]

        return _Proc

    def test_over_budget_true_when_rss_exceeds(self, monkeypatch):
        from engine.live_executor import OomGuard
        g = OomGuard(1)  # 1 MB budget — the test process is bigger
        monkeypatch.setattr(g._psutil, "Process",
                            self._fake_process(200))
        assert g.over_budget() is True
        assert g.rss_mb() == 200

    def test_it_counts_the_browser_and_not_just_python(self, monkeypatch):
        """The regression. Playwright runs Chromium as a separate process
        tree, so ``Process().memory_info()`` watches the one participant
        that does not grow.

        Measured on a real pass before the fix: python stayed at 36 MB
        while the tree reached 393 MB, so a 400 MB budget never fired and
        the kernel did the stopping instead — the symptom render.yaml
        records as "OOM-killed ~110 s into a generation".
        """
        from engine.live_executor import OomGuard
        g = OomGuard(300)
        # A 36 MB Python process with 360 MB of Chromium under it.
        monkeypatch.setattr(g._psutil, "Process",
                            self._fake_process(36, (200, 120, 40)))

        assert g.own_rss_mb() == 36, "the diagnostic view is the old number"
        assert g.rss_mb() == 396, "the guard must see the whole tree"
        assert g.over_budget() is True, (
            "396 MB against a 300 MB budget must fire; watching Python "
            "alone would report 36 and let the container die")

    def test_a_child_that_vanishes_mid_poll_does_not_break_it(self,
                                                              monkeypatch):
        """Chromium exits between listing and reading all the time."""
        from engine.live_executor import OomGuard
        g = OomGuard(300)

        class _Gone:
            def memory_info(self):
                raise RuntimeError("no such process")

        class _Proc:
            def memory_info(self):
                return type("M", (), {"rss": 50 * 1024 * 1024})()

            def children(self, recursive=False):
                return [_Gone()]

        monkeypatch.setattr(g._psutil, "Process", lambda: _Proc())
        assert g.rss_mb() == 50
        assert g.over_budget() is False

    def test_the_budget_is_derived_from_the_container_limit(self,
                                                            monkeypatch):
        """One constant cannot be right on both a 512 MB dyno and a 16 GB
        Actions runner, so the budget is the ceiling minus the worst
        single step a poll can miss."""
        from engine import live_executor as le
        monkeypatch.delenv("MEMORY_BUDGET_MB", raising=False)
        monkeypatch.setattr(le, "container_memory_limit_mb", lambda: 512)

        assert le._default_budget_mb() == 512 - le.STEP_HEADROOM_MB

        monkeypatch.setattr(le, "container_memory_limit_mb", lambda: 16000)
        assert le._default_budget_mb() == 16000 - le.STEP_HEADROOM_MB

    def test_the_headroom_covers_the_worst_measured_step(self):
        """+122 MB for ``new_page()`` was the largest jump between two
        polls in the E5.2 measurement. A headroom below it means the guard
        can be overtaken inside one step."""
        from engine.live_executor import STEP_HEADROOM_MB
        assert STEP_HEADROOM_MB >= 122

    def test_an_explicit_setting_wins_over_the_derivation(self,
                                                          monkeypatch):
        from engine import live_executor as le
        monkeypatch.setenv("MEMORY_BUDGET_MB", "250")
        monkeypatch.setattr(le, "container_memory_limit_mb", lambda: 512)
        assert le._default_budget_mb() == 250

    def test_an_unreadable_limit_falls_back_rather_than_crashing(
            self, monkeypatch):
        from engine import live_executor as le
        monkeypatch.delenv("MEMORY_BUDGET_MB", raising=False)
        monkeypatch.setattr(le, "container_memory_limit_mb", lambda: None)
        assert le._default_budget_mb() == le.FALLBACK_MEMORY_BUDGET_MB

    def test_the_limit_reader_ignores_the_no_limit_sentinel(self,
                                                            monkeypatch):
        """cgroup v1 reports a number near 2**63 for "unlimited", and
        v2 reports the string "max". Neither is a ceiling."""
        from engine import live_executor as le
        import builtins
        real_open = builtins.open

        def _fake_open(path, *a, **kw):
            if str(path).startswith("/sys/fs/cgroup"):
                return type("F", (), {
                    "read": lambda self: "max",
                    "__enter__": lambda self: self,
                    "__exit__": lambda self, *e: False})()
            return real_open(path, *a, **kw)

        monkeypatch.setattr(builtins, "open", _fake_open)
        value = le.container_memory_limit_mb()
        # Falls through to host RAM, which is a real number and not the
        # sentinel.
        assert value is None or value > 0

    def test_inactive_when_psutil_missing(self, monkeypatch):
        """With psutil swapped out the guard is functionally a no-op."""
        from engine.live_executor import OomGuard
        g = OomGuard(400)
        # Simulate "psutil never imported" by clearing the handle.
        g._psutil = None
        assert g.active is False
        assert g.rss_mb() == 0
        assert g.over_budget() is False

    def test_zero_budget_disables_guard(self):
        from engine.live_executor import OomGuard
        g = OomGuard(0)
        # Budget == 0 means "never trip" — caller opted out.
        assert g.active is False
        assert g.over_budget() is False


# ── Link discovery ──────────────────────────────────────────────────


class TestDiscoverLinks:
    def test_returns_internal_links(self, fake_pw):
        from engine.live_executor import discover_links_on_page
        page = _FakePage(evaluate_results={
            "links.add": [
                "https://example.com/about",
                "https://example.com/contact",
                "https://other.com/external",  # off-origin, filtered
            ],
        })
        out = discover_links_on_page(page, "https://example.com/")
        assert "https://example.com/about" in out
        assert "https://example.com/contact" in out
        # off-origin link dropped
        assert "https://other.com/external" not in out

    def test_respects_limit(self):
        from engine.live_executor import discover_links_on_page
        urls = [f"https://example.com/p{i}" for i in range(50)]
        page = _FakePage(evaluate_results={"links.add": urls})
        out = discover_links_on_page(page, "https://example.com/",
                                      limit=5)
        assert len(out) == 5

    def test_empty_on_evaluate_failure(self):
        """A page.evaluate that raises must yield an empty list, not
        crash the executor."""
        from engine.live_executor import discover_links_on_page

        class _ExplodingPage:
            def evaluate(self, *_a, **_kw):
                raise RuntimeError("page closed")

        out = discover_links_on_page(_ExplodingPage(), "https://example.com/")
        assert out == []


# ── Seed URL resolution ─────────────────────────────────────────────


class TestSeedUrlResolution:
    def test_explicit_start_urls_win(self, tmp_storage):
        """Explicit start_urls take precedence over SiteProfile.key_pages —
        the latter is skipped entirely. base_url is still appended as
        a safety net so a typo in start_urls doesn't strand the run
        without a seed."""
        from engine.live_executor import LiveExecutor
        ex = LiveExecutor(
            storage_root=tmp_storage,
            base_url="https://example.com/",
            site_profile={"key_pages": [{"url": "https://example.com/kp",
                                          "role": "checkout"}]},
        )
        seeds = ex._resolve_seed_urls(["https://example.com/explicit"])
        # Explicit URL is first; key_pages NOT included; base_url tail.
        assert seeds[0] == "https://example.com/explicit"
        assert "https://example.com/kp" not in seeds
        assert "https://example.com/" in seeds

    def test_key_pages_used_when_no_explicit(self, tmp_storage):
        from engine.live_executor import LiveExecutor
        ex = LiveExecutor(
            storage_root=tmp_storage,
            base_url="https://example.com/",
            site_profile={"key_pages": [
                {"url": "https://example.com/checkout", "role": "checkout"},
                {"url": "https://example.com/login",    "role": "auth"},
            ]},
        )
        seeds = ex._resolve_seed_urls(None)
        # key_pages first (precedence), then base_url appended.
        assert "https://example.com/checkout" in seeds
        assert "https://example.com/login" in seeds
        assert "https://example.com/" in seeds

    def test_base_url_fallback(self, tmp_storage):
        from engine.live_executor import LiveExecutor
        ex = LiveExecutor(
            storage_root=tmp_storage,
            base_url="https://example.com/",
        )
        seeds = ex._resolve_seed_urls(None)
        assert seeds == ["https://example.com/"]

    def test_seeds_capped_at_max_pages(self, tmp_storage):
        from engine.live_executor import LiveExecutor
        ex = LiveExecutor(
            storage_root=tmp_storage,
            base_url="https://example.com/",
            max_pages=2,
        )
        seeds = ex._resolve_seed_urls([
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/c",
            "https://example.com/d",
        ])
        assert len(seeds) == 2

    def test_normalize_dedupes_trailing_slash(self, tmp_storage):
        from engine.live_executor import LiveExecutor
        assert (LiveExecutor._normalize_url("https://x.com/foo/")
                == LiveExecutor._normalize_url("https://x.com/foo"))
        assert LiveExecutor._normalize_url("not a url") == ""


# ── Happy-path run ──────────────────────────────────────────────────


class TestLiveExecutorRun:
    def test_visits_each_seed_url(self, fake_pw, tmp_storage,
                                    monkeypatch):
        fake_pw()
        # Skip the heavy TC dispatch path — no project, no test_cases.
        from engine.live_executor import LiveExecutor
        ex = LiveExecutor(
            storage_root=tmp_storage,
            base_url="https://example.com/",
            max_pages=3,
            device_timeout_ms=60000,
        )
        report = ex.run(start_urls=[
            "https://example.com/",
            "https://example.com/about",
        ])
        assert report.total >= 2
        # Each visited URL produces one LIVE-PAGE-NNN script.
        page_scripts = [s for s in report.scripts
                         if s.tc_id.startswith("LIVE-PAGE-")]
        assert len(page_scripts) == 2
        for s in page_scripts:
            assert s.status == "passed"
            assert s.steps[0].action == "goto"
            assert s.steps[0].status == "passed"

    def test_writes_live_info_with_ts_heartbeat(self, fake_pw,
                                                  tmp_storage):
        fake_pw()
        from engine.live_executor import LiveExecutor
        ex = LiveExecutor(storage_root=tmp_storage,
                          base_url="https://example.com/")
        report = ex.run(start_urls=["https://example.com/"])
        # Live info exists and carries ``ts`` (the stall-detector key)
        # plus ``mode == 'live'`` so the UI can branch on dispatch.
        info_path = os.path.join(tmp_storage, "automation_runs",
                                  "_live", "info.json")
        with open(info_path) as f:
            info = json.load(f)
        assert info["mode"] == "live"
        assert info["status"] in ("done", "early_exit", "oom_exit")
        assert info["ts"] > 0
        assert info["run_id"] == report.run_id

    def test_navigation_failure_marks_script_failed(self, fake_pw,
                                                     tmp_storage):
        fake_pw(fail_on={"goto"})
        from engine.live_executor import LiveExecutor
        ex = LiveExecutor(storage_root=tmp_storage,
                          base_url="https://example.com/")
        report = ex.run(start_urls=["https://example.com/"])
        # Navigation_timeout finding surfaces; the script ends up
        # ``failed`` rather than ``passed``.
        page_scripts = [s for s in report.scripts
                         if s.tc_id.startswith("LIVE-PAGE-")]
        assert len(page_scripts) == 1
        assert page_scripts[0].status == "failed"
        assert any(f["defect_class"] == "navigation_timeout"
                    for f in ex.findings)


class TestLivePaintSettle:
    """The live view rendered black because the only walk frame was
    captured right after `domcontentloaded` (pre-paint). `_walk_one` now
    settles for paint before the first frame and captures a second frame
    after the heuristic battery so a single-page walk doesn't look
    frozen on one early shot."""

    def test_settle_never_raises(self, tmp_storage):
        from engine.live_executor import LiveExecutor
        ex = LiveExecutor(storage_root=tmp_storage,
                          base_url="https://example.com/")

        # A page that never loads AND whose settle wait explodes must not
        # propagate — the walk has to continue and capture *something*.
        class _RaisingPage:
            def wait_for_load_state(self, *a, **k):
                raise RuntimeError("never loads")

            def wait_for_timeout(self, *a, **k):
                raise RuntimeError("boom")

        ex._settle_for_paint(_RaisingPage())  # no exception = pass

        # A page missing wait_for_load_state entirely (older fake) still
        # falls through to the fixed settle.
        class _NoLoadStatePage:
            def __init__(self):
                self.waited = False

            def wait_for_timeout(self, *a, **k):
                self.waited = True

        p = _NoLoadStatePage()
        ex._settle_for_paint(p)
        assert p.waited is True

    def test_walk_captures_initial_and_post_heuristics_frame(
            self, fake_pw, tmp_storage):
        fake_pw()
        from engine.live_executor import LiveExecutor
        ex = LiveExecutor(storage_root=tmp_storage,
                          base_url="https://example.com/", max_pages=1)
        report = ex.run(start_urls=["https://example.com/"])
        run_dir = os.path.join(tmp_storage, "automation_runs",
                               report.run_id, "LIVE-PAGE-001")
        # Both the painted-on-load frame and the after-heuristics frame
        # land on disk, and the live mirror is populated.
        assert os.path.isfile(os.path.join(run_dir, "page.png"))
        assert os.path.isfile(os.path.join(run_dir, "page_after.png"))
        assert os.path.isfile(os.path.join(
            tmp_storage, "automation_runs", "_live", "latest.png"))

    def test_failed_navigation_skips_second_frame(self, fake_pw,
                                                   tmp_storage):
        # When goto fails there's no painted page — neither frame should
        # be written, and the walk must still not crash.
        fake_pw(fail_on={"goto"})
        from engine.live_executor import LiveExecutor
        ex = LiveExecutor(storage_root=tmp_storage,
                          base_url="https://example.com/", max_pages=1)
        report = ex.run(start_urls=["https://example.com/"])
        run_dir = os.path.join(tmp_storage, "automation_runs",
                               report.run_id, "LIVE-PAGE-001")
        assert not os.path.isfile(os.path.join(run_dir, "page_after.png"))


# ── PR-B: page screenshot fan-out into findings ─────────────────────


class TestWalkthroughScreenshotWiring:
    """PR-B regression — every walkthrough heuristic currently calls
    ``note(...)`` without ``screenshot=``, so ``finding["screenshot"]``
    used to stay "" and :func:`create_bug_from_walkthrough_finding`
    persisted ``attachments=[]``. ``_walk_one`` now fan-outs the
    page-level screenshot path into any finding emitted during that
    page visit that has no shot of its own.
    """

    def test_findings_without_screenshot_inherit_page_shot(
        self, fake_pw, tmp_storage, monkeypatch
    ):
        fake_pw()
        from engine import live_executor as le

        # Stub one heuristic to emit a finding without a screenshot —
        # this matches the shape every real heuristic currently
        # produces (none of them set ``screenshot=`` today).
        def _fake_scan(page, url, tc_id, *, note):
            note(
                "Major", "Images", "broken_image",
                f"Broken image on {url}",
                url=url, tc_id=tc_id, element="img.hero",
            )

        monkeypatch.setattr(le, "scan_broken_images", _fake_scan)
        ex = le.LiveExecutor(
            storage_root=tmp_storage,
            base_url="https://example.com/",
            max_pages=1,
        )
        ex.run(start_urls=["https://example.com/"])

        broken = [f for f in ex.findings
                   if f["defect_class"] == "broken_image"]
        assert broken, (
            "fake heuristic should have emitted one broken_image finding"
        )
        # The page-shot path was injected — no longer empty.
        assert broken[0]["screenshot"], (
            "page-level screenshot must be fanned out into findings "
            "that don't set one of their own — otherwise the bug "
            "factory writes attachments=[] and /bug-reports shows the "
            "misleading 'No attachments captured' banner"
        )
        # The path points at the actual PNG written by ``_screenshot``.
        assert broken[0]["screenshot"].endswith(".png"), (
            f"expected a .png path, got {broken[0]['screenshot']!r}"
        )

    def test_explicit_finding_screenshot_is_preserved(
        self, fake_pw, tmp_storage, monkeypatch
    ):
        """Future-proofing: if a heuristic ever does pass
        ``screenshot=`` (per-element capture), the fan-out must keep
        the explicit value rather than overwriting it with the page
        shot.
        """
        fake_pw()
        from engine import live_executor as le

        explicit = "/explicit/per-element-shot.png"

        def _fake_scan(page, url, tc_id, *, note):
            note(
                "Minor", "CTAs", "cta_tiny_tap_target",
                f"CTA too small on {url}",
                url=url, tc_id=tc_id, element="button.signup",
                screenshot=explicit,
            )

        monkeypatch.setattr(le, "scan_ctas", _fake_scan)
        ex = le.LiveExecutor(
            storage_root=tmp_storage,
            base_url="https://example.com/",
            max_pages=1,
        )
        ex.run(start_urls=["https://example.com/"])

        cta = [f for f in ex.findings
                if f["defect_class"] == "cta_tiny_tap_target"]
        assert cta, "fake heuristic should have emitted one CTA finding"
        assert cta[0]["screenshot"] == explicit, (
            "fan-out must not clobber an explicit per-element shot — "
            "future heuristics that capture targeted evidence rely "
            "on this invariant"
        )

    def test_walkthrough_step_gallery_inherits_page_shot(
        self, fake_pw, tmp_storage, monkeypatch
    ):
        """Side-effect coverage: the per-page gallery card synthesises
        one ``StepResult`` per finding (with ``action='walkthrough_check'``
        and ``screenshot_after=f.screenshot``). Before PR-B this slot
        was empty for every walkthrough defect; after the fan-out the
        thumbnail finally shows up.
        """
        fake_pw()
        from engine import live_executor as le

        def _fake_scan(page, url, tc_id, *, note):
            note("Major", "Images", "broken_image",
                 f"Broken on {url}", url=url, tc_id=tc_id)

        monkeypatch.setattr(le, "scan_broken_images", _fake_scan)
        ex = le.LiveExecutor(
            storage_root=tmp_storage,
            base_url="https://example.com/",
            max_pages=1,
        )
        report = ex.run(start_urls=["https://example.com/"])

        page_script = next(s for s in report.scripts
                            if s.tc_id.startswith("LIVE-PAGE-"))
        walk_steps = [s for s in page_script.steps
                       if s.action == "walkthrough_check"]
        assert walk_steps, "expected a walkthrough_check step"
        assert walk_steps[0].screenshot_after, (
            "per-defect gallery row must carry a screenshot path so "
            "the bug-report card and the live gallery both show a "
            "thumbnail"
        )


# ── TC dispatch via url_pattern + trigger ───────────────────────────


class TestTcDispatch:
    """Exercise the matching + execution loop with a stubbed inner
    AutomationRunner so we don't need real Playwright TC step logic
    here — :class:`AutomationRunner` itself is exhaustively tested
    elsewhere (tests/test_automation_*)."""

    def _build_executor(self, fake_pw, tmp_storage, monkeypatch,
                        test_cases):
        fake_pw()
        from engine.live_executor import LiveExecutor

        captured: dict[str, Any] = {"runs": []}

        class _FakeInnerRunner:
            def __init__(self, **_kw):
                pass

            def _run_script(self, _browser, script, _run_dir):
                captured["runs"].append({
                    "tc_id":  script.tc_id,
                    "summary": script.summary,
                    "steps":  [s.action for s in script.steps],
                })
                from engine.automation_runner import ScriptResult
                return ScriptResult(
                    tc_id=script.tc_id, summary=script.summary,
                    status="passed", duration_ms=10,
                    final_url="https://example.com/",
                )

        # The internal runner is built inside ``run()``; patch
        # AutomationRunner on the module live_executor imports it
        # from so the fake replaces it cleanly.
        import engine.live_executor as _le_mod
        monkeypatch.setattr(_le_mod, "AutomationRunner", _FakeInnerRunner)

        ex = LiveExecutor(
            storage_root=tmp_storage,
            base_url="https://example.com/",
            test_cases=test_cases,
            max_pages=2,
        )
        return ex, captured

    def test_trigger_always_fires_on_every_page(
            self, fake_pw, tmp_storage, monkeypatch):
        tcs = [{
            "id": "TC-001", "summary": "Smoke",
            "preconditions": "", "test_steps": "Open page",
            "test_data": "", "expected_result": "Page loads",
            "url_pattern": "", "trigger": "always",
        }]
        ex, captured = self._build_executor(fake_pw, tmp_storage,
                                              monkeypatch, tcs)
        ex.run(start_urls=[
            "https://example.com/", "https://example.com/about",
        ])
        # TC-001 should have run twice (once per page).
        ids = [r["tc_id"] for r in captured["runs"]]
        assert ids.count("TC-001") == 2

    def test_trigger_manual_skipped(self, fake_pw, tmp_storage,
                                      monkeypatch):
        tcs = [{
            "id": "TC-002", "summary": "Manual",
            "preconditions": "", "test_steps": "Click",
            "test_data": "", "expected_result": "OK",
            "url_pattern": "*", "trigger": "manual",
        }]
        ex, captured = self._build_executor(fake_pw, tmp_storage,
                                              monkeypatch, tcs)
        ex.run(start_urls=["https://example.com/"])
        assert captured["runs"] == []

    def test_url_pattern_match_dispatch(self, fake_pw, tmp_storage,
                                          monkeypatch):
        tcs = [{
            "id": "TC-CHK", "summary": "Checkout flow",
            "preconditions": "", "test_steps": "Click pay",
            "test_data": "", "expected_result": "Pay",
            "url_pattern": "*checkout*",
            "trigger": "walkthrough_url_match",
        }]
        ex, captured = self._build_executor(fake_pw, tmp_storage,
                                              monkeypatch, tcs)
        ex.run(start_urls=[
            "https://example.com/",            # doesn't match
            "https://example.com/checkout",    # matches
        ])
        ids = [r["tc_id"] for r in captured["runs"]]
        assert ids == ["TC-CHK"]
        # tc_bindings audit trail records the match.
        urls = [b["url"] for b in ex.tc_bindings]
        assert any("checkout" in u for u in urls)


# ── OomGuard early-exit ─────────────────────────────────────────────


class TestOomGuardEarlyExit:
    def test_run_stops_when_guard_trips_between_pages(
            self, fake_pw, tmp_storage, monkeypatch):
        fake_pw()
        from engine.live_executor import LiveExecutor, OomGuard

        # Two-state OomGuard fake: first call clean, every subsequent
        # call reports over-budget. This makes page-1 succeed and
        # forces an early exit before page-2's heuristics run.
        state = {"calls": 0}

        class _TrippingGuard(OomGuard):
            def __init__(self):
                super().__init__(400)

            def over_budget(self) -> bool:
                state["calls"] += 1
                return state["calls"] > 1

            def rss_mb(self) -> int:
                return 999

        ex = LiveExecutor(
            storage_root=tmp_storage,
            base_url="https://example.com/",
            max_pages=5,
        )
        ex.oom_guard = _TrippingGuard()

        report = ex.run(start_urls=[
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/c",
        ])
        # Only one page should make it through.
        page_scripts = [s for s in report.scripts
                         if s.tc_id.startswith("LIVE-PAGE-")]
        assert len(page_scripts) == 1
        # early_exit_reason populated for the worker echo.
        assert "oom" in ex.early_exit_reason

    def test_info_json_status_is_oom_exit(
            self, fake_pw, tmp_storage, monkeypatch):
        fake_pw()
        from engine.live_executor import LiveExecutor, OomGuard

        class _ImmediateTripGuard(OomGuard):
            def __init__(self):
                super().__init__(400)

            def over_budget(self) -> bool:
                return True

            def rss_mb(self) -> int:
                return 999

        ex = LiveExecutor(
            storage_root=tmp_storage,
            base_url="https://example.com/",
        )
        ex.oom_guard = _ImmediateTripGuard()
        ex.run(start_urls=["https://example.com/"])
        info_path = os.path.join(tmp_storage, "automation_runs",
                                  "_live", "info.json")
        with open(info_path) as f:
            info = json.load(f)
        assert info["status"] == "oom_exit"
        assert "oom" in info.get("early_exit_reason", "")


# ── runner_worker dispatch ──────────────────────────────────────────


class TestRunnerWorkerDispatch:
    def _spawn_worker(self, monkeypatch, storage_root, cfg):
        """Same in-process invocation as the walkthrough scaffold tests."""
        cfg_path = os.path.join(storage_root, "cfg.json")
        with open(cfg_path, "w") as f:
            json.dump(cfg, f)
        monkeypatch.setattr(sys, "argv", ["runner_worker", cfg_path])
        from engine import runner_worker
        rc = runner_worker.main()
        pending = os.path.join(storage_root, "automation_runs",
                                "_pending")
        cfg_id = "cfg"
        rp = os.path.join(pending, f"{cfg_id}.result.json")
        if not os.path.isfile(rp):
            return rc, None
        with open(rp) as f:
            return rc, json.load(f)

    def test_default_mode_routes_to_live(self, monkeypatch, fake_pw,
                                            tmp_storage):
        """No ``mode`` in config + LEGACY_EXECUTOR unset → live."""
        fake_pw()
        monkeypatch.delenv("LEGACY_EXECUTOR", raising=False)

        rc, result = self._spawn_worker(monkeypatch, tmp_storage, {
            "config_id": "cfg",
            "storage_root": tmp_storage,
            # No "mode" field — must default to live.
            "base_url": "https://example.com/",
            "live": {
                "start_urls": ["https://example.com/"],
                "max_pages": 1,
                "device_timeout_ms": 60000,
            },
            "runner_kwargs": {"headless": True},
        })
        assert rc == 0
        assert result is not None
        assert result["status"] == "done"
        assert result.get("mode") == "live"
        # Scripts use the LIVE- prefix, not WALK-.
        assert any(s["tc_id"].startswith("LIVE-PAGE-")
                    for s in result["report"]["scripts"])

    def test_explicit_walkthrough_redirects_to_live_without_flag(
            self, monkeypatch, fake_pw, tmp_storage):
        """Saved configs from Sprint 5 with ``mode="walkthrough"`` get
        silently redirected to live unless ``LEGACY_EXECUTOR=1``."""
        fake_pw()
        monkeypatch.delenv("LEGACY_EXECUTOR", raising=False)
        monkeypatch.delenv("WALKTHROUGH_MODE_ENABLED", raising=False)

        rc, result = self._spawn_worker(monkeypatch, tmp_storage, {
            "config_id": "cfg",
            "storage_root": tmp_storage,
            "mode": "walkthrough",
            "base_url": "https://example.com/",
            "walkthrough": {
                "start_urls": ["https://example.com/"],
                "max_pages": 1,
                "device_timeout_ms": 60000,
            },
            "runner_kwargs": {"headless": True},
        })
        assert rc == 0
        assert result["status"] == "done"
        # Redirected — mode echo is the canonical 'live'.
        assert result.get("mode") == "live"

    def test_early_exit_reason_surfaced(self, monkeypatch, fake_pw,
                                         tmp_storage):
        """When LiveExecutor.OomGuard fires, result.json carries the
        reason so the UI can render a 'stopped early' badge."""
        fake_pw()
        monkeypatch.delenv("LEGACY_EXECUTOR", raising=False)

        # Patch the OomGuard used by LiveExecutor so the FIRST page
        # already trips the over-budget check.
        import engine.live_executor as _le_mod

        class _TripGuard:
            budget_mb = 400
            def __init__(self, *a, **k): pass
            @property
            def active(self): return True
            def rss_mb(self): return 999
            def over_budget(self): return True

        monkeypatch.setattr(_le_mod, "OomGuard", _TripGuard)

        rc, result = self._spawn_worker(monkeypatch, tmp_storage, {
            "config_id": "cfg",
            "storage_root": tmp_storage,
            "base_url": "https://example.com/",
            "live": {
                "start_urls": ["https://example.com/"],
                "max_pages": 5,
            },
            "runner_kwargs": {"headless": True},
        })
        assert rc == 0
        assert "oom" in (result.get("early_exit_reason") or "").lower()
