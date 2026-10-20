"""TestFortge — the product's Work Breakdown Structure (owner decision, 2026-10-20).

Before generating anything, the product is broken down the way a tester's
high-level plan is: modules → submodules → pages. Every generated test case
and checklist item is then written *into* that structure — its summary opens
with its place in it (``[Services -> Web Development] Verify that …``) and it
is filed in the folder tree that mirrors it. The prefix and the folder come
from the same node, so they cannot disagree.

Built from evidence only, like the checklist (folder_plan.py did the first
version of this; it filed after the fact by matching text, which is why 752
of 817 cases on qarea.com landed in "General")::

    Header                         component: logo, menus, the bar itself
    Homepage                       the entry page
    Company                        a header drop-down → a module
      Our team                     its entries → pages
    Solutions
      Healthcare Solutions         an "All <X>" entry opens a submodule
        Pharma App
    Contact us                     a plain top-level link → a module
    Footer                         component; footer-only pages under it
      Privacy Policy
    Other pages                    crawled, but in no menu
    Cross-cutting                  site-wide checks, by testing type — small
      Security

Rules:

* **Depth** at most 3 (module → submodule → page).
* **Identity** is the normalised URL path for a page and the menu label for
  a module, so the same page gets the same node however its URL is spelled
  and whichever run produced it. Names are the visible nav label (first line
  of a multi-line mega-menu entry), whitespace-collapsed, ≤ 45 characters.
* A node needs a URL or a label the markup declared; nothing is invented.
* **Attribution** (:func:`attribute`) takes the hints a generator recorded
  while creating the item — the page it came from, the region, an explicit
  node an LLM chose — and only then falls back to the item's own fields.
  Cross-cutting is for checks that are genuinely site-wide.

Pure: no database. ``engine.db.ensure_wbs_folders`` maps nodes to folders;
:func:`stamp` writes the prefix and ``folder_id`` onto pack dicts.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from engine import folder_prefix as _fpx

MAX_DEPTH = 3
#: Mega-menus can list hundreds of links (testfort.com does); a folder tree
#: cannot. Past this many page nodes the rest attach by URL or not at all.
MAX_PAGE_NODES = 90
MAX_FOOTER_PAGES = 25
NAME_MAX = 45

HEADER, HOME, FOOTER, OTHER, XC = "header", "home", "footer", "other", "xc"

_SKIP_LABELS = re.compile(
    r"^(home|homepage|logo|menu|open menu|close|close menu|search|"
    r"skip to (main )?content|toggle navigation|en|ua|de|fr|es|ru|"
    r"read more|learn more|more|see all|view all)$", re.I)
_ALL_RE = re.compile(r"^all\s+(.+)$", re.I)

#: Cross-cutting topics, matched against a check's section and testing type
#: (first hit wins, so the specific ones come first).
_TOPICS = (
    ("Security", r"secur|https|xss|csrf|injection|auth(entication)? bypass"),
    ("Performance", r"perform|load time|speed|ttfb|core web vitals|latency"),
    ("Accessibility", r"accessib|wcag|a11y|screen reader|aria"),
    ("SEO", r"\bseo\b|meta description|sitemap|robots\.txt|open graph"),
    ("Compatibility", r"responsive|viewport|compatib|cross-browser|"
                      r"browser: ui|mobile view|tablet"),
    ("Localization", r"locali|internation|i18n|translat|language"),
    ("Search", r"\bsearch\b"),
    ("Forms", r"\bforms?\b|input|validation|field"),
    ("Links & Media", r"\blinks?\b|media|image|video"),
    ("Usability", r"usab|ui ?/ ?ux|\bux\b|ui_ux|interactiv"),
    ("Stability", r"javascript|console|error"),
)
_STRUCTURE_HEADER = re.compile(r"\b(header|navigation|nav bar|menu)\b", re.I)
_STRUCTURE_FOOTER = re.compile(r"\bfooter\b", re.I)
_PAGE_SECTION = re.compile(r"^\s*(?:page(?: content)?)\s*[:—–-]\s*(.+)$", re.I)


# ── text and URL helpers ─────────────────────────────────────────────

def label_of(text) -> str:
    """The visible label: first non-empty line, collapsed, capped."""
    for line in str(text or "").splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            return line
    return ""


def norm_path(url: str, base: str = "") -> str:
    """``/pricing`` for every spelling of the pricing page's URL."""
    try:
        parsed = urlparse(urljoin(base, url) if base else url)
    except ValueError:
        return ""
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/") or "/"
    path = re.sub(r"/index\.(html?|php)$", "", path, flags=re.I) or "/"
    return path.lower()


def _host(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-") or "x"


def _title_key(text: str) -> str:
    """A page title as a lookup key: the part before ``|``, folded."""
    text = re.split(r"\s[|│]\s?|\s[–—-]\s", str(text or ""))[0]
    return re.sub(r"\s+", " ", text).strip().casefold()


# ── the structure ───────────────────────────────────────────────────

@dataclass
class WbsNode:
    id: str
    name: str
    kind: str                    # component | module | submodule | page | other | crosscutting
    parent: str | None = None
    url: str = ""                # normalised path, "" when the node has none


@dataclass
class Wbs:
    base: str = ""
    nodes: dict[str, WbsNode] = field(default_factory=dict)
    by_url: dict[str, str] = field(default_factory=dict)
    by_label: dict[str, str] = field(default_factory=dict)
    by_title: dict[str, str] = field(default_factory=dict)

    # ── building ────────────────────────────────────────────────
    def add(self, node: WbsNode) -> WbsNode:
        if node.id in self.nodes:
            return self.nodes[node.id]
        taken = {n.name.casefold() for n in self.nodes.values()
                 if n.parent == node.parent}
        name, n = node.name, 2
        while name.casefold() in taken:
            name, n = f"{node.name} ({n})", n + 1
        node.name = name
        self.nodes[node.id] = node
        if node.url and node.url not in self.by_url:
            self.by_url[node.url] = node.id
        return node

    def depth(self, node_id: str) -> int:
        d, seen = 0, set()
        while node_id and node_id in self.nodes and node_id not in seen:
            seen.add(node_id)
            d += 1
            node_id = self.nodes[node_id].parent
        return d

    def cross_cutting(self, topic: str) -> str:
        self.add(WbsNode(XC, "Cross-cutting", "crosscutting"))
        return self.add(WbsNode(f"{XC}:{_slug(topic)}", topic, "crosscutting",
                                parent=XC)).id

    def other(self) -> str:
        return self.add(WbsNode(OTHER, "Other pages", "other")).id

    # ── reading ─────────────────────────────────────────────────
    def path(self, node_id: str) -> tuple[str, ...]:
        names, seen = [], set()
        while node_id and node_id in self.nodes and node_id not in seen:
            seen.add(node_id)
            names.insert(0, self.nodes[node_id].name)
            node_id = self.nodes[node_id].parent
        return tuple(names)

    def ordered(self) -> list[WbsNode]:
        """Parents before children, in the order the site lists them."""
        out: list[WbsNode] = []

        def walk(parent):
            for node in self.nodes.values():
                if node.parent == parent:
                    out.append(node)
                    walk(node.id)
        walk(None)
        return out

    def node_for_url(self, url: str) -> str | None:
        """The page's node — exact, else the nearest ancestor path, else
        the module whose pages share the URL's first segment."""
        if not url:
            return None
        if _host(url) and self.base and _host(url) != _host(self.base):
            return None
        path = norm_path(url, self.base)
        if not path:
            return None
        cursor = path
        while True:
            if cursor in self.by_url:
                return self.by_url[cursor]
            if cursor == "/" or "/" not in cursor.strip("/"):
                break
            cursor = cursor.rsplit("/", 1)[0] or "/"
        first = "/" + path.strip("/").split("/")[0] if path != "/" else "/"
        for url_key, node_id in self.by_url.items():
            if url_key != "/" and url_key.split("/")[1:2] == first.split("/")[1:2]:
                node = self.nodes[node_id]
                # The module, not a sibling page.
                while node.parent and self.nodes[node.parent].kind in (
                        "module", "submodule"):
                    node = self.nodes[node.parent]
                    if node.kind == "module":
                        break
                return node.id
        return None

    def node_for_name(self, text: str) -> str | None:
        """A crawled page or nav label named in a section/title."""
        key = _title_key(text)
        if not key:
            return None
        if key in self.by_title:
            return self.by_title[key]
        if key in self.by_label:
            return self.by_label[key]
        best = None
        for label, node_id in self.by_label.items():
            if len(label) >= 4 and re.search(
                    r"(?<!\w)" + re.escape(label) + r"(?!\w)", key):
                if best is None or len(label) > len(best[0]):
                    best = (label, node_id)
        return best[1] if best else None

    def nodes_for_glob(self, pattern: str) -> list[str]:
        hits = []
        for url_key, node_id in self.by_url.items():
            if fnmatch.fnmatch(url_key, pattern) or fnmatch.fnmatch(
                    url_key, norm_path(pattern, self.base) or pattern):
                hits.append(node_id)
        return hits

    # ── persistence ─────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {"base": self.base,
                "nodes": [dict(vars(n)) for n in self.ordered()],
                "by_title": dict(self.by_title),
                "by_label": dict(self.by_label)}

    @classmethod
    def from_dict(cls, data) -> "Wbs | None":
        if not isinstance(data, dict) or not data.get("nodes"):
            return None
        wbs = cls(base=str(data.get("base") or ""))
        for raw in data["nodes"]:
            if isinstance(raw, dict) and raw.get("id") and raw.get("name"):
                wbs.add(WbsNode(id=raw["id"], name=raw["name"],
                                kind=raw.get("kind") or "page",
                                parent=raw.get("parent"),
                                url=raw.get("url") or ""))
        wbs.by_title.update({str(k): str(v) for k, v in
                             (data.get("by_title") or {}).items()
                             if v in wbs.nodes})
        wbs.by_label.update({str(k): str(v) for k, v in
                             (data.get("by_label") or {}).items()
                             if v in wbs.nodes})
        return wbs

    def prompt_lines(self, limit: int = 120) -> list[str]:
        """``id — Path -> To -> Node (url)`` for an LLM to choose from."""
        out = []
        for node in self.ordered()[:limit]:
            path = " -> ".join(self.path(node.id))
            out.append(f"{node.id} — {path}" + (f" ({node.url})" if node.url else ""))
        return out


def build_wbs(pages: list[dict]) -> Wbs:
    """The WBS for one crawl. ``pages[0]`` is the entry page."""
    pages = [p for p in (pages or []) if isinstance(p, dict)]
    primary = pages[0] if pages else {}
    base = str(primary.get("url") or "")
    wbs = Wbs(base=base)
    wbs.add(WbsNode(HEADER, "Header", "component"))
    wbs.add(WbsNode(HOME, "Homepage", "page", url="/"))
    budget = [MAX_PAGE_NODES]

    def same_site(href: str) -> bool:
        if not href:
            return False
        host = _host(urljoin(base, href)) if base else _host(href)
        return not host or not base or host == _host(base)

    def add_page(parent: str, label: str, href: str, kind: str = "page"):
        label = label.strip()
        # A long "label" is a promotion or an article headline in a
        # mega-menu (measured on testfort.com and qarea.com), not a page.
        if not label or len(label) > NAME_MAX or _SKIP_LABELS.match(label)                 or not same_site(href):
            return None
        key = norm_path(href, base) if href else ""
        if not key:
            return None
        if key == "/":
            wbs.by_label.setdefault(label.casefold(), HOME)
            return HOME
        if key in wbs.by_url:
            wbs.by_label.setdefault(label.casefold(), wbs.by_url[key])
            return wbs.by_url[key]
        if wbs.depth(parent) + 1 > MAX_DEPTH or budget[0] <= 0:
            return None
        budget[0] -= 1
        node = wbs.add(WbsNode(f"page:{key}", label, kind, parent=parent,
                               url=key))
        wbs.by_label.setdefault(label.casefold(), node.id)
        return node.id

    def under_module(url: str) -> str | None:
        """The module/submodule a URL sits under by its path, if any."""
        owner = wbs.node_for_url(url)
        if not owner:
            return None
        node = wbs.nodes[owner]
        if node.kind in ("module", "submodule"):
            return owner
        # A menu page with pages under its own URL (/solutions/fitness →
        # /solutions/fitness/wellness) is a submodule of its menu.
        if node.kind == "page" and node.parent and wbs.nodes[
                node.parent].kind in ("module", "submodule")                 and wbs.depth(owner) < MAX_DEPTH                 and norm_path(url, base).startswith(node.url + "/"):
            node.kind = "submodule"
            return owner
        return None

    links = [l for l in (primary.get("header_links") or [])
             if isinstance(l, dict) and label_of(l.get("text"))]

    groups = [g for g in (primary.get("nav_groups") or [])
              if isinstance(g, dict) and g.get("region") == "header"
              and label_of(g.get("label"))
              and not _SKIP_LABELS.match(label_of(g.get("label")))]
    head_href = {label_of(l["text"]).casefold(): l.get("href") or ""
                 for l in links if not l.get("in_menu")}
    modules: dict[str, str] = {}
    member: dict[str, str] = {}
    for group in groups:
        label = label_of(group["label"])[:NAME_MAX]
        href = head_href.get(label.casefold(), "")
        url = norm_path(href, base) if href and same_site(href) else ""
        if url in ("/",) or url in wbs.by_url:
            url = ""
        module = wbs.add(WbsNode(f"module:{_slug(label)}", label, "module",
                                 url=url))
        modules[label.casefold()] = module.id
        wbs.by_label.setdefault(label.casefold(), module.id)
        for child in group.get("children") or []:
            member.setdefault(label_of(child).casefold(), label.casefold())

    # Header menus → modules → pages, walking the links in the order the
    # markup lists them. The drop-down children lists are capped by the
    # crawler; the links are the full record, and each one joins the menu
    # that lists it (or the menu opened last).
    current: str | None = None
    open_parent: dict[str, str] = {}
    menu_texts: set[str] = set()
    for link in links:
        text = label_of(link["text"])
        key = text.casefold()
        if not link.get("in_menu"):
            if key in modules:
                current = modules[key]
            continue
        module_id = modules.get(member.get(key, "")) or current
        if module_id is None:
            continue
        current = module_id
        menu_texts.add(key)
        module = wbs.nodes[module_id]
        href = link.get("href") or ""
        m = _ALL_RE.match(text)
        if m:
            url = norm_path(href, base) if href and same_site(href) else ""
            if not url or url == module.url:
                open_parent[module_id] = module_id   # the module's overview
                if url and url not in wbs.by_url:
                    wbs.by_url[url] = module_id
                continue
            name = m.group(1)[:NAME_MAX]
            if url in wbs.by_url:
                # The menu groups pages under a heading whose own link is
                # another module's page: a submodule identified by label.
                sub = wbs.add(WbsNode(f"group:{module_id}:{_slug(name)}",
                                      name, "submodule", parent=module_id))
            else:
                sub = wbs.add(WbsNode(f"page:{url}", name, "submodule",
                                      parent=module_id, url=url))
            wbs.by_label.setdefault(key, sub.id)
            open_parent[module_id] = sub.id
            continue
        add_page(open_parent.get(module_id, module_id), text, href)

    # Plain top-level links → a module each (Contact us, Blog).
    for link in links:
        text = label_of(link["text"])
        if link.get("in_menu") or text.casefold() in modules                 or text.casefold() in menu_texts:
            continue
        href = link.get("href") or ""
        key = norm_path(href, base) if href and same_site(href) else ""
        if not key or key in wbs.by_url or _SKIP_LABELS.match(text)                 or len(text) > NAME_MAX:
            if key == "/":
                wbs.by_label.setdefault(text.casefold(), HOME)
            continue
        node = wbs.add(WbsNode(f"page:{key}", text, "module", url=key))
        wbs.by_label.setdefault(text.casefold(), node.id)

    # Footer: its own links; a footer link into a module's URL space joins
    # that module rather than the Footer.
    wbs.add(WbsNode(FOOTER, "Footer", "component"))
    footer_budget = [MAX_FOOTER_PAGES]
    for link in primary.get("footer_links") or []:
        if not isinstance(link, dict):
            continue
        text, href = label_of(link.get("text")), link.get("href") or ""
        if not href or not same_site(href):
            continue
        key = norm_path(href, base)
        if key == "/" or key in wbs.by_url:
            add_page(FOOTER, text, href)        # records the label only
            continue
        owner = under_module(href)
        if owner:
            add_page(owner, text, href)
        elif footer_budget[0] > 0 and add_page(FOOTER, text, href):
            footer_budget[0] -= 1

    # Crawled pages no menu linked → beside their URL siblings, else Other.
    from engine.checklist_rules import _page_name
    for page in pages:
        url = str(page.get("url") or "")
        if not url:
            continue
        node_id = wbs.node_for_url(url) if norm_path(url, base) in wbs.by_url \
            else None
        if node_id is None:
            key = norm_path(url, base)
            if key == "/":
                node_id = HOME
            else:
                parent = under_module(url) or wbs.other()
                label = next((c for c in (
                    label_of(re.split(r"\s[|│]\s?", str(page.get("title") or ""))[0]),
                    label_of(page.get("h1")),
                    re.sub(r"[-_]+", " ", key.rsplit("/", 1)[-1]).strip().capitalize(),
                ) if c and len(c) <= NAME_MAX), "")
                node_id = add_page(parent, label, url) or parent
        for name in (_page_name(url, page.get("title", "")),
                     page.get("title"), page.get("h1")):
            key = _title_key(name)
            if key:
                wbs.by_title.setdefault(key, node_id)
    return wbs


# ── attribution ─────────────────────────────────────────────────────

def topic_of(*texts) -> str | None:
    hay = " ".join(str(t or "") for t in texts).casefold()
    for topic, pattern in _TOPICS:
        if re.search(pattern, hay):
            return topic
    return None


def attribute(wbs: Wbs, item: dict, field_name: str = "summary") -> str:
    """The node an item belongs to. Never returns None.

    In order: an explicit node a generator chose (an LLM picks from
    :meth:`Wbs.prompt_lines`), the region it was written for, the page it
    was generated from, its section naming a page ("Page: X", "Page Content
    — X") or the chrome, a URL pattern naming one page (or, for a stored
    row, a URL its steps mention — ``text_url``, weaker than the section),
    a cross-cutting topic, the entry page, and last ``Cross-cutting ->
    General``. The site root as a pattern is weak evidence: it is how
    site-wide checks are written, so a topic outranks it.
    """
    hint = item.get("wbs_hint") or {}
    node = hint.get("node") or item.get("wbs_node")
    if node and node in wbs.nodes:
        return node
    region = str(hint.get("region") or "").casefold()
    if region == "header":
        return HEADER
    if region == "footer":
        return FOOTER

    page_url = hint.get("page_url")
    if page_url:
        found = wbs.node_for_url(page_url)
        if found:
            return found

    section = str(item.get("section") or "")
    if section.strip().casefold() == "page content":
        return HOME                 # a single-page checklist's one surface
    m = _PAGE_SECTION.match(section)
    if m:
        found = wbs.node_for_name(m.group(1))
        if found:
            return found
    if _STRUCTURE_FOOTER.search(section):
        return FOOTER
    if _STRUCTURE_HEADER.search(section):
        return HEADER

    pattern = str(item.get("url_pattern") or hint.get("text_url")
                  or "").strip()
    root = False
    if pattern:
        if any(ch in pattern for ch in "*?["):
            hits = wbs.nodes_for_glob(pattern)
            if len(hits) == 1 or (hits and len(set(hits)) == 1):
                return hits[0]
        else:
            root = norm_path(pattern, wbs.base) == "/"
            if not root:
                found = wbs.node_for_url(pattern)
                if found:
                    return found

    text = str(item.get(field_name) or "")
    requirement = re.search(r'requirement "Page:\s*([^|"]+)', text)
    if requirement:
        found = wbs.node_for_name(requirement.group(1))
        if found:
            return found

    topic = topic_of(section, item.get("testing_type"))
    if topic:
        return wbs.cross_cutting(topic)
    if root:
        return HOME
    found = wbs.node_for_name(section)
    if found:
        return found
    topic = topic_of(text)
    if topic:
        return wbs.cross_cutting(topic)
    return wbs.cross_cutting("General")


def stamp(wbs: Wbs, folder_for, dicts: list, kind: str, *,
          nodes: list | None = None) -> dict:
    """Write each item's WBS prefix and ``folder_id``. Returns node counts.

    ``folder_for(node_id) -> (folder_id, path)`` resolves a node to its
    folder and that folder's *current* path (a renamed folder keeps its
    node), so the prefix written is exactly the folder path.
    """
    field_name = "summary" if kind == "test_case" else "objective"
    counts: dict[str, int] = {}
    for index, d in enumerate(dicts or []):
        if not isinstance(d, dict):
            continue
        node = (nodes[index] if nodes is not None and index < len(nodes)
                and nodes[index] else attribute(wbs, d, field_name))
        folder_id, path = folder_for(node)
        if not path:
            path = wbs.path(node)
        d[field_name] = _fpx.with_prefix(d.get(field_name), path)
        if folder_id is not None:
            d["folder_id"] = folder_id
        d.pop("wbs_hint", None)
        counts[node] = counts.get(node, 0) + 1
    return counts


__all__ = ["Wbs", "WbsNode", "build_wbs", "attribute", "stamp", "topic_of",
           "norm_path", "label_of", "HEADER", "HOME", "FOOTER", "OTHER", "XC"]
