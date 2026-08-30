"""The synthetic submit step is a note, not an instruction.

When a form's ``submit`` event fires, ``extension/content.js`` emits a
step nobody performed. It exists so ``engine.session_segmenter`` can see
a flow boundary; the click or keypress that actually caused the submit
is already in the stream, one step earlier.

It used to go out as ``click`` on ``css=form``, which reads wrong in the
review card and is worse than it reads. Nothing skipped it, so
``AutomationRunner`` resolved the form and called ``.click()`` on it --
and clicking a ``<form>`` clicks whatever child element sits at its
centre point. Measured against a four-control form, the replay ticked a
checkbox::

    page.locator("form").click()
    -> click landed on: ['LABEL', 'agree']
    -> checkbox now: True

So the marker is now ``submit`` with no target: a verb the pipeline
already understands (``suite_classifier`` counts it as a form
submission), addressing no element, with an explicit branch in the
runner that does nothing.

The shape is pinned in one place, :data:`MARKER`, and checked from both
ends -- the browser test proves the content script really emits it, and
the unit tests below drive the rest of the pipeline with the same dict.
A marker asserted twice against itself would prove nothing.
"""
from __future__ import annotations

import pathlib

import pytest

from engine.automation_qa import AutomationStep
from engine.automation_runner import AutomationRunner
from engine.session_segmenter import _is_submit


#: Exactly what ``extension/content.js`` ships on a form submit.
MARKER = {
    "action": "submit",
    "target": "",
    "value": "",
    "raw": 'page.locator("form").submit()',
    "comment": "form submitted",
    "target_alternates": [],
    "locator_label": "",
    "kind": "action",
    "assertion_type": "",
}


def _marker_step() -> AutomationStep:
    return AutomationStep(**MARKER)


# ── What the recorder emits ─────────────────────────────────────

CONTENT_JS = (pathlib.Path(__file__).resolve().parent.parent
              / "extension" / "content.js")

STUB = """
window.__steps = [];
window.chrome = {
  runtime: {
    id: 'stub-extension-id',
    lastError: undefined,
    sendMessage: function (msg, cb) {
      if (msg && msg.type === 'append_step') { window.__steps.push(msg.step); }
      var reply = (msg && msg.type === 'is_active_for_tab') ? {active: true} : {};
      if (cb) setTimeout(function () { cb(reply); }, 0);
    },
    onMessage: {addListener: function () {}},
  },
};
"""

# Four controls, and the one at the form's centre is a checkbox -- the
# element the old marker was clicking. Submission is cancelled so the
# page stays put and the emitted steps survive to be read.
FORM_PAGE = """
<!doctype html><meta charset=utf-8>
<form id=f style="width:400px">
  <input id=name>
  <label><input type=checkbox id=agree> I agree</label>
  <a id=terms href="#terms">Terms</a>
  <button id=go type=submit>Go</button>
</form>
<script>
  document.getElementById('f')
      .addEventListener('submit', e => e.preventDefault());
</script>
"""


class TestWhatTheRecorderEmits:
    """Runs the real content script in a real browser."""

    @pytest.fixture(scope="class")
    def steps(self):
        pytest.importorskip("playwright.sync_api",
                            reason="playwright not installed")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:                 # no Chromium build
                pytest.skip(f"chromium unavailable: {exc}")
            page = browser.new_page()
            try:
                page.set_content(FORM_PAGE)
                page.evaluate(STUB)
                page.add_script_tag(
                    content=CONTENT_JS.read_text(encoding="utf-8"))
                page.click("#go")
                page.wait_for_function(
                    "window.__steps.some(s => s.comment === 'form submitted')",
                    timeout=5000)
                yield page.evaluate("window.__steps")
            finally:
                page.close()
                browser.close()

    def test_the_marker_matches_the_shape_the_pipeline_is_tested_with(
            self, steps):
        markers = [s for s in steps if s.get("comment") == "form submitted"]
        assert len(markers) == 1, steps
        assert markers[0] == MARKER

    def test_the_click_that_caused_the_submit_is_still_recorded(self, steps):
        # The marker is allowed to be inert precisely because this step
        # exists. If the button click ever stopped being captured, the
        # inert marker would be all that was left of the submission.
        clicks = [s for s in steps if s.get("action") == "click"]
        assert len(clicks) == 1, steps
        assert "#go" in clicks[0]["target"] or "Go" in clicks[0]["target"]

    def test_no_step_addresses_the_form_itself(self, steps):
        # The form is a container the tester never interacted with.
        for s in steps:
            assert "form" not in (s.get("target") or "").lower(), s


# ── What the runner does with it ────────────────────────────────


class _ExplodingPage:
    """Every way of reaching an element raises.

    The claim under test is that replaying the marker touches nothing,
    and the strongest way to state that is a page on which touching
    anything is an error.
    """
    url = "https://example.test/form"

    def _boom(self, *a, **kw):
        raise AssertionError(
            "the submit marker tried to interact with the page")

    locator = get_by_role = get_by_text = get_by_label = _boom
    get_by_placeholder = get_by_test_id = get_by_alt_text = _boom
    get_by_title = goto = _boom

    def wait_for_timeout(self, ms: int):
        return None

    def content(self):
        return ""

    def screenshot(self, **kw):
        return b""


def _make_runner(tmp_path):
    r = AutomationRunner(storage_root=str(tmp_path),
                          base_url="https://example.test",
                          headless=True, record_video=False,
                          default_timeout_ms=200)
    r._screenshot = lambda *a, **kw: None
    r._scroll_and_highlight = lambda *a, **kw: None
    r._move_cursor_to = lambda *a, **kw: None
    r._live_pump = lambda *a, **kw: None
    r._annotate_failure = lambda *a, **kw: None
    r._visible_scroll = lambda *a, **kw: None
    return r


class TestWhatTheRunnerDoesWithIt:
    def test_replaying_the_marker_touches_nothing(self, tmp_path):
        sr = _make_runner(tmp_path)._run_step(
            _ExplodingPage(), _marker_step(), 1, str(tmp_path))
        assert sr.status == "passed", sr.comment

    def test_it_is_handled_on_purpose_not_by_falling_off_the_chain(
            self, tmp_path):
        # An unrecognised action reaches the end of the dispatch chain
        # and reports "passed" having done nothing -- the same result
        # this marker wants, arrived at by accident. The difference is
        # whether the source names the case, so read the source.
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "engine" / "automation_runner.py").read_text(
                   encoding="utf-8")
        assert 'elif step.action == "submit":' in src, (
            "the submit marker is passing by fall-through, which makes it "
            "indistinguishable from an action the runner forgot to add")


# ── What the rest of the pipeline makes of it ───────────────────


class TestThePipelineStillReadsIt:
    def test_it_is_still_a_segmenter_boundary(self):
        # The whole reason the step exists. Changing the verb must not
        # cost the segmenter the signal it was emitted for.
        assert _is_submit(_marker_step()) is True

    def test_it_counts_as_a_form_submission_for_suite_classification(self):
        from engine.suite_classifier import _has_form_submit
        assert _has_form_submit([_marker_step()]) is True

    def test_it_does_not_read_as_a_click_in_the_editor_preview(self):
        from routes.generation import _human_steps_preview
        line = _human_steps_preview([MARKER])
        assert "Click" not in line, line
        assert "css=form" not in line, line
        assert "Submit" in line, line
