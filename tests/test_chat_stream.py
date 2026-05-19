"""Tests for ``GET /chat/stream`` — Server-Sent Events streaming endpoint.

The route mirrors ``POST /chat`` but emits ``event: meta`` / ``event: delta``
/ ``event: done`` frames so the frontend can render tokens as they arrive.
A fast-path (greeting / guide / istqb / bug_form) short-circuits the LLM
entirely and is delivered as ``event: full`` + ``event: done``.

See ``docs/plans/sprint_3_performance.md`` § Task 3.1 for the full spec.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest


# ── Fake Anthropic SDK plumbing ─────────────────────────────────────

class _FakeUsage(dict):
    """Minimal usage shim — the route only logs it, never indexes it."""


class _FakeFinalMessage:
    def __init__(self, joined_text: str):
        self.usage = _FakeUsage()
        # Mimic the SDK's Message.content[].text shape.
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
        self.last_kwargs = None

    def stream(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeStream(self._chunks)


class _FakeAnthropic:
    """Drop-in for ``anthropic.Anthropic`` that yields canned chunks.

    Wired into ``sys.modules['anthropic']`` so the route's lazy
    ``from anthropic import Anthropic`` inside the generator picks up
    the fake instead of the real SDK.
    """
    instances: list["_FakeAnthropic"] = []

    def __init__(self, *, api_key: str | None = None, chunks=None):
        self.api_key = api_key
        self._chunks = chunks if chunks is not None else ["Hello ", "world", "!"]
        self.messages = _FakeMessages(self._chunks)
        _FakeAnthropic.instances.append(self)


@contextmanager
def _install_fake_anthropic(monkeypatch, chunks):
    """Patch ``sys.modules['anthropic']`` so the route picks up the fake.

    The route does ``from anthropic import Anthropic`` lazily inside
    the generator, so monkeypatching the module entry is sufficient.
    """
    import sys
    import types

    fake_mod = types.ModuleType("anthropic")

    def _factory(*, api_key=None, **kwargs):  # mimics SDK signature
        return _FakeAnthropic(api_key=api_key, chunks=chunks)

    fake_mod.Anthropic = _factory
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    _FakeAnthropic.instances.clear()
    # Do NOT clear on exit — tests need to inspect ``instances`` after
    # the response generator has drained.
    yield


def _collect_bytes(resp) -> bytes:
    """Drain a Flask streaming response into a single bytes blob."""
    out = b""
    for chunk in resp.response:
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        out += chunk
    return out


def _frames(payload: bytes) -> list[tuple[str, str]]:
    """Split SSE wire bytes into ``(event_name, data)`` pairs.

    Heartbeat comment lines (``: heartbeat``) are skipped — they have
    no ``event:`` header. Order is preserved.
    """
    text = payload.decode("utf-8", errors="replace")
    out: list[tuple[str, str]] = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        if block.startswith(":"):
            continue  # comment / heartbeat
        ev_name = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                ev_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        if ev_name:
            out.append((ev_name, data))
    return out


# ── Tests ───────────────────────────────────────────────────────────

class TestChatStreamSSE:
    def test_sse_shape_with_mocked_stream(self, client, monkeypatch):
        """A non-fast-path message hits the LLM, emits meta + 3 deltas + done."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        # "Explain test pyramid" doesn't match greeting / glossary / istqb
        # cleanly, so try_fast_path returns None and we hit the LLM path.
        chunks = ["Hello ", "world", "!"]
        with _install_fake_anthropic(monkeypatch, chunks):
            resp = client.get(
                "/chat/stream",
                query_string={"message": "Explain how SSE works", "lang": "en"},
            )
            assert resp.status_code == 200
            assert resp.mimetype == "text/event-stream"
            assert resp.headers.get("X-Accel-Buffering") == "no"
            assert resp.headers.get("Cache-Control") == "no-cache"
            payload = _collect_bytes(resp)

        # SDK was actually instantiated (LLM path was taken).
        assert len(_FakeAnthropic.instances) == 1

        # Chunks made it onto the wire verbatim.
        for needle in (b"Hello ", b"world", b"!"):
            assert needle in payload, f"missing {needle!r} in {payload!r}"

        frames = _frames(payload)
        event_names = [name for name, _ in frames]
        # meta is the first non-heartbeat frame.
        assert event_names[0] == "meta", frames
        # Three deltas in order.
        delta_frames = [d for n, d in frames if n == "delta"]
        assert len(delta_frames) == 3, delta_frames
        # Final frame is done.
        assert event_names[-1] == "done"

    def test_fast_path_bypass_does_not_call_anthropic(self, client, monkeypatch):
        """A greeting must short-circuit before any SDK construction."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        # If the route ever tries to import anthropic on this code path,
        # constructing _BoomAnthropic raises — test fails immediately.
        import sys
        import types

        class _BoomAnthropic:
            def __init__(self, *args, **kwargs):
                raise AssertionError(
                    "Fast-path should not instantiate Anthropic"
                )

        fake_mod = types.ModuleType("anthropic")
        fake_mod.Anthropic = _BoomAnthropic
        monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

        resp = client.get(
            "/chat/stream",
            query_string={"message": "hi", "lang": "en"},
        )
        assert resp.status_code == 200
        payload = _collect_bytes(resp)

        frames = _frames(payload)
        event_names = [n for n, _ in frames]
        assert "full" in event_names, frames
        assert event_names[-1] == "done"
        # No meta frame — meta is only emitted on the LLM path.
        assert "meta" not in event_names

    def test_missing_api_key_falls_back_to_rules(self, client, monkeypatch):
        """No ANTHROPIC_API_KEY → rule-based reply via event: full + done."""
        # Strip the key for the duration of the test.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        # "broken" routes to the troubleshooter — clearly past the fast
        # path, clearly past the LLM (no key), so it lands in
        # rule_based_fallback's troubleshoot branch.
        resp = client.get(
            "/chat/stream",
            query_string={"message": "automation is broken", "lang": "en"},
        )
        assert resp.status_code == 200
        payload = _collect_bytes(resp)

        frames = _frames(payload)
        event_names = [n for n, _ in frames]
        # Single full + done — no meta / delta because we never reached
        # the LLM path.
        assert "full" in event_names, frames
        assert "done" in event_names
        assert "delta" not in event_names
        assert "meta" not in event_names

    # NOTE: heartbeat behaviour (a ': heartbeat' comment after >10 s of
    # token silence) is exercised in production but skipped here — a
    # deterministic unit test would need to either patch ``time.monotonic``
    # AND stall the fake stream's iterator, which adds significant test
    # complexity for a behaviour that's literally a single ``if`` branch
    # in the route. The heartbeat code path is covered by the
    # observability of Render's edge logs in staging.
