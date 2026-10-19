"""TestFortge — the folder tree a generation run files its output into.

Built from the site's own navigation, the way a tester's high-level
checklist is (owner decision, 2026-10-19)::

    Header
      About                 ← a drop-down menu
        Company page        ← its entries
        Pricing page
      Blog page             ← a plain top-level link
      Contact us page
    Homepage
    Footer
    General                 ← only if something matched nothing above

Pure: it takes the crawler's page dicts and returns paths (tuples of folder
names) plus the lookups that decide which path an item goes to. Creating the
folders and stamping ``folder_id`` is the caller's job — see
``routes.generation._file_into_folders`` — so this module can be tested on
real crawled markup without a database.

Evidence-gated like the checklist: a menu the markup did not declare does not
get a folder. A site with no recognisable header still gets Homepage and
Footer, and everything else lands in General rather than in invented pages.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

HEADER = ("Header",)
HOMEPAGE = ("Homepage",)
FOOTER = ("Footer",)
GENERAL = ("General",)

#: A mega-menu can hold hundreds of links; a sidebar cannot. Past this many
#: page folders the rest of the menu is left to General.
MAX_PAGE_FOLDERS = 60
_LABEL_MAX = 80
#: Real nav labels are short. Measured on testfort.com (2026-10-19): the
#: mega-menu also carries a review quote and two case-study headlines
#: (47-53 characters, or several lines), which are promotions, not pages.
_PAGE_LABEL_MAX = 45
_SKIP_LABELS = re.compile(
    r"^(home|homepage|logo|menu|open menu|close|close menu|search|"
    r"skip to (main )?content|toggle navigation|en|ua|de|fr|es|ru)$", re.I)


def _clean(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:_LABEL_MAX]


def _norm_path(url: str, base: str = "") -> str:
    """``/pricing`` for every spelling of the pricing page's URL."""
    try:
        absolute = urljoin(base, url) if base else url
        parsed = urlparse(absolute)
    except ValueError:
        return ""
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/") or "/"
    path = re.sub(r"/index\.(html?|php)$", "", path, flags=re.I) or "/"
    return path.lower()


def _same_site(href: str, base: str) -> bool:
    try:
        host = urlparse(urljoin(base, href)).netloc.lower()
        base_host = urlparse(base).netloc.lower()
    except ValueError:
        return False
    strip = lambda h: h[4:] if h.startswith("www.") else h  # noqa: E731
    return not host or strip(host) == strip(base_host)


def _page_label(text: str) -> str:
    """Folder name for one page: ``Pricing`` → ``Pricing page``."""
    text = _clean(text)
    if not text:
        return ""
    return text if re.search(r"\bpage$", text, re.I) else f"{text} page"


@dataclass
class FolderPlan:
    paths: list[tuple] = field(default_factory=list)
    by_url: dict[str, tuple] = field(default_factory=dict)
    by_name: dict[str, tuple] = field(default_factory=dict)
    primary: tuple = HOMEPAGE

    def _add(self, path: tuple) -> None:
        if path not in self.paths:
            self.paths.append(path)

    # ── where things go ──────────────────────────────────────────

    def for_page(self, url: str) -> tuple | None:
        return self.by_url.get(_norm_path(url))

    def _for_name(self, text: str) -> tuple | None:
        """A nav label named in free text ("Pricing — plan cards")."""
        hay = _clean(text).casefold()
        if not hay:
            return None
        best = None
        for name, path in self.by_name.items():
            if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", hay):
                # The longest label wins: "Case studies" over "Case".
                if best is None or len(name) > len(best[0]):
                    best = (name, path)
        return best[1] if best else None

    def for_checklist(self, item: dict) -> tuple:
        """Header / Footer by section, a content section by its page."""
        section = _clean(item.get("section"))
        base, _, rest = section.partition("—")
        base = base.strip().casefold()
        if base == "header":
            return HEADER
        if base == "footer":
            return FOOTER
        if base.startswith("page content"):
            rest = rest.strip()
            if not rest:
                return self.primary
            return self.by_name.get(rest.casefold()) \
                or self._for_name(rest) or GENERAL
        # The authored checklist describes the primary surface; a section
        # name it chose ("Hero banner") belongs to that page.
        return self._for_name(section) or self.primary

    def for_test_case(self, item: dict, page_urls: list[str]) -> tuple:
        pattern = _clean(item.get("url_pattern"))
        if pattern and page_urls:
            hits = [u for u in page_urls
                    if fnmatch.fnmatch(u, pattern)
                    or fnmatch.fnmatch(_norm_path(u), pattern)]
            # A pattern that matches every page ("*") says nothing about
            # which page the case is for.
            if hits and (len(hits) < len(page_urls) or len(page_urls) == 1):
                for url in hits:
                    path = self.for_page(url)
                    if path:
                        return path
        section = _clean(item.get("section")).casefold()
        if re.search(r"\b(header|navigation|nav bar|menu)\b", section):
            return HEADER
        if re.search(r"\bfooter\b", section):
            return FOOTER
        return self._for_name(item.get("section")) \
            or self._for_name(item.get("summary")) or GENERAL


def build_plan(pages: list[dict]) -> FolderPlan:
    """The folder tree for one crawl. ``pages[0]`` is the entry page."""
    plan = FolderPlan()
    pages = [p for p in (pages or []) if isinstance(p, dict)]
    plan._add(HEADER)
    if not pages:
        plan._add(HOMEPAGE)
        plan._add(FOOTER)
        return plan

    primary = pages[0]
    base = primary.get("url") or ""
    root = _norm_path("/", base)
    plan.by_url[root] = HOMEPAGE

    links = [l for l in (primary.get("header_links") or [])
             if isinstance(l, dict) and _clean(l.get("text"))]
    href_of: dict[str, str] = {}
    for link in links:
        href_of.setdefault(_clean(link["text"]).casefold(), link.get("href") or "")

    budget = [MAX_PAGE_FOLDERS]

    def add_page(parent: tuple, text: str, href: str) -> None:
        raw = str(text or "").strip()
        if "\n" in raw or len(_clean(raw)) > _PAGE_LABEL_MAX:
            return
        label = _page_label(text)
        if not label or _SKIP_LABELS.match(_clean(text)):
            return
        if href and not _same_site(href, base):
            return
        url_key = _norm_path(href, base) if href else ""
        if url_key == root:
            plan.by_name.setdefault(_clean(text).casefold(), HOMEPAGE)
            return
        if budget[0] <= 0:
            return
        path = parent + (label,)
        if url_key and url_key in plan.by_url:
            # The same page linked twice (menu and top bar): one folder.
            plan.by_name.setdefault(_clean(text).casefold(), plan.by_url[url_key])
            return
        budget[0] -= 1
        plan._add(path)
        if url_key:
            plan.by_url[url_key] = path
        plan.by_name.setdefault(_clean(text).casefold(), path)

    groups = [g for g in (primary.get("nav_groups") or [])
              if isinstance(g, dict) and g.get("region") == "header"
              and _clean(g.get("label"))
              and not _SKIP_LABELS.match(_clean(g.get("label")))]
    group_labels = {_clean(g["label"]).casefold() for g in groups}

    # Plain top-level links first: they are the site's primary pages, and a
    # mega-menu walked first would spend the whole budget before reaching
    # them (measured on testfort.com).
    for link in links:
        text = _clean(link["text"])
        if link.get("in_menu") or text.casefold() in group_labels:
            continue
        add_page(HEADER, text, link.get("href") or "")

    for group in groups:
        label = _clean(group["label"])
        menu = HEADER + (label,)
        plan._add(menu)
        plan.by_name.setdefault(label.casefold(), menu)
        for child in group.get("children") or []:
            add_page(menu, child, href_of.get(_clean(child).casefold(), ""))

    plan._add(HOMEPAGE)
    plan._add(FOOTER)
    plan.primary = plan.for_page(base) or HOMEPAGE
    if plan.primary not in plan.paths:
        plan._add(plan.primary)

    # Content sections are named after the crawled page ("Page Content —
    # Pricing"), so the page's own name has to lead to its folder too.
    from engine.checklist_rules import _page_name
    for page in pages:
        path = plan.for_page(page.get("url") or "")
        if path:
            name = _page_name(page.get("url", ""), page.get("title", ""))
            plan.by_name.setdefault(_clean(name).casefold(), path)
    return plan


def file_items(plan: FolderPlan, tc_dicts: list[dict], cl_dicts: list[dict],
               page_urls: list[str]) -> tuple[dict, dict]:
    """``(tc paths, cl paths)``: list index → folder path, for each kind."""
    tc = {i: plan.for_test_case(d, page_urls)
          for i, d in enumerate(tc_dicts or []) if isinstance(d, dict)}
    cl = {i: plan.for_checklist(d)
          for i, d in enumerate(cl_dicts or []) if isinstance(d, dict)}
    return tc, cl


__all__ = ["FolderPlan", "build_plan", "file_items",
           "HEADER", "HOMEPAGE", "FOOTER", "GENERAL"]
