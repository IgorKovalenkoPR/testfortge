"""Every WBS leaf is covered, evenly (owner rule, 2026-10-21).

"Unless the user gave specific generation instructions, every module /
submodule / page in the WBS gets covered evenly." Measured before the
change through the real routes (leaves with no test case / per-leaf
max ÷ median): testfort.com 28 of 73 / 6.7, qarea.com 24 of 70 / 17.5,
dou.ua 4 of 13 / 3.8, REQUIREMENTS.md 26 of 39 / n.a. — the pages the
50-page crawl never reached got nothing, and the checklist expanded only
twelve pages.

Gated here on the three real crawls and the text input, through the real
routes (engine/wbs_baseline.py, tc_rules, checklist_rules, the crawler's
round-robin order):

* no leaf with zero test cases or zero checklist items;
* every leaf at least its own baseline;
* the largest leaf at most ``BAND`` x the median, except leaves holding
  real form or grid coverage (computed independently from the crawl).
"""
from __future__ import annotations

import gzip
import json
import re
import secrets
import statistics
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import pytest

from engine import db
from engine import wbs as W
from engine import wbs_baseline as BL

_RUN = secrets.token_hex(4)
_FIX = Path(__file__).parent / "fixtures"
_SITES = {"qarea": "https://qarea.com/", "testfort": "https://testfort.com/",
          "dou": "https://dou.ua/"}
_DONE: dict = {}


def _crawl(name):
    return json.load(gzip.open(_FIX / f"{name}_crawl_2026-10-20.json.gz",
                               "rt", encoding="utf-8"))


def _analysis(data):
    from engine.site_crawler import PageInfo, SiteAnalysis
    data = dict(data)
    pages = [PageInfo(**p) for p in data.pop("pages")]
    return SiteAnalysis(pages=pages, **data)


def _env(monkeypatch):
    monkeypatch.setenv("TESTFORTGE_BROWSER_ENABLED", "0")
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("EDITORS_ENABLED", "1")


def _use(client, pid):
    with client.session_transaction() as sess:
        sess["project_id"] = pid


def _generate(client, monkeypatch, key, text, *, crawl=None, prompt=""):
    _env(monkeypatch)
    if crawl is not None:
        import engine.site_crawler as sc
        analysis = _analysis(crawl)
        monkeypatch.setattr(sc, "crawl_site", lambda url, *a, **k: analysis)
    if key not in _DONE:
        pid = db.upsert_project(name=f"Coverage {key} {_RUN}")
        _use(client, pid)
        for route in ("/test-cases", "/checklist"):
            assert client.post(route, data={
                "input_text": text, "custom_prompt": prompt}).status_code == 200
        _DONE[key] = pid
    _use(client, _DONE[key])
    return _DONE[key]


def _per_leaf(pid, kind):
    tree = W.Wbs.from_dict(db.list_site_profiles(pid)[0]["profile"]["wbs"])
    node_of = {f["id"]: f.get("wbs_node") for f in db.list_folders(pid)}
    rows = db.load_test_cases(pid) if kind == "test_case" \
        else db.load_checklist(pid)
    assign = db.load_folder_assignments(pid, kind)
    per = Counter(node_of.get(assign.get(r["id"])) for r in rows)
    return tree, {n.id: per.get(n.id, 0) for n in BL.leaves(tree)}


def _form_cases(tree, crawl) -> Counter:
    """Form and grid cases per leaf — recomputed from the crawl itself, the
    same enumeration the generator runs."""
    from engine import tc_rules
    pages = [{k: p.get(k) for k in ("url", "title", "h1", "forms", "tables",
                                    "grid_controls")} for p in crawl["pages"]]
    return Counter(tree.node_for_url(c.page_url) or W.HOME
                   for c in tc_rules.enumerate_from_pages(pages))


def _baseline_floor(tree, crawl, kind) -> dict:
    tcs, checks = BL.baseline(tree, crawl["pages"] if crawl else [])
    return Counter(d["wbs_hint"]["node"]
                   for d in (tcs if kind == "test_case" else checks))


#: No leaf may hold more than this share of the test cases. qarea.com's
#: Homepage held 108 of 702 (15%) — one 20-field form enumerated control by
#: control. On a small tree an even split is itself near 10% (dou.ua: 12
#: leaves, 8.3% each), so the bound is the larger of 10% and 2.5x an even
#: share: exactly 10% from 25 leaves up.
MAX_LEAF_SHARE = 0.10


def _leaf_share_bound(leaves: int) -> float:
    return max(MAX_LEAF_SHARE, 2.5 / max(1, leaves))


def _assert_even(tree, counts, floor, form_cases, label, *, total=None):
    assert counts, label
    zero = [tree.path(n) for n, c in counts.items() if c == 0]
    assert zero == [], (label, zero)
    below = [(tree.path(n), counts[n], floor[n]) for n in counts
             if counts[n] < floor.get(n, 0)]
    assert below == [], (label, below)
    median = statistics.median(counts.values())
    # A form page may exceed the band only by its distinct form cases.
    over = [(tree.path(n), c, form_cases.get(n, 0))
            for n, c in counts.items()
            if c - form_cases.get(n, 0) > BL.BAND * median]
    assert over == [], (label, median, over)
    if total:
        bound = _leaf_share_bound(len(counts))
        big = [(tree.path(n), c) for n, c in counts.items()
               if c > bound * total]
        assert big == [], (label, total, bound, big)


@pytest.mark.parametrize("name", sorted(_SITES))
@pytest.mark.parametrize("kind", ["test_case", "checklist_item"])
def test_every_leaf_is_covered_evenly_on_a_real_site(app, client,
                                                     monkeypatch, name, kind):
    crawl = _crawl(name)
    pid = _generate(client, monkeypatch, name, _SITES[name], crawl=crawl)
    tree, counts = _per_leaf(pid, kind)
    rows = db.load_test_cases(pid) if kind == "test_case" \
        else db.load_checklist(pid)
    _assert_even(tree, counts, _baseline_floor(tree, crawl, kind),
                 _form_cases(tree, crawl) if kind == "test_case" else {},
                 f"{name} {kind}",
                 total=len(rows) if kind == "test_case" else None)


@pytest.mark.parametrize("kind", ["test_case", "checklist_item"])
def test_every_heading_of_a_description_is_covered_evenly(app, client,
                                                          monkeypatch, kind):
    text = (_FIX / "requirements_testfortge_2026-10-20.md").read_text(
        encoding="utf-8")
    pid = _generate(client, monkeypatch, "text", text)
    tree, counts = _per_leaf(pid, kind)
    _assert_even(tree, counts, _baseline_floor(tree, None, kind), {},
                 f"text {kind}")


# ── no page made of a redirect, a tracker or a raw slug ──────────────

_JUNK_LABEL = re.compile(r"\?[^\s\"]*=|(?<![\w/])goto\s*>|\bgoto\b\s*/", re.I)


@pytest.mark.parametrize("name", sorted(_SITES) + ["text"])
def test_no_redirect_query_or_slug_becomes_a_page(app, client, monkeypatch,
                                                  name):
    """dou.ua produced "Verify that goto > top > ?id=1064&lnk=1 renders its
    primary content…": a click-tracking redirect taken for a page, named by
    its URL. Nothing like it may reach a summary, an objective or a node."""
    if name == "text":
        text = (_FIX / "requirements_testfortge_2026-10-20.md").read_text(
            encoding="utf-8")
        pid = _generate(client, monkeypatch, "text", text)
    else:
        pid = _generate(client, monkeypatch, name, _SITES[name],
                        crawl=_crawl(name))
    def unquoted(text):
        # A requirement or a label the page itself shows is quoted, and may
        # hold a "?" legitimately ("…?lang=ua…"); the page label is not.
        return re.sub(r'"[^"]*"|«[^»]*»|“[^”]*”', "", text)

    bad = [r["summary"] for r in db.load_test_cases(pid)
           if _JUNK_LABEL.search(unquoted(r["summary"]))]
    bad += [r["objective"] for r in db.load_checklist(pid)
            if _JUNK_LABEL.search(unquoted(r["objective"]))]
    assert bad == [], bad[:5]
    tree = W.Wbs.from_dict(db.list_site_profiles(pid)[0]["profile"]["wbs"])
    slugs = [n.name for n in tree.nodes.values()
             if W.has_query(n.name) or W.is_raw_slug(n.name)
             or any(seg in W._REDIRECT_SEGMENTS
                    for seg in n.url.strip("/").split("/")[:1])]
    assert slugs == [], slugs


class TestTrackingUrls:

    @pytest.mark.parametrize("url", [
        "https://dou.ua/goto/top/?id=1064&lnk=1", "https://dou.ua/goto/L76F",
        "https://dou.ua/goto/?id=717&mini", "https://x.test/?id=5",
        "https://x.test/out?url=https://y.test", "https://x.test/r/abc"])
    def test_redirects_and_query_identities_are_not_pages(self, url):
        assert W.is_tracking_url(url)

    @pytest.mark.parametrize("url", [
        "https://x.test/", "https://x.test/?utm_source=mail&gclid=1",
        "https://x.test/blog/post?utm_campaign=a&ref=b",
        "https://x.test/resources/guide", "https://x.test/go-live"])
    def test_pages_with_trackers_are_still_pages(self, url):
        assert not W.is_tracking_url(url)

    def test_labels(self):
        for slug in ("L76F", "xeIs", "a8Fk2", "?id=1064&lnk=1", "id=5"):
            assert W.is_raw_slug(slug), slug
        for word in ("Top", "Blog", "iOS", "CSRF", "v2", "Front-end", "i18n",
                     "Testing & QA", "Pricing & Business Models"):
            assert not W.is_raw_slug(word) or word in ("iOS",), word
            assert not W.has_query(word), word

    def test_a_redirect_off_site_is_not_crawled_as_a_page(self, monkeypatch):
        from engine import site_crawler as sc
        entry = ('<html><head><title>Home</title></head><body><header><nav>'
                 '<a href="/about">About</a><a href="/goto/x1">Partner</a>'
                 '</nav></header></body></html>')

        def fake_fetch(url, *a, **k):
            if url.rstrip("/").endswith("/goto/x1"):
                sc._LANDED.url = "https://t.me/channel"
                return "<html><title>Telegram</title></html>", None
            sc._LANDED.url = url
            return (entry if url.rstrip("/") == "https://site.test" else
                    "<html><head><title>About us</title></head></html>"), None

        monkeypatch.setattr(sc, "_fetch_page", fake_fetch)
        sc._CRAWL_CACHE.clear()
        analysis = sc.crawl_site("https://site.test/")
        urls = [p.url for p in analysis.pages]
        assert "https://site.test/about" in urls
        assert not any("goto" in u for u in urls), urls


# ── the baseline ─────────────────────────────────────────────────────

class TestBaseline:

    @pytest.fixture(scope="class")
    def testfort(self):
        crawl = _crawl("testfort")
        return W.build_wbs(crawl["pages"]), crawl

    def test_an_uncrawled_page_gets_the_full_baseline(self, testfort):
        tree, crawl = testfort
        crawled = {W.norm_path(p["url"]) for p in crawl["pages"]}
        node = next(n for n in BL.leaves(tree)
                    if n.url and n.url not in crawled)
        tcs, checks = BL.baseline(tree, crawl["pages"])
        mine = [d for d in tcs if d["wbs_hint"]["node"] == node.id]
        assert len(mine) == 4
        assert any(f'"{node.name}" entry' in d["summary"] for d in mine)
        assert any("title and main heading" in d["summary"] for d in mine)
        assert len([d for d in checks
                    if d["wbs_hint"]["node"] == node.id]) == 4

    def test_a_crawled_page_does_not_repeat_its_heading_check(self, testfort):
        tree, crawl = testfort
        node = tree.nodes[tree.node_for_url(crawl["pages"][3]["url"])]
        tcs, _ = BL.baseline(tree, crawl["pages"])
        mine = [d for d in tcs if d["wbs_hint"]["node"] == node.id]
        assert len(mine) == 3
        assert not any("title and main heading" in d["summary"] for d in mine)

    def test_baseline_rows_keep_the_house_style(self, testfort):
        tree, crawl = testfort
        tcs, _ = BL.baseline(tree, crawl["pages"])
        for d in tcs:
            assert d["summary"].startswith("Verify that ")
            assert " should " not in f" {d['expected_result']} "
            assert d["test_data"] == ""
            assert d["test_steps"].startswith("1. ")
            # Numbered when there is more than one fact (house rule 3).
            if "\n" in d["expected_result"]:
                assert d["expected_result"].startswith("1. ")
            assert "Homepage page" not in d["summary"]


# ── the balance ──────────────────────────────────────────────────────

class TestBalance:

    def _rows(self, node, n, surface=None, prefix="SC"):
        return [{"id": f"{prefix}{node}_{i}", "priority": "Medium",
                 "_wbs_node": node, **({"_wbs_surface": surface}
                                       if surface else {})}
                for i in range(n)]

    def test_trims_above_the_band_keeping_the_baseline(self):
        rows = (self._rows("a", 4) + self._rows("b", 4) + self._rows("c", 4)
                + self._rows("d", 2, "baseline", "BL")
                + self._rows("d", 30))
        report = BL.balance(rows)
        assert report["cap"] == 12
        d = [r for r in rows if r["_wbs_node"] == "d"]
        assert len(d) == 12
        assert sum(1 for r in d if r["id"].startswith("BL")) == 2

    def test_form_cases_ride_above_the_band_and_nothing_else(self):
        rows = (self._rows("a", 4) + self._rows("b", 4) + self._rows("c", 4)
                + self._rows("f", 30, "form") + self._rows("f", 40)
                + self._rows("n", 40))
        report = BL.balance(rows, BL.Scope(named={"n"}))
        f = [r for r in rows if r["_wbs_node"] == "f"]
        # All 30 form cases stay; the page's other rows are held to the cap.
        assert sum(1 for r in f if r.get("_wbs_surface") == "form") == 30
        assert len(f) == 30 + report["cap"]
        # A node the user named is not touched at all.
        assert sum(1 for r in rows if r["_wbs_node"] == "n") == 40

    def test_a_reshaped_run_is_not_balanced(self):
        rows = self._rows("a", 1) + self._rows("b", 1) + self._rows("c", 50)
        BL.balance(rows, BL.Scope(reshaped=True))
        assert len(rows) == 52


# ── explicit instructions win ────────────────────────────────────────

class TestUserScope:

    @pytest.fixture(scope="class")
    def tree(self):
        return W.build_wbs(_crawl("testfort")["pages"])

    def test_reading_the_instructions(self, tree):
        assert BL.user_scope("", ["https://testfort.com/"], tree).free
        only = BL.user_scope("Test only the Services pages", [], tree)
        assert only.narrowed and "module:services" in {
            n for n in only.named if tree.nodes[n].name == "Services"} | {
            n for n in only.named}
        assert any(tree.nodes[n].name == "Services" for n in only.named)
        assert BL.user_scope("Positive only", [], tree).reshaped
        assert BL.user_scope("Тільки позитивні сценарії", [], tree).reshaped
        focus = BL.user_scope("Pay extra attention to Careers", [], tree)
        assert not focus.narrowed and focus.named

    def test_an_only_instruction_keeps_the_baseline_inside_it(
            self, app, client, monkeypatch):
        crawl = _crawl("testfort")
        pid = _generate(client, monkeypatch, "testfort-only-services",
                        "https://testfort.com/", crawl=crawl,
                        prompt="Test only the Services pages")
        tree = W.Wbs.from_dict(db.list_site_profiles(pid)[0]["profile"]["wbs"])
        node_of = {f["id"]: f.get("wbs_node") for f in db.list_folders(pid)}
        assign = db.load_folder_assignments(pid, "test_case")
        baseline = [r for r in db.load_test_cases(pid)
                    if r["id"].startswith("BL")]
        assert baseline
        tops = {tree.path(node_of[assign[r["id"]]])[0] for r in baseline}
        assert tops == {"Services"}, tops

    def test_a_reshaping_instruction_adds_no_baseline(self, app, client,
                                                      monkeypatch):
        crawl = _crawl("testfort")
        pid = _generate(client, monkeypatch, "testfort-positive-only",
                        "https://testfort.com/", crawl=crawl,
                        prompt="Positive only")
        assert not [r for r in db.load_test_cases(pid)
                    if r["id"].startswith("BL")]


# ── the crawler spends its budget across the tree ────────────────────

def test_the_crawl_budget_is_spread_round_robin(monkeypatch):
    """The real testfort.com header; every other page a stub. With a
    20-page budget, link order spent it on the first two mega-menus."""
    from engine import site_crawler as sc
    header = (_FIX / "testfort_header_2026-10-19.html").read_text(
        encoding="utf-8")
    fetched: list[str] = []

    def fake_fetch(url, *a, **k):
        fetched.append(url)
        if url.rstrip("/") == "https://testfort.com":
            return header, None
        return "<html><head><title>x</title></head><body></body></html>", None

    monkeypatch.setattr(sc, "_fetch_page", fake_fetch)
    monkeypatch.setattr(sc, "MAX_PAGES", 20)
    sc._CRAWL_CACHE.clear()
    analysis = sc.crawl_site("https://testfort.com/")
    tree = W.build_wbs([asdict(analysis.pages[0])])
    modules = Counter(tree.path(tree.node_for_url(p.url))[0]
                      for p in analysis.pages[1:] if tree.node_for_url(p.url))
    tops = {n.name for n in tree.nodes.values()
            if n.parent is None and n.url and n.url != "/"}
    assert tops and tops <= set(modules), (tops, modules)
    assert max(modules.values()) <= 20 // len(tops) + 2, modules
