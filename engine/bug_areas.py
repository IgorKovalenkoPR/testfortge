"""TestFortge — the quality attribute a bug belongs to.

Severity says how badly something is broken. **Area says what KIND of
broken it is**, and they are independent: a Critical accessibility defect
and a Critical payment defect go to different people, get fixed with
different skills, and belong in different sections of a report.

Six areas, which is the set the operator asked for and the set a QA report
is normally structured around::

    Functional      the product does not do what it is supposed to do
    UI & UX         it does the right thing and looks wrong doing it
    Usability       it works, and a user cannot reasonably get there
    Accessibility   it excludes people — WCAG, keyboard, screen readers
    Performance     it is too slow, too heavy, or too many requests
    Security        it leaks, trusts, or exposes something it should not

Why the mapping is exhaustive
-----------------------------
Every ``defect_class`` and every ``site_tester`` check key is mapped
explicitly, and ``tests/test_bug_areas.py`` fails when one is missing. An
unmapped key would fall through to Functional, which is the quiet failure
mode that matters here: a Performance or Security finding filed as
Functional does not disappear, it gets triaged by the wrong person and
shows up in the wrong column of the report. A default is fine for a value
we have never seen; it is not fine for one we ship.
"""
from __future__ import annotations

from typing import Any

from engine.log import get_logger

_logger = get_logger(__name__)

FUNCTIONAL = "Functional"
UI_UX = "UI & UX"
USABILITY = "Usability"
ACCESSIBILITY = "Accessibility"
PERFORMANCE = "Performance"
SECURITY = "Security"

AREAS: tuple[str, ...] = (FUNCTIONAL, UI_UX, USABILITY, ACCESSIBILITY,
                          PERFORMANCE, SECURITY)

#: Fallback for a value that is genuinely unknown — not for one we ship.
DEFAULT_AREA = FUNCTIONAL


# ── defect_class → area ──────────────────────────────────────────────
#
# Keys mirror engine.bug_template.CLASS_SEVERITY. That file answers "how
# bad"; this one answers "what kind", and the two are deliberately
# separate tables so a severity change cannot silently re-file a bug.

CLASS_AREA: dict[str, str] = {
    # ── Functional: the action cannot complete ──
    "click_timeout":        FUNCTIONAL,
    "fill_timeout":         FUNCTIONAL,
    "select_timeout":       FUNCTIONAL,
    "check_timeout":        FUNCTIONAL,
    "navigation_timeout":   FUNCTIONAL,
    "navigation_error":     FUNCTIONAL,
    "dns_error":            FUNCTIONAL,
    "text_assertion_fail":  FUNCTIONAL,
    "url_assertion_fail":   FUNCTIONAL,
    "selector_ambiguous":   FUNCTIONAL,
    "server_error":         FUNCTIONAL,
    "form_unfillable":      FUNCTIONAL,
    "cta_no_destination":   FUNCTIONAL,
    "search_no_results":    FUNCTIONAL,
    "search_broken":        FUNCTIONAL,
    "dropdown_dead":        FUNCTIONAL,
    "footer_dead_page":     FUNCTIONAL,
    "malformed_link":       FUNCTIONAL,
    "walk_step_failed":     FUNCTIONAL,
    # A thrown exception is a functional defect even when nothing visibly
    # breaks — the next user action is running on a broken page.
    "console_js_error":     FUNCTIONAL,
    "page_error":           FUNCTIONAL,

    # ── UI & UX: it renders wrong ──
    "broken_image":         UI_UX,
    "clipped_text":         UI_UX,
    "icon_fallback":        UI_UX,
    "modal_overflow":       UI_UX,

    # ── Usability: it works and the user cannot get there ──
    # A dead hamburger is not a rendering fault — on a phone the whole
    # navigation is unreachable, which is a usability wall before it is
    # anything else.
    "hamburger_dead":       USABILITY,
    "cta_disabled":         USABILITY,
    "placeholder_social":   USABILITY,
    "homepage_no_title":    USABILITY,
    "homepage_no_h1":       USABILITY,

    # ── Accessibility ──
    "axe_critical":         ACCESSIBILITY,
    "axe_serious":          ACCESSIBILITY,
    # WCAG 2.5.5 Target Size. Filed as accessibility rather than usability
    # because it is a measurable standard with a number, not a judgement.
    "cta_tiny_tap_target":  ACCESSIBILITY,

    # ── Security ──
    # target=_blank without rel=noopener hands the opened page a handle on
    # ours via window.opener. Small, but it is an exposure, not a nit.
    "social_no_noopener":   SECURITY,

    # ── Infrastructure / run-level ──
    "early_exit_oom":         FUNCTIONAL,
    "early_exit_wall_clock":  FUNCTIONAL,
    "early_exit_unknown":     FUNCTIONAL,

    "unknown":              DEFAULT_AREA,
}


# ── site_tester check key → (area, defect_class) ─────────────────────
#
# engine.site_tester runs 56 checks and reports Passed / Failed with a
# human-readable reason. Every failed one is a real finding, and before
# this table they went nowhere — which is why the product could not
# produce a Performance or a Security bug at all.

CHECK_AREA: dict[str, tuple[str, str]] = {
    # ── Performance ──
    "load_time":        (PERFORMANCE, "slow_page_load"),
    "response_times":   (PERFORMANCE, "slow_response"),
    "page_size":        (PERFORMANCE, "heavy_page"),
    "images_optimized": (PERFORMANCE, "unoptimised_images"),

    # ── Security ──
    "https":            (SECURITY, "no_https"),
    "mixed_content":    (SECURITY, "mixed_content"),
    "password_masked":  (SECURITY, "password_not_masked"),
    "external_links_target": (SECURITY, "social_no_noopener"),

    # ── Accessibility ──
    "images_have_alt":  (ACCESSIBILITY, "missing_alt_text"),
    "form_labels":      (ACCESSIBILITY, "unlabelled_field"),
    "keyboard_focus":   (ACCESSIBILITY, "no_focus_indicator"),
    "aria_landmarks":   (ACCESSIBILITY, "missing_landmarks"),
    "color_contrast_hint": (ACCESSIBILITY, "low_contrast"),
    "lang_attribute":   (ACCESSIBILITY, "missing_lang"),
    "heading_hierarchy": (ACCESSIBILITY, "heading_hierarchy"),

    # ── UI & UX ──
    "logo_displayed":   (UI_UX, "logo_missing"),
    "images_loaded":    (UI_UX, "broken_image"),
    "favicon":          (UI_UX, "favicon_missing"),
    "content_sections_order": (UI_UX, "section_order"),
    "no_placeholder":   (UI_UX, "placeholder_content"),
    "footer_displayed": (UI_UX, "footer_missing"),
    "header_on_all_pages": (UI_UX, "header_inconsistent"),
    "nav_displayed":    (UI_UX, "nav_missing"),
    "breadcrumbs":      (UI_UX, "breadcrumbs_missing"),

    # ── Usability ──
    "viewport_meta":    (USABILITY, "no_viewport_meta"),
    "cta_buttons":      (USABILITY, "cta_missing"),
    "content_readable": (USABILITY, "unreadable_content"),
    "required_field_indicators": (USABILITY, "no_required_marker"),
    "dropdown_menus":   (USABILITY, "dropdown_dead"),
    "footer_consistent": (USABILITY, "footer_inconsistent"),
    "nav_on_all_pages": (USABILITY, "nav_inconsistent"),
    "double_submit_protection": (USABILITY, "double_submit"),
    "search_field":     (USABILITY, "search_missing"),
    "mailto_links":     (USABILITY, "mailto_broken"),
    "tel_links":        (USABILITY, "tel_broken"),
    "social_links":     (USABILITY, "placeholder_social"),

    # ── Functional ──
    "page_loads":       (FUNCTIONAL, "navigation_error"),
    "all_pages_accessible": (FUNCTIONAL, "navigation_error"),
    "internal_links":   (FUNCTIONAL, "malformed_link"),
    "no_broken_links":  (FUNCTIONAL, "footer_dead_page"),
    "nav_links_work":   (FUNCTIONAL, "dropdown_dead"),
    "logo_links_home":  (FUNCTIONAL, "cta_no_destination"),
    "footer_links":     (FUNCTIONAL, "footer_dead_page"),
    "login_form":       (FUNCTIONAL, "form_unfillable"),
    "form_fields":      (FUNCTIONAL, "form_unfillable"),
    "title_on_all_pages": (FUNCTIONAL, "homepage_no_title"),
    "h1_on_all_pages":  (FUNCTIONAL, "homepage_no_h1"),
    "page_title":       (FUNCTIONAL, "homepage_no_title"),
    "h1_displayed":     (FUNCTIONAL, "homepage_no_h1"),

    # ── SEO. Not one of the six, and forcing it into Security or
    # Performance would misfile it. Findability is what a marketing site
    # is FOR, so it lands in Functional with its own defect classes. ──
    "meta_description": (FUNCTIONAL, "seo_meta_missing"),
    "canonical":        (FUNCTIONAL, "seo_canonical_missing"),
    "og_tags":          (FUNCTIONAL, "seo_og_missing"),
    "copyright":        (FUNCTIONAL, "copyright_missing"),
}

#: Severity for the defect classes this module introduces, in the same
#: vocabulary as engine.bug_template.CLASS_SEVERITY. Merged into that table
#: at import so the existing severity/priority machinery keeps working.
NEW_CLASS_SEVERITY: dict[str, str] = {
    # Performance — a slow page is a real defect but it is not a stoppage.
    "slow_page_load":       "Major",
    "slow_response":        "Major",
    "heavy_page":           "Minor",
    "unoptimised_images":   "Minor",
    # Security
    "no_https":             "Critical",
    "mixed_content":        "Major",
    "password_not_masked":  "Critical",
    # Accessibility
    "missing_alt_text":     "Major",
    "unlabelled_field":     "Major",
    "no_focus_indicator":   "Major",
    "missing_landmarks":    "Minor",
    "low_contrast":         "Major",
    "missing_lang":         "Minor",
    "heading_hierarchy":    "Minor",
    # UI & UX
    "logo_missing":         "Major",
    "favicon_missing":      "Trivial",
    "section_order":        "Minor",
    "placeholder_content":  "Major",
    "footer_missing":       "Major",
    "header_inconsistent":  "Minor",
    "nav_missing":          "Critical",
    "breadcrumbs_missing":  "Trivial",
    # Usability
    "no_viewport_meta":     "Major",
    "cta_missing":          "Major",
    "unreadable_content":   "Minor",
    "no_required_marker":   "Minor",
    "footer_inconsistent":  "Minor",
    "nav_inconsistent":     "Major",
    "double_submit":        "Major",
    "search_missing":       "Minor",
    "mailto_broken":        "Minor",
    "tel_broken":           "Minor",
    # SEO
    "seo_meta_missing":     "Minor",
    "seo_canonical_missing": "Minor",
    "seo_og_missing":       "Trivial",
    "copyright_missing":    "Trivial",
}


def _register_severities() -> None:
    """Teach ``bug_template`` the severities for the new defect classes.

    Done at import rather than by editing CLASS_SEVERITY directly so the
    two tables stay readable on their own: that file is about how bad a
    defect is, this one is about what kind it is.
    """
    try:
        from engine import bug_template
    except Exception as exc:  # pragma: no cover — defensive
        _logger.debug("bug_areas: cannot register severities: %s", exc)
        return
    for cls, severity in NEW_CLASS_SEVERITY.items():
        bug_template.CLASS_SEVERITY.setdefault(cls, severity)


_register_severities()


# ── Resolution ───────────────────────────────────────────────────────

def coerce_area(value: Any) -> str:
    """Normalise a stored / submitted area onto :data:`AREAS`."""
    raw = str(value or "").strip().lower().replace("&", "and")
    for area in AREAS:
        if raw == area.lower().replace("&", "and"):
            return area
    # Tolerate the shorthands an operator or an import might use.
    alias = {
        "ui": UI_UX, "ux": UI_UX, "uiux": UI_UX, "ui ux": UI_UX,
        "design": UI_UX, "visual": UI_UX,
        "a11y": ACCESSIBILITY, "accessible": ACCESSIBILITY,
        "perf": PERFORMANCE, "speed": PERFORMANCE,
        "sec": SECURITY, "vulnerability": SECURITY,
        "func": FUNCTIONAL, "functionality": FUNCTIONAL,
        "usable": USABILITY, "ux writing": USABILITY,
    }.get(raw)
    return alias or ""


def area_for_class(defect_class: Any) -> str:
    """The area a defect class belongs to."""
    key = str(defect_class or "").strip().lower()
    if key in CLASS_AREA:
        return CLASS_AREA[key]
    for _check, (area, cls) in CHECK_AREA.items():
        if cls == key:
            return area
    return DEFAULT_AREA


def area_for_check(check_key: Any) -> tuple[str, str]:
    """``(area, defect_class)`` for a ``site_tester`` check key."""
    key = str(check_key or "").strip().lower()
    return CHECK_AREA.get(key, (DEFAULT_AREA, "unknown"))


def resolve_area(bug: Any) -> str:
    """The area for a bug, preferring what a human already chose.

    Precedence: an explicit ``bug_area`` an operator set, then the
    ``defect_class``, then the default. A stored area is never overwritten
    by a derived one — triage is a judgement, and re-deriving it on every
    read would silently undo it.
    """
    def _get(key: str) -> Any:
        if isinstance(bug, dict):
            return bug.get(key)
        return getattr(bug, key, None)

    explicit = coerce_area(_get("bug_area"))
    if explicit:
        return explicit

    cls = _get("defect_class")
    if not cls:
        extra = _get("extra")
        if isinstance(extra, dict):
            cls = extra.get("defect_class")
            explicit = coerce_area(extra.get("bug_area"))
            if explicit:
                return explicit
    if cls:
        return area_for_class(cls)

    # Labels are how the walkthrough bug factory carries its class.
    labels = _get("labels")
    if not labels and isinstance(_get("extra"), dict):
        labels = (_get("extra") or {}).get("labels")
    for label in (labels or []):
        text = str(label or "")
        if text.startswith("defect:"):
            return area_for_class(text.split(":", 1)[1])
        if text.startswith("area:"):
            found = coerce_area(text.split(":", 1)[1].replace("_", " "))
            if found:
                return found
    return DEFAULT_AREA


def counts_by_area(bugs: Any) -> dict[str, int]:
    """``{area: n}`` over every area, zeros included.

    Zeros are deliberate: a filter chip that vanishes when its bucket is
    empty reads as "this product has no accessibility defects", when what
    it means is "nobody has looked".
    """
    out = {area: 0 for area in AREAS}
    for bug in (bugs or []):
        area = resolve_area(bug)
        out[area] = out.get(area, 0) + 1
    return out


__all__ = [
    "AREAS", "DEFAULT_AREA",
    "FUNCTIONAL", "UI_UX", "USABILITY", "ACCESSIBILITY", "PERFORMANCE",
    "SECURITY",
    "CLASS_AREA", "CHECK_AREA", "NEW_CLASS_SEVERITY",
    "coerce_area", "area_for_class", "area_for_check", "resolve_area",
    "counts_by_area",
]
