"""PR-C′ regression — passive-voice bug summary builder.

The simulator path (``engine/qa_testers.py::_make_bug_summary``) used
to negate TC summaries by inserting "not" via regex and falling back
to ``" — does not work as expected"`` when no pattern matched. The
ART project export (2026-05-25, 63 bugs from 59 TC) surfaced the
breakage:

  • "Privacy policy renders its primary content … — does not work
    as expected" (positive-voice headline; the suffix didn't make
    the title read as a defect)
  • "Every public page has not a unique, non-empty <title>"
    (broken grammar from naive ``has`` → ``has not``)
  • "The page meets basic accessibility standards" (no negation at
    all because ``meets`` wasn't in the verb-replacement list)

The QA style guide requires passive voice with an "after/while"
trigger clause, e.g.:

    "The Contact US form is not submitted after clicking the Submit
     button."

This module pins the archetype outputs that meet that contract for
every TC pattern observed in the ART export plus a handful of common
shapes the simulator routinely emits. Each test name names the
archetype it covers so a future regression points straight at the
broken row.
"""

from __future__ import annotations

import pytest

from engine.qa_testers import _make_bug_summary


class TestBugSummaryArchetypes:
    """One test per archetype defined in ``_TITLE_ARCHETYPES``.

    Assertions check the structural pieces (subject, verb form, trigger
    clause) rather than exact strings so cosmetic copy edits don't
    cascade-break the suite. Where an exact phrase is the contract
    (e.g. "after clicking the submit control"), we still pin it
    explicitly.
    """

    # ── Form / submission archetypes ───────────────────────────────

    def test_submit_archetype_yields_passive_with_after_click(self):
        bug = _make_bug_summary(
            "Verify that the Contact US form submits successfully "
            "when valid data is provided"
        )
        # User's prime style-guide example — must match exactly.
        assert bug == (
            "The Contact US form is not submitted after clicking "
            "the submit control"
        ), bug

    def test_rejects_input_archetype_inverts_subject_and_object(self):
        bug = _make_bug_summary(
            "Verify that the About us A place built for growth form "
            "on about us rejects empty / malformed input"
        )
        # Object ("empty / malformed input") fronted; form-subject
        # moved to the trigger clause.
        assert bug.startswith("Empty / malformed input is not rejected"), bug
        assert "after submitting" in bug, bug
        assert "About us" in bug, bug
        # No banned suffix.
        assert "does not work as expected" not in bug, bug

    # ── Page / content archetypes ──────────────────────────────────

    def test_renders_archetype_emits_after_page_load(self):
        bug = _make_bug_summary(
            "Verify that Privacy policy renders its primary content "
            "as observed by the crawler"
        )
        assert "is not rendered" in bug, bug
        assert "after page load" in bug, bug
        assert "Privacy policy" in bug, bug
        # Positive-voice + banned suffix combo must not return.
        assert "does not work as expected" not in bug, bug
        assert "renders its" not in bug, bug

    def test_returns_results_archetype_uses_plural_are_not(self):
        bug = _make_bug_summary(
            "Verify that the on-site search returns results relevant "
            "to a topical query"
        )
        # "Results" is plural — copy must read "are not" not "is not".
        assert "are not returned" in bug, bug
        assert "after submitting the query" in bug, bug
        assert "on-site search" in bug, bug

    def test_meets_standards_archetype_uses_plural_are_not(self):
        bug = _make_bug_summary(
            "Verify that the page meets basic accessibility standards "
            "(keyboard navigation, alt text)"
        )
        # "Standards" is plural — must read "are not met".
        assert "are not met" in bug, bug
        assert "by the page" in bug, bug
        assert bug.startswith("Basic accessibility standards"), bug

    def test_has_archetype_preserves_every_quantifier(self):
        bug = _make_bug_summary(
            "Verify that every public page has a unique, "
            "non-empty <title> under 60 characters"
        )
        # "every" cardinality must survive into the trigger clause.
        assert "is missing from" in bug, bug
        assert "every public page" in bug, bug
        # The object is the <title>, fronted in the headline.
        assert bug.startswith("Unique, non-empty <title>"), bug

    # ── Action / behaviour archetypes ──────────────────────────────

    def test_opens_archetype_yields_not_opened_after_trigger(self):
        bug = _make_bug_summary(
            "Verify that the menu opens when clicked"
        )
        # "Opens" splits from "loads" so menus/modals/drawers get the
        # right verb — "is not opened", never "is not loaded".
        assert "is not opened" in bug, bug
        assert "is not loaded" not in bug, bug
        assert "after the trigger action" in bug, bug

    def test_loads_archetype_yields_not_loaded_after_navigation(self):
        bug = _make_bug_summary(
            "Verify that the page loads quickly"
        )
        assert "is not loaded" in bug, bug
        assert "after page navigation" in bug, bug

    def test_redirects_archetype_fronts_destination(self):
        bug = _make_bug_summary(
            "Verify that the login redirects to dashboard after success"
        )
        assert bug.startswith("Redirect to"), bug
        assert "is not triggered" in bug, bug
        assert "by the login" in bug, bug

    def test_validates_archetype_emits_validation_not_enforced(self):
        bug = _make_bug_summary(
            "Verify that the search field validates required input"
        )
        assert "validation is not enforced" in bug, bug
        assert "search field" in bug, bug

    def test_displays_archetype_handles_plural_objects(self):
        bug = _make_bug_summary(
            "Verify that the navigation displays all primary links"
        )
        # Plural object → "are not" agreement.
        assert "are not displayed" in bug, bug
        assert "is not displayed" not in bug, bug
        assert "by the navigation" in bug, bug

    def test_displays_archetype_handles_singular_objects(self):
        bug = _make_bug_summary(
            "Verify that the header displays a logo"
        )
        # Singular object → "is not" agreement.
        assert "is not displayed" in bug, bug
        assert "are not displayed" not in bug, bug


class TestBugSummaryExistingNegationPath:
    """The legacy ``_negative_clause_bug_summary`` helper is still
    invoked when the TC itself already contains a negation word
    (``no``, ``never``, ``cannot``, ``without``). PR-C′ kept that
    branch — verify it still inverts correctly so we don't regress
    "loads without errors" into "does not load without errors"
    nonsense.
    """

    def test_without_inverts_to_with(self):
        bug = _make_bug_summary(
            "Verify that the page loads without console errors"
        )
        # "Without" → "with" inversion; subject preserved.
        assert "with console errors" in bug, bug
        assert "without" not in bug.lower(), bug

    def test_has_no_inverts_to_has(self):
        bug = _make_bug_summary(
            "Verify that the homepage has no broken images"
        )
        # "Has no" → "has".
        assert "has broken images" in bug.lower(), bug
        assert "has no" not in bug.lower(), bug


class TestBugSummaryFallbacks:
    """Edge cases — these used to fall through to the banned suffix.
    Now they must produce a passive-voice headline with no
    "— does not work as expected" tail.
    """

    def test_no_banned_suffix_for_unmatched_shapes(self):
        bug = _make_bug_summary(
            "Verify that the user journey completes within budget"
        )
        # "Completes" is not in any archetype; "X is Y" / "X are Y"
        # final fallbacks may not match either. The safe fallback
        # ("The expected outcome is not observed for: …") must kick
        # in — banned suffix forbidden.
        assert "does not work as expected" not in bug, bug
        assert bug != "", bug

    def test_empty_summary_returns_safe_string(self):
        bug = _make_bug_summary("")
        assert bug == "Expected behaviour is not observed", bug

    def test_only_verify_prefix_returns_safe_string(self):
        bug = _make_bug_summary("Verify that ")
        assert bug == "Expected behaviour is not observed", bug

    def test_capitalisation_preserves_acronyms_inside_phrase(self):
        # ``_cap`` must not lowercase the tail — would mangle "CTA",
        # "API", "URL" if it used ``str.capitalize()``.
        bug = _make_bug_summary("Verify that the CTA button works")
        assert "CTA" in bug, bug
