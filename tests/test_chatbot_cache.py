"""Tests for Sprint 3 Task 3.2 — Anthropic prompt caching.

Three layers of coverage:

1. **Block shape** — ``_ai_system_blocks`` returns a list whose first
   block is text-typed, carries ``cache_control={"type": "ephemeral"}``,
   and is large enough to actually be cacheable (>= 3700 chars, a rough
   proxy for the 1024-token Anthropic minimum at ~3.5 chars per token).
2. **Call-site** — ``GET /chat/stream`` forwards the blocks list as
   ``system=`` to ``client.messages.stream``. We patch the SDK and
   inspect the captured kwargs.
3. **Mockup vision** — the parallel ``_SYSTEM_BLOCKS`` constant in
   ``engine.mockup_vision`` has the same shape.

See ``docs/plans/sprint_3_performance.md`` Task 3.2 for the full spec.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest


# Rough character-count proxy for the 1024-token Anthropic cache
# minimum. Real tokenisation varies (~3.5 chars / token for English,
# more for Cyrillic) but 3700 is a safe lower bound for an English-y
# prompt and conservative for Ukrainian content.
_CACHE_MIN_CHARS = 3700


# ── Block-shape tests ─────────────────────────────────────────────

class TestSystemBlockShape:
    """Pure-Python shape assertions — no Anthropic SDK involved."""

    def test_en_blocks_have_ephemeral_cache_control(self):
        from engine import chatbot

        blocks = chatbot._ai_system_blocks("en")
        assert isinstance(blocks, list), "system blocks must be a list"
        assert len(blocks) >= 1, "expected at least one block"
        first = blocks[0]
        assert isinstance(first, dict)
        assert first.get("type") == "text"
        assert first.get("cache_control") == {"type": "ephemeral"}
        text = first.get("text") or ""
        assert len(text) >= _CACHE_MIN_CHARS, (
            f"EN persona is only {len(text)} chars — needs >= "
            f"{_CACHE_MIN_CHARS} to clear Anthropic's 1024-token cache "
            "threshold"
        )

    def test_ua_blocks_have_ephemeral_cache_control(self):
        from engine import chatbot

        blocks = chatbot._ai_system_blocks("ua")
        assert isinstance(blocks, list)
        assert len(blocks) >= 1
        first = blocks[0]
        assert first.get("type") == "text"
        assert first.get("cache_control") == {"type": "ephemeral"}
        text = first.get("text") or ""
        assert len(text) >= _CACHE_MIN_CHARS, (
            f"UA persona is only {len(text)} chars — needs >= "
            f"{_CACHE_MIN_CHARS} to clear Anthropic's 1024-token cache "
            "threshold"
        )

    def test_en_and_ua_blocks_have_distinct_text(self):
        """Different language blocks must produce different cache keys."""
        from engine import chatbot

        en_text = chatbot._ai_system_blocks("en")[0]["text"]
        ua_text = chatbot._ai_system_blocks("ua")[0]["text"]
        assert en_text != ua_text, (
            "EN and UA system blocks must differ so they cache under "
            "distinct keys"
        )

    def test_legacy_string_wrapper_returns_concatenation(self):
        """``_ai_system_prompt`` (legacy) must still return a string and
        match the joined text of the blocks list."""
        from engine import chatbot

        prompt = chatbot._ai_system_prompt("en")
        assert isinstance(prompt, str)
        assert prompt.strip()
        blocks_text = "".join(
            b.get("text", "") for b in chatbot._ai_system_blocks("en")
        )
        assert prompt == blocks_text


# ── Mockup-vision block shape ────────────────────────────────────

class TestMockupVisionBlocks:
    def test_system_blocks_have_ephemeral_cache_control(self):
        from engine import mockup_vision

        blocks = mockup_vision._SYSTEM_BLOCKS
        assert isinstance(blocks, list)
        assert len(blocks) >= 1
        first = blocks[0]
        assert isinstance(first, dict)
        assert first.get("type") == "text"
        assert first.get("cache_control") == {"type": "ephemeral"}
        text = first.get("text") or ""
        assert len(text) >= _CACHE_MIN_CHARS, (
            f"Mockup-vision system prompt is only {len(text)} chars — "
            f"needs >= {_CACHE_MIN_CHARS} to be cacheable"
        )

    def test_legacy_string_still_exposed(self):
        """``_SYSTEM_PROMPT`` is still importable for any tooling that
        reads the prompt as a single string."""
        from engine import mockup_vision

        assert isinstance(mockup_vision._SYSTEM_PROMPT, str)
        assert mockup_vision._SYSTEM_PROMPT.strip()


# ── End-to-end: /chat/stream forwards system-blocks list ──────────
#
# We reuse the fake-Anthropic plumbing pattern from test_chat_stream.py
# (S3.1) and assert that the captured ``system=`` kwarg is a list whose
# first element carries the ``cache_control`` marker.

class _FakeUsage:
    """Minimal usage shim — exposes the four numeric fields the route
    logs. Defaults to zeros so the cache fields are exercised."""
    input_tokens = 10
    output_tokens = 5
    cache_creation_input_tokens = 7
    cache_read_input_tokens = 0


class _FakeFinalMessage:
    def __init__(self, joined_text: str):
        self.usage = _FakeUsage()
        self.content = [type("Block", (), {"text": joined_text})()]


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.text_stream = iter(self._chunks)

    def get_final_message(self):
        return _FakeFinalMessage("".join(self._chunks))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeMessages:
    def __init__(self, chunks):
        self._chunks = chunks
        self.last_kwargs: dict | None = None

    def stream(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeStream(self._chunks)


class _FakeAnthropic:
    """Drop-in for ``anthropic.Anthropic`` that records the kwargs."""

    instances: list["_FakeAnthropic"] = []

    def __init__(self, *, api_key=None, chunks=None, **_kwargs):
        self.api_key = api_key
        self._chunks = chunks if chunks is not None else ["Hi.", " There."]
        self.messages = _FakeMessages(self._chunks)
        _FakeAnthropic.instances.append(self)


@contextmanager
def _install_fake_anthropic(monkeypatch, chunks):
    import sys
    import types

    fake_mod = types.ModuleType("anthropic")

    def _factory(*, api_key=None, **kwargs):
        return _FakeAnthropic(api_key=api_key, chunks=chunks)

    fake_mod.Anthropic = _factory
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    _FakeAnthropic.instances.clear()
    yield


def _drain(resp) -> bytes:
    out = b""
    for chunk in resp.response:
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        out += chunk
    return out


class TestChatStreamForwardsSystemBlocks:
    def test_system_kwarg_is_a_list_with_cache_control(self, client, monkeypatch):
        """The streaming route must pass ``system=`` as a list of blocks,
        not the legacy string form. The first block carries
        ``cache_control={"type": "ephemeral"}``."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        # "Explain how SSE works" doesn't match any fast-path handler,
        # so the route goes through the LLM branch — same approach as
        # tests/test_chat_stream.py::test_sse_shape_with_mocked_stream.
        chunks = ["ok"]
        with _install_fake_anthropic(monkeypatch, chunks):
            resp = client.get(
                "/chat/stream",
                query_string={"message": "Explain how SSE works", "lang": "en"},
            )
            assert resp.status_code == 200
            _drain(resp)  # exhaust the generator so kwargs are captured

        assert len(_FakeAnthropic.instances) == 1, (
            "expected exactly one Anthropic() construction on the LLM path"
        )
        kwargs = _FakeAnthropic.instances[0].messages.last_kwargs
        assert kwargs is not None, "messages.stream was never called"
        system = kwargs.get("system")
        assert isinstance(system, list), (
            f"system= must be a list of blocks for caching, got "
            f"{type(system).__name__}"
        )
        assert len(system) >= 1
        first = system[0]
        assert isinstance(first, dict)
        assert first.get("type") == "text"
        assert first.get("cache_control") == {"type": "ephemeral"}

    def test_ua_path_passes_ua_blocks(self, client, monkeypatch):
        """Switching ``lang=ua`` must forward the UA block, not EN."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        chunks = ["ok"]
        with _install_fake_anthropic(monkeypatch, chunks):
            resp = client.get(
                "/chat/stream",
                query_string={"message": "Explain how SSE works", "lang": "ua"},
            )
            assert resp.status_code == 200
            _drain(resp)

        from engine import chatbot

        kwargs = _FakeAnthropic.instances[0].messages.last_kwargs
        assert kwargs["system"][0]["text"] == \
            chatbot._ai_system_blocks("ua")[0]["text"]
