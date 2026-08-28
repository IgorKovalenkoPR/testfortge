"""The knowledge packs name the site under test, and speak one voice.

Two operator requirements from 2026-08-28, both reported from the product
rather than derived from the code: a generated case said "Open the
application URL in the browser" for a project whose URL was known all
along, and expected results were to read "should be".

**Why the URL was missing is the interesting half.** The packs must fit
any site — that is what makes them a usable baseline with no API key and
no crawl, which is the state of this deployment. So they carry
placeholders instead of a URL. The substitution existed before this
change but was wired into exactly one call site and used by exactly one
of nine packs, so the other eight could not have used a placeholder even
if they wrote one: it would have rendered as literal braces on screen.
:func:`engine.qa_persona._contextualise` moves it to the funnel every
template passes through, and these tests hold it there.

**The voice half has a trap in it**, and it is the reason
``normalise_summary`` exists next to ``normalise_expected_result``. The
ruling scopes "should" to the expected result; a summary carries no modal
at all. One function serving both fields silently put "should" into
summaries, where it is a lint finding — caught here by
``TestTheTwoFieldsDivergeOnPurpose`` rather than in review.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

from engine import qa_persona
from engine import tc_author


PACKS = sorted((pathlib.Path(__file__).resolve().parent.parent
                / "engine" / "qa_knowledge" / "testcases").glob("*.yaml"))


def _cases(path: pathlib.Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(
        "cases", [])


def _analysis(url: str = "https://qarea.com/", domain: str = "qarea.com"):
    return qa_persona.AnalysisResult(areas=[], url=url, url_domain=domain)


# ── The packs themselves ─────────────────────────────────────────────

class TestEveryPackNamesTheSite:
    @pytest.mark.parametrize("path", PACKS, ids=lambda p: p.name)
    def test_every_case_carries_a_placeholder(self, path):
        """Not "most cases" — a case without one is the reported bug.

        The fields checked are the ones a tester reads to know where to
        go: an expected result may legitimately never name the site.
        """
        for case in _cases(path):
            blob = " ".join([
                case.get("preconditions", ""),
                " ".join(case.get("steps", [])),
                case.get("test_data", ""),
            ])
            assert "{url}" in blob or "{host}" in blob, (
                f"{path.name}: {case['summary'][:60]!r} never names the site, "
                f"so a tester cannot tell which one it is about")

    @pytest.mark.parametrize("path", PACKS, ids=lambda p: p.name)
    def test_no_case_still_says_application_url(self, path):
        # The literal phrase from the bug report. A placeholder that was
        # added alongside the old prose would pass the test above.
        for case in _cases(path):
            steps = " ".join(case.get("steps", [])).lower()
            assert "the application url" not in steps, case["summary"][:60]


# ── Substitution ─────────────────────────────────────────────────────

class TestTheFunnelFillsThem:
    def test_a_generated_case_names_the_real_site(self):
        cases = qa_persona.generate_professional_test_cases(
            _analysis(), stories=None)
        assert cases, "no cases generated — the rest asserts nothing"
        blob = " ".join(
            c.preconditions + " " + " ".join(c.steps) + " " + c.test_data
            for c in cases)
        assert "https://qarea.com/" in blob

    def test_no_literal_placeholder_survives_to_a_case(self):
        """The failure mode this whole mechanism can produce.

        A pack writing ``{url}`` that nothing substitutes renders braces
        to the tester — worse than the generic sentence it replaced.
        """
        cases = qa_persona.generate_professional_test_cases(
            _analysis(), stories=None)
        for case in cases:
            rendered = " ".join([
                case.summary, case.preconditions, " ".join(case.steps),
                case.test_data, case.expected_result,
            ])
            assert "{url}" not in rendered, case.summary[:60]
            assert "{host}" not in rendered, case.summary[:60]

    def test_a_run_with_no_url_reads_as_prose_not_as_a_hole(self):
        # Prompt-only and attachment-only runs have no URL at all.
        # "Site is reachable at ." would be worse than what it replaced.
        cases = qa_persona.generate_professional_test_cases(
            _analysis(url="", domain=""), stories=None)
        for case in cases:
            rendered = case.preconditions + " " + " ".join(case.steps)
            assert "{url}" not in rendered
            assert " at ." not in rendered
            assert "  " not in rendered.strip(), case.summary[:60]

    def test_a_brace_in_the_text_does_not_take_generation_down(self):
        # str.replace, not str.format: expected results legitimately hold
        # braces (a JSON body, a CSS rule), and format would raise KeyError
        # on them and kill the whole run.
        tpl = qa_persona.TCTemplate(
            summary="Verify that the API echoes the payload",
            preconditions="{url} is reachable.",
            steps=['Send {"name": "x"} to the endpoint'],
            test_data='{"name": "x"}',
            expected_result='The response body should be {"name": "x"}.',
            category="Positive",
        )
        out = qa_persona._contextualise(
            [tpl], qa_persona._site_context(_analysis()))
        assert out[0].preconditions.startswith("https://qarea.com/")
        assert out[0].test_data == '{"name": "x"}'


# ── Voice ────────────────────────────────────────────────────────────

class TestEveryPackSpeaksTheHouseVoice:
    @pytest.mark.parametrize("path", PACKS, ids=lambda p: p.name)
    def test_expected_results_use_should(self, path):
        for case in _cases(path):
            assert "should" in case["expected_result"].lower(), (
                f"{path.name}: {case['summary'][:60]!r}")

    @pytest.mark.parametrize("path", PACKS, ids=lambda p: p.name)
    def test_no_summary_carries_a_modal(self, path):
        # The other half of the 2026-08-01 ruling. Enforced separately
        # from the glossary lint so a pack cannot regress it quietly.
        for case in _cases(path):
            low = case["summary"].lower()
            for modal in ("should", "must", "shall", "can ", "may ",
                          "will "):
                assert modal not in low, (
                    f"{path.name}: {case['summary'][:60]!r} carries "
                    f"{modal.strip()!r}")


class TestTheTwoFieldsDivergeOnPurpose:
    """The bug this file's docstring describes, pinned.

    "The record must be saved" has two correct outputs, and which one is
    right depends only on the field it came from. A single normaliser
    serving both put "should" into summaries.
    """

    def test_the_same_input_lands_differently_per_field(self):
        src = "The record must be saved"
        assert tc_author.normalise_expected_result(src) == \
            "The record should be saved"
        assert tc_author.normalise_summary(src) == "The record is saved"

    def test_normalise_case_uses_the_summary_rule_for_the_summary(self):
        case = tc_author.AuthoredCase(
            summary="check that the record must be saved",
            preconditions="Record is created",
            steps=["Go to the grid", 'Click on the "Save" button'],
            test_data="",
            expected_result="The record must be saved",
            category="Positive",
            priority="High",
            section="Employees grid",
        )
        fixed, residual = tc_author.normalise_case(case)
        assert fixed.expected_result == "The record should be saved"
        assert "should" not in fixed.summary.lower()
        # The finding this would have produced is the whole point: a
        # summary carrying "should" is a lint failure, so a normaliser
        # that introduces one has made the case worse than it found it.
        assert residual == []

    def test_the_summary_rule_strips_should_it_did_not_write(self):
        assert tc_author.normalise_summary(
            "The banner should be visible") == "The banner is visible"
        assert tc_author.normalise_summary(
            "Errors should not appear") == "Errors do not appear"
