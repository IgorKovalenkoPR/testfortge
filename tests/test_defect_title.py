"""A bug headline must say what broke, not what to check.

Why this file exists: the manual walk filed bugs titled with the test
case's own objective, so the Bug Reports list on prod read
``[Authentication] Verify that login is completed successfully with
valid credentials`` — an instruction, not a defect. Every objective in
this codebase opens "Verify that …" because the team's wording rules
require it, so copying one across can never produce a defect statement.

Every input below is a real objective read off the deployed pack or the
shipped ``qa_knowledge`` templates. Three defects in the transform were
found precisely by running it over them rather than over invented
examples:

* "unauthorized users cannot access protected resources" parsed
  "users" as a third-person verb → "Unauthorized does not user cannot
  access …";
* "the user can sign in" lost the space → "Usercannot sign in";
* the negation fired **inside a quoted criterion**, flipping the
  citation and leaving the real verb alone, so the bug title claimed
  the system enforced the rule. That one is the dangerous shape: a
  defect report asserting the product works.
"""
from __future__ import annotations

import pytest

from engine.bug_report import BUG_STATUSES, defect_title_from_objective


class TestTheCopularShape:
    """The dominant corpus shape: "<subject> is/are <participle> …"."""

    @pytest.mark.parametrize("objective,expected", [
        ("Verify that login is completed successfully with valid credentials",
         "Login is not completed successfully with valid credentials"),
        ("Verify that SQL injection is blocked in the login form",
         "SQL injection is not blocked in the login form"),
        ("Verify that the user is logged out successfully",
         "The user is not logged out successfully"),
        ("Verify that validation is triggered for invalid email format",
         "Validation is not triggered for invalid email format"),
        ("Verify that the login form is displayed with email/username and "
         "password fields",
         "The login form is not displayed with email/username and password "
         "fields"),
    ])
    def test_the_clause_is_negated(self, objective, expected):
        assert defect_title_from_objective(objective) == f"[Manual run] {expected}"


class TestTheActiveShape:
    @pytest.mark.parametrize("objective,expected", [
        ("Verify that the feature works as described in the requirement",
         "The feature does not work as described in the requirement"),
        ("Verify that log in returns the expected outcome per the spec with "
         "boundary values",
         "Log in does not return the expected outcome per the spec with "
         "boundary values"),
    ])
    def test_third_person_becomes_does_not(self, objective, expected):
        assert defect_title_from_objective(objective).endswith(expected)

    def test_a_plural_noun_is_not_mistaken_for_a_verb(self):
        """"unauthorized users cannot …" — "users" is not a verb.

        The tail after a finite verb can never start with another
        finite verb, which is how this is caught.
        """
        out = defect_title_from_objective(
            "Verify that unauthorized users cannot access protected resources")
        assert out == "[Manual run] Unauthorized users access protected resources"
        assert "does not user" not in out


class TestObjectivesThatAlreadyAssertANegative:
    """Their failure is the positive — negating to "not" would be wrong."""

    def test_cannot_drops_the_modal_rather_than_flipping_to_can(self):
        """"can" is a modal, and a summary carries none.

        Operator ruling 2026-08-01. Dropping "cannot" states the failure
        in plain active voice and says the same thing.
        """
        assert defect_title_from_objective(
            "Verify that unauthorized users cannot delete another user's data"
        ) == ("[Manual run] Unauthorized users delete another user's data")

    def test_a_plain_can_falls_through_rather_than_guessing(self):
        """Negating "can" needs "does not" vs "do not" — a guess at the
        subject's number. The labelled fallback is preferred to a wrong
        one; the generators no longer emit "can" in a summary anyway.
        """
        out = defect_title_from_objective(
            "Verify that user can sign in with valid input")
        assert out == "[Manual run] Failed: user can sign in with valid input"

    def test_does_not_becomes_does(self):
        assert defect_title_from_objective(
            "Verify that the browser Back button does not restore the "
            "authenticated session"
        ) == ("[Manual run] The browser Back button does restore the "
              "authenticated session")

    # Found building the manual-run bug modal: the copular rule fired on
    # an objective that was already negative and added a second "not".
    @pytest.mark.parametrize("objective,expected", [
        ("Verify that Product-Focused QA & Software Testing Services is not "
         "submitted with the required controls left empty",
         "Product-Focused QA & Software Testing Services is submitted with "
         "the required controls left empty"),
        ("Verify that error messages are not displayed for valid input",
         "Error messages are displayed for valid input"),
        ("Verify that the Save button isn't enabled before any edit",
         "The Save button is enabled before any edit"),
        ("Verify that the fields aren’t cleared on reload",
         "The fields are cleared on reload"),
        ("Verify that guests can't post comments",
         "Guests post comments"),
        ("Verify that the users do not see the admin panel",
         "The users do see the admin panel"),
        ("Verify that the page doesn't reload on submit",
         "The page does reload on submit"),
        ("Verify that the order has not shipped",
         "The order has shipped"),
        ("Verify that the job won't retry after a fatal error",
         "The job will retry after a fatal error"),
        ("Verify that the password is never logged",
         "The password is logged"),
        ("Verify that the app never crashes on rotation",
         "The app crashes on rotation"),
    ])
    def test_a_negative_objective_becomes_the_positive(self, objective,
                                                       expected):
        assert defect_title_from_objective(objective) == f"[Manual run] {expected}"

    @pytest.mark.parametrize("objective", [
        # A leading "no X" has no safe positive: "an error" vs "errors"
        # is a guess at number, and "no error is not shown" is wrong.
        "Verify that no error is shown after saving",
        "Verify that nothing is saved when the form is cancelled",
        # A modal's positive is not the defect ("must be stored").
        "Verify that the password must not be stored in plain text",
    ])
    def test_a_negative_without_a_safe_positive_falls_through(self,
                                                              objective):
        out = defect_title_from_objective(objective)
        assert out.startswith("[Manual run] Failed: ")
        assert " not not " not in out

    @pytest.mark.parametrize("verdict", ["Failed", "Blocked"])
    @pytest.mark.parametrize("negator", [
        "is not", "isn't", "are not", "aren't", "was not", "were not",
        "does not", "doesn't", "do not", "don't", "cannot", "can't",
        "is never", "has not", "won't",
    ])
    def test_no_title_ever_carries_a_double_negative(self, negator, verdict):
        out = defect_title_from_objective(
            f"Verify that the form {negator} submitted with empty fields",
            verdict=verdict).lower()
        assert "not not" not in out
        assert "not never" not in out
        assert "n't not" not in out

    def test_an_apostrophe_does_not_open_a_quoted_span(self):
        """"isn't" / "user's" must not be masked as a citation."""
        out = defect_title_from_objective(
            "Verify that the user's 'Delete' button isn't shown to guests")
        assert out == ("[Manual run] The user's 'Delete' button is shown "
                       "to guests")

    def test_a_negator_inside_a_quote_is_still_a_citation(self):
        out = defect_title_from_objective(
            "Verify that the 'Not now' link is displayed")
        assert out == "[Manual run] The 'Not now' link is not displayed"


class TestQuotedTextIsNeverNegated:
    """A quote is a citation, not a claim the headline is making."""

    def test_the_outer_verb_is_negated_and_the_citation_survives(self):
        out = defect_title_from_objective(
            "Verify that the system enforces 'Unauthorized users cannot "
            "access protected resources' and blocks attempts to bypass "
            "this restriction")
        # The verb outside the quote is what gets negated …
        assert out.startswith("[Manual run] The system does not enforce")
        # … and the criterion is reproduced exactly as written.
        assert ("'Unauthorized users cannot access protected resources'"
                in out)

    def test_a_control_label_is_untouched(self):
        out = defect_title_from_objective(
            "Verify that the 'Sign In' button is disabled until both fields "
            "are filled")
        assert "'Sign In'" in out
        assert "is not disabled" in out


class TestVerdictsOtherThanFailed:
    def test_passed_but_is_not_negated(self):
        """The behaviour worked; something else about it was wrong.

        Negating here would report the opposite of what the tester saw.
        """
        out = defect_title_from_objective(
            "Verify that login is completed successfully with valid "
            "credentials", verdict="Passed but")
        assert "not" not in out.replace("[Manual run] ", "").split()
        assert out.startswith("[Manual run] Deviation while checking that")

    def test_passed_but_keeps_a_negative_objective_as_written(self):
        out = defect_title_from_objective(
            "Verify that the form is not submitted with empty fields",
            verdict="Passed but")
        assert out == ("[Manual run] Deviation while checking that the form "
                       "is not submitted with empty fields")


class TestTheFallback:
    def test_an_unmatched_shape_is_labelled_not_invented(self):
        """Better an unpolished title than a confident wrong one."""
        out = defect_title_from_objective(
            "Verify that 100% coverage across all modules", verdict="Failed")
        assert out == "[Manual run] Failed: 100% coverage across all modules"

    def test_an_empty_objective_still_produces_a_headline(self):
        assert defect_title_from_objective("") == "[Manual run] Manual check failed"


class TestFraming:
    def test_the_section_becomes_the_prefix(self):
        assert defect_title_from_objective(
            "Verify that the page loads", section="Authentication"
        ).startswith("[Authentication] ")

    def test_no_title_still_reads_as_a_check_instruction(self):
        """The whole point: "Verify" must not survive into a bug title."""
        for objective in [
            "Verify that login is completed successfully",
            "Verify that the feature works as described in the requirement",
            "Verify that unauthorized users cannot access protected resources",
        ]:
            assert "verify" not in defect_title_from_objective(objective).lower()

    def test_the_title_is_capped(self):
        out = defect_title_from_objective("Verify that " + "x" * 900)
        assert len(out) <= 500


def test_open_is_a_real_status():
    """Guards the constant the two bug filers were fixed to write."""
    assert "Open" in BUG_STATUSES
    assert "New" not in BUG_STATUSES
