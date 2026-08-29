"""PR-D — heuristic test-suite classifier.

Given a list of :class:`engine.automation_qa.AutomationStep` (one
recorded flow), pick a suite tag from ``{"Smoke", "Regression",
"E2E"}``. Pure-Python rules — no LLM call, no DB access — so the
classifier composes cheaply over every flow that
``engine.session_segmenter`` produces.

The decision is deliberately opinionated, not exhaustive. We err on
the side of **Smoke** because false positives in higher buckets cost
the operator manual triage; the review UI lets them override on the
spot. Rules:

  1. **Regression** wins when any step touches a payment / auth flow
     URL or input (``checkout``, ``payment``, ``billing``, ``cart``,
     ``invoice``, ``order``, ``subscription``, ``login``, ``signin``,
     ``signup``, ``register``, ``password``, ``reset``, ``mfa``,
     ``2fa``, ``otp``, ``token``). These flows tend to be the
     business-critical ones release-gates orbit around. A URL's **query
     string is not searched** — a GET form echoes every field name into
     it on submit, so a page nobody typed a password into arrives
     carrying the word. See :func:`_without_query`.
  2. **E2E** when the flow walks ``≥ 2`` distinct URL paths AND
     contains at least one form-submission gesture (``fill`` followed
     by ``click`` on a submit-shaped element, or any ``submit`` action).
     Captures multi-page user journeys.
  3. **Smoke** otherwise — short flows, root navigation, single-page
     interactions. The default bucket the review UI pre-selects.

Each call returns both the tag and a one-line rationale the review
UI can show as a small hint next to the dropdown so the operator
understands why the classifier suggested what it did.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse

from engine.automation_qa import AutomationStep


# Valid suite tags. The classifier never emits anything else; the
# review UI's dropdown matches these strings exactly.
SUITE_SMOKE = "Smoke"
SUITE_REGRESSION = "Regression"
SUITE_E2E = "E2E"
VALID_SUITES = (SUITE_SMOKE, SUITE_REGRESSION, SUITE_E2E)


# Keyword triggers for Regression. Compiled once at module load so the
# classifier call stays cheap. Word-boundary on each side keeps
# ``/cartograph`` from matching ``cart`` while still firing on
# ``/cart``, ``cart/`` and ``checkout?cart=1``.
_REGRESSION_KEYWORDS = (
    "checkout", "payment", "billing", "cart", "invoice", "order",
    "subscription", "subscribe", "renew",
    "login", "signin", "signup", "register", "registration",
    "password", "reset", "forgot",
    "mfa", "2fa", "otp", "token", "auth", "authorize",
)
_REGRESSION_RE = re.compile(
    r"(?:^|[^a-z0-9])(" + "|".join(re.escape(k) for k in _REGRESSION_KEYWORDS) +
    r")(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)


# Action labels the classifier treats as form-submission gestures. The
# recorder + heuristic parser both emit ``"click"`` for submits — the
# E2E rule pairs that with a preceding ``"fill"`` to distinguish a
# real form-flow from a casual nav-click.
_SUBMIT_LIKE = {"click", "press", "check"}
_FILL_LIKE = {"fill", "select"}


# A URL anywhere in a step's target or raw line.
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def _without_query(url: str) -> str:
    """Drop a URL's query string, keeping scheme, host, path, fragment.

    A GET form puts **every field name** into the query on submit,
    whether or not the field was touched. Measured on 2026-08-29: a
    recorded walk of the Selenium practice form landed on
    ``…/submitted-form.html?my-text=…&my-password=&my-textarea=…`` and
    the classifier called the flow business-critical because it found
    the word "password" — in an empty parameter belonging to a field
    nobody had typed into. It was reading the page's form definition and
    reporting it as evidence about the flow.

    The path survives, because that is where a route lives, and so does
    the fragment, because a hash-routed SPA keeps its route there and
    dropping it would lose the real signal along with the noise.

    What this gives up is a keyword that only ever appears in a query —
    ``?next=/checkout``. That is a flow announcing where it intends to
    go; if it arrives, the next step's *path* says so.
    """
    parts = urlparse(url)
    if not parts.query:
        return url
    rebuilt = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return f"{rebuilt}#{parts.fragment}" if parts.fragment else rebuilt


def _searchable(text: str) -> str:
    """*text* with every URL's query string removed."""
    if not text or "://" not in text:
        return text or ""
    return _URL_IN_TEXT.sub(lambda m: _without_query(m.group(0)), text)


@dataclass
class SuiteVerdict:
    """Classifier output. ``tag`` is one of :data:`VALID_SUITES`,
    ``rationale`` is a one-line human-readable explanation rendered in
    the review-screen dropdown's hint slot."""
    tag: str
    rationale: str


def classify(steps: Iterable[AutomationStep]) -> SuiteVerdict:
    """Pick a suite tag for one recorded flow. See module docstring
    for the rule order."""
    step_list = list(steps or [])
    if not step_list:
        return SuiteVerdict(
            tag=SUITE_SMOKE,
            rationale="No steps captured — default Smoke.",
        )

    # Rule 1 — Regression on auth / payment keywords. Walks every
    # step's ``target`` AND ``raw`` source so the matcher catches
    # both URL-bearing steps (goto) and form-field steps (login form
    # via ``role=textbox[name="Email"]`` won't have a URL but the
    # surrounding raw line will).
    for step in step_list:
        haystack = " ".join(filter(None, (
            _searchable(getattr(step, "target", "") or ""),
            getattr(step, "value", "") or "",
            _searchable(getattr(step, "raw", "") or ""),
        )))
        m = _REGRESSION_RE.search(haystack)
        if m:
            return SuiteVerdict(
                tag=SUITE_REGRESSION,
                rationale=(
                    f"Flow touches '{m.group(1).lower()}' — "
                    "treated as business-critical regression."
                ),
            )

    # Rule 2 — E2E on ≥ 2 distinct URL paths + at least one form
    # submission gesture (fill followed by click/press, OR a literal
    # ``submit`` action).
    paths = _distinct_paths(step_list)
    has_form_submit = _has_form_submit(step_list)
    if len(paths) >= 2 and has_form_submit:
        return SuiteVerdict(
            tag=SUITE_E2E,
            rationale=(
                f"Walks {len(paths)} distinct page(s) with form "
                "submission — full user journey."
            ),
        )

    # Rule 3 — Smoke fallback. Add a small reason hint that matches
    # the actual shape so the operator sees why it landed there
    # (short flow vs root-only).
    if len(step_list) <= 5 and len(paths) <= 1:
        rationale = (
            f"{len(step_list)} step(s), single page — Smoke."
        )
    else:
        rationale = (
            "No regression keywords, no multi-page form flow — "
            "Smoke."
        )
    return SuiteVerdict(tag=SUITE_SMOKE, rationale=rationale)


def _distinct_paths(steps: list[AutomationStep]) -> list[str]:
    """Collect the unique URL paths the flow visits via ``goto``
    targets. We ignore non-URL ``target`` values (selector strings)
    because they don't contribute to "how many pages did we walk".
    """
    seen: list[str] = []
    for step in steps:
        if (step.action or "").strip().lower() != "goto":
            continue
        target = (step.target or "").strip()
        if not target.startswith(("http://", "https://")):
            continue
        try:
            path = urlparse(target).path or "/"
        except (ValueError, TypeError):
            continue
        # Normalise trailing slash so ``/foo`` and ``/foo/`` count
        # as the same page.
        path = path.rstrip("/") or "/"
        if path not in seen:
            seen.append(path)
    return seen


def _has_form_submit(steps: list[AutomationStep]) -> bool:
    """Heuristic: was there a fill (or select) followed by a
    submit-like gesture somewhere in the flow? Order matters — a
    click before any fill is a navigation click, not a form submit.
    """
    saw_fill = False
    for step in steps:
        action = (step.action or "").strip().lower()
        if action in _FILL_LIKE:
            saw_fill = True
        elif saw_fill and action in _SUBMIT_LIKE:
            return True
        # Literal submit verbs from the heuristic parser still count
        # on their own — text-authored TCs sometimes say "submit"
        # without an explicit prior fill.
        if action == "submit":
            return True
    return False


__all__ = [
    "SUITE_SMOKE", "SUITE_REGRESSION", "SUITE_E2E", "VALID_SUITES",
    "SuiteVerdict", "classify",
]
