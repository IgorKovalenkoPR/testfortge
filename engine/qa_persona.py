"""
TestFortge — Senior QA Engineer Persona

ISTQB-certified Senior QA Engineer with extensive experience in:
  - Test Plans, Test Cases, High-Level and Low-Level Checklists
  - Web / Mobile / API testing
  - ISTQB test design techniques: Equivalence Partitioning, Boundary Value
    Analysis, Decision Tables, State Transition Testing, Use Case Testing
  - TestFort documentation format

This module provides the domain knowledge that drives professional-grade
test documentation generation.  It is always invoked for every generation
request (test cases AND checklists).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ═══════════════════════════════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════════════════════════════

@dataclass
class CheckItem:
    """A single low-level checklist item."""
    objective: str
    category: str          # Positive / Negative / Edge Case / Security / Performance / Accessibility
    priority: str = "Medium"  # High / Medium / Low
    section: str = ""
    testing_type: str = ""  # see TCTemplate.testing_type


@dataclass
class TCTemplate:
    """A single test-case template."""
    summary: str
    preconditions: str
    steps: list[str]
    test_data: str
    expected_result: str
    category: str          # Positive / Negative / Edge Case / Security
    priority: str = "Medium"
    section: str = ""
    comment: str = ""
    # ``testing_type`` tags a case for prompt-driven filtering. Empty
    # by default so ``_detect_testing_type`` (in testcase_generator)
    # can tag the case by heuristic (section / summary). Generators
    # that already know the right testing type — SEO / Usability /
    # Localization — set the field explicitly and bypass the heuristic.
    testing_type: str = ""


@dataclass
class AnalysisResult:
    """What the QA persona understood from the user's input."""
    areas: list[str]                # detected testing areas
    url: str | None = None          # URL under test (if any)
    url_domain: str = ""
    url_path: str = ""
    features: list[str] = field(default_factory=list)  # detected feature keywords
    level: str = "low"              # "high" or "low" (default low-level)
    raw_requirements: list[str] = field(default_factory=list)
    browser_findings: list[dict] = field(default_factory=list)  # real browser test results
    flows: list[str] = field(default_factory=list)   # detected named flows (e.g. "checkout_flow")
    # Per-page crawler data (title / h1 / headings / nav / buttons / forms)
    # — drives site-specific test-case generation. One dict per crawled URL.
    site_pages: list[dict] = field(default_factory=list)
    site_type: str = "generic"  # wordpress / spa / ecommerce / news / dashboard / landing / static


# ═══════════════════════════════════════════════════════════════════
# 1. Input Analysis
# ═══════════════════════════════════════════════════════════════════

_URL_RE = re.compile(
    r"(https?://)?([a-z0-9][a-z0-9\-]*\.[a-z0-9\-]*\.[a-z]{2,}|[a-z0-9\-]+\.[a-z]{2,})(/[^\s]*)?",
    re.IGNORECASE,
)

# Instruction markers — these lines are commands TO the tool, not requirements
# for the system under test.
_INSTRUCTION_PATTERNS_EN = [
    r"^(please\s+)?(create|generate|re-?generate|write|make|build|prepare|design|draft|develop|produce)\s+",
    r"^(should|must|need\s+to|have\s+to)\s+(be\s+)?(covered|included|tested|checked|verified)",
    r"^(include|add|cover|ensure|focus\s+on)\s+",
    r"^(positive|negative|edge|boundary|security|performance)\s+(cases?|scenarios?|tests?|checks?)\s+(should|must|need)",
    # Meta-instructions: guidance about HOW to generate, not WHAT to test
    r"^(pay\s+attention|note\s+that|keep\s+in\s+mind|make\s+sure|remember\s+that|consider\s+that)\s+",
    r"^(each|every|all)\s+(acceptance|test|user\s+stor|scenario|requirement).*\bshould\b",
    r"^re-?generate\b",
]
_INSTRUCTION_PATTERNS_UA = [
    r"^(створи|згенеруй|перегенеруй|напиши|зроби|підготуй|розроби|побудуй)\s+",
    r"^(мають?|повинн[іа]|потрібно|необхідно)\s+(бути\s+)?(покрит|включен|перевірен|протестован)",
    r"^(додай|включи|забезпеч|покрий|перевір)\s+",
    r"^(позитивні|негативні|граничні|edge|boundary)\s+(сценарії|випадки|тести|перевірки)\s+(мають|повинні|потрібно)",
    # Meta-instructions UA
    r"^(зверни\s+увагу|врахуй|пам.ятай|переконайся)\s+",
    r"^перегенеруй\b",
]

_INSTRUCTION_RE = [re.compile(p, re.IGNORECASE) for p in
                   _INSTRUCTION_PATTERNS_EN + _INSTRUCTION_PATTERNS_UA]

# Area keywords for detection
_AREA_KEYWORDS = {
    "auth":        ["login", "log in", "sign in", "register", "sign up", "password",
                    "authentication", "logout", "session", "credential", "2fa",
                    "вхід", "увійти", "реєстрація", "пароль", "авторизація"],
    "search":      ["search", "find", "filter", "sort", "query", "lookup",
                    "пошук", "фільтр", "сортування"],
    "forms":       ["form", "input", "field", "submit", "validation",
                    "форма", "поле", "введення", "валідація"],
    "crud_create": ["create", "add", "new", "insert", "створити", "додати"],
    "crud_read":   ["view", "display", "show", "list", "dashboard", "report",
                    "перегляд", "відображення", "список"],
    "crud_update": ["edit", "update", "modify", "change", "save",
                    "редагувати", "оновити", "змінити"],
    "crud_delete": ["delete", "remove", "cancel", "deactivate",
                    "видалити", "скасувати"],
    "payment":     ["checkout", "add to cart", "shopping cart", "buy now",
                    "purchase", "cart", "кошик", "покупка", "оформлення замовлення"],
    "navigation":  ["navigation", "menu", "link", "breadcrumb", "routing",
                    "навігація", "меню", "посилання"],
    "upload":      ["upload", "import", "attach", "file upload",
                    "завантажити", "імпорт", "вкладення"],
    "export":      ["export", "download", "csv", "pdf", "xlsx",
                    "експорт", "завантаження"],
    "notification": ["notification", "alert", "email", "sms", "push",
                     "сповіщення", "повідомлення"],
    "api":         ["api", "endpoint", "rest", "graphql", "webhook"],
    "responsive":  ["responsive", "mobile", "tablet", "adaptive",
                    "адаптив", "мобільн"],
    "performance": ["performance", "load", "speed", "latency",
                    "продуктивність", "швидкість"],
    "security":    ["security", "xss", "injection", "csrf", "encrypt",
                    "безпека", "захист", "шифрування"],
    "accessibility": ["accessibility", "a11y", "wcag", "screen reader",
                      "доступність"],
}


def is_instruction(text: str) -> bool:
    """Return True if the line is a command to the tool, not a requirement."""
    stripped = text.strip()
    for pat in _INSTRUCTION_RE:
        if pat.search(stripped):
            return True
    return False


# ═══════════════════════════════════════════════════════════════════
# 1b. Flow intent detection
# ═══════════════════════════════════════════════════════════════════
#
# Named business flows (end-to-end journeys) that override ordinary area
# detection: when the user explicitly asks for a flow, the persona must
# emit the full phase-by-phase coverage from the playbook regardless of
# what the crawler sees on the landing page.

def detect_flows(text: str) -> list[str]:
    """Return the list of named flow keys triggered by *text*.

    The playbooks live in :mod:`engine.knowledge_base` under
    ``FLOW_PLAYBOOKS``. Each playbook carries its own trigger phrases in
    English and Ukrainian.
    """
    try:
        from .knowledge_base import FLOW_PLAYBOOKS
    except Exception:  # pragma: no cover - import guard
        return []
    lower = (text or "").lower()
    hits: list[str] = []
    for key, pb in FLOW_PLAYBOOKS.items():
        for trg in pb.get("triggers", []):
            if trg.lower() in lower:
                hits.append(key)
                break
    return hits


def analyze_input(requirements: list[dict],
                  custom_prompt: str = "") -> AnalysisResult:
    """Analyze structured requirements to determine testing scope.

    When a URL is detected, the site crawler fetches and analyzes the
    actual website to discover features (auth, search, forms, payment,
    navigation) and enrich the analysis accordingly.

    Parameters
    ----------
    requirements : list[dict]
        Each dict has at least ``text`` key.
    custom_prompt : str
        Additional user instructions.

    Returns
    -------
    AnalysisResult
    """
    result = AnalysisResult(areas=[], raw_requirements=[])
    all_text = " ".join(r.get("text", "") for r in requirements)
    all_text_lower = all_text.lower()
    combined = all_text_lower + " " + custom_prompt.lower()

    # Detect URL
    url_match = _URL_RE.search(all_text)
    if url_match:
        full_url = url_match.group(0)
        result.url = full_url
        domain_match = re.search(r"(?:https?://)?([^/\s]+)", full_url)
        if domain_match:
            result.url_domain = domain_match.group(1)
        path_match = re.search(r"(?:https?://)?[^/\s]+(/[^\s]*)", full_url)
        if path_match:
            result.url_path = path_match.group(1).strip("/").replace("/", " > ").replace("-", " ").replace("_", " ")

    # Detect level
    if any(kw in combined for kw in ["low level", "low-level", "детальн", "низькорівн",
                                      "детализирован", "подробн", "granular", "atomic"]):
        result.level = "low"
    elif any(kw in combined for kw in ["high level", "high-level", "високорівн",
                                        "загальн", "summary", "overview"]):
        result.level = "high"

    # Detect areas from text keywords
    detected_areas: set[str] = set()
    for area, keywords in _AREA_KEYWORDS.items():
        for kw in keywords:
            if kw in combined:
                detected_areas.add(area)
                break

    # ── Crawl URL to discover real features ───────────────────────
    # When the crawler successfully analyzes a URL, its structural findings
    # (auth, search, forms, payment) override keyword-based guesses for
    # those areas.  This prevents false positives — e.g. "pricing" text on
    # an informational site should NOT add payment/checkout test cases.
    site_analysis = None
    if result.url:
        try:
            from .site_crawler import crawl_site
            site_analysis = crawl_site(result.url)
            result.features = list(site_analysis.features_detected)
            result.site_type = site_analysis.site_type
            # Capture per-page structural data so the TC generator can emit
            # site-specific test cases that reference the actual page titles,
            # H1s, headings, nav items, buttons and forms — not just generic
            # boilerplate. Trim long fields so we don't blow up exports.
            for p in site_analysis.pages:
                if p.error:  # 404 / unreachable — skip
                    continue
                result.site_pages.append({
                    "url": p.url,
                    "title": (p.title or "")[:120],
                    "h1": (p.h1 or "")[:120],
                    "headings": [h[:80] for h in (p.headings or [])[:6]],
                    "nav_links": [n[:60] for n in (p.nav_links or [])[:8]],
                    "buttons": [b[:60] for b in (p.buttons or [])[:6]],
                    "forms": p.forms or [],
                    "has_video": bool(p.has_video),
                    "images_count": int(p.images_count or 0),
                    "links_internal_count": len(p.links_internal or []),
                })

            # Features the crawler can authoritatively confirm or deny.
            # Remove keyword-guessed areas that the crawler did NOT find.
            _CRAWLER_AUTHORITATIVE = {"auth", "search", "forms", "payment"}
            for area in _CRAWLER_AUTHORITATIVE:
                detected_areas.discard(area)

            # Now add back only what the crawler actually found
            if site_analysis.has_auth:
                detected_areas.add("auth")
            if site_analysis.has_search:
                detected_areas.add("search")
            if site_analysis.has_forms:
                detected_areas.add("forms")
            if site_analysis.has_payment:
                detected_areas.add("payment")

            # Detect navigation from crawled pages
            if site_analysis.nav_items:
                detected_areas.add("navigation")

            # Add crawled page info as synthetic requirements
            for page in site_analysis.pages:
                parts = []
                if page.title:
                    parts.append(f"Page: {page.title}")
                if page.h1:
                    parts.append(f"H1: {page.h1}")
                if page.forms:
                    for form in page.forms:
                        fields_desc = ", ".join(
                            f.get("name") or f.get("placeholder") or f.get("type", "")
                            for f in form.get("fields", [])
                            if f.get("type") not in ("hidden", "submit", "button")
                        )
                        if fields_desc:
                            parts.append(f"Form ({form.get('method', 'GET')}): {fields_desc}")
                if page.nav_links:
                    parts.append(f"Nav: {', '.join(page.nav_links[:10])}")
                if parts:
                    requirements.append({"text": " | ".join(parts)})

        except Exception:
            # Crawl failure is non-fatal — fall back to keyword-based analysis
            pass

    # ── Run browser tests (Playwright) ───────────────────────────
    # When a URL is provided, run real browser-based checks to produce
    # findings that feed into TC/CL generation.
    if result.url:
        try:
            from .browser_tester import get_or_run as browser_get_or_run
            from dataclasses import asdict
            # Cap synchronous browser-tester work so a sync /test-cases
            # POST doesn't tie up a Render free-tier worker for >2 min.
            # ``TESTFORTGE_BROWSER_PAGES`` lets you opt-in to deeper
            # checks (e.g. on a beefier deploy or via the async endpoint).
            import os
            sync_max_pages = int(os.environ.get("TESTFORTGE_BROWSER_PAGES", "5"))
            sync_timeout_ms = int(os.environ.get("TESTFORTGE_BROWSER_TIMEOUT_MS", "5000"))
            browser_report = browser_get_or_run(
                result.url, max_pages=sync_max_pages,
                timeout_ms=sync_timeout_ms,
                site_analysis=site_analysis,
            )
            result.browser_findings = [asdict(f) for f in browser_report.findings]
        except Exception:
            pass  # Browser test failure is non-fatal

    # If URL but no specific areas detected → assume full web page testing
    if result.url and not detected_areas:
        detected_areas = {"web_general"}
    elif result.url:
        detected_areas.add("web_general")

    # If nothing detected at all, use generic
    if not detected_areas:
        detected_areas = {"web_general"}

    # ── Named flow intents (e.g. "checkout flow") ────────────────
    # These take precedence over crawler-authoritative suppression:
    # if the user explicitly asked for a flow, we must emit the full
    # playbook coverage even when the landing page doesn't expose it.
    result.flows = detect_flows(combined)
    if "checkout_flow" in result.flows:
        detected_areas.update({"payment", "auth", "forms", "navigation"})

    result.areas = sorted(detected_areas)
    result.raw_requirements = [r.get("text", "") for r in requirements]

    return result


# ═══════════════════════════════════════════════════════════════════
# 2. Checklist Knowledge Base
#    Organized by area → section → list of (objective, category, priority)
# ═══════════════════════════════════════════════════════════════════

_CL = dict[str, list[tuple[str, str, str]]]  # type alias

def _web_general_checks() -> _CL:
    return {
        "Header & Navigation": [
            ("Verify that the company logo is displayed in the Header", "Positive", "High"),
            ("Click the company logo and verify the browser navigates to the Homepage URL", "Positive", "Medium"),
            ("Verify that the main navigation menu in the Header lists every expected item", "Positive", "High"),
            ("Click each navigation link and verify the browser opens the URL that matches the link label", "Positive", "High"),
            ("Verify that the navigation item for the current page has the active visual state (highlight / underline / different background)", "Positive", "Medium"),
            ("Hover a navigation item with a submenu and verify that the submenu becomes visible with all expected items", "Positive", "Medium"),
            ("Resize the viewport to 375px and verify that the Header navigation collapses into a hamburger menu button", "Positive", "High"),
            ("Tap the hamburger menu and verify the menu opens; tap outside and verify the menu closes", "Positive", "Medium"),
            ("Verify that the browser tab title matches the page name shown in the Header", "Positive", "Medium"),
            ("Verify that the breadcrumb trail shows the exact path the user navigated (if breadcrumbs are used on this page)", "Positive", "Low"),
        ],
        "Content & Layout": [
            ("Verify that the page contains exactly one H1 element and its text matches the page topic", "Positive", "High"),
            ("Verify that body text uses the site's declared font family and is legible (no clipped characters, no overflow)", "Positive", "High"),
            ("Load the page and verify that every <img> element returns HTTP 200 (no broken icons)", "Positive", "High"),
            ("Verify that every decorative image has alt=\"\" and every content image has a descriptive alt attribute", "Positive", "Medium"),
            ("Verify that every CTA button (e.g. 'Buy', 'Sign Up', 'Submit') is visible above the fold and reacts to click", "Positive", "High"),
            ("Verify that content sections appear in the order defined by the design spec (hero → features → pricing → footer, etc.)", "Positive", "Medium"),
            ("Verify that the live page contains no 'Lorem Ipsum' or 'TBD' placeholder strings", "Negative", "Medium"),
            ("Verify that no two visible elements overlap and every control stays inside its parent container at 1280px and 375px", "Negative", "Medium"),
            ("Verify that long text (e.g. > 100 chars) in titles/cards is truncated with ellipsis and a tooltip reveals the full value", "Edge Case", "Low"),
        ],
        "Links & Media": [
            ("Click every internal link on the page and verify the destination URL is on the same origin and returns HTTP 200", "Positive", "High"),
            ("Verify that every external link carries target=\"_blank\" and rel=\"noopener\"", "Positive", "Medium"),
            ("Crawl every link on the page and verify none returns HTTP 404 or 5xx", "Negative", "High"),
            ("Click play on each video/audio element and verify playback starts within 2 seconds with no decoder errors", "Positive", "Medium"),
            ("Click each social-media link and verify the destination URL matches the brand's official account for that network", "Positive", "Medium"),
            ("Click a mailto: link and verify the default email client opens with the prefilled recipient address", "Positive", "Low"),
            ("Tap a tel: link on a mobile device and verify the dialer opens with the prefilled phone number", "Positive", "Low"),
        ],
        "Footer": [
            ("Verify that the Footer is rendered at the bottom of the page and stays below the content on every viewport", "Positive", "Medium"),
            ("Click every link inside the Footer and verify each destination returns HTTP 200 and matches the link label", "Positive", "Medium"),
            ("Verify that the copyright line in the Footer shows the current year and the legal entity name from the design spec", "Positive", "Low"),
            ("Visit at least three pages of the site and verify the Footer has the same structure and links on each page", "Positive", "Low"),
        ],
        "Forms & Input": [
            ("Verify that every form field has a visible label matching the spec (no placeholder-only labels)", "Positive", "High"),
            ("Verify that every required field is marked with an asterisk and carries aria-required=\"true\"", "Positive", "Medium"),
            ("Fill every field with valid sample data and submit: verify the backend accepts the payload and the UI shows a success state", "Positive", "High"),
            ("Submit the form with required fields empty and verify an inline error appears next to each missing field", "Negative", "High"),
            ("Enter invalid email values ('test@', '@domain', 'no-at-sign') and verify each is rejected with a 'valid email required' message", "Negative", "High"),
            ("Enter letters into the phone field and verify the form blocks submission with a numeric-only message", "Negative", "Medium"),
            ("Submit a valid form and verify that a success banner or toast appears within 2 seconds and the URL/state updates as designed", "Positive", "High"),
            ("Enter valid data, trigger a validation error on one field, and verify the other fields keep their values", "Negative", "Medium"),
            ("Enter the field's maximum allowed length (per spec) and verify the form submits without truncation or error", "Edge Case", "Medium"),
            ("Enter the field's minimum allowed length and verify it passes validation", "Edge Case", "Medium"),
            ("Enter Unicode and emoji characters (e.g. 'Привіт 🚀') and verify they survive submission and round-trip in the saved record", "Edge Case", "Medium"),
            ("Submit '<script>alert(1)</script>' in every text field and verify it is stored/displayed as inert text, not executed", "Security", "High"),
            ("Submit \"' OR 1=1 --\" in every text field and verify the response is a normal validation error, not a SQL error or changed result set", "Security", "High"),
        ],
        "Responsive Design": [
            ("Resize the viewport to 375px width and verify the layout uses a single column with no horizontal scrollbar", "Positive", "High"),
            ("Resize the viewport to 768px width and verify the layout matches the tablet breakpoint of the design spec", "Positive", "High"),
            ("Resize the viewport to 1280px+ and verify the layout matches the desktop breakpoint of the design spec", "Positive", "High"),
            ("Resize the viewport between 375px and 1920px and verify images scale proportionally with no distortion or cropping of faces/text", "Positive", "Medium"),
            ("Verify that no page state (normal, modal open, form focused) introduces a horizontal scrollbar at any viewport ≥ 320px", "Negative", "High"),
            ("Open the page on a 375px viewport and verify every body text paragraph is legible at 100% zoom (font-size ≥ 14px)", "Positive", "Medium"),
            ("Tap each interactive control on a mobile viewport (375px) and verify its hit area measures ≥ 44×44 CSS pixels including padding", "Positive", "Medium"),
            ("Rotate a mobile device from portrait to landscape and verify the layout re-flows without truncation or fixed-position overlap", "Edge Case", "Low"),
        ],
        # ── Cross-browser/cross-device checks deliberately removed ──
        # These are environment variants of a single functional check and
        # belong in the test-run configuration (browser/device dropdowns),
        # not as standalone checklist rows. Testers can re-run any item
        # on another browser via Test Execution → Environment.
        "Performance": [
            ("Verify that the page is loaded within 3 seconds on a standard connection", "Positive", "High"),
            ("Verify that no JavaScript console errors are present on page load", "Negative", "High"),
            ("Verify that images are optimized (compressed, WebP format where supported)", "Positive", "Medium"),
            ("Verify that lazy loading is applied to below-the-fold images", "Positive", "Low"),
            ("Verify that the page is usable on a slow 3G connection", "Edge Case", "Medium"),
        ],
        "Accessibility (WCAG 2.1)": [
            ("Verify that all images have descriptive alt text", "Positive", "High"),
            ("Verify that color contrast ratio meets WCAG 2.1 AA standard (4.5:1 for text)", "Positive", "High"),
            ("Verify that the page is navigable using keyboard only (Tab, Enter, Escape)", "Positive", "High"),
            ("Verify that focus indicators are visible on interactive elements", "Positive", "Medium"),
            ("Verify that screen reader can read all content in logical order", "Positive", "Medium"),
            ("Verify that form fields have associated labels for assistive technologies", "Positive", "Medium"),
            ("Verify that every interactive region has an aria-role matching its purpose (navigation, main, complementary)", "Positive", "Low"),
        ],
        "Security (Basic)": [
            ("Verify that the page is served over HTTPS", "Positive", "High"),
            ("Verify that no sensitive data is exposed in page source or console", "Negative", "High"),
            ("Verify that Content Security Policy (CSP) headers are present", "Positive", "Medium"),
            ("Verify that cookies are set with Secure and HttpOnly flags", "Positive", "Medium"),
            ("Verify that no mixed content (HTTP resources on HTTPS page) is present", "Negative", "Medium"),
        ],
        "SEO (Basic)": [
            ("Verify that meta title is present and under 60 characters", "Positive", "Medium"),
            ("Verify that meta description is present and under 160 characters", "Positive", "Medium"),
            ("Verify that canonical URL is specified", "Positive", "Low"),
            ("Verify that the page has exactly one H1 and its H2/H3 tags do not skip levels", "Positive", "Low"),
        ],
    }


def _auth_checks() -> _CL:
    return {
        "Login Form — UI": [
            ("Verify that the login form is displayed with email/username and password fields", "Positive", "High"),
            ("Verify that the password field is masked (characters are hidden)", "Positive", "High"),
            ("Verify that 'Show password' toggle is revealed/hidden password characters", "Positive", "Medium"),
            ("Verify that 'Forgot password' link is displayed and redirected to recovery page", "Positive", "High"),
            ("Verify that 'Remember me' checkbox is present and functional", "Positive", "Medium"),
            ("Verify that 'Sign up' / 'Register' link is displayed for new users", "Positive", "Medium"),
        ],
        "Login — Positive": [
            ("Verify that the user is logged in successfully with valid email and password", "Positive", "High"),
            ("Verify that the user is redirected to the expected page after successful login", "Positive", "High"),
            ("Verify that the session cookie/token is set after successful login", "Positive", "High"),
            ("Verify that the username/avatar is displayed in the Header after login", "Positive", "Medium"),
            ("Verify that 'Remember me' is extended the session beyond browser close", "Positive", "Medium"),
        ],
        "Login — Negative": [
            ("Verify that login is rejected with a password that does not match the stored hash", "Negative", "High"),
            ("Verify that login is rejected with non-existent email", "Negative", "High"),
            ("Verify that login is rejected when email field is left empty", "Negative", "High"),
            ("Verify that login is rejected when password field is left empty", "Negative", "High"),
            ("Verify that the error message does not reveal which specific field failed validation (uniform 'invalid credentials' message)", "Negative", "High"),
            ("Verify that multiple failed attempts are triggered account lockout or CAPTCHA", "Negative", "High"),
        ],
        "Login — Edge Cases & Security": [
            ("Verify that email field is accepted with maximum length (254 characters)", "Edge Case", "Medium"),
            ("Verify that password field is accepted with special characters (!@#$%^&*)", "Edge Case", "Medium"),
            ("Verify that login returns the expected outcome per the spec after session timeout", "Edge Case", "Medium"),
            ("Verify that concurrent sessions from different devices are handled per policy", "Edge Case", "Medium"),
            ("Verify that SQL injection is blocked in the email field", "Security", "High"),
            ("Verify that XSS payload is sanitized in the email field", "Security", "High"),
            ("Verify that brute-force protection is activated after multiple failed attempts", "Security", "High"),
            ("Verify that the password is not logged in server logs or browser console", "Security", "High"),
        ],
        "Registration Form — UI": [
            ("Verify that the registration form is displayed with all required fields", "Positive", "High"),
            ("Verify that password strength indicator is displayed while typing", "Positive", "Medium"),
            ("Verify that 'Terms and Conditions' checkbox is present", "Positive", "Medium"),
            ("Verify that 'Already have an account? Sign in' link is displayed", "Positive", "Medium"),
        ],
        "Registration — Positive": [
            ("Verify that a new account is created successfully with valid data", "Positive", "High"),
            ("Verify that confirmation email is sent after successful registration", "Positive", "High"),
            ("Verify that the user is redirected to welcome/login page after registration", "Positive", "High"),
        ],
        "Registration — Negative": [
            ("Verify that registration is rejected with already existing email", "Negative", "High"),
            ("Verify that registration is rejected with weak password (less than 8 chars)", "Negative", "High"),
            ("Verify that registration is rejected when required fields are empty", "Negative", "High"),
            ("Verify that registration is rejected with mismatched password confirmation", "Negative", "High"),
            ("Verify that registration is rejected with invalid email format", "Negative", "High"),
        ],
        "Password Recovery": [
            ("Verify that 'Forgot password' page is displayed with email input field", "Positive", "High"),
            ("Verify that password reset email is sent for a valid registered email", "Positive", "High"),
            ("Verify that the reset link in the email is redirected to the password change form", "Positive", "High"),
            ("Verify that the new password is saved successfully after reset", "Positive", "High"),
            ("Verify that the reset link is expired after a single use", "Security", "High"),
            ("Verify that the reset link is expired after a timeout period (e.g. 24h)", "Security", "Medium"),
            ("Verify that no information is revealed for non-existent email", "Security", "High"),
        ],
        "Logout": [
            ("Verify that the user is logged out successfully when clicking Logout", "Positive", "High"),
            ("Verify that the session is destroyed after logout", "Positive", "High"),
            ("Verify that protected pages are not accessible after logout", "Positive", "High"),
            ("Verify that the browser Back button does not restore the authenticated session", "Security", "High"),
        ],
    }


def _search_checks() -> _CL:
    return {
        "Search — UI": [
            ("Verify that the search input field is displayed and accessible", "Positive", "High"),
            ("Verify that a search icon or button is present", "Positive", "Medium"),
            ("Verify that placeholder text is displayed in the search field", "Positive", "Low"),
            ("Verify that the search field is focused when clicked", "Positive", "Low"),
        ],
        "Search — Positive": [
            ("Verify that relevant results are returned for a valid search query", "Positive", "High"),
            ("Verify that search results contain the search term highlighted", "Positive", "Medium"),
            ("Verify that the result count is displayed and accurate", "Positive", "Medium"),
            ("Verify that search is triggered by pressing Enter key", "Positive", "Medium"),
            ("Verify that search is triggered by clicking the search button", "Positive", "Medium"),
            ("Verify that pagination is displayed when results exceed the page limit", "Positive", "Medium"),
        ],
        "Search — Negative": [
            ("Verify that 'No results found' message is displayed for non-matching query", "Negative", "High"),
            ("Verify that search is handled gracefully with empty query submission", "Negative", "High"),
            ("Verify that the search UI is not broken when no results are returned", "Negative", "Medium"),
        ],
        "Search — Edge Cases & Security": [
            ("Verify that search returns the expected outcome per the spec with special characters (!@#$%^&*)", "Edge Case", "Medium"),
            ("Verify that search returns the expected outcome per the spec with very long query (500+ characters)", "Edge Case", "Medium"),
            ("Verify that search returns the expected outcome per the spec with Unicode/emoji characters", "Edge Case", "Low"),
            ("Verify that SQL injection is blocked in the search field", "Security", "High"),
            ("Verify that XSS payload is sanitized in the search field", "Security", "High"),
        ],
        "Filter & Sort": [
            ("Verify that filter options are visible and matches the design spec", "Positive", "High"),
            ("Verify that results are updated when a filter is applied", "Positive", "High"),
            ("Verify that multiple filters are applied simultaneously", "Positive", "Medium"),
            ("Verify that applied filters are cleared when 'Reset' is clicked", "Positive", "Medium"),
            ("Verify that each sort option (A-Z, Z-A, date, price) reorders results in the expected direction and persists after a refresh", "Positive", "High"),
            ("Verify that the active sort/filter state is preserved after page navigation", "Positive", "Medium"),
            ("Verify that filter results show 'No matches' when no items match criteria", "Negative", "Medium"),
        ],
    }


def _forms_checks() -> _CL:
    return {
        "Form Fields — UI": [
            ("Verify that all every form field has a visible label whose text matches the design spec", "Positive", "High"),
            ("Verify that required fields are indicated with an asterisk (*)", "Positive", "High"),
            ("Verify that placeholder text is displayed in empty fields", "Positive", "Low"),
            ("Verify that every field uses the HTML input type declared in the spec (email, number, date, tel, url)", "Positive", "Medium"),
            ("Verify that every dropdown lists every option from the spec in the declared order", "Positive", "Medium"),
            ("Verify that date picker is displayed and functional", "Positive", "Medium"),
        ],
        "Form Validation — Positive": [
            ("Verify that the form is submitted successfully with all valid data", "Positive", "High"),
            ("Verify that success message/notification is displayed after submission", "Positive", "High"),
            ("Verify that the submitted payload appears in the backend storage with all fields intact", "Positive", "High"),
            ("Verify that the form is reset/redirected after successful submission", "Positive", "Medium"),
        ],
        "Form Validation — Negative": [
            ("Verify that validation error is displayed when required fields are empty", "Negative", "High"),
            ("Verify that email field is rejected with invalid format", "Negative", "High"),
            ("Verify that numeric fields are rejected with non-numeric input", "Negative", "High"),
            ("Verify that error messages are clear and specific per field", "Negative", "High"),
            ("Verify that form data is preserved when validation error occurs", "Negative", "Medium"),
            ("Verify that the form is not submitted when validation fails", "Negative", "High"),
        ],
        "Form — Edge Cases": [
            ("Verify that fields are accepted with minimum allowed length", "Edge Case", "Medium"),
            ("Verify that fields are accepted with maximum allowed length", "Edge Case", "Medium"),
            ("Verify that fields are returns the expected outcome per the spec at boundary values (min-1, max+1)", "Edge Case", "Medium"),
            ("Verify that special characters are returns the expected outcome per the spec in text fields", "Edge Case", "Medium"),
            ("Verify that copy-paste is producing the outcome defined in the design spec in all fields", "Edge Case", "Low"),
            ("Verify that double submission is prevented (double-click protection)", "Edge Case", "High"),
            ("Verify that the the form can be submitted using only Tab to reach the submit button and Enter to trigger it", "Edge Case", "Medium"),
        ],
    }


def _crud_checks(operation: str = "create") -> _CL:
    op_map = {
        "create": ("Create", "created", "creation form"),
        "read":   ("View", "displayed", "list/detail page"),
        "update": ("Edit", "updated", "edit form"),
        "delete": ("Delete", "deleted", "list page"),
    }
    label, past, location = op_map.get(operation, op_map["create"])

    if operation == "create":
        return {
            f"{label} — Positive": [
                (f"Verify that the {location} is displayed with all required fields", "Positive", "High"),
                (f"Verify that a new record is {past} successfully with valid data", "Positive", "High"),
                (f"Verify that success confirmation is displayed after {label.lower()}", "Positive", "High"),
                (f"Verify that the new record is appeared in the list view", "Positive", "High"),
                (f"Verify that every submitted field appears in the database row with the exact value entered", "Positive", "High"),
            ],
            f"{label} — Negative": [
                (f"Verify that {label.lower()} is rejected when required fields are empty", "Negative", "High"),
                (f"Verify that {label.lower()} is rejected with invalid data format", "Negative", "High"),
                (f"Verify that duplicate record {label.lower()} returns the expected outcome per the spec", "Negative", "Medium"),
                (f"Verify that validation errors are displayed per field", "Negative", "High"),
            ],
            f"{label} — Edge Cases": [
                (f"Verify that {label.lower()} returns the expected outcome per the spec with minimum length data", "Edge Case", "Medium"),
                (f"Verify that {label.lower()} returns the expected outcome per the spec with maximum length data", "Edge Case", "Medium"),
                (f"Verify that special characters are accepted in text fields", "Edge Case", "Medium"),
                (f"Verify that concurrent {label.lower()} requests are returns the expected outcome per the spec", "Edge Case", "Low"),
            ],
        }
    elif operation == "read":
        return {
            f"{label} — Positive": [
                (f"Verify that the {location} shows the same values that were saved", "Positive", "High"),
                (f"Verify that all data columns/fields are visible and formatted per the design spec (date format, currency symbol, thousands separator)", "Positive", "High"),
                (f"Verify that pagination is producing the outcome defined in the design spec when records exceed page limit", "Positive", "Medium"),
                (f"Verify that sorting reorders rows in ascending/descending order of the selected column", "Positive", "Medium"),
                (f"Verify that detail view is visible and matches the design spec when a record is selected", "Positive", "High"),
            ],
            f"{label} — Negative & Edge Cases": [
                (f"Verify that empty state is handled gracefully when no records exist", "Negative", "High"),
                (f"Verify that the page returns the expected outcome per the spec when data loading fails", "Negative", "Medium"),
                (f"Verify that a large dataset (1000+ records) is loaded without performance issues", "Edge Case", "Medium"),
            ],
        }
    elif operation == "update":
        return {
            f"{label} — Positive": [
                (f"Verify that the {location} is pre-filled with current data", "Positive", "High"),
                (f"Verify that the record is {past} successfully with valid changes", "Positive", "High"),
                (f"Verify that success confirmation is displayed after saving", "Positive", "High"),
                (f"Verify that changes are persisted and reflected in list/detail view", "Positive", "High"),
            ],
            f"{label} — Negative": [
                (f"Verify that {label.lower()} is rejected when required fields are cleared", "Negative", "High"),
                (f"Verify that {label.lower()} is rejected with invalid data format", "Negative", "High"),
                (f"Verify that original data is preserved when validation error occurs", "Negative", "Medium"),
            ],
            f"{label} — Edge Cases": [
                (f"Verify that concurrent edits to the same record are returns the expected outcome per the spec", "Edge Case", "Medium"),
                (f"Verify that {label.lower()} without any changes is handled gracefully", "Edge Case", "Low"),
                (f"Verify that unsaved changes warning is displayed when navigating away", "Edge Case", "Medium"),
            ],
        }
    else:  # delete
        return {
            f"{label} — Positive": [
                (f"Verify that confirmation dialog is displayed before deletion", "Positive", "High"),
                (f"Verify that the record is {past} successfully after confirmation", "Positive", "High"),
                (f"Verify that the record is removed from the list view after deletion", "Positive", "High"),
                (f"Verify that success message is displayed after deletion", "Positive", "High"),
            ],
            f"{label} — Negative": [
                (f"Verify that deletion is cancelled when 'Cancel' is clicked in confirmation", "Negative", "High"),
                (f"Verify that related/dependent records are returns the expected outcome per the spec on deletion", "Negative", "Medium"),
            ],
            f"{label} — Edge Cases": [
                (f"Verify that deleting the last record returns the expected outcome per the spec", "Edge Case", "Medium"),
                (f"Verify that bulk deletion is producing the outcome defined in the design spec (if supported)", "Edge Case", "Low"),
            ],
        }


def _payment_checks() -> _CL:
    return {
        "Cart": [
            ("Verify that items are added to the cart successfully", "Positive", "High"),
            ("Verify that the cart is shows the item count and order total that match the sum of cart lines", "Positive", "High"),
            ("Verify that changing quantity updates the line subtotal and the cart total immediately", "Positive", "High"),
            ("Verify that items are removed from the cart successfully", "Positive", "High"),
            ("Verify that the cart is preserved after page refresh", "Positive", "Medium"),
            ("Verify that the empty cart state is displayed when all items are removed", "Negative", "Medium"),
            ("Verify that adding the same item twice is increased the quantity, not duplicated", "Edge Case", "Medium"),
        ],
        "Checkout — Positive": [
            ("Verify that the checkout page is displayed with order summary", "Positive", "High"),
            ("Verify that shipping address form is displayed and functional", "Positive", "High"),
            ("Verify that payment method selection is displayed", "Positive", "High"),
            ("Verify that the order is placed successfully with valid payment details", "Positive", "High"),
            ("Verify that order confirmation page/email is received after purchase", "Positive", "High"),
            ("Verify that the the order total equals sum(items) + tax + shipping, matching the backend calculation", "Positive", "High"),
        ],
        "Checkout — Negative": [
            ("Verify that checkout is rejected with invalid payment details", "Negative", "High"),
            ("Verify that checkout is rejected with expired credit card", "Negative", "High"),
            ("Verify that checkout is rejected when required shipping fields are empty", "Negative", "High"),
            ("Verify that a clear error message is displayed when payment is declined", "Negative", "High"),
        ],
        "Payment — Security & Edge Cases": [
            ("Verify that payment page is served over HTTPS", "Security", "High"),
            ("Verify that credit card number is masked in UI after entry", "Security", "High"),
            ("Verify that no payment data is stored in browser localStorage/cookies", "Security", "High"),
            ("Verify that double-charge is prevented on multiple submit clicks", "Edge Case", "High"),
            ("Verify that the order returns the expected outcome per the spec when payment gateway times out", "Edge Case", "High"),
            ("Verify that the cart is preserved when payment fails", "Edge Case", "Medium"),
        ],
    }


def _navigation_checks() -> _CL:
    return {
        "Navigation": [
            ("Verify that the main navigation menu is displayed on all pages", "Positive", "High"),
            ("Click every menu item and verify the destination URL matches the item label", "Positive", "High"),
            ("Verify that the current page is highlighted in the navigation", "Positive", "Medium"),
            ("Verify that browser Back/Forward buttons are producing the outcome defined in the design spec", "Positive", "Medium"),
            ("Verify that the browser URL changes to match the destination route on every navigation", "Positive", "Medium"),
            ("Verify that 404 page is displayed for non-existent URLs", "Negative", "High"),
            ("Verify that deep links (direct URL access) are producing the outcome defined in the design spec", "Positive", "Medium"),
        ],
    }


# ── Area → knowledge function mapping ──────────────────────────────

_AREA_CHECKLIST_FN = {
    "web_general":  _web_general_checks,
    "auth":         _auth_checks,
    "search":       _search_checks,
    "forms":        _forms_checks,
    "crud_create":  lambda: _crud_checks("create"),
    "crud_read":    lambda: _crud_checks("read"),
    "crud_update":  lambda: _crud_checks("update"),
    "crud_delete":  lambda: _crud_checks("delete"),
    "payment":      _payment_checks,
    "navigation":   _navigation_checks,
}


# ═══════════════════════════════════════════════════════════════════
# 3. Section prefix mapping (TestFort format)
# ═══════════════════════════════════════════════════════════════════

_SECTION_PREFIXES = {
    "Header & Navigation":       "HDR",
    "Content & Layout":          "CNT",
    "Links & Media":             "LNK",
    "Footer":                    "FTR",
    "Forms & Input":             "FRM",
    "Responsive Design":         "RSP",
    "Cross-browser Compatibility": "BRW",
    "Performance":               "PRF",
    "Accessibility (WCAG 2.1)":  "A11Y",
    "Security (Basic)":          "SEC",
    "SEO (Basic)":               "SEO",
    # Auth
    "Login Form — UI":           "LGN",
    "Login — Positive":          "LGN",
    "Login — Negative":          "LGN",
    "Login — Edge Cases & Security": "LGN",
    "Registration Form — UI":    "REG",
    "Registration — Positive":   "REG",
    "Registration — Negative":   "REG",
    "Password Recovery":         "PWD",
    "Logout":                    "LGO",
    # Search
    "Search — UI":               "SRCH",
    "Search — Positive":         "SRCH",
    "Search — Negative":         "SRCH",
    "Search — Edge Cases & Security": "SRCH",
    "Filter & Sort":             "FLT",
    # Forms
    "Form Fields — UI":          "FRM",
    "Form Validation — Positive": "FRM",
    "Form Validation — Negative": "FRM",
    "Form — Edge Cases":         "FRM",
    # CRUD
    "Create — Positive":         "CRT",
    "Create — Negative":         "CRT",
    "Create — Edge Cases":       "CRT",
    "View — Positive":           "VIEW",
    "View — Negative & Edge Cases": "VIEW",
    "Edit — Positive":           "UPD",
    "Edit — Negative":           "UPD",
    "Edit — Edge Cases":         "UPD",
    "Delete — Positive":         "DEL",
    "Delete — Negative":         "DEL",
    "Delete — Edge Cases":       "DEL",
    # Payment
    "Cart":                      "CART",
    "Checkout — Positive":       "CHK",
    "Checkout — Negative":       "CHK",
    "Payment — Security & Edge Cases": "PAY",
    # Navigation
    "Navigation":                "NAV",
}


# ═══════════════════════════════════════════════════════════════════
# 3b. Browser Findings → Checklist / Test-Case Conversion
# ═══════════════════════════════════════════════════════════════════

_FINDING_CAT_TO_SECTION = {
    "Performance": "Performance",
    "JavaScript": "JavaScript & Console",
    "Links": "Links & Navigation",
    "Responsive": "Responsive Design",
    "Forms": "Forms",
    "Navigation": "Header & Navigation",
    "Interactivity": "Interactivity",
    "UI/UX": "UI / UX",
}

_SEVERITY_TO_PRIORITY = {"Critical": "High", "Major": "High", "Minor": "Medium"}


def _browser_findings_to_checklist(findings: list[dict]) -> list[CheckItem]:
    """Convert real browser findings into checklist items."""
    items: list[CheckItem] = []
    for f in findings:
        desc = f.get("description", "")
        status = f.get("status", "Passed")

        # Build objective text
        if desc.startswith("Verify that"):
            objective = desc
        elif status == "Failed":
            objective = f"Verify that {desc} is fixed"
        else:
            objective = f"Verify that {desc}"

        category = "Negative" if status == "Failed" else "Positive"
        section = _FINDING_CAT_TO_SECTION.get(f.get("category", ""), "Browser Tests")
        priority = _SEVERITY_TO_PRIORITY.get(f.get("severity", "Minor"), "Medium")

        items.append(CheckItem(
            objective=objective,
            category=category,
            priority=priority,
            section=f"Browser: {section}",
        ))
    return items


def _browser_findings_to_test_cases(findings: list[dict]) -> list[TCTemplate]:
    """Convert real browser findings into test case templates."""
    cases: list[TCTemplate] = []
    for f in findings:
        desc = f.get("description", "")
        status = f.get("status", "Passed")
        page_url = f.get("page_url", "")
        category_str = f.get("category", "")
        section = f"Browser: {_FINDING_CAT_TO_SECTION.get(category_str, 'Tests')}"

        if status == "Failed":
            summary = f"Verify that {desc} is fixed"
            expected = f"The issue should be resolved. Current state: {desc}"
            tc_cat = "Negative"
        else:
            summary = f"Verify that {desc}"
            expected = f"{desc}. Confirmed by automated browser check."
            tc_cat = "Positive"

        priority = _SEVERITY_TO_PRIORITY.get(f.get("severity", "Minor"), "Medium")

        cases.append(TCTemplate(
            summary=summary,
            preconditions=f"Browser: Chromium (headless). Page: {page_url}",
            steps=[f"Open {page_url} in a browser",
                   f"Perform automated check: {category_str}",
                   f"Observe: {desc}"],
            test_data=f"URL: {page_url}",
            expected_result=expected,
            category=tc_cat,
            priority=priority,
            section=section,
        ))
    return cases


# ═══════════════════════════════════════════════════════════════════
# 4. Professional Checklist Generator
# ═══════════════════════════════════════════════════════════════════

def generate_professional_checklist(analysis: AnalysisResult,
                                    custom_prompt: str = "") -> list[CheckItem]:
    """Generate a professional low-level checklist based on analysis.

    Applies ISTQB test design techniques:
      - Equivalence Partitioning (valid/invalid classes per input)
      - Boundary Value Analysis (min, min+1, max-1, max)
      - State Transition (login states, cart states)
    """
    items: list[CheckItem] = []

    # Collect checks from all detected areas
    for area in analysis.areas:
        fn = _AREA_CHECKLIST_FN.get(area)
        if fn:
            sections = fn()
            for section_name, checks in sections.items():
                for objective, category, priority in checks:
                    items.append(CheckItem(
                        objective=objective,
                        category=category,
                        priority=priority,
                        section=section_name,
                    ))

    # Named-flow playbooks (e.g. "checkout_flow")
    for flow_key in getattr(analysis, "flows", []) or []:
        items.extend(_flow_checks(flow_key))

    # If we only got web_general and there were specific feature
    # requirements, add them as a custom section
    if analysis.raw_requirements:
        for req_text in analysis.raw_requirements:
            # Skip URLs and instruction lines
            if re.match(r"^(https?://|test\s+the\s+)", req_text, re.I):
                continue
            if is_instruction(req_text):
                continue
            # Add requirement-specific checks
            short = req_text[:80].rstrip(".")
            items.append(CheckItem(
                objective=f"Verify that {short} is functioning as expected",
                category="Positive", priority="High",
                section="Requirements-specific",
            ))
            items.append(CheckItem(
                objective=f"Verify that {short} is rejected with invalid input",
                category="Negative", priority="High",
                section="Requirements-specific",
            ))

    # ── Browser findings → checklist items ────────────────────────
    # Real observations from Playwright replace/supplement templates.
    if analysis.browser_findings:
        items.extend(_browser_findings_to_checklist(analysis.browser_findings))

    # ── Filter SQL injection for URL-based testing ──────────────
    # SQL injection checks are not applicable when testing 3rd-party
    # sites by URL — we don't have backend access.
    if analysis.url:
        items = [i for i in items if "sql injection" not in i.objective.lower()
                 and "sql" not in i.objective.lower().split("inject")[0:1]]

    # Apply custom prompt category filter
    if custom_prompt:
        lower_prompt = custom_prompt.lower()
        if "positive only" in lower_prompt or "тільки позитивні" in lower_prompt:
            items = [i for i in items if i.category == "Positive"]
        elif "negative only" in lower_prompt or "тільки негативні" in lower_prompt:
            items = [i for i in items if i.category == "Negative"]
        elif "security" in lower_prompt and ("only" in lower_prompt or "focus" in lower_prompt):
            items = [i for i in items if i.category == "Security"]

    return items


# ═══════════════════════════════════════════════════════════════════
# 5. Professional Test Case Generator
# ═══════════════════════════════════════════════════════════════════

# Test Case knowledge base per area
def _auth_test_cases() -> list[TCTemplate]:
    return [
        TCTemplate(
            summary="Verify that login is completed successfully with valid credentials",
            preconditions="Application is accessible. Test user account is created.",
            steps=["Navigate to the login page",
                   "Enter a valid email address",
                   "Enter a valid password",
                   "Click the 'Login' / 'Sign In' button"],
            test_data="Email: testuser@example.com, Password: ValidPass123!",
            expected_result="User should be authenticated successfully. User should be redirected to the dashboard/homepage. Session cookie should be set.",
            category="Positive", priority="High", section="Authentication",
        ),
        TCTemplate(
            summary="Verify that login is rejected with an invalid-credentials message when the password does not match",
            preconditions="Application is accessible. Test user account is created.",
            steps=["Navigate to the login page",
                   "Enter a valid registered email address",
                   "Enter a password that does not match the stored hash",
                   "Click the 'Login' / 'Sign In' button"],
            test_data="Email: testuser@example.com, Password: WrongPass999",
            expected_result="Login should be rejected. Generic error message should be displayed (e.g. 'Invalid credentials'). No information about which field is wrong should be revealed.",
            category="Negative", priority="High", section="Authentication",
        ),
        TCTemplate(
            summary="Verify that login is rejected when email field is left empty",
            preconditions="Application is accessible. Login page is opened.",
            steps=["Leave the email field empty",
                   "Enter a password",
                   "Click the 'Login' / 'Sign In' button"],
            test_data="Email: (empty), Password: ValidPass123!",
            expected_result="Form should not be submitted. Validation error should be displayed for the email field.",
            category="Negative", priority="High", section="Authentication",
        ),
        TCTemplate(
            summary="Verify that validation is triggered for invalid email format",
            preconditions="Application is accessible. Login page is opened.",
            steps=["Enter an invalid email format in the email field",
                   "Enter a valid password",
                   "Click the 'Login' / 'Sign In' button"],
            test_data="Email: 'test@', '@domain.com', 'plaintext', 'test @example.com'",
            expected_result="Form validation should be triggered. Error message should indicate invalid email format.",
            category="Negative", priority="High", section="Authentication",
        ),
        TCTemplate(
            summary="Verify that brute-force protection is activated after multiple failed login attempts",
            preconditions="Application is accessible. Test user account is created.",
            steps=["Navigate to the login page",
                   "Enter a valid email address",
                   "Enter a password that does not match the stored hash",
                   "Repeat failed login 5+ times consecutively",
                   "Observe the system behavior"],
            test_data="Email: testuser@example.com, Password: WrongPass (repeated 5+ times)",
            expected_result="Account lockout should be activated, or CAPTCHA should be displayed, or rate-limiting should be applied after the threshold is exceeded.",
            category="Security", priority="High", section="Authentication",
        ),
        TCTemplate(
            summary="Verify that SQL injection is blocked in the login form",
            preconditions="Application is accessible. Login page is opened.",
            steps=["Enter an SQL injection payload in the email field",
                   "Enter any value in the password field",
                   "Click the 'Login' / 'Sign In' button"],
            test_data="Email: ' OR 1=1 --, Password: anything",
            expected_result="Input should be sanitized. No database error should be exposed. Login should be rejected with a generic error message.",
            category="Security", priority="High", section="Authentication",
        ),
        TCTemplate(
            summary="Verify that the user is logged out successfully",
            preconditions="User is authenticated. Application is accessible.",
            steps=["Click the 'Logout' / 'Sign Out' button or link",
                   "Observe the redirect",
                   "Attempt to access a protected page directly via URL"],
            test_data="",
            expected_result="User should be logged out. Session should be destroyed. Protected pages should not be accessible. User should be redirected to the login page.",
            category="Positive", priority="High", section="Authentication",
        ),
    ]


def _search_test_cases() -> list[TCTemplate]:
    return [
        TCTemplate(
            summary="Verify that the search returns items whose title or body contains the query string, ordered by relevance",
            preconditions="Application is accessible. Searchable data is present in the system.",
            steps=["Navigate to the page with search functionality",
                   "Enter a valid search term that matches existing data",
                   "Submit the search (press Enter or click Search button)",
                   "Review the results"],
            test_data="Search term matching existing data (e.g. 'software testing')",
            expected_result="Search results should be displayed. Results should be relevant to the search term. Result count should be accurate.",
            category="Positive", priority="High", section="Search",
        ),
        TCTemplate(
            summary="Verify that 'No results' message is displayed for a non-matching query",
            preconditions="Application is accessible. Search functionality is available.",
            steps=["Enter a search term that does not match any existing data",
                   "Submit the search"],
            test_data="Search term: 'xyznonexistent123456'",
            expected_result="'No results found' message should be displayed. No errors should occur. Search UI should remain intact.",
            category="Negative", priority="High", section="Search",
        ),
        TCTemplate(
            summary="Verify that XSS and SQL injection are blocked in the search field",
            preconditions="Application is accessible. Search functionality is available.",
            steps=["Enter an XSS payload in the search field and submit",
                   "Enter an SQL injection payload in the search field and submit",
                   "Observe the page behavior after each submission"],
            test_data="XSS: <script>alert('xss')</script>, SQL: ' OR 1=1 --",
            expected_result="Payloads should be sanitized. No script execution or database error should occur. Search should return empty results or an error message.",
            category="Security", priority="High", section="Search",
        ),
    ]


def _forms_test_cases() -> list[TCTemplate]:
    return [
        TCTemplate(
            summary="Verify that the form is submitted successfully with all valid data",
            preconditions="Application is accessible. Form page is opened.",
            steps=["Fill all required fields with valid data",
                   "Fill optional fields if present",
                   "Click the Submit/Save button",
                   "Verify success message is displayed"],
            test_data="Valid values for all fields (email, text, numbers, dates)",
            expected_result="Form should be submitted successfully. Success confirmation should be displayed. Data should be saved.",
            category="Positive", priority="High", section="Forms",
        ),
        TCTemplate(
            summary="Verify that validation errors are displayed when required fields are left empty",
            preconditions="Application is accessible. Form page is opened.",
            steps=["Leave all required fields empty",
                   "Click the Submit/Save button",
                   "Observe validation messages for each required field"],
            test_data="All fields empty",
            expected_result="Form should not be submitted. Validation error messages should be displayed for each required field.",
            category="Negative", priority="High", section="Forms",
        ),
        TCTemplate(
            summary="Verify that email field rejects invalid email format",
            preconditions="Application is accessible. Form with email field is opened.",
            steps=["Enter an invalid email address in the email field",
                   "Click the Submit/Save button",
                   "Observe the validation error"],
            test_data="Invalid emails: 'test@', '@domain.com', 'plaintext', 'test @example'",
            expected_result="Email field should reject invalid formats. Clear error message should be displayed.",
            category="Negative", priority="High", section="Forms",
        ),
        TCTemplate(
            summary="Verify that XSS and SQL injection are blocked in form fields",
            preconditions="Application is accessible. Form is opened.",
            steps=["Enter XSS payload in text fields and submit",
                   "Enter SQL injection payload in text fields and submit",
                   "Observe the behavior"],
            test_data="XSS: <script>alert('xss')</script>, SQL: ' OR 1=1 --",
            expected_result="Payloads should be sanitized. No script execution or database errors should occur.",
            category="Security", priority="High", section="Forms",
        ),
        TCTemplate(
            summary="Verify that form accepts min and max boundary values and rejects min-1 / max+1",
            preconditions="Application is accessible. Form is opened.",
            steps=["Enter minimum length values in text fields (1 character)",
                   "Enter maximum length values in text fields",
                   "Enter special characters and Unicode in all text fields",
                   "Submit the form after each test"],
            test_data="Min: 'A', Max: 255 chars, Special: @#$%^&*(), Unicode: test",
            expected_result="Min and max boundary values are accepted; min-1 and max+1 are rejected with a field-specific message. No errors or data corruption should occur.",
            category="Edge Case", priority="Medium", section="Forms",
        ),
    ]


def _payment_test_cases() -> list[TCTemplate]:
    return [
        TCTemplate(
            summary="Verify that items are added to the cart successfully",
            preconditions="Application is accessible. Products are available.",
            steps=["Navigate to a product page",
                   "Click 'Add to Cart' button",
                   "Navigate to the cart page",
                   "Verify the item shows the same name, price, and image that are stored on the product page"],
            test_data="Any available product",
            expected_result="Product should be added to the cart. Cart count should be updated. Product details in the cart match the product page fields exactly.",
            category="Positive", priority="High", section="Payment & Checkout",
        ),
        TCTemplate(
            summary="Verify that checkout is completed successfully with valid payment details",
            preconditions="Items are in the cart. Checkout page is accessible.",
            steps=["Navigate to checkout",
                   "Fill shipping/billing information",
                   "Enter valid payment details",
                   "Complete the purchase",
                   "Verify order confirmation"],
            test_data="Valid card: 4242 4242 4242 4242, Exp: 12/28, CVC: 123",
            expected_result="Order should be placed successfully. Confirmation page/email should be received. Cart should be cleared.",
            category="Positive", priority="High", section="Payment & Checkout",
        ),
        TCTemplate(
            summary="Verify that checkout is rejected with invalid payment details",
            preconditions="Items are in the cart. Checkout page is opened.",
            steps=["Enter invalid or expired payment details",
                   "Attempt to complete the purchase",
                   "Observe the error handling"],
            test_data="Declined card: 4000 0000 0000 0002, Expired: 01/20",
            expected_result="Payment should be declined. Clear error message should be displayed. Cart should be preserved for retry.",
            category="Negative", priority="High", section="Payment & Checkout",
        ),
        TCTemplate(
            summary="Verify that double-charge is prevented on multiple submit clicks",
            preconditions="Checkout page is reached. Payment form is filled.",
            steps=["Click the 'Pay' / 'Place Order' button rapidly multiple times",
                   "Verify payment was processed only once",
                   "Check for duplicate charges"],
            test_data="",
            expected_result="Only one charge should be processed. No duplicate orders should be created.",
            category="Edge Case", priority="High", section="Payment & Checkout",
        ),
    ]


def _navigation_test_cases() -> list[TCTemplate]:
    return [
        TCTemplate(
            summary="Verify that every navigation menu item opens the URL that matches its label",
            preconditions="Application is accessible. Main page is loaded.",
            steps=["Identify all items in the navigation menu",
                   "Click each menu item one by one",
                   "Verify the destination URL matches the item label and the heading on the page matches",
                   "Verify the browser URL changes to match the navigated route"],
            test_data="All navigation menu items",
            expected_result="Every navigation item opens the URL that matches its label. The browser URL matches the route declared for that menu item.",
            category="Positive", priority="High", section="Navigation",
        ),
        TCTemplate(
            summary="Verify that 404 page is displayed for non-existent URLs",
            preconditions="Application is accessible.",
            steps=["Enter a non-existent URL path in the browser address bar",
                   "Observe the page that loads"],
            test_data="Non-existent path: /this-page-does-not-exist-12345",
            expected_result="A 404 error page should be displayed. The page should be styled and user-friendly, not a raw server error.",
            category="Negative", priority="High", section="Navigation",
        ),
        TCTemplate(
            summary="Verify that the browser Back button returns to the previous route and Forward restores the next one",
            preconditions="Application is accessible. User has navigated through several pages.",
            steps=["Navigate through 3-4 different pages",
                   "Click the browser Back button",
                   "Verify the URL returns to the previous route and the page renders the same layout it had before",
                   "Click the browser Forward button",
                   "Verify the URL returns to the most recent route and the page renders the same layout it had before"],
            test_data="",
            expected_result="Browser Back and Forward restore the previous and next routes respectively. Each restored page renders without console errors or missing assets.",
            category="Positive", priority="Medium", section="Navigation",
        ),
    ]


def _web_general_test_cases() -> list[TCTemplate]:
    return [
        TCTemplate(
            summary="Verify that the Homepage loads within 3 seconds and shows the expected title, heading, and main content",
            preconditions="Application is accessible via browser.",
            steps=["Open the application URL in the browser",
                   "Wait for the document ready event and measure load time in DevTools Network tab",
                   "Verify the browser tab title matches the value declared in the design spec",
                   "Verify there is exactly one H1 element and its text matches the page topic"],
            test_data="Application URL",
            expected_result="Homepage loads in under 3 seconds on a wired connection. Browser tab title and H1 match the spec. Network tab shows no 4xx/5xx and Console tab shows no JavaScript errors.",
            category="Positive", priority="High", section="General Web",
        ),
        TCTemplate(
            summary="Verify that all images are loaded without broken links",
            preconditions="Application is accessible. Page is fully loaded.",
            steps=["Open the page in the browser",
                   "Inspect all images on the page",
                   "Verify each image is loaded (no broken image icons)",
                   "Verify images have appropriate alt attributes"],
            test_data="",
            expected_result="All images should be loaded successfully. No broken image placeholders should be visible. Images should have alt text.",
            category="Positive", priority="High", section="General Web",
        ),
        TCTemplate(
            summary="Verify that the page is responsive on mobile viewport (375px width)",
            preconditions="Application is accessible.",
            steps=["Open the page in browser DevTools responsive mode",
                   "Set viewport width to 375px (mobile)",
                   "Verify the layout uses a single column and the Header collapses into a hamburger menu",
                   "Verify no horizontal scrollbar appears",
                   "Verify that body text is ≥ 14px and readable at 100% zoom"],
            test_data="Viewport: 375x812 (iPhone)",
            expected_result="Layout uses a single column at 375px. No horizontal scrollbar. Body font ≥ 14px. Every tap target measures ≥ 44×44 CSS pixels including padding.",
            category="Positive", priority="High", section="Responsive Design",
        ),
        TCTemplate(
            summary="Verify that the page is responsive on tablet viewport (768px width)",
            preconditions="Application is accessible.",
            steps=["Open the page in browser DevTools responsive mode",
                   "Set viewport width to 768px (tablet)",
                   "Verify the layout matches the tablet breakpoint of the design spec (e.g. two-column cards)",
                   "Verify no two visible elements overlap and every container stays inside the 768px width"],
            test_data="Viewport: 768x1024 (iPad)",
            expected_result="Layout matches the tablet breakpoint defined in the design spec. No overlapping elements. No horizontal scrollbar.",
            category="Positive", priority="High", section="Responsive Design",
        ),
        # Cross-browser test case intentionally removed — the browser is a
        # Test Execution environment variable (see the Environment card on
        # /test-execution). Re-running any other case against a different
        # browser is how we cover that axis without duplicating cases.
        TCTemplate(
            summary="Verify that the page loads within 3 seconds and has no console errors",
            preconditions="Application is accessible. Browser DevTools is open.",
            steps=["Open browser DevTools (Console and Network tabs)",
                   "Load the page",
                   "Check the page load time in the Network tab",
                   "Check for JavaScript errors in the Console tab"],
            test_data="",
            expected_result="Page should load within 3 seconds. No JavaScript errors should appear in the console.",
            category="Positive", priority="High", section="Performance",
        ),
        TCTemplate(
            summary="Verify that the page is served over HTTPS with valid certificate",
            preconditions="Application URL is accessible.",
            steps=["Open the application URL",
                   "Check the browser address bar for HTTPS padlock",
                   "Click the padlock to verify certificate validity",
                   "Verify no mixed content warnings"],
            test_data="",
            expected_result="Page should be served over HTTPS. Certificate should be valid. No mixed content (HTTP resources on HTTPS page).",
            category="Security", priority="High", section="Security",
        ),
        TCTemplate(
            summary="Verify that the page meets basic accessibility standards (keyboard navigation, alt text)",
            preconditions="Application is accessible.",
            steps=["Navigate the page using only keyboard (Tab, Enter, Escape)",
                   "Verify all interactive elements are reachable via Tab",
                   "Verify focus indicators are visible on focused elements",
                   "Verify all images have descriptive alt text"],
            test_data="",
            expected_result="Page should be fully navigable via keyboard. Focus indicators should be visible. All images should have alt text.",
            category="Positive", priority="Medium", section="Accessibility",
        ),
    ]


def _generic_test_cases(action: str, original: str, section: str = "General") -> list[TCTemplate]:
    """Generate positive/negative/edge test cases for one story action."""
    short_action = action[:80].rstrip(".")
    return [
        TCTemplate(
            summary=f"Verify that {short_action} is functioning as expected",
            preconditions="System is running. User is authenticated (if applicable).",
            steps=["Navigate to the relevant page/feature",
                   f"Perform the action: {short_action}",
                   "Observe the result"],
            test_data="Valid input data",
            expected_result=f"The feature should be functioning as specified. Expected behavior should be observed.",
            category="Positive", priority="High", section=section,
        ),
        TCTemplate(
            summary=f"Verify that {short_action} is rejected with invalid input",
            preconditions="System is running. Feature is accessible.",
            steps=["Navigate to the relevant page/feature",
                   "Provide invalid or missing input data",
                   "Attempt to perform the action",
                   "Observe the error handling"],
            test_data="Invalid/empty input data",
            expected_result="Invalid input should be rejected. User-friendly error message should be displayed. No data corruption should occur.",
            category="Negative", priority="High", section=section,
        ),
        TCTemplate(
            summary=f"Verify that {short_action} returns the expected outcome per the spec with boundary values",
            preconditions="System is running. Feature is accessible.",
            steps=["Test with minimum allowed input values",
                   "Test with maximum allowed input values",
                   "Test with special characters and Unicode",
                   "Test with empty/null values"],
            test_data="Min: 1 char, Max: max length, Special: !@#$%^&*(), Empty: ''",
            expected_result="Min and max boundary values are accepted; min-1 and max+1 are rejected with a field-specific message. No errors or data corruption should occur.",
            category="Edge Case", priority="Medium", section=section,
        ),
    ]


def _ac_to_test_case(criterion: str, action: str, section: str) -> TCTemplate:
    """Convert one acceptance criterion into a concrete test case."""
    short_cr = criterion[:120].rstrip(".")
    short_action = action[:60].rstrip(".")
    return TCTemplate(
        summary=f"Verify that {short_cr.lower() if short_cr[0:1].isupper() else short_cr}",
        preconditions=f"Feature '{short_action}' is accessible. User is authenticated (if applicable).",
        steps=["Navigate to the feature under test",
               f"Perform the action: {short_action}",
               f"Validate criterion: {short_cr}"],
        test_data="Valid data matching the criterion",
        expected_result=f"{short_cr}. The system should behave as specified.",
        category="Positive", priority="High", section=section,
    )


def _ac_negative_test_case(criterion: str, action: str, section: str) -> TCTemplate:
    """Generate a negative test case for an acceptance criterion.

    Phrasing rules:
      * If the criterion already negates ("cannot", "not allowed",
        "no errors", ...) we phrase the negative TC as an attempt to
        bypass that restriction — so the summary stays grammatical
        ("Verify that the system blocks attempts to access protected
        resources by unauthorized users") instead of producing the
        contradictory "Verify that violation of 'unauthorized users
        cannot ...' returns the expected outcome per the spec".
      * Otherwise we phrase it as the negation of the positive
        criterion ("Verify that the system rejects input that violates
        '<criterion>'").
    """
    short_cr = criterion[:120].rstrip(".")
    short_action = action[:60].rstrip(".")

    # Detect criteria that already negate — they need different phrasing.
    cr_lower = short_cr.lower()
    NEG_TOKENS = (" cannot ", " can't ", " not allowed", " not permitted",
                  " is not ", " are not ", " has no ", " have no ",
                  " never ", " without ", " no error", " no console")
    already_negative = any(tok in (" " + cr_lower + " ") for tok in NEG_TOKENS)

    if already_negative:
        summary = (f"Verify that the system enforces '{short_cr[:70]}' "
                   f"and blocks attempts to bypass this restriction")
        steps = ["Navigate to the feature under test",
                 f"Attempt the action that the spec forbids: {short_action}",
                 "Observe how the system responds"]
        expected = (f"The system should enforce the restriction. The action "
                    f"should be blocked with a clear, user-facing error.")
    else:
        summary = (f"Verify that the system rejects input that violates "
                   f"'{short_cr[:70]}'")
        steps = ["Navigate to the feature under test",
                 f"Provide input/state that contradicts: {short_cr}",
                 "Observe error handling and system behavior"]
        expected = ("The system should reject the invalid input gracefully. "
                    "An appropriate, user-facing error message should be "
                    "displayed; no data should be persisted.")

    return TCTemplate(
        summary=summary,
        preconditions=f"Feature '{short_action}' is accessible.",
        steps=steps,
        test_data="Invalid/edge-case data contradicting the criterion",
        expected_result=expected,
        category="Negative", priority="Medium", section=section,
    )


def _story_test_cases(story, section: str) -> list[TCTemplate]:
    """Generate test cases for a single user story, including per-AC coverage.

    Strategy:
      1. One positive TC per acceptance criterion (each AC = a concrete check)
      2. One negative TC per AC that mentions validation/input/auth/error/data
      3. One edge-case TC for the overall story action
    This ensures every AC is covered by at least 1 positive TC.
    """
    cases: list[TCTemplate] = []
    action = story.action
    criteria = getattr(story, "acceptance_criteria", []) or []

    if criteria:
        # Positive TC for each acceptance criterion
        for ac in criteria:
            cases.append(_ac_to_test_case(ac, action, section))

        # Negative TCs for ACs that involve validation / input / data / auth
        _NEG_SIGNALS = re.compile(
            r"valid|input|field|required|error|reject|password|auth|permission"
            r"|format|length|empty|null|data|persisted|database|encrypt",
            re.IGNORECASE,
        )
        for ac in criteria:
            if _NEG_SIGNALS.search(ac):
                cases.append(_ac_negative_test_case(ac, action, section))

        # One edge-case TC for the overall action
        short_action = action[:80].rstrip(".")
        cases.append(TCTemplate(
            summary=f"Verify that {short_action} returns the expected outcome per the spec with boundary values",
            preconditions="System is running. Feature is accessible.",
            steps=["Test with minimum allowed input values",
                   "Test with maximum allowed input values",
                   "Test with special characters and Unicode",
                   "Test with empty/null values"],
            test_data="Min: 1 char, Max: max length, Special: !@#$%^&*(), Empty: ''",
            expected_result="Min and max boundary values are accepted; min-1 and max+1 are rejected with a field-specific message. No errors or data corruption should occur.",
            category="Edge Case", priority="Medium", section=section,
        ))
    else:
        # No acceptance criteria — fall back to generic 3 TCs
        cases.extend(_generic_test_cases(action, story.original_text, section))

    return cases


_AREA_TC_FN: dict[str, callable] = {
    "web_general": _web_general_test_cases,
    "auth":        _auth_test_cases,
    "search":      _search_test_cases,
    "forms":       _forms_test_cases,
    "payment":     _payment_test_cases,
    "navigation":  _navigation_test_cases,
}


# ═══════════════════════════════════════════════════════════════════
# 3b. Site-specific (per-page) test-case generation
# ═══════════════════════════════════════════════════════════════════
#
# Generic baseline TCs (Forms / Navigation / HTTPS / Responsive) are
# applicable but identical across products. To make TestForTge output
# meaningful for a specific URL, this section emits TCs derived from
# what the crawler actually saw on each page: the H1, the headings,
# button labels, nav items, real form fields, presence of video etc.

def _slugify_section(label: str) -> str:
    """Build a stable section name from a page label (H1 or path)."""
    label = (label or "").strip()
    # Drop trailing site-name suffix common in <title> ("News - football.ua")
    label = re.sub(r"\s*[\|\-–——]\s*[^|\-–——]+$", "", label)
    label = label[:60].rstrip(" .|-—")
    return label or "Page"


def _path_label(url: str) -> str:
    """Turn a URL into a short user-facing label (the path or domain)."""
    m = re.match(r"https?://([^/]+)(/.*)?", url)
    if not m:
        return url[:40]
    host, path = m.group(1), (m.group(2) or "/")
    label = path.strip("/").replace("/", " > ") or host
    return label.replace("-", " ").replace("_", " ")[:60]


def _form_label(form: dict) -> str:
    """Describe a form by its action or fields when no other anchor exists."""
    action = form.get("action") or ""
    if action:
        a = re.sub(r"^https?://[^/]+", "", action).strip("/") or "root"
        return f"form @ /{a}"
    names = [(f.get("name") or f.get("placeholder") or "").strip()
             for f in (form.get("fields") or [])]
    names = [n for n in names if n][:3]
    return f"form ({', '.join(names) or '?'})"


def _seo_test_cases() -> list[TCTemplate]:
    """SEO baseline applicable to any public web product."""
    return [
        TCTemplate(
            summary="Verify that every public page has a unique, non-empty <title> under 60 characters",
            preconditions="The site is reachable and indexable.",
            steps=[
                "Visit the homepage and at least 3 representative inner pages",
                "Inspect <head><title> via DevTools or `view-source:`",
                "Verify each title is non-empty, unique across pages, and <= 60 characters",
            ],
            test_data="Pages: /, /about, /contact, /<feature>",
            expected_result="Every audited page has its own descriptive <title>, none are duplicated, none exceed 60 chars.",
            category="Positive", priority="High", section="SEO",
            testing_type="SEO",
        ),
        TCTemplate(
            summary="Verify that every page has a meta description (50-160 chars) and canonical URL",
            preconditions="Site is reachable.",
            steps=[
                "View page source on the homepage and 3 representative inner pages",
                "Confirm <meta name=\"description\"> exists with 50-160 chars",
                "Confirm <link rel=\"canonical\"> points to the page's primary URL",
            ],
            test_data="HTML head of each audited page",
            expected_result="Each page declares a meaningful meta description in the 50-160-char range and a canonical link pointing to itself (or its preferred variant).",
            category="Positive", priority="High", section="SEO",
            testing_type="SEO",
        ),
        TCTemplate(
            summary="Verify that robots.txt and sitemap.xml are reachable and consistent",
            preconditions="Site is reachable.",
            steps=[
                "Open /robots.txt — verify HTTP 200 and that it references a Sitemap directive",
                "Open the Sitemap URL — verify HTTP 200 and valid XML",
                "Spot-check 3 URLs from the sitemap return HTTP 200",
            ],
            test_data="/robots.txt, /sitemap.xml",
            expected_result="Both files return HTTP 200. robots.txt declares the sitemap. Sample sitemap URLs resolve without 4xx/5xx.",
            category="Positive", priority="Medium", section="SEO",
            testing_type="SEO",
        ),
        TCTemplate(
            summary="Verify that Open Graph and Twitter Card meta tags are present on shareable pages",
            preconditions="Site is reachable.",
            steps=[
                "Open the homepage and one content/article page",
                "Verify presence of og:title, og:description, og:image, og:url",
                "Verify presence of twitter:card, twitter:title, twitter:description",
                "Use a card validator (e.g. opengraph.xyz) and confirm preview renders",
            ],
            test_data="Homepage URL + one content URL",
            expected_result="Both pages render a clean Open Graph + Twitter Card preview when shared on Slack/Telegram/X. og:image loads and is at least 1200×630.",
            category="Positive", priority="Medium", section="SEO",
            testing_type="SEO",
        ),
        TCTemplate(
            summary="Verify that all images on the homepage have meaningful alt attributes",
            preconditions="Homepage is reachable.",
            steps=["Open the homepage",
                   "Run an a11y/SEO audit (Lighthouse or `document.querySelectorAll('img:not([alt]), img[alt=\"\"]')`)",
                   "Inspect every <img> for an alt attribute"],
            test_data="Homepage <img> elements",
            expected_result="Every <img> has an alt attribute. Decorative images may use alt=\"\" but the attribute itself is present. No img is missing alt.",
            category="Positive", priority="Medium", section="SEO",
            testing_type="SEO",
        ),
    ]


def _usability_test_cases() -> list[TCTemplate]:
    """Heuristic usability baseline applicable to any web product."""
    return [
        TCTemplate(
            summary="Verify that the primary call-to-action on the homepage is visible above the fold",
            preconditions="Homepage is reachable in a 1280×800 viewport.",
            steps=["Open the homepage at 1280×800",
                   "Confirm the primary CTA (e.g. Sign up / Buy / Start) is visible without scrolling",
                   "Confirm it has sufficient colour contrast and a clearly clickable affordance"],
            test_data="Viewport: 1280×800",
            expected_result="The primary CTA is visible above the fold, has a contrasting background, and is recognisable as a clickable control on first glance.",
            category="Positive", priority="High", section="Usability",
            testing_type="Usability",
        ),
        TCTemplate(
            summary="Verify that body text uses a readable font size (>= 14 px) and adequate line-height",
            preconditions="Any content page is loaded at 1280×800.",
            steps=["Open a content-rich page",
                   "Inspect computed font-size and line-height of the main body text",
                   "Verify body font-size is at least 14 px and line-height is between 1.4 and 1.7"],
            test_data="Body paragraphs",
            expected_result="Body text reads cleanly at default zoom: font-size >= 14 px, line-height 1.4-1.7. No paragraphs use < 12 px.",
            category="Positive", priority="Medium", section="Usability",
            testing_type="Usability",
        ),
        TCTemplate(
            summary="Verify that interactive controls have visible hover and focus states",
            preconditions="Any page with multiple links / buttons is loaded.",
            steps=["Hover over each navigation link, button and interactive icon",
                   "Tab through the same controls using only the keyboard",
                   "Confirm both states are visually distinct from the default state"],
            test_data="Top nav, primary CTA, footer links",
            expected_result="Every interactive element has a clearly different appearance on hover and on keyboard focus. No control silently absorbs focus.",
            category="Positive", priority="Medium", section="Usability",
            testing_type="Usability",
        ),
        TCTemplate(
            summary="Verify that error and empty states are user-friendly, not raw stack traces",
            preconditions="Application is reachable.",
            steps=["Trigger a known-bad URL (404)",
                   "Trigger a form submission with invalid input",
                   "If the app exposes search, search for nonsense to hit an empty-state"],
            test_data="/this-does-not-exist-123, malformed form input, query: ‘zzzqqqxxx’",
            expected_result="Each error / empty state is rendered with a friendly message and a recovery CTA (link home, retry, contact). No raw 500 page or stack trace leaks.",
            category="Negative", priority="High", section="Usability",
            testing_type="Usability",
        ),
    ]


def _localization_test_cases(analysis: "AnalysisResult") -> list[TCTemplate]:
    """Localization checks — emitted when the site exposes multi-language UI."""
    pages = analysis.site_pages or []
    multi_lang = False
    for p in pages:
        nav_blob = " ".join(p.get("nav_links") or []).lower()
        title_blob = (p.get("title") or "").lower()
        if any(t in nav_blob for t in ("ua", "укр", "рус", "eng", "english", "deutsch", "polski", "español")):
            multi_lang = True
            break
        # Mixed Cyrillic + Latin signals likely multi-language site
        if re.search(r"[а-яіїєґ]", title_blob) and re.search(r"[a-z]{3,}", title_blob):
            multi_lang = True
            break
    if not pages or not multi_lang:
        return []
    return [
        TCTemplate(
            summary="Verify that the language switcher changes UI strings without breaking the layout",
            preconditions=f"Site is reachable at {analysis.url}.",
            steps=["Open the homepage in the default language",
                   "Use the language switcher (header / footer) to toggle to each available language",
                   "Verify visible UI strings change consistently",
                   "Verify no layout breaks (text overflow, clipped buttons, broken alignment)"],
            test_data="All language toggles exposed in nav/footer",
            expected_result="Switching language replaces the visible UI strings end-to-end. The layout adapts to longer translations without overflow or clipping. The selected language persists on subsequent navigation.",
            category="Positive", priority="High", section="Localization",
            testing_type="Localization",
        ),
        TCTemplate(
            summary="Verify that html[lang] reflects the active locale and matches the rendered text",
            preconditions="Site supports more than one language.",
            steps=["Switch to each available language",
                   "Open DevTools and inspect the <html lang=\"…\"> attribute",
                   "Confirm it changes per locale (e.g. uk, en, ru, pl)"],
            test_data="Locale toggles + DevTools",
            expected_result="The lang attribute on <html> changes to the active locale on every switch. Screen readers and browsers receive the correct language hint.",
            category="Positive", priority="Medium", section="Localization",
            testing_type="Localization",
        ),
    ]


def _site_specific_test_cases(analysis: "AnalysisResult") -> list[TCTemplate]:
    """Generate test cases anchored in actual crawled page data.

    Strategy
    --------
    * Per page: 1 content-render TC (title + H1 + main sections).
    * Per page: 1 internal-link health TC if it links out to many pages.
    * If page has buttons → 1 button-action TC listing real labels.
    * If page has video → 1 video-playback TC (per page that actually has it).
    * If page has a real business form → field-grounded form submission
      TC instead of the abstract "fill all fields" template.
    * One global "primary navigation" TC listing the actual top-nav items.
    * One global search relevance TC (only when search is detected).
    """
    cases: list[TCTemplate] = []
    pages = analysis.site_pages or []
    if not pages:
        return cases

    # 1) Site-wide primary-navigation TC using real top-nav labels
    top_nav = []
    seen = set()
    for p in pages:
        for n in (p.get("nav_links") or [])[:6]:
            n_norm = n.strip().lower()
            if n_norm and n_norm not in seen and len(n) <= 40:
                seen.add(n_norm)
                top_nav.append(n.strip())
            if len(top_nav) >= 8:
                break
        if len(top_nav) >= 8:
            break
    if top_nav:
        nav_str = " · ".join(top_nav)
        cases.append(TCTemplate(
            summary=f"Verify that the primary navigation exposes the documented sections: {nav_str[:90]}",
            preconditions=f"Site is reachable at {analysis.url}.",
            steps=[
                "Open the homepage in a browser",
                "Locate the primary navigation (top bar / header)",
                f"Confirm the following items are present and clickable: {nav_str[:200]}",
                "Click each item one by one and verify the URL changes and the destination renders the matching heading",
            ],
            test_data=f"Navigation labels observed by crawler: {nav_str[:240]}",
            expected_result=(
                "Every documented navigation item is rendered, clickable and "
                "leads to a route whose page heading matches its label. No 404 "
                "or empty-state pages on any of the items."
            ),
            category="Positive", priority="High", section="Site Navigation",
        ))

    # 2) Per-page content & UI test cases
    MAX_PAGES = 8  # cap so a 200-page site doesn't drown the spreadsheet
    for p in pages[:MAX_PAGES]:
        url = p.get("url", "")
        title = p.get("title") or p.get("h1") or _path_label(url)
        h1 = p.get("h1") or ""
        headings = p.get("headings") or []
        buttons = p.get("buttons") or []
        forms = p.get("forms") or []
        has_video = p.get("has_video")
        path_label = _path_label(url)
        section = f"Page: {_slugify_section(title or path_label)}"

        # 2a — content render TC referencing the real H1 + sections
        section_list = ", ".join(h for h in headings[:3]) if headings else ""
        content_steps = [f"Open the URL: {url}",
                         "Wait for the page to fully load (DOMContentLoaded + main images)"]
        if h1:
            content_steps.append(f"Verify the visible H1 reads: \"{h1[:80]}\"")
        if section_list:
            content_steps.append(f"Verify the page renders the following sections: {section_list[:150]}")
        content_steps.append("Open DevTools Console — confirm there are no JavaScript errors")
        cases.append(TCTemplate(
            summary=f"Verify that {path_label} renders its primary content as observed by the crawler",
            preconditions=f"User can reach {url} from a fresh browser session.",
            steps=content_steps,
            test_data=f"URL: {url}",
            expected_result=(
                f"The page loads with its declared title (\"{title[:80]}\") and "
                f"H1 (\"{h1[:80] or '—'}\"). All observed sections are visible. "
                f"No JavaScript errors are emitted on first paint."
            ),
            category="Positive", priority="High", section=section,
        ))

        # 2b — interactive button TC (only when there are real buttons)
        if buttons:
            real_btns = [b for b in buttons
                         if 2 <= len(b.strip()) <= 40
                         and b.strip().lower() not in {"×", "x", "ok", "?", "close"}]
            real_btns = real_btns[:5]
            if real_btns:
                btns_str = ", ".join(f'"{b}"' for b in real_btns)
                cases.append(TCTemplate(
                    summary=f"Verify that the interactive controls on {path_label} respond to user clicks",
                    preconditions=f"{url} is loaded.",
                    steps=[f"Open {url}",
                           f"Locate each of the following controls: {btns_str}",
                           "Click each control in turn and observe the resulting UI state (modal opens, content loads, navigation occurs, etc.)",
                           "Confirm no JavaScript errors appear in DevTools Console"],
                    test_data=f"Button labels seen on the page: {btns_str}",
                    expected_result=(
                        "Every listed control either navigates the user to "
                        "the expected destination, opens its associated dialog "
                        "or triggers its documented action without console errors."
                    ),
                    category="Positive", priority="Medium", section=section,
                ))

        # 2c — video TC (only when a video element was actually present)
        if has_video:
            cases.append(TCTemplate(
                summary=f"Verify that embedded video on {path_label} loads and plays",
                preconditions=f"{url} is loaded in a browser with audio/video enabled.",
                steps=[f"Open {url}",
                       "Locate the embedded video player",
                       "Click the play control",
                       "Verify the video starts playing within 3 seconds",
                       "Verify pause / mute / fullscreen controls respond"],
                test_data=f"Page: {url}",
                expected_result="Video starts playing within 3 s of clicking play; pause/mute/fullscreen behave as expected; no media errors in console.",
                category="Positive", priority="Medium", section=section,
            ))

        # 2d — form TC grounded in real fields, instead of the abstract template
        for form in forms:
            real_fields = [f for f in (form.get("fields") or [])
                           if f.get("type") not in ("hidden", "submit", "button")]
            if not real_fields:
                continue
            named = [(f.get("name") or f.get("placeholder") or f.get("type", "field")) for f in real_fields]
            named = [str(n) for n in named if n][:6]
            if not named:
                continue
            has_password = any(f.get("type") == "password" for f in real_fields)
            label = _form_label(form)
            if has_password:
                # Treat as login/registration form
                summary = f"Verify that submitting the {label} on {path_label} with valid credentials succeeds"
                steps = [f"Open {url}",
                         f"Fill the form fields ({', '.join(named)}) with valid values",
                         "Click the submit / sign-in button",
                         "Observe the response — successful auth should redirect or unlock content"]
                expected = "Form submits successfully, the server returns 2xx and the user is redirected to the post-auth page or sees a success state."
                category = "Positive"
            else:
                summary = f"Verify that the {label} on {path_label} accepts valid input and submits cleanly"
                steps = [f"Open {url}",
                         f"Fill the fields ({', '.join(named)}) with valid values",
                         "Submit the form",
                         "Observe network response and UI confirmation"]
                expected = "Form submits successfully (HTTP 2xx). UI shows confirmation state. No console errors."
                category = "Positive"
            cases.append(TCTemplate(
                summary=summary[:120],
                preconditions=f"{url} is reachable; the form '{label}' is rendered on the page.",
                steps=steps,
                test_data=f"Fields observed: {', '.join(named)}",
                expected_result=expected,
                category=category, priority="High", section=section,
            ))
            # Negative TC for this specific form
            cases.append(TCTemplate(
                summary=f"Verify that the {label} on {path_label} rejects empty / malformed input"[:120],
                preconditions=f"{url} is reachable; the form '{label}' is rendered.",
                steps=[f"Open {url}",
                       "Submit the form with all fields empty",
                       f"Submit the form with malformed values (e.g. invalid email, mismatched password) for: {', '.join(named[:3])}",
                       "Inspect the rendered validation messages and HTTP responses"],
                test_data=f"Empty values; malformed values for: {', '.join(named[:3])}",
                expected_result="Each invalid attempt is blocked client- or server-side with a field-specific error message. No partial write reaches the backing store.",
                category="Negative", priority="High", section=section,
            ))
            break  # one form per page is enough — avoid duplicates

    # 3) Site-search relevance (only when crawler actually saw a search input)
    if "search" in (analysis.areas or []) and pages:
        # Pick a topical seed query from the site's own headings
        topical = ""
        for p in pages:
            for h in (p.get("headings") or [])[:3]:
                if 3 <= len(h.split()) <= 6:
                    topical = h
                    break
            if topical:
                break
        seed = topical or (pages[0].get("h1") or pages[0].get("title") or "news")
        cases.append(TCTemplate(
            summary=f"Verify that the on-site search returns results relevant to a topical query (\"{seed[:60]}\")",
            preconditions=f"Site search is reachable from {analysis.url}.",
            steps=["Open the homepage",
                   "Click the search input / icon",
                   f"Submit the query: \"{seed[:80]}\"",
                   "Inspect the result list and the first three items"],
            test_data=f"Query: {seed[:120]}",
            expected_result=(
                "Search returns at least one result. The first three results "
                "are topically relevant to the query and link to existing pages "
                "(no 404). An empty-state message is rendered when the query "
                "yields no matches."
            ),
            category="Positive", priority="High", section="Site Search",
        ))

    return cases


# ═══════════════════════════════════════════════════════════════════
# 3c. Named flow generators (end-to-end playbooks)
# ═══════════════════════════════════════════════════════════════════

_FLOW_CATEGORY_BY_PHASE = {
    "Edge / Negative": "Edge Case",
    "Security & Compliance": "Security",
}


def _flow_test_cases(flow_key: str) -> list[TCTemplate]:
    """Expand one flow playbook into concrete ``TCTemplate`` items.

    Each required check becomes a test case; the section is ``{Flow
    Name} — {Phase}`` so the generated test cases cluster nicely in
    TestFort exports.
    """
    try:
        from .knowledge_base import FLOW_PLAYBOOKS
    except Exception:  # pragma: no cover
        return []
    pb = FLOW_PLAYBOOKS.get(flow_key)
    if not pb:
        return []

    cases: list[TCTemplate] = []
    flow_label = pb.get("name", flow_key)
    test_data_ref = pb.get("test_data_reference", {}) or {}
    ref_text = ", ".join(f"{k}: {v}" for k, v in list(test_data_ref.items())[:3])

    for phase_block in pb.get("required_phases", []):
        phase = phase_block.get("phase", "Flow")
        section = f"{flow_label} — {phase}"
        default_cat = _FLOW_CATEGORY_BY_PHASE.get(phase, "Positive")
        is_security_phase = phase == "Security & Compliance"

        for raw in phase_block.get("checks", []):
            summary = raw if raw.lower().startswith("verify that") else f"Verify that {raw}"
            # Security-phase items are always Security — regardless of wording.
            # Otherwise, infer category from keywords.
            low = raw.lower()
            if is_security_phase:
                category = "Security"
            elif any(k in low for k in ("invalid", "rejected", "declined", "does not", "does not create",
                                      "cannot", "error", "empty state")):
                category = "Negative"
            elif any(k in low for k in ("timeout", "duplicate", "parallel", "edge", "very large",
                                         "mid-flow", "mid-checkout", "refresh", "back button",
                                         "double-click", "double-submit", "double-charge")):
                category = "Edge Case"
            elif any(k in low for k in ("https", "pci dss", "gdpr", "authorization",
                                         "not logged", "pan", "tokenized", "xss", "csrf")):
                category = "Security"
            else:
                category = default_cat

            cases.append(TCTemplate(
                summary=summary,
                preconditions=(
                    f"The application under test exposes the checkout flow. "
                    f"Test account and test products are prepared."
                ),
                steps=[
                    f"Reach the '{phase}' step of the checkout flow",
                    f"Perform the check: {raw}",
                    "Observe the system state, UI messaging and any emails/notifications",
                ],
                test_data=ref_text if category != "Security" else "",
                expected_result=f"{raw}. The system should behave as stated without data loss or double-charging.",
                category=category,
                priority="High",
                section=section,
            ))
    return cases


def _flow_checks(flow_key: str) -> list[CheckItem]:
    """Expand one flow playbook into concrete ``CheckItem`` items."""
    try:
        from .knowledge_base import FLOW_PLAYBOOKS
    except Exception:  # pragma: no cover
        return []
    pb = FLOW_PLAYBOOKS.get(flow_key)
    if not pb:
        return []

    items: list[CheckItem] = []
    flow_label = pb.get("name", flow_key)
    for phase_block in pb.get("required_phases", []):
        phase = phase_block.get("phase", "Flow")
        section = f"{flow_label} — {phase}"
        default_cat = _FLOW_CATEGORY_BY_PHASE.get(phase, "Positive")
        is_security_phase = phase == "Security & Compliance"
        for raw in phase_block.get("checks", []):
            objective = raw if raw.lower().startswith("verify that") else f"Verify that {raw}"
            low = raw.lower()
            if is_security_phase:
                category = "Security"
            elif any(k in low for k in ("invalid", "rejected", "declined", "does not", "cannot",
                                      "error", "empty state")):
                category = "Negative"
            elif any(k in low for k in ("timeout", "duplicate", "parallel", "edge", "very large",
                                         "refresh", "back button", "double-click",
                                         "double-submit", "double-charge", "mid-flow",
                                         "mid-checkout")):
                category = "Edge Case"
            elif any(k in low for k in ("https", "pci dss", "gdpr",
                                         "authorization", "not logged",
                                         "pan", "tokenized")):
                category = "Security"
            else:
                category = default_cat
            items.append(CheckItem(
                objective=objective,
                category=category,
                priority="High",
                section=section,
            ))
    return items

# Section name for stories that match a known area
_AREA_SECTION: dict[str, str] = {
    "auth": "Authentication", "search": "Search", "forms": "Forms",
    "payment": "Payment", "navigation": "Navigation",
    "crud_create": "Data Management", "crud_read": "Data Management",
    "crud_update": "Data Management", "crud_delete": "Data Management",
    "upload": "File Management", "export": "Export",
    "notification": "Notifications", "api": "API",
    "responsive": "Responsive", "performance": "Performance",
    "security": "Security", "accessibility": "Accessibility",
}


def generate_professional_test_cases(analysis: AnalysisResult,
                                     stories: list | None = None,
                                     custom_prompt: str = "") -> list[TCTemplate]:
    """Generate professional test cases based on analysis.

    Strategy:
      1. Area-level baseline TCs from knowledge base (auth, search, etc.)
      2. Per-story TCs: each user story gets test cases derived from its
         acceptance criteria (1 positive per AC + negatives + edge cases).
         Stories are NEVER skipped — area TCs are supplementary, not a substitute.
    """
    cases: list[TCTemplate] = []

    # 1) Site-specific test cases derived from real crawler data — these
    #    reference the actual H1s, sections, buttons, forms and nav items
    #    that the crawler observed on the URL. Generated FIRST so that
    #    when they cover an area (navigation / forms) we can suppress the
    #    duplicated generic baseline below.
    site_cases = _site_specific_test_cases(analysis)
    cases.extend(site_cases)
    site_covers_navigation = any(c.section == "Site Navigation" for c in site_cases)
    site_covers_forms      = any(c.section.startswith("Page: ") and "form" in c.summary.lower()
                                  for c in site_cases)
    site_covers_search     = any(c.section == "Site Search" for c in site_cases)

    # 2) Area-specific generic baselines — emitted only when the site
    #    didn't already get site-specific coverage for that area. This
    #    prevents the spreadsheet from carrying both a "verify that the
    #    form accepts valid input" generic case and a site-specific
    #    "verify that the login form on /sign-in accepts valid input"
    #    for the same form.
    for area in analysis.areas:
        if area == "navigation" and site_covers_navigation:
            continue
        if area == "forms" and site_covers_forms:
            continue
        if area == "search" and site_covers_search:
            continue
        fn = _AREA_TC_FN.get(area)
        if fn:
            cases.extend(fn())

    # 3) Non-functional baselines that apply to any public web product
    #    when a URL is in scope. These cover the most common testing
    #    types beyond "functional" so the default output is genuine
    #    regression scope (functional + smoke + perf + security +
    #    accessibility + SEO + usability + localization). Custom-prompt
    #    narrowing (see ``generate_test_cases``) can later filter the
    #    set down to specific testing types if the user asked for a
    #    narrower scope.
    if analysis.url:
        cases.extend(_seo_test_cases())
        cases.extend(_usability_test_cases())
        cases.extend(_localization_test_cases(analysis))

    # Named-flow playbooks (e.g. "checkout_flow") — expanded per-phase
    for flow_key in getattr(analysis, "flows", []) or []:
        cases.extend(_flow_test_cases(flow_key))

    # Per-story test cases — EVERY story gets coverage
    if stories:
        from .user_story_generator import UserStory
        for story in stories:
            if not isinstance(story, UserStory):
                continue

            # Determine section name from story area
            story_area = _detect_area_for_text(story.original_text)
            section = _AREA_SECTION.get(story_area, "General")

            # Generate TCs from acceptance criteria
            cases.extend(_story_test_cases(story, section))

    # ── Browser findings → test cases ────────────────────────────
    if analysis.browser_findings:
        cases.extend(_browser_findings_to_test_cases(analysis.browser_findings))

    # ── Filter SQL injection for URL-based testing ──────────────
    if analysis.url:
        cases = [c for c in cases if "sql injection" not in c.summary.lower()
                 and "sql" not in c.summary.lower().split("inject")[0:1]]

    return cases


def _detect_area_for_text(text: str) -> str | None:
    """Detect the primary area for a piece of text."""
    lower = text.lower()
    for area, keywords in _AREA_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                return area
    return None
