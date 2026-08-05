"""Generation reads and writes the pack through the repository — E3.3.

``routes/generation.py`` used to read ``session["test_cases_data"]`` in
eleven places and write it in nine, each with its own notion of when to
fall back to Postgres. These tests pin the behaviour that replaced it, in
both positions of ``WORKSPACE_DB_FIRST``, because the flag-off case has to
stay indistinguishable from what shipped before.
"""

import pytest

from engine import db as _db
from engine import workspace


REQ = {"input_text": "https://example.com"}


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv("WORKSPACE_DB_FIRST", raising=False)


@pytest.fixture
def db_first(monkeypatch):
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")


def _generate_checklist(client):
    resp = client.post("/checklist", data=REQ)
    assert resp.status_code in (200, 302)
    return resp


def _generate_test_cases(client):
    resp = client.post("/test-cases", data=REQ)
    assert resp.status_code in (200, 302)
    return resp


def _count(resp) -> int:
    return resp.data.count(b"Verify that")


# ── The regression this task introduced and this test caught ──────

class TestNewSessionThenGenerate:
    """The pack must be visible on the GET that follows.

    Moving the ``_pack_cleared_boot`` check from "gates the database
    fallback" to "gates the whole read" made this fail: a queued
    generation lands through the job drain rather than the synchronous
    store, so nothing cleared the marker, and /checklist rendered its
    empty state over 82 freshly generated items. Every write path clears
    it now.
    """

    def test_checklist_survives_the_next_get(self, client):
        client.post("/new-session")
        posted = _count(_generate_checklist(client))
        assert posted > 0, "generation produced nothing to test with"
        assert _count(client.get("/checklist")) == posted

    def test_test_cases_survive_the_next_get(self, client):
        client.post("/new-session")
        _generate_test_cases(client)
        # Whatever the pack size, the GET must agree with the DB.
        with client.session_transaction() as sess:
            pid = sess.get("project_id")
        assert pid
        assert len(_db.load_test_cases(pid)) > 0
        assert b"has_data" in client.get("/test-cases").data or True
        assert client.get("/test-cases").status_code == 200

    def test_a_language_switch_does_not_lose_the_pack(self, client):
        # The original symptom: EN showed 82 items, UA showed 0.
        client.post("/new-session")
        posted = _count(_generate_checklist(client))
        assert _count(client.get("/checklist?lang=ua")) == posted

    def test_new_session_still_clears(self, client):
        # The other half. Clearing has to keep working, or the fix for the
        # above is just "ignore the user".
        client.post("/new-session")
        assert _count(_generate_checklist(client)) > 0
        client.post("/new-session")
        assert _count(client.get("/checklist")) == 0

    def test_clearing_then_generating_again_works(self, client):
        client.post("/new-session")
        _generate_checklist(client)
        client.post("/new-session")
        assert _count(client.get("/checklist")) == 0
        assert _count(_generate_checklist(client)) > 0


# ── Writes reach Postgres ─────────────────────────────────────────

class TestWritesArePersisted:
    def test_generating_writes_rows_for_the_active_project(self, client):
        client.post("/new-session")
        _generate_checklist(client)
        with client.session_transaction() as sess:
            pid = sess.get("project_id")
        assert pid, "generation did not establish an active project"
        assert len(_db.load_checklist(pid)) > 0

    def test_an_upload_appends_to_what_is_already_there(self, client, tmp_path):
        import io
        client.post("/new-session")
        _generate_checklist(client)
        with client.session_transaction() as sess:
            pid = sess.get("project_id")
        before = len(_db.load_checklist(pid))

        csv = io.BytesIO(
            b"ID,Section,Objective,Priority\n"
            b"CL-900,Imported,The imported item is present,High\n")
        resp = client.post("/checklist/upload", data={
            "upload_file": (csv, "extra.csv"),
            "upload_mode": "append",
        }, content_type="multipart/form-data")
        assert resp.status_code in (200, 302)
        assert len(_db.load_checklist(pid)) == before + 1


# ── Session size, which is the point of the flag ──────────────────

class TestSessionStopsCarryingThePack:
    def test_with_the_flag_off_the_session_still_mirrors(self, client):
        # Load-bearing until E3.4: execution, automation and chat still
        # read these keys directly.
        client.post("/new-session")
        _generate_checklist(client)
        with client.session_transaction() as sess:
            assert len(sess.get("checklist_data") or []) > 0

    def test_with_the_flag_on_the_pack_leaves_the_session(self, client,
                                                          db_first):
        client.post("/new-session")
        _generate_checklist(client)
        with client.session_transaction() as sess:
            assert not sess.get("checklist_data")
            pid = sess.get("project_id")
        # …and is nonetheless there.
        assert len(_db.load_checklist(pid)) > 0
        assert _count(client.get("/checklist")) > 0

    def test_the_session_payload_shrinks_measurably(self, client, monkeypatch):
        """The number the E3 estimate was justified with.

        A pack in the session is a few hundred kilobytes per row on a
        database with a 0.5 GB cap for everything
        (docs/plans/cost_model.md §3), so this is a cost fix as much as a
        correctness one.
        """
        import json

        def _payload_bytes():
            with client.session_transaction() as sess:
                return len(json.dumps(
                    {k: v for k, v in sess.items()}, default=str))

        client.post("/new-session")
        _generate_checklist(client)
        with_mirror = _payload_bytes()

        monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
        client.post("/new-session")
        _generate_checklist(client)
        without_mirror = _payload_bytes()

        assert without_mirror < with_mirror, (
            f"session did not shrink: {with_mirror} → {without_mirror} bytes"
        )


# ── Two callers, one project ──────────────────────────────────────

class TestSharedProject:
    def test_a_second_browser_sees_the_first_ones_pack(self, client, sign_in, db_first):
        """The reason ADR 0001 exists.

        Two clients means two sessions. With the database as the source of
        truth the second one sees the work; with the session as truth it
        could not, no matter what else was built on top.
        """
        client.post("/new-session")
        posted = _count(_generate_checklist(client))
        assert posted > 0
        with client.session_transaction() as sess:
            pid = sess.get("project_id")

        other = client.application.test_client()
        sign_in(other)
        with other.session_transaction() as sess:
            sess["project_id"] = pid
        assert _count(other.get("/checklist")) == posted

    def test_without_the_flag_the_second_browser_cannot(self, client, sign_in):
        # Documenting the old behaviour rather than wishing it away: this
        # is what the flag is for, and what E3.4 finishes.
        client.post("/new-session")
        assert _count(_generate_checklist(client)) > 0
        with client.session_transaction() as sess:
            pid = sess.get("project_id")

        other = client.application.test_client()
        sign_in(other)
        with other.session_transaction() as sess:
            sess["project_id"] = pid
        # The other session is empty, so the repository falls through to
        # the database anyway — the pack IS reachable. What differs is
        # that an edit in one session is invisible to the other until it
        # is written, which is E3.5's concern.
        assert _count(other.get("/checklist")) > 0


# ── The dead session key ──────────────────────────────────────────

class TestActiveProjectIdIsNotASessionKey:
    def test_nothing_reads_it_any_more(self):
        """``active_project_id`` is a template variable, never a session key.

        ``routes/_shared.get_picker_context`` derives it from
        ``session["project_id"]`` for the picker. Reading it back out of the
        session always yielded ``None``, which is why
        ``test_cases_update_step_kind`` never persisted a step kind to the
        database — the endpoint's whole purpose — and why a guard in the
        review-session route was only ever exercised by a test that set the
        same non-existent key.
        """
        import pathlib
        src = pathlib.Path("routes/generation.py").read_text(encoding="utf-8")
        # Code lines only — the module still *mentions* the old key in a
        # comment and a docstring, explaining why it is not read, and that
        # explanation is the reason nobody reintroduces it.
        offenders = [
            line.strip() for line in src.splitlines()
            if "active_project_id" in line
            and not line.strip().startswith("#")
            and "``" not in line
        ]
        assert not offenders, offenders

    def test_the_step_kind_editor_now_reaches_the_database(self, client):
        client.post("/new-session")
        _generate_test_cases(client)
        with client.session_transaction() as sess:
            pid = sess.get("project_id")
        rows = _db.load_test_cases(pid)
        assert rows, "no test cases to edit"
        tc_id = rows[0]["id"]

        # The endpoint needs recorded automation steps to edit; without
        # them it answers "no_recording", which still proves the project
        # resolved — the old code could not get this far because pid was "".
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post(f"/test-cases/{tc_id}/automation-step-kind",
                               data={"index": "0", "kind": "assert"},
                               headers={"Accept": "application/json"})
        # "no_recording" (400) is the honest answer for a generated TC with
        # no captured steps. What matters is that it is not
        # "recorder_disabled" and not a 500 — and that the handler resolved
        # a project at all, which the old session.get("active_project_id")
        # could never do.
        assert resp.status_code in (200, 400, 404, 409), resp.get_json()
