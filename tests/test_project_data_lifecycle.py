"""E8.5 — retention, export, and a delete button that deletes.

    видалення прибирає і блоби, і рядки; аудит фіксує
    (deletion removes both the blobs and the rows; the audit records it)

Three clauses, all three false before this work, and the first one was not a
near miss. Measured on a clean database:

    orphan bugs before delete: 0
    orphan bugs after delete:  1
       STILL IN THE DATABASE: 'leaky bug' | 'secret steps'

``bug_report.project_id`` is ``ON DELETE SET NULL``. That is the right
choice for the column — a bug filed through the chat widget has no project —
and the wrong behaviour for a deletion: the rows were detached, not removed,
and kept their titles, their steps, their actual and expected results, and
the storage keys of their attachments. Meanwhile no file was touched and
nothing was written to the audit log, so the most destructive action in the
product left no trace of itself.

``TestTheRowsActuallyGo`` is that regression, written first and named for
what it found.

The rest follows the three clauses, plus two things the criterion does not
mention and a reader will ask about anyway: what deletion deliberately
*keeps* (the audit trail and the spend history), and what happens when
storage refuses half way — which is the ordering decision in
``engine/retention.py``'s docstring, asserted rather than described.
"""
from __future__ import annotations

import io
import json
import secrets
import zipfile

import pytest

from app import app as flask_app
from engine import db as _db
from engine import retention
from engine import storage


# ── harness ──────────────────────────────────────────────────────────

PNG = b"\x89PNG\r\n\x1a\n" + b"evidence" * 12


@pytest.fixture(autouse=True)
def _ready(monkeypatch):
    monkeypatch.setitem(flask_app.config, "TESTING", True)
    monkeypatch.setitem(flask_app.config, "WTF_CSRF_ENABLED", False)
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    _db.init_db()
    return True


#: Set by the ``project`` fixture. The bug title is unique per test because
#: :func:`_bug_rows_anywhere` deliberately searches every project — that is
#: how it catches rows detached rather than deleted — so a shared title makes
#: one test count another test's leftovers.
_BUG_TITLE: list[str] = ["the total is wrong"]


@pytest.fixture
def project(make_project):
    """A project with something in every drawer that matters."""
    _BUG_TITLE[0] = f"the total is wrong {secrets.token_hex(3)}"
    pid = make_project(f"Doomed {secrets.token_hex(4)}")
    _db.save_test_cases(pid, [{"id": "TC-001", "title": "sign in",
                               "steps": ["open"], "expected_result": "in"}])
    _db.save_checklist(pid, [{"id": "CL-001", "item": "check the total",
                              "area": "Cart"}])
    _db.save_bug(pid, {
        "id": "BUG-001", "title": _BUG_TITLE[0],
        "severity": "Major", "priority": "High", "status": "Open",
        "steps_to_reproduce": "add two items", "actual_result": "3.00",
        "expected_result": "2.00"}, source="manual")
    return pid


def _place_evidence(project_id: str, org_id: str | None = None) -> dict:
    """Two files under this project's prefix, one under a neighbour's.

    Takes *org_id* because the key carries it (ADR 0002 §4.2), so this is
    **mode-dependent**: the engine-level tests below pass ``None`` and get
    ``org/_none/…``, while the route tests pass whatever organisation the
    signed-in client is actually in — which with the flags on is a real id.
    Writing files under one prefix and deleting under another is a test that
    measures its own mistake.
    """
    from engine import blobs as _blobs
    backend = storage.backend_for(org_id)
    keys = []
    for name in ("before.png", "after.png"):
        key = _blobs.key_for(project_id, "bug", "1", name, org_id=org_id)
        backend.put(key, io.BytesIO(PNG))
        keys.append(key)
    neighbour = _blobs.key_for("some-other-project", "bug", "1", "theirs.png",
                               org_id=org_id)
    backend.put(neighbour, io.BytesIO(PNG))
    return {"mine": keys, "neighbour": neighbour}


@pytest.fixture
def evidence(project):
    """Evidence for the caller with no organisation — the engine-level view."""
    return _place_evidence(project)


def _bug_rows_anywhere(title: str) -> int:
    """Every bug row with this title, whatever project it claims.

    Deliberately not scoped to the project: the defect was rows that stop
    claiming the project while keeping everything else, so a query filtered
    by ``project_id`` would report them gone.
    """
    with _db.session_scope() as sess:
        return sess.query(_db.BugReport).filter(
            _db.BugReport.title == title).count()


# ── the regression ───────────────────────────────────────────────────

class TestTheRowsActuallyGo:

    def test_a_deleted_project_leaves_no_bug_report_behind(self, project):
        """The measured defect. ``SET NULL`` detached these rows and every
        word in them stayed in the database."""
        assert _bug_rows_anywhere(_BUG_TITLE[0]) == 1

        retention.delete_project_data(project)

        assert _bug_rows_anywhere(_BUG_TITLE[0]) == 0, (
            "the bug rows were detached from the project, not deleted — "
            "their steps and results are still in the database")

    def test_nothing_of_the_project_is_left_in_any_content_table(
            self, project):
        retention.delete_project_data(project)

        with _db.session_scope() as sess:
            for name, label in retention.CONTENT_TABLES:
                model = getattr(_db, name, None)
                if model is None:
                    continue
                assert sess.query(model).filter(
                    model.project_id == project).count() == 0, label

    def test_the_project_row_itself_goes(self, project):
        retention.delete_project_data(project)
        assert _db.get_project(project) is None

    def test_another_project_s_rows_are_untouched(self, project,
                                                  make_project):
        neighbour = make_project(f"Innocent {secrets.token_hex(4)}")
        _db.save_bug(neighbour, {
            "id": "BUG-001", "title": "somebody else's bug",
            "severity": "Minor", "priority": "Low", "status": "Open",
            "steps_to_reproduce": "s", "actual_result": "a",
            "expected_result": "e"}, source="manual")

        retention.delete_project_data(project)

        assert _bug_rows_anywhere("somebody else's bug") == 1
        assert _db.get_project(neighbour) is not None


class TestTheBlobsActuallyGo:

    def test_the_files_are_removed_from_storage(self, project, evidence):
        backend = storage.backend_for(None)
        assert all(backend.exists(k) for k in evidence["mine"])

        report = retention.delete_project_data(project)

        assert report.blobs == 2
        assert not any(backend.exists(k) for k in evidence["mine"])

    def test_another_project_s_files_survive(self, project, evidence):
        retention.delete_project_data(project)

        assert storage.backend_for(None).exists(evidence["neighbour"]), (
            "the prefix was too wide and took a neighbour's evidence")

    def test_deletion_goes_through_the_backend_in_force(self, project,
                                                        monkeypatch):
        """Not through the filesystem. An org on a bucket must have its
        bucket cleared, and a hard-coded ``shutil.rmtree`` would delete
        nothing while reporting success."""
        asked: list[str] = []

        class _Watching(storage.LocalBackend):
            def delete_prefix(self, prefix):
                asked.append(prefix)
                return 7

        monkeypatch.setattr(storage, "backend_for",
                            lambda org_id=None: _Watching())

        report = retention.delete_project_data(project, org_id="org-a")

        assert report.blobs == 7
        assert asked and asked[0] == f"org/org-a/project/{project}/"


class TestTheAuditRecordsIt:

    def test_a_deletion_is_recorded(self, project):
        retention.delete_project_data(project, org_id="org-a",
                                      user_id="u-someone")

        entries = [e for e in _db.list_audit(project_id=project)
                   if e.get("action") == "delete_data"]
        assert entries, "the most destructive action in the product was silent"
        assert entries[0].get("user_id") == "u-someone"

    def test_the_audit_row_survives_the_deletion(self, project):
        """It has to. ``audit_log.project_id`` carries no foreign key, which
        is the reason — asserted here rather than trusted, because a later
        migration adding one would erase exactly the evidence that a
        deletion happened."""
        retention.delete_project_data(project)

        assert [e for e in _db.list_audit(project_id=project)
                if e.get("action") == "delete_data"]

    def test_it_records_what_was_removed(self, project, evidence):
        retention.delete_project_data(project)

        entry = [e for e in _db.list_audit(project_id=project)
                 if e.get("action") == "delete_data"][0]
        diff = entry.get("diff") or {}
        assert diff.get("blobs") == 2
        assert (diff.get("rows") or {}).get("bug reports") == 1

    def test_a_refused_deletion_is_recorded_too(self, project, monkeypatch):
        """"We tried to delete this and could not" is a thing an auditor
        needs as much as the success."""
        class _Refusing(storage.LocalBackend):
            def delete_prefix(self, prefix):
                raise storage.StorageError("the bucket is unreachable")

        monkeypatch.setattr(storage, "backend_for",
                            lambda org_id=None: _Refusing())

        retention.delete_project_data(project)

        assert [e for e in _db.list_audit(project_id=project)
                if e.get("action") == "delete_failed"]


class TestWhatDeletionKeeps:

    def test_the_spend_history_survives(self, project):
        """It is the organisation's bill, carries no project content, and
        deleting it would rewrite a financial record."""
        _db.record_llm_usage(kind="test_cases", model="claude-x",
                             input_tokens=10, output_tokens=5,
                             cost_micros=100, org_id="org-a",
                             project_id=project, key_source="platform")

        retention.delete_project_data(project, org_id="org-a")

        with _db.session_scope() as sess:
            assert sess.query(_db.LlmUsage).filter(
                _db.LlmUsage.project_id == project).count() == 1

    def test_the_kept_list_is_shown_not_implied(self):
        """Whatever the page renders has to come from somewhere a reader
        can find. A deletion that quietly keeps something is the defect
        this module exists to end, in miniature."""
        labels = [label for label, _why in retention.KEPT_TABLES]
        assert "audit log" in labels
        assert all(why for _label, why in retention.KEPT_TABLES), (
            "a kept table with no stated reason is a kept table nobody "
            "can argue with")


class TestWhenStorageRefuses:
    """The ordering decision, asserted rather than described.

    Blobs go first because the two half-deletions are not equally bad:
    rows pointing at missing files is visible and is what the ephemeral
    disk already does daily; files nobody can find with nothing pointing at
    them is a deletion that silently did not delete.
    """

    @pytest.fixture
    def refusing(self, monkeypatch):
        class _Refusing(storage.LocalBackend):
            def delete_prefix(self, prefix):
                raise storage.StorageError("the bucket is unreachable")

        monkeypatch.setattr(storage, "backend_for",
                            lambda org_id=None: _Refusing())
        return _Refusing

    def test_nothing_is_deleted_at_all(self, project, refusing):
        report = retention.delete_project_data(project)

        assert report.ok is False
        assert _db.get_project(project) is not None
        assert _bug_rows_anywhere(_BUG_TITLE[0]) == 1, (
            "the rows went while the files stayed — the half-deletion the "
            "ordering exists to prevent")

    def test_the_report_says_the_project_is_untouched(self, project,
                                                      refusing):
        report = retention.delete_project_data(project)

        assert "nothing was deleted" in report.problem.lower()
        assert "untouched" in report.problem.lower()

    def test_a_second_attempt_still_works_once_storage_returns(
            self, project, refusing, monkeypatch):
        assert retention.delete_project_data(project).ok is False

        monkeypatch.setattr(storage, "backend_for",
                            lambda org_id=None: storage.LocalBackend())

        assert retention.delete_project_data(project).ok is True
        assert _db.get_project(project) is None


# ── the survey ───────────────────────────────────────────────────────

class TestTheSurvey:

    def test_it_counts_without_deleting(self, project, evidence):
        report = retention.survey(project)

        assert report.rows["bug reports"] == 1
        assert report.rows["test cases"] == 1
        assert report.blobs == 2
        assert _db.get_project(project) is not None, "the survey deleted"

    def test_it_names_the_project(self, project):
        assert retention.survey(project).project_name.startswith("Doomed")

    def test_a_storage_that_cannot_be_read_does_not_break_it(
            self, project, monkeypatch):
        class _Blind(storage.LocalBackend):
            def usage(self, prefix):
                raise storage.StorageError("cannot list")

        monkeypatch.setattr(storage, "backend_for",
                            lambda org_id=None: _Blind())

        assert retention.survey(project).blobs == 0

    def test_the_summary_names_the_project_and_the_counts(self, project,
                                                          evidence):
        report = retention.delete_project_data(project)

        summary = report.summary()
        assert "Doomed" in summary
        assert "1 bug reports" in summary
        assert "2 files" in summary


# ── retention as a policy ────────────────────────────────────────────

class TestRetentionStopsBeingASymptom:
    """ADR 0002 §1.1: one day and five runs are not a policy, they are what
    an ephemeral disk forces. E8.2 made durable storage possible; this is
    the part where the numbers stop apologising for the disk."""

    def test_on_the_ephemeral_disk_the_numbers_are_the_survival_ones(
            self, monkeypatch):
        monkeypatch.delenv("STORAGE_BACKEND", raising=False)

        policy = retention.policy_for()

        assert policy.durable is False
        assert (policy.days, policy.max_runs) == (
            retention.EPHEMERAL_DAYS, retention.EPHEMERAL_MAX_RUNS)
        assert policy.source == "ephemeral-disk"

    def test_on_durable_storage_the_window_opens_up(self, monkeypatch):
        monkeypatch.setattr(storage, "describe",
                            lambda org_id=None: {"backend": "s3",
                                                 "durable": True})

        policy = retention.policy_for()

        assert policy.durable is True
        assert policy.days == retention.DURABLE_DAYS > retention.EPHEMERAL_DAYS

    def test_the_explanation_distinguishes_the_two_promises(self,
                                                            monkeypatch):
        """"Kept for 30 days" and "kept until the next restart" are
        different promises, and a page that renders one sentence for both
        is making the wrong one."""
        monkeypatch.delenv("STORAGE_BACKEND", raising=False)
        ephemeral = retention.policy_for().explanation
        monkeypatch.setattr(storage, "describe",
                            lambda org_id=None: {"durable": True})
        durable = retention.policy_for().explanation

        assert "temporary disk" in ephemeral
        assert "restart" in ephemeral
        assert "survive a restart" in durable
        assert ephemeral != durable

    def test_an_operator_can_override_the_window(self, monkeypatch):
        monkeypatch.setattr(storage, "describe",
                            lambda org_id=None: {"durable": True})
        monkeypatch.setenv("ARTEFACT_RETENTION_DAYS", "90")

        assert retention.policy_for().days == 90

    def test_zero_is_refused_rather_than_obeyed(self, monkeypatch, caplog):
        """A plausible typo for "keep forever" that would mean "delete
        everything immediately"."""
        import logging
        monkeypatch.setattr(storage, "describe",
                            lambda org_id=None: {"durable": True})
        monkeypatch.setenv("ARTEFACT_RETENTION_DAYS", "0")

        with caplog.at_level(logging.WARNING):
            assert retention.policy_for().days == retention.DURABLE_DAYS
        assert any("below 1" in r.getMessage() for r in caplog.records)

    def test_nonsense_falls_back_and_says_so(self, monkeypatch, caplog):
        import logging
        monkeypatch.setattr(storage, "describe",
                            lambda org_id=None: {"durable": True})
        monkeypatch.setenv("ARTEFACT_MAX_RUNS", "lots")

        with caplog.at_level(logging.WARNING):
            assert retention.policy_for().max_runs == retention.DURABLE_MAX_RUNS
        assert any("not a number" in r.getMessage() for r in caplog.records)


class TestTheRunnerAsksThePolicy:
    """Otherwise durable storage would make a longer window *possible* and
    change nothing, which is the shape of a setting nobody can reach."""

    def test_it_uses_the_policy_when_nothing_is_set(self, monkeypatch):
        from engine import automation_runner as runner
        monkeypatch.delenv("AUTOMATION_RUN_RETENTION_DAYS", raising=False)
        monkeypatch.delenv("AUTOMATION_RUN_MAX_KEPT", raising=False)
        monkeypatch.setattr(storage, "describe",
                            lambda org_id=None: {"durable": True})

        assert runner.retention_numbers() == (retention.DURABLE_DAYS,
                                              retention.DURABLE_MAX_RUNS)

    def test_an_explicit_setting_still_wins(self, monkeypatch):
        from engine import automation_runner as runner
        monkeypatch.setattr(storage, "describe",
                            lambda org_id=None: {"durable": True})
        monkeypatch.setenv("AUTOMATION_RUN_RETENTION_DAYS", "3")

        assert runner.retention_numbers()[0] == 3

    def test_the_override_is_read_now_and_not_at_import(self, monkeypatch):
        """The module constant is captured once. Reading it would mean an
        operator raising the window in a dashboard sees nothing until a
        redeploy — which engine/features.py and engine/storage.py both
        refuse to do."""
        from engine import automation_runner as runner
        monkeypatch.setenv("AUTOMATION_RUN_MAX_KEPT", "42")

        assert runner.retention_numbers()[1] == 42

    def test_a_broken_policy_never_blocks_a_run(self, monkeypatch):
        from engine import automation_runner as runner
        monkeypatch.delenv("AUTOMATION_RUN_RETENTION_DAYS", raising=False)
        monkeypatch.delenv("AUTOMATION_RUN_MAX_KEPT", raising=False)
        monkeypatch.setattr(retention, "policy_for",
                            lambda org_id=None: (_ for _ in ()).throw(
                                RuntimeError("database down")))

        assert runner.retention_numbers() == (
            runner.AUTOMATION_RUN_RETENTION_DAYS,
            runner.AUTOMATION_RUN_MAX_KEPT)


# ── export ───────────────────────────────────────────────────────────

class TestExportingBeforeDeleting:

    def test_the_zip_holds_the_rows_and_the_files(self, project, evidence):
        blob, notes = retention.export_project_data(project)

        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            names = archive.namelist()
            assert "project.json" in names
            assert "data/bug_reports.json" in names
            assert sum(1 for n in names if n.startswith("files/")) == 2
            bugs = json.loads(archive.read("data/bug_reports.json"))
            assert bugs[0]["title"] == _BUG_TITLE[0]
        assert notes["truncated"] is False

    def test_the_files_come_out_with_their_bytes(self, project, evidence):
        blob, _notes = retention.export_project_data(project)

        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            stored = [archive.read(n) for n in archive.namelist()
                      if n.startswith("files/")]
        assert stored and all(b == PNG for b in stored), (
            "an export of screenshots that contains no screenshot bytes is "
            "a list of filenames")

    def test_a_truncated_export_says_so_inside_the_archive(
            self, project, evidence, monkeypatch):
        """A silent cap is worse than a refusal: the person who asked for
        their data would not know to ask again."""
        monkeypatch.setattr(retention, "EXPORT_MAX_BYTES", 1)

        blob, notes = retention.export_project_data(project)

        assert notes["truncated"] is True
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            readme = archive.read("README.txt").decode()
        assert "INCOMPLETE" in readme

    def test_a_file_that_cannot_be_read_costs_only_that_file(
            self, project, evidence, monkeypatch):
        class _Flaky(storage.LocalBackend):
            def get_bytes(self, key):
                raise storage.StorageError("gone")

        monkeypatch.setattr(storage, "backend_for",
                            lambda org_id=None: _Flaky())

        blob, notes = retention.export_project_data(project)

        assert notes["skipped"]
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            assert "data/bug_reports.json" in archive.namelist()
            assert "missing" in archive.read("README.txt").decode()

    def test_it_does_not_include_another_project_s_files(self, project,
                                                         evidence):
        blob, _notes = retention.export_project_data(project)

        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            assert not any("theirs.png" in n for n in archive.namelist())

    def test_an_empty_project_still_produces_a_readable_archive(
            self, make_project):
        pid = make_project(f"Empty {secrets.token_hex(4)}")

        blob, _notes = retention.export_project_data(pid)

        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            assert json.loads(archive.read("project.json"))["id"] == pid
            assert archive.read("README.txt")


# ── the routes ───────────────────────────────────────────────────────

class TestTheButtons:

    def _client(self, project_id, sign_in):
        client = flask_app.test_client()
        sign_in(client)
        with client.session_transaction() as sess:
            sess["project_id"] = project_id
        return client

    def _org_of(self, client) -> str | None:
        """The organisation this browser is in — ``None`` with the flags off.

        The route passes it to the deletion, so the keys it clears carry it.
        Guessing instead would make these tests pass in one mode only.
        """
        from engine import permissions as _perm
        with client.session_transaction() as sess:
            return sess.get(_perm.SESSION_ORG_KEY) or None

    def test_the_delete_button_deletes_the_files_too(self, project,
                                                     sign_in):
        client = self._client(project, sign_in)
        org = self._org_of(client)
        placed = _place_evidence(project, org)

        client.post(f"/delete-project/{project}", follow_redirects=False)

        assert _db.get_project(project) is None
        assert not storage.backend_for(org).exists(placed["mine"][0])

    def test_the_flash_says_what_went(self, project, sign_in):
        client = self._client(project, sign_in)
        _place_evidence(project, self._org_of(client))

        client.post(f"/delete-project/{project}", follow_redirects=False)

        with client.session_transaction() as sess:
            said = " ".join(m for _c, m in sess.get("_flashes", []))
        assert "2 files" in said, (
            '"Project deleted." over an action that also removed evidence '
            "tells the user less than it knows")

    def test_a_storage_refusal_is_not_reported_as_success(
            self, project, sign_in, monkeypatch):
        class _Refusing(storage.LocalBackend):
            def delete_prefix(self, prefix):
                raise storage.StorageError("unreachable")

        monkeypatch.setattr(storage, "backend_for",
                            lambda org_id=None: _Refusing())
        client = self._client(project, sign_in)

        client.post(f"/delete-project/{project}", follow_redirects=False)

        with client.session_transaction() as sess:
            flashes = list(sess.get("_flashes", []))
        assert flashes and flashes[0][0] == "error"
        assert _db.get_project(project) is not None

    def test_deleting_twice_is_not_an_error(self, project, sign_in):
        client = self._client(project, sign_in)
        client.post(f"/delete-project/{project}")

        response = client.post(f"/delete-project/{project}",
                               follow_redirects=False)

        assert response.status_code in (302, 303)

    def test_the_export_route_returns_a_zip(self, project, sign_in):
        client = self._client(project, sign_in)
        _place_evidence(project, self._org_of(client))

        response = client.get(f"/projects/{project}/export")

        assert response.status_code == 200
        assert response.mimetype == "application/zip"
        assert "attachment" in response.headers["Content-Disposition"]
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            assert "project.json" in archive.namelist()

    def test_the_export_is_audited(self, project, sign_in):
        client = self._client(project, sign_in)

        client.get(f"/projects/{project}/export")

        assert [e for e in _db.list_audit(project_id=project)
                if e.get("action") == "export"], (
            "handing somebody the whole project in one file is worth a "
            "line in the audit trail")

    def test_the_export_link_sits_next_to_the_delete_button(self, project,
                                                            sign_in):
        """Rendered from the same row, so the offer is where the hesitation
        is.

        The project has to be *visible* to this caller for the row to exist
        at all — organisation membership with the flags on, ``owner_sid``
        without them (``routes/_shared.visible_projects``). Asserting the
        delete button is present as well is what keeps this honest: without
        it, a page that stopped listing projects entirely would pass by
        having neither.
        """
        client = self._client(project, sign_in)
        # One request first, so the app mints and stores this browser's
        # session id — it is derived inside a request context and there is
        # no honest way to guess it from outside one.
        client.get("/")
        with client.session_transaction() as sess:
            sid = sess.get("_tf_sid")
        with _db.session_scope() as db_sess:
            db_sess.get(_db.Project, project).owner_sid = sid

        body = client.get("/").get_data(as_text=True)

        assert f"/delete-project/{project}" in body, (
            "the project is not listed for this caller, so this test is "
            "not looking at the row it claims to")
        assert f"/projects/{project}/export" in body

    def test_the_confirmation_says_what_is_destroyed(self):
        """The old text was "Delete project?" — which asks about the
        project, while what goes is the data."""
        from engine.i18n import en, ua
        for pack in (en, ua):
            text = pack.STRINGS["delete_confirm"] if hasattr(pack, "STRINGS") \
                else pack.TRANSLATIONS["delete_confirm"]
            assert len(text) > 40, text
            assert "undone" in text.lower() or "незворотн" in text.lower()

    def test_both_routes_are_in_the_fail_closed_table(self):
        from engine import route_policy
        assert route_policy.POLICY.get("delete_project") == "admin"
        assert route_policy.POLICY.get("export_project") == "admin"
