"""
The golden set is the measure of quality for every Tedgie knowledge pack,
so it needs its own tests: a broken measure fails silently, reads as a
green build, and lets the thing it measures rot.

Two groups here, and they answer different questions:

* the *contract* tests assert the scorer does what its docstring
  promises — any-of alternatives, violations that zero the score,
  markdown that does not count as a miss, route kept out of pass/fail;
* the *set* tests assert the golden set itself is well formed and does
  not contain the self-defeating items that are easy to write by
  accident, chiefly an `avoid` string that also appears in the question
  (an answer that politely restates the question would fail) or one that
  contradicts a `require` alternative (nothing could ever pass).
"""
from __future__ import annotations

import textwrap

import pytest

from engine import tedgie_eval as ev


# ── The set itself ───────────────────────────────────────────────────

@pytest.fixture(scope="module")
def items():
    return ev.load()


class TestGoldenSetShape:
    def test_it_loads(self, items):
        assert items

    def test_size_is_in_the_agreed_range(self, items):
        # E6.1 specified 80-120 items. Below 80 the packs are undersampled;
        # above 120 the set stops being maintainable and people start
        # editing items to make CI green, which is the failure this whole
        # file exists to prevent.
        assert 80 <= len(items) <= 120, len(items)

    def test_ids_are_unique(self, items):
        ids = [i.id for i in items]
        assert len(ids) == len(set(ids))

    def test_every_pack_is_covered(self, items):
        covered = {i.pack for i in items}
        assert covered == set(ev.PACKS), set(ev.PACKS) - covered

    def test_every_pack_has_at_least_ten_items(self, items):
        # A pack scored by three questions can be passed by luck.
        for pack, group in ev.by_pack(items).items():
            assert len(group) >= 10, f"{pack} has only {len(group)}"

    def test_every_pack_is_scored_in_both_languages(self, items):
        # E6.8's acceptance is "UA answers no worse than EN on the same
        # golden set", which is unmeasurable for a pack with no UA items.
        for pack, group in ev.by_pack(items).items():
            langs = {i.lang for i in group}
            assert {"en", "uk"} <= langs, f"{pack} covers only {langs}"

    def test_ukrainian_share_is_material(self, items):
        uk = sum(1 for i in items if i.lang == "uk")
        assert uk >= 15, uk


class TestItemsAreNotSelfDefeating:
    def test_no_avoid_string_appears_in_its_own_question(self, items):
        """An answer that restates the question must not thereby fail.

        Tedgie's answers routinely open by echoing the ask ("A footer typo
        is Minor because…"). If a forbidden string is sitting in the
        question, that echo trips the guard and the item measures
        politeness instead of knowledge.
        """
        offenders = []
        for it in items:
            low = it.question.lower()
            for bad in it.avoid:
                if bad in low:
                    offenders.append(f"{it.id}: {bad!r} is in the question")
        assert not offenders, offenders

    def test_no_requirement_alternative_forces_a_violation(self, items):
        """An alternative that contains a guard can never be used.

        Only this direction is a defect. The reverse — a guard that
        contains a requirement, e.g. require "report" with guard "don't
        report" — is exactly how a guard is supposed to work: the answer
        satisfies the requirement on the word and is still failed for the
        advice, because a guard hit fails outright. Asserting both
        directions (the first version of this test) flagged four correct
        items and would have pushed me to weaken them.
        """
        offenders = []
        for it in items:
            for bad in it.avoid:
                for group in it.require:
                    for alt in group:
                        if bad in alt:
                            offenders.append(f"{it.id}: require {alt!r} contains guard {bad!r}")
        assert not offenders, offenders

    def test_strings_are_specific_enough_to_mean_something(self, items):
        """Substring matching makes short strings fire on unrelated words.

        Measured on the first draft: "no" matches "know", "ui" matches
        "req**ui**re", "to" matches almost every sentence, "10" matches
        "2010". Twelve alternatives and one guard had to be rewritten.
        Three characters is the floor at which a token stops being an
        accident — the remaining three-character ones ("can", "api",
        "har") are all real terms.
        """
        for it in items:
            for bad in it.avoid:
                assert len(bad) >= 3, f"{it.id}: guard {bad!r} is too short"
            for group in it.require:
                for alt in group:
                    assert len(alt) >= 3, f"{it.id}: alternative {alt!r} is too short"

    def test_no_item_can_pass_on_an_empty_answer(self, items):
        for it in items:
            assert not ev.score(it, "").passed, it.id

    def test_routes_name_a_known_owner(self, items):
        for it in items:
            assert it.route.split(":", 1)[0] in ev.ROUTE_NAMESPACES

    def test_the_new_packs_own_their_own_questions(self, items):
        """The mentoring packs must be the declared owner for most of
        their items.

        The measured gap was that `istqb:severity` answers concrete triage
        questions with a definition. If the golden set were to route those
        items back to `istqb:`, it would ratify the defect instead of
        catching it.
        """
        for pack in ("severity_priority", "layer", "naming", "process"):
            group = ev.by_pack(items)[pack]
            owned = sum(1 for i in group if i.route == f"pack:{pack}")
            assert owned >= len(group) * 0.7, f"{pack}: only {owned}/{len(group)} owned"


# ── The scoring contract ─────────────────────────────────────────────

def _item(**over):
    base = dict(
        id="T-1", pack="process", lang="en", question="q?",
        route="pack:process", require=(("alpha", "beta"),), avoid=(),
    )
    base.update(over)
    return ev.Item(**base)


class TestRequirementsAreAnyOf:
    def test_first_alternative_satisfies(self):
        assert ev.score(_item(), "the answer mentions alpha").passed

    def test_second_alternative_satisfies(self):
        assert ev.score(_item(), "the answer mentions beta").passed

    def test_neither_fails(self):
        r = ev.score(_item(), "the answer mentions gamma")
        assert not r.passed
        assert r.missed == (0,)

    def test_all_requirements_must_be_met(self):
        it = _item(require=(("alpha",), ("beta",)))
        assert not ev.score(it, "only alpha here").passed
        assert ev.score(it, "alpha and beta").passed

    def test_matching_is_case_insensitive(self):
        assert ev.score(_item(), "ALPHA").passed

    def test_stems_match_inflections(self):
        # Requirements are written as stems so one alternative covers
        # "localisation", "localised", "локалізація".
        it = _item(require=(("localis",),))
        assert ev.score(it, "a localisation defect").passed
        assert ev.score(it, "the string was localised").passed


class TestMarkdownDoesNotCountAsAMiss:
    def test_bold_is_stripped(self):
        # The rule layer answers with "**Recommended severity: Minor**".
        it = _item(require=(("minor",),))
        assert ev.score(it, "**Recommended severity: Minor**").passed

    def test_backticks_are_stripped(self):
        it = _item(require=(("alpha",),))
        assert ev.score(it, "see `alpha`").passed

    def test_newlines_do_not_break_a_multi_word_alternative(self):
        it = _item(require=(("click on",),))
        assert ev.score(it, "Click\non the button").passed


class TestViolationsFailOutright:
    def test_a_guard_hit_fails_a_fully_satisfied_answer(self):
        it = _item(require=(("alpha",),), avoid=("critical",))
        r = ev.score(it, "alpha, and it is critical")
        assert not r.passed
        assert r.violations == ("critical",)

    def test_a_guard_hit_zeroes_the_score(self):
        it = _item(require=(("alpha",), ("beta",)), avoid=("critical",))
        r = ev.score(it, "alpha and beta and critical")
        assert r.score == 0.0

    def test_the_explanation_names_what_was_said(self):
        it = _item(require=(("alpha",),), avoid=("critical",))
        assert "critical" in ev.score(it, "alpha critical").explain()


class TestPartialScoreIsReported:
    def test_half_the_requirements_is_half_the_score(self):
        it = _item(require=(("alpha",), ("beta",)))
        assert ev.score(it, "alpha only").score == 0.5

    def test_the_explanation_names_what_is_missing(self):
        it = _item(require=(("alpha",), ("beta", "gamma")))
        why = ev.score(it, "alpha only").explain()
        assert "beta" in why and "gamma" in why


class TestRouteIsScoredSeparately:
    def test_a_wrong_route_does_not_fail_a_correct_answer(self):
        # This is the whole point: the severity recommender exists and is
        # shadowed. An answer that happens to be right must still be
        # visible as mis-routed.
        r = ev.score(_item(), "alpha", route="istqb:severity")
        assert r.passed
        assert not r.route_ok

    def test_a_matching_route_is_ok(self):
        assert ev.score(_item(), "alpha", route="pack:process").route_ok

    def test_an_unobserved_route_is_not_reported_as_wrong(self):
        assert ev.score(_item(), "alpha", route=None).route_ok

    def test_the_explanation_mentions_routing_only_when_content_is_fine(self):
        r = ev.score(_item(), "alpha", route="istqb:severity")
        assert "expected pack:process" in r.explain()
        r2 = ev.score(_item(), "nothing here", route="istqb:severity")
        assert "missing" in r2.explain()


class TestReport:
    def _report(self):
        a = _item(id="A", pack="process", require=(("alpha",),))
        b = _item(id="B", pack="naming", route="pack:naming",
                  require=(("beta",),), avoid=("bad",))
        # C is deliberately mis-routed: content ok, wrong owner.
        c = _item(id="C", pack="naming", lang="uk", require=(("gamma",),))
        return ev.score_all([
            (a, "alpha", "pack:process"),
            (b, "beta but bad", "pack:naming"),
            (c, "gamma", "istqb:glossary"),
        ])

    def test_counts(self):
        r = self._report()
        assert (r.total, r.passed) == (3, 2)
        assert r.rate == pytest.approx(2 / 3)

    def test_violations_are_listed_separately(self):
        # Incomplete and harmful answers fail differently, and a summary
        # that mixes them buries the ones worth acting on.
        r = self._report()
        assert [x.item.id for x in r.violations] == ["B"]

    def test_per_pack_breakdown(self):
        assert self._report().by_pack() == {"process": (1, 1), "naming": (1, 2)}

    def test_per_language_breakdown(self):
        assert self._report().by_lang() == {"en": (1, 2), "uk": (1, 1)}

    def test_route_rate_counts_only_observed_routes(self):
        r = self._report()
        assert r.route_rate == pytest.approx(2 / 3)

    def test_failures_are_retrievable(self):
        assert [x.item.id for x in self._report().failures()] == ["B"]


# ── Load-time validation ─────────────────────────────────────────────

class TestMalformedSetsAreRejectedAtLoad:
    """A skipped item is a test that stops measuring without going red,
    so every one of these is a hard load error rather than a warning."""

    def _write(self, tmp_path, body):
        p = tmp_path / "gs.yaml"
        p.write_text(textwrap.dedent(body), encoding="utf-8")
        return p

    def test_unknown_pack(self, tmp_path):
        p = self._write(tmp_path, """
            items:
              - id: X-1
                pack: made_up
                q: "q?"
                route: pack:process
                require: [[alpha]]
        """)
        with pytest.raises(ev.GoldenSetError, match="unknown pack"):
            ev.load(p)

    def test_no_requirements(self, tmp_path):
        p = self._write(tmp_path, """
            items:
              - id: X-1
                pack: process
                q: "q?"
                route: pack:process
        """)
        with pytest.raises(ev.GoldenSetError, match="no requirements"):
            ev.load(p)

    def test_empty_requirement(self, tmp_path):
        p = self._write(tmp_path, """
            items:
              - id: X-1
                pack: process
                q: "q?"
                route: pack:process
                require: [[]]
        """)
        with pytest.raises(ev.GoldenSetError, match="empty"):
            ev.load(p)

    def test_duplicate_id(self, tmp_path):
        p = self._write(tmp_path, """
            items:
              - id: X-1
                pack: process
                q: "one?"
                route: pack:process
                require: [[alpha]]
              - id: X-1
                pack: process
                q: "two?"
                route: pack:process
                require: [[beta]]
        """)
        with pytest.raises(ev.GoldenSetError, match="duplicate id"):
            ev.load(p)

    def test_unknown_route_namespace(self, tmp_path):
        p = self._write(tmp_path, """
            items:
              - id: X-1
                pack: process
                q: "q?"
                route: magic:process
                require: [[alpha]]
        """)
        with pytest.raises(ev.GoldenSetError, match="namespace"):
            ev.load(p)

    def test_missing_question(self, tmp_path):
        p = self._write(tmp_path, """
            items:
              - id: X-1
                pack: process
                route: pack:process
                require: [[alpha]]
        """)
        with pytest.raises(ev.GoldenSetError, match="no question"):
            ev.load(p)

    def test_a_bare_string_requirement_is_accepted(self, tmp_path):
        # Allowed on purpose: `- alpha` for a single-alternative
        # requirement keeps the YAML readable.
        p = self._write(tmp_path, """
            items:
              - id: X-1
                pack: process
                q: "q?"
                route: pack:process
                require:
                  - alpha
        """)
        loaded = ev.load(p)
        assert loaded[0].require == (("alpha",),)
