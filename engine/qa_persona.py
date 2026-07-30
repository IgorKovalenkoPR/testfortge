"""
TestFortge — Senior QA Engineer Persona

ISTQB-grade checklist + test-case generator. Static content (per-area
checklists, test-case templates) lives in versioned YAML under
``engine/qa_knowledge/``. This module orchestrates: input analysis,
crawler/browser enrichment, area detection, named-flow expansion, and
final filtering/composition of the output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .qa_utils import (
    is_instruction,
    detect_flows,
    slugify_section as _slugify_section,
    path_label as _path_label,
    form_label as _form_label,
)


@dataclass
class CheckItem:
    objective: str
    category: str          # Positive / Negative / Edge Case / Security / Performance / Accessibility
    priority: str = "Medium"  # High / Medium / Low
    section: str = ""
    testing_type: str = ""


@dataclass
class TCTemplate:
    summary: str
    preconditions: str
    steps: list[str]
    test_data: str
    expected_result: str
    category: str          # Positive / Negative / Edge Case / Security
    priority: str = "Medium"
    section: str = ""
    comment: str = ""
    # Empty by default so ``_detect_testing_type`` (in testcase_generator)
    # can tag the case heuristically. Generators that already know the right
    # testing type — SEO / Usability / Localization — set it explicitly.
    testing_type: str = ""


@dataclass
class AnalysisResult:
    areas: list[str]
    url: str | None = None
    url_domain: str = ""
    url_path: str = ""
    features: list[str] = field(default_factory=list)
    level: str = "low"
    raw_requirements: list[str] = field(default_factory=list)
    browser_findings: list[dict] = field(default_factory=list)
    flows: list[str] = field(default_factory=list)
    # Per-page crawler data — drives site-specific test cases.
    site_pages: list[dict] = field(default_factory=list)
    site_type: str = "generic"
    # Crawler partial-failure messages bubbled up so routes can flash a
    # warning banner instead of silently degrading to generic generation.
    crawl_errors: list[str] = field(default_factory=list)
    # Crawler partial-failure messages bubbled up so routes can flash a
    # warning banner instead of silently degrading to generic generation.
    crawl_errors: list[str] = field(default_factory=list)


_URL_RE = re.compile(
    r"(https?://)?([a-z0-9][a-z0-9\-]*\.[a-z0-9\-]*\.[a-z]{2,}|[a-z0-9\-]+\.[a-z]{2,})(/[^\s]*)?",
    re.IGNORECASE,
)


# Area keywords for detection. Kept in qa_persona.py because
# engine/automation_qa.py and engine/qa_team_lead.py import it.
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


def browser_pass_enabled() -> bool:
    """Whether generation may launch Playwright inside the web worker.

    Default on, so nothing changes for a box with headroom. Set
    ``TESTFORTGE_BROWSER_ENABLED=0`` where the instance cannot afford it.

    Why this switch exists: on Render's free plan (512 MB, 1 gunicorn
    worker) Chromium needs ~250 MB on top of Flask, the LLM client and
    the crawl. The worker got OOM-killed ~110 s into a generation, and
    because JobQueue lives in that process's memory the job vanished with
    it — ``/test-cases/status/<id>`` then answered 404 and the UI could
    only say "The generation job was lost". render.yaml already dropped
    from 2 workers to 1 for the same reason.

    Turning this off keeps the requests-based crawl, which is what
    supplies the control inventory the Test Case Author agent needs. Only
    the browser-derived findings (performance, console errors, responsive
    layout) are lost, and /test-execution still runs a real browser pass
    in a detached subprocess that survives a gunicorn restart.
    """
    import os
    return (os.environ.get("TESTFORTGE_BROWSER_ENABLED", "1")
            .strip().lower() not in ("0", "false", "no", "off"))


def analyze_input(requirements: list[dict],
                  custom_prompt: str = "") -> AnalysisResult:
    """Analyze structured requirements to determine testing scope."""
    result = AnalysisResult(areas=[], raw_requirements=[])
    all_text = " ".join(r.get("text", "") for r in requirements)
    all_text_lower = all_text.lower()
    combined = all_text_lower + " " + custom_prompt.lower()

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

    if any(kw in combined for kw in ["low level", "low-level", "детальн", "низькорівн",
                                      "детализирован", "подробн", "granular", "atomic"]):
        result.level = "low"
    elif any(kw in combined for kw in ["high level", "high-level", "високорівн",
                                        "загальн", "summary", "overview"]):
        result.level = "high"

    detected_areas: set[str] = set()
    for area, keywords in _AREA_KEYWORDS.items():
        for kw in keywords:
            if kw in combined:
                detected_areas.add(area)
                break

    # Crawler feature confirmation: structural findings override keyword
    # guesses for auth/search/forms/payment so "pricing" copy on an
    # informational site does not pull in checkout cases.
    site_analysis = None
    if result.url:
        try:
            from .site_crawler import crawl_site
            site_analysis = crawl_site(result.url)
            result.features = list(site_analysis.features_detected)
            result.site_type = site_analysis.site_type
            # Bubble crawler partial-failure messages up to the route layer.
            if site_analysis.crawl_errors:
                result.crawl_errors.extend(site_analysis.crawl_errors)
            for p in site_analysis.pages:
                if p.error:
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

            _CRAWLER_AUTHORITATIVE = {"auth", "search", "forms", "payment"}
            for area in _CRAWLER_AUTHORITATIVE:
                detected_areas.discard(area)
            if site_analysis.has_auth:
                detected_areas.add("auth")
            if site_analysis.has_search:
                detected_areas.add("search")
            if site_analysis.has_forms:
                detected_areas.add("forms")
            if site_analysis.has_payment:
                detected_areas.add("payment")
            if site_analysis.nav_items:
                detected_areas.add("navigation")

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
        except Exception as exc:
            # Record but do not raise — generation continues on whatever
            # data we already have (generic templates if site_pages empty).
            result.crawl_errors.append(f"crawler exception: {exc}")

    if result.url and browser_pass_enabled():
        try:
            from .browser_tester import get_or_run as browser_get_or_run
            from dataclasses import asdict
            # Cap sync browser work so a single POST does not tie up a Render
            # free-tier worker for >2 min. Three knobs:
            #   TESTFORTGE_BROWSER_PAGES       (default 5)
            #   TESTFORTGE_BROWSER_TIMEOUT_MS  (default 5000)
            #   TESTFORTGE_BROWSER_VIEWPORTS   (default "desktop")
            import os
            sync_max_pages = int(os.environ.get("TESTFORTGE_BROWSER_PAGES", "5"))
            sync_timeout_ms = int(os.environ.get("TESTFORTGE_BROWSER_TIMEOUT_MS", "5000"))
            sync_viewports = os.environ.get("TESTFORTGE_BROWSER_VIEWPORTS", "desktop")
            browser_report = browser_get_or_run(
                result.url, max_pages=sync_max_pages,
                timeout_ms=sync_timeout_ms,
                site_analysis=site_analysis,
                viewports=sync_viewports,
            )
            result.browser_findings = [asdict(f) for f in browser_report.findings]
        except Exception:
            pass
    elif result.url:
        result.crawl_errors.append(
            "In-process browser pass skipped (TESTFORTGE_BROWSER_ENABLED=0) "
            "— performance, console and responsive findings are omitted. "
            "Run /test-execution for a real browser pass.")

    if result.url and not detected_areas:
        detected_areas = {"web_general"}
    elif result.url:
        detected_areas.add("web_general")

    if not detected_areas:
        detected_areas = {"web_general"}

    # Named flows override crawler-authoritative suppression: an explicit
    # "checkout flow" request must emit the full playbook even if the
    # landing page does not expose payment.
    result.flows = detect_flows(combined)
    if "checkout_flow" in result.flows:
        detected_areas.update({"payment", "auth", "forms", "navigation"})

    result.areas = sorted(detected_areas)
    result.raw_requirements = [r.get("text", "") for r in requirements]
    return result


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
    items: list[CheckItem] = []
    for f in findings:
        desc = f.get("description", "")
        status = f.get("status", "Passed")

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
    cases: list[TCTemplate] = []
    for f in findings:
        desc = f.get("description", "")
        status = f.get("status", "Passed")
        page_url = f.get("page_url", "")
        category_str = f.get("category", "")
        section = f"Browser: {_FINDING_CAT_TO_SECTION.get(category_str, 'Tests')}"

        if status == "Failed":
            summary = f"Verify that {desc} is fixed"
            expected = f"The issue is resolved. Current state: {desc}"
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


def generate_professional_checklist(analysis: AnalysisResult,
                                    custom_prompt: str = "") -> list[CheckItem]:
    """Generate a professional low-level checklist based on analysis."""
    from .qa_knowledge_loader import LOADER

    items: list[CheckItem] = []
    for area in analysis.areas:
        sections = LOADER.get_checklist(area)
        for section_name, checks in sections.items():
            for ci in checks:
                items.append(CheckItem(
                    objective=ci.objective,
                    category=ci.category,
                    priority=ci.priority,
                    section=section_name,
                    testing_type=ci.testing_type,
                ))

    for flow_key in getattr(analysis, "flows", []) or []:
        items.extend(_flow_checks(flow_key))

    if analysis.raw_requirements:
        for req_text in analysis.raw_requirements:
            if re.match(r"^(https?://|test\s+the\s+)", req_text, re.I):
                continue
            if is_instruction(req_text):
                continue
            short = req_text[:80].rstrip(".")
            items.append(CheckItem(
                objective=f"Verify {short} with valid input",
                category="Positive", priority="High",
                section="Requirements-specific",
            ))
            items.append(CheckItem(
                objective=f"Verify {short} rejects invalid input "
                           "with a clear validation message",
                category="Negative", priority="High",
                section="Requirements-specific",
            ))
            items.append(CheckItem(
                objective=f"Verify {short} on boundary values "
                           "(min, min+1, max-1, max)",
                category="Edge case", priority="Medium",
                section="Requirements-specific",
            ))

    if analysis.browser_findings:
        items.extend(_browser_findings_to_checklist(analysis.browser_findings))

    # SQL injection items are pointless when targeting a 3rd-party URL
    # — no backend access on those runs.
    if analysis.url:
        items = [i for i in items if "sql injection" not in i.objective.lower()
                 and "sql" not in i.objective.lower().split("inject")[0:1]]

    if custom_prompt:
        lower_prompt = custom_prompt.lower()
        if "positive only" in lower_prompt or "тільки позитивні" in lower_prompt:
            items = [i for i in items if i.category == "Positive"]
        elif "negative only" in lower_prompt or "тільки негативні" in lower_prompt:
            items = [i for i in items if i.category == "Negative"]
        elif "security" in lower_prompt and ("only" in lower_prompt or "focus" in lower_prompt):
            items = [i for i in items if i.category == "Security"]

    return items


def _generic_test_cases(action: str, original: str, section: str = "General") -> list[TCTemplate]:
    short_action = action[:80].rstrip(".")
    return [
        TCTemplate(
            summary=f"Verify that {short_action} is functioning as expected",
            preconditions="System is running. User is authenticated (if applicable).",
            steps=["Navigate to the relevant page/feature",
                   f"Perform the action: {short_action}",
                   "Observe the result"],
            test_data="Valid input data",
            expected_result=f"The feature functions as specified, and the expected behaviour is observed.",
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
            expected_result="Invalid input is rejected. A user-friendly error message is displayed. No data corruption occurs.",
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
            expected_result="Min and max boundary values are accepted; min-1 and max+1 are rejected with a field-specific message. No errors or data corruption occur.",
            category="Edge Case", priority="Medium", section=section,
        ),
    ]


def _ac_to_test_case(criterion: str, action: str, section: str) -> TCTemplate:
    short_cr = criterion[:120].rstrip(".")
    short_action = action[:60].rstrip(".")
    return TCTemplate(
        summary=f"Verify that {short_cr.lower() if short_cr[0:1].isupper() else short_cr}",
        preconditions=f"Feature '{short_action}' is accessible. User is authenticated (if applicable).",
        steps=["Navigate to the feature under test",
               f"Perform the action: {short_action}",
               f"Validate criterion: {short_cr}"],
        test_data="Valid data matching the criterion",
        expected_result=f"{short_cr}. The system behaves as specified.",
        category="Positive", priority="High", section=section,
    )


def _ac_negative_test_case(criterion: str, action: str, section: str) -> TCTemplate:
    """Negative TC from an AC.

    Already-negative criteria ("cannot", "no errors", ...) get framed as
    bypass attempts so the summary stays grammatical instead of producing
    a self-contradictory double-negative.
    """
    short_cr = criterion[:120].rstrip(".")
    short_action = action[:60].rstrip(".")

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
        expected = (f"The system enforces the restriction. The action is "
                    f"blocked, and a clear, user-facing error is displayed.")
    else:
        summary = (f"Verify that the system rejects input that violates "
                   f"'{short_cr[:70]}'")
        steps = ["Navigate to the feature under test",
                 f"Provide input/state that contradicts: {short_cr}",
                 "Observe error handling and system behavior"]
        expected = ("The system rejects the invalid input gracefully. An "
                    "appropriate, user-facing error message is displayed, "
                    "and no data is persisted.")

    return TCTemplate(
        summary=summary,
        preconditions=f"Feature '{short_action}' is accessible.",
        steps=steps,
        test_data="Invalid/edge-case data contradicting the criterion",
        expected_result=expected,
        category="Negative", priority="Medium", section=section,
    )


def _story_test_cases(story, section: str) -> list[TCTemplate]:
    cases: list[TCTemplate] = []
    action = story.action
    criteria = getattr(story, "acceptance_criteria", []) or []

    if criteria:
        for ac in criteria:
            cases.append(_ac_to_test_case(ac, action, section))

        _NEG_SIGNALS = re.compile(
            r"valid|input|field|required|error|reject|password|auth|permission"
            r"|format|length|empty|null|data|persisted|database|encrypt",
            re.IGNORECASE,
        )
        for ac in criteria:
            if _NEG_SIGNALS.search(ac):
                cases.append(_ac_negative_test_case(ac, action, section))

        short_action = action[:80].rstrip(".")
        cases.append(TCTemplate(
            summary=f"Verify that {short_action} returns the expected outcome per the spec with boundary values",
            preconditions="System is running. Feature is accessible.",
            steps=["Test with minimum allowed input values",
                   "Test with maximum allowed input values",
                   "Test with special characters and Unicode",
                   "Test with empty/null values"],
            test_data="Min: 1 char, Max: max length, Special: !@#$%^&*(), Empty: ''",
            expected_result="Min and max boundary values are accepted; min-1 and max+1 are rejected with a field-specific message. No errors or data corruption occur.",
            category="Edge Case", priority="Medium", section=section,
        ))
    else:
        cases.extend(_generic_test_cases(action, story.original_text, section))

    return cases


def _localization_test_cases(analysis: "AnalysisResult") -> list[TCTemplate]:
    """Localization checks — emitted when the site exposes multi-language UI.

    Templates live in the YAML; we just decide whether to emit, then
    interpolate the ``{url}`` placeholder with the real URL.
    """
    pages = analysis.site_pages or []
    multi_lang = False
    for p in pages:
        nav_blob = " ".join(p.get("nav_links") or []).lower()
        title_blob = (p.get("title") or "").lower()
        if any(t in nav_blob for t in ("ua", "укр", "рус", "eng", "english", "deutsch", "polski", "español")):
            multi_lang = True
            break
        if re.search(r"[а-яіїєґ]", title_blob) and re.search(r"[a-z]{3,}", title_blob):
            multi_lang = True
            break
    if not pages or not multi_lang:
        return []

    from .qa_knowledge_loader import LOADER
    url = analysis.url or ""
    out: list[TCTemplate] = []
    for tpl in LOADER.get_test_cases("localization"):
        out.append(TCTemplate(
            summary=tpl.summary,
            preconditions=tpl.preconditions.replace("{url}", url),
            steps=list(tpl.steps),
            test_data=tpl.test_data,
            expected_result=tpl.expected_result,
            category=tpl.category,
            priority=tpl.priority,
            section=tpl.section,
            testing_type=tpl.testing_type,
        ))
    return out


def _site_specific_test_cases(analysis: "AnalysisResult") -> list[TCTemplate]:
    """Test cases anchored in real crawler data (titles, H1s, buttons, forms)."""
    cases: list[TCTemplate] = []
    pages = analysis.site_pages or []
    if not pages:
        return cases

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

    # Cap per-page emissions so a 200-page site doesn't drown the export.
    MAX_PAGES = 8
    for p in pages[:MAX_PAGES]:
        url = p.get("url", "")
        title = p.get("title") or p.get("h1") or _path_label(url)
        h1 = p.get("h1") or ""
        headings = p.get("headings") or []
        buttons = p.get("buttons") or []
        forms = p.get("forms") or []
        has_video = p.get("has_video")
        path_lbl = _path_label(url)
        section = f"Page: {_slugify_section(title or path_lbl)}"

        section_list = ", ".join(h for h in headings[:3]) if headings else ""
        content_steps = [f"Open the URL: {url}",
                         "Wait for the page to fully load (DOMContentLoaded + main images)"]
        if h1:
            content_steps.append(f"Verify the visible H1 reads: \"{h1[:80]}\"")
        if section_list:
            content_steps.append(f"Verify the page renders the following sections: {section_list[:150]}")
        content_steps.append("Open DevTools Console — confirm there are no JavaScript errors")
        cases.append(TCTemplate(
            summary=f"Verify that {path_lbl} renders its primary content as observed by the crawler",
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

        if buttons:
            real_btns = [b for b in buttons
                         if 2 <= len(b.strip()) <= 40
                         and b.strip().lower() not in {"×", "x", "ok", "?", "close"}]
            real_btns = real_btns[:5]
            if real_btns:
                btns_str = ", ".join(f'"{b}"' for b in real_btns)
                cases.append(TCTemplate(
                    summary=f"Verify that the interactive controls on {path_lbl} respond to user clicks",
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

        if has_video:
            cases.append(TCTemplate(
                summary=f"Verify that embedded video on {path_lbl} loads and plays",
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
                summary = f"Verify that submitting the {label} on {path_lbl} with valid credentials succeeds"
                steps = [f"Open {url}",
                         f"Fill the form fields ({', '.join(named)}) with valid values",
                         "Click the submit / sign-in button",
                         "Observe the response — successful auth redirects or unlocks content"]
                expected = "Form submits successfully, the server returns 2xx and the user is redirected to the post-auth page or sees a success state."
                category = "Positive"
            else:
                summary = f"Verify that the {label} on {path_lbl} accepts valid input and submits cleanly"
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
            cases.append(TCTemplate(
                summary=f"Verify that the {label} on {path_lbl} rejects empty / malformed input"[:120],
                preconditions=f"{url} is reachable; the form '{label}' is rendered.",
                steps=[f"Open {url}",
                       "Submit the form with all fields empty",
                       f"Submit the form with malformed values (e.g. invalid email, mismatched password) for: {', '.join(named[:3])}",
                       "Inspect the rendered validation messages and HTTP responses"],
                test_data=f"Empty values; malformed values for: {', '.join(named[:3])}",
                expected_result="Each invalid attempt is blocked client- or server-side with a field-specific error message. No partial write reaches the backing store.",
                category="Negative", priority="High", section=section,
            ))
            break  # one form per page is enough

    if "search" in (analysis.areas or []) and pages:
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


_FLOW_CATEGORY_BY_PHASE = {
    "Edge / Negative": "Edge Case",
    "Security & Compliance": "Security",
}


def _flow_test_cases(flow_key: str) -> list[TCTemplate]:
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
                expected_result=f"{raw}. The system behaves as stated, with no data loss and no double-charging.",
                category=category,
                priority="High",
                section=section,
            ))
    return cases


def _flow_checks(flow_key: str) -> list[CheckItem]:
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
    """Generate professional test cases based on analysis."""
    from .qa_knowledge_loader import LOADER

    cases: list[TCTemplate] = []

    # Site-specific cases anchored on crawler data come FIRST so the
    # generic baseline can suppress duplicate coverage for the same areas.
    site_cases = _site_specific_test_cases(analysis)
    cases.extend(site_cases)
    site_covers_navigation = any(c.section == "Site Navigation" for c in site_cases)
    site_covers_forms      = any(c.section.startswith("Page: ") and "form" in c.summary.lower()
                                  for c in site_cases)
    site_covers_search     = any(c.section == "Site Search" for c in site_cases)

    for area in analysis.areas:
        if area == "navigation" and site_covers_navigation:
            continue
        if area == "forms" and site_covers_forms:
            continue
        if area == "search" and site_covers_search:
            continue
        cases.extend(LOADER.get_test_cases(area))

    # Non-functional baselines: when a URL is in scope, also emit SEO,
    # Usability and Localization so the default scope is real regression
    # coverage (functional + smoke + perf + security + a11y + SEO + UX).
    if analysis.url:
        cases.extend(LOADER.get_test_cases("seo"))
        cases.extend(LOADER.get_test_cases("usability"))
        cases.extend(_localization_test_cases(analysis))

    for flow_key in getattr(analysis, "flows", []) or []:
        cases.extend(_flow_test_cases(flow_key))

    if stories:
        from .user_story_generator import UserStory
        for story in stories:
            if not isinstance(story, UserStory):
                continue
            story_area = _detect_area_for_text(story.original_text)
            section = _AREA_SECTION.get(story_area, "General")
            cases.extend(_story_test_cases(story, section))

    if analysis.browser_findings:
        cases.extend(_browser_findings_to_test_cases(analysis.browser_findings))

    if analysis.url:
        cases = [c for c in cases if "sql injection" not in c.summary.lower()
                 and "sql" not in c.summary.lower().split("inject")[0:1]]

    return cases


def _detect_area_for_text(text: str) -> str | None:
    lower = text.lower()
    for area, keywords in _AREA_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                return area
    return None


# Back-compat: `_SECTION_PREFIXES` was a module-level dict consumed by
# engine.testcase_generator. The loader now owns the section→prefix map;
# this proxy keeps the import path stable for any external caller.
class _SectionPrefixProxy:
    def __getitem__(self, key):
        from .qa_knowledge_loader import LOADER
        val = LOADER.get_section_prefix(key)
        if val is None:
            raise KeyError(key)
        return val

    def get(self, key, default=None):
        from .qa_knowledge_loader import LOADER
        val = LOADER.get_section_prefix(key)
        return val if val is not None else default

    def __contains__(self, key):
        from .qa_knowledge_loader import LOADER
        return LOADER.get_section_prefix(key) is not None

    def __iter__(self):
        from .qa_knowledge_loader import LOADER
        return iter(LOADER.section_prefix_map())

    def keys(self):
        from .qa_knowledge_loader import LOADER
        return LOADER.section_prefix_map().keys()

    def items(self):
        from .qa_knowledge_loader import LOADER
        return LOADER.section_prefix_map().items()


_SECTION_PREFIXES = _SectionPrefixProxy()
