"""The column conventions from the operator review of 2026-09-14.

Four rules, one module (``engine.tc_house_style``), applied to every
generated pack by ``qa_team_lead.review_test_cases``:

* a precondition never claims a URL "is reachable" — step 1 opens it;
* preconditions and expected results carrying more than one fact are
  numbered lists, one fact per line, as the steps column already is;
* Test Data is filled for credentials and nothing else;
* expected results are written with "should" / "should be".

The cases below are the real strings the generators shipped, not invented
ones: the screenshot that prompted the review is reproduced end to end in
:class:`TestTheReviewedCase`.
"""
from __future__ import annotations

import pytest

from engine import tc_house_style as style
from engine.qa_team_lead import review_test_cases
# Imported under another name so pytest does not try to collect a
# dataclass whose name happens to start with "Test".
from engine.testcase_generator import TestCase as _TestCaseRow


def _case(**overrides):
    row = dict(
        id="SC1_001", section="Forms", section_num=1,
        summary="Verify that the form rejects empty input",
        preconditions="https://example.com is reachable.",
        test_steps="1. Open https://example.com\n2. Submit the form",
        test_data="", expected_result="The form is not submitted.",
        category="Negative", priority="High",
    )
    row.update(overrides)
    return _TestCaseRow(**row)


# ── Rule 1: reachability is not prior state ──────────────────────────

class TestOpenNotReachable:

    @pytest.mark.parametrize("text, expected", [
        ("https://x.com is reachable in the browser.",
         "https://x.com is open in the browser."),
        ("{url} is reachable and the browser cache is cleared.",
         "{url} is open and the browser cache is cleared."),
        # The trailing shape, rewritten subject-first so the URL is the
        # first thing read.
        ("Site is reachable at https://x.com.", "https://x.com is open."),
        ("Application under test is reachable at https://x.com",
         "https://x.com is open"),
    ])
    def test_reachable_becomes_open(self, text, expected):
        assert style.open_not_reachable(text) == expected

    def test_the_host_keeps_its_dots(self):
        """The URL is one token, not a sentence to be cut at its first dot.

        An earlier pattern stopped the URL at the first ``.`` and produced
        "https://testfort is open.com." — a host that does not exist.
        """
        assert style.open_not_reachable("Site is reachable at https://a.b.c.") \
            == "https://a.b.c is open."

    def test_other_states_are_untouched(self):
        assert style.open_not_reachable("User is logged in.") == \
            "User is logged in."


class TestOneCanonicalPhrasing:
    """The three spellings the operator found in one pack.

    A deliverable carries one; so does a linter, because a synonym
    defeats every grep-based duplicate check across a 4,808-row plan.
    """

    @pytest.mark.parametrize("variant", [
        "testfort.com is open.",
        "https://testfort.com is open",
        "https://testfort.com is loaded.",
        "https://testfort.com is reachable in the browser.",
        "User can reach https://testfort.com from a fresh browser session.",
        "Site is reachable at https://testfort.com.",
        "The application at https://testfort.com is open.",
    ])
    def test_every_spelling_lands_on_the_one_form(self, variant):
        assert style.format_preconditions(variant) == \
            "https://testfort.com is open"

    def test_the_scheme_is_always_stated(self):
        """A bare host assumes a scheme, and staging and prod differ."""
        assert style.absolute_url("testfort.com") == "https://testfort.com"
        assert style.absolute_url("http://x.com") == "http://x.com"

    def test_a_second_fact_welded_on_with_and_becomes_its_own_line(self):
        assert style.format_preconditions(
            "https://x.com is open in the browser and the main navigation "
            "is visible.") == ("1. https://x.com is open\n"
                               "2. The main navigation is visible")

    def test_a_shared_subject_is_not_cut_loose(self):
        """"… is reachable and is not blocked" has one subject.

        Splitting there strands a predicate with nothing to attach to.
        """
        out = style.format_preconditions(
            "https://x.com is reachable and is not blocked from indexing.")
        assert "\n" not in out
        assert out.startswith("https://x.com is open and")

    def test_a_precondition_naming_the_same_page_twice_says_it_once(self):
        assert style.format_preconditions(
            "https://x.com is open. https://x.com is loaded.") == \
            "https://x.com is open"


class TestDedupeLabels:

    def test_a_label_the_page_carries_twice_is_listed_once(self):
        """The header and the mobile drawer both hold "Menu"."""
        assert style.dedupe_labels(
            ["Menu", "Menu", "Send", "Get a quote", "Send"]) == \
            ["Menu", "Send", "Get a quote"]

    def test_order_is_the_order_the_crawler_saw_them(self):
        assert style.dedupe_labels(["b", "a", "b"]) == ["b", "a"]

    def test_case_and_padding_do_not_make_a_new_label(self):
        assert style.dedupe_labels(["Send", " send ", "SEND"]) == ["Send"]


# ── Rule 2: one fact per line ────────────────────────────────────────

class TestSplitFacts:

    def test_an_abbreviation_is_not_a_sentence_end(self):
        text = ("Submit malformed values (e.g. an invalid email) and "
                "inspect the response.")
        assert style.split_facts(text) == [text.rstrip()]

    def test_a_quoted_message_keeps_its_own_full_stop(self):
        text = ('The "You must define an Applied Job." warning is '
                'displayed. Nothing is persisted.')
        assert style.split_facts(text) == [
            'The "You must define an Applied Job." warning is displayed',
            "Nothing is persisted.",
        ]

    def test_a_semicolon_splits_even_before_a_lowercase_word(self):
        assert style.split_facts(
            "https://x.com is open; the form 'Contact' is rendered.") == [
            "https://x.com is open",
            "the form 'Contact' is rendered.",
        ]

    def test_a_decimal_is_not_a_split_point(self):
        assert style.split_facts(
            "The page loads in under 2.5 seconds on a wired connection") == [
            "The page loads in under 2.5 seconds on a wired connection"]

    def test_an_existing_list_is_split_down_to_one_fact_a_line(self):
        """Numbering is a marker, not a fence.

        An earlier version returned an already-numbered field verbatim,
        which is how three assertions shipped merged into one numbered
        line after the pipe defect below had cut the first one in half.
        """
        assert style.split_facts(
            "1. The record is saved. The grid refreshes\n2. A toast appears"
        ) == ["The record is saved", "The grid refreshes", "A toast appears"]

    def test_a_pipe_inside_a_quoted_title_is_not_a_separator(self):
        """The defect the operator caught on SC7_001.

        A page title carries a pipe — `Software Testing Solutions |
        Manual, Auto, AI` — and the pipe-separator rule could not see that
        it sat inside quotes, so the assertion was cut mid-quote and
        shipped as `2. Manual, Auto, AI") and H1 (…`.
        """
        text = ('The page should load with its declared title ("Software '
                'Testing Solutions | Manual, Auto, AI") and H1 ("End-to-End '
                'Software Testing Solutions"). All observed sections should '
                'be visible. No JavaScript error should be emitted on first '
                'paint.')
        facts = style.split_facts(text)
        assert facts[0] == ('The page should load with its declared title '
                            '("Software Testing Solutions | Manual, Auto, '
                            'AI") and H1 ("End-to-End Software Testing '
                            'Solutions")')
        assert facts[1] == "All observed sections should be visible"
        assert facts[2].startswith("No JavaScript error should be emitted")
        assert len(facts) == 3

    def test_a_period_inside_brackets_is_not_a_separator(self):
        assert style.split_facts(
            "The page loads (see the spec. section 4) and renders"
        ) == ["The page loads (see the spec. section 4) and renders"]


class TestAsNumberedList:

    def test_one_fact_is_left_as_prose(self):
        assert style.as_numbered_list("User is logged in.") == \
            "User is logged in."

    def test_two_facts_become_a_numbered_list(self):
        assert style.as_numbered_list(
            "An account exists. The cache is cleared.") == \
            "1. An account exists\n2. The cache is cleared"

    def test_a_fact_opening_with_a_host_keeps_its_case(self):
        """Capitalising a host names a different host."""
        assert style.as_numbered_list(
            "https://x.com is open; an account exists.").startswith(
            "1. https://x.com is open")

    def test_a_pipe_separated_list_is_renumbered_onto_lines(self):
        assert style.as_numbered_list(
            "1. The record is not created | 2. A warning is displayed") == \
            "1. The record is not created\n2. A warning is displayed"


# ── Rule 3: Test Data is for credentials ─────────────────────────────

class TestCredentialsOnly:

    @pytest.mark.parametrize("text", [
        "username: valid, password: valid",
        "Login: tester@example.com / Password: Secret1!",
        "API key: sk-test-123",
        "One-time code from the authenticator app",
    ])
    def test_credentials_are_kept(self, text):
        assert style.credentials_only(text) == text

    @pytest.mark.parametrize("text", [
        # The string from the reviewed screenshot: field NAMES the steps
        # already carry.
        "Empty values; malformed values for: business-email, _wpcf7_ak_hp_textarea",
        "Fields observed: business-email, message",
        "Query: QA services",
        "Viewport: 375x812 (iPhone)",
        "-",
    ])
    def test_everything_else_is_dropped(self, text):
        assert style.credentials_only(text) == ""


# ── Rule 4: the expected result is written with "should" ─────────────

class TestShouldVoice:

    @pytest.mark.parametrize("text, expected", [
        ("The required fields are highlighted",
         "The required fields should be highlighted"),
        ("The record is saved", "The record should be saved"),
        ("The record is not created",
         "The record should not be created"),
    ])
    def test_the_copula_becomes_should_be(self, text, expected):
        assert style.should_voice(text) == expected

    def test_only_the_first_copula_moves(self):
        """The second one states a condition, not something to promise."""
        assert style.should_voice(
            "The banner is displayed when the user is signed out") == \
            "The banner should be displayed when the user is signed out"

    def test_a_fact_that_already_carries_a_modal_is_left_alone(self):
        assert style.should_voice(
            "User cannot create the record without a name") == \
            "User cannot create the record without a name"
        assert style.should_voice("The data should be accepted") == \
            "The data should be accepted"

    def test_the_copula_inside_a_quoted_message_is_skipped(self):
        """The product's own wording is not the house style's to edit.

        The first ``is`` here sits inside the quoted message, so the
        rewrite has to walk past it to the assertion's own copula —
        otherwise the warning becomes "Email should be invalid".
        """
        text = 'The "Email is invalid" warning is displayed'
        assert style.should_voice(text) == \
            'The "Email is invalid" warning should be displayed'


class TestFormatExpectedResult:

    def test_voice_and_layout_are_applied_together(self):
        assert style.format_expected_result(
            "The form is not submitted. A warning is displayed.") == \
            "1. The form should not be submitted\n2. A warning should be displayed"

    def test_a_single_assertion_stays_a_sentence(self):
        assert style.format_expected_result(
            "The required fields are highlighted.") == \
            "The required fields should be highlighted."

    def test_the_pass_is_idempotent(self):
        once = style.format_expected_result(
            "The form is not submitted. A warning is displayed.")
        assert style.format_expected_result(once) == once


class TestFormatPreconditions:

    def test_preconditions_keep_the_declarative_voice(self):
        """A precondition states what already holds, so "should" is wrong.

        This is the difference between the two columns, and the reason
        they have two functions rather than one with a flag.
        """
        assert style.format_preconditions("An account exists") == \
            "An account exists"

    def test_the_pass_is_idempotent(self):
        once = style.format_preconditions(
            "https://x.com is reachable; an account exists.")
        assert style.format_preconditions(once) == once


# ── The reviewed card, end to end ────────────────────────────────────

class TestTheReviewedCase:
    """SC2_004 as the operator saw it on staging, through the reviewer."""

    def _reviewed(self):
        tc = _case(
            id="SC2_004", section="Page: services", section_num=2,
            summary="Verify that the Product Focused QA & Software Testing "
                    "Services form on testfort.com rejects empty / "
                    "malformed input",
            preconditions="https://testfort.com is reachable; the form "
                          "'Product Focused QA & Software Testing Services "
                          "form' is rendered.",
            test_steps="1. Open https://testfort.com\n"
                       "2. Submit the form with all fields empty\n"
                       "3. Submit the form with malformed values (e.g. "
                       "invalid email, mismatched password) for: "
                       "business-email, _wpcf7_ak_hp_textarea\n"
                       "4. Inspect the rendered validation messages and "
                       "HTTP responses",
            test_data="Empty values; malformed values for: business-email, "
                      "_wpcf7_ak_hp_textarea",
            expected_result="Each invalid attempt is blocked client- or "
                            "server-side with a field-specific error "
                            "message. No partial write reaches the backing "
                            "store.",
        )
        return review_test_cases([tc])[0][0]

    def test_the_precondition_no_longer_restates_step_one(self):
        assert self._reviewed().preconditions == (
            "1. https://testfort.com is open\n"
            "2. The form 'Product Focused QA & Software Testing Services "
            "form' is rendered")

    def test_the_test_data_column_is_empty(self):
        assert self._reviewed().test_data == ""

    def test_the_expected_result_is_a_should_voiced_list(self):
        assert self._reviewed().expected_result.startswith(
            "1. Each invalid attempt should be blocked")
        assert "\n2. " in self._reviewed().expected_result

    def test_the_duplicate_entry_step_is_dropped(self):
        """Operator ruling 2026-09-14, second round.

        The preconditions say the page is open; step 1 said "Open
        https://testfort.com". That is the same fact in two columns, so
        the step goes and the steps begin at the first real action.
        """
        reviewed = self._reviewed()
        assert reviewed.test_steps.startswith(
            "1. Submit the form with all fields empty")
        assert "Open https://testfort.com" not in reviewed.test_steps

    def test_the_entry_point_survives_as_structured_data(self):
        """Nothing is lost: the runner reads url_pattern, not the prose.

        Without this the heuristic replay path would fall back to the
        run-wide base_url and send every case in the run to the same page.
        """
        assert self._reviewed().url_pattern == "https://testfort.com"


class TestTheEntryPointMovedButNothingBroke:
    """The 2026-09-14 second-round ruling, and its blast radius.

    Navigation left the steps, so every consumer that assumed "step 1 is
    the navigation" had to be re-pointed at structured data. These are the
    properties that say it actually was.
    """

    def _case(self):
        tc = _case(
            preconditions="https://testfort.com is loaded; the form "
                          "'Contact' is rendered on the page.",
            test_steps="1. Open https://testfort.com\n"
                       "2. Fill the fields with valid values\n"
                       "3. Submit the form\n"
                       "4. Observe the confirmation",
            expected_result="The form is submitted. A confirmation is "
                            "displayed.",
            category="Positive", tc_format="gherkin",
        )
        return review_test_cases([tc])[0][0]

    def test_the_replay_path_prefers_the_case_url_over_the_run_base_url(self):
        """base_url is one value for a whole run.

        Falling back to it would send every case in the run to the same
        page the moment step 1 stopped carrying a per-case URL.
        """
        from engine import automation_qa
        tc = self._case()
        script = automation_qa.tc_to_script(
            {"id": tc.id, "test_steps": tc.test_steps,
             "preconditions": tc.preconditions,
             "expected_result": tc.expected_result,
             "url_pattern": tc.url_pattern},
            base_url="https://wrong-run-wide.example")
        assert script.steps[0].action == "goto"
        assert script.steps[0].target == "https://testfort.com"

    def test_the_canonical_precondition_binds_instead_of_skipping(self):
        """The quiet failure mode, measured rather than assumed.

        An unbound Given makes the scenario skip, so coverage collapses
        while CI stays green. The canonical sentence must bind to a goto.
        """
        from engine import automation_codegen as cg
        cov = cg.coverage_report([self._case()]).to_dict()
        texts = [m["text"] for m in cov["manual_preconditions"]]
        assert not any("is open" in t for t in texts), texts

    def test_the_gherkin_given_is_not_first_personed(self):
        from engine import gherkin
        feature = gherkin.gherkin_for_test_case(self._case())
        assert "Given https://testfort.com is open" in feature
        assert "I https://" not in feature

    def test_the_entry_point_gate_still_judges_something(self):
        """It used to pass any case whose step 1 had no URL.

        Once no step ever carries a URL that is every case, so the gate
        would have gone vacuously true with no failing test to say so.
        """
        from engine import glossary
        steps = ["Fill the fields with valid values", "Submit the form"]
        assert glossary.starts_from_entry_point(steps, entry_url="") is True
        assert glossary.starts_from_entry_point(
            steps, entry_url="https://x.com/careers") is True
        assert glossary.starts_from_entry_point(
            steps, entry_url="https://x.com/careers#apply") is False
        assert glossary.starts_from_entry_point(
            steps, entry_url="/hr/job-positions*") is True

    def test_a_navigation_to_a_different_page_is_not_dropped(self):
        """Only the FIRST step, and only the page already declared open."""
        kept = style.drop_redundant_entry_step(
            "1. Open https://x.com/other\n2. Do a thing\n3. Do another",
            "https://x.com is open")
        assert kept.startswith("1. Open https://x.com/other")

    @pytest.mark.parametrize("step", [
        # Pure navigation to the declared entry point, however it is
        # dressed. Each qualifier is already a precondition of its own.
        "Open testfort.com in the browser",
        "Open https://testfort.com in a browser",
        "Open testfort.com at 1280x800",
        "Open testfort.com in browser DevTools responsive mode",
        "Navigate to https://testfort.com",
    ])
    def test_pure_navigation_to_the_declared_page_is_dropped(self, step):
        blob = f"1. {step}\n2. Do a thing\n3. Do another"
        assert style.drop_redundant_entry_step(
            blob, "https://testfort.com is open") == \
            "1. Do a thing\n2. Do another"

    @pytest.mark.parametrize("step", [
        # Each of these does something BESIDES opening the entry point.
        "Open testfort.com and one content/article page",
        "Open testfort.com/robots.txt - verify HTTP 200",
        "Open the known-bad path /this-does-not-exist-123 under testfort.com",
        "Visit the Homepage and at least 3 representative inner pages",
        "Open browser DevTools (Console and Network tabs)",
    ])
    def test_a_step_that_does_more_than_navigate_survives(self, step):
        blob = f"1. {step}\n2. Do a thing\n3. Do another"
        assert style.drop_redundant_entry_step(
            blob, "https://testfort.com is open") == blob

    def test_a_two_step_case_keeps_its_navigation(self):
        """Dropping it would leave one step, which is a checklist item."""
        kept = style.drop_redundant_entry_step(
            "1. Open https://x.com\n2. Look at the banner",
            "https://x.com is open")
        assert kept.startswith("1. Open https://x.com")


class TestTheExportsCarryTheLists:
    """A numbered field is one column value, not one line of a file.

    Markdown ends a bullet at the first newline and HTML collapses it, so
    both exporters had to learn about the lists this pass produces — and
    the Markdown importer had to learn to read a continuation line back,
    or a round-trip through .md would silently keep the first fact only.
    """

    def _pack(self):
        return [_case(
            preconditions="1. https://x.com is open\n2. An account exists",
            expected_result="1. The form should not be submitted\n"
                            "2. A warning should be displayed")]

    def test_markdown_indents_the_continuation_lines(self):
        from engine import exporter
        md = exporter.export_markdown("P", [], self._pack(), [], [], {})
        assert "- **Expected Result:** 1. The form should not be submitted" in md
        assert "\n  2. A warning should be displayed" in md

    def test_a_markdown_round_trip_keeps_every_fact(self, tmp_path):
        from engine import exporter, imports
        md = exporter.export_markdown("P", [], self._pack(), [], [], {})
        path = tmp_path / "pack.md"
        path.write_text(md, encoding="utf-8")
        # Picked by id, not by position: the section heading reads as a
        # case anchor to this parser, which is older than this change.
        back = {c.id: c for c in imports._read_md_test_cases(str(path))}
        assert back["SC1_001"].expected_result == (
            "1. The form should not be submitted\n"
            "2. A warning should be displayed")
        assert back["SC1_001"].preconditions == (
            "1. https://x.com is open\n2. An account exists")

    def test_html_turns_the_newlines_into_breaks(self):
        from engine import exporter
        html = exporter.export_html("P", [], self._pack(), [], [], {})
        assert ("The form should not be submitted<br>2. A warning should be "
                "displayed") in html


class TestTheReviewIsIdempotent:
    """A regenerate re-reviews rows that have already been through here."""

    def test_a_second_pass_changes_nothing(self):
        first = review_test_cases([_case(
            preconditions="https://x.com is reachable. An account exists.",
            test_data="Fields observed: email",
            expected_result="The form is not submitted. A warning is "
                            "displayed.",
        )])[0][0]
        before = (first.preconditions, first.test_data,
                  first.expected_result)
        second = review_test_cases([first])[0][0]
        assert (second.preconditions, second.test_data,
                second.expected_result) == before
