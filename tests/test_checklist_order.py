"""Checklist order and numbering — E4.4.

The governing rule is not mine. ``qa_knowledge/style/checklist_style.yaml``,
measured from the team's own reference checklist, says:

    Numbers are stable identifiers. Inserting a check appends it at the end of
    its parent rather than renumbering the siblings.

and lists "renumbering siblings to insert a check" as an anti-pattern because
*the numbers are cited in bug reports and status updates*. The first version
of this module renumbered the whole pack after every change, which is exactly
what that forbids.

So most of what is pinned here is about what does **not** move.
"""
from __future__ import annotations

import pytest

from engine import checklist_order as order


def _pack(*spec):
    """``_pack(("Header", "A", "1.1"), …)`` → stored-shape dicts."""
    out = []
    for section, item_id, *rest in spec:
        out.append({"id": item_id, "section": section,
                    "objective": f"check {item_id}",
                    "item_num": rest[0] if rest else "",
                    "depth": rest[1] if len(rest) > 1 else 2})
    return out


def _nums(items):
    return [item["item_num"] for item in items]


def _ids(items):
    return [item["id"] for item in items]


class TestSectionIndices:

    def test_indices_are_read_from_the_numbers_already_there(self):
        """Not recomputed from position: a section that empties out would
        otherwise shift every later section's prefix."""
        items = _pack(("Header", "A", "1.1"), ("Footer", "B", "3.1"))
        assert order.section_indices(items) == {"Header": 1, "Footer": 3}

    def test_a_gap_left_by_an_emptied_section_is_preserved(self):
        items = _pack(("Header", "A", "1.1"), ("Footer", "C", "3.1"))
        # "Nav" (2) is gone entirely; Footer stays 3.
        assert order.section_indices(items)["Footer"] == 3

    def test_an_unnumbered_section_gets_the_next_free_index(self):
        items = _pack(("Header", "A", "1.1"), ("New", "B"))
        assert order.section_indices(items)["New"] == 2

    def test_names_come_back_in_pack_order(self):
        items = _pack(("Header", "A"), ("Footer", "B"), ("Header", "C"))
        assert order.sections(items) == ["Header", "Footer"]


class TestNextNumber:
    """The append rule, which is the style file's whole point."""

    def test_an_item_appended_takes_one_past_the_highest_sibling(self):
        items = _pack(("Header", "A", "1.1"), ("Header", "B", "1.2"))
        assert order.next_number(items, "Header") == "1.3"

    def test_the_siblings_are_not_renumbered(self):
        items = _pack(("Header", "A", "1.1"), ("Header", "B", "1.2"))
        order.next_number(items, "Header")
        assert _nums(items) == ["1.1", "1.2"]

    def test_a_gap_below_the_highest_is_not_reused(self):
        """1.2 was deleted; the next item is 1.4, not 1.2. Reusing a vacated
        number would point at a different check than the one cited."""
        items = _pack(("Header", "A", "1.1"), ("Header", "C", "1.3"))
        assert order.next_number(items, "Header") == "1.4"

    def test_the_first_item_of_a_new_section(self):
        items = _pack(("Header", "A", "1.1"))
        assert order.next_number(items, "Footer") == "2.1"

    def test_a_sub_item_hangs_off_the_last_top_level_item(self):
        items = _pack(("Header", "A", "1.1"), ("Header", "B", "1.2"))
        assert order.next_number(items, "Header", depth=3) == "1.2.1"

    def test_a_second_sub_item_continues_the_first(self):
        items = _pack(("Header", "A", "1.1"), ("Header", "B", "1.2"),
                      ("Header", "C", "1.2.1", 3))
        assert order.next_number(items, "Header", depth=3) == "1.2.2"

    def test_a_sub_item_with_no_parent_becomes_a_top_level_item(self):
        assert order.next_number(_pack(), "Header", depth=3) == "1.1"


class TestMove:

    def test_up(self):
        items = _pack(("H", "A", "1.1"), ("H", "B", "1.2"), ("H", "C", "1.3"))
        assert _ids(order.move(items, "C", -1)) == ["A", "C", "B"]

    def test_down(self):
        items = _pack(("H", "A", "1.1"), ("H", "B", "1.2"))
        assert _ids(order.move(items, "A", 1)) == ["B", "A"]

    def test_the_numbers_follow_the_rows(self):
        """A deliberate reorder is the one case where renumbering is right:
        "1.3" sitting above "1.2" is a sheet contradicting itself."""
        items = _pack(("H", "A", "1.1"), ("H", "B", "1.2"), ("H", "C", "1.3"))
        moved = order.move(items, "A", 1)
        assert [(i["id"], i["item_num"]) for i in moved] == [
            ("B", "1.1"), ("A", "1.2"), ("C", "1.3")]

    def test_only_the_section_that_moved_is_renumbered(self):
        """Every other section's numbers stay exactly as cited."""
        items = _pack(("Header", "A", "1.1"), ("Header", "B", "1.2"),
                      ("Footer", "C", "2.1"), ("Footer", "D", "2.2"))
        moved = order.move(items, "A", 1)
        assert [i["item_num"] for i in moved if i["section"] == "Footer"] == [
            "2.1", "2.2"]

    def test_a_section_keeps_its_index_when_renumbered(self):
        items = _pack(("Footer", "A", "3.1"), ("Footer", "B", "3.2"))
        moved = order.move(items, "A", 1)
        assert _nums(moved) == ["3.1", "3.2"]

    def test_moving_the_first_item_up_does_nothing(self):
        items = _pack(("H", "A"), ("H", "B"))
        assert _ids(order.move(items, "A", -1)) == ["A", "B"]

    def test_moving_the_last_item_down_does_nothing(self):
        items = _pack(("H", "A"), ("H", "B"))
        assert _ids(order.move(items, "B", 1)) == ["A", "B"]

    def test_a_move_cannot_cross_a_section_boundary(self):
        items = _pack(("Header", "A"), ("Header", "B"), ("Footer", "C"))
        assert _ids(order.move(items, "B", 1)) == ["A", "B", "C"]
        assert _ids(order.move(items, "C", -1)) == ["A", "B", "C"]

    def test_an_unknown_id_is_refused_by_name(self):
        with pytest.raises(order.OrderError) as exc:
            order.move(_pack(("H", "A")), "ZZ_999", 1)
        assert "ZZ_999" in str(exc.value)

    def test_a_non_numeric_delta_is_refused(self):
        with pytest.raises(order.OrderError):
            order.move(_pack(("H", "A")), "A", "down")

    def test_the_caller_s_pack_order_is_not_mutated(self):
        items = _pack(("H", "A"), ("H", "B"))
        order.move(items, "A", 1)
        assert _ids(items) == ["A", "B"]


class TestRelocate:

    def test_the_item_joins_the_destination_block(self):
        """Otherwise the page renders a second "Header" heading further down:
        the template starts a block whenever the section changes between
        adjacent rows."""
        items = _pack(("Header", "A", "1.1"), ("Footer", "B", "2.1"),
                      ("Footer", "C", "2.2"))
        moved = order.relocate(items, "C", "Header")
        assert _ids(moved) == ["A", "C", "B"]

    def test_it_is_appended_with_the_next_free_number(self):
        items = _pack(("Header", "A", "1.1"), ("Footer", "B", "2.1"))
        moved = order.relocate(items, "B", "Header")
        assert [(i["id"], i["item_num"]) for i in moved] == [
            ("A", "1.1"), ("B", "1.2")]

    def test_the_former_siblings_are_not_renumbered(self):
        """The vacated number stays vacated — a gap is honest, and closing it
        would restate numbers somebody cited."""
        items = _pack(("Header", "A", "1.1"),
                      ("Footer", "B", "2.1"), ("Footer", "C", "2.2"),
                      ("Footer", "D", "2.3"))
        moved = order.relocate(items, "C", "Header")
        assert [i["item_num"] for i in moved if i["section"] == "Footer"] == [
            "2.1", "2.3"]

    def test_relocating_within_the_same_section_changes_nothing(self):
        items = _pack(("Header", "A", "1.1"), ("Header", "B", "1.2"))
        moved = order.relocate(items, "B", "Header")
        assert [(i["id"], i["item_num"]) for i in moved] == [
            ("A", "1.1"), ("B", "1.2")]

    def test_a_brand_new_section_gets_the_next_index(self):
        items = _pack(("Header", "A", "1.1"), ("Header", "B", "1.2"))
        moved = order.relocate(items, "B", "Accessibility")
        assert moved[-1]["item_num"] == "2.1"

    def test_an_empty_destination_is_refused(self):
        with pytest.raises(order.OrderError):
            order.relocate(_pack(("H", "A")), "A", "  ")

    def test_an_over_long_destination_is_refused(self):
        with pytest.raises(order.OrderError):
            order.relocate(_pack(("H", "A")), "A", "x" * 201)


class TestRegroupItem:
    """Position *and* number, for the item's current section.

    Both callers hit the same visible defect without it: an item outside its
    section's block makes the page render a second heading with the same name.
    """

    def test_an_item_stranded_after_another_section_is_pulled_back(self):
        items = _pack(("Header", "A", "1.1"), ("Footer", "B", "2.1"),
                      ("Header", "C", "1.2"))
        regrouped = order.regroup_item(items, "C")
        assert _ids(regrouped) == ["A", "C", "B"]

    def test_a_number_that_already_belongs_is_left_alone(self):
        """A created item was numbered on the way in; renumbering here would
        waste a number for nothing."""
        items = _pack(("Header", "A", "1.1"), ("Footer", "B", "2.1"),
                      ("Header", "C", "1.2"))
        assert order.regroup_item(items, "C")[1]["item_num"] == "1.2"

    def test_a_number_from_the_old_section_is_replaced(self):
        """The shape a ``section`` change through the generic PATCH leaves:
        the field says Header, the number still says 2.x."""
        items = _pack(("Header", "A", "1.1"), ("Header", "B", "2.1"))
        regrouped = order.regroup_item(items, "B")
        assert regrouped[1]["item_num"] == "1.2"

    def test_an_item_already_in_place_is_not_moved(self):
        items = _pack(("Header", "A", "1.1"), ("Header", "B", "1.2"))
        assert _ids(order.regroup_item(items, "A")) == ["A", "B"]


class TestRenameSection:

    def test_every_item_in_the_section_follows(self):
        items = _pack(("Header", "A"), ("Header", "B"), ("Footer", "C"))
        assert order.rename_section(items, "Header", "Top bar") == 2
        assert [i["section"] for i in items] == ["Top bar", "Top bar", "Footer"]

    def test_nothing_is_renumbered(self):
        """The section keeps its index, so every number still means what it
        meant."""
        items = _pack(("Header", "A", "1.1"), ("Footer", "B", "2.1"))
        order.rename_section(items, "Header", "Top bar")
        assert _nums(items) == ["1.1", "2.1"]

    def test_renaming_onto_an_existing_section_is_refused(self):
        """Merging changes section indices, and every number in every later
        section would have to be restated. The message says what to do
        instead."""
        items = _pack(("Header", "A", "1.1"), ("Nav", "B", "2.1"))
        with pytest.raises(order.OrderError) as exc:
            order.rename_section(items, "Nav", "Header")
        assert "already exists" in str(exc.value)
        assert "Move the items" in str(exc.value)
        assert [i["section"] for i in items] == ["Header", "Nav"], \
            "the refused rename must not have applied"

    def test_renaming_to_the_same_name_is_a_no_op(self):
        items = _pack(("Header", "A"))
        assert order.rename_section(items, "Header", "Header") == 0

    def test_an_empty_name_is_refused(self):
        with pytest.raises(order.OrderError):
            order.rename_section(_pack(("H", "A")), "H", "   ")

    def test_an_over_long_name_is_refused_not_truncated(self):
        with pytest.raises(order.OrderError):
            order.rename_section(_pack(("H", "A")), "H", "x" * 201)

    def test_renaming_a_section_that_is_not_there_is_refused(self):
        with pytest.raises(order.OrderError):
            order.rename_section(_pack(("H", "A")), "Footer", "Bottom")


class TestTheConventionIsShared:

    def test_renumbering_matches_the_generator(self):
        """Two spellings of one rule is how a generated pack and an edited
        pack come to disagree about what "2.4" means."""
        from engine.checklist_rules import (Check, LowLevelChecklist, Section,
                                            assign_numbers)

        generated = LowLevelChecklist(sections=[
            Section(name="Header", checks=[
                Check(objective="a", section="Header", depth=2),
                Check(objective="b", section="Header", depth=3),
                Check(objective="c", section="Header", depth=2)]),
        ])
        assign_numbers(generated)
        from_generator = [check.number
                          for section in generated.sections
                          for check in section.checks]

        items = _pack(("Header", "a"), ("Header", "b", "", 3),
                      ("Header", "c"))
        order.renumber_section(items, "Header")
        assert _nums(items) == from_generator
