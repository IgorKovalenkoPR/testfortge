"""The automation result cache — engine/llm_cache.py (ADR 0003 §4.9).

What a bill depends on:

* an identical second request is answered without calling the model at all
  (counted at the ``call_messages`` boundary, not inferred from timing);
* anything that could change the answer is a miss — the request text, the
  caller's prompt version, the house style, the model, and above all the
  tenant: one org never reads another's result;
* rows expire after ``LLM_RESULT_CACHE_DAYS`` and each scope is capped,
  least recently used first;
* ``bypass_cache`` regenerates and replaces;
* chat and search answers are refused outright.
"""
from __future__ import annotations

import secrets
from datetime import timedelta
from types import SimpleNamespace

import pytest

from engine import db as _db
from engine import glossary, llm_cache, llm_client, llm_context, retention


@pytest.fixture(autouse=True)
def _ready(monkeypatch):
    _db.init_db()
    for var in ("ANTHROPIC_MODEL", "LLM_MODEL_FOR_AUTHORING",
                "LLM_RESULT_CACHE_DAYS", "LLM_RESULT_CACHE_MAX_ROWS",
                "AUTOMATION_AI_MODE"):
        monkeypatch.delenv(var, raising=False)
    llm_cache.reset_stats()


def _org() -> str:
    return secrets.token_hex(16)


class _Counter:
    """A ``call_messages`` stand-in that counts and answers *text*."""

    def __init__(self, text: str = '{"ok": true}', stop_reason="end_turn"):
        self.calls: list[dict] = []
        self.text = text
        self.stop_reason = stop_reason

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)],
            usage=SimpleNamespace(input_tokens=100, output_tokens=50,
                                  cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0),
            stop_reason=self.stop_reason)


def _ask(counter, *, org, content="Write cases for login", version="v1",
         kind="authoring", **extra):
    return llm_cache.cached_call(
        kind, prompt_version=version, org_id=org, call=counter,
        max_tokens=100, system=[{"type": "text", "text": "system"}],
        messages=[{"role": "user", "content": content}], **extra)


class TestHit:
    def test_second_identical_call_never_reaches_call_messages(
            self, monkeypatch):
        # Through the default path — the real module attribute, patched —
        # so the count is at the boundary every metered call crosses.
        counter = _Counter()
        monkeypatch.setattr(llm_client, "call_messages", counter)
        org = _org()
        kwargs = dict(prompt_version="v1", org_id=org, max_tokens=100,
                      system="s",
                      messages=[{"role": "user", "content": "same"}])
        first = llm_cache.cached_call("authoring", **kwargs)
        second = llm_cache.cached_call("authoring", **kwargs)
        assert len(counter.calls) == 1
        assert llm_cache.response_text(second) == \
            llm_cache.response_text(first)
        assert getattr(second, "from_cache", False) is True
        # Nothing was spent, and the stand-in says so.
        assert second.usage.input_tokens == 0
        assert second.usage.output_tokens == 0
        assert llm_cache.stats()["hits"] == {"authoring": 1}

    def test_a_hit_is_counted_on_the_row(self):
        counter, org = _Counter(), _org()
        for _ in range(3):
            _ask(counter, org=org)
        assert len(counter.calls) == 1
        with _db.session_scope() as sess:
            row = sess.query(_db.LlmResultCache).filter_by(org_id=org).one()
            assert row.hits == 2
            assert row.kind == "authoring"
            assert row.prompt_version == "v1"

    def test_a_real_caller_is_served_from_the_cache(self, monkeypatch):
        """tc_author end to end: the second identical authoring request
        costs zero calls and yields the same pack."""
        from engine import tc_author
        payload = ('{"rationale": "r", "gaps": [], "cases": [{"summary": '
                   '"Verify that the login form submits", "section": "Login",'
                   ' "category": "Positive", "priority": "High", '
                   '"preconditions": "The login page is open", "steps": '
                   '["Enter a valid email", "Click \\"Sign in\\""], '
                   '"expected_result": "The dashboard is displayed"}]}')
        counter = _Counter(payload)
        monkeypatch.setattr(tc_author, "call_messages", counter)
        monkeypatch.setenv("AUTOMATION_AI_MODE", "full")
        arts = tc_author.Artifacts(custom_prompt="login",
                                   requirements=["User can sign in"])
        with llm_context.bound(org_id=_org()):
            first = tc_author.author_test_cases(artifacts=arts,
                                                force_llm=True)
            second = tc_author.author_test_cases(artifacts=arts,
                                                 force_llm=True)
        assert first.source == second.source == "llm"
        assert len(counter.calls) == 1
        assert [c.summary for c in first.cases] == \
            [c.summary for c in second.cases]


class TestMiss:
    def test_changed_input_misses(self):
        counter, org = _Counter(), _org()
        _ask(counter, org=org, content="login")
        _ask(counter, org=org, content="checkout")
        assert len(counter.calls) == 2

    def test_changed_max_tokens_misses(self):
        counter, org = _Counter(), _org()
        llm_cache.cached_call("authoring", prompt_version="v1", org_id=org,
                              call=counter, max_tokens=100, messages=[])
        llm_cache.cached_call("authoring", prompt_version="v1", org_id=org,
                              call=counter, max_tokens=200, messages=[])
        assert len(counter.calls) == 2

    def test_changed_prompt_version_misses(self):
        counter, org = _Counter(), _org()
        _ask(counter, org=org, version="v1")
        _ask(counter, org=org, version="v2")
        assert len(counter.calls) == 2

    def test_changed_house_style_misses(self, monkeypatch):
        counter, org = _Counter(), _org()
        _ask(counter, org=org)
        # Through the real hash: a glossary edit has to change it.
        before = glossary.house_style_hash()
        monkeypatch.setattr(glossary, "glossary_text",
                            lambda: "a different glossary")
        assert glossary.house_style_hash() != before
        _ask(counter, org=org)
        assert len(counter.calls) == 2

    def test_a_different_org_misses(self):
        counter = _Counter()
        _ask(counter, org=_org())
        _ask(counter, org=_org())
        assert len(counter.calls) == 2

    def test_a_different_model_misses(self, monkeypatch):
        counter, org = _Counter(), _org()
        monkeypatch.setenv("LLM_MODEL_FOR_AUTHORING", "claude-haiku-x")
        _ask(counter, org=org)
        monkeypatch.setenv("LLM_MODEL_FOR_AUTHORING", "claude-sonnet-x")
        _ask(counter, org=org)
        assert len(counter.calls) == 2

    def test_users_without_an_org_are_scoped_apart(self):
        counter = _Counter()
        with llm_context.bound(user_id="user-a"):
            _ask(counter, org=None)
            _ask(counter, org=None)
        with llm_context.bound(user_id="user-b"):
            _ask(counter, org=None)
        assert len(counter.calls) == 2

    def test_no_tenant_means_no_cache(self):
        counter = _Counter()
        with llm_context.bound():
            _ask(counter, org=None)
            _ask(counter, org=None)
        assert len(counter.calls) == 2

    def test_a_rejected_response_is_not_stored(self):
        counter, org = _Counter("not json"), _org()
        _ask(counter, org=org, accept=lambda t: t.startswith("{"))
        _ask(counter, org=org, accept=lambda t: t.startswith("{"))
        assert len(counter.calls) == 2

    def test_a_truncated_response_is_not_stored(self):
        counter, org = _Counter(stop_reason="max_tokens"), _org()
        _ask(counter, org=org)
        _ask(counter, org=org)
        assert len(counter.calls) == 2

    def test_a_failed_call_is_not_stored(self):
        org = _org()

        def _boom(**kw):
            raise llm_client.LLMUnavailable("down")
        with pytest.raises(llm_client.LLMUnavailable):
            _ask(_boom, org=org)
        counter = _Counter()
        _ask(counter, org=org)
        assert len(counter.calls) == 1


class TestBypass:
    def test_bypass_calls_and_replaces(self):
        org = _org()
        _ask(_Counter("old"), org=org)
        fresh = _Counter("new")
        out = _ask(fresh, org=org, bypass_cache=True)
        assert len(fresh.calls) == 1
        assert llm_cache.response_text(out) == "new"
        idle = _Counter("never")
        again = _ask(idle, org=org)
        assert idle.calls == []
        assert llm_cache.response_text(again) == "new"


class TestNeverCached:
    @pytest.mark.parametrize("kind", ["consult", "search_answer"])
    def test_chat_and_search_answers_are_refused(self, kind):
        counter = _Counter()
        with pytest.raises(ValueError):
            _ask(counter, org=_org(), kind=kind)
        assert counter.calls == []


class TestExpiryAndCap:
    def _age(self, org, days):
        with _db.session_scope() as sess:
            for row in sess.query(_db.LlmResultCache).filter_by(org_id=org):
                row.created_at = row.created_at - timedelta(days=days)

    def test_an_expired_row_is_a_miss(self):
        counter, org = _Counter(), _org()
        _ask(counter, org=org)
        self._age(org, 31)
        _ask(counter, org=org)
        assert len(counter.calls) == 2

    def test_ttl_follows_the_setting(self, monkeypatch):
        monkeypatch.setenv("LLM_RESULT_CACHE_DAYS", "5")
        counter, org = _Counter(), _org()
        _ask(counter, org=org)
        self._age(org, 6)
        _ask(counter, org=org)
        assert len(counter.calls) == 2

    def test_the_sweep_purges_expired_rows(self):
        org = _org()
        _ask(_Counter(), org=org, content="old")
        self._age(org, 31)
        _ask(_Counter(), org=org, content="new")
        out = retention.sweep_expired()
        assert out.get("AI result cache", 0) >= 1
        with _db.session_scope() as sess:
            left = sess.query(_db.LlmResultCache).filter_by(org_id=org).all()
            assert len(left) == 1

    def test_cap_evicts_the_least_recently_used(self, monkeypatch):
        monkeypatch.setenv("LLM_RESULT_CACHE_MAX_ROWS", "3")
        org = _org()
        for name in ("a", "b", "c"):
            _ask(_Counter(name), org=org, content=name)
        # Make the order explicit rather than trusting clock resolution:
        # "a" used most recently, "b" least.
        now = _db._utcnow()
        with _db.session_scope() as sess:
            for row in sess.query(_db.LlmResultCache).filter_by(org_id=org):
                row.last_hit_at = {
                    "a": now, "b": now - timedelta(hours=2),
                    "c": now - timedelta(hours=1)}[row.response_text]
        _ask(_Counter("d"), org=org, content="d")
        with _db.session_scope() as sess:
            texts = sorted(r.response_text for r in sess.query(
                _db.LlmResultCache).filter_by(org_id=org))
        assert texts == ["a", "c", "d"]

    def test_the_cap_is_per_org(self, monkeypatch):
        monkeypatch.setenv("LLM_RESULT_CACHE_MAX_ROWS", "1")
        org_a, org_b = _org(), _org()
        _ask(_Counter("a"), org=org_a)
        _ask(_Counter("b"), org=org_b)
        with _db.session_scope() as sess:
            assert sess.query(_db.LlmResultCache).filter_by(
                org_id=org_a).count() == 1


class TestProjectRemoval:
    def test_deleting_a_project_drops_its_cached_results(self):
        org = _org()
        pid = _db.upsert_project("Cache " + secrets.token_hex(3),
                                 owner_sid="sid_" + secrets.token_hex(4))
        with llm_context.bound(org_id=org, project_id=pid):
            _ask(_Counter(), org=None)
        with llm_context.bound(org_id=org, project_id="other"):
            _ask(_Counter(), org=None, content="elsewhere")
        report = retention.delete_project_data(pid, org_id=None)
        assert report.ok
        with _db.session_scope() as sess:
            rows = sess.query(_db.LlmResultCache).filter_by(org_id=org).all()
            assert [r.project_id for r in rows] == ["other"]
