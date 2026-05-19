"""Unit tests for Automation QA converter and metrics."""
import pytest

from engine.automation_qa import (
    AutomationScript, AutomationStep,
    parse_manual_step, tc_to_script, _detect_action,
)
from engine.automation_report import compute_automation_metrics, detect_flaky


def test_detect_action_click():
    assert _detect_action("Click the 'Login' button") == "click"

def test_detect_action_fill():
    assert _detect_action("Enter 'foo@bar.com' into Email field") == "fill"

def test_detect_action_navigate():
    assert _detect_action("Navigate to https://example.com") == "goto"

def test_parse_fill_extracts_value():
    step = parse_manual_step("2. Enter 'alice@test.com' into Email field")
    assert step.action == "fill"
    assert step.value == "alice@test.com"

def test_parse_click_extracts_target():
    step = parse_manual_step("3. Click the 'Submit' button")
    assert step.action == "click"
    assert "submit" in step.target.lower()

def test_tc_to_script_prepends_goto():
    tc = {"id": "TC_001", "summary": "Verify login",
          "test_steps": "1. Click Login\n2. Enter 'x' into email",
          "expected_result": "User is logged in"}
    s = tc_to_script(tc, base_url="https://site.com")
    assert s.steps[0].action == "goto"
    assert s.steps[-1].action == "expect_text"

def test_metrics_coverage():
    m = compute_automation_metrics(
        {"total": 10, "passed": 7, "failed": 2, "blocked": 1, "duration_ms": 5000},
        total_tc=20,
    )
    assert m["automation_coverage"] == 50.0
    assert m["pass_rate"] == 70.0

def test_flaky_detection():
    hist = [
        {"scripts": [{"tc_id": "TC_001", "status": "passed"}]},
        {"scripts": [{"tc_id": "TC_001", "status": "failed"}]},
    ]
    assert "TC_001" in detect_flaky(hist)


# ----------------------------------------------------------------------
# Sprint 1 Task 3 — browser-context try/finally regression tests.
#
# These exercise _run_script's cleanup path: when any phase before the
# step loop crashes (new_page, init scripts, _authenticate), the
# BrowserContext must still be closed. Pre-fix, only the step loop was
# inside try/finally; a crash at lines 608-674 leaked the context and on
# 512 MB Render dynos ~4 leaked contexts (~80 MB each) OOM-killed the
# worker. We use lightweight stubs for browser / context / page that
# only model the API surface the runner touches.
# ----------------------------------------------------------------------


class _StubVideo:
    def __init__(self, path_str: str):
        self._path = path_str

    def path(self):
        return self._path


class _StubPage:
    """Minimal Playwright Page surface."""

    def __init__(self, *, new_page_raises=False, authenticate_raises=False,
                 video=None, url="https://example.com/after"):
        if new_page_raises:
            raise RuntimeError("new_page boom")
        self.video = video
        self.url = url
        self._authenticate_raises = authenticate_raises

    def set_default_timeout(self, *_a, **_kw): pass
    def set_default_navigation_timeout(self, *_a, **_kw): pass
    def on(self, *_a, **_kw): pass
    def bring_to_front(self): pass
    def evaluate(self, *_a, **_kw): pass


class _StubContext:
    """Minimal Playwright BrowserContext surface. Tracks close() calls."""

    def __init__(self, *, new_page_raises=False, page_video=None,
                 authenticate_raises=False):
        self.close_called = 0
        self._new_page_raises = new_page_raises
        self._page_video = page_video
        self._authenticate_raises = authenticate_raises

    def new_page(self):
        return _StubPage(new_page_raises=self._new_page_raises,
                         video=self._page_video,
                         authenticate_raises=self._authenticate_raises)

    def add_init_script(self, *_a, **_kw): pass

    def close(self):
        self.close_called += 1


class _StubBrowser:
    """Minimal Playwright Browser surface — hands out one stub context."""

    def __init__(self, context: _StubContext):
        self._context = context
        self.new_context_calls = 0

    def new_context(self, **_kw):
        self.new_context_calls += 1
        return self._context


def _make_runner(tmp_path, **overrides):
    from engine.automation_runner import AutomationRunner
    kwargs = dict(storage_root=str(tmp_path), base_url="https://example.com",
                  headless=True, record_video=False)
    kwargs.update(overrides)
    return AutomationRunner(**kwargs)


def _make_script():
    return AutomationScript(
        tc_id="TC_001", summary="probe",
        base_url="https://example.com",
        steps=[AutomationStep(action="wait", value="50", raw="wait 50ms")],
    )


def test_context_closed_on_new_page_failure(tmp_path):
    """If new_page() raises during context setup, the context MUST still
    be closed so the BrowserContext is released back to the kernel.
    Pre-fix this leaked because new_page() ran outside the try/finally."""
    ctx = _StubContext(new_page_raises=True)
    browser = _StubBrowser(ctx)
    runner = _make_runner(tmp_path)
    script = _make_script()

    with pytest.raises(RuntimeError, match="new_page boom"):
        runner._run_script(browser, script, str(tmp_path))

    assert ctx.close_called >= 1, "context.close() must run when new_page fails"


def test_context_closed_on_authenticate_failure(tmp_path, monkeypatch):
    """If _authenticate raises (e.g. selector timeout against a dead
    login page), the context MUST be closed. Pre-fix this leaked because
    _authenticate ran above the try/finally line."""
    from engine.automation_runner import AutomationRunner, TestCredentials

    ctx = _StubContext()
    browser = _StubBrowser(ctx)
    creds = TestCredentials(mode="provided",
                            username="u@x.com", password="p",
                            login_url="https://example.com/login")
    runner = _make_runner(tmp_path, credentials=creds)

    def _boom(self, page, tc_dir):
        raise RuntimeError("auth boom")

    monkeypatch.setattr(AutomationRunner, "_authenticate", _boom)

    script = _make_script()
    with pytest.raises(RuntimeError, match="auth boom"):
        runner._run_script(browser, script, str(tmp_path))

    assert ctx.close_called >= 1, "context.close() must run when _authenticate raises"


def test_video_path_resolved_when_steps_complete(tmp_path, monkeypatch):
    """Happy-path regression: when record_video=True and the step loop
    finishes cleanly, ScriptResult.video_path is populated from
    page.video.path() and the context is closed exactly once."""
    from engine.automation_runner import AutomationRunner

    video_file = tmp_path / "run.webm"
    video_file.write_bytes(b"\x1aE\xdf\xa3")  # token EBML magic
    video = _StubVideo(str(video_file))

    ctx = _StubContext(page_video=video)
    browser = _StubBrowser(ctx)
    runner = _make_runner(tmp_path, record_video=True)

    # Bypass the real step machinery — _run_step is exercised elsewhere
    # and would need full page-locator stubs to pass through.
    from engine.automation_runner import StepResult
    def _fake_step(self, page, step, idx, tc_dir):
        return StepResult(index=idx, action=step.action, raw=step.raw, status="passed")
    monkeypatch.setattr(AutomationRunner, "_run_step", _fake_step)

    script = _make_script()
    result = runner._run_script(browser, script, str(tmp_path))

    assert result.status == "passed"
    assert ctx.close_called == 1
    assert result.video_path, "video_path must be populated on the happy path"
    assert "run.webm" in result.video_path
