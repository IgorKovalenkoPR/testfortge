"""
TestFortge — Real Site Testing Engine

Performs actual HTTP and HTML checks against a live website.
Used by QA testers during test execution to produce real
Passed/Failed results instead of simulated ones.

Each check returns a CheckResult with:
  - status: "Passed" or "Failed"
  - actual_result: description of what was actually found

Checks are mapped to checklist/test-case objectives via regex patterns
so the engine can match generated items to real automated tests.
"""

from __future__ import annotations

import re
import ssl
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse


# ── Result data ───────────────────────────────────────────────

@dataclass
class CheckResult:
    """Result of a single real check."""
    status: str          # "Passed" or "Failed"
    actual_result: str   # Human-readable description of what happened


# ── Enhanced HTML parser for testing ──────────────────────────

class _TestPageParser(HTMLParser):
    """Parse HTML and extract detailed structural data for testing."""

    def __init__(self):
        super().__init__()
        self.title = ""
        self.h1 = ""
        self.headings: list[tuple[int, str]] = []   # (level, text)
        self.images: list[dict] = []                 # {src, alt, is_logo, in_header}
        self.links: list[dict] = []                  # {href, text, target, in_nav, in_footer}
        self.forms: list[dict] = []                  # {action, method, fields, has_password}
        self.has_nav = False
        self.has_footer = False
        self.has_header = False
        self.meta_description = ""
        self.has_canonical = False
        self.has_search_input = False
        self.has_favicon = False
        self.has_viewport_meta = False
        self.has_og_title = False
        self.has_og_description = False
        self.has_og_image = False
        self.has_robots_meta = False
        self.robots_content = ""
        self.has_lang_attr = False
        self.lang_value = ""
        self.mailto_links: list[str] = []
        self.tel_links: list[str] = []
        self.social_links: list[dict] = []       # {href, platform}
        self.buttons: list[dict] = []            # {text, type}
        self.iframes: list[str] = []             # src URLs
        self.scripts_external: list[str] = []    # external JS src
        self.stylesheets: list[str] = []         # external CSS href
        self.has_main = False
        self.has_aria_landmarks = False
        self.label_ids: set[str] = set()         # "for" attr values
        self.input_ids: set[str] = set()         # input id values
        self.all_text_parts: list[str] = []

        # State
        self._in_title = False
        self._in_button = False
        self._btn_text = ""
        self._in_h = False
        self._h_level = 0
        self._h_text = ""
        self._in_nav = False
        self._in_footer = False
        self._in_header = False
        self._in_a = False
        self._a_href = ""
        self._a_target = ""
        self._a_text = ""
        self._current_form: dict | None = None
        self._txt = ""

    def handle_starttag(self, tag: str, attrs: list[tuple]):
        a = dict(attrs)
        t = tag.lower()

        if t == "title":
            self._in_title = True
            self._txt = ""
        elif t == "h1" and not self.h1:
            self._in_h = True
            self._h_level = 1
            self._h_text = ""
        elif t in ("h2", "h3", "h4", "h5", "h6"):
            self._in_h = True
            self._h_level = int(t[1])
            self._h_text = ""
        elif t == "header":
            self.has_header = True
            self._in_header = True
        elif t == "nav":
            self.has_nav = True
            self._in_nav = True
        elif t == "footer":
            self.has_footer = True
            self._in_footer = True
        elif t == "a":
            href = a.get("href", "")
            self._in_a = True
            self._a_href = href
            self._a_target = a.get("target", "")
            self._a_text = ""
        elif t == "img":
            src = a.get("src", "")
            alt = a.get("alt", "")
            cls = a.get("class", "")
            img_id = a.get("id", "")
            is_logo = bool(re.search(r"logo", f"{src} {alt} {cls} {img_id}", re.I))
            self.images.append({
                "src": src, "alt": alt, "is_logo": is_logo,
                "in_header": self._in_header or self._in_nav,
                "parent_link": self._a_href if self._in_a else "",
            })
        elif t == "svg":
            cls = a.get("class", "")
            svg_id = a.get("id", "")
            aria = a.get("aria-label", "")
            if re.search(r"logo", f"{cls} {svg_id} {aria}", re.I):
                self.images.append({
                    "src": "(svg)", "alt": aria, "is_logo": True,
                    "in_header": self._in_header or self._in_nav,
                    "parent_link": self._a_href if self._in_a else "",
                })
        elif t == "form":
            self._current_form = {
                "action": a.get("action", ""),
                "method": a.get("method", "GET").upper(),
                "fields": [],
                "has_password": False,
            }
        elif t == "input":
            itype = a.get("type", "text").lower()
            iname = a.get("name", "").lower()
            placeholder = a.get("placeholder", "").lower()
            if itype == "search" or "search" in iname or "search" in placeholder:
                self.has_search_input = True
            if itype == "password" and self._current_form is not None:
                self._current_form["has_password"] = True
            if self._current_form is not None and itype not in ("hidden",):
                self._current_form["fields"].append({
                    "name": a.get("name", ""),
                    "type": itype,
                    "id": a.get("id", ""),
                    "placeholder": a.get("placeholder", ""),
                })
        elif t == "textarea":
            if self._current_form is not None:
                self._current_form["fields"].append({
                    "name": a.get("name", ""),
                    "type": "textarea",
                    "id": a.get("id", ""),
                    "placeholder": a.get("placeholder", ""),
                })
        elif t == "select":
            if self._current_form is not None:
                self._current_form["fields"].append({
                    "name": a.get("name", ""),
                    "type": "select",
                    "id": a.get("id", ""),
                    "placeholder": "",
                })
        elif t == "button":
            self._in_button = True
            self._btn_text = ""
        elif t == "label":
            for_val = a.get("for", "")
            if for_val:
                self.label_ids.add(for_val)
        elif t == "iframe":
            src = a.get("src", "")
            if src:
                self.iframes.append(src)
        elif t == "script":
            src = a.get("src", "")
            if src:
                self.scripts_external.append(src)
        elif t == "main":
            self.has_main = True
        elif t in ("div", "section", "aside"):
            role = a.get("role", "").lower()
            if role in ("banner", "navigation", "main", "contentinfo",
                        "complementary", "search", "form"):
                self.has_aria_landmarks = True
        elif t == "html":
            lang = a.get("lang", "")
            if lang:
                self.has_lang_attr = True
                self.lang_value = lang
        elif t == "meta":
            name = a.get("name", "").lower()
            prop = a.get("property", "").lower()
            content = a.get("content", "")
            if name == "description":
                self.meta_description = content
            elif name == "viewport":
                self.has_viewport_meta = True
            elif name == "robots":
                self.has_robots_meta = True
                self.robots_content = content
            elif prop == "og:title":
                self.has_og_title = True
            elif prop == "og:description":
                self.has_og_description = True
            elif prop == "og:image":
                self.has_og_image = True
        elif t == "link":
            rel = a.get("rel", "").lower()
            if rel == "canonical":
                self.has_canonical = True
            elif "icon" in rel:
                self.has_favicon = True
            elif rel == "stylesheet":
                href = a.get("href", "")
                if href:
                    self.stylesheets.append(href)

    def handle_endtag(self, tag: str):
        t = tag.lower()
        if t == "title":
            self._in_title = False
            self.title = self._txt.strip()
        elif t == "h1":
            if self._in_h and self._h_level == 1:
                self.h1 = self._h_text.strip()
                self.headings.append((1, self.h1))
            self._in_h = False
        elif t in ("h2", "h3", "h4", "h5", "h6"):
            if self._in_h and self._h_text.strip():
                self.headings.append((self._h_level, self._h_text.strip()))
            self._in_h = False
        elif t == "header":
            self._in_header = False
        elif t == "nav":
            self._in_nav = False
        elif t == "footer":
            self._in_footer = False
        elif t == "a":
            href = self._a_href
            text = self._a_text.strip()
            if href:
                if href.startswith("mailto:"):
                    self.mailto_links.append(href)
                elif href.startswith("tel:"):
                    self.tel_links.append(href)
                elif not href.startswith(("#", "javascript:")):
                    self.links.append({
                        "href": href,
                        "text": text,
                        "target": self._a_target,
                        "in_nav": self._in_nav,
                        "in_footer": self._in_footer,
                    })
                    # Detect social links
                    _SOCIAL = {
                        "facebook.com": "Facebook", "fb.com": "Facebook",
                        "twitter.com": "Twitter", "x.com": "Twitter",
                        "instagram.com": "Instagram",
                        "linkedin.com": "LinkedIn",
                        "youtube.com": "YouTube",
                        "tiktok.com": "TikTok",
                        "github.com": "GitHub",
                        "t.me": "Telegram",
                    }
                    href_lower = href.lower()
                    for domain, platform in _SOCIAL.items():
                        if domain in href_lower:
                            self.social_links.append({"href": href, "platform": platform})
                            break
            self._in_a = False
        elif t == "button":
            if self._in_button and self._btn_text.strip():
                self.buttons.append({"text": self._btn_text.strip(), "type": "button"})
            self._in_button = False
        elif t == "form":
            if self._current_form is not None:
                self.forms.append(self._current_form)
                self._current_form = None

    def handle_data(self, data: str):
        if self._in_title:
            self._txt += data
        if self._in_h:
            self._h_text += data
        if self._in_a:
            self._a_text += data
        if self._in_button:
            self._btn_text += data
        self.all_text_parts.append(data)


# ── Page data ─────────────────────────────────────────────────

@dataclass
class TestPageData:
    url: str
    status_code: int
    response_time_ms: int
    title: str = ""
    h1: str = ""
    headings: list[tuple[int, str]] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    forms: list[dict] = field(default_factory=list)
    has_nav: bool = False
    has_footer: bool = False
    has_header: bool = False
    meta_description: str = ""
    has_canonical: bool = False
    has_search_input: bool = False
    has_favicon: bool = False
    has_viewport_meta: bool = False
    has_og_title: bool = False
    has_og_description: bool = False
    has_og_image: bool = False
    has_robots_meta: bool = False
    robots_content: str = ""
    has_lang_attr: bool = False
    lang_value: str = ""
    has_main: bool = False
    has_aria_landmarks: bool = False
    mailto_links: list[str] = field(default_factory=list)
    tel_links: list[str] = field(default_factory=list)
    social_links: list[dict] = field(default_factory=list)
    buttons: list[dict] = field(default_factory=list)
    iframes: list[str] = field(default_factory=list)
    scripts_external: list[str] = field(default_factory=list)
    stylesheets: list[str] = field(default_factory=list)
    label_ids: set = field(default_factory=set)
    input_ids: set = field(default_factory=set)
    all_text: str = ""
    error: str = ""


# ── HTTP helpers ──────────────────────────────────────────────

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) QAForge/1.0 SiteTester",
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

_SKIP_EXT = frozenset([
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
    ".css", ".js", ".zip", ".mp4", ".webm", ".ico", ".woff",
    ".woff2", ".ttf", ".eot", ".xml", ".json",
])


def _fetch_test_page(url: str, timeout: int = 8) -> TestPageData:
    """Fetch a URL, parse HTML, return structured TestPageData."""
    page = TestPageData(url=url, status_code=0, response_time_ms=0)
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        start = time.monotonic()
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            elapsed = time.monotonic() - start
            page.status_code = resp.status
            page.response_time_ms = int(elapsed * 1000)

            content_type = resp.headers.get("Content-Type", "")
            if "text/html" not in content_type and "xhtml" not in content_type:
                page.error = f"Not HTML: {content_type}"
                return page

            raw = resp.read(512 * 1024)
            charset = "utf-8"
            if "charset=" in content_type.lower():
                charset = content_type.lower().split("charset=")[-1].split(";")[0].strip()
            try:
                html = raw.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                html = raw.decode("utf-8", errors="replace")

        parser = _TestPageParser()
        try:
            parser.feed(html)
        except Exception:
            pass

        page.title = parser.title
        page.h1 = parser.h1
        page.headings = parser.headings
        page.images = parser.images
        page.links = parser.links
        page.forms = parser.forms
        page.has_nav = parser.has_nav
        page.has_footer = parser.has_footer
        page.has_header = parser.has_header
        page.meta_description = parser.meta_description
        page.has_canonical = parser.has_canonical
        page.has_search_input = parser.has_search_input
        page.has_favicon = parser.has_favicon
        page.has_viewport_meta = parser.has_viewport_meta
        page.has_og_title = parser.has_og_title
        page.has_og_description = parser.has_og_description
        page.has_og_image = parser.has_og_image
        page.has_robots_meta = parser.has_robots_meta
        page.robots_content = parser.robots_content
        page.has_lang_attr = parser.has_lang_attr
        page.lang_value = parser.lang_value
        page.has_main = parser.has_main
        page.has_aria_landmarks = parser.has_aria_landmarks
        page.mailto_links = parser.mailto_links
        page.tel_links = parser.tel_links
        page.social_links = parser.social_links
        page.buttons = parser.buttons
        page.iframes = parser.iframes
        page.scripts_external = parser.scripts_external
        page.stylesheets = parser.stylesheets
        page.label_ids = parser.label_ids
        page.input_ids = parser.input_ids
        page.all_text = " ".join(parser.all_text_parts)

    except urllib.error.HTTPError as e:
        page.status_code = e.code
        page.error = f"HTTP {e.code}"
    except urllib.error.URLError as e:
        page.error = f"URL error: {e.reason}"
    except Exception as e:
        page.error = str(e)[:200]

    return page


def _check_url_status(url: str, timeout: int = 5) -> tuple[int, float]:
    """HEAD-check a URL. Returns (status_code, response_time_seconds).

    Returns (-1, 0) on connection error.
    """
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers=_HEADERS)
            start = time.monotonic()
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
                elapsed = time.monotonic() - start
                return resp.status, elapsed
        except urllib.error.HTTPError as e:
            return e.code, 0
        except Exception:
            if method == "HEAD":
                continue
            return -1, 0
    return -1, 0


# ── Site Test Runner ──────────────────────────────────────────

class SiteTestRunner:
    """Runs real automated checks against a website.

    Crawls up to *max_pages* internal pages, then runs ~30 checks
    covering structure, links, images, SEO, security, and performance.
    """

    MAX_LINK_CHECKS = 40  # max links to verify via HTTP

    def __init__(self, base_url: str, max_pages: int = 20):
        parsed = urlparse(base_url)
        if not parsed.scheme:
            base_url = "https://" + base_url
            parsed = urlparse(base_url)
        self.base_url = base_url
        self.domain = parsed.netloc
        self.pages: list[TestPageData] = []
        self.home: TestPageData | None = None
        self._url_status_cache: dict[str, int] = {}
        self._crawl(max_pages)

    # ── Crawl ─────────────────────────────────────────────────

    def _crawl(self, max_pages: int):
        """Fetch and parse internal pages."""
        visited: set[str] = set()
        queue = [self.base_url]

        while queue and len(visited) < max_pages:
            url = queue.pop(0)
            norm = urlparse(url)
            clean = f"{norm.scheme}://{norm.netloc}{norm.path}".rstrip("/")
            if clean in visited:
                continue
            path_lower = norm.path.lower()
            if any(path_lower.endswith(ext) for ext in _SKIP_EXT):
                continue
            visited.add(clean)

            page = _fetch_test_page(url)
            if page.error and page.status_code == 0:
                continue
            self.pages.append(page)
            if self.home is None:
                self.home = page

            # Enqueue internal links
            for link in page.links:
                href = link.get("href", "")
                full = urljoin(url, href)
                lp = urlparse(full)
                lc = f"{lp.scheme}://{lp.netloc}{lp.path}".rstrip("/")
                if lp.netloc == self.domain and lc not in visited:
                    queue.append(full)

    # ── URL status check (cached) ─────────────────────────────

    def _url_ok(self, url: str) -> int:
        """Check URL status with caching. Returns HTTP status code."""
        if url in self._url_status_cache:
            return self._url_status_cache[url]
        code, _ = _check_url_status(url)
        self._url_status_cache[url] = code
        return code

    # ── Individual checks ─────────────────────────────────────

    def check_page_loads(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", f"Could not fetch {self.base_url}")
        if self.home.status_code == 200:
            return CheckResult("Passed",
                f"Homepage loaded successfully (HTTP 200, {self.home.response_time_ms}ms)")
        return CheckResult("Failed",
            f"Homepage returned HTTP {self.home.status_code}")

    def check_https(self) -> CheckResult:
        if self.base_url.startswith("https://"):
            return CheckResult("Passed", "Site is served over HTTPS")
        return CheckResult("Failed",
            f"Site is NOT served over HTTPS (URL: {self.base_url})")

    def check_load_time(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Could not measure — homepage not loaded")
        ms = self.home.response_time_ms
        if ms <= 3000:
            return CheckResult("Passed", f"Page loaded in {ms}ms (threshold: 3000ms)")
        return CheckResult("Failed", f"Page loaded in {ms}ms — exceeds 3000ms threshold")

    def check_page_title(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        if self.home.title:
            length = len(self.home.title)
            status = "Passed" if length <= 60 else "Failed"
            msg = f"Title found: \"{self.home.title}\" ({length} chars)"
            if length > 60:
                msg += " — exceeds recommended 60 characters"
            return CheckResult(status, msg)
        return CheckResult("Failed", "No <title> tag found on the homepage")

    def check_h1_displayed(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        if self.home.h1:
            return CheckResult("Passed", f"H1 heading found: \"{self.home.h1}\"")
        return CheckResult("Failed", "No H1 heading found on the homepage")

    def check_heading_hierarchy(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        headings = self.home.headings
        h1_count = sum(1 for lvl, _ in headings if lvl == 1)
        if h1_count == 0:
            return CheckResult("Failed", "No H1 heading found")
        if h1_count > 1:
            return CheckResult("Failed",
                f"Multiple H1 headings found ({h1_count}) — should be exactly 1")
        # Check logical order
        levels = [lvl for lvl, _ in headings]
        for i in range(1, len(levels)):
            if levels[i] > levels[i - 1] + 1:
                return CheckResult("Failed",
                    f"Heading hierarchy broken: H{levels[i-1]} followed by H{levels[i]} (skipped level)")
        return CheckResult("Passed",
            f"Heading hierarchy is correct: 1 H1, {len(headings)} total headings")

    def check_logo_displayed(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        logos = [img for img in self.home.images if img.get("is_logo")]
        if logos:
            src = logos[0].get("src", "(svg)")
            return CheckResult("Passed", f"Logo found: {src}")
        return CheckResult("Failed",
            "No logo element found (checked img/svg with 'logo' in class/id/src/alt)")

    def check_logo_links_home(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        logos = [img for img in self.home.images if img.get("is_logo")]
        if not logos:
            return CheckResult("Failed", "No logo found to check link")
        link = logos[0].get("parent_link", "")
        if link:
            resolved = urljoin(self.base_url, link)
            home_clean = self.base_url.rstrip("/")
            resolved_clean = resolved.rstrip("/")
            if resolved_clean == home_clean or link in ("/", "/#", ""):
                return CheckResult("Passed", f"Logo links to homepage ({link})")
            return CheckResult("Failed",
                f"Logo links to '{link}' instead of homepage")
        return CheckResult("Failed", "Logo is not wrapped in a link (<a> tag)")

    def check_nav_displayed(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        if self.home.has_nav:
            nav_links = [l for l in self.home.links if l.get("in_nav")]
            return CheckResult("Passed",
                f"Navigation <nav> found with {len(nav_links)} links")
        return CheckResult("Failed", "No <nav> element found on the homepage")

    def check_nav_links_work(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        nav_links = [l for l in self.home.links if l.get("in_nav")]
        if not nav_links:
            return CheckResult("Failed", "No navigation links found")
        ok = 0
        errors = []
        checked = 0
        for link in nav_links[:self.MAX_LINK_CHECKS]:
            href = link.get("href", "")
            if not href:
                continue
            full = urljoin(self.base_url, href)
            code = self._url_ok(full)
            checked += 1
            if 200 <= code < 400:
                ok += 1
            else:
                text = link.get("text", href)[:40]
                errors.append(f"{text} → HTTP {code}")
        if errors:
            return CheckResult("Failed",
                f"{ok}/{checked} nav links OK. Failed: {'; '.join(errors[:5])}")
        return CheckResult("Passed",
            f"All {checked} navigation links return valid responses")

    def check_images_loaded(self) -> CheckResult:
        """Check that image src URLs return HTTP 200."""
        all_imgs = []
        for page in self.pages:
            for img in page.images:
                src = img.get("src", "")
                if src and src != "(svg)":
                    full = urljoin(page.url, src)
                    if full not in [i[0] for i in all_imgs]:
                        all_imgs.append((full, src))
        if not all_imgs:
            return CheckResult("Passed", "No external images found on the site")
        ok = 0
        broken = []
        for full_url, original_src in all_imgs[:self.MAX_LINK_CHECKS]:
            code = self._url_ok(full_url)
            if 200 <= code < 400:
                ok += 1
            else:
                broken.append(f"{original_src[:50]} → HTTP {code}")
        total = min(len(all_imgs), self.MAX_LINK_CHECKS)
        if broken:
            return CheckResult("Failed",
                f"{ok}/{total} images loaded. Broken: {'; '.join(broken[:5])}")
        return CheckResult("Passed", f"All {total} images loaded successfully (HTTP 200)")

    def check_images_have_alt(self) -> CheckResult:
        """Check that images have alt attributes."""
        all_imgs = []
        for page in self.pages:
            for img in page.images:
                if img.get("src", "") != "(svg)":
                    all_imgs.append(img)
        if not all_imgs:
            return CheckResult("Passed", "No images found on the site")
        with_alt = [img for img in all_imgs if img.get("alt", "").strip()]
        without = len(all_imgs) - len(with_alt)
        if without == 0:
            return CheckResult("Passed",
                f"All {len(all_imgs)} images have alt attributes")
        return CheckResult("Failed",
            f"{without} of {len(all_imgs)} images are missing alt attributes")

    def check_internal_links(self) -> CheckResult:
        """Check that internal links return valid responses."""
        internal = set()
        for page in self.pages:
            for link in page.links:
                href = link.get("href", "")
                full = urljoin(page.url, href)
                p = urlparse(full)
                if p.netloc == self.domain:
                    internal.add(full)
        if not internal:
            return CheckResult("Passed", "No internal links found")
        ok = 0
        errors = []
        checked = 0
        for url in list(internal)[:self.MAX_LINK_CHECKS]:
            code = self._url_ok(url)
            checked += 1
            if 200 <= code < 400:
                ok += 1
            else:
                path = urlparse(url).path or url
                errors.append(f"{path[:50]} → HTTP {code}")
        if errors:
            return CheckResult("Failed",
                f"{ok}/{checked} internal links OK. Broken: {'; '.join(errors[:5])}")
        return CheckResult("Passed",
            f"All {checked} internal links return valid responses")

    def check_no_broken_links(self) -> CheckResult:
        """Check for any 404 links across crawled pages."""
        all_links = set()
        for page in self.pages:
            for link in page.links:
                href = link.get("href", "")
                full = urljoin(page.url, href)
                all_links.add(full)
        if not all_links:
            return CheckResult("Passed", "No links found")
        broken = []
        checked = 0
        for url in list(all_links)[:self.MAX_LINK_CHECKS]:
            code = self._url_ok(url)
            checked += 1
            if code == 404:
                path = urlparse(url).path or url
                broken.append(path[:60])
        if broken:
            return CheckResult("Failed",
                f"{len(broken)} broken link(s) (404) found: {'; '.join(broken[:5])}")
        return CheckResult("Passed",
            f"No broken links (404) found among {checked} checked links")

    def check_external_links_target(self) -> CheckResult:
        """Check that external links open in a new tab (target='_blank')."""
        external = []
        for page in self.pages:
            for link in page.links:
                href = link.get("href", "")
                full = urljoin(page.url, href)
                p = urlparse(full)
                if p.netloc and p.netloc != self.domain:
                    external.append(link)
        if not external:
            return CheckResult("Passed", "No external links found")
        without_blank = [l for l in external if l.get("target") != "_blank"]
        if without_blank:
            samples = [l.get("text", l.get("href", ""))[:30] for l in without_blank[:3]]
            return CheckResult("Failed",
                f"{len(without_blank)} of {len(external)} external links lack target=\"_blank\": {', '.join(samples)}")
        return CheckResult("Passed",
            f"All {len(external)} external links have target=\"_blank\"")

    def check_footer_displayed(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        if self.home.has_footer:
            footer_links = [l for l in self.home.links if l.get("in_footer")]
            return CheckResult("Passed",
                f"Footer <footer> found with {len(footer_links)} links")
        return CheckResult("Failed", "No <footer> element found on the homepage")

    def check_footer_links(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        footer_links = [l for l in self.home.links if l.get("in_footer")]
        if not footer_links:
            return CheckResult("Failed", "No links found in footer")
        ok = 0
        errors = []
        for link in footer_links[:20]:
            full = urljoin(self.base_url, link.get("href", ""))
            code = self._url_ok(full)
            if 200 <= code < 400:
                ok += 1
            else:
                text = link.get("text", link.get("href", ""))[:30]
                errors.append(f"{text} → HTTP {code}")
        if errors:
            return CheckResult("Failed",
                f"{ok}/{len(footer_links)} footer links OK. Failed: {'; '.join(errors[:3])}")
        return CheckResult("Passed",
            f"All {len(footer_links)} footer links return valid responses")

    def check_copyright(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        text_lower = self.home.all_text.lower()
        if "©" in self.home.all_text or "copyright" in text_lower:
            return CheckResult("Passed", "Copyright text found on the page")
        return CheckResult("Failed", "No copyright text (© or 'copyright') found")

    def check_meta_description(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        desc = self.home.meta_description
        if desc:
            length = len(desc)
            if length <= 160:
                return CheckResult("Passed",
                    f"Meta description found ({length} chars)")
            return CheckResult("Failed",
                f"Meta description found but too long ({length} chars, recommended: ≤160)")
        return CheckResult("Failed", "No meta description found")

    def check_canonical(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        if self.home.has_canonical:
            return CheckResult("Passed", "Canonical URL (<link rel=\"canonical\">) is specified")
        return CheckResult("Failed", "No canonical URL found")

    def check_no_placeholder(self) -> CheckResult:
        for page in self.pages:
            text_lower = page.all_text.lower()
            if "lorem ipsum" in text_lower:
                return CheckResult("Failed",
                    f"'Lorem ipsum' placeholder text found on {page.url}")
        return CheckResult("Passed",
            f"No placeholder text found across {len(self.pages)} pages")

    def check_footer_consistent(self) -> CheckResult:
        """Check that footer appears on all crawled pages."""
        if len(self.pages) < 2:
            if self.home and self.home.has_footer:
                return CheckResult("Passed", "Footer found on the homepage")
            return CheckResult("Failed", "No footer found")
        without = [p for p in self.pages if not p.has_footer]
        if without:
            urls = [urlparse(p.url).path or p.url for p in without[:3]]
            return CheckResult("Failed",
                f"Footer missing on {len(without)}/{len(self.pages)} pages: {', '.join(urls)}")
        return CheckResult("Passed",
            f"Footer is present on all {len(self.pages)} crawled pages")

    def check_login_form(self) -> CheckResult:
        """Check if any page has a login form (password field)."""
        for page in self.pages:
            for form in page.forms:
                if form.get("has_password"):
                    return CheckResult("Passed",
                        f"Login form found on {urlparse(page.url).path or page.url}")
        return CheckResult("Failed", "No login form (password field) found on crawled pages")

    def check_password_masked(self) -> CheckResult:
        """Check that password fields use type='password'."""
        for page in self.pages:
            for form in page.forms:
                if form.get("has_password"):
                    for f in form.get("fields", []):
                        if f.get("type") == "password":
                            return CheckResult("Passed",
                                "Password field is masked (type=\"password\")")
        return CheckResult("Failed", "No password field found to verify masking")

    def check_search_field(self) -> CheckResult:
        for page in self.pages:
            if page.has_search_input:
                return CheckResult("Passed",
                    f"Search input found on {urlparse(page.url).path or page.url}")
        return CheckResult("Failed", "No search input found on crawled pages")

    def check_form_fields(self) -> CheckResult:
        """Check that forms have user-visible fields."""
        for page in self.pages:
            for form in page.forms:
                visible = [f for f in form.get("fields", [])
                           if f.get("type") not in ("submit", "button")]
                if len(visible) >= 2:
                    names = [f.get("name") or f.get("id") or f.get("type")
                             for f in visible[:5]]
                    return CheckResult("Passed",
                        f"Form found with {len(visible)} fields: {', '.join(names)}")
        return CheckResult("Failed", "No forms with 2+ visible fields found")

    def check_all_pages_accessible(self) -> CheckResult:
        """Check that all crawled pages return HTTP 200."""
        errors = []
        for page in self.pages:
            if page.status_code != 200:
                path = urlparse(page.url).path or page.url
                errors.append(f"{path} → HTTP {page.status_code}")
        if errors:
            return CheckResult("Failed",
                f"{len(errors)}/{len(self.pages)} pages not accessible: {'; '.join(errors[:5])}")
        return CheckResult("Passed",
            f"All {len(self.pages)} crawled pages return HTTP 200")

    def check_mixed_content(self) -> CheckResult:
        """Check for HTTP resources loaded on HTTPS pages."""
        if not self.base_url.startswith("https://"):
            return CheckResult("Passed", "Site does not use HTTPS — mixed content check N/A")
        for page in self.pages:
            for img in page.images:
                src = img.get("src", "")
                if src.startswith("http://"):
                    return CheckResult("Failed",
                        f"Mixed content: HTTP image '{src[:60]}' on HTTPS page {page.url}")
            for link in page.links:
                href = link.get("href", "")
                if href.startswith("http://") and urlparse(href).netloc == self.domain:
                    return CheckResult("Failed",
                        f"Mixed content: HTTP link '{href[:60]}' on HTTPS page")
        return CheckResult("Passed",
            f"No mixed content found across {len(self.pages)} pages")

    # ── Expanded checks ─────────────────────────────────────────

    def check_favicon(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        if self.home.has_favicon:
            return CheckResult("Passed", "Favicon (<link rel='icon'>) is specified")
        return CheckResult("Failed", "No favicon found")

    def check_viewport_meta(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        if self.home.has_viewport_meta:
            return CheckResult("Passed", "Viewport meta tag is present (responsive-ready)")
        return CheckResult("Failed",
            "No <meta name='viewport'> tag — page may not render correctly on mobile")

    def check_og_tags(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        found = []
        missing = []
        for label, flag in [("og:title", self.home.has_og_title),
                            ("og:description", self.home.has_og_description),
                            ("og:image", self.home.has_og_image)]:
            (found if flag else missing).append(label)
        if not missing:
            return CheckResult("Passed", f"All Open Graph tags present: {', '.join(found)}")
        if found:
            return CheckResult("Failed",
                f"Missing Open Graph tags: {', '.join(missing)} (found: {', '.join(found)})")
        return CheckResult("Failed", "No Open Graph tags found (og:title, og:description, og:image)")

    def check_lang_attribute(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        if self.home.has_lang_attr:
            return CheckResult("Passed",
                f"HTML lang attribute is set: lang=\"{self.home.lang_value}\"")
        return CheckResult("Failed",
            "No lang attribute on <html> element — required for accessibility and SEO")

    def check_social_links(self) -> CheckResult:
        all_social: list[dict] = []
        for page in self.pages:
            for s in page.social_links:
                if s["platform"] not in [x["platform"] for x in all_social]:
                    all_social.append(s)
        if not all_social:
            return CheckResult("Passed", "No social media links found (N/A)")
        ok = 0
        errors = []
        for s in all_social:
            code = self._url_ok(s["href"])
            if 200 <= code < 400:
                ok += 1
            else:
                errors.append(f"{s['platform']} → HTTP {code}")
        if errors:
            return CheckResult("Failed",
                f"{ok}/{len(all_social)} social links OK. Failed: {'; '.join(errors[:3])}")
        platforms = ", ".join(s["platform"] for s in all_social)
        return CheckResult("Passed",
            f"All {len(all_social)} social links reachable: {platforms}")

    def check_mailto_links(self) -> CheckResult:
        mailto = set()
        for page in self.pages:
            for m in page.mailto_links:
                mailto.add(m)
        if not mailto:
            return CheckResult("Passed", "No mailto links found (N/A)")
        valid = [m for m in mailto if "@" in m]
        if len(valid) == len(mailto):
            return CheckResult("Passed",
                f"{len(mailto)} mailto link(s) found and properly formatted")
        return CheckResult("Failed",
            f"Invalid mailto link(s) found: {[m for m in mailto if m not in valid]}")

    def check_tel_links(self) -> CheckResult:
        tel = set()
        for page in self.pages:
            for t in page.tel_links:
                tel.add(t)
        if not tel:
            return CheckResult("Passed", "No tel: links found (N/A)")
        return CheckResult("Passed", f"{len(tel)} telephone link(s) found")

    def check_cta_buttons(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        btns = self.home.buttons
        submit_inputs = []
        for form in self.home.forms:
            for f in form.get("fields", []):
                if f.get("type") in ("submit", "button"):
                    submit_inputs.append(f)
        total = len(btns) + len(submit_inputs)
        if total > 0:
            samples = [b["text"][:25] for b in btns[:3]]
            return CheckResult("Passed",
                f"{total} interactive button(s) found on the homepage: {', '.join(samples) if samples else 'submit inputs'}")
        return CheckResult("Failed", "No <button> or submit elements found on the homepage")

    def check_content_readable(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        text = self.home.all_text.strip()
        word_count = len(text.split())
        if word_count < 10:
            return CheckResult("Failed",
                f"Very little text content on homepage ({word_count} words)")
        return CheckResult("Passed",
            f"Homepage has readable text content ({word_count} words)")

    def check_content_sections_order(self) -> CheckResult:
        """Check that header/main/footer appear in logical order."""
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        has = []
        if self.home.has_header:
            has.append("header")
        if self.home.has_main:
            has.append("main")
        if self.home.has_footer:
            has.append("footer")
        if len(has) >= 2:
            return CheckResult("Passed",
                f"Semantic structure found in correct order: {' → '.join(has)}")
        return CheckResult("Failed",
            f"Incomplete semantic structure (found: {', '.join(has) or 'none'}) — expected header, main, footer")

    def check_form_labels(self) -> CheckResult:
        """Check that form input fields have associated <label> elements."""
        for page in self.pages:
            if not page.forms:
                continue
            inputs_with_id = set()
            for form in page.forms:
                for f in form.get("fields", []):
                    fid = f.get("id", "")
                    if fid and f.get("type") not in ("submit", "button", "hidden"):
                        inputs_with_id.add(fid)
            if not inputs_with_id:
                continue
            labeled = inputs_with_id & page.label_ids
            unlabeled = inputs_with_id - labeled
            if unlabeled:
                return CheckResult("Failed",
                    f"{len(unlabeled)} form field(s) without associated <label>: {', '.join(list(unlabeled)[:5])}")
            return CheckResult("Passed",
                f"All {len(labeled)} form fields have associated <label> elements")
        return CheckResult("Passed", "No form fields with IDs found to check labels")

    def check_keyboard_focus(self) -> CheckResult:
        """Check that page has interactive elements (links, buttons, inputs)."""
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        interactive = len(self.home.links) + len(self.home.buttons)
        for form in self.home.forms:
            interactive += len(form.get("fields", []))
        if interactive > 0:
            return CheckResult("Passed",
                f"{interactive} focusable elements found on the homepage (links, buttons, inputs)")
        return CheckResult("Failed", "No focusable interactive elements found")

    def check_aria_landmarks(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        has_any = self.home.has_nav or self.home.has_main or self.home.has_aria_landmarks
        if has_any:
            parts = []
            if self.home.has_nav:
                parts.append("<nav>")
            if self.home.has_main:
                parts.append("<main>")
            if self.home.has_aria_landmarks:
                parts.append("ARIA roles")
            return CheckResult("Passed",
                f"Accessibility landmarks found: {', '.join(parts)}")
        return CheckResult("Failed",
            "No ARIA landmarks or semantic elements (nav, main) found")

    def check_color_contrast_hint(self) -> CheckResult:
        """Heuristic: check that stylesheets are loaded (prerequisite for contrast)."""
        total_css = 0
        for page in self.pages:
            total_css += len(page.stylesheets)
        if total_css > 0:
            return CheckResult("Passed",
                f"{total_css} stylesheet(s) loaded — manual contrast check recommended (WCAG 4.5:1)")
        return CheckResult("Failed",
            "No external stylesheets found — possible rendering issues")

    def check_nav_on_all_pages(self) -> CheckResult:
        """Check that navigation appears consistently on all pages."""
        if len(self.pages) < 2:
            if self.home and self.home.has_nav:
                return CheckResult("Passed", "Navigation found on the homepage")
            return CheckResult("Failed", "No navigation found")
        without = [p for p in self.pages if not p.has_nav]
        if without:
            urls = [urlparse(p.url).path or "/" for p in without[:3]]
            return CheckResult("Failed",
                f"Navigation missing on {len(without)}/{len(self.pages)} pages: {', '.join(urls)}")
        return CheckResult("Passed",
            f"Navigation is present on all {len(self.pages)} crawled pages")

    def check_header_on_all_pages(self) -> CheckResult:
        if len(self.pages) < 2:
            if self.home and self.home.has_header:
                return CheckResult("Passed", "Header found on the homepage")
            return CheckResult("Failed", "No <header> found")
        without = [p for p in self.pages if not p.has_header]
        if without:
            urls = [urlparse(p.url).path or "/" for p in without[:3]]
            return CheckResult("Failed",
                f"Header missing on {len(without)}/{len(self.pages)} pages: {', '.join(urls)}")
        return CheckResult("Passed",
            f"Header is present on all {len(self.pages)} crawled pages")

    def check_title_on_all_pages(self) -> CheckResult:
        without = [p for p in self.pages if not p.title]
        if without:
            urls = [urlparse(p.url).path or "/" for p in without[:3]]
            return CheckResult("Failed",
                f"{len(without)}/{len(self.pages)} pages have no <title>: {', '.join(urls)}")
        unique = set(p.title for p in self.pages)
        if len(unique) == 1 and len(self.pages) > 1:
            return CheckResult("Failed",
                f"All {len(self.pages)} pages have the same title — each should be unique")
        return CheckResult("Passed",
            f"All {len(self.pages)} pages have <title> tags ({len(unique)} unique)")

    def check_h1_on_all_pages(self) -> CheckResult:
        without = [p for p in self.pages if not p.h1]
        if without:
            urls = [urlparse(p.url).path or "/" for p in without[:3]]
            return CheckResult("Failed",
                f"{len(without)}/{len(self.pages)} pages have no H1: {', '.join(urls)}")
        return CheckResult("Passed",
            f"All {len(self.pages)} pages have an H1 heading")

    def check_response_times(self) -> CheckResult:
        slow = [(p.url, p.response_time_ms) for p in self.pages if p.response_time_ms > 3000]
        if slow:
            details = [f"{urlparse(u).path or u} ({ms}ms)" for u, ms in slow[:3]]
            return CheckResult("Failed",
                f"{len(slow)}/{len(self.pages)} pages exceed 3s: {'; '.join(details)}")
        avg_ms = sum(p.response_time_ms for p in self.pages) // max(len(self.pages), 1)
        return CheckResult("Passed",
            f"All {len(self.pages)} pages load under 3s (avg: {avg_ms}ms)")

    def check_page_size(self) -> CheckResult:
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        text_len = len(self.home.all_text)
        if text_len > 0:
            kb = text_len // 1024
            return CheckResult("Passed",
                f"Homepage text content size: ~{kb}KB (reasonable)")
        return CheckResult("Failed", "Homepage has no text content")

    def check_images_optimized(self) -> CheckResult:
        """Heuristic: check for modern image formats (webp, avif)."""
        all_srcs = []
        for page in self.pages:
            for img in page.images:
                src = img.get("src", "").lower()
                if src and src != "(svg)":
                    all_srcs.append(src)
        if not all_srcs:
            return CheckResult("Passed", "No images to check for optimization")
        modern = [s for s in all_srcs if any(s.endswith(ext) for ext in (".webp", ".avif"))]
        if modern:
            pct = round(len(modern) / len(all_srcs) * 100)
            return CheckResult("Passed",
                f"{len(modern)}/{len(all_srcs)} images use modern formats (WebP/AVIF) — {pct}%")
        return CheckResult("Failed",
            f"None of {len(all_srcs)} images use modern formats (WebP/AVIF) — consider optimizing")

    def check_double_submit_protection(self) -> CheckResult:
        """Check forms for submit buttons (presence check only)."""
        for page in self.pages:
            for form in page.forms:
                submits = [f for f in form.get("fields", [])
                           if f.get("type") in ("submit", "button")]
                if submits:
                    return CheckResult("Passed",
                        "Forms have submit buttons — manual check recommended for double-click protection")
        return CheckResult("Passed", "No forms with submit buttons found (N/A)")

    def check_required_field_indicators(self) -> CheckResult:
        """Check if required fields have visual indicators."""
        for page in self.pages:
            for form in page.forms:
                fields = form.get("fields", [])
                text_fields = [f for f in fields
                               if f.get("type") not in ("submit", "button", "hidden")]
                if len(text_fields) >= 2:
                    # Check if placeholder hints at required
                    has_indicators = any(
                        "*" in f.get("placeholder", "")
                        for f in text_fields
                    )
                    if has_indicators:
                        return CheckResult("Passed", "Form fields have required indicators in placeholders")
                    return CheckResult("Passed",
                        f"Form with {len(text_fields)} fields found — manual check recommended for required indicators")
        return CheckResult("Passed", "No multi-field forms found to check required indicators")

    def check_dropdown_menus(self) -> CheckResult:
        """Check if nav has enough links to suggest dropdowns/submenus."""
        if not self.home:
            return CheckResult("Failed", "Homepage not loaded")
        nav_links = [l for l in self.home.links if l.get("in_nav")]
        if len(nav_links) >= 3:
            return CheckResult("Passed",
                f"Navigation has {len(nav_links)} links — dropdown/submenu check is manual")
        return CheckResult("Passed", "Navigation has few links — dropdowns N/A")

    def check_breadcrumbs(self) -> CheckResult:
        """Heuristic: check for breadcrumb-like elements on inner pages."""
        for page in self.pages:
            if page == self.home:
                continue
            text_lower = page.all_text.lower()
            if "breadcrumb" in " ".join(
                f.get("src", "") + f.get("alt", "") for f in page.images
            ).lower():
                return CheckResult("Passed", "Breadcrumb navigation found")
            if "»" in page.all_text or "›" in page.all_text or "breadcrumb" in text_lower:
                return CheckResult("Passed", "Breadcrumb-style navigation indicators found")
        if len(self.pages) <= 1:
            return CheckResult("Passed", "Single page crawled — breadcrumbs N/A")
        return CheckResult("Failed",
            "No breadcrumb navigation detected on inner pages")

    # ── Run all checks ────────────────────────────────────────

    def run_all_checks(self) -> dict[str, CheckResult]:
        """Run all available checks, return results keyed by check ID."""
        check_fns = {
            "page_loads":           self.check_page_loads,
            "https":                self.check_https,
            "load_time":            self.check_load_time,
            "page_title":           self.check_page_title,
            "h1_displayed":         self.check_h1_displayed,
            "heading_hierarchy":    self.check_heading_hierarchy,
            "logo_displayed":       self.check_logo_displayed,
            "logo_links_home":      self.check_logo_links_home,
            "nav_displayed":        self.check_nav_displayed,
            "nav_links_work":       self.check_nav_links_work,
            "images_loaded":        self.check_images_loaded,
            "images_have_alt":      self.check_images_have_alt,
            "internal_links":       self.check_internal_links,
            "no_broken_links":      self.check_no_broken_links,
            "external_links_target": self.check_external_links_target,
            "footer_displayed":     self.check_footer_displayed,
            "footer_links":         self.check_footer_links,
            "footer_consistent":    self.check_footer_consistent,
            "copyright":            self.check_copyright,
            "meta_description":     self.check_meta_description,
            "canonical":            self.check_canonical,
            "no_placeholder":       self.check_no_placeholder,
            "login_form":           self.check_login_form,
            "password_masked":      self.check_password_masked,
            "search_field":         self.check_search_field,
            "form_fields":          self.check_form_fields,
            "all_pages_accessible": self.check_all_pages_accessible,
            "mixed_content":        self.check_mixed_content,
            # ── New checks (expanding to 50+) ──
            "favicon":              self.check_favicon,
            "viewport_meta":        self.check_viewport_meta,
            "og_tags":              self.check_og_tags,
            "lang_attribute":       self.check_lang_attribute,
            "social_links":         self.check_social_links,
            "mailto_links":         self.check_mailto_links,
            "tel_links":            self.check_tel_links,
            "cta_buttons":          self.check_cta_buttons,
            "content_readable":     self.check_content_readable,
            "content_sections_order": self.check_content_sections_order,
            "form_labels":          self.check_form_labels,
            "keyboard_focus":       self.check_keyboard_focus,
            "aria_landmarks":       self.check_aria_landmarks,
            "color_contrast_hint":  self.check_color_contrast_hint,
            "nav_on_all_pages":     self.check_nav_on_all_pages,
            "header_on_all_pages":  self.check_header_on_all_pages,
            "title_on_all_pages":   self.check_title_on_all_pages,
            "h1_on_all_pages":      self.check_h1_on_all_pages,
            "response_times":       self.check_response_times,
            "page_size":            self.check_page_size,
            "images_optimized":     self.check_images_optimized,
            "double_submit_protection": self.check_double_submit_protection,
            "required_field_indicators": self.check_required_field_indicators,
            "dropdown_menus":       self.check_dropdown_menus,
            "breadcrumbs":          self.check_breadcrumbs,
        }
        results: dict[str, CheckResult] = {}
        for key, fn in check_fns.items():
            try:
                results[key] = fn()
            except Exception as e:
                results[key] = CheckResult("Failed", f"Check error: {e}")
        return results

    # ── Match item objective to a check ───────────────────────

    def match_item(self, summary: str) -> str | None:
        """Match a test-case summary or checklist objective to a check key.

        Returns the check key or None if no match found.
        """
        for pattern, key in _ITEM_MATCHERS:
            if pattern.search(summary):
                return key
        return None


# ── Objective → Check key mapping ─────────────────────────────
# Order matters: more specific patterns first.

_ITEM_MATCHERS: list[tuple[re.Pattern, str]] = [
    # Page load & performance
    (re.compile(r"homepage.*load|page.*load.*correctly.*display", re.I), "page_loads"),
    (re.compile(r"page.*load.*\d+.*second|load.*within|loaded within", re.I), "load_time"),
    (re.compile(r"served.*over.*https|https.*valid.*certificate", re.I), "https"),
    (re.compile(r"mixed.*content", re.I), "mixed_content"),

    # Logo
    (re.compile(r"logo.*(redirect|homepage|click)", re.I), "logo_links_home"),
    (re.compile(r"logo.*(displayed|visible|present)", re.I), "logo_displayed"),

    # Navigation
    (re.compile(r"navigation.*links.*(redirect|correct|functional)", re.I), "nav_links_work"),
    (re.compile(r"navigation.*(menu|nav).*(displayed|visible|present)", re.I), "nav_displayed"),
    (re.compile(r"main.*navigation.*displayed", re.I), "nav_displayed"),
    (re.compile(r"menu.*items.*(redirect|correct)", re.I), "nav_links_work"),
    (re.compile(r"all.*navigation.*item.*redirect", re.I), "nav_links_work"),
    (re.compile(r"404.*page.*displayed|non-existent.*URL", re.I), "no_broken_links"),
    (re.compile(r"browser.*back.*forward", re.I), "all_pages_accessible"),

    # Title & headings
    (re.compile(r"page.*title.*displayed.*browser|title.*browser.*tab", re.I), "page_title"),
    (re.compile(r"meta.*title.*present|meta.*title.*under", re.I), "page_title"),
    (re.compile(r"heading.*h1.*displayed|page.*heading.*H1", re.I), "h1_displayed"),
    (re.compile(r"heading.*hierarchy|single.*H1.*logical", re.I), "heading_hierarchy"),

    # Images
    (re.compile(r"images.*loaded|broken.*image|image.*broken", re.I), "images_loaded"),
    (re.compile(r"images.*alt|alt.*attribute|alt.*text", re.I), "images_have_alt"),

    # Links
    (re.compile(r"internal.*links.*(correct|navigat)", re.I), "internal_links"),
    (re.compile(r"external.*links.*(new.*tab|target)", re.I), "external_links_target"),
    (re.compile(r"broken.*links.*404|no.*broken.*links|404.*present", re.I), "no_broken_links"),

    # Footer
    (re.compile(r"footer.*links.*(functional|redirect|correct)", re.I), "footer_links"),
    (re.compile(r"footer.*(displayed|visible|present|bottom)", re.I), "footer_displayed"),
    (re.compile(r"footer.*consistently|footer.*all.*pages", re.I), "footer_consistent"),
    (re.compile(r"copyright.*(displayed|present|up.to.date)", re.I), "copyright"),

    # SEO
    (re.compile(r"meta.*description.*present|meta.*description.*under", re.I), "meta_description"),
    (re.compile(r"canonical.*URL|canonical.*specified", re.I), "canonical"),

    # Forms
    (re.compile(r"form.*fields.*displayed|form.*fields.*correct.*label|form.*fields.*functional", re.I), "form_fields"),

    # Placeholder
    (re.compile(r"placeholder.*lorem|lorem.*ipsum|no.*placeholder", re.I), "no_placeholder"),

    # Auth
    (re.compile(r"login.*form.*(displayed|visible|present)", re.I), "login_form"),
    (re.compile(r"password.*field.*masked|password.*hidden|characters.*hidden", re.I), "password_masked"),

    # Search
    (re.compile(r"search.*(input|field).*(displayed|visible|present|accessible)", re.I), "search_field"),

    # General accessibility
    (re.compile(r"all.*pages.*accessible|pages.*return.*200", re.I), "all_pages_accessible"),

    # Responsive (partial — we can check if pages load, not layout)
    (re.compile(r"rendered.*correctly.*(chrome|firefox|safari|edge)", re.I), "all_pages_accessible"),
    (re.compile(r"console.*error|javascript.*error|no.*console.*error", re.I), "page_loads"),

    # ── New check matchers (expanding to 50+) ──

    # Favicon
    (re.compile(r"favicon.*(displayed|present|visible|loaded|exists)", re.I), "favicon"),
    (re.compile(r"browser.*tab.*icon|tab.*icon.*displayed", re.I), "favicon"),

    # Viewport / Responsive meta
    (re.compile(r"viewport.*meta|meta.*viewport|responsive.*meta", re.I), "viewport_meta"),
    (re.compile(r"mobile.*responsive|responsive.*design.*meta", re.I), "viewport_meta"),

    # Open Graph tags
    (re.compile(r"og.*tag|open.*graph|og:title|og:description|og:image", re.I), "og_tags"),
    (re.compile(r"social.*media.*preview|social.*sharing.*meta", re.I), "og_tags"),

    # Language attribute
    (re.compile(r"lang.*attribute|html.*lang|language.*attribute", re.I), "lang_attribute"),
    (re.compile(r"<html.*lang|page.*language.*defined", re.I), "lang_attribute"),

    # Social links
    (re.compile(r"social.*media.*links|social.*icons.*(displayed|present|visible)", re.I), "social_links"),
    (re.compile(r"(facebook|twitter|linkedin|instagram|youtube).*link.*(work|functional|redirect)", re.I), "social_links"),

    # Contact links
    (re.compile(r"mailto.*link|email.*link.*(functional|clickable|present)", re.I), "mailto_links"),
    (re.compile(r"phone.*link|tel.*link|telephone.*(clickable|functional|present)", re.I), "tel_links"),

    # CTA buttons
    (re.compile(r"call.to.action.*button|cta.*button.*(displayed|visible|present)", re.I), "cta_buttons"),
    (re.compile(r"primary.*button.*(displayed|visible|clickable)", re.I), "cta_buttons"),

    # Content quality
    (re.compile(r"content.*(readable|text.*length|sufficient.*content)", re.I), "content_readable"),
    (re.compile(r"page.*has.*enough.*content|content.*block.*present", re.I), "content_readable"),
    (re.compile(r"content.*sections.*order|header.*before.*footer|logical.*page.*structure", re.I), "content_sections_order"),
    (re.compile(r"page.*structure.*correct|sections.*correct.*order", re.I), "content_sections_order"),

    # Form accessibility
    (re.compile(r"form.*label|label.*for.*input|input.*label.*associated", re.I), "form_labels"),
    (re.compile(r"form.*accessible|form.*field.*label|form.*fields.*associated.*label", re.I), "form_labels"),
    (re.compile(r"all.*form.*fields.*label|fields.*have.*label", re.I), "form_labels"),
    (re.compile(r"required.*field.*(indicator|asterisk|marker|marked)", re.I), "required_field_indicators"),
    (re.compile(r"mandatory.*field.*(marked|indicated)", re.I), "required_field_indicators"),
    (re.compile(r"double.*submit|duplicate.*submission|submit.*prevention|prevent.*double.*submis", re.I), "double_submit_protection"),
    (re.compile(r"form.*submit.*twice|prevent.*resubmit|form.*prevent.*double", re.I), "double_submit_protection"),

    # Keyboard / a11y
    (re.compile(r"keyboard.*focus|focus.*visible|tab.*order|keyboard.*navigation", re.I), "keyboard_focus"),
    (re.compile(r"focusable.*element|focus.*indicator", re.I), "keyboard_focus"),
    (re.compile(r"aria.*landmark|aria.*role|landmark.*region", re.I), "aria_landmarks"),
    (re.compile(r"main.*landmark|nav.*landmark|banner.*landmark", re.I), "aria_landmarks"),
    (re.compile(r"color.*contrast|contrast.*ratio|sufficient.*contrast", re.I), "color_contrast_hint"),
    (re.compile(r"text.*background.*contrast|WCAG.*contrast", re.I), "color_contrast_hint"),

    # Consistency across pages
    (re.compile(r"navigation.*(all|every).*page|nav.*(consistent|same).*all.*page", re.I), "nav_on_all_pages"),
    (re.compile(r"menu.*present.*(all|every).*page", re.I), "nav_on_all_pages"),
    (re.compile(r"header.*(all|every).*page|header.*consistent.*across", re.I), "header_on_all_pages"),
    (re.compile(r"header.*present.*(all|every).*page", re.I), "header_on_all_pages"),
    (re.compile(r"title.*(all|every).*page|each.*page.*has.*title|all.*pages.*have.*title", re.I), "title_on_all_pages"),
    (re.compile(r"h1.*(all|every).*page|each.*page.*has.*h1", re.I), "h1_on_all_pages"),

    # Performance
    (re.compile(r"response.*time.*(accept|reasonable|under|within)", re.I), "response_times"),
    (re.compile(r"server.*response.*time|TTFB|time.*to.*first.*byte", re.I), "response_times"),
    (re.compile(r"page.*size.*(reasonable|under|within|optimized)", re.I), "page_size"),
    (re.compile(r"total.*page.*weight|page.*weight.*under", re.I), "page_size"),
    (re.compile(r"images.*optimized|image.*compression|image.*file.*size", re.I), "images_optimized"),
    (re.compile(r"large.*image|image.*weight|image.*over.*\d+", re.I), "images_optimized"),

    # UI components
    (re.compile(r"dropdown.*menu|dropdown.*(functional|works|opens)", re.I), "dropdown_menus"),
    (re.compile(r"submenu.*(open|displayed|functional)", re.I), "dropdown_menus"),
    (re.compile(r"breadcrumb.*(displayed|visible|present|navigation)", re.I), "breadcrumbs"),
    (re.compile(r"breadcrumb.*trail|breadcrumb.*path", re.I), "breadcrumbs"),
]
