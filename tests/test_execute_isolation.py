"""
Isolation, identity and resumption for the manual execution walk.

Every test here is a defect the E5.1 audit measured on the live routes, and
each one shared a property that makes it worth a permanent test: the wrong
behaviour looked exactly like the right one on screen.

* a run in project A rendered project B's content, because the pack was read
  from the session (the active project) before the run's own project. Item
  ids are per-project sequences, so ``TC_001`` exists everywhere and the
  substitution is silent by construction;
* one verdict closed two items, because results were keyed on the item id
  alone while the walk mixes two id spaces. A two-item run reported
  "2 of 2, finished" after a single click;
* any caller who knew a run id could read it and write verdicts into it,
  with no project, no session and no user;
* an interrupted walk could not be found again — the state to resume it was
  in the database and nothing listed it.
"""
from __future__ import annotations

import re

import pytest

from engine import db as _db
from engine import manual_run as mr
from engine.testcase_generator import ChecklistItem, TestCase
from routes._shared import SERVER_START_TIME, cl_to_dict, tc_to_dict


def _seed(pid, summary, *, tc_id="TC_001", cl_id="HDR_001",
          cl_objective="Verify that the logo opens the Homepage"):
    _db.save_test_cases(pid, [tc_to_dict(TestCase(
        id=tc_id, section="Careers", section_num=1, summary=summary,
        preconditions="The form is opened", test_steps="1. Go to the site",
        test_data="", expected_result="A success message is displayed",
        priority="High", category="Positive"))])
    _db.save_checklist(pid, [cl_to_dict(ChecklistItem(
        id=cl_id, section="Header", objective=cl_objective, item_num="1.1",
        priority="High", category="Positive"))])


def _activate(client, pid, name="P"):
    with client.session_transaction() as sess:
        sess["_session_active_since"] = SERVER_START_TIME
        sess["project_id"] = pid
        sess["project_setup"] = {"project_name": name}
        sess.pop("test_cases_data", None)
        sess.pop("checklist_data", None)


def _start(client) -> int:
    resp = client.post("/test-execution/manual/start",
                       data={"run_mode": "manual", "tester": "alice"})
    match = re.search(r"/manual/(\d+)", resp.headers.get("Location", ""))
    assert match, f"run did not start: {resp.status_code}"
    return int(match.group(1))


@pytest.fixture
def two_projects(client, request):
    a = _db.upsert_project(f"iso-A-{request.node.name}")
    b = _db.upsert_project(f"iso-B-{request.node.name}")
    _seed(a, "AAA the summary that belongs to project A")
    _seed(b, "BBB the summary that belongs to project B")
    _activate(client, a, "A")
    return {"a": a, "b": b, "run_id": _start(client)}


class TestTheRunOwnsItsContent:
    def test_the_walk_shows_the_runs_own_project(self, client, two_projects):
        page = client.get(
            f"/test-execution/manual/{two_projects['run_id']}"
        ).get_data(as_text=True)
        assert "AAA the summary" in page
        assert "BBB the summary" not in page

    def test_switching_projects_and_back_still_shows_the_right_pack(
            self, client, two_projects):
        # The sequence that produced the defect: walk project A, switch to
        # B for something else, come back to A's run.
        _activate(client, two_projects["b"], "B")
        client.get("/test-execution")
        _activate(client, two_projects["a"], "A")
        page = client.get(
            f"/test-execution/manual/{two_projects['run_id']}"
        ).get_data(as_text=True)
        assert "AAA the summary" in page
        assert "BBB the summary" not in page

    def test_another_projects_content_is_never_substituted(
            self, client, two_projects):
        """The load-bearing assertion, kept separate from the 404 above.

        A page that 404s also passes "does not show B" — so this reads the
        pack loader directly, where the substitution actually happened, and
        would fail even if the authorisation check were removed.
        """
        from routes.execution_manual import _run_pack
        run = _db.get_execution_run(two_projects["run_id"])
        _activate(client, two_projects["b"], "B")
        with client.application.test_request_context("/"):
            tcs, _cls = _run_pack(run)
        assert [t.summary for t in tcs] == [
            "AAA the summary that belongs to project A"]


class TestRunsAreScopedToTheirProject:
    def test_a_run_from_another_project_is_not_readable(
            self, client, two_projects):
        _activate(client, two_projects["b"], "B")
        got = client.get(f"/test-execution/manual/{two_projects['run_id']}")
        # 404, not 403: whether a run id exists in a project the caller
        # cannot see is not something to confirm.
        assert got.status_code == 404

    def test_following_a_run_link_with_no_project_selects_it(
            self, client, two_projects, forget_workspace):
        """A read with nothing active adopts the run's project.

        This is the hand-off: a colleague on another machine opens the link
        and the walk works. My first cut refused it, which closed the
        measured hole and broke the module's founding property — the walk
        surviving a lost session is why the cursor lives in the database at
        all. Adoption grants nothing that the project picker does not
        already grant to every session when authentication is off, so
        refusing would have been theatre at the price of a real workflow.
        The write path is where the line is drawn instead; see below.
        """
        forget_workspace(client)
        got = client.get(f"/test-execution/manual/{two_projects['run_id']}")
        assert got.status_code == 200
        assert "AAA the summary" in got.get_data(as_text=True)
        with client.session_transaction() as sess:
            assert sess.get("project_id") == two_projects["a"]

    def test_a_session_with_no_project_cannot_write_a_verdict(
            self, client, two_projects, forget_workspace):
        """Writes do not adopt. A verdict is what damages data, and the
        hand-off flow loads the page first — which adopts — so a POST that
        arrives having never read the run is not that flow."""
        run_id = two_projects["run_id"]
        forget_workspace(client)
        resp = client.post(f"/test-execution/manual/{run_id}/verdict",
                           data={"external_id": "TC_001", "kind": "test_case",
                                 "verdict": "Failed"})
        assert resp.status_code == 404
        # The measured defect wrote the row and returned a redirect, so the
        # status code alone is not the assertion that matters.
        assert _db.list_case_results(run_id) == []

    def test_the_owner_can_still_read_and_write(self, client, two_projects):
        run_id = two_projects["run_id"]
        assert client.get(
            f"/test-execution/manual/{run_id}").status_code == 200
        client.post(f"/test-execution/manual/{run_id}/verdict",
                    data={"external_id": "TC_001", "kind": "test_case",
                          "verdict": "Passed"})
        assert len(_db.list_case_results(run_id)) == 1


class TestAnItemIsIdentifiedByKindAndId:
    """The two id spaces are separate sequences, so a test case and a
    checklist item can carry the same label."""

    @pytest.fixture
    def shared_id(self, client, request):
        pid = _db.upsert_project(f"iso-dup-{request.node.name}")
        _seed(pid, "the test case", tc_id="X_001", cl_id="X_001",
              cl_objective="the checklist item")
        _activate(client, pid, "dup")
        return {"pid": pid, "run_id": _start(client)}

    def test_one_verdict_closes_one_item(self, client, shared_id):
        run_id = shared_id["run_id"]
        client.post(f"/test-execution/manual/{run_id}/verdict",
                    data={"external_id": "X_001", "kind": "test_case",
                          "verdict": "Passed"})
        run = _db.get_execution_run(run_id)
        queue = mr.restore_queue(
            (run["env_payload"] or {})["manual_queue"],
            [TestCase(**t) for t in _db.load_test_cases(shared_id["pid"])],
            [ChecklistItem(**c) for c in _db.load_checklist(shared_id["pid"])])
        progress = mr.compute_progress(queue, _db.list_case_results(run_id))
        # Measured before the fix: done=2, finished=True — a run reporting
        # itself complete with half its items never looked at.
        assert (progress.done, progress.total) == (1, 2)
        assert not progress.finished

    def test_each_kind_records_its_own_verdict(self, client, shared_id):
        run_id = shared_id["run_id"]
        for kind, verdict in (("test_case", "Passed"), ("checklist", "Failed")):
            client.post(f"/test-execution/manual/{run_id}/verdict",
                        data={"external_id": "X_001", "kind": kind,
                              "verdict": verdict})
        rows = {(r["case_kind"], r["status"])
                for r in _db.list_case_results(run_id)}
        assert rows == {("test_case", "Passed"), ("checklist", "Failed")}

    def test_correcting_one_does_not_overwrite_the_other(self, client,
                                                        shared_id):
        run_id = shared_id["run_id"]
        for kind in ("test_case", "checklist"):
            client.post(f"/test-execution/manual/{run_id}/verdict",
                        data={"external_id": "X_001", "kind": kind,
                              "verdict": "Passed"})
        client.post(f"/test-execution/manual/{run_id}/verdict",
                    data={"external_id": "X_001", "kind": "checklist",
                          "verdict": "Blocked"})
        rows = {r["case_kind"]: r["status"]
                for r in _db.list_case_results(run_id)}
        assert rows == {"test_case": "Passed", "checklist": "Blocked"}

    def test_the_progress_table_shows_each_kinds_own_status(self, client,
                                                           shared_id):
        run_id = shared_id["run_id"]
        client.post(f"/test-execution/manual/{run_id}/verdict",
                    data={"external_id": "X_001", "kind": "checklist",
                          "verdict": "Blocked"})
        page = client.get(
            f"/test-execution/manual/{run_id}").get_data(as_text=True)
        # One Blocked badge, not two: the template keys on (kind, id) too.
        assert page.count("badge-blocked") == 1


class TestVerdictsByItem:
    def test_keyed_by_kind_and_id(self):
        out = mr.verdicts_by_item([
            {"case_external_id": "X_001", "case_kind": "test_case",
             "status": "Passed"},
            {"case_external_id": "X_001", "case_kind": "checklist",
             "status": "Failed"},
        ])
        assert out[("test_case", "X_001")]["status"] == "Passed"
        assert out[("checklist", "X_001")]["status"] == "Failed"

    def test_a_row_with_no_kind_reads_as_a_test_case(self):
        # Historic rows always carried a kind, since save_case_result has
        # always defaulted it; a row with none is read the way it was
        # written rather than dropped.
        out = mr.verdicts_by_item([
            {"case_external_id": "X_001", "status": "Passed"}])
        assert ("test_case", "X_001") in out

    def test_last_write_wins_within_one_kind(self):
        out = mr.verdicts_by_item([
            {"case_external_id": "X_001", "case_kind": "test_case",
             "status": "Passed"},
            {"case_external_id": "X_001", "case_kind": "test_case",
             "status": "Failed"},
        ])
        assert out[("test_case", "X_001")]["status"] == "Failed"


class TestUpdateCaseResultNarrowsByKind:
    def test_without_a_kind_it_still_updates(self, request):
        pid = _db.upsert_project(f"iso-upd-{request.node.name}")
        run_id = _db.start_execution_run(pid, {"mode": "manual"})
        _db.save_case_result(run_id, case_external_id="A_1",
                             case_kind="test_case", status="Passed")
        assert _db.update_case_result(run_id, "A_1", status="Failed")
        assert _db.list_case_results(run_id)[0]["status"] == "Failed"

    def test_with_a_kind_it_updates_only_that_row(self, request):
        pid = _db.upsert_project(f"iso-upd2-{request.node.name}")
        run_id = _db.start_execution_run(pid, {"mode": "manual"})
        _db.save_case_result(run_id, case_external_id="A_1",
                             case_kind="test_case", status="Passed")
        _db.save_case_result(run_id, case_external_id="A_1",
                             case_kind="checklist", status="Passed")
        assert _db.update_case_result(run_id, "A_1", case_kind="checklist",
                                     status="Blocked")
        rows = {r["case_kind"]: r["status"]
                for r in _db.list_case_results(run_id)}
        assert rows == {"test_case": "Passed", "checklist": "Blocked"}

    def test_an_absent_kind_updates_nothing(self, request):
        pid = _db.upsert_project(f"iso-upd3-{request.node.name}")
        run_id = _db.start_execution_run(pid, {"mode": "manual"})
        _db.save_case_result(run_id, case_external_id="A_1",
                             case_kind="test_case", status="Passed")
        assert not _db.update_case_result(run_id, "A_1", case_kind="checklist",
                                         status="Blocked")


class TestAnInterruptedWalkCanBeFound:
    def test_open_runs_are_listed_for_the_project(self, client, two_projects):
        rows = _db.list_open_runs(two_projects["a"], mode="manual")
        assert [r["id"] for r in rows] == [two_projects["run_id"]]

    def test_a_closed_run_is_not_listed(self, client, two_projects):
        _db.finish_execution_run(two_projects["run_id"], status="completed")
        assert _db.list_open_runs(two_projects["a"], mode="manual") == []

    def test_another_projects_open_run_is_not_listed(self, client,
                                                    two_projects):
        assert _db.list_open_runs(two_projects["b"], mode="manual") == []

    def test_the_mode_filter_excludes_other_run_kinds(self, client,
                                                     two_projects):
        _db.start_execution_run(two_projects["a"], {"mode": "live"})
        rows = _db.list_open_runs(two_projects["a"], mode="manual")
        assert [r["id"] for r in rows] == [two_projects["run_id"]]
        assert len(_db.list_open_runs(two_projects["a"])) == 2

    def test_the_execution_page_offers_to_resume(self, client, two_projects):
        page = client.get("/test-execution").get_data(as_text=True)
        assert f"/manual/{two_projects['run_id']}/resume" in page

    def test_resume_lands_on_the_first_item_without_a_verdict(
            self, client, two_projects):
        run_id = two_projects["run_id"]
        client.post(f"/test-execution/manual/{run_id}/verdict",
                    data={"external_id": "TC_001", "kind": "test_case",
                          "verdict": "Passed"})
        resp = client.get(f"/test-execution/manual/{run_id}/resume")
        assert resp.status_code == 302
        assert "i=1" in resp.headers["Location"]

    def test_resume_on_a_finished_walk_lands_on_the_last_item(
            self, client, two_projects):
        run_id = two_projects["run_id"]
        for ext, kind in (("TC_001", "test_case"), ("HDR_001", "checklist")):
            client.post(f"/test-execution/manual/{run_id}/verdict",
                        data={"external_id": ext, "kind": kind,
                              "verdict": "Passed"})
        # Clamped rather than pointing one past the end, which would render
        # an empty walk and read as data loss.
        resp = client.get(f"/test-execution/manual/{run_id}/resume")
        assert "i=1" in resp.headers["Location"]

    def test_resume_is_scoped_like_the_page(self, client, two_projects):
        _activate(client, two_projects["b"], "B")
        assert client.get(
            f"/test-execution/manual/{two_projects['run_id']}/resume"
        ).status_code == 404


class TestAnItemDeletedMidWalk:
    """Somebody regenerates the pack while a walk is open. The run keeps its
    original total — shortening it would overstate coverage — so the item
    stays in the queue with nothing behind it."""

    @pytest.fixture
    def emptied(self, client, request):
        pid = _db.upsert_project(f"iso-gone-{request.node.name}")
        _seed(pid, "the original test case")
        _activate(client, pid, "gone")
        run_id = _start(client)
        # Regenerate the pack without the test case.
        _db.save_test_cases(pid, [])
        return {"pid": pid, "run_id": run_id}

    def test_the_item_is_marked_missing(self, client, emptied):
        run = _db.get_execution_run(emptied["run_id"])
        queue = mr.restore_queue(
            (run["env_payload"] or {})["manual_queue"], [],
            [ChecklistItem(**c) for c in _db.load_checklist(emptied["pid"])])
        gone = [q for q in queue if q.missing]
        assert [q.external_id for q in gone] == ["TC_001"]

    def test_the_walk_says_why_the_item_is_blank(self, client, emptied):
        # Measured in the browser: rendered as an ordinary item this is a
        # blank card with five verdict buttons and no explanation. The
        # placeholder was unit-tested and invisible on screen.
        page = client.get(
            f"/test-execution/manual/{emptied['run_id']}?i=0"
        ).get_data(as_text=True)
        assert "no longer in the pack" in page
        assert "Skipped" in page

    def test_the_run_keeps_its_original_total(self, client, emptied):
        run = _db.get_execution_run(emptied["run_id"])
        queue = mr.restore_queue(
            (run["env_payload"] or {})["manual_queue"], [],
            [ChecklistItem(**c) for c in _db.load_checklist(emptied["pid"])])
        assert len(queue) == 2


class TestAnItemWithNothingInIt:
    """The editors' "add" button writes an empty row for the author to fill.
    A walk that starts first renders a card with an id and nothing else."""

    def test_an_empty_item_is_not_reported_as_missing(self):
        item = mr.QueueItem(external_id="TC_001", kind="test_case")
        assert item.empty and not item.missing

    def test_a_missing_item_is_not_reported_as_empty(self):
        item = mr.QueueItem(external_id="TC_001", kind="test_case",
                            missing=True, summary="(gone)")
        assert item.missing and not item.empty

    def test_an_item_with_only_steps_is_judgeable(self):
        item = mr.QueueItem(external_id="TC_001", kind="test_case",
                            steps=["Open the page"])
        assert not item.empty

    def test_a_checklist_objective_alone_is_enough(self):
        item = mr.QueueItem(external_id="HDR_001", kind="checklist",
                            summary="Verify that the logo is displayed")
        assert not item.empty

    def test_the_walk_says_so_and_points_at_the_fix(self, client, request):
        pid = _db.upsert_project(f"iso-empty-{request.node.name}")
        _db.save_test_cases(pid, [tc_to_dict(TestCase(
            id="TC_001", section="", section_num=1, summary="",
            preconditions="", test_steps="", test_data="",
            expected_result="", priority="", category=""))])
        _db.save_checklist(pid, [])
        _activate(client, pid, "empty")
        run_id = _start(client)
        page = client.get(
            f"/test-execution/manual/{run_id}?i=0").get_data(as_text=True)
        assert "no steps and no expected result" in page
        assert "no longer in the pack" not in page


class TestAssignment:
    def test_a_run_records_who_it_belongs_to_when_auth_is_off(
            self, auth_off, client, two_projects):
        # With authentication off there is no user to attach, so the
        # machine-readable field stays empty and the free-text tester name
        # is what the run carries. Asserted so the field's absence is a
        # decision rather than an accident.
        run = _db.get_execution_run(two_projects["run_id"])
        payload = run["env_payload"]
        assert payload.get("assignee_id") == ""
        assert payload.get("tester") == "alice"
