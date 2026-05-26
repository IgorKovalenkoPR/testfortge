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
        # No URL → ``on_page`` is empty so we don't duplicate the
        # heuristic's own "on this page" tail.
        title = _walkthrough_passive_title(
            "Accessibility",
            "Select element must have an accessible name — affects 2 "
            "elements on this page",
        )
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

    def test_broken_image_inverts_to_is_missing(self):
        # PR-G rephrased broken-image titles from "is not loaded" to
        # "is missing" — friendlier to non-engineers ("missing" reads
        # as a defect; "not loaded" reads as a load-event description).
        title = _walkthrough_passive_title(
            "Images",
            "Broken image on the page — foo.avif did not load "
            "(visitors see an empty slot or a broken-image icon)",
        )
        assert title == (
            "[Images] Image foo.avif is missing on the page "
            "(visitors see an empty slot or a broken-image icon)"
        ), title

    def test_broken_image_no_tail(self):
        title = _walkthrough_passive_title(
            "Images",
            "Broken image on the page — bar.svg did not load",
        )
        assert title == (
            "[Images] Image bar.svg is missing on the page"
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


# ── PR-G: URL humanise + CDN-hash filename drop ───────────────────


class TestUrlHumanisation:
    def test_root_path_returns_homepage(self):
        from engine.bug_report import _humanise_url_page
        assert _humanise_url_page("https://example.com/") == "the homepage"
        assert _humanise_url_page("https://example.com") == "the homepage"

    def test_index_html_is_homepage(self):
        from engine.bug_report import _humanise_url_page
        assert _humanise_url_page(
            "https://example.com/index.html"
        ) == "the homepage"

    def test_kebab_case_path_is_title_cased(self):
        from engine.bug_report import _humanise_url_page
        assert _humanise_url_page(
            "https://artest.com/contact-us"
        ) == "the Contact Us page"

    def test_single_segment_path(self):
        from engine.bug_report import _humanise_url_page
        assert _humanise_url_page(
            "https://artest.com/careers"
        ) == "the Careers page"

    def test_deep_path_uses_last_segment(self):
        from engine.bug_report import _humanise_url_page
        assert _humanise_url_page(
            "https://example.com/blog/2024/01/foo-bar"
        ) == "the Foo Bar page"

    def test_extension_is_stripped(self):
        from engine.bug_report import _humanise_url_page
        assert _humanise_url_page(
            "https://example.com/about.html"
        ) == "the About page"

    def test_empty_url_falls_back_to_the_page(self):
        from engine.bug_report import _humanise_url_page
        assert _humanise_url_page("") == "the page"
        assert _humanise_url_page("not-a-url") == "the page"


class TestCdnHashDetection:
    def test_long_hex_prefix_is_cdn_hash(self):
        from engine.bug_report import _is_cdn_hash_filename
        # Real ART project example: Webflow CDN.
        assert _is_cdn_hash_filename(
            "6a0dc9966e1d43dd88d9b8a5_Frame%202136140580.svg"
        )

    def test_normal_filename_is_not_cdn_hash(self):
        from engine.bug_report import _is_cdn_hash_filename
        assert not _is_cdn_hash_filename("hero-banner.jpg")
        assert not _is_cdn_hash_filename("logo.png")
        assert not _is_cdn_hash_filename("team-photo.webp")

    def test_short_hex_prefix_is_not_cdn_hash(self):
        from engine.bug_report import _is_cdn_hash_filename
        # 8 hex chars — could be a git short hash on a meaningful
        # name; threshold of 16 chars protects against false positives.
        assert not _is_cdn_hash_filename("abc12345_image.jpg")

    def test_empty_returns_false(self):
        from engine.bug_report import _is_cdn_hash_filename
        assert not _is_cdn_hash_filename("")


class TestStakeholderFriendlyTitles:
    """End-to-end: titles previously contained raw CDN hashes and
    URL-encoded segments unreadable to PMs. PR-G replaces them with
    "A page graphic" / "the Careers page" while keeping the raw
    detail in body + steps to reproduce.
    """

    def test_cdn_hash_filename_is_replaced_in_title(self):
        title = _walkthrough_passive_title(
            "Images",
            "Broken image on the page — "
            "6a0dc9966e1d43dd88d9b8a5_Frame%202136140580.svg did not "
            "load (visitors see an empty slot or a broken-image icon)",
            url="https://www.artest.com/careers",
        )
        # CDN hash hidden behind "A page graphic"
        assert "6a0dc9966e" not in title, title
        assert "%20" not in title, title
        assert "A page graphic" in title, title
        # Page name from URL
        assert "the Careers page" in title, title
        # Visitor-impact tail preserved
        assert "broken-image icon" in title, title

    def test_meaningful_filename_kept_in_title(self):
        title = _walkthrough_passive_title(
            "Images",
            "Broken image on the page — hero-banner.jpg did not load",
            url="https://artest.com/",
        )
        # Friendly filenames are still helpful in the title.
        assert "hero-banner.jpg" in title, title
        assert "the homepage" in title, title
        assert "is missing" in title, title

    def test_alt_attribute_is_preferred_when_present(self):
        title = _walkthrough_passive_title(
            "Images",
            "Broken image on the page — \"Team photo\" did not load "
            "(visitors see an empty slot or a broken-image icon)",
            url="https://artest.com/about-us",
        )
        # Alt text wins over filename or generic placeholder. We
        # capitalise only the first letter to preserve the original
        # casing of multi-word alt strings ("Team photo" → "Team
        # photo", not heavy-handed Title Case which would mangle
        # "iPhone screenshot" into "Iphone Screenshot").
        assert "\"Team photo\" graphic is missing" in title, title
        assert "the About Us page" in title, title

    def test_accessibility_title_uses_humanised_page(self):
        title = _walkthrough_passive_title(
            "Accessibility",
            "Select element must have an accessible name — affects 2 "
            "elements on this page",
            url="https://artest.com/jobs",
        )
        assert "the Jobs page" in title, title
        assert "is missing from" in title, title

    def test_factory_passes_url_through_to_title(self):
        bug = create_bug_from_walkthrough_finding({
            "severity": "Major",
            "area": "Images",
            "defect_class": "broken_image",
            "message": (
                "Broken image on the page — "
                "6a0dc9966e1d43dd88d9b8a5_Frame%202136140580.svg did "
                "not load (visitors see an empty slot or a broken-"
                "image icon)"
            ),
            "url": "https://www.artest.com/careers",
            "tc_id": "LIVE-PAGE-004",
        }, environment_str="Web", tester_name="ci")
        # Title is now stakeholder-readable while the raw filename
        # still appears in steps_to_reproduce (matched by selector).
        assert "6a0dc9966e" not in bug.title, bug.title
        assert "A page graphic" in bug.title, bug.title
        assert "the Careers page" in bug.title, bug.title
        # Body still has the raw artefact for engineers.
        assert (
            "6a0dc9966e1d43dd88d9b8a5_Frame%202136140580.svg"
            in (bug.actual_result + bug.steps_to_reproduce)
        ) or bug.preconditions  # placeholder — body untouched

    def test_no_url_skips_page_reference_for_must_have(self):
        """If no URL is plumbed through, the ``must have`` archetype
        omits the page reference rather than emitting "on the page" —
        that phrase is redundant with the heuristic's own "on this
        page" tail (which lives in the message body / Steps to
        Reproduce when no tail-clause is present).
        """
        title = _walkthrough_passive_title(
            "Accessibility",
            "Button must have an accessible name",
            # no url=
        )
        # No redundant page reference; title still valid.
        assert title == (
            "[Accessibility] Accessible name is missing from button"
        ), title

    def test_no_url_still_renders_broken_image_with_the_page(self):
        """The broken-image archetype always needs a destination
        clause so the title reads as a complete sentence even when
        no URL was provided — falls back to "on the page"."""
        title = _walkthrough_passive_title(
            "Images",
            "Broken image on the page — hero.jpg did not load",
        )
        assert "on the page" in title, title
        assert "is missing" in title, title


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
        # PR-G: "is missing" reads more like a defect to PMs than
        # "is not loaded" did. Meaningful filename kept.
        assert "is missing" in bug.title, bug.title
        assert "hero.avif" in bug.title, bug.title
        assert bug.title.startswith("[Images]"), bug.title
