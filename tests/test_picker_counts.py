"""The project picker said "1 bugs" — in the header of every module page.

Found by walking the dashboard and reading the same number twice on one
screen: the picker said ``Acme Web Shop (0 TC · 0 CL · 1 bugs)`` while the
recent-projects strip four lines away in the same template said ``1 bug``.
The strip goes through ``engine.i18n.plural``; the picker hardcoded the
plural noun inside its ``pp_counts`` string.

Ukrainian is where it mattered. The hardcoded UA form was ``багів``, the
*many* form, so every page header read ``1 багів`` — and the language needs
three forms, which ``dm_unit_bugs`` already declares and the picker was not
asking for. The facility existed, was used a few lines away, and this one
call site did not use it: the same asymmetry that found the timestamps, the
export filenames and the automation listing this week.

``pp_counts`` is now identical in both dictionaries, which the parity gate
flags by default and correctly: nothing translatable is left in it. It is in
that gate's ``DELIBERATELY_ENGLISH`` allowlist with the reason, which is what
the allowlist is for.
"""
from __future__ import annotations

import pytest

from engine import db as _db
from engine.i18n import plural


class TestTheNounAgreesWithTheNumber:

    @pytest.mark.parametrize("count,word", [
        (0, "bugs"), (1, "bug"), (2, "bugs"), (5, "bugs"), (21, "bugs"),
    ])
    def test_english(self, count, word):
        # The dictionary *key*, not the raw forms — that is the call the
        # product makes, and a test that bypassed the lookup would pass
        # with the key missing from a dictionary entirely.
        assert plural("en", count, "dm_unit_bugs") == word

    @pytest.mark.parametrize("count,word", [
        (1, "баг"), (2, "баги"), (4, "баги"), (5, "багів"), (11, "багів"),
        (21, "баг"), (22, "баги"), (0, "багів"),
    ])
    def test_ukrainian(self, count, word):
        """The cases that catch people out are 11–14: they take the *many*
        form despite ending in 1–4. Included because a two-form
        implementation passes every other row in this table."""
        assert plural("ua", count, "dm_unit_bugs") == word


class TestThePickerUsesIt:

    @pytest.fixture
    def project_with_bugs(self, client):
        def _make(count, name):
            # Created through the product's own route rather than
            # ``upsert_project``: with authentication off the picker lists
            # ``list_projects(owner_sid=…)``, and a project made without an
            # owner is not in it at all — so a fixture-made project renders
            # no counts and the assertion below would fail for a reason that
            # has nothing to do with plurals.
            client.post("/projects/db/create", data={"project_name": name},
                        follow_redirects=True)
            with client.session_transaction() as sess:
                pid = sess.get("project_id")
            assert pid, "the project was not created"
            for index in range(count):
                _db.save_bug(pid, {"bug_id": f"BUG-{index + 1:03d}",
                                   "title": f"Defect {index + 1}",
                                   "severity": "Minor", "priority": "Low",
                                   "status": "Open"})
            return pid
        return _make

    @pytest.mark.parametrize("count,expected", [(1, "1 bug"), (2, "2 bugs"),
                                                (5, "5 bugs")])
    def test_english_reads_correctly(self, client, project_with_bugs,
                                     count, expected):
        project_with_bugs(count, f"Picker EN {count}")
        body = client.get("/?lang=en").get_data(as_text=True)
        assert expected in body, expected
        if count == 1:
            assert "1 bugs" not in body

    @pytest.mark.parametrize("count,expected", [(1, "1 баг"), (2, "2 баги"),
                                                (5, "5 багів")])
    def test_ukrainian_reads_correctly(self, client, project_with_bugs,
                                       count, expected):
        project_with_bugs(count, f"Picker UA {count}")
        body = client.get("/?lang=ua").get_data(as_text=True)
        assert expected in body, expected

    def test_the_two_renderings_on_one_screen_agree(self, client,
                                                    project_with_bugs):
        """The defect as it was seen: one number, one screen, two answers.
        The picker and the recent-projects strip both name the bug count, and
        a reader looking at both should not have to wonder which is right.
        """
        project_with_bugs(1, "Picker agreement")
        body = client.get("/?lang=en").get_data(as_text=True)
        assert "1 bugs" not in body, "the picker is plural again"
        assert body.count("1 bug") >= 2, (
            "expected the count in both the picker and the strip")


class TestTheStringItself:

    def test_both_dictionaries_take_the_word_as_a_placeholder(self):
        from engine.i18n import TRANSLATIONS
        for lang in ("en", "ua"):
            value = TRANSLATIONS[lang]["pp_counts"]
            assert "%(bugs_word)s" in value, (lang, value)
            # And the plural noun is gone from the literal — leaving it
            # there would render "1 bug bugs". Matched with the leading
            # space: ``%(bugs)d`` is the *number's* placeholder and is
            # legitimately called that, which a bare "bugs)" also matched.
            assert " bugs)" not in value, (lang, value)
            assert " багів)" not in value, (lang, value)

    def test_the_template_passes_the_pluralised_word(self):
        import pathlib
        source = pathlib.Path("templates/_project_picker.html").read_text(
            encoding="utf-8")
        assert "bugs_word=plural(" in source
        assert "dm_unit_bugs" in source
