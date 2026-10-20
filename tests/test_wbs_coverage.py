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


def _form_nodes(tree, crawl) -> set:
    """Leaves that hold real form or grid cases — from the crawl itself."""
    from engine import tc_rules
    pages = [{k: p.get(k) for k in ("url", "title", "h1", "forms", "tables",
                                    "grid_controls")} for p in crawl["pages"]]
    return {tree.node_for_url(c.page_url) or W.HOME
            for c in tc_rules.enumerate_from_pages(pages)}


def _baseline_floor(tree, crawl, kind) -> dict:
    tcs, checks = BL.baseline(tree, crawl["pages"] if crawl else [])
    return Counter(d["wbs_hint"]["node"]
                   for d in (tcs if kind == "test_case" else checks))


def _assert_even(tree, counts, floor, exempt, label):
    assert counts, label
    zero = [tree.path(n) for n, c in counts.items() if c == 0]
    assert zero == [], (label, zero)
    below = [(tree.path(n), counts[n], floor[n]) for n in counts
             if counts[n] < floor.get(n, 0)]
    assert below == [], (label, below)
    median = statistics.median(counts.values())
    over = [(tree.path(n), c) for n, c in counts.items()
            if n not in exempt and c > BL.BAND * median]
    assert over == [], (label, median, over)


@pytest.mark.parametrize("name", sorted(_SITES))
@pytest.mark.parametrize("kind", ["test_case", "checklist_item"])
def test_every_leaf_is_covered_evenly_on_a_real_site(app, client,
                                                     monkeypatch, name, kind):
    crawl = _crawl(name)
    pid = _generate(client, monkeypatch, name, _SITES[name], crawl=crawl)
    tree, counts = _per_leaf(pid, kind)
    _assert_even(tree, counts, _baseline_floor(tree, crawl, kind),
                 _form_nodes(tree, crawl), f"{name} {kind}")


@pytest.mark.parametrize("kind", ["test_case", "checklist_item"])
def test_every_heading_of_a_description_is_covered_evenly(app, client,
                                                          monkeypatch, kind):
    text = (_FIX / "requirements_testfortge_2026-10-20.md").read_text(
        encoding="utf-8")
    pid = _generate(client, monkeypatch, "text", text)
    tree, counts = _per_leaf(pid, kind)
    _assert_even(tree, counts, _baseline_floor(tree, None, kind), set(),
                 f"text {kind}")


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

    def test_a_form_node_and_a_named_node_are_exempt(self):
        rows = (self._rows("a", 4) + self._rows("b", 4) + self._rows("c", 4)
                + self._rows("f", 1, "form") + self._rows("f", 40)
                + self._rows("n", 40))
        BL.balance(rows, BL.Scope(named={"n"}))
        assert len(rows) == 12 + 41 + 40

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
