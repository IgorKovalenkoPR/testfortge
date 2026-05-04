"""TestFortge — Structured bug-report builder.

Translates a raw test failure (deterministic simulator OR Playwright
automation context) into ISTQB-aligned, test.io / softwaretestinghelp /
qatestlab-compliant bug fields:

* title  — "[Component / Area]: [Observable Defect]" (7–15 words)
* summary — 2–5 sentence narrative, third-person, observable language
* steps_to_reproduce — numbered atomic steps, user-visible labels
* actual_result — observable state + verbatim error message
* expected_result — "should" phrasing, behavior per spec
* severity / priority / frequency — derived from defect class

Why a separate module
---------------------
The historical helpers in :mod:`engine.qa_testers` (``_make_bug_summary``,
``_make_bug_expected``, ``_make_bug_actual``) operate by naive string
inversion ("is" -> "is not"). They produce un-actionable titles like
"... — does not work as expected" that operators flagged as garbage.
This module replaces them with rule-driven builders that follow the
distilled standards. ``qa_testers.execute_items`` still calls the old
helpers as a deterministic fallback, but
:func:`rewrite_bug_from_automation` upgrades any bug that has Playwright
failure context attached.

All builders are pure functions of their inputs. No side effects, no
network calls, deterministic — so unit tests and visual diffs are easy.

References
----------
* https://academy.test.io/en/articles/2541949-bug-report-requirements
* https://www.softwaretestinghelp.com/sample-bug-report/
* https://qatestlab.com/resources/knowledge-center/sample-deliverables/bug-reports/
"""

from __future__ import annotations

import re
from typing import Any


# ── Constants ──────────────────────────────────────────────────────

# Phrases banned from titles per the standards. Order matters — longer
# patterns FIRST so e.g. "does not work as expected" is removed in one
# shot rather than leaving "as expected" dangling after "does not work"
# is stripped first.
BANNED_TITLE_PHRASES = (
    "does not work as expected",
    "doesn't work as expected",
    "does not work",
    "doesn't work",
    "issue with",
    "problem with",
    "error with",
    "is broken",
    "not working",
    "weird behavior",
    "weird behaviour",
    "glitch",
    "as expected",  # last-resort: leftover after a partial strip
)

# Maps Playwright-error fragments -> defect class. Order matters: more
# specific patterns come first.
ERROR_CLASS_PATTERNS = (
    (re.compile(r"Locator\.click.*Timeout", re.I),       "click_timeout"),
    (re.compile(r"Locator\.fill.*Timeout", re.I),        "fill_timeout"),
    (re.compile(r"Locator\.select_option.*Timeout", re.I), "select_timeout"),
    (re.compile(r"Locator\.check.*Timeout", re.I),       "check_timeout"),
    (re.compile(r"Locator\.type.*Timeout", re.I),        "fill_timeout"),
    (re.compile(r"Expected text not found", re.I),       "text_assertion_fail"),
    (re.compile(r"Expected URL to contain", re.I),       "url_assertion_fail"),
    (re.compile(r"page\.goto.*Timeout", re.I),           "navigation_timeout"),
    (re.compile(r"net::ERR", re.I),                      "navigation_error"),
    (re.compile(r"strict mode violation", re.I),         "selector_ambiguous"),
    (re.compile(r"net::ERR_NAME_NOT_RESOLVED", re.I),    "dns_error"),
    (re.compile(r"\b(403|404|500|502|503|504)\b"),       "server_error"),
)

# Defect-class -> severity / priority defaults. Tunable per area
# importance via :func:`adjust_for_area`.
CLASS_DEFAULTS: dict[str, dict[str, str]] = {
    "click_timeout":        {"severity": "Major",    "priority": "High"},
    "fill_timeout":         {"severity": "Major",    "priority": "High"},
    "select_timeout":       {"severity": "Major",    "priority": "High"},
    "check_timeout":        {"severity": "Minor",    "priority": "Medium"},
    "navigation_timeout":   {"severity": "Critical", "priority": "Highest"},
    "navigation_error":     {"severity": "Critical", "priority": "Highest"},
    "dns_error":            {"severity": "Critical", "priority": "Highest"},
    "text_assertion_fail":  {"severity": "Major",    "priority": "High"},
    "url_assertion_fail":   {"severity": "Major",    "priority": "High"},
    "selector_ambiguous":   {"severity": "Minor",    "priority": "Medium"},
    "server_error":         {"severity": "Critical", "priority": "Highest"},
    "unknown":              {"severity": "Major",    "priority": "Medium"},
}

# Areas considered "core" for severity boosting. Substring-matched against
# component / TC summary / URL path.
CORE_AREA_HINTS = (
    "login", "sign in", "signin", "auth",
    "checkout", "payment", "cart", "purchase", "order",
    "register", "sign up", "signup",
    "search", "navigation", "nav",
    "home", "homepage",
)


# ── Defect classification ─────────────────────────────────────────

def classify_error(error_message: str) -> str:
    """Return a defect-class key from ERROR_CLASS_PATTERNS, or
    ``"unknown"`` if nothing matches. Always returns a string, never
    raises. Operators see this in the bug as a label so they can group
    on it later."""
    if not error_message:
        return "unknown"
    msg = str(error_message)
    for pat, kind in ERROR_CLASS_PATTERNS:
        if pat.search(msg):
            return kind
    return "unknown"


def is_core_area(*hints: str) -> bool:
    """True if any hint string contains a core-area keyword. Used to
    bump severity from Major -> Critical when a primary user journey
    is affected."""
    blob = " ".join(h or "" for h in hints).lower()
    return any(k in blob for k in CORE_AREA_HINTS)


def severity_priority(defect_class: str,
                      *area_hints: str) -> tuple[str, str]:
    """Return (severity, priority) for a defect class.

    Boosts severity to Critical and priority to Highest when the affected
    area is one of CORE_AREA_HINTS — login, checkout, etc. The full
    decision table lives in CLASS_DEFAULTS; this is the only public
    knob callers should touch.
    """
    base = CLASS_DEFAULTS.get(defect_class, CLASS_DEFAULTS["unknown"])
    sev = base["severity"]
    pri = base["priority"]
    if is_core_area(*area_hints):
        # Bump one rung up the ladder.
        sev_ladder = ["Trivial", "Minor", "Major", "Critical"]
        pri_ladder = ["Lowest", "Low", "Medium", "High", "Highest"]
        try:
            sev = sev_ladder[min(sev_ladder.index(sev) + 1,
                                 len(sev_ladder) - 1)]
        except ValueError:
            pass
        try:
            pri = pri_ladder[min(pri_ladder.index(pri) + 1,
                                 len(pri_ladder) - 1)]
        except ValueError:
            pass
    return sev, pri


# ── Component extraction ──────────────────────────────────────────

_AREA_PATTERNS = (
    # path-segment hints from URL — last non-empty segment becomes
    # the component fallback.
    re.compile(r"/(?:en|ua|uk|ru|es|fr)/?(.+?)/?$", re.I),
)


def extract_component(*, tc_section: str = "",
                      final_url: str = "",
                      tc_summary: str = "") -> str:
    """Pick a human-friendly component label.

    Priority: explicit TC section > URL last path segment > TC summary
    first 3 words. Always returns SOMETHING because empty component
    produces titles like ": Button click times out" which look broken.
    """
    if tc_section and tc_section.strip():
        return tc_section.strip().rstrip(":")
    if final_url:
        try:
            from urllib.parse import urlparse
            path = (urlparse(final_url).path or "").strip("/")
            if path:
                last = path.split("/")[-1]
                # Drop file extensions like .html
                last = re.sub(r"\.(html?|aspx?|php)$", "", last, flags=re.I)
                last = last.replace("-", " ").replace("_", " ").strip()
                if last:
                    return last.title() + " Page"
        except Exception:
            pass
    if tc_summary:
        words = re.sub(r"^Verify that\s+", "", tc_summary,
                       flags=re.I).split()
        if words:
            return " ".join(words[:3]).rstrip(",.:;").title()
    return "Application"


# ── Title / summary builders ──────────────────────────────────────

# Defect-class -> short observable phrase used in titles + AR.
DEFECT_PHRASES: dict[str, str] = {
    "click_timeout":       "click does not register; element times out",
    "fill_timeout":        "input field cannot be filled; element times out",
    "select_timeout":      "dropdown selection times out without selecting",
    "check_timeout":       "checkbox toggle times out",
    "navigation_timeout":  "page navigation times out",
    "navigation_error":    "page fails to load (network error)",
    "dns_error":           "host cannot be resolved (DNS error)",
    "text_assertion_fail": "expected text is missing from the page",
    "url_assertion_fail":  "URL does not match the expected destination",
    "selector_ambiguous":  "multiple elements match the same selector",
    "server_error":        "server returns an HTTP error response",
    "unknown":             "unexpected behaviour during execution",
}


def build_title(component: str, defect_class: str,
                step_action: str = "",
                error_message: str = "") -> str:
    """Compose a title following the standard:

        [Component]: [Observable defect, terse, no banned phrases]

    7–15 words target. Strips banned phrases. Falls back to a generic
    phrase when defect_class is unknown.
    """
    phrase = DEFECT_PHRASES.get(defect_class) or DEFECT_PHRASES["unknown"]
    # If we know the action that failed, mention the surface (button,
    # field, link) for extra specificity.
    surface = ""
    if step_action == "click":
        # extract a target hint from the error message if present
        m = re.search(r'role=button\[name=/([^/]+)/', error_message or "", re.I)
        if m:
            surface = m.group(1).strip().split('|')[0]
        else:
            m2 = re.search(r"text=([^\s]+)", error_message or "")
            if m2:
                surface = m2.group(1).strip().strip("\"'")
    title = f"{component}: {phrase}"
    if surface and len(surface) < 32 and surface not in title:
        title = f"{component} — {surface!r}: {phrase}"
    title = _strip_banned(title)
    # Cap at 130 chars (~15 words). Trim cleanly on the last space.
    if len(title) > 130:
        cut = title.rfind(" ", 0, 127)
        if cut > 60:
            title = title[:cut] + "…"
        else:
            title = title[:127] + "…"
    return title


def _strip_banned(text: str) -> str:
    """Remove banned filler phrases from a string. Case-insensitive,
    leaves surrounding whitespace tidy."""
    out = text
    for ph in BANNED_TITLE_PHRASES:
        out = re.sub(re.escape(ph), "", out, flags=re.I)
    out = re.sub(r"\s{2,}", " ", out).strip(" -—:;,.")
    return out


def build_summary(*, component: str, defect_class: str,
                  step_action: str = "",
                  error_message: str = "",
                  final_url: str = "",
                  console_errors: list[str] | None = None,
                  tc_summary: str = "") -> str:
    """Compose a 2–5 sentence narrative description of the defect.

    Includes scenario context and impact, omits speculation about root
    cause, references console errors when present.
    """
    sentences: list[str] = []
    phrase = DEFECT_PHRASES.get(defect_class) or DEFECT_PHRASES["unknown"]
    where = f" on the {component}" if component and component != "Application" else ""
    # Strip "Verify that " — TC summaries usually embed it but it reads
    # awkward in narrative description ("While exercising the Verify
    # that the homepage...").
    scenario = re.sub(r"^Verify that\s+", "", tc_summary or "",
                      flags=re.I).strip().rstrip(".")
    if not scenario:
        scenario = "scenario"
    sentences.append(
        f"While exercising the scenario \"{scenario}\", "
        f"the {phrase}{where}.".replace(" ,", ",")
    )
    if final_url:
        sentences.append(f"The defect surfaces at {final_url}.")
    if step_action:
        verbed = {
            "click": "clicking",
            "fill": "filling",
            "select": "selecting from a dropdown",
            "check": "toggling a checkbox",
            "expect_text": "verifying the page text",
            "expect_url": "verifying the destination URL",
            "goto": "navigating to the page",
            "scroll": "scrolling the page",
        }.get(step_action, step_action)
        sentences.append(f"It is reproducible by {verbed} after the prior step succeeds.")
    if console_errors:
        head = console_errors[0][:140].replace('"', "'")
        sentences.append(f"Browser console reports: \"{head}\".")
    sentences.append(
        "User cannot complete the test scenario; the run is blocked at this step."
    )
    return " ".join(sentences)


# ── STR / AR / ER builders ────────────────────────────────────────

def build_steps_to_reproduce(*, base_url: str,
                              tc_steps: str = "",
                              tc_preconditions: str = "",
                              failure_step_action: str = "",
                              failure_step_index: int = 0) -> str:
    """Return numbered atomic STR.

    Strategy:
      * If TC has explicit steps, normalise them (one action per line).
      * Otherwise synthesise from the failed step + base URL.
      * Always start with "1. Navigate to <URL>".
    """
    # Track preamble (preconditions) separately from numbered steps so
    # the closing "Observe" step always gets the next sequential number,
    # never a duplicate of the prior verb step (regression spotted in
    # smoke-test #2).
    preamble: list[str] = []
    if tc_preconditions and tc_preconditions.strip():
        preamble.append(
            f"Preconditions: {tc_preconditions.strip().rstrip('.')}.")
    numbered: list[str] = []
    numbered.append(f"Navigate to {base_url or '<application URL>'}.")
    if tc_steps and tc_steps.strip():
        # The TC steps may already be numbered; normalise + renumber.
        raw = re.split(r"\r?\n+", tc_steps.strip())
        for line in raw:
            s = re.sub(r"^\s*\d+[.)]\s*", "", line).strip()
            if s:
                numbered.append(s.rstrip("."))
    elif failure_step_action:
        verb = {
            "click":       "Click the button identified in the test case.",
            "fill":        "Enter the value into the corresponding input field.",
            "select":      "Select the option from the dropdown.",
            "check":       "Toggle the checkbox.",
            "expect_text": "Verify that the expected text is present.",
            "expect_url":  "Verify that the page URL matches the expected target.",
            "goto":        "Open the target URL.",
            "scroll":      "Scroll through the page content.",
        }.get(failure_step_action, "Perform the action defined in the test case.")
        numbered.append(verb)
    numbered.append("Observe the result on the resulting page.")
    rendered = preamble + [f"{i}. {step}" for i, step in
                            enumerate(numbered, start=1)]
    return "\n".join(rendered)


def build_actual_result(*, defect_class: str,
                        error_message: str = "",
                        final_url: str = "",
                        console_errors: list[str] | None = None) -> str:
    """Compose AR with observable state + verbatim error if present."""
    phrase = DEFECT_PHRASES.get(defect_class) or DEFECT_PHRASES["unknown"]
    bits: list[str] = [phrase.capitalize() + "."]
    if error_message and error_message.strip():
        first_line = error_message.strip().splitlines()[0][:300]
        bits.append(f"Underlying error: {first_line}")
    if final_url:
        bits.append(f"Final URL observed: {final_url}.")
    if console_errors:
        head = console_errors[0][:200]
        bits.append(f"Browser console reports: {head}")
    return " ".join(bits)


def build_expected_result(*, defect_class: str,
                          tc_expected: str = "",
                          step_action: str = "") -> str:
    """Compose ER in 'should' phrasing. Prefers the TC's explicit
    expected_result when it exists, else derives a sensible default
    from the defect class + action."""
    if tc_expected and tc_expected.strip():
        exp = tc_expected.strip().rstrip(".")
        if not re.search(r"\bshould\b", exp, re.I):
            exp = f"The system should behave as specified: {exp}"
        return exp + "."
    fallback = {
        "click_timeout":
            "The button should respond to the click within 1 second and "
            "perform its associated action visibly.",
        "fill_timeout":
            "The input field should accept keyboard input and display the "
            "typed value as it is entered.",
        "select_timeout":
            "The dropdown should open on click and accept the chosen "
            "option, updating the field to reflect the selection.",
        "navigation_timeout":
            "The page should load and become interactive within 5 seconds, "
            "displaying its primary content.",
        "navigation_error":
            "The page should load successfully over HTTPS and serve its "
            "primary content.",
        "text_assertion_fail":
            "The page should render the expected text after the prior step "
            "completes.",
        "url_assertion_fail":
            "The browser should navigate to the expected destination URL "
            "after the action.",
        "selector_ambiguous":
            "The page should expose exactly one element matching the "
            "described label so user actions are unambiguous.",
        "server_error":
            "The backend should return a successful HTTP response (2xx) "
            "for the request triggered by this step.",
        "dns_error":
            "The host should resolve and serve a valid response.",
    }
    return fallback.get(
        defect_class,
        "The application should perform the action correctly without "
        "errors and proceed to the next state.",
    )


# ── Top-level orchestrator ────────────────────────────────────────

def rewrite_bug_from_automation(bug_dict: dict[str, Any], *,
                                 automation_failure: dict[str, Any] | None,
                                 base_url: str = "",
                                 tc_summary: str = "",
                                 tc_steps: str = "",
                                 tc_preconditions: str = "",
                                 tc_expected: str = "",
                                 tc_section: str = "") -> dict[str, Any]:
    """Mutate ``bug_dict`` in place with rule-driven fields when
    Playwright failure context is available.

    Falls back to the existing dict (already populated by the
    deterministic simulator) when ``automation_failure`` is None or
    empty — that branch keeps "minor cosmetic" simulator-only bugs
    intact rather than replacing their content with a generic stub.

    Returns the same dict for caller-chaining convenience.
    """
    if not automation_failure:
        # Even without automation, scrub the most common banned phrase
        # from the title so simulator-only bugs at least look polished.
        title = bug_dict.get("title", "")
        scrubbed = _strip_banned(title)
        if scrubbed and scrubbed != title:
            bug_dict["title"] = scrubbed
        return bug_dict
    err = automation_failure.get("comment") or ""
    final_url = automation_failure.get("final_url") or base_url or ""
    console = automation_failure.get("console_errors") or []
    step_action = automation_failure.get("step_action") or ""
    step_index = int(automation_failure.get("step_index") or 0)

    defect_class = classify_error(err)
    component = extract_component(
        tc_section=tc_section, final_url=final_url, tc_summary=tc_summary,
    )
    sev, pri = severity_priority(defect_class, component, tc_summary, final_url)

    bug_dict["title"] = build_title(component, defect_class,
                                    step_action=step_action,
                                    error_message=err)
    # Some templates use 'summary' explicitly; legacy code reads from
    # 'title' as a stand-in. Set both so neither path looks ragged.
    bug_dict.setdefault("description", "")
    bug_dict["description"] = build_summary(
        component=component, defect_class=defect_class,
        step_action=step_action, error_message=err,
        final_url=final_url, console_errors=console,
        tc_summary=tc_summary,
    )
    bug_dict["actual_result"] = build_actual_result(
        defect_class=defect_class, error_message=err,
        final_url=final_url, console_errors=console,
    )
    bug_dict["expected_result"] = build_expected_result(
        defect_class=defect_class, tc_expected=tc_expected,
        step_action=step_action,
    )
    bug_dict["steps_to_reproduce"] = build_steps_to_reproduce(
        base_url=base_url, tc_steps=tc_steps,
        tc_preconditions=tc_preconditions,
        failure_step_action=step_action,
        failure_step_index=step_index,
    )
    bug_dict["severity"] = sev
    bug_dict["priority"] = pri
    # Frequency stays "Always" for deterministic single-run failures
    # per the standards (single-run rule); change only if we have
    # cross-run evidence.
    bug_dict["frequency"] = bug_dict.get("frequency") or "Always"
    # Stamp the defect-class as a label so operators can filter the
    # backlog by failure mode (click_timeout, text_assertion_fail, ...).
    labels = list(bug_dict.get("labels") or [])
    cls_label = f"defect:{defect_class}"
    if cls_label not in labels:
        labels.append(cls_label)
    bug_dict["labels"] = labels
    return bug_dict


__all__ = [
    "BANNED_TITLE_PHRASES",
    "ERROR_CLASS_PATTERNS",
    "CLASS_DEFAULTS",
    "classify_error",
    "is_core_area",
    "severity_priority",
    "extract_component",
    "build_title",
    "build_summary",
    "build_steps_to_reproduce",
    "build_actual_result",
    "build_expected_result",
    "rewrite_bug_from_automation",
]
