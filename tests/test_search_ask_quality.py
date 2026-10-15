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

    def test_base_url_reaches_the_model(self, corpus):
        """The Ukrainian Base URL question: most of what the model reads
        must say "Base URL" — and with the two ranking changes reverted
        (no phrase boost, vectors at full weight) most of it does not, so
        the changes, not luck, are what this sees."""
        q = "Як налаштувати Base URL для проєкту?"
        assert _base_url_items(corpus, q) >= 4
        corpus["mp"].setenv("SEARCH_BOOST_PHRASE", "0")
        corpus["mp"].setenv("SEARCH_W_VEC", "1")
        try:
            assert _base_url_items(corpus, q) <= 3
        finally:
            corpus["mp"].delenv("SEARCH_BOOST_PHRASE")
            corpus["mp"].delenv("SEARCH_W_VEC")


def _base_url_items(corpus, q: str) -> int:
    body = retrieve.search(retrieve.SearchParams(
        q=q, allowed=[corpus["pid"]], limit=answer.MAX_ITEMS, explain=False,
        istqb=False))
    items = answer.load_items(body["results"], [corpus["pid"]])
    return sum(1 for i in items if "Base URL" in i.body)


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


class TestWhatTheModelIsHanded:
    """Found re-running gate 3 on production after the deploy."""

    def test_the_chunk_sent_is_the_one_that_answers(self, corpus):
        """"How do I move test cases to another project?" ranked the
        Projects page first on its opening chunk, and the model, handed
        that chunk, said moving was not described: the Move section had
        never been indexed (see TestTheWholeGuideIsIndexed)."""
        body = retrieve.search(retrieve.SearchParams(
            q="How do I move test cases to another project?",
            allowed=[corpus["pid"]], limit=answer.MAX_ITEMS, explain=False,
            istqb=False))
        hit = next(r for r in body["results"] if r["ref"] == "en:projects")
        q = "How do I move test cases to another project?"
        item = answer.load_items([hit], [corpus["pid"]], question=q)[0]
        assert "Move artefacts" in item.body, item.body[:300]
        # What the model reads, not merely what was retrieved: the first
        # 400 characters of this chunk are the end of the Export section.
        line = item.line()
        assert "Dashboard card" in line and "Move artefacts" in line, line
        assert len(line) < answer.EXCERPT_CHARS + 120

    def test_the_answering_page_leads(self, corpus):
        q = "How do I move test cases to another project?"
        refs = _refs(corpus, q)
        assert refs[0] == "en:projects", refs

    def test_a_word_naming_the_kind_counts_for_the_gate(self, corpus):
        """"What does the registration checklist cover?" retrieved exactly
        the registration checklist items — whose text never says
        "checklist" — and the gate refused it at 1 word of 3."""
        body = retrieve.search(retrieve.SearchParams(
            q="What does the registration checklist cover?",
            allowed=[corpus["pid"]], kinds=["checklist_item"],
            limit=answer.MAX_ITEMS, explain=False, istqb=False))
        # Checklist items only: nothing else can supply the word.
        assert {r["kind"] for r in body["results"]} == {"checklist_item"}
        assert body["results"]
        assert answer.coverage(body["results"]) >= answer.min_coverage(), [
            (r["ref"], r["covered"], r["words"]) for r in body["results"]]

    def test_nonsense_is_still_refused(self, corpus):
        body = retrieve.search(retrieve.SearchParams(
            q="зелений слон танцює на Марсі ксилофоном",
            allowed=[corpus["pid"]], limit=answer.MAX_ITEMS, explain=False,
            istqb=False))
        assert not answer.is_grounded(body["results"])


class TestTheWholeGuideIsIndexed:
    """Every Guide section reaches the index to its last word. Clipped at
    BODY_MAX like user data, eight sections lost their tail — Test
    Execution 5,000 characters of 9,000 — and the re-run of gate 3 on
    production answered "moving is not described" from a Projects page
    whose Move section had never been indexed."""

    def test_the_end_of_every_section_is_in_some_chunk(self):
        drafts = list(indexer._iter_guide())
        assert len({d.ref for d in drafts}) >= 20
        long_ones = 0
        for lang, path in indexer._guide_files():
            src = path.read_text(encoding="utf-8")
            for section, fragment in indexer._TEMPLATE.findall(src):
                text = indexer._html_text(fragment)
                long_ones += len(text) > indexer.BODY_MAX
                tail = " ".join(text.split())[-60:]
                chunks = " ".join(" ".join(d.body.split()) for d in drafts
                                  if d.ref == f"{lang}:{section}")
                assert tail in chunks, (lang, section, len(text))
        # The sizes that made this a defect are still there to test.
        assert long_ones >= 4


class TestTheExcerpt:
    def test_the_window_holding_most_query_words_wins(self):
        filler = "Nothing relevant is said in this sentence at all. " * 30
        body = filler + "To move test cases, activate the project first."
        got = answer.excerpt(body, answer.question_stems(
            "How do I move test cases?"))
        assert "move test cases" in got and len(got) <= answer.EXCERPT_CHARS

    def test_a_short_body_is_whole(self):
        assert answer.excerpt("Short body.", {"short"}) == "Short body."

    def test_without_a_question_the_line_is_the_opening(self):
        item = answer.Item(doc_id=1, ref="x", kind="guide", title="T",
                           body="a " * 500 + "move", url=None, project_id=None,
                           content_hash="h", score=0.0)
        assert "move" not in item.line()


def test_ask_hands_the_model_the_excerpt_not_the_opening(corpus, monkeypatch):
    """Through the whole of ask_events, with the model faked: the prompt
    for the Move question must carry the Move card, which sits past the
    first 400 characters of its chunk."""
    from tests.test_search_ask import FakeLLM
    fake = FakeLLM()
    monkeypatch.setattr(answer, "_client_factory", fake.factory)
    monkeypatch.setenv("PLATFORM_SEARCH_AI", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-" + "x" * 30)
    for name in ("RAG_MIN_GROUNDING", "RAG_MIN_COVERAGE", "ANTHROPIC_MODEL",
                 "LLM_MODEL_FOR_SEARCH_ANSWER", "MODEL_SWITCHER"):
        monkeypatch.delenv(name, raising=False)
    # The daily cap and the monthly budget count usage rows in the worker's
    # database, which other Ask tests on the same xdist worker fill; this
    # test is about the prompt, not those gates.
    from engine import llm_client
    monkeypatch.setattr(answer, "over_daily_cap", lambda **_kw: False)
    monkeypatch.setattr(llm_client, "check_budget", lambda *_a, **_kw: None)
    answer.cap_gate.reset()
    answer.limiter.reset()
    q = f"How do I move test cases to another project? {secrets.token_hex(2)}"
    events = list(answer.ask_events(answer.AskRequest(
        q=q, allowed=[corpus["pid"]], lang="en")))
    assert fake.calls, [e.get("code") for e in events if e["type"] == "error"]
    assert "Dashboard card Move artefacts" in fake.prompt, fake.prompt[:600]


def test_the_url_question_gets_the_paragraph_that_answers_it(corpus):
    """Two paragraphs of one Guide chunk carry the same question words; a
    400-character window handed the model the url_pattern one and it said
    URL generation was not described (gate 3, 2026-10-15)."""
    q = "How do I generate test cases from a URL?"
    body = retrieve.search(retrieve.SearchParams(
        q=q, allowed=[corpus["pid"]], limit=answer.MAX_ITEMS, explain=False,
        istqb=False))
    items = answer.load_items(body["results"], [corpus["pid"]], question=q)
    lines = " ".join(i.line() for i in items)
    assert "Paste a URL on its own line" in lines
    packed = answer.pack_prompt(q, items, lang="en")
    assert packed.dropped == 0 and packed.input_tokens <=         answer.INPUT_TOKEN_BUDGET
