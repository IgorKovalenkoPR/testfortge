"""Tedgie did not pay its way, and its own sibling said why that mattered.

Two chat surfaces, one feature. ``routes/chat.py``'s streaming path meters
with ``org_id=session.get("org_id")`` and explains itself: *"a usage report
that silently omitted the chattiest surface in the product would be worse
than no report."* The non-streaming path — ``POST /chat`` →
``engine.chatbot._ai_respond`` → ``llm_client.call_messages`` — passed no
``org_id``, no ``project_id`` and no ``user_id`` at all. Measured at the
seam, not inferred: the kwargs the call received were ``kind``,
``max_tokens``, ``system``, ``messages``, full stop.

Two things followed from that one omission:

* the usage row was written with **no organisation**, so a team's chat spend
  counted against no allowance and appeared in no usage panel — the exact
  omission the streaming path's docstring calls worse than nothing;
* ``llm_client``'s budget gate is conditioned on ``org_id``, so with none
  passed it was skipped. A team over its monthly allowance kept getting
  Tedgie on the platform key while generation was refused.

And the streaming path had two of its own, in the four lines that opened the
SDK client:

* it read ``os.environ["ANTHROPIC_API_KEY"]`` directly, skipping
  ``engine.llm_keys`` — so a team that had supplied its own key had its chat
  streamed on the platform's, and the platform paid;
* it had no budget check at all. It cannot go through ``call_messages``
  (the SDK's streaming helper has no equivalent there), and the gate lived
  inside that function, so building its own client meant skipping the gate
  by construction. The gate is now ``llm_client.check_budget``, shared.

Refuted while looking, and recorded so it is not re-checked: ``_meter_stream``
reads ``session`` from inside the generator, which would raise into its bare
``except`` and meter nothing — except that the route wraps the generator in
``stream_with_context``, so the request context outlives the view. There is
now a test below that pins that wrapper, because the metering silently
depends on it.
"""
from __future__ import annotations

import inspect
import secrets
from unittest import mock

import pytest

from engine import chatbot
from engine import db as _db
from engine import llm_client
from engine import llm_cost


@pytest.fixture(autouse=True)
def _ready(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    _db.init_db()


def _capture_call_messages():
    """Patch ``chatbot.call_messages`` and return the kwargs dict it sees."""
    seen: dict = {}

    def _fake(**kwargs):
        seen.update(kwargs)
        raise chatbot.LLMUnavailable("not calling the real thing in a test")

    return seen, _fake


class TestTheNonStreamingPath:

    def test_the_llm_call_is_told_who_is_asking(self):
        seen, fake = _capture_call_messages()
        with mock.patch.object(chatbot, "_ANTHROPIC_OK", True), \
             mock.patch.object(chatbot, "call_messages", fake):
            chatbot.respond("how do I test a login form", "en",
                            project_id="p" * 32, org_id="o" * 32,
                            user_id="u" * 32)
        assert seen.get("org_id") == "o" * 32
        assert seen.get("project_id") == "p" * 32
        assert seen.get("user_id") == "u" * 32

    def test_the_route_passes_all_three(self, client):
        """The engine can carry them and still be handed nothing — which is
        what it was handed. Asserted through the route, because that is the
        caller that has them."""
        seen, fake = _capture_call_messages()
        # The suite's own organisation, not a made-up one: the route is
        # role-gated, and planting an org the signed-in user is not a member
        # of makes this assert a 403 that has nothing to do with the subject.
        with client.session_transaction() as sess:
            expected_org = sess.get("org_id")
        with mock.patch.object(chatbot, "_ANTHROPIC_OK", True), \
             mock.patch.object(chatbot, "call_messages", fake):
            response = client.post("/chat",
                                   json={"message": "how do I test a form"})
        assert response.status_code == 200
        assert "org_id" in seen, sorted(seen)
        assert seen["org_id"] == expected_org

    def test_a_caller_with_no_request_still_works(self):
        """The eval harness and the MCP tool call ``respond`` with nothing.
        The new arguments default to None for the same reason ``project_id``
        does, and this is the test that keeps it that way."""
        seen, fake = _capture_call_messages()
        with mock.patch.object(chatbot, "_ANTHROPIC_OK", True), \
             mock.patch.object(chatbot, "call_messages", fake):
            reply = chatbot.respond("how do I test a login form", "en")
        assert reply is not None
        assert seen.get("org_id") is None


class TestTheBudgetGate:

    @pytest.fixture
    def broke_org(self):
        """An organisation whose platform spend is past its own limit."""
        org = _db.create_organization(f"Broke {secrets.token_hex(4)}")
        _db.set_org_settings(org, {"llm_budget_usd": 1}) \
            if hasattr(_db, "set_org_settings") else None
        return org

    def test_the_gate_is_one_function_now(self):
        """It lived inside ``call_messages``, which the streaming path cannot
        reach. A gate only the non-streaming caller can run is half a gate."""
        assert callable(llm_client.check_budget)
        assert "check_budget" in llm_client.__all__

    def test_a_byok_org_is_exempt(self):
        """Capping somebody's spend on their own key would be a strange
        thing for a platform to do — and it is why the streaming path has to
        resolve the key *before* it can ask about the budget."""
        llm_client.check_budget("o" * 32, key_source="org")   # must not raise

    def test_no_org_is_exempt(self):
        llm_client.check_budget(None)                          # must not raise

    def test_it_refuses_when_the_allowance_is_spent(self, monkeypatch):
        monkeypatch.setattr(
            llm_cost, "budget_state",
            lambda *a, **k: {"limit_micros": 1_000_000,
                             "spent_micros": 2_000_000,
                             "over": True, "ratio": 2.0})
        with pytest.raises(llm_client.LLMBudgetExceeded):
            llm_client.check_budget("o" * 32, key_source="platform")

    def test_it_fails_open_when_metering_cannot_answer(self, monkeypatch):
        """A metering outage must not lock every org out of generation. The
        budget is a cost guard, not a security control — so this one is
        deliberately the opposite posture from the access checks."""
        def _boom(*a, **k):
            raise RuntimeError("meter is away")

        monkeypatch.setattr(llm_cost, "budget_state", _boom)
        llm_client.check_budget("o" * 32, key_source="platform")


class TestTheStreamingPath:
    """Read off the route's source, because what matters here is which
    function it reaches — and a request that never opens a real SDK stream
    cannot show that."""

    @staticmethod
    def _source():
        import routes.chat
        return inspect.getsource(routes.chat)

    def test_it_no_longer_reads_the_key_out_of_the_environment(self):
        source = self._source()
        assert "llm_keys" in source, "BYOK resolution is missing again"
        # The environment is still the documented fallback when resolution
        # itself fails, so its presence is fine — being the *only* path is
        # not.
        assert "resolve_key" in source

    def test_it_checks_the_budget_before_streaming(self):
        source = self._source()
        assert "check_budget" in source
        assert "LLMBudgetExceeded" in source

    def test_it_still_falls_back_rather_than_erroring(self):
        """Over budget is not a failure to show the user. The rule-based
        reply is what ``llm_client`` gives a generation run in the same
        state, and the two surfaces should answer alike."""
        source = self._source()
        # Anchored on the call, not on the name: the name appears first in
        # the comment that explains it, and splitting there measured prose.
        marker = "_llm_client.check_budget(org_id"
        assert marker in source
        head = source.split(marker, 1)[1][:900]
        assert "rule_based_fallback" in head, head

    def test_the_generator_keeps_its_request_context(self):
        """``_meter_stream`` reads ``session`` from inside the generator, and
        swallows everything. Without this wrapper it would meter nothing and
        say nothing — the failure its own docstring calls worse than no
        report."""
        assert "stream_with_context(generate())" in self._source()
