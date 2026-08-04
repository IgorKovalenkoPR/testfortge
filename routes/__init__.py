"""TestFortge — Routes package.

Exposes :func:`register_all` which wires every route module onto the
given Flask app. Each sub-module uses the **register-function pattern**
(rather than blueprints) so endpoint names stay flat — ``url_for("index")``
keeps working without any template changes.
"""

from __future__ import annotations

from flask import Flask

from . import (auth, members, dashboard, projects, generation, execution,
               execution_live, execution_manual, bugs, automation,
               estimation, chat, ops, guide, debug)


def register_all(app: Flask) -> None:
    """Attach every routes/*.py module to the app."""
    # First, so ``url_for("auth_login")`` resolves for the redirect that
    # engine.permissions issues when an unauthenticated caller reaches a
    # protected route.
    auth.register(app)
    # After auth: members.py imports new_invite_token from it, and its
    # routes are decorated with the same permission layer.
    members.register(app)
    dashboard.register(app)
    projects.register(app)
    generation.register(app)
    execution.register(app)
    # Stage 7 refactor: ``/test-execution/live*`` + bug-flow routes live in
    # their own modules now. ``execution_live`` must register AFTER
    # ``execution`` to keep the URL-rule order stable for tests that
    # match the first-defined rule. ``bugs`` order is irrelevant
    # (distinct paths) but kept together with execution for cohesion.
    execution_live.register(app)
    # The step-by-step manual walk. Its own module for the same reason
    # execution_live is: routes/execution.py is 2,800 lines and a new
    # surface belongs beside it, not inside it.
    execution_manual.register(app)
    bugs.register(app)
    automation.register(app)
    estimation.register(app)
    chat.register(app)
    ops.register(app)
    guide.register(app)
    debug.register(app)

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

    # Access policy for every route (E2.3). Installed last so its
    # before_request hook runs after the HTTP Basic gate — that gate is the
    # outer perimeter during the rollout and should reject first — and
    # after every rule exists, since it validates the table against the
    # URL map at import time.
    from engine import route_policy as _route_policy
    _route_policy.install(app)

    # Identity + role, for every template. Lets the UI hide what the
    # server would refuse — which is politeness, not security; the
    # boundary is engine.permissions' decorators. Same defensive shape as
    # above: a failure here must not 500 the page, and the fallback is the
    # least-privileged reading of every flag.
    @app.context_processor
    def _inject_identity_context():
        try:
            from engine import permissions as _perm
            return _perm.template_context()
        except Exception:
            return {"auth_active": False, "org_active": False,
                    "current_user": None, "current_role": None,
                    "is_admin": False}


__all__ = ["register_all"]
