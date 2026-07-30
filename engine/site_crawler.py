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

# 2026-05-04 — operator reported testfort.com only got 10 pages crawled
# and ~66 TCs total, "too shallow even for a low-level checklist".
# Bumped 15 → 50 so a typical marketing/WordPress site (which usually
# has 20-40 internal pages) is fully covered. The per-page cap in
# engine/qa_estimator.features_from_site_analysis was bumped in
# parallel so the richer crawl actually surfaces as features.
MAX_PAGES = 50          # max pages to fetch
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

    Delegates to :func:`engine.security.require_safe_url` which is the
    single source of truth for SSRF policy — same DNS-rebinding-resistant
    check, plus honors the ``SSRF_ALLOWLIST_BYPASS=1`` env opt-out.
    Re-raises the central :class:`engine.security.UnsafeUrlError` as the
    local :class:`UnsafeURLError` for backward compatibility with the
    caller's existing ``except UnsafeURLError`` handler.
    """
    from engine.security import require_safe_url, UnsafeUrlError as _UU
    try:
        require_safe_url(url)
    except _UU as exc:
        raise UnsafeURLError(str(exc)) from exc


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
        # Constraint + labelling capture (see _field docstring).
        self._current_select: dict | None = None
        self._pending_option: str | None = None
        self._option_text = ""
        self._label_for: str | None = None
        self._label_text: str | None = None
        # A <label> that wraps its control ("<label>Size <select>…") must
        # stop collecting text once the control opens, or the option
        # labels end up glued onto the field name.
        self._label_closed = False
        self._labels_by_for: dict[str, str] = {}

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
                    if self._current_form is not None and not self._current_form.get("submit_text"):
                        self._current_form["submit_text"] = val
            if self._current_form is not None:
                self._current_form["fields"].append(
                    self._field(attr_dict, attr_dict.get("type", "text")))
                self._close_label_text()
        elif tag_lower == "textarea":
            if self._current_form is not None:
                self._current_form["fields"].append(
                    self._field(attr_dict, "textarea"))
                self._close_label_text()
        elif tag_lower == "select":
            if self._current_form is not None:
                self._close_label_text()
                fld = self._field(attr_dict, "select")
                fld["options"] = []
                self._current_form["fields"].append(fld)
                self._current_select = fld
        elif tag_lower == "option":
            # Real option values let a generated step name the choice
            # instead of saying "select a value" — the difference between
            # a runnable case and a placeholder.
            if self._current_select is not None:
                self._pending_option = attr_dict.get("value", "")
                self._option_text = ""
        elif tag_lower == "label":
            # <label for="x"> is how a human names the control, so steps
            # can quote the visible label instead of the machine-readable
            # ``name`` attribute.
            self._label_for = attr_dict.get("for", "")
            self._label_text = ""
            self._label_closed = False
        elif tag_lower == "form":
            # Capture human context the form sits inside so qa_persona
            # can build readable TC names instead of falling back to
            # the URL action (which on WordPress / Contact Form 7 is
            # cryptic — '/contact#wpcf7-f14405-o1').
            self._current_form = {
                "action": attr_dict.get("action", ""),
                "method": attr_dict.get("method", "GET").upper(),
                "fields": [],
                "heading": (self.h1 or (self.headings[-1] if self.headings else "") or self.title),
                "submit_text": "",
            }
        elif tag_lower == "img":
            self.images_count += 1
        elif tag_lower == "video":
            self.has_video = True
        elif tag_lower == "meta":
            name = attr_dict.get("name", "").lower()
            if name == "description":
                self.meta_description = attr_dict.get("content", "")

    def _close_label_text(self) -> None:
        """Stop accumulating label text (a nested control just opened)."""
        if self._label_text is not None:
            self._label_closed = True

    @staticmethod
    def _field(attr_dict: dict, ftype: str) -> dict:
        """Normalise one form control, keeping its constraints intact.

        ``required`` / ``maxlength`` / ``min`` / ``max`` / ``pattern`` are
        what let a deterministic generator decide which cases are
        *justified*: a required-field negative is only honest when the
        markup actually says required, and a boundary case needs a real
        limit to sit on. This parser used to drop all of them, which left
        every consumer guessing — and guessing is how a suite fills up
        with cases that fail for the wrong reason.
        """
        out = {
            "name": attr_dict.get("name", ""),
            "type": (ftype or "text").lower(),
            "placeholder": attr_dict.get("placeholder", ""),
            "id": attr_dict.get("id", ""),
            "required": ("required" in attr_dict
                         or attr_dict.get("aria-required") == "true"),
        }
        for attr in ("maxlength", "minlength", "min", "max", "pattern", "step"):
            val = attr_dict.get(attr)
            if val not in (None, ""):
                out[attr] = val
        return out

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
            txt = self._current_text.strip()
            if txt:
                self.buttons.append(txt)
                # Attach as submit_text for the form we're currently
                # inside, if any — the parser feeds qa_persona's
                # _form_label which prefers this over the URL action.
                if self._current_form is not None and not self._current_form.get("submit_text"):
                    self._current_form["submit_text"] = txt
            self._in_button = False
        elif tag_lower == "option":
            if self._current_select is not None:
                shown = (self._option_text or "").strip() \
                    or (self._pending_option or "").strip()
                if shown and len(self._current_select["options"]) < 12:
                    self._current_select["options"].append(shown)
            self._pending_option = None
            self._option_text = ""
        elif tag_lower == "select":
            self._current_select = None
        elif tag_lower == "label":
            text = " ".join((self._label_text or "").split())
            if text:
                if self._label_for:
                    self._labels_by_for[self._label_for] = text[:80]
                elif (self._current_form is not None
                        and self._current_form["fields"]):
                    # <label>Name <input></label> nesting: attribute it to
                    # the control it wraps.
                    last = self._current_form["fields"][-1]
                    last.setdefault("label", text[:80])
            self._label_for = None
            self._label_text = None
            self._label_closed = False
        elif tag_lower == "form":
            if self._current_form is not None:
                # A <label for=...> may appear before its control, so run
                # the for -> id match once the whole form has been seen.
                for fld in self._current_form["fields"]:
                    if fld.get("label"):
                        continue
                    fid = fld.get("id")
                    if fid and fid in self._labels_by_for:
                        fld["label"] = self._labels_by_for[fid]
                self.forms.append(self._current_form)
                self._current_form = None
                self._current_select = None

    def handle_data(self, data: str):
        if self._in_title or self._in_h1 or self._in_a or self._in_button:
            self._current_text += data
        if self._in_heading:
            self._heading_text += data
        if self._pending_option is not None:
            self._option_text += data
        if self._label_text is not None and not self._label_closed:
            self._label_text += data


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

# Per-process TTL cache. A single /test-cases or /checklist POST today
# triggers two crawls of the same URL — once via qa_persona's
# rule-based area-detector and once via the Stage-2 site-aware
# pipeline. Both went over the wire, doubling latency and pushing
# the synchronous worker past its deadline on real sites. A small
# in-memory cache keyed by URL collapses these to one HTTP fetch
# without changing the caller signatures.
#
# 5-minute TTL matches the operator-facing "click Generate twice in
# a row" loop without ever serving genuinely stale data on a longer
# regenerate. Cache lives only inside one gunicorn worker — that's
# acceptable on Render free tier where we run with a single worker.
import time as _time
_CRAWL_CACHE: dict[str, tuple[float, "SiteAnalysis"]] = {}
_CRAWL_CACHE_TTL = 300.0


def crawl_site(url: str) -> SiteAnalysis:
    """Crawl a website starting from *url* and return a SiteAnalysis.

    Fetches up to MAX_PAGES internal pages. Safe to call on any URL —
    returns partial results on errors. Results are memoised per-URL
    for ``_CRAWL_CACHE_TTL`` seconds so back-to-back callers on the
    same target (legacy ``qa_persona`` + Stage-2 ``site_recon``) only
    pay one network round-trip.
    """
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)

    cache_key = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    now = _time.time()
    hit = _CRAWL_CACHE.get(cache_key)
    if hit and hit[0] > now:
        return hit[1]

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

    # Memoise — see _CRAWL_CACHE comment above for TTL rationale.
    _CRAWL_CACHE[cache_key] = (now + _CRAWL_CACHE_TTL, analysis)
    return analysis
