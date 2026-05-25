"""TestFortge — Bug report routes.

  * POST /create-bug-report   — create a single bug entry
  * GET  /bug-reports         — list bugs (DB-first, session fallback)
  * POST /bugs/bulk           — bulk-update / delete bugs
  * GET  /export-bug-reports  — markdown export

Extracted from ``routes/execution.py`` in the Stage 7 refactor so the
bug-flow lives next to its helpers (``_persist_bug``,
``_hydrate_bugs``, ``_bug_row_to_session_dict``, ``_bug_dict_to_db_row``)
without bloating the execution-flow module. ``_persist_bug`` is
re-exported because ``test_execution_results`` in ``execution.py``
still mirrors each TC-driven / walkthrough / LiveExecutor bug into the
DB through it.
"""

from __future__ import annotations

from datetime import datetime

from flask import (Flask, Response, flash, g, redirect, render_template,
                   request, session, url_for)

from engine.log import get_logger
from engine.bug_report import (
    BugReport, BUG_SEVERITIES, BUG_PRIORITIES, BUG_STATUSES, BUG_FREQUENCIES,
    generate_bug_id, bug_to_dict, dict_to_bug,
    export_bug_report_markdown,
)

from engine import db as _db

from ._shared import ensure_active_project, get_session_id
from .projects import _require_project_owner

log = get_logger(__name__)


def _bug_row_to_session_dict(row: dict) -> dict:
    """Convert a row returned by :func:`engine.db.list_bugs` into the
    session-flat shape :func:`dict_to_bug` consumes.

    Three notable mappings:
      * ``row.id`` (int DB row id) → ``db_id`` on the session BugReport
      * ``row.external_id`` (e.g. ``"BUG-001"``) → display ``id``
      * ``row.extra.assignee`` → first-class ``assignee`` field so the
        existing template renders it without an attribute lookup hack

    Anything in ``row.extra`` that ``BugReport`` already knows about
    (``frequency``, ``component``, etc.) is unpacked too so the JSON
    blob doesn't shadow first-class fields after a re-render.
    """
    extra = row.get("extra") or {}
    out = {
        "id":                 row.get("external_id") or f"BUG-{int(row.get('id') or 0):03d}",
        "db_id":              int(row.get("id") or 0),
        "title":              row.get("title") or "",
        "severity":           row.get("severity") or "Minor",
        "priority":           row.get("priority") or "Medium",
        "status":             row.get("status") or "Open",
        "environment":        row.get("environment") or "",
        "preconditions":      extra.get("preconditions", ""),
        "steps_to_reproduce": row.get("steps_to_reproduce") or "",
        "actual_result":      row.get("actual_result") or "",
        "expected_result":    row.get("expected_result") or "",
        "frequency":          extra.get("frequency", "Always"),
        "affects_version":    row.get("version") or "",
        "found_in_build":     extra.get("found_in_build", ""),
        "attachments":        extra.get("attachments") or [],
        "linked_item_id":     extra.get("linked_item_id", ""),
        "linked_item_type":   extra.get("linked_item_type", ""),
        "reporter":           row.get("reporter") or "",
        "assignee":           extra.get("assignee", ""),
        "created_at":         (row.get("created_at") or "")
                                if isinstance(row.get("created_at"), str)
                                else (row.get("created_at").isoformat()
                                      if row.get("created_at") else ""),
        "component":          extra.get("component", ""),
        "labels":             extra.get("labels") or [],
        "comment":            row.get("comment") or "",
    }
    return out


def _hydrate_bugs(project_id: str | None) -> list:
    """Return the list of ``BugReport`` dataclass instances to render
    on ``/bug-reports``. DB-first so ``db_id`` is populated; falls
    back to session ``bug_reports_data`` only when the DB read fails
    or no project is active yet (legacy / pre-project flow).
    """
    if project_id:
        try:
            rows = _db.list_bugs(project_id) or []
        except Exception as exc:  # pragma: no cover — DB outage shouldn't 500
            log.warning("hydrate_bugs: list_bugs failed: %s", exc)
            rows = []
        if rows:
            # DB list_bugs is ordered desc by created_at; the template
            # has always shown newest-first, so we keep that order.
            return [dict_to_bug(_bug_row_to_session_dict(r)) for r in rows]
    # Session fallback — older flow that wrote bug_reports_data directly.
    bugs_data = session.get("bug_reports_data", []) or []
    return [dict_to_bug(b) for b in bugs_data]


def _bug_dict_to_db_row(bug_dict: dict) -> dict:
    """Reshape an in-session BugReport dict into the keys engine.db.save_bug
    expects. Anything outside the canonical column set ends up in BugReport.extra."""
    return {
        "id":                  bug_dict.get("id"),
        "title":               bug_dict.get("title"),
        "severity":            bug_dict.get("severity"),
        "priority":            bug_dict.get("priority"),
        "status":              bug_dict.get("status") or "Open",
        "environment":         bug_dict.get("environment"),
        "browser":             bug_dict.get("browser"),
        "os":                  bug_dict.get("os"),
        "version":             bug_dict.get("affects_version") or bug_dict.get("version"),
        "steps_to_reproduce":  bug_dict.get("steps_to_reproduce"),
        "actual_result":       bug_dict.get("actual_result"),
        "expected_result":     bug_dict.get("expected_result"),
        "comment":             bug_dict.get("comment"),
        "reporter":            bug_dict.get("reporter"),
        # Pass everything else (frequency, labels, attachments, found_in_build,
        # linked_item_id, etc.) through the .extra JSON column.
        "frequency":           bug_dict.get("frequency"),
        "affects_version":     bug_dict.get("affects_version"),
        "found_in_build":      bug_dict.get("found_in_build"),
        "linked_item_id":      bug_dict.get("linked_item_id"),
        "linked_item_type":    bug_dict.get("linked_item_type"),
        "assignee":            bug_dict.get("assignee"),
        "component":           bug_dict.get("component"),
        "labels":              bug_dict.get("labels"),
        "attachments":         bug_dict.get("attachments"),
        "preconditions":       bug_dict.get("preconditions"),
        "created_at":          bug_dict.get("created_at"),
    }


def _persist_bug(bug_dict: dict, source: str = "manual",
                 run_id: int | None = None) -> int | None:
    """Mirror an in-session bug into BugReport. Best-effort write."""
    pid = ensure_active_project()
    if not pid:
        return None
    payload = _bug_dict_to_db_row(bug_dict)
    if run_id is not None:
        payload["run_id"] = run_id
    try:
        return _db.save_bug(pid, payload, source=source)
    except Exception as exc:  # pragma: no cover — best-effort
        log.warning("persist bug failed: %s", exc)
        return None


def register(app: Flask) -> None:
    @app.route("/create-bug-report", methods=["POST"])
    def create_bug_report():
        bugs = session.get("bug_reports_data", [])
        existing = [dict_to_bug(b) for b in bugs]
        new_id = generate_bug_id(existing)

        # Manual bug-create form. We accept the ISTQB-mandatory metadata
        # (frequency, affects_version, found_in_build) and Jira workflow
        # fields (assignee, labels) when present and fall back to safe
        # defaults so a tester can submit the minimal required set.
        project_setup = session.get("project_setup", {}) or {}
        labels_raw = request.form.get("labels", "").strip()
        labels = [lbl.strip() for lbl in labels_raw.split(",") if lbl.strip()] if labels_raw else []

        bug = BugReport(
            id=new_id,
            title=request.form.get("title", ""),
            severity=request.form.get("severity", "Major"),
            priority=request.form.get("priority", "High"),
            status="Open",
            environment=request.form.get("environment", ""),
            preconditions=request.form.get("preconditions", ""),
            steps_to_reproduce=request.form.get("steps_to_reproduce", ""),
            actual_result=request.form.get("actual_result", ""),
            expected_result=request.form.get("expected_result", ""),
            frequency=request.form.get("frequency", "Always") or "Always",
            affects_version=(
                request.form.get("affects_version", "").strip()
                or project_setup.get("project_version")
                or project_setup.get("project_name")
                or "Unspecified"
            ),
            found_in_build=request.form.get("found_in_build", "").strip(),
            linked_item_id=request.form.get("linked_item_id", ""),
            linked_item_type=request.form.get("linked_item_type", ""),
            reporter=request.form.get("reporter", ""),
            assignee=request.form.get("assignee", "").strip(),
            component=request.form.get("component", ""),
            labels=labels,
            created_at=datetime.now().isoformat(),
        )

        bug_d = bug_to_dict(bug)
        bugs.append(bug_d)
        session["bug_reports_data"] = bugs
        _persist_bug(bug_d, source="manual")

        flash(g.t.get("bug_saved", "Bug report created successfully") + f" ({new_id})",
              "success")
        return redirect(url_for("bug_reports_page"))

    @app.route("/bug-reports")
    def bug_reports_page():
        # Sprint 4 task 4.2: prefer the DB as source of truth so the
        # ``db_id`` field is populated on every rendered card (the
        # bulk-edit checkboxes need it). Session ``bug_reports_data``
        # stays as the fallback for the legacy / pre-project flow.
        pid = ensure_active_project()
        bugs = _hydrate_bugs(pid)

        # Sprint 5 follow-up: filter by bug source. PR #12 started
        # writing walkthrough findings as bugs with
        # ``linked_item_type="walkthrough"`` + a ``source:walkthrough``
        # label; the operator who runs both modes wants to filter the
        # listing between "what the runner caught" and "what the TC
        # pack caught" without having to grep labels by eye. The
        # filter is a single query-string param so the operator can
        # bookmark / share a filtered URL.
        #
        # Stage 6: LiveExecutor early-exit infra bugs land with
        # ``linked_item_type="live_executor"`` + ``source:live_executor``
        # label. Treat them as walkthrough-sourced for filter purposes
        # so they don't fall into the "manual_tc" bucket — they are
        # auto-generated by the runner, not manually filed against a TC.
        source_filter = (request.args.get("source") or "").strip().lower()
        if source_filter not in ("walkthrough", "manual_tc", ""):
            source_filter = ""
        if source_filter:
            _RUNNER_LINKED_TYPES = {"walkthrough", "live_executor"}
            _RUNNER_LABELS = {"source:walkthrough", "source:live_executor"}
            def _is_walkthrough(bug) -> bool:
                if (getattr(bug, "linked_item_type", "") or "").lower() in _RUNNER_LINKED_TYPES:
                    return True
                labels = getattr(bug, "labels", None) or []
                return any(str(lbl).lower() in _RUNNER_LABELS for lbl in labels)
            if source_filter == "walkthrough":
                bugs = [b for b in bugs if _is_walkthrough(b)]
            else:  # manual_tc
                bugs = [b for b in bugs if not _is_walkthrough(b)]

        stats = {
            "total": len(bugs),
            "open": sum(1 for b in bugs if b.status == "Open"),
            "critical": sum(1 for b in bugs if b.severity == "Critical"),
            "major": sum(1 for b in bugs if b.severity == "Major"),
        }

        return render_template("bug_reports.html", bugs=bugs, stats=stats,
                               severities=BUG_SEVERITIES, priorities=BUG_PRIORITIES,
                               statuses=BUG_STATUSES, frequencies=BUG_FREQUENCIES,
                               source_filter=source_filter)

    @app.route("/bugs/bulk", methods=["POST"])
    def bugs_bulk():
        """Apply ``action`` to every bug whose ``db_id`` is in the
        ``bug_ids[]`` form list. Sprint 4 task 4.2.

        Auth: today this is a project-owner gate (Sprint 1 ``owner_sid``
        check). Once Sprint 5 lands the role system, this swaps to
        ``_require_project_role(pid, "tester")`` for non-destructive
        actions and ``_require_project_role(pid, "admin")`` for
        ``delete``. The audit trail already records the ``actor`` so
        the upgrade is purely a permission tightening.
        """
        pid = ensure_active_project()
        if not pid:
            flash(g.t.get("bug_bulk_no_project",
                          "Pick or create a project before bulk editing."),
                  "error")
            return redirect(url_for("bug_reports_page"))

        # Same ownership gate every other write route honours. Returns
        # the project meta on success and ``abort(403)`` otherwise.
        if _require_project_owner(pid) is None:
            flash(g.t.get("bug_bulk_no_project",
                          "Project not found."), "error")
            return redirect(url_for("bug_reports_page"))

        raw_ids = request.form.getlist("bug_ids")
        ids = sorted({int(x) for x in raw_ids if x.isdigit() and int(x) > 0})
        action = (request.form.get("action") or "").strip()
        # The toolbar uses ``<action>_value`` (e.g. ``status_value``) so
        # each action keeps its own input field; legacy callers can also
        # send ``value=`` unscoped.
        value = (request.form.get(f"{action}_value")
                 or request.form.get("value")
                 or "").strip() or None

        if action not in _db.ALLOWED_BULK_ACTIONS or not ids:
            flash(g.t.get("bug_bulk_invalid",
                          "Pick at least one bug and a valid action."),
                  "error")
            return redirect(url_for("bug_reports_page"))

        actor = get_session_id(session)[:8]
        try:
            n = _db.bulk_update_bugs(
                pid, ids, action=action, value=value, actor=actor,
            )
        except Exception as exc:
            log.warning("bulk_update_bugs failed: %s", exc)
            flash(g.t.get("bug_bulk_failed",
                          "Bulk update failed — see server logs."),
                  "error")
            return redirect(url_for("bug_reports_page"))

        # Refresh the session cache so the next render shows the new
        # values without forcing a hard reload of the session pickle.
        try:
            rows = _db.list_bugs(pid) or []
            session["bug_reports_data"] = [
                _bug_row_to_session_dict(r) for r in rows
            ]
            session.modified = True
        except Exception:  # pragma: no cover — best-effort cache refresh
            pass

        suffix = "s" if n != 1 else ""
        flash(g.t.get("bug_bulk_ok",
                      "Updated {n} bug{s}.").format(n=n, s=suffix),
              "success")
        return redirect(url_for("bug_reports_page"))

    @app.route("/export-bug-reports")
    def export_bug_reports():
        bugs_data = session.get("bug_reports_data", [])
        bugs = [dict_to_bug(b) for b in bugs_data]

        if not bugs:
            flash("No bug reports to export.", "error")
            return redirect(url_for("bug_reports_page"))

        lines = ["# Bug Reports\n"]
        for bug in bugs:
            lines.append(export_bug_report_markdown(bug))
            lines.append("---\n")
        content = "\n".join(lines)

        name = session.get("project_setup", {}).get("project_name", "project").replace(" ", "_")
        return Response(
            content, mimetype="text/markdown",
            headers={"Content-Disposition": f"attachment; filename=bug_reports_{name}.md"},
        )


__all__ = ["register", "_persist_bug"]
