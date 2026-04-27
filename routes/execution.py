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
    WEB_PLATFORMS, WEB_BROWSERS, MOBILE_WEB_OSES, MOBILE_WEB_BROWSERS,
    MOBILE_RESOLUTIONS, IOS_DEVICES, ANDROID_DEVICES,
    get_tester, execute_items,
)
from engine.bug_report import (
    BugReport, BUG_SEVERITIES, BUG_PRIORITIES, BUG_STATUSES, BUG_FREQUENCIES,
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
            # The Testing Types / Assigned Tester / Test Account UI was
            # removed — testing scope is now driven by the prompt that
            # produced the test cases (see Test Cases / Checklist pages).
            # We keep accepting the fields if posted (older bookmarks /
            # automation), but otherwise default sensibly.
            tester_id = request.form.get("tester_id", "mid_1")
            testing_types = request.form.getlist("testing_types") or ["Regression"]
            selected_ids = request.form.getlist("selected_items")

            credentials = credentials_from_form(request.form)
            session["test_execution_credentials"] = credentials_to_session(credentials)

            # Harvest per-item manual overrides submitted alongside the
            # selection checkboxes: status_<item_id> and bug_<item_id>.
            # Anything at the default ("auto" / empty) is ignored so the
            # runner falls back to real-site checks or the simulator.
            manual_statuses: dict[str, str] = {}
            manual_bug_refs: dict[str, str] = {}
            for field, raw in request.form.items():
                if field.startswith("status_"):
                    item_id = field[len("status_"):]
                    val = (raw or "").strip()
                    if val in ("Passed", "Failed", "Blocked"):
                        manual_statuses[item_id] = val
                elif field.startswith("bug_"):
                    item_id = field[len("bug_"):]
                    val = (raw or "").strip()
                    if val:
                        manual_bug_refs[item_id] = val

            # Resolve environment — one run targets exactly one
            # environment kind. The UI shows only that kind's inputs;
            # we read just those and build a human-readable string.
            env_type = (request.form.get("env_type") or "web").strip().lower()

            def _resolve_custom(val: str, custom_field: str, default: str) -> str:
                if val == "__custom":
                    return (request.form.get(custom_field, "") or "").strip() or default
                return val or default

            if env_type == "mobile_web":
                os_name = request.form.get("mw_os", "iOS").strip() or "iOS"
                browser = request.form.get("mw_browser", "Chrome").strip() or "Chrome"
                resolution = _resolve_custom(
                    request.form.get("mw_resolution", "375x812"),
                    "mw_resolution_custom", "375x812",
                )
                version = (request.form.get("mw_version", "") or "").strip()
                bits = [f"Mobile Web · {os_name}", browser, resolution]
                if version:
                    bits.append(f"OS {version}")
                environment = " / ".join(bits)
            elif env_type == "ios":
                device = _resolve_custom(
                    request.form.get("ios_device", "iPhone 15"),
                    "ios_device_custom", "iPhone",
                )
                version = (request.form.get("ios_version", "") or "").strip()
                build = (request.form.get("ios_build", "") or "").strip()
                bits = ["iOS", device]
                if version:
                    bits.append(f"iOS {version}")
                if build:
                    bits.append(f"build {build}")
                environment = " / ".join(bits)
            elif env_type == "android":
                device = _resolve_custom(
                    request.form.get("android_device", "Pixel 8"),
                    "android_device_custom", "Android device",
                )
                version = (request.form.get("android_version", "") or "").strip()
                build = (request.form.get("android_build", "") or "").strip()
                bits = ["Android", device]
                if version:
                    bits.append(f"Android {version}")
                if build:
                    bits.append(f"build {build}")
                environment = " / ".join(bits)
            else:
                # Web (default)
                env_type = "web"
                platform = request.form.get("web_platform", "Windows").strip() or "Windows"
                browser = request.form.get("web_browser", "Chrome").strip() or "Chrome"
                version = (request.form.get("web_version", "") or "").strip()
                bits = [f"Web · {platform}", browser]
                if version:
                    bits.append(version)
                environment = " / ".join(bits)

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
                manual_statuses=manual_statuses or None,
                manual_bug_refs=manual_bug_refs or None,
            )

            # Assign real bug IDs to new bugs and stamp ``affects_version``
            # from the saved project setup so every defect carries the
            # full ISTQB-mandatory metadata. Engine-side we left
            # ``affects_version`` blank because the engine has no
            # awareness of Flask sessions; we fill it here.
            existing_bugs = [dict_to_bug(b) for b in session.get("bug_reports_data", [])]
            all_bugs = session.get("bug_reports_data", [])
            project_setup = session.get("project_setup", {}) or {}
            affects_version = (
                project_setup.get("project_version")
                or project_setup.get("version")
                or project_setup.get("project_name")
                or "Unspecified"
            )
            bug_id_map: dict[str, str] = {}
            for bug_dict in execution["bugs"]:
                new_id = generate_bug_id(
                    existing_bugs + [dict_to_bug(b) for b in all_bugs[len(existing_bugs):]]
                )
                bug_dict["id"] = new_id
                if not bug_dict.get("affects_version"):
                    bug_dict["affects_version"] = affects_version
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
                "site_url": site_url,
                "created_at": datetime.now().isoformat(),
            }
            test_runs.append(run_record)
            session["test_runs"] = test_runs

            # Build a detailed, honest flash so the tester knows exactly
            # what just happened: how many checks were real HTTP probes
            # against the resource URL versus deterministic simulation.
            stats = execution["stats"]
            sources = stats.get("sources", {})
            real_n = sources.get("real_check", 0)
            sim_n = sources.get("simulated", 0)
            man_n = sources.get("manual", 0)
            bug_n = len(execution["bugs"])
            parts = [
                g.t.get("te_results_saved",
                        "Test execution results saved successfully") + ".",
                f"{stats['passed']} Passed / {stats['failed']} Failed / "
                f"{stats['blocked']} Blocked ({stats['pass_rate']}% pass rate).",
            ]
            if site_url and real_n:
                parts.append(
                    f"{real_n} item(s) auto-checked against {site_url}; "
                    f"{sim_n} simulated; {man_n} manual."
                )
            elif site_url and not real_n:
                parts.append(
                    f"No checks could be matched to {site_url}; "
                    f"{sim_n} simulated; {man_n} manual."
                )
            else:
                parts.append(
                    "No resource URL configured — results are deterministic "
                    "simulations. Add a URL on the Requirements page to "
                    "enable real HTTP/HTML checks."
                )
            if bug_n:
                parts.append(
                    f"{bug_n} bug report(s) auto-created for Failed/Blocked "
                    f"items — see the Bug Reports page."
                )
            flash(" ".join(parts), "success")
            return redirect(url_for("test_execution_page"))

        cred = credentials_from_session(session.get("test_execution_credentials"))
        existing_bug_ids = [
            b.get("id") for b in session.get("bug_reports_data", []) if b.get("id")
        ]
        return render_template("test_execution.html",
                               has_tc_data=has_tc, has_cl_data=has_cl,
                               tc_count=len(tc_data), cl_count=len(cl_data),
                               tc_items=tc_data, cl_items=cl_data,
                               test_runs=test_runs,
                               resource_urls=resource_urls,
                               existing_bug_ids=existing_bug_ids,
                               platforms=PLATFORMS, browsers=BROWSERS,
                               devices=DEVICES, mobile_web=MOBILE_WEB,
                               screen_sizes=SCREEN_SIZES,
                               # Per-env-kind option pools for the new
                               # 4-tab Test Environment selector.
                               web_platforms=WEB_PLATFORMS,
                               web_browsers=WEB_BROWSERS,
                               mobile_web_oses=MOBILE_WEB_OSES,
                               mobile_web_browsers=MOBILE_WEB_BROWSERS,
                               mobile_resolutions=MOBILE_RESOLUTIONS,
                               ios_devices=IOS_DEVICES,
                               android_devices=ANDROID_DEVICES,
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
                               statuses=BUG_STATUSES, frequencies=BUG_FREQUENCIES)

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
