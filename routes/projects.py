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
from engine import workspace as _workspace
from engine.log import get_logger

from ._shared import (
    GENERATED_KEYS, SERVER_START_TIME, cl_to_dict, get_session_id,
    tc_to_dict,
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


def _require_project_owner(project_id: str) -> dict | None:
    """Authorization gate for project-scoped routes.

    Two eras coexist here on purpose, and which one applies is decided per
    project, not per instance:

    **Organisation era** — ``ORG_MODE`` is on *and* the project has an
    ``org_id``. Access is membership in that organisation. This is the one
    that matters going forward.

    **Legacy era** — everything else: the project predates organisations
    and its ``owner_sid`` is the only claim anyone has to it. Every project
    that exists today is in this state, so removing the branch would lock
    the current users out of their own work. The claim flow (E1.6) is what
    moves a project from one era to the other.

    Behaviour:
      * Returns the project meta dict when the caller may act on it.
      * ``abort(400)`` on a malformed ``project_id``.
      * ``abort(403)`` when the project belongs to someone else.
      * Returns ``None`` when the project does not exist, so the caller can
        choose its own 404 / flash UX (some redirect, some abort).

    Reads ownership before hydrating the full meta, so a failed check
    costs one narrow query rather than a whole row.
    """
    if not _is_valid_project_id(project_id):
        abort(400)

    meta = _db.get_project(project_id)
    if not meta:
        return None

    from engine import permissions as _perm

    project_org = meta.get("org_id")
    if _perm.org_active() and project_org:
        # The project belongs to an organisation: membership decides.
        user_id = _perm.current_user_id()
        role = _db.get_org_role(project_org, user_id) if user_id else None
        if role is None:
            log.warning(
                "project access denied pid=%s org=%s user=%s — not a member",
                project_id[:8], project_org[:8], (user_id or "-")[:8])
            abort(403)
        return meta

    # Legacy: the anonymous session that made it is the only claimant.
    owner = meta.get("owner_sid")
    if owner is None:
        # No owner at all. Pre-dates owner_sid being recorded; there is
        # nobody to compare against, so allow and log so the size of the
        # gap stays visible in ops until E1.6 has claimed them.
        log.info("project pid=%s has NULL owner_sid — allowing (legacy)",
                 project_id[:8])
        return meta
    sid = get_session_id()
    if owner != sid:
        log.warning(
            "project access denied pid=%s owner=%s sid=%s",
            project_id[:8], (owner or "")[:8], (sid or "")[:8])
        abort(403)
    return meta


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
    # Picking, creating or loading a project supersedes an earlier
    # "New session": drop the marker so /test-cases and /checklist are
    # allowed to restore this project's saved pack again.
    session.pop("_pack_cleared_boot", None)


def _safe_next_target(default_endpoint: str = "index") -> str:
    """Honour an optional ``next`` form field so the project-picker
    redirect lands the user back on the module they came from
    (Estimation, Test Cases, …) instead of bouncing to the dashboard.

    Hardened: only allows same-origin paths starting with ``/``. Any
    cross-site value (or one that smells like a protocol) falls back
    to ``default_endpoint`` — protects against open-redirect abuse.
    """
    raw = (request.form.get("next") or "").strip()
    if not raw:
        return url_for(default_endpoint)
    # Reject anything that looks like a scheme or a protocol-relative
    # URL. We only want our own paths.
    if (not raw.startswith("/")) or raw.startswith("//"):
        return url_for(default_endpoint)
    if "://" in raw:
        return url_for(default_endpoint)
    return raw


def register(app: Flask) -> None:
    """Attach project-management routes on the app."""

    @app.route("/new-session", methods=["POST"])
    def new_session():
        """Clear all generated data and the active project pointer."""
        for key in GENERATED_KEYS:
            session.pop(key, None)
        session.pop("project_id", None)
        # Mark the clear as deliberate and stamp it with this boot.
        #
        # /test-cases and /checklist restore a saved pack from Postgres
        # when the session has none, because a free-plan restart wipes the
        # filesystem session store and the work would otherwise look lost.
        # That recovery must not undo an explicit "New session" — without
        # this marker the very next GET resurrected everything the user
        # just asked to clear. The stamp is the discriminator: it lives in
        # the session, so a genuine restart takes it with the rest and
        # recovery resumes on the next boot.
        session["_pack_cleared_boot"] = SERVER_START_TIME
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
        meta = _require_project_owner(project_id)
        if not meta:
            flash("Project not found.", "error")
            return redirect(url_for("index"))

        # Reset only the generated keys — leave preferences (lang, etc.)
        # intact so the user doesn't lose their UI state on a load.
        for key in GENERATED_KEYS:
            session.pop(key, None)

        _set_active_project(meta["id"], meta["name"], meta.get("base_url"))

        # Read through engine.workspace, so every hydrate site agrees on
        # the shape. This one used to assign raw ``list_bugs`` rows into
        # ``bug_reports_data`` — a key that holds session-flat dicts
        # everywhere else — which only stayed invisible because
        # ``_hydrate_bugs`` prefers the database whenever a project is
        # active and never actually read the malformed fallback.
        tcs = _workspace.test_cases(project_id)
        cls = _workspace.checklist(project_id)
        bugs = _workspace.bugs(project_id)
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
        meta = _require_project_owner(project_id)
        if not meta:
            # Idempotent: deleting a missing project is a noop.
            flash("Project deleted.", "success")
            return redirect(url_for("index"))
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
        """Create a fresh project from any module's project-picker form.

        Honours an optional ``next`` form field so the user lands back
        on the module they came from (Estimation, Test Cases, …)
        instead of being kicked to the dashboard. Operator-reported
        2026-05-04: the legacy hard-coded redirect to ``index`` was
        a dead-end UX every time the picker fired off /estimation.
        """
        name = (request.form.get("project_name") or "").strip()
        base_url = (request.form.get("base_url") or "").strip() or None
        description = (request.form.get("description") or "").strip() or None
        next_url = _safe_next_target("index")
        if not name:
            flash("Project name is required.", "error")
            return redirect(next_url)

        try:
            project_id = _db.upsert_project(
                name=name, base_url=base_url, description=description,
                owner_sid=get_session_id(),
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(next_url)

        # Wipe generated keys (TCs/CL/runs/bugs from a previous active
        # project) so switching to a fresh project shows an empty
        # workspace instead of the previous project's artefacts.
        for key in GENERATED_KEYS:
            session.pop(key, None)
        _set_active_project(project_id, name, base_url)
        flash(f"Project '{name}' created and activated.", "success")
        return redirect(next_url)

    @app.route("/projects/db/select/<project_id>", methods=["POST"])
    def db_select_project(project_id):
        """Activate an existing project AND hydrate the session from DB
        so the page the user lands on shows that project's TCs / CL /
        bugs (instead of the previous project's leftover data).

        Honours optional ``next`` form field for redirect target.
        """
        next_url = _safe_next_target("index")
        meta = _require_project_owner(project_id)
        if not meta:
            flash("Project not found.", "error")
            return redirect(next_url)

        # Wipe the previous project's generated keys before loading the
        # selected one — otherwise switching from a 60-TC project to a
        # 5-TC project would leave the 60 TCs in session.
        for key in GENERATED_KEYS:
            session.pop(key, None)

        _set_active_project(meta["id"], meta["name"], meta.get("base_url"))

        # Hydrate from DB. Each load is best-effort — a brand-new
        # project will return empty lists, which is fine. The set of
        # things we hydrate has to mirror exactly what each module's
        # GET handler reads from the session — operator-reported
        # 2026-05-05: the previous switch loaded TCs / CL / bugs but
        # silently lost estimations and run history, leaving the user
        # confused why their previous project's estimation table was
        # empty after switching back.
        try:
            # The GENERATED_KEYS wipe above emptied the session, so these
            # reads fall through to the database — but drop the
            # per-request cache too, in case an earlier read in this same
            # request populated it from the project we are leaving.
            _workspace.invalidate()
            tcs = _workspace.test_cases(project_id)
            cls = _workspace.checklist(project_id)
            bugs = _workspace.bugs(project_id)
            if tcs:
                session["test_cases_data"] = tcs
            if cls:
                session["checklist_data"] = cls
            if bugs:
                session["bug_reports_data"] = bugs

            # Latest estimation snapshot — Estimation page reads
            # session["estimation_result"] to render the result card.
            # Without this hydrate, switching to a project that had a
            # past estimation showed an empty Estimation page.
            if hasattr(_db, "list_estimations"):
                ests = _db.list_estimations(project_id, limit=1) or []
                if ests:
                    latest = ests[0] or {}
                    rp = (latest.get("result_payload")
                          or latest.get("result") or {})
                    if isinstance(rp, dict) and rp:
                        session["estimation_result"] = rp
                    ip = (latest.get("input_payload")
                          or latest.get("input") or {})
                    if isinstance(ip, dict) and ip:
                        # Re-populate the form snapshot so the next
                        # /estimation render shows the same coefficients
                        # the user used last time on this project.
                        prev_form = session.get("estimation_form") or {}
                        for k, v in ip.items():
                            prev_form.setdefault(k, v)
                        session["estimation_form"] = prev_form

            # Test-execution run history — Test Execution page renders
            # session["test_runs"] as a list of past runs. We keep the
            # last 20 (matching the in-page cap) so switching back
            # shows the actual run table the user remembers.
            # Shaped by engine.workspace, not inline here. This block used
            # to carry its own copy of the mapping, and that is exactly how
            # run history went missing when switching projects: the copy
            # did not know about a field the template read. One shaper, one
            # place to add the next field.
            runs = _workspace.runs(project_id, limit=20)
            if runs:
                session["test_runs"] = runs
        except Exception as exc:
            log.warning("project select: hydrate failed: %s", exc)

        flash(
            f"Active project: {meta['name']} "
            f"({len(session.get('test_cases_data') or [])} TC · "
            f"{len(session.get('checklist_data') or [])} CL · "
            f"{len(session.get('bug_reports_data') or [])} bugs · "
            f"{len(session.get('test_runs') or [])} runs).",
            "success",
        )
        return redirect(next_url)


    @app.route("/projects/db/rename/<project_id>", methods=["POST"])
    def db_rename_project(project_id):
        """Rename / re-tag a project. Useful for the auto-created
        'Untitled project YYYY-MM-DD HH:MM' rows."""
        meta = _require_project_owner(project_id)
        if not meta:
            flash("Project not found.", "error")
            return redirect(url_for("index"))
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

        # Gate the source — the caller must own (or legacy-NULL) it
        # before we let them shuffle artefacts out.
        source_meta = _require_project_owner(source_pid)
        if not source_meta:
            flash("Source project not found.", "error")
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
        else:
            # Existing target — must also be owned by this session, or
            # an attacker could shovel their own artefacts into a
            # victim's project.
            target_meta = _require_project_owner(target_pid)
            if not target_meta:
                flash("Target project not found.", "error")
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
