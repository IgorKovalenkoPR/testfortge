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
            browser_report = browser_get_or_run(
                result.url, max_pages=10,
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
            ("Verify that company logo is displayed on the page", "Positive", "High"),
            ("Verify that company logo is redirected to the homepage when clicked", "Positive", "Medium"),
            ("Verify that main navigation menu is displayed with all expected items", "Positive", "High"),
            ("Verify that navigation links are redirected to correct pages", "Positive", "High"),
            ("Verify that the active/current page is highlighted in the navigation", "Positive", "Medium"),
            ("Verify that dropdown/submenu items are displayed on hover or click", "Positive", "Medium"),
            ("Verify that navigation is collapsed into a hamburger menu on mobile viewport", "Positive", "High"),
            ("Verify that the hamburger menu is opened and closed correctly", "Positive", "Medium"),
            ("Verify that the page title is displayed correctly in the browser tab", "Positive", "Medium"),
            ("Verify that breadcrumb navigation is displayed and functional (if applicable)", "Positive", "Low"),
        ],
        "Content & Layout": [
            ("Verify that the page heading (H1) is displayed correctly", "Positive", "High"),
            ("Verify that all text content is readable and properly formatted", "Positive", "High"),
            ("Verify that all images are loaded without broken links", "Positive", "High"),
            ("Verify that images have appropriate alt attributes", "Positive", "Medium"),
            ("Verify that CTA (Call to Action) buttons are displayed and clickable", "Positive", "High"),
            ("Verify that content sections are displayed in the correct order", "Positive", "Medium"),
            ("Verify that no placeholder/Lorem Ipsum text is present", "Negative", "Medium"),
            ("Verify that no overlapping or misaligned elements are present", "Negative", "Medium"),
            ("Verify that text truncation is handled gracefully with ellipsis or expansion", "Edge Case", "Low"),
        ],
        "Links & Media": [
            ("Verify that all internal links are navigated to correct pages", "Positive", "High"),
            ("Verify that all external links are opened in a new tab", "Positive", "Medium"),
            ("Verify that no broken links (404) are present on the page", "Negative", "High"),
            ("Verify that video/audio elements are played correctly (if present)", "Positive", "Medium"),
            ("Verify that social media links are redirected to correct profiles", "Positive", "Medium"),
            ("Verify that email links (mailto:) are opened in the default email client", "Positive", "Low"),
            ("Verify that phone links (tel:) are initiated a call on mobile devices", "Positive", "Low"),
        ],
        "Footer": [
            ("Verify that footer is displayed at the bottom of the page", "Positive", "Medium"),
            ("Verify that footer links are functional and redirected to correct pages", "Positive", "Medium"),
            ("Verify that copyright information is displayed and up to date", "Positive", "Low"),
            ("Verify that footer is displayed consistently across all pages", "Positive", "Low"),
        ],
        "Forms & Input": [
            ("Verify that all form fields are displayed with correct labels", "Positive", "High"),
            ("Verify that required fields are marked with an asterisk or indicator", "Positive", "Medium"),
            ("Verify that form is submitted successfully with all valid data", "Positive", "High"),
            ("Verify that validation errors are displayed when required fields are left empty", "Negative", "High"),
            ("Verify that email field is rejected with invalid format (e.g. 'test@', '@domain')", "Negative", "High"),
            ("Verify that phone field is rejected with non-numeric characters", "Negative", "Medium"),
            ("Verify that success confirmation is displayed after form submission", "Positive", "High"),
            ("Verify that form data is not lost when validation error occurs", "Negative", "Medium"),
            ("Verify that form fields are accepted with maximum length input", "Edge Case", "Medium"),
            ("Verify that form fields are accepted with minimum length input", "Edge Case", "Medium"),
            ("Verify that special characters are handled correctly in text fields", "Edge Case", "Medium"),
            ("Verify that XSS payloads are sanitized in all input fields", "Security", "High"),
            ("Verify that SQL injection is blocked in all input fields", "Security", "High"),
        ],
        "Responsive Design": [
            ("Verify that page layout is adapted correctly for mobile viewport (375px)", "Positive", "High"),
            ("Verify that page layout is adapted correctly for tablet viewport (768px)", "Positive", "High"),
            ("Verify that page layout is adapted correctly for desktop viewport (1280px+)", "Positive", "High"),
            ("Verify that images are scaled proportionally on different screen sizes", "Positive", "Medium"),
            ("Verify that no horizontal scrollbar is appeared on any viewport", "Negative", "High"),
            ("Verify that text is readable without zooming on mobile devices", "Positive", "Medium"),
            ("Verify that touch targets (buttons, links) are at least 44x44px on mobile", "Positive", "Medium"),
            ("Verify that the page orientation change (portrait/landscape) is handled correctly", "Edge Case", "Low"),
        ],
        "Cross-browser Compatibility": [
            ("Verify that the page is rendered correctly in Google Chrome (latest)", "Positive", "High"),
            ("Verify that the page is rendered correctly in Mozilla Firefox (latest)", "Positive", "High"),
            ("Verify that the page is rendered correctly in Safari (latest)", "Positive", "Medium"),
            ("Verify that the page is rendered correctly in Microsoft Edge (latest)", "Positive", "Medium"),
        ],
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
            ("Verify that ARIA roles and landmarks are used correctly", "Positive", "Low"),
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
            ("Verify that heading hierarchy is correct (single H1, logical H2-H6)", "Positive", "Low"),
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
            ("Verify that the username/avatar is displayed in the header after login", "Positive", "Medium"),
            ("Verify that 'Remember me' is extended the session beyond browser close", "Positive", "Medium"),
        ],
        "Login — Negative": [
            ("Verify that login is rejected with incorrect password", "Negative", "High"),
            ("Verify that login is rejected with non-existent email", "Negative", "High"),
            ("Verify that login is rejected when email field is left empty", "Negative", "High"),
            ("Verify that login is rejected when password field is left empty", "Negative", "High"),
            ("Verify that the error message does not reveal which field is incorrect", "Negative", "High"),
            ("Verify that multiple failed attempts are triggered account lockout or CAPTCHA", "Negative", "High"),
        ],
        "Login — Edge Cases & Security": [
            ("Verify that email field is accepted with maximum length (254 characters)", "Edge Case", "Medium"),
            ("Verify that password field is accepted with special characters (!@#$%^&*)", "Edge Case", "Medium"),
            ("Verify that login is handled correctly after session timeout", "Edge Case", "Medium"),
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
            ("Verify that search is handled correctly with special characters (!@#$%^&*)", "Edge Case", "Medium"),
            ("Verify that search is handled correctly with very long query (500+ characters)", "Edge Case", "Medium"),
            ("Verify that search is handled correctly with Unicode/emoji characters", "Edge Case", "Low"),
            ("Verify that SQL injection is blocked in the search field", "Security", "High"),
            ("Verify that XSS payload is sanitized in the search field", "Security", "High"),
        ],
        "Filter & Sort": [
            ("Verify that filter options are displayed correctly", "Positive", "High"),
            ("Verify that results are updated when a filter is applied", "Positive", "High"),
            ("Verify that multiple filters are applied simultaneously", "Positive", "Medium"),
            ("Verify that applied filters are cleared when 'Reset' is clicked", "Positive", "Medium"),
            ("Verify that sort options (A-Z, Z-A, date, price) are applied correctly", "Positive", "High"),
            ("Verify that the active sort/filter state is preserved after page navigation", "Positive", "Medium"),
            ("Verify that filter results show 'No matches' when no items match criteria", "Negative", "Medium"),
        ],
    }


def _forms_checks() -> _CL:
    return {
        "Form Fields — UI": [
            ("Verify that all form fields are displayed with correct labels", "Positive", "High"),
            ("Verify that required fields are indicated with an asterisk (*)", "Positive", "High"),
            ("Verify that placeholder text is displayed in empty fields", "Positive", "Low"),
            ("Verify that field types are correct (email, number, date, etc.)", "Positive", "Medium"),
            ("Verify that dropdown/select fields are displayed with correct options", "Positive", "Medium"),
            ("Verify that date picker is displayed and functional", "Positive", "Medium"),
        ],
        "Form Validation — Positive": [
            ("Verify that the form is submitted successfully with all valid data", "Positive", "High"),
            ("Verify that success message/notification is displayed after submission", "Positive", "High"),
            ("Verify that submitted data is saved correctly in the system", "Positive", "High"),
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
            ("Verify that fields are handled correctly at boundary values (min-1, max+1)", "Edge Case", "Medium"),
            ("Verify that special characters are handled correctly in text fields", "Edge Case", "Medium"),
            ("Verify that copy-paste is working correctly in all fields", "Edge Case", "Low"),
            ("Verify that double submission is prevented (double-click protection)", "Edge Case", "High"),
            ("Verify that the form is submitted correctly using Tab + Enter navigation", "Edge Case", "Medium"),
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
                (f"Verify that all submitted data is saved correctly in the database", "Positive", "High"),
            ],
            f"{label} — Negative": [
                (f"Verify that {label.lower()} is rejected when required fields are empty", "Negative", "High"),
                (f"Verify that {label.lower()} is rejected with invalid data format", "Negative", "High"),
                (f"Verify that duplicate record {label.lower()} is handled correctly", "Negative", "Medium"),
                (f"Verify that validation errors are displayed per field", "Negative", "High"),
            ],
            f"{label} — Edge Cases": [
                (f"Verify that {label.lower()} is handled correctly with minimum length data", "Edge Case", "Medium"),
                (f"Verify that {label.lower()} is handled correctly with maximum length data", "Edge Case", "Medium"),
                (f"Verify that special characters are accepted in text fields", "Edge Case", "Medium"),
                (f"Verify that concurrent {label.lower()} requests are handled correctly", "Edge Case", "Low"),
            ],
        }
    elif operation == "read":
        return {
            f"{label} — Positive": [
                (f"Verify that the {location} is displayed with correct data", "Positive", "High"),
                (f"Verify that all data columns/fields are visible and correctly formatted", "Positive", "High"),
                (f"Verify that pagination is working correctly when records exceed page limit", "Positive", "Medium"),
                (f"Verify that data sorting is applied correctly", "Positive", "Medium"),
                (f"Verify that detail view is displayed correctly when a record is selected", "Positive", "High"),
            ],
            f"{label} — Negative & Edge Cases": [
                (f"Verify that empty state is handled gracefully when no records exist", "Negative", "High"),
                (f"Verify that the page is handled correctly when data loading fails", "Negative", "Medium"),
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
                (f"Verify that concurrent edits to the same record are handled correctly", "Edge Case", "Medium"),
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
                (f"Verify that related/dependent records are handled correctly on deletion", "Negative", "Medium"),
            ],
            f"{label} — Edge Cases": [
                (f"Verify that deleting the last record is handled correctly", "Edge Case", "Medium"),
                (f"Verify that bulk deletion is working correctly (if supported)", "Edge Case", "Low"),
            ],
        }


def _payment_checks() -> _CL:
    return {
        "Cart": [
            ("Verify that items are added to the cart successfully", "Positive", "High"),
            ("Verify that the cart is displayed the correct item count and total", "Positive", "High"),
            ("Verify that item quantity is updated correctly in the cart", "Positive", "High"),
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
            ("Verify that the order total is calculated correctly (items + tax + shipping)", "Positive", "High"),
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
            ("Verify that the order is handled correctly when payment gateway times out", "Edge Case", "High"),
            ("Verify that the cart is preserved when payment fails", "Edge Case", "Medium"),
        ],
    }


def _navigation_checks() -> _CL:
    return {
        "Navigation": [
            ("Verify that the main navigation menu is displayed on all pages", "Positive", "High"),
            ("Verify that all menu items are redirected to the correct pages", "Positive", "High"),
            ("Verify that the current page is highlighted in the navigation", "Positive", "Medium"),
            ("Verify that browser Back/Forward buttons are working correctly", "Positive", "Medium"),
            ("Verify that the URL is updated correctly when navigating between pages", "Positive", "Medium"),
            ("Verify that 404 page is displayed for non-existent URLs", "Negative", "High"),
            ("Verify that deep links (direct URL access) are working correctly", "Positive", "Medium"),
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
            summary="Verify that login is rejected when incorrect password is provided",
            preconditions="Application is accessible. Test user account is created.",
            steps=["Navigate to the login page",
                   "Enter a valid registered email address",
                   "Enter an incorrect password",
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
                   "Enter an incorrect password",
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
            summary="Verify that correct results are returned for a valid search query",
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
            summary="Verify that form handles boundary values correctly",
            preconditions="Application is accessible. Form is opened.",
            steps=["Enter minimum length values in text fields (1 character)",
                   "Enter maximum length values in text fields",
                   "Enter special characters and Unicode in all text fields",
                   "Submit the form after each test"],
            test_data="Min: 'A', Max: 255 chars, Special: @#$%^&*(), Unicode: test",
            expected_result="All boundary values should be handled correctly. No errors or data corruption should occur.",
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
                   "Verify the item is displayed with correct details"],
            test_data="Any available product",
            expected_result="Product should be added to the cart. Cart count should be updated. Product details should be correct.",
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
            summary="Verify that all navigation menu items redirect to correct pages",
            preconditions="Application is accessible. Main page is loaded.",
            steps=["Identify all items in the navigation menu",
                   "Click each menu item one by one",
                   "Verify the correct page is loaded for each item",
                   "Verify the URL is updated correctly"],
            test_data="All navigation menu items",
            expected_result="Each navigation item should redirect to the correct page. URL should match the expected route.",
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
            summary="Verify that browser Back and Forward buttons work correctly",
            preconditions="Application is accessible. User has navigated through several pages.",
            steps=["Navigate through 3-4 different pages",
                   "Click the browser Back button",
                   "Verify the previous page loads correctly",
                   "Click the browser Forward button",
                   "Verify the next page loads correctly"],
            test_data="",
            expected_result="Browser history navigation should work correctly. Pages should load without errors.",
            category="Positive", priority="Medium", section="Navigation",
        ),
    ]


def _web_general_test_cases() -> list[TCTemplate]:
    return [
        TCTemplate(
            summary="Verify that the homepage loads correctly and displays expected content",
            preconditions="Application is accessible via browser.",
            steps=["Open the application URL in the browser",
                   "Verify the page loads without errors",
                   "Verify the page title is displayed correctly in the browser tab",
                   "Verify the main heading (H1) is displayed"],
            test_data="Application URL",
            expected_result="Homepage should load within 3 seconds. Page title and main heading should be displayed correctly. No JavaScript errors in console.",
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
                   "Verify layout adapts correctly",
                   "Verify no horizontal scrollbar appears",
                   "Verify text is readable without zooming"],
            test_data="Viewport: 375x812 (iPhone)",
            expected_result="Page layout should adapt to mobile. No horizontal scroll. Text should be readable. Touch targets should be adequate (44x44px).",
            category="Positive", priority="High", section="Responsive Design",
        ),
        TCTemplate(
            summary="Verify that the page is responsive on tablet viewport (768px width)",
            preconditions="Application is accessible.",
            steps=["Open the page in browser DevTools responsive mode",
                   "Set viewport width to 768px (tablet)",
                   "Verify layout adapts correctly",
                   "Verify no overlapping or misaligned elements"],
            test_data="Viewport: 768x1024 (iPad)",
            expected_result="Page layout should adapt to tablet size. No overlapping elements. Content should be properly formatted.",
            category="Positive", priority="High", section="Responsive Design",
        ),
        TCTemplate(
            summary="Verify that the page works correctly in Chrome, Firefox, Safari, and Edge",
            preconditions="Application is accessible. Multiple browsers are available.",
            steps=["Open the page in Google Chrome (latest)",
                   "Open the page in Mozilla Firefox (latest)",
                   "Open the page in Safari (latest)",
                   "Open the page in Microsoft Edge (latest)",
                   "Compare rendering and functionality across browsers"],
            test_data="",
            expected_result="Page should render consistently across all browsers. No browser-specific visual or functional issues.",
            category="Positive", priority="High", section="Cross-browser",
        ),
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
            summary=f"Verify that {short_action} is handled correctly with boundary values",
            preconditions="System is running. Feature is accessible.",
            steps=["Test with minimum allowed input values",
                   "Test with maximum allowed input values",
                   "Test with special characters and Unicode",
                   "Test with empty/null values"],
            test_data="Min: 1 char, Max: max length, Special: !@#$%^&*(), Empty: ''",
            expected_result="All boundary values should be handled correctly. No errors or data corruption should occur.",
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
    """Generate a negative test case for an acceptance criterion."""
    short_cr = criterion[:120].rstrip(".")
    short_action = action[:60].rstrip(".")
    return TCTemplate(
        summary=f"Verify that violation of '{short_cr[:70]}' is handled correctly",
        preconditions=f"Feature '{short_action}' is accessible.",
        steps=["Navigate to the feature under test",
               "Attempt to violate the expected behavior",
               f"Provide input/state that contradicts: {short_cr}",
               "Observe error handling and system behavior"],
        test_data="Invalid/edge-case data contradicting the criterion",
        expected_result=f"The system should prevent or handle the violation gracefully. Appropriate error message should be displayed.",
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
            summary=f"Verify that {short_action} is handled correctly with boundary values",
            preconditions="System is running. Feature is accessible.",
            steps=["Test with minimum allowed input values",
                   "Test with maximum allowed input values",
                   "Test with special characters and Unicode",
                   "Test with empty/null values"],
            test_data="Min: 1 char, Max: max length, Special: !@#$%^&*(), Empty: ''",
            expected_result="All boundary values should be handled correctly. No errors or data corruption should occur.",
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

    # Area-specific test cases from knowledge base (supplementary)
    for area in analysis.areas:
        fn = _AREA_TC_FN.get(area)
        if fn:
            cases.extend(fn())

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
