"""E8.4 — backups, and the one thing that makes a backup real.

    відновлення перевірене на реальному бандлі, не «має працювати»
    (restore verified on a real bundle, not "should work")

The criterion is about evidence, and it is pointed at the thing backups are
famous for: existing, going green nightly, and not restoring. So the centre
of this file is ``TestTheRoundTrip`` — build a project with real content,
back it up, **destroy the original**, restore, and compare the result field
by field. Not "a zip was produced"; not "the row count matches". The
restored bug's wording, and its screenshot fetched through the route that
serves it.

Note the order: the bundle's **bytes are read before the deletion**, because
deleting a project deletes its bundles too (E8.5 — a deletion that leaves a
restorable copy is not a deletion). That is a real limitation of backups
here, asserted in ``TestWhereTheyLive`` rather than worked around. The first
draft of ``engine/backup.py`` claimed the opposite in its own docstring and
deleted the bundles anyway; this test found it by failing to read an archive
it had just written.

That last part is not decoration. Attachment keys carry the project id (ADR
0002 §4.2), so a restore into a new project that copied the keys verbatim
would produce a project with the right row counts and a gallery of broken
images — complete by every measure except the one that matters. The test
fetches the bytes.

What is verified here and what is not
-------------------------------------
Everything below runs against the **local** storage backend, which is a real
backend doing real writes, so the round trip is genuine. The S3 path shares
every line of code above `Backend.put` and is exercised against a fake
client, because there is no bucket on this machine — E0.5 is the owner's
action and E8.7 is the task that runs against a live one.

So: *restore verified on a real bundle*, yes. *Restore verified against
Cloudflare R2*, not yet, and this docstring says so rather than letting a
green suite imply it.
"""
from __future__ import annotations

import io
import json
import secrets
import zipfile

import pytest

from app import app as flask_app
from engine import backup
from engine import db as _db
from engine import retention
from engine import storage


PNG = b"\x89PNG\r\n\x1a\n" + b"screenshot-bytes" * 6


@pytest.fixture(autouse=True)
def _ready(monkeypatch):
    monkeypatch.setitem(flask_app.config, "TESTING", True)
    monkeypatch.setitem(flask_app.config, "WTF_CSRF_ENABLED", False)
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("BACKUP_KEEP", raising=False)
    _db.init_db()
    return True


@pytest.fixture
def loaded(make_project):
    """A project with content in every restorable table, and a screenshot.

    Values are unique per run so an assertion cannot pass on another test's
    leftovers — the mistake ``tests/test_project_data_lifecycle.py`` made
    once and now avoids the same way.
    """
    from engine import blobs as _blobs

    tag = secrets.token_hex(3)
    pid = make_project(f"Payments {tag}")
    # `summary` and `test_steps`, not `title`/`steps` — the pack shape is
    # named for the TestFort sheet it round-trips through. Written the wrong
    # way first, and the round trip caught it by comparing None to None and
    # passing: a fixture that stores nothing makes every "the content came
    # back" assertion vacuous.
    _db.save_test_cases(pid, [
        {"id": "TC-001", "summary": f"sign in with a valid card {tag}",
         "test_steps": "open /checkout", "expected_result": "receipt"},
        {"id": "TC-002", "summary": f"expired card is refused {tag}",
         "test_steps": "pay with an expired card",
         "expected_result": "refused"},
    ])
    _db.save_checklist(pid, [
        {"id": "CL-001", "item": f"the total includes VAT {tag}",
         "area": "Cart"}])
    _db.save_bug(pid, {
        "id": "BUG-001", "title": f"the total is wrong {tag}",
        "severity": "Major", "priority": "High", "status": "Open",
        "steps_to_reproduce": "add two items",
        "actual_result": "3.00", "expected_result": "2.00"}, source="manual")

    db_id = _db.list_bugs(pid)[0]["id"]
    key = _blobs.key_for(pid, "bug", str(db_id), "evidence.png")
    storage.backend_for(None).put(key, io.BytesIO(PNG))
    _db.append_bug_attachment(pid, db_id, key)

    return {"pid": pid, "tag": tag, "bug_db_id": db_id, "key": key}


def _bug(project_id: str) -> dict:
    from engine import workspace as _workspace
    rows = _db.list_bugs(project_id)
    return _workspace.bug_row_to_dict(rows[0]) if rows else {}


# ── the criterion ────────────────────────────────────────────────────

class TestTheRoundTrip:
    """Back up, destroy, restore, compare. On a real bundle."""

    def test_a_deleted_project_comes_back_with_its_content(self, loaded):
        bundle = backup.create(loaded["pid"])
        raw = backup.read(bundle.key)
        retention.delete_project_data(loaded["pid"])
        assert _db.get_project(loaded["pid"]) is None, "the fixture survived"

        report = backup.restore(raw)

        assert report.ok, report.problem
        restored = report.project_id
        assert restored and restored != loaded["pid"]
        assert len(_db.load_test_cases(restored) or []) == 2
        assert len(_db.load_checklist(restored) or []) == 1
        assert len(_db.list_bugs(restored)) == 1

    def test_the_words_come_back_and_not_just_the_counts(self, loaded):
        """A row count is satisfied by two empty rows."""
        bundle = backup.create(loaded["pid"])
        raw = backup.read(bundle.key)
        retention.delete_project_data(loaded["pid"])

        restored = backup.restore(raw).project_id

        bug = _bug(restored)
        assert bug["title"] == f"the total is wrong {loaded['tag']}"
        assert bug["steps_to_reproduce"] == "add two items"
        assert bug["actual_result"] == "3.00"
        assert bug["expected_result"] == "2.00"
        summaries = {tc.get("summary") for tc in _db.load_test_cases(restored)}
        assert f"expired card is refused {loaded['tag']}" in summaries
        assert None not in summaries, (
            "the cases came back blank — a row count would still pass")

    def test_the_screenshot_comes_back_and_can_be_fetched(self, loaded,
                                                          sign_in):
        """The one that catches a restore that looks complete.

        Attachment keys carry the project id, so copying them verbatim into
        a new project gives correct row counts and a gallery of broken
        images. Fetched through ``automation_asset`` — the route the bug
        page actually uses — rather than asserted as a list of strings.
        """
        bundle = backup.create(loaded["pid"])
        raw = backup.read(bundle.key)
        retention.delete_project_data(loaded["pid"])

        restored = backup.restore(raw).project_id

        keys = _bug(restored).get("attachments") or []
        assert keys, "the restored bug has no attachment at all"
        assert restored in keys[0], (
            f"the key still points at the old project: {keys[0]}")

        client = flask_app.test_client()
        sign_in(client)
        response = client.get(f"/automation/asset/{keys[0]}")
        assert response.status_code == 200
        assert response.data == PNG, "the bytes did not survive the trip"

    def test_a_bug_still_knows_which_test_case_it_came_from(self, loaded):
        """``related_case_id`` is an integer primary key, and the restored
        cases get new ones. Copied verbatim it would point at whatever row
        happens to hold that id — most likely another project's.

        **The original is deliberately left in place here**, unlike the
        tests above. Deleting it first made this pass against a broken
        implementation: SQLite reuses row ids after a delete, so the old id
        became valid again and happened to name the restored case. Postgres
        does not reuse sequence values, so the same code would have failed
        in production and passed in CI — the divergence E9 already met
        twice. Keeping the original alive forces the ids apart, which is
        what makes the assertion mean anything.
        """
        with _db.session_scope() as sess:
            case = sess.query(_db.TestCase).filter(
                _db.TestCase.project_id == loaded["pid"]).first()
            bug = sess.query(_db.BugReport).filter(
                _db.BugReport.project_id == loaded["pid"]).first()
            bug.related_case_id = case.id
            # `summary`, not `title`: the column is named for the sheet.
            wanted_summary = case.summary

        bundle = backup.create(loaded["pid"])
        restored = backup.restore(backup.read(bundle.key)).project_id

        with _db.session_scope() as sess:
            bug = sess.query(_db.BugReport).filter(
                _db.BugReport.project_id == restored).one()
            assert bug.related_case_id is not None, "the link was dropped"
            case = sess.get(_db.TestCase, bug.related_case_id)
            assert case is not None, "it points at a row that is not there"
            assert case.project_id == restored, (
                "the bug points at a test case in another project — the id "
                "was copied instead of re-pointed")
            assert case.summary == wanted_summary

    def test_the_report_says_what_it_did_not_bring_back(self, loaded):
        """Run history is in the bundle and is not re-inserted. A restore
        that quietly returns less than it was given is the same defect as a
        deletion that quietly keeps something."""
        bundle = backup.create(loaded["pid"])

        report = backup.restore(backup.read(bundle.key))

        assert "execution runs" in report.not_restored
        assert "not restored" in report.summary().lower()

    def test_restoring_never_touches_the_project_it_came_from(self, loaded):
        """Somebody restoring has just lost something. An in-place restore
        that goes wrong in that moment destroys the other copy."""
        bundle = backup.create(loaded["pid"])

        report = backup.restore(backup.read(bundle.key))

        assert report.project_id != loaded["pid"]
        assert _db.get_project(loaded["pid"]) is not None
        assert len(_db.list_bugs(loaded["pid"])) == 1, "the original changed"


# ── integrity ────────────────────────────────────────────────────────

class TestABundleThatCannotBeTrustedIsRefused:

    def test_a_good_bundle_verifies(self, loaded):
        raw, _manifest = backup.build(loaded["pid"])
        ok, problem = backup.verify(raw)
        assert ok, problem

    def test_a_tampered_member_is_caught(self, loaded):
        raw, _m = backup.build(loaded["pid"])
        edited = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as src, \
                zipfile.ZipFile(edited, "w") as dst:
            for name in src.namelist():
                body = src.read(name)
                if name == "data/bug_reports.json":
                    body = body.replace(b"the total is wrong",
                                        b"nothing is wrong")
                dst.writestr(name, body)

        ok, problem = backup.verify(edited.getvalue())

        assert not ok
        assert "checksum" in problem

    def test_a_truncated_archive_is_caught(self, loaded):
        raw, _m = backup.build(loaded["pid"])
        ok, problem = backup.verify(raw[: len(raw) // 2])
        assert not ok and "zip" in problem.lower()

    def test_a_zip_that_is_not_a_bundle_is_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("hello.txt", "not a backup")

        ok, problem = backup.verify(buffer.getvalue())

        assert not ok and "manifest" in problem

    def test_a_newer_bundle_version_is_refused_rather_than_guessed(
            self, loaded):
        """Reading an unfamiliar layout produces a project that looks
        complete and is not — the worst possible restore outcome."""
        raw, _m = backup.build(loaded["pid"])
        bumped = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as src, \
                zipfile.ZipFile(bumped, "w") as dst:
            for name in src.namelist():
                body = src.read(name)
                if name == backup.MANIFEST_NAME:
                    manifest = json.loads(body)
                    manifest["bundle_version"] = backup.BUNDLE_VERSION + 5
                    body = json.dumps(manifest).encode()
                dst.writestr(name, body)

        ok, problem = backup.verify(bumped.getvalue())

        assert not ok
        assert "Upgrade" in problem

    def test_restore_refuses_a_bundle_that_does_not_verify(self, loaded):
        report = backup.restore(b"this is not a zip at all")

        assert report.ok is False
        assert report.project_id == "", "a project was created anyway"

    def test_the_manifest_carries_a_truncated_export_forward(
            self, loaded, monkeypatch):
        """An export that hit its size cap makes an incomplete backup. A
        backup that does not say so is exactly what this epic's criterion
        is aimed at."""
        from engine import blobs as _blobs
        # Two files, because the cap is checked before each write: with one
        # file there is nothing left to leave out, and reporting "truncated"
        # would be a lie in the other direction.
        second = _blobs.key_for(loaded["pid"], "bug",
                                str(loaded["bug_db_id"]), "second.png")
        storage.backend_for(None).put(second, io.BytesIO(PNG))
        monkeypatch.setattr(retention, "EXPORT_MAX_BYTES", 1)

        _raw, manifest = backup.build(loaded["pid"])

        assert manifest["truncated"] is True


# ── where bundles live ───────────────────────────────────────────────

class TestWhereTheyLive:

    def test_a_bundle_is_outside_the_project_prefix(self, loaded):
        """Under the organisation, not the project. Inside it, deleting a
        project by accident would take the backup with it — and that is the
        single most common disaster backups exist for."""
        bundle = backup.create(loaded["pid"], org_id="org-a")

        assert bundle.key.startswith("org/org-a/backup/")
        assert f"/project/{loaded['pid']}/" not in bundle.key

    def test_deleting_a_project_removes_its_bundles(self, loaded):
        """The mirror consequence, and the more dangerous one. A deletion
        that skips the backups leaves a complete, restorable copy of
        everything it claimed to remove."""
        backup.create(loaded["pid"])
        assert backup.list_bundles(loaded["pid"])

        report = retention.delete_project_data(loaded["pid"])

        assert report.bundles >= 1, "the deletion did not count them"
        assert backup.list_bundles(loaded["pid"]) == []

    def test_the_deletion_report_mentions_the_backups(self, loaded):
        backup.create(loaded["pid"])
        report = retention.delete_project_data(loaded["pid"])
        assert "backup" in report.summary().lower()

    def test_one_project_s_bundles_are_not_another_s(self, loaded,
                                                     make_project):
        other = make_project(f"Other {secrets.token_hex(3)}")
        backup.create(loaded["pid"])
        backup.create(other)

        assert len(backup.list_bundles(loaded["pid"])) == 1
        assert len(backup.list_bundles(other)) == 1

    def test_only_the_newest_are_kept(self, loaded, monkeypatch):
        """An unattended weekly job must not fill the bucket."""
        monkeypatch.setenv("BACKUP_KEEP", "2")
        from datetime import datetime, timedelta, timezone
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for day in range(5):
            backup.create(loaded["pid"], now=base + timedelta(days=day))

        bundles = backup.list_bundles(loaded["pid"])

        assert len(bundles) == 2
        assert bundles[0].key > bundles[1].key, "not the newest two"

    def test_a_nonsense_keep_count_falls_back(self, monkeypatch):
        monkeypatch.setenv("BACKUP_KEEP", "0")
        assert backup.keep_count() == backup.DEFAULT_KEEP
        monkeypatch.setenv("BACKUP_KEEP", "many")
        assert backup.keep_count() == backup.DEFAULT_KEEP

    def test_bundles_go_through_the_configured_backend(self, loaded,
                                                        monkeypatch):
        """An organisation on its own bucket must have its backups land
        there, not on the server's disk."""
        written: list[str] = []

        class _Watching(storage.LocalBackend):
            def put(self, key, source):
                written.append(key)
                return super().put(key, source)

        monkeypatch.setattr(storage, "backend_for",
                            lambda org_id=None: _Watching())

        backup.create(loaded["pid"], org_id="org-b")

        assert any(k.startswith("org/org-b/backup/") for k in written)

    def test_a_storage_failure_is_raised_and_not_swallowed(self, loaded,
                                                           monkeypatch):
        """A backup job that reports success over a failed write is the
        canonical version of this whole problem."""
        class _Refusing(storage.LocalBackend):
            def put(self, key, source):
                raise storage.StorageError("the bucket is unreachable")

        monkeypatch.setattr(storage, "backend_for",
                            lambda org_id=None: _Refusing())

        with pytest.raises(storage.StorageError):
            backup.create(loaded["pid"])


# ── audit ────────────────────────────────────────────────────────────

class TestItIsAudited:

    def test_a_backup_is_recorded(self, loaded):
        backup.create(loaded["pid"], user_id="u-someone")

        entries = [e for e in _db.list_audit(project_id=loaded["pid"])
                   if e.get("action") == "backup"]
        assert entries and entries[0].get("user_id") == "u-someone"

    def test_a_restore_is_recorded_against_the_new_project(self, loaded):
        bundle = backup.create(loaded["pid"])

        report = backup.restore(backup.read(bundle.key), user_id="u-someone")

        entries = [e for e in _db.list_audit(project_id=report.project_id)
                   if e.get("action") == "restore"]
        assert entries, "a project appeared out of nowhere with no record"
        assert entries[0].get("entity_id") == loaded["pid"], (
            "the record does not say which project it came from")


# ── the buttons ──────────────────────────────────────────────────────

class TestTheRoutes:

    def _client(self, project_id, sign_in):
        client = flask_app.test_client()
        sign_in(client)
        with client.session_transaction() as sess:
            sess["project_id"] = project_id
        return client

    def _flashes(self, client):
        with client.session_transaction() as sess:
            return list(sess.pop("_flashes", []))

    def test_the_backup_button_writes_a_bundle(self, loaded, sign_in):
        client = self._client(loaded["pid"], sign_in)

        client.post(f"/projects/{loaded['pid']}/backup",
                    follow_redirects=False)

        from engine import permissions as _perm
        with client.session_transaction() as sess:
            org = sess.get(_perm.SESSION_ORG_KEY) or None
        assert backup.list_bundles(loaded["pid"], org_id=org)

    def test_a_failed_backup_is_not_reported_as_success(self, loaded,
                                                        sign_in, monkeypatch):
        class _Refusing(storage.LocalBackend):
            def put(self, key, source):
                raise storage.StorageError("unreachable")

        monkeypatch.setattr(storage, "backend_for",
                            lambda org_id=None: _Refusing())
        client = self._client(loaded["pid"], sign_in)

        client.post(f"/projects/{loaded['pid']}/backup",
                    follow_redirects=False)

        flashes = self._flashes(client)
        assert flashes and flashes[0][0] == "error"

    def test_the_restore_button_rebuilds_the_project(self, loaded, sign_in):
        from engine import permissions as _perm
        client = self._client(loaded["pid"], sign_in)
        with client.session_transaction() as sess:
            org = sess.get(_perm.SESSION_ORG_KEY) or None
        bundle = backup.create(loaded["pid"], org_id=org)
        before = len(_db.list_projects())

        client.post(f"/projects/{loaded['pid']}/restore",
                    data={"key": bundle.key}, follow_redirects=False)

        assert len(_db.list_projects()) == before + 1
        assert any(f[0] == "success" for f in self._flashes(client))

    def test_a_key_from_another_project_is_refused(self, loaded, sign_in,
                                                   make_project):
        """The key comes from a form, and a key is a path. Without this
        check the route reads whatever object the caller names."""
        from engine import permissions as _perm
        client = self._client(loaded["pid"], sign_in)
        with client.session_transaction() as sess:
            org = sess.get(_perm.SESSION_ORG_KEY) or None
        other = make_project(f"Somebody else {secrets.token_hex(3)}")
        theirs = backup.create(other, org_id=org)
        before = len(_db.list_projects())

        client.post(f"/projects/{loaded['pid']}/restore",
                    data={"key": theirs.key}, follow_redirects=False)

        assert len(_db.list_projects()) == before
        flashes = self._flashes(client)
        assert flashes and flashes[0][0] == "error"

    def test_a_made_up_key_is_refused(self, loaded, sign_in):
        client = self._client(loaded["pid"], sign_in)

        client.post(f"/projects/{loaded['pid']}/restore",
                    data={"key": "org/_none/backup/../../etc/passwd"},
                    follow_redirects=False)

        flashes = self._flashes(client)
        assert flashes and flashes[0][0] == "error"

    def test_both_routes_are_in_the_fail_closed_table(self):
        from engine import route_policy
        assert route_policy.POLICY.get("backup_project") == "admin"
        assert route_policy.POLICY.get("restore_project") == "admin"


# ── the scheduled run ────────────────────────────────────────────────

class TestTheScheduledRun:

    def test_it_refuses_when_no_token_is_configured(self, monkeypatch):
        """An open endpoint that walks every project and writes to storage
        is a way to fill somebody's bucket for them."""
        monkeypatch.delenv("BACKUP_TOKEN", raising=False)
        client = flask_app.test_client()

        response = client.post("/api/backup/run")

        assert response.status_code == 403
        assert response.get_json()["error"] == "backup_disabled"

    def test_a_wrong_token_is_401(self, monkeypatch):
        monkeypatch.setenv("BACKUP_TOKEN", "the-real-one")
        client = flask_app.test_client()

        response = client.post("/api/backup/run",
                               headers={"X-TFG-Token": "not-it"})

        assert response.status_code == 401

    def test_it_backs_up_every_project(self, loaded, monkeypatch):
        monkeypatch.setenv("BACKUP_TOKEN", "t0ken")
        client = flask_app.test_client()

        response = client.post("/api/backup/run",
                               headers={"X-TFG-Token": "t0ken"})

        assert response.status_code == 200
        body = response.get_json()
        assert body["backed_up"] >= 1 and body["failed"] == 0
        assert any(r["project_id"] == loaded["pid"] for r in body["results"])

    def test_a_partial_failure_is_not_a_green_run(self, loaded, monkeypatch):
        """"37 backed up" over a run where four failed is the shape of
        report that gets believed."""
        monkeypatch.setenv("BACKUP_TOKEN", "t0ken")

        def _explode(project_id, **kwargs):
            raise storage.StorageError("bucket full")

        monkeypatch.setattr(backup, "create", _explode)
        client = flask_app.test_client()

        response = client.post("/api/backup/run",
                               headers={"X-TFG-Token": "t0ken"})

        assert response.status_code == 500
        assert response.get_json()["failed"] >= 1

    def test_it_is_csrf_exempt_with_protection_actually_on(self, loaded,
                                                           monkeypatch):
        """The conftest disables CSRF, so an endpoint that needs an
        exemption passes every other test in this file and 400s in
        production. Flipped back on here deliberately.
        """
        monkeypatch.setenv("BACKUP_TOKEN", "t0ken")
        monkeypatch.setitem(flask_app.config, "WTF_CSRF_ENABLED", True)
        client = flask_app.test_client()

        response = client.post("/api/backup/run",
                               headers={"X-TFG-Token": "t0ken"})

        assert response.status_code != 400, (
            "CSRFProtect rejected the scheduled job; the exemption is not "
            "in effect")

    def test_the_basic_gate_lets_it_through(self):
        """It is a machine caller. The gate maintained its own allowlist
        once and every token endpoint 401'd from the perimeter before its
        own token was looked at (E1.8)."""
        from engine import route_policy
        assert "api_backup_run" in route_policy.MACHINE
        assert "api_backup_run" in route_policy.OPEN


class TestTheWorkflowExists:

    def test_there_is_a_scheduled_workflow_and_it_no_ops_without_a_url(self):
        import pathlib
        path = (pathlib.Path(__file__).resolve().parent.parent
                / ".github" / "workflows" / "backup.yml")
        assert path.is_file(), "nothing schedules the scheduled backup"
        text = path.read_text(encoding="utf-8")
        assert "schedule:" in text and "cron:" in text
        assert "BACKUP_URL" in text and "BACKUP_TOKEN" in text
        assert "exit 0" in text, (
            "a fork with no deployment should see a skipped job, not a red "
            "one — the argument keepalive.yml already makes")
