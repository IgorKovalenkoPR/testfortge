"""Sprint 5 follow-up — TC walkthrough-meta editor on /test-cases.

PR #12 (merged) added the walkthrough findings subtab + Run-mode
radio, but the per-TC ``url_pattern`` / ``trigger`` columns that the
walkthrough TC-matcher reads were not surfaced in any editor. This
follow-up patches:

* :class:`engine.testcase_generator.TestCase` — two new fields with
  backward-compatible defaults (``url_pattern=""``, ``trigger="manual"``).
* :func:`engine.db.save_test_cases` / :func:`engine.db.load_test_cases` —
  round-trip the columns the PR-2 migration created.
* :func:`routes._shared.tc_to_dict` — preserves the fields when the
  dataclass is shoved into the session.
* ``POST /test-cases/<tc_id>/walkthrough-meta`` — inline edit endpoint
  that the new collapsible form on the TC card submits to.

These tests pin the round-trip + the route's input sanitisation
(unknown ``trigger`` values fall back to ``manual``, never silently
opt a TC into walkthrough firing).
"""

from __future__ import annotations

import json

import pytest


# ── Shared seed helper ───────────────────────────────────────────


def _seed_two_tcs(client):
    """Two TCs with distinct walkthrough metadata. Pins
    ``_session_active_since`` so app.before_request doesn't wipe the
    pack before the assertions run."""
    with client.session_transaction() as s:
        s["test_cases_data"] = [
            {"id": "TC-001", "section": "Login", "section_num": 1,
             "summary": "Login happy path",
             "preconditions": "", "test_steps": "1. Open / 2. Submit",
             "test_data": "", "expected_result": "Welcome page shows",
             "issues": "", "comment": "", "user_story_id": "US-1",
             "category": "Positive", "priority": "High",
             "status": "Unchecked", "testing_type": "Functional",
             "url_pattern": "", "trigger": "manual"},
            {"id": "TC-002", "section": "Checkout", "section_num": 2,
             "summary": "Checkout submits",
             "preconditions": "Logged in", "test_steps": "1. Add\n2. Pay",
             "test_data": "card=4242", "expected_result": "Order saved",
             "issues": "", "comment": "", "user_story_id": "US-2",
             "category": "Positive", "priority": "Critical",
             "status": "Unchecked", "testing_type": "Functional",
             "url_pattern": "*/checkout/*",
             "trigger": "walkthrough_url_match"},
        ]
        s["_session_active_since"] = 9_999_999_999


# ── 1. Schema round-trip on dataclass + session ─────────────────


class TestSchemaRoundTrip:
    def test_testcase_dataclass_defaults_are_backward_compatible(self):
        """Pre-Sprint-5 callers construct ``TestCase`` without the new
        fields. Defaults must mirror the DB server_defaults so the
        walkthrough TC-matcher's ``trigger == "manual"`` filter keeps
        every legacy TC out of the walkthrough firing path."""
        from engine.testcase_generator import TestCase
        tc = TestCase(
            id="TC-LEGACY-1", section="X", section_num=1, summary="x",
            preconditions="", test_steps="1. step", test_data="",
            expected_result="y",
        )
        assert tc.url_pattern == ""
        assert tc.trigger == "manual"

    def test_tc_to_dict_preserves_walkthrough_fields(self):
        from engine.testcase_generator import TestCase
        from routes._shared import tc_to_dict
        tc = TestCase(
            id="TC-RT-1", section="Checkout", section_num=1, summary="x",
            preconditions="", test_steps="", test_data="",
            expected_result="",
            url_pattern="*/cart/*", trigger="walkthrough_url_match",
        )
        d = tc_to_dict(tc)
        assert d["url_pattern"] == "*/cart/*"
        assert d["trigger"] == "walkthrough_url_match"

    def test_reconstruct_handles_legacy_session_blobs(self):
        """Session blobs persisted before Sprint 5 don't carry the
        new keys. ``reconstruct_test_cases`` must still produce
        valid dataclass instances by leaning on the field defaults."""
        from routes._shared import reconstruct_test_cases
        rebuilt = reconstruct_test_cases([{
            "id": "TC-OLD-1", "section": "X", "section_num": 1,
            "summary": "x", "preconditions": "", "test_steps": "",
            "test_data": "", "expected_result": "",
            "issues": "", "comment": "", "user_story_id": "",
            "category": "Positive", "priority": "Medium",
            "status": "Unchecked", "testing_type": "Functional",
        }])
        assert rebuilt[0].url_pattern == ""
        assert rebuilt[0].trigger == "manual"


# ── 2. POST /test-cases/<id>/walkthrough-meta ────────────────────


class TestWalkthroughMetaEndpoint:
    def test_form_post_updates_session_and_redirects(self, client):
        _seed_two_tcs(client)
        resp = client.post(
            "/test-cases/TC-001/walkthrough-meta",
            data={
                "url_pattern": "*/login/*",
                "trigger": "walkthrough_url_match",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        # Anchored redirect lands on the edited card.
        assert resp.headers["Location"].endswith("/test-cases#TC-001")
        with client.session_transaction() as s:
            tcs = s.get("test_cases_data") or []
        edited = next(tc for tc in tcs if tc["id"] == "TC-001")
        assert edited["url_pattern"] == "*/login/*"
        assert edited["trigger"] == "walkthrough_url_match"
        # The other TC is untouched — a single-TC edit must never
        # leak through to a sibling.
        other = next(tc for tc in tcs if tc["id"] == "TC-002")
        assert other["url_pattern"] == "*/checkout/*"
        assert other["trigger"] == "walkthrough_url_match"

    def test_json_post_returns_ack(self, client):
        """Fetch-based clients pass Accept: application/json — they
        expect a structured response so they can re-render the
        affected card without a full reload."""
        _seed_two_tcs(client)
        resp = client.post(
            "/test-cases/TC-002/walkthrough-meta",
            data={"url_pattern": "*/cart/checkout",
                  "trigger": "always"},
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body == {
            "id": "TC-002",
            "url_pattern": "*/cart/checkout",
            "trigger": "always",
        }

    def test_unknown_trigger_value_falls_back_to_manual(self, client):
        """Sanitisation guard — a typo / hostile input must NEVER
        sneak through as a valid trigger value. Walkthrough TC
        matcher treats unknown triggers as skip, but storing "manual"
        is more honest about the operator's intent (they tried to
        change it, the form rejected it)."""
        _seed_two_tcs(client)
        client.post(
            "/test-cases/TC-001/walkthrough-meta",
            data={"url_pattern": "", "trigger": "ai_oracle"},
        )
        with client.session_transaction() as s:
            tcs = s.get("test_cases_data") or []
        edited = next(tc for tc in tcs if tc["id"] == "TC-001")
        assert edited["trigger"] == "manual"

    def test_long_url_pattern_is_truncated_to_200(self, client):
        """The DB column caps at 200 chars; the route must not let a
        4 kB payload pass through and trigger a DataError on the next
        ``save_test_cases`` call."""
        _seed_two_tcs(client)
        client.post(
            "/test-cases/TC-001/walkthrough-meta",
            data={"url_pattern": "x" * 5000, "trigger": "always"},
        )
        with client.session_transaction() as s:
            tcs = s.get("test_cases_data") or []
        edited = next(tc for tc in tcs if tc["id"] == "TC-001")
        assert len(edited["url_pattern"]) == 200

    def test_404_when_tc_not_in_pack(self, client):
        _seed_two_tcs(client)
        resp = client.post(
            "/test-cases/TC-NOPE/walkthrough-meta",
            data={"url_pattern": "*", "trigger": "always"},
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["error"] == "not_found"


# ── 3. Template render — collapsed editor visible on each TC ────


class TestTemplateRender:
    def test_walkthrough_meta_form_renders_per_tc(self, client):
        _seed_two_tcs(client)
        resp = client.get("/test-cases")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # Both TC IDs appear as form action targets — one form per
        # TC means the operator can patch them independently.
        assert "/test-cases/TC-001/walkthrough-meta" in body
        assert "/test-cases/TC-002/walkthrough-meta" in body
        # TC-002 pre-populates the URL pattern in the input box.
        assert 'value="*/checkout/*"' in body
        # Trigger select renders each enum value.
        assert 'value="walkthrough_url_match"' in body
        assert 'value="always"' in body
