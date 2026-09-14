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

    def test_an_existing_list_is_re_read_not_re_split(self):
        """A numbered fact that contains two sentences stays one fact."""
        assert style.split_facts(
            "1. The record is saved. The grid refreshes\n2. A toast appears"
        ) == ["The record is saved. The grid refreshes", "A toast appears"]


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

    def test_the_steps_are_not_touched(self):
        """This pass owns three columns; the steps belong to tc_steps."""
        assert self._reviewed().test_steps.startswith(
            "1. Open https://testfort.com")


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
