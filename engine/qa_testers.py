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


def _make_bug_summary(tc_summary: str) -> str:
    """Convert test case summary to bug summary (passive voice, negated).

    Example:
      TC:  'Verify that login is completed successfully with valid credentials'
      Bug: 'Login is not completed successfully with valid credentials'
    """
    s = tc_summary.strip()
    # Strip "Verify that " prefix
    prefix = "Verify that "
    if s.startswith(prefix):
        s = s[len(prefix):]
    elif s.lower().startswith(prefix.lower()):
        s = s[len(prefix):]

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
            exp = f"The feature should work correctly: {exp}"
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


def execute_items(items: list, item_type: str, tester_id: str,
                  environment: str, testing_types: list[str],
                  selected_ids: list[str] | None = None,
                  site_url: str = "") -> dict:
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

        # ── Decide status: real check or simulation ───────────
        status = ""
        comment = ""

        if runner and summary:
            check_key = runner.match_item(summary)
            if check_key and check_key in real_checks:
                cr = real_checks[check_key]
                status = cr.status                    # "Passed" or "Failed"
                comment = cr.actual_result             # real description

        if not status:
            # Fallback: deterministic simulation
            status = _compute_status(summary, category, priority, tester_id, environment)
            comment = _generate_comment(status, summary, tester_name)

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
        }

        # For Failed/Blocked: tester creates a bug report
        if status in ("Failed", "Blocked"):
            bug_counter += 1
            bug_summary = _make_bug_summary(summary)
            bug_expected = _make_bug_expected(summary, expected)
            # Use real actual_result when available
            bug_actual = comment if comment else _make_bug_actual(bug_summary)

            bug = {
                "id": "",  # assigned later with generate_bug_id
                "title": bug_summary,
                "severity": _severity_from_priority(priority),
                "priority": priority,
                "status": "Open",
                "environment": environment,
                "preconditions": preconditions,
                "steps_to_reproduce": steps,
                "actual_result": bug_actual,
                "expected_result": bug_expected,
                "attachments": [],
                "linked_item_id": item_id,
                "linked_item_type": item_type,
                "reporter": tester_name,
                "assignee": "",
                "created_at": now,
                "component": section,
                "labels": [],
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
    }

    return {"results": results, "bugs": bugs, "stats": stats}
