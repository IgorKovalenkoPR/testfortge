"""/api/search, /api/hints, /api/search/status, /api/search/reindex
(ADR 0003 §4.5, §4.6, §4.8; task T5).

Every assertion is about what a caller receives — the JSON — and where a
claim is about ranking it names the ref, not a count: a test that checks
"some results came back" passes over a ranking that returns the wrong ones.
"""
from __future__ import annotations

import re
import secrets
import time

import pytest

from engine import db as _db
from engine.search import hooks, indexer, retrieve
from tests._search_seed import seed_golden


@pytest.fixture
def search_on(monkeypatch):
    monkeypatch.setenv("PLATFORM_SEARCH", "1")
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("SEARCH_INDEX_DEBOUNCE_S", "3600")
    monkeypatch.delenv("EMBEDDINGS_PROVIDER", raising=False)
    for var in ("SEARCH_W_LEX", "SEARCH_W_VEC", "SEARCH_BOOST_TITLE",
                "SEARCH_BOOST_OPEN_BUG", "SEARCH_VEC_MIN"):
        monkeypatch.delenv(var, raising=False)
    _db.init_db()
    hooks.install()
    hooks.reset()
    retrieve.clear_caches()
    yield
    hooks.reset()


@pytest.fixture
def shop(search_on, make_project):
    """The golden project, indexed. Searched with ``project_id`` so the
    test reads the same in both auth eras (a project the caller may open)."""
    pid = make_project(f"shop {secrets.token_hex(4)}")
    seed_golden(pid)
    indexer.reindex_project(pid)
    hooks.reset()
    return pid


def _search(client, pid, q, **params):
    params = {"q": q, "project_id": pid, **params}
    resp = client.get("/api/search", query_string=params)
    assert resp.status_code == 200, resp.data
    return resp.get_json()


def _refs(body, n=None):
    return [r["ref"] for r in body["results"][:n]]


# ── Flag ──────────────────────────────────────────────────────────────

class TestFlagOff:
    @pytest.mark.parametrize("method,path", [
        ("get", "/api/search?q=login"),
        ("get", "/api/search?q=log&quick=1"),
        ("get", "/api/hints?page=test_cases"),
        ("get", "/api/search/status"),
        ("post", "/api/search/reindex"),
    ])
    def test_every_endpoint_is_hidden(self, client, monkeypatch, method,
                                      path):
        monkeypatch.delenv("PLATFORM_SEARCH", raising=False)
        resp = getattr(client, method)(path, json={} if method == "post"
                                       else None)
        assert resp.status_code == 404

    def test_flag_without_its_prerequisite_is_off(self, client, monkeypatch):
        monkeypatch.setenv("PLATFORM_SEARCH", "1")
        monkeypatch.delenv("WORKSPACE_DB_FIRST", raising=False)
        assert client.get("/api/search?q=login").status_code == 404


# ── Shapes ────────────────────────────────────────────────────────────

class TestShape:
    def test_full_response(self, client, shop):
        body = _search(client, shop, "login with valid password")
        for key in ("query", "mode", "stale", "stale_projects", "took_ms",
                    "corpus_version", "results", "groups", "group_order",
                    "facets", "filters", "chips", "explain", "scope"):
            assert key in body, key
        assert body["mode"] == "hybrid-py"      # SQLite: Python cosine
        assert body["scope"] == "project"
        assert body["query"]["lang"] == "en"
        top = body["results"][0]
        assert top["ref"] == "TC-001"
        for key in ("doc_id", "kind", "ref", "title", "snippet", "url",
                    "project", "facets", "score", "why", "chunk_no"):
            assert key in top, key
        assert top["project"]["id"] == shop
        why = top["why"]
        for key in ("lex_rank", "vec_rank", "lex_score", "vec_score", "rrf",
                    "matched_terms", "concepts", "typo", "boosts", "mode"):
            assert key in why, key
        assert why["lex_rank"] and why["vec_rank"]
        assert "login" in why["matched_terms"]
        assert "auth" in why["concepts"]
        assert "<mark>" in top["snippet"]

    def test_explanation_reports_pipeline_and_timings(self, client, shop):
        ex = _search(client, shop, "login")["explain"]
        assert set(ex["timings_ms"]) >= {"parse", "embed", "retrieve",
                                         "rerank", "total"}
        assert ex["lexical"] == "python-bm25"
        assert ex["vector"] == "python-cosine"
        assert ex["weights"] == {"lex": 1.0, "vec": retrieve.DEFAULT_W_VEC,
                                 "rrf_k": 60}
        assert ex["filters"]["before"] >= ex["filters"]["after"] > 0

    def test_results_are_grouped_by_kind(self, client, shop):
        body = _search(client, shop, "password")
        assert set(body["group_order"]) >= {"test_case", "bug"}
        for kind, items in body["groups"].items():
            if kind == "istqb_book":
                continue
            assert all(i["kind"] == kind for i in items)
            assert len(items) <= retrieve.GROUP_SIZE

    def test_snippet_escapes_the_body(self, client, shop):
        body = _search(client, shop, "review comment renders html script")
        bug = next(r for r in body["results"] if r["ref"] == "BUG-021")
        # Only <mark> survives as markup; the stored tag is escaped (and
        # its matched word marked inside the escaped text).
        assert re.sub(r"</?mark>", "", bug["snippet"]).count("<") == 0
        assert "&lt;<mark>script</mark>&gt;" in bug["snippet"]

    def test_empty_query_is_an_empty_answer(self, client, shop):
        body = _search(client, shop, "   ")
        assert body["results"] == [] and body["mode"] is None

    def test_kinds_filter(self, client, shop):
        body = _search(client, shop, "password", kinds="bug")
        assert body["results"] and \
            {r["kind"] for r in body["results"]} == {"bug"}


# ── The pipeline, observable through the API ─────────────────────────

class TestPipeline:
    def test_translation_is_explained(self, client, shop):
        body = _search(client, shop, "не працює вхід")
        assert body["query"]["lang"] == "ua"
        assert body["query"]["translated"].get("вхід") == "login"
        assert "BUG-015" in _refs(body, 5)
        labels = [t["label"] for t in body["explain"]["terms"]]
        assert "вхід→login" in labels

    def test_did_you_mean(self, client, shop):
        body = _search(client, shop, "paswword reset")
        assert body["query"]["corrections"] == {"paswword": "password"}
        assert body["query"]["did_you_mean"] == "password reset"
        assert {"TC-006", "TC-007"} & set(_refs(body, 5))
        assert body["chips"][0]["id"] == "did_you_mean"
        assert body["chips"][0]["q"] == "password reset"

    def test_cyrillic_typo_is_corrected_through_the_dictionary(self, client,
                                                                shop):
        body = _search(client, shop, "ркгистрация")
        assert body["query"]["corrections"] == {"ркгистрация": "регистрация"}
        assert {"TC-009", "TC-010", "TC-011"} & set(_refs(body, 5))

    def test_a_known_word_is_not_corrected(self, client, shop):
        # "passwords" is not in the data, but its stem is.
        body = _search(client, shop, "passwords")
        assert body["query"]["corrections"] == {}

    @pytest.mark.parametrize("q,ref", [("TC-18", "TC-018"),
                                       ("bug 15", "BUG-015"),
                                       ("cl14", "CL_014")])
    def test_id_fast_path_is_pinned_first(self, client, shop, q, ref):
        body = _search(client, shop, q)
        assert body["results"][0]["ref"] == ref
        assert "id_exact" in body["results"][0]["why"]["boosts"]

    @pytest.mark.parametrize("q", ["LGN_010", "lgn_010", " LGN_010 "])
    def test_a_teams_own_id_is_pinned_first(self, client, search_on,
                                            make_project, q):
        # Live 2026-10-13: "LGN_010" listed SC2_010 first — the id was
        # split into "lgn" + "010" and the other "010" scored higher.
        pid = make_project(f"ids {secrets.token_hex(4)}")
        _db.save_test_cases(pid, [
            {"id": "SC2_010", "summary": "Checkout 010 total 010 rounding",
             "test_steps": "1. Add item 010", "expected_result": "010"},
            {"id": "LGN_010", "summary": "Login with a valid password",
             "test_steps": "1. Sign in", "expected_result": "Dashboard"},
            {"id": "LGN_011", "summary": "Login with a locked account",
             "test_steps": "1. Sign in", "expected_result": "Refused"}])
        indexer.reindex_project(pid)
        hooks.reset()
        body = _search(client, pid, q, explain=1)
        assert body["results"][0]["ref"] == "LGN_010"
        assert "id_exact" in body["results"][0]["why"]["boosts"]

    @pytest.mark.parametrize("q,want", [
        ("LGN_010", "LGN_010"), ("e2e-2", "E2E-2"), ("SC2_010", "SC2_010"),
        ("TC-18", None), ("bug 15", None),      # the TC/CL/BUG/RUN path
        ("login 010", None), ("LGN", None), ("010", None), ("_010", None)])
    def test_ref_query_parse(self, q, want):
        from engine.search.text import parse_ref_query
        assert parse_ref_query(q) == want

    def test_rrf_weights_are_configurable(self, client, shop, monkeypatch):
        monkeypatch.setenv("SEARCH_W_VEC", "0")
        body = _search(client, shop, "login")
        assert all(r["why"]["rrf"] == pytest.approx(
            1 / (60 + r["why"]["lex_rank"]), abs=1e-6)
            for r in body["results"] if r["why"]["lex_rank"])

    def test_lexical_mode_when_no_embedder(self, client, shop, monkeypatch):
        monkeypatch.setenv("EMBEDDINGS_PROVIDER", "voyage")   # reserved
        body = _search(client, shop, "login with valid password")
        assert body["mode"] == "lexical"
        assert body["results"][0]["ref"] == "TC-001"
        assert body["results"][0]["why"]["vec_rank"] is None

    def test_lexical_mode_over_the_python_vector_cap(self, client, shop,
                                                     monkeypatch):
        monkeypatch.setenv("SEARCH_PY_VECTOR_MAX_DOCS", "10")
        body = _search(client, shop, "login")
        assert body["mode"] == "lexical"
        assert body["explain"]["vector"] == "skipped: too many documents"


# ── Filters and relaxing ──────────────────────────────────────────────

class TestFilters:
    def test_facet_filter_applies(self, client, shop):
        body = _search(client, shop, "payment", kinds="bug", status="open")
        assert body["results"]
        assert all(r["kind"] == "bug" and
                   r["facets"]["status"].lower() == "open"
                   for r in body["results"])
        assert body["filters"]["applied"] == {"status": "open"}
        assert body["filters"]["relaxed"] == []

    def test_a_filter_that_empties_the_list_is_relaxed_and_said_so(
            self, client, shop):
        body = _search(client, shop, "login", status="no-such-status")
        assert body["results"], "relaxed: results come back"
        relaxed = body["filters"]["relaxed"]
        assert relaxed and relaxed[0]["filter"] == "status"
        assert relaxed[0]["value"] == "no-such-status"
        assert "status" not in body["filters"]["applied"]
        assert body["explain"]["filters"]["relaxed"] == relaxed

    def test_a_kind_nothing_matches_is_relaxed(self, client, shop):
        body = _search(client, shop, "login", kinds="run")
        assert body["results"]
        assert body["filters"]["relaxed"][0]["filter"] == "kinds"

    def test_priority_filter(self, client, shop):
        body = _search(client, shop, "payment", priority="critical")
        assert body["results"]
        assert all(r["facets"]["priority"].lower() == "critical"
                   for r in body["results"])


# ── Chips ─────────────────────────────────────────────────────────────

class TestChips:
    def test_only_open_bugs(self, client, shop):
        body = _search(client, shop, "session logout")
        chip = next(c for c in body["chips"] if c["id"] == "open_bugs")
        assert chip == {"id": "open_bugs", "label": "Only open bugs",
                        "q": "session logout", "kinds": ["bug"],
                        "filters": {"status": "open"}}

    def test_linked_bugs_chip_and_following_it(self, client, shop):
        body = _search(client, shop, "TC-018")
        chip = next(c for c in body["chips"]
                    if c["id"] == "linked:TC-018")
        assert chip["label"] == "Linked bugs for TC-018 (1)"
        follow = _search(client, shop, chip["q"], kinds="bug",
                         linked=chip["linked"])
        assert _refs(follow) == ["BUG-004"]
        assert follow["filters"]["applied"] == {"linked": "TC-018"}

    def test_linked_test_for_a_bug(self, client, shop):
        body = _search(client, shop, "BUG-004")
        chip = next(c for c in body["chips"] if c["id"] == "linked:BUG-004")
        assert chip["q"] == "TC-018" and chip["kinds"] == ["test_case"]

    def test_chips_are_deterministic(self, client, shop):
        a = _search(client, shop, "card payment")["chips"]
        b = _search(client, shop, "card payment")["chips"]
        assert a == b and len(a) <= 4


# ── Quick mode (typeahead) ────────────────────────────────────────────

class TestQuick:
    def test_prefix_and_shape(self, client, shop):
        body = _search(client, shop, "regist", quick=1)
        assert body["quick"] is True and body["mode"] == "lexical"
        assert "explain" not in body and "chips" not in body
        assert {"TC-009", "TC-010", "TC-011"} <= set(_refs(body))
        assert all("why" not in r for r in body["results"])
        assert len(body["results"]) <= retrieve.QUICK_LIMIT
        for items in body["groups"].values():
            assert len(items) <= retrieve.QUICK_PER_GROUP

    def test_id(self, client, shop):
        assert _refs(_search(client, shop, "TC-45", quick=1))[0] == "TC-045"

    def test_cyrillic_prefix_reaches_english_titles(self, client, shop):
        body = _search(client, shop, "кошик", quick=1)
        assert {"TC-012", "TC-013", "TC-014"} & set(_refs(body))

    def test_never_checks_lag_or_queues(self, client, shop, monkeypatch):
        def boom(_pid):
            raise AssertionError("quick=1 must not run the lag check")
        monkeypatch.setattr(hooks, "check_lag", boom)
        _db.save_test_cases(shop, [{"id": "TC-900", "summary": "new"}])
        hooks.reset()
        body = _search(client, shop, "login", quick=1)
        assert body["results"] and "stale" not in body
        assert shop not in hooks.pending()


# ── Stale ─────────────────────────────────────────────────────────────

class TestStale:
    def test_behind_index_says_stale_and_schedules(self, client, shop):
        assert _search(client, shop, "login")["stale"] is False
        _db.save_bug(shop, {"id": "BUG-900", "title": "Brand new defect"})
        hooks.reset()                  # forget what the ORM hook queued
        body = _search(client, shop, "login")
        assert body["stale"] is True and body["stale_projects"] == [shop]
        assert shop in hooks.pending(), "the lag check queued a reindex"
        hooks.drain()
        after = _search(client, shop, "brand new defect")
        assert after["stale"] is False
        assert after["results"][0]["ref"] == "BUG-900"

    def test_never_indexed_project_is_stale(self, client, search_on,
                                            make_project):
        pid = make_project(f"fresh {secrets.token_hex(4)}")
        body = _search(client, pid, "anything")
        assert body["stale"] is True


# ── Hints ─────────────────────────────────────────────────────────────

class TestHints:
    def test_page_tips_link_to_the_guide(self, client, search_on):
        body = client.get("/api/hints?page=test_cases&lang=en").get_json()
        assert body["page"] == "test_cases"
        tips = [h for h in body["hints"] if h["type"] == "tip"]
        assert tips and all(h["learn_more"]["url"].startswith("/guide#")
                            for h in tips)
        guide = next(h for h in body["hints"] if h["type"] == "guide")
        assert guide["url"] == "/guide#test-cases"
        assert guide["topic"] == "Test Cases" and guide["excerpt"]

    @pytest.mark.parametrize("alias", ["test_cases_page", "/test-cases",
                                       "test-cases"])
    def test_aliases(self, client, search_on, alias):
        assert client.get("/api/hints", query_string={"page": alias}) \
            .get_json()["page"] == "test_cases"

    @pytest.mark.parametrize("page", ["test_cases", "checklist",
                                      "bug_reports", "executions",
                                      "estimation", "automation", "settings"])
    def test_every_curated_page_in_both_languages(self, client, search_on,
                                                  page):
        for lang in ("en", "ua"):
            body = client.get("/api/hints", query_string={
                "page": page, "lang": lang}).get_json()
            assert body["page"] == page and body["lang"] == lang
            assert [h for h in body["hints"] if h["type"] == "tip"]
            assert [h for h in body["hints"] if h["type"] == "guide"]

    def test_unknown_page_gets_default_tips(self, client, search_on):
        body = client.get("/api/hints?page=nowhere").get_json()
        assert body["page"] == "_default" and body["hints"]

    def test_similar_items_for_a_context_document(self, client, shop):
        _db.save_test_cases(shop, [
            {"id": "TC-100", "summary": "Login with valid email and password",
             "test_steps": "1. Open the login page\n2. Enter a valid email "
                           "and password\n3. Click Sign in"},
            {"id": "TC-101", "summary": "Login with valid email and password "
                                        "on mobile",
             "test_steps": "1. Open the login page\n2. Enter a valid email "
                           "and password\n3. Click Sign in"},
            {"id": "TC-102", "summary": "Export invoices as PDF"}])
        indexer.reindex_project(shop)
        body = client.get("/api/hints", query_string={
            "page": "test_cases", "project_id": shop,
            "context": "test_case:TC-100"}).get_json()
        similar = [h for h in body["hints"] if h["type"] == "similar"]
        assert similar and similar[0]["ref"] == "TC-101"
        assert similar[0]["label"] == "Possible duplicate"
        assert "TC-102" not in [h.get("ref") for h in similar]


# ── Status, reindex, CSRF, readyz ─────────────────────────────────────

class TestAdmin:
    def test_status(self, client, shop):
        body = client.get("/api/search/status").get_json()
        for key in ("fts", "pgvector", "provider", "vec_version", "docs",
                    "vectors", "pending_projects", "last_indexed_at",
                    "stale_projects"):
            assert key in body, key
        assert body["fts"] is False and body["pgvector"] is False
        assert body["provider"] == "hash"

    def test_reindex_queues_a_job_that_runs(self, client, shop):
        _db.save_bug(shop, {"id": "BUG-901", "title": "Queued reindex"})
        hooks.reset()
        resp = client.post("/api/search/reindex", json={"project_id": shop})
        assert resp.status_code == 202, resp.data
        job_id = resp.get_json()["job_id"]
        from engine.job_queue import DONE, FAILED, get_queue
        deadline = time.monotonic() + 30
        job = get_queue().get(job_id)
        while job.status not in (DONE, FAILED) and \
                time.monotonic() < deadline:
            time.sleep(0.05)
            job = get_queue().get(job_id)
        assert job.status == DONE, job.error
        assert _search(client, shop, "queued reindex")["results"][0]["ref"] \
            == "BUG-901"

    def test_reindex_of_a_project_you_cannot_open_is_403(self, client,
                                                         search_on):
        foreign = _db.upsert_project(f"x {secrets.token_hex(4)}",
                                     owner_sid="someone-else",
                                     org_id=_db.create_organization(
                                         f"other {secrets.token_hex(3)}"))
        resp = client.post("/api/search/reindex",
                           json={"project_id": foreign})
        assert resp.status_code == 403

    def test_readyz_reports_search_capabilities(self, client):
        body = client.get("/readyz").get_json()
        assert body["search"] == {"fts": False, "pgvector": False,
                                  "provider": "hash"}


class TestCsrf:
    """The reindex POST is a session-authenticated JSON write: it keeps
    CSRF protection and the browser sends ``X-CSRFToken``."""

    @pytest.fixture(autouse=True)
    def _csrf_on(self, app, monkeypatch):
        monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", True)

    def test_csrf_is_really_on(self, app):
        assert app.config["WTF_CSRF_ENABLED"] is True

    def test_post_without_a_token_is_refused(self, client, search_on):
        resp = client.post("/api/search/reindex", json={})
        assert resp.status_code == 400

    def test_post_with_the_page_token_is_accepted(self, client, search_on):
        page = client.get("/guide").get_data(as_text=True)
        token = re.search(r'<meta name="csrf-token" content="([^"]+)"',
                          page).group(1)
        resp = client.post("/api/search/reindex", json={},
                           headers={"X-CSRFToken": token})
        assert resp.status_code == 202, resp.data

    def test_get_endpoints_need_no_token(self, client, search_on):
        assert client.get("/api/search?q=x").status_code == 200


# ── Policy and Postgres statements ────────────────────────────────────

def test_endpoints_are_classified():
    from engine.route_policy import POLICY
    assert POLICY["api_search"] == "user"
    assert POLICY["api_hints"] == "user"
    assert POLICY["api_search_status"] == "admin"
    assert POLICY["api_search_reindex"] == "admin"


class TestPostgresStatements:
    """The PG paths cannot run on SQLite; their SQL is compiled against the
    PG dialect so the scope clause is provably in them."""

    def _sql(self, stmt) -> str:
        from sqlalchemy.dialects import postgresql
        return str(stmt.compile(dialect=postgresql.dialect()))

    def test_fts_statement_carries_the_scope_clause(self):
        sql = self._sql(retrieve.fts_statement(["p1", "p2"], ["bug"],
                                               "login | pay:*"))
        assert "search_doc.scope" in sql and "search_doc.project_id IN" in sql
        assert "tsv @@ to_tsquery('simple'" in sql
        assert "search_doc.kind IN" in sql

    RAW_BIND = re.compile(r"(?<![:\w]):[a-z_]\w*")

    @pytest.mark.parametrize("build,bind", [
        (lambda: retrieve.fts_statement(["p1"], None, "login | pay:*"), "tsq"),
        (lambda: retrieve.fts_statement(["p1"], None, "base | url",
                                        weights=[("url", 4.5)]), "w0"),
        (lambda: retrieve.vector_statement(["p1"], None, "[0.1]"), "qv"),
        (lambda: retrieve.corpus_stats_statement(["p1"], None, ["login"]),
         "q0"),
    ], ids=["fts", "fts_idf_pool", "vector", "corpus_stats"])
    def test_every_parameter_is_bound_not_left_in_the_sql(self, build, bind):
        """literal_column("… :tsq …") compiles the colon-name verbatim, and
        psycopg2 rejects it: every Postgres FTS query failed and search fell
        back to Python BM25 unseen until a live explain said so
        (2026-10-15). A bound parameter compiles to %(name)s."""
        compiled = retrieve_pg_compile(build())
        sql = str(compiled)
        assert not self.RAW_BIND.findall(sql), sql
        assert bind in compiled.params and f"%({bind})s" in sql

    def test_fts_statement_returns_the_lex_bm25_reorders(self):
        sql = self._sql(retrieve.fts_statement(["p1"], None, "login"))
        assert "search_doc.lex_title" in sql and "search_doc.lex_body" in sql
        assert "LIMIT" in sql

    def test_corpus_stats_carry_the_scope_clause(self):
        """The document frequencies BM25 ranks the pool with must count the
        caller's scope and nothing wider, or another tenant's data would
        shape this one's ranking."""
        stmt = retrieve.corpus_stats_statement(["p1", "p2"], ["bug"],
                                               ["login", "об'єкт"])
        sql = self._sql(stmt)
        assert "search_doc.scope" in sql and "search_doc.project_id IN" in sql
        assert "search_doc.kind IN" in sql
        assert sql.count("FILTER (WHERE search_doc.tsv @@") == 2
        params = stmt.compile().params
        assert params["q0"] == "login" and params["q1"] == "об & єкт"

    def test_vector_statement_carries_the_scope_clause(self):
        sql = self._sql(retrieve.vector_statement(["p1"], None, "[0.1]"))
        assert "search_doc.scope" in sql and "search_doc.project_id IN" in sql
        assert "<=> CAST(" in sql

    def test_tsquery_text(self):
        terms = [retrieve.Term("login", "login", "query"),
                 retrieve.Term("об'єкт", "x", "query"),
                 retrieve.Term("regist", "regist", "query")]
        assert retrieve.tsquery_text(terms) == "login | об | єкт | regist:*"


def retrieve_pg_compile(stmt):
    from sqlalchemy.dialects import postgresql
    return stmt.compile(dialect=postgresql.dialect())
