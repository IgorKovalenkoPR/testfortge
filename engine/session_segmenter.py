"""PR-D — LLM-driven session segmenter.

Takes one recorded session (a flat ``list[AutomationStep]`` from
``engine.recorder_parser``) and splits it into discrete "logical
flows" the operator probably meant to test. Each flow becomes one
proposed test case in the review UI.

The split is **LLM-driven** by intent — a simple goto/submit
heuristic would mis-segment flows like *"login -> dashboard -> open
settings -> change password"* where the meaningful boundary is
between *settings* and *change password*, not at every ``goto``.

Pipeline:

  1. Build a compact textual summary of the steps (action + locator
     label + value) — keeps the prompt cheap.
  2. Call the existing :func:`engine.llm_client.call_messages` with a
     strict JSON-only response shape ``{flows: [{summary, intent,
     start_idx, end_idx}]}``.
  3. Validate indices, clip to bounds, and emit
     ``list[ProposedTC]`` — each carries the full steps slice + LLM
     summary + suggested suite tag from
     :func:`engine.suite_classifier.classify`.

Fallback: if the LLM is unavailable, returns malformed JSON, or
produces zero flows, we emit **one** ProposedTC containing every
step. The operator can split it manually in v2; for MVP, a single
unhelpfully-segmented TC is still strictly better than dropping the
recording.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from engine.automation_qa import AutomationStep
from engine.llm_client import LLMUnavailable, call_messages
from engine.log import get_logger
from engine.suite_classifier import (SUITE_SMOKE, SuiteVerdict,
                                       VALID_SUITES, classify)

_logger = get_logger(__name__)


# Same model env-var the chatbot uses, so a host that already
# overrode it for cost reasons gets the same override here. Capped
# output tokens are intentionally small — the JSON shape we ask for
# is short.
import os
_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
_MAX_TOKENS = int(os.environ.get("SESSION_SEGMENTER_MAX_TOKENS", "1500"))


@dataclass
class ProposedTC:
    """One segment of a recorded session, ready for the review UI.

    ``steps`` is the contiguous slice of ``AutomationStep`` the
    segmenter assigned to this flow. ``summary`` and ``intent`` come
    from the LLM (or a fallback string when LLM is unavailable).
    ``suggested_suite`` is the heuristic classifier's verdict — UI
    pre-selects this in the dropdown but lets the operator override.
    """
    summary: str
    intent: str
    steps: list[AutomationStep]
    suggested_suite: str = SUITE_SMOKE
    rationale: str = ""

    def to_dict(self) -> dict:
        """JSON-serialisable shape for the SessionDraft row."""
        import dataclasses as _dc
        return {
            "summary": self.summary,
            "intent": self.intent,
            "suggested_suite": self.suggested_suite,
            "rationale": self.rationale,
            "steps": [_dc.asdict(s) for s in self.steps],
        }


def segment(steps: list[AutomationStep]) -> list[ProposedTC]:
    """Split a recorded session into proposed TCs.

    Empty input → empty output. LLM failure → one-flow fallback.
    Otherwise honours the LLM's flow boundaries, runs each slice
    through :func:`suite_classifier.classify`, and returns the list.
    """
    if not steps:
        return []

    # Single-step recordings don't need segmentation — wrap as one TC.
    if len(steps) == 1:
        return [_single_flow(steps, summary="Single-step flow",
                              intent="One recorded action")]

    flows = _call_llm(steps)
    if not flows:
        # LLM unavailable / malformed — fallback to a single proposed
        # TC covering the whole session. Operator can manually split
        # in v2 (out of scope for MVP).
        return [_single_flow(
            steps,
            summary=_fallback_summary(steps),
            intent="LLM segmentation unavailable — manual review needed.",
        )]

    out: list[ProposedTC] = []
    for flow in flows:
        slice_ = steps[flow["start_idx"]:flow["end_idx"] + 1]
        if not slice_:
            continue
        verdict: SuiteVerdict = classify(slice_)
        out.append(ProposedTC(
            summary=flow.get("summary", "").strip() or
                    _fallback_summary(slice_),
            intent=flow.get("intent", "").strip(),
            steps=slice_,
            suggested_suite=verdict.tag,
            rationale=verdict.rationale,
        ))

    # If LLM returned empty / all slices were empty, still give the
    # operator one card to review.
    if not out:
        return [_single_flow(
            steps,
            summary=_fallback_summary(steps),
            intent="Segmenter returned no valid flows — full session "
                   "as one TC.",
        )]
    return out


# ── LLM call + parsing ──────────────────────────────────────────


def _call_llm(steps: list[AutomationStep]) -> list[dict]:
    """Returns a list of flow dicts ``[{summary, intent, start_idx,
    end_idx}]`` with validated, in-bounds indices. Empty on any
    failure path so the caller can fall back."""
    try:
        resp = call_messages(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": _build_user_prompt(steps),
            }],
        )
    except LLMUnavailable as exc:
        _logger.warning("session_segmenter LLM unavailable: %s", exc)
        return []
    except Exception as exc:  # pragma: no cover — defensive
        _logger.warning("session_segmenter LLM call failed: %s", exc)
        return []

    text = _extract_text(resp)
    if not text:
        return []
    payload = _extract_json(text)
    if not payload:
        return []
    return _validate_flows(payload, total_steps=len(steps))


def _extract_text(resp) -> str:
    """Pull the assistant's text out of the Anthropic SDK response."""
    try:
        content = getattr(resp, "content", None) or []
        parts: list[str] = []
        for block in content:
            t = getattr(block, "text", None)
            if t:
                parts.append(t)
        return "".join(parts).strip()
    except Exception:  # pragma: no cover — defensive
        return ""


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    """The model usually returns clean JSON. If it wraps the block in
    ```json fences or chatty preamble, pull the outermost ``{...}``."""
    text = text.strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (ValueError, TypeError):
        return None


def _validate_flows(payload: dict, total_steps: int) -> list[dict]:
    """Coerce ``payload['flows']`` to a list of flow dicts with
    in-bounds integer indices. Out-of-range / malformed entries are
    dropped silently; the fallback path catches the empty case."""
    raw = payload.get("flows")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            s = int(item.get("start_idx"))
            e = int(item.get("end_idx"))
        except (TypeError, ValueError):
            continue
        # Clip to bounds. Allow zero-width flow to be dropped rather
        # than wrap-around shenanigans.
        if s < 0 or e < s or e >= total_steps:
            continue
        out.append({
            "summary": str(item.get("summary", "") or "")[:200],
            "intent":  str(item.get("intent", "") or "")[:200],
            "start_idx": s,
            "end_idx": e,
        })
    # Ensure ordering by start_idx so the review UI lists flows in
    # session order even if the LLM shuffled them.
    out.sort(key=lambda f: f["start_idx"])
    return out


# ── Helpers ─────────────────────────────────────────────────────


def _single_flow(steps: list[AutomationStep], *, summary: str,
                  intent: str) -> ProposedTC:
    """Build a ProposedTC carrying the entire session as one flow.
    Used by the fallback paths."""
    verdict = classify(steps)
    return ProposedTC(
        summary=summary,
        intent=intent,
        steps=steps,
        suggested_suite=verdict.tag,
        rationale=verdict.rationale,
    )


def _fallback_summary(steps: list[AutomationStep]) -> str:
    """One-line synthetic summary when the LLM didn't give one.
    Picks the first ``goto`` target's path as the anchor; falls back
    to the action count."""
    for step in steps:
        if (step.action or "").lower() == "goto" and step.target:
            return f"Recorded session — {step.target}"[:200]
    return f"Recorded session — {len(steps)} step(s)"


def _build_user_prompt(steps: list[AutomationStep]) -> str:
    """Compact step-listing the LLM segments. Keeps each line short
    so the prompt fits a sensible budget even for 50-step sessions."""
    lines = [
        "You are a senior QA test designer. Below is a flat list of "
        "recorded user actions, indexed from 0. Split them into the "
        "minimum number of discrete logical test flows a human "
        "would write as separate test cases.",
        "",
        "RULES:",
        "- A new flow starts when the user's intent shifts (e.g. "
        "  finished login → now exploring settings).",
        "- Adjacent fills + a click that submits the form belong to "
        "  ONE flow, not separate ones.",
        "- Every step must belong to exactly one flow. start_idx and "
        "  end_idx are inclusive.",
        "- Prefer fewer flows over many; aim for 1-5 total.",
        "- For each flow, give a short summary (one sentence, ≤80 "
        "  chars, imperative voice, e.g. 'Sign in with email and "
        "  password') and an intent (one sentence describing what "
        "  the user is trying to accomplish).",
        "",
        "OUTPUT FORMAT: respond with ONLY valid JSON, no prose, no "
        "code fences. Schema:",
        '  {"flows": [{"summary": "...", "intent": "...", '
        '"start_idx": N, "end_idx": M}]}',
        "",
        "Recorded steps:",
    ]
    for i, step in enumerate(steps):
        action = step.action or "?"
        target = (step.target or "")[:80]
        value = (step.value or "")[:40]
        label = (step.locator_label or "")[:40]
        parts = [f"{i}: {action}"]
        if target:
            parts.append(f"target={target!r}")
        if value:
            parts.append(f"value={value!r}")
        if label:
            parts.append(f"label={label!r}")
        lines.append("  " + " ".join(parts))
    return "\n".join(lines)


# ── System prompt ───────────────────────────────────────────────


_SYSTEM_PROMPT = (
    "You are a senior QA test designer. Your job is to split a "
    "recorded automation session into the smallest number of "
    "discrete logical test flows. Output strictly valid JSON in the "
    "schema the user provides — no preamble, no code fences, no "
    "trailing commentary. Cover every step exactly once. If unsure "
    "where a boundary belongs, prefer keeping steps together rather "
    "than oversplitting."
)


__all__ = ["ProposedTC", "segment", "VALID_SUITES"]
