"""TestFortge — Routes package.

Exposes :func:`register_all` which wires every route module onto the
given Flask app. Each sub-module uses the **register-function pattern**
(rather than blueprints) so endpoint names stay flat — ``url_for("index")``
keeps working without any template changes.
"""

from __future__ import annotations

from flask import Flask

from . import (dashboard, projects, generation, execution,
               automation, estimation, chat, ops, guide, test_plan)


def register_all(app: Flask) -> None:
    """Attach every routes/*.py module to the app."""
    dashboard.register(app)
    projects.register(app)
    generation.register(app)
    execution.register(app)
    automation.register(app)
    estimation.register(app)
    chat.register(app)
    ops.register(app)
    guide.register(app)
    test_plan.register(app)

    # Global context processor — exposes ``projects`` (the user's
    # owned project list) and ``active_project_id`` to EVERY template
    # render automatically. This is what powers
    # ``{% include '_project_picker.html' %}`` in any module's
    # template without each route having to pass them by hand. Cheap:
    # the underlying DB call is cached at the SQLAlchemy session level
    # for the duration of one HTTP request.
    @app.context_processor
    def _inject_picker_context():
        try:
            from ._shared import get_picker_context
            return get_picker_context()
        except Exception:
            # Defensive: a context-processor exception 500s the page,
            # which is much worse than just hiding the picker.
            return {"projects": [], "active_project_id": ""}


__all__ = ["register_all"]
