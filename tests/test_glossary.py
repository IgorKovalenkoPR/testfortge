"""UI terminology glossary + reviewer wording rules.

Covers ``engine.glossary``: asset loading, the alias / avoid index, the
quote-safe rewriter, and each lint rule against the reviewer comment that
produced it.

Provenance of the rules under test — the threaded comments a team lead
left on ``Training Plan_Horban Yaroslavna.xlsx``:

  * "Try do not use ""correct/incorrect"", specify the action or the result"
  * "Remove dot at the end" / "extra space"
  * "typo: Verify"
  * "Footer/Header/Main/Homepage should be started from a capital letter"
  * "1. What do you scroll?"
  * "Let`s imagine if this URL will be changed … starting from main URL"

The last one is only half-enforceable in code; see
:func:`engine.glossary.starts_from_entry_point` for what was left to the
authoring agent and why.
"""
from __future__ import annotations

import pytest

from engine import glossary as g


# ── Assets ───────────────────────────────────────────────────────────

class TestAssets:
    def test_glossary_loads_with_the_workbook_terms(self):
        terms = g.terms()
        # 91 workbook rows collapse to 87 unique terms once the duplicate
        # headings are merged (Toggle button ×2, Checkbox / Check Box,
        # Accordion ×3, Preloader gif / Preloader).
        assert len(terms) >= 85
        names = {t["term"] for t in terms}
        for expected in ("Tooltip", "Breadcrumbs", "Accordion", "Modal",
                         "Hamburger button", "Entry field", "Checkbox",
                         "Radio button", "Carousel", "Placeholder text",
                         "Tab bar", "Crash log", "Favicon", "WYSIWYG"):
            assert expected in names, expected

    def test_every_term_declares_a_kind_and_a_definition(self):
        for t in g.terms():
            assert t.get("kind") in ("element", "property", "concept",
                                     "gesture", "artifact"), t["term"]
            assert (t.get("definition") or "").strip(), t["term"]

    def test_element_terms_carry_a_control_type_or_say_why_not(self):
        # control_type is what follows a quoted label ('the "Save" button').
        # Only elements that have no label may omit it.
        missing = [t["term"] for t in g.element_terms()
                   if not t.get("control_type")]
        assert missing == ["Scroll bar"], missing

    def test_wording_rules_load_with_the_reviewer_quotes(self):
        text = g.wording_rules_text()
        assert text
        for quote in ("Remove dot at the end", "What do you scroll?",
                      "typo: Verify", "correct/incorrect",
                      "starting from main URL",
                      "capital letter"):
            assert quote in text, quote

    def test_assets_are_cached(self):
        assert g.glossary_text() is g.glossary_text()
        assert g.wording_rules_text() is g.wording_rules_text()

    def test_banned_buckets_are_populated(self):
        assert "correctly" in g.banned_phrases("grading")
        assert "must" in g.banned_phrases("modal")
        # The operator's ruling: "should" is not banned.
        assert "should" not in g.banned_phrases("modal")
        assert "scroll down" in g.banned_phrases("vague_object")

    def test_approved_verbs_cover_the_house_vocabulary(self):
        verbs = g.approved_verbs()
        for v in ("Go to", "Click on", "Fill in", "Mark", "Press",
                  "Pay attention to", "Try to find", "Scroll"):
            assert v in verbs, v


# ── Index ────────────────────────────────────────────────────────────

class TestIndex:
    @pytest.mark.parametrize("alias,canonical", [
        ("dropdown", "Drop-down"),
        ("drop down", "Drop-down"),
        ("burger menu", "Hamburger button"),
        ("text box", "Entry field"),
        ("tickbox", "Checkbox"),
        ("lightbox", "Modal"),
        ("spoiler", "Accordion"),
        ("infotip", "Tooltip"),
        ("Tooltip", "Tooltip"),
    ])
    def test_canonical_term(self, alias, canonical):
        assert g.canonical_term(alias) == canonical

    def test_unknown_term_returns_empty(self):
        assert g.canonical_term("flurbing widgetron") == ""

    def test_control_type_resolves_through_an_alias(self):
        assert g.control_type_for("switcher") == "toggle"
        assert g.control_type_for("Hamburger button") == "button"


# ── Rewriting ────────────────────────────────────────────────────────

class TestQuoteSafety:
    def test_never_rewrites_inside_double_quotes(self):
        # A quoted string is an on-screen label. Rewriting one turns a
        # working locator into a broken one.
        src = 'Verify that the "dropdown" label is shown in the footer'
        out = g.normalise_text(src, kind="title")
        assert '"dropdown"' in out
        assert "Footer" in out

    def test_rewrites_outside_quotes_in_the_same_string(self):
        src = 'Expand the "Why Us" dropdown in the header'
        out = g.normalise_text(src)
        assert out == 'Expand the "Why Us" drop-down in the Header'


class TestSpellingVariants:
    @pytest.mark.parametrize("src,want", [
        ("the dropdown is expanded", "the drop-down is expanded"),
        ("the drop down is expanded", "the drop-down is expanded"),
        ("the scrollbar is visible", "the scroll bar is visible"),
        ("the check box is marked", "the checkbox is marked"),
        ("the tool tip is shown", "the tooltip is shown"),
    ])
    def test_variant_is_rewritten_to_the_canonical_term(self, src, want):
        assert g.canonicalise_spellings(src) == want

    def test_common_noun_mirrors_the_author_case(self):
        # "Drop-down" is title-cased in the YAML because the workbook uses
        # headings, but it is a common noun — mid-sentence it stays lower.
        assert g.canonicalise_spellings("a dropdown here") == \
            "a drop-down here"
        assert g.canonicalise_spellings("Dropdown is expanded") == \
            "Drop-down is expanded"

    def test_proper_noun_keeps_its_case(self):
        assert g.canonicalise_spellings("the iframe loads") == \
            "the IFrame loads"

    def test_semantic_rename_is_never_applied_silently(self):
        # "burger menu" means Hamburger button, but guessing that in place
        # would edit a name the author may have taken off the screen.
        src = "Tap the burger menu"
        assert g.normalise_text(src) == src
        assert any("Hamburger button" in i for i in g.lint_text(src))


class TestRegionCapitalisation:
    @pytest.mark.parametrize("src,want", [
        ("the footer is displayed", "the Footer is displayed"),
        ("in the header of the page", "in the Header of the page"),
        ("the homepage opens", "the Homepage opens"),
        ("the footer contacts are visible", "the Footer contacts are visible"),
        ("the footer logo", "the Footer logo"),
    ])
    def test_region_is_capitalised(self, src, want):
        assert g.capitalise_regions(src) == want

    @pytest.mark.parametrize("src", [
        "every column header it declares",
        "the header row is frozen",
        "the header checkbox marks all rows",
        "the HTTP request header is set",
        "the table footer shows totals",
    ])
    def test_non_region_use_is_left_alone(self, src):
        # These broke four grid tests when a blanket rule first shipped.
        # A grid's column header is not the page Header.
        assert g.capitalise_regions(src) == src
        assert not [i for i in g.lint_text(src) if "page region" in i]


class TestPunctuationAndSpacing:
    @pytest.mark.parametrize("src,want", [
        ("Verify that the Footer is displayed.",
         "Verify that the Footer is displayed"),
        ("Verify that the list is sorted", "Verify that the list is sorted"),
    ])
    def test_trailing_period_is_stripped_from_titles(self, src, want):
        assert g.normalise_text(src, kind="title") == want

    def test_abbreviations_keep_their_period(self):
        src = "Verify that the list shows tags, labels, etc."
        assert g.strip_trailing_period(src) == src

    def test_expected_result_keeps_its_punctuation(self):
        src = "The form is not submitted. An error message is displayed."
        assert g.normalise_text(src, kind="expected") == src

    def test_double_space_and_space_before_punctuation_are_fixed(self):
        assert g.tidy_spacing("the  Footer is  shown ,  always") == \
            "the Footer is shown, always"


# ── Linting ──────────────────────────────────────────────────────────

class TestLintTitles:
    def test_missing_verify_opener_is_flagged(self):
        assert any('does not open with "Verify"' in i
                   for i in g.lint_text("The Footer is displayed",
                                        kind="title"))

    @pytest.mark.parametrize("opener", ["Check", "Validate", "Ensure",
                                        "Make sure", "Test that"])
    def test_wrong_opener_names_itself(self, opener):
        issues = g.lint_text(f"{opener} the Footer is displayed",
                             kind="title")
        assert any("opens with" in i and "Verify" in i for i in issues)

    def test_opener_check_can_be_switched_off(self):
        # The corpus's error-message sweep uses "<Surface>: <action>".
        src = ("Employee Create page: Trying to proceed with all the "
               "required fields empty")
        assert g.lint_text(src, kind="title", check_opener=False) == []
        assert g.lint_text(src, kind="title") != []

    def test_trailing_period_is_flagged(self):
        assert any("trailing period" in i for i in
                   g.lint_text("Verify that the Footer is displayed.",
                               kind="title"))


class TestLintGrading:
    @pytest.mark.parametrize("word", ["correctly", "incorrectly", "properly",
                                      "as expected", "works fine"])
    def test_graded_outcome_is_flagged(self, word):
        issues = g.lint_text(f"The filter {word}", kind="expected")
        assert any("graded outcome" in i for i in issues), issues

    def test_observable_outcome_passes(self):
        assert g.lint_text(
            "Only projects related to the selected industry are displayed",
            kind="expected") == []


class TestLintModals:
    @pytest.mark.parametrize("src", ["The form must be submitted",
                                     "The row shall not persist",
                                     "The banner ought to be visible"])
    def test_requirement_voice_is_flagged(self, src):
        assert any("requirement" in i
                   for i in g.lint_text(src, kind="expected")), src

    @pytest.mark.parametrize("src", [
        "The entered data should be accepted",
        "Button should be hidden when there is no additional data",
    ])
    def test_should_is_accepted(self, src):
        # Operator ruling — the team's own reviewed deliverable writes it
        # throughout and the reviewing team lead let every instance stand.
        assert g.lint_text(src, kind="expected") == []


class TestLintSteps:
    def test_vague_scroll_is_flagged_with_the_reviewer_question(self):
        issues = g.lint_text("Scroll down", kind="step")
        assert any("What do you scroll?" in i for i in issues), issues

    def test_scroll_with_an_object_passes(self):
        assert g.lint_text("Scroll the page down to the Footer",
                           kind="step") == []

    def test_generic_step_is_flagged(self):
        assert any("generic step" in i for i in
                   g.lint_text("Navigate to the relevant page", kind="step"))

    @pytest.mark.parametrize("src", ["Verify the title matches",
                                     "Check the load time",
                                     "Ensure no errors appear"])
    def test_assertion_inside_a_step_is_flagged(self, src):
        assert any("assertion inside a step" in i
                   for i in g.lint_text(src, kind="step")), src

    def test_house_observation_verbs_pass(self):
        for src in ("Pay attention to the Console tab",
                    "Try to find a horizontal scroll bar"):
            assert g.lint_text(src, kind="step") == [], src

    def test_lint_steps_prefixes_the_index(self):
        issues = g.lint_steps(["Go to the site: https://x.test/",
                               "Scroll down"])
        assert issues and issues[0].startswith("step 2: ")


class TestEntryPoint:
    @pytest.mark.parametrize("step", [
        "Go to the site: https://testfort.com/",
        "Go to the site: https://qarea.com/projects",
        "Go to the site: https://example.com/services/mobile/android",
        "Go to HR module -> Job Positions grid",
    ])
    def test_page_navigation_is_accepted(self, step):
        # Path depth is deliberately not judged here — the reference corpus
        # opens deep pages directly, and whether a menu click is required
        # is a judgement about the product, left to the authoring agent.
        assert g.starts_from_entry_point([step]) is True

    @pytest.mark.parametrize("step", [
        "Go to https://qarea.com/careers#form",
        "Go to https://qarea.com/projects?industry=finance",
    ])
    def test_in_page_state_is_rejected(self, step):
        assert g.starts_from_entry_point([step]) is False

    def test_empty_steps_are_not_a_finding(self):
        assert g.starts_from_entry_point([]) is True
        assert g.starts_from_entry_point(["", None]) is True


# ── Content guard ────────────────────────────────────────────────────

class TestShippedTemplatesComply:
    """The no-API-key path must produce text a reviewer would accept.

    These templates ARE the free-tier output, so a finding here is a
    finding a client would see.
    """

    def test_every_shipped_test_case_template_is_clean(self):
        from engine.qa_knowledge_loader import LOADER
        offenders: list[str] = []
        for area in LOADER.areas():
            for tc in (LOADER.get_test_cases(area) or []):
                summary = getattr(tc, "summary", "") or ""
                issues = (
                    g.lint_text(summary, kind="title")
                    + g.lint_text(getattr(tc, "expected_result", "") or "",
                                  kind="expected")
                    + g.lint_steps(getattr(tc, "steps", None) or [])
                )
                if issues:
                    offenders.append(f"{area}: {summary[:60]} → {issues}")
        assert not offenders, offenders

    def test_every_shipped_checklist_template_is_clean(self):
        from engine.qa_knowledge_loader import LOADER
        offenders: list[str] = []
        for area in LOADER.areas():
            for _sec, checks in (LOADER.get_checklist(area) or {}).items():
                for ci in checks:
                    issues = g.lint_text(ci.objective, kind="objective")
                    if issues:
                        offenders.append(f"{area}: {ci.objective[:60]} "
                                         f"→ {issues}")
        # 34 objectives were phrased as test cases ("do X and verify Y")
        # when the linter landed; all were rewritten into the house
        # observation grammar. This must stay at zero.
        assert not offenders, offenders
