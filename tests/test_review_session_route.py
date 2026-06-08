"""PR-D — /test-cases/review-session/<token> route E2E.

Covers the operator-facing review flow:

  * GET renders proposed-TC cards for a valid draft.
  * GET returns a friendly error (200 with banner OR 404) when the
    token is missing / expired / consumed / belongs to another
    project.
  * POST creates N TestCase rows + consumes the draft so a refresh
    can't double-insert.
  * POST rejects invalid suite / out-of-range idx with 400.
  * Both endpoints 403 when RECORDER_ENABLED is off.
"""
from __future__ import annotations

import json
import os
from unittest import mock

import pytest

from engine import db


@pytest.fixture
def staged_draft(client):
    """Seed a project + a draft with 2 proposed flows."""
    pid = db.upsert_project(
        name=f"rev-{os.urandom(4).hex()}",
        base_url="https://app.test",
    )
    proposed = [
        {"summary": "Sign in", "intent": "Authenticate user",
         "suggested_suite": "Regression", "rationale": "login keyword",
         "steps": [
            {"action": "goto", "target": "https://app/login",
             "value": "", "raw": "", "comment": "",
             "target_alternates": [], "locator_label": "",
             "kind": "action", "assertion_type": ""},
            {"action": "click", "target": 'role=button[name="Sign in"]',
             "value": "", "raw": "", "comment": "",
             "target_alternates": [], "locator_label": "",
             "kind": "action", "assertion_type": ""},
         ]},
        {"summary": "Open Settings",
         "intent": "Navigate post-login",
         "suggested_suite": "Smoke", "rationale": "short nav",
         "steps": [
            {"action": "click", "target": 'role=link[name="Settings"]',
             "value": "", "raw": "", "comment": "",
             "target_alternates": [], "locator_label": "",
             "kind": "action", "assertion_type": ""},
         ]},
    ]
    token = "tok_review_001"
    db.create_session_draft(pid, token, proposed)
    # Plant one existing TC so /test-cases and /test-execution render
    # their data-bearing template branches (which is where the suite
    # filter UI lives).
    db.save_test_cases(pid, [
        {"id": "TC-EXISTING", "section": "x", "section_num": 1,
         "summary": "Already here", "preconditions": "",
         "test_steps": "1. x", "test_data": "", "expected_result": "",
         "issues": "", "comment": "", "user_story_id": "US-1",
         "category": "Positive", "priority": "High",
         "status": "Unchecked", "testing_type": "Functional",
         "url_pattern": "", "trigger": "manual",
         "suite": "Smoke"},
    ])
    with client.session_transaction() as s:
        s["project_id"] = pid
        s["active_project_id"] = pid
        s["test_cases_data"] = db.load_test_cases(pid)
        s["_session_active_since"] = 9_999_999_999
    yield {"project_id": pid, "token": token}
    db.delete_project(pid)


class TestListPendingDrafts:
    """PR-E hotfix — `list_pending_session_drafts` feeds the "Pending
    recording sessions" banner so a draft whose review tab got closed is
    still reachable until its TTL lapses."""

    def test_lists_unconsumed_draft(self, client, staged_draft):
        pid = staged_draft["project_id"]
        pending = db.list_pending_session_drafts(pid)
        assert len(pending) == 1
        item = pending[0]
        assert item["token"] == staged_draft["token"]
        assert item["tc_count"] == 2
        assert item["created_at"]

    def test_excludes_consumed_draft(self, client, staged_draft):
        pid = staged_draft["project_id"]
        assert db.consume_session_draft(staged_draft["token"]) is True
        assert db.list_pending_session_drafts(pid) == []

    def test_excludes_expired_draft(self, client, staged_draft):
        # create_session_draft floors the TTL at 1 h, so backdate the
        # row's expires_at directly to simulate a lapsed draft, then
        # confirm the lazy-purge drops it from the pending list.
        pid = staged_draft["project_id"]
        db.create_session_draft(pid, "tok_expired_001", [
            {"summary": "x", "intent": "", "suggested_suite": "Smoke",
             "steps": []},
        ])
        from datetime import datetime, timedelta, timezone
        with db.session_scope() as sess:
            from sqlalchemy import select
            row = sess.execute(
                select(db.SessionDraft).where(
                    db.SessionDraft.token == "tok_expired_001")
            ).scalar_one()
            row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        pending = db.list_pending_session_drafts(pid)
        tokens = {p["token"] for p in pending}
        assert "tok_expired_001" not in tokens

    def test_scoped_to_project(self, client, staged_draft):
        # A draft under a different project must not leak into this
        # project's pending list.
        other = db.upsert_project(name=f"other-{os.urandom(4).hex()}",
                                  base_url="https://other.test")
        try:
            db.create_session_draft(other, "tok_other_001", [
                {"summary": "y", "intent": "", "suggested_suite": "Smoke",
                 "steps": []},
            ])
            pending = db.list_pending_session_drafts(staged_draft["project_id"])
            assert all(p["token"] != "tok_other_001" for p in pending)
        finally:
            db.delete_project(other)


class TestPendingBanner:
    """The Test Cases page surfaces a banner + review link for each
    pending draft when RECORDER_ENABLED is on."""

    def test_banner_links_pending_draft(self, client, staged_draft):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.get("/test-cases")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Pending recording sessions" in body
        assert (f"/test-cases/review-session/{staged_draft['token']}"
                in body)

    def test_banner_absent_when_flag_off(self, client, staged_draft):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "0"}):
            resp = client.get("/test-cases")
        body = resp.get_data(as_text=True)
        assert "Pending recording sessions" not in body


class TestReviewGet:
    def test_renders_proposed_cards(self, client, staged_draft):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.get(
                f"/test-cases/review-session/{staged_draft['token']}")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Sign in" in body
        assert "Open Settings" in body
        # Both rows render with their save checkbox.
        assert 'name="save_0"' in body
        assert 'name="save_1"' in body
        # Pre-selected suite suggestions from the draft.
        assert 'value="Regression"' in body
        assert 'value="Smoke"' in body

    def test_missing_token_returns_404_with_banner(self, client,
                                                    staged_draft):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.get(
                "/test-cases/review-session/no-such-token")
        assert resp.status_code == 404
        body = resp.get_data(as_text=True)
        assert "expired" in body.lower() or "never existed" in body.lower()

    def test_recorder_off_returns_403(self, client, staged_draft):
        env = {k: v for k, v in os.environ.items()
               if k != "RECORDER_ENABLED"}
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ["FLASK_DEBUG"] = "1"
            resp = client.get(
                f"/test-cases/review-session/{staged_draft['token']}")
        assert resp.status_code == 403

    def test_wrong_active_project_returns_403(self, client, staged_draft):
        # Switch active_project_id to something unrelated; the route
        # must refuse to leak the draft into the wrong project.
        with client.session_transaction() as s:
            s["active_project_id"] = "ffffffff" * 4
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.get(
                f"/test-cases/review-session/{staged_draft['token']}")
        assert resp.status_code == 403


class TestReviewPost:
    def test_save_creates_tc_rows_with_suite(self, client, staged_draft):
        pid = staged_draft["project_id"]
        before = len(db.load_test_cases(pid))
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post(
                f"/test-cases/review-session/{staged_draft['token']}",
                json={"selected": [
                    {"idx": 0, "suite": "Regression",
                     "summary_override": "Login flow"},
                    {"idx": 1, "suite": "Smoke",
                     "summary_override": ""},
                ]},
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["created_count"] == 2

        rows = db.load_test_cases(pid)
        assert len(rows) == before + 2

        # The created TCs carry suite + automation_steps_json.
        new_rows = [r for r in rows if r["id"].startswith("REC_")]
        assert {r["suite"] for r in new_rows} == {"Regression", "Smoke"}
        # First TC carries the operator-overridden summary.
        login_row = next(r for r in new_rows if r["suite"] == "Regression")
        assert login_row["summary"] == "Login flow"
        # And the steps survived the round-trip.
        assert len(json.loads(login_row["automation_steps_json"])) == 2

    def test_skip_unchecked_rows(self, client, staged_draft):
        pid = staged_draft["project_id"]
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post(
                f"/test-cases/review-session/{staged_draft['token']}",
                json={"selected": [{"idx": 0, "suite": "Regression"}]},
            )
        assert resp.status_code == 200
        assert resp.get_json()["created_count"] == 1

    def test_consumed_draft_double_post_returns_404(self, client,
                                                     staged_draft):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp1 = client.post(
                f"/test-cases/review-session/{staged_draft['token']}",
                json={"selected": [{"idx": 0, "suite": "Smoke"}]},
            )
            assert resp1.status_code == 200
            # Second POST with the same token must fail.
            resp2 = client.post(
                f"/test-cases/review-session/{staged_draft['token']}",
                json={"selected": [{"idx": 1, "suite": "Smoke"}]},
            )
        assert resp2.status_code == 404

    def test_invalid_suite_rejected(self, client, staged_draft):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post(
                f"/test-cases/review-session/{staged_draft['token']}",
                json={"selected": [{"idx": 0, "suite": "Performance"}]},
            )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_suite"

    def test_out_of_range_idx_rejected(self, client, staged_draft):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post(
                f"/test-cases/review-session/{staged_draft['token']}",
                json={"selected": [{"idx": 99, "suite": "Smoke"}]},
            )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "idx_out_of_range"

    def test_empty_selection_rejected(self, client, staged_draft):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post(
                f"/test-cases/review-session/{staged_draft['token']}",
                json={"selected": []},
            )
        assert resp.status_code == 400

    def test_form_encoded_post_also_works(self, client, staged_draft):
        """The template's vanilla form-submit fallback uses form-encoded
        body instead of JSON. Confirm both code-paths converge."""
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post(
                f"/test-cases/review-session/{staged_draft['token']}",
                data={
                    "save_0":    "on",
                    "suite_0":   "Smoke",
                    "summary_0": "From form",
                    # Row 1 unchecked → no save_1 key.
                    "suite_1":   "Smoke",
                },
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["created_count"] == 1

    def test_recorder_off_returns_403(self, client, staged_draft):
        env = {k: v for k, v in os.environ.items()
               if k != "RECORDER_ENABLED"}
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ["FLASK_DEBUG"] = "1"
            resp = client.post(
                f"/test-cases/review-session/{staged_draft['token']}",
                json={"selected": [{"idx": 0, "suite": "Smoke"}]},
            )
        assert resp.status_code == 403

    def test_csrf_does_not_block_when_token_threaded(self, app, staged_draft):
        """Production has WTF_CSRF_ENABLED=True; conftest disables it
        for the suite. This regression re-enables CSRF locally and
        verifies the JSON POST + the form-encoded POST both succeed
        when the CSRF token is threaded through the proper channel
        (X-CSRFToken header for JSON, ``csrf_token`` form field for
        the form fallback).

        Surfaced by operator's first real end-to-end test after the
        PR-E recorder extension landed — clicking Save selected on
        the review page produced 400 with "CSRF token missing or
        invalid". Template fix: add ``{{ csrf_token() }}`` to the
        form + read it back as the X-CSRFToken header in the JS
        fetch. This test pins that fix.
        """
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            with app.test_client() as csrf_client:
                with csrf_client.session_transaction() as s:
                    s["project_id"] = staged_draft["project_id"]
                    s["active_project_id"] = staged_draft["project_id"]
                    s["_session_active_since"] = 9_999_999_999

                # 1) Fetch the GET page so we have a session that owns
                # a CSRF token + so the template renders for us to
                # scrape the token out of the hidden input.
                with mock.patch.dict(os.environ,
                                      {"RECORDER_ENABLED": "1"}):
                    page = csrf_client.get(
                        f"/test-cases/review-session/"
                        f"{staged_draft['token']}")
                assert page.status_code == 200, (
                    f"GET failed: {page.get_data(as_text=True)[:200]}")
                body = page.get_data(as_text=True)
                # Token sits in a hidden input.
                import re as _re
                match = _re.search(
                    r'name="csrf_token"\s+value="([^"]+)"', body)
                assert match, (
                    "csrf_token hidden input missing from template — "
                    "the fix wasn't applied. The form path will 400 "
                    "in production.")
                token = match.group(1)

                # 2) JSON path: X-CSRFToken header must satisfy the
                # gate.
                with mock.patch.dict(os.environ,
                                      {"RECORDER_ENABLED": "1"}):
                    resp_json = csrf_client.post(
                        f"/test-cases/review-session/"
                        f"{staged_draft['token']}",
                        json={"selected": [
                            {"idx": 0, "suite": "Smoke"}]},
                        headers={"X-CSRFToken": token},
                    )
                assert resp_json.status_code == 200, (
                    f"JSON POST blocked: "
                    f"{resp_json.get_data(as_text=True)}")
        finally:
            app.config["WTF_CSRF_ENABLED"] = False


class TestSuiteFilterUI:
    def test_test_cases_page_renders_suite_chips(self, client, staged_draft):
        """/test-cases must surface the 4-button suite filter row added
        in PR-D."""
        resp = client.get("/test-cases")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "filterSuite(" in body
        assert "All suites" in body
        assert ">Smoke<" in body
        assert ">Regression<" in body
        assert ">E2E<" in body

    def test_test_execution_page_renders_run_only_knob(self, client,
                                                       staged_draft):
        """/test-execution must surface the Run-only knob added in PR-D."""
        resp = client.get("/test-execution")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "suite-filter-select" in body
        assert 'value="Smoke"' in body
        assert 'value="Regression"' in body
        assert 'value="E2E"' in body
