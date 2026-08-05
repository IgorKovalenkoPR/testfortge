"""The Bug Reports editor — requirement 7 (E4.5).

Body fields are config over the E4.1 substrate and the E4.2 component. What is
bug-specific: a status is not free text and not a fixed vocabulary either —
which values are legal depends on where the report is now and on who is
asking. See tests/test_bug_workflow.py for the rules themselves.

Attachments are **not** part of this: see the note at the end of the module.
"""
from __future__ import annotations

import pytest

from engine import bug_workflow as wf
from engine import db, editable


def _bug(external_id="BUG-001", **overrides):
    row = {
        "id": external_id,
        "title": "Sign-in fails with a valid password",
        "severity": "Major", "priority": "High", "status": "Open",
        "environment": "Chrome 120 / Windows",
        "preconditions": "An account exists",
        "steps_to_reproduce": "1. Open /login\n2. Submit valid details",
        "actual_result": "A 500 page is shown",
        "expected_result": "The dashboard opens",
        "comment": "", "assignee": "", "bug_area": "",
        "browser": "Chrome", "os": "Windows", "version": "1.2.0",
    }
    row.update(overrides)
    return row


@pytest.fixture
def project(app, request):
    """This test's own project — see the note in tests/test_tc_editor.py."""
    pid = db.upsert_project(name=f"E4.5 {request.node.name}"[:180])
    db.save_bug(pid, _bug(), source="manual")
    return pid


@pytest.fixture
def editing_on(monkeypatch):
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("EDITORS_ENABLED", "1")
    return True


def _stored(project_id, external_id="BUG-001"):
    for row in db.list_bugs(project_id):
        if (row.get("external_id") or row.get("id")) == external_id:
            return row
    raise AssertionError(f"{external_id} not stored")


class TestBodyFields:
    """The part that needed no new code — proving it needed none."""

    def test_the_body_is_patched_like_any_field(self, project, editing_on):
        item = editable.patch("bug", project, "BUG-001", {
            "actual_result": "A 500 page is shown and nothing is logged"})
        assert item["actual_result"].endswith("nothing is logged")
        assert item["ai_generated"] is False
        assert item["row_version"] == 2

    def test_a_closed_vocabulary_is_enforced(self, project, editing_on):
        """Severity has a real vocabulary, unlike a test case's priority."""
        with pytest.raises(editable.ValidationFailed):
            editable.patch("bug", project, "BUG-001",
                           {"severity": "Quite bad"})

    def test_a_field_outside_the_allowlist_is_refused(self, project,
                                                     editing_on):
        with pytest.raises(editable.FieldNotEditable):
            editable.patch("bug", project, "BUG-001",
                           {"attachment": "/etc/passwd"})


class TestStatusGuard:
    """The gate lives in the substrate, so every write path gets it."""

    def test_an_allowed_move_goes_through(self, project, editing_on):
        item = editable.patch("bug", project, "BUG-001",
                              {"status": "In Progress"},
                              has_role=lambda role: False)
        assert item["status"] == "In Progress"

    def test_closing_is_refused_for_a_plain_member(self, project, editing_on):
        with pytest.raises(wf.TransitionRefused) as exc:
            editable.patch("bug", project, "BUG-001", {"status": "Closed"},
                           has_role=lambda role: False)
        assert exc.value.reason == "needs_role"
        assert _stored(project)["status"] == "Open", \
            "a refused transition must not have applied"

    def test_closing_is_allowed_for_an_admin(self, project, editing_on):
        item = editable.patch("bug", project, "BUG-001", {"status": "Closed"},
                              has_role=lambda role: True)
        assert item["status"] == "Closed"

    def test_a_nonsense_move_is_refused_even_for_an_admin(self, project,
                                                          editing_on):
        editable.patch("bug", project, "BUG-001", {"status": "Closed"},
                       has_role=lambda role: True)
        with pytest.raises(wf.TransitionRefused) as exc:
            editable.patch("bug", project, "BUG-001",
                           {"status": "In Progress"},
                           has_role=lambda role: True)
        assert exc.value.reason == "not_allowed_from"

    def test_re_posting_the_same_status_is_not_a_transition(self, project,
                                                           editing_on):
        """A form that re-sends the value it rendered must not be refused —
        and a no-op is still not a write, so the version does not move."""
        item = editable.patch("bug", project, "BUG-001", {"status": "Open"},
                              has_role=lambda role: False)
        assert item["row_version"] == 1

    def test_the_whole_patch_is_refused_not_half_applied(self, project,
                                                         editing_on):
        """Guards run as a complete pass before anything is written, the same
        contract validation has."""
        with pytest.raises(wf.TransitionRefused):
            editable.patch("bug", project, "BUG-001",
                           {"title": "A new title", "status": "Closed"},
                           has_role=lambda role: False)
        stored = _stored(project)
        assert stored["title"].startswith("Sign-in fails"), \
            "the title must not have landed while the status was refused"

    def test_a_guard_is_declared_on_the_entity_not_in_a_route(self):
        """A rule enforced in one handler is a rule the next one forgets."""
        assert "status" in editable.entity("bug").field_guards

    def test_other_entities_have_no_guards(self):
        for name in ("test_case", "checklist_item"):
            assert editable.entity(name).field_guards == {}


class TestCreate:

    def test_a_hand_filed_report_gets_the_next_id(self, client, project,
                                                  editing_on):
        headers = _prepare(client, project)
        resp = client.post("/api/edit/bug", json={"values": {}},
                           headers=headers)
        assert resp.status_code == 201
        assert resp.get_json()["item"]["id"] == "BUG-002"

    def test_it_opens_as_open_and_not_ai_generated(self, client, project,
                                                  editing_on):
        headers = _prepare(client, project)
        item = client.post("/api/edit/bug", json={"values": {}},
                           headers=headers).get_json()["item"]
        assert item["status"] == "Open"
        assert item["severity"] == "Minor"
        assert item["ai_generated"] is False

    def test_a_bug_cannot_be_deleted_through_the_editor(self, client, project,
                                                        editing_on):
        """``routes/bugs.py`` already decided this question: bulk delete is
        admin-only, because deleting a report destroys evidence somebody
        gathered. A per-row delete open to every member would undo that."""
        headers = _prepare(client, project)
        resp = client.delete("/api/edit/bug/BUG-001", headers=headers)
        assert resp.status_code == 405
        assert resp.get_json()["error"] == "not_deletable"


class TestEndpoints:

    def test_a_refused_role_is_a_403_naming_the_reason(self, client, project,
                                                      editing_on,
                                                      monkeypatch):
        """403, not 400: "try again" would be a lie — the answer is "ask an
        admin"."""
        monkeypatch.setattr("engine.permissions.has_role",
                            lambda role: role == "user")
        headers = _prepare(client, project)
        resp = client.patch("/api/edit/bug/BUG-001",
                            json={"changes": {"status": "Closed"}},
                            headers=headers)
        assert resp.status_code == 403
        assert resp.get_json()["reason"] == "needs_role"

    def test_a_refused_move_is_a_400(self, client, project, editing_on,
                                     monkeypatch):
        monkeypatch.setattr("engine.permissions.has_role", lambda role: True)
        headers = _prepare(client, project)
        client.patch("/api/edit/bug/BUG-001",
                     json={"changes": {"status": "Closed"}}, headers=headers)
        resp = client.patch("/api/edit/bug/BUG-001",
                            json={"changes": {"status": "In Progress"}},
                            headers=headers)
        assert resp.status_code == 400
        assert resp.get_json()["reason"] == "not_allowed_from"

    def test_the_status_change_is_audited(self, client, project, editing_on):
        headers = _prepare(client, project)
        client.patch("/api/edit/bug/BUG-001",
                     json={"changes": {"status": "In Progress"}},
                     headers=headers)
        rows = [row for row in db.list_audit(project_id=project, limit=20)
                if row["action"] == "update"]
        assert rows and "status" in (rows[0]["diff"] or {})

    def test_creating_needs_a_csrf_token(self, client, project, editing_on):
        with client.session_transaction() as sess:
            sess["project_id"] = project
        client.application.config["WTF_CSRF_ENABLED"] = True
        try:
            assert client.post("/api/edit/bug",
                               json={"values": {}}).status_code == 400
        finally:
            client.application.config["WTF_CSRF_ENABLED"] = False


class TestBulkToolbarHonoursTheSameGate:
    """A rule the bulk toolbar can bypass is decorative.

    "Set status → Closed" over twenty checkboxes is the *easiest* way to skip
    a gate the single-row editor enforces.
    """

    @staticmethod
    def _post(client, project, form):
        with client.session_transaction() as sess:
            sess["project_id"] = project
        return client.post("/bugs/bulk", data=form, follow_redirects=True)

    def test_bulk_close_is_refused_for_a_plain_member(self, client, project,
                                                      monkeypatch):
        monkeypatch.setattr("engine.permissions.has_role",
                            lambda role: role == "user")
        db_id = _stored(project)["id"]
        self._post(client, project, {"bug_ids": [str(db_id)],
                                     "action": "close"})
        assert _stored(project)["status"] == "Open"

    def test_bulk_status_to_closed_is_refused_too(self, client, project,
                                                  monkeypatch):
        monkeypatch.setattr("engine.permissions.has_role",
                            lambda role: role == "user")
        db_id = _stored(project)["id"]
        self._post(client, project, {"bug_ids": [str(db_id)],
                                     "action": "status",
                                     "status_value": "Closed"})
        assert _stored(project)["status"] == "Open"

    def test_bulk_status_to_an_open_status_still_works(self, client, project,
                                                      monkeypatch):
        """The gate is on closing, not on triage."""
        monkeypatch.setattr("engine.permissions.has_role",
                            lambda role: role == "user")
        db_id = _stored(project)["id"]
        self._post(client, project, {"bug_ids": [str(db_id)],
                                     "action": "status",
                                     "status_value": "In Progress"})
        assert _stored(project)["status"] == "In Progress"

    def test_an_admin_may_bulk_close(self, client, project, monkeypatch):
        monkeypatch.setattr("engine.permissions.has_role", lambda role: True)
        db_id = _stored(project)["id"]
        self._post(client, project, {"bug_ids": [str(db_id)],
                                     "action": "close"})
        assert _stored(project)["status"] == "Closed"


class TestPage:

    def test_the_body_fields_are_editable(self, client, project, editing_on):
        body = _render(client, project)
        for field in ("title", "severity", "priority", "environment",
                      "preconditions", "steps_to_reproduce", "actual_result",
                      "expected_result", "comment"):
            assert f'data-ie-field="{field}"' in body, field

    def test_the_status_is_a_select_carrying_the_row_version(self, client,
                                                            project,
                                                            editing_on):
        body = _render(client, project)
        assert 'data-bug-status="BUG-001"' in body
        assert "data-bug-version" in body

    def test_the_select_offers_only_reachable_statuses(self, client, project,
                                                      editing_on,
                                                      monkeypatch):
        """From Open there is no way to Reopened, so it must not be offered —
        a control that lists a move the server refuses is a broken control."""
        monkeypatch.setattr("engine.permissions.has_role", lambda role: True)
        options = _status_options(_render(client, project))
        assert "Reopened" not in options
        assert set(options) == set(wf.allowed_from("Open"))

    def test_closed_is_absent_for_a_plain_member(self, client, project,
                                                 editing_on, monkeypatch):
        """The server checks again — a missing option is UX, not a
        permission — but offering it would be an invitation to a 403."""
        monkeypatch.setattr("engine.permissions.has_role",
                            lambda role: role == "user")
        assert "Closed" not in _status_options(_render(client, project))

    def test_closed_is_offered_to_an_admin(self, client, project, editing_on,
                                          monkeypatch):
        monkeypatch.setattr("engine.permissions.has_role", lambda role: True)
        assert "Closed" in _status_options(_render(client, project))

    def test_create_is_offered(self, client, project, editing_on):
        assert 'id="bug-create"' in _render(client, project)

    def test_all_three_scripts_are_loaded_in_order(self, client, project,
                                                   editing_on):
        body = _render(client, project)
        assert body.index("js/editor-shared.js") < body.index(
            "js/bug-editor.js")
        assert "js/inline-edit.js" in body

    def test_the_editor_is_absent_with_the_flag_off(self, client, project,
                                                    monkeypatch):
        monkeypatch.setenv("EDITORS_ENABLED", "0")
        body = _render(client, project)
        for marker in ("data-bug-status", "js/bug-editor.js",
                       "js/inline-edit.js", 'id="bug-create"'):
            assert marker not in body, marker

    def test_the_status_still_shows_as_a_badge_with_the_flag_off(
            self, client, project, monkeypatch):
        monkeypatch.setenv("EDITORS_ENABLED", "0")
        body = _render(client, project)
        assert "status-badge-open" in body
        assert "Sign-in fails with a valid password" in body


class TestAttachmentsAreOutOfScope:
    """Deliberately not built here, and the reason is not "no time".

    An attachment needs somewhere to live. Today ``bug_report.attachment``
    holds a path to a run artefact on the local filesystem, which on the free
    plan is wiped on every restart — so an uploaded screenshot would vanish
    without telling anybody, which is worse than not offering the upload.
    Blob storage is E0.5 and needs the owner's dashboard action; the follow-up
    is recorded as E4.5a in the plan.

    What is pinned here is that nothing pretends otherwise.
    """

    def test_attachment_is_not_an_editable_field(self):
        assert "attachment" not in editable.entity("bug").fields

    def test_the_page_offers_no_upload_control(self, client, project,
                                              editing_on):
        body = _render(client, project)
        assert 'type="file"' not in body.split(
            'class="bug-card', 1)[-1], "no upload inside a bug card"

    def test_existing_attachments_still_render(self, client, project,
                                              editing_on):
        """The gallery of run artefacts is untouched by this editor."""
        assert "bug_attachments" in _render(client, project) or \
            "Attachment" in _render(client, project)


# ── Helpers ───────────────────────────────────────────────────────

def _prepare(client, project):
    with client.session_transaction() as sess:
        sess["project_id"] = project
    token = client.get("/api/csrf-token").get_json()["token"]
    return {"X-CSRFToken": token}


def _render(client, project):
    with client.session_transaction() as sess:
        sess["project_id"] = project
    resp = client.get("/bug-reports")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def _status_options(body):
    import re
    block = re.search(r'data-bug-status="BUG-001".*?</select>', body, re.S)
    assert block, "the status control should be rendered"
    return re.findall(r'<option value="([^"]+)"', block.group(0))
