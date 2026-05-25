"""TestForTge — Autonomous QA walkthrough runner (TFWefloLab port).

This module mirrors :mod:`engine.automation_runner`'s public surface
(``run() -> RunReport``) so :mod:`engine.runner_worker` can dispatch
to it via a ``mode="walkthrough"`` config field without touching the
existing TC-driven code path.

PR-1 landed the scaffold (navigate + screenshot, empty ``findings``).
PR-2 (this revision) adds the exploration heuristic battery ported
from ``F:/ClaudeProjects/Webflow/Testing/tests/walkthrough.spec.js``:

* Broken-image scan      — ``naturalWidth === 0`` per visible ``<img>``
* Hamburger / dropdown   — mobile menu open-check + desktop dropdown
                            hover/click probe
* Footer + social-link   — placeholder hosts + missing ``rel=noopener``
* Search-field probe     — open trigger, fill "test", poll for results
* Form auto-fill         — type-aware sample data, **no submit**
* CTA / tap-target audit — buttons with no destination, sub-24 px hit
                            areas, disabled CTAs
* axe-core a11y          — ``add_script_tag`` loads the script over CDN
                            then ``page.evaluate("axe.run(...)")``
* Console / page errors  — noise-filtered, humanised headlines

Every heuristic emits ``dict`` findings into ``self.findings`` with a
shape compatible with :func:`engine.walkthrough_dedup.fingerprint` and
:func:`engine.bug_report.create_bug_from_walkthrough_finding`:

    {
        "severity":    "Critical | Major | Minor | Trivial",
        "defect_class": str,       # bug_template.CLASS_SEVERITY key
        "area":        "Images | Navigation | Footer | ...",
        "message":     "Human-friendly headline",
        "url":         "https://...",          # page where defect surfaced
        "element":     "img[src=...]",         # selector / hint (optional)
        "screenshot":  "automation_runs/.../img-broken-1.png",
        "fix_hint":    "Open the URL in a new tab; if 404 ...",  # opt
        "dev_detail":  "naturalWidth=0\\nparent .hero",            # opt
        "tc_id":       "WALK-IMG-001",         # synthesised, stable
        "console_errors": [...],
    }

Reuses dataclasses from :mod:`engine.automation_runner` so the
existing :func:`engine.runner_worker._serialise_report` writer
handles walkthrough results unchanged.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime
from typing import Any

from .automation_runner import (
    AUTOMATION_RUN_MAX_KEPT,
    AUTOMATION_RUN_RETENTION_DAYS,
    RunReport,
    ScriptResult,
    StepResult,
    _purge_old_automation_runs,
)
from .bug_template import CLASS_SEVERITY
from .log import get_logger
from .walkthrough_dedup import dedupe as _dedupe_findings
from .walkthrough_tc_match import match_tcs_for_url

_logger = get_logger(__name__)


# Feature flag — controls whether ``runner_worker`` will even consider
# dispatching to a walkthrough run. Default OFF so existing deployments
# never see a behaviour change from this PR landing.
WALKTHROUGH_FLAG_ENV = "WALKTHROUGH_MODE_ENABLED"

# Severity hierarchy used in findings — matches CLASS_SEVERITY values
# so the bug-template ladder lines up. ``Trivial`` is reserved for
# cosmetic-only defects the heuristics don't currently emit.
_SEV_CRITICAL = "Critical"
_SEV_MAJOR    = "Major"
_SEV_MINOR    = "Minor"

# Browser-noise filter (ported from walkthrough.spec.js:49–:57). Errors
# whose URL matches these schemes come from the browser / DevTools /
# extensions, NOT the site under test. Operators reported false-positive
# "site bugs" pointing at ``web-inspector://bootstrap.js`` before this
# guard.
_NOISE_URL_RE = re.compile(
    r"\b(web-inspector|chrome-extension|chrome|devtools|edge-extension|"
    r"moz-extension|safari-extension|safari-web-extension|extension|"
    r"webkit-masked-url):", re.I)
_NOISE_MSG_RE = re.compile(
    r"\bResizeObserver loop|\bNon-Error promise rejection captured|"
    r"\bScript error\.?$", re.I)
# Third-party services that habitually log errors that aren't site bugs.
_IGNORABLE_CONSOLE_RE = re.compile(
    r"favicon|google-analytics|googletagmanager|facebook\.net|hotjar|"
    r"hubspot|intercom|net::ERR_BLOCKED_BY_CLIENT", re.I)

# Webflow / generic "bad" placeholder hosts that signal an unconfigured
# footer social link (`example.com`, `localhost`, `change-me`, ...).
_BAD_SOCIAL_HOST_RE = re.compile(
    r"(localhost|127\.0\.0\.1|example\.com|placeholder|tbd|"
    r"change[-_ ]?me)", re.I)

# axe-core script loaded over CDN. We use ``page.add_script_tag`` rather
# than carrying a binary copy in-tree — the CDN URL is pinned to a
# specific minor version so the result is reproducible. The CDN reach
# is best-effort; on failure the a11y step silently no-ops (the rest of
# the walkthrough still runs and produces findings).
_AXE_CDN_URL = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.8.4/axe.min.js"


def feature_enabled() -> bool:
    """Whether the walkthrough dispatch path is currently active.

    A simple env-var check kept in this module so the route layer can
    import a single boolean without re-reading ``os.environ`` inline.
    Always reads fresh so test fixtures can flip it via
    ``monkeypatch.setenv`` without restart.
    """
    return (os.environ.get(WALKTHROUGH_FLAG_ENV) or "").strip() == "1"


# ── Public stateless scan helpers ─────────────────────────────────
#
# Stage 3 (live_executor) needs the same heuristic battery without
# bringing along WalkthroughRunner's instance state. Each helper now
# lives as a module-level function that takes an explicit ``note``
# callable — the runner records findings, the scanner only describes
# them. WalkthroughRunner._scan_* methods below stay as thin wrappers
# that bind ``self._note`` so existing call sites (and the PR-2 test
# suite) keep working byte-identically.
#
# Each helper has the signature:
#
#     scan_<name>(page, url, tc_id, *, note, device_kind="desktop",
#                 max_form_fills=5, axe_enabled=True)
#
# ``note`` matches WalkthroughRunner._note's keyword shape so a runner
# can pass it in directly.


def scan_broken_images(page, url: str, tc_id: str, *, note) -> None:
    """Walk ``document.images``; flag any whose ``naturalWidth`` is
    zero. See WalkthroughRunner._scan_broken_images for the rationale.
    """
    broken = page.evaluate(WalkthroughRunner._JS_FIND_BROKEN_IMAGES) or []
    for img in broken:
        src = img.get("src") or ""
        alt = img.get("alt") or ""
        try:
            from urllib.parse import urlparse
            filename = urlparse(src).path.rsplit("/", 1)[-1] or src
        except Exception:
            filename = src.rsplit("/", 1)[-1] if "/" in src else src
        headline = (
            f'"{alt}" did not load' if alt
            else f'{filename} did not load'
        )
        note(
            _SEV_MAJOR, "Images", "broken_image",
            f"Broken image on the page — {headline} "
            f"(visitors see an empty slot or a broken-image icon)",
            url=url, tc_id=tc_id,
            element=f'img[src="{src}"]',
            fix_hint=(
                "Open the URL in a new tab. If 404, re-upload the "
                "asset and re-bind in the page. If it loads in a "
                "tab but not on the page, check CORS / hotlink "
                "protection and that the path uses HTTPS."
            ),
            dev_detail=(
                f"<img src='{src}'{' alt=' + repr(alt) if alt else ''}> "
                f"· naturalWidth = 0\n"
                f"parent .{(img.get('parentCls') or '').split()[0] if img.get('parentCls') else 'unknown'}"
            ),
            user_impact=(
                "Visitors see a broken-image icon, an empty white "
                "box, or a layout that jumps when the image fails. "
                "On marketing pages this directly impacts perceived "
                "credibility and conversion."
            ),
        )


def scan_navigation_menu(page, url: str, tc_id: str, *, note,
                          device_kind: str = "desktop") -> None:
    """Mobile/tablet: tap hamburger, verify visible nav-link count rises.
    Desktop: probe dropdown popup visibility. See _scan_navigation_menu.
    """
    if device_kind in ("mobile", "tablet"):
        burger_sel = (
            '.w-nav-button, [aria-label*="menu" i], '
            'button[class*="menu" i], button[class*="hamburger" i]')
        burger = page.locator(burger_sel).first
        try:
            if not burger.count():
                return
        except Exception:
            return
        try:
            before = page.evaluate(WalkthroughRunner._JS_COUNT_VISIBLE_NAV) or 0
            burger.click(timeout=5000)
            page.wait_for_timeout(700)
            after = page.evaluate(WalkthroughRunner._JS_COUNT_VISIBLE_NAV) or 0
        except Exception as exc:
            note(
                _SEV_MINOR, "Navigation", "dropdown_dead",
                f"Hamburger trigger threw on click: "
                f"{type(exc).__name__}",
                url=url, tc_id=tc_id, element=burger_sel,
                fix_hint=(
                    "The button may be obscured by another element "
                    "or its click handler throws. Check the on-click "
                    "interaction in the page builder."
                ),
            )
            return
        if after <= before:
            note(
                _SEV_CRITICAL, "Navigation", "hamburger_dead",
                "The hamburger / mobile menu does not open "
                "when tapped",
                url=url, tc_id=tc_id, element=burger_sel,
                user_impact=(
                    f"Mobile and tablet visitors cannot reach any "
                    f"page other than this one. Visible nav links: "
                    f"{before} before tap, {after} after."
                ),
                fix_hint=(
                    "Either the on-click interaction is unbound, or "
                    "the menu element has a CSS override keeping it "
                    "hidden at this breakpoint (display:none, "
                    "visibility:hidden, off-screen transform)."
                ),
            )
    else:
        try:
            probes = page.evaluate(WalkthroughRunner._JS_PROBE_DROPDOWNS) or []
        except Exception:
            return
        for p in probes:
            if not p.get("hasList"):
                continue
            if not p.get("popupVisible"):
                note(
                    _SEV_MINOR, "Navigation", "dropdown_dead",
                    f'Dropdown "{p.get("label")}" did not render '
                    "an open sub-menu during the probe",
                    url=url, tc_id=tc_id,
                    element=".w-dropdown-toggle",
                )


def scan_footer_social(page, url: str, tc_id: str, *, note) -> None:
    """Scroll to footer; flag placeholder hosts and missing rel=noopener."""
    try:
        page.evaluate(
            "() => window.scrollTo({top: document.body.scrollHeight,"
            " behavior: 'smooth'})")
        page.wait_for_timeout(500)
    except Exception:
        pass
    socials = page.evaluate(WalkthroughRunner._JS_FOOTER_SOCIALS) or []
    from urllib.parse import urlparse
    for s in socials:
        href = (s.get("href") or "").strip()
        label = s.get("label") or ""
        if not href:
            continue
        try:
            host = urlparse(href).hostname or ""
        except Exception:
            note(
                _SEV_MINOR, "Footer", "malformed_link",
                f"Malformed social link: {href}",
                url=url, tc_id=tc_id, element=href,
            )
            continue
        if _BAD_SOCIAL_HOST_RE.search(host):
            note(
                _SEV_MAJOR, "Footer", "placeholder_social",
                f"Social link points to placeholder host: {host}",
                url=url, tc_id=tc_id, element=href,
                user_impact=(
                    "Visitors clicking the social icon land on a "
                    "broken or unrelated page; perceived credibility "
                    "drops on a marketing site."
                ),
                fix_hint=(
                    "Update the link href to the brand's real "
                    "social profile, or remove the icon until it "
                    "has a destination."
                ),
            )
        elif (s.get("target") == "_blank"
                and "noopener" not in (s.get("rel") or "")):
            note(
                _SEV_MINOR, "Security", "social_no_noopener",
                "External social link opens in a new tab without "
                'rel="noopener"',
                url=url, tc_id=tc_id, element=href,
                fix_hint=(
                    'Add rel="noopener noreferrer" so window.opener '
                    "leaks are prevented and link rel security is "
                    "consistent."
                ),
            )


def scan_search_field(page, url: str, tc_id: str, *, note) -> None:
    """Open search if behind toggle, fill 'test', poll for results."""
    for sel in (
        'button[aria-label*="search" i]',
        '[role="button"][aria-label*="search" i]',
        'a[aria-label*="search" i]',
        '.search-toggle, .nav-search, .search-icon, .search-button',
    ):
        t = page.locator(sel).first
        try:
            if not t.count():
                continue
            if not t.is_visible():
                continue
            t.click(timeout=2500)
            page.wait_for_timeout(400)
            break
        except Exception:
            continue
    input_cands = page.locator(
        'input[type="search"], input[role="searchbox"], '
        'input[aria-label*="search" i], '
        'input[placeholder*="search" i], '
        '[role="search"] input[type="text"], '
        '[role="search"] input:not([type])')
    try:
        n = input_cands.count()
    except Exception:
        return
    search_input = None
    for i in range(n):
        c = input_cands.nth(i)
        try:
            if c.is_visible():
                search_input = c
                break
        except Exception:
            continue
    if search_input is None:
        return
    try:
        search_input.click(timeout=2500)
        search_input.fill("test")
        page.wait_for_timeout(1200)
        observed = page.evaluate(WalkthroughRunner._JS_OBSERVE_SEARCH_RESULTS) or {}
    except Exception as exc:
        note(
            _SEV_MAJOR, "Search", "search_broken",
            f"Search field interaction failed: "
            f"{type(exc).__name__}",
            url=url, tc_id=tc_id,
            fix_hint=(
                "Verify the search input is focusable and accepts "
                "keyboard input on this device."
            ),
        )
        return
    if not observed.get("found"):
        note(
            _SEV_MAJOR, "Search", "search_no_results",
            'The search field accepts the query "test" but no '
            "suggestions or results appear within 1.2 s",
            url=url, tc_id=tc_id, element='input[type="search"]',
            fix_hint=(
                "Confirm the search submits to a working endpoint "
                "(Site Search / Algolia / etc.); verify the "
                "dropdown isn't hidden by overflow or z-index."
            ),
        )
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
    except Exception:
        pass


def scan_forms(page, url: str, tc_id: str, *, note,
               max_form_fills: int = 5) -> None:
    """Auto-fill every visible input/textarea in up to ``max_form_fills``
    forms with type-aware sample data. **Never submits.**"""
    forms = page.locator("form")
    try:
        form_count = forms.count()
    except Exception:
        return
    for fi in range(min(form_count, max_form_fills)):
        form = forms.nth(fi)
        try:
            if not form.is_visible():
                continue
        except Exception:
            continue
        inputs = form.locator(
            'input[type=text], input[type=email], '
            'input[type=tel], input[type=url], '
            'input[type=number], input:not([type]), textarea')
        try:
            n = inputs.count()
        except Exception:
            continue
        for k in range(min(n, 8)):
            inp = inputs.nth(k)
            try:
                if not inp.is_visible():
                    continue
                if inp.is_disabled():
                    continue
                itype = inp.get_attribute("type") or ""
                sample = (
                    "qa+test@example.com" if itype == "email" else
                    "+10000000000"        if itype == "tel"   else
                    "https://example.com" if itype == "url"   else
                    "42"                  if itype == "number" else
                    "QA test"
                )
                inp.fill(sample, timeout=2500)
            except Exception as exc:
                note(
                    _SEV_MAJOR, "Forms", "form_unfillable",
                    f"Could not fill input #{k + 1} in form #"
                    f"{fi + 1}: {type(exc).__name__}",
                    url=url, tc_id=tc_id,
                    fix_hint=(
                        "Check that the input is not covered by "
                        "an overlay and accepts keyboard input; "
                        "remove any readonly/disabled attribute "
                        "that's left over from prototype state."
                    ),
                )


def scan_ctas(page, url: str, tc_id: str, *, note) -> None:
    """Audit visible buttons / link.button / role=button for missing
    destinations, sub-24px tap targets, and disabled state."""
    issues = page.evaluate(WalkthroughRunner._JS_CTA_AUDIT) or []
    for it in issues:
        reason = it.get("reason") or ""
        text   = it.get("text") or "(no text)"
        sel    = it.get("sel") or ""
        if reason == "no destination":
            note(
                _SEV_MAJOR, "CTAs", "cta_no_destination",
                f'"{text}" — link has no destination '
                "(href empty, href=\"#\", or no click handler)",
                url=url, tc_id=tc_id, element=sel,
                fix_hint=(
                    "Either set a real href on the link, or remove "
                    "the call-to-action if there's nothing for it "
                    "to do yet."
                ),
            )
        elif reason.startswith("tap target"):
            note(
                _SEV_MINOR, "CTAs", "cta_tiny_tap_target",
                f'"{text}" — {reason}',
                url=url, tc_id=tc_id, element=sel,
                fix_hint=(
                    "Increase the minimum hit area to 24x24 px "
                    "(WCAG 2.5.5). Padding around the element "
                    "counts."
                ),
            )
        elif reason == "disabled":
            note(
                _SEV_MINOR, "CTAs", "cta_disabled",
                f'"{text}" — rendered as disabled on the published '
                "page",
                url=url, tc_id=tc_id, element=sel,
            )


def scan_axe(page, url: str, tc_id: str, *, note,
             axe_enabled: bool = True) -> None:
    """Inject axe-core via CDN, run rules, emit critical/serious as findings."""
    if not axe_enabled:
        return
    try:
        page.add_script_tag(url=_AXE_CDN_URL)
    except Exception as exc:
        _logger.debug("walkthrough: axe CDN unreachable: %s", exc)
        return
    try:
        result = page.evaluate(WalkthroughRunner._JS_RUN_AXE) or {}
    except Exception as exc:
        _logger.debug("walkthrough: axe.run failed: %s", exc)
        return
    if result.get("error"):
        _logger.debug("walkthrough: axe error: %s", result["error"])
        return
    for v in (result.get("violations") or []):
        impact = v.get("impact") or ""
        if impact not in ("critical", "serious"):
            continue
        nodes = v.get("nodes") or []
        node0 = nodes[0] if nodes else {}
        tags  = v.get("tags") or []
        wcag_tag = next(
            (t for t in tags if t.startswith("wcag")), "")
        help_text = (v.get("help") or "").strip()
        headline = (
            f"{help_text} — affects {len(nodes)} element"
            f"{'' if len(nodes) == 1 else 's'} on this page"
        )
        note(
            _SEV_CRITICAL if impact == "critical" else _SEV_MAJOR,
            "Accessibility",
            "axe_critical" if impact == "critical" else "axe_serious",
            headline,
            url=url, tc_id=tc_id,
            element=node0.get("target") or "",
            fix_hint=(
                (node0.get("failureSummary") or "")
                + (f"\nWCAG: {wcag_tag}" if wcag_tag else "")
                + (f"\nDocs: {v.get('helpUrl')}"
                    if v.get("helpUrl") else "")
            ).strip(),
            dev_detail=(
                f"axe rule: {v.get('id')}\n"
                f"impact: {impact}\n"
                f"nodes affected: {len(nodes)}\n"
                f"first node selector: {node0.get('target') or '?'}\n"
                f"first node HTML: {node0.get('html') or ''}"
            ),
        )


def collect_console_errors(console_errors: list[dict[str, Any]],
                            page_errors: list[dict[str, Any]],
                            url: str, tc_id: str, *, note) -> None:
    """Emit one finding per unique error, filtering DevTools/extension noise."""
    seen_page: set[str] = set()
    for err in page_errors:
        msg = err.get("message") or ""
        combined = " ".join(filter(None, (
            msg,
            err.get("first_frame") or "",
            (err.get("stack") or "")[:400],
        )))
        if _NOISE_URL_RE.search(combined) or _NOISE_MSG_RE.search(msg):
            continue
        key = msg[:200]
        if key in seen_page:
            continue
        seen_page.add(key)
        note(
            _SEV_MAJOR, "JS", "page_error",
            _humanize_js_error(msg),
            url=url, tc_id=tc_id,
            element=err.get("first_frame") or "",
            dev_detail=(
                f"{msg}\n  at {err.get('first_frame') or ''}\n\n"
                + "\n".join((err.get("stack") or "").split("\n")[:6])
            ).strip(),
        )

    seen_con: set[str] = set()
    for err in console_errors:
        text = err.get("text") or ""
        if _IGNORABLE_CONSOLE_RE.search(text):
            continue
        if _NOISE_URL_RE.search(text + " " + (err.get("url") or "")):
            continue
        key = text[:200]
        if key in seen_con:
            continue
        seen_con.add(key)
        where = ""
        if err.get("url") and ".js" in (err.get("url") or ""):
            where = (
                f"{err.get('url')}"
                f"{':' + str(err.get('line')) if err.get('line') else ''}"
                f"{':' + str(err.get('col'))  if err.get('col')  else ''}"
            )
        note(
            _SEV_MAJOR, "Console", "console_js_error",
            _humanize_js_error(text),
            url=url, tc_id=tc_id,
            element=where,
            dev_detail=f"console.error: {text}"
                        + (f"\n  at {where}" if where else ""),
        )


def install_error_listeners(page,
                             console_errors: list[dict[str, Any]],
                             page_errors: list[dict[str, Any]]) -> None:
    """Wire page.on('console') / page.on('pageerror') to capture browser-
    side errors fired during the session."""
    if not hasattr(page, "on"):
        return

    def _on_console(msg):
        try:
            if getattr(msg, "type", lambda: "")() != "error":
                return
            loc = {}
            try:
                loc = msg.location() or {}
            except Exception:
                loc = {}
            console_errors.append({
                "text":     getattr(msg, "text", lambda: "")(),
                "url":      loc.get("url") or page.url,
                "line":     loc.get("lineNumber"),
                "col":      loc.get("columnNumber"),
                "page_url": page.url,
            })
        except Exception:
            pass

    def _on_pageerror(err):
        try:
            stack = str(getattr(err, "stack", "") or "")
            m = re.search(r"\bat\s+.+?\(([^)]+)\)", stack)
            first_frame = m.group(1) if m else ""
            if not first_frame:
                for line in stack.split("\n"):
                    if re.search(r"\.js:\d+", line):
                        first_frame = line.strip()
                        break
            page_errors.append({
                "message":     str(getattr(err, "message", err) or ""),
                "stack":       stack,
                "first_frame": first_frame,
                "page_url":    page.url,
            })
        except Exception:
            pass

    try:
        page.on("console",   _on_console)
        page.on("pageerror", _on_pageerror)
    except Exception as exc:
        _logger.debug("walkthrough: page.on hook failed: %s", exc)


class WalkthroughRunner:
    """Drop-in replacement for :class:`AutomationRunner` when the
    config carries ``mode="walkthrough"``.

    Constructor accepts a subset of ``AutomationRunner.__init__`` args
    plus walkthrough-specific knobs (``max_pages``, ``device_timeout_ms``).
    Unknown kwargs are silently ignored so the dispatch layer can pass
    one ``runner_kwargs`` dict for both runners without per-runner
    keyword-list maintenance.
    """

    def __init__(
        self,
        storage_root: str,
        base_url: str,
        *,
        headless: bool = True,
        viewport: tuple[int, int] = (1280, 800),
        navigation_timeout_ms: int = 45000,
        device_timeout_ms: int = 480000,
        max_pages: int = 6,
        max_form_fills: int = 5,
        axe_enabled: bool = True,
        record_video: bool = False,
        test_cases: list[dict[str, Any]] | None = None,
        **_ignored: Any,
    ):
        self.storage_root = storage_root
        self.base_url = (base_url or "").strip()
        self.headless = bool(headless)
        # Cap headless viewport identically to AutomationRunner — the
        # same Render-free-tier reasoning applies (0.1 CPU cannot
        # screenshot 1920x1080 inside our timeout budget). See the
        # comment block in automation_runner.AutomationRunner.__init__
        # for the operator-reported diag that motivated this cap.
        chosen = tuple(viewport)
        if headless and chosen[0] > 1280 and chosen[1] > 800:
            self.viewport = (1280, 800)
        else:
            self.viewport = chosen
        self.navigation_timeout_ms = int(navigation_timeout_ms)
        self.device_timeout_ms = int(device_timeout_ms)
        self.max_pages = max(1, int(max_pages))
        # Soft cap on forms to fill per page. Five matches the upstream
        # spec; production sites with many disjoint forms (search +
        # newsletter + contact + login + custom Webflow form) can still
        # all be covered, but a CMS-driven page with N+ identical reply
        # forms doesn't blow the per-device budget.
        self.max_form_fills = max(1, int(max_form_fills))
        # Whether to inject axe-core and run the a11y scan. Off-line
        # environments (CI without external network) flip this off so
        # the CDN ``page.add_script_tag`` never times out the run.
        self.axe_enabled = bool(axe_enabled)
        self.record_video = bool(record_video)
        # Findings ring — appended to by the heuristic battery in
        # ``_walk_one``. Each dict has the shape documented in the
        # module docstring. The ring is per-runner, not per-page: a
        # single walkthrough that visits 6 pages accumulates findings
        # from every page in one list, ordered by emit time.
        self.findings: list[dict] = []
        # Project's TestCases as plain dicts. Empty list = no TC binding
        # (walkthrough only runs the heuristic battery). PR-2 uses this
        # to surface URL-pattern matches via the new ``tc_bindings``
        # field on each per-URL ScriptResult — actual TC script
        # execution (running the parsed steps through the existing
        # _run_script pipeline) lands in PR-3 along with the UI radio,
        # where the route layer wires in credentials + env metadata.
        self.test_cases: list[dict[str, Any]] = list(test_cases or [])
        # Recorded TC bindings per URL, populated by ``_walk_one`` so
        # the debug endpoint + future PR-3 UI can render which TCs
        # would have fired without yet running them.
        self.tc_bindings: list[dict[str, Any]] = []

    # ── device classification ───────────────────────────────────────

    @property
    def device_kind(self) -> str:
        """``mobile`` / ``tablet`` / ``desktop`` derived from viewport.

        Used by the heuristic battery to decide which probes apply at
        the current breakpoint (hamburger vs. dropdown, tap-target size
        threshold). Mirrors TFWefloLab's :func:`classifyDevice`.
        """
        w = self.viewport[0]
        if w <= 480:
            return "mobile"
        if w <= 1024:
            return "tablet"
        return "desktop"

    # ── findings emitter ────────────────────────────────────────────

    def _note(self, severity: str, area: str, defect_class: str,
              message: str, *, url: str, tc_id: str,
              element: str = "", screenshot: str = "",
              fix_hint: str = "", dev_detail: str = "",
              user_impact: str = "") -> None:
        """Append a structured finding to ``self.findings``.

        Keeps the per-heuristic call-site terse — one ``self._note(...)``
        per detected defect, no manual dict construction. Severity must
        match ``CLASS_SEVERITY``'s ladder; an unknown ``defect_class``
        is logged but still recorded so operators can see it in
        ``findings.json`` even when the bug-template doesn't know about
        it yet.
        """
        if defect_class not in CLASS_SEVERITY:
            _logger.debug("walkthrough: unknown defect_class %r — "
                          "still recording but bug-template won't "
                          "produce a customised template",
                          defect_class)
        self.findings.append({
            "severity":     severity,
            "area":         area,
            "defect_class": defect_class,
            "message":      message,
            "url":          url,
            "element":      element,
            "screenshot":   screenshot,
            "fix_hint":     fix_hint,
            "dev_detail":   dev_detail,
            "user_impact":  user_impact,
            "tc_id":        tc_id,
            "console_errors": [],
        })

    # ── live-feed helpers ─────────────────────────────────────────

    def _reset_live_dir(self, runs_root: str) -> str:
        """Wipe the shared ``_live`` dir at run start and return its
        path. Mirrors ``AutomationRunner._reset_live_dir`` in spirit so
        the existing ``/test-execution/live`` route picks up walk-
        through frames with no template changes.
        """
        live_dir = os.path.join(runs_root, "_live")
        try:
            os.makedirs(live_dir, exist_ok=True)
            stale = os.path.join(live_dir, "latest.png")
            if os.path.exists(stale):
                os.remove(stale)
            strip_dir = os.path.join(live_dir, "strip")
            if os.path.isdir(strip_dir):
                for fn in os.listdir(strip_dir):
                    try:
                        os.remove(os.path.join(strip_dir, fn))
                    except OSError:
                        pass
        except OSError as exc:
            _logger.debug("walkthrough live reset failed: %s", exc)
            return ""
        return live_dir

    def _write_live_info(self, live_dir: str, *, status: str,
                         total: int, done: int, current_url: str = "",
                         run_id: str = "",
                         started_ts: float = 0.0) -> None:
        """Atomic write of ``_live/info.json`` so the /live endpoint
        picks up walkthrough progress with no template changes."""
        if not live_dir:
            return
        info = {
            "status": status,
            "run_id": run_id,
            "total": total,
            "done": done,
            "current_tc": current_url,  # ``current_tc`` is what the
            "current_url": current_url,  # template reads — walkthrough
                                          # uses URL where TC-driven uses TC id.
            "elapsed_ms": int(max(0.0, time.time() - started_ts) * 1000),
            "mode": "walkthrough",
        }
        path = os.path.join(live_dir, "info.json")
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(info, f)
            os.replace(tmp, path)
        except OSError as exc:
            _logger.debug("walkthrough live info write failed: %s", exc)

    # ── main entry point ──────────────────────────────────────────

    def run(self, start_urls: list[str] | None = None) -> RunReport:
        """Walk a list of URLs and produce a :class:`RunReport`.

        ``start_urls`` defaults to ``[self.base_url]`` so the simplest
        invocation matches the existing single-URL Test Execution form.
        Each URL becomes one :class:`ScriptResult` whose ``tc_id`` is
        a stable ``WALK-N`` identifier — the audit trail can quote it
        the same way it quotes ``TC-001``.

        PR-1 scaffold: navigates, captures one screenshot per URL,
        no findings. PR-2 will fill ``self.findings`` and emit one
        :class:`StepResult` per detected defect class.
        """
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover — playwright is
            # required for any real run; the scaffold returns a clean
            # "blocked" report so the worker can surface the error
            # without crashing.
            _logger.error("playwright import failed: %s", exc)
            return RunReport(
                run_id="",
                started_at=datetime.now().isoformat(timespec="seconds"),
                finished_at=datetime.now().isoformat(timespec="seconds"),
                base_url=self.base_url,
                headless=self.headless,
                total=0,
                blocked=1,
                scripts=[],
            )

        urls = [u.strip() for u in (start_urls or [self.base_url]) if u and u.strip()]
        if not urls:
            return RunReport(
                run_id="",
                started_at=datetime.now().isoformat(timespec="seconds"),
                finished_at=datetime.now().isoformat(timespec="seconds"),
                base_url=self.base_url,
                headless=self.headless,
                total=0,
                scripts=[],
            )
        urls = urls[: self.max_pages]

        run_id = (datetime.now().strftime("%Y%m%d_%H%M%S_")
                  + uuid.uuid4().hex[:6])
        runs_root = os.path.join(self.storage_root, "automation_runs")
        run_dir = os.path.join(runs_root, run_id)
        os.makedirs(run_dir, exist_ok=True)

        # Retention purge — same policy as AutomationRunner so walk-
        # through runs don't fill the free-tier disk.
        try:
            _purge_old_automation_runs(
                runs_root,
                AUTOMATION_RUN_RETENTION_DAYS,
                AUTOMATION_RUN_MAX_KEPT,
            )
        except Exception as exc:
            _logger.debug("walkthrough retention purge skipped: %s", exc)

        live_dir = self._reset_live_dir(runs_root)
        started_ts = time.time()
        self._write_live_info(
            live_dir, status="starting", total=len(urls), done=0,
            run_id=run_id, started_ts=started_ts,
        )

        scripts: list[ScriptResult] = []
        wall_deadline = started_ts + (self.device_timeout_ms / 1000.0)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless)
            context_kwargs = {"viewport": {
                "width": self.viewport[0], "height": self.viewport[1],
            }}
            if self.record_video:
                context_kwargs["record_video_dir"] = run_dir
            context = browser.new_context(**context_kwargs)
            try:
                for idx, url in enumerate(urls, start=1):
                    if time.time() >= wall_deadline:
                        # Outer wall-clock kill — every page beyond
                        # this point is reported as blocked so the
                        # operator sees the budget exhaustion.
                        scripts.append(self._blocked_script(idx, url,
                            "device_timeout exceeded before this URL"))
                        continue
                    self._write_live_info(
                        live_dir, status="running",
                        total=len(urls), done=idx - 1,
                        current_url=url, run_id=run_id,
                        started_ts=started_ts,
                    )
                    script = self._walk_one(
                        context, idx, url, run_dir, live_dir,
                    )
                    scripts.append(script)
            finally:
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

        passed = sum(1 for s in scripts if s.status == "passed")
        failed = sum(1 for s in scripts if s.status == "failed")
        blocked = sum(1 for s in scripts if s.status == "blocked")
        duration_ms = int((time.time() - started_ts) * 1000)

        self._write_live_info(
            live_dir, status="done", total=len(urls), done=len(urls),
            run_id=run_id, started_ts=started_ts,
        )

        return RunReport(
            run_id=run_id,
            started_at=datetime.fromtimestamp(started_ts)
                .isoformat(timespec="seconds"),
            finished_at=datetime.now().isoformat(timespec="seconds"),
            base_url=self.base_url,
            headless=self.headless,
            total=len(scripts),
            passed=passed,
            failed=failed,
            blocked=blocked,
            duration_ms=duration_ms,
            scripts=scripts,
        )

    # ── per-URL walk ─────────────────────────────────────────────

    def _walk_one(self, context, idx: int, url: str,
                  run_dir: str, live_dir: str) -> ScriptResult:
        """Visit ``url``, screenshot, run the heuristic battery, and
        return a :class:`ScriptResult` aggregating navigation + the
        per-heuristic step entries.

        Each heuristic appends to ``self.findings`` for downstream bug
        emission, and also emits a corresponding :class:`StepResult`
        with ``status="failed"`` so the existing per-TC results UI
        renders the walkthrough output without any template changes —
        operators can scroll a walkthrough run the same way they scroll
        a TC-driven run.
        """
        tc_id = f"WALK-{idx:03d}"
        page = context.new_page()
        page.set_default_navigation_timeout(self.navigation_timeout_ms)
        steps: list[StepResult] = []
        # Console / page error listeners installed BEFORE navigation
        # so we capture errors thrown during page load (very common
        # for IX2 init failures and other Webflow custom-code bugs).
        console_errors: list[dict[str, Any]] = []
        page_errors: list[dict[str, Any]] = []
        self._install_error_listeners(page, console_errors, page_errors)

        t0 = time.time()

        # ── Step 1: navigation ─────────────────────────────────────
        try:
            page.goto(url, wait_until="domcontentloaded",
                      timeout=self.navigation_timeout_ms)
            shot_path = self._screenshot(page, run_dir, live_dir,
                                          tc_id, "page")
            steps.append(StepResult(
                index=1,
                action="goto",
                raw=url,
                status="passed",
                duration_ms=int((time.time() - t0) * 1000),
                screenshot_after=shot_path,
            ))
            final_url = (page.url if hasattr(page, "url") else "") or url
        except Exception as exc:
            steps.append(StepResult(
                index=1,
                action="goto",
                raw=url,
                status="failed",
                duration_ms=int((time.time() - t0) * 1000),
                comment=f"{type(exc).__name__}: {exc}"[:500],
            ))
            self._note(
                _SEV_CRITICAL, "Loading", "navigation_timeout",
                f"Page failed to load within "
                f"{self.navigation_timeout_ms} ms: "
                f"{type(exc).__name__}",
                url=url, tc_id=tc_id,
                fix_hint=(
                    "Verify the URL is publicly reachable; check the "
                    "hosting status and DNS. Heavy hero videos can "
                    "blow past the default 45s budget — raise "
                    "navigation_timeout_ms if the site is intentionally "
                    "slow to first-paint."
                ),
            )
            try:
                page.close()
            except Exception:
                pass
            return ScriptResult(
                tc_id=tc_id,
                summary=f"Walkthrough: {url}",
                status="failed",
                duration_ms=int((time.time() - t0) * 1000),
                steps=steps,
                comment=f"navigation failed: {type(exc).__name__}",
                final_url=url,
            )

        # ── TC binding (record only — execution lands in PR-3) ─────
        # Walkthrough mode uses URL patterns to decide which project
        # TCs are eligible to run on each visited page. PR-2 records
        # the binding so the debug endpoint + result.json carry an
        # auditable trail; PR-3 wires the actual script execution
        # through ``AutomationRunner._run_script``.
        if self.test_cases:
            matched_tcs = match_tcs_for_url(self.test_cases, final_url)
            if matched_tcs:
                self.tc_bindings.append({
                    "tc_id":   tc_id,
                    "url":     final_url,
                    "matches": [
                        {
                            "id":          t.get("id"),
                            "external_id": t.get("external_id")
                                            or t.get("id"),
                            "summary":     t.get("summary", "")[:120],
                            "url_pattern": t.get("url_pattern", ""),
                            "trigger":     t.get("trigger", ""),
                        }
                        for t in matched_tcs
                    ],
                })

        # ── Steps 2–N: heuristic battery ───────────────────────────
        # Each helper is best-effort: a failure inside one heuristic
        # must NOT abort the rest of the walk, so we wrap each call.
        # The helpers themselves swallow Playwright element-timeouts
        # internally and only re-raise on programmer errors.
        before_count = len(self.findings)
        for label, fn in (
            ("broken_images",    self._scan_broken_images),
            ("navigation_menu",  self._scan_navigation_menu),
            ("footer_social",    self._scan_footer_social),
            ("search_field",     self._scan_search_field),
            ("forms",            self._scan_forms),
            ("ctas",             self._scan_ctas),
            ("axe",              self._scan_axe),
        ):
            try:
                fn(page, final_url, tc_id)
            except Exception as exc:
                _logger.debug("walkthrough heuristic %s raised: %s",
                              label, exc)
                self._note(
                    _SEV_MINOR, "Walkthrough", "walk_step_failed",
                    f"Heuristic {label!r} raised "
                    f"{type(exc).__name__}: "
                    f"{str(exc)[:160]}",
                    url=final_url, tc_id=tc_id,
                )

        # ── Console / page-error sweep ─────────────────────────────
        self._collect_console_errors(console_errors, page_errors,
                                      final_url, tc_id)

        # Synthesise StepResults from the new findings so the existing
        # UI gallery shows one row per defect. Status is "failed" so
        # the per-TC card visibly flags the walkthrough output even
        # though the run itself is a "report, don't block" exercise.
        for offset, finding in enumerate(self.findings[before_count:],
                                          start=2):
            steps.append(StepResult(
                index=offset,
                action="walkthrough_check",
                raw=f"{finding['area']}: {finding['message'][:120]}",
                status="failed",
                duration_ms=0,
                comment=finding.get("dev_detail", "") or
                        finding.get("message", ""),
                screenshot_after=finding.get("screenshot", "") or "",
                console_errors=list(finding.get("console_errors")
                                     or []),
            ))

        try:
            page.close()
        except Exception:
            pass

        # Pass/fail policy matches TFWefloLab: only Critical findings
        # fail the script. High/Medium/Minor surface but don't block.
        page_findings = self.findings[before_count:]
        any_critical = any(f.get("severity") == _SEV_CRITICAL
                            for f in page_findings)
        status = "failed" if any_critical else "passed"
        comment = (f"{len(page_findings)} finding(s) on this page"
                    if page_findings else "")
        return ScriptResult(
            tc_id=tc_id,
            summary=f"Walkthrough: {url}",
            status=status,
            duration_ms=int((time.time() - t0) * 1000),
            steps=steps,
            comment=comment,
            final_url=final_url,
        )

    # ── helpers ──────────────────────────────────────────────────

    def _blocked_script(self, idx: int, url: str,
                        reason: str) -> ScriptResult:
        return ScriptResult(
            tc_id=f"WALK-{idx:03d}",
            summary=f"Walkthrough: {url}",
            status="blocked",
            duration_ms=0,
            steps=[StepResult(
                index=1, action="goto", raw=url, status="blocked",
                comment=reason,
            )],
            comment=reason,
        )

    def _screenshot(self, page, run_dir: str, live_dir: str,
                    tc_id: str, label: str) -> str:
        """Save a screenshot to ``<run_dir>/<tc_id>/<label>.png`` and
        mirror it to ``<live_dir>/latest.png`` so the existing
        ``/test-execution/live`` viewer picks it up.

        Returns the storage-relative path so the post-run serialiser
        and ``_build_automation_assets`` in :mod:`runner_worker`
        resolve it the same way they resolve TC-driven shots.
        """
        rel_dir = os.path.join("automation_runs",
                                os.path.basename(run_dir), tc_id)
        abs_dir = os.path.join(self.storage_root, rel_dir)
        os.makedirs(abs_dir, exist_ok=True)
        rel_path = os.path.join(rel_dir, f"{label}.png").replace(os.sep, "/")
        abs_path = os.path.join(self.storage_root,
                                rel_path.replace("/", os.sep))
        try:
            page.screenshot(path=abs_path, full_page=False)
        except Exception as exc:
            _logger.debug("walkthrough screenshot failed: %s", exc)
            return ""
        if live_dir:
            try:
                shutil.copyfile(abs_path,
                                os.path.join(live_dir, "latest.png.tmp"))
                os.replace(os.path.join(live_dir, "latest.png.tmp"),
                           os.path.join(live_dir, "latest.png"))
            except OSError:
                pass
        return rel_path


    # ── error listeners ─────────────────────────────────────────────

    def _install_error_listeners(self, page,
                                  console_errors: list[dict[str, Any]],
                                  page_errors: list[dict[str, Any]]) -> None:
        """Wire page.on('console') / page.on('pageerror'). Delegates
        to module-level :func:`install_error_listeners`.
        """
        install_error_listeners(page, console_errors, page_errors)

    # ── heuristic: broken images ────────────────────────────────────

    _JS_FIND_BROKEN_IMAGES = """
    () => {
        return Array.from(document.images)
            .filter(img => img.src && !img.src.startsWith('data:')
                          && img.naturalWidth === 0)
            .map(img => ({
                src:      img.src,
                alt:      img.alt || '',
                cls:      img.className || '',
                parentCls: (img.parentElement && img.parentElement.className) || ''
            }))
            .slice(0, 25);
    }
    """

    def _scan_broken_images(self, page, url: str, tc_id: str) -> None:
        """Walk ``document.images``; flag any whose ``naturalWidth`` is
        zero. Delegates to module-level :func:`scan_broken_images` so
        :mod:`engine.live_executor` can reuse the same heuristic
        without inheriting from :class:`WalkthroughRunner`.
        """
        scan_broken_images(page, url, tc_id, note=self._note)

    # ── heuristic: navigation menu (hamburger / dropdowns) ─────────

    _JS_COUNT_VISIBLE_NAV = """
    () => {
        const els = document.querySelectorAll(
            '.w-nav-menu a, nav a, [role="navigation"] a');
        return Array.from(els).filter(el => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        }).length;
    }
    """

    _JS_PROBE_DROPDOWNS = """
    () => {
        const out = [];
        const triggers = document.querySelectorAll(
            '.w-dropdown-toggle, [aria-haspopup="true"], [aria-haspopup="menu"]');
        Array.from(triggers).slice(0, 6).forEach((tr, i) => {
            const label = (tr.textContent || '').trim().slice(0, 30);
            const dd    = tr.closest('.w-dropdown') || tr.parentElement;
            const list  = dd && dd.querySelector(
                '.w-dropdown-list, [role="menu"], .dropdown-menu');
            let popupVisible = false;
            if (list) {
                const r  = list.getBoundingClientRect();
                const cs = getComputedStyle(list);
                popupVisible = r.width > 0 && r.height > 0
                            && cs.display !== 'none'
                            && cs.visibility !== 'hidden';
            }
            out.push({ index: i, label, hasList: !!list,
                       popupVisible });
        });
        return out;
    }
    """

    def _scan_navigation_menu(self, page, url: str, tc_id: str) -> None:
        """Mobile/tablet: tap hamburger and verify nav-link count rises.
        Desktop: probe dropdown popup visibility. Delegates to
        module-level :func:`scan_navigation_menu`.
        """
        scan_navigation_menu(page, url, tc_id, note=self._note,
                              device_kind=self.device_kind)

    # ── heuristic: footer + social links ───────────────────────────

    _JS_FOOTER_SOCIALS = """
    () => {
        const out = [];
        const re = /(facebook|twitter|x\\.com|instagram|linkedin|youtube|youtu\\.be|tiktok|github|pinterest|threads|t\\.me|telegram|wa\\.me|discord|medium|vimeo|reddit)/i;
        const links = document.querySelectorAll(
            'footer a[href], [role="contentinfo"] a[href]');
        Array.from(links).forEach(a => {
            if (!re.test(a.href || '')) return;
            out.push({
                href:   a.href,
                target: a.getAttribute('target') || '',
                rel:    a.getAttribute('rel') || '',
                label:  (a.getAttribute('aria-label')
                         || a.textContent || '').trim().slice(0, 30)
            });
        });
        return out;
    }
    """

    def _scan_footer_social(self, page, url: str, tc_id: str) -> None:
        """Scroll to footer; flag placeholder hosts + missing
        rel=noopener. Delegates to module-level :func:`scan_footer_social`.
        """
        scan_footer_social(page, url, tc_id, note=self._note)

    # ── heuristic: search field ─────────────────────────────────────

    _JS_OBSERVE_SEARCH_RESULTS = """
    () => {
        const sels = [
            '[role="listbox"]', '[role="search"] ul',
            '[role="search"] [aria-label*="result" i]',
            '.search-results', '.search-suggestions',
            '.search-autocomplete', '.autocomplete',
            '.suggestions', '[aria-expanded="true"] + *',
            '.search-dropdown', '.results-dropdown'
        ];
        for (const sel of sels) {
            const el = document.querySelector(sel);
            if (!el) continue;
            const r  = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            if (r.width > 0 && r.height > 0
                && cs.display !== 'none'
                && cs.visibility !== 'hidden') {
                return { found: true, sel,
                         count: el.querySelectorAll(
                             'li, a, [role="option"]').length };
            }
        }
        return { found: false };
    }
    """

    def _scan_search_field(self, page, url: str, tc_id: str) -> None:
        """Find a visible search input (or its toggle), type 'test',
        wait 1.2 s, check for results. Delegates to module-level
        :func:`scan_search_field`.
        """
        scan_search_field(page, url, tc_id, note=self._note)

    # ── heuristic: forms (auto-fill, no submit) ────────────────────

    def _scan_forms(self, page, url: str, tc_id: str) -> None:
        """Auto-fill every visible <input>/<textarea> with sample data.
        Never submits. Delegates to module-level :func:`scan_forms`.
        """
        scan_forms(page, url, tc_id, note=self._note,
                   max_form_fills=self.max_form_fills)

    # ── heuristic: CTA / tap-target audit ──────────────────────────

    _JS_CTA_AUDIT = """
    () => {
        const out = [];
        const els = document.querySelectorAll(
            'button:not([type=hidden]), .w-button, a.button, '
            '[role="button"], input[type=submit]');
        Array.from(els).slice(0, 60).forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return;
            const cs = getComputedStyle(el);
            if (cs.visibility === 'hidden' || cs.display === 'none') return;
            const txt = (el.textContent || el.getAttribute('value')
                         || '').trim().slice(0, 30);
            if (el.disabled
                    || el.getAttribute('aria-disabled') === 'true') {
                out.push({text: txt, reason: 'disabled', sel: el.tagName});
                return;
            }
            if (el.tagName === 'A') {
                const href    = el.getAttribute('href');
                const hasHook = el.hasAttribute('data-w-id')
                             || el.hasAttribute('onclick')
                             || el.hasAttribute('data-modal');
                if ((!href || href === '#'
                        || href === 'javascript:void(0)') && !hasHook) {
                    out.push({text: txt, reason: 'no destination',
                              sel: 'a'});
                    return;
                }
            }
            if (r.width < 24 || r.height < 24) {
                out.push({
                    text: txt,
                    reason: `tap target ${Math.round(r.width)}x${Math.round(r.height)}px`,
                    sel: el.tagName.toLowerCase()
                });
            }
        });
        return out.slice(0, 20);
    }
    """

    def _scan_ctas(self, page, url: str, tc_id: str) -> None:
        """Audit visible buttons / link.button / role=button. Delegates
        to module-level :func:`scan_ctas`.
        """
        scan_ctas(page, url, tc_id, note=self._note)

    # ── heuristic: axe-core a11y scan ──────────────────────────────

    _JS_RUN_AXE = """
    () => {
        if (typeof window.axe === 'undefined') {
            return { error: 'axe-not-loaded', violations: [] };
        }
        return window.axe.run(document, {
            runOnly: { type: 'tag',
                       values: ['wcag2a','wcag2aa','wcag21a','wcag21aa'] }
        }).then(res => ({
            error: null,
            violations: (res.violations || []).map(v => ({
                id:       v.id,
                impact:   v.impact,
                help:     v.help,
                helpUrl:  v.helpUrl,
                description: v.description,
                tags:     v.tags || [],
                nodes:    (v.nodes || []).slice(0, 3).map(n => ({
                    target:         (n.target || [])[0] || '',
                    html:           (n.html || '').slice(0, 220),
                    failureSummary: n.failureSummary || ''
                }))
            }))
        })).catch(err => ({ error: String(err), violations: [] }));
    }
    """

    def _scan_axe(self, page, url: str, tc_id: str) -> None:
        """Inject axe-core via CDN, emit critical/serious as findings.
        Delegates to module-level :func:`scan_axe`.
        """
        scan_axe(page, url, tc_id, note=self._note,
                 axe_enabled=self.axe_enabled)

    # ── post-walk: console / page error sweep ──────────────────────

    def _collect_console_errors(self,
                                 console_errors: list[dict[str, Any]],
                                 page_errors: list[dict[str, Any]],
                                 url: str, tc_id: str) -> None:
        """Emit one finding per unique error. Delegates to module-level
        :func:`collect_console_errors`.
        """
        collect_console_errors(console_errors, page_errors,
                                url, tc_id, note=self._note)

    # ── post-run dedup ──────────────────────────────────────────────

    def dedupe_findings(self) -> list[dict[str, Any]]:
        """Return ``self.findings`` collapsed via
        :func:`engine.walkthrough_dedup.dedupe`.

        Useful for callers (debug endpoint / future PR-3 UI) that want
        the deduped view without mutating the raw findings ring. Does
        not modify ``self.findings``.
        """
        return _dedupe_findings(list(self.findings))


# ── module-level helpers ──────────────────────────────────────────


def _humanize_js_error(raw: str) -> str:
    """Map a technical JS error to a plain-language headline.

    Ported from walkthrough.spec.js:64–:128. Operators consume the
    walkthrough output as a triage queue, not a developer console —
    "A script tried to use a page element that does not exist" beats
    ``Cannot read properties of null`` when the reviewer is a PM.
    """
    m = raw or ""
    if re.search(r"Cannot read prop(erties)?|of (null|undefined)",
                  m, re.I):
        return "A script tried to use a page element that does not exist"
    if re.search(r"MutationObserver|IntersectionObserver|ResizeObserver",
                  m, re.I):
        return ("A script tried to monitor a page element that was "
                "not found")
    if re.search(r"addEventListener|removeEventListener", m, re.I) \
            and re.search(r"null|undefined", m, re.I):
        return ("A click / scroll handler could not attach — its "
                "target element is missing")
    if re.search(r"is not a function", m, re.I):
        return "A script called something that does not exist"
    if re.search(r"Failed to fetch|NetworkError|Load failed|ERR_",
                  m, re.I):
        return "A network request failed during the page session"
    if re.search(r"Unexpected token|SyntaxError", m, re.I):
        return "A script could not be parsed — broken JavaScript file"
    if re.search(r"CORS|Cross-Origin", m, re.I):
        return "A cross-origin request was blocked by the browser"
    if re.search(r"quota|QuotaExceeded", m, re.I):
        return "Browser storage is full — the page could not save state"
    return "A JavaScript error happened during the user journey"


__all__ = [
    "WalkthroughRunner",
    "WALKTHROUGH_FLAG_ENV",
    "feature_enabled",
    # Stage 3 — stateless scan helpers reusable by engine.live_executor.
    "scan_broken_images",
    "scan_navigation_menu",
    "scan_footer_social",
    "scan_search_field",
    "scan_forms",
    "scan_ctas",
    "scan_axe",
    "collect_console_errors",
    "install_error_listeners",
]
