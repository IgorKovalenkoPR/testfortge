"""E4.5a — attaching evidence to a bug by hand, and where it goes.

The acceptance criterion is one sentence and it is the whole point: **a file
attached to a bug is visible on the bug page from a different session** —
not only in the browser that uploaded it. That is
``TestTheFileSurvivesTheSession``, and everything else here exists because
of what measuring it turned up.

Two mechanisms called almost the same thing
-------------------------------------------
The bug row carries both, and the names differ by one letter:

* ``bug_report.attachment`` — one ``VARCHAR(500)``. It is the evidence
  **link** the team's own bug spreadsheet puts on every row, imported and
  exported as text;
* ``attachments`` — a **list of storage keys** in the ``extra`` JSON,
  rendered as a gallery through ``automation_asset``.

An upload is the second kind. ``TestTheTwoAttachmentFieldsStayApart`` pins
that they are separate on purpose, because "attachment vs attachments" is
otherwise a coin toss for whoever reads this next, and merging them would
either break the spreadsheet round trip or flatten a gallery into one slot.

The defect this found
---------------------
``routes/chat.py`` already uploaded a file when filing a bug from the chat
widget. It wrote to ``UPLOAD_FOLDER/chat_bug_attachments/`` while the page
renders attachments out of ``STORAGE_ROOT`` — two different directories.
Measured before the fix: the upload succeeded, the row recorded the name,
and the gallery got a **404**. A broken image where the evidence should be,
and nothing reported it, because the save and the read are different
requests. ``TestTheChatUploadIsActuallyServable`` is that regression.

Storage: local disk, deliberately
---------------------------------
E8.2 (the storage abstraction) is blocked on ADR 0002, which is Proposed and
waiting on the owner. The prompt for E4.5a offered the choice — wait, or
limit to local disk and say so in a test — and this is the second branch.
``TestTheLocalDiskLimitation`` states it: on the free plan that disk is
ephemeral, so a restart takes the file. Written as a test rather than a
comment so that the day it stops being true, something goes red.
"""
from __future__ import annotations

import io
import os
import re
import secrets

import pytest

from app import app as flask_app
from engine import blobs as _blobs
from engine import db as _db
from engine import workspace as _workspace
from engine.automation_paths import STORAGE_ROOT

PNG = b"\x89PNG\r\n\x1a\n" + b"pretend-image-bytes" * 8


@pytest.fixture(autouse=True)
def _ready(monkeypatch):
    monkeypatch.setitem(flask_app.config, "TESTING", True)
    monkeypatch.setitem(flask_app.config, "WTF_CSRF_ENABLED", False)
    _db.init_db()
    return True


@pytest.fixture
def project():
    return _db.upsert_project(name=f"Evidence {secrets.token_hex(4)}")


@pytest.fixture
def bug(project):
    """One bug in the database, in the shape the *page* sees.

    Mapped through ``workspace.bug_row_to_dict`` rather than used raw:
    ``list_bugs`` returns the row, where ``id`` is the integer primary key
    and ``external_id`` is ``BUG-001``. The template and the route both
    speak the mapped shape, where ``db_id`` is the integer and ``id`` is the
    public one — and mixing the two is how a test addresses the wrong bug.
    """
    _db.save_bug(project, {
        "id": "BUG-001", "title": "The total is wrong on checkout",
        "severity": "Major", "priority": "High", "status": "Open",
        "steps_to_reproduce": "1. Add two items", "actual_result": "3.00",
        "expected_result": "2.00",
    }, source="manual")
    rows = _db.list_bugs(project)
    assert rows, "the fixture created no bug"
    return _workspace.bug_row_to_dict(rows[0])


#: Injected by the ``_signs_in`` fixture below.
_SIGN_IN = []


def _client(project_id: str):
    """A browser with this project active. A fresh cookie jar each call —
    the acceptance criterion is about a *different* session.

    Signed in through conftest's ``sign_in`` when the flags are on. This
    file builds its own clients rather than using the shared ``client``
    fixture (it needs several at once), and a client that skips the sign-in
    gets redirected to /auth/login by the route policy — so every assertion
    below would measure the login page. Caught by running the suite the way
    the product ships.
    """
    client = flask_app.test_client()
    if _SIGN_IN:
        _SIGN_IN[0](client)
    with client.session_transaction() as sess:
        sess["project_id"] = project_id
    return client


@pytest.fixture(autouse=True)
def _signs_in(sign_in):
    """Hand conftest's sign-in helper to :func:`_client`.

    A no-op with the flags off, which is what makes it safe to apply to
    every test here rather than to the handful that would notice.
    """
    _SIGN_IN[:] = [sign_in]
    yield
    _SIGN_IN.clear()


def _upload(client, db_id: int, name: str = "evidence.png",
            content: bytes = PNG):
    return client.post(
        f"/bug-reports/{db_id}/attach",
        data={"attachment": (io.BytesIO(content), name)},
        content_type="multipart/form-data", follow_redirects=True)


def _markup_only(html: str) -> str:
    """*html* with comments and raw-text elements removed.

    ``<script>`` and ``<style>`` contents are CDATA to an HTML parser: a
    ``<form>`` written inside them is text, not an element. Any structural
    check that counts tags has to agree with the parser or it invents
    findings.
    """
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    html = re.sub(r"<script\b.*?</script\s*>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style\b.*?</style\s*>", "", html, flags=re.S | re.I)
    return html


def _bug_row(project_id: str, db_id: int) -> dict:
    """The mapped row for one bug, straight from the database."""
    for row in _db.list_bugs(project_id):
        if row.get("id") == db_id:
            return _workspace.bug_row_to_dict(row)
    return {}


def _attachments(project_id: str, db_id: int) -> list[str]:
    return list(_bug_row(project_id, db_id).get("attachments") or [])


# ── The acceptance criterion ─────────────────────────────────────────

class TestTheFileSurvivesTheSession:
    """"Visible on the bug page after coming back in another session."

    Stated that way in the prompt for a reason: an upload that only works
    in the tab that performed it is the session-as-source-of-truth defect
    ADR 0001 exists to end, reappearing in a new place.
    """

    def test_a_second_session_sees_the_attachment_on_the_page(self, project,
                                                              bug):
        uploader = _client(project)
        assert _upload(uploader, bug["db_id"]).status_code == 200

        # A different browser entirely — new cookie jar, same project.
        visitor = _client(project)
        page = visitor.get("/bug-reports").get_data(as_text=True)

        keys = _attachments(project, bug["db_id"])
        assert len(keys) == 1, keys
        assert keys[0] in page, (
            "the attachment is in the database but not on the page the "
            "second session gets")

    def test_the_second_session_can_actually_fetch_the_bytes(self, project,
                                                             bug):
        """Being named on the page is not the same as being downloadable —
        which is exactly how the chat upload was broken for months."""
        _upload(_client(project), bug["db_id"])
        key = _attachments(project, bug["db_id"])[0]

        response = _client(project).get(f"/automation/asset/{key}")

        assert response.status_code == 200, (
            f"{key} rendered on the page and 404s when fetched")
        assert response.data == PNG

    def test_it_is_in_the_database_not_the_session(self, project, bug):
        _upload(_client(project), bug["db_id"])
        assert _attachments(project, bug["db_id"]), (
            "nothing reached the row, so the file lives only in whichever "
            "session uploaded it")


# ── The two fields ───────────────────────────────────────────────────

class TestTheTwoAttachmentFieldsStayApart:
    """``attachment`` (a link, from the team's spreadsheet) and
    ``attachments`` (stored keys, a gallery) are different things."""

    def test_an_upload_touches_the_list_and_not_the_column(self, project,
                                                           bug):
        _db.save_bug(project, {
            "id": "BUG-002", "title": "Has a spreadsheet link",
            "attachment": "https://intranet.example/evidence/42",
        }, source="manual")
        row = next(_workspace.bug_row_to_dict(r) for r in _db.list_bugs(project)
                   if r.get("external_id") == "BUG-002")

        _upload(_client(project), row["db_id"])

        after = next(_workspace.bug_row_to_dict(r)
                     for r in _db.list_bugs(project)
                     if r.get("external_id") == "BUG-002")
        assert after["attachment"] == "https://intranet.example/evidence/42", (
            "the upload overwrote the spreadsheet's evidence link")
        assert len(after.get("attachments") or []) == 1

    def test_the_column_is_a_link_and_the_list_is_keys(self, project, bug):
        """Named so the distinction is findable. The column is free text
        that round-trips through import/export; the list holds keys that
        ``automation_asset`` can resolve."""
        _upload(_client(project), bug["db_id"])
        key = _attachments(project, bug["db_id"])[0]

        assert key.startswith("project/"), key
        assert _blobs.exists(key)
        # And the column is untouched by any of it.
        assert not _bug_row(project, bug["db_id"]).get("attachment")


# ── The regression this file found ───────────────────────────────────

class TestTheChatUploadIsActuallyServable:
    """A bug filed from the chat widget with a screenshot.

    Before E4.5a the file was written under ``UPLOAD_FOLDER`` and the page
    served from ``STORAGE_ROOT``, so the gallery got a 404 — measured, not
    inferred. Both halves are asserted here, because "the file exists" and
    "the page can show it" were separately true and jointly false.
    """

    def _file_a_chat_bug(self, project_id: str):
        client = _client(project_id)
        response = client.post("/chat/bug-form", data={
            "summary": "Filed from the chat with evidence",
            "environment": "Win/Chrome",
            "steps_to_reproduce": "1. Open checkout",
            "actual_result": "It shows 3.00",
            "expected_result": "It should show 2.00",
            "attachment": (io.BytesIO(PNG), "from-chat.png"),
        }, content_type="multipart/form-data")
        assert response.status_code == 200, response.get_data(as_text=True)
        return client

    def test_the_attachment_it_records_can_be_fetched(self, project):
        client = self._file_a_chat_bug(project)

        filed = [_workspace.bug_row_to_dict(r) for r in _db.list_bugs(project)]
        filed = [r for r in filed if r.get("attachments")]
        assert filed, "the chat bug recorded no attachment"
        key = filed[0]["attachments"][0]

        assert client.get(f"/automation/asset/{key}").status_code == 200, (
            f"{key} was stored somewhere the bug page cannot serve from — "
            f"the gallery shows a broken image")

    def test_the_bug_is_still_filed_when_the_attachment_cannot_be_stored(
            self, project, monkeypatch):
        """Unlike the bug page's own upload, the chat form must not lose the
        report to save the screenshot: the file is one optional field on a
        form whose point is the bug."""
        def _boom(*a, **k):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(_blobs, "save", _boom)
        before = len(_db.list_bugs(project))

        self._file_a_chat_bug(project)

        assert len(_db.list_bugs(project)) == before + 1


# ── Refusals ─────────────────────────────────────────────────────────

class TestRefusals:
    """ADR 0002 §4.6: a person who chose a file is standing here, so a file
    we cannot store is an error, never a quiet success."""

    def test_a_disallowed_type_is_refused_and_nothing_is_recorded(self,
                                                                 project,
                                                                 bug):
        response = _upload(_client(project), bug["db_id"], name="payload.exe",
                           content=b"MZ\x90\x00")

        assert "cannot be attached" in response.get_data(as_text=True)
        assert _attachments(project, bug["db_id"]) == []

    def test_an_empty_file_is_refused_rather_than_shown_as_evidence(
            self, project, bug):
        """A blank image that renders as nothing is worse than a refusal,
        because it looks like evidence somebody reviewed."""
        response = _upload(_client(project), bug["db_id"], content=b"")

        assert "empty" in response.get_data(as_text=True).lower()
        assert _attachments(project, bug["db_id"]) == []

    def test_a_bug_from_another_project_is_not_addressable(self, project,
                                                           bug):
        other = _db.upsert_project(name=f"Neighbour {secrets.token_hex(4)}")
        client = _client(other)

        response = _upload(client, bug["db_id"])

        assert _attachments(project, bug["db_id"]) == [], (
            "a bug id from another project accepted an attachment")
        assert "not in this project" in response.get_data(as_text=True)

    def test_a_refused_write_leaves_no_orphan_on_disk(self, project, bug):
        """The file is saved before the row is updated, so a bug that turns
        out not to be addressable must have its bytes taken back out —
        otherwise retention has to guess what they belonged to."""
        other = _db.upsert_project(name=f"Orphans {secrets.token_hex(4)}")

        _upload(_client(other), bug["db_id"])

        stray = os.path.join(STORAGE_ROOT, "project", other, "bug",
                             str(bug["db_id"]))
        assert not (os.path.isdir(stray) and os.listdir(stray)), (
            "the refused upload left bytes on disk with nothing pointing "
            "at them")

    def test_no_file_chosen_is_a_message_not_a_crash(self, project, bug):
        response = _client(project).post(
            f"/bug-reports/{bug['db_id']}/attach", data={},
            follow_redirects=True)
        assert response.status_code == 200
        assert "Choose a file" in response.get_data(as_text=True)


# ── The key scheme ───────────────────────────────────────────────────

class TestTheKey:
    """Project-scoped, per ADR 0002 §4.2 — minus the org segment, which is
    E8.2's to add."""

    def test_the_same_filename_in_two_projects_does_not_collide(self):
        """The defect this scheme removes rather than fixes:
        ``routes/estimation.py`` saves under a bare ``secure_filename``, so
        ``requirements.docx`` from two projects is one file."""
        first = _blobs.key_for("proj-aaa", "bug", "7", "shot.png")
        second = _blobs.key_for("proj-bbb", "bug", "7", "shot.png")
        assert first != second
        assert first.startswith("project/proj-aaa/bug/7/")
        assert second.startswith("project/proj-bbb/bug/7/")

    def test_the_same_file_twice_keeps_both(self, project, bug):
        """Attaching ``before.png`` and then a corrected ``before.png`` is
        two pieces of evidence, not an overwrite."""
        client = _client(project)
        _upload(client, bug["db_id"])
        _upload(client, bug["db_id"])

        keys = _attachments(project, bug["db_id"])
        assert len(keys) == 2 and keys[0] != keys[1]
        assert all(_blobs.exists(k) for k in keys)

    def test_a_traversing_name_cannot_escape_the_root(self):
        for hostile in ("../../etc/passwd.png", "..\\..\\win.png",
                        "/absolute/path.png"):
            key = _blobs.key_for("p", "bug", "1", hostile)
            resolved = os.path.realpath(_blobs.absolute(key))
            assert resolved.startswith(os.path.realpath(STORAGE_ROOT))

    def test_an_unknown_kind_is_refused(self):
        """``kind`` is part of the key, so it is a closed set: a typo would
        create a sibling tree that nothing lists and nothing deletes."""
        with pytest.raises(_blobs.UploadRefused):
            _blobs.key_for("p", "screenshots", "1", "a.png")

    def test_no_project_is_refused(self):
        with pytest.raises(_blobs.UploadRefused):
            _blobs.key_for("", "bug", "1", "a.png")

    def test_a_key_that_escapes_the_root_is_refused(self):
        with pytest.raises(_blobs.UploadRefused):
            _blobs.absolute("../../outside.png")

    def test_a_nameless_upload_is_refused(self):
        class _Nameless:
            filename = ""

        with pytest.raises(_blobs.UploadRefused):
            _blobs.save(_Nameless(), project_id="p", kind="bug",
                        entity_id="1")

    def test_exists_says_no_for_an_impossible_key(self):
        assert _blobs.exists("../../etc/passwd") is False

    def test_deleting_a_prefix_that_is_not_there_is_zero(self):
        assert _blobs.delete_prefix("project/never-existed") == 0

    def test_the_key_is_servable_by_the_asset_route(self):
        """A key the asset route's regex rejects is not a key — the file
        would be stored and then 400 on every view."""
        from routes._shared import SAFE_ASSET_RE
        key = _blobs.key_for("proj-1", "bug", "42", "a nasty name!.png")
        assert SAFE_ASSET_RE.fullmatch(key), key

    def test_deleting_a_prefix_removes_a_project_s_evidence(self, project,
                                                            bug):
        """The claim that makes a blob-index table unnecessary for E8.5.
        Asserted rather than assumed, because a scheme whose central
        property is never exercised is one nobody can rely on."""
        _upload(_client(project), bug["db_id"])
        key = _attachments(project, bug["db_id"])[0]
        assert _blobs.exists(key)

        removed = _blobs.delete_prefix(f"project/{project}")

        assert removed == 1
        assert not _blobs.exists(key)


# ── The limitation, stated on purpose ────────────────────────────────

class TestTheLocalDiskLimitation:
    """E4.5a ships on local disk **deliberately**, because E8.2 is blocked
    on ADR 0002 being approved.

    These tests exist so the limitation is a recorded decision rather than
    something a reader has to infer — and so that when E8.2 lands, the
    things that stop being true fail here first.
    """

    def test_the_bytes_are_on_this_machine_s_disk(self, project, bug):
        _upload(_client(project), bug["db_id"])
        key = _attachments(project, bug["db_id"])[0]

        on_disk = os.path.join(STORAGE_ROOT, *key.split("/"))
        assert os.path.isfile(on_disk), (
            "E4.5a stores locally by design; if this now fails because the "
            "bytes went to object storage, E8.2 has landed and this class "
            "is what needs rewriting")

    def test_losing_the_disk_loses_the_file_and_the_row_still_points_at_it(
            self, project, bug):
        """What the ephemeral free-tier disk actually costs, spelled out.

        A restart on Render free takes the directory with it. The row keeps
        the key, so the page keeps rendering a gallery entry — and the
        entry 404s. That is the state E8.2 exists to end, and pretending
        otherwise here would be the same "assumption recorded as a fact"
        this programme has now met four times.
        """
        _upload(_client(project), bug["db_id"])
        key = _attachments(project, bug["db_id"])[0]

        # Simulate the restart: the disk goes, the database does not.
        _blobs.delete_prefix(f"project/{project}")

        assert _attachments(project, bug["db_id"]) == [key], (
            "the row should still name the attachment — that is the point")
        assert _client(project).get(
            f"/automation/asset/{key}").status_code == 404

    def test_there_is_no_storage_backend_choice_yet(self):
        """``STORAGE_BACKEND_CONFIGURABLE`` stays off until E8.2/E8.7, or
        the Admin UI offers a choice with nothing behind it."""
        from engine import features
        assert features.is_enabled("STORAGE_BACKEND_CONFIGURABLE") is False


# ── The rendered page, not the template source ───────────────────────

class TestTheUploadFormIsRealHtml:
    """The defect a grep-based test could not see.

    Every bug card on this page sits inside one big
    ``<form action="/bugs/bulk">``. The first version of E4.5a put a second
    ``<form>`` in the card — which is invalid HTML: the parser drops the
    inner one, its controls are adopted by the outer one, and the Attach
    button posts the file to ``/bugs/bulk``.

    The template read correctly and the server-side assertion — the string
    ``/attach`` appears in the body — passed. **A real browser is what showed
    the form was not in the DOM at all.** So the invariant is now checked
    structurally here, on the rendered page, rather than trusted to a
    substring.
    """

    @staticmethod
    def _open_forms_before(html: str, needle: str) -> int:
        """How many ``<form>`` elements are still open where *needle* starts.

        Three things had to be got right, and each was wrong once while this
        was being written — which is why the helper is longer than the test:

        * **comments** are not markup. ``_project_picker.html`` mentions
          ``<form action>`` in one;
        * **script bodies** are not markup either. The same file writes
          ``<form action>`` again inside an inline script, explaining why
          the picker sets its action in JS. That one read as a genuinely
          unclosed ``<form>`` on the page;
        * the document is cleaned **before** the position is located. The
          first version sliced and then cleaned, so a prefix that cut
          through a ``<script>`` left it unterminated and its body counted
          as markup again.
        """
        cleaned = _markup_only(html)
        assert needle in cleaned, f"{needle} is not in the markup"
        head = cleaned[:cleaned.index(needle)]
        depth = 0
        for match in re.finditer(r"<form\b[^>]*>|</form\s*>", head):
            depth += -1 if match.group(0).startswith("</") else 1
        return depth

    def test_no_upload_form_is_nested_inside_another_form(self, project, bug):
        html = _client(project).get("/bug-reports").get_data(as_text=True)
        needle = f'id="bug-attach-{bug["db_id"]}"'
        assert needle in html, "the upload form is not on the page"

        assert self._open_forms_before(html, needle) == 0, (
            "the upload form is inside another form, so the browser will "
            "drop it and the Attach button will post to /bugs/bulk")

    def test_the_controls_are_tied_to_it_by_the_form_attribute(self, project,
                                                               bug):
        """The controls stay in the card for layout; the association is what
        makes them submit to the right place."""
        html = _client(project).get("/bug-reports").get_data(as_text=True)
        card = html.split('id="bug-attach-', 1)[0]

        assert f'form="bug-attach-{bug["db_id"]}"' in card, (
            "the file input is not associated with the upload form, so it "
            "submits with the bulk toolbar instead")
        assert 'type="file"' in card

    def test_the_bulk_form_is_still_one_form(self, project, bug):
        """The other half: fixing the nesting must not have broken the
        toolbar by closing its form early."""
        html = _client(project).get("/bug-reports").get_data(as_text=True)
        cleaned = _markup_only(html)
        assert cleaned.count('id="bug-bulk-form"') == 1
        opens = len(re.findall(r"<form\b", cleaned))
        closes = len(re.findall(r"</form\s*>", cleaned))
        assert opens == closes, (opens, closes)


# ── The harness ──────────────────────────────────────────────────────

class TestTheHarnessWouldNotice:
    def test_each_client_is_a_separate_session(self, project):
        a, b = _client(project), _client(project)
        with a.session_transaction() as sess:
            sess["_marker"] = "only-in-a"
        with b.session_transaction() as sess:
            assert sess.get("_marker") is None, (
                "the two clients share a cookie jar, so every "
                "'different session' assertion above is meaningless")

    def test_the_bug_fixture_has_a_row_id_to_address(self, bug):
        assert bug.get("db_id"), (
            "without db_id the upload form is not rendered and the route "
            "has nothing to key on")

    def test_the_form_is_rendered_for_a_bug_that_has_one(self, project, bug):
        page = _client(project).get("/bug-reports").get_data(as_text=True)
        assert f"/bug-reports/{bug['db_id']}/attach" in page

    def test_the_upload_actually_writes_bytes(self, project, bug):
        _upload(_client(project), bug["db_id"])
        key = _attachments(project, bug["db_id"])[0]
        assert os.path.getsize(_blobs.absolute(key)) == len(PNG)


@pytest.fixture(autouse=True)
def _sweep_evidence():
    """Take this file's blobs back off the developer's disk.

    ``STORAGE_ROOT`` is the working checkout, not a tmp_path, because the
    asset route serves from there and the point of these tests is that the
    route can serve them. Cleaning up afterwards is the price.
    """
    yield
    root = os.path.join(STORAGE_ROOT, "project")
    if not os.path.isdir(root):
        return
    for name in os.listdir(root):
        try:
            _blobs.delete_prefix(f"project/{name}")
        except Exception:      # pragma: no cover — best effort
            pass
