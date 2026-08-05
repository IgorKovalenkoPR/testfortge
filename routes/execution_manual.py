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
from engine import permissions as _perm
from engine.log import get_logger

from ._shared import (pack_checklist, pack_test_cases,
                      reconstruct_checklist, reconstruct_test_cases,
                      resolve_active_project)

log = get_logger(__name__)


def _pack(project_id: str | None) -> tuple[list, list]:
    """The pack to start a run from: the active project's, session first.

    Only for *starting* a run, where the active project is the subject by
    definition. A run that already exists must use :func:`_run_pack`.
    """
    tcs = reconstruct_test_cases(pack_test_cases())
    cls = reconstruct_checklist(pack_checklist())
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


def _run_pack(run: dict) -> tuple[list, list]:
    """The pack belonging to *the run's* project, read from the database.

    Never the session pack. The session holds whatever project the browser
    currently has active, and this used to be consulted first — so a tester
    who switched projects and came back to an open run walked the *other*
    project's content under this run's item ids. Measured: a run in project
    A rendered project B's summaries, and the verdicts recorded against
    them went to A.

    That is not an edge case. Item ids are per-project sequences, so
    ``TC_001`` exists in every project and the ids collide by construction
    rather than by accident — the mismatch is silent, and the walk looks
    perfectly normal.
    """
    project_id = run.get("project_id")
    if not project_id:
        return [], []
    tcs: list = []
    cls: list = []
    try:
        tcs = reconstruct_test_cases(_db.load_test_cases(project_id))
    except Exception as exc:  # pragma: no cover — best-effort
        log.warning("manual run: TC load failed for %s: %s", project_id, exc)
    try:
        cls = reconstruct_checklist(_db.load_checklist(project_id))
    except Exception as exc:  # pragma: no cover — best-effort
        log.warning("manual run: CL load failed for %s: %s", project_id, exc)
    if not tcs and not cls:
        # A run started before the pack was persisted (the pre-E3 flow kept
        # it in the session). Fall back only when the run's project is the
        # active one, so the fallback can never introduce another project's
        # content — the defect above.
        if project_id == resolve_active_project(session, pin=False):
            tcs = reconstruct_test_cases(pack_test_cases())
            cls = reconstruct_checklist(pack_checklist())
    return tcs, cls


def _authorise(run: dict, *, adopt: bool = False) -> None:
    """Abort unless this run is in scope for the caller.

    Two properties are in tension here and both are load-bearing, so this
    resolves them rather than picking one. The walk must survive a lost
    session — that is why the cursor lives in the database, and a hand-off
    to a colleague on another machine is a real workflow. But a run must
    also belong to its project, because the measured defect was a tester
    with project B active resuming project A's run and walking B's content
    under A's ids.

    The rule:

    * **A different project active → 404.** Always, read or write. That is
      the accidental case and the one that corrupts data.
    * **No project active → adopt it on a read.** Following a run link
      selects the run's project, which is exactly what the hand-off needs.
      This grants nothing: with authentication off, the project picker is
      already open to every session, so refusing here would be theatre
      while breaking a documented workflow.
    * **No project active → refuse a write.** A verdict is the thing that
      damages data, and a tester who followed a link has loaded the page
      first, which adopted the project. A POST that arrives without ever
      having read the run is not that flow.
    * **Auth on → the assignee or an admin.** This is the only real
      per-person boundary; without authentication there is no identity to
      enforce one against, and claiming otherwise would overstate what the
      deployment can promise.

    404 rather than 403 for the project mismatch: whether a run id exists
    in a project the caller cannot see is not something to confirm.
    """
    run_pid = run.get("project_id")
    if not run_pid:
        abort(404)

    if _perm.auth_active():
        assignee = str((run.get("env_payload") or {}).get("assignee_id") or "")
        me = _perm.current_user_id() or ""
        if assignee and me and assignee != me and not _perm.is_admin():
            abort(403)

    active = resolve_active_project(session, pin=False)
    if active and active != run_pid:
        abort(404)
    if not active:
        if not adopt:
            abort(404)
        session["project_id"] = run_pid


def _load_run(run_id: int, *, adopt: bool = False) -> tuple[dict, list, list, list]:
    """``(run, queue, results, pack)`` for an open manual run, or 404.

    ``adopt`` is passed on to :func:`_authorise`: reads may select the
    run's project, writes may not.
    """
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
    _authorise(run, adopt=adopt)

    tcs, cls = _run_pack(run)
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

        # Who the walk belongs to. The free-text `tester` was already here
        # and is what the bug report quotes as reporter; `assignee_id` is
        # the machine-readable half that ownership checks and the "my runs"
        # filter need, and it defaults to whoever started the run because
        # that is true in every case and needs no form field.
        assignee_id = ""
        assignee_name = ""
        if _perm.auth_active():
            requested = (request.form.get("assignee_id") or "").strip()
            me = _perm.current_user_id() or ""
            # Only an admin may hand a run to someone else; without that
            # check, "assign" would be a way to write into another
            # tester's queue.
            assignee_id = requested if (requested and _perm.is_admin()) else me
            user = _perm.current_user() or {}
            if assignee_id == (user.get("id") or ""):
                assignee_name = str(user.get("name") or user.get("email") or "")

        env_payload = {
            "mode": "manual",
            "manual_queue": mr.queue_to_payload(queue),
            "environment": (request.form.get("env_custom") or "").strip(),
            "tester": ((request.form.get("tester") or "").strip()
                       or assignee_name),
            "assignee_id": assignee_id,
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
        run, queue, results, _pk = _load_run(run_id, adopt=True)
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
            current_verdict=verdicts.get(current.key, {}) if current else {},
            all_verdicts=mr.VERDICTS,
            defect_verdicts=mr.DEFECT_VERDICTS,
            finished=progress.finished,
        )

    @app.route("/test-execution/manual/<int:run_id>/resume", methods=["GET"])
    def manual_run_resume(run_id):
        """Land on the first item without a verdict.

        A separate URL from the page so the "Resume" link on the execution
        page does not have to know where the walk got to — the cursor is
        derived, and computing it in a template would duplicate the
        derivation that ``compute_progress`` owns.
        """
        run, queue, results, _pk = _load_run(run_id, adopt=True)
        progress = mr.compute_progress(queue, results)
        return redirect(url_for("manual_run_page", run_id=run_id,
                                i=min(progress.cursor,
                                      max(0, len(queue) - 1))))

    @app.route("/test-execution/manual/<int:run_id>/verdict",
               methods=["POST"])
    def manual_run_verdict(run_id):
        run, queue, results, _pk = _load_run(run_id)
        external_id = (request.form.get("external_id") or "").strip()
        verdict = mr.coerce_verdict(request.form.get("verdict"))
        notes = (request.form.get("notes") or "").strip()

        # The kind comes from the form because the id alone does not
        # identify the row: a test case and a checklist item may carry the
        # same label. An older form that posts no kind still resolves, by
        # falling back to the first item with that id — which is what the
        # whole walk did before, so the fallback is no worse than the
        # previous behaviour and the new form is better than both.
        kind = (request.form.get("kind") or "").strip()
        if kind:
            item = next((q for q in queue if q.key == (kind, external_id)), None)
        else:
            item = next((q for q in queue if q.external_id == external_id), None)
        if item is None or not verdict:
            flash("That verdict could not be recorded — unknown item or "
                  "status.", "warning")
            return redirect(url_for("manual_run_page", run_id=run_id))

        bug_id = None
        if verdict in mr.DEFECT_VERDICTS and \
                request.form.get("file_bug") == "1":
            bug_id = _file_bug(run, item, verdict, notes)

        already = mr.verdicts_by_item(results).get(item.key)
        try:
            if already:
                # Overwrite rather than append: a tester correcting a
                # mis-click must not leave the run counting the item twice.
                _db.update_case_result(
                    run_id, external_id, case_kind=item.kind,
                    status=verdict, notes=notes,
                    **({"bug_report_id": bug_id} if bug_id else {}))
            else:
                _db.save_case_result(
                    run_id, case_external_id=external_id,
                    case_kind=item.kind, status=verdict, notes=notes,
                    # A person clicked this one.
                    source="manual",
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
