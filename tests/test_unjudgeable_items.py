"""A blank card said "nothing here to judge" and offered Passed.

Walked: create a test case with the editors' "+ New test case" button, leave
it empty, start a manual run. The third card carries an id, no steps, no
expected result, and this message —

    This item has no steps and no expected result yet. There is nothing here
    to judge. Write it in the Test Cases or Checklist module first…

— above five verdict buttons. Clicking Passed took the run from "2 / 3
judged" to **"3 / 3 judged · 66.7% passed of 3 executed"**. Coverage that
never happened, on the same screen that promises "a partial walk is never
reported as full coverage".

The sibling state is worse in the same way. When an item is ``missing`` —
deleted from the pack mid-walk — the warning says *"Record it as Skipped —
the run keeps its original total, so the coverage number stays honest"*, and
Passed sat directly underneath that sentence.

Both branches were written by somebody who saw the problem: the template
comment above the empty case reads "Blank card, five verdict buttons, nothing
to judge. Point at the fix rather than inviting a guess." The message was
added; the buttons were not. This closes the second half.

``Skipped`` is the honest verdict because it is excluded from
``EXECUTED_VERDICTS``: the run keeps its original total and the percentage
stays true. That is the whole reason it is the one offered.
"""
from __future__ import annotations

import pytest

from engine import db as _db
from engine import manual_run as mr
from engine.testcase_generator import TestCase
from routes._shared import SERVER_START_TIME, tc_to_dict


def _case(**kwargs):
    base = dict(id="TC-001", section="Checkout", section_num=1,
                summary="Verify the discount applies",
                preconditions="", test_steps="1. Add an item", test_data="",
                expected_result="Total drops", issues="", comment="",
                user_story_id="", category="Positive", priority="High",
                status="Unchecked")
    base.update(kwargs)
    return TestCase(**base)


@pytest.fixture(autouse=True)
def _ready():
    _db.init_db()


@pytest.fixture
def walk(client, make_project):
    """A run over one written case and one blank one, in that order."""
    pid = make_project("Unjudgeable walk")
    _db.save_test_cases(pid, [
        tc_to_dict(_case()),
        tc_to_dict(_case(id="TC-002", summary="", test_steps="",
                         expected_result="")),
    ])
    with client.session_transaction() as sess:
        sess["project_id"] = pid
        sess["_session_active_since"] = SERVER_START_TIME
        assignee = sess.get("_user_id") or ""
    queue = mr.build_queue(
        [_case(), _case(id="TC-002", summary="", test_steps="",
                        expected_result="")], [], [])
    run_id = _db.start_execution_run(pid, {
        "mode": "manual", "manual_queue": mr.queue_to_payload(queue),
        "environment": "", "tester": "walker", "assignee_id": assignee})
    return {"client": client, "run_id": run_id, "pid": pid}


class TestTheRule:
    """``QueueItem.allowed_verdicts`` — the whole fix in one property."""

    def test_a_written_item_may_take_any_verdict(self):
        item = mr.build_queue([_case()], [], [])[0]
        assert item.allowed_verdicts == mr.VERDICTS

    def test_an_empty_item_may_only_be_skipped(self):
        item = mr.build_queue(
            [_case(summary="", test_steps="", expected_result="")], [], [])[0]
        assert item.empty is True
        assert item.allowed_verdicts == ("Skipped",)

    def test_a_missing_item_may_only_be_skipped(self):
        item = mr.QueueItem(external_id="TC-404", kind="test_case",
                            missing=True)
        assert item.allowed_verdicts == ("Skipped",)

    def test_skipped_is_the_one_that_keeps_the_figure_honest(self):
        """Not an arbitrary choice: it is the only verdict excluded from
        ``EXECUTED_VERDICTS``, so recording it leaves the denominator alone.
        If that ever changes, this rule needs rethinking rather than
        renaming."""
        assert "Skipped" not in mr.EXECUTED_VERDICTS
        for verdict in mr.VERDICTS:
            if verdict != "Skipped":
                assert verdict in mr.EXECUTED_VERDICTS


class TestTheScreen:

    def _page(self, walk, index):
        return walk["client"].get(
            f"/test-execution/manual/{walk['run_id']}?i={index}"
        ).get_data(as_text=True)

    def test_the_blank_card_offers_only_skipped(self, walk):
        body = self._page(walk, 1)
        assert 'value="Skipped"' in body
        for gone in ("Passed", "Failed", "Blocked"):
            assert f'name="verdict" value="{gone}"' not in body, gone

    def test_the_written_card_still_offers_them_all(self, walk):
        """The control. A fix that reduced every card to Skipped would pass
        the test above and destroy the module."""
        body = self._page(walk, 0)
        for verdict in mr.VERDICTS:
            assert f'value="{verdict}"' in body, verdict


class TestThePostIsRefusedToo:
    """The buttons are the interface, not the rule — a stale tab, a
    resubmitted page or a script posts whatever it likes."""

    def _post(self, walk, external_id, verdict):
        return walk["client"].post(
            f"/test-execution/manual/{walk['run_id']}/verdict",
            data={"external_id": external_id, "kind": "test_case",
                  "notes": "", "verdict": verdict},
            follow_redirects=False)

    def test_passing_a_blank_item_is_refused(self, walk):
        self._post(walk, "TC-002", "Passed")
        results = _db.list_case_results(walk["run_id"])
        assert results == [], results

    def test_the_refusal_says_what_to_do(self, walk):
        self._post(walk, "TC-002", "Passed")
        with walk["client"].session_transaction() as sess:
            flashed = " ".join(str(m) for m in sess.get("_flashes", []))
        assert "Skipped" in flashed and "nothing to judge" in flashed, flashed

    def test_skipping_a_blank_item_is_recorded(self, walk):
        """The verdict the page does offer has to work, or the item can
        never leave the queue."""
        self._post(walk, "TC-002", "Skipped")
        statuses = [r.get("status") for r in
                    _db.list_case_results(walk["run_id"])]
        assert statuses == ["Skipped"], statuses

    def test_the_written_item_is_unaffected(self, walk):
        self._post(walk, "TC-001", "Passed")
        statuses = [r.get("status") for r in
                    _db.list_case_results(walk["run_id"])]
        assert statuses == ["Passed"], statuses

    def test_the_coverage_figure_stays_honest(self, walk):
        """The number the whole fix is about. One written case passed, one
        blank case skipped: one executed item, all of it green — not two
        thirds of a walk that never happened."""
        self._post(walk, "TC-001", "Passed")
        self._post(walk, "TC-002", "Skipped")
        results = _db.list_case_results(walk["run_id"])
        progress = mr.compute_progress(
            mr.restore_queue(
                (_db.get_execution_run(walk["run_id"])["env_payload"]
                 or {})["manual_queue"],
                [_case(), _case(id="TC-002", summary="", test_steps="",
                                expected_result="")], []),
            results)
        assert progress.executed == 1, progress
        # 100% of what was actually exercised, not 50% of a walk that
        # counted a blank card. ``pass_rate`` is the figure the page prints.
        assert progress.pass_rate == 100.0, progress
        assert progress.counts.get("Skipped") == 1, progress.counts
