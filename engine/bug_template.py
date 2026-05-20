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

# Defect-class -> Severity baseline (system impact ONLY).
# Aligned with testsigma's framing:
#   https://testsigma.com/blog/difference-between-priority-and-severity/
#
# **Severity** measures how badly the defect impairs the application
# itself — it is INDEPENDENT of when/whether the team fixes it.
#
#   * Critical — system unusable; blocks core flow; data loss / crash;
#     security breach; whole module down.
#   * Major    — important feature works incorrectly; significant
#     deviation from spec; common task is broken; workaround
#     exists but is inconvenient.
#   * Minor    — small deviation that doesn't break the feature;
#     visual glitch on a non-critical screen; one of several paths
#     fails.
#   * Trivial  — cosmetic issue; typo; spacing; non-blocking
#     accessibility hint; no functional impact.
#
# Defect classes map to severity by what they actually break in the
# system, NOT by the area:
CLASS_SEVERITY: dict[str, str] = {
    # The element disappeared / didn't show — the action itself can't
    # complete. Regardless of WHICH action, the user is stuck on this
    # step.
    "click_timeout":        "Major",
    "fill_timeout":         "Major",
    "select_timeout":       "Major",
    "check_timeout":        "Minor",   # checkbox toggle is rarely path-blocking
    # The PAGE doesn't load — the user can't even reach the feature.
    # That's "system unusable" by any definition.
    "navigation_timeout":   "Critical",
    "navigation_error":     "Critical",
    "dns_error":            "Critical",
    # Content / contract assertions — the page renders but doesn't
    # match what the spec promises. Major if the assertion was about
    # something the user actively reads (text, URL); Minor if it's
    # about flexibility (one of multiple selectors matched).
    "text_assertion_fail":  "Major",
    "url_assertion_fail":   "Major",
    "selector_ambiguous":   "Minor",
    # Server returned 4xx/5xx — system-level failure regardless of
    # which screen the user was on.
    "server_error":         "Critical",
    # ── TFWefloLab walkthrough defect classes ─────────────────────
    # Map TFWefloLab's `findings[].severity` strings ("Critical" /
    # "High" / "Medium" / "Low") to TestForTge's testsigma-aligned
    # severity ladder. The walkthrough emits these via
    # walkthrough_runner heuristics; severity here is the SYSTEM
    # impact regardless of where the defect surfaces (priority then
    # folds in area weight via severity_priority()).
    "broken_image":         "Major",     # marketing pages: empty slot
                                          # tanks perceived credibility
    "hamburger_dead":       "Critical",  # mobile nav unreachable =
                                          # whole site blocked on phones
    "dropdown_dead":        "Major",     # one nav sub-menu won't open
    "homepage_no_title":    "Minor",     # SEO/cosmetic, page still works
    "homepage_no_h1":       "Minor",
    "footer_dead_page":     "Major",     # near-empty inner page
    "placeholder_social":   "Major",     # footer points at example.com
    "social_no_noopener":   "Minor",     # security-hygiene, not blocking
    "malformed_link":       "Minor",
    "search_no_results":    "Major",
    "search_broken":        "Major",
    "form_unfillable":      "Major",     # form input that rejects input
    "cta_no_destination":   "Major",     # button that does nothing
    "cta_tiny_tap_target":  "Minor",     # <24px hit area, hard to tap
    "cta_disabled":         "Minor",
    "axe_critical":         "Critical",  # axe-core impact=critical
    "axe_serious":          "Major",     # axe-core impact=serious
    "clipped_text":         "Minor",     # overflow:hidden truncates label
    "icon_fallback":        "Minor",     # tofu/PUA char where icon
                                          # should render
    "modal_overflow":       "Minor",     # modal mis-sized for viewport
    "console_js_error":     "Major",     # uncaught exception during walk
    "page_error":           "Major",     # window.onerror caught a throw
    "walk_step_failed":     "Minor",     # interaction (scroll, etc.)
                                          # raised but didn't block run
    # ──────────────────────────────────────────────────────────────
    "unknown":              "Major",
}

# **Priority** measures how urgently the team should fix the defect.
# Per testsigma, priority is DRIVEN BY:
#   * how visible the defect is to end-users (frequency of encounter)
#   * what business value the affected area carries
#   * release timing pressure
#
# Priority is NOT a function of severity alone — a Critical bug in a
# rarely-used admin screen may be Low priority, while a Trivial typo on
# the homepage hero may be Highest because every visitor sees it.
#
# We model priority via the affected area's BUSINESS WEIGHT, then fold
# in severity as a tie-breaker.

# Area weight lookup. Substring-matched against component / TC summary
# / URL path. Higher weight = more business-critical.
AREA_WEIGHTS: list[tuple[tuple[str, ...], int]] = [
    # Revenue-path features get the top weight — broken checkout costs
    # money every minute it's down.
    (("checkout", "payment", "cart", "purchase", "order",
      "billing", "invoice", "subscription"), 5),
    # Auth gates everything; users who can't log in see nothing.
    (("login", "sign in", "signin", "auth",
      "register", "sign up", "signup",
      "password", "2fa", "mfa", "otp"), 4),
    # First-impression surfaces — the homepage and search are the most-
    # visited pages on most products.
    (("home", "homepage", "landing", "index",
      "search", "navigation", "nav", "menu", "header"), 3),
    # Core in-app workflows below the auth gate.
    (("dashboard", "profile", "account", "settings",
      "create", "edit", "delete"), 2),
    # Everything else (admin, support, marketing pages…) defaults to 1.
]

# Backwards-compat — the picker still imports CORE_AREA_HINTS for
# is_core_area. We keep the symbol but redefine via the new weights.
CORE_AREA_HINTS = tuple(
    kw for keywords, weight in AREA_WEIGHTS if weight >= 3
    for kw in keywords
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
    """True if any hint string contains a core-area keyword (auth,
    checkout, search, navigation, homepage). Surfaces in the Guide
    explanation; severity_priority() uses the richer area-weight model
    instead."""
    blob = " ".join(h or "" for h in hints).lower()
    return any(k in blob for k in CORE_AREA_HINTS)


def _area_weight(*hints: str) -> int:
    """Resolve the business weight of the affected area. Returns the
    HIGHEST weight from AREA_WEIGHTS that has at least one keyword
    appearing in the supplied hints. Falls back to 1 (everything else)
    when nothing matches."""
    blob = " ".join(h or "" for h in hints).lower()
    best = 1
    for keywords, weight in AREA_WEIGHTS:
        if any(k in blob for k in keywords) and weight > best:
            best = weight
    return best


def severity_priority(defect_class: str,
                      *area_hints: str) -> tuple[str, str]:
    """Return (severity, priority) computed as INDEPENDENT axes
    per the testsigma framing.

    **Severity** comes purely from the defect class — what's broken
    in the system, regardless of where:
        Critical / Major / Minor / Trivial → CLASS_SEVERITY[defect_class]

    **Priority** comes from the AREA WEIGHT + severity tie-breaker:
        weight 5 (revenue path)        → Highest at any non-Trivial sev
        weight 4 (auth, gating)        → Highest if Critical, else High
        weight 3 (homepage, search,    → High if Critical, Medium if
                  navigation)            Major, Low if Minor
        weight 2 (in-app workflows)    → Medium if Critical, Low if
                                         Major, Lowest if Minor
        weight 1 (everything else)     → Low if Critical, Lowest else

    This deliberately produces decoupled examples like:
      * login click_timeout       → Major / Highest (Major bug, but
                                    auth flow → fix immediately)
      * homepage typo             → Trivial / High (cosmetic, but
                                    every visitor sees it)
      * admin-page text mismatch  → Major / Low (real bug, niche
                                    audience, fix in next sprint)
    """
    sev = CLASS_SEVERITY.get(defect_class) or CLASS_SEVERITY["unknown"]
    weight = _area_weight(*area_hints)

    # Map (weight, severity) -> priority. Independent table — easier
    # to reason about than a chain of if/elif.
    if weight >= 5:
        pri = "Highest" if sev != "Trivial" else "High"
    elif weight == 4:
        pri = ("Highest" if sev == "Critical"
               else "High" if sev in ("Major", "Minor")
               else "Medium")
    elif weight == 3:
        pri = ("High" if sev == "Critical"
               else "Medium" if sev == "Major"
               else "Low" if sev == "Minor"
               else "Lowest")
    elif weight == 2:
        pri = ("Medium" if sev == "Critical"
               else "Low" if sev == "Major"
               else "Lowest")
    else:  # weight 1 — niche / admin / non-customer-visible
        pri = "Low" if sev == "Critical" else "Lowest"

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
    if tc_section and isinstance(tc_section, str) and tc_section.strip():
        return tc_section.strip().rstrip(":")
    # Defensive: callers occasionally hand us None / non-string values
    # for final_url when reconstruction failed mid-flight. Coerce to
    # string and bail early when there's nothing to parse — without
    # this guard we'd produce titles like "None Page".
    if final_url and isinstance(final_url, str):
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
    # Walkthrough phrases — match TFWefloLab's user-facing wording so
    # operators see the same headline whether the run came from
    # walkthrough mode or TC-driven mode.
    "broken_image":        "image fails to load (broken-image icon visible to visitors)",
    "hamburger_dead":      "mobile hamburger menu does not open when tapped",
    "dropdown_dead":       "header dropdown menu does not open on hover/click",
    "homepage_no_title":   "homepage is missing a <title> element",
    "homepage_no_h1":      "homepage has no visible <h1> heading",
    "footer_dead_page":    "inner page renders near-empty content",
    "placeholder_social":  "footer social link points to a placeholder host",
    "social_no_noopener":  "external link opens in new tab without rel=\"noopener\"",
    "malformed_link":      "link href is malformed and cannot be parsed",
    "search_no_results":   "search field accepts input but returns no results",
    "search_broken":       "search field interaction fails",
    "form_unfillable":     "form input field rejects keyboard input",
    "cta_no_destination":  "call-to-action button has no destination (href=\"#\" or empty)",
    "cta_tiny_tap_target": "tap target is smaller than the 24x24 px minimum",
    "cta_disabled":        "call-to-action is rendered disabled on the page",
    "axe_critical":        "accessibility rule violation (axe-core impact=critical)",
    "axe_serious":         "accessibility rule violation (axe-core impact=serious)",
    "clipped_text":        "text content is clipped by its container's overflow",
    "icon_fallback":       "icon font failed to load; fallback character is shown",
    "modal_overflow":      "modal dialog does not fit the viewport correctly",
    "console_js_error":    "uncaught JavaScript error in the browser console",
    "page_error":          "uncaught page-level exception during the walkthrough",
    "walk_step_failed":    "walkthrough interaction step raised an exception",
    "unknown":             "unexpected behaviour during execution",
}


# Walkthrough findings carry a free-text ``area`` label
# ("Images", "Navigation", "Footer", "Accessibility", "CTAs", ...)
# that maps 1:1 to a defect class. Used by walkthrough_runner to label
# a finding without the heuristic having to know about CLASS_SEVERITY.
# Multiple areas may resolve to the same class when the system impact
# is identical (e.g. "Console" + "JS" → console_js_error).
WALKTHROUGH_AREA_TO_CLASS: dict[str, str] = {
    "Images":               "broken_image",
    "Navigation":           "hamburger_dead",  # override in note() when
                                                # the cause is a dropdown
    "Layout":               "modal_overflow",
    "Footer":               "placeholder_social",
    "Security":             "social_no_noopener",
    "Search":               "search_broken",
    "Forms":                "form_unfillable",
    "CTAs":                 "cta_no_destination",
    "Accessibility":        "axe_serious",
    "Text clipping":        "clipped_text",
    "Icon fallback":        "icon_fallback",
    "Modal layout":         "modal_overflow",
    "JS":                   "page_error",
    "Console":              "console_js_error",
    "Loading":              "navigation_timeout",
    "HTTP":                 "server_error",
    "SEO":                  "homepage_no_title",
    "Content":              "homepage_no_h1",
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
        # ── Walkthrough fallbacks ─────────────────────────────────
        "broken_image":
            "The image should load successfully and render at its "
            "designed size so visitors see the intended content.",
        "hamburger_dead":
            "Tapping the hamburger icon on mobile/tablet should open "
            "the navigation menu and reveal every top-level link.",
        "dropdown_dead":
            "Hovering or clicking the dropdown trigger should open its "
            "sub-menu and let the visitor reach the nested pages.",
        "homepage_no_title":
            "The homepage should expose a non-empty <title> so the "
            "browser tab, search results, and history entries are "
            "meaningful.",
        "homepage_no_h1":
            "The homepage should expose a single visible <h1> "
            "describing the page's primary topic.",
        "footer_dead_page":
            "Linked inner pages should render their full content "
            "rather than a near-empty body.",
        "placeholder_social":
            "Footer social links should point to the brand's real "
            "social media profiles, not placeholder hosts.",
        "social_no_noopener":
            "External links that open in a new tab should include "
            'rel="noopener" so window.opener leaks are prevented.',
        "malformed_link":
            "Every link's href should be a valid URL that the browser "
            "can resolve and follow.",
        "search_no_results":
            "Submitting a query through the site search should return "
            "matching results or a friendly empty-state message within "
            "a few seconds.",
        "search_broken":
            "The site search should accept keyboard input and return a "
            "result or empty-state panel after the query is submitted.",
        "form_unfillable":
            "Each visible form input should accept keyboard input and "
            "display the typed value as it is entered.",
        "cta_no_destination":
            "Every visible call-to-action should have a destination "
            "(href, click handler, or modal trigger) so visitors know "
            "what tapping it will do.",
        "cta_tiny_tap_target":
            "Interactive elements should expose at least a 24x24 px "
            "hit area (WCAG 2.5.5) so touch users can tap them "
            "reliably.",
        "cta_disabled":
            "Calls-to-action that the visitor is meant to interact "
            "with should not be rendered as disabled on the published "
            "page.",
        "axe_critical":
            "The page should pass the axe-core WCAG 2.1 AA ruleset "
            "without any critical-impact violations.",
        "axe_serious":
            "The page should pass the axe-core WCAG 2.1 AA ruleset "
            "without any serious-impact violations.",
        "clipped_text":
            "Container styles should accommodate the rendered text "
            "without clipping it with overflow:hidden or fixed widths.",
        "icon_fallback":
            "Icon fonts should load successfully so visitors see the "
            "designed glyph rather than a fallback character.",
        "modal_overflow":
            "Modal dialogs should size themselves to fit every "
            "supported viewport without overflowing or under-filling.",
        "console_js_error":
            "The page session should complete without emitting any "
            "JavaScript console errors that affect user-visible "
            "behaviour.",
        "page_error":
            "The page should not emit uncaught page-level exceptions "
            "during a typical user journey.",
        "walk_step_failed":
            "The walkthrough interaction (scroll, hover, navigation) "
            "should complete without raising an exception.",
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
    "CLASS_SEVERITY",
    "DEFECT_PHRASES",
    "WALKTHROUGH_AREA_TO_CLASS",
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
