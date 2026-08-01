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
        assert out == "[Manual run] Unauthorized users can access protected resources"
        assert "does not user" not in out


class TestObjectivesThatAlreadyAssertANegative:
    """Their failure is the positive — negating to "not" would be wrong."""

    def test_cannot_becomes_can(self):
        assert defect_title_from_objective(
            "Verify that unauthorized users cannot delete another user's data"
        ) == ("[Manual run] Unauthorized users can delete another user's data")

    def test_does_not_becomes_does(self):
        assert defect_title_from_objective(
            "Verify that the browser Back button does not restore the "
            "authenticated session"
        ) == ("[Manual run] The browser Back button does restore the "
              "authenticated session")


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
