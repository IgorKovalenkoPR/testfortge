"""TestFortge — Saved-project management routes.

  * POST /new-session            — clear current session's generated data
  * POST /save-project           — snapshot session to STORAGE_FOLDER/<ts>
  * GET  /load-project/<folder>  — hydrate session from a saved snapshot
  * POST /delete-project/<folder> — remove a saved snapshot
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime

from flask import Flask, abort, flash, redirect, request, session, url_for

from engine.log import get_logger

from ._shared import (
    GENERATED_KEYS, SAFE_FOLDER_RE,
    get_project_dir, invalidate_projects_cache,
)

log = get_logger(__name__)


def register(app: Flask) -> None:
    """Attach project-storage routes on the app."""

    @app.route("/new-session", methods=["POST"])
    def new_session():
        """Clear all generated data, start fresh."""
        for key in GENERATED_KEYS:
            session.pop(key, None)
        return redirect(url_for("index"))

    @app.route("/save-project", methods=["POST"])
    def save_project():
        project_name = request.form.get("project_name", "").strip()
        if not project_name:
            project_name = session.get("project_setup", {}).get("project_name", "Project")
        setup = session.get("project_setup", {})
        setup["project_name"] = project_name
        session["project_setup"] = setup

        project_dir = get_project_dir(project_name)
        os.makedirs(project_dir, exist_ok=True)

        meta = {
            "project_name": project_name,
            "saved_at": datetime.now().isoformat(),
            "requirements_count": len(session.get("raw_requirements", [])),
            "test_cases_count": len(session.get("test_cases_data", [])),
            "checklist_count": len(session.get("checklist_data", [])),
        }
        with open(os.path.join(project_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        with open(os.path.join(project_dir, "session_data.json"), "w", encoding="utf-8") as f:
            json.dump({
                "project_setup": session.get("project_setup", {}),
                "raw_requirements": session.get("raw_requirements", []),
                "user_stories": session.get("user_stories", []),
                "test_cases_data": session.get("test_cases_data", []),
                "checklist_data": session.get("checklist_data", []),
                "custom_prompt": session.get("custom_prompt", ""),
                "execution_results": session.get("execution_results", {}),
                "bug_reports_data": session.get("bug_reports_data", []),
                "test_runs": session.get("test_runs", []),
            }, f, ensure_ascii=False, indent=2)

        invalidate_projects_cache()
        flash(f"Project saved: {project_name}", "success")
        return redirect(url_for("index"))

    @app.route("/load-project/<folder>")
    def load_project(folder):
        if not SAFE_FOLDER_RE.fullmatch(folder):
            abort(400)
        data_path = os.path.join(app.config["STORAGE_FOLDER"], folder, "session_data.json")
        if not os.path.isfile(data_path):
            flash("Project not found.", "error")
            return redirect(url_for("index"))
        with open(data_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key in ("project_setup", "raw_requirements", "user_stories",
                    "test_cases_data", "checklist_data", "custom_prompt",
                    "execution_results", "bug_reports_data", "test_runs"):
            if key in data:
                session[key] = data[key]
        if session.get("test_cases_data"):
            session["_show_tc_once"] = True
        if session.get("checklist_data"):
            session["_show_cl_once"] = True
        flash(f"Project '{data.get('project_setup', {}).get('project_name', '')}' loaded.", "success")
        return redirect(url_for("index"))

    @app.route("/delete-project/<folder>", methods=["POST"])
    def delete_project(folder):
        if not SAFE_FOLDER_RE.fullmatch(folder):
            abort(400)
        project_dir = os.path.join(app.config["STORAGE_FOLDER"], folder)
        # Resolve and confirm the canonicalised path still lives under
        # STORAGE_FOLDER before any destructive operation.
        storage_root = os.path.realpath(app.config["STORAGE_FOLDER"])
        real_target = os.path.realpath(project_dir)
        if not real_target.startswith(storage_root + os.sep):
            abort(400)
        if os.path.isdir(real_target):
            shutil.rmtree(real_target)
            invalidate_projects_cache()
            flash("Project deleted.", "success")
        return redirect(url_for("index"))


__all__ = ["register"]
