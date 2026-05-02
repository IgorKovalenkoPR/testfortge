"""TestFortge — Test-case / checklist generation + export routes.

  * GET/POST /test-cases   — generate + display test cases
  * GET/POST /checklist    — generate + display checklist
  * GET /export/<fmt>      — markdown/html/csv/xlsx export
"""

from __future__ import annotations

import os
import tempfile

from flask import (Flask, Response, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.utils import secure_filename

from engine.file_parser import split_into_requirements
from engine.qa_persona import is_instruction
from engine.user_story_generator import generate_user_stories
from engine.testcase_generator import (
    generate_test_cases, generate_checklist, generate_traceability,
)
from engine.exporter import (
    export_markdown, export_html,
    export_csv_testcases, export_csv_checklist,
    export_xlsx_testcases, export_xlsx_checklist,
)
from engine.imports import parse_test_cases as import_parse_test_cases
from engine.imports import parse_checklist as import_parse_checklist
from engine.job_queue import get_queue, DONE, FAILED

from engine import db as _db
from engine.log import get_logger

from ._shared import (
    reconstruct_stories, reconstruct_test_cases, reconstruct_checklist,
    tc_to_dict, cl_to_dict, story_to_dict, get_session_id,
    parse_page_input, extract_resource_urls, ensure_active_project,
)

# Hard cap on concurrent generation jobs per session — same threshold
# as automation/estimation. Prevents a runaway tab from monopolising
# the worker pool on Render free tier.
MAX_CONCURRENT_GEN_JOBS = 2

_log = get_logger(__name__)


def _persist_test_cases(tc_dicts: list[dict]) -> None:
    """Mirror the in-session TC list into Postgres for the active project.

    Best-effort: a DB outage must not block the user from seeing their
    generated cases on screen. Errors are logged and swallowed."""
    pid = ensure_active_project()
    if not pid:
        return
    try:
        _db.save_test_cases(pid, tc_dicts)
    except Exception as exc:  # pragma: no cover — best-effort write
        _log.warning("persist test cases failed: %s", exc)


def _persist_checklist(cl_dicts: list[dict]) -> None:
    """Same contract as :func:`_persist_test_cases` but for Checklist."""
    pid = ensure_active_project()
    if not pid:
        return
    try:
        _db.save_checklist(pid, cl_dicts)
    except Exception as exc:  # pragma: no cover
        _log.warning("persist checklist failed: %s", exc)



def _back_to_caller(default: str = "test_cases_page", extra_qs: str = "") -> str:
    """Resolve the URL to redirect to after a form submission so the
    user stays on the page they came from.

    Looks at the Referer header first; if it points at a page hosted
    by us (and the endpoint is one of the accepted return targets), we
    use it. Otherwise we fall back to ``default`` so a missing or
    spoofed Referer can't bounce the user to an external site.

    ``extra_qs`` is appended verbatim to the resolved URL — callers
    use this to pass ``auto_run=1`` so the destination page knows to
    auto-click the Run button after the upload landed.
    """
    from urllib.parse import urlparse
    target = url_for(default)
    referrer = (request.referrer or "").strip()
    if referrer:
        try:
            host = urlparse(referrer).path or ""
            for ep, prefix in (
                ("test_execution_page", "/test-execution"),
                ("test_cases_page",      "/test-cases"),
                ("checklist_page",       "/checklist"),
            ):
                if host == prefix or host.startswith(prefix + "/") or host == prefix + "/":
                    target = url_for(ep)
                    break
        except Exception:
            pass
    if extra_qs:
        target = target + ("&" if "?" in target else "?") + extra_qs
    return target


def _drain_tc_job_into_session() -> None:
    """If a previous POST left a tc_gen job_id in the session and that
    job is now finished, copy the result into the session keys the
    GET render reads. No-op when no job_id is stored or the job is
    still pending. Best-effort: never raises.
    """
    job_id = session.get("tc_gen_job_id")
    if not job_id:
        return
    try:
        job = get_queue().get(job_id)
    except Exception:
        return
    if not job or job.kind != "tc_gen":
        return
    if job.status == DONE and job.result:
        r = job.result
        session["test_cases_data"]   = r.get("tc_dicts", [])
        session["user_stories"]      = r.get("stories", [])
        session["raw_requirements"]  = r.get("raw_requirements", [])
        session["traceability_data"] = r.get("trace", [])
        session.pop("tc_gen_job_id", None)
    elif job.status == FAILED:
        # Surface the worker error once and stop polling for this id.
        from flask import flash as _flash, g as _g
        _flash(
            (_g.t.get("mvp_gen_failed", "Generation failed") if hasattr(_g, "t")
             else "Generation failed")
            + ": " + (job.error or "unknown"),
            "error",
        )
        session.pop("tc_gen_job_id", None)


def _drain_cl_job_into_session() -> None:
    """Same as :func:`_drain_tc_job_into_session` but for the
    checklist queue. Handles the matching cl_gen_job_id key."""
    job_id = session.get("cl_gen_job_id")
    if not job_id:
        return
    try:
        job = get_queue().get(job_id)
    except Exception:
        return
    if not job or job.kind != "cl_gen":
        return
    if job.status == DONE and job.result:
        r = job.result
        session["checklist_data"]    = r.get("cl_dicts", [])
        session["user_stories"]      = r.get("stories", [])
        session["raw_requirements"]  = r.get("raw_requirements", [])
        session.pop("cl_gen_job_id", None)
    elif job.status == FAILED:
        from flask import flash as _flash, g as _g
        _flash(
            (_g.t.get("mvp_gen_failed", "Generation failed") if hasattr(_g, "t")
             else "Generation failed")
            + ": " + (job.error or "unknown"),
            "error",
        )
        session.pop("cl_gen_job_id", None)


def register(app: Flask) -> None:
    # Hard-coded blocking budget for the legacy sync POST. Render's
    # gunicorn timeout is 300 s; anything we let block beyond ~250 s
    # risks a worker kill and a 502 on the user's tab. The async
    # /test-cases/run-async pair has no such restriction — it submits
    # to JobQueue and returns immediately.
    SYNC_GEN_BUDGET_S = 90

    @app.route("/test-cases", methods=["GET", "POST"])
    def test_cases_page():
        if request.method == "POST":
            # The sync POST is now a thin shim around the JobQueue so a
            # slow LLM never holds the single gunicorn worker hostage
            # past the 300 s ceiling — that's what produced the 502 the
            # operator reported. We submit the same job a JS client
            # would, then block for up to SYNC_GEN_BUDGET_S seconds. If
            # it finishes, render the result; if not, redirect to GET
            # with a flash and let the user refresh once the background
            # job is done.
            raw_lines, errors, custom_prompt = parse_page_input()

            if not raw_lines:
                flash(g.t.get("mvp_no_input",
                              "Please enter requirements or upload files."), "error")
                return render_template("test_cases.html",
                                       test_cases=[], traceability=[],
                                       has_data=False, errors=errors)

            sync_pid = ensure_active_project()
            sync_raw_lines = raw_lines
            sync_custom_prompt = custom_prompt

            def _sync_worker(raw_lines=sync_raw_lines,
                             custom_prompt=sync_custom_prompt,
                             pid=sync_pid):
                parsed = split_into_requirements(raw_lines)
                parsed = [r for r in parsed if not is_instruction(r.text)]
                raw_for_persona = (
                    [{"id": r.id, "text": r.text} for r in parsed]
                    if parsed else
                    [{"id": f"RAW-{i+1}", "text": line}
                     for i, line in enumerate(raw_lines) if line.strip()]
                )
                stories = (generate_user_stories(parsed, custom_prompt)
                           if parsed else [])
                tcl = generate_test_cases(stories, custom_prompt,
                                          raw_requirements=raw_for_persona)
                trc = generate_traceability(stories, tcl) if tcl else []
                tcd = [tc_to_dict(tc) for tc in tcl]
                if pid and tcd:
                    try: _db.save_test_cases(pid, tcd)
                    except Exception: pass
                return {"tc_dicts": tcd,
                        "stories": [story_to_dict(s) for s in stories],
                        "raw_requirements": raw_for_persona,
                        "trace": trc}

            sid = get_session_id(session)
            job_id = get_queue().submit("tc_gen", _sync_worker,
                                        meta={"session_id": sid})
            session["tc_gen_job_id"] = job_id

            import time as _time
            deadline = _time.time() + SYNC_GEN_BUDGET_S
            job = None
            while _time.time() < deadline:
                job = get_queue().get(job_id)
                if job is None or job.status in (DONE, FAILED):
                    break
                _time.sleep(0.5)

            if job is None or job.status != DONE:
                # Job is still running in the background — don't block
                # the worker any longer. Holding screen + flash; the
                # GET path will show results once the background job
                # writes them into the session.
                flash(
                    g.t.get(
                        "mvp_gen_in_background",
                        "Generation is still running in the background. "
                        "Please wait a few seconds and refresh this page."
                    ),
                    "info",
                )
                return redirect(url_for("test_cases_page"))

            r = job.result or {}
            session["test_cases_data"] = r.get("tc_dicts", [])
            session["user_stories"] = r.get("stories", [])
            session["raw_requirements"] = r.get("raw_requirements", [])
            session["traceability_data"] = r.get("trace", [])

            tc_list = reconstruct_test_cases(session["test_cases_data"])
            trace = session["traceability_data"]
            if not tc_list:
                flash(g.t.get(
                    "mvp_no_quality_requirements",
                    "Could not detect any testable requirements in the provided input."),
                    "error")
                return render_template("test_cases.html",
                                       test_cases=[], traceability=[],
                                       has_data=False, errors=errors)
            return render_template("test_cases.html",
                                   test_cases=tc_list, traceability=trace,
                                   has_data=True, errors=errors,
                                   resource_urls=extract_resource_urls())

            # ── Legacy code path retained below for reference; the
            #    block above replaces it. Falls through harmlessly. ──
            parsed_reqs = split_into_requirements(raw_lines)

            # Filter out instruction lines — "Create test cases...",
            # "Pay attention..." are commands TO the tool, not requirements.
            parsed_reqs = [r for r in parsed_reqs if not is_instruction(r.text)]

            # Even if split_into_requirements finds nothing (e.g. URL-only
            # input), the QA persona can still analyze the raw text.
            raw_reqs_for_persona = ([{"id": r.id, "text": r.text} for r in parsed_reqs]
                                    if parsed_reqs
                                    else [{"id": f"RAW-{i+1}", "text": line}
                                          for i, line in enumerate(raw_lines) if line.strip()])

            if parsed_reqs:
                new_stories = generate_user_stories(parsed_reqs, custom_prompt)
                session["user_stories"] = [story_to_dict(s) for s in new_stories]
                session["raw_requirements"] = [{"id": r.id, "text": r.text} for r in parsed_reqs]
            else:
                new_stories = []
                session["user_stories"] = []
                session["raw_requirements"] = raw_reqs_for_persona

            tc_list = generate_test_cases(new_stories, custom_prompt,
                                          raw_requirements=raw_reqs_for_persona)

            if not tc_list:
                flash(g.t.get("mvp_no_quality_requirements",
                              "Could not detect any testable requirements in the provided input."),
                      "error")
                return render_template("test_cases.html",
                                       test_cases=[], traceability=[],
                                       has_data=False, errors=errors)

            trace = generate_traceability(new_stories, tc_list)
            tc_dicts = [tc_to_dict(tc) for tc in tc_list]
            session["test_cases_data"] = tc_dicts
            session["traceability_data"] = trace
            _persist_test_cases(tc_dicts)
            return render_template("test_cases.html",
                                   test_cases=tc_list, traceability=trace,
                                   has_data=True, errors=errors,
                                   resource_urls=extract_resource_urls())

        # GET — first drain any background job whose result
        # arrived after the previous sync POST already returned. This
        # is the bug operators reported as "I added a URL + file, the
        # spinner finished, but the page is empty": the job was still
        # running when the 60 s sync budget expired, redirected with a
        # flash, and nothing else moved the result into the session.
        _drain_tc_job_into_session()
        tc_data = session.get("test_cases_data", [])
        trace_data = session.get("traceability_data", [])
        if tc_data:
            tc_list = reconstruct_test_cases(tc_data)
            return render_template("test_cases.html",
                                   test_cases=tc_list, traceability=trace_data,
                                   has_data=True, errors=[],
                                   resource_urls=extract_resource_urls())

        return render_template("test_cases.html",
                               test_cases=[], traceability=[],
                               has_data=False, errors=[], resource_urls=[])

    @app.route("/checklist", methods=["GET", "POST"])
    def checklist_page():
        if request.method == "POST":
            # Same async-via-JobQueue shim as /test-cases above.
            raw_lines, errors, custom_prompt = parse_page_input()

            if not raw_lines:
                flash(g.t.get("mvp_no_input",
                              "Please enter requirements or upload files."), "error")
                return render_template("checklist.html",
                                       checklist=[], has_data=False, errors=errors)

            sync_pid = ensure_active_project()
            sync_raw_lines = raw_lines
            sync_custom_prompt = custom_prompt

            def _sync_worker(raw_lines=sync_raw_lines,
                             custom_prompt=sync_custom_prompt,
                             pid=sync_pid):
                parsed = split_into_requirements(raw_lines)
                parsed = [r for r in parsed if not is_instruction(r.text)]
                raw_for_persona = (
                    [{"id": r.id, "text": r.text} for r in parsed]
                    if parsed else
                    [{"id": f"RAW-{i+1}", "text": line}
                     for i, line in enumerate(raw_lines) if line.strip()]
                )
                stories = (generate_user_stories(parsed, custom_prompt)
                           if parsed else [])
                cll = generate_checklist(stories, custom_prompt,
                                         raw_requirements=raw_for_persona)
                cld = [cl_to_dict(c) for c in cll]
                if pid and cld:
                    try: _db.save_checklist(pid, cld)
                    except Exception: pass
                return {"cl_dicts": cld,
                        "stories": [story_to_dict(s) for s in stories],
                        "raw_requirements": raw_for_persona}

            sid = get_session_id(session)
            job_id = get_queue().submit("cl_gen", _sync_worker,
                                        meta={"session_id": sid})
            session["cl_gen_job_id"] = job_id

            import time as _time
            deadline = _time.time() + 60
            job = None
            while _time.time() < deadline:
                job = get_queue().get(job_id)
                if job is None or job.status in (DONE, FAILED):
                    break
                _time.sleep(0.5)

            if job is None or job.status != DONE:
                flash(
                    g.t.get(
                        "mvp_gen_in_background",
                        "Generation is still running in the background. "
                        "Please wait a few seconds and refresh this page."
                    ),
                    "info",
                )
                return redirect(url_for("checklist_page"))

            r = job.result or {}
            session["checklist_data"] = r.get("cl_dicts", [])
            session["user_stories"] = r.get("stories", [])
            session["raw_requirements"] = r.get("raw_requirements", [])

            cl_list = reconstruct_checklist(session["checklist_data"])
            if not cl_list:
                flash(g.t.get(
                    "mvp_no_quality_requirements",
                    "Could not detect any testable requirements in the provided input."),
                    "error")
                return render_template("checklist.html",
                                       checklist=[], has_data=False, errors=errors)
            return render_template("checklist.html", checklist=cl_list,
                                   has_data=True, errors=errors,
                                   resource_urls=extract_resource_urls())

            # Legacy code path retained below; replaced above.
            parsed_reqs = split_into_requirements(raw_lines)
            parsed_reqs = [r for r in parsed_reqs if not is_instruction(r.text)]

            raw_reqs_for_persona = ([{"id": r.id, "text": r.text} for r in parsed_reqs]
                                    if parsed_reqs
                                    else [{"id": f"RAW-{i+1}", "text": line}
                                          for i, line in enumerate(raw_lines) if line.strip()])

            if parsed_reqs:
                new_stories = generate_user_stories(parsed_reqs, custom_prompt)
                session["user_stories"] = [story_to_dict(s) for s in new_stories]
                session["raw_requirements"] = [{"id": r.id, "text": r.text} for r in parsed_reqs]
            else:
                new_stories = []
                session["user_stories"] = []
                session["raw_requirements"] = raw_reqs_for_persona

            cl_list = generate_checklist(new_stories, custom_prompt,
                                         raw_requirements=raw_reqs_for_persona)

            if not cl_list:
                flash(g.t.get("mvp_no_quality_requirements",
                              "Could not detect any testable requirements in the provided input."),
                      "error")
                return render_template("checklist.html",
                                       checklist=[], has_data=False, errors=errors)

            cl_dicts = [cl_to_dict(cl) for cl in cl_list]
            session["checklist_data"] = cl_dicts
            _persist_checklist(cl_dicts)
            return render_template("checklist.html", checklist=cl_list,
                                   has_data=True, errors=errors,
                                   resource_urls=extract_resource_urls())

        # GET — drain any background checklist job that finished
        # after the sync POST returned. Same bug as the TC path.
        _drain_cl_job_into_session()
        cl_data = session.get("checklist_data", [])
        if cl_data:
            cl_list = reconstruct_checklist(cl_data)
            return render_template("checklist.html",
                                   checklist=cl_list, has_data=True, errors=[],
                                   resource_urls=extract_resource_urls())

        return render_template("checklist.html", checklist=[], has_data=False,
                               errors=[], resource_urls=[])

    # ── Upload existing TC / CL packs ──────────────────────────────
    # Lets a tester import a previously-built test pack so it can be
    # run via /test-execution (manual) or /automation (Playwright).
    # Format is inferred from the uploaded filename's extension.
    _UPLOAD_EXTS = {"xlsx", "csv", "md", "markdown", "json"}

    def _save_upload(file_storage) -> tuple[str, str] | tuple[None, str]:
        """Persist the upload to a temp file. Returns (path, filename)
        or (None, error_message)."""
        if not file_storage or not file_storage.filename:
            return (None, g.t.get("upload_no_file", "No file selected."))
        filename = secure_filename(file_storage.filename)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in _UPLOAD_EXTS:
            return (None, g.t.get(
                "upload_bad_ext",
                f"Unsupported file type ‘{ext}’. Use one of: xlsx, csv, md, json."))
        # Use a tempfile under UPLOAD_FOLDER so the existing 64 MB cap
        # and write-permission probes apply uniformly.
        upload_dir = app.config.get("UPLOAD_FOLDER") or tempfile.gettempdir()
        os.makedirs(upload_dir, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="tc_import_", suffix=f"_{filename}",
                                    dir=upload_dir)
        try:
            with os.fdopen(fd, "wb") as out:
                file_storage.save(out)
        except Exception as exc:
            return (None, f"Could not save the uploaded file: {exc}")
        return (path, filename)

    @app.route("/test-cases/upload", methods=["POST"])
    def test_cases_upload():
        path, filename = _save_upload(request.files.get("upload_file"))
        if not path:
            flash(filename, "error")
            return redirect(_back_to_caller(default="test_cases_page"))
        try:
            cases = import_parse_test_cases(path, filename)
        except Exception as exc:
            flash(f"Import failed: {exc}", "error")
            return redirect(_back_to_caller(default="test_cases_page"))
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        if not cases:
            flash(g.t.get(
                "upload_no_rows",
                "No test cases were recognised in the file. Check that it has a "
                "header row and at least an ‘ID’ + ‘Summary’ + ‘Steps’ column."),
                "error")
            return redirect(_back_to_caller(default="test_cases_page"))

        mode = (request.form.get("upload_mode") or "replace").lower()
        existing = session.get("test_cases_data", []) if mode == "append" else []
        merged = existing + [tc_to_dict(tc) for tc in cases]
        session["test_cases_data"] = merged
        _persist_test_cases(merged)
        # Imported packs don't carry their own user stories, so reset
        # the traceability matrix — it would otherwise reference IDs
        # that no longer exist.
        session.pop("traceability_data", None)

        flash(
            g.t.get("upload_tc_ok",
                    f"Imported {len(cases)} test case(s) from {filename}.")
            + (f" Total now: {len(merged)}." if mode == "append" else ""),
            "success",
        )
        # Stay on whatever page the form was POSTed from. The same
        # upload form lives on /test-cases AND on /test-execution; the
        # operator who hit Upload from the execution page expects to
        # land back there, not on the generation page. Run is then
        # triggered by the user clicking the Run button — uniform
        # behaviour with the generation flow.
        return redirect(_back_to_caller(default="test_cases_page"))

    # ── Async generation pipeline ────────────────────────────────
    # The sync /test-cases and /checklist POST handlers can block for
    # 30–90 s on a busy LLM, which surfaces in the UI as a frozen page.
    # The pair below splits that into:
    #   POST /test-cases/run-async    — submits the job, returns
    #                                   {"job_id": ..., "status": "pending"}
    #   GET  /test-cases/status/<id>  — polled by the modal; reports
    #                                   pending / running / done / failed,
    #                                   and on done writes the result back
    #                                   into session so a redirect to
    #                                   /test-cases renders normally.
    # Same pair exists for /checklist further below.
    @app.route("/test-cases/run-async", methods=["POST"])
    def test_cases_run_async():
        raw_lines, errors, custom_prompt = parse_page_input()
        if not raw_lines:
            return jsonify({"error": "no_input",
                            "message": g.t.get("mvp_no_input",
                                "Please enter requirements or upload files.")}), 400

        sid = get_session_id(session)
        active = get_queue().count_active_by_meta(
            "tc_gen", "session_id", sid)
        if active >= MAX_CONCURRENT_GEN_JOBS:
            resp = jsonify({
                "error": "rate_limited",
                "message": (f"You already have {active} active generation "
                            f"jobs. Wait for them to finish before starting "
                            f"another."),
                "active": active,
                "limit": MAX_CONCURRENT_GEN_JOBS,
            })
            resp.status_code = 429
            resp.headers["Retry-After"] = "20"
            return resp

        # Resolve the active project id NOW, while we still hold a request
        # context (and therefore a real session). The worker thread runs
        # without any request context and cannot touch ``session`` to
        # auto-create a project — so persistence has to be done with the
        # pid we resolve here. Falsy result is fine: persistence becomes a
        # no-op and the in-session result still lights up the page.
        pid = ensure_active_project()

        def _worker(raw_lines=raw_lines, custom_prompt=custom_prompt, pid=pid):
            parsed_reqs = split_into_requirements(raw_lines)
            parsed_reqs = [r for r in parsed_reqs if not is_instruction(r.text)]
            raw_reqs_for_persona = (
                [{"id": r.id, "text": r.text} for r in parsed_reqs]
                if parsed_reqs else
                [{"id": f"RAW-{i+1}", "text": line}
                 for i, line in enumerate(raw_lines) if line.strip()]
            )
            new_stories = (generate_user_stories(parsed_reqs, custom_prompt)
                           if parsed_reqs else [])
            tc_list = generate_test_cases(new_stories, custom_prompt,
                                          raw_requirements=raw_reqs_for_persona)
            trace = generate_traceability(new_stories, tc_list) if tc_list else []
            tc_dicts = [tc_to_dict(tc) for tc in tc_list]
            # Persist INSIDE the worker so the polling /status endpoint
            # never has to do a DB round-trip. On free-tier Postgres a
            # cold connection can take 1–2 s and was visibly stalling the
            # poll (browser caps at 6 concurrent requests per origin —
            # one slow /status hangs the modal forever once the cap is
            # hit). Best-effort: a DB outage must not hide the result.
            if pid and tc_dicts:
                try:
                    _db.save_test_cases(pid, tc_dicts)
                except Exception as exc:  # pragma: no cover
                    _log.warning("tc_gen worker persist: %s", exc)
            return {
                "tc_dicts": tc_dicts,
                "stories": [story_to_dict(s) for s in new_stories],
                "raw_requirements": raw_reqs_for_persona,
                "trace": trace,
            }

        job_id = get_queue().submit(
            "tc_gen", _worker, meta={"session_id": sid})
        session["tc_gen_job_id"] = job_id
        return jsonify({"job_id": job_id, "status": "pending"})

    @app.route("/test-cases/status/<job_id>", methods=["GET"])
    def test_cases_status(job_id):
        # Polling endpoint — must stay cheap and never block. The browser
        # caps concurrent connections to 6 per origin, so a single slow
        # /status response can stall the entire modal. The worker has
        # already persisted to Postgres before reaching DONE; here we
        # only mirror its result into the session.
        job = get_queue().get(job_id)
        if job is None or job.kind != "tc_gen":
            return jsonify({"error": "not_found"}), 404
        payload = job.to_public_dict()
        if job.status == DONE and job.result:
            r = job.result
            session["test_cases_data"] = r.get("tc_dicts", [])
            session["user_stories"] = r.get("stories", [])
            session["raw_requirements"] = r.get("raw_requirements", [])
            session["traceability_data"] = r.get("trace", [])
            # Tell the client where to send the user once it sees DONE.
            # Surfacing the URL in the payload (instead of hard-coding it
            # in the template) means the same /status JSON is enough to
            # drive a redirect even when the page is reopened in another
            # tab and the original template hash is gone.
            payload["redirect_url"] = url_for("test_cases_page")
        return jsonify(payload)

    @app.route("/checklist/run-async", methods=["POST"])
    def checklist_run_async():
        raw_lines, errors, custom_prompt = parse_page_input()
        if not raw_lines:
            return jsonify({"error": "no_input",
                            "message": g.t.get("mvp_no_input",
                                "Please enter requirements or upload files.")}), 400

        sid = get_session_id(session)
        active = get_queue().count_active_by_meta(
            "cl_gen", "session_id", sid)
        if active >= MAX_CONCURRENT_GEN_JOBS:
            resp = jsonify({
                "error": "rate_limited",
                "message": (f"You already have {active} active generation "
                            f"jobs. Wait for them to finish before starting "
                            f"another."),
                "active": active,
                "limit": MAX_CONCURRENT_GEN_JOBS,
            })
            resp.status_code = 429
            resp.headers["Retry-After"] = "20"
            return resp

        # Same rationale as /test-cases/run-async: resolve pid here so the
        # worker can persist without needing a request context, and the
        # /status endpoint never has to make a DB round-trip.
        pid = ensure_active_project()

        def _worker(raw_lines=raw_lines, custom_prompt=custom_prompt, pid=pid):
            parsed_reqs = split_into_requirements(raw_lines)
            parsed_reqs = [r for r in parsed_reqs if not is_instruction(r.text)]
            raw_reqs_for_persona = (
                [{"id": r.id, "text": r.text} for r in parsed_reqs]
                if parsed_reqs else
                [{"id": f"RAW-{i+1}", "text": line}
                 for i, line in enumerate(raw_lines) if line.strip()]
            )
            new_stories = (generate_user_stories(parsed_reqs, custom_prompt)
                           if parsed_reqs else [])
            cl_list = generate_checklist(new_stories, custom_prompt,
                                         raw_requirements=raw_reqs_for_persona)
            cl_dicts = [cl_to_dict(c) for c in cl_list]
            if pid and cl_dicts:
                try:
                    _db.save_checklist(pid, cl_dicts)
                except Exception as exc:  # pragma: no cover
                    _log.warning("cl_gen worker persist: %s", exc)
            return {
                "cl_dicts": cl_dicts,
                "stories": [story_to_dict(s) for s in new_stories],
                "raw_requirements": raw_reqs_for_persona,
            }

        job_id = get_queue().submit(
            "cl_gen", _worker, meta={"session_id": sid})
        session["cl_gen_job_id"] = job_id
        return jsonify({"job_id": job_id, "status": "pending"})

    @app.route("/checklist/status/<job_id>", methods=["GET"])
    def checklist_status(job_id):
        # Polling endpoint — see /test-cases/status for rationale.
        job = get_queue().get(job_id)
        if job is None or job.kind != "cl_gen":
            return jsonify({"error": "not_found"}), 404
        payload = job.to_public_dict()
        if job.status == DONE and job.result:
            r = job.result
            session["checklist_data"] = r.get("cl_dicts", [])
            session["user_stories"] = r.get("stories", [])
            session["raw_requirements"] = r.get("raw_requirements", [])
            payload["redirect_url"] = url_for("checklist_page")
        return jsonify(payload)

    @app.route("/checklist/upload", methods=["POST"])
    def checklist_upload():
        path, filename = _save_upload(request.files.get("upload_file"))
        if not path:
            flash(filename, "error")
            return redirect(_back_to_caller(default="checklist_page"))
        try:
            items = import_parse_checklist(path, filename)
        except Exception as exc:
            flash(f"Import failed: {exc}", "error")
            return redirect(_back_to_caller(default="checklist_page"))
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        if not items:
            flash(g.t.get(
                "upload_no_rows_cl",
                "No checklist items were recognised in the file. Check that it "
                "has a header row and at least an ‘Objective’ column."),
                "error")
            return redirect(_back_to_caller(default="checklist_page"))

        mode = (request.form.get("upload_mode") or "replace").lower()
        existing = session.get("checklist_data", []) if mode == "append" else []
        merged = existing + [cl_to_dict(it) for it in items]
        session["checklist_data"] = merged
        _persist_checklist(merged)

        flash(
            g.t.get("upload_cl_ok",
                    f"Imported {len(items)} checklist item(s) from {filename}.")
            + (f" Total now: {len(merged)}." if mode == "append" else ""),
            "success",
        )
        return redirect(_back_to_caller(default="checklist_page"))

    @app.route("/export/<fmt>")
    @app.route("/export/<fmt>")
    def export(fmt):
        stories = reconstruct_stories(session.get("user_stories", []))
        tc_list = reconstruct_test_cases(session.get("test_cases_data", []))
        cl_list = reconstruct_checklist(session.get("checklist_data", []))

        if stories and not tc_list:
            tc_list = generate_test_cases(stories)
        if stories and not cl_list:
            cl_list = generate_checklist(stories)

        trace = session.get("traceability_data", [])
        if not trace and stories and tc_list:
            trace = generate_traceability(stories, tc_list)

        name = session.get("project_setup", {}).get("project_name", "project").replace(" ", "_")

        if fmt == "markdown":
            content = export_markdown(name, stories, tc_list, cl_list, trace, {})
            return Response(content, mimetype="text/markdown",
                            headers={"Content-Disposition": f"attachment; filename=testfortge_{name}.md"})
        if fmt == "html":
            content = export_html(name, stories, tc_list, cl_list, trace, {})
            return Response(content, mimetype="text/html",
                            headers={"Content-Disposition": f"attachment; filename=testfortge_{name}.html"})
        if fmt == "csv-testcases":
            content = export_csv_testcases(tc_list)
            return Response(content, mimetype="text/csv",
                            headers={"Content-Disposition": "attachment; filename=test_cases.csv"})
        if fmt == "csv-checklist":
            content = export_csv_checklist(cl_list)
            return Response(content, mimetype="text/csv",
                            headers={"Content-Disposition": "attachment; filename=checklist.csv"})
        if fmt == "xlsx-testcases":
            content = export_xlsx_testcases(tc_list)
            return Response(
                content,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": "attachment; filename=test_cases.xlsx"})
        if fmt == "xlsx-checklist":
            content = export_xlsx_checklist(cl_list)
            return Response(
                content,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": "attachment; filename=checklist.xlsx"})
        return "Unknown format", 400


__all__ = ["register"]

