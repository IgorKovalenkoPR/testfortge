"""A summary carries no modal verb. "should" belongs to expected results.

Operator ruling, 2026-08-01:

    Саммері для тест-кейсів, чек-лістів та баг-репортів має бути
    оформлено в Passive Voice / Active Voice без використання модальних
    дієслів. Єдине модальне дієслово, яке може використовуватися — це
    should / should be, і то лише у Expected result тест-кейсів та
    баг-репортів.

This refines the earlier ruling recorded at ``tc_author._WEAK_MODAL_RE``,
which accepted "should" without saying where. It is accepted in an
expected result and nowhere else.

The rule is enforced in ``engine.glossary.lint_text`` rather than only in
the prompt, so it also holds on the free / no-API-key path — which, with
the LLM authors on fallback, is currently every path.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

from engine import glossary as g


QA_KNOWLEDGE = (pathlib.Path(__file__).resolve().parent.parent
                / "engine" / "qa_knowledge")


class TestTheRuleIsLoaded:
    def test_the_bucket_parses(self):
        words = g.banned_phrases("modal_summary_only")
        assert "should" in words
        assert "can" in words
        assert "may" in words

    def test_must_and_shall_stay_banned_everywhere(self):
        """They were already banned; the new bucket must not replace it."""
        assert "must" in g.banned_phrases("modal")
        assert "shall" in g.banned_phrases("modal")


class TestSummariesRejectModals:
    @pytest.mark.parametrize("modal", ["should", "can", "cannot", "could",
                                       "may", "might", "will", "would"])
    def test_a_modal_in_an_objective_is_flagged(self, modal):
        found = g.lint_text(
            f"Verify that the user {modal} open the record", kind="objective")
        assert any("modal" in f for f in found), found

    @pytest.mark.parametrize("modal", ["should", "can", "may"])
    def test_a_modal_in_a_title_is_flagged(self, modal):
        found = g.lint_text(
            f"Verify that the record {modal} be saved", kind="title")
        assert any("modal" in f for f in found), found

    def test_a_modal_free_summary_passes(self):
        assert g.lint_text(
            "Verify that the record is saved after clicking the "
            '"Save" button', kind="objective") == []

    def test_active_voice_passes_too(self):
        """Both voices are acceptable — the ruling names them together."""
        assert g.lint_text(
            'Verify that the "Save" button stores the record',
            kind="objective") == []


class TestExpectedResultsKeepShould:
    def test_should_is_allowed_in_an_expected_result(self):
        assert g.lint_text("The entered data should be accepted",
                           kind="expected") == []

    def test_should_be_is_allowed_too(self):
        assert g.lint_text("The record should be visible in the list",
                           kind="expected") == []

    @pytest.mark.parametrize("modal", ["must", "shall", "ought to"])
    def test_the_stronger_modals_are_still_rejected_there(self, modal):
        found = g.lint_text(f"The record {modal} be saved", kind="expected")
        assert found, f'"{modal}" should still be flagged in an expected result'


class TestTheShippedCorpusObeysIt:
    """These templates are the free-tier output — a finding here is a
    finding a client sees. With both LLM authors on fallback, they are
    also the only output.
    """

    def _yaml_files(self) -> list[pathlib.Path]:
        return [p for p in sorted(QA_KNOWLEDGE.rglob("*.yaml"))
                if p.parent.name in ("checklists", "testcases")]

    def test_there_are_templates_to_check(self):
        assert len(self._yaml_files()) > 5

    @pytest.mark.parametrize(
        "path",
        [p for p in sorted(QA_KNOWLEDGE.rglob("*.yaml"))
         if p.parent.name in ("checklists", "testcases")],
        ids=lambda p: p.name,
    )
    def test_every_objective_summary_expected_and_step_is_clean(self, path):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        problems: list[str] = []
        for section in doc.get("sections") or []:
            for item in section.get("items") or []:
                text = item.get("objective") or ""
                problems += [f"objective {text!r}: {f}"
                             for f in g.lint_text(text, kind="objective")]
        for case in doc.get("cases") or []:
            summary = case.get("summary") or ""
            problems += [f"summary {summary!r}: {f}"
                         for f in g.lint_text(summary, kind="title")]
            expected = case.get("expected_result") or ""
            problems += [f"expected {expected!r}: {f}"
                         for f in g.lint_text(expected, kind="expected")]
            problems += [f"step: {f}"
                         for f in g.lint_steps(case.get("steps") or [])]
        assert not problems, "\n  ".join([""] + problems)


class TestTheGeneratorStoppedEmittingCan:
    """`tc_rules` composed "Verify that User can <objective>"."""

    def test_the_imperative_is_inflected_instead(self):
        from engine.tc_rules import _third_person
        assert _third_person("Enter a valid value") == "enters a valid value"
        assert _third_person("Search a value") == "searches a value"
        assert _third_person("Apply the filter") == "applies the filter"
        assert _third_person("Go to the page") == "goes to the page"

    def test_a_non_verb_opener_is_refused_rather_than_mangled(self):
        """Prefixing a subject to a statement produced "User 'launch' is …"."""
        from engine.tc_rules import _third_person
        assert _third_person("'Launch' is displayed") is None
        assert _third_person("") is None
