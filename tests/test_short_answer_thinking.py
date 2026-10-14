"""Short-capped calls must not spend their cap on thinking (2026-10-14).

Live on testforge.qarea.org, every grounded Ask came back with zero answer
text while the usage panel billed exactly 300 output tokens — the Ask cap.
Claude Haiku 5.5 runs adaptive thinking unless told not to; thinking is
billed as output but never reaches ``text_stream``, so the whole budget
went where nobody sees it and the stream ended as if it had answered.

The fakes here behave like that model: a call that does not switch
thinking off streams nothing and stops on ``max_tokens``. So "the answer
arrives" is only true if the request really carried the off switch.
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from engine import llm_client, llm_keys, llm_models
from tests.test_search_ask import (  # noqa: F401 — fixtures by import
    GOOD_ANSWER, FakeLLM, _ask, _text, ask_on, llm, shop)


def _thinks(kwargs: dict) -> bool:
    """Whether a request to Haiku 5.5 would think: on unless disabled."""
    return (kwargs.get("thinking") or {}).get("type") != "disabled"


# ── The table ─────────────────────────────────────────────────────────

class TestShortAnswerParams:
    @pytest.mark.parametrize("model, expected", [
        ("claude-haiku-5-5", {"thinking": {"type": "disabled"}}),
        ("claude-sonnet-5-5", {"thinking": {"type": "between_tools"}}),
        ("claude-sonnet-5", {"thinking": {"type": "disabled"}}),
        # Opus 5.5 rejects "disabled" with a 400 — it must not be read as
        # its prefix claude-opus-5.
        ("claude-opus-5-5", {"output_config": {"effort": "low"}}),
        ("claude-opus-5", {"thinking": {"type": "disabled"}}),
        ("claude-fable-5-1", {"output_config": {"effort": "low"}}),
        ("CLAUDE-HAIKU-5-5", {"thinking": {"type": "disabled"}}),
    ])
    def test_each_family(self, model, expected):
        assert llm_models.short_answer_params(model) == expected

    @pytest.mark.parametrize("model", [
        "claude-haiku-4-5", "claude-opus-4-8", "claude-sonnet-4-6",
        "something-else", "", None])
    def test_models_that_do_not_think_by_default_get_nothing(self, model):
        assert llm_models.short_answer_params(model) == {}

    def test_returns_a_copy(self):
        llm_models.short_answer_params("claude-haiku-5-5")["thinking"][
            "type"] = "adaptive"
        assert llm_models.short_answer_params("claude-haiku-5-5") == {
            "thinking": {"type": "disabled"}}


# ── Ask ───────────────────────────────────────────────────────────────

class ThinkingLLM(FakeLLM):
    """Haiku 5.5 as observed live: without the off switch the stream is
    empty and the cap is spent."""

    def factory(self, api_key):
        client = super().factory(api_key)
        inner = client.messages.stream
        fake = self

        def stream(**kwargs):
            if _thinks(kwargs):
                fake.chunks, fake.usage = [], (1620, 300)
                fake.stop_reason = "max_tokens"
            else:
                fake.chunks, fake.usage = list(GOOD_ANSWER), (1620, 80)
                fake.stop_reason = "end_turn"
            return inner(**kwargs)

        return SimpleNamespace(messages=SimpleNamespace(stream=stream))


@pytest.fixture
def thinking_llm(monkeypatch):
    from engine.search import answer
    fake = ThinkingLLM()
    monkeypatch.setattr(answer, "_client_factory", fake.factory)
    return fake


class TestAsk:
    def test_the_answer_arrives_on_haiku(self, client, shop, thinking_llm):
        events = _ask(client, shop)
        assert thinking_llm.calls[-1]["model"] == "claude-haiku-5-5"
        assert thinking_llm.calls[-1]["thinking"] == {"type": "disabled"}
        assert "Login with valid credentials" in _text(events)
        assert not [e for e in events if e["type"] == "error"]

    def test_no_visible_text_is_an_error_not_silence(self, client, shop,
                                                     llm):
        llm.chunks, llm.usage, llm.stop_reason = [], (1620, 300), "max_tokens"
        events = _ask(client, shop)
        assert _text(events) == ""
        errors = [e for e in events if e["type"] == "error"]
        assert [(e["code"], e.get("reason")) for e in errors] == [
            ("upstream", "empty_answer")]
        done = events[-1]
        assert done["type"] == "done"
        assert done["usage"]["output_tokens"] == 300   # still billed, shown
        assert done["answer_key"] is None

    def test_an_empty_answer_is_not_cached(self, client, shop, llm):
        llm.chunks, llm.stop_reason = [], "end_turn"
        _ask(client, shop)
        llm.chunks = list(GOOD_ANSWER)
        events = _ask(client, shop)
        assert len(llm.calls) == 2
        assert "Login with valid credentials" in _text(events)


# ── The non-streamed chokepoint ───────────────────────────────────────

@pytest.fixture
def sdk(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-platform-test")
    monkeypatch.delenv(llm_keys.ENCRYPTION_KEY_ENV, raising=False)
    for name in ("ANTHROPIC_MODEL", "LLM_MODEL_FOR_CONSULT",
                 "LLM_CONSULT_TIER", "ANTHROPIC_MODEL_HAIKU"):
        monkeypatch.delenv(name, raising=False)
    seen: list[dict] = []

    class _Messages:
        @staticmethod
        def create(**kwargs):
            seen.append(kwargs)
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                      cache_read_input_tokens=0,
                                      cache_creation_input_tokens=0),
                content=[])

    monkeypatch.setattr(llm_client, "_build_client",
                        lambda api_key=None: SimpleNamespace(
                            messages=_Messages()))
    monkeypatch.setattr(llm_client, "_retryable_exception_types", lambda: ())
    return seen


class TestCallMessages:
    def _call(self, **kw):
        return llm_client.call_messages(
            kind="consult", max_tokens=600,
            messages=[{"role": "user", "content": "hi"}], **kw)

    def test_short_answer_adds_the_routed_models_switch(self, sdk):
        self._call(short_answer=True)
        assert sdk[-1]["model"] == "claude-haiku-5-5"
        assert sdk[-1]["thinking"] == {"type": "disabled"}
        assert "short_answer" not in sdk[-1]

    def test_off_by_default(self, sdk):
        self._call()
        assert "thinking" not in sdk[-1]

    def test_the_caller_wins(self, sdk):
        self._call(short_answer=True, thinking={"type": "adaptive"})
        assert sdk[-1]["thinking"] == {"type": "adaptive"}

    def test_follows_the_model_actually_sent(self, sdk):
        self._call(short_answer=True, model="claude-opus-5-5")
        assert "thinking" not in sdk[-1]
        assert sdk[-1]["output_config"] == {"effort": "low"}


def test_the_tedgie_chat_asks_for_a_short_answer(monkeypatch):
    from engine import chatbot
    seen: dict = {}

    def fake_call(**kwargs):
        seen.update(kwargs)
        raise llm_client.LLMUnavailable("stop here")

    monkeypatch.setattr(chatbot, "call_messages", fake_call)
    monkeypatch.setattr(chatbot, "_ANTHROPIC_OK", True)
    chatbot._ai_respond("explain the test pyramid", "en")
    assert seen.get("short_answer") is True


# ── The streamed Tedgie ───────────────────────────────────────────────

def test_the_streamed_chat_turns_thinking_off(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    for name in ("ANTHROPIC_MODEL", "LLM_MODEL_FOR_CONSULT",
                 "LLM_CONSULT_TIER", "ANTHROPIC_MODEL_HAIKU"):
        monkeypatch.delenv(name, raising=False)
    calls: list[dict] = []

    class _Stream:
        def __init__(self, kwargs):
            self._kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        @property
        def text_stream(self):
            if not _thinks(self._kwargs):
                yield from ["Hello ", "world"]

        def get_final_message(self):
            return SimpleNamespace(usage=SimpleNamespace(
                input_tokens=1, output_tokens=1, cache_read_input_tokens=0,
                cache_creation_input_tokens=0), stop_reason="end_turn")

    def factory(*, api_key=None, **kw):
        def stream(**kwargs):
            calls.append(kwargs)
            return _Stream(kwargs)
        return SimpleNamespace(messages=SimpleNamespace(stream=stream))

    mod = types.ModuleType("anthropic")
    mod.Anthropic = factory
    monkeypatch.setitem(sys.modules, "anthropic", mod)

    resp = client.get("/chat/stream", query_string={
        "message": "Explain how SSE works", "lang": "en"})
    payload = b"".join(c if isinstance(c, bytes) else c.encode()
                       for c in resp.response)
    assert calls, "the LLM path was not taken"
    assert calls[-1]["thinking"] == {"type": "disabled"}
    assert b"Hello " in payload
