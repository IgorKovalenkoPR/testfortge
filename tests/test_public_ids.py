"""Public item ids are unique — E4.4a.

The bug this closes was measured, not theorised: ``POST /checklist`` for
https://example.com produced an 82-item pack containing ``CNT_001`` twice —
once from the site-aware builder's "Page Content" section, once from a
rule-driven "Content & Layout" section. Each builder counts from 1 over its
own output and the route concatenates the lists.

Two things made that expensive rather than cosmetic:

* every editor addresses a row by this id, so the duplicated row is
  unaddressable (``editable.AmbiguousEntity``), which blocked E4.4;
* a unique index over the column — added in E4.3 for test cases — turned the
  duplicate into a rolled-back INSERT, so the *whole pack* vanished and the
  page rendered empty.

So the tests here care about two properties in particular: ids that are
already unique are never disturbed (they appear in exports and in bug reports
that cite "failed at CNT_014"), and a duplicate never costs the user their
pack.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import text

from engine import db, editable, public_ids


class TestSplitAndFormat:

    @pytest.mark.parametrize("value,expected", [
        ("CNT_001", ("CNT_", 1)),
        ("TC-004", ("TC-", 4)),
        ("SC1_012", ("SC1_", 12)),
        ("WALK-100", ("WALK-", 100)),
        ("LIVE-PAGE-007", ("LIVE-PAGE-", 7)),
        ("header", ("header", None)),
        ("", ("", None)),
    ])
    def test_the_separator_stays_with_the_prefix(self, value, expected):
        """So a renumbered id looks like its neighbours instead of switching
        from ``_`` to ``-``."""
        assert public_ids.split_id(value) == expected

    def test_formatting_pads_to_three(self):
        assert public_ids.format_id("CNT_", 7) == "CNT_007"

    def test_a_wider_scheme_can_be_kept(self):
        assert public_ids.format_id("CNT_", 7, width=4) == "CNT_0007"


class TestEnsureUnique:

    def test_a_unique_pack_is_left_completely_alone(self):
        """These ids leave the system — exports, bug reports, review
        comments. Renumbering one that did not have to change is a
        regression in itself."""
        items = [{"id": "CNT_001"}, {"id": "CNT_002"}, {"id": "HDR_001"}]
        assert public_ids.ensure_unique(items) == []
        assert [i["id"] for i in items] == ["CNT_001", "CNT_002", "HDR_001"]

    def test_the_first_occurrence_keeps_the_id(self):
        """It is the one most likely to be already cited somewhere."""
        items = [{"id": "CNT_001", "objective": "first"},
                 {"id": "CNT_001", "objective": "second"}]
        public_ids.ensure_unique(items)
        assert items[0]["id"] == "CNT_001"
        assert items[1]["id"] != "CNT_001"

    def test_a_new_number_never_lands_on_a_used_one(self):
        """The reservation pass exists for this: CNT_002 is taken by an item
        that is not being renumbered, so the duplicate has to skip it."""
        items = [{"id": "CNT_001"}, {"id": "CNT_001"}, {"id": "CNT_002"}]
        public_ids.ensure_unique(items)
        assert [i["id"] for i in items] == ["CNT_001", "CNT_003", "CNT_002"]

    def test_ids_end_up_distinct(self):
        items = [{"id": "X_001"} for _ in range(20)]
        public_ids.ensure_unique(items)
        assert len({i["id"] for i in items}) == 20

    def test_a_blank_id_gets_the_fallback_prefix(self):
        items = [{"id": ""}, {"id": None}, {}]
        public_ids.ensure_unique(items, fallback_prefix="CL_")
        assert [i["id"] for i in items] == ["CL_001", "CL_002", "CL_003"]

    def test_a_duplicate_without_a_number_starts_numbering(self):
        items = [{"id": "Header"}, {"id": "Header"}]
        public_ids.ensure_unique(items)
        assert items[0]["id"] == "Header"
        assert items[1]["id"] == "Header_001"

    def test_a_wider_numbering_scheme_is_not_narrowed(self):
        items = [{"id": "CNT_0001"}, {"id": "CNT_0001"}]
        public_ids.ensure_unique(items)
        assert items[1]["id"] == "CNT_0002"

    def test_the_taken_set_is_honoured(self):
        items = [{"id": "CNT_001"}]
        public_ids.ensure_unique(items, taken={"CNT_001", "CNT_002"})
        assert items[0]["id"] == "CNT_003"

    def test_it_mutates_in_place(self):
        """Deliberate, and the reason it can live inside ``save_*``: the same
        list is mirrored into the Flask session, and a database that
        renumbered an id while the session kept the old one would show an id
        no editor could find."""
        items = [{"id": "A_001"}, {"id": "A_001"}]
        returned = public_ids.ensure_unique(items)
        assert returned == [("A_001", "A_002")]
        assert items[1]["id"] == "A_002"

    def test_it_works_on_dataclasses_too(self):
        """The generators hand round ``ChecklistItem`` instances, not dicts."""
        @dataclass
        class Item:
            id: str

        items = [Item("CNT_001"), Item("CNT_001")]
        public_ids.ensure_unique(items)
        assert [i.id for i in items] == ["CNT_001", "CNT_002"]

    def test_the_same_input_gives_the_same_ids(self):
        """A regeneration must not shuffle ids that nothing else changed."""
        def pack():
            return [{"id": "CNT_001"}, {"id": "CNT_001"}, {"id": "HDR_001"}]

        first, second = pack(), pack()
        public_ids.ensure_unique(first)
        public_ids.ensure_unique(second)
        assert [i["id"] for i in first] == [i["id"] for i in second]

    def test_an_empty_pack_is_fine(self):
        assert public_ids.ensure_unique([]) == []


class TestPackWrites:

    @pytest.fixture
    def project(self, app, request):
        return db.upsert_project(name=f"E4.4a {request.node.name}"[:180])

    def test_a_checklist_with_duplicates_stores_every_row(self, project):
        """The regression that started this: the unique index turned the
        duplicate into a rolled-back INSERT, so an 82-item pack stored
        nothing and /checklist rendered its empty state."""
        items = [
            {"id": "CNT_001", "section": "Content & Layout",
             "objective": "Exactly one H1"},
            {"id": "CNT_001", "section": "Page Content",
             "objective": "The heading matches the design"},
            {"id": "HDR_001", "section": "Header", "objective": "Logo links home"},
        ]
        db.save_checklist(project, items)
        stored = db.load_checklist(project)
        assert len(stored) == 3
        assert len({row["id"] for row in stored}) == 3

    def test_the_callers_list_carries_the_assigned_ids(self, project):
        """``routes/generation`` mirrors the same list into the session."""
        items = [{"id": "CNT_001", "objective": "first"},
                 {"id": "CNT_001", "objective": "second"}]
        db.save_checklist(project, items)
        assert [i["id"] for i in items] == [
            row["id"] for row in db.load_checklist(project)]

    def test_test_cases_get_the_same_guarantee(self, project):
        """Two generators sharing a prefix would otherwise lose the pack."""
        cases = [
            {"id": "SC1_001", "summary": "First", "section_num": 1},
            {"id": "SC1_001", "summary": "Second", "section_num": 1},
        ]
        db.save_test_cases(project, cases)
        stored = db.load_test_cases(project)
        assert len(stored) == 2
        assert len({row["id"] for row in stored}) == 2

    def test_an_append_does_not_collide_with_what_is_already_there(
            self, client, project):
        """The most user-visible case: uploading a pack whose ids overlap the
        stored ones. The rows already in the project keep their ids — someone
        may have cited them — and the incoming rows take the next free
        numbers instead of duplicating or being lost.
        """
        import io

        db.save_checklist(project, [
            {"id": "CNT_001", "objective": "Existing one", "item_num": "1.1"},
            {"id": "CNT_002", "objective": "Existing two", "item_num": "1.2"},
        ])
        with client.session_transaction() as sess:
            sess["project_id"] = project
        csv = ("ID,Section,Objective,Category,Priority\n"
               "CNT_001,Content,Uploaded one,Positive,High\n"
               "CNT_002,Content,Uploaded two,Positive,High\n")
        resp = client.post("/checklist/upload", data={
            "upload_file": (io.BytesIO(csv.encode()), "cl.csv"),
            "upload_mode": "append",
        }, content_type="multipart/form-data", follow_redirects=True)
        assert resp.status_code == 200

        rows = db.load_checklist(project)
        assert len(rows) == 4, "nothing may be dropped or merged away"
        assert len({row["id"] for row in rows}) == 4
        by_id = {row["id"]: row["objective"] for row in rows}
        assert by_id["CNT_001"] == "Existing one"
        assert by_id["CNT_002"] == "Existing two"

    def test_a_checklist_item_is_addressable_afterwards(self, project):
        """The point of the whole task: E4.4 needs this to be true."""
        db.save_checklist(project, [
            {"id": "CNT_001", "objective": "First"},
            {"id": "CNT_001", "objective": "Second"},
        ])
        row = editable.get("checklist_item", project, "CNT_002")
        assert row is not None and row["objective"] == "Second"


class TestTheIndex:

    @pytest.fixture
    def project(self, app, request):
        return db.upsert_project(name=f"E4.4a idx {request.node.name}"[:180])

    def test_both_editable_packs_are_constrained(self, app):
        """The index is the thing that fails loudly if either the write-time
        guard or the backfill regresses."""
        with db.session_scope() as sess:
            names = {row[0] for row in sess.execute(text(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND name LIKE 'ux_%'")).all()}
        assert "ux_test_case_project_external_id" in names
        assert "ux_checklist_item_project_external_id" in names

    def test_a_duplicate_insert_is_refused(self, project):
        insert = text(
            "INSERT INTO checklist_item (project_id, external_id, objective, "
            "created_at, updated_at, row_version, ai_generated) VALUES "
            "(:p, 'CNT_001', :o, '2026-01-01', '2026-01-01', 1, 1)")
        with db.session_scope() as sess:
            sess.execute(insert, {"p": project, "o": "first"})
        with pytest.raises(Exception) as exc:
            with db.session_scope() as sess:
                sess.execute(insert, {"p": project, "o": "second"})
        assert "UNIQUE" in str(exc.value).upper()

    def test_two_projects_may_use_the_same_id(self, project, app):
        """It is scoped per project, not global — every project starts at 1."""
        other = db.upsert_project(name="E4.4a idx neighbour")
        db.save_checklist(project, [{"id": "CNT_001", "objective": "mine"}])
        db.save_checklist(other, [{"id": "CNT_001", "objective": "theirs"}])
        assert db.load_checklist(project)[0]["id"] == "CNT_001"
        assert db.load_checklist(other)[0]["id"] == "CNT_001"


class TestBackfill:
    """Rows written before E4.4a. The index cannot be created over them, so
    the data is repaired first."""

    @pytest.fixture
    def legacy(self, app, request):
        project = db.upsert_project(name=f"E4.4a legacy {request.node.name}"[:180])
        # The engine the ORM is bound to, not ``get_engine()``. They can
        # differ: a test elsewhere that swaps ``_engine`` without ``_Session``
        # leaves the sessionmaker pointing at another database, and then the
        # project written above is invisible to a raw connection — which
        # presents as "FOREIGN KEY constraint failed" here rather than
        # anywhere near the cause.
        with db.session_scope() as sess:
            engine = sess.get_bind()
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS "
                              "ux_checklist_item_project_external_id"))
            for position, (external_id, objective) in enumerate([
                    ("CNT_001", "first"), ("CNT_001", "second"),
                    ("CNT_001", "third"), ("CNT_002", "fourth"),
                    ("HDR_001", "fifth")]):
                conn.execute(text(
                    "INSERT INTO checklist_item (project_id, external_id, "
                    "objective, created_at, updated_at, row_version, "
                    "ai_generated) VALUES (:p, :e, :o, '2026-01-01', "
                    "'2026-01-01', :v, 0)"),
                    {"p": project, "e": external_id, "o": objective,
                     "v": position + 1})
        yield project, engine
        # Put the index back. The whole suite shares one database, so a
        # dropped index would silently disarm the guard for every module
        # that happens to run after this one.
        db._renumber_duplicate_public_ids(engine)
        db._ensure_public_id_unique_indexes(engine)

    @staticmethod
    def _rows(engine, project):
        with engine.begin() as conn:
            return conn.execute(text(
                "SELECT external_id, objective, row_version FROM "
                "checklist_item WHERE project_id = :p ORDER BY id"),
                {"p": project}).all()

    def test_duplicates_are_renumbered_and_the_first_keeps_its_id(self,
                                                                  legacy):
        project, engine = legacy
        db._renumber_duplicate_public_ids(engine)
        rows = self._rows(engine, project)
        assert [r[0] for r in rows] == ["CNT_001", "CNT_003", "CNT_004",
                                        "CNT_002", "HDR_001"]

    def test_the_repair_preserves_provenance(self, legacy):
        """The metadata lives on the row, so renaming the id must not
        disturb ``row_version`` or ``ai_generated`` — otherwise the repair
        would hand somebody's edited item back to the next regeneration."""
        project, engine = legacy
        db._renumber_duplicate_public_ids(engine)
        assert [r[2] for r in self._rows(engine, project)] == [1, 2, 3, 4, 5]

    def test_the_index_can_be_created_after_the_repair(self, legacy):
        project, engine = legacy
        db._renumber_duplicate_public_ids(engine)
        db._ensure_public_id_unique_indexes(engine)
        with engine.begin() as conn:
            names = {row[0] for row in conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND name = 'ux_checklist_item_project_external_id'")).all()}
        assert names, "the repair has to leave the data indexable"

    def test_the_repair_is_idempotent(self, legacy):
        project, engine = legacy
        db._renumber_duplicate_public_ids(engine)
        first = self._rows(engine, project)
        db._renumber_duplicate_public_ids(engine)
        assert self._rows(engine, project) == first
