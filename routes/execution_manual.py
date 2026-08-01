"""TestFortge — the step-by-step manual execution walk.

  * POST /test-execution/manual/start          — open a run, redirect to it
  * GET  /test-execution/manual/<run_id>       — the current item
  * POST /test-execution/manual/<run_id>/verdict — record one verdict
  * POST /test-execution/manual/<run_id>/finish  — close the run

Executing a pack by hand used to be one bulk form: every item on screen
with a status drop-down beside it. That records a decision, it does not
help anyone make one — the tester had already done the work elsewhere.

This walks one item at a time with its preconditions, steps and expected
result in front of the tester. See :mod:`engine.manual_run` for why the
cursor is derived from the results in the database rather than kept in the
session.

A separate module from ``routes/execution.py`` deliberately: that file is
2,800 lines and the Stage-7 refactor split it once already. A new surface
belongs beside it, not inside it.
"""
from __future__ import annotations

from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)

from engine import bug_report as _bug_report
from engine import db as _db
from engine import manual_run as mr
from engine.log import get_logger

from ._shared import (reconstruct_checklist, reconstruct_test_cases,
                      resolve_active_project)

log = get_logger(__name__)


def _pack(project_id: str | None) -> tuple[list, list]:
    """The project's test cases and checklist, session first then DB."""
    tcs = reconstruct_test_cases(session.get("test_cases_data", []))
    cls = reconstruct_checklist(session.get("checklist_data", []))
    if project_id:
        if not tcs:
            try:
                tcs = reconstruct_test_cases(_db.load_test_cases(project_id))
            except Exception as exc:  # pragma: no cover — best-effort
                log.warning("manual run: TC reload failed: %s", exc)
        if not cls:
            try:
                cls = reconstruct_checklist(_db.load_checklist(project_id))
            except Exception as exc:  # pragma: no cover — best-effort
                log.warning("manual run: CL reload failed: %s", exc)
    return tcs, cls


def _load_run(run_id: int) -> tuple[dict, list, list, list]:
    """``(run, queue, results, pack)`` for an open manual run, or 404."""
    run = None
    try:
        run = _db.get_execution_run(run_id)
    except Exception as exc:  # pragma: no cover — best-effort
        log.warning("manual run %s lookup failed: %s", run_id, exc)
    if run is None:
        abort(404)

    payload = (run.get("env_payload") or {}).get("manual_queue")
    if payload is None:
        # Not a manual run — the walk would have nothing to show, and
        # silently rendering an empty page reads as data loss.
        abort(404)

    tcs, cls = _pack(run.get("project_id"))
    queue = mr.restore_queue(payload, tcs, cls)
    try:
        results = _db.list_case_results(run_id)
    except Exception as exc:  # pragma: no cover — best-effort
        log.warning("manual run %s results failed: %s", run_id, exc)
        results = []
    return run, queue, results, [tcs, cls]


def register(app: Flask) -> None:

    @app.route("/test-execution/manual/start", methods=["POST"])
    def manual_run_start():
        """Open a manual run over the selected items."""
        pid = resolve_active_project(session)
        if not pid:
            flash("Select or create a project before running a pack.",
                  "warning")
            return redirect(url_for("test_execution_page"))

        tcs, cls = _pack(pid)
        selected = request.form.getlist("selected_items")
        source = (request.form.get("source") or "").strip()
        # The source picker scopes the walk when the operator used it; with
        # nothing selected the walk covers everything, which is what the
        # existing "leave all checked" convention already means.
        if source == "test_cases":
            cls = []
        elif source == "checklist":
            tcs = []

        queue = mr.build_queue(tcs, cls, selected)
        if not queue:
            flash("Nothing to run — the selection is empty.", "warning")
            return redirect(url_for("test_execution_page"))

        env_payload = {
            "mode": "manual",
            "manual_queue": mr.queue_to_payload(queue),
            "environment": (request.form.get("env_custom") or "").strip(),
            "tester": (request.form.get("tester") or "").strip(),
        }
        try:
            run_id = _db.start_execution_run(
                pid, env_payload,
                base_url=(request.form.get("base_url") or "").strip() or None)
        except Exception as exc:
            log.exception("manual run start failed")
            flash(f"Could not open the run: {exc}", "danger")
            return redirect(url_for("test_execution_page"))

        return redirect(url_for("manual_run_page", run_id=run_id))

    @app.route("/test-execution/manual/<int:run_id>", methods=["GET"])
    def manual_run_page(run_id):
        run, queue, results, _pk = _load_run(run_id)
        progress = mr.compute_progress(queue, results)
        verdicts = mr.verdicts_by_item(results)

        # ``?i=`` lets the tester step back to re-read or correct an item.
        # Clamped rather than 404'd: an out-of-range index in a URL someone
        # edited should land somewhere sensible, not on an error page.
        try:
            index = int(request.args.get("i", progress.cursor))
        except (TypeError, ValueError):
            index = progress.cursor
        index = max(0, min(index, max(0, len(queue) - 1)))

        current = queue[index] if queue and index < len(queue) else None
        return render_template(
            "test_execution_manual.html",
            run=run, run_id=run_id, queue=queue, current=current,
            index=index, progress=progress, verdicts=verdicts,
            current_verdict=verdicts.get(
                current.external_id, {}) if current else {},
            all_verdicts=mr.VERDICTS,
            defect_verdicts=mr.DEFECT_VERDICTS,
            finished=progress.finished,
        )

    @app.route("/test-execution/manual/<int:run_id>/verdict",
               methods=["POST"])
    def manual_run_verdict(run_id):
        run, queue, results, _pk = _load_run(run_id)
        external_id = (request.form.get("external_id") or "").strip()
        verdict = mr.coerce_verdict(request.form.get("verdict"))
        notes = (request.form.get("notes") or "").strip()

        item = next((q for q in queue if q.external_id == external_id), None)
        if item is None or not verdict:
            flash("That verdict could not be recorded — unknown item or "
                  "status.", "warning")
            return redirect(url_for("manual_run_page", run_id=run_id))

        bug_id = None
        if verdict in mr.DEFECT_VERDICTS and \
                request.form.get("file_bug") == "1":
            bug_id = _file_bug(run, item, verdict, notes)

        already = mr.verdicts_by_item(results).get(external_id)
        try:
            if already:
                # Overwrite rather than append: a tester correcting a
                # mis-click must not leave the run counting the item twice.
                _db.update_case_result(
                    run_id, external_id, status=verdict, notes=notes,
                    **({"bug_report_id": bug_id} if bug_id else {}))
            else:
                _db.save_case_result(
                    run_id, case_external_id=external_id,
                    case_kind=item.kind, status=verdict, notes=notes,
                    bug_report_id=bug_id)
        except Exception as exc:
            log.exception("manual verdict save failed")
            flash(f"Could not record the verdict: {exc}", "danger")
            return redirect(url_for("manual_run_page", run_id=run_id))

        if bug_id:
            flash(f"Verdict recorded and bug #{bug_id} filed.", "success")

        # Advance to the next item without a verdict, so correcting an
        # earlier one returns the tester to where they were rather than
        # restarting the walk.
        refreshed = mr.compute_progress(queue, _db.list_case_results(run_id))
        if refreshed.finished:
            return redirect(url_for("manual_run_page", run_id=run_id))
        return redirect(url_for("manual_run_page", run_id=run_id,
                                i=refreshed.cursor))

    @app.route("/test-execution/manual/<int:run_id>/finish",
               methods=["POST"])
    def manual_run_finish(run_id):
        run, queue, results, _pk = _load_run(run_id)
        progress = mr.compute_progress(queue, results)
        try:
            _db.finish_execution_run(
                run_id,
                status="completed" if progress.finished else "partial",
                stats=mr.run_stats(progress))
        except Exception as exc:
            log.exception("manual run finish failed")
            flash(f"Could not close the run: {exc}", "danger")
            return redirect(url_for("manual_run_page", run_id=run_id))

        if progress.finished:
            flash(f"Run closed — {progress.executed} executed, "
                  f"{progress.pass_rate}% passed.", "success")
        else:
            # Closing early is legitimate, but the number has to say so:
            # a partial run reported as complete would overstate coverage.
            flash(f"Run closed early — {progress.done} of {progress.total} "
                  f"items have a verdict.", "warning")
        return redirect(url_for("test_execution_page"))


def _file_bug(run: dict, item, verdict: str, notes: str) -> int | None:
    """Create a bug from a failed manual check. Returns its id, or None.

    Deliberately best-effort: a bug that could not be filed must not lose
    the verdict the tester just recorded, which is the more expensive
    thing to re-do.
    """
    try:
        from engine.bug_template import severity_priority
        severity, priority = severity_priority(
            "unknown", item.section or "", item.summary or "")
    except Exception:  # pragma: no cover — defensive
        severity, priority = "Major", "Medium"

    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(item.steps, 1)) \
        or "1. Perform the check described in the summary"
    body = {
        # NOT the test case's own title. Every objective opens "Verify
        # that …", and a defect store where each headline is an
        # instruction to check something tells the reader nothing about
        # what broke. bug_report negates the clause instead.
        "title": _bug_report.defect_title_from_objective(
            item.summary, section=item.section, verdict=verdict),
        "severity": severity,
        "priority": priority,
        # Must be a member of bug_report.BUG_STATUSES — the Bug Reports
        # "Open" tile and every status filter compare against it, so a
        # value outside the vocabulary makes the bug invisible to both.
        "status": "Open",
        "environment": (run.get("env_payload") or {}).get("environment", ""),
        # The state the tester started from is what makes the report
        # reproducible; dropping it puts the burden back on the reader.
        "preconditions": item.preconditions or "",
        "steps_to_reproduce": steps,
        # The tester's own words are the actual result. Inventing one from
        # the expected result would put words in their mouth.
        "actual_result": notes or f"The check was marked {verdict} during a "
                                  f"manual run; no detail was recorded.",
        "expected_result": item.expected_result or item.summary,
        "comment": f"Filed from manual run #{run.get('id')} "
                   f"({item.external_id}).",
        "reporter": (run.get("env_payload") or {}).get("tester", "") or "",
        "extra": {"manual_run_id": run.get("id"),
                  "case_external_id": item.external_id,
                  "verdict": verdict},
    }
    try:
        return _db.save_bug(run.get("project_id"), body, source="execution")
    except Exception as exc:  # pragma: no cover — best-effort
        log.warning("manual run: bug filing failed: %s", exc)
        return None


__all__ = ["register"]
