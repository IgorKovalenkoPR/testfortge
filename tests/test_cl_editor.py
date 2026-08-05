"""The Checklist editor — requirement 6 (E4.4).

Fields are config over the E4.1 substrate and the E4.2 component. What is
checklist-specific and tested here: order (which is row order), numbering
(which the house style says is a stable identifier), sections, and the HTTP
surface for all three.

The numbering rules under test come from
``engine/qa_knowledge/style/checklist_style.yaml`` — measured from the team's
own reference checklist — not from a preference of mine. See
tests/test_checklist_order.py for the unit-level statement of them.
"""
from __future__ import annotations

import pytest

from engine import db, editable


def _item(item_id, section="Header", number="", **overrides):
    row = {
        "id": item_id, "section": section, "item_num": number, "depth": 2,
        "objective": f"Verify that {item_id} is correct",
        "comments": "", "user_story_id": "", "category": "Positive",
        "priority": "High", "status": "Unchecked",
        "testing_type": "Functional",
    }
    row.update(overrides)
    return row


@pytest.fixture
def project(app, request):
    """This test's own project — see the note in tests/test_tc_editor.py."""
    pid = db.upsert_project(name=f"E4.4 {request.node.name}"[:180])
    db.save_checklist(pid, [
        _item("HDR_001", "Header", "1.1"),
        _item("HDR_002", "Header", "1.2"),
        _item("FTR_001", "Footer", "2.1"),
    ])
    return pid


@pytest.fixture
def editing_on(monkeypatch):
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("EDITORS_ENABLED", "1")
    return True


def _stored(project_id):
    return [(row["id"], row["item_num"], row["section"])
            for row in db.load_checklist(project_id)]


class TestFields:
    """The part that needed no new code — proving it needed none."""

    def test_an_objective_is_patched_like_any_field(self, project,
                                                    editing_on):
        item = editable.patch("checklist_item", project, "HDR_001",
                              {"objective": "Verify that the logo links home"})
        assert item["objective"] == "Verify that the logo links home"
        assert item["ai_generated"] is False
        assert item["row_version"] == 2

    def test_a_field_outside_the_allowlist_is_refused(self, project,
                                                     editing_on):
        with pytest.raises(editable.FieldNotEditable):
            editable.patch("checklist_item", project, "HDR_001",
                           {"depth": 3})

    def test_house_style_advice_covers_the_objective(self):
        """The checklist's title field is linted like a test case summary."""
        from engine import tc_author
        findings = tc_author.house_style_findings("objective",
                                                 "the logo links home")
        assert findings


class TestCreateAndDelete:

    def test_a_new_item_is_appended_with_the_next_free_number(
            self, client, project, editing_on):
        """The style file's append rule: one past the highest sibling."""
        headers = _prepare(client, project)
        resp = client.post("/api/edit/checklist_item",
                           json={"values": {"section": "Header",
                                            "objective": "By hand"}},
                           headers=headers)
        assert resp.status_code == 201
        assert resp.get_json()["item"]["item_num"] == "1.3"

    def test_a_new_item_is_not_ai_generated(self, client, project,
                                           editing_on):
        headers = _prepare(client, project)
        resp = client.post("/api/edit/checklist_item", json={"values": {}},
                           headers=headers)
        assert resp.get_json()["item"]["ai_generated"] is False

    def test_the_siblings_are_not_renumbered_by_an_insert(
            self, client, project, editing_on):
        """The anti-pattern the style file names: "renumbering siblings to
        insert a check", because the numbers are cited in bug reports."""
        headers = _prepare(client, project)
        client.post("/api/edit/checklist_item",
                    json={"values": {"section": "Header"}}, headers=headers)
        stored = dict((row[0], row[1]) for row in _stored(project))
        assert stored["HDR_001"] == "1.1"
        assert stored["HDR_002"] == "1.2"
        assert stored["FTR_001"] == "2.1"

    def test_a_new_item_lands_inside_its_section_block(self, client, project,
                                                      editing_on):
        """Found in the browser, and the number alone did not fix it.

        ``editable.create`` appends to the end of the *pack*, which is the end
        of whichever section happens to be last. The template opens a new
        section block whenever the name changes between adjacent rows, so
        adding an item to "Header" while "Footer" was last rendered a **second
        "Header" heading** at the bottom of the table — with the item under it.
        """
        headers = _prepare(client, project)
        resp = client.post("/api/edit/checklist_item",
                           json={"values": {"section": "Header"}},
                           headers=headers)
        new_id = resp.get_json()["item"]["id"]
        rows = _stored(project)
        # Contiguous blocks: every row of a section sits together.
        blocks = []
        for _, _, section in rows:
            if not blocks or blocks[-1] != section:
                blocks.append(section)
        assert blocks == ["Header", "Footer"], \
            f"one block per section, got {blocks} from {rows}"
        header = [row[0] for row in rows if row[2] == "Header"]
        assert header[-1] == new_id, \
            "the new item belongs at the end of Header, not of the pack"
        assert rows[-1][2] == "Footer", "Footer is still the last block"

    def test_deleting_leaves_the_number_vacated(self, client, project,
                                                editing_on):
        """A gap is the honest result. Closing it would move numbers other
        people have written down."""
        headers = _prepare(client, project)
        resp = client.delete("/api/edit/checklist_item/HDR_001",
                             headers=headers)
        assert resp.status_code == 200
        assert _stored(project) == [("HDR_002", "1.2", "Header"),
                                    ("FTR_001", "2.1", "Footer")]

    def test_a_stale_version_refuses_the_delete(self, client, project,
                                                editing_on):
        headers = _prepare(client, project)
        resp = client.delete(
            "/api/edit/checklist_item/HDR_001?row_version=99", headers=headers)
        assert resp.status_code == 409
        assert len(db.load_checklist(project)) == 3


class TestMove:

    def test_moving_down_swaps_the_pair_and_their_numbers(
            self, client, project, editing_on):
        headers = _prepare(client, project)
        resp = client.post("/api/edit/checklist_item/HDR_001/move",
                           json={"delta": 1}, headers=headers)
        assert resp.status_code == 200
        assert _stored(project) == [("HDR_002", "1.1", "Header"),
                                    ("HDR_001", "1.2", "Header"),
                                    ("FTR_001", "2.1", "Footer")]

    def test_only_the_moved_section_is_renumbered(self, client, project,
                                                 editing_on):
        headers = _prepare(client, project)
        client.post("/api/edit/checklist_item/HDR_001/move",
                    json={"delta": 1}, headers=headers)
        footer = [row for row in _stored(project) if row[2] == "Footer"]
        assert footer == [("FTR_001", "2.1", "Footer")]

    def test_the_order_survives_a_reload(self, client, project, editing_on):
        """Order is row order, so the move has to rewrite the pack — the
        point of doing it server-side rather than in the DOM."""
        headers = _prepare(client, project)
        client.post("/api/edit/checklist_item/HDR_001/move",
                    json={"delta": 1}, headers=headers)
        assert [row[0] for row in _stored(project)][:2] == ["HDR_002",
                                                            "HDR_001"]

    def test_a_move_at_a_section_edge_is_a_no_op_not_an_error(
            self, client, project, editing_on):
        headers = _prepare(client, project)
        resp = client.post("/api/edit/checklist_item/HDR_002/move",
                           json={"delta": 1}, headers=headers)
        assert resp.status_code == 200
        assert _stored(project)[:2] == [("HDR_001", "1.1", "Header"),
                                        ("HDR_002", "1.2", "Header")]

    def test_provenance_survives_the_pack_rewrite(self, client, project,
                                                 editing_on):
        """``save_checklist`` deletes and re-inserts. E4.1's metadata is
        carried across on the public id — without that a reorder would hand
        an edited item back to the next regeneration."""
        editable.patch("checklist_item", project, "FTR_001",
                       {"objective": "Verify that the copyright year is current"})
        headers = _prepare(client, project)
        client.post("/api/edit/checklist_item/HDR_001/move",
                    json={"delta": 1}, headers=headers)
        meta = db.load_edit_metadata(project, "checklist")
        assert meta["FTR_001"]["ai_generated"] is False
        assert meta["FTR_001"]["row_version"] == 2

    def test_an_unknown_item_is_a_400_naming_it(self, client, project,
                                                editing_on):
        headers = _prepare(client, project)
        resp = client.post("/api/edit/checklist_item/NOPE_001/move",
                           json={"delta": 1}, headers=headers)
        assert resp.status_code == 400
        assert "NOPE_001" in resp.get_json()["message"]


class TestRelocate:

    def test_the_item_joins_the_destination_section(self, client, project,
                                                   editing_on):
        headers = _prepare(client, project)
        resp = client.post("/api/edit/checklist_item/FTR_001/section",
                           json={"section": "Header"}, headers=headers)
        assert resp.status_code == 200
        assert _stored(project) == [("HDR_001", "1.1", "Header"),
                                    ("HDR_002", "1.2", "Header"),
                                    ("FTR_001", "1.3", "Header")]

    def test_a_section_change_through_the_generic_patch_also_regroups(
            self, client, project, editing_on):
        """The editor uses the dedicated endpoint, but this path exists — and
        an item left where it was makes the page render a second heading for
        the same section further down."""
        headers = _prepare(client, project)
        resp = client.patch("/api/edit/checklist_item/FTR_001",
                            json={"changes": {"section": "Header"}},
                            headers=headers)
        assert resp.status_code == 200
        assert [row[2] for row in _stored(project)] == ["Header"] * 3
        assert _stored(project)[-1][1] == "1.3"

    def test_an_empty_destination_is_refused(self, client, project,
                                             editing_on):
        headers = _prepare(client, project)
        resp = client.post("/api/edit/checklist_item/FTR_001/section",
                           json={"section": "   "}, headers=headers)
        assert resp.status_code == 400


class TestRenameSection:

    def test_every_item_in_the_section_follows(self, client, project,
                                              editing_on):
        headers = _prepare(client, project)
        resp = client.post("/api/edit/checklist/rename-section",
                           json={"from": "Header", "to": "Top bar"},
                           headers=headers)
        assert resp.status_code == 200
        assert [row[2] for row in _stored(project)] == ["Top bar", "Top bar",
                                                        "Footer"]

    def test_nothing_is_renumbered(self, client, project, editing_on):
        headers = _prepare(client, project)
        client.post("/api/edit/checklist/rename-section",
                    json={"from": "Header", "to": "Top bar"}, headers=headers)
        assert [row[1] for row in _stored(project)] == ["1.1", "1.2", "2.1"]

    def test_renaming_onto_an_existing_section_is_refused_with_advice(
            self, client, project, editing_on):
        headers = _prepare(client, project)
        resp = client.post("/api/edit/checklist/rename-section",
                           json={"from": "Footer", "to": "Header"},
                           headers=headers)
        assert resp.status_code == 400
        assert "already exists" in resp.get_json()["message"]
        assert [row[2] for row in _stored(project)] == ["Header", "Header",
                                                        "Footer"]

    def test_one_audit_row_for_the_whole_operation(self, client, project,
                                                  editing_on):
        """The person performed one action; N rows would bury the edits that
        matter (the E4.9 bulk-ops rule)."""
        headers = _prepare(client, project)
        client.post("/api/edit/checklist/rename-section",
                    json={"from": "Header", "to": "Top bar"}, headers=headers)
        rows = [row for row in db.list_audit(project_id=project, limit=20)
                if row["action"] == "reorder"]
        assert len(rows) == 1


class TestCsrfAndGating:

    @pytest.mark.parametrize("path,body", [
        ("/api/edit/checklist_item/HDR_001/move", {"delta": 1}),
        ("/api/edit/checklist_item/HDR_001/section", {"section": "Footer"}),
        ("/api/edit/checklist/rename-section", {"from": "Header",
                                                "to": "Top"}),
    ])
    def test_every_new_endpoint_needs_a_token(self, client, project,
                                             editing_on, path, body):
        """This app protects every non-GET method. Without the token a new
        endpoint passes the suite and 400s in production."""
        with client.session_transaction() as sess:
            sess["project_id"] = project
        client.application.config["WTF_CSRF_ENABLED"] = True
        try:
            assert client.post(path, json=body).status_code == 400
        finally:
            client.application.config["WTF_CSRF_ENABLED"] = False

    @pytest.mark.parametrize("path", [
        "/api/edit/checklist_item/HDR_001/move",
        "/api/edit/checklist_item/HDR_001/section",
        "/api/edit/checklist/rename-section",
    ])
    def test_nothing_is_reachable_with_editing_off(self, client, project,
                                                   monkeypatch, path):
        monkeypatch.setenv("EDITORS_ENABLED", "0")
        headers = _prepare(client, project)
        resp = client.post(path, json={}, headers=headers)
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "editors_disabled"


class TestPage:

    def test_every_editable_field_is_marked_up(self, client, project,
                                               editing_on):
        body = _render(client, project)
        for field in ("objective", "category", "priority", "comments"):
            assert f'data-ie-field="{field}"' in body, field

    def test_each_row_carries_its_id_and_number(self, client, project,
                                               editing_on):
        body = _render(client, project)
        assert 'data-cl-id="HDR_001"' in body
        assert "data-cl-num" in body

    def test_the_row_controls_are_offered(self, client, project, editing_on):
        body = _render(client, project)
        for marker in ('data-cl-move="-1"', 'data-cl-move="1"',
                       'data-cl-relocate="Header"',
                       'data-cl-delete="HDR_001"'):
            assert marker in body, marker

    def test_the_section_header_offers_rename_and_add(self, client, project,
                                                      editing_on):
        body = _render(client, project)
        assert 'data-cl-rename="Header"' in body
        assert 'data-cl-add="Header"' in body

    def test_move_controls_are_disabled_at_the_section_edges(
            self, client, project, editing_on):
        """A move is bounded by its section. ``loop.first``/``loop.last``
        describe the whole pack, so with them the last item of section 1
        offered a "move down" the server correctly refused — which reads as
        a broken button."""
        body = _render(client, project)
        rows = {}
        for item_id in ("HDR_001", "HDR_002", "FTR_001"):
            block = body.split(f'data-cl-id="{item_id}"', 1)[1].split(
                "</tr>", 1)[0]
            up = block.split('data-cl-move="-1"', 1)[1].split(">", 1)[0]
            down = block.split('data-cl-move="1"', 1)[1].split(">", 1)[0]
            rows[item_id] = ("disabled" in up, "disabled" in down)
        assert rows["HDR_001"] == (True, False), "first in Header"
        assert rows["HDR_002"] == (False, True), "last in Header"
        assert rows["FTR_001"] == (True, True), "alone in Footer"

    def test_the_section_list_is_offered_for_relocation(self, client,
                                                        project, editing_on):
        assert 'data-cl-sections="Header|Footer"' in _render(client, project)

    def test_all_three_scripts_are_loaded_in_order(self, client, project,
                                                   editing_on):
        """cl-editor.js reads ``window.TestFortgeEditor`` as it loads, and
        deferred scripts run in document order."""
        body = _render(client, project)
        assert body.index("js/editor-shared.js") < body.index("js/cl-editor.js")
        assert "js/inline-edit.js" in body

    def test_the_editor_is_absent_with_the_flag_off(self, client, project,
                                                    monkeypatch):
        monkeypatch.setenv("EDITORS_ENABLED", "0")
        body = _render(client, project)
        for marker in ("data-cl-move", "data-cl-delete", "data-cl-rename",
                       "js/cl-editor.js", "js/inline-edit.js",
                       "data-cl-editor-status"):
            assert marker not in body, marker

    def test_the_objectives_still_render_with_the_flag_off(self, client,
                                                           project,
                                                           monkeypatch):
        monkeypatch.setenv("EDITORS_ENABLED", "0")
        body = _render(client, project)
        assert "Verify that HDR_001 is correct" in body
        assert "1.1" in body


class TestSharedPlumbing:
    """One copy of the CSRF header, not one per editor."""

    @staticmethod
    def _read(name):
        import pathlib
        return pathlib.Path(f"static/js/{name}").read_text(encoding="utf-8")

    @staticmethod
    def _code(name):
        """The file with its comments removed.

        Needed because these files *explain* what they avoid — "No inline
        handlers and no innerHTML: the CSP has no unsafe-inline for scripts" —
        and a probe over the raw text flags the explanation. A test that
        cannot tell prose from code fails on good documentation.
        """
        import pathlib
        import re
        body = pathlib.Path(f"static/js/{name}").read_text(encoding="utf-8")
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
        return re.sub(r"^\s*//.*$", "", body, flags=re.MULTILINE)

    def test_the_shared_module_owns_the_csrf_header(self):
        shared = self._read("editor-shared.js")
        assert "X-CSRFToken" in shared
        assert "window.TestFortgeEditor" in shared

    @pytest.mark.parametrize("name", ["tc-editor.js", "cl-editor.js"])
    def test_no_editor_sends_its_own_csrf_header(self, name):
        """Four copies of this would be four chances to forget it — which
        passes a suite that disables CSRF and 400s in production."""
        body = self._read(name)
        assert "X-CSRFToken" not in body
        assert "window.TestFortgeEditor" in body

    @pytest.mark.parametrize("name", ["tc-editor.js", "cl-editor.js",
                                      "editor-shared.js", "inline-edit.js"])
    def test_no_editor_uses_inline_handlers_or_innerhtml(self, name):
        """The CSP has no unsafe-inline for scripts, and every value in these
        files is somebody's test documentation."""
        body = self._code(name)
        for forbidden in ("innerHTML", "eval(", "setAttribute('on",
                          'setAttribute("on', ".onclick ="):
            assert forbidden not in body, forbidden

    def test_the_checklist_editor_does_not_reimplement_numbering(self):
        """The house style decides which numbers may change. A second
        implementation in JavaScript would eventually disagree, and the
        disagreement would only show up when somebody cited "2.4"."""
        body = self._code("cl-editor.js")
        assert "item_num" in body, "it should read numbers from the response"
        for computed in ("+ 1", "parseInt", "split('.')"):
            assert computed not in body, computed


# ── Helpers ───────────────────────────────────────────────────────

def _prepare(client, project):
    with client.session_transaction() as sess:
        sess["project_id"] = project
    token = client.get("/api/csrf-token").get_json()["token"]
    return {"X-CSRFToken": token}


def _render(client, project):
    with client.session_transaction() as sess:
        sess["project_id"] = project
    resp = client.get("/checklist")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)
