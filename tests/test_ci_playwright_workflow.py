"""
The Playwright-in-CI workflow (E5.2′).

A workflow file cannot be unit-tested by running it, so these assert the
properties that would be quietly lost in an edit and would each turn the
run into a lie:

* **the ingest must be checked before the browsers are installed.** Getting
  a green suite and dropping it on the floor is the worst outcome available
  — the app shows no run and nobody knows why;
* **the test step must not fail the job.** A failing suite is the normal
  reason to run one, and a non-zero exit there would skip the ingest, so
  the app would show nothing exactly when it has something to show;
* **the token must come from a secret**, never a literal;
* **the job must have a timeout**, because a hung suite on a free plan
  spends the monthly allowance on one run.

The YAML is also parsed, which catches the class of typo that makes GitHub
ignore the file and report nothing at all.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "playwright.yml"


@pytest.fixture(scope="module")
def wf() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def raw() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def job(wf) -> dict:
    return wf["jobs"]["run"]


@pytest.fixture(scope="module")
def steps(job) -> list[dict]:
    return job["steps"]


def _index(steps: list[dict], needle: str) -> int:
    for i, step in enumerate(steps):
        if needle.lower() in str(step.get("name") or step.get("uses") or "").lower():
            return i
    raise AssertionError(f"no step matching {needle!r}")


class TestItParses:
    def test_the_file_is_valid_yaml(self, wf):
        assert isinstance(wf, dict)

    def test_it_has_the_one_job(self, wf):
        assert list(wf["jobs"]) == ["run"]

    def test_the_triggers_are_manual_and_dispatchable(self, wf):
        # `on:` is the YAML 1.1 boolean `True` once parsed — GitHub uses its
        # own parser, so this is only a quirk of reading the file here.
        triggers = wf[True] if True in wf else wf["on"]
        assert set(triggers) == {"workflow_dispatch", "repository_dispatch"}

    def test_there_is_no_schedule(self, wf):
        # A nightly cron would spend the free allowance whether or not
        # anybody wanted a run that night. Runs are requested, not assumed.
        triggers = wf[True] if True in wf else wf["on"]
        assert "schedule" not in triggers

    def test_the_app_can_trigger_it_by_a_named_event(self, wf):
        triggers = wf[True] if True in wf else wf["on"]
        assert triggers["repository_dispatch"]["types"] == ["tfg-playwright-run"]


class TestCostControls:
    def test_the_job_has_a_timeout(self, job):
        assert 0 < job["timeout-minutes"] <= 30

    def test_in_flight_runs_are_cancelled(self, wf):
        assert wf["concurrency"]["cancel-in-progress"] is True

    def test_only_one_browser_engine_is_installed(self, raw):
        # A cross-browser matrix triples install time and cache size; that is
        # a decision for whoever needs it, not the default cost of a run.
        assert "playwright install --with-deps chromium" in raw
        assert "install --with-deps\n" not in raw

    def test_permissions_are_read_only(self, wf):
        assert wf["permissions"] == {"contents": "read"}


class TestFailingEarlyWhenNothingCanWork:
    def test_the_ingest_is_checked_before_the_browsers_are_installed(self, steps):
        assert _index(steps, "Check the ingest") < _index(steps, "Install Chromium")

    def test_the_suite_is_checked_before_the_browsers_are_installed(self, steps):
        assert _index(steps, "Check the suite") < _index(steps, "Install Chromium")

    def test_both_secrets_are_named_in_the_error(self, raw):
        assert "TFG_INGEST_URL" in raw and "TFG_INGEST_TOKEN" in raw
        # The message has to say what to set them to, or the operator has to
        # come and read this file.
        assert "automation/allure-results" in raw
        assert "AUTOMATION_INGEST_TOKEN" in raw

    def test_an_empty_results_directory_is_an_error_not_a_silent_pass(self, raw):
        assert "produced no allure-results" in raw


class TestTheSuitesVerdictDoesNotSkipTheIngest:
    def test_the_test_step_continues_on_error(self, steps):
        step = steps[_index(steps, "Run the suite")]
        assert step["continue-on-error"] is True

    def test_the_ingest_runs_after_the_tests(self, steps):
        assert _index(steps, "Run the suite") < _index(steps, "Send the results")

    def test_the_job_still_reports_the_failure_afterwards(self, steps):
        # A red suite must be a red job — just not before the results are in
        # the app.
        last = steps[-1]
        assert last["if"] == "steps.tests.outcome == 'failure'"
        assert "exit 1" in last["run"]

    def test_the_failure_reflector_is_the_last_step(self, steps):
        assert _index(steps, "Reflect") == len(steps) - 1


class TestTheResultsSurviveAFailedIngest:
    def test_they_are_uploaded_as_an_artifact(self, steps):
        assert any("upload-artifact" in str(s.get("uses", "")) for s in steps)

    def test_the_upload_happens_before_the_post(self, steps):
        assert _index(steps, "upload-artifact") < _index(steps, "Send the results")


class TestNoSecretsInTheFile:
    def test_the_token_comes_from_a_secret(self, raw):
        assert "secrets.TFG_INGEST_TOKEN" in raw

    def test_the_url_comes_from_a_secret(self, raw):
        assert "secrets.TFG_INGEST_URL" in raw

    def test_nothing_looks_like_a_pasted_token(self, raw):
        import re
        # A real token would be a long opaque run of base64-ish characters
        # on an assignment line. Catches the "just for testing" paste that
        # then gets committed.
        for line in raw.splitlines():
            if "TOKEN" in line.upper() and "secrets." not in line:
                assert not re.search(r"[:=]\s*[A-Za-z0-9+/_-]{20,}", line), line


class TestTheIngestCallMatchesTheEndpointsContract:
    """The endpoint is in routes/automation.py; these keep the caller honest
    about what it actually reads."""

    def test_the_token_goes_in_the_header_the_endpoint_reads(self, raw):
        assert "X-TFG-Token:" in raw

    def test_the_archive_is_sent_as_the_results_field(self, raw):
        assert "results=@allure-results.zip" in raw

    def test_the_origin_is_declared_as_ci(self, raw):
        assert "origin=ci" in raw

    def test_the_project_and_label_are_sent(self, raw):
        assert "project_id=$PROJECT_ID" in raw
        assert "label=$RUN_LABEL" in raw

    @pytest.mark.parametrize("code,needle", [
        ("401", "token"),
        ("403", "disabled"),
        ("422", "zero results"),
    ])
    def test_each_endpoint_refusal_gets_its_own_explanation(self, raw, code,
                                                            needle):
        # The endpoint distinguishes these three deliberately; collapsing
        # them into "ingest failed" throws away the diagnosis it did.
        assert code in raw
        assert needle in raw.lower()

    def test_the_endpoint_still_returns_201_on_success(self):
        # If the endpoint's success code ever changes, the workflow's case
        # statement silently starts failing every run.
        import inspect

        from routes import automation
        src = inspect.getsource(automation)
        assert "}), 201" in src
