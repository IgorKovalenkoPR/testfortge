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


# ─────────────────────────────────────────────────────────────────
# Versioned platform map (Bug-fix #5)
# ─────────────────────────────────────────────────────────────────
# WEB_PLATFORMS above is the flat list shipped before; many call-sites
# (history, exports, backfilled DB rows) still expect it. The dict
# below adds release-level granularity used by the new OS-version
# selector on the Test Execution form. Order matters — it determines
# the order that options appear inside each optgroup.
WEB_PLATFORMS_VERSIONED: dict[str, list[str]] = {
    "Windows": ["Windows 11", "Windows 10"],
    "macOS":   ["macOS Sequoia (15)", "macOS Sonoma (14)",
                "macOS Ventura (13)", "macOS Monterey (12)"],
    "Linux":   ["Ubuntu 24.04", "Ubuntu 22.04", "Fedora 40", "Debian 12"],
    "ChromeOS":["ChromeOS"],
}

MOBILE_OS_VERSIONS: dict[str, list[str]] = {
    "iOS":     ["iOS 18", "iOS 17", "iOS 16"],
    "Android": ["Android 15", "Android 14", "Android 13", "Android 12"],
}

# Flat list of "OS — version" strings the form selector renders. We
# emit the optgroup label inside the option so the route can resolve
# both the family and the exact version from the single posted value.
WEB_OS_VERSIONS_FLAT: list[tuple[str, str]] = [
    (family, version)
    for family, versions in WEB_PLATFORMS_VERSIONED.items()
    for version in versions
]


# ─────────────────────────────────────────────────────────────────
# Engine × Platform × Browser matrix (Feature #6)
# ─────────────────────────────────────────────────────────────────
# Render free tier runs Linux only — we cannot summon a real macOS
# host. What we CAN do reproducibly is drive the right Playwright
# engine (Chromium / Firefox / WebKit) and pin a real-world UA +
# viewport for the chosen (OS version, browser) pair, so any rendering
# differences that surface in WebKit-only or Firefox-only bugs DO get
# caught, and the produced screenshots/videos honour the OS look the
# tester picked. Matrix below is consulted by AutomationRunner.
#
# Fields:
#   engine    — Playwright engine name; valid: "chromium" | "firefox" | "webkit"
#   ua        — User-Agent string sent on every request, matches the
#               browser+OS the tester said they wanted
#   viewport  — (width, height) in CSS pixels. Picked to match the
#               typical default for that OS+browser combo.
#
# Lookup precedence (most → least specific):
#   1. exact (os_version, browser) match
#   2. (os_family, browser)   match — e.g. ("Windows", "Chrome") falls
#      back from ("Windows 10", "Chrome")
#   3. ("*", browser)         — engine-only fallback by browser
#   4. PLATFORM_BROWSER_DEFAULT — chromium / generic UA / 1280x800
#
# UAs are 2025-era release strings. Where Edge/Brave share the
# Chromium engine, we still ship distinct UAs so any UA-sniffing the
# site under test does behaves correctly.

PLATFORM_BROWSER_DEFAULT: dict = {
    "engine": "chromium",
    "ua": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"),
    "viewport": (1280, 800),
}

PLATFORM_BROWSER_MATRIX: dict[tuple[str, str], dict] = {
    # ── Windows ──────────────────────────────────────────────
    ("Windows 11", "Chrome"): {
        "engine": "chromium",
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"),
        "viewport": (1920, 1080),
    },
    ("Windows 11", "Microsoft Edge"): {
        "engine": "chromium",
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0"),
        "viewport": (1920, 1080),
    },
    ("Windows 11", "Firefox"): {
        "engine": "firefox",
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) "
               "Gecko/20100101 Firefox/130.0"),
        "viewport": (1920, 1080),
    },
    ("Windows 11", "Opera"): {
        "engine": "chromium",
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36 OPR/123.0.0.0"),
        "viewport": (1920, 1080),
    },
    ("Windows 11", "Brave"): {
        "engine": "chromium",
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Brave/138"),
        "viewport": (1920, 1080),
    },
    ("Windows 10", "Chrome"): {
        "engine": "chromium",
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"),
        "viewport": (1920, 1080),
    },
    ("Windows 10", "Microsoft Edge"): {
        "engine": "chromium",
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0"),
        "viewport": (1920, 1080),
    },
    ("Windows 10", "Firefox"): {
        "engine": "firefox",
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) "
               "Gecko/20100101 Firefox/130.0"),
        "viewport": (1920, 1080),
    },
    # ── macOS ────────────────────────────────────────────────
    ("macOS Sequoia (15)", "Safari"): {
        "engine": "webkit",
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"),
        "viewport": (1680, 1050),
    },
    ("macOS Sonoma (14)", "Safari"): {
        "engine": "webkit",
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15"),
        "viewport": (1680, 1050),
    },
    ("macOS Ventura (13)", "Safari"): {
        "engine": "webkit",
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15"),
        "viewport": (1680, 1050),
    },
    ("macOS Monterey (12)", "Safari"): {
        "engine": "webkit",
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.6 Safari/605.1.15"),
        "viewport": (1680, 1050),
    },
    ("macOS Sequoia (15)", "Chrome"): {
        "engine": "chromium",
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"),
        "viewport": (1680, 1050),
    },
    ("macOS Sonoma (14)", "Chrome"): {
        "engine": "chromium",
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"),
        "viewport": (1680, 1050),
    },
    ("macOS Sonoma (14)", "Firefox"): {
        "engine": "firefox",
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:130.0) "
               "Gecko/20100101 Firefox/130.0"),
        "viewport": (1680, 1050),
    },
    # ── Linux ────────────────────────────────────────────────
    ("Ubuntu 24.04", "Chrome"): {
        "engine": "chromium",
        "ua": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"),
        "viewport": (1366, 768),
    },
    ("Ubuntu 24.04", "Firefox"): {
        "engine": "firefox",
        "ua": ("Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:130.0) "
               "Gecko/20100101 Firefox/130.0"),
        "viewport": (1366, 768),
    },
    ("Ubuntu 22.04", "Chrome"): {
        "engine": "chromium",
        "ua": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"),
        "viewport": (1366, 768),
    },
    ("Ubuntu 22.04", "Firefox"): {
        "engine": "firefox",
        "ua": ("Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:130.0) "
               "Gecko/20100101 Firefox/130.0"),
        "viewport": (1366, 768),
    },
    # ── ChromeOS ─────────────────────────────────────────────
    ("ChromeOS", "Chrome"): {
        "engine": "chromium",
        "ua": ("Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"),
        "viewport": (1366, 768),
    },
    # ── Mobile (Mobile Web env) ──────────────────────────────
    ("iOS 18", "Safari (iOS)"): {
        "engine": "webkit",
        "ua": ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) "
               "Version/18.0 Mobile/15E148 Safari/604.1"),
        "viewport": (390, 844),
    },
    ("iOS 17", "Safari (iOS)"): {
        "engine": "webkit",
        "ua": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) "
               "Version/17.4 Mobile/15E148 Safari/604.1"),
        "viewport": (390, 844),
    },
    ("Android 15", "Chrome"): {
        "engine": "chromium",
        "ua": ("Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/138.0.0.0 Mobile Safari/537.36"),
        "viewport": (412, 915),
    },
    ("Android 14", "Chrome"): {
        "engine": "chromium",
        "ua": ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/138.0.0.0 Mobile Safari/537.36"),
        "viewport": (412, 915),
    },
}

# Family-level fallbacks (used when an exact (version, browser) row is
# missing, so newly-added OS versions still resolve to a sensible UA).
PLATFORM_BROWSER_FAMILY: dict[tuple[str, str], dict] = {
    ("Windows", "Chrome"):           PLATFORM_BROWSER_MATRIX[("Windows 11", "Chrome")],
    ("Windows", "Microsoft Edge"):   PLATFORM_BROWSER_MATRIX[("Windows 11", "Microsoft Edge")],
    ("Windows", "Firefox"):          PLATFORM_BROWSER_MATRIX[("Windows 11", "Firefox")],
    ("Windows", "Opera"):            PLATFORM_BROWSER_MATRIX[("Windows 11", "Opera")],
    ("Windows", "Brave"):            PLATFORM_BROWSER_MATRIX[("Windows 11", "Brave")],
    ("Windows", "Safari"):           PLATFORM_BROWSER_MATRIX[("Windows 11", "Chrome")],
    ("macOS",   "Safari"):           PLATFORM_BROWSER_MATRIX[("macOS Sonoma (14)", "Safari")],
    ("macOS",   "Chrome"):           PLATFORM_BROWSER_MATRIX[("macOS Sonoma (14)", "Chrome")],
    ("macOS",   "Firefox"):          PLATFORM_BROWSER_MATRIX[("macOS Sonoma (14)", "Firefox")],
    ("macOS",   "Microsoft Edge"):   PLATFORM_BROWSER_MATRIX[("macOS Sonoma (14)", "Chrome")],
    ("Linux",   "Chrome"):           PLATFORM_BROWSER_MATRIX[("Ubuntu 24.04", "Chrome")],
    ("Linux",   "Firefox"):          PLATFORM_BROWSER_MATRIX[("Ubuntu 24.04", "Firefox")],
    ("ChromeOS", "Chrome"):          PLATFORM_BROWSER_MATRIX[("ChromeOS", "Chrome")],
    ("iOS",     "Safari (iOS)"):     PLATFORM_BROWSER_MATRIX[("iOS 18", "Safari (iOS)")],
    ("iOS",     "Chrome"):           PLATFORM_BROWSER_MATRIX[("iOS 18", "Safari (iOS)")],
    ("Android", "Chrome"):           PLATFORM_BROWSER_MATRIX[("Android 15", "Chrome")],
    ("Android", "Microsoft Edge"):   PLATFORM_BROWSER_MATRIX[("Android 15", "Chrome")],
}


def resolve_platform_browser(os_version: str, browser: str) -> dict:
    """Look up engine / UA / viewport for the chosen (os_version, browser).

    Falls back from the exact match to the OS family, then to the
    chromium default. Always returns a dict — never raises.
    """
    if not os_version and not browser:
        return dict(PLATFORM_BROWSER_DEFAULT)
    os_version = (os_version or "").strip()
    browser = (browser or "").strip() or "Chrome"

    # 1) exact
    exact = PLATFORM_BROWSER_MATRIX.get((os_version, browser))
    if exact:
        return dict(exact)

    # 2) family
    family = ""
    for fam, versions in WEB_PLATFORMS_VERSIONED.items():
        if os_version in versions:
            family = fam
            break
    if not family:
        for fam, versions in MOBILE_OS_VERSIONS.items():
            if os_version in versions:
                family = fam
                break
    if family:
        fam_hit = PLATFORM_BROWSER_FAMILY.get((family, browser))
        if fam_hit:
            return dict(fam_hit)

    # 2b) Prefix family — handles brand-new OS versions that aren't yet
    # in WEB_PLATFORMS_VERSIONED but still announce a known family in
    # their string ("Windows 99" -> "Windows", "macOS Tahoe" -> "macOS").
    # Without this, every fresh OS release looked like a Linux Chrome
    # session because it landed in PLATFORM_BROWSER_DEFAULT.
    lowered = os_version.lower()
    for fam in list(WEB_PLATFORMS_VERSIONED) + list(MOBILE_OS_VERSIONS):
        if lowered.startswith(fam.lower()):
            fam_hit = PLATFORM_BROWSER_FAMILY.get((fam, browser))
            if fam_hit:
                return dict(fam_hit)
            break

    # 3) plain default
    return dict(PLATFORM_BROWSER_DEFAULT)


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


# PR-C′: archetype-driven passive-voice bug summary builder.
#
# Previous implementation negated TC summaries by inserting "not" via
# regex (e.g. ``rejects`` → ``does not reject``) and falling back to
# ``" — does not work as expected"`` when no pattern matched. That
# produced titles like:
#   • "Privacy policy renders its primary content — does not work as
#     expected" (positive-voice headline with a tacked-on suffix)
#   • "Every public page has not a unique, non-empty <title>"
#     (broken grammar from naive ``has`` → ``has not``)
#   • "The page meets basic accessibility standards" (no negation at
#     all because ``meets`` wasn't in the pattern list)
#
# The QA style guide mandates passive voice with an "after/while"
# trigger clause, e.g.:
#     "The Contact US form is not submitted after clicking the Submit
#      button."
#
# Each archetype below detects a TC-summary shape and emits the
# matching passive-voice headline with an after-/while-clause where
# the trigger is unambiguous. The order matters — more specific
# patterns must come before generic ones.

def _strip_phrase(s: str) -> str:
    """Strip whitespace and trailing punctuation; collapse internal
    runs of whitespace into single spaces. Used to clean substrings
    captured by archetype regexes before they enter the template."""
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip(".,;:!?")


def _cap(s: str) -> str:
    """Uppercase the first character without touching the rest. Avoids
    ``str.capitalize()`` which lowercases the tail and would mangle
    acronyms / camelCase words inside captured TC phrases."""
    if not s:
        return s
    return s[0].upper() + s[1:]


# Singular nouns that end in ``s`` and would otherwise fool the
# plural-detection heuristic below. Lower-cased; matched on the last
# space- or hyphen-separated token of the captured object phrase.
_SINGULAR_S_NOUNS = frozenset({
    "address", "access", "process", "status", "this", "his", "glass",
    "press", "class", "boss", "moss", "loss", "miss", "kiss", "less",
    "mass", "bus", "yes", "news", "series", "species", "analysis",
    "basis", "thesis", "diagnosis", "css", "rss", "js", "cms",
})


def _is_plural_object_phrase(s: str) -> bool:
    """Best-effort plural detector for the captured-object slot in the
    ``displays``/``shows`` archetypes. Picks the last whitespace- or
    hyphen-separated token, lowercases it, and treats it as plural
    when it ends in ``s`` and is not on the
    :data:`_SINGULAR_S_NOUNS` deny-list. Conservative — when in doubt
    we return ``False`` so the generated copy reads "is not displayed"
    rather than the wrong "are not displayed".
    """
    if not s:
        return False
    last = re.split(r"[\s\-]+", s.strip().rstrip(".,;:!?"))[-1].lower()
    if not last:
        return False
    if last in _SINGULAR_S_NOUNS:
        return False
    if last.endswith("ss"):
        return False  # "class", "press", "address" tail without lookup
    return last.endswith("s")


_TITLE_ARCHETYPES: list[tuple[re.Pattern[str], "Callable[[re.Match[str]], str]"]] = [
    # Form rejects empty/malformed input — form-validation TC.
    # Captures: (1) form-noun, (2) what's being rejected, (3) "input"/"values".
    (re.compile(
        r"^(?:the\s+)?(.+?)\s+rejects?\s+(.+?)\s+(input|values?)\s*$",
        re.I),
     lambda m: (
         f"{_cap(_strip_phrase(m.group(2)))} {m.group(3).lower()} "
         f"is not rejected after submitting "
         f"the {_strip_phrase(m.group(1))}"
     )),
    # Form/feature submits — form-submission TC.
    (re.compile(r"^(?:the\s+)?(.+?)\s+submits?\b.*$", re.I),
     lambda m: (
         f"The {_strip_phrase(m.group(1))} is not submitted "
         f"after clicking the submit control"
     )),
    # Page/feature renders content — page-render TC.
    (re.compile(r"^(?:the\s+)?(.+?)\s+renders?\s+(.+?)\s*$", re.I),
     lambda m: (
         f"{_cap(_strip_phrase(m.group(2)))} is not rendered "
         f"on {_strip_phrase(m.group(1))} after page load"
     )),
    # X returns results — search/feature-returns TC. ``results`` is
    # plural so we use ``are not returned`` to keep grammar correct.
    (re.compile(r"^(?:the\s+)?(.+?)\s+returns?\s+(.+?)\s*$", re.I),
     lambda m: (
         f"{_cap(_strip_phrase(m.group(2)))} are not returned "
         f"by the {_strip_phrase(m.group(1))} "
         f"after submitting the query"
     )),
    # X meets Y standards — compliance TC. ``standards`` is plural →
    # ``are not met``.
    (re.compile(r"^(?:the\s+)?(.+?)\s+meets?\s+(.+?)\s*$", re.I),
     lambda m: (
         f"{_cap(_strip_phrase(m.group(2)))} are not met "
         f"by the {_strip_phrase(m.group(1))}"
     )),
    # X redirects to Y — redirect TC. Run BEFORE the generic
    # has/displays/loads matchers because ``redirects to`` could
    # otherwise mis-parse as ``redirects` action verb without target.
    (re.compile(r"^(?:the\s+)?(.+?)\s+redirects?\s+to\s+(.+?)\s*$", re.I),
     lambda m: (
         f"Redirect to {_strip_phrase(m.group(2))} is not triggered "
         f"by the {_strip_phrase(m.group(1))}"
     )),
    # X validates Y — validation TC.
    (re.compile(r"^(?:the\s+)?(.+?)\s+validates?\s+(.+?)\s*$", re.I),
     lambda m: (
         f"{_cap(_strip_phrase(m.group(2)))} validation is not "
         f"enforced by the {_strip_phrase(m.group(1))}"
     )),
    # X accepts Y — input-accept TC.
    (re.compile(r"^(?:the\s+)?(.+?)\s+accepts?\s+(.+?)\s*$", re.I),
     lambda m: (
         f"{_cap(_strip_phrase(m.group(2)))} is not accepted "
         f"by the {_strip_phrase(m.group(1))}"
     )),
    # X allows Y — capability TC.
    (re.compile(r"^(?:the\s+)?(.+?)\s+allows?\s+(.+?)\s*$", re.I),
     lambda m: (
         f"{_cap(_strip_phrase(m.group(2)))} is not allowed "
         f"by the {_strip_phrase(m.group(1))}"
     )),
    # X displays/shows Y — visibility TC. ``displays`` commonly takes
    # plural objects (links, items, results) so we detect the case via
    # :func:`_is_plural_object_phrase` and pick ``is``/``are``
    # accordingly — otherwise "All primary links is not displayed"
    # leaks broken subject-verb agreement.
    (re.compile(r"^(?:the\s+)?(.+?)\s+(?:displays?|shows?)\s+(.+?)\s*$", re.I),
     lambda m: (
         f"{_cap(_strip_phrase(m.group(2)))} "
         f"{'are' if _is_plural_object_phrase(m.group(2)) else 'is'} "
         f"not displayed by the {_strip_phrase(m.group(1))}"
     )),
    # X prevents Y — guard TC.
    (re.compile(r"^(?:the\s+)?(.+?)\s+prevents?\s+(.+?)\s*$", re.I),
     lambda m: (
         f"{_cap(_strip_phrase(m.group(2)))} is not prevented "
         f"by the {_strip_phrase(m.group(1))}"
     )),
    # every/X has Y — uniqueness/presence TC. The ``every`` prefix
    # (when present) is preserved on the subject so the headline still
    # reads "is missing from every public page" instead of dropping the
    # cardinality.
    (re.compile(r"^(every\s+|each\s+)?(?:the\s+)?(.+?)\s+has\s+"
                 r"(?:a\s+|an\s+)?(.+?)\s*$", re.I),
     lambda m: (
         f"{_cap(_strip_phrase(m.group(3)))} is missing from "
         f"{(m.group(1) or '').strip()} "
         f"{_strip_phrase(m.group(2))}".strip()
     )),
    # X opens — open TC. Split from ``loads`` so the headline uses
    # ``is not opened`` rather than the wrong ``is not loaded`` for
    # menus / modals / drawers that the operator clicks to open.
    (re.compile(r"^(?:the\s+)?(.+?)\s+opens?\b.*$", re.I),
     lambda m: (
         f"The {_strip_phrase(m.group(1))} is not opened "
         f"after the trigger action"
     )),
    # X loads — load TC. The trailing optional group catches
    # "loads correctly" / "loads quickly" suffixes without leaking
    # them into the headline.
    (re.compile(r"^(?:the\s+)?(.+?)\s+loads?\b.*$", re.I),
     lambda m: (
         f"The {_strip_phrase(m.group(1))} is not loaded "
         f"after page navigation"
     )),
    # X works (correctly) — generic behavior TC.
    (re.compile(r"^(?:the\s+)?(.+?)\s+works?\b.*$", re.I),
     lambda m: (
         f"The {_strip_phrase(m.group(1))} does not work "
         f"as expected"
     )),
    # Generic "X is Y" — state TC. Last-resort archetype that always
    # produces a passive-voice headline so we don't fall through to the
    # banned " — does not work as expected" suffix.
    (re.compile(r"^(?:the\s+)?(.+?)\s+is\s+(.+?)\s*$", re.I),
     lambda m: (
         f"The {_strip_phrase(m.group(1))} is not "
         f"{_strip_phrase(m.group(2))} as expected"
     )),
    # Generic "X are Y" — plural state TC.
    (re.compile(r"^(.+?)\s+are\s+(.+?)\s*$", re.I),
     lambda m: (
         f"{_cap(_strip_phrase(m.group(1)))} are not "
         f"{_strip_phrase(m.group(2))} as expected"
     )),
]


def _make_bug_summary(tc_summary: str) -> str:
    """Convert a TC summary into a passive-voice bug summary with an
    "after/while" trigger clause.

    Strategy: detect the TC archetype (form-reject, page-render,
    search-returns, redirect, validation, …) and emit the matching
    passive-voice headline that states the failure observably. Each
    archetype owns its grammar (singular/plural agreement, trigger
    clause) — no generic "insert 'not' after the first verb" trick
    that produced "has not a unique" or "renders … does not work as
    expected" in earlier revisions.

    Falls back to a safe generic template ("The expected outcome is
    not observed for: <subject>") so we never emit grammatically
    broken titles.

    Examples (drawn from real failure modes the simulator path
    produced on the ART project, see PR-C′ commit body):

      TC:  "Verify that the Contact US form is submitted with valid input"
      Bug: "The Contact US form is not submitted after clicking the submit control"

      TC:  "Verify that Privacy policy renders its primary content"
      Bug: "Its primary content is not rendered on Privacy policy after page load"

      TC:  "Verify that the on-site search returns results for a topical query"
      Bug: "Results for a topical query are not returned by the on-site search after submitting the query"

      TC:  "Verify that every public page has a unique, non-empty <title>"
      Bug: "Unique, non-empty <title> is missing from every public page"

      TC:  "Verify that the page meets basic accessibility standards"
      Bug: "Basic accessibility standards are not met by the page"

    Special-case: if the TC already negates ("has no console errors",
    "without crashes"), invert the existing negation via
    :func:`_negative_clause_bug_summary` rather than stacking another
    "not" on top of it.
    """
    s = tc_summary.strip().rstrip(".")
    # Strip "Verify that" prefix (case-insensitive). The trailing
    # ``\s*`` accepts zero whitespace so a bare ``"Verify that"``
    # input collapses to the empty-input sentinel below rather than
    # being treated as a real TC summary.
    s = re.sub(r"^Verify that\b\s*", "", s, flags=re.I).strip()
    if not s:
        return "Expected behaviour is not observed"

    # Existing-negation path — TCs like "has no console errors" need
    # the existing ``no`` inverted rather than mechanical negation.
    if _has_existing_negation(s):
        inverted = _negative_clause_bug_summary(s)
        if inverted:
            return _cap(_strip_phrase(inverted))

    # Archetype walk — first match wins.
    for pat, fn in _TITLE_ARCHETYPES:
        m = pat.match(s)
        if m:
            try:
                title = fn(m)
            except Exception:
                continue
            if title:
                return _cap(_strip_phrase(title))

    # Generic safe fallback — passive-voice, no banned suffix.
    return f"The expected outcome is not observed for: {s}"


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
