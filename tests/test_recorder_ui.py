"""PR-B/7 tests — Record button block in templates/test_cases.html.

Pins the contract:

* The Recorder ``<details>`` block renders per-TC only when the host has
  RECORDER_ENABLED=1.
* The pre-filled CLI command carries the active project_id + TC external
  id so the tester can copy it without editing project metadata.
* TCs with recorded steps (``automation_steps_json`` non-empty) surface
  a "Has recorded steps ✓" badge — testers see at a glance which TCs
  already have a capture.

PR-E's session banner is pinned by ``TestTheSessionBannerIsReachable``,
and it is a separate contract from the per-TC block above. The per-TC
``<details>`` genuinely needs a case to attach to; the session banner is
a way to *create* the first cases, and it shipped inside the same
``has_data and test_cases`` gate as the case list — so the entry point
sat behind the thing it produces and a new project never saw it.

``TestTheBlockedPopupIsRecoverable`` pins the other half. The launch
handler opens the recording tab with ``window.open`` after an await,
which is exactly where a browser blocks a popup, and it used to announce
success without reading the return value: green tick, modal closed, no
tab, token spent, nothing on screen to recover from.
"""
from __future__ import annotations

import json
import os
from unittest import mock

import pytest

from engine import db


@pytest.fixture
def seeded_session(client):
    """Plant a project + two TCs (one with recording, one without) in
    the session so the /test-cases GET renders both rows."""
    pid = db.upsert_project(
        name=f"recorder-ui-{os.urandom(4).hex()}",
        base_url="https://app.example.com",
    )
    db.save_test_cases(pid, [
        {"id": "TC-PLAIN", "section": "Login", "section_num": 1,
         "summary": "Sign in", "preconditions": "",
         "test_steps": "1. Open\n2. Submit", "test_data": "",
         "expected_result": "Welcome", "issues": "", "comment": "",
         "user_story_id": "US-1", "category": "Positive",
         "priority": "High", "status": "Unchecked",
         "testing_type": "Functional", "url_pattern": "",
         "trigger": "manual"},
        {"id": "TC-RECORDED", "section": "Checkout", "section_num": 2,
         "summary": "Pay", "preconditions": "",
         "test_steps": "1. Checkout", "test_data": "",
         "expected_result": "Order created", "issues": "", "comment": "",
         "user_story_id": "US-2", "category": "Positive",
         "priority": "High", "status": "Unchecked",
         "testing_type": "Functional", "url_pattern": "",
         "trigger": "manual"},
    ])
    db.update_tc_automation_steps(pid, "TC-RECORDED", [
        {"action": "goto", "target": "https://app.example.com/cart",
         "value": "", "raw": "page.goto(...)", "comment": ""}
    ])
    with client.session_transaction() as s:
        s["project_id"] = pid
        s["test_cases_data"] = db.load_test_cases(pid)
        s["_session_active_since"] = 9_999_999_999
    yield pid
    db.delete_project(pid)


class TestRecorderBlockVisibility:
    def test_renders_when_flag_on(self, client, seeded_session):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.get("/test-cases")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "tc-recorder-edit" in body
        assert "Record steps" in body
        # CLI command pre-fills the TC ids.
        assert "--tc TC-PLAIN" in body
        assert "--tc TC-RECORDED" in body

    def test_hidden_when_flag_off(self, client, seeded_session):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "0"}):
            resp = client.get("/test-cases")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "tc-recorder-edit" not in body
        assert "Record steps" not in body

    def test_hidden_when_flag_unset(self, client, seeded_session):
        # Strip the env var entirely — default state must hide the
        # block so a host that never opted in stays clean.
        env = {k: v for k, v in os.environ.items() if k != "RECORDER_ENABLED"}
        with mock.patch.dict(os.environ, env, clear=True):
            # Also re-set FLASK_DEBUG so init_db's guard still passes.
            os.environ["FLASK_DEBUG"] = "1"
            resp = client.get("/test-cases")
        assert resp.status_code == 200
        assert "tc-recorder-edit" not in resp.get_data(as_text=True)


class TestRecordedStepsBadge:
    def test_badge_shown_when_steps_present(self, client, seeded_session):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.get("/test-cases")
        body = resp.get_data(as_text=True)
        # Sanity: the badge must appear exactly once across the entire
        # response — once for TC-RECORDED, never for TC-PLAIN.
        assert body.count("Has recorded steps") == 1
        # And that one occurrence must sit between the TC-RECORDED
        # card opening and the next TC card (or end of section).
        badge_idx = body.index("Has recorded steps")
        tc_recorded_idx = body.index('id="TC-RECORDED"')
        tc_plain_idx = body.index('id="TC-PLAIN"')
        # Both cards exist; whichever appears later defines the card
        # boundary the badge must fall before.
        next_card_idx = max(tc_recorded_idx, tc_plain_idx)
        if next_card_idx == tc_recorded_idx:
            # TC-RECORDED renders second — the badge must come after
            # its anchor and before EOF.
            assert badge_idx > tc_recorded_idx
        else:
            # TC-RECORDED renders first — badge must sit between its
            # anchor and the next TC's anchor.
            assert tc_recorded_idx < badge_idx < tc_plain_idx


class TestSessionRecorderModalToggle:
    """PR-E hotfix — the "Start session recording" modal must hide via the
    ``hidden`` attribute the JS toggles.

    The original markup set ``style="...display:flex..."`` inline on the
    overlay. Inline declarations out-specify the user-agent
    ``[hidden] { display: none }`` rule, so the overlay rendered on page
    load and ``modal.hidden = true`` (Cancel / Launch / backdrop click)
    silently failed — the popup could never close. Same gotcha PR-A/PR-H
    fixed for the bulk toolbar and reset-project modal: scope the visible
    layout to ``:not([hidden])`` and add an explicit
    ``[hidden] { display: none !important }`` fallback.
    """

    def test_overlay_uses_class_not_inline_display(self, client, seeded_session):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.get("/test-cases")
        body = resp.get_data(as_text=True)
        # The overlay wrapper exists, is hidden by default, and carries
        # the layout class rather than an inline display.
        assert 'id="ext-recorder-modal"' in body
        assert 'class="ext-recorder-modal"' in body
        modal_tag = body[body.index('id="ext-recorder-modal"'):
                         body.index('id="ext-recorder-modal"') + 200]
        assert "hidden" in modal_tag, "modal must start hidden"
        assert "display:flex" not in modal_tag and "display: flex" not in modal_tag, (
            "inline display on the overlay out-specifies the UA "
            "[hidden] rule, so the hidden attribute can never hide it"
        )

    def test_overlay_css_scopes_visible_to_not_hidden(self, client, seeded_session):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.get("/test-cases")
        body = resp.get_data(as_text=True)
        # The visible-state rule must be scoped to :not([hidden]) and an
        # explicit [hidden] { display: none !important } must exist so the
        # JS toggle wins regardless of source order / future edits.
        assert ".ext-recorder-modal:not([hidden])" in body
        assert ".ext-recorder-modal[hidden]" in body
        hidden_rule_idx = body.index(".ext-recorder-modal[hidden]")
        hidden_rule = body[hidden_rule_idx:hidden_rule_idx + 80]
        assert "none" in hidden_rule and "!important" in hidden_rule


class TestExtensionDeepLinkHook:
    """The extension popup opens `/test-cases#tfg-record=<url>` to kick
    off recording. The page must carry the JS hook that reads that hash,
    pre-fills the modal, and reuses the Launch path. Pinned here so the
    popup→page contract can't silently drift."""

    def test_autolaunch_hook_present(self, client, seeded_session):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.get("/test-cases")
        body = resp.get_data(as_text=True)
        assert "tfg-record=" in body, (
            "page must read the extension's #tfg-record=<url> deep link"
        )
        assert "maybeAutoLaunchFromHash" in body

    def test_hook_inert_when_flag_off(self, client, seeded_session):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "0"}):
            resp = client.get("/test-cases")
        body = resp.get_data(as_text=True)
        # The hook lives in the always-present script but guards on the
        # modal existing; with the flag off the modal isn't rendered, so
        # the auto-launch can never fire. Pin that the surface is gone.
        assert 'id="ext-recorder-modal"' not in body
        assert 'id="ext-recorder-start"' not in body


class TestCliCommandPrefill:
    def test_command_includes_project_id(self, client, seeded_session):
        pid = seeded_session
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.get("/test-cases")
        body = resp.get_data(as_text=True)
        assert f"--project {pid}" in body
        # And the JS data-attribute carries the same so the click
        # handler's dynamic-rebuild path produces the same command.
        assert f'data-project="{pid}"' in body


# ── PR-E: the session banner ─────────────────────────────────────────

@pytest.fixture
def empty_project(client):
    """A project with no test cases — a new project, in other words."""
    pid = db.upsert_project(
        name=f"recorder-empty-{os.urandom(4).hex()}",
        base_url="https://app.example.com",
    )
    with client.session_transaction() as s:
        s["project_id"] = pid
        s["test_cases_data"] = []
        s["_session_active_since"] = 9_999_999_999
    yield pid
    db.delete_project(pid)


class TestTheSessionBannerIsReachable:
    def test_it_renders_before_any_test_case_exists(self, client,
                                                    empty_project):
        """The reported bug, pinned at the case that hit it.

        Recording a session is how a project with nothing in it gets its
        first cases. Gating the button on cases already existing made it
        unreachable exactly then.
        """
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            body = client.get("/test-cases").get_data(as_text=True)
        assert 'id="ext-recorder-start"' in body
        assert 'id="ext-recorder-modal"' in body

    def test_it_still_renders_once_cases_exist(self, client,
                                               seeded_session):
        # Moving the block must not have moved it out of the page.
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            body = client.get("/test-cases").get_data(as_text=True)
        assert 'id="ext-recorder-start"' in body

    def test_the_flag_still_governs_it(self, client, empty_project):
        # The surface stays invisible on a host that is not in the pilot.
        env = {k: v for k, v in os.environ.items()
               if k != "RECORDER_ENABLED"}
        with mock.patch.dict(os.environ, env, clear=True):
            body = client.get("/test-cases").get_data(as_text=True)
        assert 'id="ext-recorder-start"' not in body


class TestTheBlockedPopupIsRecoverable:
    """The markup half. The behaviour is JS and is walked in a browser.

    Asserting on script text is weak evidence, so this checks the two
    things a server test can actually establish: the element the recovery
    link is written into exists and ships hidden, and the handler reads
    what ``window.open`` returned instead of assuming it worked.
    """

    def test_the_page_carries_a_hidden_slot_for_the_manual_link(
            self, client, empty_project):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            body = client.get("/test-cases").get_data(as_text=True)
        assert 'id="ext-recorder-manual"' in body
        # Ships hidden: an empty paragraph reserving space under the
        # status line would read as a rendering fault.
        slot = body[body.index('id="ext-recorder-manual"'):][:120]
        assert "hidden" in slot

    def test_the_handler_reads_the_window_open_result(self, client,
                                                      empty_project):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            body = client.get("/test-cases").get_data(as_text=True)
        assert "const opened = window.open(handoffUrl" in body
        assert "if (!opened)" in body
        # And does not claim success before knowing.
        opened_at = body.index("const opened = window.open(handoffUrl")
        success_at = body.index("Opening recording tab")
        assert opened_at < success_at, (
            "the success message is painted before the result is read")

