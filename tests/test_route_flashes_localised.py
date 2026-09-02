"""Twenty-seven flash messages rendered English in every language.

Every ``flash()`` in ``routes/`` reads its text as
``g.t.get("key", "English fallback")``, and twenty-seven of those keys were
in neither dictionary — so a Ukrainian operator met English at every one.
Measured with ``lang="ua"`` before the fix:

    POST /bugs/bulk (as a member)  "Deleting bug reports is limited to
                                    admins. Close them instead…"
    POST /test-cases/upload        "No file selected."

This is the shape ``engine/i18n/en.py`` says M-2 swept out of
``templates/`` — "referenced as ``t.get('key', 'English')`` and existed in
neither dictionary, so they rendered English in both languages" — one
directory over and never followed up. The parity gate could not see it
either: ``TestEveryKeyATemplateAsksForExists`` walks
``templates/*.html`` and nothing else.

Three of the twenty-seven were worse than untranslated.

* Five had an **f-string** fallback, which already carried the numbers and
  filenames. Adding the key to a dictionary would have kept the words and
  dropped the figures — a message whose entire content is its figures.
  They use ``%(name)s`` now.
* Three fragments were appended as English f-strings **outside** the
  ``t.get`` (" Total now: N.", " Skipped N already in this project (…)"),
  so they stayed English whatever the dictionary held. They are keys.
* Two counted messages built their plural as ``"s" if n != 1`` — two forms
  for a language that has three. Same defect the project picker had ("1
  bugs" in every language) and the same facility fixes it.

And one key was the fallback for four different sentences
(``bug_bulk_no_project``: "before bulk editing", "before resetting", and
"Project not found." twice, in two routes). Adding it to the dictionary was
never a one-line change: three of the four call sites would have started
saying something else. ``bug_attach_no_project`` was doing the same for two.
"""
from __future__ import annotations

import secrets

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import permissions as _perm
from engine import session_timeout as _timeout
from engine.i18n import TRANSLATIONS

EN = TRANSLATIONS["en"]
UA = TRANSLATIONS["ua"]


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    _db.init_db()


def _client(app, role="user", lang="ua"):
    org = _db.create_organization(f"Org {secrets.token_hex(3)}")
    uid = _db.create_user(
        f"u-{secrets.token_hex(4)}@example.test",
        password_hash=_auth.hash_password("a perfectly good passphrase"))
    _db.add_org_member(org, uid, role)
    pid = _db.upsert_project(name=f"P {secrets.token_hex(3)}", org_id=org)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess[_perm.SESSION_USER_KEY] = uid
        sess[_perm.SESSION_ORG_KEY] = org
        sess["project_id"] = pid
        sess["lang"] = lang
        _timeout.stamp(sess)
    return c, pid


def _flashes(client):
    with client.session_transaction() as sess:
        out = [str(m[1]) for m in sess.get("_flashes", [])]
        sess["_flashes"] = []
    return out


class TestAUkrainianOperatorGetsUkrainian:

    def test_the_admin_only_refusal(self, app):
        client, pid = _client(app, "user", "ua")
        _db.save_bug(pid, {"id": "BUG-001", "title": "x",
                           "severity": "Minor", "priority": "High",
                           "status": "Open"})
        client.post("/bugs/bulk", data={"action": "delete",
                                        "bug_ids": ["1"]})
        said = " ".join(_flashes(client))
        assert said, "no flash at all"
        assert EN["bug_bulk_delete_admin"] not in said
        assert "адмін" in said, said

    def test_the_upload_refusal(self, app):
        client, _ = _client(app, "user", "ua")
        client.post("/test-cases/upload", data={})
        said = " ".join(_flashes(client))
        assert EN["upload_no_file"] not in said
        assert said.strip() == UA["upload_no_file"], said

    def test_english_is_unchanged(self, app):
        """The control. Every English value is its old fallback verbatim,
        so an English reader sees exactly what they saw before — a fix
        that translated by rewriting the English would pass everything
        above."""
        client, _ = _client(app, "user", "en")
        client.post("/test-cases/upload", data={})
        assert " ".join(_flashes(client)).strip() == "No file selected."


class TestTheCountedMessagesAgreeWithTheirNumber:
    """Ukrainian has three forms and 11–14 take the *many* one despite
    ending in 1–4, which is the case a two-form hack cannot express."""

    def _update(self, app, n, lang):
        client, pid = _client(app, "admin", lang)
        for i in range(n):
            _db.save_bug(pid, {"id": f"BUG-{i + 1:03d}", "title": "x",
                               "severity": "Minor", "priority": "High",
                               "status": "Open"})
        ids = [str(row["id"]) for row in _db.list_bugs(pid)[:n]]
        client.post("/bugs/bulk", data={"action": "status",
                                        "status_value": "Resolved",
                                        "bug_ids": ids})
        return " ".join(_flashes(client))

    @pytest.mark.parametrize("n,expected", [(1, "баг"), (2, "баги"),
                                            (5, "багів")])
    def test_ukrainian_picks_the_right_form(self, app, n, expected):
        said = self._update(app, n, "ua")
        assert f"{n} {expected}." in said, said

    @pytest.mark.parametrize("n,expected", [(1, "bug"), (2, "bugs")])
    def test_english_still_reads_as_it_did(self, app, n, expected):
        assert f"Updated {n} {expected}." in self._update(app, n, "en")

    def test_the_forms_are_declared_for_both_languages(self):
        assert EN["bug_word"] == "bug|bugs"
        assert len(UA["bug_word"].split("|")) == 3, UA["bug_word"]


class TestTheInterpolatedOnesKeptTheirFigures:
    """The five f-string fallbacks. A message that says "Imported test
    case(s) from ." has lost the only thing it was for."""

    def test_the_upload_report_carries_its_count_and_filename(self, app):
        client, _ = _client(app, "admin", "ua")
        csv = (b"ID,Summary,Test Steps,Expected Result\n"
               b"TC-001,A case,1. Do it,It works\n")
        from io import BytesIO
        client.post("/test-cases/upload",
                    data={"upload_file": (BytesIO(csv), "pack.csv"),
                          "upload_mode": "replace"},
                    content_type="multipart/form-data")
        said = " ".join(_flashes(client))
        assert "1" in said and "pack.csv" in said, said

    def test_the_bad_extension_names_the_extension(self, app):
        client, _ = _client(app, "admin", "ua")
        from io import BytesIO
        client.post("/test-cases/upload",
                    data={"upload_file": (BytesIO(b"x"), "notes.docx")},
                    content_type="multipart/form-data")
        said = " ".join(_flashes(client))
        assert "docx" in said, said
        assert UA["upload_bad_ext"].split("%")[0] in said, said

    @pytest.mark.parametrize("key,names", [
        ("te_auto_run_done", ("%(passed)d", "%(failed)d", "%(blocked)d")),
        ("te_auto_run_failed", ("%(error)s",)),
        ("upload_bad_ext", ("%(ext)s",)),
        ("upload_tc_ok", ("%(n)d", "%(file)s")),
        ("upload_cl_ok", ("%(n)d", "%(file)s")),
        ("upload_total_now", ("%(n)d",)),
        ("upload_skipped", ("%(n)d", "%(ids)s")),
        ("tc_walkthrough_meta_saved", ("%(tc)s",)),
        ("crawl_partial", ("%(errors)s",)),
    ])
    def test_both_languages_carry_the_placeholders(self, key, names):
        """Rule 4 of the parity gate checks placeholders agree between the
        two languages. This checks they are *there* — a translation that
        dropped ``%(file)s`` would agree with an English value that had
        also lost it."""
        for lang, table in (("en", EN), ("ua", UA)):
            for name in names:
                assert name in table[key], (lang, key, name)
