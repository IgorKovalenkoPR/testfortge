"""PR-D — session segmenter tests.

LLM call is mocked at the ``call_messages`` boundary so the test
suite has no real network dependency. We pin three contracts:

  1. **Happy path** — LLM returns valid JSON → segmenter slices the
     steps accordingly and runs each slice through the classifier.
  2. **Fallback** — LLM unavailable / malformed → the deterministic
     split below. It used to be one card carrying the whole session and
     the words "manual review needed", which on an instance with no API
     key is every recording ever made: the segmenter declining its own
     job and saying so politely. The recording marks its own
     boundaries, and splitting on them needs no model.
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


# ── The deterministic split (no API key, which is every free instance)

def _no_llm():
    from engine.llm_client import LLMUnavailable
    return mock.patch("engine.session_segmenter.call_messages",
                      side_effect=LLMUnavailable("no api key"))


def _submit(target=""):
    # The shape extension/content.js actually emits, pinned against the
    # real content script in tests/test_recorder_submit_marker.py. It
    # was `action="click"` on `css=form` until that turned out to be a
    # note the runner replayed as a click on the form -- the segmenter
    # never cared which verb it wore, and this fixture said "click"
    # long after the recorder stopped saying it.
    return AutomationStep(action="submit", target=target,
                          raw='page.locator("form").submit()',
                          comment="form submitted")


class TestSplittingWithoutAModel:
    def test_a_submit_ends_a_flow(self):
        """The boundary the recorder was already emitting for nobody.

        extension/content.js stamps a synthetic submit step and says in
        its own comment that it does so "so the segmenter sees a natural
        flow boundary". Nothing read it until now.
        """
        steps = [
            AutomationStep(action="goto", target="https://app/contact"),
            AutomationStep(action="fill", target="label=Email",
                            value="a@b.test"),
            _submit(),
            AutomationStep(action="fill", target="label=Search",
                            value="widgets"),
            AutomationStep(action="click",
                            target='role=button[name="Search"]'),
        ]
        with _no_llm():
            out = segment(steps)

        assert len(out) == 2, [o.summary for o in out]
        assert len(out[0].steps) == 3
        assert len(out[1].steps) == 2
        assert "form submitted" in out[0].summary

    def test_a_goto_nobody_clicked_starts_a_flow(self):
        steps = [
            AutomationStep(action="goto", target="https://app/one"),
            AutomationStep(action="fill", target="label=A", value="1"),
            # Not caused by a click — somebody went somewhere new.
            AutomationStep(action="goto", target="https://app/two"),
            AutomationStep(action="fill", target="label=B", value="2"),
        ]
        with _no_llm():
            out = segment(steps)

        assert len(out) == 2
        assert out[0].steps[0].target.endswith("/one")
        assert out[1].steps[0].target.endswith("/two")

    def test_a_goto_a_click_caused_does_not_split_the_click_off(self):
        """The rule that keeps this from shredding every session.

        A link click produces click → goto. Splitting between them would
        cut one action in half and leave a card holding a lone
        navigation — which is what the walk on staging actually
        recorded.
        """
        # Long enough that _merge_short_runs cannot hide a wrong split:
        # with the rule removed this becomes two flows of two and three
        # steps, and neither is short enough to be folded back. The
        # three-step version of this test passed either way — caught by
        # mutation, not by reading it.
        steps = [
            AutomationStep(action="goto", target="https://app/services"),
            AutomationStep(action="click",
                            target='role=link[name="Company"]'),
            AutomationStep(action="goto", target="https://app/company"),
            AutomationStep(action="click",
                            target='role=link[name="Team"]'),
            AutomationStep(action="fill", target="label=Search",
                            value="qa"),
        ]
        with _no_llm():
            out = segment(steps)

        assert len(out) == 1, [o.summary for o in out]
        assert len(out[0].steps) == 5

    def test_a_lone_step_is_folded_into_a_neighbour(self):
        # A single navigation is a seam, not a test case, and a card
        # holding one wastes the review it asks for.
        steps = [
            AutomationStep(action="goto", target="https://app/one"),
            AutomationStep(action="fill", target="label=A", value="1"),
            _submit(),
            AutomationStep(action="goto", target="https://app/two"),
        ]
        with _no_llm():
            out = segment(steps)

        assert len(out) == 1, [o.summary for o in out]
        assert len(out[0].steps) == 4

    def test_every_step_survives_the_split(self):
        """Nothing may be dropped on the floor.

        A segmenter that loses a step turns a recording into a test case
        that cannot reproduce what was recorded — worse than not
        splitting at all.
        """
        steps = [
            AutomationStep(action="goto", target="https://app/a"),
            AutomationStep(action="fill", target="label=A", value="1"),
            _submit(),
            AutomationStep(action="goto", target="https://app/b"),
            AutomationStep(action="click", target='role=button[name="Go"]'),
            AutomationStep(action="goto", target="https://app/c"),
            AutomationStep(action="fill", target="label=C", value="3"),
        ]
        with _no_llm():
            out = segment(steps)

        rebuilt = [st for flow in out for st in flow.steps]
        assert rebuilt == steps

    def test_it_says_how_it_split_rather_than_posing_as_a_model(self):
        steps = [
            AutomationStep(action="goto", target="https://app/one"),
            AutomationStep(action="fill", target="label=A", value="1"),
            _submit(),
            AutomationStep(action="goto", target="https://app/two"),
            AutomationStep(action="fill", target="label=B", value="2"),
        ]
        with _no_llm():
            out = segment(steps)

        for flow in out:
            assert "without an LLM" in flow.intent
            assert "unavailable" not in flow.intent.lower()

    def test_one_real_flow_is_reported_as_one_flow_not_as_a_failure(self):
        """The wording matters on the commonest session of all.

        A short browse with no submit and no fresh start genuinely is
        one flow. Saying "manual review needed" there told the operator
        the tool had given up when it had in fact answered.
        """
        steps = [
            AutomationStep(action="goto", target="https://app/services"),
            AutomationStep(action="click",
                            target='role=link[name="Company"]'),
            AutomationStep(action="goto", target="https://app/company"),
        ]
        with _no_llm():
            out = segment(steps)

        assert len(out) == 1
        assert "one flow" in out[0].intent
        assert "manual review needed" not in out[0].intent

    def test_each_flow_is_classified_on_its_own_content(self):
        # The suite tag belongs to the flow, not to the session: a login
        # flow and a search flow should not inherit one tag.
        steps = [
            AutomationStep(action="goto", target="https://app/login"),
            AutomationStep(action="fill", target="label=Password",
                            value="x"),
            _submit(),
            AutomationStep(action="goto", target="https://app/home"),
            AutomationStep(action="click",
                            target='role=link[name="About"]'),
        ]
        with _no_llm():
            out = segment(steps)

        assert len(out) == 2
        assert all(f.suggested_suite for f in out)
        assert all(f.rationale for f in out)

