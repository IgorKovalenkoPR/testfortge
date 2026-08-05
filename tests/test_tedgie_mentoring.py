"""
Tests for the mentoring packs, the router that reaches them, and the CI
gate that stops the answers rotting.

The gate at the bottom is the point of the file. Everything above it tests
mechanism; ``TestTheGate`` runs the whole golden set through the real
deterministic chain and fails the build if the score drops. Prompt and pack
edits are cheap to make and their damage is invisible without this — which
is exactly how the severity recommender came to be unreachable for months
while every test stayed green.
"""
from __future__ import annotations

import textwrap

import pytest

from engine import chatbot, mentoring
from engine import tedgie_eval as ev


@pytest.fixture(autouse=True)
def _fresh_packs():
    # The pack cache is process-wide; a test that loads a temp pack must
    # not leave it behind for the next one.
    mentoring.clear_cache()
    yield
    mentoring.clear_cache()


# ── Packs ─────────────────────────────────────────────────────────────

class TestPacksLoad:
    def test_every_declared_pack_exists(self):
        for name in mentoring.PACKS:
            assert mentoring.load_pack(name) is not None, name

    def test_entry_ids_are_unique_within_a_pack(self):
        for name in mentoring.PACKS:
            ids = [e.id for e in mentoring.load_pack(name).entries]
            assert len(ids) == len(set(ids)), name

    def test_entry_ids_are_unique_across_packs(self):
        # The catch-all list in chatbot refers to entries by bare id, and a
        # collision there would silence the wrong entry.
        seen: dict[str, str] = {}
        for name in mentoring.PACKS:
            for entry in mentoring.load_pack(name).entries:
                assert entry.id not in seen, f"{entry.id} in {name} and {seen[entry.id]}"
                seen[entry.id] = name

    def test_every_entry_has_a_source(self):
        # An answer that states a house rule must say which file the rule
        # came from, or nobody can check it against the generator.
        for name in mentoring.PACKS:
            for entry in mentoring.load_pack(name).entries:
                assert entry.source, f"{entry.id} has no source"

    def test_every_pack_has_exactly_one_catch_all(self):
        for name in mentoring.PACKS:
            general = [e for e in mentoring.load_pack(name).entries if e.weight < 0]
            assert len(general) == 1, f"{name} has {len(general)}"

    def test_a_catch_all_is_marked_as_one_on_the_answer(self):
        """The router must not need a list of catch-all ids.

        The first version of this kept one in chatbot, and it spelled the
        process pack's entry `process_general` when the pack calls it
        `proc_general` — so that catch-all was never actually declined in
        the fast path. It happened to be harmless because a negative weight
        loses to any specific entry anyway, which is precisely why nothing
        caught it. The flag now travels with the answer.
        """
        general = mentoring.answer("Tell me about the process in general")
        assert general is not None and general.is_catch_all
        specific = mentoring.answer("A rare crash loses the user's unsaved form. Severity?")
        assert specific is not None and not specific.is_catch_all

    def test_missing_localisation_falls_back_to_english(self):
        # No uk packs exist yet (E6.8). The fallback must be visible, not
        # silent, so a half-done localisation shows up in the eval rather
        # than reading as fluent Ukrainian.
        found = mentoring.answer("What severity for a footer typo?", "uk")
        assert found is not None
        assert found.fell_back_to_english is True

    def test_english_answers_are_not_marked_as_fallback(self):
        found = mentoring.answer("What severity for a footer typo?", "en")
        assert found is not None and found.fell_back_to_english is False


class TestMalformedPacksAreRejected:
    def _pack(self, tmp_path, body, name="severity_priority"):
        d = tmp_path / "mentoring"
        d.mkdir(exist_ok=True)
        (d / f"{name}.en.yaml").write_text(textwrap.dedent(body), encoding="utf-8")
        mentoring.PACK_DIR, old = d, mentoring.PACK_DIR
        mentoring.clear_cache()
        return old

    def _restore(self, old):
        mentoring.PACK_DIR = old
        mentoring.clear_cache()

    def test_entry_without_triggers(self, tmp_path):
        old = self._pack(tmp_path, """
            pack: severity_priority
            entries:
              - id: x
                answer: anything
        """)
        try:
            with pytest.raises(mentoring.PackError, match="no triggers"):
                mentoring.load_pack("severity_priority")
        finally:
            self._restore(old)

    def test_entry_without_an_answer(self, tmp_path):
        old = self._pack(tmp_path, """
            pack: severity_priority
            entries:
              - id: x
                any: [alpha]
        """)
        try:
            with pytest.raises(mentoring.PackError, match="no answer"):
                mentoring.load_pack("severity_priority")
        finally:
            self._restore(old)

    def test_trigger_too_short_to_mean_anything(self, tmp_path):
        # Substring matching: "ui" fires inside "require".
        old = self._pack(tmp_path, """
            pack: severity_priority
            entries:
              - id: x
                any: [ui]
                answer: anything
        """)
        try:
            with pytest.raises(mentoring.PackError, match="too short"):
                mentoring.load_pack("severity_priority")
        finally:
            self._restore(old)


# ── Matching ──────────────────────────────────────────────────────────

def _entry(**over):
    base = dict(id="e1", pack="process", any_of=("alpha",), answer="A")
    base.update(over)
    return mentoring.Entry(**base)


def _pack(*entries):
    return mentoring.Pack(name="process", lang="en", label="p", entries=entries)


class TestMatching:
    def test_any_needs_one_hit(self):
        p = _pack(_entry())
        assert mentoring.match_entry("says alpha", p) is not None
        assert mentoring.match_entry("says beta", p) is None

    def test_all_groups_each_need_a_hit(self):
        e = _entry(any_of=(), all_of=(("alpha",), ("beta",)))
        p = _pack(e)
        assert mentoring.match_entry("alpha only", p) is None
        assert mentoring.match_entry("alpha and beta", p) is not None

    def test_none_vetoes(self):
        p = _pack(_entry(none_of=("stop",)))
        assert mentoring.match_entry("alpha", p) is not None
        assert mentoring.match_entry("alpha but stop", p) is None

    def test_more_hits_wins(self):
        broad = _entry(id="broad", any_of=("alpha",))
        narrow = _entry(id="narrow", any_of=("alpha", "beta"))
        m = mentoring.match_entry("alpha beta", _pack(broad, narrow))
        assert m.entry.id == "narrow"

    def test_longer_hit_breaks_a_tie(self):
        # Equal hit counts, so the more specific phrase should win: it was
        # written for this question and the short one was not.
        vague = _entry(id="vague", any_of=("test",))
        exact = _entry(id="exact", any_of=("test step wording",))
        m = mentoring.match_entry("about test step wording", _pack(vague, exact))
        assert m.entry.id == "exact"

    def test_negative_weight_loses_to_anything_specific(self):
        general = _entry(id="general", any_of=("alpha",), weight=-3)
        specific = _entry(id="specific", any_of=("alpha",))
        m = mentoring.match_entry("alpha", _pack(general, specific))
        assert m.entry.id == "specific"

    def test_quotes_are_stripped_from_the_message(self):
        # Measured on the golden set: people quote the words the triggers
        # are made of. "Should I write 'click' or 'press'" matched nothing.
        p = _pack(_entry(any_of=("click or press",)))
        assert mentoring.match_entry("write 'click' or 'press' here", p) is not None
        assert mentoring.match_entry('write "click" or "press" here', p) is not None


class TestDefinitionalShape:
    @pytest.mark.parametrize("q", [
        "What is severity?", "what are the test levels",
        "Explain risk-based testing", "difference between error and defect",
        "Що таке дефект?", "яка різниця між верифікацією і валідацією",
    ])
    def test_recognised(self, q):
        assert mentoring.is_definitional(q)

    @pytest.mark.parametrize("q", [
        "What severity for a footer typo?",
        "Is the wrong total backend or frontend?",
        "Should I write click or press?",
        "Опечатка в футері. Яка серйозність?",
    ])
    def test_not_recognised(self, q):
        assert not mentoring.is_definitional(q)


# ── The router in chatbot ─────────────────────────────────────────────

class TestRouting:
    def test_a_concrete_triage_question_reaches_the_pack(self):
        # The defect this whole epic starts from: detect_topic matched the
        # word "severity" and answered with its definition.
        r = chatbot.try_fast_path("A rare crash that loses the user's unsaved form. Severity?")
        assert r is not None and r.intent == "pack:severity_priority"

    def test_a_definitional_question_stays_with_istqb(self):
        r = chatbot.try_fast_path("What is severity?")
        assert r is not None and "istqb" in r.intent
        assert not r.intent.startswith("pack:")

    def test_a_definitional_shape_about_nothing_in_the_syllabus_is_ours(self):
        # Shape and subject are both required for the veto: this one has
        # the shape and no syllabus subject.
        r = chatbot.try_fast_path("What evidence should I attach so the dev doesn't send it back?")
        assert r is not None and r.intent == "pack:layer"

    def test_the_catch_all_is_declined_in_the_fast_path(self):
        # So chatbot_guide's severity decision tree keeps the cue
        # combinations no single pack entry covers.
        r = chatbot._mentoring_reply("Tell me about severity in general")
        assert r is None

    def test_the_catch_all_is_reachable_from_the_fallback(self):
        r = chatbot._mentoring_reply("Tell me about severity in general",
                                    allow_catch_all=True)
        assert r is not None and r.intent == "pack:severity_priority"

    def test_house_rules_come_from_the_pack_not_the_retriever(self):
        # Before the pack existed, this question was answered from the BM25
        # index with a Java Selenium snippet scoring 10.6 against a
        # relevance floor of 4.0.
        r = chatbot.try_fast_path("Should I write 'click' or 'press' in a test step?")
        assert r is not None and r.intent == "pack:naming"
        assert "click on" in r.text.lower()

    def test_the_pack_agrees_with_the_generator_on_should(self):
        # wording_rules.yaml arbitrates "should" as accepted in an expected
        # result and banned in a title. Chat advice that contradicts the
        # file tc_author reads is the one failure mode this pack exists to
        # prevent, so it is asserted rather than trusted.
        r = chatbot.try_fast_path("Can I write 'should' in an expected result?")
        assert r is not None and r.intent == "pack:naming"
        low = r.text.lower()
        assert "accepted" in low
        assert "title" in low


class TestComparativeQuestions:
    def test_three_terms_are_all_answered(self):
        # Measured: the most-asked question in testing returned the
        # definition of "failure" alone, because the glossary detector
        # takes the longest single alias match.
        r = chatbot.try_fast_path("What is the difference between error, defect and failure?")
        assert r is not None
        low = r.text.lower()
        assert "human" in low or "mistake" in low
        assert "flaw" in low or "work product" in low

    def test_verification_versus_validation(self):
        r = chatbot.try_fast_path("What is verification vs validation?")
        assert r is not None
        low = r.text.lower()
        assert "requirement" in low or "specification" in low
        assert "needs" in low or "intended" in low or "fit for" in low

    def test_a_pair_with_no_syllabus_topic_is_assembled_from_the_glossary(self):
        r = chatbot.try_fast_path("What's the difference between smoke and regression?")
        assert r is not None and r.intent == "istqb:comparison"
        low = r.text.lower()
        assert "smoke" in low and "regression" in low

    def test_a_single_term_still_gets_its_own_definition(self):
        r = chatbot.try_fast_path("What is a defect?")
        assert r is not None and r.intent != "istqb:comparison"


class TestQuestionsAboutBugsAreNotReportsOfBugs:
    """Two bare nouns in trigger lists made every question *about* bugs
    look like a bug being filed or a failure being reported."""

    def test_asking_where_the_module_is_does_not_open_the_filing_form(self):
        r = chatbot.try_fast_path("Where do I see the bug reports?")
        assert r is None or r.intent != "bug_form"

    def test_asking_where_the_module_is_is_not_troubleshooting(self):
        r = chatbot.rule_based_fallback("Where do I see the bug reports?")
        assert not r.intent.startswith("troubleshoot")
        assert "bug report" in r.text.lower()

    def test_asking_how_to_export_them_is_not_troubleshooting(self):
        r = chatbot.rule_based_fallback("How do I export bug reports?")
        assert not r.intent.startswith("troubleshoot")

    def test_actually_filing_one_still_opens_the_form(self):
        r = chatbot.try_fast_path("I found a bug, let me file a bug report")
        assert r is not None and r.intent == "bug_form"

    def test_something_actually_broken_still_reaches_the_troubleshooter(self):
        r = chatbot.rule_based_fallback("Automation is broken")
        assert r.intent.startswith("troubleshoot")


# ── The gate ──────────────────────────────────────────────────────────

#: Measured baselines, deterministic chain only, no LLM.
#:
#: English is at 100% and is gated there: every EN item has a deterministic
#: owner, so any drop is a real regression rather than a coverage gap.
#:
#: Ukrainian is at 7/19 with one of the four packs localised (E6.8 is
#: partly done: severity_priority.ua.yaml exists, layer / naming / process
#: do not). It is gated at the measured floor so the number cannot quietly
#: get worse while the localisation is outstanding, and the floor must be
#: raised as each UA pack lands. Gating UA at 100% today would just mean a
#: permanently red build, which teaches everyone to ignore it.
BASELINE_EN = 85
BASELINE_UK = 7
BASELINE_TOTAL = 92


class TestTheGate:
    @pytest.fixture(scope="class")
    def report(self):
        return ev.run_rule_layer()

    def test_english_does_not_regress(self, report):
        en = [r for r in report.results if r.item.lang == "en"]
        passed = sum(1 for r in en if r.passed)
        assert passed >= BASELINE_EN, (
            f"{passed}/{len(en)} — regressed from {BASELINE_EN}:\n"
            + "\n".join(f"  {r.item.id}: {r.explain()}"
                        for r in en if not r.passed)
        )

    def test_ukrainian_does_not_regress(self, report):
        uk = [r for r in report.results if r.item.lang == "uk"]
        passed = sum(1 for r in uk if r.passed)
        assert passed >= BASELINE_UK, (
            f"{passed}/{len(uk)} — regressed from {BASELINE_UK}:\n"
            + "\n".join(f"  {r.item.id}: {r.explain()}" for r in uk if not r.passed)
        )

    def test_the_whole_set_does_not_regress(self, report):
        assert report.passed >= BASELINE_TOTAL, ev.format_report(report, verbose=True)

    def test_nothing_gives_wrong_advice(self, report):
        # A missing requirement is an incomplete answer; a guard hit is a
        # harmful one. This assertion has no tolerance on purpose.
        assert not report.violations, "\n".join(
            f"{r.item.id}: {r.explain()}" for r in report.violations
        )

    def test_every_english_item_is_answered_by_its_declared_owner(self, report):
        # Content can be right while the pack that owns the question is
        # unreachable — the defect the golden set was written to expose.
        wrong = [(r.item.id, r.route, r.item.route)
                 for r in report.results
                 if r.item.lang == "en" and not r.route_ok]
        assert not wrong, wrong

    def test_each_pack_holds_its_own_english_items(self, report):
        for pack, (ok, total) in report.by_pack().items():
            en = [r for r in report.results
                  if r.item.pack == pack and r.item.lang == "en"]
            passed = sum(1 for r in en if r.passed)
            assert passed == len(en), f"{pack}: {passed}/{len(en)}"
