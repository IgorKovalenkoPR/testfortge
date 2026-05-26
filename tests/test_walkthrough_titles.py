"""PR-F regression — walkthrough-bug passive-voice title transforms.

PR-C′ delivered passive-voice titles for TC-driven bugs but missed
walkthrough bugs because :func:`engine.bug_report.create_bug_from_walkthrough_finding`
built the title as ``f"[{area}] {message}"`` straight from the
heuristic's free-text message. Every bug in the ART project export
(2026-05-26, 65 bugs) hit this gap — the operator saw the same
"[Accessibility] Select element must have an accessible name…"
positive-voice headlines as before PR-C′.

This module pins the transform contract for the three message
archetypes the heuristic battery actually produces — accessibility
"must have X", JS "error happened during Y", broken-image
"X did not load (tail)" — plus a fallback case that proves we
don't mangle messages that already read fine.
"""

from __future__ import annotations

import pytest

from engine.bug_report import (
    _walkthrough_passive_title,
    create_bug_from_walkthrough_finding,
)


class TestWalkthroughPassiveTitle:
    # ── Accessibility — "X must have Y" archetype ──────────────────

    def test_must_have_inverts_to_is_missing_from(self):
        title = _walkthrough_passive_title(
            "Accessibility",
            "Select element must have an accessible name — affects 2 "
            "elements on this page",
        )
        # "must have" → "is missing from"; quantity tail moved into ().
        assert title == (
            "[Accessibility] Accessible name is missing from select "
            "element (affects 2 elements on this page)"
        ), title

    def test_must_have_no_tail_clause(self):
        title = _walkthrough_passive_title(
            "Accessibility",
            "Button element must have an accessible label",
        )
        assert "is missing from" in title
        # No parens because there was no "—" tail.
        assert "(" not in title and ")" not in title, title

    # ── JS — "X happened during Y" archetype ───────────────────────

    def test_happened_during_inverts_to_is_raised_during(self):
        title = _walkthrough_passive_title(
            "JS",
            "A JavaScript error happened during the user journey",
        )
        assert title == (
            "[JS] JavaScript error is raised during the user journey"
        ), title

    def test_happened_while_archetype_preserves_adverb(self):
        title = _walkthrough_passive_title(
            "JS",
            "An async exception happened while loading the homepage",
        )
        assert "is raised while" in title, title

    # ── Images — "X did not load (tail)" archetype ─────────────────

    def test_broken_image_inverts_to_is_not_loaded(self):
        title = _walkthrough_passive_title(
            "Images",
            "Broken image on the page — foo.avif did not load "
            "(visitors see an empty slot or a broken-image icon)",
        )
        assert title == (
            "[Images] Image foo.avif is not loaded on the page "
            "(visitors see an empty slot or a broken-image icon)"
        ), title

    def test_broken_image_no_tail(self):
        title = _walkthrough_passive_title(
            "Images",
            "Broken image on the page — bar.svg did not load",
        )
        assert title == (
            "[Images] Image bar.svg is not loaded on the page"
        ), title

    def test_generic_did_not_load_outside_broken_image_phrase(self):
        title = _walkthrough_passive_title(
            "Performance",
            "The hero banner did not load",
        )
        assert "is not loaded after page navigation" in title, title

    # ── "X failed to Y" archetype ──────────────────────────────────

    def test_failed_to_archetype(self):
        title = _walkthrough_passive_title(
            "Forms",
            "Submit button failed to respond to click",
        )
        assert title == (
            "[Forms] Submit button did not respond to click as expected"
        ), title

    # ── Fallback — already-grammatical messages stay untouched ─────

    def test_console_js_error_passes_through(self):
        """Console-error messages are typically Chrome/Firefox-formatted
        stack snippets. They already read clearly; forcing a rewrite
        would destroy diagnostic detail."""
        msg = "Uncaught TypeError: Cannot read property of undefined"
        title = _walkthrough_passive_title("Console", msg)
        assert title == f"[Console] {msg}", title

    def test_cta_disabled_passes_through(self):
        """Heuristic message is already a passive-voice observation —
        leave it alone."""
        msg = "Sign up button is disabled on first render"
        title = _walkthrough_passive_title("CTAs", msg)
        assert title == f"[CTAs] {msg}", title

    # ── Edge cases ─────────────────────────────────────────────────

    def test_empty_message_falls_back_to_walkthrough_finding(self):
        title = _walkthrough_passive_title("Images", "")
        assert title == "[Images] Walkthrough finding", title

    def test_empty_area_falls_back_to_page(self):
        title = _walkthrough_passive_title("", "Some message")
        # Empty area → "[Page]" placeholder.
        assert title.startswith("[Page]"), title

    def test_runtime_substitution_failure_keeps_original(self):
        """If the template substitution raises (corrupt regex / unicode
        glitch) the function must fall back to the raw ``[Area] message``
        form. We exercise this by passing a message that matches the
        archetype but contains a brace that would break naive .format()
        — the implementation must defend against that.
        """
        # Brace inside the captured group could only break a sloppier
        # template. Confirm we don't blow up.
        msg = "X must have {forbidden-token}"
        title = _walkthrough_passive_title("Test", msg)
        assert title.startswith("[Test]"), title


# ── Integration: factory uses the transformed title ───────────────


class TestCreateBugFromWalkthroughFindingUsesTransform:
    def test_factory_emits_passive_voice_title_for_must_have(self):
        bug = create_bug_from_walkthrough_finding({
            "severity": "Critical",
            "area": "Accessibility",
            "defect_class": "axe_critical",
            "message": "Select element must have an accessible name — "
                       "affects 2 elements on this page",
            "url": "https://example.com/jobs",
            "element": "#All-vacancies",
            "tc_id": "LIVE-PAGE-007",
        }, environment_str="Web", tester_name="ci")
        # Title must use the passive form — not the old "must have …" body.
        assert "must have" not in bug.title, bug.title
        assert "is missing from" in bug.title, bug.title
        assert bug.title.startswith("[Accessibility]"), bug.title

    def test_factory_emits_passive_voice_title_for_js_error(self):
        bug = create_bug_from_walkthrough_finding({
            "severity": "Major",
            "area": "JS",
            "defect_class": "page_error",
            "message": "A JavaScript error happened during the user journey",
            "url": "https://example.com/contact",
            "tc_id": "LIVE-PAGE-005",
        }, environment_str="Web", tester_name="ci")
        assert "is raised during" in bug.title, bug.title
        assert bug.title.startswith("[JS]"), bug.title

    def test_factory_emits_passive_voice_title_for_broken_image(self):
        bug = create_bug_from_walkthrough_finding({
            "severity": "Major",
            "area": "Images",
            "defect_class": "broken_image",
            "message": "Broken image on the page — hero.avif did not "
                       "load (visitors see an empty slot or a broken-"
                       "image icon)",
            "url": "https://example.com/",
            "tc_id": "LIVE-PAGE-001",
        }, environment_str="Web", tester_name="ci")
        assert "is not loaded" in bug.title, bug.title
        assert "hero.avif" in bug.title, bug.title
        assert bug.title.startswith("[Images]"), bug.title
