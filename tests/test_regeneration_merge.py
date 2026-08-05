"""A regeneration keeps manual edits — E4.7.

The behaviour this replaces, measured before the module existed:

    save_test_cases(pack); editable.patch(summary); save_test_cases(pack)
    → the summary is the generated text again, and the metadata still says
      row_version=2, ai_generated=False

Both halves mattered: the edit was gone, *and* the row still claimed to be
human-edited — so the "edited" pill and this very guard would both have
trusted a row that no longer held the edit.
"""
from __future__ import annotations

import pytest

from engine import db, editable, regeneration


def _tc(index, summary="Generated", **overrides):
    row = {"id": f"SC1_{index:03d}", "section": "Auth", "section_num": 1,
           "summary": summary, "preconditions": "", "test_steps": "1. Open",
           "test_data": "", "expected_result": "It works", "issues": "",
           "comment": "", "user_story_id": "", "category": "Positive",
           "priority": "High", "status": "Unchecked",
           "testing_type": "Functional"}
    row.update(overrides)
    return row


def _cl(index, objective="Generated"):
    return {"id": f"HDR_{index:03d}", "section": "Header",
            "item_num": f"1.{index}", "depth": 2, "objective": objective,
            "comments": "", "user_story_id": "", "category": "Positive",
            "priority": "High", "status": "Unchecked",
            "testing_type": "Functional"}


@pytest.fixture
def editing_on(monkeypatch):
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("EDITORS_ENABLED", "1")
    return True


@pytest.fixture
def project(app, request):
    return db.upsert_project(name=f"E4.7 {request.node.name}"[:180])


# ── The pure policy ───────────────────────────────────────────────

class TestMerge:

    @staticmethod
    def _meta(*edited_ids):
        return {item_id: {"ai_generated": False} for item_id in edited_ids}

    def test_an_edited_row_is_kept_whole(self):
        existing = [_tc(1, "A human wrote this")]
        incoming = [_tc(1, "The generator wrote this")]
        rows, report = regeneration.merge(existing, incoming,
                                         self._meta("SC1_001"))
        assert rows[0]["summary"] == "A human wrote this"
        assert report.kept == ["SC1_001"]
        assert report.generated == 0

    def test_no_field_is_taken_from_the_incoming_version(self):
        """Field-level merging needs per-field provenance, and there is only a
        row-level flag. Guessing which fields a person meant to own — and
        guessing wrong silently — is the failure this exists to stop."""
        existing = [_tc(1, "Mine", expected_result="My expectation")]
        incoming = [_tc(1, "Theirs", expected_result="Their expectation")]
        rows, _ = regeneration.merge(existing, incoming, self._meta("SC1_001"))
        assert rows[0]["expected_result"] == "My expectation"

    def test_an_untouched_row_is_regenerated(self):
        rows, report = regeneration.merge([_tc(1, "Old")], [_tc(1, "New")], {})
        assert rows[0]["summary"] == "New"
        assert report.generated == 1
        assert report.kept == []

    def test_a_new_row_is_added(self):
        rows, report = regeneration.merge([_tc(1)], [_tc(1), _tc(2)], {})
        assert [row["id"] for row in rows] == ["SC1_001", "SC1_002"]
        assert report.generated == 2

    def test_an_edited_row_the_new_pack_omits_is_still_kept(self):
        """A wipe-and-replace would delete it — which is the same data loss
        wearing a different hat."""
        existing = [_tc(1, "Mine"), _tc(2, "Generated")]
        rows, report = regeneration.merge(existing, [_tc(2, "Regenerated")],
                                          self._meta("SC1_001"))
        assert {row["id"] for row in rows} == {"SC1_001", "SC1_002"}
        assert report.orphans_kept == ["SC1_001"]
        assert next(r for r in rows if r["id"] == "SC1_001")["summary"] == "Mine"

    def test_the_incoming_order_is_respected(self):
        """A regeneration is entitled to decide the order; kept orphans go
        last, because their old position no longer means anything."""
        existing = [_tc(1, "Mine"), _tc(2), _tc(3)]
        rows, _ = regeneration.merge(existing, [_tc(3), _tc(2)],
                                     self._meta("SC1_001"))
        assert [row["id"] for row in rows] == ["SC1_003", "SC1_002",
                                               "SC1_001"]

    def test_neither_argument_is_mutated(self):
        existing = [_tc(1, "Mine")]
        incoming = [_tc(1, "Theirs")]
        regeneration.merge(existing, incoming, self._meta("SC1_001"))
        assert existing[0]["summary"] == "Mine"
        assert incoming[0]["summary"] == "Theirs"

    def test_replace_discards_and_says_how_many(self):
        """Available deliberately — somebody may want the generator's version
        back — but never quietly."""
        rows, report = regeneration.merge(
            [_tc(1, "Mine")], [_tc(1, "Theirs")], self._meta("SC1_001"),
            policy="replace")
        assert rows[0]["summary"] == "Theirs"
        assert report.discarded == ["SC1_001"]
        assert "discarded" in report.message()

    def test_an_unknown_policy_is_refused_by_name(self):
        with pytest.raises(ValueError) as exc:
            regeneration.merge([], [], {}, policy="clobber")
        assert "clobber" in str(exc.value)

    def test_a_merge_that_protected_nothing_has_nothing_to_say(self):
        _, report = regeneration.merge([_tc(1)], [_tc(1)], {})
        assert report.message() == ""

    def test_the_message_names_the_items(self):
        _, report = regeneration.merge([_tc(1, "Mine")], [_tc(1)],
                                       self._meta("SC1_001"))
        assert "SC1_001" in report.message()
        assert "1 of your edits kept" in report.message()

    def test_the_message_caps_the_sample(self):
        ids = [f"SC1_{i:03d}" for i in range(1, 12)]
        existing = [_tc(i, "Mine") for i in range(1, 12)]
        _, report = regeneration.merge(existing, [], self._meta(*ids))
        assert "…" in report.message()


# ── Through the pack writers ──────────────────────────────────────

class TestProtectedWrites:

    def test_the_measured_bug_is_fixed(self, project, editing_on):
        """Exactly the sequence from the module docstring."""
        db.save_test_cases(project, [_tc(1, "Generated one"),
                                     _tc(2, "Generated two")])
        editable.patch("test_case", project, "SC1_001",
                       {"summary": "A human rewrote this carefully"})
        db.save_test_cases(project, [_tc(1, "Generated one"),
                                     _tc(2, "Generated two")],
                           protect_edits=True)
        stored = {row["id"]: row["summary"]
                  for row in db.load_test_cases(project)}
        assert stored["SC1_001"] == "A human rewrote this carefully"
        assert stored["SC1_002"] == "Generated two"

    def test_the_flag_and_the_content_agree_afterwards(self, project,
                                                       editing_on):
        """The old behaviour left a row saying ai_generated=False while
        holding generated text — which is what made the pill a lie."""
        db.save_test_cases(project, [_tc(1)])
        editable.patch("test_case", project, "SC1_001", {"summary": "Mine"})
        db.save_test_cases(project, [_tc(1, "Regenerated")],
                           protect_edits=True)
        meta = db.load_edit_metadata(project)
        stored = db.load_test_cases(project)[0]
        assert meta["SC1_001"]["ai_generated"] is False
        assert stored["summary"] == "Mine"

    def test_it_is_off_by_default_so_every_old_caller_is_unchanged(
            self, project, editing_on):
        """A reorder or an upload writes the rows it was given. Only the
        Generate paths ask for protection."""
        db.save_test_cases(project, [_tc(1)])
        editable.patch("test_case", project, "SC1_001", {"summary": "Mine"})
        db.save_test_cases(project, [_tc(1, "Regenerated")])
        assert db.load_test_cases(project)[0]["summary"] == "Regenerated"

    def test_the_checklist_is_protected_too(self, project, editing_on):
        db.save_checklist(project, [_cl(1), _cl(2)])
        editable.patch("checklist_item", project, "HDR_001",
                       {"objective": "Verify that the logo links home"})
        db.save_checklist(project, [_cl(1), _cl(2)], protect_edits=True)
        stored = {row["id"]: row["objective"]
                  for row in db.load_checklist(project)}
        assert stored["HDR_001"] == "Verify that the logo links home"

    def test_the_report_is_available_once(self, project, editing_on):
        db.save_test_cases(project, [_tc(1)])
        editable.patch("test_case", project, "SC1_001", {"summary": "Mine"})
        db.save_test_cases(project, [_tc(1)], protect_edits=True)
        report = db.take_merge_report("test_cases")
        assert report is not None and report.kept == ["SC1_001"]
        assert db.take_merge_report("test_cases") is None, \
            "popped, so a later unprotected write cannot repeat the message"

    def test_replace_over_the_writer_discards(self, project, editing_on):
        db.save_test_cases(project, [_tc(1)])
        editable.patch("test_case", project, "SC1_001", {"summary": "Mine"})
        db.save_test_cases(project, [_tc(1, "Regenerated")],
                           protect_edits=True, policy="replace")
        assert db.load_test_cases(project)[0]["summary"] == "Regenerated"
        assert db.take_merge_report("test_cases").discarded == ["SC1_001"]

    def test_a_kept_row_keeps_its_version_and_provenance(self, project,
                                                        editing_on):
        db.save_test_cases(project, [_tc(1)])
        editable.patch("test_case", project, "SC1_001", {"summary": "Mine"})
        db.save_test_cases(project, [_tc(1)], protect_edits=True)
        meta = db.load_edit_metadata(project)["SC1_001"]
        assert meta["row_version"] == 2 and meta["ai_generated"] is False

    def test_an_edit_survives_two_regenerations(self, project, editing_on):
        db.save_test_cases(project, [_tc(1)])
        editable.patch("test_case", project, "SC1_001", {"summary": "Mine"})
        for _ in range(2):
            db.save_test_cases(project, [_tc(1, "Regenerated")],
                               protect_edits=True)
        assert db.load_test_cases(project)[0]["summary"] == "Mine"


# ── Through the app ───────────────────────────────────────────────

class TestThroughGenerate:

    def test_the_generate_path_asks_for_protection(self):
        """Pinned in the source, because the alternative is a full generation
        run in a unit test — and what matters is that the call site opted in."""
        import pathlib
        body = pathlib.Path("routes/generation.py").read_text(encoding="utf-8")
        assert body.count("protect_edits=True") == 2, \
            "both _store_test_cases and _store_checklist"
        assert "_flash_merge_report" in body

    def test_the_user_is_told_what_was_kept(self, project, editing_on):
        report = regeneration.MergeReport(policy="merge", generated=12,
                                          kept=["SC1_001", "SC1_004"])
        message = report.message()
        assert "12 regenerated" in message
        assert "2 of your edits kept" in message
