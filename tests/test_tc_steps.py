"""Steps as a list, not a blob (E4.3).

The conversion is the risky part: the stored text is what the runner's
heuristic parser reads and what a person exports to a spreadsheet, so a
round trip must not quietly change the steps.
"""
from __future__ import annotations

import pytest

from engine import tc_steps


class TestParse:

    def test_a_numbered_blob_becomes_bare_steps(self):
        blob = "1. Open /login\n2. Enter the details\n3. Submit"
        assert tc_steps.parse(blob) == [
            "Open /login", "Enter the details", "Submit"]

    def test_nothing_is_no_steps(self):
        assert tc_steps.parse("") == []
        assert tc_steps.parse(None) == []

    def test_blank_lines_are_formatting_not_empty_steps(self):
        """Otherwise a spaced-out blob renders back with numbered gaps."""
        assert tc_steps.parse("1. First\n\n\n2. Second") == ["First", "Second"]

    @pytest.mark.parametrize("line,expected", [
        ("1. Open", "Open"),
        ("2) Open", "Open"),
        ("3 - Open", "Open"),
        ("4: Open", "Open"),
        ("- Open", "Open"),
        ("* Open", "Open"),
        ("Step 5: Open", "Open"),
        ("step 6. Open", "Open"),
        ("  7.   Open  ", "Open"),
    ])
    def test_every_marker_dialect_is_stripped(self, line, expected):
        """These blobs come from LLMs, from spreadsheets and from people.

        A marker left in place is renumbered into "1. 1. Open".
        """
        assert tc_steps.parse(line) == [expected]

    def test_a_step_that_merely_starts_with_a_number_is_kept_whole(self):
        """"2 items" is content, not a marker — the marker needs a separator."""
        assert tc_steps.parse("1. Add 2 items to the cart") == [
            "Add 2 items to the cart"]

    def test_an_unnumbered_blob_is_still_a_list(self):
        assert tc_steps.parse("Open the page\nClick Save") == [
            "Open the page", "Click Save"]


class TestRender:

    def test_steps_are_numbered_from_one(self):
        assert tc_steps.render(["Open", "Click"]) == "1. Open\n2. Click"

    def test_empty_list_renders_empty(self):
        assert tc_steps.render([]) == ""

    def test_a_round_trip_preserves_the_steps(self):
        blob = "1. Open /login\n2. Enter the details\n3. Submit"
        assert tc_steps.render(tc_steps.parse(blob)) == blob

    def test_a_round_trip_normalises_a_ragged_blob(self):
        """The point of the round trip: numbering becomes consistent."""
        ragged = "- Open /login\n- Enter the details\n- Submit"
        assert tc_steps.render(tc_steps.parse(ragged)) == (
            "1. Open /login\n2. Enter the details\n3. Submit")


class TestAdd:

    def test_appending_puts_the_step_last(self):
        assert tc_steps.add(["A"], "B") == ["A", "B"]

    def test_an_index_inserts_before_it(self):
        assert tc_steps.add(["A", "B"], "X", index=1) == ["A", "X", "B"]

    def test_inserting_just_past_the_end_is_an_append(self):
        assert tc_steps.add(["A", "B"], "X", index=2) == ["A", "B", "X"]

    def test_inserting_beyond_that_is_refused(self):
        with pytest.raises(tc_steps.StepError):
            tc_steps.add(["A"], "X", index=5)

    def test_an_empty_step_is_refused(self):
        with pytest.raises(tc_steps.StepError):
            tc_steps.add([], "   ")

    def test_a_marker_typed_by_the_user_is_stripped_not_doubled(self):
        assert tc_steps.add(["A"], "2. B") == ["A", "B"]

    def test_the_step_cap_is_enforced(self):
        full = [f"step {i}" for i in range(tc_steps.MAX_STEPS)]
        with pytest.raises(tc_steps.StepError):
            tc_steps.add(full, "one more")

    def test_an_over_long_step_is_refused_not_truncated(self):
        with pytest.raises(tc_steps.StepError):
            tc_steps.add([], "x" * (tc_steps.MAX_STEP_LENGTH + 1))

    def test_the_input_list_is_not_mutated(self):
        steps = ["A"]
        tc_steps.add(steps, "B")
        assert steps == ["A"]


class TestEdit:

    def test_one_step_changes(self):
        assert tc_steps.edit(["A", "B"], 1, "C") == ["A", "C"]

    def test_a_missing_step_is_refused(self):
        with pytest.raises(tc_steps.StepError):
            tc_steps.edit(["A"], 3, "C")

    def test_blanking_a_step_is_refused(self):
        """Deleting is a different operation with a different button."""
        with pytest.raises(tc_steps.StepError):
            tc_steps.edit(["A"], 0, "")


class TestRemove:

    def test_the_rest_renumber(self):
        steps = tc_steps.remove(["A", "B", "C"], 1)
        assert tc_steps.render(steps) == "1. A\n2. C"

    def test_a_missing_step_is_refused(self):
        with pytest.raises(tc_steps.StepError):
            tc_steps.remove(["A"], 1)

    def test_removing_the_last_step_leaves_no_steps(self):
        assert tc_steps.remove(["A"], 0) == []


class TestMove:

    def test_up(self):
        assert tc_steps.move(["A", "B", "C"], 2, -1) == ["A", "C", "B"]

    def test_down(self):
        assert tc_steps.move(["A", "B", "C"], 0, 1) == ["B", "A", "C"]

    def test_moving_the_first_step_up_does_nothing(self):
        """The button is on every row; scolding the user is worse."""
        assert tc_steps.move(["A", "B"], 0, -1) == ["A", "B"]

    def test_moving_the_last_step_down_does_nothing(self):
        assert tc_steps.move(["A", "B"], 1, 1) == ["A", "B"]

    def test_a_missing_step_is_refused(self):
        with pytest.raises(tc_steps.StepError):
            tc_steps.move(["A"], 4, 1)


class TestApply:
    """The single entry point the route uses, so it does not branch on ops."""

    def test_add_returns_a_renumbered_blob(self):
        assert tc_steps.apply("1. A", "add", text="B") == "1. A\n2. B"

    def test_remove_renumbers(self):
        assert tc_steps.apply("1. A\n2. B\n3. C", "remove", index=0) == (
            "1. B\n2. C")

    def test_move_renumbers(self):
        assert tc_steps.apply("1. A\n2. B", "move", index=1, delta=-1) == (
            "1. B\n2. A")

    def test_edit_keeps_the_position(self):
        assert tc_steps.apply("1. A\n2. B", "edit", index=0, text="Z") == (
            "1. Z\n2. B")

    def test_an_unknown_op_is_refused_by_name(self):
        with pytest.raises(tc_steps.StepError) as exc:
            tc_steps.apply("1. A", "reverse")
        assert "reverse" in str(exc.value)
        # And the message lists what is possible, so a client can recover.
        assert "move" in str(exc.value)

    def test_adding_to_a_case_with_no_steps_works(self):
        assert tc_steps.apply("", "add", text="First") == "1. First"

    def test_a_non_numeric_index_is_refused(self):
        with pytest.raises(tc_steps.StepError):
            tc_steps.apply("1. A", "remove", index="second")
