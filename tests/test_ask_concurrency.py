"""Races on the Ask path and the two LLM caches, forced rather than hoped for.

gunicorn runs ``gthread`` with four threads in the container, so every
in-process limiter and every check-then-write on these paths is reachable
by two requests at once. Spawning eight threads and hoping the scheduler
interleaves them proves nothing — an 8-thread race test once passed 10/10
over a live lost-update bug. Each test here instead **injects the competing
operation at the critical point** through a monkeypatched seam:

* the per-user token bucket — the competitor is started *inside* the first
  request's locked section (from the bucket dict's ``get``) and given time
  to run; with the lock it cannot get in until the first is done;
* the daily org cap — the competitor runs *inside* the first request's
  model call (from the fake SDK's stream), which is exactly the window
  between "cap checked" and "usage row written"; and, for the gate's own
  lock, between its read of the in-flight count and its write;
* the two cache puts — the competitor runs its whole put *between* the
  first put's read (``sess.get`` → no row) and its INSERT.

Each "with the lock" test has a twin that removes the lock and asserts the
race *does* happen, so the forcing demonstrably reaches the window: without
that twin a test that never interleaved would pass on broken code too.
The two fixes (``DailyCapGate`` and the upsert retry) were checked the same
way against the code before them — see the commit message.
"""
from __future__ import annotations

import contextlib
import logging
import secrets
import threading


from engine import db as _db
from engine import llm_cache
from engine.search import answer
from tests.test_search_ask import (  # noqa: F401 — fixtures
    GOOD_Q, FakeLLM, ask_on, shop,
)

#: How long the first party waits, inside its critical section, for the
#: competitor. Long enough for an unblocked competitor to finish (it takes
#: milliseconds); a blocked one simply times out here.
COMPETITOR_WINDOW_S = 0.5


class Competitor:
    """Run *fn* in a thread, started from inside someone's critical section,
    and report whether it finished while that section was still held."""

    def __init__(self, fn):
        self.fn = fn
        self.result = None
        self.error: BaseException | None = None
        self.finished_inside = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            self.result = self.fn()
        except BaseException as exc:  # noqa: BLE001 — reported by the test
            self.error = exc

    def start_and_wait(self):
        self._thread.start()
        self._thread.join(COMPETITOR_WINDOW_S)
        self.finished_inside = not self._thread.is_alive()

    def join(self):
        self._thread.join(10)
        assert not self._thread.is_alive(), "the competitor never finished"
        if self.error:
            raise self.error
        return self.result


class _NoLock:
    """A lock that does not lock — to show the race the real one prevents."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ── 1. The per-user token bucket ─────────────────────────────────────

class TestTheUserBucket:
    def _race(self, limiter: answer.AskLimiter):
        key = f"user-{secrets.token_hex(4)}"
        now = 1_000_000.0
        competitor = Competitor(lambda: limiter.try_acquire(key, now=now))
        state = limiter._state

        class _Pausing(dict):
            paused = False

            def get(self, k, default=None):
                # Read first, then let the competitor in: the window is
                # between this read and the write that follows it.
                seen = super().get(k, default)
                if not _Pausing.paused:
                    _Pausing.paused = True     # only the first caller
                    competitor.start_and_wait()
                return seen

        limiter._state = _Pausing(state)
        first = limiter.try_acquire(key, now=now)
        second = competitor.join()
        return first, second, competitor.finished_inside

    def test_two_requests_for_the_last_token_one_passes(self, monkeypatch):
        monkeypatch.setenv("RAG_ASK_USER_PER_MIN", "1")
        first, second, finished_inside = self._race(answer.AskLimiter())
        assert finished_inside is False, \
            "the competitor got into the bucket while the first held it"
        assert sorted([first, second], key=str) == [None, "minute"], \
            (first, second)

    def test_without_the_lock_both_would_pass(self, monkeypatch):
        """The twin: same forcing, lock removed — the race is real."""
        monkeypatch.setenv("RAG_ASK_USER_PER_MIN", "1")
        limiter = answer.AskLimiter()
        limiter._lock = _NoLock()
        first, second, finished_inside = self._race(limiter)
        assert finished_inside is True
        assert (first, second) == (None, None), \
            "the forcing did not reach the window; the lock test proves nothing"

    def test_two_asks_for_the_last_token_one_is_answered(
            self, shop, monkeypatch):
        """End to end through ask_events: the second ask arrives while the
        first one's answer is streaming."""
        monkeypatch.setenv("RAG_ASK_USER_PER_MIN", "1")
        events = _race_two_asks(shop, monkeypatch)
        a_err, b_err = _error(events["a"]), _error(events["b"])
        assert a_err is None, events["a"]
        assert b_err and b_err["code"] == "rate_limited" \
            and b_err["reason"] == "user_minute", events["b"]
        assert events["calls"] == 1


# ── 2. The daily org cap ─────────────────────────────────────────────

def _error(events):
    found = [e for e in events if e.get("type") == "error"]
    return found[0] if found else None


def _race_two_asks(pid, monkeypatch, *, user_id=None):
    """Ask twice for the same user; the second ask runs to completion from
    inside the first one's model stream. Returns both event lists and the
    number of model calls."""
    user_id = user_id or f"race-{secrets.token_hex(6)}"
    llm = FakeLLM()

    def _req():
        return answer.AskRequest(q=GOOD_Q, allowed=[pid], scope="project",
                                 project_id=pid, user_id=user_id)

    second: list[dict] = []
    original = llm.factory

    def factory(api_key):
        client = original(api_key)
        stream_for = client.messages.stream

        class _Messages:
            def stream(self, **kwargs):
                s = stream_for(**kwargs)
                if not second and len(llm.calls) == 1:
                    # The first ask is now past every cap check and inside
                    # its model call: the window. The second ask arrives.
                    second.extend(answer.ask_events(_req()))
                return s

        client.messages = _Messages()
        return client

    monkeypatch.setattr(answer, "_client_factory", factory)
    first = list(answer.ask_events(_req()))
    return {"a": first, "b": second, "calls": len(llm.calls),
            "user_id": user_id}


def _usage_rows(user_id: str) -> int:
    with _db.session_scope() as sess:
        return sess.query(_db.LlmUsage).filter(
            _db.LlmUsage.kind == answer.KIND,
            _db.LlmUsage.user_id == user_id).count()


class TestTheDailyCap:
    def test_two_asks_for_the_last_slot_one_is_answered(self, shop,
                                                       monkeypatch):
        answer.cap_gate.reset()
        monkeypatch.setenv("RAG_ASK_DAILY_CAP_PER_ORG", "1")
        events = _race_two_asks(shop, monkeypatch)
        assert _error(events["a"]) is None, events["a"]
        b_err = _error(events["b"])
        assert b_err and b_err["code"] == "rate_limited" \
            and b_err["reason"] == "org_day", events["b"]
        assert events["calls"] == 1, "the cap let two model calls through"
        assert _usage_rows(events["user_id"]) == 1
        # Nothing left reserved once the answer is metered.
        assert answer.cap_gate.in_flight(
            answer.DailyCapGate.scope_key(None, events["user_id"])) == 0

    def test_a_refused_or_failed_ask_gives_its_slot_back(self, shop,
                                                        monkeypatch):
        """A reservation that leaked would close the cap for the day."""
        answer.cap_gate.reset()
        monkeypatch.setenv("RAG_ASK_DAILY_CAP_PER_ORG", "5")
        monkeypatch.setenv("RAG_ASK_USER_PER_MIN", "1")
        uid = f"leak-{secrets.token_hex(6)}"
        key = answer.DailyCapGate.scope_key(None, uid)
        llm = FakeLLM()
        monkeypatch.setattr(answer, "_client_factory", llm.factory)
        req = answer.AskRequest(q=GOOD_Q, allowed=[shop], scope="project",
                                project_id=shop, user_id=uid)
        list(answer.ask_events(req))                 # answered
        assert answer.cap_gate.in_flight(key) == 0
        list(answer.ask_events(req))                 # cache hit: no slot
        assert answer.cap_gate.in_flight(key) == 0
        req2 = answer.AskRequest(q="password reset email", allowed=[shop],
                                 scope="project", project_id=shop,
                                 user_id=uid)
        refused = list(answer.ask_events(req2))      # user bucket refuses
        assert _error(refused)["reason"] == "user_minute"
        assert answer.cap_gate.in_flight(key) == 0
        # A stream abandoned mid-answer (client went away) releases too.
        answer.limiter.reset()
        gen = answer.ask_events(answer.AskRequest(
            q="login error message", allowed=[shop], scope="project",
            project_id=shop, user_id=uid))
        for ev in gen:
            if ev["type"] == "text":
                assert answer.cap_gate.in_flight(key) == 1
                break
        gen.close()
        assert answer.cap_gate.in_flight(key) == 0

    def _gate_race(self, gate: answer.DailyCapGate, monkeypatch):
        """The window is between the gate reading the in-flight count and
        writing it back, so the competitor is let in from that read."""
        uid = f"gate-{secrets.token_hex(6)}"
        competitor = Competitor(
            lambda: gate.try_reserve(org_id=None, user_id=uid))

        class _Pausing(dict):
            paused = False

            def get(self, k, default=None):
                seen = super().get(k, default)
                if not _Pausing.paused:
                    _Pausing.paused = True
                    competitor.start_and_wait()
                return seen

        gate._inflight = _Pausing(gate._inflight)
        first = gate.try_reserve(org_id=None, user_id=uid)
        second = competitor.join()
        return first, second, competitor.finished_inside

    def test_the_gate_serialises_count_and_reserve(self, monkeypatch):
        monkeypatch.setenv("RAG_ASK_DAILY_CAP_PER_ORG", "1")
        first, second, finished_inside = self._gate_race(
            answer.DailyCapGate(), monkeypatch)
        assert finished_inside is False
        assert [first is not None, second is not None].count(True) == 1, \
            (first, second)

    def test_without_the_gates_lock_both_would_reserve(self, monkeypatch):
        monkeypatch.setenv("RAG_ASK_DAILY_CAP_PER_ORG", "1")
        gate = answer.DailyCapGate()
        gate._lock = _NoLock()
        first, second, finished_inside = self._gate_race(gate, monkeypatch)
        assert finished_inside is True
        assert first is not None and second is not None, \
            "the forcing did not reach the window"


# ── 3. The cache puts ────────────────────────────────────────────────

@contextlib.contextmanager
def _competitor_between_read_and_insert(model, competitor_put):
    """The first session whose ``get(model, key)`` finds no row runs
    *competitor_put* to completion before returning — so the competitor's
    INSERT commits between the first put's read and its own INSERT."""
    real_factory = _db._Session
    fired = []
    armed = [True]

    def factory(*args, **kwargs):
        sess = real_factory(*args, **kwargs)
        if not armed[0]:
            return sess
        real_get = sess.get

        def get(cls, ident, *a, **kw):
            row = real_get(cls, ident, *a, **kw)
            if cls is model and row is None and armed[0]:
                armed[0] = False         # the competitor's own put is plain
                fired.append(competitor_put())
            return row

        sess.get = get
        return sess

    _db._Session = factory
    try:
        yield fired
    finally:
        _db._Session = real_factory


def _rag_rows(key: str):
    with _db.session_scope() as sess:
        return [dict(r.answer_json or {}) for r in
                sess.query(_db.RagAnswerCache).filter(
                    _db.RagAnswerCache.key == key)]


class TestTheRagAnswerCachePut:
    def test_the_same_key_twice_is_one_row_and_no_error(self, caplog):
        _db.init_db()
        key, scope = secrets.token_hex(32), f"org-{secrets.token_hex(4)}"

        def put(text):
            return _db.put_rag_answer(key=key, scope=scope, model="m",
                                      lang="en", answer={"text": text})

        caplog.set_level(logging.WARNING)
        with _competitor_between_read_and_insert(
                _db.RagAnswerCache, lambda: put("the competitor's")) as fired:
            ours = put("ours")
        assert fired == [True], "the competitor never ran in the window"
        assert ours is True, "the put lost the race and reported failure"
        rows = _rag_rows(key)
        assert len(rows) == 1
        # The later writer wins, as an upsert should.
        assert rows[0]["text"] == "ours", rows
        assert "write failed" not in caplog.text, caplog.text


class TestTheLlmResultCachePut:
    def _put(self, key, scope, text):
        return _db.put_llm_result_cache(
            key=key, scope=scope, kind="authoring", prompt_version="v1",
            model="m", response_text=text)

    def test_the_same_key_twice_is_one_row_and_no_error(self, caplog):
        _db.init_db()
        key, scope = secrets.token_hex(32), f"org-{secrets.token_hex(4)}"
        caplog.set_level(logging.WARNING)
        with _competitor_between_read_and_insert(
                _db.LlmResultCache,
                lambda: self._put(key, scope, "the competitor's")) as fired:
            ours = self._put(key, scope, "ours")
        assert fired == [True]
        assert ours is True
        row = _db.get_llm_result_cache(key, scope=scope)
        assert row and row["response_text"] == "ours", row
        with _db.session_scope() as sess:
            assert sess.query(_db.LlmResultCache).filter(
                _db.LlmResultCache.key == key).count() == 1
        assert "write failed" not in caplog.text, caplog.text

    def test_two_identical_misses_through_cached_call(self, monkeypatch,
                                                      caplog):
        """Two identical automation requests both miss, both call the
        model, both store; the second runs entirely inside the first one's
        model call. This pins the outcome at the ``cached_call`` level (one
        row, the later writer's answer, and the next request a hit). The
        narrower window — both puts past their read before either INSERTs
        — is the forced one in the test above."""
        _db.init_db()
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        org = secrets.token_hex(16)
        calls = []

        def ask(text_back):
            return llm_cache.cached_call(
                "authoring", prompt_version="v1", org_id=org,
                call=lambda **kw: _respond(kw, text_back),
                max_tokens=100, system="s",
                messages=[{"role": "user", "content": "same question"}])

        def _respond(kw, text_back):
            calls.append(text_back)
            if len(calls) == 1:
                ask("competitor's answer")       # the competing miss
            from types import SimpleNamespace
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text_back)],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                      cache_read_input_tokens=0,
                                      cache_creation_input_tokens=0),
                stop_reason="end_turn")

        caplog.set_level(logging.WARNING)
        resp = ask("first answer")
        assert calls == ["first answer", "competitor's answer"]
        assert llm_cache.response_text(resp) == "first answer"
        with _db.session_scope() as sess:
            rows = sess.query(_db.LlmResultCache).filter(
                _db.LlmResultCache.org_id == org).all()
            assert len(rows) == 1
            assert rows[0].response_text == "first answer"
        assert "write failed" not in caplog.text, caplog.text
        # And the next identical request is a hit on that one row.
        again = ask("never called")
        assert getattr(again, "from_cache", False)
        assert len(calls) == 2
