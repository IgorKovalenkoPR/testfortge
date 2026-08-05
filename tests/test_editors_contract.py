"""One contract, checked against every editable entity — E4.10.

E4.3–E4.6 each have their own test module for what is specific to them. This
one asserts the things that must be true of *all* of them, by iterating the
registry rather than by naming entities — so an entity added later is covered
the moment it is registered, and cannot be added with a hole in it.

That is the whole argument for the substrate. Four bespoke editors would be
four allowlists, four validators and four audit shapes, and the one that forgot
a piece would look exactly like the three that did not. These tests are what
turns "would look exactly like" into "fails in CI".

E4.9's bulk operations are here too, for the same reason: they run over the
same registry.
"""
from __future__ import annotations

import secrets

import pytest

from engine import db, editable

#: A token per run, not just per test.
#:
#: ``conftest`` cannot always delete the scratch database on Windows — the file
#: may still be held open — so a stable project name silently reuses the
#: previous run's rows. E4.1 preserves ``row_version`` across a pack save, so a
#: version assertion then depends on how many times the suite has been run.
#: Measured: three of these tests failed on the second invocation and passed on
#: the first.
_RUN = secrets.token_hex(4)


#: Every registered entity, and a field of each that is safe to change.
ENTITIES = sorted(editable.entities())


def _seed(project_id: str, entity_name: str) -> str:
    """One row of ``entity_name``, returning its public id."""
    if entity_name == "test_case":
        db.save_test_cases(project_id, [{
            "id": "SC1_001", "section": "Auth", "section_num": 1,
            "summary": "Verify that a user signs in", "preconditions": "",
            "test_steps": "1. Open", "test_data": "",
            "expected_result": "The dashboard opens", "issues": "",
            "comment": "", "user_story_id": "", "category": "Positive",
            "priority": "High", "status": "Unchecked",
            "testing_type": "Functional"}])
        return "SC1_001"
    if entity_name == "checklist_item":
        db.save_checklist(project_id, [{
            "id": "HDR_001", "section": "Header", "item_num": "1.1",
            "depth": 2, "objective": "Verify that the logo links home",
            "comments": "", "user_story_id": "", "category": "Positive",
            "priority": "High", "status": "Unchecked",
            "testing_type": "Functional"}])
        return "HDR_001"
    if entity_name == "bug":
        db.save_bug(project_id, {
            "id": "BUG-001", "title": "Sign-in fails", "severity": "Major",
            "priority": "High", "status": "Open", "environment": "Chrome",
            "preconditions": "", "steps_to_reproduce": "1. Open",
            "actual_result": "500", "expected_result": "opens",
            "comment": "", "assignee": "", "bug_area": "", "browser": "",
            "os": "", "version": ""}, source="manual")
        return "BUG-001"
    raise AssertionError(
        f"{entity_name} has no seed here. A newly registered entity needs one "
        f"— that is the point of this module.")


def _safe_field(entity_name: str) -> tuple[str, str]:
    """A field of this entity and a value that is valid for it.

    Chosen from the entity's own declaration rather than hardcoded, so it
    stays valid when a field's rules change: the first free-text field that is
    not guarded, because a guarded field's answer depends on state.
    """
    config = editable.entity(entity_name)
    for name, spec in config.fields.items():
        if name in config.field_guards:
            continue
        if spec.kind == "text":
            return name, "A value a person typed"
        if spec.kind == "choice" and spec.choices:
            return name, spec.choices[-1]
    raise AssertionError(f"{entity_name} has no plainly settable field")


@pytest.fixture
def editing_on(monkeypatch):
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("EDITORS_ENABLED", "1")
    return True


@pytest.fixture
def project(app, request):
    return db.upsert_project(name=f"E4.10 {request.node.name} {_RUN}"[:180])


# ── The registry itself ───────────────────────────────────────────

class TestTheRegistry:

    def test_the_expected_entities_are_registered(self):
        """A guard against an entity silently disappearing from the substrate
        — every editor's UI would then render read-only with no error."""
        assert set(ENTITIES) == {"bug", "checklist_item", "test_case"}

    @pytest.mark.parametrize("entity_name", ENTITIES)
    def test_every_entity_declares_a_model_and_an_id_column(self, entity_name):
        config = editable.entity(entity_name)
        assert config.model_name and config.id_column
        assert getattr(db, config.model_name, None) is not None
        assert config.id_column in {
            column.name for column in
            getattr(db, config.model_name).__table__.columns}

    @pytest.mark.parametrize("entity_name", ENTITIES)
    def test_every_entity_has_fields_and_they_exist_as_columns(self,
                                                              entity_name):
        """An allowlist naming a column that does not exist is an allowlist
        that 500s on the first edit of that field."""
        config = editable.entity(entity_name)
        columns = {column.name for column in
                   getattr(db, config.model_name).__table__.columns}
        assert config.fields
        missing = sorted(set(config.fields) - columns)
        assert not missing, f"{entity_name}: {missing}"

    @pytest.mark.parametrize("entity_name", ENTITIES)
    def test_every_entity_carries_the_editing_metadata(self, entity_name):
        """row_version / ai_generated / edited_by / edited_at — the contract
        the concurrency check, the provenance and the audit all rest on."""
        columns = {column.name for column in
                   getattr(db, editable.entity(entity_name).model_name
                           ).__table__.columns}
        for needed in ("row_version", "ai_generated", "edited_by",
                       "edited_at"):
            assert needed in columns, f"{entity_name} lacks {needed}"

    @pytest.mark.parametrize("entity_name", ENTITIES)
    def test_a_creatable_entity_declares_a_prefix(self, entity_name):
        """Without one, minted ids would collide with the generators'."""
        config = editable.entity(entity_name)
        if config.creatable:
            assert config.id_prefix, entity_name

    def test_an_unknown_entity_is_named_in_the_error(self):
        with pytest.raises(editable.UnknownEntity) as exc:
            editable.entity("estimation")
        # Estimation is edited through engine/estimation_edit.py — its content
        # is a JSON payload, not columns. The message should list what *is*
        # registered so the caller can see that immediately.
        for name in ENTITIES:
            assert name in str(exc.value)


# ── The per-entity contract ───────────────────────────────────────

@pytest.mark.parametrize("entity_name", ENTITIES)
class TestEveryEntity:

    def test_a_field_outside_the_allowlist_is_refused_by_name(
            self, entity_name, project, editing_on):
        entity_id = _seed(project, entity_name)
        with pytest.raises(editable.FieldNotEditable) as exc:
            editable.patch(entity_name, project, entity_id,
                           {"project_id": "somebody-elses-project"})
        assert "project_id" in exc.value.names

    def test_an_edit_persists_and_flips_provenance(self, entity_name, project,
                                                   editing_on):
        entity_id = _seed(project, entity_name)
        field, value = _safe_field(entity_name)
        item = editable.patch(entity_name, project, entity_id, {field: value})
        assert item[field] == value
        assert item["ai_generated"] is False
        assert item["row_version"] == 2

    def test_a_stale_version_is_refused_and_nothing_changes(
            self, entity_name, project, editing_on):
        entity_id = _seed(project, entity_name)
        field, value = _safe_field(entity_name)
        editable.patch(entity_name, project, entity_id, {field: value})
        with pytest.raises(db.WriteConflict):
            editable.patch(entity_name, project, entity_id,
                           {field: "something else"}, expected_version=1)
        assert editable.get(entity_name, project, entity_id)[field] == value

    def test_a_no_op_is_not_a_write(self, entity_name, project, editing_on):
        entity_id = _seed(project, entity_name)
        before = editable.get(entity_name, project, entity_id)
        after = editable.patch(entity_name, project, entity_id,
                               {k: before[k] for k, _ in
                                [_safe_field(entity_name)]})
        assert after["row_version"] == before["row_version"]
        assert after["ai_generated"] == before["ai_generated"]

    def test_another_projects_row_is_not_found(self, entity_name, project,
                                               editing_on, app):
        entity_id = _seed(project, entity_name)
        other = db.upsert_project(name=f"E4.10 neighbour {entity_name}")
        field, value = _safe_field(entity_name)
        with pytest.raises(editable.EntityNotFound):
            editable.patch(entity_name, other, entity_id, {field: value})
        assert editable.get(entity_name, project, entity_id) is not None

    def test_an_edit_is_audited_with_the_before_and_after(
            self, entity_name, project, editing_on):
        entity_id = _seed(project, entity_name)
        field, value = _safe_field(entity_name)
        editable.patch(entity_name, project, entity_id, {field: value},
                       actor="somebody")
        rows = [row for row in db.list_audit(project_id=project, limit=10)
                if row["action"] == "update"]
        assert rows, f"{entity_name}: no audit row"
        assert field in (rows[0]["diff"] or {})
        assert rows[0]["diff"][field][1] == value

    def test_an_over_long_value_is_refused_not_truncated(
            self, entity_name, project, editing_on):
        entity_id = _seed(project, entity_name)
        config = editable.entity(entity_name)
        field = next((name for name, spec in config.fields.items()
                      if spec.kind == "text" and spec.max_length
                      and name not in config.field_guards), None)
        if field is None:
            pytest.skip(f"{entity_name} has no bounded text field")
        limit = config.fields[field].max_length
        with pytest.raises(editable.ValidationFailed) as exc:
            editable.patch(entity_name, project, entity_id,
                           {field: "x" * (limit + 1)})
        assert exc.value.field_name == field

    def test_a_required_field_cannot_be_emptied(self, entity_name, project,
                                                editing_on):
        entity_id = _seed(project, entity_name)
        config = editable.entity(entity_name)
        field = next((name for name, spec in config.fields.items()
                      if spec.required), None)
        if field is None:
            pytest.skip(f"{entity_name} has no required field")
        with pytest.raises(editable.ValidationFailed):
            editable.patch(entity_name, project, entity_id, {field: "   "})

    def test_the_row_survives_a_regeneration_once_edited(
            self, entity_name, project, editing_on):
        """E4.7's guarantee, checked per entity rather than once — the flag is
        set by the substrate, and the protection is only worth anything if
        both halves agree for every entity that has the flag."""
        if entity_name == "bug":
            pytest.skip("bugs are appended, never regenerated as a pack")
        entity_id = _seed(project, entity_name)
        field, value = _safe_field(entity_name)
        editable.patch(entity_name, project, entity_id, {field: value})
        saver = (db.save_test_cases if entity_name == "test_case"
                 else db.save_checklist)
        loader = (db.load_test_cases if entity_name == "test_case"
                  else db.load_checklist)
        pack = loader(project)
        regenerated = [dict(row, **{field: "The generator's words"})
                       for row in pack]
        saver(project, regenerated, protect_edits=True)
        assert loader(project)[0][field] == value


# ── E4.9's bulk operations, over the same registry ────────────────

@pytest.mark.parametrize("entity_name", ENTITIES)
class TestBulkForEveryEntity:

    def test_a_bulk_change_applies_and_audits_once(self, entity_name, project,
                                                   editing_on):
        entity_id = _seed(project, entity_name)
        field, value = _safe_field(entity_name)
        result = editable.patch_many(entity_name, project, [entity_id],
                                     {field: value}, actor="somebody")
        assert result.changed == [entity_id]
        rows = [row for row in db.list_audit(project_id=project, limit=10)
                if row["action"] == "bulk_update"]
        assert len(rows) == 1, "one row for the operation, not one per item"

    def test_a_bad_value_is_refused_before_anything_is_written(
            self, entity_name, project, editing_on):
        """Validated once, up front — a bulk edit is refused whole rather than
        applied to the rows that happened to come first."""
        entity_id = _seed(project, entity_name)
        before = editable.get(entity_name, project, entity_id)
        with pytest.raises(editable.FieldNotEditable):
            editable.patch_many(entity_name, project, [entity_id],
                                {"nonexistent_field": "x"})
        assert editable.get(entity_name, project, entity_id) == before

    def test_a_missing_id_is_reported_not_fatal(self, entity_name, project,
                                                editing_on):
        entity_id = _seed(project, entity_name)
        field, value = _safe_field(entity_name)
        result = editable.patch_many(entity_name, project,
                                     [entity_id, "NOPE-999"], {field: value})
        assert result.changed == [entity_id]
        assert result.missing == ["NOPE-999"]

    def test_an_unchanged_row_is_counted_separately(self, entity_name,
                                                    project, editing_on):
        entity_id = _seed(project, entity_name)
        current = editable.get(entity_name, project, entity_id)
        field, _ = _safe_field(entity_name)
        result = editable.patch_many(entity_name, project, [entity_id],
                                     {field: current[field]})
        assert result.unchanged == [entity_id]
        assert result.changed == []

    def test_an_empty_selection_is_refused(self, entity_name, project,
                                           editing_on):
        with pytest.raises(editable.ValidationFailed):
            editable.patch_many(entity_name, project, [], {"x": "y"})

    def test_another_projects_rows_are_untouched(self, entity_name, project,
                                                 editing_on, app):
        entity_id = _seed(project, entity_name)
        other = db.upsert_project(name=f"E4.10 bulk neighbour {entity_name}")
        field, value = _safe_field(entity_name)
        result = editable.patch_many(entity_name, other, [entity_id],
                                     {field: value})
        assert result.changed == [] and result.missing == [entity_id]

    def test_bulk_delete_honours_the_deletable_flag(self, entity_name,
                                                     project, editing_on):
        entity_id = _seed(project, entity_name)
        config = editable.entity(entity_name)
        if not config.deletable:
            with pytest.raises(editable.NotDeletable):
                editable.remove_many(entity_name, project, [entity_id])
            return
        result = editable.remove_many(entity_name, project, [entity_id])
        assert result.changed == [entity_id]
        assert result.verb == "deleted", "a delete must not report 'updated'"
        assert editable.get(entity_name, project, entity_id) is None


# ── The HTTP surface, over the same registry ──────────────────────

class TestEndpointsForEveryEntity:

    @staticmethod
    def _headers(client, project):
        with client.session_transaction() as sess:
            sess["project_id"] = project
        return {"X-CSRFToken": client.get("/api/csrf-token"
                                          ).get_json()["token"]}

    @pytest.mark.parametrize("entity_name", ENTITIES)
    def test_get_and_patch_round_trip(self, client, project, editing_on,
                                      entity_name):
        entity_id = _seed(project, entity_name)
        headers = self._headers(client, project)
        got = client.get(f"/api/edit/{entity_name}/{entity_id}",
                         headers=headers)
        assert got.status_code == 200
        assert got.get_json()["item"]["id"] == entity_id

        field, value = _safe_field(entity_name)
        patched = client.patch(f"/api/edit/{entity_name}/{entity_id}",
                               json={"changes": {field: value}},
                               headers=headers)
        assert patched.status_code == 200
        assert patched.get_json()["item"][field] == value

    @pytest.mark.parametrize("entity_name", ENTITIES)
    def test_a_stale_version_is_a_409(self, client, project, editing_on,
                                      entity_name):
        entity_id = _seed(project, entity_name)
        headers = self._headers(client, project)
        field, value = _safe_field(entity_name)
        client.patch(f"/api/edit/{entity_name}/{entity_id}",
                     json={"changes": {field: value}}, headers=headers)
        resp = client.patch(f"/api/edit/{entity_name}/{entity_id}",
                            json={"changes": {field: "again"},
                                  "row_version": 1}, headers=headers)
        assert resp.status_code == 409

    @pytest.mark.parametrize("entity_name", ENTITIES)
    def test_the_bulk_endpoints_need_a_csrf_token(self, client, project,
                                                  editing_on, entity_name):
        """Every non-form endpoint in this app has to be checked: without the
        token it passes a suite that disables CSRF and 400s in production."""
        entity_id = _seed(project, entity_name)
        with client.session_transaction() as sess:
            sess["project_id"] = project
        client.application.config["WTF_CSRF_ENABLED"] = True
        try:
            for path in (f"/api/edit/{entity_name}/bulk",
                         f"/api/edit/{entity_name}/bulk-delete"):
                assert client.post(path, json={"ids": [entity_id]}
                                   ).status_code == 400, path
        finally:
            client.application.config["WTF_CSRF_ENABLED"] = False

    @pytest.mark.parametrize("entity_name", ENTITIES)
    def test_nothing_is_reachable_with_editing_off(self, client, project,
                                                    monkeypatch, entity_name):
        entity_id = _seed(project, entity_name)
        monkeypatch.setenv("EDITORS_ENABLED", "0")
        headers = self._headers(client, project)
        for method, path, body in (
                ("get", f"/api/edit/{entity_name}/{entity_id}", None),
                ("patch", f"/api/edit/{entity_name}/{entity_id}",
                 {"changes": {"x": "y"}}),
                ("post", f"/api/edit/{entity_name}/bulk",
                 {"ids": [entity_id], "changes": {"x": "y"}}),
                ("post", f"/api/edit/{entity_name}/bulk-delete",
                 {"ids": [entity_id]}),
        ):
            resp = getattr(client, method)(path, json=body, headers=headers)
            assert resp.status_code == 404, path
            assert resp.get_json()["error"] == "editors_disabled"

    def test_the_shared_front_end_is_loaded_by_every_editor_page(self, client,
                                                                 project,
                                                                 editing_on):
        """One CSRF implementation, one status line — four copies would be
        four chances to omit the header."""
        _seed(project, "test_case")
        _seed(project, "checklist_item")
        _seed(project, "bug")
        with client.session_transaction() as sess:
            sess["project_id"] = project
        for page in ("/test-cases", "/checklist", "/bug-reports"):
            body = client.get(page).get_data(as_text=True)
            assert "js/editor-shared.js" in body, page
            assert "js/inline-edit.js" in body, page
