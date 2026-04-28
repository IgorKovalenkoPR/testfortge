"""TestFortge — Test-case / checklist generation + export routes.

  * GET/POST /test-cases   — generate + display test cases
  * GET/POST /checklist    — generate + display checklist
  * GET /export/<fmt>      — markdown/html/csv/xlsx export
"""

from __future__ import annotations

import os
import tempfile

from flask import Flask, Response, flash, g, redirect, render_template, request, session, url_for
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

from ._shared import (
    reconstruct_stories, reconstruct_test_cases, reconstruct_checklist,
    tc_to_dict, cl_to_dict, story_to_dict,
    parse_page_input, extract_resource_urls,
)


def register(app: Flask) -> None:
    @app.route("/test-cases", methods=["GET", "POST"])
    def test_cases_page():
        if request.method == "POST":
            raw_lines, errors, custom_prompt = parse_page_input()

            if not raw_lines:
                flash(g.t.get("mvp_no_input",
                              "Please enter requirements or upload files."), "error")
                return render_template("test_cases.html",
                                       test_cases=[], traceability=[],
                                       has_data=False, errors=errors)

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
            session["test_cases_data"] = [tc_to_dict(tc) for tc in tc_list]
            session["traceability_data"] = trace
            return render_template("test_cases.html",
                                   test_cases=tc_list, traceability=trace,
                                   has_data=True, errors=errors,
                                   resource_urls=extract_resource_urls())

        # GET: show session data if it exists.
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
            raw_lines, errors, custom_prompt = parse_page_input()

            if not raw_lines:
                flash(g.t.get("mvp_no_input",
                              "Please enter requirements or upload files."), "error")
                return render_template("checklist.html",
                                       checklist=[], has_data=False, errors=errors)

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

            session["checklist_data"] = [cl_to_dict(cl) for cl in cl_list]
            return render_template("checklist.html", checklist=cl_list,
                                   has_data=True, errors=errors,
                                   resource_urls=extract_resource_urls())

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
            return redirect(url_for("test_cases_page"))
        try:
            cases = import_parse_test_cases(path, filename)
        except Exception as exc:
            flash(f"Import failed: {exc}", "error")
            return redirect(url_for("test_cases_page"))
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
            return redirect(url_for("test_cases_page"))

        mode = (request.form.get("upload_mode") or "replace").lower()
        existing = session.get("test_cases_data", []) if mode == "append" else []
        merged = existing + [tc_to_dict(tc) for tc in cases]
        session["test_cases_data"] = merged
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
        return redirect(url_for("test_cases_page"))

    @app.route("/checklist/upload", methods=["POST"])
    def checklist_upload():
        path, filename = _save_upload(request.files.get("upload_file"))
        if not path:
            flash(filename, "error")
            return redirect(url_for("checklist_page"))
        try:
            items = import_parse_checklist(path, filename)
        except Exception as exc:
            flash(f"Import failed: {exc}", "error")
            return redirect(url_for("checklist_page"))
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
            return redirect(url_for("checklist_page"))

        mode = (request.form.get("upload_mode") or "replace").lower()
        existing = session.get("checklist_data", []) if mode == "append" else []
        merged = existing + [cl_to_dict(it) for it in items]
        session["checklist_data"] = merged

        flash(
            g.t.get("upload_cl_ok",
                    f"Imported {len(items)} checklist item(s) from {filename}.")
            + (f" Total now: {len(merged)}." if mode == "append" else ""),
            "success",
        )
        return redirect(url_for("checklist_page"))

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
