"""TestForTge — Test Plan generator routes (Sprint 4 task 4.3).

Wires the long-existing :mod:`engine.test_plan_generator` (TestFort
13-section template) onto three endpoints:

  * GET  /test-plan         — render whatever's cached for the session
  * POST /test-plan         — generate from current project_setup + TCs
  * GET  /test-plan/export  — serve the cached plan as Markdown

Inputs to the generator are minimal and procedural — no LLM call —
so the route is intentionally lightweight (no JobQueue). The custom
prompt is already capped to 1000 chars by :func:`parse_page_input`
(Sprint 4 task 4.4), so we can interpolate it without further wrapping.
"""

from __future__ import annotations

import dataclasses

from flask import (Flask, Response, flash, g, redirect, render_template,
                   request, session, url_for)

from engine import db as _db
from engine.advisor import ProjectContext
from engine.exporter import export_test_plan_markdown
from engine.log import get_logger
from engine.test_plan_generator import (TestPlan, TestPlanSection,
                                        generate_test_plan)

from ._shared import ensure_active_project, parse_page_input

_log = get_logger(__name__)


def _plan_to_dict(plan: TestPlan) -> dict:
    """Convert a :class:`TestPlan` dataclass into the session-safe
    nested-dict shape. ``dataclasses.asdict`` walks the sections
    recursively, which is what we want."""
    return dataclasses.asdict(plan)


def _dict_to_plan(data: dict | None) -> TestPlan | None:
    """Inverse of :func:`_plan_to_dict`. Defensive about missing keys
    so a session pickled by an earlier app version still renders."""
    if not isinstance(data, dict) or not data.get("sections"):
        return None
    sections = [
        TestPlanSection(
            number=s.get("number", ""),
            title=s.get("title", ""),
            content=s.get("content", ""),
            subsections=list(s.get("subsections") or []),
            tables=list(s.get("tables") or []),
        )
        for s in data.get("sections", [])
    ]
    return TestPlan(
        project_name=data.get("project_name", "Project"),
        version=data.get("version", "1.0"),
        date=data.get("date", ""),
        sections=sections,
    )


def _project_features(project_id: str) -> list[str]:
    """Build the "Features to be Tested" list from the project's
    existing test cases — one entry per unique section, in the order
    the cases appear. Empty list when no TCs are saved yet, in which
    case :func:`generate_test_plan` falls back to domain defaults."""
    if not project_id:
        return []
    try:
        rows = _db.load_test_cases(project_id) or []
    except Exception as exc:  # pragma: no cover — DB outage shouldn't 500
        _log.warning("test plan: load_test_cases failed: %s", exc)
        return []
    seen: set[str] = set()
    features: list[str] = []
    for r in rows:
        section = (r.get("section") or "").strip()
        if section and section not in seen:
            seen.add(section)
            features.append(section)
    return features


def _build_context(setup: dict, name_fallback: str) -> ProjectContext:
    """Map session ``project_setup`` to a :class:`ProjectContext`.

    ``ProjectContext.__init__`` already supplies safe defaults for every
    field, so missing keys just fall through to "other / web / biweekly".
    """
    return ProjectContext(
        project_name=setup.get("project_name") or name_fallback or "Project",
        domain=setup.get("domain") or "other",
        platform=setup.get("platform") or "web",
        tech_stack=setup.get("tech_stack") or [],
        team_size=setup.get("team_size") or "small",
        has_api=bool(setup.get("has_api", True)),
        has_payments=bool(setup.get("has_payments", False)),
        has_auth=bool(setup.get("has_auth", True)),
        release_frequency=setup.get("release_frequency") or "biweekly",
    )


def register(app: Flask) -> None:

    @app.route("/test-plan", methods=["GET", "POST"])
    def test_plan_page():
        pid = ensure_active_project()
        setup = session.get("project_setup") or {}

        if request.method == "POST":
            # ``custom_prompt`` is the only field the generator actually
            # consumes from the form; raw_lines / errors are ignored
            # here, but we still call parse_page_input so the 1000-char
            # cap (Sprint 4 task 4.4) is applied identically to the
            # other generation endpoints.
            _raw, _errors, custom_prompt = parse_page_input()

            ctx = _build_context(setup, name_fallback=pid)
            features = _project_features(pid)
            plan = generate_test_plan(ctx, features=features,
                                      custom_prompt=custom_prompt)
            session["test_plan_data"] = _plan_to_dict(plan)
            session.modified = True
            flash(
                g.t.get("test_plan_generated",
                        "Test plan generated."),
                "success",
            )
            return redirect(url_for("test_plan_page"))

        plan = _dict_to_plan(session.get("test_plan_data"))
        return render_template(
            "test_plan.html",
            plan=plan,
            has_data=plan is not None,
        )

    @app.route("/test-plan/export", methods=["GET"])
    def test_plan_export():
        plan = _dict_to_plan(session.get("test_plan_data"))
        if plan is None:
            flash(
                g.t.get("test_plan_export_empty",
                        "Generate a test plan before exporting."),
                "error",
            )
            return redirect(url_for("test_plan_page"))
        content = export_test_plan_markdown(plan)
        name = (plan.project_name or "project").replace(" ", "_")
        return Response(
            content,
            mimetype="text/markdown",
            headers={
                "Content-Disposition":
                    f"attachment; filename=testfortge_{name}_test_plan.md",
            },
        )


__all__ = ["register"]
