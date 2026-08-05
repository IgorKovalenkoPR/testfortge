"""The shared inline-edit substrate — E4.1.

Requirements 4 to 7 are the same request in four places: let the user fix
what the generator got wrong. These tests pin the substrate all four
editors sit on — the field allowlist, validation, project scoping,
optimistic concurrency, provenance and the audit trail — so that E4.3 to
E4.6 are configuration rather than four chances to leave one of those out.
"""

import secrets

import pytest

from engine import db as _db
from engine import editable


@pytest.fixture(autouse=True)
def _db_ready():
    _db.init_db()


@pytest.fixture(autouse=True)
def _editors_on(monkeypatch):
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("EDITORS_ENABLED", "1")


def _project() -> str:
    return _db.upsert_project(name=f"Edit-{secrets.token_hex(5)}")


def _tc(external_id="TC-001") -> dict:
    return {"id": external_id, "section": "Auth", "section_num": 1,
            "summary": "Sign in with valid credentials",
            "preconditions": "", "test_steps": "1. Open the page",
            "test_data": "", "expected_result": "The dashboard opens",
            "issues": "", "comment": "", "user_story_id": "US-1",
            "category": "Functional", "priority": "High", "status": "",
            "testing_type": "Functional"}


def _cl(external_id="CL-001") -> dict:
    return {"id": external_id, "section": "Auth", "section_num": 1,
            "objective": "The sign-in page loads", "priority": "High",
            "category": "Functional", "comment": "", "expected_result": "",
            "user_story_id": "US-1", "testing_type": "Functional"}


def _project_with_tc(external_id="TC-001") -> str:
    pid = _project()
    _db.save_test_cases(pid, [_tc(external_id)])
    return pid


def _project_with_bug(external_id="BUG-001") -> str:
    pid = _project()
    _db.save_bug(pid, {
        "id": external_id, "title": "Hero image fails to load",
        "severity": "Major", "priority": "High", "status": "Open",
        "steps_to_reproduce": "1. Open the page.",
        "actual_result": "No image.", "expected_result": "An image.",
    })
    return pid


# ── The registry ──────────────────────────────────────────────────

class TestRegistry:
    def test_the_four_requirements_have_an_entity_each(self):
        # Estimation is absent on purpose: its editable content is a JSON
        # payload, not columns, so it needs a field kind this substrate does
        # not have yet. E4.6 adds it — and adds it *here*, which is the
        # point of a registry.
        assert set(editable.entities()) == {"test_case", "checklist_item",
                                            "bug"}

    def test_an_unregistered_entity_raises_rather_than_guessing(self):
        with pytest.raises(editable.UnknownEntity):
            editable.entity("test_cases")        # plural typo

    def test_every_declared_field_exists_on_its_model(self):
        """The declaration cannot drift from the schema unnoticed.

        A field allowlisted but absent from the model would raise deep
        inside a patch, on whichever row a user happened to be editing.
        """
        for name, config in editable.entities().items():
            model = getattr(_db, config.model_name)
            columns = set(model.__table__.columns.keys())
            missing = sorted(set(config.fields) - columns)
            assert not missing, f"{name} declares absent column(s): {missing}"
            assert config.id_column in columns

    def test_no_entity_lets_a_client_edit_its_own_bookkeeping(self):
        # Editing row_version, ai_generated or project_id through the same
        # door as a summary would defeat the concurrency guard, the
        # regeneration guard and the tenancy scoping respectively.
        forbidden = {"row_version", "ai_generated", "edited_by", "edited_at",
                     "project_id", "id", "external_id", "created_at"}
        for name, config in editable.entities().items():
            overlap = sorted(set(config.fields) & forbidden)
            assert not overlap, f"{name} exposes {overlap}"

    def test_choices_are_declared_only_where_a_vocabulary_exists(self):
        from engine.bug_report import BUG_SEVERITIES
        # Bugs have one and it is enforced…
        assert editable.entity("bug").fields["severity"].choices == \
            tuple(BUG_SEVERITIES)
        # …test cases do not: the generators write free text there, and a
        # closed list would reject the product's own output.
        assert editable.entity("test_case").fields["priority"].choices == ()


# ── The allowlist ─────────────────────────────────────────────────

class TestFieldAllowlist:
    def test_a_declared_field_is_accepted(self):
        assert editable.validate("test_case", {"summary": "Rewritten"}) == \
            {"summary": "Rewritten"}

    def test_an_undeclared_field_is_refused_and_named(self):
        with pytest.raises(editable.FieldNotEditable) as exc:
            editable.validate("test_case", {"summary": "ok",
                                            "project_id": "elsewhere"})
        assert exc.value.names == ["project_id"]

    def test_the_whole_patch_is_refused_not_the_valid_half(self):
        # A partly-applied edit is the hardest kind to notice.
        pid = _project_with_tc()
        with pytest.raises(editable.FieldNotEditable):
            editable.patch("test_case", pid, "TC-001",
                           {"summary": "Changed", "row_version": 99})
        rows = _db.load_test_cases(pid)
        assert rows[0]["summary"] == "Sign in with valid credentials"

    def test_an_attempt_to_move_a_row_between_projects_is_refused(self):
        pid = _project_with_tc()
        with pytest.raises(editable.FieldNotEditable):
            editable.patch("test_case", pid, "TC-001",
                           {"project_id": "deadbeef" * 4})


# ── Validation ────────────────────────────────────────────────────

class TestValidation:
    def test_a_required_field_cannot_be_emptied(self):
        with pytest.raises(editable.ValidationFailed) as exc:
            editable.validate("test_case", {"summary": "   "})
        assert exc.value.field_name == "summary"
        assert "empty" in str(exc.value)

    def test_an_optional_field_can_be_cleared(self):
        assert editable.validate("test_case", {"comment": ""}) == {"comment": ""}
        assert editable.validate("test_case", {"comment": None}) == \
            {"comment": ""}

    def test_whitespace_is_stripped(self):
        assert editable.validate(
            "test_case", {"summary": "  Trimmed  "}) == {"summary": "Trimmed"}

    def test_over_long_text_is_refused_rather_than_truncated(self):
        # Silently shortening someone's test steps is a data-loss bug that
        # presents as a formatting quirk.
        limit = editable.entity("test_case").fields["summary"].max_length
        with pytest.raises(editable.ValidationFailed) as exc:
            editable.validate("test_case", {"summary": "x" * (limit + 1)})
        assert str(limit) in str(exc.value)

    def test_a_value_at_the_limit_is_accepted(self):
        limit = editable.entity("test_case").fields["summary"].max_length
        assert editable.validate("test_case", {"summary": "x" * limit})

    def test_a_closed_vocabulary_is_enforced(self):
        with pytest.raises(editable.ValidationFailed) as exc:
            editable.validate("bug", {"severity": "Catastrophic"})
        assert "Critical" in str(exc.value)      # the message lists them

    def test_a_valid_choice_passes(self):
        assert editable.validate("bug", {"severity": "Critical"}) == \
            {"severity": "Critical"}

    def test_free_text_priority_on_a_test_case_is_not_vocabulary_checked(self):
        # Because the generator writes its own values there.
        assert editable.validate("test_case", {"priority": "Highest"})


# ── Project scoping ───────────────────────────────────────────────

class TestScoping:
    def test_a_row_is_read_within_its_project(self):
        pid = _project_with_tc()
        assert editable.get("test_case", pid, "TC-001")["summary"]

    def test_another_projects_row_is_not_found(self):
        mine = _project()
        theirs = _project_with_tc()
        assert editable.get("test_case", mine, "TC-001") is None
        with pytest.raises(editable.EntityNotFound):
            editable.patch("test_case", mine, "TC-001", {"summary": "Mine now"})
        # …and theirs is untouched.
        assert _db.load_test_cases(theirs)[0]["summary"] == \
            "Sign in with valid credentials"

    def test_a_missing_row_and_a_foreign_row_answer_the_same(self):
        # Telling them apart would confirm an id exists somewhere the caller
        # cannot see.
        mine = _project()
        _project_with_tc("TC-777")
        assert editable.get("test_case", mine, "TC-777") is None
        assert editable.get("test_case", mine, "TC-NOPE") is None

    def test_no_project_is_refused(self):
        with pytest.raises(editable.EntityNotFound):
            editable.patch("test_case", "", "TC-001", {"summary": "x"})


# ── The edit itself ───────────────────────────────────────────────

class TestPatch:
    def test_a_field_changes_and_comes_back(self):
        pid = _project_with_tc()
        result = editable.patch("test_case", pid, "TC-001",
                                {"summary": "Sign in with a valid password"})
        assert result["summary"] == "Sign in with a valid password"
        assert _db.load_test_cases(pid)[0]["summary"] == \
            "Sign in with a valid password"

    def test_several_fields_change_together(self):
        pid = _project_with_tc()
        editable.patch("test_case", pid, "TC-001", {
            "summary": "Rewritten", "expected_result": "Also rewritten",
            "priority": "Low"})
        row = _db.load_test_cases(pid)[0]
        assert (row["summary"], row["expected_result"], row["priority"]) == \
            ("Rewritten", "Also rewritten", "Low")

    def test_untouched_fields_are_left_alone(self):
        pid = _project_with_tc()
        before = _db.load_test_cases(pid)[0]
        editable.patch("test_case", pid, "TC-001", {"summary": "Only this"})
        after = _db.load_test_cases(pid)[0]
        for key in ("test_steps", "expected_result", "category", "priority"):
            assert after[key] == before[key], key

    def test_a_checklist_item_edits_the_same_way(self):
        pid = _project()
        _db.save_checklist(pid, [_cl()])
        editable.patch("checklist_item", pid, "CL-001",
                       {"objective": "The page loads within two seconds"})
        assert _db.load_checklist(pid)[0]["objective"] == \
            "The page loads within two seconds"

    def test_a_bug_edits_the_same_way(self):
        pid = _project_with_bug()
        editable.patch("bug", pid, "BUG-001",
                       {"severity": "Critical", "status": "In Progress"})
        row = _db.list_bugs(pid)[0]
        assert (row["severity"], row["status"]) == ("Critical", "In Progress")


# ── Provenance ────────────────────────────────────────────────────

class TestProvenance:
    def test_generated_rows_start_marked_as_generated(self):
        pid = _project_with_tc()
        assert editable.get("test_case", pid, "TC-001")["ai_generated"] is True

    def test_an_edit_flips_the_flag(self):
        """The guard E4.7's merge policy reads.

        Without it, requirement 5 ("let me fix the generated test case")
        becomes a way to lose the fix on the next Generate click.
        """
        pid = _project_with_tc()
        result = editable.patch("test_case", pid, "TC-001",
                                {"summary": "Human wording"}, actor="u-1")
        assert result["ai_generated"] is False
        assert result["edited_by"] == "u-1"
        assert result["edited_at"]

    def test_a_pack_rewrite_does_not_revert_the_provenance(self):
        """The interaction that makes E4.1 useful rather than decorative.

        ``save_test_cases`` deletes and re-inserts, and the dicts it is
        given come from ``load_test_cases``, which strips to the dataclass
        fields — so a pack write reset ``ai_generated`` to True and
        ``row_version`` to 1 for a row a human had just edited. Both inline
        editors and every upload go through a pack write, so the provenance
        survived until roughly the next click.
        """
        pid = _project()
        _db.save_test_cases(pid, [_tc("TC-001"), _tc("TC-002")])
        editable.patch("test_case", pid, "TC-001", {"summary": "Human"},
                       actor="u-1")

        _db.save_test_cases(pid, _db.load_test_cases(pid))

        edited = editable.get("test_case", pid, "TC-001")
        assert edited["ai_generated"] is False
        assert edited["row_version"] == 2
        assert edited["edited_by"] == "u-1"
        assert edited["summary"] == "Human"

    def test_a_pack_rewrite_leaves_untouched_rows_marked_generated(self):
        # The other half: preserving metadata must not invent it.
        pid = _project()
        _db.save_test_cases(pid, [_tc("TC-001"), _tc("TC-002")])
        editable.patch("test_case", pid, "TC-001", {"summary": "Human"})
        _db.save_test_cases(pid, _db.load_test_cases(pid))
        untouched = editable.get("test_case", pid, "TC-002")
        assert untouched["ai_generated"] is True
        assert untouched["row_version"] == 1

    def test_the_checklist_preserves_it_the_same_way(self):
        pid = _project()
        _db.save_checklist(pid, [_cl("CL-001")])
        editable.patch("checklist_item", pid, "CL-001",
                       {"objective": "Human wording"})
        _db.save_checklist(pid, _db.load_checklist(pid))
        assert editable.get("checklist_item", pid,
                            "CL-001")["ai_generated"] is False

    def test_an_edit_through_an_inline_editor_route_survives_its_own_save(
            self, client):
        """End to end, through the route that does a read-modify-write.

        /test-cases/<id>/walkthrough-meta rewrites the whole pack, so this
        is the path where the reversion actually bit.
        """
        pid = _project()
        _db.save_test_cases(pid, [_tc("TC-001")])
        editable.patch("test_case", pid, "TC-001", {"summary": "Human"},
                       actor="u-2")
        with client.session_transaction() as sess:
            sess["project_id"] = pid

        resp = client.post("/test-cases/TC-001/walkthrough-meta",
                           data={"url_pattern": "/checkout*",
                                 "trigger": "walkthrough_url_match"},
                           headers={"Accept": "application/json"})
        assert resp.status_code == 200
        after = editable.get("test_case", pid, "TC-001")
        assert after["ai_generated"] is False, (
            "the inline editor's pack write reverted the provenance")
        assert after["summary"] == "Human"

    def test_an_anonymous_edit_still_records_that_it_was_edited(self):
        # While AUTH_ENABLED is off there is no user id to attribute it to,
        # and the flag matters more than the name.
        pid = _project_with_tc()
        result = editable.patch("test_case", pid, "TC-001",
                                {"summary": "Changed"}, actor=None)
        assert result["ai_generated"] is False
        assert result["edited_by"] is None


# ── Concurrency ───────────────────────────────────────────────────

class TestOptimisticConcurrency:
    def test_the_version_starts_at_one_and_increments(self):
        pid = _project_with_tc()
        assert editable.get("test_case", pid, "TC-001")["row_version"] == 1
        editable.patch("test_case", pid, "TC-001", {"summary": "Second"})
        assert editable.get("test_case", pid, "TC-001")["row_version"] == 2

    def test_a_matching_version_is_accepted(self):
        pid = _project_with_tc()
        version = editable.get("test_case", pid, "TC-001")["row_version"]
        editable.patch("test_case", pid, "TC-001", {"summary": "Fine"},
                       expected_version=version)

    def test_a_stale_version_is_refused(self):
        pid = _project_with_tc()
        stale = editable.get("test_case", pid, "TC-001")["row_version"]
        editable.patch("test_case", pid, "TC-001", {"summary": "Colleague"})
        with pytest.raises(_db.WriteConflict) as exc:
            editable.patch("test_case", pid, "TC-001", {"summary": "Mine"},
                           expected_version=stale)
        assert exc.value.expected == stale
        assert exc.value.actual > stale

    def test_the_colleagues_edit_survives_the_refusal(self):
        pid = _project_with_tc()
        stale = editable.get("test_case", pid, "TC-001")["row_version"]
        editable.patch("test_case", pid, "TC-001", {"summary": "Colleague"})
        with pytest.raises(_db.WriteConflict):
            editable.patch("test_case", pid, "TC-001", {"summary": "Mine"},
                           expected_version=stale)
        assert _db.load_test_cases(pid)[0]["summary"] == "Colleague"

    def test_it_is_the_same_exception_the_pack_guard_raises(self):
        # One exception type across E3.5 and E4.1 means one 409 contract,
        # and a client that handles a conflict handles both.
        assert issubclass(_db.WriteConflict, RuntimeError)

    def test_two_people_editing_different_rows_do_not_collide(self):
        """Why this is per row and not per pack.

        E3.5's pack version is the right unit for a wipe-and-replace save
        and the wrong one here — it would make two colleagues fixing two
        different test cases conflict for no reason.
        """
        pid = _project()
        _db.save_test_cases(pid, [_tc("TC-001"), _tc("TC-002")])
        v1 = editable.get("test_case", pid, "TC-001")["row_version"]
        v2 = editable.get("test_case", pid, "TC-002")["row_version"]
        editable.patch("test_case", pid, "TC-001", {"summary": "A"},
                       expected_version=v1)
        editable.patch("test_case", pid, "TC-002", {"summary": "B"},
                       expected_version=v2)
        summaries = {r["id"]: r["summary"] for r in _db.load_test_cases(pid)}
        assert summaries == {"TC-001": "A", "TC-002": "B"}

    def test_omitting_the_version_still_writes(self):
        # So a first-cut client works before it tracks versions.
        pid = _project_with_tc()
        editable.patch("test_case", pid, "TC-001", {"summary": "Unguarded"})
        assert _db.load_test_cases(pid)[0]["summary"] == "Unguarded"


class TestNoOpPatches:
    def test_writing_the_same_value_does_not_bump_the_version(self):
        """A no-op is not a write.

        An editor that PATCHes on blur would otherwise manufacture a
        conflict for whoever is editing the row next.
        """
        pid = _project_with_tc()
        before = editable.get("test_case", pid, "TC-001")
        result = editable.patch("test_case", pid, "TC-001",
                                {"summary": before["summary"]})
        assert result["row_version"] == before["row_version"]

    def test_a_no_op_does_not_flip_the_provenance_flag(self):
        pid = _project_with_tc()
        summary = editable.get("test_case", pid, "TC-001")["summary"]
        result = editable.patch("test_case", pid, "TC-001",
                                {"summary": summary})
        assert result["ai_generated"] is True

    def test_a_no_op_writes_no_audit_row(self):
        # Otherwise the real edits are buried in noise.
        pid = _project_with_tc()
        summary = editable.get("test_case", pid, "TC-001")["summary"]
        editable.patch("test_case", pid, "TC-001", {"summary": summary})
        assert _db.list_audit(project_id=pid, entity="test_case") == []


# ── Audit ─────────────────────────────────────────────────────────

class TestAudit:
    def test_an_edit_is_audited_with_before_and_after(self):
        pid = _project_with_tc()
        editable.patch("test_case", pid, "TC-001",
                       {"summary": "The new wording"}, actor="u-9")
        rows = _db.list_audit(project_id=pid, entity="test_case")
        assert len(rows) == 1
        entry = rows[0]
        assert entry["action"] == "update"
        assert entry["entity_id"] == "TC-001"
        assert entry["user_id"] == "u-9"
        assert entry["diff"]["summary"] == ["Sign in with valid credentials",
                                           "The new wording"]

    def test_only_the_fields_that_changed_are_audited(self):
        pid = _project_with_tc()
        unchanged = editable.get("test_case", pid, "TC-001")["priority"]
        editable.patch("test_case", pid, "TC-001",
                       {"summary": "Changed", "priority": unchanged})
        diff = _db.list_audit(project_id=pid, entity="test_case")[0]["diff"]
        assert set(diff) == {"summary"}

    def test_a_refused_edit_is_not_audited(self):
        pid = _project_with_tc()
        with pytest.raises(editable.ValidationFailed):
            editable.patch("test_case", pid, "TC-001", {"summary": ""})
        assert _db.list_audit(project_id=pid, entity="test_case") == []


# ── The HTTP surface ──────────────────────────────────────────────

class TestEditEndpoint:
    def _active(self, client, pid):
        with client.session_transaction() as sess:
            sess["project_id"] = pid

    def test_get_returns_the_row_its_version_and_the_allowlist(self, client):
        pid = _project_with_tc()
        self._active(client, pid)
        body = client.get("/api/edit/test_case/TC-001").get_json()
        assert body["item"]["row_version"] == 1
        assert "summary" in body["editable_fields"]
        assert "project_id" not in body["editable_fields"]

    def test_patch_applies_and_returns_the_new_version(self, client):
        pid = _project_with_tc()
        self._active(client, pid)
        resp = client.patch("/api/edit/test_case/TC-001",
                            json={"changes": {"summary": "Via HTTP"},
                                  "row_version": 1})
        assert resp.status_code == 200
        assert resp.get_json()["item"]["row_version"] == 2
        assert _db.load_test_cases(pid)[0]["summary"] == "Via HTTP"

    def test_a_stale_version_answers_409(self, client):
        pid = _project_with_tc()
        self._active(client, pid)
        editable.patch("test_case", pid, "TC-001", {"summary": "Colleague"})
        resp = client.patch("/api/edit/test_case/TC-001",
                            json={"changes": {"summary": "Mine"},
                                  "row_version": 1})
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["error"] == "conflict"
        assert "reload" in body["message"].lower()
        assert body["current_version"] == 2

    def test_an_undeclared_field_answers_400_and_names_it(self, client):
        pid = _project_with_tc()
        self._active(client, pid)
        resp = client.patch("/api/edit/test_case/TC-001",
                            json={"changes": {"project_id": "x"}})
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"] == "field_not_editable"
        assert body["fields"] == ["project_id"]

    def test_a_validation_failure_answers_400_with_the_field(self, client):
        pid = _project_with_bug()
        self._active(client, pid)
        resp = client.patch("/api/edit/bug/BUG-001",
                            json={"changes": {"severity": "Catastrophic"}})
        assert resp.status_code == 400
        assert resp.get_json()["field"] == "severity"

    def test_another_projects_row_answers_404(self, client):
        mine = _project()
        _project_with_tc("TC-555")
        self._active(client, mine)
        assert client.get("/api/edit/test_case/TC-555").status_code == 404
        resp = client.patch("/api/edit/test_case/TC-555",
                            json={"changes": {"summary": "Mine now"}})
        assert resp.status_code == 404

    def test_an_unknown_entity_answers_404(self, client):
        self._active(client, _project())
        assert client.get("/api/edit/wombat/W-1").status_code == 404

    def test_an_empty_patch_is_a_400_not_a_silent_success(self, client):
        pid = _project_with_tc()
        self._active(client, pid)
        for payload in ({}, {"changes": {}}, {"changes": "nope"}):
            resp = client.patch("/api/edit/test_case/TC-001", json=payload)
            assert resp.status_code == 400, payload

    def test_a_non_numeric_version_is_a_400(self, client):
        pid = _project_with_tc()
        self._active(client, pid)
        resp = client.patch("/api/edit/test_case/TC-001",
                            json={"changes": {"summary": "x"},
                                  "row_version": "soon"})
        assert resp.status_code == 400

    def test_the_endpoint_is_absent_until_the_flag_is_on(self, client,
                                                        monkeypatch):
        # And absent rather than forbidden: an editor that does not exist
        # yet should not look like one the caller lacks rights for.
        monkeypatch.delenv("EDITORS_ENABLED", raising=False)
        pid = _project_with_tc()
        self._active(client, pid)
        resp = client.patch("/api/edit/test_case/TC-001",
                            json={"changes": {"summary": "x"}})
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "editors_disabled"

    def test_editors_stay_off_without_the_db_backed_workspace(self, client,
                                                              monkeypatch):
        """ADR 0001's gate, at the endpoint.

        Editing a Flask session edits a private copy of shared team data, so
        the editors cannot come on before the workspace does — and
        engine.features enforces the dependency rather than trusting a
        deploy checklist.
        """
        monkeypatch.setenv("EDITORS_ENABLED", "1")
        monkeypatch.delenv("WORKSPACE_DB_FIRST", raising=False)
        pid = _project_with_tc()
        self._active(client, pid)
        resp = client.patch("/api/edit/test_case/TC-001",
                            json={"changes": {"summary": "x"}})
        assert resp.status_code == 404

    def test_the_page_sees_the_edit_immediately(self, client):
        """The per-request cache must not serve the pre-edit pack.

        engine.editable writes through the model rather than the repository,
        so the invalidation is the endpoint's job — and forgetting it would
        show a stale list right after a save.
        """
        pid = _project_with_tc()
        self._active(client, pid)
        client.patch("/api/edit/test_case/TC-001",
                     json={"changes": {"summary": "Freshly edited"}})
        assert b"Freshly edited" in client.get("/test-cases").data
