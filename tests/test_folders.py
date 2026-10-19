"""Folders for test cases and checklist items (owner request, 2026-10-19).

One tree per project, shared by both kinds; deleting a folder moves its
contents up; generation files its output into a tree built from the site's
navigation. Covered here:

  * the DB helpers in engine/db.py — tree rules, delete semantics, and the
    one that matters most: a wipe-and-replace pack save keeps every row in
    its folder, because the pack dicts never carry ``folder_id``;
  * the HTTP surface in routes/folders.py — including CSRF actually on, and
    a folder of another project being unreachable;
  * the page markup the script relies on;
  * engine/folder_plan.py on the real testfort.com header (fetched
    2026-10-19, trimmed to the <header>): synthetic menus have hidden
    live-page defects in this codebase before, and this one did too — the
    mega-menu used the whole folder budget before the top-level links, and a
    review quote came through as a "page".
"""
from __future__ import annotations

import secrets
from dataclasses import asdict
from pathlib import Path

import pytest

from engine import db
from engine import folder_plan as fp

_RUN = secrets.token_hex(4)
_FIXTURE = Path(__file__).parent / "fixtures" / "testfort_header_2026-10-19.html"


def _case(external_id, **overrides):
    row = {
        "id": external_id, "section": "Auth", "section_num": 1,
        "summary": f"Verify that {external_id} works",
        "preconditions": "", "test_steps": "1. Open the page",
        "test_data": "", "expected_result": "The page opens",
        "issues": "", "comment": "", "user_story_id": "",
        "category": "Positive", "priority": "High", "status": "Unchecked",
        "testing_type": "Functional",
    }
    row.update(overrides)
    return row


def _item(external_id, section="Header", **overrides):
    row = {"id": external_id, "section": section, "objective":
           f"Verify that {external_id} is correct", "category": "Positive",
           "priority": "High", "status": "Unchecked", "item_num": "",
           "depth": 2}
    row.update(overrides)
    return row


@pytest.fixture
def project(app, request):
    pid = db.upsert_project(name=f"Folders {request.node.name} {_RUN}"[:180])
    db.save_test_cases(pid, [_case("TC-001"), _case("TC-002"),
                             _case("TC-003")])
    db.save_checklist(pid, [_item("HDR_001"), _item("FTR_001", "Footer")])
    return pid


@pytest.fixture
def other_project(app, request):
    return db.upsert_project(
        name=f"Folders other {request.node.name} {_RUN}"[:180])


@pytest.fixture
def editing_on(monkeypatch):
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("EDITORS_ENABLED", "1")
    return True


def _names(pid):
    return [(f["name"], f["parent_id"]) for f in db.list_folders(pid)]


def _metadata(pid, kind="test_cases"):
    return db.load_edit_metadata(pid, kind)


# ── The tree ──────────────────────────────────────────────────────────

class TestTree:

    def test_folders_nest_and_list_parents_first(self, project):
        module = db.create_folder(project, "Header")
        page = db.create_folder(project, "Pricing page", module["id"])
        db.create_folder(project, "Footer")
        assert _names(project) == [("Header", None),
                                   ("Pricing page", module["id"]),
                                   ("Footer", None)]
        assert page["source"] == "user"

    def test_a_sibling_with_the_same_name_is_refused_ignoring_case(
            self, project):
        db.create_folder(project, "Header")
        with pytest.raises(db.FolderError):
            db.create_folder(project, "  header ")

    def test_the_same_name_under_another_parent_is_fine(self, project):
        a = db.create_folder(project, "Header")
        b = db.create_folder(project, "Footer")
        db.create_folder(project, "Links", a["id"])
        db.create_folder(project, "Links", b["id"])
        assert len(db.list_folders(project)) == 4

    def test_a_blank_name_is_refused(self, project):
        with pytest.raises(db.FolderError):
            db.create_folder(project, "   ")

    def test_a_parent_from_another_project_is_refused(self, project,
                                                      other_project):
        foreign = db.create_folder(other_project, "Theirs")
        with pytest.raises(db.FolderError):
            db.create_folder(project, "Mine", foreign["id"])

    def test_nesting_is_capped(self, project):
        parent = None
        for depth in range(db.FOLDER_MAX_DEPTH):
            parent = db.create_folder(project, f"L{depth}",
                                      parent and parent["id"])
        with pytest.raises(db.FolderError):
            db.create_folder(project, "too deep", parent["id"])

    def test_a_folder_cannot_move_into_its_own_subtree(self, project):
        top = db.create_folder(project, "Top")
        mid = db.create_folder(project, "Mid", top["id"])
        low = db.create_folder(project, "Low", mid["id"])
        with pytest.raises(db.FolderError):
            db.move_folder(project, top["id"], low["id"])
        with pytest.raises(db.FolderError):
            db.move_folder(project, top["id"], top["id"])

    def test_move_reparents_and_reorders(self, project):
        a = db.create_folder(project, "A")
        b = db.create_folder(project, "B")
        c = db.create_folder(project, "C")
        db.move_folder(project, c["id"], None, 0)
        assert [n for n, _ in _names(project)] == ["C", "A", "B"]
        db.move_folder(project, b["id"], a["id"])
        assert ("B", a["id"]) in _names(project)

    def test_rename_takes_the_folder_from_the_generator(self, project):
        auto = db.create_folder(project, "Pricing page", source="auto")
        renamed = db.rename_folder(project, auto["id"], "Plans")
        assert renamed == {**renamed, "name": "Plans", "source": "user"}


class TestDelete:

    def test_contents_move_up_one_level_and_nothing_is_deleted(
            self, project):
        header = db.create_folder(project, "Header")
        menu = db.create_folder(project, "About", header["id"])
        page = db.create_folder(project, "Company page", menu["id"])
        db.assign_to_folder(project, "test_case", ["TC-001"], menu["id"])
        db.assign_to_folder(project, "checklist_item", ["HDR_001"],
                            menu["id"])

        result = db.delete_folder(project, menu["id"])

        assert result["moved_items"] == 2
        assert result["moved_folders"] == 1
        assert ("Company page", header["id"]) in _names(project)
        assert db.load_folder_assignments(project, "test_case") == {
            "TC-001": header["id"]}
        assert db.load_folder_assignments(project, "checklist_item") == {
            "HDR_001": header["id"]}
        assert len(db.load_test_cases(project)) == 3
        assert page["id"] in {f["id"] for f in db.list_folders(project)}

    def test_deleting_a_top_level_folder_unfiles_its_items(self, project):
        top = db.create_folder(project, "Header")
        db.assign_to_folder(project, "test_case", ["TC-001"], top["id"])
        db.delete_folder(project, top["id"])
        assert db.load_folder_assignments(project, "test_case") == {}

    def test_a_child_named_like_a_new_sibling_is_suffixed(self, project):
        a = db.create_folder(project, "A")
        db.create_folder(project, "Links")
        db.create_folder(project, "Links", a["id"])
        db.delete_folder(project, a["id"])
        assert sorted(n for n, _ in _names(project)) == ["Links",
                                                        "Links (2)"]


# ── Filing items ──────────────────────────────────────────────────────

class TestAssign:

    def test_a_move_files_the_rows_and_counts_as_a_human_edit(
            self, project):
        folder = db.create_folder(project, "Pricing page")
        result = db.assign_to_folder(project, "test_case",
                                     ["TC-001", "TC-002", "NOPE"],
                                     folder["id"], actor="u1")
        assert result == {"changed": ["TC-001", "TC-002"],
                          "missing": ["NOPE"]}
        meta = _metadata(project)
        assert meta["TC-001"]["ai_generated"] is False
        assert meta["TC-001"]["edited_by"] == "u1"
        # Not bumped: the inline editor's held version must stay valid.
        assert meta["TC-001"]["row_version"] == 1
        assert meta["TC-003"]["ai_generated"] is True

    def test_moving_to_unfiled(self, project):
        folder = db.create_folder(project, "X")
        db.assign_to_folder(project, "test_case", ["TC-001"], folder["id"])
        db.assign_to_folder(project, "test_case", ["TC-001"], None)
        assert db.load_folder_assignments(project, "test_case") == {}

    def test_another_projects_folder_is_refused(self, project,
                                                other_project):
        foreign = db.create_folder(other_project, "Theirs")
        with pytest.raises(db.FolderError):
            db.assign_to_folder(project, "test_case", ["TC-001"],
                                foreign["id"])

    def test_another_projects_rows_are_not_touched(self, project,
                                                   other_project):
        db.save_test_cases(other_project, [_case("TC-001")])
        folder = db.create_folder(project, "Mine")
        db.assign_to_folder(project, "test_case", ["TC-001"], folder["id"])
        assert db.load_folder_assignments(other_project, "test_case") == {}


class TestPackWritesKeepFolders:
    """The regression this design exists to prevent.

    ``save_test_cases`` / ``save_checklist`` delete and re-insert, and the
    dicts they are given come from ``load_*``, which has no ``folder_id``.
    Every reorder, upload and inline edit goes through one — so without the
    carry-over every folder would empty on the next click.
    """

    def test_a_round_trip_through_the_pack_keeps_the_folder(self, project):
        folder = db.create_folder(project, "Pricing page")
        db.assign_to_folder(project, "test_case", ["TC-002"], folder["id"])
        db.assign_to_folder(project, "checklist_item", ["FTR_001"],
                            folder["id"])

        db.save_test_cases(project, db.load_test_cases(project))
        db.save_checklist(project, db.load_checklist(project))

        assert db.load_folder_assignments(project, "test_case") == {
            "TC-002": folder["id"]}
        assert db.load_folder_assignments(project, "checklist_item") == {
            "FTR_001": folder["id"]}

    def test_an_explicit_folder_in_the_incoming_dict_wins(self, project):
        old = db.create_folder(project, "Old")
        new = db.create_folder(project, "New")
        db.assign_to_folder(project, "test_case", ["TC-001"], old["id"])
        db.save_test_cases(project, [_case("TC-001", folder_id=new["id"])])
        assert db.load_folder_assignments(project, "test_case") == {
            "TC-001": new["id"]}

    def test_a_foreign_folder_id_in_the_dict_is_ignored(self, project,
                                                        other_project):
        foreign = db.create_folder(other_project, "Theirs")
        db.save_test_cases(project,
                           [_case("TC-001", folder_id=foreign["id"])])
        assert db.load_folder_assignments(project, "test_case") == {}

    def test_regeneration_keeps_a_rows_human_folder(self, project):
        """A move is an edit: the merge keeps the row, and its folder."""
        mine = db.create_folder(project, "Mine")
        auto = db.create_folder(project, "Auto", source="auto")
        db.assign_to_folder(project, "test_case", ["TC-001"], mine["id"])
        db.save_test_cases(
            project,
            [_case("TC-001", folder_id=auto["id"]),
             _case("TC-002", folder_id=auto["id"])],
            protect_edits=True)
        assert db.load_folder_assignments(project, "test_case") == {
            "TC-001": mine["id"], "TC-002": auto["id"]}

    def test_the_dataclass_round_trip_carries_a_generated_folder(
            self, project):
        """Generation goes dict → dataclass → dict before it saves."""
        from routes._shared import reconstruct_test_cases, tc_to_dict
        folder = db.create_folder(project, "Gen", source="auto")
        dicts = [tc_to_dict(tc) for tc in reconstruct_test_cases(
            [_case("TC-009", folder_id=folder["id"])])]
        db.save_test_cases(project, dicts)
        assert db.load_folder_assignments(project, "test_case") == {
            "TC-009": folder["id"]}

    def test_ensure_folder_path_is_idempotent(self, project):
        first = db.ensure_folder_path(project, ["Header", "About",
                                                "Company page"])
        again = db.ensure_folder_path(project, ["header", "About",
                                                "Company page"])
        assert first == again
        assert len(db.list_folders(project)) == 3


# ── HTTP ──────────────────────────────────────────────────────────────

def _use(client, project):
    with client.session_transaction() as sess:
        sess["project_id"] = project


class TestRoutes:

    def test_create_rename_move_delete(self, client, project, editing_on):
        _use(client, project)
        resp = client.post("/api/folders", json={"name": "Header"})
        assert resp.status_code == 201, resp.get_json()
        header = resp.get_json()["folder"]["id"]
        page = client.post("/api/folders", json={
            "name": "Pricing page", "parent_id": header}).get_json()["folder"]

        assert client.patch(f"/api/folders/{page['id']}",
                            json={"name": "Plans page"}).status_code == 200
        assert client.patch(f"/api/folders/{page['id']}",
                            json={"parent_id": None}).status_code == 200
        assert client.patch(f"/api/folders/{header}",
                            json={"parent_id": header}).status_code == 400

        resp = client.post("/api/folders/assign", json={
            "kind": "test_case", "ids": ["TC-001"], "folder_id": page["id"]})
        assert resp.get_json()["changed"] == ["TC-001"]

        listing = client.get("/api/folders").get_json()
        assert [f["name"] for f in listing["folders"]] == ["Header",
                                                           "Plans page"]
        assert listing["assignments"]["test_case"] == {"TC-001": page["id"]}

        assert client.delete(f"/api/folders/{page['id']}").status_code == 200
        assert client.get("/api/folders").get_json()["assignments"][
            "test_case"] == {}

    def test_bad_input_is_a_400_not_a_500(self, client, project,
                                          editing_on):
        _use(client, project)
        assert client.post("/api/folders", json={"name": ""}).status_code == 400
        assert client.post("/api/folders", json={
            "name": "x", "parent_id": "abc"}).status_code == 400
        assert client.post("/api/folders/assign", json={
            "kind": "bug", "ids": ["X"]}).status_code == 400
        assert client.post("/api/folders/assign", json={
            "kind": "test_case", "ids": []}).status_code == 400
        assert client.patch("/api/folders/999999",
                            json={"name": "x"}).status_code == 404
        assert client.delete("/api/folders/999999").status_code == 404

    def test_another_projects_folder_is_unreachable(
            self, client, project, other_project, editing_on):
        foreign = db.create_folder(other_project, "Theirs")
        _use(client, project)
        assert client.patch(f"/api/folders/{foreign['id']}",
                            json={"name": "Mine now"}).status_code == 404
        assert client.delete(
            f"/api/folders/{foreign['id']}").status_code == 404
        assert client.post("/api/folders/assign", json={
            "kind": "test_case", "ids": ["TC-001"],
            "folder_id": foreign["id"]}).status_code == 400
        assert _names(other_project) == [("Theirs", None)]

    def test_writes_are_off_with_the_editors(self, client, project,
                                             monkeypatch):
        monkeypatch.setenv("EDITORS_ENABLED", "0")
        _use(client, project)
        assert client.post("/api/folders",
                           json={"name": "X"}).status_code == 404
        assert client.get("/api/folders").status_code == 200

    def test_csrf_is_enforced_and_satisfied(self, app, client, project,
                                            editing_on):
        """conftest turns CSRF off; production does not."""
        _use(client, project)
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            refused = client.post("/api/folders", json={"name": "No token"})
            assert refused.status_code == 400
            token = client.get("/api/csrf-token").get_json()["token"]
            ok = client.post("/api/folders", json={"name": "With token"},
                             headers={"X-CSRFToken": token})
            assert ok.status_code == 201
        finally:
            app.config["WTF_CSRF_ENABLED"] = False
        assert _names(project) == [("With token", None)]


class TestPages:

    @pytest.mark.parametrize("url,kind,ids", [
        ("/test-cases", "test_case", ("TC-001", "TC-002", "TC-003")),
        ("/checklist", "checklist_item", ("HDR_001", "FTR_001")),
    ])
    def test_the_panel_and_the_item_hooks_render(self, client, project,
                                                 editing_on, url, kind, ids):
        folder = db.create_folder(project, "Header")
        _use(client, project)
        body = client.get(url).get_data(as_text=True)
        assert body.count("data-folder-panel") == 1
        assert f'data-folder-kind="{kind}"' in body
        for item_id in ids:
            assert f'data-folder-item="{item_id}"' in body
        assert body.count("data-folder-section") >= 1
        assert "js/folders.js" in body
        assert f'"name": "Header"' in body or '"name":"Header"' in body
        assert f'"id": {folder["id"]}' in body or f'"id":{folder["id"]}' in body


# ── The generated tree ────────────────────────────────────────────────

@pytest.fixture(scope="module")
def testfort_page() -> dict:
    from engine.site_crawler import _parse_page
    html = _FIXTURE.read_text(encoding="utf-8")
    return asdict(_parse_page(html, "https://testfort.com/", "testfort.com"))


class TestFolderPlanOnRealMarkup:

    def test_header_menus_pages_then_homepage_and_footer(self,
                                                         testfort_page):
        plan = fp.build_plan([testfort_page])
        assert plan.paths[0] == fp.HEADER
        assert plan.paths[-2:] == [fp.HOMEPAGE, fp.FOOTER]
        assert ("Header", "Why Us") in plan.paths
        assert ("Header", "Why Us", "Pricing & Business Models page") \
            in plan.paths
        assert ("Header", "Services", "Manual Testing page") in plan.paths
        assert ("Header", "Resources", "Blog page") in plan.paths

    def test_promotions_in_the_mega_menu_are_not_pages(self, testfort_page):
        names = [p[-1] for p in fp.build_plan([testfort_page]).paths]
        assert not any("impressed" in n or "\n" in n for n in names), names
        assert not any("LuxiEdge" in n for n in names), names
        assert all(len(n) <= fp._PAGE_LABEL_MAX + len(" page")
                   for n in names), names

    def test_links_map_to_their_page_folders(self, testfort_page):
        plan = fp.build_plan([testfort_page])
        assert plan.for_page("https://testfort.com/") == fp.HOMEPAGE
        assert plan.for_page("https://testfort.com/careers/") == (
            "Header", "Why Us", "Careers page")
        assert plan.primary == fp.HOMEPAGE

    def test_a_plain_top_level_link_is_not_starved_by_a_mega_menu(self):
        page = {"url": "https://ex.com/", "header_links":
                [{"text": f"Item {i}", "href": f"/i{i}", "in_menu": True}
                 for i in range(200)]
                + [{"text": "Contact us", "href": "/contact",
                    "in_menu": False}],
                "nav_groups": [{"region": "header", "label": "Big",
                                "children": [f"Item {i}"
                                             for i in range(200)]}]}
        plan = fp.build_plan([page])
        assert ("Header", "Contact us page") in plan.paths
        pages = [p for p in plan.paths if p[-1].endswith(" page")]
        assert len(pages) == fp.MAX_PAGE_FOLDERS

    def test_no_header_still_gives_homepage_and_footer(self):
        plan = fp.build_plan([{"url": "https://ex.com/"}])
        assert plan.paths == [fp.HEADER, fp.HOMEPAGE, fp.FOOTER]


class TestFiling:

    @pytest.fixture
    def plan(self, testfort_page):
        careers = {"url": "https://testfort.com/careers", "title": "Careers"}
        return fp.build_plan([testfort_page, careers])

    def test_checklist_sections(self, plan):
        assert plan.for_checklist({"section": "Header"}) == fp.HEADER
        assert plan.for_checklist({"section": "Footer"}) == fp.FOOTER
        assert plan.for_checklist({"section": "Page Content"}) == fp.HOMEPAGE
        assert plan.for_checklist(
            {"section": "Page Content — Careers"}) == (
                "Header", "Why Us", "Careers page")
        assert plan.for_checklist(
            {"section": "Page Content — Nowhere"}) == fp.GENERAL
        # An authored section name belongs to the primary surface.
        assert plan.for_checklist({"section": "Hero banner"}) == fp.HOMEPAGE

    def test_test_cases_by_url_pattern_then_by_name(self, plan):
        urls = ["https://testfort.com/", "https://testfort.com/careers"]
        assert plan.for_test_case({"url_pattern": "*/careers*"}, urls) == (
            "Header", "Why Us", "Careers page")
        # Matching every page says nothing about which one.
        assert plan.for_test_case({"url_pattern": "*",
                                   "section": "Security"}, urls) == fp.GENERAL
        assert plan.for_test_case({"section": "Footer links"}, urls) \
            == fp.FOOTER
        assert plan.for_test_case({"section": "Blog grid"}, urls) == (
            "Header", "Resources", "Blog page")

    def test_generation_creates_the_tree_once_and_stamps_items(
            self, project, testfort_page):
        from routes.generation import _file_into_folders
        tcs = [_case("SA1_001", url_pattern="*/careers*"),
               _case("SA1_002", section="Security")]
        cls = [_item("HDR_001"), _item("CNT_001", "Page Content")]
        pages = [testfort_page,
                 {"url": "https://testfort.com/careers", "title": "Careers"}]

        _file_into_folders(project, pages, tcs, cls)
        count = len(db.list_folders(project))
        _file_into_folders(project, pages, [dict(t) for t in tcs],
                           [dict(c) for c in cls])

        assert len(db.list_folders(project)) == count
        by_id = {f["id"]: f for f in db.list_folders(project)}
        assert by_id[tcs[0]["folder_id"]]["name"] == "Careers page"
        assert by_id[tcs[1]["folder_id"]]["name"] == "General"
        assert by_id[cls[0]["folder_id"]]["name"] == "Header"
        assert by_id[cls[1]["folder_id"]]["name"] == "Homepage"
        assert all(f["source"] == "auto" for f in by_id.values())

        db.save_test_cases(project, tcs)
        assert db.load_folder_assignments(project, "test_case")[
            "SA1_001"] == tcs[0]["folder_id"]
