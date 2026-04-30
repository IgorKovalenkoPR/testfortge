"""TestFortge — Project-management routes (DB-backed).

  * POST /new-session                   — clear current session's generated data
  * POST /save-project                  — upsert active project in the DB
                                          (also persists TC / CL snapshots)
  * GET  /load-project/<project_id>     — hydrate session from the DB
  * POST /delete-project/<project_id>   — drop a project (cascades children)
  * POST /projects/db/create            — explicit "Create new project" form
  * POST /projects/db/select/<project_id> — switch the active project
                                            without loading its data into
                                            the current session

The legacy ``storage/<timestamp>/`` JSON-snapshot model has been
removed; everything now lives in the relational schema declared in
:mod:`engine.db`.
"""

from __future__ import annotations

from flask import Flask, abort, flash, redirect, request, session, url_for

from engine import db as _db
from engine.log import get_logger

from ._shared import (
    GENERATED_KEYS, cl_to_dict, get_session_id, tc_to_dict,
)

log = get_logger(__name__)


# A project_id is a 32-char uuid hex string. The validator keeps the
# url-routing decoupled from the DB layer — bad input is short-circuited
# before any query runs.
_PROJECT_ID_LEN = 32


def _is_valid_project_id(s: str | None) -> bool:
    if not s or len(s) != _PROJECT_ID_LEN:
        return False
    return all(c in "0123456789abcdef" for c in s)


def _set_active_project(project_id: str, name: str,
                        base_url: str | None = None) -> None:
    """Mirror the active project into the session so templates and
    sidebar widgets stay in sync."""
    setup = session.get("project_setup") or {}
    setup["project_name"] = name
    if base_url:
        setup["base_url"] = base_url
    session["project_setup"] = setup
    session["project_id"] = project_id


def register(app: Flask) -> None:
    """Attach project-management routes on the app."""

    @app.route("/new-session", methods=["POST"])
    def new_session():
        """Clear all generated data and the active project pointer."""
        for key in GENERATED_KEYS:
            session.pop(key, None)
        session.pop("project_id", None)
        return redirect(url_for("index"))

    @app.route("/save-project", methods=["POST"])
    def save_project():
        """Upsert the active project and snapshot in-session TC / CL.

        The form only requires ``project_name`` — everything else is
        sourced from the current session so we keep the existing
        "Save current work" UX with a single click.
        """
        project_name = (request.form.get("project_name") or "").strip()
        if not project_name:
            project_name = (session.get("project_setup") or {}) \
                .get("project_name", "Project")

        base_url = (session.get("project_setup") or {}).get("base_url") \
            or (session.get("project_setup") or {}).get("project_url")

        try:
            project_id = _db.upsert_project(
                name=project_name,
                base_url=base_url,
                description=None,
                owner_sid=get_session_id(),
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))

        _set_active_project(project_id, project_name, base_url)

        # Persist current TC/CL snapshots so /load-project later
        # rehydrates exactly what the user had on screen.
        tc_data = session.get("test_cases_data") or []
        cl_data = session.get("checklist_data") or []
        try:
            if tc_data:
                _db.save_test_cases(project_id, tc_data)
            if cl_data:
                _db.save_checklist(project_id, cl_data)
        except Exception:  # pragma: no cover — flash-and-survive
            log.exception("save-project: failed to persist TC/CL snapshot")

        flash(f"Project saved: {project_name}", "success")
        return redirect(url_for("index"))

    @app.route("/load-project/<project_id>")
    def load_project(project_id):
        """Hydrate the current session from a stored project."""
        if not _is_valid_project_id(project_id):
            abort(400)
        meta = _db.get_project(project_id)
        if not meta:
            flash("Project not found.", "error")
            return redirect(url_for("index"))

        # Reset only the generated keys — leave preferences (lang, etc.)
        # intact so the user doesn't lose their UI state on a load.
        for key in GENERATED_KEYS:
            session.pop(key, None)

        _set_active_project(meta["id"], meta["name"], meta.get("base_url"))

        tcs = _db.load_test_cases(project_id)
        cls = _db.load_checklist(project_id)
        bugs = _db.list_bugs(project_id=project_id)
        if tcs:
            session["test_cases_data"] = tcs
            session["_show_tc_once"] = True
        if cls:
            session["checklist_data"] = cls
            session["_show_cl_once"] = True
        if bugs:
            session["bug_reports_data"] = bugs

        flash(f"Project '{meta['name']}' loaded.", "success")
        return redirect(url_for("index"))

    @app.route("/delete-project/<project_id>", methods=["POST"])
    def delete_project(project_id):
        if not _is_valid_project_id(project_id):
            abort(400)
        _db.delete_project(project_id)
        # If the active project was just deleted, clear the pointer too.
        if session.get("project_id") == project_id:
            session.pop("project_id", None)
            (session.get("project_setup") or {}).pop("project_name", None)
        flash("Project deleted.", "success")
        return redirect(url_for("index"))

    # ── Explicit project-picker actions ────────────────────────────

    @app.route("/projects/db/create", methods=["POST"])
    def db_create_project():
        """Create a fresh project from the dashboard picker form."""
        name = (request.form.get("project_name") or "").strip()
        base_url = (request.form.get("base_url") or "").strip() or None
        description = (request.form.get("description") or "").strip() or None
        if not name:
            flash("Project name is required.", "error")
            return redirect(url_for("index"))

        try:
            project_id = _db.upsert_project(
                name=name, base_url=base_url, description=description,
                owner_sid=get_session_id(),
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))

        # Newly-created project becomes the active one — but we don't
        # touch GENERATED_KEYS so any in-flight work survives the click.
        _set_active_project(project_id, name, base_url)
        flash(f"Project '{name}' created and activated.", "success")
        return redirect(url_for("index"))

    @app.route("/projects/db/select/<project_id>", methods=["POST"])
    def db_select_project(project_id):
        """Activate an existing project without overwriting session data."""
        if not _is_valid_project_id(project_id):
            abort(400)
        meta = _db.get_project(project_id)
        if not meta:
            flash("Project not found.", "error")
            return redirect(url_for("index"))
        _set_active_project(meta["id"], meta["name"], meta.get("base_url"))
        flash(f"Active project: {meta['name']}", "success")
        return redirect(url_for("index"))


    @app.route("/projects/db/rename/<project_id>", methods=["POST"])
    def db_rename_project(project_id):
        """Rename / re-tag a project. Useful for the auto-created
        'Untitled project YYYY-MM-DD HH:MM' rows."""
        if not _is_valid_project_id(project_id):
            abort(400)
        new_name = (request.form.get("project_name") or "").strip()
        if not new_name:
            flash("Project name cannot be empty.", "error")
            return redirect(url_for("index"))
        new_url = request.form.get("base_url")
        new_desc = request.form.get("description")
        try:
            touched = _db.update_project(
                project_id,
                name=new_name,
                base_url=new_url if new_url is not None else None,
                description=new_desc if new_desc is not None else None,
            )
        except Exception as exc:  # pragma: no cover
            log.warning("rename project failed: %s", exc)
            flash("Could not rename project — see server logs.", "error")
            return redirect(url_for("index"))
        if not touched:
            flash("Nothing changed.", "info")
            return redirect(url_for("index"))
        # Mirror into session if this happens to be the active project.
        if session.get("project_id") == project_id:
            setup = session.get("project_setup") or {}
            setup["project_name"] = new_name
            if new_url is not None:
                setup["base_url"] = new_url.strip() or None
            session["project_setup"] = setup
        flash(f"Project renamed to '{new_name}'.", "success")
        return redirect(url_for("index"))

    @app.route("/projects/db/move-artifacts", methods=["POST"])
    def db_move_artifacts():
        """Move every artefact from a source project to a target project.

        Source defaults to the currently-active project; target is either
        an existing project_id (``target_project_id``) or a new project
        whose name is provided in ``target_project_name`` (we create it
        on the fly).
        """
        source_pid = (request.form.get("source_project_id")
                      or session.get("project_id") or "").strip()
        if not source_pid or not _is_valid_project_id(source_pid):
            flash("No valid source project selected.", "error")
            return redirect(url_for("index"))

        target_pid = (request.form.get("target_project_id") or "").strip()
        new_name = (request.form.get("target_project_name") or "").strip()

        if target_pid and not _is_valid_project_id(target_pid):
            flash("Invalid target project.", "error")
            return redirect(url_for("index"))

        if not target_pid:
            if not new_name:
                flash("Pick an existing project or name a new one.", "error")
                return redirect(url_for("index"))
            try:
                target_pid = _db.upsert_project(
                    name=new_name, owner_sid=get_session_id(),
                )
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("index"))

        if target_pid == source_pid:
            flash("Source and target are the same project — nothing to do.",
                  "info")
            return redirect(url_for("index"))

        try:
            moved = _db.move_artifacts(source_pid, target_pid)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        except Exception as exc:  # pragma: no cover
            log.warning("move_artifacts failed: %s", exc)
            flash("Move failed — see server logs.", "error")
            return redirect(url_for("index"))

        total = sum(moved.values())
        if total == 0:
            flash("Source project had no artefacts to move.", "info")
        else:
            # Build a per-kind summary, omitting zeros.
            parts = [f"{n} {k.replace('_', ' ')}"
                     for k, n in moved.items() if n]
            target_meta = _db.get_project(target_pid)
            target_label = (target_meta or {}).get("name", "target")
            flash(f"Moved to '{target_label}': " + ", ".join(parts) + ".",
                  "success")
            # If we moved off the active project, refresh in-session lists
            # so the dashboard shows zero counts for the newly-emptied one.
            if session.get("project_id") == source_pid:
                for k in ("test_cases_data", "checklist_data",
                          "bug_reports_data", "test_runs",
                          "estimation_result"):
                    session.pop(k, None)
        return redirect(url_for("index"))


__all__ = ["register"]
