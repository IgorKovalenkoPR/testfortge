"""PR-B/5 tests — engine.recorder subprocess wrapper.

The wrapper is a thin Popen shim around ``playwright codegen``. We do
not launch a real browser in CI (no display, slow, flaky), so the
contract under test is purely the *invocation shape*: command list,
timeout enforcement, missing-url guard, file-path resolution.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from engine import recorder


class TestRunCodegenInvocation:
    def test_builds_expected_command(self, tmp_path):
        out = tmp_path / "captured.py"
        with mock.patch("subprocess.Popen") as mock_popen:
            mock_proc = mock.MagicMock()
            mock_proc.wait.return_value = 0
            mock_proc.returncode = 0
            mock_popen.return_value = mock_proc
            recorder.run_codegen("https://app.example.com", out)

        args = mock_popen.call_args[0][0]
        assert "playwright" in args
        assert "codegen" in args
        assert "--target" in args
        assert args[args.index("--target") + 1] == "python-async"
        assert "--output" in args
        assert "--test-id-attribute" in args
        assert (args[args.index("--test-id-attribute") + 1]
                == recorder.DEFAULT_TEST_ID_ATTRIBUTES)
        # URL is the final positional argument.
        assert args[-1] == "https://app.example.com"

    def test_custom_test_id_attributes(self, tmp_path):
        out = tmp_path / "captured.py"
        with mock.patch("subprocess.Popen") as mock_popen:
            mock_proc = mock.MagicMock()
            mock_proc.wait.return_value = 0
            mock_proc.returncode = 0
            mock_popen.return_value = mock_proc
            recorder.run_codegen(
                "https://x.test", out,
                test_id_attributes="qa-id",
            )
        args = mock_popen.call_args[0][0]
        assert args[args.index("--test-id-attribute") + 1] == "qa-id"

    def test_empty_url_raises(self, tmp_path):
        with pytest.raises(ValueError, match="url"):
            recorder.run_codegen("", tmp_path / "x.py")

    def test_creates_output_parent_dir(self, tmp_path):
        nested = tmp_path / "deep" / "nested" / "captured.py"
        with mock.patch("subprocess.Popen") as mock_popen:
            mock_proc = mock.MagicMock()
            mock_proc.wait.return_value = 0
            mock_proc.returncode = 0
            mock_popen.return_value = mock_proc
            recorder.run_codegen("https://x.test", nested)
        assert nested.parent.is_dir()


class TestTimeout:
    def test_timeout_terminates_subprocess(self, tmp_path):
        out = tmp_path / "captured.py"
        with mock.patch("subprocess.Popen") as mock_popen:
            mock_proc = mock.MagicMock()
            mock_proc.wait.side_effect = [
                subprocess.TimeoutExpired(cmd="playwright codegen", timeout=1),
                None,  # graceful shutdown after .terminate()
            ]
            mock_proc.returncode = -15
            mock_popen.return_value = mock_proc
            with pytest.raises(subprocess.TimeoutExpired):
                recorder.run_codegen("https://x.test", out, timeout_s=1)
            mock_proc.terminate.assert_called_once()


class TestCodegenAvailable:
    def test_probe_returns_bool(self):
        # On dev hosts Playwright is installed, on bare CI it may not
        # be — either way the probe returns a bool without raising.
        assert isinstance(recorder.codegen_available(), bool)
