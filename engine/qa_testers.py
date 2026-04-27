"""
TestFortge — QA Tester Personas

Defines 4 QA tester personas who work under the QA Team Lead
(qa_team_lead.py). They execute test cases and checklists generated
by the Senior QA Engineer and reviewed by the Team Lead.

Team Composition:
  - 2 Middle QA Engineers (mid_1, mid_2)
  - 2 Junior+ QA Engineers (jr_1, jr_2)

All testers hold:
  - ISTQB Foundation Level
  - ISTQB Advanced Test Analyst

Each tester can execute ANY combination of testing types —
there are no specialty restrictions.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ═══════════════════════════════════════════════════════════════════
# 1. Tester Persona
# ═══════════════════════════════════════════════════════════════════

@dataclass
class Tester:
    """A QA tester persona with defined skills and focus areas."""
    id: str                   # e.g. "mid_1", "mid_2", "jr_1", "jr_2"
    name: str                 # e.g. "Olena Marchenko"
    level: str                # "Middle" or "Junior+"
    certifications: list[str]
    specialties: list[str]    # all testing types — no restrictions
    domains: list[str]        # e.g. ["E-commerce", "FinTech", "Healthcare"]
    tools: list[str]          # e.g. ["Chrome DevTools", "Lighthouse", "axe"]


# ═══════════════════════════════════════════════════════════════════
# 2. Testing Types (MUST come before TESTERS)
# ═══════════════════════════════════════════════════════════════════

TESTING_TYPES: list[str] = [
    "Functional",
    "Usability",
    "UI/UX",
    "Accessibility",
    "Smoke",
    "Regression",
    "SEO",
    "Performance",
    "E&E",
]


# ═══════════════════════════════════════════════════════════════════
# 3. Pre-defined Testers
# ═══════════════════════════════════════════════════════════════════

_SHARED_CERTIFICATIONS = [
    "ISTQB Foundation Level",
    "ISTQB Advanced Test Analyst",
]

TESTERS: list[Tester] = [
    Tester(
        id="mid_1",
        name="Olena Marchenko",
        level="Middle",
        certifications=list(_SHARED_CERTIFICATIONS),
        specialties=list(TESTING_TYPES),
        domains=["E-commerce", "FinTech", "SaaS"],
        tools=["Chrome DevTools", "Jira", "TestRail", "Postman"],
    ),
    Tester(
        id="mid_2",
        name="Dmytro Koval",
        level="Middle",
        certifications=list(_SHARED_CERTIFICATIONS),
        specialties=list(TESTING_TYPES),
        domains=["E-commerce", "Healthcare", "Government"],
        tools=["Lighthouse", "axe", "WAVE", "Chrome DevTools", "JMeter"],
    ),
    Tester(
        id="jr_1",
        name="Anastasia Bondar",
        level="Junior+",
        certifications=list(_SHARED_CERTIFICATIONS),
        specialties=list(TESTING_TYPES),
        domains=["E-commerce", "EdTech", "SaaS"],
        tools=["Chrome DevTools", "Jira", "Zephyr", "BrowserStack"],
    ),
    Tester(
        id="jr_2",
        name="Maxym Shevchenko",
        level="Junior+",
        certifications=list(_SHARED_CERTIFICATIONS),
        specialties=list(TESTING_TYPES),
        domains=["E-commerce", "Telecom", "FinTech"],
        tools=["BrowserStack", "Chrome DevTools", "Responsively", "Xcode Simulator"],
    ),
]


# ═══════════════════════════════════════════════════════════════════
# 4. Platform / Browser / Device Configuration
# ═══════════════════════════════════════════════════════════════════

@dataclass
class TestEnvironment:
    """Describes the platform, browser, device, and screen size for a test run."""
    platform: str     # "Windows", "Mac OS", or custom
    browser: str      # "Chrome", "Safari", "Microsoft Edge", "Firefox", or custom
    device: str       # "Desktop", "Android", "iOS", or custom
    screen_size: str  # e.g. "1920x1080", "375x812", or custom


PLATFORMS: list[str] = ["Windows", "Mac OS"]

BROWSERS: list[str] = ["Chrome", "Safari", "Microsoft Edge", "Firefox"]

DEVICES: list[str] = ["Desktop", "Android", "iOS"]

MOBILE_WEB: list[str] = ["iOS Chrome", "iOS Safari", "Android Chrome"]

SCREEN_SIZES: dict[str, str] = {
    "Desktop HD": "1920x1080",
    "Desktop": "1366x768",
    "Laptop": "1280x800",
    "Tablet": "768x1024",
    "Mobile": "375x812",
    "Mobile Small": "320x568",
}


# ─── Environment-aware option pools (Test Execution UI) ─────────
# A test run targets exactly one environment kind. The constants below
# back the per-kind dropdowns so the user only sees options that make
# sense for the selected environment (Web vs Mobile Web vs native iOS
# vs native Android). Each kind also accepts a free-text version field
# in the UI so the tester can record the exact OS / browser build.

WEB_PLATFORMS: list[str] = ["Windows", "macOS", "Linux", "ChromeOS"]

WEB_BROWSERS: list[str] = ["Chrome", "Microsoft Edge", "Firefox", "Safari", "Opera", "Brave"]

MOBILE_WEB_OSES: list[str] = ["iOS", "Android"]

MOBILE_WEB_BROWSERS: list[str] = [
    "Safari (iOS)", "Chrome", "Microsoft Edge", "Firefox",
    "Samsung Internet", "Opera Mobile",
]

# Common mobile / tablet device-pixel resolutions. Keys are user-facing
# labels, values are the WxH string we store on the run.
MOBILE_RESOLUTIONS: dict[str, str] = {
    "iPhone 15 Pro Max": "430x932",
    "iPhone 14/15": "390x844",
    "iPhone 13 mini / SE": "375x667",
    "Pixel 7 / 8": "412x915",
    "Galaxy S22 / S23": "360x780",
    "Galaxy Note": "412x914",
    "iPad Air / Pro 11\"": "820x1180",
    "iPad Pro 12.9\"": "1024x1366",
    "Android Tablet": "800x1280",
}

# Native device-model presets — purely suggestions; the tester can
# always override with the free-text Custom field shown in the UI.
IOS_DEVICES: list[str] = [
    "iPhone 15 Pro Max", "iPhone 15 Pro", "iPhone 15", "iPhone 14",
    "iPhone 13 mini", "iPhone SE (3rd gen)",
    "iPad Pro 12.9\"", "iPad Air", "iPad mini",
]

ANDROID_DEVICES: list[str] = [
    "Google Pixel 8 Pro", "Google Pixel 7", "Samsung Galaxy S24 Ultra",
    "Samsung Galaxy S23", "Samsung Galaxy A54", "Xiaomi Redmi Note 13",
    "OnePlus 12", "Samsung Galaxy Tab S9",
]


# ═══════════════════════════════════════════════════════════════════
# 5. Test Execution Status Constants
# ═══════════════════════════════════════════════════════════════════

EXECUTION_STATUSES: list[str] = ["Passed", "Failed", "Blocked"]


# ═══════════════════════════════════════════════════════════════════
# 6. Test Execution Result
# ═══════════════════════════════════════════════════════════════════

@dataclass
class TestExecutionResult:
    """Result of executing a single test case or checklist item."""
    item_id: str               # test case or checklist item ID
    item_type: str             # "test_case" or "checklist"
    status: str                # "Passed", "Failed", "Blocked"
    tester_id: str
    environment: TestEnvironment
    testing_types: list[str]
    comment: str = ""
    bug_id: str = ""           # linked bug report ID (for Failed/Blocked)
    timestamp: str = ""


# ═══════════════════════════════════════════════════════════════════
# 7. Tester Assignment
# ═══════════════════════════════════════════════════════════════════

def assign_testers(items_count: int) -> list[str]:
    """Distribute items across available testers using round-robin.

    Returns a list of tester IDs whose length equals *items_count*.
    Items are spread evenly; any remainder is distributed one extra
    item to the first testers in roster order.

    >>> assign_testers(5)
    ['mid_1', 'mid_2', 'jr_1', 'jr_2', 'mid_1']
    >>> assign_testers(0)
    []
    """
    if items_count <= 0:
        return []

    tester_ids = [t.id for t in TESTERS]
    return [tester_ids[i % len(tester_ids)] for i in range(items_count)]


# ═══════════════════════════════════════════════════════════════════
# 8. Tester Lookup
# ═══════════════════════════════════════════════════════════════════

def get_tester(tester_id: str) -> Tester | None:
    """Return a Tester by their ID, or None if not found.

    >>> get_tester("mid_1").name
    'Olena Marchenko'
    >>> get_tester("nonexistent") is None
    True
    """
    for tester in TESTERS:
        if tester.id == tester_id:
            return tester
    return None


# ═══════════════════════════════════════════════════════════════════
# 9. Auto-Execution Engine
# ═══════════════════════════════════════════════════════════════════

import hashlib
import re
from datetime import datetime, timezone


def _deterministic_hash(text: str, tester_id: str, env: str) -> int:
    """Stable hash for deterministic status assignment."""
    raw = f"{text}|{tester_id}|{env}"
    return int(hashlib.sha256(raw.encode()).hexdigest(), 16)


# Keywords that raise probability of failure during testing
_RISK_KEYWORDS = [
    "edge case", "negative", "security", "error", "invalid",
    "boundary", "empty", "exceed", "special character", "sql injection",
    "xss", "unauthorized", "forbidden", "timeout", "expired",
    "block", "disabled", "overflow", "null", "large",
]


def _compute_status(summary: str, category: str, priority: str,
                    tester_id: str, env: str) -> str:
    """Deterministically decide Passed/Failed/Blocked for an item.

    Uses a stable hash so the same input always produces the same status.
    Higher risk items (negative, security, edge case, high priority) have
    higher probability of failure.
    """
    h = _deterministic_hash(summary, tester_id, env)

    # Base fail probability: 12%
    fail_pct = 12

    # Raise for risky categories
    cat_lower = category.lower()
    if cat_lower in ("negative", "edge case", "security"):
        fail_pct += 15
    if priority == "High":
        fail_pct += 5

    # Raise for risk keywords in summary
    summary_lower = summary.lower()
    for kw in _RISK_KEYWORDS:
        if kw in summary_lower:
            fail_pct += 4
            break

    # Cap at 45%
    fail_pct = min(fail_pct, 45)

    # Blocked probability: 3% flat
    bucket = h % 100
    if bucket < 3:
        return "Blocked"
    if bucket < 3 + fail_pct:
        return "Failed"
    return "Passed"


def _generate_comment(status: str, summary: str, tester_name: str) -> str:
    """Generate a realistic tester comment based on status."""
    if status == "Passed":
        return ""
    if status == "Blocked":
        _blockers = [
            "Test environment is not available — server returned 503",
            "Required test data could not be prepared — database migration pending",
            "Dependent feature is not deployed to the test environment yet",
            "Third-party service (payment gateway) is down in staging",
            "Access permissions not configured — unable to reach the page",
        ]
        h = _deterministic_hash(summary, tester_name, "blocked")
        return _blockers[h % len(_blockers)]
    return ""


_NEGATION_TOKENS = (" no ", " not ", " never ", " without ", " cannot ", " can't ", " doesn't ", " don't ", " isn't ", " aren't ")


def _has_existing_negation(text: str) -> bool:
    """True when the sentence already contains a negation word.

    Prevents the negator from producing double-negation summaries like
    "has not no console errors" when the TC asserts a negative property
    (e.g. "has no console errors", "cannot access", "without errors").
    Surrounded with spaces so we don't match e.g. "innot" inside a word.
    """
    padded = " " + text.lower().strip() + " "
    return any(tok in padded for tok in _NEGATION_TOKENS)


def _negative_clause_bug_summary(s: str) -> str:
    """Build a bug summary for a TC whose objective already negates.

    Strategy: invert "no/not/cannot" to its positive form when we can,
    otherwise prefix "Issue: " so we never emit "is not no" or
    "does not cannot" garbage. Also prefer "and the page has no errors"
    → "and the page has errors".
    """
    inversions = [
        (r'\bhas no\b', 'has'),
        (r'\bhave no\b', 'have'),
        (r'\bwith no\b', 'with'),
        (r'\bwithout\b', 'with'),
        (r'\bcannot\b', 'can'),
        (r"\bcan't\b", 'can'),
        (r"\bdoesn't\b", 'does'),
        (r"\bdon't\b", 'do'),
        (r"\bisn't\b", 'is'),
        (r"\baren't\b", 'are'),
        (r'\bnever\b', ''),
        (r'\bnot\b', ''),
    ]
    out = s
    changed = False
    for pat, repl in inversions:
        new_s, count = re.subn(pat, repl, out, count=1, flags=re.I)
        if count:
            out = new_s
            changed = True
            break
    out = re.sub(r'\s{2,}', ' ', out).strip()
    if not changed or not out:
        return f"Issue: {s.strip()}"
    return out


def _make_bug_summary(tc_summary: str) -> str:
    """Convert test case summary to bug summary (passive voice, negated).

    Example:
      TC:  'Verify that login is completed successfully with valid credentials'
      Bug: 'Login is not completed successfully with valid credentials'

    Special case: if the TC already contains a negation word ("no",
    "never", "cannot", "without", ...) we invert the existing negation
    instead of stacking another "not" on top of it — otherwise we'd emit
    nonsense like "has not no console errors" (BUG-001).
    """
    s = tc_summary.strip()
    # Strip "Verify that " prefix
    prefix = "Verify that "
    if s.startswith(prefix):
        s = s[len(prefix):]
    elif s.lower().startswith(prefix.lower()):
        s = s[len(prefix):]

    if _has_existing_negation(s):
        s = _negative_clause_bug_summary(s)
        if s:
            s = s[0].upper() + s[1:]
        return s

    # Negate: insert "not" after first "is/are/can/should/does/has"
    patterns = [
        (r'\b(is)\b', r'\1 not'),
        (r'\b(are)\b', r'\1 not'),
        (r'\b(can)\b', r'can not'),
        (r'\b(should)\b', r'should not'),
        (r'\b(does)\b', r'does not'),
        (r'\b(has)\b', r'has not'),
        (r'\b(allows?)\b', r'does not allow'),
        (r'\b(displays?)\b', r'does not display'),
        (r'\b(works?)\b', r'does not work'),
        (r'\b(loads?)\b', r'does not load'),
        (r'\b(shows?)\b', r'does not show'),
        (r'\b(redirects?)\b', r'does not redirect'),
        (r'\b(accepts?)\b', r'does not accept'),
        (r'\b(rejects?)\b', r'does not reject'),
        (r'\b(validates?)\b', r'does not validate'),
        (r'\b(prevents?)\b', r'does not prevent'),
    ]
    negated = False
    for pat, repl in patterns:
        new_s, count = re.subn(pat, repl, s, count=1)
        if count > 0:
            s = new_s
            negated = True
            break

    if not negated:
        s = s + " — does not work as expected"

    # Capitalize first letter
    if s:
        s = s[0].upper() + s[1:]
    return s


def _make_bug_expected(tc_summary: str, tc_expected: str) -> str:
    """Generate expected result for bug report using should/should be."""
    if tc_expected:
        exp = tc_expected.strip()
        # Ensure it uses "should" phrasing
        if not any(w in exp.lower() for w in ("should", "shall")):
            exp = exp.rstrip(".")
            exp = f"The feature should behave as specified: {exp}"
        return exp
    # Fallback from summary
    s = tc_summary.strip()
    if s.startswith("Verify that "):
        s = s[len("Verify that "):]
    return f"The {s.rstrip('.')} should work as expected"


def _make_bug_actual(bug_summary: str) -> str:
    """Generate actual result for bug report."""
    return bug_summary


def _severity_from_priority(priority: str) -> str:
    """Map item priority to bug severity."""
    return {"High": "Major", "Medium": "Minor", "Low": "Trivial"}.get(priority, "Minor")


# ── Steps / preconditions synthesis for ISTQB-compliant bug reports ──
#
# ISTQB Foundation Level mandates non-empty Preconditions and
# Steps to Reproduce on every defect. The previous implementation
# left both fields empty for any bug auto-created from a checklist
# item (because checklist items themselves don't carry steps), making
# those bugs un-actionable. The helpers below synthesise both fields
# from whatever signal we have: the test case's own steps, the
# checklist objective, the section/component, the run environment,
# and the resource URL under test.

def _split_env(environment: str) -> dict:
    """Pull platform/browser/device/screen out of the joined env string.

    ``environment`` is built upstream as ``"<platform> / <browser> / <device> / <screen>"``.
    Falls back to whatever pieces are present so we never crash on a
    short/legacy string.
    """
    parts = [p.strip() for p in (environment or "").split("/")]
    while len(parts) < 4:
        parts.append("")
    return {"platform": parts[0], "browser": parts[1],
            "device": parts[2], "screen": parts[3]}


def _make_preconditions(item_type: str, section: str, environment: str,
                        site_url: str, item_preconditions: str = "") -> str:
    """Produce a non-empty ISTQB-style Preconditions block.

    Honours the item's own preconditions when present (test cases ship
    them, checklist items don't), then layers in environment + URL so
    a developer can replicate without consulting any other artefact.
    """
    env = _split_env(environment)
    pre_lines: list[str] = []
    if item_preconditions and item_preconditions.strip():
        pre_lines.append(item_preconditions.strip().rstrip("."))
    if env["platform"] and env["browser"]:
        pre_lines.append(
            f"Test environment ready: {env['platform']} with {env['browser']} "
            f"on {env['device'] or 'Desktop'} ({env['screen'] or 'default resolution'})"
        )
    elif environment:
        pre_lines.append(f"Test environment ready: {environment}")
    if site_url:
        pre_lines.append(f"Application under test is reachable at {site_url}")
    if section:
        section_clean = section.strip()
        pre_lines.append(f"User has access to the {section_clean} area of the application")
    if not pre_lines:
        pre_lines.append("Application is deployed to the test environment and reachable")
    return "; ".join(pre_lines) + "."


_VERB_HINTS = (
    ("login", "submit valid credentials on the login form"),
    ("log in", "submit valid credentials on the login form"),
    ("sign in", "submit valid credentials on the sign-in form"),
    ("sign up", "fill in the registration form"),
    ("register", "fill in the registration form"),
    ("search", "enter a query into the search field and submit"),
    ("checkout", "add an item to the cart and proceed to checkout"),
    ("payment", "proceed to the payment step and submit payment details"),
    ("upload", "select a file using the file picker"),
    ("download", "trigger the download action"),
    ("filter", "apply a filter from the filter panel"),
    ("sort", "select a sort option"),
    ("password", "submit the password change form"),
    ("email", "submit the email field"),
)


def _objective_to_action(objective: str) -> str:
    """Translate a checklist objective into a step verb-phrase.

    Looks up keyword hints first; otherwise echoes the objective as-is
    with the leading "Verify that " stripped so the step reads like an
    action a tester actually performs.
    """
    text = objective.strip().rstrip(".")
    lower = text.lower()
    for kw, action in _VERB_HINTS:
        if kw in lower:
            return action
    # Strip "Verify that ..." → use the rest as a noun-phrase action.
    for prefix in ("verify that ", "check that ", "ensure that ", "confirm that "):
        if lower.startswith(prefix):
            return f"perform the action: {text[len(prefix):]}"
    return f"perform the action: {text}"


def _make_steps_for_checklist(objective: str, section: str,
                              environment: str, site_url: str) -> str:
    """Build a numbered Steps-to-Reproduce list for a checklist-derived bug.

    The previous version returned ``""`` here, which violated ISTQB's
    mandatory-fields rule and made the bug un-actionable. Now we
    always emit a 4-step recipe: open the URL, navigate to the area,
    perform the action implied by the objective, observe the result.
    """
    env = _split_env(environment)
    target = site_url or "the application under test"
    browser = env["browser"] or "the browser"
    section_clean = (section or "").strip() or "the relevant"
    action = _objective_to_action(objective)

    steps = [
        f"Open {target} in {browser}.",
        f"Navigate to the {section_clean} section/page.",
        f"Attempt to {action}.",
        "Observe the application's response.",
    ]
    return "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))


def _make_steps_for_testcase(test_steps: str, summary: str, section: str,
                             environment: str, site_url: str) -> str:
    """Normalise (or synthesise) a numbered step list for a TC bug.

    Test cases generated by the Senior QA module already ship with
    ``"1. Step\\n2. Step"`` text, but legacy/imported TCs sometimes
    have a single-line blob or stray bullet markers. We delegate the
    coercion to :func:`bug_report.normalize_steps_to_numbered_list`,
    falling back to the checklist synthesiser when the field is empty.
    """
    from .bug_report import normalize_steps_to_numbered_list
    normalised = normalize_steps_to_numbered_list(test_steps)
    if normalised:
        return normalised
    return _make_steps_for_checklist(summary, section, environment, site_url)


def _make_found_in_build(environment: str, run_iso_ts: str) -> str:
    """Compose a build identifier from environment + run timestamp.

    Format: ``"<platform>-<browser>@<YYYYMMDDTHHMM>"`` — short enough
    for a Jira "Found in Build" cell, deterministic, and free of any
    spaces so it's safe to use as a label or filter value.
    """
    env = _split_env(environment)
    plat = (env["platform"] or "env").replace(" ", "")
    brw = (env["browser"] or "browser").replace(" ", "")
    ts = (run_iso_ts or "").replace("-", "").replace(":", "")[:13] or "now"
    return f"{plat}-{brw}@{ts}"


def execute_items(items: list, item_type: str, tester_id: str,
                  environment: str, testing_types: list[str],
                  selected_ids: list[str] | None = None,
                  site_url: str = "",
                  manual_statuses: dict[str, str] | None = None,
                  manual_bug_refs: dict[str, str] | None = None) -> dict:
    """Auto-execute test cases or checklist items.

    When *site_url* is provided the tester runs **real automated checks**
    against the live website (HTTP requests, link verification, HTML
    structure analysis, etc.) via :class:`SiteTestRunner`.  Items that
    can be matched to an automated check receive a real Passed/Failed
    status with an actual-result description.  Items that cannot be
    matched (e.g. purely manual UX checks) fall back to deterministic
    simulation.

    Parameters
    ----------
    items : list[TestCase | ChecklistItem dict]
        Items to execute (list of dicts).
    item_type : str
        "test_case" or "checklist".
    tester_id : str
        ID of the assigned tester.
    environment : str
        Environment string like "Windows / Chrome / Desktop / 1920x1080".
    testing_types : list[str]
        Selected testing types for this run.
    selected_ids : list[str] | None
        If provided, only execute items with these IDs.
        If None, execute all items.
    site_url : str
        URL of the website under test.  When provided the engine
        will crawl the site and perform real HTTP/HTML checks.
    manual_statuses : dict[str, str] | None
        Per-item overrides of the form ``{item_id: "Passed"|"Failed"|"Blocked"}``.
        Any item whose ID is a key here skips both the real-site runner and
        the deterministic simulator and takes the mapped status as-is.
    manual_bug_refs : dict[str, str] | None
        Per-item map ``{item_id: existing_bug_id}``.  When present, the
        result is stamped with the existing bug ID and NO new bug report
        is generated for that item — even on Failed/Blocked.

    Returns
    -------
    dict with keys:
        results: list of execution result dicts
        bugs: list of bug report dicts
        stats: dict with passed/failed/blocked/total counts
    """
    tester = get_tester(tester_id)
    tester_name = tester.name if tester else tester_id
    testing_types_str = ", ".join(testing_types)
    now = datetime.now(timezone.utc).isoformat()

    # ── Run real site checks if URL available ─────────────────
    real_checks: dict = {}
    runner = None
    if site_url:
        try:
            from .site_tester import SiteTestRunner
            runner = SiteTestRunner(site_url)
            real_checks = runner.run_all_checks()
        except Exception:
            pass  # fall back to simulation

    results = []
    bugs = []
    passed = failed = blocked = 0
    bug_counter = 0
    # Bookkeeping so callers can flash an honest summary of how each
    # status was decided — manual override, live HTTP check, or the
    # deterministic fallback simulator.
    sources = {"manual": 0, "real_check": 0, "simulated": 0}

    for item in items:
        item_id = item.get("id", "")

        # Skip if not in selected list
        if selected_ids is not None and item_id not in selected_ids:
            continue

        # Extract fields based on item type
        if item_type == "test_case":
            summary = item.get("summary", "")
            steps = item.get("test_steps", "")
            expected = item.get("expected_result", "")
            preconditions = item.get("preconditions", "")
            section = item.get("section", "")
        else:
            summary = item.get("objective", "")
            steps = ""
            expected = ""
            preconditions = ""
            section = item.get("section", "")

        category = item.get("category", "Positive")
        priority = item.get("priority", "Medium")

        # ── Decide status: manual override → real check → simulation ──
        status = ""
        comment = ""
        source = ""  # "manual" | "real_check" | "simulated"

        # Manual override wins: the tester set Pass/Fail/Blocked in the UI
        # before hitting Run. Skips both the real runner and the simulator
        # so the human verdict is always preserved.
        if manual_statuses and item_id in manual_statuses:
            mstatus = manual_statuses[item_id].strip()
            if mstatus in ("Passed", "Failed", "Blocked"):
                status = mstatus
                comment = f"Status set manually by {tester_name}."
                source = "manual"

        if not status and runner and summary:
            check_key = runner.match_item(summary)
            if check_key and check_key in real_checks:
                cr = real_checks[check_key]
                status = cr.status                    # "Passed" or "Failed"
                comment = cr.actual_result             # real description
                source = "real_check"

        if not status:
            # Fallback: deterministic simulation
            status = _compute_status(summary, category, priority, tester_id, environment)
            comment = _generate_comment(status, summary, tester_name)
            source = "simulated"

        sources[source] = sources.get(source, 0) + 1

        # Track counts
        if status == "Passed":
            passed += 1
        elif status == "Failed":
            failed += 1
        elif status == "Blocked":
            blocked += 1

        result_entry = {
            "item_id": item_id,
            "item_type": item_type,
            "status": status,
            "tester_id": tester_id,
            "tester_name": tester_name,
            "environment": environment,
            "testing_types": testing_types_str,
            "comment": comment,
            "bug_id": "",
            "timestamp": now,
            "source": source,
        }

        # If the tester linked this result to an existing bug, reuse it
        # rather than creating a new bug report.
        if manual_bug_refs and item_id in manual_bug_refs:
            ref = manual_bug_refs[item_id].strip()
            if ref:
                result_entry["bug_id"] = ref
                results.append(result_entry)
                continue

        # For Failed/Blocked: tester creates a bug report.
        # Every auto-generated defect must satisfy ISTQB's
        # mandatory-fields contract — preconditions and steps to
        # reproduce in particular were previously empty for any bug
        # spawned from a failed checklist item, so we synthesise them
        # here from the run context (objective + section + env + URL).
        if status in ("Failed", "Blocked"):
            bug_counter += 1
            bug_summary = _make_bug_summary(summary)
            bug_expected = _make_bug_expected(summary, expected)
            # Use real actual_result when available
            bug_actual = comment if comment else _make_bug_actual(bug_summary)

            # Steps + preconditions: synthesise so they are NEVER empty.
            if item_type == "test_case":
                bug_steps = _make_steps_for_testcase(
                    steps, summary, section, environment, site_url,
                )
            else:
                bug_steps = _make_steps_for_checklist(
                    summary, section, environment, site_url,
                )
            bug_preconditions = _make_preconditions(
                item_type, section, environment, site_url, preconditions,
            )

            # Tag the bug with reproducibility + build metadata so the
            # downstream Jira-style export and the in-app cards have
            # every ISTQB-mandatory field filled.
            labels = [item_type]
            if category:
                labels.append(f"category:{category.lower().replace(' ', '-')}")
            if testing_types:
                labels.extend(f"type:{t.lower().replace(' ', '-')}" for t in testing_types)

            bug = {
                "id": "",  # assigned later with generate_bug_id
                "title": bug_summary,
                "severity": _severity_from_priority(priority),
                "priority": priority,
                "status": "Open",
                "environment": environment,
                "preconditions": bug_preconditions,
                "steps_to_reproduce": bug_steps,
                "actual_result": bug_actual,
                "expected_result": bug_expected,
                # ISTQB-mandatory metadata
                "frequency": "Always",
                "affects_version": "",        # filled by routes/execution.py
                "found_in_build": _make_found_in_build(environment, now),
                "attachments": [],
                "linked_item_id": item_id,
                "linked_item_type": item_type,
                "reporter": tester_name,
                "assignee": "",
                "created_at": now,
                "component": section,
                "labels": labels,
                "comment": comment if status == "Blocked" else "",
            }
            bugs.append(bug)
            result_entry["bug_id"] = f"__pending_{bug_counter}"

        results.append(result_entry)

    total = passed + failed + blocked
    stats = {
        "total": total,
        "passed": passed,
        "failed": failed,
        "blocked": blocked,
        "pass_rate": round(passed / total * 100, 1) if total else 0,
        "sources": sources,
        "site_url": site_url,
    }

    return {"results": results, "bugs": bugs, "stats": stats}
