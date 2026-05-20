"""TFWefloLab integration PR-2 — heuristic battery + dedup + factory.

The PR-1 scaffold tests in ``test_walkthrough_scaffold.py`` already
verify navigation / max_pages / device timeout / worker dispatch via a
default-empty ``_FakePage``. The tests below exercise the new code
landing in PR-2:

* the seven heuristic methods on :class:`WalkthroughRunner` — each
  driven via a custom ``_FakePage`` evaluate-result table to emulate
  the JS bridge's output without spawning Chromium
* :func:`engine.walkthrough_dedup.fingerprint` + ``dedupe`` — both the
  flat-list and per-env mapping shapes
* :func:`engine.bug_report.create_bug_from_walkthrough_finding`
* :mod:`engine.walkthrough_tc_match` — regex / glob / substring kinds
  plus the trigger-aware ``match_tcs_for_url`` selector
* ``engine.bug_template`` — every PR-2 defect class lands a non-empty
  ``DEFECT_PHRASES`` entry and a sane :func:`severity_priority` output
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from typing import Any

import pytest


# Reuse the upgraded fake-Playwright surface from the scaffold tests
# rather than duplicating ~150 LOC of Page / Locator stubs.
sys.path.insert(0, os.path.dirname(__file__))
from test_walkthrough_scaffold import (  # noqa: E402
    _FakeBrowser, _FakeChromium, _FakeContext, _FakeLocator,
    _FakePage, _FakePlaywright, _FakePlaywrightCM, fake_pw,
    tmp_storage,
)


# ── 1. broken-image heuristic ─────────────────────────────────────


def _make_runner(tmp_storage, **kwargs):
    """Construct a WalkthroughRunner with sensible test defaults."""
    from engine.walkthrough_runner import WalkthroughRunner
    return WalkthroughRunner(
        storage_root=tmp_storage,
        base_url=kwargs.pop("base_url", "https://example.com/"),
        headless=True,
        # Most heuristic tests don't need network access.
        axe_enabled=kwargs.pop("axe_enabled", False),
        **kwargs,
    )


def _page_with_evaluate(evaluate_table: dict[str, Any],
                         locator_table: dict[str, Any] | None = None,
                         final_url: str = "https://example.com/"):
    """One-page fake configured with a JS-eval lookup + locator table."""
    page = _FakePage(final_url=final_url,
                      evaluate_results=evaluate_table)
    if locator_table:
        page._locator_table = locator_table
    return page


class TestBrokenImageHeuristic:
    def test_emits_one_finding_per_broken_image(self, tmp_storage):
        runner = _make_runner(tmp_storage)
        page = _page_with_evaluate({
            "document.images": [
                {"src": "https://cdn.example.com/hero.png",
                 "alt": "Hero banner", "cls": "hero", "parentCls": "hero-wrap"},
                {"src": "https://cdn.example.com/icon.svg",
                 "alt": "", "cls": "", "parentCls": ""},
            ],
        })
        runner._scan_broken_images(page,
                                    "https://example.com/", "WALK-001")
        assert len(runner.findings) == 2
        codes = {f["defect_class"] for f in runner.findings}
        assert codes == {"broken_image"}
        # Selector carries the src so dedup can collapse across envs.
        assert any('hero.png' in f["element"]
                    for f in runner.findings)
        # Alt-text-based headline beats the filename when available.
        assert any('"Hero banner"' in f["message"]
                    for f in runner.findings)
        # Major severity per the CLASS_SEVERITY ladder.
        for f in runner.findings:
            assert f["severity"] == "Major"

    def test_no_findings_when_all_images_load(self, tmp_storage):
        runner = _make_runner(tmp_storage)
        page = _page_with_evaluate({"document.images": []})
        runner._scan_broken_images(page,
                                    "https://example.com/", "WALK-001")
        assert runner.findings == []


# ── 2. navigation menu (hamburger / dropdown) ─────────────────────


class _BurgerLocator(_FakeLocator):
    """Locator that responds positively to count/is_visible and tracks
    whether click() was called. Used to simulate a real hamburger
    trigger that ``locator(...).first`` returns."""

    def __init__(self, *, clicked: bool = False):
        super().__init__()
        self._clicked = clicked

    @property
    def first(self):
        return self

    def count(self) -> int:
        return 1

    def is_visible(self) -> bool:
        return True

    def click(self, **_kw) -> None:
        self._clicked = True


class TestNavigationMenuHeuristic:
    def test_hamburger_dead_on_mobile(self, tmp_storage):
        runner = _make_runner(tmp_storage, viewport=(390, 844))
        assert runner.device_kind == "mobile"
        burger = _BurgerLocator()
        # Before == after means tap did NOT reveal more nav links.
        page = _page_with_evaluate(
            {"countVisibleNav": 0,
             ".w-nav-menu a": 0,
             "Array.from(els).filter": 0},
            locator_table={".w-nav-button": burger},
        )
        runner._scan_navigation_menu(page,
                                      "https://example.com/", "WALK-001")
        assert burger._clicked, "burger trigger should be clicked"
        assert len(runner.findings) == 1
        f = runner.findings[0]
        assert f["defect_class"] == "hamburger_dead"
        assert f["severity"] == "Critical"

    def test_hamburger_passes_when_menu_opens(self, tmp_storage):
        runner = _make_runner(tmp_storage, viewport=(390, 844))
        burger = _BurgerLocator()

        # Return 0 before click, 5 after. The runner calls the same
        # JS twice; we toggle via a small counter.
        call_count = {"n": 0}

        class _TogglePage(_FakePage):
            def evaluate(self, expression, *_a, **_kw):
                if "filter(el =>" in expression:
                    call_count["n"] += 1
                    return 0 if call_count["n"] == 1 else 5
                return []

        page = _TogglePage(final_url="https://example.com/")
        page._locator_table = {".w-nav-button": burger}
        runner._scan_navigation_menu(page,
                                      "https://example.com/", "WALK-001")
        assert runner.findings == []

    def test_desktop_skips_hamburger_probe(self, tmp_storage):
        runner = _make_runner(tmp_storage, viewport=(1440, 900))
        assert runner.device_kind == "desktop"
        # Desktop path uses dropdown probe — empty result = no findings.
        page = _page_with_evaluate({"w-dropdown-toggle": []})
        runner._scan_navigation_menu(page,
                                      "https://example.com/", "WALK-001")
        assert runner.findings == []


# ── 3. footer / social-link sanity ─────────────────────────────────


class TestFooterSocialHeuristic:
    def test_placeholder_host_flagged(self, tmp_storage):
        runner = _make_runner(tmp_storage)
        page = _page_with_evaluate({
            "footer a[href]": [
                # Real brand URL — must not be flagged.
                {"href": "https://facebook.com/example",
                 "target": "_blank", "rel": "noopener", "label": "FB"},
                # ``example.com`` hostname is the canonical placeholder
                # marker we look for.
                {"href": "https://twitter.example.com/x",
                 "target": "", "rel": "", "label": "TW"},
                # ``localhost`` hostname is a placeholder leak.
                {"href": "https://twitter.localhost/x",
                 "target": "", "rel": "", "label": "IG"},
            ],
        })
        runner._scan_footer_social(page,
                                    "https://example.com/", "WALK-001")
        defect_classes = [f["defect_class"] for f in runner.findings]
        assert defect_classes.count("placeholder_social") == 2

    def test_noopener_flagged_when_target_blank(self, tmp_storage):
        runner = _make_runner(tmp_storage)
        page = _page_with_evaluate({
            "footer a[href]": [
                {"href": "https://twitter.com/real",
                 "target": "_blank", "rel": "", "label": "TW"},
            ],
        })
        runner._scan_footer_social(page,
                                    "https://example.com/", "WALK-001")
        assert len(runner.findings) == 1
        assert runner.findings[0]["defect_class"] == "social_no_noopener"
        assert runner.findings[0]["severity"] == "Minor"

    def test_noopener_ok_when_rel_set(self, tmp_storage):
        runner = _make_runner(tmp_storage)
        page = _page_with_evaluate({
            "footer a[href]": [
                {"href": "https://linkedin.com/real",
                 "target": "_blank", "rel": "noopener noreferrer",
                 "label": "LI"},
            ],
        })
        runner._scan_footer_social(page,
                                    "https://example.com/", "WALK-001")
        assert runner.findings == []


# ── 4. CTA / tap-target audit ──────────────────────────────────────


class TestCtaAuditHeuristic:
    def test_no_destination_link_flagged(self, tmp_storage):
        runner = _make_runner(tmp_storage)
        page = _page_with_evaluate({
            "button:not": [
                {"text": "Learn more", "reason": "no destination",
                 "sel": "a"},
            ],
        })
        runner._scan_ctas(page, "https://example.com/", "WALK-001")
        assert len(runner.findings) == 1
        f = runner.findings[0]
        assert f["defect_class"] == "cta_no_destination"
        assert f["severity"] == "Major"

    def test_tiny_tap_target_flagged_minor(self, tmp_storage):
        runner = _make_runner(tmp_storage)
        page = _page_with_evaluate({
            "button:not": [
                {"text": "OK", "reason": "tap target 18x18px",
                 "sel": "button"},
            ],
        })
        runner._scan_ctas(page, "https://example.com/", "WALK-001")
        assert runner.findings[0]["defect_class"] == "cta_tiny_tap_target"
        assert runner.findings[0]["severity"] == "Minor"


# ── 5. axe-core a11y ───────────────────────────────────────────────


class TestAxeHeuristic:
    def test_skipped_when_disabled(self, tmp_storage):
        runner = _make_runner(tmp_storage, axe_enabled=False)
        page = _page_with_evaluate({"axe": "should-not-be-called"})
        runner._scan_axe(page, "https://example.com/", "WALK-001")
        # Crucially: add_script_tag was NEVER called, so the CDN miss
        # cannot stall the offline run.
        assert ("add_script_tag", "should-not-call") not in page.calls
        assert runner.findings == []

    def test_critical_violation_becomes_critical_finding(self, tmp_storage):
        runner = _make_runner(tmp_storage, axe_enabled=True)
        page = _page_with_evaluate({
            "window.axe": {
                "error": None,
                "violations": [
                    {"id": "html-has-lang", "impact": "critical",
                     "help": "<html> element must have lang attribute",
                     "helpUrl": "https://dequeuniversity.com/.../html-has-lang",
                     "description": "ensure lang is set",
                     "tags": ["wcag2a", "wcag311"],
                     "nodes": [{"target": "html", "html": "<html>",
                                 "failureSummary": "Fix any of the following: ..."}]},
                    {"id": "minor-thing", "impact": "minor",
                     "help": "stuff", "helpUrl": "", "description": "",
                     "tags": ["wcag2aa"], "nodes": []},
                ],
            },
        })
        runner._scan_axe(page, "https://example.com/", "WALK-001")
        # Minor impact never emits a finding.
        assert len(runner.findings) == 1
        f = runner.findings[0]
        assert f["defect_class"] == "axe_critical"
        assert f["severity"] == "Critical"
        assert "html-has-lang" in (f["dev_detail"] or "")
        assert "WCAG: wcag2a" in (f["fix_hint"] or "")

    def test_serious_violation_becomes_major(self, tmp_storage):
        runner = _make_runner(tmp_storage, axe_enabled=True)
        page = _page_with_evaluate({
            "window.axe": {
                "error": None,
                "violations": [{
                    "id": "color-contrast", "impact": "serious",
                    "help": "Contrast must be at least 4.5:1",
                    "helpUrl": "", "description": "", "tags": [],
                    "nodes": [{"target": ".cta-pill", "html": "<a class=cta-pill>...",
                                "failureSummary": ""}],
                }],
            },
        })
        runner._scan_axe(page, "https://example.com/", "WALK-001")
        assert len(runner.findings) == 1
        assert runner.findings[0]["defect_class"] == "axe_serious"
        assert runner.findings[0]["severity"] == "Major"


# ── 6. console / page-error sweep ─────────────────────────────────


class TestConsoleErrorSweep:
    def test_page_error_emits_major_finding(self, tmp_storage):
        runner = _make_runner(tmp_storage)
        runner._collect_console_errors(
            console_errors=[],
            page_errors=[{
                "message": "Cannot read properties of null (reading 'addEventListener')",
                "stack":   "TypeError: ...\n    at init (app.js:42:17)",
                "first_frame": "app.js:42:17",
                "page_url": "https://example.com/",
            }],
            url="https://example.com/",
            tc_id="WALK-001",
        )
        assert len(runner.findings) == 1
        f = runner.findings[0]
        assert f["defect_class"] == "page_error"
        assert "does not exist" in f["message"]
        assert f["severity"] == "Major"

    def test_browser_noise_is_filtered(self, tmp_storage):
        runner = _make_runner(tmp_storage)
        runner._collect_console_errors(
            console_errors=[],
            page_errors=[
                # DevTools internal — must be filtered.
                {"message": "MutationObserver target null",
                 "stack":   "  at web-inspector://bootstrap.js:621:5",
                 "first_frame": "web-inspector://bootstrap.js:621:5",
                 "page_url": "https://example.com/"},
            ],
            url="https://example.com/",
            tc_id="WALK-001",
        )
        assert runner.findings == []

    def test_duplicate_console_errors_collapsed(self, tmp_storage):
        runner = _make_runner(tmp_storage)
        runner._collect_console_errors(
            console_errors=[
                {"text": "TypeError: x is not a function",
                 "url": "https://example.com/app.js", "line": 10,
                 "col": 4, "page_url": "https://example.com/"},
                {"text": "TypeError: x is not a function",
                 "url": "https://example.com/app.js", "line": 10,
                 "col": 4, "page_url": "https://example.com/about"},
            ],
            page_errors=[],
            url="https://example.com/",
            tc_id="WALK-001",
        )
        assert len(runner.findings) == 1

    def test_third_party_noise_ignored(self, tmp_storage):
        runner = _make_runner(tmp_storage)
        runner._collect_console_errors(
            console_errors=[
                {"text": "net::ERR_BLOCKED_BY_CLIENT favicon.ico",
                 "url": "https://example.com/favicon.ico", "line": None,
                 "col": None, "page_url": "https://example.com/"},
            ],
            page_errors=[],
            url="https://example.com/",
            tc_id="WALK-001",
        )
        assert runner.findings == []


# ── 7. dedup module ───────────────────────────────────────────────


class TestDedupFingerprint:
    def test_normalises_node_counts(self):
        from engine.walkthrough_dedup import fingerprint
        f1 = {"severity": "Critical", "area": "Accessibility",
              "message": "html-has-lang (3 nodes)", "element": "html"}
        f2 = {"severity": "Critical", "area": "Accessibility",
              "message": "html-has-lang (12 nodes)", "element": "html"}
        assert fingerprint(f1) == fingerprint(f2)

    def test_normalises_unit_bearing_numbers(self):
        """Numbers attached to a unit (px / ms / s / %) collapse so a
        defect surfacing as ``120ms`` on one env and ``180ms`` on
        another fingerprints identically. Unitless dimensions stay
        distinct on purpose — a 16-px hit target is a different defect
        from a 22-px one even though both are too small."""
        from engine.walkthrough_dedup import fingerprint
        f1 = {"severity": "Major", "area": "Loading",
              "message": "Page took 1200ms to first paint",
              "element": ""}
        f2 = {"severity": "Major", "area": "Loading",
              "message": "Page took 1850ms to first paint",
              "element": ""}
        assert fingerprint(f1) == fingerprint(f2)

    def test_normalises_visible_count_phrase(self):
        from engine.walkthrough_dedup import fingerprint
        f1 = {"severity": "Critical", "area": "Navigation",
              "message": "Hamburger does not open — visible 0 → 3",
              "element": ".w-nav-button"}
        f2 = {"severity": "Critical", "area": "Navigation",
              "message": "Hamburger does not open — visible 0 → 12",
              "element": ".w-nav-button"}
        assert fingerprint(f1) == fingerprint(f2)


class TestDedupAggregate:
    def test_flat_list_collapses_duplicates(self):
        from engine.walkthrough_dedup import dedupe
        findings = [
            {"severity": "Major", "area": "Images", "message": "broken hero",
             "element": "img[src=hero.png]", "url": "https://x/a"},
            {"severity": "Major", "area": "Images", "message": "broken hero",
             "element": "img[src=hero.png]", "url": "https://x/b"},
        ]
        out = dedupe(findings)
        assert len(out) == 1
        assert out[0]["occurrences"] == 2
        assert out[0]["urls"] == ["https://x/a", "https://x/b"]
        assert "fingerprint" in out[0]

    def test_per_env_records_environment_labels(self):
        from engine.walkthrough_dedup import dedupe
        out = dedupe({
            "web": [
                {"severity": "Major", "area": "Images", "message": "broken",
                 "element": "img", "url": "https://x/"}
            ],
            "mobile_web": [
                {"severity": "Major", "area": "Images", "message": "broken",
                 "element": "img", "url": "https://x/"}
            ],
        })
        assert len(out) == 1
        assert out[0]["environments"] == ["web", "mobile_web"]
        assert out[0]["occurrences"] == 2

    def test_distinct_defects_stay_separate(self):
        from engine.walkthrough_dedup import dedupe
        findings = [
            {"severity": "Major", "area": "Images", "message": "broken hero",
             "element": "img[src=a]", "url": "https://x/"},
            {"severity": "Major", "area": "Images", "message": "broken hero",
             "element": "img[src=b]", "url": "https://x/"},
        ]
        out = dedupe(findings)
        assert len(out) == 2


# ── 8. url_pattern matcher ─────────────────────────────────────────


class TestMatchUrlPattern:
    @pytest.mark.parametrize("pattern,url,expected", [
        ("",                              "https://x/checkout", True),
        ("checkout",                      "https://x/checkout/123", True),
        ("CHECKOUT",                      "https://x/Checkout/123", True),
        ("missing",                       "https://x/home", False),
        ("*/checkout/*",                  "https://x/checkout/123", True),
        ("*/checkout/*",                  "https://x/cart", False),
        (r"^https://x/checkout/\d+$",     "https://x/checkout/42", True),
        (r"^https://x/checkout/\d+$",     "https://x/checkout/foo", False),
        ("[invalid(regex",                 "https://x/", False),
    ])
    def test_kinds(self, pattern, url, expected):
        from engine.walkthrough_tc_match import match_url_pattern
        assert match_url_pattern(pattern, url) is expected


class TestMatchTcsForUrl:
    def test_manual_trigger_never_fires(self):
        from engine.walkthrough_tc_match import match_tcs_for_url
        tcs = [{"id": "TC-1", "url_pattern": "/checkout",
                 "trigger": "manual"}]
        assert match_tcs_for_url(tcs, "https://x/checkout") == []

    def test_always_fires_regardless(self):
        from engine.walkthrough_tc_match import match_tcs_for_url
        tcs = [{"id": "TC-2", "url_pattern": "",
                 "trigger": "always"}]
        out = match_tcs_for_url(tcs, "https://x/anywhere")
        assert [t["id"] for t in out] == ["TC-2"]

    def test_url_match_fires_on_pattern_match_only(self):
        from engine.walkthrough_tc_match import match_tcs_for_url
        tcs = [
            {"id": "T-A", "url_pattern": "*/checkout/*",
             "trigger": "walkthrough_url_match"},
            {"id": "T-B", "url_pattern": "*/admin/*",
             "trigger": "walkthrough_url_match"},
        ]
        out = match_tcs_for_url(tcs, "https://x/checkout/42")
        assert [t["id"] for t in out] == ["T-A"]

    def test_unknown_trigger_silently_skipped(self):
        from engine.walkthrough_tc_match import match_tcs_for_url
        out = match_tcs_for_url([
            {"id": "TC-3", "url_pattern": "/x", "trigger": "future-mode"},
        ], "https://x/")
        assert out == []


# ── 9. tc_bindings emitted by _walk_one ────────────────────────────


class TestRunnerTcBindings:
    def test_tc_binding_recorded_for_matching_url(self, fake_pw,
                                                    tmp_storage):
        fake_pw()
        runner = _make_runner(
            tmp_storage,
            test_cases=[
                {"id": "TC-9", "external_id": "TC-009",
                 "summary": "Checkout flow",
                 "url_pattern": "*/checkout*",
                 "trigger": "walkthrough_url_match"},
                {"id": "TC-10", "external_id": "TC-010",
                 "summary": "Admin only",
                 "url_pattern": "*/admin*",
                 "trigger": "walkthrough_url_match"},
            ],
        )
        runner.run(start_urls=["https://example.com/checkout/123"])
        assert len(runner.tc_bindings) == 1
        b = runner.tc_bindings[0]
        assert b["url"] == "https://example.com/checkout/123"
        assert [m["external_id"] for m in b["matches"]] == ["TC-009"]


# ── 10. bug_template walkthrough defect classes ────────────────────


class TestBugTemplateWalkthroughClasses:
    @pytest.mark.parametrize("defect_class,expected_severity", [
        ("broken_image",        "Major"),
        ("hamburger_dead",      "Critical"),
        ("placeholder_social",  "Major"),
        ("social_no_noopener",  "Minor"),
        ("cta_no_destination",  "Major"),
        ("cta_tiny_tap_target", "Minor"),
        ("axe_critical",        "Critical"),
        ("axe_serious",         "Major"),
        ("console_js_error",    "Major"),
        ("page_error",          "Major"),
    ])
    def test_class_severity_table(self, defect_class, expected_severity):
        from engine.bug_template import CLASS_SEVERITY
        assert CLASS_SEVERITY[defect_class] == expected_severity

    @pytest.mark.parametrize("defect_class", [
        "broken_image", "hamburger_dead", "dropdown_dead",
        "placeholder_social", "social_no_noopener", "search_no_results",
        "form_unfillable", "cta_no_destination", "cta_tiny_tap_target",
        "axe_critical", "axe_serious", "clipped_text", "icon_fallback",
        "console_js_error", "page_error",
    ])
    def test_every_class_has_a_phrase(self, defect_class):
        from engine.bug_template import DEFECT_PHRASES
        phrase = DEFECT_PHRASES.get(defect_class)
        assert phrase, f"{defect_class!r} missing from DEFECT_PHRASES"

    def test_severity_priority_for_walkthrough(self):
        from engine.bug_template import severity_priority
        # hamburger_dead on a marketing-page URL → Critical / High (no
        # auth/checkout keywords).
        sev, pri = severity_priority("hamburger_dead", "Navigation",
                                       "https://example.com/")
        assert sev == "Critical"
        assert pri in ("Highest", "High")
        # Same defect on a checkout URL → Critical / Highest (revenue
        # path).
        sev2, pri2 = severity_priority("hamburger_dead", "Navigation",
                                         "https://example.com/checkout/123")
        assert pri2 == "Highest"


# ── 11. create_bug_from_walkthrough_finding factory ───────────────


class TestBugFactory:
    def test_bug_built_from_broken_image_finding(self):
        from engine.bug_report import create_bug_from_walkthrough_finding
        finding = {
            "severity":     "Major",
            "area":         "Images",
            "defect_class": "broken_image",
            "message":      "Hero banner did not load",
            "url":          "https://example.com/checkout/",
            "element":      'img[src="https://cdn/hero.png"]',
            "screenshot":   "automation_runs/r1/WALK-001/img.png",
            "fix_hint":     "Re-upload asset",
            "dev_detail":   "naturalWidth=0",
            "user_impact":  "Visitors see broken-image icon",
            "tc_id":        "WALK-001",
        }
        bug = create_bug_from_walkthrough_finding(
            finding, environment_str="Web / Chrome",
            tester_name="ci", base_url="https://example.com/")
        assert bug.severity == "Major"
        # Checkout URL → highest priority via the area-weight table.
        assert bug.priority == "Highest"
        assert bug.linked_item_type == "walkthrough"
        assert bug.linked_item_id == "WALK-001"
        assert "defect:broken_image" in bug.labels
        assert "source:walkthrough" in bug.labels
        assert bug.attachments == ["automation_runs/r1/WALK-001/img.png"]
        # STR includes the URL and the element pointer.
        assert "https://example.com/checkout/" in bug.steps_to_reproduce
        assert ("img[src=" in bug.steps_to_reproduce or
                "img[src=" in bug.actual_result)
        # Expected result includes the fix hint.
        assert "Re-upload asset" in bug.expected_result

    def test_factory_handles_missing_optional_fields(self):
        from engine.bug_report import create_bug_from_walkthrough_finding
        bug = create_bug_from_walkthrough_finding({
            "severity": "Minor", "area": "CTAs",
            "defect_class": "cta_tiny_tap_target",
            "message": "Button — tap target 18x18px",
            "url": "https://example.com/",
        })
        assert bug.severity == "Minor"
        assert bug.linked_item_type == "walkthrough"
        # No screenshot → no attachments.
        assert bug.attachments == []


# ── 12. WALKTHROUGH_FLAG passes through runner_worker payload ─────


class TestRunnerWorkerWalkthroughPayload:
    def test_result_json_carries_findings_and_bindings(
            self, monkeypatch, fake_pw, tmp_storage):
        import json
        fake_pw()
        monkeypatch.setenv("WALKTHROUGH_MODE_ENABLED", "1")

        cfg = {
            "config_id": "cfg",
            "storage_root": tmp_storage,
            "mode": "walkthrough",
            "base_url": "https://example.com/",
            "walkthrough": {
                "start_urls": ["https://example.com/"],
                "max_pages": 1,
                "device_timeout_ms": 60000,
                "axe_enabled": False,
                "test_cases": [
                    {"id": "TC-100", "external_id": "TC-100",
                     "summary": "demo", "url_pattern": "",
                     "trigger": "always"},
                ],
            },
            "runner_kwargs": {"headless": True},
        }
        cfg_path = os.path.join(tmp_storage, "cfg.json")
        with open(cfg_path, "w") as f:
            json.dump(cfg, f)
        monkeypatch.setattr(sys, "argv", ["runner_worker", cfg_path])

        from engine import runner_worker
        rc = runner_worker.main()
        assert rc == 0
        with open(os.path.join(tmp_storage, "automation_runs",
                                 "_pending", "cfg.result.json")) as f:
            result = json.load(f)
        # Schema is now: report + walkthrough_findings +
        # walkthrough_findings_deduped + walkthrough_tc_bindings.
        assert "walkthrough_findings" in result
        assert "walkthrough_findings_deduped" in result
        assert "walkthrough_tc_bindings" in result
        # ``always``-triggered TC must have surfaced in the binding
        # record for the visited URL.
        bindings = result["walkthrough_tc_bindings"]
        assert any(
            any(m["external_id"] == "TC-100" for m in b["matches"])
            for b in bindings
        )
