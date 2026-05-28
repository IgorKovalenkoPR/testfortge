"""PR-C — Assertion Mode coverage.

Pins the contract for the three plan-spec assertion variants:

  * ``assertion + visible``  — re-uses ``_try_locator_chain`` for
    PR-A multi-locator fallback, then ``wait_for(state='visible', 5 s)``.
  * ``assertion + text``     — ``page.get_by_text(value, exact=False)``
    must exist; falls back to ``page.content()`` substring scan when
    the text lives in an attribute.
  * ``assertion + url``      — ``fnmatch`` glob on ``page.url`` so
    ``https://app/dashboard*`` passes regardless of query string.

Also pins backward-compat: ``_decode_recorded_steps`` returns ``kind="action"``
for every pre-PR-C payload that omits the field, and the codegen parser
recognises the codegen-toolbar ``expect(...)`` assertions emitted by
Playwright 1.40+ as assertion steps.

The runner suite uses a hand-rolled fake page that implements the slice
of Playwright surface the assertion branch exercises (no real browser).
"""
from __future__ import annotations

import json
import os
from unittest import mock

import pytest

from engine import db
from engine.automation_qa import AutomationStep, _decode_recorded_steps
from engine.automation_runner import AutomationRunner, _step_label
from engine.recorder_parser import parse_codegen_output


# ── Fake Playwright surface ─────────────────────────────────────


class _FakeLocator:
    def __init__(self, selector: str, visible: bool):
        self.selector = selector
        self.visible = visible

    @property
    def first(self):
        return self

    def wait_for(self, state: str = "visible", timeout: int = 0):
        if not self.visible:
            raise TimeoutError(
                f"locator '{self.selector}' never visible after {timeout} ms")

    # Used by `_scroll_and_highlight` + `_safe_bbox` — return None /
    # benign values so the assertion branch doesn't need any extra
    # plumbing.
    def bounding_box(self, timeout: int = 0):
        return None

    def scroll_into_view_if_needed(self, timeout: int = 0):
        return None

    def evaluate(self, script: str):
        return None


class _FakePage:
    def __init__(self, mapping: dict[str, bool], url: str = "",
                 content: str = ""):
        self._mapping = dict(mapping)
        self._url = url
        self._content = content

    @property
    def url(self):
        return self._url

    def content(self):
        return self._content

    def wait_for_timeout(self, ms: int):
        return None

    def screenshot(self, **kwargs):
        # The runner calls page.screenshot through self._screenshot;
        # we sidestep that by stubbing _screenshot on the runner.
        return b""

    def locator(self, selector: str):
        visible = self._mapping.get(selector, False)
        return _FakeLocator(selector, visible)

    def get_by_role(self, role, name=None):
        sel = (f'role={role}[name="{getattr(name, "pattern", name)}"]'
                if name is not None else f"role={role}")
        return self.locator(sel)

    def get_by_text(self, text, exact: bool = False):
        return self.locator(f"text={text}")

    def get_by_placeholder(self, pat):
        return self.locator(f"placeholder={getattr(pat, 'pattern', pat)}")

    def get_by_test_id(self, tid):
        return self.locator(f"data-testid={tid}")

    def get_by_label(self, text):
        return self.locator(f"label={text}")

    def get_by_alt_text(self, text):
        return self.locator(f"alt={text}")

    def get_by_title(self, text):
        return self.locator(f"title={text}")


def _make_runner(tmp_path):
    r = AutomationRunner(storage_root=str(tmp_path),
                          base_url="https://example.test",
                          headless=True, record_video=False,
                          default_timeout_ms=200)
    # Stub the IO-touching helpers — assertion branch doesn't care
    # about screenshots / cursor / live mirror.
    r._screenshot = lambda *a, **kw: None
    r._scroll_and_highlight = lambda *a, **kw: None
    r._move_cursor_to = lambda *a, **kw: None
    r._live_pump = lambda *a, **kw: None
    r._annotate_failure = lambda *a, **kw: None
    return r


# ── Decoder backward compat ─────────────────────────────────────


class TestDecoderBackwardCompat:
    def test_payload_without_kind_defaults_to_action(self):
        """Plan scenario #6: pre-PR-C recordings (no ``kind`` field)
        must deserialise as plain action steps so the runner walks
        the legacy dispatch chain."""
        payload = json.dumps([
            {"action": "click", "target": "data-testid=ok"},
            {"action": "fill", "target": "label=Email", "value": "a@b.c"},
        ])
        steps = _decode_recorded_steps(payload)
        assert len(steps) == 2
        for s in steps:
            assert s.kind == "action"
            assert s.assertion_type == ""

    def test_assertion_payload_round_trips(self):
        """Editor dropdown writes ``kind=assertion`` + ``assertion_type=visible``
        — the decoder must hand the runner the same shape."""
        payload = json.dumps([
            {"action": "expect_visible", "target": "role=heading[name=\"Welcome\"]",
             "kind": "assertion", "assertion_type": "visible"},
        ])
        steps = _decode_recorded_steps(payload)
        assert steps[0].kind == "assertion"
        assert steps[0].assertion_type == "visible"
        assert steps[0].target == 'role=heading[name="Welcome"]'

    def test_assertion_payload_unknown_type_normalises_to_visible(self):
        """Defensive default: an unknown ``assertion_type`` falls back
        to ``visible`` rather than crashing the runner — matches how
        the decoder handles every other forward-compat field."""
        payload = json.dumps([
            {"action": "expect", "kind": "assertion",
             "assertion_type": "future-mode"},
        ])
        steps = _decode_recorded_steps(payload)
        assert steps[0].kind == "assertion"
        assert steps[0].assertion_type == "visible"

    def test_action_step_strips_assertion_type(self):
        """``assertion_type`` is only meaningful when ``kind=assertion``.
        For action steps, the decoder must scrub it so the runner does
        not branch on stale metadata from a previous edit."""
        payload = json.dumps([
            {"action": "click", "kind": "action",
             "assertion_type": "visible"},
        ])
        steps = _decode_recorded_steps(payload)
        assert steps[0].kind == "action"
        assert steps[0].assertion_type == ""


# ── Runner assertion branch ─────────────────────────────────────


class TestAssertionVisible:
    def test_visible_element_passes(self, tmp_path):
        """Plan scenario #1."""
        page = _FakePage({"role=heading[name=\"Welcome\"]": True})
        step = AutomationStep(
            action="expect_visible",
            target='role=heading[name="Welcome"]',
            kind="assertion", assertion_type="visible",
            raw="expect(page.get_by_role(...)).to_be_visible()",
        )
        runner = _make_runner(tmp_path)
        sr = runner._run_step(page, step, 1, str(tmp_path))
        assert sr.status == "passed", sr.comment

    def test_missing_element_fails_with_clear_comment(self, tmp_path):
        """Plan scenario #2 — the run continues; assertion is failed."""
        page = _FakePage({})  # nothing visible
        step = AutomationStep(
            action="expect_visible",
            target="data-testid=missing",
            kind="assertion", assertion_type="visible",
            raw="expect missing",
        )
        runner = _make_runner(tmp_path)
        sr = runner._run_step(page, step, 2, str(tmp_path))
        assert sr.status == "failed"
        assert "Assert visible" in sr.comment

    def test_visible_walks_alternates_from_pr_a_chain(self, tmp_path):
        """Assertion+visible must reuse PR-A's multi-locator fallback
        — the contract pinned in [[project-sprint8-recorder-pra]]."""
        page = _FakePage({
            "data-testid=primary":  False,   # primary missing
            "role=button":          True,    # alternate wins
        })
        step = AutomationStep(
            action="expect_visible",
            target="data-testid=primary",
            target_alternates=["role=button"],
            kind="assertion", assertion_type="visible",
            raw="expect alternate",
        )
        runner = _make_runner(tmp_path)
        sr = runner._run_step(page, step, 1, str(tmp_path))
        assert sr.status == "passed", sr.comment


class TestAssertionText:
    def test_text_substring_match_via_get_by_text(self, tmp_path):
        """Plan scenario #3."""
        page = _FakePage({"text=Hello world": True})
        step = AutomationStep(
            action="expect_text", value="Hello world",
            kind="assertion", assertion_type="text",
            raw="expect text",
        )
        runner = _make_runner(tmp_path)
        sr = runner._run_step(page, step, 1, str(tmp_path))
        assert sr.status == "passed", sr.comment

    def test_text_falls_back_to_content_scan(self, tmp_path):
        """When get_by_text misses, the runner scans page.content() —
        catches the attribute-only case (placeholder, aria-label)."""
        page = _FakePage({}, content="<html>only inside attr</html>")
        step = AutomationStep(
            action="expect_text", value="only inside attr",
            kind="assertion", assertion_type="text",
            raw="expect text in content",
        )
        runner = _make_runner(tmp_path)
        sr = runner._run_step(page, step, 1, str(tmp_path))
        assert sr.status == "passed", sr.comment

    def test_text_missing_fails(self, tmp_path):
        page = _FakePage({}, content="<html>nothing matches</html>")
        step = AutomationStep(
            action="expect_text", value="this is absent",
            kind="assertion", assertion_type="text",
        )
        runner = _make_runner(tmp_path)
        sr = runner._run_step(page, step, 1, str(tmp_path))
        assert sr.status == "failed"
        assert "Assert text not found" in sr.comment


class TestAssertionUrl:
    def test_url_glob_match_passes_regardless_of_query(self, tmp_path):
        """Plan scenario #4."""
        page = _FakePage({}, url="https://app.example.com/dashboard?id=42")
        step = AutomationStep(
            action="expect_url",
            target="https://app.example.com/dashboard*",
            kind="assertion", assertion_type="url",
        )
        runner = _make_runner(tmp_path)
        sr = runner._run_step(page, step, 1, str(tmp_path))
        assert sr.status == "passed", sr.comment

    def test_url_substring_match_no_glob_chars(self, tmp_path):
        page = _FakePage({}, url="https://app.example.com/contact?x=1")
        step = AutomationStep(
            action="expect_url",
            target="example.com/contact",
            kind="assertion", assertion_type="url",
        )
        runner = _make_runner(tmp_path)
        sr = runner._run_step(page, step, 1, str(tmp_path))
        assert sr.status == "passed", sr.comment

    def test_url_mismatch_fails(self, tmp_path):
        page = _FakePage({}, url="https://app.example.com/login")
        step = AutomationStep(
            action="expect_url",
            target="https://app.example.com/dashboard*",
            kind="assertion", assertion_type="url",
        )
        runner = _make_runner(tmp_path)
        sr = runner._run_step(page, step, 1, str(tmp_path))
        assert sr.status == "failed"
        assert "Assert URL" in sr.comment


class TestStepLabel:
    def test_action_step_label_unchanged(self):
        step = AutomationStep(action="click", target="data-testid=ok")
        assert _step_label(step) == "click"

    def test_assertion_step_label_shows_type(self):
        step = AutomationStep(action="expect_visible",
                                kind="assertion", assertion_type="visible")
        assert _step_label(step) == "assert visible"


# ── Recorder parser: codegen expect(...) recognition ────────────


class TestCodegenAssertionParser:
    """Plan scenario: ``Ctrl+Shift+A`` hot-key — implemented via
    codegen's native Assert toolbar buttons which emit ``expect(...)``
    calls. The parser must round-trip them into assertion steps."""

    def test_expect_visible_recognised_as_assertion_visible(self):
        src = (
            "async def run(page):\n"
            "    await page.goto('https://app.example.com/')\n"
            "    await expect(page.get_by_role('heading', "
            "name='Welcome')).to_be_visible()\n"
        )
        steps = parse_codegen_output(src)
        assert len(steps) == 2
        assert steps[0].action == "goto"
        assert steps[1].kind == "assertion"
        assert steps[1].assertion_type == "visible"
        assert steps[1].target == 'role=heading[name="Welcome"]'

    def test_expect_to_have_url_recognised_as_assertion_url(self):
        src = (
            "async def run(page):\n"
            "    await expect(page).to_have_url("
            "'https://app.example.com/dashboard*')\n"
        )
        steps = parse_codegen_output(src)
        assert len(steps) == 1
        assert steps[0].kind == "assertion"
        assert steps[0].assertion_type == "url"
        assert steps[0].target == "https://app.example.com/dashboard*"

    def test_expect_to_contain_text_recognised_as_assertion_text(self):
        src = (
            "async def run(page):\n"
            "    await expect(page.get_by_text('Hello')).to_contain_text("
            "'Hello world')\n"
        )
        steps = parse_codegen_output(src)
        assert len(steps) == 1
        assert steps[0].kind == "assertion"
        assert steps[0].assertion_type == "text"
        assert steps[0].value == "Hello world"
        assert steps[0].target == "text=Hello"

    def test_action_and_assertion_interleave(self):
        """Mixed sequence: click then assert visible — the parser must
        keep relative order so the runner replays the user's flow."""
        src = (
            "async def run(page):\n"
            "    await page.get_by_role('button', name='Sign in').click()\n"
            "    await expect(page.get_by_text('Welcome')).to_be_visible()\n"
            "    await page.get_by_label('Email').fill('a@b.c')\n"
        )
        steps = parse_codegen_output(src)
        assert [s.kind for s in steps] == ["action", "assertion", "action"]
        assert steps[0].action == "click"
        assert steps[1].assertion_type == "visible"
        assert steps[2].action == "fill"


# ── TC editor POST endpoint round-trip ──────────────────────────


@pytest.fixture
def editor_project(client):
    """Plant a project + one TC with a recorded action step so the
    editor POST has something to patch."""
    pid = db.upsert_project(
        name=f"editor-prc-{os.urandom(4).hex()}",
        base_url="https://app.example.com",
    )
    db.save_test_cases(pid, [
        {"id": "TC-EDIT", "section": "Sign in", "section_num": 1,
         "summary": "Sign in", "preconditions": "",
         "test_steps": "1. Click submit", "test_data": "",
         "expected_result": "Welcome", "issues": "", "comment": "",
         "user_story_id": "US-1", "category": "Positive",
         "priority": "High", "status": "Unchecked",
         "testing_type": "Functional", "url_pattern": "",
         "trigger": "manual"},
    ])
    db.update_tc_automation_steps(pid, "TC-EDIT", [
        {"action": "click",
         "target": 'role=button[name="Sign in"]',
         "value": "", "raw": "click", "comment": ""},
        {"action": "expect_visible",
         "target": "role=heading[name=\"Welcome\"]",
         "value": "", "raw": "expect", "comment": ""},
    ])
    with client.session_transaction() as s:
        s["project_id"] = pid
        s["active_project_id"] = pid
        s["test_cases_data"] = db.load_test_cases(pid)
        s["_session_active_since"] = 9_999_999_999
    yield pid
    db.delete_project(pid)


class TestEditorDropdownRoundTrip:
    """Plan scenario #5: change kind to assertion → POST → reload →
    correct kind/type persisted to DB."""

    def test_post_updates_kind_and_persists(self, client, editor_project):
        pid = editor_project
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post(
                "/test-cases/TC-EDIT/automation-step-kind",
                json={"index": 1, "kind": "assertion",
                       "assertion_type": "visible"},
            )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["ok"] is True
        assert body["changed"] == 1

        # Reload from DB — runner's source of truth.
        tcs = db.load_test_cases(pid)
        tc = next(t for t in tcs if t["id"] == "TC-EDIT")
        steps = json.loads(tc["automation_steps_json"])
        assert steps[1]["kind"] == "assertion"
        assert steps[1]["assertion_type"] == "visible"
        # Untouched neighbour stays as action.
        assert steps[0].get("kind", "action") == "action"

    def test_post_rejects_invalid_kind(self, client, editor_project):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post(
                "/test-cases/TC-EDIT/automation-step-kind",
                json={"index": 0, "kind": "bogus"},
            )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_kind"

    def test_post_rejects_invalid_assertion_type(self, client, editor_project):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post(
                "/test-cases/TC-EDIT/automation-step-kind",
                json={"index": 0, "kind": "assertion",
                       "assertion_type": "rainbow"},
            )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_assertion_type"

    def test_post_rejects_out_of_range_index(self, client, editor_project):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post(
                "/test-cases/TC-EDIT/automation-step-kind",
                json={"index": 99, "kind": "action"},
            )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "index_out_of_range"

    def test_post_returns_403_when_flag_off(self, client, editor_project):
        env = {k: v for k, v in os.environ.items() if k != "RECORDER_ENABLED"}
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ["FLASK_DEBUG"] = "1"
            resp = client.post(
                "/test-cases/TC-EDIT/automation-step-kind",
                json={"index": 0, "kind": "action"},
            )
        assert resp.status_code == 403

    def test_post_unknown_tc_returns_404(self, client, editor_project):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post(
                "/test-cases/TC-MISSING/automation-step-kind",
                json={"index": 0, "kind": "action"},
            )
        assert resp.status_code == 404


class TestEditorDropdownRendering:
    """The kind dropdown must render whenever a TC has recorded steps —
    the operator's only path to flip kind without re-recording."""

    def test_dropdown_appears_for_recorded_tc(self, client, editor_project):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.get("/test-cases")
        body = resp.get_data(as_text=True)
        assert "data-step-editor=\"TC-EDIT\"" in body
        # Both recorded steps surface as <select> rows with the
        # data-index attribute the JS uses for POSTs.
        assert 'data-step-kind-select' in body
        assert 'data-index="0"' in body
        assert 'data-index="1"' in body
