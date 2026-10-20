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
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from engine import folder_prefix as _fpx

MAX_DEPTH = 3
#: Mega-menus can list hundreds of links (testfort.com does); a folder tree
#: cannot. Past this many page nodes the rest attach by URL or not at all.
MAX_PAGE_NODES = 90
MAX_FOOTER_PAGES = 25
#: Crawled pages with no ancestor node at all, listed under "Other pages".
MAX_OTHER_PAGES = 15
NAME_MAX = 45

HEADER, HOME, FOOTER, OTHER, XC = "header", "home", "footer", "other", "xc"

_SKIP_LABELS = re.compile(
    r"^(home|homepage|logo|menu|open menu|close|close menu|search|"
    r"skip to (main )?content|toggle navigation|en|ua|de|fr|es|ru|"
    r"read more|learn more|more|see all|view all)$", re.I)

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
    ("Search", r"\bsearch\b|\bfilters?\b|\bsort"),
    ("Forms", r"\bforms?\b|input|validation|field"),
    ("Links & Media", r"\blinks?\b|media|image|video"),
    ("Usability", r"usab|ui ?/ ?ux|\bux\b|ui_ux|interactiv"),
    ("Stability", r"javascript|console|error"),
)
_STRUCTURE_HEADER = re.compile(r"\b(header|navigation|nav bar|menu)\b", re.I)
_STRUCTURE_FOOTER = re.compile(r"\bfooter\b", re.I)
_LAYOUT_SECTION = re.compile(r"\b(content\s*&\s*layout|layout)\b", re.I)
_FORM_SECTION = re.compile(r"\bforms?\b", re.I)
_SEARCH_SECTION = re.compile(r"\bsearch\b", re.I)
_SEARCH_FIELD = re.compile(r"^(q|s|search|query|keywords?|term)$", re.I)
#: A form on at least this share of crawled pages is chrome (as in tc_rules).
_CHROME_SHARE = 0.4


def _is_search_form(form: dict) -> bool:
    fields = [f for f in (form or {}).get("fields") or [] if isinstance(f, dict)]
    return any(str(f.get("type") or "").lower() == "search"
               or _SEARCH_FIELD.match(str(f.get("name") or "")) for f in fields)


def _search_home(wbs: "Wbs", pages: list[dict], base: str) -> str:
    """The node the site's search control belongs to (see ``search_node``)."""
    holders = [p for p in pages
               if any(_is_search_form(f) for f in p.get("forms") or [])]
    if not holders:
        return ""
    if len(pages) >= 3 and len(holders) >= _CHROME_SHARE * len(pages):
        return HEADER
    first = str(holders[0].get("url") or "")
    if norm_path(first, base) == "/":
        return HOME
    return wbs.node_for_url(first) or ""
_PAGE_SECTION = re.compile(r"^\s*(?:page(?: content)?)\s*[:—–-]\s*(.+)$", re.I)


# ── text and URL helpers ─────────────────────────────────────────────

#: Unicode categories that are decoration, not words: emoji and other
#: pictographs (So), modifier symbols such as skin tones (Sk), private-use
#: and unassigned code points. Letters of any script, digits, punctuation
#: and math/currency signs stay.
_DECORATION = {"So", "Sk", "Co", "Cs", "Cn"}
#: Emoji glue: variation selectors, the zero-width joiner, the keycap mark.
_EMOJI_GLUE = {"\ufe0e", "\ufe0f", "\u200d", "\u20e3"}


#: "Show everything in this group" phrasing on a group's own landing link,
#: in the languages the product is used in. Matched as a whole leading or
#: trailing phrase only, so "Alliance" or "Allegro" are left alone.
_SHOW_ALL_WORDS = (
    r"(?:view|see|show|browse|explore)\s+all|all|"
    r"(?:переглянути|дивитися|показати)\s+(?:усі|всі|все)|усі|всі|все|"
    r"alle|tous|toutes|todos|todas|tutti|tutte|wszystkie")
_SHOW_ALL_LEAD = re.compile(rf"^(?:{_SHOW_ALL_WORDS})\b[\s:–—-]*", re.I)
_SHOW_ALL_TAIL = re.compile(rf"[\s:–—-]*\b(?:{_SHOW_ALL_WORDS})\s*[→»>]?$",
                            re.I)


def group_name(label: str, url: str = "") -> str:
    """A submodule's name from its group's landing link.

    The link reads "All Healthcare Solutions" / "Усі послуги" / "Healthcare
    — view all"; the group is "Healthcare Solutions". The phrase goes; if
    nothing is left, the group is named after its URL's last segment.
    """
    text = _SHOW_ALL_TAIL.sub("", _SHOW_ALL_LEAD.sub("", label or "")).strip()
    if text and text.casefold() != (label or "").casefold():
        return text[:1].upper() + text[1:]
    if text:
        return text
    slug = (url or "").rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[-_]+", " ", slug).strip().capitalize()


def strip_decoration(text) -> str:
    """``"👍👎 Оцініть свою компанію"`` → ``"Оцініть свою компанію"``."""
    kept = "".join(ch for ch in str(text or "")
                   if ch not in _EMOJI_GLUE
                   and unicodedata.category(ch) not in _DECORATION)
    return re.sub(r"\s+", " ", kept).strip(" ·•|–—-")


def label_of(text) -> str:
    """The visible label: first non-empty line, collapsed, capped, and
    without the emoji or pictographs a menu decorates it with."""
    for line in str(text or "").splitlines():
        line = strip_decoration(line)
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
    # A single-page app routes in the fragment (#/pricing, #!/pricing):
    # there the fragment is the page, an ordinary #anchor is not.
    frag = parsed.fragment or ""
    if frag.startswith(("/", "!/")):
        route = re.sub(r"/+", "/", "/" + frag.lstrip("!").lstrip("/"))
        route = route.split("?")[0].rstrip("/")
        if route:
            path = (path.rstrip("/") + "/#" + route) if path != "/"                 else "/#" + route
    return path.lower()


def _host(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _slug(text: str) -> str:
    # \w, not [a-z0-9]: two Cyrillic menu labels must not both become "x".
    return re.sub(r"[^\w]+", "-", text.casefold()).strip("-_") or "x"


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
    #: The node of the first crawled page carrying a form — where generic
    #: form checks ("Forms & Input") are exercised. "" when there is none.
    primary_form: str = ""
    #: Where the site's search control lives: the Header when one search
    #: form repeats across the site (chrome), the page that carries it
    #: otherwise, "" when the crawl found none.
    search_node: str = ""
    #: ``site`` — read from a crawl; ``text`` — read from a description.
    mode: str = "site"
    #: Text mode: a requirement line (folded) → the heading it sits under.
    by_line: dict[str, str] = field(default_factory=dict)

    # ── building ────────────────────────────────────────────────
    def add(self, node: WbsNode) -> WbsNode:
        if node.id in self.nodes:
            return self.nodes[node.id]
        # Prefix-safe: the folder name and the prefix must be one string.
        node.name = _fpx.safe_name(strip_decoration(node.name)) or node.name
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
        # A title quoted in a requirement is often cut ("Page: $2000 за
        # хату й ділянк…"); a long enough prefix still names one page.
        if len(key) >= 12:
            hits = {node for title, node in self.by_title.items()
                    if title.startswith(key.rstrip("…. ")) or
                    (len(title) >= 12 and key.startswith(title))}
            if len(hits) == 1:
                return hits.pop()
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
                "by_label": dict(self.by_label),
                "primary_form": self.primary_form,
                "search_node": self.search_node,
                "mode": self.mode,
                "by_line": dict(self.by_line)}

    @classmethod
    def from_dict(cls, data) -> "Wbs | None":
        if not isinstance(data, dict) or not data.get("nodes"):
            return None
        wbs = cls(base=str(data.get("base") or ""),
                  mode=str(data.get("mode") or "site"))
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
        form = str(data.get("primary_form") or "")
        wbs.primary_form = form if form in wbs.nodes else ""
        search = str(data.get("search_node") or "")
        wbs.search_node = search if search in wbs.nodes else ""
        wbs.by_line.update({str(k): str(v) for k, v in
                            (data.get("by_line") or {}).items()
                            if v in wbs.nodes})
        return wbs

    def node_for_line(self, text: str) -> str | None:
        """The heading a requirement line was written under (text mode)."""
        key = _line_key(text)
        if len(key) < 12 or not self.by_line:
            return None
        if key in self.by_line:
            return self.by_line[key]
        # Quoted requirements are cut ("…for the requirement \"Статистика
        # виконання (pass / fail /…"); a long shared prefix is one line.
        stem = key[:40]
        hits = {node for line, node in self.by_line.items()
                if line.startswith(stem) or (len(line) >= 20
                                             and key.startswith(line[:40]))}
        return hits.pop() if len(hits) == 1 else None

    def prompt_lines(self, limit: int = 120) -> list[str]:
        """``id — Path -> To -> Node (url)`` for an LLM to choose from."""
        out = []
        for node in self.ordered()[:limit]:
            path = " -> ".join(self.path(node.id))
            out.append(f"{node.id} — {path}" + (f" ({node.url})" if node.url else ""))
        return out


def build_wbs(pages: list[dict]) -> Wbs:
    """The WBS for one crawl. ``pages[0]`` is the entry page.

    Generic by construction — nothing here knows a site, a language or a
    menu name. The evidence, in order of authority:

    1. **Header menus** (``nav_groups``): each one a module; its links,
       walked in markup order, its pages. A link whose URL is a path prefix
       of the links after it opens a submodule (``/solutions/healthcare`` →
       ``/solutions/healthcare/pharma-app``) — the URL says so, in any
       language. A link to the module's own URL is its overview.
    2. **Plain header links** (a flat nav): a module each.
    3. **Footer links**: into a module's URL space → that module; else the
       Footer.
    4. **No header navigation at all** (a hamburger rendered outside the
       header, an SPA shell, a page the crawler only saw links in): modules
       from the crawled URLs' first path segment, named by the page at that
       URL if it was crawled.
    5. **Crawled pages no menu declares** are instances, not structure: an
       article, a forum topic. They map to their nearest node by URL and
       get no folder of their own; only a page with no ancestor at all is
       listed under "Other pages".
    """
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

    def usable(label: str) -> bool:
        # A long "label" is a promotion or an article headline in a
        # mega-menu (measured on testfort.com and qarea.com), not a page;
        # an e-mail address is a mailto link, not a page; a bracketed
        # placeholder ("[email protected]" from an obfuscating CDN) is not
        # text the site shows anyone.
        return bool(label) and len(label) <= NAME_MAX \
            and not _SKIP_LABELS.match(label) and "@" not in label \
            and not (label.startswith("[") and label.endswith("]"))

    def add_page(parent: str, label: str, href: str, kind: str = "page"):
        label = label.strip()
        if not usable(label) or not same_site(href):
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
        if node.kind == "page" and node.parent \
                and wbs.nodes[node.parent].kind in ("module", "submodule") \
                and wbs.depth(owner) < MAX_DEPTH \
                and norm_path(url, base).startswith(node.url + "/"):
            node.kind = "submodule"
            return owner
        return None

    links = [l for l in (primary.get("header_links") or [])
             if isinstance(l, dict) and label_of(l.get("text"))]
    groups = [g for g in (primary.get("nav_groups") or [])
              if isinstance(g, dict) and g.get("region") == "header"
              and usable(label_of(g.get("label")))]
    head_href = {label_of(l["text"]).casefold(): l.get("href") or ""
                 for l in links if not l.get("in_menu")}
    modules: dict[str, str] = {}
    member: dict[str, str] = {}
    for group in groups:
        label = label_of(group["label"])
        href = head_href.get(label.casefold(), "")
        url = norm_path(href, base) if href and same_site(href) else ""
        if url == "/" or url in wbs.by_url:
            url = ""
        module = wbs.add(WbsNode(f"module:{_slug(label)}", label, "module",
                                 url=url))
        modules[label.casefold()] = module.id
        wbs.by_label.setdefault(label.casefold(), module.id)
        for child in group.get("children") or []:
            member.setdefault(label_of(child).casefold(), label.casefold())

    # 1. Header menus, in markup order. The crawler caps the drop-down
    # children lists; the links are the full record, and each one joins the
    # menu that lists it (or the menu opened last).
    in_menu: list[tuple[str, str, str]] = []
    current: str | None = None
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
        href = link.get("href") or ""
        if same_site(href):
            in_menu.append((module_id, text, norm_path(href, base)))
    menu_texts = {text.casefold() for _, text, _ in in_menu}
    open_parent: dict[str, str] = {}
    for index, (module_id, text, url) in enumerate(in_menu):
        module = wbs.nodes[module_id]
        if not url or url == module.url:
            open_parent[module_id] = module_id      # the module's overview
            if url and url not in wbs.by_url:
                wbs.by_url[url] = module_id
            continue
        parent = open_parent.get(module_id, module_id)
        if parent != module_id and not url.startswith(
                wbs.nodes[parent].url + "/"):
            parent = open_parent[module_id] = module_id
        node_id = add_page(parent, text, url)
        following = [u for m, _, u in in_menu[index + 1:index + 4]
                     if m == module_id]
        if node_id and node_id != HOME \
                and wbs.nodes[node_id].kind == "page" \
                and wbs.depth(node_id) < MAX_DEPTH \
                and any(u.startswith(url + "/") for u in following):
            node = wbs.nodes[node_id]
            node.kind = "submodule"
            node.name = group_name(node.name, node.url) or node.name
            open_parent[module_id] = node_id

    # 2. Plain top-level links → a module each.
    for link in links:
        text = label_of(link["text"])
        if link.get("in_menu") or text.casefold() in modules \
                or text.casefold() in menu_texts:
            continue
        href = link.get("href") or ""
        key = norm_path(href, base) if href and same_site(href) else ""
        if key == "/":
            wbs.by_label.setdefault(text.casefold(), HOME)
            continue
        if not key or key in wbs.by_url or not usable(text):
            continue
        node = wbs.add(WbsNode(f"page:{key}", text, "module", url=key))
        wbs.by_label.setdefault(text.casefold(), node.id)

    # 3. Footer.
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

    from engine.checklist_rules import _page_name

    def page_name(page: dict, key: str) -> str:
        return next((c for c in (
            label_of(_title_key(page.get("title")) and re.split(
                r"\s[|│]\s?|\s[–—]\s", str(page.get("title") or ""))[0]),
            label_of(page.get("h1")),
            re.sub(r"[-_]+", " ", key.rstrip("/").rsplit("/", 1)[-1])
            .strip().capitalize(),
        ) if c and usable(c)), "")

    # 4. No header navigation: the URL structure is the evidence left.
    has_nav = any(n.kind in ("module", "submodule") for n in wbs.nodes.values())
    if not has_nav:
        by_url = {norm_path(str(p.get("url") or ""), base): p for p in pages}
        for key in sorted(k for k in by_url if k and k != "/"):
            first = "/" + key.strip("/").split("/")[0]
            if first not in wbs.by_url:
                page = by_url.get(first)
                name = page_name(page, first) if page else page_name({}, first)
                if name:
                    wbs.add(WbsNode(f"page:{first}", name, "module",
                                    url=first))

    for page in pages:
        if any((f or {}).get("fields") for f in page.get("forms") or []):
            key = norm_path(str(page.get("url") or ""), base)
            wbs.primary_form = HOME if key == "/" else (
                wbs.node_for_url(str(page.get("url") or "")) or "")
            break
    wbs.search_node = _search_home(wbs, pages, base)

    # 5. Crawled pages: map to the nearest node; only orphans are listed.
    other_budget = [MAX_OTHER_PAGES]
    for page in pages:
        url = str(page.get("url") or "")
        if not url:
            continue
        key = norm_path(url, base)
        node_id = HOME if key == "/" else wbs.node_for_url(url)
        if node_id is None:
            if other_budget[0] > 0:
                node_id = add_page(wbs.other(), page_name(page, key), url)
                other_budget[0] -= 1
            node_id = node_id or wbs.other()
        for name in (_page_name(url, page.get("title", "")),
                     page.get("title"), page.get("h1")):
            title = _title_key(name)
            if title:
                wbs.by_title.setdefault(title, node_id)
    return wbs


def surface_urls(wbs: Wbs, pages: list[dict], per_node: int = 2) -> set[str]:
    """The crawled pages whose own content gets test cases.

    Every page with a node of its own, and at most ``per_node`` of the
    instance pages that map onto one node (25 articles share a template;
    walking it twice is coverage, 25 times is duplication).
    """
    out: set[str] = set()
    taken: dict[str, int] = {}
    for page in pages or []:
        url = str((page or {}).get("url") or "")
        if not url:
            continue
        key = norm_path(url, wbs.base)
        node_id = HOME if key == "/" else (wbs.node_for_url(url) or OTHER)
        node = wbs.nodes.get(node_id)
        if node is not None and node.url == key:
            out.add(url)
            continue
        if taken.get(node_id, 0) < per_node:
            taken[node_id] = taken.get(node_id, 0) + 1
            out.add(url)
    return out


# ── attribution ─────────────────────────────────────────────────────

def topic_of(*texts) -> str | None:
    hay = " ".join(str(t or "") for t in texts).casefold()
    for topic, pattern in _TOPICS:
        if re.search(pattern, hay):
            return topic
    return None


def _attribute(wbs: Wbs, item: dict, field_name: str = "summary") -> str:
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
    quoted = re.search(r'requirement "([^"]+)', text)
    for requirement in (hint.get("requirement"),
                        quoted.group(1) if quoted else None):
        if not requirement:
            continue
        page = re.match(r"\s*Page:\s*([^|]+)", requirement)
        found = (wbs.node_for_name(page.group(1)) if page
                 else wbs.node_for_line(requirement))
        if found:
            return found

    # Generic template families that are not site-wide: layout checks are
    # written for "the page" (the entry page), form checks for a form —
    # the first one the crawl found, when it found one.
    if _LAYOUT_SECTION.search(section):
        return HOME
    if _FORM_SECTION.search(section) and wbs.primary_form:
        return wbs.primary_form
    # Search templates belong where the search control is: the Header for
    # a site-wide search box (dou.ua: one form on 47 of 49 pages).
    if _SEARCH_SECTION.search(section) and wbs.search_node:
        return wbs.search_node

    if wbs.mode == "text":
        # A description has no pages to point at; what an item says about
        # itself — its area, then its own words — is the evidence.
        found = wbs.node_for_name(section) or wbs.node_for_name(text)
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
    area = _clean_heading(section.split("—")[0])
    if wbs.mode == "text" and area and not _GENERIC_AREA.match(area):
        # No node names this area: the area is a module of its own.
        # Kind "area": made on demand from a generator's own section name,
        # not declared by the document — not a leaf the baseline owes.
        node = wbs.add(WbsNode(f"text:{_slug(area)}", area, "area"))
        _alias_labels(wbs, node)
        return node.id
    return wbs.cross_cutting("General")


#: Section names generators use for "no particular area" — never a module.
_GENERIC_AREA = re.compile(
    r"^(general|general web|misc|other|requirements?[- ]specific)$", re.I)


def attribute(wbs: Wbs, item: dict, field_name: str = "summary") -> str:
    """The node an item belongs to. Never returns None, and always a node
    of ``wbs`` (a description has no Header or Homepage: the chrome checks
    the web templates still write go to Cross-cutting there)."""
    node = _attribute(wbs, item, field_name)
    if node in wbs.nodes:
        return node
    return wbs.cross_cutting("UI")


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
        # For engine.wbs_baseline.balance, which removes both before save.
        d["_wbs_node"] = node
        surface = (d.get("wbs_hint") or {}).get("surface")
        if surface:
            d["_wbs_surface"] = surface
        d.pop("wbs_hint", None)
        counts[node] = counts.get(node, 0) + 1
    return counts



# ── a product with no crawlable site: the WBS from the input text ─────
#
# Generation from a description (requirements, a spec, an attachment) has
# no navigation to read, but a written description has structure of its
# own: headings, a numbered outline, "Area:" lines. Same rules as the site
# builder — evidence only, depth ≤ 3, names as the author wrote them —
# so an item still gets ``[Module -> Page]``, never "General".

_MD_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_OUTLINE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,2})[.)]?\s+(\S.{0,79})$")
_COLON_HEAD = re.compile(r"^\s*([^\s:][^:]{1,59}):\s*$")
_NUMBERING = re.compile(r"^\s*\d+(?:\.\d+)*[.)]?\s+")
MAX_TEXT_MODULES = 30
MAX_TEXT_PAGES = 30


def _clean_heading(text) -> str:
    text = _NUMBERING.sub("", str(text or ""))
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(":").strip()
    return text[:NAME_MAX].strip()


def _text_headings(lines) -> list[tuple[int, str]]:
    """``[(level, name)]`` — markdown headings, else an outline, else
    ``Area:`` lines. Whichever the text actually uses."""
    lines = [str(l or "") for l in lines or []]
    found = [(len(m.group(1)), m.group(2)) for m in
             (_MD_HEADING.match(l) for l in lines) if m]
    if not found:
        for line in lines:
            m = _OUTLINE.match(line)
            # A heading, not a numbered requirement: short, no sentence.
            if m and not re.search(r"[.;!?]\s*$", m.group(2)) \
                    and len(m.group(2).split()) <= 8:
                found.append((m.group(1).count(".") + 1, m.group(2)))
    if not found:
        found = [(1, m.group(1)) for m in
                 (_COLON_HEAD.match(l) for l in lines) if m]
    return [(level, _clean_heading(name)) for level, name in found
            if _clean_heading(name)]


def _alias_labels(wbs: "Wbs", node: "WbsNode") -> None:
    name = node.name.casefold()
    wbs.by_label.setdefault(name, node.id)
    bare = re.sub(r"\s*[(\[].*?[)\]]\s*", " ", name).strip()
    if bare and bare != name and len(bare) >= 3:
        wbs.by_label.setdefault(bare, node.id)


def build_wbs_from_text(lines) -> "Wbs":
    """The WBS a written description declares. Empty (no modules) when the
    text has no structure at all; attribution then makes a module of each
    generated item's own area rather than filing it under "General"."""
    wbs = Wbs(base="", mode="text")
    heads = _text_headings(lines)
    if not heads:
        return wbs
    levels = sorted({level for level, _ in heads})
    # A level used once is the document's title, not a module.
    while len(levels) > 1 and sum(1 for l, _ in heads if l == levels[0]) == 1:
        levels = levels[1:]
    rank = {level: i for i, level in enumerate(levels[:3])}
    stack: list[str | None] = [None, None, None]
    counts = {"module": 0, "page": 0}
    for level, name in heads:
        if level not in rank:
            continue
        depth = rank[level]
        parent = stack[depth - 1] if depth else None
        if depth and parent is None:
            continue
        kind = "module" if depth == 0 else "page"
        bucket = "module" if depth == 0 else "page"
        if counts[bucket] >= (MAX_TEXT_MODULES if depth == 0
                              else MAX_TEXT_MODULES * MAX_TEXT_PAGES):
            continue
        counts[bucket] += 1
        if parent and wbs.nodes[parent].kind == "page":
            wbs.nodes[parent].kind = "submodule"
        path = "/".join(_slug(n) for n in (
            *(wbs.path(parent) if parent else ()), name))
        node = wbs.add(WbsNode(f"text:{path}", name, kind, parent=parent))
        _alias_labels(wbs, node)
        stack[depth] = node.id
        for deeper in range(depth + 1, 3):
            stack[deeper] = None
    _record_lines(wbs, lines)
    return wbs


def _line_key(text) -> str:
    text = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", str(text or ""))
    text = re.sub(r"[*_`#>]+", "", text)
    return re.sub(r"\s+", " ", text).strip().casefold()[:80]


def _record_lines(wbs: "Wbs", lines) -> None:
    """Each requirement line → the deepest node of the headings above it."""
    by_name = {}
    for node in wbs.ordered():
        by_name.setdefault(node.name, node.id)
    current = None
    for line in lines or []:
        text = str(line or "")
        m = _MD_HEADING.match(text) or _OUTLINE.match(text)
        name = _clean_heading(m.group(2)) if m else ""
        if name and name in by_name:
            current = by_name[name]
            continue
        key = _line_key(text)
        if current and len(key) >= 12:
            wbs.by_line.setdefault(key, current)


_TEXT_PROMPT_VERSION = "wbs_text/1"


def build_wbs_from_text_llm(text: str, *, force: bool = False) -> "Wbs | None":
    """The model's breakdown of a description, or None.

    Asked for modules → pages only; the answer is validated and capped like
    the deterministic one, and an unusable answer returns None so the
    caller keeps :func:`build_wbs_from_text`.
    """
    text = str(text or "").strip()
    if not text:
        return None
    try:
        from engine import wbs_attribution as _attr
        if not (force or _attr._llm_available()):
            return None
        from engine.llm_cache import cached_call
        from engine.llm_client import call_messages
        prompt = (
            "Break this product down into a work breakdown structure for "
            "testing: its modules, and the pages or screens inside each. "
            "Use the names the text itself uses, in its language. Answer "
            "with ONE JSON object {\"modules\": [{\"name\": \"…\", "
            "\"pages\": [\"…\"]}]}. At most 30 modules, 30 pages each; "
            "nothing the text does not describe.\n\n" + text[:12000])
        resp = cached_call(
            "analysis", prompt_version=_TEXT_PROMPT_VERSION,
            call=call_messages, max_tokens=3000,
            accept=lambda t: _parse_modules(t) is not None,
            messages=[{"role": "user", "content": prompt}])
        raw = "\n".join(getattr(b, "text", "") or ""
                        for b in getattr(resp, "content", []) or [])
    except Exception:
        return None
    modules = _parse_modules(raw)
    if not modules:
        return None
    wbs = Wbs(base="", mode="text")
    for module in modules[:MAX_TEXT_MODULES]:
        name = _clean_heading(module.get("name"))
        if not name:
            continue
        mod = wbs.add(WbsNode(f"text:{_slug(name)}", name, "module"))
        _alias_labels(wbs, mod)
        pages = module.get("pages")
        for page in (pages if isinstance(pages, list) else [])[:MAX_TEXT_PAGES]:
            pname = _clean_heading(page.get("name") if isinstance(page, dict)
                                   else page)
            if pname:
                node = wbs.add(WbsNode(f"text:{_slug(name)}/{_slug(pname)}",
                                       pname, "page", parent=mod.id))
                _alias_labels(wbs, node)
    return wbs if wbs.nodes else None


def _parse_modules(text: str):
    import json as _json
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        value = _json.loads(m.group(0))
    except ValueError:
        return None
    modules = value.get("modules") if isinstance(value, dict) else None
    if not isinstance(modules, list) or not all(
            isinstance(x, dict) for x in modules):
        return None
    return modules


__all__ = ["Wbs", "WbsNode", "build_wbs", "surface_urls",
           "build_wbs_from_text", "build_wbs_from_text_llm", "attribute", "stamp", "topic_of",
           "norm_path", "label_of", "HEADER", "HOME", "FOOTER", "OTHER", "XC"]
