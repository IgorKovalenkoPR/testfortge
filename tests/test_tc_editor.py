"""The Test Cases editor — requirement 5 (E4.3).

Three things beyond E4.1's field patching: whole items can be created and
deleted, steps are a list rather than a blob, and a save comes back with
house-style advice.

CSRF is deliberately exercised here. The app protects every non-GET method,
so a new POST/DELETE that nobody sends a token to passes a test suite with
``WTF_CSRF_ENABLED=False`` and 400s in production.
"""
from __future__ import annotations

import pytest

from sqlalchemy import text

from engine import db, editable, tc_author, workspace


# ── Fixtures ──────────────────────────────────────────────────────

def _case(external_id="SC1_001", **overrides):
    row = {
        "id": external_id, "section": "Auth", "section_num": 1,
        "summary": "Verify that a user signs in with valid credentials",
        "preconditions": "An account exists",
        "test_steps": "1. Open /login\n2. Enter the credentials\n3. Submit",
        "test_data": "user@example.com",
        "expected_result": "The dashboard opens",
        "issues": "", "comment": "", "user_story_id": "US-1",
        "category": "Positive", "priority": "High", "status": "Unchecked",
        "testing_type": "Functional", "suite": "Smoke",
    }
    row.update(overrides)
    return row


@pytest.fixture
def project(app, request):
    """A project of this test's own.

    Not a shared name: ``upsert_project`` keys on it, so every test would
    reuse one row — and because E4.1 deliberately preserves ``row_version``
    across a pack save (a pack write must not erase somebody's provenance),
    a version bumped by an earlier test survived into the next one's fixture.
    The assertions that read a version then passed or failed by test order.
    """
    pid = db.upsert_project(name=f"E4.3 {request.node.name}"[:180])
    db.save_test_cases(pid, [_case()])
    return pid


@pytest.fixture
def editing_on(monkeypatch):
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("EDITORS_ENABLED", "1")
    return True


# ── Creating a case by hand ───────────────────────────────────────

class TestCreate:

    def test_a_new_case_is_not_ai_generated(self, project, editing_on):
        """Nobody generated it, and E4.7 reads this flag to decide what a
        regeneration may overwrite. A hand-written case surviving Generate is
        the whole point of the flag."""
        item = editable.create("test_case", project,
                               {"summary": "Written by hand"})
        assert item["ai_generated"] is False
        assert item["row_version"] == 1

    def test_the_id_ignores_generator_ids(self, project, editing_on):
        """The seeded case is SC1_001, so the first hand-written one is
        TC-001 — not TC-002 counted off a prefix it does not share."""
        assert editable.create("test_case", project, {})["id"] == "TC-001"
        assert editable.create("test_case", project, {})["id"] == "TC-002"

    def test_defaults_match_a_generated_case(self, project, editing_on):
        """A new case has to appear in the execution list beside the rest,
        which means the same status vocabulary."""
        item = editable.create("test_case", project, {})
        assert item["status"] == "Unchecked"
        assert item["priority"] == "Medium"

    def test_supplied_values_win_over_defaults(self, project, editing_on):
        item = editable.create("test_case", project, {"priority": "High"})
        assert item["priority"] == "High"

    def test_a_field_outside_the_allowlist_is_refused(self, project,
                                                     editing_on):
        with pytest.raises(editable.FieldNotEditable):
            editable.create("test_case", project, {"external_id": "TC-999"})

    def test_creating_is_audited(self, project, editing_on):
        item = editable.create("test_case", project, {"summary": "Audited"})
        rows = [r for r in db.list_audit(limit=20)
                if r["action"] == "create" and r["entity_id"] == item["id"]]
        assert rows, "a created case must leave an audit row"

    def test_creating_bumps_the_pack_version(self, project, editing_on):
        """A wipe-and-replace save that started before this must be refused
        rather than silently drop the new case (E3.5's contract)."""
        before = db.pack_versions(project)["test_cases"]
        editable.create("test_case", project, {})
        assert db.pack_versions(project)["test_cases"] > before

    def test_an_entity_that_does_not_allow_it_says_so(self, project,
                                                      editing_on):
        with pytest.raises(editable.NotCreatable):
            editable.create("checklist_item", project, {})

    def test_no_project_is_refused(self, editing_on, app):
        with pytest.raises(editable.EntityNotFound):
            editable.create("test_case", "", {})


# ── Deleting a case ───────────────────────────────────────────────

class TestRemove:

    def test_a_case_is_gone_afterwards(self, project, editing_on):
        editable.remove("test_case", project, "SC1_001")
        assert [c["id"] for c in db.load_test_cases(project)] == []

    def test_a_stale_version_is_a_conflict(self, project, editing_on):
        """Deleting an item somebody just edited destroys work newer than
        what the deleter was looking at."""
        with pytest.raises(db.WriteConflict):
            editable.remove("test_case", project, "SC1_001",
                            expected_version=99)
        assert db.load_test_cases(project), "the refused delete must not apply"

    def test_the_matching_version_is_accepted(self, project, editing_on):
        editable.remove("test_case", project, "SC1_001", expected_version=1)
        assert not db.load_test_cases(project)

    def test_another_projects_case_is_not_found(self, project, editing_on):
        other = db.upsert_project(name="somebody else")
        db.save_test_cases(other, [_case("TC-500")])
        with pytest.raises(editable.EntityNotFound):
            editable.remove("test_case", project, "TC-500")
        assert db.load_test_cases(other), "the other project keeps its case"

    def test_deleting_is_audited_with_what_was_lost(self, project,
                                                    editing_on):
        editable.remove("test_case", project, "SC1_001")
        row = next(r for r in db.list_audit(limit=20)
                   if r["action"] == "delete")
        assert "summary" in (row["diff"] or {}), \
            "the audit must record what the deleted case said"

    def test_deleting_bumps_the_pack_version(self, project, editing_on):
        before = db.pack_versions(project)["test_cases"]
        editable.remove("test_case", project, "SC1_001")
        assert db.pack_versions(project)["test_cases"] > before

    def test_an_entity_that_does_not_allow_it_says_so(self, project,
                                                      editing_on):
        with pytest.raises(editable.NotDeletable):
            editable.remove("checklist_item", project, "CL-001")


# ── Edit metadata for the page ────────────────────────────────────

class TestEditMetadata:

    def test_versions_come_back_keyed_by_public_id(self, project,
                                                   editing_on):
        meta = db.load_edit_metadata(project)
        assert meta["SC1_001"]["row_version"] == 1
        assert meta["SC1_001"]["ai_generated"] is True

    def test_an_edit_shows_up_as_provenance(self, project, editing_on):
        editable.patch("test_case", project, "SC1_001",
                       {"summary": "Verify that a user signs in"})
        meta = db.load_edit_metadata(project)
        assert meta["SC1_001"]["row_version"] == 2
        assert meta["SC1_001"]["ai_generated"] is False

    def test_rows_with_no_public_id_are_skipped(self, project, editing_on):
        """The edit endpoint is keyed on external_id, so offering a version
        for a row that has none would invite a 404."""
        from sqlalchemy import text
        with db.session_scope() as sess:
            sess.execute(text(
                "UPDATE test_case SET external_id = NULL "
                "WHERE external_id = 'SC1_001'"))
        assert db.load_edit_metadata(project) == {}

    def test_the_repository_is_empty_while_session_first(self, project,
                                                         monkeypatch):
        """Without a row in the database there is no version to hold — and
        an editor cannot be reached in that configuration anyway."""
        monkeypatch.setenv("WORKSPACE_DB_FIRST", "0")
        assert workspace.edit_metadata(project) == {}


@pytest.fixture
def planted_duplicate(app, request):
    """Two checklist rows sharing a public id, planted below ``save_*``.

    E4.4a stopped the writers from producing this and added a unique index,
    so a duplicate can no longer be created through the normal path — which
    is the point. The guard below it stays anyway, and stays tested: the
    index creation is best-effort (an instance whose historical data could
    not be repaired runs without it), and any future writer that bypasses
    ``save_checklist`` would reintroduce the state. A guard that is only
    exercised by the bug it prevents is a guard nobody knows still works.
    """
    project = db.upsert_project(name=f"dupe {request.node.name}"[:180])
    # The ORM's own bind — see the note in tests/test_public_ids.py.
    with db.session_scope() as sess:
        engine = sess.get_bind()
    with engine.begin() as conn:
        conn.execute(text(
            "DROP INDEX IF EXISTS ux_checklist_item_project_external_id"))
        for objective in ("First", "Second"):
            conn.execute(text(
                "INSERT INTO checklist_item (project_id, external_id, "
                "objective, created_at, updated_at, row_version, "
                "ai_generated) VALUES (:p, 'CNT_001', :o, '2026-01-01', "
                "'2026-01-01', 1, 1)"), {"p": project, "o": objective})
    yield project
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM checklist_item WHERE project_id = :p"),
                     {"p": project})
    db._ensure_public_id_unique_indexes(engine)


class TestDuplicatePublicIds:
    """A measured constraint on this substrate, not a hypothetical.

    Every entity here is addressed by its public id, and the checklist
    generators used to emit duplicates — measured on ``POST /checklist`` for
    https://example.com: an 82-item pack containing ``CNT_001`` twice. So
    "edit item CNT_001" was undefined for that pack, and a unique index over
    the column made every checklist save roll back.

    E4.4a fixed the source (see tests/test_public_ids.py). What is pinned
    here is the substrate's own behaviour: if a duplicate does exist, it is
    refused rather than resolved by picking a row.
    """

    def test_test_case_ids_are_unique_per_project(self, project, editing_on):
        """The index the editor's id minting relies on, so a same-instant
        collision is a retryable IntegrityError instead of two TC-004s."""
        from sqlalchemy import text
        with db.session_scope() as sess:
            with pytest.raises(Exception):
                sess.execute(text(
                    "INSERT INTO test_case (project_id, external_id) "
                    "VALUES (:p, 'SC1_001')"), {"p": project})

    def test_a_checklist_save_is_not_blocked_by_duplicate_ids(self, project,
                                                              editing_on):
        """The generator's own output has to be storable — E4.4a makes the
        ids unique on the way in rather than rejecting the pack."""
        items = [
            {"id": "CNT_001", "section": "Content", "objective": "First",
             "item_num": "1.1"},
            {"id": "CNT_001", "section": "Content", "objective": "Second",
             "item_num": "1.2"},
        ]
        db.save_checklist(project, items)
        stored = db.load_checklist(project)
        assert len(stored) == 2
        assert len({row["id"] for row in stored}) == 2

    def test_an_ambiguous_id_is_refused_not_guessed(self, planted_duplicate,
                                                    editing_on):
        """Picking either row would be a coin toss over somebody's docs."""
        with pytest.raises(editable.AmbiguousEntity):
            editable.get("checklist_item", planted_duplicate, "CNT_001")
        with pytest.raises(editable.AmbiguousEntity):
            editable.patch("checklist_item", planted_duplicate, "CNT_001",
                           {"objective": "Third"})

    def test_the_endpoint_says_which_problem_it_is(self, client,
                                                   planted_duplicate,
                                                   editing_on):
        with client.session_transaction() as sess:
            sess["project_id"] = planted_duplicate
        resp = client.get("/api/edit/checklist_item/CNT_001")
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "ambiguous_id"


# ── House-style advice ────────────────────────────────────────────

class TestHouseStyle:

    def test_a_summary_without_the_opener_is_flagged(self):
        findings = tc_author.house_style_findings("summary",
                                                  "the user signs in")
        assert any("Verify" in f for f in findings)

    def test_the_corpus_error_message_grammar_is_exempt(self):
        """"<Surface>: <attempted action>" is a sanctioned alternate title
        form and legitimately does not open with a verb."""
        findings = tc_author.house_style_findings(
            "summary", "Login page: Submit an empty password")
        assert not any("Verify" in f for f in findings)

    def test_a_modal_in_an_expected_result_is_reported_once(self):
        """Measured: the glossary already reports "must" as reading like a
        requirement, so a second check said the same thing twice."""
        findings = tc_author.house_style_findings(
            "expected_result", "The dashboard must open")
        modal = [f for f in findings if "must" in f]
        assert len(modal) == 1, findings

    def test_steps_are_reported_by_position(self):
        """"step 3 is a placeholder" is actionable; "the steps are" is not."""
        findings = tc_author.house_style_findings(
            "test_steps", "1. Open /login\n2. Perform the action")
        assert any(f.startswith("step 2:") for f in findings)
        assert not any(f.startswith("step 1:") for f in findings)

    def test_a_placeholder_only_tc_author_knows_is_still_caught(self):
        """The two rule sets overlap but neither contains the other."""
        findings = tc_author.house_style_findings(
            "test_steps", "1. Open the relevant page")
        assert findings and findings[0].startswith("step 1:")

    def test_a_field_with_no_rules_is_silent(self):
        assert tc_author.house_style_findings("priority", "High") == []

    def test_empty_text_is_silent(self):
        assert tc_author.house_style_findings("summary", "") == []


# ── The HTTP surface ──────────────────────────────────────────────

class TestEndpoints:

    @staticmethod
    def _prepare(client, project):
        with client.session_transaction() as sess:
            sess["project_id"] = project
        token = client.get("/api/csrf-token").get_json()["token"]
        return {"X-CSRFToken": token}

    def test_create_returns_201_and_the_item(self, client, project,
                                            editing_on):
        headers = self._prepare(client, project)
        resp = client.post("/api/edit/test_case",
                           json={"values": {"summary": "From the UI"}},
                           headers=headers)
        assert resp.status_code == 201
        assert resp.get_json()["item"]["summary"] == "From the UI"

    def test_delete_refuses_a_stale_version(self, client, project,
                                            editing_on):
        headers = self._prepare(client, project)
        resp = client.delete("/api/edit/test_case/SC1_001?row_version=99",
                             headers=headers)
        assert resp.status_code == 409
        assert db.load_test_cases(project), "nothing may be deleted on 409"

    def test_delete_rejects_a_non_numeric_version(self, client, project,
                                                  editing_on):
        headers = self._prepare(client, project)
        resp = client.delete("/api/edit/test_case/SC1_001?row_version=soon",
                             headers=headers)
        assert resp.status_code == 400

    @pytest.mark.parametrize("body,expected", [
        ({"op": "add", "text": "4. Confirm the dashboard"},
         ["Open /login", "Enter the credentials", "Submit",
          "Confirm the dashboard"]),
        ({"op": "remove", "index": 1},
         ["Open /login", "Submit"]),
        ({"op": "move", "index": 0, "delta": 1},
         ["Enter the credentials", "Open /login", "Submit"]),
        ({"op": "edit", "index": 2, "text": "Press Sign in"},
         ["Open /login", "Enter the credentials", "Press Sign in"]),
    ])
    def test_every_step_operation(self, client, project, editing_on,
                                  body, expected):
        headers = self._prepare(client, project)
        resp = client.post("/api/edit/test_case/SC1_001/steps",
                           json=body, headers=headers)
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["steps"] == expected

    def test_a_step_operation_persists_renumbered(self, client, project,
                                                  editing_on):
        headers = self._prepare(client, project)
        client.post("/api/edit/test_case/SC1_001/steps",
                    json={"op": "remove", "index": 0}, headers=headers)
        stored = db.load_test_cases(project)[0]["test_steps"]
        assert stored == "1. Enter the credentials\n2. Submit", \
            "deleting a step must renumber the rest, not leave a gap"

    def test_a_stale_step_operation_is_a_conflict(self, client, project,
                                                 editing_on):
        """Checked before the operation is applied, so a stale reorder is
        refused rather than computed and thrown away."""
        headers = self._prepare(client, project)
        resp = client.post("/api/edit/test_case/SC1_001/steps",
                           json={"op": "move", "index": 0, "delta": 1,
                                 "row_version": 99}, headers=headers)
        assert resp.status_code == 409
        assert db.load_test_cases(project)[0]["test_steps"].startswith(
            "1. Open /login")

    def test_a_step_operation_bumps_the_row_version(self, client, project,
                                                    editing_on):
        headers = self._prepare(client, project)
        resp = client.post("/api/edit/test_case/SC1_001/steps",
                           json={"op": "add", "text": "Sign out",
                                 "row_version": 1}, headers=headers)
        assert resp.get_json()["item"]["row_version"] == 2

    def test_an_unknown_operation_is_named_in_the_400(self, client, project,
                                                      editing_on):
        headers = self._prepare(client, project)
        resp = client.post("/api/edit/test_case/SC1_001/steps",
                           json={"op": "shuffle"}, headers=headers)
        assert resp.status_code == 400
        assert "shuffle" in resp.get_json()["message"]

    def test_an_empty_step_is_refused(self, client, project, editing_on):
        headers = self._prepare(client, project)
        resp = client.post("/api/edit/test_case/SC1_001/steps",
                           json={"op": "add", "text": "   "},
                           headers=headers)
        assert resp.status_code == 400

    def test_steps_on_a_missing_case_are_404(self, client, project,
                                             editing_on):
        headers = self._prepare(client, project)
        resp = client.post("/api/edit/test_case/TC-404/steps",
                           json={"op": "add", "text": "x"}, headers=headers)
        assert resp.status_code == 404

    def test_a_save_carries_advisory_warnings(self, client, project,
                                              editing_on):
        """Advice, not a gate: the save succeeded."""
        headers = self._prepare(client, project)
        resp = client.patch(
            "/api/edit/test_case/SC1_001",
            json={"changes": {"expected_result": "The result must be correct"}},
            headers=headers)
        assert resp.status_code == 200
        assert resp.get_json()["warnings"]["expected_result"]
        assert db.load_test_cases(project)[0]["expected_result"] == \
            "The result must be correct"

    def test_creating_needs_a_csrf_token(self, client, project, editing_on):
        """Every non-form endpoint in this app has to be checked for this:
        without the token it passes the suite and 400s in production."""
        with client.session_transaction() as sess:
            sess["project_id"] = project
        client.application.config["WTF_CSRF_ENABLED"] = True
        try:
            resp = client.post("/api/edit/test_case", json={"values": {}})
            assert resp.status_code == 400
        finally:
            client.application.config["WTF_CSRF_ENABLED"] = False

    def test_deleting_needs_a_csrf_token(self, client, project, editing_on):
        with client.session_transaction() as sess:
            sess["project_id"] = project
        client.application.config["WTF_CSRF_ENABLED"] = True
        try:
            resp = client.delete("/api/edit/test_case/SC1_001")
            assert resp.status_code == 400
            assert db.load_test_cases(project)
        finally:
            client.application.config["WTF_CSRF_ENABLED"] = False

    @pytest.mark.parametrize("method,path", [
        ("post", "/api/edit/test_case"),
        ("delete", "/api/edit/test_case/SC1_001"),
        ("post", "/api/edit/test_case/SC1_001/steps"),
    ])
    def test_nothing_is_reachable_with_editing_off(self, client, project,
                                                    monkeypatch, method,
                                                    path):
        monkeypatch.setenv("EDITORS_ENABLED", "0")
        headers = self._prepare(client, project)
        resp = getattr(client, method)(path, json={}, headers=headers)
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "editors_disabled"


# ── The page ──────────────────────────────────────────────────────

class TestPage:

    @staticmethod
    def _render(client, project):
        with client.session_transaction() as sess:
            sess["project_id"] = project
        resp = client.get("/test-cases")
        assert resp.status_code == 200
        return resp.get_data(as_text=True)

    def test_every_editable_field_is_marked_up(self, client, project,
                                               editing_on):
        body = self._render(client, project)
        for field in ("summary", "preconditions", "test_data",
                      "expected_result", "issues", "comment", "priority",
                      "category", "suite"):
            assert f'data-ie-field="{field}"' in body, field

    def test_the_row_version_is_rendered_for_the_client_to_send(
            self, client, project, editing_on):
        assert 'data-ie-version="1"' in self._render(client, project)

    def test_the_steps_are_a_list_with_controls(self, client, project,
                                               editing_on):
        body = self._render(client, project)
        assert 'data-tc-steps="SC1_001"' in body
        assert body.count('data-step-index=') == 3
        assert 'data-step-op="move"' in body
        assert 'data-step-op="remove"' in body

    def test_the_first_and_last_move_buttons_are_disabled(self, client,
                                                          project,
                                                          editing_on):
        """Greyed out rather than absent: a control that appears and vanishes
        as steps move is harder to aim at than one that stays put."""
        body = self._render(client, project)
        assert body.count("disabled") >= 2

    def test_create_and_delete_are_offered(self, client, project,
                                           editing_on):
        body = self._render(client, project)
        assert 'id="tc-create"' in body
        assert 'data-tc-delete="SC1_001"' in body

    def test_both_scripts_are_loaded(self, client, project, editing_on):
        body = self._render(client, project)
        assert "js/inline-edit.js" in body
        assert "js/tc-editor.js" in body

    def test_the_editor_is_absent_with_the_flag_off(self, client, project,
                                                    monkeypatch):
        monkeypatch.setenv("EDITORS_ENABLED", "0")
        body = self._render(client, project)
        for marker in ("data-tc-steps", "js/tc-editor.js",
                       "js/inline-edit.js", 'id="tc-create"',
                       "data-tc-delete"):
            assert marker not in body, marker

    def test_the_steps_still_read_as_text_with_the_flag_off(
            self, client, project, monkeypatch):
        """The page has to keep working exactly as it did."""
        monkeypatch.setenv("EDITORS_ENABLED", "0")
        body = self._render(client, project)
        assert "1. Open /login" in body

    def test_multi_line_values_keep_their_breaks(self):
        """The fields lost their ``nl2br`` filter when they became editable.

        The component writes with textContent — deliberately, the values are
        somebody's test data — so it cannot be handed <br> markup. CSS does
        the job instead, and without this rule three preconditions on three
        lines render as one run-on line.
        """
        import pathlib
        css = pathlib.Path("static/css/style.css").read_text(encoding="utf-8")
        assert '.ie[data-ie-kind="textarea"]' in css
        block = css.split('.ie[data-ie-kind="textarea"]', 1)[1].split("}", 1)[0]
        assert "pre-wrap" in block

    def test_the_card_is_an_edit_row_and_always_carries_the_marker(
            self, client, project, editing_on):
        """The "edited" pill has to appear without a reload.

        E4.2's CSS reveals a marker that is *inside* an element carrying
        ``data-ie-edited``. Rendering the marker only when the case was
        already edited meant a live edit could never reveal it — and marking
        only the fields (not the card) meant the selector never matched. Both
        halves are needed, so both are pinned.
        """
        body = self._render(client, project)
        assert "data-ie-row" in body
        assert "ie-edited-marker" in body, \
            "the marker must be in the DOM for the CSS to reveal"

    def test_an_edited_case_is_marked_on_the_server_too(self, client,
                                                        project,
                                                        editing_on):
        """For the case that was already edited before this page load."""
        editable.patch("test_case", project, "SC1_001",
                       {"comment": "checked by hand"})
        body = self._render(client, project)
        assert "data-ie-edited" in body

    def test_an_untouched_case_is_not_marked(self, client, project,
                                             editing_on):
        assert "data-ie-edited" not in self._render(client, project)

    def test_the_step_editor_marks_the_row_not_only_the_fields(self):
        """The page's own script has its own copy of this, and the first
        version updated the field spans alone — so a step edit did not reveal
        the pill. Measured in the browser."""
        import pathlib
        js = pathlib.Path("static/js/tc-editor.js").read_text(encoding="utf-8")
        body = js.split("function markEdited(caseId, item) {", 1)[1].split(
            "\n    }", 1)[0]
        assert "document.getElementById(caseId)" in body
        assert "data-ie-edited" in body

    def test_editors_enabled_is_callable_inside_a_block(self, client,
                                                        project,
                                                        editing_on):
        """A regression guard with a real cause.

        ``editors_enabled`` is a Jinja global callable because macros cannot
        see context variables. E4.2 also injected a *bool* of the same name
        through the context processor, which shadows the global inside a
        block — so the same call that worked in a macro raised "'bool' object
        is not callable" on the first page to use it in a block.
        """
        from engine import permissions
        assert "editors_enabled" not in permissions.template_context()
        # And the page that calls it in a block renders.
        assert self._render(client, project)
