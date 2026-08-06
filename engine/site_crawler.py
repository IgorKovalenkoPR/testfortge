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

from engine import security as _security
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
MAX_BODY_KB = 512       # truncate the DECODED document
# Wire-bytes cap, applied before decompression. Deliberately smaller than
# MAX_BODY_KB is large: gzipped markup runs 5-10x, so 256 KB of wire data
# already exceeds the decoded cap on any real page, and a decompression
# bomb cannot spend more than this much of our bandwidth.
MAX_RAW_KB = 256
# Ceiling on the decompressed size, so a small compressed payload cannot
# expand without bound. 8 MB is ~16x the decoded cap — generous enough
# that no legitimate page trips it.
MAX_DECOMPRESSED_KB = 8 * 1024

# Grid recognition. A page shell built out of <table> (old markup still
# does this) must not become a "grid" — sort / pagination / bulk-action
# cases generated against a layout table are exactly the invention
# house_style.yaml calls out. Hence the qualification rules in
# ``_is_grid`` and the caps below.
MAX_TABLES_PER_PAGE = 10
MAX_GRID_COLUMNS = 24

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
    # Data grids only — layout tables are dropped by _PageParser.
    tables: list[dict] = field(default_factory=list)
    # Page-level controls that surround a grid: pager, search, filters,
    # bulk actions, create control. See _PageParser.grid_controls.
    grid_controls: dict = field(default_factory=dict)
    nav_links: list[str] = field(default_factory=list)    # text of nav links
    buttons: list[str] = field(default_factory=list)      # button texts
    images_count: int = 0
    has_video: bool = False
    meta_description: str = ""
    headings: list[str] = field(default_factory=list)     # h2/h3 texts
    #: The same headings WITH their level: ``[{"level": 2, "text": …}]``.
    #: A low-level checklist nests an h3 under the h2 above it (reference
    #: rows 2.3 → 2.3.1), which a flattened list cannot express. ``headings``
    #: stays as-is for every existing consumer.
    heading_levels: list[dict] = field(default_factory=list)
    links_internal: list[str] = field(default_factory=list)
    links_external: list[str] = field(default_factory=list)
    # ── Page regions (PR-2) ─────────────────────────────────────────
    # A low-level checklist opens with a Header section and closes with a
    # Footer one — 7 and 12 checks respectively in the reference
    # deliverable — so the generator needs to know which region a link
    # sits in. Everything below is evidence-gated: an empty list means the
    # markup did not say, and the checklist simply omits those rows rather
    # than inventing them.
    header_links: list[dict] = field(default_factory=list)   # {text, href}
    footer_links: list[dict] = field(default_factory=list)   # {text, href}
    email_links: list[str] = field(default_factory=list)     # mailto: targets
    phone_links: list[str] = field(default_factory=list)     # tel: targets
    social_links: list[dict] = field(default_factory=list)   # {network, href}
    legal_links: list[dict] = field(default_factory=list)    # {text, href}
    has_header_logo: bool = False
    has_footer_logo: bool = False
    #: Menu structure in the chrome: {region, label, children[]}. The
    #: reference deliverable spends one Header row per menu, so a flat
    #: link list is not enough — see _PageParser.nav_groups.
    nav_groups: list[dict] = field(default_factory=list)
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
    #: At least one page renders a data grid (layout tables excluded).
    #: The estimator scales its budget off this — a list surface is worth
    #: 12-20 cases in the reference corpus, which no per-page density
    #: formula built from forms and buttons can see.
    has_grid: bool = False
    grid_count: int = 0
    has_footer: bool = True
    page_count: int = 0
    crawl_errors: list[str] = field(default_factory=list)
    # Architecture detection — used by qa_estimator to scale TC budget
    # per site type. One of: "wordpress", "spa", "ecommerce", "dashboard",
    # "landing", "static", "generic".
    site_type: str = "generic"
    architecture_notes: list[str] = field(default_factory=list)


# ── Grid evidence patterns ───────────────────────────────────────
#
# Every pattern here answers one question: "does the markup itself say
# this control exists?". They are deliberately narrow — a site that does
# not name its filter a filter simply yields no filter cases, which is
# the correct outcome. Guessing would produce cases that fail for the
# wrong reason.

# ── Page-region evidence ─────────────────────────────────────────
#
# Which region a link sits in decides whether it becomes a Header check, a
# Footer check, or a Page Content check. Same discipline as the grid
# patterns: the markup has to say so.

#: Containers a site actually uses for its chrome. ``<header>`` /
#: ``<footer>`` and their ARIA landmark equivalents are unambiguous; a
#: div/section needs the word in its class or id.
_REGION_CONTAINERS = frozenset({"header", "footer", "div", "section",
                                "aside", "nav"})
_HEADER_HINT_RE = re.compile(
    r"(?:^|[^a-z-])(?:site-?|page-?|main-?|top-?|global-?)?header"
    r"(?:[^a-z-]|$)|\bmasthead\b|\bnavbar\b|\btop-?bar\b", re.IGNORECASE)
_FOOTER_HINT_RE = re.compile(
    r"(?:^|[^a-z-])(?:site-?|page-?|main-?|global-?)?footer"
    r"(?:[^a-z-]|$)|\bcolophon\b", re.IGNORECASE)
#: A column header, a table header row or an HTTP header is not the page
#: Header. Same false-positive family the terminology linter had to fence
#: off; see engine/glossary.py PAGE_REGIONS.
_REGION_FALSE_FRIEND_RE = re.compile(
    r"\b(?:column|table|row|cell|grid|card|modal|accordion|section|panel|"
    r"sticky-?wrap|sub)-?(?:header|footer)\b|\b(?:header|footer)-?"
    r"(?:row|cell|col|column|title|text|label)\b", re.IGNORECASE)
_LOGO_HINT_RE = re.compile(r"\blogo\b|\bbrand\b|\bhome-?link\b", re.IGNORECASE)
_LEGAL_RE = re.compile(
    r"privacy|cookie|terms|gdpr|\blegal\b|impressum|disclaimer|"
    r"accessibility-statement", re.IGNORECASE)
#: Social platforms, mapped to the name a checklist row should print. The
#: reference Footer sweep names the networks it found ("Instagram,
#: LinkedIn, YouTube") rather than writing a generic row.
_SOCIAL_DOMAINS: dict[str, str] = {
    "facebook.com": "Facebook", "fb.com": "Facebook",
    "instagram.com": "Instagram",
    "linkedin.com": "LinkedIn",
    "youtube.com": "YouTube", "youtu.be": "YouTube",
    "twitter.com": "X (Twitter)", "x.com": "X (Twitter)",
    "t.me": "Telegram", "telegram.me": "Telegram",
    "tiktok.com": "TikTok",
    "github.com": "GitHub",
    "gitlab.com": "GitLab",
    "dribbble.com": "Dribbble",
    "behance.net": "Behance",
    "medium.com": "Medium",
    "pinterest.com": "Pinterest",
    "wa.me": "WhatsApp", "whatsapp.com": "WhatsApp",
    "viber.com": "Viber",
    "clutch.co": "Clutch",
    "upwork.com": "Upwork",
    "goodfirms.co": "GoodFirms",
    "vimeo.com": "Vimeo",
    "reddit.com": "Reddit",
    "threads.net": "Threads",
    "bsky.app": "Bluesky",
    "mastodon.social": "Mastodon",
    "discord.gg": "Discord", "discord.com": "Discord",
    "slack.com": "Slack",
    "spotify.com": "Spotify",
    "apple.com/app-store": "App Store",
    "play.google.com": "Google Play",
}


def _region_of(tag: str, attr_dict: dict, blob: str) -> str:
    """``"header"`` / ``"footer"`` / ``""`` for a container start tag."""
    if tag not in _REGION_CONTAINERS:
        return ""
    role = str(attr_dict.get("role") or "").strip().lower()
    if tag == "header" or role == "banner":
        return "header"
    if tag == "footer" or role == "contentinfo":
        return "footer"
    if _REGION_FALSE_FRIEND_RE.search(blob):
        return ""
    if _FOOTER_HINT_RE.search(blob):
        return "footer"
    if _HEADER_HINT_RE.search(blob):
        return "header"
    return ""


def _social_network(href_lower: str) -> str:
    """Platform name for a social URL, or ``""``.

    Matched on the host (plus a path prefix for the two app stores) so a
    blog post that merely mentions facebook.com in a query string does not
    register as a social profile link.
    """
    try:
        from urllib.parse import urlparse
        host = (urlparse(href_lower).netloc or "").lstrip("www.")
        path = urlparse(href_lower).path or ""
    except Exception:  # pragma: no cover — defensive
        return ""
    if not host:
        return ""
    for domain, name in _SOCIAL_DOMAINS.items():
        if "/" in domain:
            d_host, _, d_path = domain.partition("/")
            if host.endswith(d_host) and path.lstrip("/").startswith(d_path):
                return name
        elif host == domain or host.endswith("." + domain):
            return name
    return ""


_PAGER_HINT_RE = re.compile(r"pagin|\bpager\b|\bpaging\b", re.IGNORECASE)
_PAGER_ARIA_RE = re.compile(
    r"\b(?:next|previous|prev)\s+page\b|\bpage\s+\d+\b|pagin", re.IGNORECASE)
#: "sortable", "sort-header", "js-sort", "orderby". The lookbehind keeps
#: Wikipedia's ``class="unsortable"`` — an opt-*out* — from reading as an
#: opt-in, and _UNSORTABLE_RE catches the rest of the opt-out spellings.
_SORT_HINT_RE = re.compile(
    r"(?<!un)sortable|\bsort\b|sort[-_]|[-_]sort|order[-_]?by",
    re.IGNORECASE)
_UNSORTABLE_RE = re.compile(r"unsortable|no[-_]?sort", re.IGNORECASE)
_SEARCH_HINT_RE = re.compile(r"search|filter|query|\bq\b", re.IGNORECASE)
_FILTER_HINT_RE = re.compile(r"filter|refine|facet", re.IGNORECASE)
_BULK_HINT_RE = re.compile(
    r"bulk|mass[-_ ]?action|action[-_ ]?selector", re.IGNORECASE)
#: "Create", "New", "Add product", "+ Add" — but not "Add to cart",
#: which is a purchase control, not a record-creation one.
_CREATE_LABEL_RE = re.compile(
    r"^(?:[+＋]\s*)?(?:create|new|add)\b[\w &/'-]{0,24}$", re.IGNORECASE)

#: Only container elements open a pagination scope. A void element
#: (<input>, <img>) never gets an end tag, so tracking one would leave
#: the scope open for the rest of the document.
_PAGER_CONTAINERS = {"nav", "ul", "ol", "div", "section", "span", "p",
                     "table", "tfoot", "td", "footer", "aside"}


def _attr_blob(attr_dict: dict) -> str:
    """The identifying attributes, joined, for pattern matching."""
    return " ".join(str(attr_dict.get(k) or "") for k in
                    ("name", "id", "class", "aria-label", "role", "rel"))


def _control_label(attr_dict: dict) -> str:
    """Best human-readable name for a control from its attributes."""
    for key in ("aria-label", "title", "name", "id"):
        val = str(attr_dict.get(key) or "").strip()
        if val:
            return re.sub(r"[_\-]+", " ", val).strip()[:60]
    return ""


def _declares_sorting(blob: str, has_aria_sort: bool = False) -> bool:
    """Whether these attributes say "this sorts".

    An explicit opt-out wins: ``class="wikitable sortable"`` on the table
    with ``class="unsortable"`` on one header means every column but that
    one sorts, and generating a sort case for the excluded column would
    fail for the wrong reason.
    """
    if _UNSORTABLE_RE.search(blob):
        return False
    return has_aria_sort or bool(_SORT_HINT_RE.search(blob))


def _is_grid(tbl: dict) -> bool:
    """Whether a parsed <table> is a data grid rather than page layout.

    Two or more ``<th>`` column headers is the honest signature of a
    tabular record list. A layout table has neither headers nor a
    caption, and ``role="presentation"`` is the author telling us
    outright that the table carries no tabular semantics.
    """
    if tbl.get("_role") in ("presentation", "none"):
        return False
    columns = tbl.get("columns") or []
    if len(columns) >= 2:
        return True
    # An explicit role is the author asserting tabular semantics, so a
    # single-column grid is believable there and nowhere else.
    return bool(columns) and tbl.get("_role") in ("grid", "table")


# ── HTML Parser ──────────────────────────────────────────────────

class _PageParser(HTMLParser):
    """Extract structural info from an HTML page."""

    def __init__(self):
        super().__init__()
        self.title = ""
        self.h1 = ""
        self.headings: list[str] = []
        self.heading_levels: list[dict] = []
        self._heading_level = 0
        self.forms: list[dict] = []
        self.nav_links: list[str] = []
        self.buttons: list[str] = []
        self.links: list[str] = []
        self.images_count = 0
        self.has_video = False
        self.meta_description = ""
        #: Data grids, layout tables excluded (see :func:`_is_grid`).
        self.tables: list[dict] = []
        #: Controls that surround a grid. Collected page-wide rather than
        #: "above the table": HTMLParser walks a token stream, not a tree,
        #: so proximity is not knowable. On a page with two grids both
        #: inherit the same controls — the pragmatic trade for keeping the
        #: parser a single linear pass.
        self.grid_controls: dict = {
            "pagination": False,
            "pagination_labels": [],
            "search": False,
            "search_labels": [],
            "filters": [],
            "bulk_actions": [],
            "create_controls": [],
        }

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
        self._labels_by_for: dict[str, str] = {}
        # Grid capture. Tables nest (a data grid inside a layout table is
        # ordinary), so cells attribute to the innermost open table.
        self._table_stack: list[dict] = []
        self._th_text: str | None = None
        self._caption_text: str | None = None
        self._pager_tag = ""
        self._pager_nest = 0
        #: Where <option> text goes. A <select> can feed the form's field
        #: inventory, the bulk-action list, or both.
        self._option_sinks: list[list] = []
        self._bulk_options: list | None = None
        #: Header / Footer region scope — see handle_starttag.
        self._region = ""
        self._region_tag = ""
        self._region_nest = 0
        self._a_href = ""
        self._a_is_logo = False
        #: Region link inventories. ``{"text": …, "href": …}`` pairs, in
        #: document order, deduplicated on (text, href).
        self.header_links: list[dict] = []
        self.footer_links: list[dict] = []
        #: Contact affordances a checklist owes a row each: the reference
        #: Footer sweep checks "clicking the email opens the default mail
        #: client" and "clicking the phone number opens the call
        #: application" separately.
        self.email_links: list[str] = []
        self.phone_links: list[str] = []
        #: ``{"network": "LinkedIn", "href": …}`` — the reference names the
        #: networks it found rather than writing a generic "social icons"
        #: row, so the network has to be resolved here.
        self.social_links: list[dict] = []
        #: Privacy / Cookie / Terms. The rows a client checks first and the
        #: ones most often left pointing at a 404.
        self.legal_links: list[dict] = []
        #: True when an anchor marked as the logo was seen in the Header.
        self.has_header_logo = False
        self.has_footer_logo = False
        #: Menu structure inside the Header / Footer chrome:
        #: ``{"region": "header", "label": "Why Us", "children": [...]}``.
        #:
        #: This is what a flat link list cannot give. The reference
        #: deliverable spends ONE Header row per menu — 'Verify that all
        #: sub-items are visible and clickable from the "Why Us" drop-down
        #: menu' — so a 60-link flat inventory would generate 60 unusable
        #: rows where the team writes 5. Derived from <ul>/<li> nesting: a
        #: top-level <li> whose subtree opens another list is a drop-down,
        #: and its first anchor is the label.
        self.nav_groups: list[dict] = []
        self._list_depth = 0
        self._li_stack: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple]):
        attr_dict = dict(attrs)
        tag_lower = tag.lower()
        blob = _attr_blob(attr_dict)

        # Pagination scope — a container the markup names a pager. Its
        # links are the page-number / next / previous controls.
        if self._pager_nest:
            if tag_lower == self._pager_tag:
                self._pager_nest += 1
        elif tag_lower in _PAGER_CONTAINERS and _PAGER_HINT_RE.search(blob):
            self._pager_tag = tag_lower
            self._pager_nest = 1
            self.grid_controls["pagination"] = True

        # Header / Footer scope. A low-level checklist opens with a Header
        # section and closes with a Footer one (7 and 12 checks in the
        # reference deliverable), so which region a link sits in is the
        # difference between generating those sections and guessing them.
        # Same tag+nest idiom as the pager scope above: it survives the
        # unclosed tags real pages ship, where a tag stack would not.
        if self._region_nest:
            if tag_lower == self._region_tag:
                self._region_nest += 1
        else:
            region = _region_of(tag_lower, attr_dict, blob)
            if region:
                self._region = region
                self._region_tag = tag_lower
                self._region_nest = 1
                self._list_depth = 0
                self._li_stack = []

        # Menu structure inside the chrome. Only tracked while in a region
        # — a <ul> in the page body is content, not navigation.
        if self._region:
            if tag_lower in ("ul", "ol"):
                self._list_depth += 1
            elif tag_lower == "li":
                self._li_stack.append({"depth": self._list_depth,
                                       "label": "", "children": []})

        if tag_lower == "title":
            self._in_title = True
            self._current_text = ""
        elif tag_lower == "h1" and not self.h1:
            self._in_h1 = True
            self._current_text = ""
        elif tag_lower in ("h2", "h3", "h4"):
            self._in_heading = True
            self._heading_text = ""
            self._heading_level = int(tag_lower[1])
        elif tag_lower == "nav":
            self._in_nav = True
        elif tag_lower == "a":
            href = attr_dict.get("href", "")
            if href and not href.startswith(("#", "javascript:")):
                self.links.append(href)
            self._in_a = True
            self._current_text = ""
            # Remember the href so handle_endtag can pair it with the link
            # text once the text has been collected.
            self._a_href = href
            self._a_is_logo = bool(_LOGO_HINT_RE.search(blob))
            rel = str(attr_dict.get("rel") or "").lower()
            if ("next" in rel or "prev" in rel
                    or _PAGER_ARIA_RE.search(
                        str(attr_dict.get("aria-label") or ""))):
                self.grid_controls["pagination"] = True
            self._note_cell_link()
        elif tag_lower == "button":
            self._in_button = True
            self._current_text = ""
            self._note_cell_link()
        elif tag_lower == "input":
            btn_type = attr_dict.get("type", "").lower()
            if btn_type in ("submit", "button"):
                val = attr_dict.get("value", "")
                if val:
                    self.buttons.append(val)
                    self._note_create_control(val)
                    if self._current_form is not None and not self._current_form.get("submit_text"):
                        self._current_form["submit_text"] = val
            if btn_type == "checkbox":
                self._note_row_checkbox()
            if btn_type == "search" or (
                    btn_type in ("", "text")
                    and _SEARCH_HINT_RE.search(
                        blob + " " + str(attr_dict.get("placeholder") or ""))):
                self.grid_controls["search"] = True
                label = (str(attr_dict.get("placeholder") or "").strip()
                         or _control_label(attr_dict) or "Search")
                self._append_control("search_labels", label)
            if self._current_form is not None:
                self._current_form["fields"].append(
                    self._field(attr_dict, attr_dict.get("type", "text")))
        elif tag_lower == "textarea":
            if self._current_form is not None:
                self._current_form["fields"].append(
                    self._field(attr_dict, "textarea"))
        elif tag_lower == "select":
            sinks: list[list] = []
            if self._current_form is not None:
                fld = self._field(attr_dict, "select")
                fld["options"] = []
                self._current_form["fields"].append(fld)
                self._current_select = fld
                sinks.append(fld["options"])
            if _BULK_HINT_RE.search(blob):
                # The options of a bulk-action picker ARE the actions —
                # "Delete selected", "Export", "Mark as read".
                self._bulk_options = []
                sinks.append(self._bulk_options)
            elif _FILTER_HINT_RE.search(blob):
                self._append_control("filters", _control_label(attr_dict))
            self._option_sinks = sinks
        elif tag_lower == "option":
            # Real option values let a generated step name the choice
            # instead of saying "select a value" — the difference between
            # a runnable case and a placeholder.
            if self._option_sinks:
                self._pending_option = attr_dict.get("value", "")
                self._option_text = ""
        elif tag_lower == "table":
            self._table_stack.append({
                "caption": "",
                "columns": [],
                "sortable_columns": [],
                "row_count": 0,
                "has_checkboxes": False,
                "select_all": False,
                "row_links": False,
                "_role": str(attr_dict.get("role") or "").lower(),
                "_in_thead": False,
                "_row_has_td": False,
                # Grid libraries mark the table sortable, not each
                # header — class="wikitable sortable", DataTables, and
                # bootstrap-table all do it this way.
                "_all_sortable": _declares_sorting(blob),
                "_th_sortable": False,
                "_th_unsortable": False,
                "_th_has_link": False,
                "_th_is_row_header": False,
                # Header cells of the row being read. Only committed once
                # the row is known to hold no <td> — see _flush_header_row.
                "_row_ths": [],
            })
        elif tag_lower == "caption" and self._table_stack:
            self._caption_text = ""
        elif tag_lower == "thead" and self._table_stack:
            self._table_stack[-1]["_in_thead"] = True
        elif tag_lower == "tr" and self._table_stack:
            tbl = self._table_stack[-1]
            # Markup that omits </tr> would otherwise lose the header row
            # it just finished, so flush here as well as on the end tag.
            self._flush_header_row(tbl)
            tbl["_row_has_td"] = False
        elif tag_lower == "th" and self._table_stack:
            tbl = self._table_stack[-1]
            self._th_text = ""
            tbl["_th_sortable"] = _declares_sorting(
                blob, "aria-sort" in attr_dict)
            tbl["_th_unsortable"] = bool(_UNSORTABLE_RE.search(blob))
            tbl["_th_has_link"] = False
            tbl["_th_is_row_header"] = (
                str(attr_dict.get("scope") or "").lower()
                in ("row", "rowgroup"))
        elif tag_lower == "td" and self._table_stack:
            self._table_stack[-1]["_row_has_td"] = True
        elif tag_lower == "label":
            # <label for="x"> is how a human names the control, so steps
            # can quote the visible label instead of the machine-readable
            # ``name`` attribute.
            self._label_for = attr_dict.get("for", "")
            self._label_text = ""
        elif tag_lower == "form":
            if str(attr_dict.get("role") or "").lower() == "search":
                self.grid_controls["search"] = True
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

    # ── Grid evidence helpers ────────────────────────────────────

    def _append_control(self, key: str, label: str, cap: int = 8) -> None:
        """Record a de-duplicated, capped grid control label."""
        label = " ".join((label or "").split())[:60]
        bucket = self.grid_controls[key]
        if label and label not in bucket and len(bucket) < cap:
            bucket.append(label)

    @staticmethod
    def _flush_header_row(tbl: dict) -> None:
        """Commit the buffered ``<th>`` cells — if they form a header row.

        A row that also holds ``<td>`` is a data row whose leading
        ``<th>`` is a *row* header, not a column. PyPI's hash table is
        the real case: ``<th scope="row">SHA256</th><td>…</td>`` made
        "SHA256" / "MD5" / "BLAKE2b-256" look like three extra columns,
        which would have produced sort cases for columns that do not
        exist. Buffering catches it even when ``scope`` is omitted.
        """
        buffered = tbl.get("_row_ths") or []
        if buffered and not tbl["_row_has_td"]:
            for text, sortable in buffered:
                if len(tbl["columns"]) >= MAX_GRID_COLUMNS:
                    break
                # A <tfoot> that repeats the header row is standard in
                # DataTables markup; without this the columns — and the
                # sort cases fanned out over them — come out doubled.
                if text in tbl["columns"]:
                    continue
                tbl["columns"].append(text)
                if sortable:
                    tbl["sortable_columns"].append(text)
        tbl["_row_ths"] = []

    def _note_cell_link(self) -> None:
        """A link/button inside a data cell opens the record it sits on.

        Inside a ``<th>`` the same markup means something else — a
        clickable header is how a grid exposes sorting — so it is
        recorded there instead.
        """
        if not self._table_stack:
            return
        tbl = self._table_stack[-1]
        if self._th_text is not None:
            tbl["_th_has_link"] = True
        elif tbl["_row_has_td"] and not tbl["_in_thead"]:
            tbl["row_links"] = True

    def _note_row_checkbox(self) -> None:
        """A checkbox in the header selects all rows; in a row, one row."""
        if not self._table_stack:
            return
        tbl = self._table_stack[-1]
        if not tbl["_row_has_td"] and (self._th_text is not None
                                       or tbl["_in_thead"]):
            tbl["select_all"] = True
        elif tbl["_row_has_td"]:
            tbl["has_checkboxes"] = True

    def _note_create_control(self, text: str) -> None:
        """Record a "Create" / "New" / "Add …" control by its own label."""
        label = " ".join((text or "").split())
        if not label or " to " in label.lower():
            # "Add to cart" is a purchase control, not record creation.
            return
        if _CREATE_LABEL_RE.match(label):
            self._append_control("create_controls", label, cap=6)

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

    def _classify_link(self, text: str, href: str, is_logo: bool) -> None:
        """File one anchor into the region / contact / social inventories.

        Called once per ``</a>`` so the link text is already collected.
        Every bucket is capped — a sitemap-in-the-footer page would
        otherwise hand the checklist author 400 rows to walk.
        """
        href = (href or "").strip()
        if not href:
            return
        low = href.lower()

        if low.startswith("mailto:"):
            addr = href[7:].split("?")[0].strip()
            if addr and addr not in self.email_links \
                    and len(self.email_links) < 6:
                self.email_links.append(addr)
            return
        if low.startswith("tel:"):
            num = href[4:].strip()
            if num and num not in self.phone_links \
                    and len(self.phone_links) < 6:
                self.phone_links.append(num)
            return
        if low.startswith(("#", "javascript:", "data:")):
            return

        network = _social_network(low)
        if network:
            if not any(s["network"] == network for s in self.social_links) \
                    and len(self.social_links) < 14:
                self.social_links.append({"network": network, "href": href})
            return

        if _LEGAL_RE.search(low) or (text and _LEGAL_RE.search(text)):
            if not any(l["href"] == href for l in self.legal_links) \
                    and len(self.legal_links) < 8:
                self.legal_links.append({"text": text, "href": href})
            # A legal link also belongs to whichever region holds it — the
            # reference sweep lists it under Footer.

        if is_logo:
            if self._region == "header":
                self.has_header_logo = True
            elif self._region == "footer":
                self.has_footer_logo = True

        if not self._region or not text:
            return

        # Attribute the anchor to the menu it belongs to. The label is the
        # first anchor of a top-level <li>; anything deeper is a child.
        in_menu = False
        if self._li_stack:
            top = self._li_stack[0]
            if top["depth"] <= 1:
                if len(self._li_stack) == 1 and not top["label"]:
                    top["label"] = text
                elif len(self._li_stack) > 1 or top["label"]:
                    in_menu = True
                    if text != top["label"] and text not in top["children"] \
                            and len(top["children"]) < 20:
                        top["children"].append(text)

        sink = (self.header_links if self._region == "header"
                else self.footer_links)
        if len(sink) >= 60:
            return
        if not any(l["text"] == text and l["href"] == href for l in sink):
            # ``in_menu`` says this link is reachable only by opening a
            # drop-down. The checklist covers those with one row per menu,
            # so it must be able to tell them from the top-level links it
            # gives a row each. Deriving it downstream from the children
            # lists does not work: those are capped at 20 per menu, and a
            # mega-menu overflows the cap.
            sink.append({"text": text, "href": href, "in_menu": in_menu})

    def handle_endtag(self, tag: str):
        tag_lower = tag.lower()
        if self._pager_nest and tag_lower == self._pager_tag:
            self._pager_nest -= 1
        if self._region:
            if tag_lower in ("ul", "ol"):
                self._list_depth = max(0, self._list_depth - 1)
            elif tag_lower == "li" and self._li_stack:
                frame = self._li_stack.pop()
                # A top-level item that opened its own sub-list is a
                # drop-down. One with no children is a plain link and is
                # already recorded in header_links / footer_links.
                if frame["depth"] <= 1 and frame["label"] \
                        and frame["children"] \
                        and len(self.nav_groups) < 14 \
                        and not any(g["label"] == frame["label"]
                                    for g in self.nav_groups):
                    self.nav_groups.append({
                        "region": self._region,
                        "label": frame["label"],
                        "children": frame["children"][:20],
                    })
        if self._region_nest and tag_lower == self._region_tag:
            self._region_nest -= 1
            if self._region_nest <= 0:
                self._region_nest = 0
                self._region = ""
                self._region_tag = ""
                self._list_depth = 0
                self._li_stack = []
        if tag_lower == "title":
            self._in_title = False
            self.title = self._current_text.strip()
        elif tag_lower == "h1":
            if self._in_h1:
                self.h1 = self._current_text.strip()
            self._in_h1 = False
        elif tag_lower in ("h2", "h3", "h4"):
            if self._in_heading and self._heading_text.strip():
                text = self._heading_text.strip()
                # h4 feeds heading_levels only. Adding it to `headings`
                # would change what every existing consumer sees.
                if self._heading_level <= 3:
                    self.headings.append(text)
                if len(self.heading_levels) < 60:
                    self.heading_levels.append(
                        {"level": self._heading_level, "text": text})
            self._in_heading = False
        elif tag_lower == "nav":
            self._in_nav = False
        elif tag_lower == "a":
            txt = self._current_text.strip()
            if self._in_nav and txt:
                self.nav_links.append(txt)
            if self._pager_nest and txt:
                self._append_control("pagination_labels", txt, cap=10)
            self._note_create_control(txt)
            self._classify_link(txt, self._a_href, self._a_is_logo)
            self._a_href = ""
            self._a_is_logo = False
            self._in_a = False
        elif tag_lower == "button":
            txt = self._current_text.strip()
            if self._pager_nest and txt:
                self._append_control("pagination_labels", txt, cap=10)
            self._note_create_control(txt)
            if txt:
                self.buttons.append(txt)
                # Attach as submit_text for the form we're currently
                # inside, if any — the parser feeds qa_persona's
                # _form_label which prefers this over the URL action.
                if self._current_form is not None and not self._current_form.get("submit_text"):
                    self._current_form["submit_text"] = txt
            self._in_button = False
        elif tag_lower == "option":
            shown = (self._option_text or "").strip() \
                or (self._pending_option or "").strip()
            if shown:
                for sink in self._option_sinks:
                    if len(sink) < 12:
                        sink.append(shown)
            self._pending_option = None
            self._option_text = ""
        elif tag_lower == "select":
            if self._bulk_options is not None:
                for opt in self._bulk_options:
                    # Drop the picker's own "-- Bulk actions --" prompt:
                    # it names the control, not an action.
                    if _BULK_HINT_RE.search(opt) or opt.strip("- ").strip() == "":
                        continue
                    self._append_control("bulk_actions", opt.strip("- ").strip())
                self._bulk_options = None
            self._option_sinks = []
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
        elif tag_lower == "caption" and self._table_stack:
            text = " ".join((self._caption_text or "").split())
            if text:
                self._table_stack[-1]["caption"] = text[:80]
            self._caption_text = None
        elif tag_lower == "th" and self._table_stack:
            tbl = self._table_stack[-1]
            text = " ".join((self._th_text or "").split())
            if text and not tbl["_th_is_row_header"]:
                # A header that carries aria-sort, a sort class, a
                # link/button, or that sits in a table the markup calls
                # sortable, is the markup saying the column sorts.
                sortable = not tbl["_th_unsortable"] and bool(
                    tbl["_th_sortable"] or tbl["_th_has_link"]
                    or tbl["_all_sortable"])
                tbl["_row_ths"].append((text[:60], sortable))
            self._th_text = None
            tbl["_th_sortable"] = False
            tbl["_th_unsortable"] = False
            tbl["_th_has_link"] = False
            tbl["_th_is_row_header"] = False
        elif tag_lower == "thead" and self._table_stack:
            self._flush_header_row(self._table_stack[-1])
            self._table_stack[-1]["_in_thead"] = False
        elif tag_lower == "tr" and self._table_stack:
            tbl = self._table_stack[-1]
            if tbl["_row_has_td"] and not tbl["_in_thead"]:
                tbl["row_count"] += 1
            self._flush_header_row(tbl)
            tbl["_row_has_td"] = False
        elif tag_lower == "table" and self._table_stack:
            self._flush_header_row(self._table_stack[-1])
            tbl = self._table_stack.pop()
            self._th_text = None
            self._caption_text = None
            if _is_grid(tbl) and len(self.tables) < MAX_TABLES_PER_PAGE:
                self.tables.append(
                    {k: v for k, v in tbl.items() if not k.startswith("_")})
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
        if self._th_text is not None:
            self._th_text += data
        if self._caption_text is not None:
            self._caption_text += data
        if self._label_text is not None and self._pending_option is None:
            # Both sides of a wrapped control matter: "Customer name:
            # <input>" puts the name before it, "<input> Small" after.
            # Only <option> text is excluded — that was the real source
            # of the original label/option bleed, not position.
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
            # Ask for compression and decode it. Omitting this header does
            # NOT guarantee an identity response — python.org served gzip
            # regardless, which decoded to binary noise and produced a page
            # record with zero headings, zero links and zero forms while
            # reporting no error at all. Every downstream module then
            # silently under-covered that site. See _decompress.
            "Accept-Encoding": "gzip, deflate",
        })
        # safe_opener, not urlopen: the policy has to apply to every hop.
        # urlopen follows redirects by default, so validating only the
        # first URL let an allowed host bounce the fetch to a private
        # address. See engine.security._ValidatingRedirectHandler.
        with _security.safe_opener().open(req, timeout=FETCH_TIMEOUT, context=ctx) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" not in content_type and "application/xhtml" not in content_type:
                return "", f"Not HTML: {content_type}"
            raw = resp.read(MAX_RAW_KB * 1024)
            raw, decomp_err = _decompress(
                raw, resp.headers.get("Content-Encoding", ""))
            if decomp_err:
                return "", decomp_err
            # Try to decode
            charset = "utf-8"
            ct = content_type.lower()
            if "charset=" in ct:
                charset = ct.split("charset=")[-1].split(";")[0].strip()
            try:
                html = raw.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                html = raw.decode("utf-8", errors="replace")
            # Truncate the DECODED document, not the wire bytes: with
            # compression on, a 512 KB cap on the wire is several megabytes
            # of markup.
            return html[:MAX_BODY_KB * 1024], ""
    except urllib.error.HTTPError as e:
        _logger.info("fetch failed: HTTP %s for %s", e.code, url)
        return "", f"HTTP {e.code}"
    except urllib.error.URLError as e:
        _logger.warning("fetch failed: URL error %s for %s", e.reason, url)
        return "", f"URL error: {e.reason}"
    except Exception as e:
        _logger.warning("fetch failed: %s for %s", e, url)
        return "", str(e)[:100]


def _decompress(raw: bytes, encoding: str) -> tuple[bytes, str]:
    """Decode a ``Content-Encoding`` body. Returns ``(bytes, error)``.

    A truncated stream is normal here — the wire read is capped at
    MAX_RAW_KB mid-document — so a partial inflate is kept rather than
    discarded: half a page still names most of its controls, whereas an
    empty page names none. Only a stream that yields nothing at all is an
    error.
    """
    enc = (encoding or "").strip().lower()
    if not enc or enc == "identity":
        return raw, ""
    limit = MAX_DECOMPRESSED_KB * 1024
    try:
        if enc in ("gzip", "x-gzip"):
            import zlib
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            out = d.decompress(raw, limit)
        elif enc == "deflate":
            import zlib
            try:
                out = zlib.decompressobj().decompress(raw, limit)
            except zlib.error:
                # Raw deflate without the zlib wrapper — servers do ship
                # this despite the RFC.
                out = zlib.decompressobj(-zlib.MAX_WBITS).decompress(
                    raw, limit)
        elif enc == "br":
            try:
                import brotli  # type: ignore
            except ImportError:
                return b"", "unsupported Content-Encoding: br"
            out = brotli.decompress(raw)[:limit]
        else:
            return b"", f"unsupported Content-Encoding: {enc}"
    except Exception as exc:
        _logger.warning("decompress failed (%s): %s", enc, exc)
        return b"", f"decompress failed: {type(exc).__name__}"
    if not out:
        return b"", f"empty body after {enc} decode"
    return out, ""


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
    page.heading_levels = parser.heading_levels[:60]
    page.forms = parser.forms
    page.tables = parser.tables
    # Only meaningful next to a grid — a pager on a page with no table
    # evidences nothing the list_surface model can use.
    page.grid_controls = dict(parser.grid_controls) if parser.tables else {}
    page.nav_links = parser.nav_links
    page.buttons = parser.buttons
    page.images_count = parser.images_count
    page.has_video = parser.has_video
    page.meta_description = parser.meta_description
    page.header_links = parser.header_links
    page.footer_links = parser.footer_links
    page.email_links = parser.email_links
    page.phone_links = parser.phone_links
    page.social_links = parser.social_links
    page.legal_links = parser.legal_links
    page.has_header_logo = parser.has_header_logo
    page.has_footer_logo = parser.has_footer_logo
    page.nav_groups = parser.nav_groups

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

    # Grids. _PageParser has already refused layout tables, so a
    # non-zero count really does mean "this site lists records".
    analysis.grid_count = sum(len(page.tables or []) for page in analysis.pages)
    analysis.has_grid = analysis.grid_count > 0

    # Build features list
    analysis.features_detected = ["web_general"]
    if analysis.has_auth:
        analysis.features_detected.append("auth")
    if analysis.has_search:
        analysis.features_detected.append("search")
    if analysis.has_forms:
        analysis.features_detected.append("forms")
    if analysis.has_grid:
        analysis.features_detected.append("grids")
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

    # Grids are reported, not ranked. A pricing comparison table on a
    # marketing page is a real grid, and letting it vote for "dashboard"
    # would inflate that site's whole budget — the per-page grid budget
    # already charges for the grid itself, on the page that has one.
    if getattr(analysis, "grid_count", 0):
        notes.append(f"{analysis.grid_count} data grid(s) parsed — "
                     f"list-surface coverage applies")

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
