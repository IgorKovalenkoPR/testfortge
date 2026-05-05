"""TestFortge — User Guide route.

Renders ``/guide`` with one section per product module: Dashboard,
Estimation, Test Cases, Checklist, Test Execution, Bug Reports,
Tedgie chat. The content lives in the template (not in code) so a PM
or tech writer can edit it without touching Python.

Architecture
------------
The guide is a static template — no DB calls, no session writes. The
page is anchored (``#dashboard``, ``#estimation``, ...) so deep links
from elsewhere in the app (e.g. an empty-state "How does this work?"
button) can land on the right section.
"""

from __future__ import annotations

from flask import Flask, render_template, session

from ._shared import get_session_id  # noqa: F401 — kept for symmetry


def register(app: Flask) -> None:
    @app.route("/guide", methods=["GET"])
    def guide_page():
        return render_template(
            "guide.html",
            lang=session.get("lang", "en"),
        )


__all__ = ["register"]
