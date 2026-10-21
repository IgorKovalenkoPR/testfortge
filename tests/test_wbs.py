"""The product WBS that generation files into (owner decision, 2026-10-20).

Generation first breaks the product down into modules → submodules → pages
(engine/wbs.py), writes each item's place as its summary prefix, and files it
in the folder for that node. Measured before this on a real qarea.com crawl
through the real routes: 752 of 817 test cases in "General", 65 Unfiled.

Everything here runs on real markup — the qarea.com crawl captured
2026-10-20 (50 pages, gzipped) and the testfort.com header from 2026-10-19 —
because synthetic menus have hidden live-page defects in this codebase three
times running (and did again: the crawler's 20-entry menu cap cut qarea.com's
AI and Resources menus, which only the real header showed).
"""
from __future__ import annotations

import gzip
import json
import secrets
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import pytest

from engine import db
from engine import folder_prefix as fpx
from engine import wbs as W

_RUN = secrets.token_hex(4)
_FIX = Path(__file__).parent / "fixtures"
QAREA = _FIX / "qarea_crawl_2026-10-20.json.gz"
TESTFORT = _FIX / "testfort_header_2026-10-19.html"


# ── fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qarea() -> dict:
    return json.load(gzip.open(QAREA, "rt", encoding="utf-8"))


@pytest.fixture(scope="module")
def qarea_tree(qarea) -> W.Wbs:
    return W.build_wbs(qarea["pages"])


@pytest.fixture(scope="module")
def testfort_page() -> dict:
    from engine.site_crawler import _parse_page
    return asdict(_parse_page(TESTFORT.read_text(encoding="utf-8"),
                              "https://testfort.com/", "testfort.com"))


def _analysis(data: dict):
    from engine.site_crawler import PageInfo, SiteAnalysis
    data = dict(data)
    pages = [PageInfo(**p) for p in data.pop("pages")]
    return SiteAnalysis(pages=pages, **data)


def _paths(tree: W.Wbs) -> set:
    return {tree.path(n.id) for n in tree.nodes.values()}


# ── the tree, on real markup ─────────────────────────────────────────

class TestTreeOnQareaCom:

    def test_modules_in_the_order_the_header_shows_them(self, qarea_tree):
        top = [n.name for n in qarea_tree.ordered() if n.parent is None]
        assert top == ["Header", "Homepage", "Company", "Services",
                       "Solutions", "Resources", "Footer"]

    def test_the_owners_example_path_exists(self, qarea_tree):
        node = qarea_tree.node_for_url(
            "https://qarea.com/services/web-development")
        assert qarea_tree.path(node) == ("Services", "Web Development")

    def test_submodules_from_the_menu_and_from_the_url(self, qarea_tree):
        paths = _paths(qarea_tree)
        # A menu link whose URL prefixes the links after it opens a
        # submodule — the URL says so in any language; its "All …" landing
        # label names the group without the phrase (wbs.group_name).
        assert ("Solutions", "Healthcare Solutions", "Pharma App") in paths
        # The AI entries' URLs share no prefix with their heading's link,
        # so the URL rule leaves them flat under the menu that lists them.
        assert ("Solutions", "Generative AI") in paths
        # A menu page with pages under its URL becomes a submodule.
        assert ("Solutions", "Fitness", "Wellness App") in paths
        # Past the crawler's old 20-entry cap — the reason it was raised.
        assert ("Solutions", "Fintech Solutuions",
                "Accounting Software") in paths

    def test_footer_links_into_a_module_join_the_module(self, qarea_tree):
        assert ("Services", "DevOps") in _paths(qarea_tree)
        assert ("Footer", "Privacy Policy") in _paths(qarea_tree)

    def test_depth_names_and_identity_rules(self, qarea_tree):
        urls = [n.url for n in qarea_tree.nodes.values() if n.url]
        assert len(urls) == len(set(urls)), "one node per page"
        for node in qarea_tree.nodes.values():
            assert qarea_tree.depth(node.id) <= W.MAX_DEPTH, node
            assert "\n" not in node.name and len(node.name) <= W.NAME_MAX + 4
        # Mega-menu promotions are not pages.
        assert not any("Discover how" in n.name or "HuffPost" in n.name
                       for n in qarea_tree.nodes.values())

    def test_the_same_page_gets_the_same_node_however_spelled(self,
                                                              qarea_tree):
        expected = qarea_tree.node_for_url("https://qarea.com/careers")
        for spelling in ("https://qarea.com/careers/", "/careers",
                         "https://qarea.com/careers/index.html",
                         "https://QAREA.com/Careers"):
            assert qarea_tree.node_for_url(spelling) == expected, spelling
        # A page under a known one belongs to it.
        assert qarea_tree.node_for_url(
            "https://qarea.com/services/web-development/react") == \
            qarea_tree.node_for_url("https://qarea.com/services/web-development")
        assert qarea_tree.node_for_url("https://elsewhere.com/careers") is None

    def test_rebuilding_gives_identical_ids_and_paths(self, qarea):
        one, two = W.build_wbs(qarea["pages"]), W.build_wbs(qarea["pages"])
        assert [(n.id, one.path(n.id)) for n in one.ordered()] == \
            [(n.id, two.path(n.id)) for n in two.ordered()]

    def test_round_trips_through_its_stored_form(self, qarea_tree):
        back = W.Wbs.from_dict(json.loads(json.dumps(qarea_tree.to_dict())))
        assert [(n.id, back.path(n.id)) for n in back.ordered()] == \
            [(n.id, qarea_tree.path(n.id)) for n in qarea_tree.ordered()]
        assert back.node_for_name("Our Team") == \
            qarea_tree.node_for_name("Our Team")


class TestTreeOnTestfortCom:

    def test_modules_pages_and_a_plain_link(self, testfort_page):
        tree = W.build_wbs([testfort_page])
        paths = _paths(tree)
        assert ("Why Us", "Careers") in paths
        assert ("Services", "Manual Testing") in paths
        assert ("Industries", "Fintech") in paths
        assert ("Contact us",) in paths           # a plain top-level link
        assert {("Header",), ("Homepage",), ("Footer",)} <= paths

    def test_no_review_quote_or_case_study_becomes_a_page(self,
                                                          testfort_page):
        names = [n.name for n in W.build_wbs([testfort_page]).nodes.values()]
        assert not any("impressed" in n or "LuxiEdge" in n for n in names)


# ── attribution ──────────────────────────────────────────────────────

class TestAttribute:

    def _a(self, tree, **item):
        return tree.path(W.attribute(tree, item))

    def test_hints_the_generator_recorded_come_first(self, qarea_tree):
        node = qarea_tree.node_for_url("https://qarea.com/our-team")
        assert W.attribute(qarea_tree, {"wbs_hint": {"node": node},
                                        "section": "Footer"}) == node
        # An id the model made up is ignored, not trusted.
        assert self._a(qarea_tree, wbs_hint={"node": "page:/nope",
                                             "region": "footer"}) == ("Footer",)
        assert self._a(qarea_tree, wbs_hint={"region": "header"},
                       section="Page Content — Our Team") == ("Header",)
        assert self._a(qarea_tree, wbs_hint={
            "page_url": "https://qarea.com/"}) == ("Homepage",)
        assert self._a(qarea_tree, wbs_hint={
            "page_url": "https://qarea.com/services/web-development"}) == \
            ("Services", "Web Development")

    def test_sections_naming_a_page_or_the_chrome(self, qarea_tree):
        assert self._a(qarea_tree, section="Page: Our Team") == \
            ("Company", "Our team")
        assert self._a(qarea_tree,
                       section="Page Content — Web Development Outsourcing "
                               "Company with Domain Experts") == \
            ("Services", "Web Development")
        assert self._a(qarea_tree, section="Page Content") == ("Homepage",)
        assert self._a(qarea_tree, section="Header & Navigation") == ("Header",)
        assert self._a(qarea_tree, section="Site Navigation") == ("Header",)
        assert self._a(qarea_tree, section="Footer") == ("Footer",)

    def test_url_patterns(self, qarea_tree):
        assert self._a(qarea_tree, url_pattern="https://qarea.com/careers") \
            == ("Company", "Careers")
        assert self._a(qarea_tree, url_pattern="*/careers*") == \
            ("Company", "Careers")
        # The site root as a pattern is how site-wide checks are written.
        assert self._a(qarea_tree, url_pattern="https://qarea.com",
                       section="SEO") == ("Cross-cutting", "SEO")
        assert self._a(qarea_tree, url_pattern="https://qarea.com",
                       section="General Web") == ("Homepage",)
        # A glob over every page says nothing about which one.
        assert self._a(qarea_tree, url_pattern="*", section="Security") == \
            ("Cross-cutting", "Security")

    def test_a_requirement_quoting_its_page(self, qarea_tree):
        text = ('Verify that valid input is accepted for the requirement '
                '"Page: Our Pricing Models | H1: How We Work"')
        assert self._a(qarea_tree, section="Requirements-specific",
                       summary=text) == ("Company", "Pricing models")

    def test_nothing_to_go_on_is_cross_cutting_general(self, qarea_tree):
        assert self._a(qarea_tree, section="Misc", summary="Verify that x") \
            == ("Cross-cutting", "General")

    def test_stamp_writes_the_prefix_and_drops_the_hint(self, qarea_tree):
        d = {"summary": "[Old] Verify that x", "wbs_hint": {
            "page_url": "https://qarea.com/careers"}}
        counts = W.stamp(qarea_tree, lambda n: (7, ("Company", "Careers")),
                         [d], "test_case")
        # ``_wbs_node`` is for engine.wbs_baseline.balance, which removes it
        # before the pack is saved.
        assert d == {"summary": "[Company -> Careers] Verify that x",
                     "folder_id": 7, "_wbs_node": "page:/careers"}
        assert sum(counts.values()) == 1


# ── generators record where an item came from ────────────────────────

class TestGeneratorsRecordTheirPage:

    def test_rule_enumerated_cases_know_their_page(self, qarea):
        from engine import tc_rules
        pages = [{"url": p["url"], "title": p["title"], "h1": p["h1"],
                  "forms": p["forms"], "tables": p["tables"],
                  "grid_controls": p["grid_controls"]}
                 for p in qarea["pages"]]
        cases = tc_rules.enumerate_from_pages(pages)
        assert cases and all(c.page_url.startswith("https://qarea.com")
                             for c in cases)
        assert c_url_pattern_untouched(cases)

    def test_checklist_sections_carry_page_and_region(self, qarea):
        from engine import checklist_rules as rules
        items = rules.to_checklist_items(rules.build_checklist(
            qarea["pages"], url="https://qarea.com/"))
        by_section = {i.section: i.wbs_hint for i in items}
        assert by_section["Header"] == {"region": "header"}
        assert by_section["Footer"] == {"region": "footer"}
        content = [h for s, h in by_section.items()
                   if s.startswith("Page Content")]
        assert content and all(h.get("page_url") for h in content)

    def test_the_model_is_given_the_node_ids_and_they_are_read_back(
            self, qarea_tree):
        from engine import tc_author, test_strategy
        lines = qarea_tree.prompt_lines()
        assert any(l.startswith("page:/services/web-development — "
                                "Services -> Web Development") for l in lines)
        prompt = tc_author._build_user_prompt(
            None, None, tc_author.Artifacts(url="https://qarea.com",
                                            wbs_nodes=lines))
        assert "page:/services/web-development" in prompt
        case = tc_author._coerce_case({"summary": "Verify that x",
                                       "wbs": "page:/careers"})
        assert case.wbs_node == "page:/careers"
        from engine.site_recon import SiteProfile
        sprompt = test_strategy._build_user_prompt(
            SiteProfile(url="https://qarea.com"), lines)
        assert "page:/careers" in sprompt and '"wbs"' in sprompt
        assert test_strategy._coerce_check(
            {"objective": "x", "wbs": "home"}).wbs_node == "home"


def c_url_pattern_untouched(cases) -> bool:
    """Filing is not a walkthrough opt-in: url_pattern stays as it was."""
    return all(not c.url_pattern for c in cases)


# ── folders bound to nodes ───────────────────────────────────────────

@pytest.fixture
def project(app, request):
    return db.upsert_project(name=f"WBS {request.node.name} {_RUN}"[:180])


def _folder_path(project, folder_id):
    by_id = {f["id"]: f for f in db.list_folders(project)}
    names = []
    while folder_id is not None and folder_id in by_id:
        names.insert(0, by_id[folder_id]["name"])
        folder_id = by_id[folder_id]["parent_id"]
    return tuple(names)


class TestFoldersFollowNodes:

    def test_every_node_gets_a_folder_once(self, project, qarea_tree):
        first = db.ensure_wbs_folders(project, qarea_tree)
        again = db.ensure_wbs_folders(project, qarea_tree)
        assert first == again
        # Cross-cutting folders appear only once something lands in them.
        assert len(db.list_folders(project)) == len(
            [n for n in qarea_tree.nodes.values() if n.kind != "crosscutting"])
        for node_id, (fid, path) in first.items():
            assert path == qarea_tree.path(node_id)

    def test_a_renamed_or_moved_folder_keeps_its_node(self, project,
                                                      qarea_tree):
        folders = db.ensure_wbs_folders(project, qarea_tree)
        web = qarea_tree.node_for_url("https://qarea.com/services/web-development")
        fid = folders[web][0]
        db.rename_folder(project, fid, "Web Dev")
        mine = db.create_folder(project, "Mine")
        db.move_folder(project, fid, mine["id"])

        d = {"summary": "Verify that x", "wbs_hint": {
            "page_url": "https://qarea.com/services/web-development"}}
        db.apply_wbs(project, qarea_tree, [d], "test_case")
        assert d["folder_id"] == fid
        assert d["summary"] == "[Mine -> Web Dev] Verify that x"
        assert len([f for f in db.list_folders(project)
                    if f["name"] == "Web Development"]) == 0

    def test_an_existing_folder_of_the_same_name_is_adopted(self, project,
                                                            qarea_tree):
        header = db.create_folder(project, "header")
        folders = db.ensure_wbs_folders(project, qarea_tree)
        assert folders[W.HEADER][0] == header["id"]

    def test_stale_empty_auto_folders_are_pruned_and_nothing_else(
            self, project):
        old = db.ensure_folder_path(project, ["Header", "Services",
                                              "Web Development page"])
        mine = db.create_folder(project, "Kept by hand")
        used = db.ensure_folder_path(project, ["General"])
        db.save_test_cases(project, [_case("TC-1", folder_id=used)])
        assert db.prune_stale_auto_folders(project) == 3
        names = {f["name"] for f in db.list_folders(project)}
        assert names == {"Kept by hand", "General"}
        assert old not in {f["id"] for f in db.list_folders(project)}
        assert mine


def _case(external_id, summary="Verify that x", **over):
    row = {"id": external_id, "section": "Auth", "section_num": 1,
           "summary": summary, "preconditions": "", "test_steps": "1. Open",
           "test_data": "", "expected_result": "Opens", "issues": "",
           "comment": "", "user_story_id": "", "category": "Positive",
           "priority": "High", "status": "Unchecked",
           "testing_type": "Functional"}
    row.update(over)
    return row


def _item(external_id, objective="Verify that x", section="Header"):
    return {"id": external_id, "section": section, "objective": objective,
            "category": "Positive", "priority": "High",
            "status": "Unchecked", "item_num": "", "depth": 2}


# ── "Attribute to WBS" on a project generated before the WBS ─────────

@pytest.fixture
def editing_on(monkeypatch):
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("EDITORS_ENABLED", "1")
    return True


@pytest.fixture
def old_project(project, qarea_tree):
    """What the dev QArea project looked like: General + Unfiled."""
    general = db.ensure_folder_path(project, ["General"])
    stale = db.ensure_folder_path(project, ["Header", "Company",
                                            "Our team page"])
    db.save_test_cases(project, [
        _case("SA10_001", "[General] Verify that the First name field is "
              "displayed", section="QArea SoftwareDevelopment Team",
              test_steps="1. Go to https://qarea.com/our-team -> QArea "
                         "SoftwareDevelopment Team\n2. Look",
              folder_id=general),
        _case("SC9_001", "Verify that contact us renders its content",
              section="Page: Contact Us",
              url_pattern="https://qarea.com/contact-us"),
        # Legacy site-wide checks name the site root in their
        # preconditions; found in the browser putting them on Homepage.
        _case("SC16_001", "Verify that every page has a unique title",
              section="SEO", url_pattern="https://qarea.com",
              preconditions="Site is reachable at https://qarea.com."),
        _case("SC1_001", "Verify that the primary navigation exposes the "
              "documented sections", section="Site Navigation",
              url_pattern="https://qarea.com",
              preconditions="Site is reachable at https://qarea.com."),
    ])
    db.save_checklist(project, [
        _item("CNT_001", section="Page Content — Our Team"),
        _item("HDR_001", section="Header"),
    ])
    # A fresh tree: the module fixture is shared, and attribution adds
    # Cross-cutting nodes to whatever tree it is given.
    fresh = W.Wbs.from_dict(json.loads(json.dumps(qarea_tree.to_dict())))
    fresh.cross_cutting("General")     # as a stored WBS from earlier runs
    db.save_site_profile(project, "https://qarea.com/",
                         {"url": "https://qarea.com/",
                          "wbs": fresh.to_dict()}, {})
    return project, stale


def _use(client, project):
    with client.session_transaction() as sess:
        sess["project_id"] = project


class TestAttributeAction:

    def test_preview_counts_and_writes_nothing(self, client, old_project,
                                               editing_on):
        project, _ = old_project
        before = db.load_test_cases(project)
        _use(client, project)
        resp = client.post("/api/folders/attribute", json={})
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["preview"] is True and body["source"] == "stored"
        tc = body["kinds"]["test_case"]
        assert tc["total"] == 4 and tc["changed"] == 4
        assert sorted(map(tuple, tc["by_node"])) == [
            ("Company > Contact Us", 1), ("Company > Our team", 1),
            ("Cross-cutting > SEO", 1), ("Header", 1)]
        assert db.load_test_cases(project) == before

    def test_apply_files_everything_and_prefix_equals_folder(
            self, client, old_project, editing_on):
        project, stale = old_project
        _use(client, project)
        resp = client.post("/api/folders/attribute", json={"apply": True})
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["written"] == {"test_case": 4, "checklist_item": 2}
        assert body["pruned_folders"] >= 1
        assert stale not in {f["id"] for f in db.list_folders(project)}
        # Nothing landed in Cross-cutting > General, so no such folder —
        # and the old top-level "General" went with the other leftovers.
        folders = {f["id"]: f for f in db.list_folders(project)}
        general = [
            (f, _folder_path(project, f["id"]),
             [k for k in ("test_case", "checklist_item")
              if f["id"] in db.load_folder_assignments(project, k).values()],
             db.load_edit_metadata(project, "test_cases"))
            for f in folders.values() if f["name"] == "General"]
        assert not general, general
        for kind, rows, field in (
                ("test_case", db.load_test_cases(project), "summary"),
                ("checklist_item", db.load_checklist(project), "objective")):
            assign = db.load_folder_assignments(project, kind)
            for row in rows:
                assert row["id"] in assign, row["id"]
                assert fpx.parse(row[field]) == \
                    _folder_path(project, assign[row["id"]]), row
        tcs = {r["id"]: r for r in db.load_test_cases(project)}
        assert tcs["SA10_001"]["summary"] == \
            "[Company -> Our team] Verify that the First name field is " \
            "displayed"

    def test_edited_rows_are_left_alone_unless_included(
            self, client, old_project, editing_on):
        project, _ = old_project
        mine = db.create_folder(project, "Moved by hand")
        assert db.assign_to_folder(project, "test_case", ["SC9_001"],
                                   mine["id"])["changed"] == ["SC9_001"]
        _use(client, project)
        body = client.post("/api/folders/attribute",
                           json={"apply": True}).get_json()
        assert body["kinds"]["test_case"]["skipped_edited"] == 1
        assert db.load_folder_assignments(project, "test_case")[
            "SC9_001"] == mine["id"]
        body = client.post("/api/folders/attribute", json={
            "apply": True, "include_edited": True}).get_json()
        assert _folder_path(project, db.load_folder_assignments(
            project, "test_case")["SC9_001"]) == ("Company", "Contact Us")

    def test_no_structure_and_no_url_is_a_400(self, client, project,
                                              editing_on):
        _use(client, project)
        assert client.post("/api/folders/attribute",
                           json={}).status_code == 400

    def test_the_entry_page_is_enough_without_a_stored_wbs(
            self, client, project, editing_on, qarea, monkeypatch):
        from engine import site_crawler
        html = "<html><header><nav><a href='/careers'>Careers</a></nav>" \
               "</header><footer></footer></html>"
        monkeypatch.setattr(site_crawler, "_fetch_page",
                            lambda url, *a, **k: (html, None))
        db.save_site_profile(project, "https://qarea.com/", {}, {})
        db.save_test_cases(project, [_case(
            "SC1_001", "Verify that careers renders", section="Page: Jobs",
            url_pattern="https://qarea.com/careers")])
        _use(client, project)
        body = client.post("/api/folders/attribute", json={}).get_json()
        assert body["source"] == "entry_page"
        assert body["kinds"]["test_case"]["by_node"] == [["Careers", 1]]

    def test_csrf_is_enforced_and_satisfied(self, app, client, old_project,
                                            editing_on):
        project, _ = old_project
        _use(client, project)
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            assert client.post("/api/folders/attribute",
                               json={}).status_code == 400
            token = client.get("/api/csrf-token").get_json()["token"]
            assert client.post("/api/folders/attribute", json={},
                               headers={"X-CSRFToken": token}
                               ).status_code == 200
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

    def test_the_button_and_its_hidden_panel_render(self, client, project,
                                                    editing_on):
        db.save_test_cases(project, [_case("TC-1")])
        _use(client, project)
        body = client.get("/test-cases").get_data(as_text=True)
        assert body.count("data-folder-attribute") == 1
        assert "data-folder-attr hidden" in body


class TestModelPass:

    def test_only_ids_from_the_list_are_taken(self, qarea_tree, monkeypatch):
        from engine import llm_cache
        from engine import wbs_attribution as attr

        class Resp:
            content = [type("B", (), {"text": json.dumps({
                "A": "page:/careers", "B": "page:/invented", "Z": "home"})})()]
        seen = {}

        def fake(kind, **kw):
            seen["prompt"] = kw["messages"][0]["content"]
            return Resp()
        monkeypatch.setattr(llm_cache, "cached_call", fake)
        out = attr.llm_nodes(qarea_tree, [("A", "Careers form"),
                                          ("B", "Something")], force=True)
        assert out == {"A": "page:/careers"}
        assert "page:/services/web-development" in seen["prompt"]

    def test_no_key_no_call(self, qarea_tree, monkeypatch):
        from engine import wbs_attribution as attr
        monkeypatch.setattr(attr, "_llm_available", lambda: False)
        assert attr.llm_nodes(qarea_tree, [("A", "x")]) == {}


# ── the gate: a real crawl through the real routes ───────────────────

_GENERATED: dict = {}


@pytest.fixture
def generated(app, client, qarea, monkeypatch, editing_on, request):
    """One generation per worker: four tests read it, one regenerates."""
    if "pid" in _GENERATED:
        _use(client, _GENERATED["pid"])
        import engine.site_crawler as sc
        analysis = _analysis(qarea)
        monkeypatch.setattr(sc, "crawl_site", lambda url, *a, **k: analysis)
        monkeypatch.setenv("TESTFORTGE_BROWSER_ENABLED", "0")
        return _GENERATED["pid"]
    import engine.site_crawler as sc
    analysis = _analysis(qarea)
    monkeypatch.setattr(sc, "crawl_site", lambda url, *a, **k: analysis)
    # The legacy stream's in-process browser pass is live network.
    monkeypatch.setenv("TESTFORTGE_BROWSER_ENABLED", "0")
    pid = db.upsert_project(name=f"WBS gate {request.node.name} {_RUN}"[:180])
    _use(client, pid)
    assert client.post("/test-cases", data={
        "input_text": "https://qarea.com/"}).status_code == 200
    assert client.post("/checklist", data={
        "input_text": "https://qarea.com/"}).status_code == 200
    _GENERATED["pid"] = pid
    return pid


def _distribution(pid, kind):
    rows = (db.load_test_cases(pid) if kind == "test_case"
            else db.load_checklist(pid))
    field = "summary" if kind == "test_case" else "objective"
    assign = db.load_folder_assignments(pid, kind)
    return rows, field, assign


class TestGateOnARealCrawl:

    @pytest.mark.parametrize("kind", ["test_case", "checklist_item"])
    def test_unfiled_zero_cross_cutting_small_prefix_is_folder(
            self, generated, kind):
        rows, field, assign = _distribution(generated, kind)
        assert len(rows) > 100
        unfiled = [r["id"] for r in rows if r["id"] not in assign]
        assert unfiled == []
        top = Counter(_folder_path(generated, assign[r["id"]])[0]
                      for r in rows)
        assert top["Cross-cutting"] / len(rows) < 0.15, top
        assert "General" not in top
        bad = [r["id"] for r in rows
               if fpx.parse(r[field]) != _folder_path(generated,
                                                      assign[r["id"]])]
        assert bad == []

    def test_both_streams_are_attributed_to_real_pages(self, generated):
        rows, field, assign = _distribution(generated, "test_case")
        legacy = [r for r in rows if r["id"].startswith("SC")]
        assert legacy and all(r["id"] in assign for r in legacy)
        pages = {_folder_path(generated, assign[r["id"]]) for r in legacy}
        assert ("Company", "Contact Us") in pages
        # A rule-enumerated case sits on the page it was enumerated on — the
        # one its own navigation step opens — not just on a page whose
        # heading happens to share a word with its form.
        import re
        tree = W.Wbs.from_dict(
            db.list_site_profiles(generated)[0]["profile"]["wbs"])
        enumerated = [r for r in rows if r["id"].startswith("SA")]
        assert len(enumerated) > 100
        for r in enumerated:
            url = re.search(r"Go to (https?://\S+)", r["test_steps"]).group(1)
            assert _folder_path(generated, assign[r["id"]]) == \
                tree.path(tree.node_for_url(url)), (r["id"], url)
        cl, _, cl_assign = _distribution(generated, "checklist_item")
        web = [r for r in cl if r[field.replace("summary", "objective")]
               .startswith("[Services -> Web Development] ")]
        assert len(web) > 10
        assert all(_folder_path(generated, cl_assign[r["id"]]) ==
                   ("Services", "Web Development") for r in web)

    def test_the_tree_is_the_wbs_and_is_stored(self, generated, qarea):
        names = {(f["name"], f["parent_id"] is None)
                 for f in db.list_folders(generated)}
        for module in ("Header", "Homepage", "Company", "Services",
                       "Solutions", "Footer"):
            assert (module, True) in names
        stored = db.list_site_profiles(generated)[0]["profile"]["wbs"]
        assert W.Wbs.from_dict(stored).node_for_url(
            "https://qarea.com/careers") == "page:/careers"

    def test_regenerating_reproduces_the_same_filing(self, generated,
                                                     client):
        rows, field, _ = _distribution(generated, "test_case")
        before = sorted((r["id"], r[field]) for r in rows)
        folders = len(db.list_folders(generated))
        client.post("/test-cases", data={"input_text": "https://qarea.com/"})
        rows, field, _ = _distribution(generated, "test_case")
        assert sorted((r["id"], r[field]) for r in rows) == before
        assert len(db.list_folders(generated)) == folders


# ── the same gate on structurally different real sites ───────────────
#
#   qarea.com     header mega-menus with nested groups, one contact form
#                 embedded ~4x on every page (205 forms, 3 field sets)
#   testfort.com  header mega-menus, a 30-entry services menu, big footer
#   dou.ua        Ukrainian, a flat header nav (no drop-downs), ~40
#                 articles and forum topics crawled from the front page

_SITES = {"qarea": "https://qarea.com/", "testfort": "https://testfort.com/",
          "dou": "https://dou.ua/"}
_GATE: dict = {}

#: No single WBS node may hold more than this share of the test cases.
#: Before the fix qarea.com's Homepage held 47% and two Company pages the
#: rest; what remains on the largest node after it is the site's one real
#: form plus that page's own content (22.3% on qarea.com, 10% on
#: testfort.com, 19.9% on dou.ua). A node above a quarter of a multi-page
#: site means coverage collapsed onto it again. For a tree with only a
#: handful of page nodes a quarter is unreachable by construction, so the
#: bound loosens to 1.5x an even split there.
MAX_NODE_SHARE = 0.25


def _site_pid(app, client, monkeypatch, name):
    import engine.site_crawler as sc
    data = json.load(gzip.open(_FIX / f"{name}_crawl_2026-10-20.json.gz",
                               "rt", encoding="utf-8"))
    analysis = _analysis(data)
    monkeypatch.setattr(sc, "crawl_site", lambda url, *a, **k: analysis)
    monkeypatch.setenv("TESTFORTGE_BROWSER_ENABLED", "0")
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("EDITORS_ENABLED", "1")
    if name not in _GATE:
        pid = db.upsert_project(name=f"WBS site gate {name} {_RUN}")
        _use(client, pid)
        for route in ("/test-cases", "/checklist"):
            assert client.post(route, data={
                "input_text": _SITES[name]}).status_code == 200
        _GATE[name] = (pid, data)
    pid, data = _GATE[name]
    _use(client, pid)
    return pid, data


@pytest.mark.parametrize("name", sorted(_SITES))
class TestGateOnEverySite:

    @pytest.mark.parametrize("kind", ["test_case", "checklist_item"])
    def test_unfiled_cross_cutting_and_prefix(self, app, client, monkeypatch,
                                              name, kind):
        pid, _ = _site_pid(app, client, monkeypatch, name)
        rows, field, assign = _distribution(pid, kind)
        assert len(rows) > 100
        assert [r["id"] for r in rows if r["id"] not in assign] == []
        paths = [_folder_path(pid, assign[r["id"]]) for r in rows]
        top = Counter(p[0] for p in paths)
        assert top["Cross-cutting"] / len(rows) < 0.15, top
        assert "General" not in top
        assert [r["id"] for r, p in zip(rows, paths)
                if fpx.parse(r[field]) != p] == []

    def test_every_module_with_crawled_pages_has_test_cases(
            self, app, client, monkeypatch, name):
        pid, data = _site_pid(app, client, monkeypatch, name)
        tree = W.Wbs.from_dict(db.list_site_profiles(pid)[0]["profile"]["wbs"])
        crawled = {tree.path(tree.node_for_url(p["url"]) or W.HOME)[0]
                   for p in data["pages"]}
        rows, _, assign = _distribution(pid, "test_case")
        covered = {_folder_path(pid, fid)[0] for fid in assign.values()}
        assert crawled and crawled <= covered, crawled - covered

    def test_folder_names_are_words_not_decoration(self, app, client,
                                                   monkeypatch, name):
        pid, _ = _site_pid(app, client, monkeypatch, name)
        _assert_clean_names(pid)

    def test_the_header_case_lists_the_real_modules(self, app, client,
                                                    monkeypatch, name):
        # dou.ua listed "the documented sections: Місяць" — a calendar
        # widget's month switcher, taken from some other page's <nav>.
        pid, _ = _site_pid(app, client, monkeypatch, name)
        tree = W.Wbs.from_dict(db.list_site_profiles(pid)[0]["profile"]["wbs"])
        modules = {n.name for n in tree.nodes.values() if n.parent is None}
        header = [r for r in db.load_test_cases(pid)
                  if "primary navigation exposes the documented sections"
                  in r["summary"]]
        assert len(header) == 1
        listed = header[0]["summary"].split("sections:", 1)[1].split("·")
        named = {label.strip() for label in listed} & modules
        assert len(named) >= 3, (listed, sorted(modules))

    def test_no_single_node_holds_more_than_a_quarter(
            self, app, client, monkeypatch, name):
        pid, _ = _site_pid(app, client, monkeypatch, name)
        rows, _, assign = _distribution(pid, "test_case")
        per_node = Counter(assign[r["id"]] for r in rows)
        bound = max(MAX_NODE_SHARE, 1.5 / len(per_node))
        node, top = per_node.most_common(1)[0]
        assert top / len(rows) <= bound, (_folder_path(pid, node),
                                          top, len(rows))


# ── a product with no site: the WBS from the description ─────────────

REQUIREMENTS = _FIX / "requirements_testfortge_2026-10-20.md"


class TestTextInput:

    @pytest.fixture
    def text_pid(self, app, client, monkeypatch):
        monkeypatch.setenv("TESTFORTGE_BROWSER_ENABLED", "0")
        monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
        monkeypatch.setenv("EDITORS_ENABLED", "1")
        if "text" not in _GATE:
            pid = db.upsert_project(name=f"WBS text gate {_RUN}")
            _use(client, pid)
            text = REQUIREMENTS.read_text(encoding="utf-8")
            for route in ("/test-cases", "/checklist"):
                assert client.post(route, data={
                    "input_text": text}).status_code == 200
            _GATE["text"] = pid
        _use(client, _GATE["text"])
        return _GATE["text"]

    def test_the_document_is_not_mistaken_for_a_site(self):
        from routes.generation import _detect_first_url
        lines = REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        # It quotes https://fonts.googleapis.com in a CSP block; that is
        # not the product. Generation crawled Google Fonts before.
        assert _detect_first_url(lines) is None
        assert _detect_first_url(
            ["https://example.test/ is the site under test."]) == \
            "https://example.test/"
        assert _detect_first_url(["Site: https://example.test"]) == \
            "https://example.test"
        assert _detect_first_url(["| CSP | https://x.test |"]) is None
        assert _detect_first_url(["```", "https://x.test", "```"]) is None

    def test_headings_become_modules_and_pages(self):
        lines = REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        tree = W.build_wbs_from_text(lines)
        paths = _paths(tree)
        assert ("Функціональні блоки (що робить)", "Bug Reports") in paths
        assert ("Security Posture", "CSRF") in paths
        # The H1 is the document title, not a module.
        assert not any(p[0].startswith("TestFortge") for p in paths)
        assert tree.node_for_line(
            "Статистика виконання (pass / fail / blocked).") == \
            "text:функціональні-блоки-що-робить/dashboard"

    def test_an_outline_and_plain_lines(self):
        outline = W.build_wbs_from_text([
            "1. Checkout", "1.1 Cart", "Add an item to the cart.",
            "1.2 Payment", "2. Accounts", "2.1 Sign in"])
        assert ("Checkout", "Payment") in _paths(outline)
        assert ("Accounts", "Sign in") in _paths(outline)
        flat = W.build_wbs_from_text(["User can log in", "User can search"])
        assert flat.nodes == {}
        # No structure: each item's own area becomes a module …
        node = W.attribute(flat, {"section": "Authentication",
                                  "summary": "Verify that x"})
        assert flat.path(node) == ("Authentication",)
        # … but a generic area name never does.
        node = W.attribute(flat, {"section": "Requirements-specific",
                                  "summary": "Verify that x"})
        assert flat.path(node) == ("Cross-cutting", "General")

    @pytest.mark.parametrize("kind", ["test_case", "checklist_item"])
    def test_generation_from_the_document_is_filed(self, text_pid, kind):
        rows, field, assign = _distribution(text_pid, kind)
        assert len(rows) > 100
        assert [r["id"] for r in rows if r["id"] not in assign] == []
        paths = [_folder_path(text_pid, assign[r["id"]]) for r in rows]
        assert [r["id"] for r, p in zip(rows, paths)
                if fpx.parse(r[field]) != p] == []
        general = sum(1 for p in paths if p[-1] == "General")
        assert general <= len(rows) * 0.01
        top = Counter(p[0] for p in paths)
        # Test cases come from the document's requirements and land under
        # its headings. The checklist also carries a fixed block of ~76
        # web templates (header, footer, layout, generic forms) that a
        # described product has no page to own: Cross-cutting by honesty,
        # hence the looser bound for it.
        limit = 0.15 if kind == "test_case" else 0.35
        assert top["Cross-cutting"] / len(rows) < limit, top
        assert ("Функціональні блоки (що робить)" in top
                and "Security Posture" in top), top

    def test_folder_names_are_words_not_decoration(self, text_pid):
        _assert_clean_names(text_pid)

    def test_the_model_answer_is_validated(self, monkeypatch):
        from engine import llm_cache

        class Resp:
            content = [type("B", (), {"text": json.dumps({"modules": [
                {"name": "Каталог", "pages": ["Пошук", {"name": "Фільтри"}]},
                {"name": ""}, {"name": "Кошик", "pages": "not a list"}]})})()]
        monkeypatch.setattr(llm_cache, "cached_call", lambda *a, **k: Resp())
        tree = W.build_wbs_from_text_llm("Каталог …", force=True)
        assert _paths(tree) == {("Каталог",), ("Каталог", "Пошук"),
                                ("Каталог", "Фільтри"), ("Кошик",)}
        assert W.build_wbs_from_text_llm("") is None

        class Bad:
            content = [type("B", (), {"text": "no json here"})()]
        monkeypatch.setattr(llm_cache, "cached_call", lambda *a, **k: Bad())
        assert W.build_wbs_from_text_llm("x", force=True) is None


# ── generic: nothing about one site, language or menu shape ──────────

def _assert_clean_names(pid) -> None:
    """No emoji, pictograph or private-use character in any folder name,
    and no "All …" / "Усі …" landing-link phrasing left on a group."""
    import unicodedata
    for folder in db.list_folders(pid):
        name = folder["name"]
        bad = [ch for ch in name
               if unicodedata.category(ch) in {"So", "Sk", "Co", "Cs", "Cn"}
               or ch in "\ufe0e\ufe0f\u200d\u20e3"]
        assert not bad, (name, bad)
        assert not W._SHOW_ALL_LEAD.match(name), name


class TestNames:

    def test_decoration_is_stripped_and_words_kept(self):
        assert W.strip_decoration("👍👎 Оцініть свою компанію") == \
            "Оцініть свою компанію"
        assert W.strip_decoration("★ Featured") == "Featured"
        for kept in ("C++ & C#", "Résumé — Über", "100% Free", "Shop $5",
                     "日本語のページ", "Поради та FAQ"):
            assert W.strip_decoration(kept) == kept

    def test_show_all_landing_links_name_their_group(self):
        assert W.group_name("All Healthcare Solutions",
                            "/solutions/healthcare") == "Healthcare Solutions"
        assert W.group_name("Усі послуги", "/poslugy") == "Послуги"
        assert W.group_name("Healthcare — view all", "/h") == "Healthcare"
        assert W.group_name("View all", "/solutions/finance") == "Finance"
        assert W.group_name("Все", "/catalog/shoes") == "Shoes"
        # Whole phrases only.
        assert W.group_name("Alliance program", "/a") == "Alliance program"
        assert W.group_name("Allegro", "/a") == "Allegro"

    def test_on_the_real_menus(self, qarea_tree):
        assert ("Solutions", "Healthcare Solutions", "Pharma App") in \
            _paths(qarea_tree)


class TestGeneric:

    @pytest.fixture(scope="class")
    def dou(self):
        return json.load(gzip.open(_FIX / "dou_crawl_2026-10-20.json.gz",
                                   "rt", encoding="utf-8"))

    def test_a_flat_non_english_nav_is_a_module_per_link(self, dou):
        tree = W.build_wbs(dou["pages"])
        modules = [n.name for n in tree.ordered()
                   if n.parent is None and n.kind == "module"]
        assert modules[:4] == ["Форум", "Стрічка", "Календар", "Спільноти"]
        # Off-site links (jobs.dou.ua) are not this site's pages.
        assert "Робота" not in modules
        ids = [n.id for n in tree.nodes.values()]
        assert len(ids) == len(set(ids))

    def test_crawled_instances_collapse_into_their_module(self, dou):
        tree = W.build_wbs(dou["pages"])
        lenta = tree.node_for_url("https://dou.ua/lenta/")
        assert tree.node_for_url(
            "https://dou.ua/lenta/articles/ai-tools-6/?from=recent") == lenta
        assert not [n for n in tree.nodes.values()
                    if n.url.startswith("/lenta/") or
                    n.url.startswith("/forums/topic")]
        # …and their content is walked twice per node, not 25 times.
        surfaces = W.surface_urls(tree, dou["pages"])
        assert len([u for u in surfaces if "/lenta/" in u]) <= 3
        assert len(surfaces) < len(dou["pages"]) / 2

    def test_no_header_navigation_falls_back_to_the_url_structure(self, dou):
        pages = [dict(p) for p in dou["pages"]]
        pages[0] = {**pages[0], "header_links": [], "nav_groups": [],
                    "footer_links": []}
        tree = W.build_wbs(pages)
        modules = {n.url for n in tree.nodes.values() if n.kind == "module"}
        assert {"/forums", "/lenta"} <= modules
        assert tree.path(tree.node_for_url(
            "https://dou.ua/forums/topic/62652/"))[0] != "Other pages"

    def test_hash_routes_are_pages(self):
        assert W.norm_path("https://app.test/#/pricing") == "/#/pricing"
        assert W.norm_path("https://app.test/#!/pricing/") == "/#/pricing"
        assert W.norm_path("https://app.test/docs#install") == "/docs"
        page = {"url": "https://app.test/", "header_links": [
            {"text": "Pricing", "href": "#/pricing", "in_menu": False},
            {"text": "Docs", "href": "#/docs", "in_menu": False}]}
        tree = W.build_wbs([page])
        assert tree.path(tree.node_for_url("https://app.test/#/docs")) == \
            ("Docs",)

    def test_cyrillic_menu_labels_do_not_collide(self):
        page = {"url": "https://x.test/", "nav_groups": [
            {"region": "header", "label": "Послуги", "children": []},
            {"region": "header", "label": "Галузі", "children": []}]}
        tree = W.build_wbs([page])
        assert {"Послуги", "Галузі"} <= {n.name for n in tree.nodes.values()}


class TestSharedFormsAreOneSurface:
    """The 192 / 192 split: the same contact form, enumerated per page."""

    @pytest.fixture(scope="class")
    def qarea_pages(self, qarea):
        return [{k: p[k] for k in ("url", "title", "h1", "forms", "tables",
                                   "grid_controls", "headings", "buttons",
                                   "nav_links")} for p in qarea["pages"]]

    def test_a_form_embedded_on_every_page_is_enumerated_once(
            self, qarea_pages):
        from engine import tc_rules
        cases = tc_rules.enumerate_from_pages(qarea_pages)
        pages = Counter(c.page_url for c in cases)
        # 205 embedded forms, three field sets: the shared one on the
        # entry page, two variants (Careers, Testing & QA) for their extra
        # controls only.
        assert set(pages) == {"https://qarea.com/",
                              "https://qarea.com/careers",
                              "https://qarea.com/services/testing-and-qa"}
        # One case per rule per form (rules merged over the controls they
        # apply to), not one per control: the 20-field form is ~14 cases.
        assert sum(pages.values()) <= 40, pages
        assert max(pages.values()) < 20, pages

    def test_every_page_gets_its_own_content_cases(self, qarea_pages):
        from engine import tc_rules
        cases = tc_rules.enumerate_page_surfaces(qarea_pages)
        assert {c.page_url for c in cases} == {p["url"] for p in qarea_pages}
        # Chrome repeated across the site is nobody's content.
        assert not [c for c in cases if '"Back" button' in c.summary]
        assert all(c.preconditions and len(c.steps) >= 2 for c in cases)

class TestTheAsyncPathTheUiTakes:
    """The UI posts to /…/run-async, not to the sync form handler.

    The checklist worker never ran the site-aware stage: on qarea.com it
    returned the 289 template rows, all Unfiled, in the browser while the
    sync path built ~1,000 filed rows. Same gate, the real path.
    """

    def _wait(self, client, url):
        import time
        for _ in range(600):
            body = client.get(url).get_json()
            if body.get("status") in ("done", "failed"):
                return body
            time.sleep(0.1)
        raise AssertionError("job did not finish")

    @pytest.mark.parametrize("kind,start,status", [
        ("checklist_item", "/checklist/run-async", "/checklist/status/"),
        ("test_case", "/test-cases/run-async", "/test-cases/status/"),
    ])
    def test_async_generation_is_filed_into_the_wbs(
            self, app, client, qarea, monkeypatch, editing_on, request,
            kind, start, status):
        import engine.site_crawler as sc
        analysis = _analysis(qarea)
        monkeypatch.setattr(sc, "crawl_site", lambda url, *a, **k: analysis)
        monkeypatch.setenv("TESTFORTGE_BROWSER_ENABLED", "0")
        pid = db.upsert_project(
            name=f"WBS async {request.node.name} {_RUN}"[:180])
        _use(client, pid)
        resp = client.post(start, data={"input_text": "https://qarea.com/"})
        assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
        assert self._wait(client, status + resp.get_json()["job_id"]
                          )["status"] == "done"
        rows, field, assign = _distribution(pid, kind)
        assert len(rows) > 300
        assert [r["id"] for r in rows if r["id"] not in assign] == []
        assert all(fpx.parse(r[field]) ==
                   _folder_path(pid, assign[r["id"]]) for r in rows)
