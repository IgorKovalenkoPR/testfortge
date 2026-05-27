"""PR-B/6 tests — tools.tfg_record CLI.

End-to-end flow exercised via ``--from-file`` so we never launch a real
browser. The pipe under test is:
    capture file → parser → AutomationStep[] → DB column.

Each test seeds its own project + TC, runs the CLI in-process via
``main(argv)``, and asserts on the returned exit code + DB state.
"""
from __future__ import annotations

import json
import os
import textwrap
from unittest import mock

import pytest

from engine import db
from tools import tfg_record


@pytest.fixture
def recorder_on():
    with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
        yield


@pytest.fixture
def project_with_tc():
    pid = db.upsert_project(
        name=f"tfg-record-cli-{os.urandom(4).hex()}",
        base_url="https://app.example.com",
    )
    db.save_test_cases(pid, [{
        "id": "TC-LOGIN", "section": "Login", "section_num": 1,
        "summary": "Sign in", "preconditions": "",
        "test_steps": "1. Open\n2. Submit", "test_data": "",
        "expected_result": "Welcome page",
        "issues": "", "comment": "", "user_story_id": "US-1",
        "category": "Positive", "priority": "High",
        "status": "Unchecked", "testing_type": "Functional",
        "url_pattern": "", "trigger": "manual",
    }])
    yield pid, "TC-LOGIN"
    db.delete_project(pid)


@pytest.fixture
def capture_file(tmp_path):
    """Write a realistic codegen capture for ``--from-file`` ingest."""
    f = tmp_path / "captured.py"
    f.write_text(textwrap.dedent('''
        from playwright.async_api import async_playwright

        async def run(playwright):
            browser = await playwright.chromium.launch()
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto("https://app.example.com/login")
            await page.get_by_label("Email").fill("user@x.test")
            await page.get_by_role("button", name="Sign in").click()
            await browser.close()
    ''').strip(), encoding="utf-8")
    return f


class TestFeatureFlag:
    def test_refuses_without_flag(self, project_with_tc, capture_file,
                                    capsys):
        pid, tc_id = project_with_tc
        rc = tfg_record.main([
            "--project", pid, "--tc", tc_id,
            "--from-file", str(capture_file),
        ])
        assert rc == 2
        assert "RECORDER_ENABLED" in capsys.readouterr().err


class TestFromFileIngest:
    def test_full_round_trip(self, recorder_on, project_with_tc,
                               capture_file, capsys):
        pid, tc_id = project_with_tc
        rc = tfg_record.main([
            "--project", pid, "--tc", tc_id,
            "--from-file", str(capture_file),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "attached 3 recorded step(s)" in out

        loaded = db.load_test_cases(pid)
        payload = loaded[0]["automation_steps_json"]
        decoded = json.loads(payload)
        assert [s["action"] for s in decoded] == ["goto", "fill", "click"]
        assert decoded[1]["target"] == "label=Email"
        assert decoded[1]["value"] == "user@x.test"

    def test_unknown_tc_returns_exit_3(self, recorder_on, project_with_tc,
                                          capture_file, capsys):
        pid, _ = project_with_tc
        rc = tfg_record.main([
            "--project", pid, "--tc", "TC-MISSING",
            "--from-file", str(capture_file),
        ])
        assert rc == 3
        assert "not found" in capsys.readouterr().err

    def test_missing_file_returns_exit_4(self, recorder_on, project_with_tc,
                                            capsys):
        pid, tc_id = project_with_tc
        rc = tfg_record.main([
            "--project", pid, "--tc", tc_id,
            "--from-file", "/nope/does/not/exist.py",
        ])
        assert rc == 4
        assert "does not exist" in capsys.readouterr().err

    def test_empty_capture_returns_exit_6(self, recorder_on,
                                             project_with_tc, tmp_path,
                                             capsys):
        """A capture file with no recorded actions surfaces a clear
        warning and exits 6 without touching the DB."""
        pid, tc_id = project_with_tc
        empty = tmp_path / "empty.py"
        empty.write_text("x = 1\nprint('hi')\n", encoding="utf-8")
        rc = tfg_record.main([
            "--project", pid, "--tc", tc_id,
            "--from-file", str(empty),
        ])
        assert rc == 6
        # DB column remains empty — no spurious write.
        loaded = db.load_test_cases(pid)
        assert loaded[0]["automation_steps_json"] == ""


class TestArgParser:
    def test_url_and_from_file_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            tfg_record.build_arg_parser().parse_args([
                "--project", "p", "--tc", "t",
                "--url", "https://x", "--from-file", "y.py",
            ])

    def test_one_source_required(self):
        with pytest.raises(SystemExit):
            tfg_record.build_arg_parser().parse_args([
                "--project", "p", "--tc", "t",
            ])
