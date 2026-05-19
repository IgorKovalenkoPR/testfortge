"""Unit tests for :mod:`engine.llm_client`.

The wrapper exists to give every Anthropic call in the codebase the same
60 s timeout and 3-attempt exponential-backoff retry policy. These tests
monkeypatch ``anthropic.Anthropic`` so nothing hits the network.

Implements Sprint 1 Task 6 of TestForTge.
"""
from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from engine import llm_client


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeMessages:
    """Stand-in for ``Anthropic().messages``. Counts ``create`` calls and
    pops a scripted side-effect per call: an exception is raised, anything
    else is returned verbatim."""

    def __init__(self, side_effects: list[Any]):
        self._side_effects = list(side_effects)
        self.calls: list[dict] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._side_effects:
            raise AssertionError("FakeMessages ran out of side effects")
        effect = self._side_effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        return effect


class _FakeAnthropic:
    """Stand-in for the ``anthropic.Anthropic`` constructor."""

    last_instance: "_FakeAnthropic | None" = None

    def __init__(self, *, api_key: str | None = None, **_: Any):
        self.api_key = api_key
        self.messages = _FakeMessages(self._next_side_effects)
        _FakeAnthropic.last_instance = self

    # The test sets the side_effects list on the *class* before triggering
    # construction; the instance copies it on init.
    _next_side_effects: list[Any] = []


# ---------------------------------------------------------------------------
# Fixture: stub the anthropic module to expose deterministic exception
# classes and a controllable Anthropic constructor.
# ---------------------------------------------------------------------------

class _StubAPIConnectionError(Exception):
    pass


class _StubAPITimeoutError(Exception):
    pass


class _StubRateLimitError(Exception):
    pass


class _StubInternalServerError(Exception):
    pass


@pytest.fixture
def anthropic_stub(monkeypatch: pytest.MonkeyPatch):
    """Install a fake ``anthropic`` module on sys.modules.

    Returns a small handle the test uses to script call outcomes.
    """
    fake_mod = types.ModuleType("anthropic")
    fake_mod.Anthropic = _FakeAnthropic  # type: ignore[attr-defined]
    fake_mod.APIConnectionError = _StubAPIConnectionError  # type: ignore[attr-defined]
    fake_mod.APITimeoutError = _StubAPITimeoutError  # type: ignore[attr-defined]
    fake_mod.RateLimitError = _StubRateLimitError  # type: ignore[attr-defined]
    fake_mod.InternalServerError = _StubInternalServerError  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    # Always run with an API key by default.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-xxxxx")

    # Speed up retries — wait_exponential sleeps real seconds otherwise.
    # tenacity calls ``time.sleep`` via its ``nap`` submodule; patching
    # ``time.sleep`` globally covers both modern and older releases.
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda _s: None)
    try:
        import tenacity.nap as _nap
        monkeypatch.setattr(_nap, "sleep", lambda _s: None, raising=False)
    except Exception:
        pass

    handle = types.SimpleNamespace(
        set_side_effects=lambda effects: setattr(
            _FakeAnthropic, "_next_side_effects", effects
        ),
        last_client=lambda: _FakeAnthropic.last_instance,
        APITimeoutError=_StubAPITimeoutError,
        APIConnectionError=_StubAPIConnectionError,
        RateLimitError=_StubRateLimitError,
        InternalServerError=_StubInternalServerError,
    )
    _FakeAnthropic.last_instance = None
    _FakeAnthropic._next_side_effects = []
    yield handle


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_retries_on_api_timeout(anthropic_stub):
    """Two transient APITimeoutError raises then success — call_messages
    returns the eventual response."""
    ok_resp = object()
    anthropic_stub.set_side_effects([
        anthropic_stub.APITimeoutError("attempt 1"),
        anthropic_stub.APITimeoutError("attempt 2"),
        ok_resp,
    ])

    result = llm_client.call_messages(
        model="claude-sonnet-4-5",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
    )

    assert result is ok_resp
    client = anthropic_stub.last_client()
    assert client is not None
    assert len(client.messages.calls) == 3


def test_gives_up_after_three_attempts(anthropic_stub):
    """Always-failing transient errors raise LLMUnavailable after 3 tries."""
    anthropic_stub.set_side_effects([
        anthropic_stub.RateLimitError("nope 1"),
        anthropic_stub.RateLimitError("nope 2"),
        anthropic_stub.RateLimitError("nope 3"),
    ])

    with pytest.raises(llm_client.LLMUnavailable):
        llm_client.call_messages(
            model="claude-sonnet-4-5",
            max_tokens=100,
            messages=[{"role": "user", "content": "hi"}],
        )

    client = anthropic_stub.last_client()
    assert client is not None
    assert len(client.messages.calls) == 3


def test_missing_api_key_raises_llm_unavailable_immediately(
    anthropic_stub, monkeypatch
):
    """No ANTHROPIC_API_KEY → fast-fail with LLMUnavailable, zero SDK
    calls, zero retries."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(llm_client.LLMUnavailable):
        llm_client.call_messages(
            model="claude-sonnet-4-5",
            max_tokens=100,
            messages=[{"role": "user", "content": "hi"}],
        )

    # Constructor was never called — no client instance ever existed.
    assert anthropic_stub.last_client() is None


def test_timeout_kwarg_propagates(anthropic_stub):
    """The 60 s default timeout must be forwarded to messages.create."""
    ok_resp = object()
    anthropic_stub.set_side_effects([ok_resp])

    llm_client.call_messages(
        model="claude-sonnet-4-5",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
    )

    client = anthropic_stub.last_client()
    assert client is not None
    assert len(client.messages.calls) == 1
    assert client.messages.calls[0].get("timeout") == 60
