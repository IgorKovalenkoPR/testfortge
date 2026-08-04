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
from engine import bug_areas as _bug_areas
from engine import permissions as _perm
from engine import workspace as _workspace
# CSV injection guard, shared with engine/exporter.py — a cell beginning
# with = + - @ is executed as a formula by Excel on open.
from engine.exporter import _sanitize_cell

from ._shared import (ensure_active_project, get_session_id,
                      resolve_active_project)
from .projects import _require_project_owner

log = get_logger(__name__)


#: DB row → the session-flat shape ``dict_to_bug`` consumes.
#:
#: The mapping itself moved to :func:`engine.workspace.bug_row_to_dict` in
#: E3.2, so the repository and this module cannot disagree about it. They
#: did once: the copy that shaped runs in ``routes/projects.py`` did not
#: know about every field, which is how switching projects lost run
#: history. Kept as an alias because the name is used throughout this file.
_bug_row_to_session_dict = _workspace.bug_row_to_dict


def _hydrate_bugs(project_id: str | None,
                  run_id: int | None = None) -> list:
    """Return the list of ``BugReport`` dataclass instances to render
    on ``/bug-reports``. DB-first so ``db_id`` is populated; falls
    back to session ``bug_reports_data`` only when the DB read fails
    or no project is active yet (legacy / pre-project flow).

    ``run_id`` scopes the DB read to a single Test Execution run (the
    run filter — see :func:`bug_reports_page`). The session fallback
    is intentionally *not* run-scoped: run scoping is a DB-era feature
    and the fallback only fires when there is no DB-backed project, so
    the run dropdown never renders in that mode anyway.
    """
    if project_id:
        try:
            rows = _db.list_bugs(project_id, run_id=run_id) or []
        except Exception as exc:  # pragma: no cover — DB outage shouldn't 500
            log.warning("hydrate_bugs: list_bugs failed: %s", exc)
            rows = []
        # When a run is explicitly requested we honour an empty result
        # (that run genuinely filed no bugs) rather than leaking the
        # session cache from a different scope.
        if rows or run_id is not None:
            # DB list_bugs is ordered desc by created_at; the template
            # has always shown newest-first, so we keep that order.
            return [dict_to_bug(_bug_row_to_session_dict(r)) for r in rows]
    # Session fallback — older flow that wrote bug_reports_data directly.
    bugs_data = session.get("bug_reports_data", []) or []
    return [dict_to_bug(b) for b in bugs_data]


def _run_filter_options(project_id: str,
                        counts: dict | None = None) -> list[dict]:
    """Build the run-filter dropdown data for ``/bug-reports``.

    Returns a list of ``{"id", "label", "bug_count"}`` dicts, newest
    run first — only runs that actually filed at least one bug are
    included, so the dropdown doesn't fill with empty passes. The
    label reads e.g. ``"Run #42 · testfort.com · 2026-07-13 14:30"``
    so the operator can tell runs apart by site + time at a glance.

    ``counts`` (``{run_id: bug_count}`` from
    :func:`engine.db.count_bugs_by_run`) is accepted pre-computed so
    the caller can reuse it for the unfiltered project total without a
    second grouped query.
    """
    try:
        runs = _db.list_execution_runs(project_id) or []
        if counts is None:
            counts = _db.count_bugs_by_run(project_id) or {}
    except Exception as exc:  # pragma: no cover — best-effort
        log.warning("run_filter_options failed: %s", exc)
        return []

    opts: list[dict] = []
    for r in runs:
        rid = r.get("id")
        if rid is None:
            continue
        n = counts.get(rid, 0)
        if not n:
            continue  # skip runs that produced no bugs
        # Host from base_url for a compact, recognisable label.
        host = ""
        base_url = r.get("base_url") or ""
        if base_url:
            try:
                from urllib.parse import urlparse
                host = urlparse(base_url).netloc or base_url
            except Exception:  # pragma: no cover — defensive
                host = base_url
        started = (r.get("started_at") or "")
        if isinstance(started, str) and len(started) >= 16:
            started = started[:16].replace("T", " ")
        label_bits = [f"Run #{rid}"]
        if host:
            label_bits.append(host)
        if started:
            label_bits.append(str(started))
        opts.append({
            "id": rid,
            "label": " · ".join(label_bits),
            "bug_count": n,
        })
    return opts


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
        # PR-H smart-filing metadata — lands in BugReport.extra so
        # cross-run dedup can query it back out, the per-bug
        # annotation diagnostic surfaces in the UI, and the
        # page-aggregated broken-image bug can describe N filenames.
        "defect_class":        bug_dict.get("defect_class"),
        "page_url":            bug_dict.get("page_url"),
        "dedup_signature":     bug_dict.get("dedup_signature"),
        "occurrence_count":    bug_dict.get("occurrence_count"),
        "annotation_status":   bug_dict.get("annotation_status"),
        "aggregated_filenames": bug_dict.get("aggregated_filenames"),
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

        # Run filter (this PR): scope the listing to a single Test
        # Execution run so the operator can look at just the latest
        # pass instead of every bug the project ever accumulated
        # across repeated runs. ``run`` is a query-string param so a
        # scoped view is bookmarkable, exactly like ``source`` below.
        #   * ``run=latest``   — the most recent run that filed a bug
        #   * ``run=<int>``    — that specific run id
        #   * absent / "all"   — no run scoping (historical default)
        # Invalid / stale ids fall back to the unscoped view rather
        # than silently showing zero bugs.
        # One grouped count query, reused for both the dropdown option
        # counts and the unfiltered project total below.
        try:
            run_counts = _db.count_bugs_by_run(pid) if pid else {}
        except Exception as exc:  # pragma: no cover — best-effort
            log.warning("count_bugs_by_run failed: %s", exc)
            run_counts = {}
        run_options = _run_filter_options(pid, run_counts) if pid else []
        run_raw = (request.args.get("run") or "").strip().lower()
        run_id: int | None = None
        run_filter = ""  # echoed to the template for active-state
        if run_raw and run_raw != "all":
            if run_raw == "latest":
                if run_options:
                    run_id = run_options[0]["id"]
                    run_filter = "latest"
            elif run_raw.isdigit():
                candidate = int(run_raw)
                if any(o["id"] == candidate for o in run_options):
                    run_id = candidate
                    run_filter = str(candidate)

        bugs = _hydrate_bugs(pid, run_id=run_id)

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

        # ── Quality-attribute filter (PR-6) ──────────────────────
        #
        # Counted BEFORE the filter is applied, so every chip keeps its
        # true number while one is active — a chip whose count changed to
        # match the filtered view would tell the operator nothing.
        area_counts = _bug_areas.counts_by_area(bugs)
        area_filter = _bug_areas.coerce_area(request.args.get("area"))
        if area_filter:
            bugs = [b for b in bugs
                    if _bug_areas.resolve_area(b) == area_filter]

        stats = {
            "total": len(bugs),
            "open": sum(1 for b in bugs if b.status == "Open"),
            "critical": sum(1 for b in bugs if b.severity == "Critical"),
            "major": sum(1 for b in bugs if b.severity == "Major"),
        }

        # Unfiltered project bug total for the "Reset Project bugs"
        # affordance. Reset deletes EVERY bug on the project regardless
        # of the active source/run filter, so its button + confirm
        # dialog must show the true count — not ``stats.total``, which
        # now reflects the filtered view. Falls back to the filtered
        # count in the session-only path (no DB project, no run_counts).
        project_bug_total = sum(run_counts.values()) if run_counts \
            else stats["total"]

        return render_template("bug_reports.html", bugs=bugs, stats=stats,
                               severities=BUG_SEVERITIES, priorities=BUG_PRIORITIES,
                               statuses=BUG_STATUSES, frequencies=BUG_FREQUENCIES,
                               source_filter=source_filter,
                               run_options=run_options, run_filter=run_filter,
                               areas=_bug_areas.AREAS,
                               area_counts=area_counts,
                               area_filter=area_filter,
                               project_bug_total=project_bug_total)

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

        # Most bulk actions are ordinary triage; `delete` destroys evidence
        # a tester gathered and cannot be undone. The route as a whole is
        # user-level (see engine/route_policy.POLICY) and this one action
        # asks for admin — which is exactly what the note left in this
        # function during Sprint 4 anticipated. Checked here rather than by
        # splitting the endpoint, because the toolbar posts every action to
        # one URL and changing that buys nothing.
        if action == "delete" and not _perm.has_role("admin"):
            flash(g.t.get(
                "bug_bulk_delete_admin",
                "Deleting bug reports is limited to admins. Close them "
                "instead, or ask an admin."), "error")
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

    @app.route("/bugs/reset", methods=["POST"])
    def bugs_reset():
        """Hard-delete every bug attached to the active project.

        PR-H: operators who hammered Test Execution multiple times on
        an unchanged site ended up with 130+ bug rows that all
        describe the same handful of defects on the site. The
        cross-run dedup added in this PR stops fresh duplicates from
        piling up, but the pre-PR-H accumulation is still on disk —
        operators need a one-click "start over" affordance to clear
        the existing pile without dropping the entire project.

        Auth gate mirrors ``/bugs/bulk``: project-owner check via
        ``_require_project_owner``. The reason every operator sees a
        confirm modal in the UI before this POST fires is the same
        rationale — bulk delete is irreversible and the action
        deletes EVERY bug row, not a checkbox subset.

        Form parameters:
            * ``confirm=yes`` (mandatory) — the template's modal sets
              this on the "Yes, reset" button so a stray POST from a
              broken script can't wipe the project.
        """
        pid = ensure_active_project()
        if not pid:
            flash(g.t.get("bug_bulk_no_project",
                          "Pick or create a project before resetting."),
                  "error")
            return redirect(url_for("bug_reports_page"))
        if _require_project_owner(pid) is None:
            flash(g.t.get("bug_bulk_no_project",
                          "Project not found."), "error")
            return redirect(url_for("bug_reports_page"))
        # Mandatory confirm token — the template's modal sets this.
        if (request.form.get("confirm") or "").strip().lower() != "yes":
            flash(g.t.get(
                "bug_reset_unconfirmed",
                "Reset cancelled — confirmation missing."), "error")
            return redirect(url_for("bug_reports_page"))

        try:
            n = _db.delete_bugs_for_project(pid)
        except Exception as exc:
            log.warning("delete_bugs_for_project failed: %s", exc)
            flash(g.t.get(
                "bug_reset_failed",
                "Reset failed — see server logs."), "error")
            return redirect(url_for("bug_reports_page"))

        # Wipe the session cache so the next render doesn't show the
        # rows we just deleted from the DB.
        session["bug_reports_data"] = []
        session.modified = True

        suffix = "s" if n != 1 else ""
        flash(g.t.get(
            "bug_reset_ok",
            "Project reset — {n} bug{s} deleted.").format(n=n, s=suffix),
            "success")
        return redirect(url_for("bug_reports_page"))

    @app.route("/export-bug-reports.csv")
    def export_bug_reports_csv():
        """The team's own bug sheet, column for column.

        Shape taken from the "Bugs" tab of
        ``Training Plan_Horban Yaroslavna.xlsx``, with ``Area`` added —
        the operator asked for the six quality attributes and the
        reference sheet has no column for them.
        """
        import csv
        import io as _io

        pid = resolve_active_project(session)
        bugs = _hydrate_bugs(pid)
        if not bugs:
            flash("No bug reports to export.", "error")
            return redirect(url_for("bug_reports_page"))

        buf = _io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "Bug ID", "Summary", "Area", "Status", "Priority", "Severity",
            "Reporter", "Date", "Environment", "Preconditions",
            "Steps to reproduce", "Actual result", "Expected result",
            "Attachment", "Note", "Assignee",
        ])
        for bug in bugs:
            attachment = getattr(bug, "attachment", "") or ""
            if not attachment:
                # The reference sheet has one Attachment cell per row; the
                # runner records a list, so join rather than drop the tail.
                attachment = ", ".join(
                    str(a) for a in (getattr(bug, "attachments", None) or []))
            writer.writerow([_sanitize_cell(v) for v in (
                bug.id, bug.title,
                getattr(bug, "bug_area", "Functional"),
                bug.status, bug.priority, bug.severity,
                bug.reporter, getattr(bug, "created_at", ""),
                bug.environment, getattr(bug, "preconditions", ""),
                bug.steps_to_reproduce, bug.actual_result,
                bug.expected_result, attachment,
                bug.comment, getattr(bug, "assignee", ""),
            )])
        name = ((session.get("project_setup") or {}).get("project_name")
                or "project").replace(" ", "_")
        return Response(
            buf.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition":
                     f"attachment; filename=bug_reports_{name}.csv"})

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
