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


class TestTheRequirementsSpecificRowsAreClean:
    """Found on prod after the rule shipped, by generating and reading.

    ``qa_persona`` builds three checklist rows per raw requirement and
    interpolated the requirement text bare: with a requirement of "user
    can sign in" that produced "Verify user can sign in rejects invalid
    input with a clear validation message" — ungrammatical whatever the
    requirement said, and carrying the operator's modal into a summary.

    Nothing linted this path, which is how it survived a commit that
    cleaned every template and both other generators.
    """

    def _objectives(self, requirement: str) -> list[str]:
        from engine.qa_persona import (analyze_input,
                                       generate_professional_checklist)
        analysis = analyze_input([{"text": requirement}])
        items = generate_professional_checklist(analysis)
        return [i.objective for i in items
                if "Requirements-specific" in (i.section or "")]

    def test_a_requirement_with_a_modal_still_yields_clean_rows(self):
        objectives = self._objectives(
            "The user can sign in with an email and a password")
        assert objectives, "no Requirements-specific rows were produced"
        for objective in objectives:
            findings = [f for f in g.lint_text(objective, kind="objective")
                        if "modal" in f]
            assert not findings, f"{objective}: {findings}"

    def test_the_requirement_is_quoted_not_interpolated_bare(self):
        """Quoting is what makes the row grammatical for any input."""
        objectives = self._objectives(
            "The user can sign in with an email and a password")
        assert all('"' in o for o in objectives), objectives
        # …and the operator's own words survive inside the quotes.
        assert any("can sign in" in o for o in objectives), objectives


class TestTheVoiceGuard:
    """The operator's shape: the thing under test is the subject.

        Verify that the "Contact" form is submitted after clicking the
        "Submit" button

    A first pass at the modal ruling replaced "Verify that User can save"
    with "Verify that User saves" — modal-free, but active voice with the
    tester as the actor, which is what the ruling is against.
    """

    @pytest.mark.parametrize("summary", [
        'Verify that User uploads an accepted file to the "CV" field',
        "Verify that User saves the record",
        "Verify that the user selects an existing value",
    ])
    def test_the_tester_as_actor_is_flagged(self, summary):
        assert [f for f in g.lint_text(summary, kind="objective")
                if "passive voice" in f], summary

    @pytest.mark.parametrize("summary", [
        'Verify that an accepted file is uploaded to the "CV" field',
        "Verify that the record is saved after clicking the \"Save\" control",
        # Stative active voice is explicitly allowed — "інколи Active
        # Voice (якщо це доречно)".
        "Verify that the record counter matches the visible row count",
        # "User" as part of a noun, not as the actor.
        "Verify that the User account is deleted",
        'Verify that the "User" drop-down is displayed',
    ])
    def test_the_passive_and_the_allowed_active_pass(self, summary):
        assert [f for f in g.lint_text(summary, kind="objective")
                if "passive voice" in f] == [], summary


class TestVoiceIsReportedNotRewritten:
    """Where the two rules meet, and why the seam is where it is.

    The modal rule rewrites: "User cannot create X" becomes "User does
    not create X", which is mechanical and loses nothing. That result is
    still active voice with the tester as the subject, so the voice rule
    then reports it — it does not try to reorder the clause into "X is
    not created", because reordering arbitrary prose is exactly the
    machine that broke this corpus in May.

    So a normaliser can hand back text its own linter flags. That is the
    intended split: rewrite only what is safe, name the rest.
    """

    def test_the_modal_is_removed_and_the_voice_is_flagged(self):
        rewritten = g.normalise_text(
            "Verify that User cannot create the record", kind="title")
        assert "cannot" not in rewritten
        assert rewritten == "Verify that User does not create the record"
        findings = g.lint_text(rewritten, kind="title")
        assert [f for f in findings if "passive voice" in f], findings
        assert not [f for f in findings if "modal" in f]

    def test_the_passive_form_needs_neither(self):
        text = "Verify that the record is not created"
        assert g.normalise_text(text, kind="title") == text
        assert g.lint_text(text, kind="title") == []


class TestTheCoverageModelTitles:
    """The phrasing lives in the asset, so the asset is what is checked."""

    def _yaml(self) -> str:
        return (QA_KNOWLEDGE / "style" / "coverage_rules.yaml").read_text(
            encoding="utf-8")

    def test_every_per_control_case_carries_a_title(self):
        """Without one the generic fallback composes the summary, and a
        fallback is not where a client-visible sentence should be born.
        """
        import yaml as _yaml
        doc = _yaml.safe_load(self._yaml())
        per_type = ((doc.get("create_form") or {})
                    .get("per_control_type")) or {}
        missing = [c["objective"] for v in per_type.values()
                   for c in (v.get("cases") or []) if not c.get("title")]
        assert not missing, missing

    def test_every_title_renders_and_lints_clean(self):
        import re as _re
        values = {"grid": "Job Positions", "column": "Name",
                  "control": "Create", "filter": "Internal",
                  "action": "Archive", "label": "Email", "noun": "field",
                  "section": "Contact form", "search": "Search"}
        problems: list[str] = []
        titles = _re.findall(r"title: '([^']+)'", self._yaml())
        assert len(titles) > 40, "title templates went missing"
        for template in titles:
            try:
                rendered = template.format(**values)
            except KeyError as exc:
                problems.append(f"unknown placeholder {exc} in {template!r}")
                continue
            problems += [f"{rendered!r}: {f}"
                         for f in g.lint_text(rendered, kind="title")]
        assert not problems, "\n  ".join([""] + problems)


class TestGeneratedFromRealMarkup:
    """The end the client sees: crawled controls in, summaries out."""

    def _cases(self):
        from engine.tc_rules import enumerate_from_pages
        return enumerate_from_pages([{
            "url": "https://x.test/contact", "title": "Contact",
            "forms": [{"action": "/contact", "submit_text": "Send",
                       "fields": [
                           {"name": "name", "label": "Name",
                            "type": "text", "required": True},
                           {"name": "email", "label": "Email",
                            "type": "email", "required": True},
                           {"name": "topic", "label": "Topic",
                            "type": "select",
                            "options": ["Sales", "Support"]},
                           {"name": "cv", "label": "CV", "type": "file"},
                       ]}]}])

    def test_cases_are_produced(self):
        assert len(self._cases()) > 8

    def test_no_generated_summary_puts_the_tester_in_the_subject(self):
        for case in self._cases():
            assert [f for f in g.lint_text(case.summary, kind="title")
                    if "passive voice" in f] == [], case.summary

    def test_generated_summaries_are_house_style_clean(self):
        for case in self._cases():
            assert g.lint_text(case.summary, kind="title") == [], case.summary


class TestThePassiveFallback:
    """For a rule added without a title. It refuses more than it accepts."""

    @pytest.mark.parametrize("objective,expected", [
        ("Enter a valid value", "a valid value is entered"),
        ("Clear the selection", "the selection is cleared"),
        ("Submit the form", "the form is submitted"),
        ("Run the report", "the report is run"),
        ("Reset the record to its initial state",
         "the record is reset to its initial state"),
        # The trailing prepositional phrase stays after the verb.
        ("Pick a date via the date-picker",
         "a date is picked via the date-picker"),
        ("Delete the record through the confirmation dialog",
         "the record is deleted through the confirmation dialog"),
    ])
    def test_the_one_safe_shape_is_rewritten(self, objective, expected):
        from engine.tc_rules import _passive_clause
        assert _passive_clause(objective) == expected

    @pytest.mark.parametrize("objective", [
        # Compound — reordering needs a parser.
        "Add and delete an attachment",
        "Change one field, save, reload, and confirm only that field changed",
        # Already an assertion: "… updates is confirmed" is not English.
        "Confirm required fields are visually marked before submission",
        # No noun phrase to promote: "empty when required is left".
        "Leave empty when required",
        # Not an imperative at all.
        "'Launch' is displayed",
        "Enter",
        "",
    ])
    def test_everything_else_is_refused(self, objective):
        from engine.tc_rules import _passive_clause
        assert _passive_clause(objective) is None


class TestParticiples:
    def test_the_irregulars_the_corpus_uses(self):
        assert g.past_participle("reset") == "reset"
        assert g.past_participle("run") == "run"
        assert g.past_participle("leave") == "left"
        assert g.past_participle("find") == "found"

    def test_the_regular_rules(self):
        assert g.past_participle("enter") == "entered"
        assert g.past_participle("apply") == "applied"
        assert g.past_participle("save") == "saved"
        assert g.past_participle("submit") == "submitted"   # doubling

    def test_an_unknown_shape_is_refused_not_guessed(self):
        assert g.past_participle("") is None
        assert g.past_participle("'Launch'") is None

    def test_number_agreement(self):
        assert g.reads_plural("the characters") is True
        assert g.reads_plural("a valid value") is False
        assert g.reads_plural("all rows") is True
        # "-ss" is not a plural.
        assert g.reads_plural("the address") is False


class TestTheRequirementExcerpt:
    """A citation cut mid-word looks faithful and is not."""

    def test_a_long_requirement_is_cut_on_a_word_boundary(self):
        from engine.qa_persona import _excerpt
        out = _excerpt("The user can sign in with an email and a password. "
                       "The user can reset a forgotten password.", 80)
        assert out.endswith("…")
        assert not out.rstrip("…").endswith(" ")
        # the cut never lands inside a word
        assert out.rstrip("…").split()[-1] in \
            "The user can sign in with an email and a password The".split()

    def test_a_short_requirement_is_left_alone(self):
        from engine.qa_persona import _excerpt
        assert _excerpt("The user signs in.", 80) == "The user signs in"
