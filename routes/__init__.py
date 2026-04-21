"""TestFortge — Routes package.

Exposes :func:`register_all` which wires every route module onto the
given Flask app. Each sub-module uses the **register-function pattern**
(rather than blueprints) so endpoint names stay flat — ``url_for("index")``
keeps working without any template changes.
"""

from __future__ import annotations

from flask import Flask

from . import dashboard, projects, generation, execution, automation, estimation, chat


def register_all(app: Flask) -> None:
    """Attach every routes/*.py module to the app."""
    dashboard.register(app)
    projects.register(app)
    generation.register(app)
    execution.register(app)
    automation.register(app)
    estimation.register(app)
    chat.register(app)


__all__ = ["register_all"]
