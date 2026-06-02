"""PR-D — session segmenter tests.

LLM call is mocked at the ``call_messages`` boundary so the test
suite has no real network dependency. We pin three contracts:

  1. **Happy path** — LLM returns valid JSON → segmenter slices the
     steps accordingly and runs each slice through the classifier.
  2. **Fallback** — LLM unavailable / malformed → ONE proposed TC
     covering the whole session (better than dropping the recording).
  3. **Index hygiene** — out-of-range / duplicate / inverted indices
     from the LLM are dropped instead of crashing the call.
"""
from __future__ import annotations

import json
from unittest import mock

from engine.automation_qa import AutomationStep
from engine.session_segmenter import ProposedTC, segment


# Common step list used by happy-path tests. Two logical flows: login
# (steps 0-2) + dashboard nav (steps 3-4).
_STEPS = [
    AutomationStep(action="goto", target="https://app/login"),
    AutomationStep(action="fill", target="label=Email", value="x@y.z"),
    AutomationStep(action="click", target='role=button[name="Sign in"]'),
    AutomationStep(action="goto", target="https://app/dashboard"),
    AutomationStep(action="click", target='role=link[name="Settings"]'),
]


def _mock_llm_resp(payload):
    """Build a fake Anthropic response carrying the given JSON string
    as the only text block — matches the shape ``_extract_text``
    expects."""
    class _Block:
        def __init__(self, text): self.text = text
    class _Resp:
        def __init__(self, text): self.content = [_Block(text)]
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload)
    return _Resp(payload)


class TestHappyPath:
    def test_two_flows_split_and_classified(self):
        llm_payload = {
            "flows": [
                {"summary": "Sign in", "intent": "Authenticate user",
                 "start_idx": 0, "end_idx": 2},
                {"summary": "Open settings",
                 "intent": "Navigate post-login",
                 "start_idx": 3, "end_idx": 4},
            ]
        }
        with mock.patch("engine.session_segmenter.call_messages",
                          return_value=_mock_llm_resp(llm_payload)):
            out = segment(_STEPS)
        assert len(out) == 2
        assert out[0].summary == "Sign in"
        assert len(out[0].steps) == 3
        # First flow touches /login → classifier says Regression.
        assert out[0].suggested_suite == "Regression"
        assert out[1].summary == "Open settings"
        assert len(out[1].steps) == 2

    def test_llm_response_with_code_fences_still_parses(self):
        """Some models wrap JSON in ```json blocks — the extractor
        should strip them via the regex fallback."""
        wrapped = "```json\n" + json.dumps({"flows": [
            {"summary": "All", "intent": "test", "start_idx": 0,
             "end_idx": 4}
        ]}) + "\n```"
        with mock.patch("engine.session_segmenter.call_messages",
                          return_value=_mock_llm_resp(wrapped)):
            out = segment(_STEPS)
        assert len(out) == 1
        assert out[0].summary == "All"


class TestFallback:
    def test_llm_unavailable_returns_single_flow(self):
        from engine.llm_client import LLMUnavailable
        with mock.patch("engine.session_segmenter.call_messages",
                          side_effect=LLMUnavailable("no api key")):
            out = segment(_STEPS)
        assert len(out) == 1
        assert len(out[0].steps) == len(_STEPS)
        # Still ran the classifier — login keyword wins.
        assert out[0].suggested_suite == "Regression"

    def test_malformed_json_returns_single_flow(self):
        with mock.patch("engine.session_segmenter.call_messages",
                          return_value=_mock_llm_resp("not valid json")):
            out = segment(_STEPS)
        assert len(out) == 1
        assert len(out[0].steps) == len(_STEPS)

    def test_empty_flows_list_returns_single_flow(self):
        with mock.patch("engine.session_segmenter.call_messages",
                          return_value=_mock_llm_resp({"flows": []})):
            out = segment(_STEPS)
        assert len(out) == 1


class TestIndexHygiene:
    def test_out_of_range_end_idx_is_dropped(self):
        llm_payload = {
            "flows": [
                {"summary": "Good", "intent": "ok",
                 "start_idx": 0, "end_idx": 1},
                {"summary": "Bad", "intent": "out of bounds",
                 "start_idx": 0, "end_idx": 999},
            ]
        }
        with mock.patch("engine.session_segmenter.call_messages",
                          return_value=_mock_llm_resp(llm_payload)):
            out = segment(_STEPS)
        # Only the in-bounds flow survives.
        assert len(out) == 1
        assert out[0].summary == "Good"

    def test_inverted_indices_dropped(self):
        llm_payload = {
            "flows": [
                {"summary": "Inverted", "intent": "bad",
                 "start_idx": 4, "end_idx": 1},
            ]
        }
        with mock.patch("engine.session_segmenter.call_messages",
                          return_value=_mock_llm_resp(llm_payload)):
            out = segment(_STEPS)
        # All flows rejected → fallback to single ProposedTC.
        assert len(out) == 1
        assert len(out[0].steps) == len(_STEPS)


class TestSingleStepEdgeCase:
    def test_one_step_no_llm_call(self):
        with mock.patch("engine.session_segmenter.call_messages") as m:
            out = segment([_STEPS[0]])
        m.assert_not_called()
        assert len(out) == 1
        assert out[0].summary == "Single-step flow"

    def test_empty_returns_empty(self):
        with mock.patch("engine.session_segmenter.call_messages") as m:
            out = segment([])
        m.assert_not_called()
        assert out == []


class TestToDict:
    def test_proposed_tc_to_dict_serialisable(self):
        pc = ProposedTC(
            summary="x", intent="y",
            steps=[_STEPS[0]],
            suggested_suite="Smoke", rationale="r",
        )
        d = pc.to_dict()
        # Round-trips through json.
        s = json.dumps(d)
        out = json.loads(s)
        assert out["summary"] == "x"
        assert out["suggested_suite"] == "Smoke"
        assert len(out["steps"]) == 1
