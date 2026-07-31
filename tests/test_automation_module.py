"""The Automation module — codegen out, Allure results in.

Covers ``engine.automation_codegen`` (step binding, honest coverage
reporting, the generated bundle), ``engine.allure_ingest`` (pure-Python
result parsing) and the ``/automation`` routes.

The property most of these tests defend is one decision: **a check nobody
performed must never report green.** An assertion the step library cannot
bind resolves to a skip, a skip is counted separately from a pass and a
failure, and the pass rate is computed over executed scenarios only. A
suite that passes because nothing was checked converts an unknown into a
false assurance, which is worse than having no suite.
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from engine import allure_ingest as ai
from engine import automation_codegen as cg
from engine import gherkin as gk
from engine.testcase_generator import TestCase


def _tc(**kw) -> TestCase:
    base = dict(
        id="SC1_001", section="Careers page", section_num=1,
        summary='Verify that User cannot submit the form with invalid data '
                'in the "Phone number" field',
        preconditions='The "Apply" form is opened on the '
                      'https://example.com/careers page',
        test_steps=(
            "1. Go to the site: https://example.com/careers\n"
            '2. Fill in the "Phone number" field with invalid data\n'
            '3. Mark the "I agree to the Cookie Policy" checkbox\n'
            "4. Click the [Find your role] button\n"
            "5. Pay attention to the result"
        ),
        test_data="Phone number: (32) 4512",
        expected_result="An error message is displayed",
        category="Negative", priority="High", tc_format="gherkin",
    )
    base.update(kw)
    return TestCase(**base)


def _seed(client, cases: list, *, project_id: str = "") -> None:
    from routes._shared import tc_to_dict, SERVER_START_TIME
    with client.session_transaction() as sess:
        sess["_session_active_since"] = SERVER_START_TIME
        sess["test_cases_data"] = [tc_to_dict(c) for c in cases]
        if project_id:
            sess["project_id"] = project_id
            sess["project_setup"] = {"project_name": "probe"}


# ── Step binding ─────────────────────────────────────────────────────

class TestStepBinding:
    @pytest.mark.parametrize("text", [
        "I go to the site: https://example.com/",
        "I open https://example.com/careers",
        'I click the [Find your role] button',
        'I click on the "Contact us" button',
        'I click on the "Contact us" button in the Header',
        'I expand the "Services" drop-down menu',
        'I select "Angular" from the "Technologies" drop-down',
        'I fill in the "Email" field with valid data',
        "I fill in the Name field with valid data",
        'I enter an invalid value into the "Email" field',
        'I clear the "Email" field',
        'I mark the "I agree" checkbox',
        'I unmark the "I agree" checkbox',
        'I press the "Enter" key',
        'I hover over the "Android" block',
        "I scroll the page down",
        "I scroll the page down to the Footer",
        "I use the following test data:",
        "I look at the page",
    ])
    def test_house_action_verbs_bind(self, text):
        step = gk.Step("When", text)
        setattr(step, "_resolved_kind", "action")
        assert cg.classify_step(step) is not None, text

    @pytest.mark.parametrize("text", [
        'the "Contact us" button is displayed',
        'the "Contact us" button is not displayed',
        'the "Submit" button is disabled',
        "the Homepage is opened",
        'the "Cases" page is opened',
        'the URL contains "careers"',
        "an error message is displayed",
        "no error message is displayed",
        'the text "Thank you" is displayed',
    ])
    def test_checkable_assertions_bind(self, text):
        step = gk.Step("Then", text)
        setattr(step, "_resolved_kind", "assertion")
        assert cg.classify_step(step) is not None, text

    @pytest.mark.parametrize("text", [
        "the layout matches the design",
        'the "Phone number" field is highlighted in red',
        "the section is visible and matches the design",
    ])
    def test_prose_assertions_are_left_unbound_on_purpose(self, text):
        # Guessing at these would produce a check that passes for the
        # wrong reason — the anti-pattern house_style.yaml calls inventing
        # evidence.
        step = gk.Step("Then", text)
        setattr(step, "_resolved_kind", "assertion")
        assert cg.classify_step(step) is None, text

    def test_url_carrying_precondition_binds(self):
        # The most common Given in the corpus. It names the URL, so it is
        # executable — leaving it unbound would strand most scenarios.
        step = gk.Step("Given",
                       'the "Apply" form is opened on the '
                       'https://example.com/careers page')
        setattr(step, "_resolved_kind", "precondition")
        assert cg.classify_step(step) is not None

    def test_state_precondition_stays_unbound(self):
        step = gk.Step("Given", "Employee is created")
        setattr(step, "_resolved_kind", "precondition")
        assert cg.classify_step(step) is None


# ── Coverage honesty ────────────────────────────────────────────────

class TestCoverage:
    def test_fully_bindable_case_is_fully_runnable(self):
        cov = cg.coverage_report([_tc()])
        assert cov.scenarios == 1
        assert cov.bound_steps == cov.steps
        assert cov.partly_manual_scenarios == 0
        assert cov.to_dict()["runnable_scenarios"] == 1

    def test_unbindable_assertion_makes_the_scenario_skip_not_pass(self):
        cov = cg.coverage_report([_tc(
            expected_result='The "Phone number" field is highlighted in red')])
        assert cov.partly_manual_scenarios == 1
        assert cov.to_dict()["runnable_scenarios"] == 0
        assert len(cov.manual_assertions) == 1
        assert cov.manual_assertions[0].case_id == "SC1_001"

    def test_unbindable_precondition_is_counted_separately(self):
        # The consequence differs from an unbound assertion: the scenario
        # skips BEFORE acting, because starting from the wrong state would
        # produce a result about a different situation.
        cov = cg.coverage_report([_tc(preconditions="Employee is created")])
        assert len(cov.manual_preconditions) == 1
        assert cov.manual_assertions == []
        assert cov.partly_manual_scenarios == 1

    def test_unbound_action_is_neither_of_those(self):
        # A missing action is a defect in the test case, so it fails
        # rather than skips — and it is reported in its own bucket.
        cov = cg.coverage_report([_tc(
            test_steps="1. Go to the site: https://example.com/\n"
                       "2. Do something clever with the widget")])
        assert cov.unbound_actions
        assert "SC1_001" in cov.unbound_actions[0]

    def test_bound_pct_is_reported(self):
        cov = cg.coverage_report([_tc()])
        assert cov.bound_pct == 100
        assert cg.Coverage().bound_pct == 0     # no divide-by-zero

    def test_manual_cases_are_excluded_from_the_bundle(self):
        files = cg.build_project([_tc(tc_format="manual")])
        assert not any(p.startswith("features/") for p in files)


# ── Generated bundle ─────────────────────────────────────────────────

class TestBundle:
    @pytest.fixture(scope="class")
    def files(self) -> dict:
        return cg.build_project(
            [_tc(), _tc(id="SC1_002", section="Header",
                        preconditions="", test_data="",
                        summary="Verify that the Homepage is opened after "
                                "clicking the logo",
                        test_steps='1. Go to the site: https://example.com/\n'
                                   '2. Click on the "Logo" link',
                        expected_result="The Homepage is opened")],
            base_url="https://example.com", project_name="Demo",
            locators={"Find your role": {
                "primary": 'role=button[name="Find your role"]',
                "alternates": ["data-testid=apply"]}})

    def test_every_expected_file_is_present(self, files):
        for path in ("package.json", "tsconfig.json", "playwright.config.ts",
                     "steps/locators.ts", "steps/fixtures.ts",
                     "steps/actions.ts", "steps/assertions.ts",
                     "scripts/upload-results.mjs", "locators.json",
                     ".github/workflows/automation.yml", ".gitignore",
                     "README.md", "MANUAL-ASSERTIONS.md"):
            assert path in files, path

    def test_one_feature_file_per_section(self, files):
        assert "features/careers-page.feature" in files
        assert "features/header.feature" in files

    def test_generated_features_are_valid_gherkin(self, files):
        for path, text in files.items():
            if path.startswith("features/"):
                assert gk.lint(text) == [], path

    def test_package_json_pins_the_toolchain(self, files):
        pkg = json.loads(files["package.json"])
        dev = pkg["devDependencies"]
        assert dev["@playwright/test"] == cg.PLAYWRIGHT_VERSION
        assert "playwright-bdd" in dev
        # allure-playwright is what produces allure-results; without it the
        # run reports nothing and the ingest sees an empty upload.
        assert "allure-playwright" in dev

    def test_config_wires_the_allure_reporter_and_base_url(self, files):
        cfg = files["playwright.config.ts"]
        assert "allure-playwright" in cfg
        assert "resultsDir: 'allure-results'" in cfg
        assert "https://example.com" in cfg

    def test_learned_locators_are_carried_into_the_bundle(self, files):
        loc = json.loads(files["locators.json"])
        assert loc["Find your role"]["primary"] == \
            'role=button[name="Find your role"]'
        assert "data-testid=apply" in loc["Find your role"]["alternates"]

    def test_locator_decoder_covers_the_shared_selector_format(self, files):
        ts = files["steps/locators.ts"]
        # The same symbolic format the Python runner decodes, so a selector
        # that survived there survives here.
        for prefix in ("role=", "label=", "placeholder=", "text=",
                       "data-testid=", "alt=", "title="):
            assert prefix in ts, prefix

    def test_locator_failure_names_every_candidate(self, files):
        # A message naming only the last attempt is unactionable.
        assert "Tried:" in files["steps/locators.ts"]

    def test_assertion_catch_all_skips_rather_than_passes(self, files):
        ts = files["steps/assertions.ts"]
        assert "test.skip(true," in ts
        assert "not automatable" in ts
        # The catch-all has to be LAST or it shadows every real pattern.
        assert ts.rindex("Then(/^(.*)$/") > ts.rindex("Then(/^the text")

    def test_precondition_catch_all_is_given_only(self, files):
        # An unbound WHEN must fail, not skip: a missing action is a defect
        # in the test case.
        ts = files["steps/actions.ts"]
        assert "Given(/^(.*)$/" in ts
        assert "When(/^(.*)$/" not in ts

    def test_readme_states_the_honesty_rule(self, files):
        assert "never reports green" in files["README.md"]

    def test_manual_assertions_doc_lists_by_case_id(self):
        files = cg.build_project([_tc(
            expected_result='The "Phone" field is highlighted in red')])
        doc = files["MANUAL-ASSERTIONS.md"]
        assert "SC1_001" in doc
        assert "highlighted in red" in doc

    def test_zip_contains_the_same_files(self, files):
        data = cg.bundle_zip([_tc()], base_url="https://example.com")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
        assert "package.json" in names
        assert "features/careers-page.feature" in names


# ── Allure ingest ────────────────────────────────────────────────────

def _result(name, status, case_id="", ms=1000, msg="", suite="Careers page",
            flaky=False, steps=None):
    return {
        "name": name, "status": status, "start": 0, "stop": ms,
        "labels": ([{"name": "tag", "value": f"TC-{case_id}"}] if case_id
                   else []) + [{"name": "suite", "value": suite}],
        "statusDetails": {"message": msg, "flaky": flaky},
        "steps": steps or [],
    }


def _archive(docs, prefix="allure-results/") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i, doc in enumerate(docs):
            zf.writestr(f"{prefix}{i}-result.json", json.dumps(doc))
    return buf.getvalue()


class TestAllureParsing:
    def test_statuses_are_counted_separately(self):
        s = ai.summarise([
            _result("A", "passed", "SC1_001"),
            _result("B", "failed", "SC1_002"),
            _result("C", "broken", "SC1_003"),
            _result("D", "skipped", "SC1_004"),
        ])
        assert (s.passed, s.failed, s.broken, s.skipped) == (1, 1, 1, 1)
        assert s.total == 4

    def test_pass_rate_excludes_skipped(self):
        # THE property. 1 passed of 2 executed is 50%, not 33% (which would
        # blame a skip on the product) and not 100% (which would claim a
        # check nobody performed).
        s = ai.summarise([
            _result("A", "passed", "SC1_001"),
            _result("B", "failed", "SC1_002"),
            _result("C", "skipped", "SC1_003"),
        ])
        assert s.executed == 2
        assert s.pass_rate == 50.0

    def test_all_skipped_run_has_no_pass_rate_rather_than_a_perfect_one(self):
        s = ai.summarise([_result("A", "skipped", "SC1_001")])
        assert s.executed == 0
        assert s.pass_rate == 0.0

    def test_case_id_is_recovered_from_the_tag(self):
        s = ai.summarise([_result("A", "passed", "SC1_007")])
        assert s.results[0].case_id == "SC1_007"

    def test_deepest_failed_step_is_reported(self):
        # A reporter may leave an outer step green while a nested one
        # failed; stopping at the green parent loses the only useful name.
        s = ai.summarise([_result("A", "failed", "SC1_001", steps=[{
            "name": "When I click the [Send] button", "status": "passed",
            "steps": [{"name": "expect(locator).toBeVisible()",
                       "status": "failed", "steps": []}]}])])
        assert s.results[0].failed_step == "expect(locator).toBeVisible()"

    def test_duration_sums_across_results(self):
        s = ai.summarise([_result("A", "passed", ms=1500),
                          _result("B", "passed", ms=2500)])
        assert s.duration_ms == 4000

    def test_flaky_results_are_listed(self):
        s = ai.summarise([_result("A", "passed", "SC1_001", flaky=True),
                          _result("B", "passed", "SC1_002")])
        assert s.flaky == ["A"]

    def test_by_suite_groups_counts(self):
        s = ai.summarise([_result("A", "passed", suite="Header"),
                          _result("B", "failed", suite="Header"),
                          _result("C", "passed", suite="Footer")])
        assert s.by_suite()["Header"]["passed"] == 1
        assert s.by_suite()["Header"]["failed"] == 1
        assert s.by_suite()["Footer"]["passed"] == 1

    def test_worst_status_wins_per_case(self):
        # A case that failed on one browser is not passing.
        s = ai.summarise([_result("A", "passed", "SC1_001"),
                          _result("A", "failed", "SC1_001")])
        assert ai.statuses_by_case(s) == {"SC1_001": "failed"}

    def test_unknown_status_is_not_silently_a_pass(self):
        s = ai.summarise([_result("A", "weird-status", "SC1_001")])
        assert s.results[0].status == "unknown"
        assert s.passed == 0

    def test_nameless_result_is_dropped(self):
        assert ai.parse_result({"status": "passed"}) is None
        assert ai.parse_result("not a dict") is None


class TestAllureArchive:
    @pytest.mark.parametrize("prefix", ["allure-results/", ""])
    def test_both_zip_layouts_are_accepted(self, prefix):
        # Both are what people actually produce; refusing one would be a
        # support ticket rather than a safety property.
        s = ai.parse_archive(_archive([_result("A", "passed", "SC1_001")],
                                      prefix))
        assert s.total == 1

    def test_non_result_files_are_ignored(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("allure-results/1-result.json",
                        json.dumps(_result("A", "passed")))
            zf.writestr("allure-results/1-container.json", "{}")
            zf.writestr("allure-results/screenshot.png", "not json")
        assert ai.parse_archive(buf.getvalue()).total == 1

    def test_not_a_zip_says_so(self):
        s = ai.parse_archive(b"plain text")
        assert s.total == 0
        assert any("Not a zip" in w for w in s.warnings)

    def test_empty_archive_explains_the_likely_cause(self):
        buf = io.BytesIO()
        zipfile.ZipFile(buf, "w").close()
        s = ai.parse_archive(buf.getvalue())
        assert any("allure-report" in w for w in s.warnings)

    def test_oversize_upload_is_refused(self):
        s = ai.parse_archive(b"x" * (ai.MAX_UPLOAD_BYTES + 1))
        assert any("over the" in w for w in s.warnings)

    def test_a_corrupt_member_does_not_lose_the_rest(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("allure-results/1-result.json",
                        json.dumps(_result("A", "passed")))
            zf.writestr("allure-results/2-result.json", "{not json")
        s = ai.parse_archive(buf.getvalue())
        assert s.total == 1
        assert s.warnings

    def test_metrics_shape(self):
        s = ai.summarise([_result("A", "passed"), _result("B", "skipped")])
        m = ai.to_metrics(s)
        assert m["automation_total"] == 2
        assert m["automation_executed"] == 1
        assert m["automation_skipped"] == 1


# ── Routes ───────────────────────────────────────────────────────────

class TestAutomationPage:
    def test_module_page_renders(self, client):
        body = client.get("/automation").get_data(as_text=True)
        assert "Automation" in body
        # It is a module again, not a redirect to Test Execution.
        assert client.get("/automation").status_code == 200

    def test_page_explains_why_there_is_no_run_button(self, client):
        body = client.get("/automation").get_data(as_text=True)
        assert "npm ci" in body
        assert "npm run upload" in body

    def test_page_shows_the_coverage_numbers(self, client):
        _seed(client, [_tc()])
        body = client.get("/automation").get_data(as_text=True)
        assert "steps bound" in body
        assert "run end to end" in body

    def test_page_states_the_honesty_rule(self, client):
        _seed(client, [_tc()])
        body = client.get("/automation").get_data(as_text=True)
        assert "never reports green" in body

    def test_manual_only_pack_points_at_the_format_knob(self, client):
        _seed(client, [_tc(tc_format="manual")])
        body = client.get("/automation").get_data(as_text=True)
        assert "No automation-targeted test cases" in body

    def test_nav_links_to_the_module(self, client):
        assert "/automation" in client.get("/").get_data(as_text=True)


class TestBundleDownload:
    def test_bundle_is_a_zip(self, client):
        _seed(client, [_tc()])
        resp = client.get("/automation/bundle.zip")
        assert resp.status_code == 200
        assert resp.mimetype == "application/zip"
        with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
            assert "package.json" in zf.namelist()

    def test_manual_only_pack_refuses_with_an_explanation(self, client):
        _seed(client, [_tc(tc_format="manual")])
        resp = client.get("/automation/bundle.zip")
        assert resp.status_code == 409
        assert b"No automation-targeted" in resp.data


class TestIngestEndpoint:
    def test_disabled_without_a_token(self, client, monkeypatch):
        monkeypatch.delenv("AUTOMATION_INGEST_TOKEN", raising=False)
        resp = client.post("/automation/allure-results")
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "ingest_disabled"

    def test_wrong_token_is_rejected(self, client, monkeypatch):
        monkeypatch.setenv("AUTOMATION_INGEST_TOKEN", "right")
        resp = client.post("/automation/allure-results",
                           headers={"X-TFG-Token": "wrong"})
        assert resp.status_code == 401

    def test_valid_upload_is_stored_and_summarised(self, client, monkeypatch):
        monkeypatch.setenv("AUTOMATION_INGEST_TOKEN", "right")
        data = _archive([
            _result("A", "passed", "SC1_001"),
            _result("B", "failed", "SC1_002"),
            _result("C", "skipped", "SC1_003"),
        ])
        resp = client.post(
            "/automation/allure-results",
            headers={"X-TFG-Token": "right"},
            data={"results": (io.BytesIO(data), "allure-results.zip"),
                  "label": "main#42", "origin": "ci"},
            content_type="multipart/form-data")
        assert resp.status_code == 201
        payload = resp.get_json()
        assert payload["automation_passed"] == 1
        assert payload["automation_skipped"] == 1
        # Executed = 2, so 1 pass is 50% — a skip is neither a pass nor a
        # failure anywhere in this pipeline.
        assert payload["automation_pass_rate"] == 50.0
        assert payload["run_id"]

    def test_empty_body_says_what_to_attach(self, client, monkeypatch):
        monkeypatch.setenv("AUTOMATION_INGEST_TOKEN", "right")
        resp = client.post("/automation/allure-results",
                           headers={"X-TFG-Token": "right"},
                           data={}, content_type="multipart/form-data")
        assert resp.status_code == 400
        assert "results" in resp.get_json()["message"]

    def test_wrong_directory_is_422_not_400(self, client, monkeypatch):
        # The request was well-formed; its contents were not what the
        # endpoint needs, and the warning says which.
        monkeypatch.setenv("AUTOMATION_INGEST_TOKEN", "right")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("allure-report/index.html", "<html></html>")
        resp = client.post(
            "/automation/allure-results",
            headers={"X-TFG-Token": "right"},
            data={"results": (io.BytesIO(buf.getvalue()), "r.zip")},
            content_type="multipart/form-data")
        assert resp.status_code == 422
        assert resp.get_json()["warnings"]

    def test_endpoint_survives_csrf_enabled(self, monkeypatch):
        """The exemption has to be verified with CSRF actually ON.

        conftest disables WTF_CSRF_ENABLED, so an endpoint that is NOT
        exempt passes every other test in this file and then 400s on
        production for every CI upload — a session cookie and a csrf_token
        are exactly what a CI job does not have. Introspecting the exempt
        set would not catch a registration that silently failed, so this
        flips the flag and posts for real.
        """
        monkeypatch.setenv("AUTOMATION_INGEST_TOKEN", "right")
        from app import app

        app.config["WTF_CSRF_ENABLED"] = True
        try:
            with app.test_client() as csrf_client:
                data = _archive([_result("A", "passed", "SC1_001")])
                resp = csrf_client.post(
                    "/automation/allure-results",
                    headers={"X-TFG-Token": "right"},
                    data={"results": (io.BytesIO(data), "r.zip")},
                    content_type="multipart/form-data")
            assert resp.status_code == 201, (
                f"expected 201, got {resp.status_code} — the CSRF exemption "
                f"did not register, so every CI upload will 400")
        finally:
            app.config["WTF_CSRF_ENABLED"] = False


class TestRunHistory:
    def test_run_detail_renders_the_status_legend(self, client, monkeypatch):
        monkeypatch.setenv("AUTOMATION_INGEST_TOKEN", "right")
        data = _archive([
            _result("A", "failed", "SC1_002", msg="expected visible"),
            _result("B", "skipped", "SC1_003",
                    msg="Assertion not automatable"),
        ])
        resp = client.post(
            "/automation/allure-results",
            headers={"X-TFG-Token": "right"},
            data={"results": (io.BytesIO(data), "r.zip")},
            content_type="multipart/form-data")
        run_id = resp.get_json()["run_id"]

        body = client.get(f"/automation/runs/{run_id}").get_data(as_text=True)
        # The three non-passing statuses mean different things and the fix
        # differs, so the page has to say which is which.
        assert "an assertion did not hold" in body
        assert "threw before it could assert" in body
        assert "nobody checked" in body
        assert "expected visible" in body

    def test_unknown_run_is_404(self, client):
        assert client.get("/automation/runs/99999").status_code == 404
