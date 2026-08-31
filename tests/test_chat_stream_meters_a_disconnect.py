"""Tokens spent on an abandoned stream still have to be counted.

Found by walking Tedgie, 2026-08-31. ``/chat/stream`` meters its token
usage after the delta loop::

    for text in stream.text_stream:
        yield _sse("delta", {"text": text})
    final = stream.get_final_message()
    ...
    _meter_stream(final)
    _append_history(message, reply_dict)
    yield _sse("done", ...)

When the client goes away mid-answer, ``GeneratorExit`` is raised *at the
yield*, so none of that runs. The route notices — there is an
``except GeneratorExit`` that logs "SSE client disconnected mid-stream" —
and meters nothing.

The tokens were generated and billed upstream regardless; closing a tab
does not refund them. So the meter under-reports by exactly the abandoned
streams, and the comment above ``_meter_stream`` names that as the thing
it must not do:

    Tedgie is the highest-volume LLM surface in the product, so a usage
    report that omitted it would understate spend by more than everything
    else combined.

Disconnecting is not exotic. Closing the tab, navigating away and losing
the network all do it, and Render's proxy kills idle connections at 30 s
— which is why this route emits heartbeats at all.

``anthropic.lib.streaming.MessageStream`` exposes
``current_message_snapshot`` (SDK 0.97.0, verified), and
``llm_cost.extract_usage`` is duck-typed with every field optional, so
the partial snapshot meters through the same path as a completed one.
"""
from __future__ import annotations

import uuid

import pytest

from engine import db as _db


class _FakeUsage:
    input_tokens = 1200
    output_tokens = 40
    cache_read_input_tokens = 900
    cache_creation_input_tokens = 0


class _FakeSnapshot:
    usage = _FakeUsage()


class _FakeStream:
    """Yields deltas forever, so the test controls when it stops."""

    def __init__(self):
        self.current_message_snapshot = _FakeSnapshot()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        while True:
            yield "word "

    def get_final_message(self):        # pragma: no cover — never reached
        raise AssertionError("the test disconnects before the stream ends")


class _FakeMessages:
    def stream(self, **kwargs):
        return _FakeStream()


class _FakeAnthropic:
    def __init__(self, *a, **kw):
        self.messages = _FakeMessages()


@pytest.fixture()
def streaming(client, monkeypatch):
    """A signed-in client whose /chat/stream reaches the LLM branch."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)
    client.post("/projects/db/create",
                data={"project_name": f"Chat {uuid.uuid4().hex[:6]}"},
                follow_redirects=True)
    return client


def _usage_rows() -> int:
    """How many llm_usage rows exist, across everything."""
    from engine.db import session_scope, LlmUsage
    with session_scope() as sess:
        return sess.query(LlmUsage).count()


def _disconnect_after_two_frames(client) -> int:
    """Start the stream, read a little, then hang up. Returns frames read."""
    resp = client.get(
        "/chat/stream?message=how+do+I+measure+regression+coverage",
        buffered=False)
    seen = 0
    it = resp.iter_encoded()
    try:
        for _chunk in it:
            seen += 1
            if seen >= 3:
                break
    finally:
        # What a browser does when the tab closes.
        it.close()
        resp.close()
    return seen


def test_the_stream_really_started(streaming):
    # Guard the guard: if the route took a fast path or refused, the
    # assertion below would pass with no LLM call to meter.
    assert _disconnect_after_two_frames(streaming) >= 3


def test_an_abandoned_stream_is_still_metered(streaming):
    before = _usage_rows()
    _disconnect_after_two_frames(streaming)
    after = _usage_rows()
    assert after > before, (
        "the client hung up mid-answer and the tokens went unrecorded")


def test_what_is_recorded_is_what_the_snapshot_knew(streaming):
    from engine.db import session_scope, LlmUsage
    _disconnect_after_two_frames(streaming)
    with session_scope() as sess:
        row = sess.query(LlmUsage).order_by(LlmUsage.id.desc()).first()
    assert row is not None
    assert row.input_tokens == 1200, row.input_tokens
    assert row.cache_read_tokens == 900, row.cache_read_tokens
    assert row.kind == "consult", row.kind


def test_metering_a_disconnect_does_not_double_count(streaming):
    # The handler runs on the way out of the generator. If it also ran on
    # the normal path, a completed reply would be counted twice — and
    # that is a worse report than the one this fixes.
    before = _usage_rows()
    _disconnect_after_two_frames(streaming)
    assert _usage_rows() == before + 1
