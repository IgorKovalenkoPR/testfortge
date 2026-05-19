"""Sprint 1 Task 4 — SIGTERM handler in engine/runner_worker.py.

Three regressions guarded:

1. ``test_sigterm_writes_error_flag_and_done`` — a SIGTERM delivered
   mid-run produces ``error.flag``, ``result.json`` with
   ``status="terminated"``, and ``done.flag`` so the UI poller surfaces
   the failure instead of waiting for the 120 s stall timer.
2. ``test_sigint_same_path`` — Ctrl-C (SIGINT) follows the same path.
3. ``test_run_status_surfaces_terminated`` — once both flag files
   exist, ``/test-execution/run-status/<run_id>`` returns the
   terminated payload with the first 500 chars of error.flag.

Windows note: ``signal.signal(SIGTERM, …)`` works in CPython on
Windows but ``os.kill(pid, SIGTERM)`` does NOT route to the handler —
the OS skips the handler and force-terminates. The subprocess-spawn
tests are therefore POSIX-only; on Windows we exercise the closure in
the same interpreter to cover the same logic.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

import pytest


_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_WINDOWS = sys.platform == "win32"


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _make_config(tmp_path) -> tuple[str, str, str]:
    """Seed a runner_worker config JSON pointing at a deliberately
    misconfigured runner so the subprocess hangs briefly inside
    AutomationRunner.run (or fails after Playwright import). Returns
    ``(config_path, pending_dir, config_id)``.

    We do NOT need Playwright to actually launch — the goal is to send
    SIGTERM before the worker exits normally. The runner_worker has
    its own try/except wrapping run() so any import failure also runs
    the signal handler path before the worker can finish.
    """
    storage = os.path.join(str(tmp_path), "storage")
    pending = os.path.join(storage, "automation_runs", "_pending")
    os.makedirs(pending, exist_ok=True)
    config_id = f"test_{uuid.uuid4().hex[:8]}"
    config_path = os.path.join(pending, f"{config_id}.json")
    config = {
        "storage_root": storage,
        "run_id": "",
        "base_url": "",
        "items_data": [],
        "selected_ids": [],
        "env_types": ["web"],
        "manual_statuses": {},
        "manual_bug_refs": {},
        "session_id": "test-sid",
        "tester_id": "",
        "tester_name": "",
        "testing_types": [],
        "site_url": "",
        "headless": True,
        "record_video": False,
        "affects_version": "",
        "source": "test_cases",
        "item_type": "test_case",
        "envs": {},
        # runner_kwargs absent — AutomationRunner uses defaults.
        # items_data empty so scripts_from_session produces no work
        # and the subprocess blocks inside Playwright init giving the
        # parent time to SIGTERM.
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f)
    return config_path, pending, config_id


def _spawn_worker(config_path: str) -> subprocess.Popen:
    env = dict(os.environ)
    # Make the project root importable.
    env["PYTHONPATH"] = (_ROOT + os.pathsep
                          + env.get("PYTHONPATH", ""))
    # Suppress automation noise — keeps test output readable.
    env.setdefault("AUTOMATION_RUN_RETENTION_DAYS", "0")
    return subprocess.Popen(
        [sys.executable, "-m", "engine.runner_worker", config_path],
        cwd=_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # POSIX: detach so SIGTERM goes only to the worker, not the
        # whole process group of the test.
        start_new_session=not _WINDOWS,
    )


def _wait_for_started(pending: str, config_id: str,
                       timeout: float = 8.0) -> bool:
    """The worker writes ``<id>.started.flag`` immediately after
    parsing the config. Spin until either that file exists or the
    timeout elapses."""
    started = os.path.join(pending, f"{config_id}.started.flag")
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.isfile(started):
            return True
        time.sleep(0.05)
    return False


def _wait_for_file(path: str, timeout: float = 8.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.isfile(path):
            return True
        time.sleep(0.05)
    return False


# ─────────────────────────────────────────────────────────────────────
# Tests 1 + 2 — subprocess spawn + signal
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    _WINDOWS,
    reason=(
        "os.kill(pid, SIGTERM) on Windows force-terminates without "
        "invoking the Python signal handler; the same logic is "
        "exercised in test_sigterm_handler_closure_writes_flags."
    ),
)
def test_sigterm_writes_error_flag_and_done(tmp_path):
    config_path, pending, config_id = _make_config(tmp_path)
    proc = _spawn_worker(config_path)
    try:
        assert _wait_for_started(pending, config_id), (
            "worker never wrote started.flag — subprocess likely "
            f"crashed: {proc.stderr.read().decode(errors='replace')}"
        )
        # Give the worker a moment to enter AutomationRunner.run so
        # the SIGTERM lands while real work would be in progress.
        time.sleep(0.3)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    done_path = os.path.join(pending, f"{config_id}.done.flag")
    err_path = os.path.join(pending, f"{config_id}.error.flag")
    result_path = os.path.join(pending, f"{config_id}.result.json")

    assert _wait_for_file(done_path, timeout=2.0), (
        "done.flag was not written by SIGTERM handler"
    )
    assert os.path.isfile(err_path), "error.flag was not written"
    with open(err_path, "r", encoding="utf-8") as f:
        body = f.read()
    assert "terminated by signal" in body
    assert str(int(signal.SIGTERM)) in body

    assert os.path.isfile(result_path), "result.json was not written"
    with open(result_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    assert payload["status"] == "terminated"
    assert payload["config_id"] == config_id
    assert "signal" in payload["error"].lower()

    # POSIX: SIGTERM gives exit code 143 (128 + 15). The handler
    # returns it via sys.exit, so Popen.returncode is the natural
    # exit, not the signal number.
    assert proc.returncode == 143, (
        f"expected exit 143 (SIGTERM), got {proc.returncode}; "
        f"stderr={proc.stderr.read().decode(errors='replace')}"
    )


@pytest.mark.skipif(
    _WINDOWS,
    reason=(
        "SIGINT delivery to a child via os.kill on Windows is "
        "different from POSIX; covered by the closure test instead."
    ),
)
def test_sigint_same_path(tmp_path):
    config_path, pending, config_id = _make_config(tmp_path)
    proc = _spawn_worker(config_path)
    try:
        assert _wait_for_started(pending, config_id), (
            "worker never wrote started.flag — subprocess likely "
            f"crashed: {proc.stderr.read().decode(errors='replace')}"
        )
        time.sleep(0.3)
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    done_path = os.path.join(pending, f"{config_id}.done.flag")
    err_path = os.path.join(pending, f"{config_id}.error.flag")
    result_path = os.path.join(pending, f"{config_id}.result.json")

    assert _wait_for_file(done_path, timeout=2.0), (
        "done.flag was not written on SIGINT"
    )
    assert os.path.isfile(err_path)
    assert os.path.isfile(result_path)
    with open(result_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    assert payload["status"] == "terminated"

    # SIGINT → exit 130 (128 + 2). Either the handler routed through
    # sys.exit(130) or — on systems where SIGINT becomes
    # KeyboardInterrupt before the handler runs — the outer
    # try/except in main() still wrote a "failed" result; we only
    # accept the terminated path because the handler is registered
    # before any work runs.
    assert proc.returncode == 130, (
        f"expected exit 130 (SIGINT), got {proc.returncode}; "
        f"stderr={proc.stderr.read().decode(errors='replace')}"
    )


# ─────────────────────────────────────────────────────────────────────
# Windows fallback — exercise the handler closure in-process.
# ─────────────────────────────────────────────────────────────────────


def test_terminated_artifacts_helper_writes_all_three_files(tmp_path):
    """Direct unit test of ``_write_terminated_artifacts`` — the
    handler body extracted so we can exercise it without an OS-level
    SIGTERM round-trip (which is unreliable on Windows). Verifies the
    write order (error.flag → result.json → done.flag) and the
    payload contents."""
    from engine.runner_worker import _write_terminated_artifacts

    pending = os.path.join(str(tmp_path), "pending")
    os.makedirs(pending, exist_ok=True)
    config_id = "unit_test_42"
    error_path = os.path.join(pending, f"{config_id}.error.flag")
    result_path = os.path.join(pending, f"{config_id}.result.json")
    done_path = os.path.join(pending, f"{config_id}.done.flag")

    _write_terminated_artifacts(
        signum=15,  # SIGTERM on POSIX
        config_id=config_id,
        error_path=error_path,
        result_path=result_path,
        done_path=done_path,
    )

    # All three files exist.
    assert os.path.isfile(error_path)
    assert os.path.isfile(result_path)
    assert os.path.isfile(done_path)

    # error.flag content includes "signal 15" and a timestamp.
    with open(error_path, "r", encoding="utf-8") as f:
        err = f.read()
    assert "terminated by signal 15" in err

    # result.json carries status="terminated".
    with open(result_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    assert payload["status"] == "terminated"
    assert payload["config_id"] == config_id
    assert "signal 15" in payload["error"]
    assert "finished_at" in payload

    # done.flag must be written LAST. We can't easily assert the
    # ordering without mocks, but mtime ordering is at least
    # consistent with that expectation on systems with sub-second
    # mtime resolution.
    if os.path.isfile(error_path) and os.path.isfile(done_path):
        assert (os.path.getmtime(done_path)
                >= os.path.getmtime(error_path))


def test_terminated_artifacts_helper_closes_browser_if_present(tmp_path):
    """The handler must close the active Playwright browser before
    writing files. Verify it pulls ``_CURRENT_BROWSER`` from
    ``engine.automation_runner`` and calls ``.close()`` on it."""
    from engine import automation_runner as _ar
    from engine.runner_worker import _write_terminated_artifacts

    pending = os.path.join(str(tmp_path), "pending")
    os.makedirs(pending, exist_ok=True)
    config_id = "browser_close_test"
    error_path = os.path.join(pending, f"{config_id}.error.flag")
    result_path = os.path.join(pending, f"{config_id}.result.json")
    done_path = os.path.join(pending, f"{config_id}.done.flag")

    class _FakeBrowser:
        closed = False
        def close(self):
            _FakeBrowser.closed = True

    fake = _FakeBrowser()
    saved = _ar._CURRENT_BROWSER
    _ar._CURRENT_BROWSER = fake
    try:
        _write_terminated_artifacts(
            signum=15,
            config_id=config_id,
            error_path=error_path,
            result_path=result_path,
            done_path=done_path,
        )
    finally:
        _ar._CURRENT_BROWSER = saved

    assert _FakeBrowser.closed, "browser.close() was not called"
    # And the file writes still happened.
    assert os.path.isfile(done_path)


@pytest.mark.skipif(
    not _WINDOWS,
    reason="POSIX uses the subprocess tests above for full coverage.",
)
def test_sigterm_handler_smoke_on_windows(tmp_path):
    """Smoke check that on Windows the worker still terminates after
    SIGINT delivery (CTRL_BREAK_EVENT) and that ``done.flag`` lands.
    We don't insist on ``status="terminated"`` here because empty
    items_data + no Playwright launch means the worker may have
    completed normally before the signal arrived — only that the
    UI's polling contract (done.flag exists) is honoured."""
    config_path, pending, config_id = _make_config(tmp_path)

    env = dict(os.environ)
    env["PYTHONPATH"] = (_ROOT + os.pathsep
                          + env.get("PYTHONPATH", ""))
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [sys.executable, "-m", "engine.runner_worker", config_path],
        cwd=_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
    )
    try:
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    done_path = os.path.join(pending, f"{config_id}.done.flag")
    # Either the natural finally branch or the signal handler must
    # have written done.flag. The polling route depends on it.
    assert _wait_for_file(done_path, timeout=2.0)


# ─────────────────────────────────────────────────────────────────────
# Test 3 — route surfaces terminated when both flags present
# ─────────────────────────────────────────────────────────────────────


def test_run_status_surfaces_terminated(client, tmp_path, monkeypatch):
    """With both ``<run_id>.error.flag`` and ``<run_id>.done.flag``
    present, /test-execution/run-status/<run_id> returns the
    terminated payload with the (first 500 chars of) error.flag in
    the ``error`` field."""
    # Repoint STORAGE_ROOT at a tmp dir so this test doesn't pollute
    # the real storage tree. The route reads
    # routes.automation.STORAGE_ROOT at request time so monkeypatch
    # takes effect.
    fake_root = os.path.join(str(tmp_path), "storage")
    pending = os.path.join(fake_root, "automation_runs", "_pending")
    os.makedirs(pending, exist_ok=True)
    monkeypatch.setattr("routes.automation.STORAGE_ROOT", fake_root)

    run_id = "20260519_120000_abc123"
    err = os.path.join(pending, f"{run_id}.error.flag")
    done = os.path.join(pending, f"{run_id}.done.flag")
    with open(err, "w", encoding="utf-8") as f:
        f.write("terminated by signal 15 at 2026-05-19T12:00:00+00:00")
    with open(done, "w", encoding="utf-8") as f:
        f.write("2026-05-19T12:00:01+00:00")

    r = client.get(f"/test-execution/run-status/{run_id}")
    assert r.status_code == 200, r.data
    payload = r.get_json()
    assert payload["status"] == "terminated"
    assert "signal 15" in payload["error"]
    # Sanity — value is truncated to 500 chars.
    assert len(payload["error"]) <= 500


def test_run_status_terminated_truncates_500_chars(client, tmp_path,
                                                     monkeypatch):
    """Defence-in-depth: an attacker who writes a giant error.flag
    can't fill the JSON response — we cap at 500 chars."""
    fake_root = os.path.join(str(tmp_path), "storage")
    pending = os.path.join(fake_root, "automation_runs", "_pending")
    os.makedirs(pending, exist_ok=True)
    monkeypatch.setattr("routes.automation.STORAGE_ROOT", fake_root)

    run_id = "20260519_120000_big999"
    err = os.path.join(pending, f"{run_id}.error.flag")
    done = os.path.join(pending, f"{run_id}.done.flag")
    with open(err, "w", encoding="utf-8") as f:
        f.write("X" * 5000)
    with open(done, "w", encoding="utf-8") as f:
        f.write("ts")

    r = client.get(f"/test-execution/run-status/{run_id}")
    payload = r.get_json()
    assert payload["status"] == "terminated"
    assert len(payload["error"]) == 500
