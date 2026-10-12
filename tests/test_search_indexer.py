"""The search indexer (ADR 0003 §4.3, §4.4 layer 3, §7.3).

Every test asserts what ended up in ``search_doc`` — the rows, their keys,
their tenancy columns, whether a row was *rewritten* — rather than that the
indexer returned a count. A counter that says "0 upserted" while the rows
were rewritten is the green-test-for-the-wrong-property trap; so the
"unchanged" tests compare row ids and ``indexed_at`` before and after.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import pytest

from engine import db as _db
from engine.db import SearchDoc, SearchScopeState
from engine.search import hooks, indexer
from engine.search.embed import from_blob, get_embedder


# ── harness ──────────────────────────────────────────────────────────

@pytest.fixture
def search_on(monkeypatch):
    monkeypatch.setenv("PLATFORM_SEARCH", "1")
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    # Timers never fire inside a test; tests drain explicitly.
    monkeypatch.setenv("SEARCH_INDEX_DEBOUNCE_S", "3600")
    _db.init_db()
    hooks.install()
    hooks.reset()
    yield
    hooks.reset()


def _project(org_id: str | None = None, name: str = "search") -> str:
    return _db.upsert_project(f"{name} {secrets.token_hex(4)}", org_id=org_id)


def _docs(pid: str) -> dict[tuple[str, str, int], dict]:
    with _db.session_scope() as sess:
        rows = sess.query(SearchDoc).filter(SearchDoc.project_id == pid).all()
        return {(r.kind, r.ref, r.chunk_no): {
            "id": r.id, "doc_key": r.doc_key, "scope": r.scope,
            "org_id": r.org_id, "project_id": r.project_id,
            "title": r.title, "body": r.body, "lex_title": r.lex_title,
            "lex_body": r.lex_body, "facets": r.facets, "url": r.url,
            "hash": r.content_hash, "vec_version": r.vec_version,
            "vec_blob": r.vec_blob, "indexed_at": r.indexed_at,
            "lang": r.lang} for r in rows}


def _state(key: str) -> dict | None:
    with _db.session_scope() as sess:
        st = sess.get(SearchScopeState, key)
        if st is None:
            return None
        return {"version": st.version, "indexed_at": st.indexed_at,
                "doc_count": st.doc_count, "vec_version": st.vec_version,
                "last_error": st.last_error, "vocab": st.vocab_json}


TC = {"id": "TC-001", "summary": "Login fails with a valid password",
      "preconditions": "user exists", "test_steps": "1. open /login\n2. submit",
      "expected_result": "the dashboard opens", "priority": "High",
      "status": "Unchecked", "section": "Auth"}


# ── chunking (pure) ──────────────────────────────────────────────────

class TestChunking:
    def test_a_short_body_is_one_chunk_unchanged(self):
        body = "x " * 700
        assert indexer.chunk_body(body) == [body]

    def test_a_long_body_is_split_with_overlap_on_boundaries(self):
        sentences = [f"Sentence number {i} ends here." for i in range(120)]
        body = " ".join(sentences)
        chunks = indexer.chunk_body(body)
        assert len(chunks) > 1
        assert all(len(c) <= indexer.CHUNK_SIZE for c in chunks)
        # Every window but the last ends on a sentence boundary …
        assert all(c.endswith(".") for c in chunks[:-1])
        # … and each one repeats the tail of the previous one.
        for prev, nxt in zip(chunks, chunks[1:]):
            assert prev[-40:].split()[-1] in nxt
        # Nothing is lost: every sentence is in some chunk.
        joined = " ".join(chunks)
        assert all(s in joined for s in sentences)

    def test_text_with_no_boundaries_still_terminates(self):
        body = "a" * 5000
        chunks = indexer.chunk_body(body)
        assert 5 <= len(chunks) <= 10
        assert sum(len(c) for c in chunks) >= 5000

    def test_title_is_repeated_and_body_clipped(self, search_on):
        pid = _project()
        long_steps = "\n".join(f"Step {i}: click the button labelled {i}."
                               for i in range(400))
        _db.save_test_cases(pid, [dict(TC, test_steps=long_steps)])
        indexer.reindex_project(pid)
        docs = _docs(pid)
        chunks = sorted(k for k in docs if k[0] == "test_case")
        assert len(chunks) >= 3
        assert [k[2] for k in chunks] == list(range(len(chunks)))
        assert {docs[k]["title"] for k in chunks} == {
            "TC-001 · Login fails with a valid password"}
        assert all(len(docs[k]["body"]) <= indexer.BODY_MAX for k in chunks)
        # Clipped at 4,000 characters before chunking: the last steps of a
        # 15 KB body are not indexed at all.
        assert not any("Step 399" in docs[k]["body"] for k in chunks)


# ── each kind: create, edit, delete ──────────────────────────────────

class TestKinds:
    def test_test_case_lifecycle(self, search_on):
        pid = _project()
        _db.save_test_cases(pid, [TC])
        indexer.reindex_project(pid)
        doc = _docs(pid)[("test_case", "TC-001", 0)]
        assert doc["title"] == "TC-001 · Login fails with a valid password"
        assert "open /login" in doc["body"]
        assert doc["facets"]["priority"] == "High"
        assert doc["url"] == "/test-cases#TC-001"
        assert doc["doc_key"] == f"project:{pid}:test_case:TC-001:0"
        assert "login" in doc["lex_title"]
        emb = get_embedder()
        assert doc["vec_version"] == emb.version
        vec = from_blob(doc["vec_blob"])
        assert len(vec) == emb.dims
        assert abs(sum(x * x for x in vec) - 1.0) < 1e-3

        _db.save_test_cases(pid, [dict(TC, summary="Logout keeps the session")])
        indexer.reindex_project(pid)
        edited = _docs(pid)[("test_case", "TC-001", 0)]
        assert edited["title"] == "TC-001 · Logout keeps the session"
        assert edited["hash"] != doc["hash"]
        assert edited["id"] == doc["id"]   # updated in place, not re-keyed

        _db.save_test_cases(pid, [])
        indexer.reindex_project(pid)
        assert not [k for k in _docs(pid) if k[0] == "test_case"]

    def test_checklist_item_lifecycle(self, search_on):
        pid = _project()
        _db.save_checklist(pid, [{"id": "CL_001", "section": "Cart",
                                  "objective": "The total updates",
                                  "priority": "High"}])
        indexer.reindex_project(pid)
        doc = _docs(pid)[("checklist_item", "CL_001", 0)]
        assert doc["title"] == "CL_001 · Cart"
        assert doc["body"] == "The total updates"
        assert doc["facets"]["depth"] == 2

        _db.save_checklist(pid, [{"id": "CL_001", "section": "Cart",
                                  "objective": "The total is rounded"}])
        indexer.reindex_project(pid)
        assert _docs(pid)[("checklist_item", "CL_001", 0)]["body"] == \
            "The total is rounded"
        _db.save_checklist(pid, [])
        indexer.reindex_project(pid)
        assert not _docs(pid)

    def test_bug_lifecycle(self, search_on):
        pid = _project()
        bid = _db.save_bug(pid, {"id": "BUG-1", "title": "Cart total wrong",
                                 "severity": "Major", "status": "Open",
                                 "steps_to_reproduce": "add two items",
                                 "actual_result": "3.00",
                                 "expected_result": "2.00"})
        indexer.reindex_project(pid)
        doc = _docs(pid)[("bug", "BUG-1", 0)]
        assert doc["title"] == "BUG-1 · Cart total wrong"
        assert "add two items" in doc["body"] and "3.00" in doc["body"]
        assert doc["facets"]["severity"] == "Major"

        _db.bulk_update_bugs(pid, [bid], action="close", value=None,
                             actor="tester")
        indexer.reindex_project(pid)
        assert _docs(pid)[("bug", "BUG-1", 0)]["facets"]["status"] == "Closed"

        _db.bulk_update_bugs(pid, [bid], action="delete", value=None,
                             actor="tester")
        indexer.reindex_project(pid)
        assert ("bug", "BUG-1", 0) not in _docs(pid)

    def test_run_and_only_failed_or_blocked_case_results(self, search_on):
        pid = _project()
        run_id = _db.start_execution_run(pid, {"mode": "manual",
                                               "browser": "chromium"})
        _db.save_case_result(run_id, case_external_id="TC-001",
                             status="Passed", notes="fine")
        _db.save_case_result(run_id, case_external_id="TC-002",
                             status="Failed",
                             notes="Traceback\n" + "frame\n" * 400)
        _db.save_case_result(run_id, case_external_id="TC-003",
                             status="blocked", notes="env down")
        _db.finish_execution_run(run_id, "completed",
                                 {"passed": 1, "failed": 1, "blocked": 1})
        indexer.reindex_project(pid)
        docs = _docs(pid)
        run_doc = docs[("run", f"run:{run_id}", 0)]
        assert run_doc["title"].startswith(f"Run #{run_id}")
        assert "failed: 1" in run_doc["body"]
        assert run_doc["facets"] == {"status": "completed", "mode": "manual"}
        results = {k[1] for k in docs if k[0] == "case_result"}
        assert results == {f"run:{run_id}:TC-002", f"run:{run_id}:TC-003"}
        failed = docs[("case_result", f"run:{run_id}:TC-002", 0)]
        assert len(failed["body"]) <= indexer.CASE_NOTE_MAX

    def test_only_the_latest_estimation(self, search_on):
        pid = _project()
        _db.save_estimation(pid, {"scope": "first"}, {}, 10.0)
        _db.save_estimation(pid, {"scope": "second"}, {}, 20.0)
        indexer.reindex_project(pid)
        ests = [v for k, v in _docs(pid).items() if k[0] == "estimation"]
        assert len(ests) == 1
        assert ests[0]["title"] == "Estimate · 20 h"
        assert "second" in ests[0]["body"]


# ── layer 3: content hash ────────────────────────────────────────────

class TestContentHash:
    def test_an_unchanged_project_is_not_rewritten(self, search_on):
        pid = _project()
        _db.save_test_cases(pid, [TC, dict(TC, id="TC-002", summary="other")])
        _db.save_bug(pid, {"id": "BUG-1", "title": "broken"})
        first = indexer.reindex_project(pid)
        before = _docs(pid)
        assert first["upserted"] == len(before) == 3

        second = indexer.reindex_project(pid)
        after = _docs(pid)
        assert second["upserted"] == 0 and second["deleted"] == 0
        # The property itself: same rows, not rewritten.
        assert {k: (v["id"], v["indexed_at"]) for k, v in after.items()} == \
            {k: (v["id"], v["indexed_at"]) for k, v in before.items()}

    def test_wipe_and_replace_with_the_same_content_rewrites_nothing(
            self, search_on):
        pid = _project()
        _db.save_test_cases(pid, [TC])
        indexer.reindex_project(pid)
        before = _docs(pid)
        # save_test_cases deletes and re-inserts: new row ids, same content.
        _db.save_test_cases(pid, [TC])
        res = indexer.reindex_project(pid)
        assert res["upserted"] == 0
        assert _docs(pid)[("test_case", "TC-001", 0)]["indexed_at"] == \
            before[("test_case", "TC-001", 0)]["indexed_at"]

    def test_one_edit_rewrites_one_document(self, search_on):
        pid = _project()
        _db.save_test_cases(pid, [dict(TC, id=f"TC-{i:03d}",
                                       summary=f"case {i}")
                                  for i in range(1, 6)])
        indexer.reindex_project(pid)
        before = _docs(pid)
        pack = [dict(TC, id=f"TC-{i:03d}", summary=f"case {i}")
                for i in range(1, 6)]
        pack[2]["summary"] = "case three, edited"
        _db.save_test_cases(pid, pack)
        res = indexer.reindex_project(pid)
        assert res["upserted"] == 1
        after = _docs(pid)
        changed = {k for k in after
                   if after[k]["indexed_at"] != before[k]["indexed_at"]}
        assert changed == {("test_case", "TC-003", 0)}

    def test_rebuild_rewrites_everything(self, search_on):
        pid = _project()
        _db.save_test_cases(pid, [TC])
        indexer.reindex_project(pid)
        res = indexer.reindex_project(pid, rebuild=True)
        assert res["upserted"] == 1

    def test_state_records_the_start_of_the_pass_and_bumps_on_change(
            self, search_on):
        pid = _project()
        _db.save_test_cases(pid, [TC])
        t0 = datetime.now(timezone.utc)
        indexer.reindex_project(pid)
        st = _state(pid)
        assert st["doc_count"] == 1 and st["version"] == 1
        assert st["vec_version"] == get_embedder().version
        indexed = st["indexed_at"]
        if indexed.tzinfo is None:
            indexed = indexed.replace(tzinfo=timezone.utc)
        assert t0 - timedelta(seconds=1) <= indexed
        indexer.reindex_project(pid)
        assert _state(pid)["version"] == 1          # nothing changed
        _db.save_test_cases(pid, [])
        indexer.reindex_project(pid)
        assert _state(pid)["version"] == 2
        assert _state(pid)["doc_count"] == 0


# ── flag ─────────────────────────────────────────────────────────────

class TestFlag:
    def test_flag_off_writes_nothing(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_SEARCH", "0")
        pid = _project()
        _db.save_test_cases(pid, [TC])
        res = indexer.reindex_project(pid)
        assert res["skipped"]
        assert _docs(pid) == {} and _state(pid) is None
        assert indexer.index_global() == {"skipped": "PLATFORM_SEARCH is off"}

    def test_flag_without_its_prerequisite_is_off(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_SEARCH", "1")
        monkeypatch.setenv("WORKSPACE_DB_FIRST", "0")
        pid = _project()
        _db.save_test_cases(pid, [TC])
        assert indexer.reindex_project(pid)["skipped"]
        assert _docs(pid) == {}

    def test_ignore_flag_is_the_backfill_override(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_SEARCH", "0")
        pid = _project()
        _db.save_test_cases(pid, [TC])
        indexer.reindex_project(pid, ignore_flag=True)
        assert ("test_case", "TC-001", 0) in _docs(pid)


# ── project deletion ─────────────────────────────────────────────────

class TestProjectDelete:
    def test_retention_delete_removes_docs_and_state(self, search_on):
        from engine import retention
        pid = _project()
        _db.save_test_cases(pid, [TC])
        indexer.reindex_project(pid)
        assert _docs(pid) and _state(pid)
        report = retention.delete_project_data(pid)
        assert report.ok
        assert _docs(pid) == {} and _state(pid) is None

    def test_db_delete_project_removes_docs_and_state(self, search_on):
        pid = _project()
        _db.save_bug(pid, {"id": "BUG-1", "title": "kept by SET NULL"})
        indexer.reindex_project(pid)
        assert _docs(pid)
        _db.delete_project(pid)
        assert _docs(pid) == {} and _state(pid) is None

    def test_reindexing_a_vanished_project_cleans_up(self, search_on):
        pid = _project()
        _db.save_test_cases(pid, [TC])
        indexer.reindex_project(pid)
        # A raw delete that bypasses both cleanups above: the indexer itself
        # must not resurrect or keep the scope.
        with _db.session_scope() as sess:
            sess.get(SearchScopeState, pid).indexed_at = None
        with _db.get_engine().begin() as conn:
            from sqlalchemy import text
            conn.execute(text("DELETE FROM test_case WHERE project_id = :p"),
                         {"p": pid})
            conn.execute(text("DELETE FROM project WHERE id = :p"),
                         {"p": pid})
        res = indexer.reindex_project(pid)
        assert res["skipped"] == "project gone"
        assert _docs(pid) == {} and _state(pid) is None


# ── tenancy ──────────────────────────────────────────────────────────

class TestTenancy:
    def test_two_orgs_never_share_documents(self, search_on):
        org_a = _db.create_organization(f"Org A {secrets.token_hex(3)}")
        org_b = _db.create_organization(f"Org B {secrets.token_hex(3)}")
        pa = _project(org_a, "tenant a")
        pb = _project(org_b, "tenant b")
        # Identical content in both: the case where a shared key would leak.
        _db.save_test_cases(pa, [TC])
        _db.save_test_cases(pb, [TC])
        indexer.reindex_project(pa)
        indexer.reindex_project(pb)
        da, db_ = _docs(pa), _docs(pb)
        assert {v["org_id"] for v in da.values()} == {org_a}
        assert {v["org_id"] for v in db_.values()} == {org_b}
        assert {v["project_id"] for v in da.values()} == {pa}
        assert not ({v["id"] for v in da.values()}
                    & {v["id"] for v in db_.values()})
        assert not ({v["doc_key"] for v in da.values()}
                    & {v["doc_key"] for v in db_.values()})

        # Emptying A and reindexing A leaves B's documents untouched.
        _db.save_test_cases(pa, [])
        indexer.reindex_project(pa)
        assert _docs(pa) == {}
        assert {k: v["indexed_at"] for k, v in _docs(pb).items()} == \
            {k: v["indexed_at"] for k, v in db_.items()}

    def test_a_project_changing_org_has_its_documents_rewritten(
            self, search_on):
        org_a = _db.create_organization(f"Org A {secrets.token_hex(3)}")
        org_b = _db.create_organization(f"Org B {secrets.token_hex(3)}")
        pid = _project(org_a)
        _db.save_test_cases(pid, [TC])
        indexer.reindex_project(pid)
        assert {v["org_id"] for v in _docs(pid).values()} == {org_a}
        assert _db.set_project_org(pid, org_b)
        indexer.reindex_project(pid)
        assert {v["org_id"] for v in _docs(pid).values()} == {org_b}

    def test_every_project_document_carries_both_tenancy_columns(
            self, search_on):
        org = _db.create_organization(f"Org {secrets.token_hex(3)}")
        pid = _project(org)
        _db.save_test_cases(pid, [TC])
        _db.save_checklist(pid, [{"id": "CL_001", "objective": "x"}])
        _db.save_bug(pid, {"id": "BUG-1", "title": "y"})
        run_id = _db.start_execution_run(pid, {})
        _db.save_case_result(run_id, case_external_id="TC-001",
                             status="Failed", notes="z")
        _db.save_estimation(pid, {"a": 1}, {}, 1.0)
        indexer.reindex_project(pid)
        docs = _docs(pid)
        assert {k[0] for k in docs} == set(indexer.PROJECT_KINDS)
        assert all(v["scope"] == "project" and v["org_id"] == org
                   and v["project_id"] == pid for v in docs.values())


# ── global corpora ───────────────────────────────────────────────────

def _global_docs() -> dict[str, dict]:
    with _db.session_scope() as sess:
        return {r.doc_key: {"id": r.id, "kind": r.kind, "org_id": r.org_id,
                            "project_id": r.project_id, "scope": r.scope,
                            "indexed_at": r.indexed_at, "title": r.title,
                            "body": r.body}
                for r in sess.query(SearchDoc)
                .filter(SearchDoc.scope == "global")}


class TestGlobalCorpora:
    def test_guide_and_syllabus_are_indexed_as_global_and_read_only(
            self, search_on):
        indexer.index_global(force=True)
        docs = _global_docs()
        kinds = {v["kind"] for v in docs.values()}
        assert {"guide", "istqb"} <= kinds <= indexer.GLOBAL_KINDS
        assert all(v["org_id"] is None and v["project_id"] is None
                   for v in docs.values())
        guide = [v for v in docs.values() if v["kind"] == "guide"]
        # Both languages, real text, no markup.
        assert any(k.startswith("global:-:guide:ua:") for k in docs)
        assert any(k.startswith("global:-:guide:en:workflow:") for k in docs)
        assert not any("<p>" in v["body"] or "{#" in v["body"]
                       for v in guide)
        st = _state("global")
        assert st["doc_count"] == len(docs)

    def test_an_unchanged_corpus_is_not_reindexed(self, search_on):
        indexer.index_global(force=True)
        before = _global_docs()
        res = indexer.index_global()
        assert res["guide"]["skipped"] == "unchanged"
        assert res["istqb"]["skipped"] == "unchanged"
        assert {k: v["indexed_at"] for k, v in _global_docs().items()} == \
            {k: v["indexed_at"] for k, v in before.items()}

    def test_a_changed_source_rewrites_only_what_changed(
            self, search_on, tmp_path, monkeypatch):
        src = indexer.GUIDE_DIR / "_sections_en.html"
        copy = tmp_path / "_sections_en.html"
        copy.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        monkeypatch.setattr(indexer, "GUIDE_DIR", tmp_path)
        indexer.index_global(force=True, corpora=["guide"])
        before = _global_docs()
        assert not any(":guide:ua:" in k for k in before
                       if before[k]["kind"] == "guide"), \
            "the UA file is outside the patched directory"
        text = copy.read_text(encoding="utf-8")
        copy.write_text(text.replace("Every other card describes one screen.",
                                     "Every other card describes a screen "
                                     "and its zebra."), encoding="utf-8")
        res = indexer.index_global(corpora=["guide"])
        assert res["guide"]["skipped"] == ""
        after = _global_docs()
        changed = [k for k in after
                   if after[k]["indexed_at"] != before[k]["indexed_at"]]
        # Only the edited section's chunks (the edit can shift a window
        # boundary, so possibly more than one of them) — no other section.
        assert changed and res["guide"]["upserted"] == len(changed)
        assert all(":guide:en:workflow:" in k for k in changed)
        assert len(changed) < len([k for k in after
                                   if after[k]["kind"] == "guide"])
        assert any("zebra" in after[k]["body"] for k in changed)
        # Restore the real corpus for the next test in this worker. Not
        # monkeypatch.undo(): that would also undo the fixture's flag.
        monkeypatch.setattr(indexer, "GUIDE_DIR", src.parent)
        indexer.index_global(force=True, corpora=["guide"])
        assert any(":guide:ua:" in k for k in _global_docs())

    def test_the_global_scope_refuses_a_project_kind(self, search_on):
        bad = indexer.Draft(kind="test_case", ref="x", title="t", body="b")
        with pytest.raises(ValueError):
            indexer._sync(scope="global", project_id=None, org_id=None,
                          drafts=[bad], kinds={"test_case"},
                          result=indexer.SyncResult("global:test"))


# ── layer 2: the lag check ───────────────────────────────────────────

class TestLagCheck:
    def test_never_indexed_is_stale(self, search_on):
        pid = _project()
        assert indexer.is_stale(pid)

    def test_fresh_after_a_pass_and_stale_after_a_touch(self, search_on):
        pid = _project()
        indexer.reindex_project(pid)
        assert not indexer.is_stale(pid)
        assert _db.touch_project(pid)
        assert indexer.is_stale(pid)

    def test_a_write_during_the_pass_keeps_the_index_stale(
            self, search_on, monkeypatch):
        """Forced interleaving: the pass reads the rows, then — before it
        records its state — a write lands. Recording the *start* of the
        pass is what keeps the lag check honest; recording the end would
        call this index fresh while it misses the write."""
        pid = _project()
        _db.save_test_cases(pid, [TC])
        real = indexer._iter_test_cases

        def interleaved(project_id):
            yield from real(project_id)
            _db.save_test_cases(project_id, [dict(TC, summary="landed mid-pass")])

        monkeypatch.setitem(indexer._PROJECT_BUILDERS, "test_case",
                            interleaved)
        indexer.reindex_project(pid)
        assert _docs(pid)[("test_case", "TC-001", 0)]["title"].endswith(
            "valid password")
        assert indexer.is_stale(pid)
        monkeypatch.setitem(indexer._PROJECT_BUILDERS, "test_case", real)
        hooks.reset()
        indexer.reindex_project(pid)
        assert _docs(pid)[("test_case", "TC-001", 0)]["title"].endswith(
            "landed mid-pass")
        assert not indexer.is_stale(pid)

    def test_a_vector_version_change_is_stale(self, search_on, monkeypatch):
        pid = _project()
        indexer.reindex_project(pid)
        assert not indexer.is_stale(pid)
        monkeypatch.setattr(indexer, "_active_vec_version",
                            lambda: (None, "hash-v9.9-288"))
        assert indexer.is_stale(pid)


# ── pgvector mirror (no Postgres here: the SQL it would send) ────────

class TestPgvectorMirror:
    def test_literal_is_pgvector_text_format(self):
        assert indexer._vector_literal([0.5, -0.25, 0.0]) == "[0.5,-0.25,0.0]"

    def test_mirror_is_skipped_without_the_capability(self, search_on):
        from engine.search import caps
        assert not caps.get("pgvector")      # SQLite

        class Boom:
            def begin_nested(self):          # pragma: no cover — must not run
                raise AssertionError("tried to write search_vector")
        indexer._mirror_vectors(Boom(), [(1, "p", [0.0])])

    def test_mirror_writes_one_upsert_per_vector_when_available(
            self, monkeypatch):
        from contextlib import nullcontext

        from engine.search import caps
        monkeypatch.setitem(caps, "pgvector", True)
        sent = []

        class Sess:
            def begin_nested(self):
                return nullcontext()

            def execute(self, stmt, params):
                sent.append((str(stmt), params))
        indexer._mirror_vectors(Sess(), [(7, "pid", [1.0, 0.0])])
        assert len(sent) == 1
        sql, params = sent[0]
        assert "INSERT INTO search_vector" in sql and "ON CONFLICT" in sql
        assert params == {"id": 7, "pid": "pid", "emb": "[1.0,0.0]"}


# ── "Did you mean" vocabulary (T5) ───────────────────────────────────

class TestVocabulary:
    def test_reindex_stores_words_with_document_frequency(self, search_on):
        pid = _project()
        _db.save_test_cases(pid, [TC, dict(TC, id="TC-002",
                                           summary="Login works on mobile")])
        indexer.reindex_project(pid)
        vocab = _state(pid)["vocab"]
        assert vocab["v"] == indexer.VOCAB_VERSION
        terms = vocab["terms"]
        assert terms["login"] == 2            # words, counted per document
        assert terms["mobile"] == 1
        assert "the" not in terms and "001" not in terms

    def test_an_unchanged_pass_does_not_rebuild_it(self, search_on,
                                                   monkeypatch):
        pid = _project()
        _db.save_test_cases(pid, [TC])
        indexer.reindex_project(pid)

        def boom(_f):
            raise AssertionError("vocabulary rebuilt on an unchanged pass")
        monkeypatch.setattr(indexer, "build_vocab", boom)
        indexer.reindex_project(pid)

    def test_a_change_rebuilds_it(self, search_on):
        pid = _project()
        _db.save_test_cases(pid, [TC])
        indexer.reindex_project(pid)
        _db.save_test_cases(pid, [dict(TC, summary="Checkout with coupon")])
        indexer.reindex_project(pid)
        terms = _state(pid)["vocab"]["terms"]
        assert "coupon" in terms and "fails" not in terms

    def test_a_scope_indexed_before_vocabularies_gets_one(self, search_on):
        pid = _project()
        _db.save_test_cases(pid, [TC])
        indexer.reindex_project(pid)
        with _db.session_scope() as sess:
            sess.get(SearchScopeState, pid).vocab_json = None
        indexer.reindex_project(pid)           # nothing changed …
        assert "login" in _state(pid)["vocab"]["terms"]   # … but rebuilt

    def test_capped(self, search_on, monkeypatch):
        monkeypatch.setattr(indexer, "VOCAB_MAX", 3)
        pid = _project()
        _db.save_test_cases(pid, [TC])
        indexer.reindex_project(pid)
        assert len(_state(pid)["vocab"]["terms"]) == 3
