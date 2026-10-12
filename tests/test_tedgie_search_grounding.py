"""Tedgie on the shared retrieval layer (ADR 0003, task T8).

* A lookup ("which tests cover login?", "покажи відкриті баги по оплаті")
  is answered from the index with **zero** model calls — counted, not
  inferred: ``engine.chatbot.call_messages`` is replaced by a recorder.
* An open question about the caller's own items reaches the model with the
  retrieved items in its prompt, cited ``[[REF]]`` — and nothing of another
  organisation's, whose documents are written to match the same question.
* ``PLATFORM_SEARCH`` off: the reply and the prompt are what they were
  before T8, scope or no scope.

Tenancy is the retrieval layer's (``tests/test_search_tenancy.py`` owns
that gate); here it is checked where the chat could break it — the prompt
and the route that computes the scope.
"""
from __future__ import annotations

import json
import secrets
from types import SimpleNamespace

import pytest

from engine import auth as _auth
from engine import chatbot
from engine import db as _db
from engine import permissions as _perm
from engine.llm_safety import wrap_user_input
from engine.search import chat_grounding, hooks, indexer, retrieve
from tests._search_seed import seed_golden

MARK_B = "quokkafruit"
OPEN_Q = "why does checkout keep failing in our tests?"


class FakeLLM:
    """Stand-in for ``engine.chatbot.call_messages``: records every call,
    answers with a citation of a retrieved ref and of one never retrieved."""

    def __init__(self):
        self.calls: list[dict] = []
        self.reply = "Checkout is covered by [[TC-018]] and [[BUG-424242]]."

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(text=self.reply)],
                               usage=None)

    @property
    def prompt(self) -> str:
        return self.calls[-1]["messages"][-1]["content"]


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(chatbot, "call_messages", fake)
    monkeypatch.setattr(chatbot, "_ANTHROPIC_OK", True)
    return fake


@pytest.fixture
def search_on(monkeypatch):
    monkeypatch.setenv("PLATFORM_SEARCH", "1")
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("SEARCH_INDEX_DEBOUNCE_S", "3600")
    for name in ("EMBEDDINGS_PROVIDER", "RAG_MIN_GROUNDING", "SEARCH_W_LEX",
                 "SEARCH_W_VEC", "SEARCH_VEC_MIN"):
        monkeypatch.delenv(name, raising=False)
    _db.init_db()
    hooks.install()
    hooks.reset()
    retrieve.clear_caches()
    yield
    hooks.reset()


@pytest.fixture
def orgs(search_on, monkeypatch):
    """Two organisations. A holds the golden fixture; B the same plus
    items that carry :data:`MARK_B` and are written to match the checkout
    question — so a leak would be the *best* match, not a stray one."""
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    pwd = _auth.hash_password("a perfectly good passphrase")
    out = {}
    for side in ("a", "b"):
        org = _db.create_organization(f"T8 {side} {secrets.token_hex(4)}")
        user = _db.create_user(f"t8-{side}-{secrets.token_hex(5)}@example.com",
                               password_hash=pwd, email_verified=True)
        _db.add_org_member(org, user, "admin")
        pid = _db.upsert_project(f"T8 {side} {secrets.token_hex(4)}",
                                 org_id=org)
        out[side] = {"org": org, "user": user, "pid": pid}
    seed_golden(out["a"]["pid"])
    seed_golden(out["b"]["pid"], extra_cases=[{
        "id": "TC-777",
        "summary": f"Checkout {MARK_B} fails in our tests",
        "test_steps": f"1. Open the {MARK_B} checkout\n2. Pay",
        "status": "Failed"}])
    _db.save_bug(out["b"]["pid"], {
        "id": "BUG-777", "title": f"Checkout {MARK_B} payment failing",
        "status": "Open", "steps_to_reproduce": f"Pay in {MARK_B}"})
    for side in ("a", "b"):
        indexer.reindex_project(out[side]["pid"])
    hooks.reset()
    return out


def _scope(side: dict) -> chat_grounding.ChatScope:
    return chat_grounding.ChatScope(allowed=[side["pid"]],
                                    names={side["pid"]: "Shop A"},
                                    project_id=side["pid"])


def _as(client, side: dict) -> None:
    from engine import session_timeout as _timeout
    with client.session_transaction() as sess:
        sess.clear()
        sess[_perm.SESSION_USER_KEY] = side["user"]
        sess[_perm.SESSION_ORG_KEY] = side["org"]
        _timeout.stamp(sess)


# ── Lookups: answered from the index, no model ───────────────────────

class TestLookup:
    def test_which_tests_cover_login_needs_no_llm(self, orgs, llm):
        reply = chatbot.respond("which tests cover login?", "en",
                                search_scope=_scope(orgs["a"]))
        assert llm.calls == []
        assert reply.intent == "search_lookup"
        assert "TC-001" in reply.refs
        assert "[[TC-001]]" in reply.text
        assert reply.refs["TC-001"].startswith("/")
        assert MARK_B not in reply.text

    def test_ukrainian_open_bugs_about_payment(self, orgs, llm):
        reply = chatbot.respond("покажи відкриті баги по оплаті", "ua",
                                search_scope=_scope(orgs["a"]))
        assert llm.calls == []
        assert reply.intent == "search_lookup"
        assert reply.refs and all(r.startswith("BUG-") for r in reply.refs)
        assert "Знайдено" in reply.text
        assert MARK_B not in reply.text

    def test_lookup_lists_only_the_callers_scope(self, orgs, llm):
        # B asks for its own marker: found. A asks for it: not a lookup hit
        # (falls through to the ordinary chain), never B's items.
        b = chatbot.respond(f"find bugs about {MARK_B}", "en",
                            search_scope=_scope(orgs["b"]))
        assert b.intent == "search_lookup" and "BUG-777" in b.refs
        a = chatbot.respond(f"find bugs about {MARK_B}", "en",
                            search_scope=_scope(orgs["a"]))
        assert "BUG-777" not in a.refs
        assert MARK_B not in a.text.replace(f"about {MARK_B}", "")

    def test_no_scope_is_the_old_chain(self, orgs, llm):
        reply = chatbot.respond("which tests cover login?", "en")
        assert reply.intent != "search_lookup"
        assert reply.refs == {}

    def test_greeting_and_bug_form_are_never_replaced(self, orgs, llm):
        fast = chatbot.try_fast_path("hello", "en")
        assert chatbot.search_lookup("hello", "en", _scope(orgs["a"]),
                                     fast) is None


# ── Open questions: grounded prompt ─────────────────────────────────

class TestGrounding:
    def test_open_question_gets_retrieved_refs_in_the_prompt(self, orgs,
                                                             llm):
        reply = chatbot.respond(OPEN_Q, "en",
                                search_scope=_scope(orgs["a"]))
        assert len(llm.calls) == 1
        prompt = llm.prompt
        assert prompt.startswith(wrap_user_input(OPEN_Q))
        assert "Project items retrieved" in prompt
        cited = [r for r in reply.refs]
        assert cited, "grounded reply carries its refs"
        for ref in cited:
            assert f"[[{ref}]]" in prompt
        # No foreign-organisation content anywhere in what the model saw.
        assert MARK_B not in prompt
        assert orgs["b"]["pid"] not in json.dumps(llm.calls, default=str)

    def test_unretrieved_citation_is_dropped(self, orgs, llm):
        g = chat_grounding.grounding(OPEN_Q, _scope(orgs["a"]))
        first = next(iter(g.refs))
        llm.reply = f"See [[{first}]] and [[BUG-424242]]."
        reply = chatbot.respond(OPEN_Q, "en",
                                search_scope=_scope(orgs["a"]))
        assert reply.text == f"See [[{first}]] and."
        assert reply.refs[first].startswith("/")

    def test_grounding_block_is_bounded(self, orgs):
        from engine.search import answer
        g = chat_grounding.grounding(OPEN_Q, _scope(orgs["a"]))
        assert g is not None
        assert len(g.items) <= chat_grounding.GROUNDING_ITEMS
        assert answer.estimate_tokens(g.block) <= \
            chat_grounding.GROUNDING_TOKEN_BUDGET + 200

    def test_definition_question_is_not_grounded(self, orgs, llm):
        assert chat_grounding.grounding("what is a test case",
                                        _scope(orgs["a"])) is None


# ── Flag off: unchanged ─────────────────────────────────────────────

class TestFlagOff:
    @pytest.mark.parametrize("q", ["which tests cover login?", OPEN_Q,
                                   "покажи відкриті баги по оплаті"])
    def test_reply_and_prompt_identical(self, orgs, llm, monkeypatch, q):
        scope = _scope(orgs["a"])
        monkeypatch.setenv("PLATFORM_SEARCH", "0")
        with_scope = chatbot.respond_dict(q, "en", search_scope=scope)
        prompts_with = [c["messages"] for c in llm.calls]
        llm.calls.clear()
        without = chatbot.respond_dict(q, "en")
        prompts_without = [c["messages"] for c in llm.calls]
        assert with_scope == without
        assert "refs" not in with_scope
        assert prompts_with == prompts_without
        for messages in prompts_with:
            assert messages[-1]["content"] == wrap_user_input(q)

    def test_route_scope_is_none_when_off(self, app, monkeypatch):
        monkeypatch.setenv("PLATFORM_SEARCH", "0")
        from routes.chat import _search_scope
        with app.test_request_context("/chat"):
            assert _search_scope("whatever") is None


# ── Through the routes (scope from visible_projects) ────────────────

class TestRoutes:
    def test_post_chat_lookup_carries_refs(self, orgs, llm, client):
        _as(client, orgs["a"])
        resp = client.post("/chat", json={"message": "which tests cover login?",
                                          "lang": "en"})
        assert resp.status_code == 200, resp.data
        body = resp.get_json()
        assert body["intent"] == "search_lookup"
        assert "TC-001" in body["refs"]
        assert llm.calls == []
        assert MARK_B not in json.dumps(body)

    def test_post_chat_open_question_never_sees_org_b(self, orgs, llm,
                                                      client):
        _as(client, orgs["a"])
        resp = client.post("/chat", json={"message": OPEN_Q, "lang": "en"})
        assert resp.status_code == 200, resp.data
        assert len(llm.calls) == 1
        assert MARK_B not in llm.prompt
        assert "Project items retrieved" in llm.prompt

    def test_stream_lookup_is_one_full_frame_without_a_client(
            self, orgs, client, monkeypatch):
        import anthropic

        def _boom(*a, **k):
            raise AssertionError("the model must not be called for a lookup")

        monkeypatch.setattr(anthropic, "Anthropic", _boom)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-" + "x" * 30)
        _as(client, orgs["a"])
        resp = client.get("/chat/stream?message=which+tests+cover+login%3F"
                          "&lang=en")
        text = resp.get_data(as_text=True)
        assert "event: full" in text and "event: delta" not in text
        full = json.loads(text.split("event: full\ndata: ", 1)[1]
                          .split("\n", 1)[0])
        assert full["intent"] == "search_lookup"
        assert "TC-001" in full["refs"]


    def test_stream_open_question_is_grounded_and_sends_refs(
            self, orgs, client, monkeypatch):
        import anthropic
        seen: list[dict] = []

        class _Stream:
            text_stream = iter(["Look at ", "[[BUG-424242]] first."])

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return None

        class _Client:
            def __init__(self, **_kw):
                self.messages = SimpleNamespace(stream=self._stream)

            @staticmethod
            def _stream(**kwargs):
                seen.append(kwargs)
                return _Stream()

        monkeypatch.setattr(anthropic, "Anthropic", _Client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-" + "x" * 30)
        _as(client, orgs["a"])
        resp = client.get("/chat/stream", query_string={"message": OPEN_Q,
                                                        "lang": "en"})
        text = resp.get_data(as_text=True)
        assert len(seen) == 1
        prompt = seen[0]["messages"][-1]["content"]
        assert "Project items retrieved" in prompt and MARK_B not in prompt
        meta = json.loads(text.split("event: meta\ndata: ", 1)[1]
                          .split("\n", 1)[0])
        assert meta["refs"] and all(f"[[{r}]]" in prompt
                                    for r in meta["refs"])
        done = json.loads(text.split("event: done\ndata: ", 1)[1]
                          .split("\n", 1)[0])
        assert done["refs"] == meta["refs"]
        # The invented ref streamed through as text, but it is not in
        # refs, so the widget renders it as plain text, never a link.
        assert "BUG-424242" not in done["refs"]


# ── Citation cleaning ───────────────────────────────────────────────

def test_clean_citations_keeps_known_drops_unknown():
    out = chat_grounding.clean_citations(
        "See [[TC-1]] and [[TC-9]].", {"TC-1": "/test-cases#TC-1"})
    assert out == "See [[TC-1]] and."


def test_classifier_shapes():
    c = chat_grounding.classify
    assert c("which tests cover login?").lookup
    assert c("which tests cover login?").topic == "login"
    ua = c("покажи відкриті баги по оплаті")
    assert ua.lookup and ua.kinds == ["bug"] and ua.filters == {
        "status": "open"}
    assert not c(OPEN_Q).lookup
    assert c("what is boundary value analysis") is None
    assert not chat_grounding.is_project_question("what is a test case")
