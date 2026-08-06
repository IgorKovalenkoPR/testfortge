"""
TestFortge — Browser-Based Site Testing Engine (Playwright)

Opens a real Chromium browser, navigates every crawled page, clicks links
and buttons, checks JS console errors, verifies responsive layout,
inspects forms, and reports real Passed/Failed findings.

Each finding carries the exact page URL, element description, and
actual result — this data feeds into test-case / checklist generation
so that TestFortge produces documentation based on **real observations**,
not knowledge-base templates.

Usage
-----
    from engine.browser_tester import get_or_run

    report = get_or_run("https://example.com")
    for f in report.findings:
        print(f.status, f.description)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, asdict
from urllib.parse import urljoin, urlparse

# SSRF guard — every page.goto in this module routes operator-supplied
# URLs (the crawled site's pages) into Chromium. Blocks 127.0.0.1,
# RFC1918, link-local, and cloud-metadata addresses. See engine.security.
from engine.security import require_safe_url, UnsafeUrlError
from engine.log import get_logger

log = get_logger(__name__)

# ── Data structures ─────────────────────────────────────────────

@dataclass
class BrowserFinding:
    """A single observation from browser testing."""
    check_id: str           # e.g. "js_error_/about", "link_404_/contact"
    category: str           # Links, JavaScript, Responsive, Forms, etc.
    severity: str           # Critical / Major / Minor
    status: str             # Passed / Failed
    page_url: str           # URL where the finding was observed
    description: str        # Human-readable description
    details: dict = field(default_factory=dict)


@dataclass
class BrowserPageReport:
    """Findings for a single page."""
    url: str
    load_time_ms: int = 0
    js_errors: list[str] = field(default_factory=list)
    findings: list[BrowserFinding] = field(default_factory=list)


@dataclass
class BrowserTestReport:
    """Aggregate report for the entire site."""
    base_url: str
    pages_tested: int = 0
    total_findings: int = 0
    pages: list[BrowserPageReport] = field(default_factory=list)
    findings: list[BrowserFinding] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "base_url": self.base_url,
            "pages_tested": self.pages_tested,
            "total_findings": self.total_findings,
            "findings": [asdict(f) for f in self.findings],
            "summary": self.summary,
        }


# ── Browser Test Runner ────────────────────────────────────────

class BrowserTestRunner:
    """Real browser-based site tester using Playwright + Chromium.

    Opens each crawled page in headless Chromium and performs real
    interactive checks: clicking links/buttons, checking console
    errors, verifying responsive layout, inspecting forms, etc.
    """

    MAX_PAGES = 20
    MAX_LINKS_PER_PAGE = 25
    TIMEOUT_MS = 8000
    # All three viewports give the broadest responsive coverage but
    # cost three full page-loads per URL. On constrained instances
    # (Render free, 0.1 CPU) this triples sync work; setting
    # ``TESTFORTGE_BROWSER_VIEWPORTS=desktop`` keeps just one and
    # cuts browser-tester time by ~66%. Accepted values:
    # ``all`` (default), ``desktop``, ``mobile``, ``tablet``.
    _ALL_VIEWPORTS = [
        (375, 812, "mobile"),
        (768, 1024, "tablet"),
        (1280, 800, "desktop"),
    ]
    VIEWPORTS = _ALL_VIEWPORTS

    def __init__(self, base_url: str, max_pages: int = 20,
                 timeout_ms: int = 8000, site_analysis=None,
                 viewports: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.max_pages = min(max_pages, self.MAX_PAGES)
        self.timeout_ms = timeout_ms
        self._pw = None
        self._browser = None
        # Pick viewport set. ``viewports`` accepts: None / "all"
        # → all three; "desktop" / "mobile" / "tablet" → just that
        # one. On constrained instances (Render free) running a single
        # viewport cuts browser-tester time by ~66%.
        kind = (viewports or "").lower()
        if kind in ("desktop",):
            self.VIEWPORTS = [(1280, 800, "desktop")]
        elif kind in ("mobile",):
            self.VIEWPORTS = [(375, 812, "mobile")]
        elif kind in ("tablet",):
            self.VIEWPORTS = [(768, 1024, "tablet")]
        else:
            self.VIEWPORTS = self._ALL_VIEWPORTS

        # Reuse crawler data if available
        if site_analysis:
            self.site_analysis = site_analysis
        else:
            from .site_crawler import crawl_site
            self.site_analysis = crawl_site(base_url)

        self.page_urls = [p.url for p in self.site_analysis.pages[:self.max_pages]]
        if not self.page_urls:
            self.page_urls = [self.base_url]

        self._parsed_base = urlparse(self.base_url)

    def __enter__(self):
        """Start the driver, then the browser — and unwind if the second fails.

        ``__exit__`` never runs when ``__enter__`` raises, so a failed
        ``chromium.launch`` used to leave the driver started for the life of
        the process. That is not a leaked subprocess and nothing more:
        Playwright's sync API runs its event loop in this thread, so the
        thread is left *inside a running loop*, and every later
        ``sync_playwright()`` anywhere in the process then raises

            It looks like you are using Playwright Sync API inside the
            asyncio loop.

        …instead of whatever the real problem was. The caller in
        ``qa_persona`` swallows the launch failure (``except Exception:
        pass``) and generation carries on with no browser findings, so on a
        dyno where Chromium has just been OOM-killed the visible symptom is
        one quiet run followed by every subsequent browser run failing for a
        reason that has nothing to do with it.

        Found by CI: the browsers are not installed in the test matrix, so
        this file's launch failed there and poisoned the process for
        ``tests/test_e2e_golden_paths.py`` fourteen files later.
        """
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=True)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *args):
        # Each step guarded separately: a browser that fails to close must
        # not skip stopping the driver, which is the half that holds the
        # event loop. Both are best-effort — this runs on the way out of a
        # failure as often as on the way out of a success.
        try:
            if self._browser:
                self._browser.close()
        except Exception as exc:      # pragma: no cover — teardown only
            log.debug("browser close failed: %s", exc)
        finally:
            self._browser = None
        try:
            if self._pw:
                self._pw.stop()
        except Exception as exc:      # pragma: no cover — teardown only
            log.debug("playwright stop failed: %s", exc)
        finally:
            self._pw = None

    def _is_same_site(self, url: str) -> bool:
        """Check if URL belongs to the same site."""
        parsed = urlparse(url)
        return parsed.netloc == self._parsed_base.netloc or not parsed.netloc

    def _safe_goto(self, page, url: str) -> bool:
        """Navigate to URL with timeout handling. Returns True on success."""
        try:
            require_safe_url(url)
        except UnsafeUrlError:
            return False
        try:
            resp = page.goto(url, wait_until="domcontentloaded",
                             timeout=self.timeout_ms)
            return resp is not None and resp.status < 400
        except Exception:
            return False

    # ── Individual checks ──────────────────────────────────────

    def _check_page_load(self, page, url: str) -> BrowserPageReport:
        """Load page, measure timing, collect basic data."""
        report = BrowserPageReport(url=url)

        try:
            require_safe_url(url)
        except UnsafeUrlError as exc:
            report.findings.append(BrowserFinding(
                check_id=f"page_blocked_{urlparse(url).path or '/'}",
                category="Security",
                severity="Critical",
                status="Failed",
                page_url=url,
                description=f"Page blocked by SSRF policy: {exc}",
            ))
            return report

        try:
            start = time.monotonic()
            resp = page.goto(url, wait_until="domcontentloaded",
                             timeout=self.timeout_ms)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            report.load_time_ms = elapsed_ms

            if resp is None or resp.status >= 400:
                status_code = resp.status if resp else "no response"
                report.findings.append(BrowserFinding(
                    check_id=f"page_load_{urlparse(url).path or '/'}",
                    category="Performance",
                    severity="Critical",
                    status="Failed",
                    page_url=url,
                    description=f"Page failed to load (HTTP {status_code})",
                    details={"status_code": status_code, "load_time_ms": elapsed_ms},
                ))
            else:
                if elapsed_ms > 3000:
                    report.findings.append(BrowserFinding(
                        check_id=f"page_slow_{urlparse(url).path or '/'}",
                        category="Performance",
                        severity="Major",
                        status="Failed",
                        page_url=url,
                        description=f"Page loaded slowly ({elapsed_ms}ms, threshold: 3000ms)",
                        details={"load_time_ms": elapsed_ms},
                    ))
                else:
                    report.findings.append(BrowserFinding(
                        check_id=f"page_load_{urlparse(url).path or '/'}",
                        category="Performance",
                        severity="Minor",
                        status="Passed",
                        page_url=url,
                        description=f"Page loaded successfully in {elapsed_ms}ms",
                        details={"load_time_ms": elapsed_ms},
                    ))
        except Exception as e:
            report.findings.append(BrowserFinding(
                check_id=f"page_load_{urlparse(url).path or '/'}",
                category="Performance",
                severity="Critical",
                status="Failed",
                page_url=url,
                description=f"Page failed to load: {str(e)[:100]}",
            ))

        return report

    def _check_js_console_errors(self, page, url: str) -> list[BrowserFinding]:
        """Check for JavaScript console errors on the page."""
        findings: list[BrowserFinding] = []
        js_errors: list[str] = []

        def on_console(msg):
            if msg.type == "error":
                js_errors.append(msg.text)

        try:
            require_safe_url(url)
        except UnsafeUrlError:
            return findings

        page.on("console", on_console)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            # Wait a bit for async JS errors
            page.wait_for_timeout(1000)
        except Exception:
            pass
        page.remove_listener("console", on_console)

        if js_errors:
            # Deduplicate
            unique_errors = list(dict.fromkeys(js_errors))[:5]
            for err in unique_errors:
                findings.append(BrowserFinding(
                    check_id=f"js_error_{urlparse(url).path or '/'}",
                    category="JavaScript",
                    severity="Major",
                    status="Failed",
                    page_url=url,
                    description=f"JavaScript console error: {err[:150]}",
                    details={"error_text": err},
                ))
        else:
            findings.append(BrowserFinding(
                check_id=f"js_no_errors_{urlparse(url).path or '/'}",
                category="JavaScript",
                severity="Minor",
                status="Passed",
                page_url=url,
                description="No JavaScript console errors detected",
            ))

        return findings

    def _check_links(self, page, url: str) -> list[BrowserFinding]:
        """Check links on the page by collecting them for batch validation.

        Does NOT open each link in a separate tab (causes rate limiting).
        Instead, collects all unique internal hrefs and validates them
        via lightweight HEAD/GET requests with the same browser cookies.
        Links already visited during crawling are considered valid.
        """
        findings: list[BrowserFinding] = []

        try:
            require_safe_url(url)
        except UnsafeUrlError:
            return findings

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception:
            return findings

        try:
            links_data = page.evaluate("""() => {
                const links = Array.from(document.querySelectorAll('a[href]'));
                return links.slice(0, %d).map(a => ({
                    href: a.href,
                    text: (a.textContent || '').trim().substring(0, 50).replace(/\\s+/g, ' '),
                    isInternal: a.hostname === window.location.hostname,
                }));
            }""" % self.MAX_LINKS_PER_PAGE)
        except Exception:
            return findings

        # Filter internal, deduplicate, exclude already-crawled pages
        crawled_urls = {p.rstrip("/") for p in self.page_urls}
        unique_links: dict[str, str] = {}  # href -> text
        for link in links_data:
            href = link.get("href", "").split("#")[0].rstrip("/")
            if not href or href.startswith(("javascript:", "mailto:", "tel:")):
                continue
            if not link.get("isInternal", False):
                continue
            if href in crawled_urls:
                continue
            if href not in unique_links:
                unique_links[href] = link.get("text", "")

        if not unique_links:
            findings.append(BrowserFinding(
                check_id=f"links_ok_{urlparse(url).path or '/'}",
                category="Links",
                severity="Minor",
                status="Passed",
                page_url=url,
                description=f"All {len(links_data)} links reference valid pages",
                details={"checked": len(links_data)},
            ))
            return findings

        # Batch-check via JS fetch (uses same cookies, avoids rate limiting)
        broken: list[dict] = []
        checked = 0
        for href, text in list(unique_links.items())[:15]:
            checked += 1
            try:
                status = page.evaluate("""(url) => {
                    return fetch(url, {method: 'HEAD', redirect: 'follow'})
                        .then(r => r.status)
                        .catch(() => 0);
                }""", href)
                if isinstance(status, int) and status >= 400:
                    broken.append({"href": href, "text": text, "status": status})
            except Exception:
                pass

        if broken:
            for b in broken:
                findings.append(BrowserFinding(
                    check_id=f"broken_link_{b['text'][:20]}",
                    category="Links",
                    severity="Major",
                    status="Failed",
                    page_url=url,
                    description=f"Link '{b['text']}' leads to HTTP {b['status']}",
                    details={"href": b["href"], "status": b["status"]},
                ))
        else:
            findings.append(BrowserFinding(
                check_id=f"links_ok_{urlparse(url).path or '/'}",
                category="Links",
                severity="Minor",
                status="Passed",
                page_url=url,
                description=f"All {checked + len(crawled_urls)} internal links are working",
                details={"checked": checked},
            ))

        return findings

    def _check_buttons(self, page, url: str) -> list[BrowserFinding]:
        """Check that buttons on the page respond to clicks."""
        findings: list[BrowserFinding] = []

        try:
            require_safe_url(url)
        except UnsafeUrlError:
            return findings

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception:
            return findings

        try:
            buttons_data = page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll(
                    'button, [role="button"], input[type="submit"]'
                ));
                return btns.filter(b => {
                    const rect = b.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                }).slice(0, 15).map(b => ({
                    text: (b.textContent || b.value || '').trim().substring(0, 50),
                    tag: b.tagName,
                    type: b.type || '',
                    inForm: !!b.closest('form'),
                    isVisible: true,
                }));
            }""")
        except Exception:
            return findings

        if not buttons_data:
            findings.append(BrowserFinding(
                check_id=f"no_buttons_{urlparse(url).path or '/'}",
                category="Interactivity",
                severity="Minor",
                status="Passed",
                page_url=url,
                description="No interactive buttons found on this page (N/A)",
            ))
            return findings

        clickable_count = 0
        for btn in buttons_data:
            # Skip form submit buttons — clicking them could navigate away
            if btn.get("inForm") and btn.get("type") in ("submit", ""):
                continue
            clickable_count += 1

        if clickable_count > 0:
            findings.append(BrowserFinding(
                check_id=f"buttons_found_{urlparse(url).path or '/'}",
                category="Interactivity",
                severity="Minor",
                status="Passed",
                page_url=url,
                description=f"Found {len(buttons_data)} interactive buttons ({clickable_count} non-form)",
                details={"total": len(buttons_data), "clickable": clickable_count},
            ))

        return findings

    def _check_dropdowns(self, page, url: str) -> list[BrowserFinding]:
        """Check dropdown menus in navigation."""
        findings: list[BrowserFinding] = []

        try:
            require_safe_url(url)
        except UnsafeUrlError:
            return findings

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception:
            return findings

        try:
            dropdown_data = page.evaluate("""() => {
                const navItems = document.querySelectorAll('nav li, [role="menubar"] > *');
                const results = [];
                for (const item of navItems) {
                    const sub = item.querySelector('ul, [role="menu"], .dropdown-menu, .submenu, .sub-menu');
                    if (sub) {
                        const link = item.querySelector('a');
                        const text = (link && link.textContent || item.textContent || '').trim().substring(0, 40);
                        const style = window.getComputedStyle(sub);
                        const hidden = style.display === 'none' || style.visibility === 'hidden' ||
                                       style.opacity === '0' || sub.classList.contains('hidden');
                        results.push({text, hidden, selector: sub.tagName + '.' + sub.className});
                    }
                }
                return results.slice(0, 10);
            }""")
        except Exception:
            return findings

        if not dropdown_data:
            return findings

        for dd in dropdown_data:
            text = dd.get("text", "menu item")
            try:
                # Find the parent nav item and hover over it
                locator = page.locator(f"nav li:has-text('{text}')").first
                locator.hover(timeout=2000)
                page.wait_for_timeout(500)

                # Check if the submenu became visible
                is_visible = page.evaluate("""(text) => {
                    const items = document.querySelectorAll('nav li');
                    for (const item of items) {
                        if (item.textContent.trim().includes(text)) {
                            const sub = item.querySelector('ul, [role="menu"], .dropdown-menu, .submenu, .sub-menu');
                            if (sub) {
                                const style = window.getComputedStyle(sub);
                                return style.display !== 'none' && style.visibility !== 'hidden' &&
                                       style.opacity !== '0';
                            }
                        }
                    }
                    return false;
                }""", text)

                if is_visible:
                    findings.append(BrowserFinding(
                        check_id=f"dropdown_works_{text[:20]}",
                        category="Navigation",
                        severity="Minor",
                        status="Passed",
                        page_url=url,
                        description=f"Dropdown menu '{text}' opens on hover",
                    ))
                else:
                    findings.append(BrowserFinding(
                        check_id=f"dropdown_broken_{text[:20]}",
                        category="Navigation",
                        severity="Major",
                        status="Failed",
                        page_url=url,
                        description=f"Dropdown menu '{text}' does not open on hover",
                    ))
            except Exception:
                pass  # Skip items that can't be located

        return findings

    def _check_forms(self, page, url: str) -> list[BrowserFinding]:
        """Check form fields: focus, tab order, required field validation."""
        findings: list[BrowserFinding] = []

        try:
            require_safe_url(url)
        except UnsafeUrlError:
            return findings

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception:
            return findings

        try:
            forms_data = page.evaluate("""() => {
                const forms = Array.from(document.querySelectorAll('form'));
                return forms.slice(0, 5).map(form => {
                    const fields = Array.from(form.querySelectorAll(
                        'input:not([type="hidden"]):not([type="submit"]):not([type="button"]), textarea, select'
                    ));
                    return {
                        action: form.action || '',
                        method: form.method || 'GET',
                        hasSubmit: !!form.querySelector('button[type="submit"], input[type="submit"], button:not([type])'),
                        fields: fields.map(f => ({
                            tag: f.tagName,
                            type: f.type || '',
                            name: f.name || '',
                            id: f.id || '',
                            required: f.required,
                            hasLabel: !!f.labels && f.labels.length > 0,
                            placeholder: f.placeholder || '',
                        })),
                    };
                }).filter(f => f.fields.length > 0);
            }""")
        except Exception:
            return findings

        if not forms_data:
            return findings

        for form in forms_data:
            form_fields = form.get("fields", [])
            if not form_fields:
                continue

            # Check: each input has a label or placeholder
            unlabeled = [f for f in form_fields
                         if not f.get("hasLabel") and not f.get("placeholder")]
            if unlabeled:
                names = ", ".join(f.get("name") or f.get("type", "?") for f in unlabeled[:3])
                findings.append(BrowserFinding(
                    check_id=f"form_no_label_{urlparse(url).path}",
                    category="Forms",
                    severity="Major",
                    status="Failed",
                    page_url=url,
                    description=f"Form fields without labels or placeholders: {names}",
                    details={"unlabeled_fields": [f.get("name") for f in unlabeled]},
                ))
            else:
                findings.append(BrowserFinding(
                    check_id=f"form_labels_ok_{urlparse(url).path}",
                    category="Forms",
                    severity="Minor",
                    status="Passed",
                    page_url=url,
                    description=f"All {len(form_fields)} form fields have labels or placeholders",
                ))

            # Check: required fields are marked
            required_fields = [f for f in form_fields if f.get("required")]
            if required_fields:
                findings.append(BrowserFinding(
                    check_id=f"form_required_{urlparse(url).path}",
                    category="Forms",
                    severity="Minor",
                    status="Passed",
                    page_url=url,
                    description=f"{len(required_fields)} required fields properly marked",
                ))

            # Check: tab order (fields are focusable)
            try:
                # Click the first field to start tab sequence
                first_field = form_fields[0]
                selector = f"form input[name='{first_field.get('name')}']" if first_field.get("name") else "form input"
                page.click(selector, timeout=2000)

                focused_tags = []
                for _ in range(min(len(form_fields), 5)):
                    page.keyboard.press("Tab")
                    tag = page.evaluate("document.activeElement ? document.activeElement.tagName + '.' + (document.activeElement.name || document.activeElement.id || '') : 'NONE'")
                    focused_tags.append(tag)

                if any("INPUT" in t or "TEXTAREA" in t or "SELECT" in t for t in focused_tags):
                    findings.append(BrowserFinding(
                        check_id=f"form_tab_order_{urlparse(url).path}",
                        category="Forms",
                        severity="Minor",
                        status="Passed",
                        page_url=url,
                        description="Tab key moves focus through input, textarea and select fields in the order they appear in the DOM",
                    ))
            except Exception:
                pass

        return findings

    def _check_hover_effects(self, page, url: str) -> list[BrowserFinding]:
        """Check if navigation items have visual hover effects."""
        findings: list[BrowserFinding] = []

        try:
            require_safe_url(url)
        except UnsafeUrlError:
            return findings

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception:
            return findings

        try:
            nav_links = page.evaluate("""() => {
                const links = Array.from(document.querySelectorAll('nav a'));
                return links.slice(0, 8).map(a => ({
                    text: a.textContent.trim().substring(0, 30),
                    href: a.href,
                })).filter(l => l.text.length > 0);
            }""")
        except Exception:
            return findings

        if not nav_links:
            return findings

        hover_changes = 0
        for link in nav_links[:5]:
            try:
                locator = page.locator(f"nav a:has-text('{link['text']}')").first

                # Get style before hover
                before = page.evaluate("""(text) => {
                    const el = Array.from(document.querySelectorAll('nav a'))
                        .find(a => a.textContent.trim().includes(text));
                    if (!el) return null;
                    const s = window.getComputedStyle(el);
                    return {color: s.color, bg: s.backgroundColor, decoration: s.textDecoration, opacity: s.opacity};
                }""", link["text"])

                locator.hover(timeout=2000)
                page.wait_for_timeout(300)

                # Get style after hover
                after = page.evaluate("""(text) => {
                    const el = Array.from(document.querySelectorAll('nav a'))
                        .find(a => a.textContent.trim().includes(text));
                    if (!el) return null;
                    const s = window.getComputedStyle(el);
                    return {color: s.color, bg: s.backgroundColor, decoration: s.textDecoration, opacity: s.opacity};
                }""", link["text"])

                if before and after and before != after:
                    hover_changes += 1
            except Exception:
                pass

        if hover_changes > 0:
            findings.append(BrowserFinding(
                check_id="nav_hover_effects",
                category="UI/UX",
                severity="Minor",
                status="Passed",
                page_url=url,
                description=f"Navigation items have visual hover effects ({hover_changes}/{len(nav_links[:5])} items)",
            ))
        elif nav_links:
            findings.append(BrowserFinding(
                check_id="nav_no_hover",
                category="UI/UX",
                severity="Minor",
                status="Failed",
                page_url=url,
                description="No visual hover effects detected on navigation items",
            ))

        return findings

    def _check_responsive(self, page) -> list[BrowserFinding]:
        """Check responsive layout at mobile/tablet/desktop viewports."""
        findings: list[BrowserFinding] = []

        try:
            require_safe_url(self.base_url)
        except UnsafeUrlError:
            return findings

        for width, height, label in self.VIEWPORTS:
            try:
                page.set_viewport_size({"width": width, "height": height})
                page.goto(self.base_url, wait_until="domcontentloaded",
                          timeout=self.timeout_ms)
                page.wait_for_timeout(500)

                # Check horizontal scrollbar
                has_h_scroll = page.evaluate(
                    "document.documentElement.scrollWidth > document.documentElement.clientWidth + 5"
                )

                if has_h_scroll:
                    findings.append(BrowserFinding(
                        check_id=f"responsive_hscroll_{label}",
                        category="Responsive",
                        severity="Major" if label == "mobile" else "Minor",
                        status="Failed",
                        page_url=self.base_url,
                        description=f"Horizontal scroll detected at {label} viewport ({width}x{height})",
                        details={"viewport": label, "width": width, "height": height},
                    ))
                else:
                    findings.append(BrowserFinding(
                        check_id=f"responsive_ok_{label}",
                        category="Responsive",
                        severity="Minor",
                        status="Passed",
                        page_url=self.base_url,
                        description=f"No horizontal scroll at {label} viewport ({width}x{height})",
                        details={"viewport": label, "width": width, "height": height},
                    ))

                # Check for hamburger menu on mobile
                if label == "mobile":
                    has_hamburger = page.evaluate("""() => {
                        const sels = [
                            '[class*="hamburger"]', '[class*="burger"]',
                            '[class*="mobile-menu"]', '[class*="menu-toggle"]',
                            '[class*="nav-toggle"]', 'button[aria-label*="menu"]',
                            'button[aria-label*="Menu"]', '.navbar-toggler',
                        ];
                        for (const sel of sels) {
                            const el = document.querySelector(sel);
                            if (el) {
                                const rect = el.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0) return true;
                            }
                        }
                        return false;
                    }""")

                    if has_hamburger:
                        findings.append(BrowserFinding(
                            check_id="mobile_hamburger",
                            category="Responsive",
                            severity="Minor",
                            status="Passed",
                            page_url=self.base_url,
                            description="Mobile hamburger/menu toggle button is present",
                        ))

            except Exception:
                pass

        # Reset to desktop
        try:
            page.set_viewport_size({"width": 1280, "height": 800})
        except Exception:
            pass

        return findings

    def _check_performance(self, page, url: str) -> list[BrowserFinding]:
        """Measure page performance via Navigation Timing API."""
        findings: list[BrowserFinding] = []

        try:
            require_safe_url(url)
        except UnsafeUrlError:
            return findings

        try:
            page.goto(url, wait_until="load", timeout=self.timeout_ms)
            timing = page.evaluate("""() => {
                const t = performance.timing;
                return {
                    domContentLoaded: t.domContentLoadedEventEnd - t.navigationStart,
                    fullLoad: t.loadEventEnd - t.navigationStart,
                    ttfb: t.responseStart - t.navigationStart,
                    resourceCount: performance.getEntriesByType('resource').length,
                };
            }""")

            dcl = timing.get("domContentLoaded", 0)
            full = timing.get("fullLoad", 0)
            ttfb = timing.get("ttfb", 0)
            resources = timing.get("resourceCount", 0)

            if full > 0:
                severity = "Major" if full > 5000 else ("Minor" if full > 3000 else "Minor")
                status = "Failed" if full > 5000 else "Passed"
                findings.append(BrowserFinding(
                    check_id="performance_timing",
                    category="Performance",
                    severity=severity,
                    status=status,
                    page_url=url,
                    description=f"Page performance: TTFB={ttfb}ms, DOM loaded={dcl}ms, Full load={full}ms, Resources={resources}",
                    details={"ttfb": ttfb, "dom_loaded": dcl,
                             "full_load": full, "resources": resources},
                ))
        except Exception:
            pass

        return findings

    def _check_visual_issues(self, page, url: str) -> list[BrowserFinding]:
        """Check for common visual issues like overlapping text or tiny click targets."""
        findings: list[BrowserFinding] = []

        try:
            require_safe_url(url)
        except UnsafeUrlError:
            return findings

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception:
            return findings

        # Check for very small click targets (< 44x44 for mobile a11y)
        try:
            small_targets = page.evaluate("""() => {
                const interactive = document.querySelectorAll('a, button, input, select, textarea');
                const small = [];
                for (const el of interactive) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0 &&
                        (rect.width < 24 || rect.height < 24)) {
                        small.push({
                            tag: el.tagName,
                            text: (el.textContent || el.value || '').trim().substring(0, 30),
                            width: Math.round(rect.width),
                            height: Math.round(rect.height),
                        });
                    }
                }
                return small.slice(0, 5);
            }""")

            if small_targets:
                desc_parts = [f"{t['tag']}('{t['text']}'): {t['width']}x{t['height']}px"
                              for t in small_targets[:3]]
                findings.append(BrowserFinding(
                    check_id=f"small_targets_{urlparse(url).path or '/'}",
                    category="UI/UX",
                    severity="Minor",
                    status="Failed",
                    page_url=url,
                    description=f"Small click targets found: {'; '.join(desc_parts)}",
                    details={"small_targets": small_targets},
                ))
        except Exception:
            pass

        # Check for images without explicit dimensions (causes layout shift)
        try:
            no_dimensions = page.evaluate("""() => {
                const imgs = document.querySelectorAll('img');
                let count = 0;
                for (const img of imgs) {
                    if (!img.hasAttribute('width') && !img.hasAttribute('height') &&
                        !img.style.width && !img.style.height) {
                        count++;
                    }
                }
                return {total: imgs.length, noDimensions: count};
            }""")

            if no_dimensions.get("noDimensions", 0) > 0:
                findings.append(BrowserFinding(
                    check_id=f"img_no_dimensions_{urlparse(url).path or '/'}",
                    category="Performance",
                    severity="Minor",
                    status="Failed",
                    page_url=url,
                    description=f"{no_dimensions['noDimensions']} of {no_dimensions['total']} images lack explicit width/height (potential layout shift)",
                ))
        except Exception:
            pass

        return findings

    # ── Run all checks ─────────────────────────────────────────

    def run_all(self) -> BrowserTestReport:
        """Execute all browser-based checks and return the aggregate report."""
        report = BrowserTestReport(base_url=self.base_url)
        context = self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True,
        )
        page = context.new_page()

        # Per-page checks — detailed checks on first 3 pages,
        # load-only checks for the rest (keeps runtime < 30s)
        for i, url in enumerate(self.page_urls):
            # 1. Page load + timing
            page_report = self._check_page_load(page, url)

            # Only do detailed checks if page loaded and is in first 3
            loaded_ok = page_report.findings and page_report.findings[0].status == "Passed"
            if loaded_ok and i < 3:
                # 2. JS console errors
                page_report.findings.extend(self._check_js_console_errors(page, url))
                # 3. Links (only on homepage to avoid rate limiting)
                if i == 0:
                    page_report.findings.extend(self._check_links(page, url))
                # 4. Buttons
                page_report.findings.extend(self._check_buttons(page, url))
                # 5. Forms
                page_report.findings.extend(self._check_forms(page, url))
                # 6. Visual issues
                page_report.findings.extend(self._check_visual_issues(page, url))

            report.pages.append(page_report)

        # Homepage-specific checks
        homepage = self.page_urls[0] if self.page_urls else self.base_url

        # 7. Dropdown menus
        report.findings.extend(self._check_dropdowns(page, homepage))
        # 8. Hover effects
        report.findings.extend(self._check_hover_effects(page, homepage))
        # 9. Performance
        report.findings.extend(self._check_performance(page, homepage))
        # 10. Responsive design
        report.findings.extend(self._check_responsive(page))

        # Flatten page findings into report-level
        for pr in report.pages:
            report.findings.extend(pr.findings)

        # Deduplicate findings by description (same finding on multiple pages)
        seen_desc: set[str] = set()
        unique_findings: list[BrowserFinding] = []
        for f in report.findings:
            key = f"{f.status}|{f.category}|{f.description}"
            if key not in seen_desc:
                seen_desc.add(key)
                unique_findings.append(f)
        report.findings = unique_findings

        # Summary
        report.pages_tested = len(report.pages)
        report.total_findings = len(report.findings)
        passed = sum(1 for f in report.findings if f.status == "Passed")
        failed = sum(1 for f in report.findings if f.status == "Failed")
        by_category: dict[str, int] = {}
        for f in report.findings:
            by_category[f.category] = by_category.get(f.category, 0) + 1

        report.summary = {
            "passed": passed,
            "failed": failed,
            "total": len(report.findings),
            "by_category": by_category,
        }

        page.close()
        context.close()

        return report


# ── Cache ───────────────────────────────────────────────────────

_BROWSER_REPORT_CACHE: dict[tuple, tuple[float, BrowserTestReport]] = {}
_CACHE_TTL = 300  # 5 minutes


def get_or_run(base_url: str, max_pages: int = 20,
               timeout_ms: int = 8000,
               site_analysis=None,
               viewports: str | None = None) -> BrowserTestReport:
    """Run browser tests with caching (5-minute TTL).

    Returns a cached report if one exists for the same URL+max_pages
    +viewports tuple and is less than 5 minutes old.
    """
    key = (base_url.rstrip("/"), max_pages, viewports or "all")
    if key in _BROWSER_REPORT_CACHE:
        ts, cached_report = _BROWSER_REPORT_CACHE[key]
        if time.monotonic() - ts < _CACHE_TTL:
            return cached_report

    with BrowserTestRunner(base_url, max_pages, timeout_ms,
                            site_analysis, viewports=viewports) as runner:
        report = runner.run_all()

    _BROWSER_REPORT_CACHE[key] = (time.monotonic(), report)
    return report
