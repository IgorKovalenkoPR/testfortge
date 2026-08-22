"""The traceability matrix has to name the test cases it traces to.

E11: the exported Markdown's section 4 listed every requirement with the
Test Cases and Categories columns empty —

    | REQ-034 | US-034 | High |  |  |

``generate_traceability`` joins on ``TestCase.user_story_id``
(``testcase_generator.py:1120``), and the ``TestCase(...)`` built by
``generate_test_cases`` never set it. ``_make_tc``, the *other* constructor
in the same module, always has — so the field looked populated wherever
anyone checked, and the matrix was empty wherever anyone read it.

Asserted on the join rather than on the exporter's text, because the
exporter was rendering exactly what it was given. A test that only checked
"section 4 exists" passed throughout.
"""
from __future__ import annotations

import pytest

from engine import file_parser as _parser
from engine import testcase_generator as _tcgen
from engine import user_story_generator as _usgen


REQUIREMENT_LINES = [
    "User can log in with email and password.",
    "User can reset password via email link.",
    "User can export orders to CSV.",
]


@pytest.fixture(scope="module")
def generated():
    reqs = _parser.split_into_requirements(REQUIREMENT_LINES)
    stories = _usgen.generate_user_stories(reqs)
    cases = _tcgen.generate_test_cases(stories)
    return stories, cases


def test_the_requirements_parsed(generated):
    """Guard the fixture — zero stories would make every assertion vacuous."""
    stories, cases = generated
    assert len(stories) == len(REQUIREMENT_LINES)
    assert cases


def test_story_derived_cases_carry_their_story(generated):
    stories, cases = generated
    story_ids = {s.id for s in stories}
    linked = [c for c in cases if getattr(c, "user_story_id", "")]
    assert linked, (
        "no generated case carries a user_story_id — the traceability "
        "matrix cannot be built from these")
    # Whatever is stamped has to be a story that exists, or the join
    # silently drops it again.
    assert {c.user_story_id for c in linked} <= story_ids


def test_every_requirement_row_names_its_test_cases(generated):
    stories, cases = generated
    matrix = _tcgen.generate_traceability(stories, cases)

    assert len(matrix) == len(stories)
    empty = [r["user_story_id"] for r in matrix if not r["test_case_ids"]]
    assert not empty, f"rows with an empty Test Cases column: {empty}"

    for row in matrix:
        assert row["test_count"] == len(row["test_case_ids"])
        # The Categories column came from the same join, so it was empty
        # for the same reason.
        assert row["categories"], row["user_story_id"]


def test_area_cases_without_a_story_are_not_invented(generated):
    """The KB-derived area packs cover an area, not one story.

    Those legitimately have no ``user_story_id``, and the fix must not
    fabricate one — a matrix row claiming coverage that no story asked for
    is worse than a blank cell.
    """
    stories, cases = generated
    unlinked = [c for c in cases if not getattr(c, "user_story_id", "")]
    # Not asserting a count — how many area cases get emitted depends on
    # the detected areas. Only that being unlinked stays possible.
    for case in unlinked:
        assert case.user_story_id == ""
