"""Filing by the ``[Folder -> Subfolder]`` summary prefix (owner rule, 2026-10-20).

    [Header] Verify that QArea logo is displayed in Header
        → top-level folder "Header"
    [Services -> Web Development] Verify that the "Tell us about your
    project" contact form is open after clicking the "Hire us" button
        → "Web Development" inside "Services"

Covered here:

  * engine/folder_prefix.py — every edge case the module docstring settles;
  * the parser on real generated summaries: the checklist the rules engine
    writes for the real testfort.com header (the fixture test_folders.py
    uses), prefixed by the navigation plan and parsed back;
  * the consumers that compose text from a summary skip the prefix — bug
    titles, the house-style review, the authored-case lint, Gherkin;
  * engine/db.py filing — create-or-reuse (ignoring case), the import rule,
    re-filing;
  * the HTTP surface — inline edits (and the manual-move rule), uploads,
    the re-file action with CSRF actually on, and generation.
"""
from __future__ import annotations

import io
import secrets
from dataclasses import asdict
from pathlib import Path

import pytest

from engine import db
from engine import wbs as wbs_mod
from engine import folder_prefix as fpx

_RUN = secrets.token_hex(4)
_FIXTURE = Path(__file__).parent / "fixtures" / "testfort_header_2026-10-19.html"

LOGO = "[Header] Verify that QArea logo is displayed in Header"
HIRE = ('[Services -> Web Development] Verify that the "Tell us about your '
        'project" contact form is open after clicking the "Hire us" button')


# ── The parser ────────────────────────────────────────────────────────

class TestParse:

    def test_the_owners_two_examples(self):
        assert fpx.split(LOGO) == (
            ("Header",), "Verify that QArea logo is displayed in Header")
        path, body = fpx.split(HIRE)
        assert path == ("Services", "Web Development")
        assert body.startswith('Verify that the "Tell us about your project"')

    def test_deeper_paths(self):
        assert fpx.parse("[A -> B -> C -> D] x") == ("A", "B", "C", "D")

    def test_whitespace_is_trimmed_and_collapsed(self):
        assert fpx.split("   [  Services   ->   Web    Development ]   "
                         "Verify that x") == (
            ("Services", "Web Development"), "Verify that x")
        assert fpx.parse("[A->B]x") == ("A", "B")

    @pytest.mark.parametrize("text", [
        "[A > B] x", "[A → B] x", "[A => B] x", "[A —> B] x", "[A –> B] x",
        "[A --> B] x", "[A>B] x",
    ])
    def test_separator_variants(self, text):
        assert fpx.parse(text) == ("A", "B")

    def test_slash_is_part_of_a_name_not_a_separator(self):
        assert fpx.parse("[Footer -> Terms / Privacy] x") == (
            "Footer", "Terms / Privacy")

    def test_empty_segments_are_dropped(self):
        assert fpx.parse("[A -> -> B] x") == ("A", "B")
        assert fpx.parse("[ -> B] x") == ("B",)
        assert fpx.parse("[A -> ] x") == ("A",)

    @pytest.mark.parametrize("text", [
        "[] Verify that x", "[ ] Verify that x", "[ -> ] Verify that x",
        "[x] Verify that x", "[X] done",
        "[docs](https://ex.com) Verify that x",
        "[A [B]] Verify that x", "[unclosed Verify that x",
        "Verify that the [Submit] button is clickable",
        "Verify that x [Header]",
        "", None,
    ])
    def test_not_a_prefix(self, text):
        assert fpx.parse(text) == ()
        assert fpx.strip_prefix(text) == ("" if text is None else text)

    def test_no_prefix_keeps_the_text_unchanged(self):
        text = "Verify that the page opens"
        assert fpx.split(text) == ((), text)

    def test_depth_is_capped_like_the_tree(self):
        assert fpx.parse("[1>2>3>4>5>6>7>8] x") == ("1", "2", "3", "4", "5",
                                                    "6")
        assert db.FOLDER_MAX_DEPTH == fpx.MAX_DEPTH
        assert db.FOLDER_NAME_MAX == fpx.NAME_MAX

    def test_prefix_changed_ignores_case_and_spacing(self):
        assert not fpx.prefix_changed(LOGO, "[header]  Verify that y")
        assert not fpx.prefix_changed("[A->B] x", "[ a  >  b ] other text")
        assert fpx.prefix_changed("[A] x", "[A -> B] x")
        assert fpx.prefix_changed("Verify that x", "[A] Verify that x")
        # Removing the prefix is not a request to move the item anywhere.
        assert not fpx.prefix_changed("[A] x", "x")
        assert not fpx.prefix_changed("x", "y")

    def test_format_and_with_prefix(self):
        assert fpx.format_prefix(("Services", "Web Development")) == \
            "[Services -> Web Development]"
        assert fpx.with_prefix("Verify that x", ("Header",)) == \
            "[Header] Verify that x"
        # Replaces an old prefix instead of stacking a second one.
        assert fpx.with_prefix("[Old] Verify that x", ("New", "Sub")) == \
            "[New -> Sub] Verify that x"
        assert fpx.with_prefix("[Old] Verify that x", ()) == "Verify that x"
        # A name holding a separator or a bracket still parses back.
        odd = fpx.with_prefix("x", ("A -> B", "[C]"))
        assert fpx.parse(odd) == ("A B", "C")

    def test_map_body_keeps_the_prefix(self):
        assert fpx.map_body("[A] hello", str.upper) == "[A] HELLO"
        assert fpx.map_body("hello", str.upper) == "HELLO"


# ── Real generated summaries ─────────────────────────────────────────

@pytest.fixture(scope="module")
def testfort_page() -> dict:
    from engine.site_crawler import _parse_page
    html = _FIXTURE.read_text(encoding="utf-8")
    return asdict(_parse_page(html, "https://testfort.com/", "testfort.com"))


@pytest.fixture(scope="module")
def real_checks(testfort_page):
    from engine import checklist_rules as rules
    items = rules.to_checklist_items(rules.build_checklist(
        [testfort_page], url="https://testfort.com/"))
    assert len(items) >= 5   # the fixture is the header only
    return items


class TestOnRealGeneratedSummaries:

    def test_no_generated_objective_is_read_as_a_prefix(self, real_checks):
        # The rules write "[Label] button" mid-sentence; none may parse.
        assert any("[" in c.objective for c in real_checks)
        assert [c.objective for c in real_checks
                if fpx.parse(c.objective)] == []

    def test_planned_prefixes_round_trip(self, real_checks, testfort_page):
        tree = wbs_mod.build_wbs([testfort_page])
        for check in real_checks:
            path = tree.path(wbs_mod.attribute(tree, asdict(check),
                                               "objective"))
            prefixed = fpx.with_prefix(check.objective, path)
            assert fpx.parse(prefixed) == path, prefixed
            assert fpx.strip_prefix(prefixed) == check.objective
        menu = [c for c in real_checks
                if "sub-items" in c.objective and "Services" in c.objective]
        assert menu, "the Services menu check is missing from the fixture"

    def test_the_house_style_review_leaves_prefixed_items_alone(
            self, real_checks, testfort_page):
        from engine.qa_team_lead import review_checklist
        from engine.testcase_generator import ChecklistItem
        tree = wbs_mod.build_wbs([testfort_page])
        items = [ChecklistItem(**{**asdict(c), "objective": fpx.with_prefix(
            c.objective, tree.path(wbs_mod.attribute(tree, asdict(c),
                                                     "objective")))})
            for c in real_checks]
        before = [i.objective for i in items]
        reviewed, report = review_checklist(items)
        assert not [f for f in report.findings
                    if f.category == "Passive Voice"]
        assert [i.objective for i in reviewed] == before

    def test_the_review_fixes_the_body_and_keeps_the_prefix(self):
        from engine.qa_team_lead import _ensure_verify_that
        assert _ensure_verify_that("[Header] Logo is displayed") \
            .startswith("[Header] Verify that ")

    def test_the_authored_case_lint_reads_past_the_prefix(self):
        from engine import tc_author
        assert tc_author.house_style_findings("summary", LOGO) == \
            tc_author.house_style_findings(
                "summary", fpx.strip_prefix(LOGO))

    def test_bug_titles_do_not_carry_the_prefix(self):
        from engine.bug_report import defect_title_from_objective
        assert defect_title_from_objective(LOGO, section="Header") == \
            defect_title_from_objective(fpx.strip_prefix(LOGO),
                                        section="Header")
        title = defect_title_from_objective(HIRE, section="Services")
        assert "Web Development]" not in title
        assert title.startswith("[Services] ")

    def test_gherkin_scenario_name_has_no_prefix(self):
        from engine import gherkin
        tc = {"summary": LOGO, "preconditions": "", "test_steps": "1. Look",
              "expected_result": "Shown"}

        class Row:
            pass
        row = Row()
        row.__dict__.update(tc)
        assert gherkin.scenario_from_test_case(row).name == \
            fpx.strip_prefix(LOGO)


# ── Filing in the database ───────────────────────────────────────────

def _case(external_id, summary=None, **overrides):
    row = {
        "id": external_id, "section": "Auth", "section_num": 1,
        "summary": summary or f"Verify that {external_id} works",
        "preconditions": "", "test_steps": "1. Open the page",
        "test_data": "", "expected_result": "The page opens",
        "issues": "", "comment": "", "user_story_id": "",
        "category": "Positive", "priority": "High", "status": "Unchecked",
        "testing_type": "Functional",
    }
    row.update(overrides)
    return row


def _item(external_id, objective=None, section="Header"):
    return {"id": external_id, "section": section,
            "objective": objective or f"Verify that {external_id} is shown",
            "category": "Positive", "priority": "High",
            "status": "Unchecked", "item_num": "", "depth": 2}


@pytest.fixture
def project(app, request):
    return db.upsert_project(name=f"Prefix {request.node.name} {_RUN}"[:180])


@pytest.fixture
def editing_on(monkeypatch):
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("EDITORS_ENABLED", "1")
    return True


def _path_of(project, folder_id):
    by_id = {f["id"]: f for f in db.list_folders(project)}
    names = []
    while folder_id is not None:
        names.insert(0, by_id[folder_id]["name"])
        folder_id = by_id[folder_id]["parent_id"]
    return tuple(names)


def _where(project, kind, external_id):
    fid = db.load_folder_assignments(project, kind).get(external_id)
    return _path_of(project, fid) if fid else None


class TestDbFiling:

    def test_creates_the_path_and_reuses_folders_ignoring_case(self,
                                                               project):
        existing = db.create_folder(project, "Header", source="user")
        cases = [_case("TC-001", LOGO), _case("TC-002", HIRE),
                 _case("TC-003", "[HEADER ->  Logo] Verify that y"),
                 _case("TC-004")]
        assert db.file_by_prefix(project, cases, "test_case") == 3
        db.save_test_cases(project, cases)

        assert cases[0]["folder_id"] == existing["id"]
        assert _where(project, "test_case", "TC-002") == (
            "Services", "Web Development")
        assert _where(project, "test_case", "TC-003") == ("Header", "Logo")
        assert _where(project, "test_case", "TC-004") is None
        names = [(f["name"], f["parent_id"]) for f in db.list_folders(project)]
        assert names.count(("Header", None)) == 1
        # The prefix stays in the stored summary.
        assert db.load_test_cases(project)[0]["summary"] == LOGO

    def test_an_import_files_only_new_or_changed_prefixes(self, project):
        db.save_test_cases(project, [_case("TC-001", LOGO),
                                     _case("TC-002", LOGO)])
        db.refile_from_prefixes(project, "test_case")
        manual = db.create_folder(project, "Moved by hand")
        db.assign_to_folder(project, "test_case", ["TC-001", "TC-002"],
                            manual["id"])

        incoming = [_case("TC-001", "[header] Verify that it still shows"),
                    _case("TC-002", "[Footer] Verify that it moved"),
                    _case("TC-003", "[Footer] Verify that it is new")]
        previous = db.stored_prefix_texts(project, "test_case")
        assert db.file_by_prefix(project, incoming, "test_case",
                                 previous=previous) == 2
        db.save_test_cases(project, incoming)
        assert _where(project, "test_case", "TC-001") == ("Moved by hand",)
        assert _where(project, "test_case", "TC-002") == ("Footer",)
        assert _where(project, "test_case", "TC-003") == ("Footer",)

    def test_refile_applies_the_rule_to_everything_stored(self, project):
        db.save_checklist(project, [_item("HDR_001", LOGO),
                                    _item("SRV_001", HIRE),
                                    _item("GEN_001")])
        manual = db.create_folder(project, "Elsewhere")
        db.assign_to_folder(project, "checklist_item", ["GEN_001"],
                            manual["id"])

        result = db.refile_from_prefixes(project, "checklist_item")
        assert sorted(result["changed"]) == ["HDR_001", "SRV_001"]
        assert result["no_prefix"] == 1
        assert _where(project, "checklist_item", "SRV_001") == (
            "Services", "Web Development")
        # Unprefixed: left where a person put it.
        assert _where(project, "checklist_item", "GEN_001") == ("Elsewhere",)

        again = db.refile_from_prefixes(project, "checklist_item")
        assert again["changed"] == [] and again["unchanged"] == 2

    def test_refile_overrides_a_manual_move_because_it_was_asked_for(
            self, project):
        db.save_test_cases(project, [_case("TC-001", LOGO)])
        db.refile_from_prefixes(project, "test_case")
        manual = db.create_folder(project, "Manual")
        db.assign_to_folder(project, "test_case", ["TC-001"], manual["id"])
        db.refile_from_prefixes(project, "test_case")
        assert _where(project, "test_case", "TC-001") == ("Header",)

    def test_unknown_kind_is_refused(self, project):
        with pytest.raises(db.FolderError):
            db.refile_from_prefixes(project, "bug")


# ── HTTP ──────────────────────────────────────────────────────────────

def _use(client, project):
    with client.session_transaction() as sess:
        sess["project_id"] = project


def _patch(client, entity, entity_id, **changes):
    return client.patch(f"/api/edit/{entity}/{entity_id}",
                        json={"changes": changes})


class TestInlineEdit:

    def test_the_owners_examples_land_in_their_folders(self, client,
                                                       project, editing_on):
        db.save_test_cases(project, [_case("TC-001"), _case("TC-002")])
        _use(client, project)
        resp = _patch(client, "test_case", "TC-001", summary=LOGO)
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["item"]["summary"] == LOGO
        assert _path_of(project, resp.get_json()["folder_id"]) == ("Header",)

        assert _patch(client, "test_case", "TC-002",
                      summary=HIRE).status_code == 200
        assert _where(project, "test_case", "TC-002") == (
            "Services", "Web Development")

    def test_a_manual_move_survives_edits_that_keep_the_prefix(
            self, client, project, editing_on):
        db.save_test_cases(project, [_case("TC-001", LOGO)])
        db.refile_from_prefixes(project, "test_case")
        manual = db.create_folder(project, "Moved by hand")
        _use(client, project)
        assert client.post("/api/folders/assign", json={
            "kind": "test_case", "ids": ["TC-001"],
            "folder_id": manual["id"]}).status_code == 200

        # Another field, the same prefix re-typed, the prefix removed:
        # none of them moves the item.
        for changes in ({"test_steps": "1. Look at the header"},
                        {"summary": "[header] Verify that the logo is shown"},
                        {"summary": "Verify that the logo is shown"}):
            resp = _patch(client, "test_case", "TC-001", **changes)
            assert resp.status_code == 200, resp.get_json()
            assert "folder_id" not in resp.get_json()
            assert _where(project, "test_case", "TC-001") == (
                "Moved by hand",), changes

        # A changed prefix does.
        assert _patch(client, "test_case", "TC-001",
                      summary="[Footer] Verify that the logo is shown"
                      ).status_code == 200
        assert _where(project, "test_case", "TC-001") == ("Footer",)

    def test_checklist_objective(self, client, project, editing_on):
        db.save_checklist(project, [_item("HDR_001")])
        _use(client, project)
        assert _patch(client, "checklist_item", "HDR_001",
                      objective=HIRE).status_code == 200
        assert _where(project, "checklist_item", "HDR_001") == (
            "Services", "Web Development")

    def test_a_new_item_created_with_a_prefix_is_filed(self, client,
                                                       project, editing_on):
        db.save_test_cases(project, [_case("TC-001")])
        _use(client, project)
        resp = client.post("/api/edit/test_case",
                           json={"values": {"summary": LOGO}})
        assert resp.status_code == 201, resp.get_json()
        new_id = resp.get_json()["item"]["id"]
        assert _where(project, "test_case", new_id) == ("Header",)


class TestRefileRoute:

    def test_refile_files_and_returns_the_tree(self, client, project,
                                               editing_on):
        db.save_test_cases(project, [_case("TC-001", LOGO),
                                     _case("TC-002", HIRE),
                                     _case("TC-003")])
        _use(client, project)
        resp = client.post("/api/folders/refile", json={"kind": "test_case"})
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert sorted(body["changed"]) == ["TC-001", "TC-002"]
        assert body["no_prefix"] == 1
        assert set(body["assignments"]["test_case"]) == {"TC-001", "TC-002"}
        assert _where(project, "test_case", "TC-002") == (
            "Services", "Web Development")

    def test_bad_kind_and_editors_off(self, client, project, editing_on,
                                      monkeypatch):
        _use(client, project)
        assert client.post("/api/folders/refile",
                           json={"kind": "bug"}).status_code == 400
        monkeypatch.setenv("EDITORS_ENABLED", "0")
        assert client.post("/api/folders/refile",
                           json={"kind": "test_case"}).status_code == 404

    def test_csrf_is_enforced_and_satisfied(self, app, client, project,
                                            editing_on):
        """conftest turns CSRF off; production does not."""
        db.save_test_cases(project, [_case("TC-001", LOGO)])
        _use(client, project)
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            refused = client.post("/api/folders/refile",
                                  json={"kind": "test_case"})
            assert refused.status_code == 400
            assert _where(project, "test_case", "TC-001") is None
            token = client.get("/api/csrf-token").get_json()["token"]
            ok = client.post("/api/folders/refile",
                             json={"kind": "test_case"},
                             headers={"X-CSRFToken": token})
            assert ok.status_code == 200
        finally:
            app.config["WTF_CSRF_ENABLED"] = False
        assert _where(project, "test_case", "TC-001") == ("Header",)

    def test_the_button_renders_on_both_pages(self, client, project,
                                              editing_on):
        db.save_test_cases(project, [_case("TC-001")])
        db.save_checklist(project, [_item("HDR_001")])
        _use(client, project)
        for url in ("/test-cases", "/checklist"):
            body = client.get(url).get_data(as_text=True)
            assert body.count("data-folder-refile") == 1, url


class TestUpload:

    def _upload(self, client, project, csv, path, mode="replace"):
        _use(client, project)
        return client.post(path, data={
            "upload_file": (io.BytesIO(csv.encode()), "pack.csv"),
            "upload_mode": mode}, content_type="multipart/form-data",
            follow_redirects=True)

    def test_test_case_upload_files_by_prefix(self, client, project):
        csv = ("ID,Summary,Steps,Expected\n"
               f'TC-901,"{LOGO}",Open,Logo\n'
               f'TC-902,"{HIRE.replace(chr(34), chr(34) * 2)}",Click,Form\n'
               "TC-903,Verify that nothing is filed,Open,Page\n")
        assert self._upload(client, project, csv,
                            "/test-cases/upload").status_code == 200
        assert _where(project, "test_case", "TC-901") == ("Header",)
        assert _where(project, "test_case", "TC-902") == (
            "Services", "Web Development")
        assert _where(project, "test_case", "TC-903") is None

    def test_reupload_does_not_undo_a_manual_move(self, client, project):
        csv = "ID,Summary,Steps,Expected\n" f'TC-901,"{LOGO}",Open,Logo\n'
        self._upload(client, project, csv, "/test-cases/upload")
        manual = db.create_folder(project, "Moved by hand")
        db.assign_to_folder(project, "test_case", ["TC-901"], manual["id"])
        self._upload(client, project, csv, "/test-cases/upload")
        assert _where(project, "test_case", "TC-901") == ("Moved by hand",)

    def test_checklist_upload_files_by_prefix(self, client, project):
        csv = ("ID,Section,Objective\n"
               f'HDR_901,Header,"{LOGO}"\n')
        assert self._upload(client, project, csv,
                            "/checklist/upload").status_code == 200
        assert _where(project, "checklist_item", "HDR_901") == ("Header",)


class TestGeneration:

    def test_generation_writes_the_wbs_prefix_and_files_by_it(self, project,
                                                          testfort_page):
        from routes.generation import _file_into_folders
        tcs = [_case("SA1_001", url_pattern="*/careers*"),
               _case("SA1_002", section="Security"),
               _case("SA1_003", "[Services -> Web Development] Verify that x")]
        cls = [_item("HDR_001"), _item("CNT_001", section="Page Content")]
        pages = [testfort_page,
                 {"url": "https://testfort.com/careers", "title": "Careers"}]
        _file_into_folders(project, pages, tcs, cls)

        # The WBS decides (engine/wbs.py): the page the URL names, the
        # site-wide topic, the chrome — and a prefix a writer guessed is
        # replaced by the node's path rather than kept beside it.
        assert tcs[0]["summary"] == \
            "[Why Us -> Careers] Verify that SA1_001 works"
        assert tcs[1]["summary"].startswith(
            "[Cross-cutting -> Security] Verify that")
        assert tcs[2]["summary"] == "[Cross-cutting -> General] Verify that x"
        assert cls[0]["objective"].startswith("[Header] Verify that")
        assert cls[1]["objective"].startswith("[Homepage] Verify that")
        for d, field in ((tcs[0], "summary"), (tcs[2], "summary"),
                         (cls[0], "objective")):
            assert _path_of(project, d["folder_id"]) == fpx.parse(d[field])
