"""
TestFortge — Site Crawler / Analyzer

Fetches a website URL, discovers pages, extracts forms, navigation,
and structural elements. Returns a SiteAnalysis that the QA persona
uses to generate comprehensive test cases and checklists.

Designed for QA testing — not a full spider. Crawls up to MAX_PAGES
internal links from the landing page.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import ssl
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from engine.log import get_logger

_logger = get_logger(__name__)


# ── Limits ────────────────────────────────────────────────────────

MAX_PAGES = 15          # max pages to fetch
FETCH_TIMEOUT = 8       # seconds per request
MAX_BODY_KB = 512       # truncate response body

# Only these schemes are ever fetched — ``file://`` / ``gopher://`` and
# friends would bypass our SSRF guard, so they're rejected up-front.
_ALLOWED_SCHEMES = {"http", "https"}


# ── SSRF guard ───────────────────────────────────────────────────

class UnsafeURLError(ValueError):
    """Raised when a URL targets a non-public host / forbidden scheme."""


def _is_public_ip(ip_str: str) -> bool:
    """Return True if ``ip_str`` is a routable, non-internal address.

    Guards against SSRF: we refuse RFC1918 private ranges, loopback,
    link-local (incl. the AWS / GCP metadata endpoint 169.254.169.254),
    multicast, reserved, and unspecified addresses.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _assert_safe_url(url: str) -> None:
    """Raise :class:`UnsafeURLError` if ``url`` points at a private host.

    Resolves every A/AAAA record for the hostname; if *any* record is
    internal we refuse, so an attacker can't bypass us with a DNS record
    that mixes public + private answers.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme not allowed: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no host")

    # Reject numeric literals that are already internal — skip DNS.
    try:
        ipaddress.ip_address(host)
        if not _is_public_ip(host):
            raise UnsafeURLError(f"non-public IP: {host}")
        return
    except ValueError:
        pass  # it's a hostname; resolve

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise UnsafeURLError(f"DNS failure for {host}: {e}") from e
    for info in infos:
        ip_str = info[4][0]
        if not _is_public_ip(ip_str):
            raise UnsafeURLError(
                f"host {host} resolves to non-public address {ip_str}")


# ── Data structures ──────────────────────────────────────────────

@dataclass
class PageInfo:
    """Extracted info from a single page."""
    url: str
    title: str = ""
    h1: str = ""
    forms: list[dict] = field(default_factory=list)      # {action, method, fields:[]}
    nav_links: list[str] = field(default_factory=list)    # text of nav links
    buttons: list[str] = field(default_factory=list)      # button texts
    images_count: int = 0
    has_video: bool = False
    meta_description: str = ""
    headings: list[str] = field(default_factory=list)     # h2/h3 texts
    links_internal: list[str] = field(default_factory=list)
    links_external: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class SiteAnalysis:
    """Aggregated analysis of a crawled website."""
    base_url: str
    domain: str
    pages: list[PageInfo] = field(default_factory=list)
    all_page_urls: list[str] = field(default_factory=list)
    nav_items: list[str] = field(default_factory=list)
    forms_found: list[dict] = field(default_factory=list)
    features_detected: list[str] = field(default_factory=list)
    has_auth: bool = False
    has_search: bool = False
    has_forms: bool = False
    has_payment: bool = False
    has_footer: bool = True
    page_count: int = 0
    crawl_errors: list[str] = field(default_factory=list)
    # Architecture detection — used by qa_estimator to scale TC budget
    # per site type. One of: "wordpress", "spa", "ecommerce", "dashboard",
    # "landing", "static", "generic".
    site_type: str = "generic"
    architecture_notes: list[str] = field(default_factory=list)


# ── HTML Parser ──────────────────────────────────────────────────

class _PageParser(HTMLParser):
    """Extract structural info from an HTML page."""

    def __init__(self):
        super().__init__()
        self.title = ""
        self.h1 = ""
        self.headings: list[str] = []
        self.forms: list[dict] = []
        self.nav_links: list[str] = []
        self.buttons: list[str] = []
        self.links: list[str] = []
        self.images_count = 0
        self.has_video = False
        self.meta_description = ""

        self._in_title = False
        self._in_h1 = False
        self._in_heading = False
        self._in_nav = False
        self._in_a = False
        self._in_button = False
        self._current_form: dict | None = None
        self._current_text = ""
        self._heading_text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple]):
        attr_dict = dict(attrs)
        tag_lower = tag.lower()

        if tag_lower == "title":
            self._in_title = True
            self._current_text = ""
        elif tag_lower == "h1" and not self.h1:
            self._in_h1 = True
            self._current_text = ""
        elif tag_lower in ("h2", "h3"):
            self._in_heading = True
            self._heading_text = ""
        elif tag_lower == "nav":
            self._in_nav = True
        elif tag_lower == "a":
            href = attr_dict.get("href", "")
            if href and not href.startswith(("#", "javascript:")):
                self.links.append(href)
            self._in_a = True
            self._current_text = ""
        elif tag_lower == "button":
            self._in_button = True
            self._current_text = ""
        elif tag_lower == "input":
            btn_type = attr_dict.get("type", "").lower()
            if btn_type in ("submit", "button"):
                val = attr_dict.get("value", "")
                if val:
                    self.buttons.append(val)
            if self._current_form is not None:
                self._current_form["fields"].append({
                    "name": attr_dict.get("name", ""),
                    "type": attr_dict.get("type", "text"),
                    "placeholder": attr_dict.get("placeholder", ""),
                })
        elif tag_lower == "textarea":
            if self._current_form is not None:
                self._current_form["fields"].append({
                    "name": attr_dict.get("name", ""),
                    "type": "textarea",
                    "placeholder": attr_dict.get("placeholder", ""),
                })
        elif tag_lower == "select":
            if self._current_form is not None:
                self._current_form["fields"].append({
                    "name": attr_dict.get("name", ""),
                    "type": "select",
                    "placeholder": "",
                })
        elif tag_lower == "form":
            self._current_form = {
                "action": attr_dict.get("action", ""),
                "method": attr_dict.get("method", "GET").upper(),
                "fields": [],
            }
        elif tag_lower == "img":
            self.images_count += 1
        elif tag_lower == "video":
            self.has_video = True
        elif tag_lower == "meta":
            name = attr_dict.get("name", "").lower()
            if name == "description":
                self.meta_description = attr_dict.get("content", "")

    def handle_endtag(self, tag: str):
        tag_lower = tag.lower()
        if tag_lower == "title":
            self._in_title = False
            self.title = self._current_text.strip()
        elif tag_lower == "h1":
            if self._in_h1:
                self.h1 = self._current_text.strip()
            self._in_h1 = False
        elif tag_lower in ("h2", "h3"):
            if self._in_heading and self._heading_text.strip():
                self.headings.append(self._heading_text.strip())
            self._in_heading = False
        elif tag_lower == "nav":
            self._in_nav = False
        elif tag_lower == "a":
            if self._in_nav and self._current_text.strip():
                self.nav_links.append(self._current_text.strip())
            self._in_a = False
        elif tag_lower == "button":
            if self._current_text.strip():
                self.buttons.append(self._current_text.strip())
            self._in_button = False
        elif tag_lower == "form":
            if self._current_form is not None:
                self.forms.append(self._current_form)
                self._current_form = None

    def handle_data(self, data: str):
        if self._in_title or self._in_h1 or self._in_a or self._in_button:
            self._current_text += data
        if self._in_heading:
            self._heading_text += data


# ── Fetcher ──────────────────────────────────────────────────────

def _fetch_page(url: str) -> tuple[str, str]:
    """Fetch a URL and return (html_content, error).

    Returns ("", error_message) on failure.

    Refuses non-HTTP(S) schemes and any host that resolves to a private /
    loopback / link-local IP — this kills SSRF attempts targeting cloud
    metadata endpoints or internal services.
    """
    try:
        _assert_safe_url(url)
    except UnsafeURLError as e:
        return "", f"blocked: {e}"

    try:
        # Verify TLS certificates. Previously this code disabled
        # verification globally which made the crawler a TLS-downgrade
        # primitive; strict verification is the secure default.
        ctx = ssl.create_default_context()

        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TestfortForge/1.0",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT, context=ctx) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" not in content_type and "application/xhtml" not in content_type:
                return "", f"Not HTML: {content_type}"
            raw = resp.read(MAX_BODY_KB * 1024)
            # Try to decode
            charset = "utf-8"
            ct = content_type.lower()
            if "charset=" in ct:
                charset = ct.split("charset=")[-1].split(";")[0].strip()
            try:
                html = raw.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                html = raw.decode("utf-8", errors="replace")
            return html, ""
    except urllib.error.HTTPError as e:
        _logger.info("fetch failed: HTTP %s for %s", e.code, url)
        return "", f"HTTP {e.code}"
    except urllib.error.URLError as e:
        _logger.warning("fetch failed: URL error %s for %s", e.reason, url)
        return "", f"URL error: {e.reason}"
    except Exception as e:
        _logger.warning("fetch failed: %s for %s", e, url)
        return "", str(e)[:100]


def _parse_page(html: str, page_url: str, base_domain: str) -> PageInfo:
    """Parse an HTML page into PageInfo."""
    parser = _PageParser()
    try:
        parser.feed(html)
    except Exception as exc:  # malformed HTML is common — keep going with what we parsed
        _logger.debug("HTML parse partial failure for %s: %s", page_url, exc)

    page = PageInfo(url=page_url)
    page.title = parser.title
    page.h1 = parser.h1
    page.headings = parser.headings[:30]
    page.forms = parser.forms
    page.nav_links = parser.nav_links
    page.buttons = parser.buttons
    page.images_count = parser.images_count
    page.has_video = parser.has_video
    page.meta_description = parser.meta_description

    # Categorize links
    for href in parser.links:
        full = urljoin(page_url, href)
        parsed = urlparse(full)
        if parsed.netloc == base_domain or not parsed.netloc:
            if full not in page.links_internal:
                page.links_internal.append(full)
        else:
            if full not in page.links_external:
                page.links_external.append(full)

    return page


# ── Feature Detection ────────────────────────────────────────────

_AUTH_KEYWORDS = re.compile(
    r"(log\s*in|sign\s*in|sign\s*up|register|forgot\s*password|create\s*account)",
    re.IGNORECASE,
)
_SEARCH_KEYWORDS = re.compile(
    r"(search|find|look\s*up|magnifying|searchbar)",
    re.IGNORECASE,
)
# Payment detection — only words that indicate actual transactional
# e-commerce functionality (cart, checkout, buy).  Informational words
# like "price" / "pricing" are deliberately excluded because they
# commonly appear on non-e-commerce sites (SaaS pricing pages,
# service pricing models, etc.) and cause false positives.
_PAYMENT_KEYWORDS = re.compile(
    r"(cart|shopping.cart|checkout|buy\s+now|purchase|add\s+to\s+cart|subscribe|order\s+now|pay\s+now)",
    re.IGNORECASE,
)


def _detect_features(analysis: SiteAnalysis, all_html: str):
    """Detect site features from aggregated HTML content."""
    text = all_html.lower()

    # Auth
    for page in analysis.pages:
        all_text = " ".join([page.title, page.h1] + page.nav_links + page.buttons +
                            [f.get("action", "") for f in page.forms])
        if _AUTH_KEYWORDS.search(all_text):
            analysis.has_auth = True
            break

    # Search
    for page in analysis.pages:
        for form in page.forms:
            for f in form.get("fields", []):
                if f.get("type") == "search" or "search" in f.get("name", "").lower():
                    analysis.has_search = True
                    break
            if analysis.has_search:
                break
        all_text = " ".join(page.buttons + page.nav_links)
        if _SEARCH_KEYWORDS.search(all_text):
            analysis.has_search = True
        if analysis.has_search:
            break

    # Forms — distinguish "business" forms (auth, signup, contact,
    # checkout) from utility forms (site search, single-field newsletter
    # opt-in). Generic Forms baseline TCs only make sense for the
    # former. A news/info site (e.g. football.ua) typically exposes
    # only a search box + a 1-field newsletter — those should NOT
    # trigger the generic "Forms" suite that talks about email
    # validation and required fields the site doesn't actually have.
    _BUSINESS_FIELD_NAMES = {
        "password", "passwd", "pwd", "pass",
        "email", "e-mail", "mail",
        "name", "firstname", "first_name", "lastname", "last_name", "fullname", "full_name",
        "phone", "tel", "mobile", "address", "city", "country", "zip", "postal",
        "company", "organization", "subject", "message", "comment", "comments",
        "card", "cardnumber", "card_number", "cvv", "expiry",
        "username", "user", "login", "account",
    }
    for page in analysis.pages:
        for form in page.forms:
            real_fields = [f for f in form.get("fields", [])
                           if f.get("type") not in ("hidden", "submit", "button")]
            if not real_fields:
                continue
            # An auth form (any password field) is always a business form.
            has_password = any(f.get("type") == "password" for f in real_fields)
            # A search-only form ⇒ skip. Catches single-field site search
            # even when the input has type="text" with name="q"/"query".
            if len(real_fields) == 1:
                only = real_fields[0]
                only_type = only.get("type", "")
                only_name = (only.get("name") or "").lower()
                if only_type == "search" or any(t in only_name for t in ("search", "query", " q ", "_q", "q_", "find")):
                    continue
                # 1-field newsletter (just email) — too thin to drive
                # a meaningful Forms TC suite.
                if not has_password:
                    continue
            # Multi-field form: count business-named fields. Require
            # ≥2 named business fields OR the presence of a password
            # field. This filters out 2-field utility forms whose names
            # don't match anything we recognise (random search filters
            # with two inputs, comment widgets that just take name+text
            # without an explicit "email"/"comment" naming).
            named = {(f.get("name") or "").lower() for f in real_fields}
            business_match = sum(1 for n in named if any(b in n for b in _BUSINESS_FIELD_NAMES))
            is_business_form = has_password or business_match >= 2
            if not is_business_form:
                continue
            analysis.has_forms = True
            analysis.forms_found.append({
                "page": page.url,
                "action": form.get("action", ""),
                "fields": real_fields,
                "is_auth": has_password,
            })
        if analysis.has_forms:
            break

    # Payment — only check structural elements (nav, buttons, forms),
    # NOT the full HTML body, to avoid false positives from words like
    # "price" or "pricing" in informational content.
    for page in analysis.pages:
        all_text = " ".join([page.title, page.h1] + page.nav_links + page.buttons +
                            [f.get("action", "") for f in page.forms])
        if _PAYMENT_KEYWORDS.search(all_text):
            analysis.has_payment = True
            break

    # Build features list
    analysis.features_detected = ["web_general"]
    if analysis.has_auth:
        analysis.features_detected.append("auth")
    if analysis.has_search:
        analysis.features_detected.append("search")
    if analysis.has_forms:
        analysis.features_detected.append("forms")
    if analysis.has_payment:
        analysis.features_detected.append("payment")

    # Architecture detection — used by the estimator to scale TC budgets
    _detect_architecture(analysis, text)


# ── Architecture detection ───────────────────────────────────────

_WP_SIGNALS = re.compile(
    r"(/wp-content/|/wp-includes/|/wp-json/|/wp-admin/|"
    r"<meta[^>]+name=['\"]generator['\"][^>]+content=['\"]wordpress)",
    re.IGNORECASE,
)
_SPA_SIGNALS = re.compile(
    r"(__next_data__|data-reactroot|ng-app\b|ng-version\b|v-app-root|"
    r"id=['\"]?app['\"]?[^>]*>\s*</div>|id=['\"]?root['\"]?[^>]*>\s*</div>|"
    r"/_nuxt/|/assets/index-[a-z0-9]{6,}\.js)",
    re.IGNORECASE,
)
_ECOM_SIGNALS = re.compile(
    r"(shopify|woocommerce|magento|bigcommerce|/cart|/checkout|add-to-cart|"
    r"data-product-id|product[-_]?sku)",
    re.IGNORECASE,
)
_DASHBOARD_KEYWORDS = re.compile(
    r"(dashboard|admin\s*panel|analytics|reports|metrics|kpi|"
    r"user\s*management|role\s*management)",
    re.IGNORECASE,
)


def _detect_architecture(analysis: SiteAnalysis, html_text: str) -> None:
    """Populate `site_type` + `architecture_notes` on the analysis.

    Heuristics score each candidate on signals from the crawled HTML and
    the aggregated page metadata (forms, nav, buttons, links). The winning
    category drives the estimator's per-page TC budget.
    """
    notes: list[str] = []

    # WordPress — very reliable: content served from /wp-content/ etc.
    wp_match = bool(_WP_SIGNALS.search(html_text))
    if wp_match:
        notes.append("WordPress signals found (/wp-content/, /wp-json/ or generator meta)")

    # SPA — look for framework markers in the shell HTML
    spa_match = bool(_SPA_SIGNALS.search(html_text))
    if spa_match:
        notes.append("SPA signals found (React/Vue/Angular/Next/Nuxt shell)")

    # E-commerce — cart/checkout signals OR payment keywords already detected
    ecom_match = bool(_ECOM_SIGNALS.search(html_text)) or analysis.has_payment
    if ecom_match:
        notes.append("E-commerce signals found (cart/checkout/shop platform)")

    # Dashboard / admin SaaS
    nav_text = " ".join(analysis.nav_items).lower()
    title_text = " ".join(p.title.lower() + " " + p.h1.lower() for p in analysis.pages)
    dash_match = bool(_DASHBOARD_KEYWORDS.search(nav_text + " " + title_text))
    if dash_match:
        notes.append("Dashboard/admin signals found (analytics / reports / admin panel)")

    # Landing vs multi-page
    landing = len(analysis.pages) <= 2 and not (ecom_match or dash_match)

    # Rank candidates
    if ecom_match:
        site_type = "ecommerce"
    elif dash_match:
        site_type = "dashboard"
    elif spa_match and not wp_match:
        site_type = "spa"
    elif wp_match:
        site_type = "wordpress"
    elif landing:
        site_type = "landing"
    elif analysis.has_forms or analysis.has_auth:
        site_type = "app"
    else:
        site_type = "static"

    analysis.site_type = site_type
    analysis.architecture_notes = notes or [f"No strong architecture signals — defaulting to '{site_type}'"]


# ── Main Crawl Function ─────────────────────────────────────────

def crawl_site(url: str) -> SiteAnalysis:
    """Crawl a website starting from *url* and return a SiteAnalysis.

    Fetches up to MAX_PAGES internal pages. Safe to call on any URL —
    returns partial results on errors.
    """
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)

    base_domain = parsed.netloc
    analysis = SiteAnalysis(base_url=url, domain=base_domain)

    visited: set[str] = set()
    queue: list[str] = [url]
    all_html_parts: list[str] = []

    while queue and len(visited) < MAX_PAGES:
        current_url = queue.pop(0)

        # Normalize
        norm = urlparse(current_url)
        clean_url = f"{norm.scheme}://{norm.netloc}{norm.path}".rstrip("/")
        if clean_url in visited:
            continue
        visited.add(clean_url)

        # Skip non-page URLs
        path_lower = norm.path.lower()
        skip_ext = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
                     ".css", ".js", ".zip", ".mp4", ".webm", ".ico", ".woff",
                     ".woff2", ".ttf", ".eot", ".xml", ".json")
        if any(path_lower.endswith(ext) for ext in skip_ext):
            continue

        html, error = _fetch_page(current_url)
        if error:
            analysis.crawl_errors.append(f"{current_url}: {error}")
            continue

        all_html_parts.append(html)
        page = _parse_page(html, current_url, base_domain)
        analysis.pages.append(page)
        analysis.all_page_urls.append(current_url)

        # Enqueue internal links
        for link in page.links_internal:
            link_parsed = urlparse(link)
            link_clean = f"{link_parsed.scheme}://{link_parsed.netloc}{link_parsed.path}".rstrip("/")
            if link_clean not in visited and link_parsed.netloc == base_domain:
                queue.append(link)

    # Collect nav items from first page
    if analysis.pages:
        analysis.nav_items = analysis.pages[0].nav_links

    analysis.page_count = len(analysis.pages)

    # Detect features from all collected HTML
    all_html = " ".join(all_html_parts)
    _detect_features(analysis, all_html)

    return analysis
