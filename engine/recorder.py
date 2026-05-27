"""Codegen subprocess wrapper used by the ``tfg record`` CLI.

Thin shim around::

    python -m playwright codegen \\
        --target python-async \\
        --output <tmpfile> \\
        --test-id-attribute data-testid,data-test,data-qa \\
        <url>

The codegen process blocks until the operator closes the recording
browser window (no piping needed — codegen owns its own UI). We poll
for child exit, kill it after :data:`RECORDER_TIMEOUT_S` so a forgotten
window doesn't pin the parent forever, and return the captured file
for :mod:`engine.recorder_parser` to ingest.

Kept dependency-light so the wrapper imports cleanly on hosts that
don't ship the Playwright browsers (the CLI does its own preflight
check before calling :func:`run_codegen`).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from engine.log import get_logger

_logger = get_logger(__name__)


# Codegen runtime cap. 30 min is generous — manual QA recording flows
# under 5 min in practice, but accessibility / multi-step regressions
# can stretch. Override via the env var on slow hosts. Hard-floored at
# 60 s so a typo can't disable the safety net entirely.
RECORDER_TIMEOUT_S = max(60, int(os.environ.get("RECORDER_TIMEOUT_S", "1800")))

# Default attribute list for ``--test-id-attribute``. Codegen prefers
# locators built from these data attributes when present on the
# clicked element, which means the captured selector survives DOM
# refactors and pre-stages PR-A's multi-locator ranking (testid sits
# at the top of the strategy table).
DEFAULT_TEST_ID_ATTRIBUTES = "data-testid,data-test,data-qa"


def run_codegen(url: str,
                output_path: str | os.PathLike[str],
                test_id_attributes: str = DEFAULT_TEST_ID_ATTRIBUTES,
                timeout_s: int | None = None,
                browser: str = "chromium") -> Path:
    """Open a codegen browser at ``url`` and return the captured file.

    Blocks until the operator closes the codegen window or
    ``timeout_s`` expires. Raises :class:`subprocess.TimeoutExpired`
    on the timeout path — the CLI surfaces that as a friendly message;
    a half-recorded file at ``output_path`` may still be present (the
    parser is defensive about truncated payloads).

    Args:
        url: Starting URL for the recording. Required; codegen needs
            an initial navigation target.
        output_path: Where codegen writes the captured Python script.
            Caller owns lifecycle — typically a ``tempfile`` path.
        test_id_attributes: Comma-separated data attributes codegen
            prefers when building locators. Defaults to the trio
            most TestFortge sites use.
        timeout_s: Max recording duration. Defaults to
            :data:`RECORDER_TIMEOUT_S`.
        browser: One of ``chromium`` / ``firefox`` / ``webkit``.
            Chromium is the only well-tested default; the others
            work but receive less CI exposure.

    Returns:
        Resolved :class:`pathlib.Path` to the captured file.
    """
    if not url:
        raise ValueError("run_codegen: url is required")
    out = Path(os.fspath(output_path)).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "playwright", "codegen",
        "--target", "python-async",
        "--output", str(out),
        "--test-id-attribute", test_id_attributes,
        "--browser", browser,
        url,
    ]
    _logger.info("recorder: launching codegen → %s", out)
    proc = subprocess.Popen(cmd)
    try:
        proc.wait(timeout=timeout_s if timeout_s is not None else RECORDER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _logger.warning("recorder: codegen exceeded %ss — terminating",
                        timeout_s or RECORDER_TIMEOUT_S)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise
    if proc.returncode not in (0, None):
        _logger.warning("recorder: codegen exited with code %s — captured "
                        "file may be partial", proc.returncode)
    return out


def codegen_available() -> bool:
    """Cheap probe — ``True`` if the codegen subcommand resolves.

    The CLI runs this before :func:`run_codegen` so testers see a
    clear "install Playwright + ``playwright install chromium`` first"
    message instead of an opaque subprocess failure.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "codegen", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and "codegen" in (result.stdout or "")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
