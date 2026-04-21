"""TestFortge — Manual test execution + bug report routes.

  * GET/POST /test-execution                 — configure and run test items
  * POST     /test-execution/generate-account — throw-away test account
  * POST     /create-bug-report              — create a bug entry
  * GET      /bug-reports                    — list and manage bugs
  * GET      /export-bug-reports             — markdown export
"""

from __future__ import annotations

from datetime import datetime

from flask import (Flask, Response, flash, g, redirect, render_template,
                   request, session, url_for)

from engine.log import get_logger
from engine.qa_testers import (
    TESTERS, PLATFORMS, BROWSERS, DEVICES, MOBILE_WEB,
    SCREEN_SIZES, TESTING_TYPES,
    get_tester, execute_items,
)
from engine.bug_report import (
    BugReport, BUG_SEVERITIES, BUG_PRIORITIES, BUG_STATUSES,
    generate_bug_id, bug_to_dict, dict_to_bug,
    export_bug_report_markdown,
)
from engine.test_credentials import (
    credentials_from_form, credentials_from_session, credentials_to_session,
    generate_test_account,
)

from ._shared import extract_resource_urls

log = get_logger(__name__)


def register(app: Flask) -> None:
    @app.route("/test-execution", methods=["GET", "POST"])
    def test_execution_page():
        tc_data = session.get("test_cases_data", [])
        cl_data = session.get("checklist_data", [])
        has_tc = bool(tc_data)
        has_cl = bool(cl_data)
        resource_urls = extract_resource_urls()

        test_runs = session.get("test_runs", [])

        if request.method == "POST":
            source = request.form.get("source", "test_cases")
            tester_id = request.form.get("tester_id", "mid_1")
            testing_types = request.form.getlist("testing_types") or ["Functional"]
            selected_ids = request.form.getlist("selected_items")

            credentials = credentials_from_form(request.form)
            session["test_execution_credentials"] = credentials_to_session(credentials)

            # Resolve environment — "__custom" selects the corresponding
            # free-text input, otherwise use the dropdown value.
            platform = request.form.get("platform_custom") or request.form.get("platform", "Windows")
            browser = request.form.get("browser_custom") or request.form.get("browser", "Chrome")
            device = request.form.get("device_custom") or request.form.get("device", "Desktop")
            screen = request.form.get("screen_custom") or request.form.get("screen_size", "1920x1080")
            if platform == "__custom":
                platform = request.form.get("platform_custom", "Windows")
            if browser == "__custom":
                browser = request.form.get("browser_custom", "Chrome")
            if device == "__custom":
                device = request.form.get("device_custom", "Desktop")
            if screen == "__custom":
                screen = request.form.get("screen_custom", "1920x1080")

            environment = f"{platform} / {browser} / {device} / {screen}"

            items_data = tc_data if source == "test_cases" else cl_data
            item_type = "test_case" if source == "test_cases" else "checklist"

            # First http URL wins — used for real-site testing when provided.
            site_url = ""
            for url in resource_urls:
                if url.startswith("http"):
                    site_url = url
                    break

            execution = execute_items(
                items=items_data,
                item_type=item_type,
                tester_id=tester_id,
                environment=environment,
                testing_types=testing_types,
                selected_ids=selected_ids or None,
                site_url=site_url,
            )

            # Assign real bug IDs to new bugs
            existing_bugs = [dict_to_bug(b) for b in session.get("bug_reports_data", [])]
            all_bugs = session.get("bug_reports_data", [])
            bug_id_map: dict[str, str] = {}
            for bug_dict in execution["bugs"]:
                new_id = generate_bug_id(
                    existing_bugs + [dict_to_bug(b) for b in all_bugs[len(existing_bugs):]]
                )
                bug_dict["id"] = new_id
                linked = bug_dict.get("linked_item_id", "")
                bug_id_map[linked] = new_id
                all_bugs.append(bug_dict)

            for r in execution["results"]:
                if r["bug_id"].startswith("__pending_"):
                    r["bug_id"] = bug_id_map.get(r["item_id"], r["bug_id"])

            session["bug_reports_data"] = all_bugs

            tester = get_tester(tester_id)
            tester_name = tester.name if tester else tester_id
            run_record = {
                "run_id": len(test_runs) + 1,
                "source": source,
                "tester_id": tester_id,
                "tester_name": tester_name,
                "environment": environment,
                "testing_types": ", ".join(testing_types),
                "results": execution["results"],
                "stats": execution["stats"],
                "bug_count": len(execution["bugs"]),
                "created_at": datetime.now().isoformat(),
            }
            test_runs.append(run_record)
            session["test_runs"] = test_runs

            flash(g.t.get("te_results_saved",
                          "Test execution results saved successfully"), "success")
            return redirect(url_for("test_execution_page"))

        cred = credentials_from_session(session.get("test_execution_credentials"))
        return render_template("test_execution.html",
                               has_tc_data=has_tc, has_cl_data=has_cl,
                               tc_count=len(tc_data), cl_count=len(cl_data),
                               tc_items=tc_data, cl_items=cl_data,
                               test_runs=test_runs,
                               resource_urls=resource_urls,
                               platforms=PLATFORMS, browsers=BROWSERS,
                               devices=DEVICES, mobile_web=MOBILE_WEB,
                               screen_sizes=SCREEN_SIZES,
                               testing_types=TESTING_TYPES, testers=TESTERS,
                               cred=cred.as_public_dict())

    @app.route("/test-execution/generate-account", methods=["POST"])
    def test_execution_generate_account():
        """Generate a throw-away test account for the Test Execution module."""
        resource_urls = extract_resource_urls()
        base_url = ""
        for url in resource_urls:
            if url.startswith("http"):
                base_url = url
                break
        register_url = request.form.get("cred_register_url", "").strip()
        login_url = request.form.get("cred_login_url", "").strip()
        domain = "testfortge.test"
        if base_url:
            try:
                from urllib.parse import urlparse
                domain = urlparse(base_url).netloc or domain
            except Exception as exc:
                log.debug("domain parse failed: %s", exc)
        cred = generate_test_account(base_domain=domain,
                                     register_url=register_url,
                                     login_url=login_url)
        session["test_execution_credentials"] = credentials_to_session(cred)
        flash(f"Generated test account: {cred.username}", "success")
        return redirect(url_for("test_execution_page"))

    @app.route("/create-bug-report", methods=["POST"])
    def create_bug_report():
        bugs = session.get("bug_reports_data", [])
        existing = [dict_to_bug(b) for b in bugs]
        new_id = generate_bug_id(existing)

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
            linked_item_id=request.form.get("linked_item_id", ""),
            linked_item_type=request.form.get("linked_item_type", ""),
            reporter=request.form.get("reporter", ""),
            component=request.form.get("component", ""),
            created_at=datetime.now().isoformat(),
        )

        bugs.append(bug_to_dict(bug))
        session["bug_reports_data"] = bugs

        flash(g.t.get("bug_saved", "Bug report created successfully") + f" ({new_id})",
              "success")
        return redirect(url_for("bug_reports_page"))

    @app.route("/bug-reports")
    def bug_reports_page():
        bugs_data = session.get("bug_reports_data", [])
        bugs = [dict_to_bug(b) for b in bugs_data]

        stats = {
            "total": len(bugs),
            "open": sum(1 for b in bugs if b.status == "Open"),
            "critical": sum(1 for b in bugs if b.severity == "Critical"),
            "major": sum(1 for b in bugs if b.severity == "Major"),
        }

        return render_template("bug_reports.html", bugs=bugs, stats=stats,
                               severities=BUG_SEVERITIES, priorities=BUG_PRIORITIES,
                               statuses=BUG_STATUSES)

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


__all__ = ["register"]
