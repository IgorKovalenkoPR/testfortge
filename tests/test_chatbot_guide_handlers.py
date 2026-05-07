"""Regression tests for the six Tedgie Guide-promised handlers.

These pin the behavior advertised in templates/guide.html ("What
Tedgie is good at") so a future refactor that re-orders the rule
dispatch can't silently break Guide alignment again.

For each Guide-listed sample question we assert:
  * the resolved intent name (so we know which dispatcher branch ran)
  * a substring of the reply body that proves the answer is on-topic
    rather than a generic module-help blurb or a glossary dump.

We do NOT assert the full reply text — the wording can evolve as
content writers iterate. Intent + a key phrase is enough to guard
against the four root causes documented in the audit:
  1. EP comparison shadowed by single-topic detector
  2. Domain-specific testing-types question shadowed by generic types
  3. Diag question routed to ISTQB RAG instead of the diag checklist
  4. "negative case" message shadowed by Test Cases module synonym
"""
from __future__ import annotations

import os

import pytest

# Tedgie's AI path is conditionally enabled; force-disable it so the
# rule-based dispatch is exercised end-to-end (this matches the
# behavior on testfortge.onrender.com when no Anthropic key is set).
os.environ.pop("ANTHROPIC_API_KEY", None)

from engine.chatbot import respond  # noqa: E402 — env tweak before import


@pytest.mark.parametrize(
    "question, expected_intent, must_contain",
    [
        # 1) EP vs BVA comparison — must include both definitions and
        # a worked example, not just one technique.
        (
            "What's the difference between equivalence partitioning "
            "and boundary value analysis?",
            "istqb:equivalence_vs_boundary",
            ["Equivalence Partitioning", "Boundary Value Analysis",
             "Worked example"],
        ),
        # 2) Testing types for a payment flow — per-type rationale,
        # not the generic ISTQB §2.2.2 dump.
        (
            "Which testing types apply to a payment flow?",
            "istqb:types_for_payment",
            ["Functional", "Security", "Performance",
             "Compatibility", "Accessibility", "PCI-DSS"],
        ),
        # 3) Empty live view — diag checklist with the three concrete
        # checks the Guide promises.
        (
            "Why is my live view empty?",
            "diag_live_view_empty",
            ["Base URL", "Playwright", "Screenshots"],
        ),
        # 4) Bug summary — empty-session message when no bugs exist
        # (the request-context pathway is exercised in app-level tests).
        (
            "Summarise the last 10 bugs by component.",
            "bug_summary_empty",
            ["I don't see any saved bugs"],
        ),
        # 5) Negative cases for login — must list concrete login-flow
        # negatives, not the Test Cases module help blurb.
        (
            "Suggest 5 negative cases I'm missing for the login flow.",
            "negative_cases:login",
            ["Negative cases", "lockout"],
        ),
        # 6) Severity recommendation for an intermittent checkout
        # error — must recommend Major and explain the
        # critical-area + intermittent reasoning.
        (
            "What's a good severity for an intermittent checkout error?",
            "severity_recommendation",
            ["Recommended severity: Major", "intermittently"],
        ),
    ],
)
def test_guide_promised_question(question, expected_intent, must_contain):
    reply = respond(question, "en")
    assert reply.intent == expected_intent, (
        f"intent mismatch for {question!r}: expected {expected_intent}, "
        f"got {reply.intent}"
    )
    for phrase in must_contain:
        assert phrase in reply.text, (
            f"reply for {question!r} missing phrase {phrase!r}\n"
            f"---\n{reply.text}\n---"
        )


# ── Sanity: the new handlers must NOT shadow legitimate definitional
# questions or other dispatch paths. These pin the precedence rules.

def test_plain_severity_definition_still_routes_to_glossary():
    reply = respond("what is severity?", "en")
    assert reply.intent == "istqb_glossary:severity"


def test_greeting_still_works():
    reply = respond("hi", "en")
    assert reply.intent == "greeting"


def test_requirement_clarifier_still_works():
    reply = respond("as a user I want to log in", "en")
    assert reply.intent == "clarify_requirement"


def test_test_case_techniques_question_unaffected():
    reply = respond("Explain test case techniques", "en")
    # Either glossary alias or ISTQB topic — but never a guide handler.
    assert not reply.intent.startswith("negative_cases:")
    assert not reply.intent.startswith("istqb:types_for_")
