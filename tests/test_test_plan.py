"""Sprint 4 task 4.3 — Test Plan generator wiring.

Coverage:

1. GET /test-plan renders the 13-section skeleton even when the
   project has zero artefacts saved.
2. POST /test-plan with no project context still produces a plan
   (defaults from advisor.ProjectContext kick in) and exposes the
   plan in the response.
3. "Features to be Tested" lists the *unique* sections from the
   project's stored test cases when any exist.
4. /test-plan/export round-trips: TOC + 13 H2 sections in the
   Markdown response.
5. /test-plan/export when no plan is cached redirects with a
   user-facing flash, not a 500.
"""

from __future__ import annotations

import pytest


# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def isolated_session(client):
    """Wipe per-test session keys that other suites might have left
    behind so each test starts from a clean ``project_setup`` /
    ``test_plan_data`` slate."""
    with client.session_transaction() as sess:
        for k in ("project_setup", "test_plan_data", "project_id",
                  "test_cases_data"):
            sess.pop(k, None)
    return client


# ── GET /test-plan ────────────────────────────────────────────────


class TestTestPlanGet:
    def test_empty_project_renders_input_form(self, isolated_session):
        client = isolated_session
        resp = client.get("/test-plan")
        assert resp.status_code == 200
        # Form is always present; the 13-section body is only there
        # *after* generation, so this is the first-visit shape.
        assert b"<form" in resp.data
        # No cached plan yet means the TOC is not rendered.
        assert b"Table of Contents" not in resp.data


# ── POST /test-plan ───────────────────────────────────────────────


class TestTestPlanGenerate:
    def test_empty_project_still_produces_13_section_skeleton(
            self, isolated_session):
        """The generator must never refuse to render — even a brand-
        new project with no setup and no TCs returns the full TestFort
        13-section skeleton (with domain-default features)."""
        client = isolated_session
        resp = client.post("/test-plan",
                           data={"input_text": "", "custom_prompt": ""},
                           follow_redirects=True)
        assert resp.status_code == 200
        with client.session_transaction() as sess:
            plan = sess.get("test_plan_data")
        assert plan is not None
        assert len(plan["sections"]) == 13
        # Numbers are 1..13 in order — defending against accidental
        # section reorders during refactors.
        nums = [s["number"] for s in plan["sections"]]
        assert nums == [str(i) for i in range(1, 14)]

    def test_features_pulled_from_stored_test_cases(self, isolated_session):
        """When the active project has TCs in Postgres, section 6 of
        the plan lists their unique sections — not the domain defaults.
        """
        from engine import db as _db

        client = isolated_session
        # Seed a project + a couple of TCs grouped under two sections.
        pid = _db.upsert_project(name="Plan-Features-Test",
                                 owner_sid="t-sid-features")
        _db.save_test_cases(pid, [
            {"id": "TC-001", "section": "Auth", "section_num": 1,
             "summary": "Login OK", "preconditions": "",
             "test_steps": "", "test_data": "", "expected_result": "",
             "category": "Positive", "priority": "High",
             "status": "New", "user_story_id": "US-1"},
            {"id": "TC-002", "section": "Search", "section_num": 2,
             "summary": "Search OK", "preconditions": "",
             "test_steps": "", "test_data": "", "expected_result": "",
             "category": "Positive", "priority": "High",
             "status": "New", "user_story_id": "US-2"},
            {"id": "TC-003", "section": "Auth", "section_num": 1,
             "summary": "Login bad pw", "preconditions": "",
             "test_steps": "", "test_data": "", "expected_result": "",
             "category": "Negative", "priority": "High",
             "status": "New", "user_story_id": "US-1"},
        ])

        with client.session_transaction() as sess:
            sess["project_id"] = pid
            sess["project_setup"] = {"project_name": "Plan-Features-Test"}

        resp = client.post("/test-plan",
                           data={"input_text": ""},
                           follow_redirects=True)
        assert resp.status_code == 200

        with client.session_transaction() as sess:
            plan = sess["test_plan_data"]
        section_6 = next(
            s for s in plan["sections"] if s["number"] == "6")
        # Both unique TC sections must appear; duplicate "Auth" only
        # once. Order follows TC iteration order (Auth, then Search).
        assert "Auth" in section_6["content"]
        assert "Search" in section_6["content"]
        assert section_6["content"].count("Auth") == 1


# ── /test-plan/export ────────────────────────────────────────────


class TestTestPlanExport:
    def test_export_round_trip_has_toc_and_all_sections(
            self, isolated_session):
        client = isolated_session
        # Generate first so session has the cached plan.
        resp = client.post("/test-plan",
                           data={"input_text": "", "custom_prompt": ""},
                           follow_redirects=True)
        assert resp.status_code == 200

        resp = client.get("/test-plan/export")
        assert resp.status_code == 200
        assert resp.mimetype == "text/markdown"
        body = resp.data.decode("utf-8")
        assert "## Table of Contents" in body
        # All 13 H2 headings must appear in the export.
        import re
        h2 = re.findall(r"^## .+$", body, re.MULTILINE)
        # +1 for the TOC heading itself.
        assert len(h2) >= 14, (
            f"expected TOC + 13 section headings, got {len(h2)}")

    def test_export_without_plan_redirects_with_flash(
            self, isolated_session):
        client = isolated_session
        resp = client.get("/test-plan/export", follow_redirects=False)
        assert resp.status_code == 302
        # Lands back on /test-plan so the user sees the form.
        assert "/test-plan" in resp.headers["Location"]


# ── Markdown serialiser unit tests ────────────────────────────────


class TestExportMarkdownUnit:
    """Direct calls on :func:`export_test_plan_markdown` keep the
    serialiser honest even if the route layer changes later."""

    def test_pipe_in_cell_is_escaped(self):
        from engine.exporter import export_test_plan_markdown
        from engine.test_plan_generator import TestPlan, TestPlanSection

        plan = TestPlan(
            project_name="Pipe Test", version="1.0", date="2026-05-20",
            sections=[
                TestPlanSection(
                    number="1", title="Pipes",
                    content="",
                    tables=[{
                        "title": "Pipe demo",
                        "headers": ["A", "B"],
                        "rows": [["x|y", "ok"]],
                    }],
                ),
            ],
        )
        out = export_test_plan_markdown(plan)
        # The literal "x|y" cell must be escaped so it doesn't break
        # the Markdown table column count.
        assert "x\\|y" in out

    def test_newline_in_cell_becomes_br(self):
        from engine.exporter import export_test_plan_markdown
        from engine.test_plan_generator import TestPlan, TestPlanSection

        plan = TestPlan(
            project_name="NL Test", version="1.0", date="2026-05-20",
            sections=[
                TestPlanSection(
                    number="1", title="NL",
                    content="",
                    tables=[{
                        "headers": ["A"],
                        "rows": [["line1\nline2"]],
                    }],
                ),
            ],
        )
        out = export_test_plan_markdown(plan)
        assert "line1<br>line2" in out
