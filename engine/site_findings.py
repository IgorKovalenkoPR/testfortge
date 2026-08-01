"""TestFortge — turn failed ``site_tester`` checks into bug reports.

``engine.site_tester`` runs 53 checks against a live site and reports
Passed / Failed with a human-readable reason. Before this module those
Failed results went nowhere: they rendered on the Tools page and stopped.

That was the reason the product could not produce a **Performance** or a
**Security** bug at all — not because nothing was detected, but because
what was detected never became a bug report. Four of the six quality
attributes the operator asked for were unreachable from the Bug Reports
module no matter how broken the site was.

Every finding here is grounded in a check that actually ran and actually
failed. Nothing is inferred, and a check that could not run produces no
bug rather than a guessed one.
"""
from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import urlparse

from engine import bug_areas
from engine.log import get_logger

_logger = get_logger(__name__)

#: Checks whose failure says "we could not look", not "the product is
#: broken". Filing those as defects would put our own inability to reach
#: the site on the product's bug list.
_INFRASTRUCTURE_CHECKS = frozenset({"page_loads", "all_pages_accessible"})

#: What each check was looking for, in the product's own terms. Used as
#: the expected result, because "the check passed" is not something a
#: developer can act on.
_EXPECTED: dict[str, str] = {
    "load_time": "The Homepage loads within 3 seconds",
    "response_times": "Every crawled page responds within 3 seconds",
    "page_size": "The Homepage weighs less than the page-size budget",
    "images_optimized": "Images are served in a compressed, sized format",
    "https": "The site is served over HTTPS",
    "mixed_content": "Every resource on an HTTPS page is loaded over HTTPS",
    "password_masked": "The password field masks what the user types",
    "external_links_target": 'Every target="_blank" link carries rel="noopener"',
    "images_have_alt": "Every image carries descriptive alt text",
    "form_labels": "Every form field has an associated label",
    "keyboard_focus": "A visible focus indicator is displayed on every focused element",
    "aria_landmarks": "The page declares its ARIA landmarks",
    "color_contrast_hint": "Text meets the WCAG AA contrast ratio",
    "lang_attribute": "The document declares its language",
    "heading_hierarchy": "Headings descend one level at a time",
    "logo_displayed": "The logo is displayed in the Header",
    "images_loaded": "Every image is loaded with no broken-image placeholder",
    "favicon": "The site declares a favicon",
    "no_placeholder": "No placeholder or lorem-ipsum content is displayed",
    "footer_displayed": "The Footer is displayed",
    "nav_displayed": "The navigation menu is displayed",
    "viewport_meta": "The page declares a viewport meta tag",
    "cta_buttons": "The page carries a call-to-action control",
    "required_field_indicators": "Required fields are marked as required",
    "search_field": "A search control is available",
    "mailto_links": "Every mailto: link carries a valid address",
    "tel_links": "Every tel: link carries a valid number",
    "social_links": "Every social media link opens the brand's own account",
    "meta_description": "The page declares a meta description",
    "canonical": "The page declares a canonical URL",
    "og_tags": "The page declares its Open Graph tags",
}


def _title(check_key: str, area: str, actual: str, host: str) -> str:
    """A title that names what is wrong, not which check noticed it.

    "load_time check failed" is a fact about our tooling. "The Homepage
    takes 7.4s to load" is a fact about the product, which is what a bug
    title is for.
    """
    subject = (_EXPECTED.get(check_key) or check_key.replace("_", " "))
    # The check's own message is usually the most specific thing we have.
    first = (actual or "").strip().split(". ")[0].strip().rstrip(".")
    body = first if len(first) >= 12 else f"{subject} — not met"
    return f"[{area}] {body}"[:500]


def finding_from_check(check_key: str, result: Any, *,
                       base_url: str = "") -> dict | None:
    """One failed :class:`site_tester.CheckResult` → a bug dict.

    Returns ``None`` for a passing check, an infrastructure check, or a
    check whose failure is our own error rather than the product's.
    """
    status = str(getattr(result, "status", "") or "").strip().lower()
    if status != "failed":
        return None
    if check_key in _INFRASTRUCTURE_CHECKS:
        return None

    actual = str(getattr(result, "actual_result", "") or "").strip()
    # "Check error: …" is this tool falling over, not the site being
    # broken. Filing it would put our own stack trace on the client's
    # defect list.
    if actual.lower().startswith("check error"):
        _logger.debug("site_findings: skipping errored check %s", check_key)
        return None
    if not actual:
        return None

    area, defect_class = bug_areas.area_for_check(check_key)
    try:
        from engine.bug_template import severity_priority
        # The hints say WHERE the defect is, not what kind it is. Passing
        # the quality attribute here was wrong and produced "Critical /
        # Low" for a site served over plain HTTP: "Security" matches none
        # of the area keywords, so the weight collapsed to 1. These checks
        # run against the whole site starting at its Homepage, which is a
        # weight-3 surface, so that is what they are told.
        severity, priority = severity_priority(
            defect_class, "homepage", base_url, check_key)
    except Exception:  # pragma: no cover — defensive
        severity, priority = "Major", "Medium"

    # A Critical bug at Low priority is a contradiction a report should
    # not contain — it reads as "serious, ignore it". Site-wide defects
    # like no-HTTPS affect every page and every visitor, so the floor is
    # raised rather than the severity lowered.
    if severity == "Critical" and priority in ("Low", "Lowest"):
        priority = "High"

    host = urlparse(base_url).netloc or base_url
    return {
        "title": _title(check_key, area, actual, host),
        "bug_area": area,
        "severity": severity,
        "priority": priority,
        # Must be a member of bug_report.BUG_STATUSES — see the same
        # note in routes/execution_manual.py. A value outside it drops
        # the finding out of the "Open" tile and every status filter.
        "status": "Open",
        "environment": f"{host}" if host else "",
        "preconditions": f"The site {base_url} is reachable" if base_url
                         else "",
        "steps_to_reproduce": _steps(check_key, base_url),
        "actual_result": actual,
        "expected_result": _EXPECTED.get(
            check_key, f"The {check_key.replace('_', ' ')} check passes"),
        "comment": f"Detected by the automated site sweep "
                   f"(check: {check_key}).",
        "defect_class": defect_class,
        "labels": [f"defect:{defect_class}", f"area:{area.lower()}",
                   "source:site_sweep"],
    }


def _steps(check_key: str, base_url: str) -> str:
    """Reproduction steps in the house style, starting from the entry URL.

    Generic where the check is generic — inventing a click path the sweep
    never took would be a step nobody can follow.
    """
    first = f"1. Go to the site: {base_url}" if base_url \
        else "1. Go to the site under test"
    second = {
        "load_time": "2. Pay attention to the load time in the DevTools "
                     "Network tab",
        "response_times": "2. Pay attention to the response time of each "
                          "page in the DevTools Network tab",
        "page_size": "2. Pay attention to the transferred size in the "
                     "DevTools Network tab",
        "images_optimized": "2. Pay attention to the format and size of "
                            "each image in the DevTools Network tab",
        "https": "2. Pay attention to the scheme in the browser address bar",
        "mixed_content": "2. Pay attention to the Console tab",
        "password_masked": "2. Fill in the password field with any value\n"
                           "3. Pay attention to the characters displayed",
        "external_links_target": "2. Pay attention to the rel attribute of "
                                 "every external link in the page source",
        "images_have_alt": "2. Pay attention to the alt attribute of every "
                           "image in the page source",
        "form_labels": "2. Pay attention to the label associated with each "
                       "form field in the page source",
        "keyboard_focus": "2. Press the \"Tab\" key repeatedly\n"
                          "3. Pay attention to the focus indicator",
        "color_contrast_hint": "2. Pay attention to the contrast ratio of "
                               "the body text",
    }.get(check_key, "2. Pay attention to the page")
    return f"{first}\n{second}"


def findings_from_results(results: Any, *, base_url: str = "",
                          limit: int = 40) -> list[dict]:
    """Every failed check as a bug dict, most severe first.

    Capped, and the cap is reported by the caller rather than swallowed: a
    site with 30 accessibility failures should not bury its one Critical
    security finding under them, and it should not silently drop the tail
    either.
    """
    rank = {"Critical": 0, "Major": 1, "Minor": 2, "Trivial": 3}
    out: list[dict] = []
    for key, result in (results or {}).items():
        finding = finding_from_check(str(key), result, base_url=base_url)
        if finding is not None:
            out.append(finding)
    out.sort(key=lambda f: rank.get(f.get("severity", "Minor"), 2))
    return out[:limit]


def dropped_count(results: Any, kept: Iterable[dict], *,
                  base_url: str = "") -> int:
    """How many findings the cap discarded, for the caller to surface."""
    total = sum(1 for key, result in (results or {}).items()
                if finding_from_check(str(key), result,
                                      base_url=base_url) is not None)
    return max(0, total - len(list(kept)))


def summarise_by_area(findings: Iterable[dict]) -> dict[str, int]:
    """``{area: n}`` over all six areas, zeros included."""
    return bug_areas.counts_by_area(findings)


__all__ = [
    "finding_from_check", "findings_from_results", "dropped_count",
    "summarise_by_area",
]
