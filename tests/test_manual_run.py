"""The step-by-step manual execution walk.

Covers ``engine.manual_run`` (queue building, progress derivation, verdict
vocabulary) and the ``/test-execution/manual/*`` routes.

Two properties most of these defend:

* **The walk is resumable.** The cursor is derived from the results in the
  database, never from the session — a manual pass through 60 checks spans
  laptop sleeps, browser restarts and hand-offs to a colleague on another
  machine, and a session-backed cursor loses the walk in all three.
* **A skip is not a pass.** ``Skipped`` means nobody looked. It is
  excluded from the pass rate, exactly as the automation ingest excludes
  its own skips, because counting it either way misreports the run.
"""
from __future__ import annotations

import re

import pytest

from engine import db as _db
from engine import manual_run as mr
from engine.testcase_generator import ChecklistItem, TestCase


def _tc(**kw) -> TestCase:
    base = dict(
        id="SC1_001", section="Careers", section_num=1,
        summary="Verify that User can submit the form",
        preconditions="The form is opened",
        test_steps="1. Go to the site: https://x.test/\n"
                   "2. Click the [Send] button",
        test_data="Email: a@b.test",
        expected_result="A success message is displayed",
        priority="High", category="Positive")
    base.update(kw)
    return TestCase(**base)


def _cl(**kw) -> ChecklistItem:
    base = dict(
        id="HDR_001", section="Header",
        objective="Verify that the Homepage is opened after clicking the logo",
        item_num="1.1", priority="High", category="Positive")
    base.update(kw)
    return ChecklistItem(**base)


# ── Queue ────────────────────────────────────────────────────────────

class TestQueue:
    def test_test_cases_and_checklist_flatten_into_one_walk(self):
        q = mr.build_queue([_tc()], [_cl()])
        assert [(i.kind, i.external_id) for i in q] == \
            [("test_case", "SC1_001"), ("checklist", "HDR_001")]

    def test_steps_are_split_and_unnumbered(self):
        q = mr.build_queue([_tc()], [])
        assert q[0].steps == ["Go to the site: https://x.test/",
                              "Click the [Send] button"]

    def test_single_line_numbered_steps_are_split(self):
        assert mr.split_steps("1. Go to https://x/ 2. Click Send") == \
            ["Go to https://x/", "Click Send"]

    def test_checklist_objective_is_both_summary_and_expectation(self):
        # A checklist row IS the observation — it has no steps and no
        # separate expected result.
        item = mr.build_queue([], [_cl()])[0]
        assert item.steps == []
        assert item.expected_result == item.summary

    def test_selection_filters_the_walk(self):
        q = mr.build_queue([_tc(), _tc(id="SC1_002")], [_cl()],
                           selected=["SC1_002", "HDR_001"])
        assert [i.external_id for i in q] == ["SC1_002", "HDR_001"]

    def test_empty_selection_means_everything(self):
        assert len(mr.build_queue([_tc()], [_cl()], selected=[])) == 2

    def test_payload_stores_identity_only(self):
        # Content is re-read on every render, so an item edited mid-walk
        # shows its current text rather than a copy frozen at start.
        payload = mr.queue_to_payload(mr.build_queue([_tc()], []))
        assert payload == [{"external_id": "SC1_001", "kind": "test_case"}]

    def test_restore_reads_current_content(self):
        payload = mr.queue_to_payload(mr.build_queue([_tc()], []))
        edited = _tc(summary="Verify that User can submit the amended form")
        restored = mr.restore_queue(payload, [edited], [])
        assert restored[0].summary.endswith("amended form")

    def test_deleted_item_becomes_a_placeholder_not_a_gap(self):
        # Silently shortening the walk would make the run's own totals
        # stop adding up.
        restored = mr.restore_queue(
            [{"kind": "test_case", "external_id": "GONE"}], [_tc()], [])
        assert len(restored) == 1
        assert "no longer in the pack" in restored[0].summary


# ── Progress ─────────────────────────────────────────────────────────

class TestProgress:
    def test_fresh_walk_starts_at_zero(self):
        p = mr.compute_progress(mr.build_queue([_tc()], [_cl()]), [])
        assert (p.total, p.done, p.cursor, p.percent) == (2, 0, 0, 0)
        assert not p.finished

    def test_cursor_points_at_the_first_unjudged_item(self):
        q = mr.build_queue([_tc(), _tc(id="SC1_002")], [_cl()])
        p = mr.compute_progress(
            q, [{"case_external_id": "SC1_001", "status": "Passed"}])
        assert p.cursor == 1
        assert p.done == 1

    def test_out_of_order_verdicts_still_resolve_the_cursor(self):
        # A tester who jumped ahead leaves a hole; the cursor is the first
        # hole, not the count.
        q = mr.build_queue([_tc(), _tc(id="SC1_002"), _tc(id="SC1_003")], [])
        p = mr.compute_progress(
            q, [{"case_external_id": "SC1_003", "status": "Passed"}])
        assert p.cursor == 0 and p.done == 1

    def test_last_write_wins_on_a_correction(self):
        q = mr.build_queue([_tc()], [])
        p = mr.compute_progress(q, [
            {"case_external_id": "SC1_001", "status": "Failed"},
            {"case_external_id": "SC1_001", "status": "Passed"},
        ])
        assert p.counts == {"Passed": 1}
        assert p.done == 1

    def test_skip_is_excluded_from_the_pass_rate(self):
        # THE property. One passed of one executed is 100%; the skip is
        # neither a pass nobody earned nor a failure nobody caused.
        q = mr.build_queue([_tc(), _tc(id="SC1_002")], [])
        p = mr.compute_progress(q, [
            {"case_external_id": "SC1_001", "status": "Passed"},
            {"case_external_id": "SC1_002", "status": "Skipped"},
        ])
        assert p.finished
        assert p.executed == 1
        assert p.pass_rate == 100.0

    def test_all_skipped_has_no_pass_rate_rather_than_a_perfect_one(self):
        q = mr.build_queue([_tc()], [])
        p = mr.compute_progress(
            q, [{"case_external_id": "SC1_001", "status": "Skipped"}])
        assert p.executed == 0 and p.pass_rate == 0.0

    def test_passed_but_counts_as_a_pass(self):
        q = mr.build_queue([_tc()], [])
        p = mr.compute_progress(
            q, [{"case_external_id": "SC1_001", "status": "Passed but"}])
        assert p.pass_rate == 100.0

    def test_blocked_counts_as_executed_but_not_passed(self):
        # "Blocked" means it was attempted and could not run — a real
        # signal about the build, unlike "Skipped".
        q = mr.build_queue([_tc()], [])
        p = mr.compute_progress(
            q, [{"case_external_id": "SC1_001", "status": "Blocked"}])
        assert p.executed == 1 and p.pass_rate == 0.0

    def test_run_stats_shape(self):
        q = mr.build_queue([_tc(), _tc(id="SC1_002")], [])
        p = mr.compute_progress(q, [
            {"case_external_id": "SC1_001", "status": "Failed"},
            {"case_external_id": "SC1_002", "status": "Skipped"},
        ])
        stats = mr.run_stats(p)
        assert stats["mode"] == "manual"
        assert stats["total"] == 2 and stats["executed"] == 1
        assert stats["failed"] == 1 and stats["skipped"] == 1


class TestVerdictVocabulary:
    @pytest.mark.parametrize("raw,want", [
        ("Passed", "Passed"), ("passed", "Passed"),
        ("passed but", "Passed but"), ("FAILED", "Failed"),
        ("Blocked", "Blocked"), ("skipped", "Skipped"),
    ])
    def test_coerce(self, raw, want):
        assert mr.coerce_verdict(raw) == want

    @pytest.mark.parametrize("raw", ["", None, "nonsense", "OK", "green"])
    def test_unknown_verdict_is_refused(self, raw):
        assert mr.coerce_verdict(raw) == ""

    def test_only_defect_verdicts_offer_a_bug(self):
        assert set(mr.DEFECT_VERDICTS) == {"Failed", "Passed but"}


# ── Routes ───────────────────────────────────────────────────────────

@pytest.fixture()
def walk(client, request):
    """A project with two items and an open manual run.

    The project name is unique per test: ``upsert_project`` keys on the
    name, so a shared one let bugs filed by one test leak into the next
    and made "no bug unless asked" pass for the wrong reason.
    """
    from routes._shared import cl_to_dict, tc_to_dict, SERVER_START_TIME
    pid = _db.upsert_project(f"manual-walk-{request.node.name}")
    _db.save_test_cases(pid, [tc_to_dict(_tc())])
    _db.save_checklist(pid, [cl_to_dict(_cl())])
    with client.session_transaction() as sess:
        sess["_session_active_since"] = SERVER_START_TIME
        sess["project_id"] = pid
        sess["project_setup"] = {"project_name": "walk"}
        sess.pop("test_cases_data", None)
        sess.pop("checklist_data", None)
    resp = client.post("/test-execution/manual/start",
                       data={"run_mode": "manual"})
    run_id = int(re.search(r"/manual/(\d+)", resp.headers["Location"]).group(1))
    return {"pid": pid, "run_id": run_id}


class TestModeRouting:
    def test_manual_mode_hands_the_post_over_preserving_the_body(self, client):
        # 307, not 302: the method AND the body have to survive, or the
        # selection and the environment are lost and the mode breaks
        # without JavaScript.
        resp = client.post("/test-execution", data={"run_mode": "manual"})
        assert resp.status_code == 307
        assert resp.headers["Location"].endswith(
            "/test-execution/manual/start")

    def test_execution_page_offers_all_four_modes(self, client, walk):
        # The config form only renders once the project has a pack — an
        # empty project has nothing to choose a mode FOR.
        body = client.get("/test-execution").get_data(as_text=True)
        assert 'value="tc_driven"' in body
        assert 'value="manual"' in body
        assert 'value="walkthrough"' in body
        # The TS suite is a link, not a radio — an option that silently
        # did nothing here would be worse than saying where it lives.
        assert "/automation" in body

    def test_start_without_a_project_says_so(self, client):
        with client.session_transaction() as sess:
            sess.pop("project_id", None)
        resp = client.post("/test-execution/manual/start",
                           data={"run_mode": "manual"},
                           follow_redirects=True)
        assert b"project" in resp.data.lower()


class TestWalkPage:
    def test_first_item_is_shown_with_everything_needed_to_judge_it(
            self, client, walk):
        body = client.get(
            f"/test-execution/manual/{walk['run_id']}").get_data(as_text=True)
        assert "SC1_001" in body
        assert "The form is opened" in body            # preconditions
        assert "Click the [Send] button" in body       # steps, unnumbered
        assert "A success message is displayed" in body  # expected
        assert "Email: a@b.test" in body               # test data

    def test_every_verdict_is_one_click(self, client, walk):
        body = client.get(
            f"/test-execution/manual/{walk['run_id']}").get_data(as_text=True)
        for verdict in mr.VERDICTS:
            assert f'value="{verdict}"' in body

    def test_checklist_item_does_not_pretend_to_have_steps(self, client,
                                                           walk):
        body = client.get(
            f"/test-execution/manual/{walk['run_id']}?i=1").get_data(
                as_text=True)
        assert "the objective above is the whole check" in body

    def test_index_out_of_range_is_clamped_not_404(self, client, walk):
        resp = client.get(f"/test-execution/manual/{walk['run_id']}?i=999")
        assert resp.status_code == 200

    def test_non_manual_run_is_404(self, client, walk):
        other = _db.start_execution_run(walk["pid"], {"mode": "tc_driven"})
        assert client.get(
            f"/test-execution/manual/{other}").status_code == 404

    def test_unknown_run_is_404(self, client):
        assert client.get("/test-execution/manual/999999").status_code == 404


class TestVerdicts:
    def test_recording_advances_to_the_next_unjudged_item(self, client, walk):
        resp = client.post(
            f"/test-execution/manual/{walk['run_id']}/verdict",
            data={"external_id": "SC1_001", "verdict": "Passed"})
        assert resp.headers["Location"].endswith("i=1")

    def test_verdict_is_persisted(self, client, walk):
        client.post(f"/test-execution/manual/{walk['run_id']}/verdict",
                    data={"external_id": "SC1_001", "verdict": "Blocked",
                          "notes": "staging was down"})
        rows = _db.list_case_results(walk["run_id"])
        assert len(rows) == 1
        assert rows[0]["status"] == "Blocked"
        assert rows[0]["notes"] == "staging was down"
        assert rows[0]["case_kind"] == "test_case"

    def test_correction_overwrites_rather_than_duplicating(self, client,
                                                           walk):
        # A second row for the same item would double-count in the totals.
        for verdict in ("Failed", "Passed"):
            client.post(f"/test-execution/manual/{walk['run_id']}/verdict",
                        data={"external_id": "SC1_001", "verdict": verdict})
        rows = _db.list_case_results(walk["run_id"])
        assert len(rows) == 1
        assert rows[0]["status"] == "Passed"

    def test_unknown_verdict_is_refused_without_writing(self, client, walk):
        client.post(f"/test-execution/manual/{walk['run_id']}/verdict",
                    data={"external_id": "SC1_001", "verdict": "green"})
        assert _db.list_case_results(walk["run_id"]) == []

    def test_unknown_item_is_refused_without_writing(self, client, walk):
        client.post(f"/test-execution/manual/{walk['run_id']}/verdict",
                    data={"external_id": "NOPE", "verdict": "Passed"})
        assert _db.list_case_results(walk["run_id"]) == []

    def test_walk_survives_a_lost_session(self, client, walk):
        """The cursor lives in the database, so a new browser resumes it.

        This is why the position is derived from the results rather than
        kept in the session: a 60-check walk spans laptop sleeps and
        hand-offs, and a session-backed cursor loses all three.
        """
        client.post(f"/test-execution/manual/{walk['run_id']}/verdict",
                    data={"external_id": "SC1_001", "verdict": "Passed"})
        with client.session_transaction() as sess:
            sess.clear()
        body = client.get(
            f"/test-execution/manual/{walk['run_id']}").get_data(as_text=True)
        assert "1 / 2" in body


class TestBugFiling:
    def test_failed_item_can_file_a_bug_carrying_the_testers_words(
            self, client, walk):
        client.post(f"/test-execution/manual/{walk['run_id']}/verdict",
                    data={"external_id": "SC1_001", "verdict": "Failed",
                          "notes": "The form returned HTTP 500",
                          "file_bug": "1"})
        bugs = _db.list_bugs(walk["pid"])
        assert len(bugs) == 1
        # The tester's own words ARE the actual result — deriving one from
        # the expected result would put words in their mouth.
        assert bugs[0]["actual_result"] == "The form returned HTTP 500"
        assert bugs[0]["expected_result"] == "A success message is displayed"
        assert "Go to the site" in bugs[0]["steps_to_reproduce"]

    def test_the_bug_is_linked_to_the_result_row(self, client, walk):
        client.post(f"/test-execution/manual/{walk['run_id']}/verdict",
                    data={"external_id": "SC1_001", "verdict": "Failed",
                          "notes": "broken", "file_bug": "1"})
        row = _db.list_case_results(walk["run_id"])[0]
        assert row["bug_report_id"]

    def test_no_bug_unless_asked(self, client, walk):
        client.post(f"/test-execution/manual/{walk['run_id']}/verdict",
                    data={"external_id": "SC1_001", "verdict": "Failed",
                          "notes": "broken"})
        assert _db.list_bugs(walk["pid"]) == []

    def test_a_pass_never_files_a_bug_even_if_asked(self, client, walk):
        client.post(f"/test-execution/manual/{walk['run_id']}/verdict",
                    data={"external_id": "SC1_001", "verdict": "Passed",
                          "file_bug": "1"})
        assert _db.list_bugs(walk["pid"]) == []

    def test_a_verdict_survives_a_failed_bug_filing(self, client, walk,
                                                    monkeypatch):
        # Losing the verdict would be the more expensive thing to re-do.
        def _boom(*a, **kw):
            raise RuntimeError("db down")
        monkeypatch.setattr(_db, "save_bug", _boom)
        client.post(f"/test-execution/manual/{walk['run_id']}/verdict",
                    data={"external_id": "SC1_001", "verdict": "Failed",
                          "notes": "broken", "file_bug": "1"})
        rows = _db.list_case_results(walk["run_id"])
        assert rows and rows[0]["status"] == "Failed"


class TestFinish:
    def test_finishing_a_complete_walk_records_completed(self, client, walk):
        for ext, verdict in (("SC1_001", "Passed"), ("HDR_001", "Failed")):
            client.post(f"/test-execution/manual/{walk['run_id']}/verdict",
                        data={"external_id": ext, "verdict": verdict})
        client.post(f"/test-execution/manual/{walk['run_id']}/finish")
        run = _db.get_execution_run(walk["run_id"])
        assert run["status"] == "completed"
        assert run["stats"]["passed"] == 1 and run["stats"]["failed"] == 1
        assert run["stats"]["pass_rate"] == 50.0

    def test_closing_early_is_recorded_as_partial(self, client, walk):
        # A partial run reported as complete would overstate coverage.
        client.post(f"/test-execution/manual/{walk['run_id']}/verdict",
                    data={"external_id": "SC1_001", "verdict": "Passed"})
        client.post(f"/test-execution/manual/{walk['run_id']}/finish")
        run = _db.get_execution_run(walk["run_id"])
        assert run["status"] == "partial"
        assert run["stats"]["total"] == 2
        assert run["stats"]["passed"] == 1

    def test_finish_page_flashes_the_shortfall(self, client, walk):
        resp = client.post(f"/test-execution/manual/{walk['run_id']}/finish",
                           follow_redirects=True)
        assert b"0 of 2" in resp.data

    def test_completed_walk_shows_the_close_prompt(self, client, walk):
        for ext in ("SC1_001", "HDR_001"):
            client.post(f"/test-execution/manual/{walk['run_id']}/verdict",
                        data={"external_id": ext, "verdict": "Passed"})
        body = client.get(
            f"/test-execution/manual/{walk['run_id']}").get_data(as_text=True)
        assert "Every item has a verdict" in body
