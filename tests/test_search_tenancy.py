"""Search tenancy (ADR 0003 §4.2, §6 gate 4): organisation A never sees
organisation B's documents — through any parameter of any search endpoint —
and global documents are visible to both. Both eras: organisations
(``ORG_MODE``) and the legacy browser-session owner.

The leak tests look for B's *content* (a marker word only B's documents
contain) as well as B's project id, because a leak through a snippet, a
chip or a "did you mean" would not carry a project id at all.
"""
from __future__ import annotations

import json
import secrets

import pytest
from sqlalchemy import event

from engine import auth as _auth
from engine import db as _db
from engine import permissions as _perm
from engine.search import hooks, indexer, retrieve
from tests._search_seed import seed_golden

MARK_B = "quokkafruit"
MARK_GLOBAL = "pangolinguide"


@pytest.fixture
def search_on(monkeypatch):
    monkeypatch.setenv("PLATFORM_SEARCH", "1")
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("SEARCH_INDEX_DEBOUNCE_S", "3600")
    monkeypatch.delenv("EMBEDDINGS_PROVIDER", raising=False)
    _db.init_db()
    hooks.install()
    hooks.reset()
    retrieve.clear_caches()
    yield
    hooks.reset()


def _global_doc() -> None:
    """One global Guide document carrying :data:`MARK_GLOBAL`."""
    res = indexer.SyncResult(scope_key="test-global")
    indexer._sync(scope="global", project_id=None, org_id=None, drafts=[
        indexer.Draft(kind="guide", ref=f"zz:{MARK_GLOBAL}",
                      title=f"Guide · {MARK_GLOBAL} handbook",
                      body=f"The {MARK_GLOBAL} handbook explains exports.",
                      url="/guide#tips")],
        kinds={"guide"}, result=res)


def _fill(pid: str, *, marker: str | None = None) -> None:
    """The golden data; with *marker*, a test case and a bug that carry it
    (and the same ids, TC-777 / BUG-777, that the other side will ask
    for)."""
    extra = []
    if marker:
        extra = [{"id": "TC-777", "summary": f"Secret {marker} migration",
                  "test_steps": f"1. Run the {marker} job"}]
    seed_golden(pid, extra_cases=extra)
    if marker:
        _db.save_bug(pid, {"id": "BUG-777", "title": f"{marker} leak",
                           "status": "Open",
                           "steps_to_reproduce": f"Open the {marker} page"})
    indexer.reindex_project(pid)


@pytest.fixture
def orgs(search_on, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    pwd = _auth.hash_password("a perfectly good passphrase")
    out = {}
    for side in ("a", "b"):
        org = _db.create_organization(f"Tenancy {side} {secrets.token_hex(4)}")
        user = _db.create_user(f"t-{side}-{secrets.token_hex(5)}@example.com",
                               password_hash=pwd, email_verified=True)
        _db.add_org_member(org, user, "admin")
        pid = _db.upsert_project(f"T{side} {secrets.token_hex(4)}",
                                 org_id=org)
        out[side] = {"org": org, "user": user, "pid": pid}
    _fill(out["a"]["pid"])
    _fill(out["b"]["pid"], marker=MARK_B)
    _global_doc()
    hooks.reset()
    return out


def _as(client, side: dict) -> None:
    from engine import session_timeout as _timeout
    with client.session_transaction() as sess:
        sess.clear()
        sess[_perm.SESSION_USER_KEY] = side["user"]
        sess[_perm.SESSION_ORG_KEY] = side["org"]
        _timeout.stamp(sess)


def _assert_clean(body: dict, *, mine: set, foreign: str) -> None:
    """No foreign project id anywhere in *body*, and no foreign content in
    anything the index produced. (The caller's own query is echoed back in
    ``query``, ``explain`` and each chip's ``q`` — when the query *is* the
    marker, finding it there proves nothing.)"""
    assert foreign not in json.dumps(body).lower(), "foreign project leaked"
    produced = {
        "results": body.get("results"), "groups": body.get("groups"),
        "facets": body.get("facets"), "total": body.get("total"),
        "did_you_mean": (body.get("query") or {}).get("did_you_mean"),
        "corrections": (body.get("query") or {}).get("corrections"),
        "chips": [{k: v for k, v in c.items() if k != "q"}
                  for c in body.get("chips") or []],
    }
    text = json.dumps(produced, ensure_ascii=False).lower()
    assert MARK_B not in text, "foreign content leaked"
    for r in body.get("results", []):
        pid = (r.get("project") or {}).get("id")
        assert pid is None or pid in mine


QUERIES = ["quokkafruit", "quokkafrut", "quokkafruit leak", "TC-777",
           "BUG-777", "TC-001", "login password", "секрет міграція",
           MARK_GLOBAL]

COMBOS = [
    {}, {"quick": "1"}, {"scope": "org"}, {"kinds": "bug"},
    {"kinds": "test_case,bug,checklist_item"}, {"status": "open"},
    {"priority": "high"}, {"linked": "TC-777"}, {"linked": "BUG-777"},
    {"explain": "0"}, {"limit": "100"}, {"scope": "project"},
]


class TestOrgEra:
    def test_a_never_sees_b(self, client, orgs):
        a, b = orgs["a"], orgs["b"]
        _as(client, a)
        for q in QUERIES:
            for combo in COMBOS + [{"project_id": a["pid"]},
                                   {"scope": "org", "project_id": a["pid"]}]:
                resp = client.get("/api/search",
                                  query_string={"q": q, **combo})
                assert resp.status_code == 200, (q, combo, resp.data)
                _assert_clean(resp.get_json(), mine={a["pid"]},
                              foreign=b["pid"])

    def test_b_still_finds_its_own(self, client, orgs):
        """The negative above is only meaningful if B can find it."""
        b = orgs["b"]
        _as(client, b)
        body = client.get("/api/search",
                          query_string={"q": MARK_B}).get_json()
        assert {r["ref"] for r in body["results"]} >= {"TC-777", "BUG-777"}
        assert all(r["project"]["id"] == b["pid"] for r in body["results"]
                   if r["project"])

    @pytest.mark.parametrize("extra", [{}, {"quick": "1"}, {"scope": "org"},
                                       {"scope": "project"},
                                       {"kinds": "bug"}])
    def test_an_explicit_foreign_project_is_403(self, client, orgs, extra):
        _as(client, orgs["a"])
        for key in ("project_id", "project"):
            resp = client.get("/api/search", query_string={
                "q": "login", key: orgs["b"]["pid"], **extra})
            assert resp.status_code == 403, (key, extra)
            assert MARK_B not in resp.get_data(as_text=True)

    def test_a_missing_project_answers_like_a_foreign_one(self, client,
                                                          orgs):
        _as(client, orgs["a"])
        resp = client.get("/api/search", query_string={
            "q": "login", "project_id": "f" * 32})
        assert resp.status_code == 403

    def test_hints_and_reindex_refuse_a_foreign_project(self, client, orgs):
        _as(client, orgs["a"])
        resp = client.get("/api/hints", query_string={
            "page": "test_cases", "project_id": orgs["b"]["pid"],
            "context": "test_case:TC-777"})
        assert resp.status_code == 403
        resp = client.post("/api/search/reindex",
                           json={"project_id": orgs["b"]["pid"]})
        assert resp.status_code == 403

    def test_status_counts_only_what_a_sees(self, client, orgs):
        _as(client, orgs["a"])
        body = client.get("/api/search/status").get_json()
        with _db.session_scope() as sess:
            from engine.db import SearchDoc
            mine = sess.query(SearchDoc).filter(
                (SearchDoc.project_id == orgs["a"]["pid"])
                | (SearchDoc.scope == "global")).count()
        assert body["docs"] == mine
        assert orgs["b"]["pid"] not in json.dumps(body)

    def test_global_documents_are_visible_to_both(self, client, orgs):
        for side in ("a", "b"):
            _as(client, orgs[side])
            for extra in ({}, {"project_id": orgs[side]["pid"]}):
                body = client.get("/api/search", query_string={
                    "q": MARK_GLOBAL, **extra}).get_json()
                refs = [r["ref"] for r in body["results"]]
                assert f"zz:{MARK_GLOBAL}" in refs, (side, extra)
                hit = body["results"][refs.index(f"zz:{MARK_GLOBAL}")]
                assert hit["project"] is None and hit["kind"] == "guide"

    def test_did_you_mean_does_not_offer_the_other_tenants_words(
            self, client, orgs):
        _as(client, orgs["a"])
        body = client.get("/api/search", query_string={
            "q": "quokkafrut"}).get_json()
        assert MARK_B not in (body["query"]["did_you_mean"] or "")
        _as(client, orgs["b"])
        body = client.get("/api/search", query_string={
            "q": "quokkafrut"}).get_json()
        assert body["query"]["did_you_mean"] == MARK_B, \
            "B's own vocabulary corrects it — the check above means something"

    def test_a_user_in_no_team_is_refused(self, client, orgs):
        loner = _db.create_user(f"loner-{secrets.token_hex(5)}@example.com",
                                password_hash=_auth.hash_password(
                                    "a perfectly good passphrase"),
                                email_verified=True)
        from engine import session_timeout as _timeout
        with client.session_transaction() as sess:
            sess.clear()
            sess[_perm.SESSION_USER_KEY] = loner
            _timeout.stamp(sess)
        # No organisation → no role; the route policy refuses before search
        # (and were it to let them through, visible_projects() is [] for
        # them — global only, see TestScopeClauseIsTheOnlyWay).
        resp = client.get("/api/search", query_string={"q": MARK_GLOBAL})
        assert resp.status_code == 403


class TestLegacyEra:
    @pytest.fixture
    def sids(self, search_on, monkeypatch):
        monkeypatch.delenv("AUTH_ENABLED", raising=False)
        monkeypatch.delenv("ORG_MODE", raising=False)
        from engine import permissions as perm
        monkeypatch.setattr(perm, "auth_active", lambda: False)
        monkeypatch.setattr(perm, "org_active", lambda: False)
        pa = _db.upsert_project(f"La {secrets.token_hex(4)}",
                                owner_sid="sid-a-" + secrets.token_hex(4))
        pb = _db.upsert_project(f"Lb {secrets.token_hex(4)}",
                                owner_sid="sid-b-" + secrets.token_hex(4))
        _fill(pa)
        _fill(pb, marker=MARK_B)
        _global_doc()
        hooks.reset()
        sid_a = _db.get_project(pa)["owner_sid"]
        import routes._shared as shared
        monkeypatch.setattr(shared, "get_session_id",
                            lambda *a, **k: sid_a)
        return {"a": pa, "b": pb}

    def test_a_foreign_owner_sid_is_not_visible(self, client, sids):
        for q in QUERIES:
            for combo in COMBOS:
                resp = client.get("/api/search",
                                  query_string={"q": q, **combo})
                assert resp.status_code == 200, (q, combo)
                _assert_clean(resp.get_json(), mine={sids["a"]},
                              foreign=sids["b"])

    def test_own_project_is_found_without_naming_it(self, client, sids):
        body = client.get("/api/search",
                          query_string={"q": "login password"}).get_json()
        assert {r["project"]["id"] for r in body["results"]
                if r["project"]} == {sids["a"]}

    def test_explicit_foreign_project_is_403(self, client, sids):
        for extra in ({}, {"quick": "1"}, {"scope": "org"}):
            resp = client.get("/api/search", query_string={
                "q": "login", "project_id": sids["b"], **extra})
            assert resp.status_code == 403


class TestScopeClauseIsTheOnlyWay:
    def test_none_is_refused(self, search_on):
        with pytest.raises(retrieve.TenancyError):
            retrieve._scope_clause(None)
        with pytest.raises(retrieve.TenancyError):
            retrieve.search(retrieve.SearchParams(q="x", allowed=None))
        with pytest.raises(retrieve.TenancyError):
            retrieve.quick_search("x", None)

    def test_empty_list_means_global_only(self, search_on, make_project):
        pid = make_project(f"scoped {secrets.token_hex(4)}")
        _fill(pid)
        _global_doc()
        body = retrieve.search(retrieve.SearchParams(
            q=f"login {MARK_GLOBAL}", allowed=[], istqb=False))
        assert body["results"]
        assert all(r["project"] is None for r in body["results"])

    def test_every_search_doc_statement_carries_the_clause(
            self, client, search_on, make_project, monkeypatch):
        """Every SQL statement that reads ``search_doc`` during every kind
        of request has the scope predicate in it — counted at the cursor,
        so a code path that built its own query would show up here."""
        pid = make_project(f"sql {secrets.token_hex(4)}")
        _fill(pid)
        hooks.reset()
        calls = []
        real = retrieve._scope_clause

        def spy(allowed, **kw):
            assert isinstance(allowed, list), type(allowed)
            calls.append(list(allowed))
            return real(allowed, **kw)
        monkeypatch.setattr(retrieve, "_scope_clause", spy)

        seen: list[str] = []

        def capture(conn, cursor, statement, params, context, many):
            low = statement.lower()
            if "from search_doc" in low or "join search_doc" in low:
                seen.append(statement)
        engine = _db.get_engine()
        event.listen(engine, "before_cursor_execute", capture)
        try:
            for qs in ({"q": "login"}, {"q": "TC-18"},
                       {"q": "log", "quick": "1"},
                       {"q": "TC-018", "linked": "TC-018"},
                       {"q": "paswword", "status": "nope"}):
                resp = client.get("/api/search",
                                  query_string={**qs, "project_id": pid})
                assert resp.status_code == 200
            client.get("/api/hints", query_string={
                "page": "test_cases", "project_id": pid,
                "context": "test_case:TC-001"})
            client.get("/api/search/status")
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert calls, "the spy saw nothing — the test would prove nothing"
        assert seen
        for stmt in seen:
            assert "search_doc.scope" in stmt, stmt
