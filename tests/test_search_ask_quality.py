"""What Ask is given to read — ADR 0003 gate 3, run live on 2026-10-15.

Twenty questions went to testforge.qarea.org. Every answer was grounded, and
in four of them the model honestly said the materials did not cover the
question — while the materials did. Each was a retrieval miss, reproduced
here on the real Guide and ISTQB index, the golden web-shop project and the
two bugs the live executor files on an out-of-memory exit:

* "чек-лист" — the hyphen split it into "чек" + "лист" (a letter, so
  "email"), and the Guide only ever writes "чек-ліст";
* "Base URL" in a Ukrainian question — one Latin pair against two
  Ukrainian words that each count twice (own stem and translation);
* "live executor stopping early" — the bug titled with those words was
  the best lexical hit and came 12th, because it had no hash-vector
  neighbour and every document in both lists outranked it;
* "нестачу пам'яті" — no translation, so nothing tied the question to
  "out-of-memory".

Plus two Guide gaps (project Export, Move artefacts) and "Test Execution vs
Automation QA" answered from the ISTQB syllabus instead of the two module
pages.

Ask reads :data:`engine.search.answer.MAX_ITEMS` results, so that is the
line each expected item has to make.
"""
from __future__ import annotations

import dataclasses
import secrets

import pytest

from engine import db as _db
from engine.bug_report import create_bug_from_early_exit
from engine.search import answer, hooks, indexer, retrieve
from engine.search.text import analyse, search_tokens
from tests._search_seed import seed_golden

#: Gate-3 questions verbatim, with what the answer needed.
MISSES = [
    ("Як створити чек-лист для проєкту?", {"ua:checklist"}),
    ("Як налаштувати Base URL для проєкту?", {"ua:test-execution"}),
    ("What bugs mention the live executor stopping early?", {"OOM"}),
    ("Чому прогін зупинився через нестачу пам’яті?", {"OOM"}),
    ("How do I export a project?", {"en:projects"}),
    ("How do I move test cases to another project?", {"en:projects"}),
    ("What is the difference between Test Execution and Automation QA?",
     {"en:test-execution", "en:automation"}),
]


@pytest.fixture(scope="module")
def corpus():
    mp = pytest.MonkeyPatch()
    for name, value in (("PLATFORM_SEARCH", "1"), ("WORKSPACE_DB_FIRST", "1"),
                        ("SEARCH_INDEX_DEBOUNCE_S", "3600")):
        mp.setenv(name, value)
    for name in ("EMBEDDINGS_PROVIDER", "SEARCH_W_LEX", "SEARCH_W_VEC",
                 "SEARCH_BOOST_TITLE", "SEARCH_BOOST_OPEN_BUG",
                 "SEARCH_BOOST_PHRASE", "SEARCH_BOOST_MODULE",
                 "SEARCH_VEC_MIN"):
        mp.delenv(name, raising=False)
    _db.init_db()
    hooks.install()
    indexer.index_global(force=True)
    pid = _db.upsert_project(f"askq {secrets.token_hex(4)}")
    seed_golden(pid)
    oom = set()
    for run, total in (("r5", 120), ("r21", 300)):
        bug = dataclasses.asdict(create_bug_from_early_exit(
            "oom_budget_exceeded_after_tcs", run_id=run,
            base_url="https://shop.test", rss_mb=443, budget_mb=382,
            cases_done=3, cases_total=total))
        bug["id"] = f"BUG-{900 + total}"
        _db.save_bug(pid, bug)
        oom.add(bug["id"])
    indexer.reindex_project(pid)
    hooks.reset()
    retrieve.clear_caches()
    yield {"pid": pid, "oom": oom, "mp": mp}
    hooks.reset()
    mp.undo()


def _refs(corpus, q: str, n: int = answer.MAX_ITEMS) -> list[str]:
    body = retrieve.search(retrieve.SearchParams(
        q=q, allowed=[corpus["pid"]], limit=n, explain=False, istqb=False))
    return [r["ref"] for r in body["results"]]


class TestTheGateThreeMisses:
    @pytest.mark.parametrize("q,want", MISSES, ids=[m[0][:40] for m in MISSES])
    def test_the_material_reaches_the_model(self, corpus, q, want):
        want = (want - {"OOM"}) | (corpus["oom"] if "OOM" in want else set())
        got = _refs(corpus, q)
        assert want <= set(got), (q, sorted(want - set(got)), got)

    def test_the_oom_bugs_lead_for_the_english_phrasing(self, corpus):
        """Not merely present: the question names them."""
        got = _refs(corpus, "What bugs mention the live executor stopping "
                            "early?")
        assert set(got[:2]) == corpus["oom"], got

    def test_every_spelling_of_checklist_finds_the_checklist_page(
            self, corpus):
        for q in ("Як створити чек-лист для проєкту?",
                  "Як створити чеклист для проєкту?",
                  "Як створити чек-ліст для проєкту?",
                  "Как создать чек-лист для проекта?"):
            assert "ua:checklist" in _refs(corpus, q) or \
                "en:checklist" in _refs(corpus, q), q


class TestAnalysis:
    @pytest.mark.parametrize("raw", ["чек-лист", "чеклист", "чек-ліст",
                                     "чек лист", "Чек-Лист"])
    def test_checklist_is_one_word_however_it_is_spelt(self, raw):
        a = analyse(raw)
        assert a.tokens == ["чекліст"], a.tokens
        assert a.mapping == {"чекліст": "checklist"}

    def test_inflections_survive_the_join(self):
        a = analyse("пункти чек-ліста")
        assert "чекліста" in a.tokens and a.mapping["чекліста"] == "checklist"

    def test_a_check_is_not_a_checklist(self):
        assert analyse("чек оплати").tokens == ["чек", "оплати"]

    def test_out_of_memory_in_ukrainian(self):
        a = analyse("Чому прогін зупинився через нестачу пам’яті?")
        assert a.mapping["пам'яті"] == "memory"
        assert a.mapping["зупинився"] == "stopped"
        assert "через" not in search_tokens("через нестачу")


class TestBoosts:
    def test_phrases_are_adjacent_query_stems(self):
        pq = retrieve.parse_query("Як налаштувати Base URL для проєкту?")
        assert " base url " in pq.phrases

    def test_module_names_of_two_words_or_more(self):
        assert retrieve.module_phrase("Guide · Test Execution") == \
            " test execut "
        assert retrieve.module_phrase("Guide · Automation QA") == " autom qa "
        assert retrieve.module_phrase("Guide · Projects") is None
        assert retrieve.module_phrase("Guide · Runs") is None

    def test_phrase_boost_and_vector_weight_lift_base_url(self, corpus):
        """With both reverted (no phrase boost, vectors at full weight) the
        Ukrainian Base URL question loses the page that explains it — so the
        two changes, not luck, are what the test above sees."""
        q = "Як налаштувати Base URL для проєкту?"
        corpus["mp"].setenv("SEARCH_BOOST_PHRASE", "0")
        corpus["mp"].setenv("SEARCH_W_VEC", "1")
        try:
            off = _refs(corpus, q)
        finally:
            corpus["mp"].delenv("SEARCH_BOOST_PHRASE")
            corpus["mp"].delenv("SEARCH_W_VEC")
        assert "ua:test-execution" not in off, off
        assert "ua:test-execution" in _refs(corpus, q)


class TestPostgresRescoring:
    """On Postgres the FTS index only finds a pool and BM25 orders it with
    the scope's statistics. The pool cannot be run here; what can be is the
    claim that makes it safe — scored with the corpus figures, a pool ranks
    its documents exactly as BM25 over the whole scope does."""

    def test_a_pool_with_corpus_stats_scores_like_the_whole_scope(
            self, corpus):
        pq = retrieve.parse_query("live executor stopped early memory")
        cands = retrieve._load_candidates([corpus["pid"]], None,
                                          with_vectors=False)
        full, _m = retrieve._bm25_py(cands, pq.terms, prefix_last=False)
        stems = [t.stem for t in pq.terms]
        df = {s: sum(1 for c in cands
                     if s in c.lex_title.split() or s in c.lex_body.split())
              for s in stems}
        avgdl = sum(len(c.lex_title.split()) * retrieve.TITLE_WEIGHT
                    + len(c.lex_body.split()) for c in cands) / len(cands)
        hit_ids = {d for d, _s in full[:40]}
        pool = [c for c in cands if c.id in hit_ids]
        assert len(pool) < len(cands) // 4
        part, _m = retrieve._bm25_py(pool, pq.terms, prefix_last=False,
                                     corpus=(len(cands), df, avgdl))
        assert [d for d, _s in part] == [d for d, _s in full[:40]]
        assert dict(part) == pytest.approx(dict(full[:40]))

    def test_without_corpus_stats_the_pool_ranks_differently(self, corpus):
        """The control: the statistics are what make the two agree."""
        pq = retrieve.parse_query("live executor stopped early memory")
        cands = retrieve._load_candidates([corpus["pid"]], None,
                                          with_vectors=False)
        full, _m = retrieve._bm25_py(cands, pq.terms, prefix_last=False)
        pool = [c for c in cands if c.id in {d for d, _s in full[:40]}]
        part, _m = retrieve._bm25_py(pool, pq.terms, prefix_last=False)
        assert dict(part) != pytest.approx(dict(full[:40]))
