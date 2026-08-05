"""
End-to-end for both execution paths (E5.7).

``test_pipeline_e2e.py`` walks the generation chain and touches each module
in the joins. This file does the other axis: the two ways a run actually
happens, each followed all the way to the number a person reads afterwards.

    manual:      start → walk every item → one Failed files a bug →
                 finish → the run's own stats, the bug, the Runs page
    automation:  generate the suite → a CI job posts allure-results to the
                 token-authenticated ingest → the run, its metrics, its
                 history page

The assertions are deliberately about **reported outcomes**, not about
endpoints returning 200. Every defect the E5.1 audit found returned 200 and
rendered a page that looked right; a journey test that only checks status
codes would have passed throughout.

No browsers are started here. The automation path's real Playwright pass
happens in GitHub Actions (E5.2′) and its contract is asserted in
``test_ci_playwright_workflow.py`` — what this file owns is the half that
runs in the app: what the app does with the results when they arrive.
"""
from __future__ import annotations

import io
import json
import re
import zipfile

import pytest

from engine import db as _db
from engine.testcase_generator import ChecklistItem, TestCase
from routes._shared import SERVER_START_TIME, cl_to_dict, tc_to_dict


# ── Fixtures ──────────────────────────────────────────────────────────

def _cases():
    return [
        TestCase(id="SC1_001", section="Careers", section_num=1,
                 summary="Verify that User can submit the application form",
                 preconditions="The Careers page is opened",
                 test_steps="1. Go to the site: https://x.test/careers\n"
                            "2. Fill in the \"Email\" entry field with a valid email\n"
                            "3. Click on the \"Send\" button",
                 test_data="Email: qa.tester@example.com",
                 expected_result="A success message is displayed",
                 priority="High", category="Positive"),
        TestCase(id="SC1_002", section="Careers", section_num=1,
                 summary="Verify that User cannot submit the form with an empty email",
                 preconditions="The Careers page is opened",
                 test_steps="1. Go to the site: https://x.test/careers\n"
                            "2. Leave the \"Email\" entry field empty\n"
                            "3. Click on the \"Send\" button",
                 test_data="",
                 expected_result="The form is not submitted and an error message "
                                 "is displayed below the field",
                 priority="High", category="Negative"),
    ]


def _checklist():
    return [
        ChecklistItem(id="HDR_001", section="Header",
                      objective="Verify that the logo opens the Homepage",
                      item_num="1.1", priority="High", category="Positive"),
    ]


@pytest.fixture
def project(client, request):
    pid = _db.upsert_project(f"e5e2e-{request.node.name}")
    _db.save_test_cases(pid, [tc_to_dict(c) for c in _cases()])
    _db.save_checklist(pid, [cl_to_dict(c) for c in _checklist()])
    with client.session_transaction() as sess:
        sess["_session_active_since"] = SERVER_START_TIME
        sess["project_id"] = pid
        sess["project_setup"] = {"project_name": "e5-e2e"}
        sess.pop("test_cases_data", None)
        sess.pop("checklist_data", None)
    return pid


# ── The manual path ───────────────────────────────────────────────────

class TestTheManualJourney:
    """One walk, from the Execute page to the numbers it produced."""

    @pytest.fixture
    def walked(self, client, project):
        resp = client.post("/test-execution/manual/start",
                           data={"run_mode": "manual", "tester": "alice",
                                 "env_custom": "staging"})
        run_id = int(re.search(r"/manual/(\d+)",
                               resp.headers["Location"]).group(1))

        verdicts = [
            ("SC1_001", "test_case", "Passed", "", None),
            # The failure files a bug, with the tester's own words as the
            # actual result.
            ("SC1_002", "test_case", "Failed",
             "The form submitted with an empty email and returned HTTP 500",
             "1"),
            ("HDR_001", "checklist", "Skipped", "", None),
        ]
        for ext, kind, verdict, notes, file_bug in verdicts:
            data = {"external_id": ext, "kind": kind, "verdict": verdict,
                    "notes": notes}
            if file_bug:
                data["file_bug"] = file_bug
            client.post(f"/test-execution/manual/{run_id}/verdict", data=data)
        return {"run_id": run_id, "pid": project}

    def test_the_walk_advances_to_the_next_unjudged_item(self, client, project):
        resp = client.post("/test-execution/manual/start",
                           data={"run_mode": "manual"})
        run_id = int(re.search(r"/manual/(\d+)",
                               resp.headers["Location"]).group(1))
        after = client.post(f"/test-execution/manual/{run_id}/verdict",
                            data={"external_id": "SC1_001",
                                  "kind": "test_case", "verdict": "Passed"})
        # Not back to the start: correcting an earlier item must return the
        # tester to where they were, not restart the walk.
        assert "i=1" in after.headers["Location"]

    def test_every_verdict_is_recorded_once(self, client, walked):
        rows = _db.list_case_results(walked["run_id"])
        assert len(rows) == 3
        assert {(r["case_external_id"], r["case_kind"], r["status"])
                for r in rows} == {
            ("SC1_001", "test_case", "Passed"),
            ("SC1_002", "test_case", "Failed"),
            ("HDR_001", "checklist", "Skipped"),
        }

    def test_the_failure_filed_a_bug_carrying_the_testers_words(self, client,
                                                                walked):
        bugs = _db.list_bugs(walked["pid"])
        assert len(bugs) == 1
        bug = bugs[0]
        assert "HTTP 500" in (bug.get("actual_result") or "")
        # Not the test case's own title: every objective opens "Verify
        # that…", and a defect store where each headline is an instruction
        # tells the reader nothing about what broke.
        assert not (bug.get("title") or "").startswith("Verify that")
        assert (bug.get("environment") or "") == "staging"

    def test_the_bug_links_back_to_the_run_and_the_item(self, client, walked):
        bug = _db.list_bugs(walked["pid"])[0]
        extra = bug.get("extra") or {}
        assert extra.get("manual_run_id") == walked["run_id"]
        assert extra.get("case_external_id") == "SC1_002"

    def test_the_verdict_row_points_at_the_bug(self, client, walked):
        rows = {r["case_external_id"]: r
                for r in _db.list_case_results(walked["run_id"])}
        assert rows["SC1_002"]["bug_report_id"] == \
            _db.list_bugs(walked["pid"])[0]["id"]

    def test_finishing_reports_the_walk_honestly(self, client, walked):
        client.post(f"/test-execution/manual/{walked['run_id']}/finish")
        run = _db.get_execution_run(walked["run_id"])
        stats = run["stats"] or {}
        assert run["status"] == "completed"
        assert stats["total"] == 3
        # A skip is not a pass and not a failure: two items were exercised,
        # one of them passed.
        assert stats["executed"] == 2
        assert stats["skipped"] == 1
        assert stats["pass_rate"] == 50.0

    def test_a_walk_closed_early_says_so(self, client, project):
        resp = client.post("/test-execution/manual/start",
                           data={"run_mode": "manual"})
        run_id = int(re.search(r"/manual/(\d+)",
                               resp.headers["Location"]).group(1))
        client.post(f"/test-execution/manual/{run_id}/verdict",
                    data={"external_id": "SC1_001", "kind": "test_case",
                          "verdict": "Passed"})
        client.post(f"/test-execution/manual/{run_id}/finish")
        run = _db.get_execution_run(run_id)
        # "partial", not "completed": a partial run reported as complete
        # overstates coverage, which is the number people act on.
        assert run["status"] == "partial"
        assert run["stats"]["total"] == 3
        assert run["stats"]["executed"] == 1

    def test_the_finished_run_leaves_the_open_list(self, client, walked):
        assert _db.list_open_runs(walked["pid"], mode="manual")
        client.post(f"/test-execution/manual/{walked['run_id']}/finish")
        assert not _db.list_open_runs(walked["pid"], mode="manual")

    def test_the_runs_page_shows_it_as_reviewable(self, client, walked):
        client.post(f"/test-execution/manual/{walked['run_id']}/finish")
        page = client.get("/test-execution/runs").get_data(as_text=True)
        assert f"#{walked['run_id']}" in page
        assert "Review" in page

    def test_the_walk_is_resumable_after_the_browser_is_lost(self, client,
                                                             walked):
        with client.session_transaction() as sess:
            sess.clear()
            sess["_session_active_since"] = SERVER_START_TIME
        body = client.get(
            f"/test-execution/manual/{walked['run_id']}").get_data(as_text=True)
        # The cursor is derived from the results, so a new browser lands on
        # the same walk with the same progress.
        assert "3 / 3" in body or "3/3" in body


# ── The automation path ───────────────────────────────────────────────

def _allure_zip(results: list[dict]) -> bytes:
    """An ``allure-results`` archive of the shape allure-playwright emits."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, doc in enumerate(results):
            zf.writestr(f"{i}-result.json", json.dumps(doc))
    return buf.getvalue()


def _result(name: str, status: str, case_id: str, *, message: str = ""):
    doc = {"name": name, "status": status, "start": 0, "stop": 1200,
           "labels": [{"name": "tag", "value": f"TC-{case_id}"},
                      {"name": "suite", "value": "Careers"}],
           "steps": []}
    if message:
        doc["statusDetails"] = {"message": message}
    return doc


class TestTheAutomationJourney:
    """What the app does when a CI job posts its results back."""

    @pytest.fixture
    def targeted(self, client, project):
        """The same project with its cases marked for automation.

        `is_automation_targeted` keys on tc_format == "gherkin", so a pack
        generated for manual execution has nothing for the bundle to build
        — which is the 409 above, not a defect.
        """
        rows = _db.load_test_cases(project)
        for row in rows:
            row["tc_format"] = "gherkin"
        _db.save_test_cases(project, rows)
        return project

    @pytest.fixture
    def token(self, monkeypatch):
        monkeypatch.setenv("AUTOMATION_INGEST_TOKEN", "e2e-token")
        return "e2e-token"

    @pytest.fixture
    def archive(self):
        return _allure_zip([
            _result("Verify that User can submit the application form",
                    "passed", "SC1_001"),
            _result("Verify that User cannot submit the form with an empty email",
                    "failed", "SC1_002", message="expected an error message"),
            _result("Verify that the logo opens the Homepage", "skipped",
                    "HDR_001"),
        ])

    def test_a_manual_only_pack_explains_itself_instead_of_downloading(
            self, client, project):
        # 409 with a sentence, not an empty archive: an empty zip reads as a
        # broken download rather than as "you did not ask for BDD".
        resp = client.get("/automation/bundle.zip")
        assert resp.status_code == 409
        assert "BDD" in resp.get_data(as_text=True)

    def test_the_suite_is_generated_from_the_project(self, client, targeted):
        resp = client.get("/automation/bundle.zip")
        assert resp.status_code == 200
        with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
            names = zf.namelist()
        # The three files the CI workflow needs to exist before it installs
        # 300 MB of browsers.
        assert "package.json" in names
        assert "playwright.config.ts" in names
        assert any(n.startswith("steps/") for n in names)

    def test_the_generated_config_reports_to_allure(self, client, targeted):
        resp = client.get("/automation/bundle.zip")
        with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
            config = zf.read("playwright.config.ts").decode()
        # Without this reporter the CI job produces no allure-results and
        # the workflow's "produced nothing" guard fires.
        assert "allure-playwright" in config

    def test_a_ci_post_creates_a_run(self, client, project, token, archive):
        resp = client.post(
            "/automation/allure-results",
            data={"results": (io.BytesIO(archive), "allure-results.zip"),
                  "origin": "ci", "label": "CI run", "project_id": project},
            headers={"X-TFG-Token": token},
            content_type="multipart/form-data")
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["project_id"] == project
        # The ingest namespaces its metrics so the dashboard can hold the
        # manual and automated numbers side by side without either shadowing
        # the other.
        assert body["automation_total"] == 3
        # A skip is neither a pass nor a failure — the same rule the manual
        # walk applies to its own skips.
        assert body["automation_passed"] == 1
        assert body["automation_failed"] == 1
        assert body["automation_pass_rate"] == 50.0
        assert body["automation_skipped"] == 1

    def test_the_run_is_readable_afterwards(self, client, project, token,
                                           archive):
        resp = client.post(
            "/automation/allure-results",
            data={"results": (io.BytesIO(archive), "allure-results.zip"),
                  "project_id": project},
            headers={"X-TFG-Token": token},
            content_type="multipart/form-data")
        run_id = resp.get_json()["run_id"]
        page = client.get(f"/automation/runs/{run_id}")
        assert page.status_code == 200

    def test_the_run_is_attributed_to_ci(self, client, project, token,
                                        archive):
        resp = client.post(
            "/automation/allure-results",
            data={"results": (io.BytesIO(archive), "allure-results.zip"),
                  "project_id": project},
            headers={"X-TFG-Token": token},
            content_type="multipart/form-data")
        run = _db.get_automation_run(resp.get_json()["run_id"])
        # Inferred from the token header when the form does not say, so a
        # minimal curl still lands in the right bucket.
        assert run["origin"] == "ci"

    def test_a_bad_token_is_refused(self, client, project, token, archive):
        resp = client.post(
            "/automation/allure-results",
            data={"results": (io.BytesIO(archive), "allure-results.zip")},
            headers={"X-TFG-Token": "wrong"},
            content_type="multipart/form-data")
        assert resp.status_code == 401

    def test_ingestion_is_off_when_no_token_is_configured(self, client,
                                                          project, archive,
                                                          monkeypatch):
        monkeypatch.delenv("AUTOMATION_INGEST_TOKEN", raising=False)
        resp = client.post(
            "/automation/allure-results",
            data={"results": (io.BytesIO(archive), "allure-results.zip")},
            headers={"X-TFG-Token": "anything"},
            content_type="multipart/form-data")
        # 403 with an explanation, not a 500 and not a silent accept: an
        # open ingest endpoint would let anyone write run history into
        # somebody's project.
        assert resp.status_code == 403
        assert "AUTOMATION_INGEST_TOKEN" in resp.get_json()["message"]

    def test_an_archive_with_no_results_is_422_not_a_run(self, client,
                                                        project, token):
        empty = _allure_zip([])
        resp = client.post(
            "/automation/allure-results",
            data={"results": (io.BytesIO(empty), "allure-results.zip"),
                  "project_id": project},
            headers={"X-TFG-Token": token},
            content_type="multipart/form-data")
        # The request was well-formed and its contents were not what the
        # endpoint needs, which is what 422 says and 400 does not.
        assert resp.status_code == 422

    def test_the_workflows_own_status_codes_are_the_ones_handled(self):
        """The CI job branches on 201 / 401 / 403 / 422 by number.

        Asserted here rather than only in the workflow tests because this
        is the file that exercises the endpoint: if a code changes, the
        assertions above go red *and* this one names the workflow as the
        thing to update.
        """
        from pathlib import Path
        wf = (Path(__file__).resolve().parents[1] / ".github" / "workflows"
              / "playwright.yml").read_text(encoding="utf-8")
        for code in ("201", "401", "403", "422"):
            assert code in wf


# ── Both paths land in the same place ─────────────────────────────────

class TestBothPathsAreVisibleTogether:
    def test_a_manual_and_an_automated_run_coexist_in_the_project(
            self, client, project, monkeypatch):
        monkeypatch.setenv("AUTOMATION_INGEST_TOKEN", "e2e-token")
        resp = client.post("/test-execution/manual/start",
                           data={"run_mode": "manual"})
        manual_id = int(re.search(r"/manual/(\d+)",
                                  resp.headers["Location"]).group(1))
        client.post("/automation/allure-results",
                    data={"results": (io.BytesIO(_allure_zip([
                        _result("Verify that User can submit the application form",
                                "passed", "SC1_001")])), "r.zip"),
                          "project_id": project},
                    headers={"X-TFG-Token": "e2e-token"},
                    content_type="multipart/form-data")
        runs = _db.list_execution_runs(project)
        assert manual_id in [r["id"] for r in runs]
        # The automation ingest writes an AutomationRun, not an
        # ExecutionRun: two different lifecycles that meet in the metrics,
        # not in one table. Asserted so a future merge of the two is a
        # deliberate change rather than a surprise.
        assert len(_db.list_automation_runs(project)) == 1
