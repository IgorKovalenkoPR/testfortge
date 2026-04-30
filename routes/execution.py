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

from engine import db as _db

from ._shared import extract_resource_urls, ensure_active_project

log = get_logger(__name__)


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
    @app.route("/test-execution", methods=["GET", "POST"])
    def test_execution_page():
        tc_data = session.get("test_cases_data", [])
        cl_data = session.get("checklist_data", [])
        has_tc = bool(tc_data)
        has_cl = bool(cl_data)
        resource_urls = extract_resource_urls()

        test_runs = session.get("test_runs", [])

        if request.method == "POST":
          try:
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

              # ── Resolve selected environments (multi-checkbox) ─────
              # User may pick one or more env types — each selected env
              # produces its own run record so testers can compare side-
              # by-side (e.g. Web vs iOS for the same TC pack).
              env_types = [e.strip().lower() for e in
                            request.form.getlist("env_type") if e.strip()]
              if not env_types:
                  env_types = ["web"]  # safety default

              def _resolve_custom(val: str, custom_field: str, default: str) -> str:
                  if val == "__custom":
                      return (request.form.get(custom_field, "") or "").strip() or default
                  return val or default

              def _build_env_string(et: str) -> str:
                  if et == "mobile_web":
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
                      return " / ".join(bits)
                  if et == "ios":
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
                      return " / ".join(bits)
                  if et == "android":
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
                      return " / ".join(bits)
                  # Web (default)
                  platform = request.form.get("web_platform", "Windows").strip() or "Windows"
                  browser = request.form.get("web_browser", "Chrome").strip() or "Chrome"
                  version = (request.form.get("web_version", "") or "").strip()
                  bits = [f"Web · {platform}", browser]
                  if version:
                      bits.append(version)
                  return " / ".join(bits)

              items_data = tc_data if source == "test_cases" else cl_data
              item_type = "test_case" if source == "test_cases" else "checklist"

              # ── Automation configuration (folded in from Automation QA) ──
              # When the tester supplies a Base URL, web / mobile-web envs
              # additionally drive a Playwright session — capturing
              # screenshots, optional video, and live-watching the run when
              # ``headless=No`` is chosen. iOS / Android native fall back
              # to the deterministic simulator.
              base_url = (request.form.get("base_url") or "").strip()
              scope = (request.form.get("scope") or "all").strip().lower()
              headless = request.form.get("headless", "1") == "1"
              record_video = request.form.get("record_video", "1") == "1"
              session["automation_base_url"] = base_url

              # Pick the URL used by the deterministic site-tester. Prefer
              # the explicit Base URL the user just typed; fall back to
              # the Resource URLs panel if they didn't.
              site_url = base_url
              if not site_url:
                  for url in resource_urls:
                      if url.startswith("http"):
                          site_url = url
                          break

              project_setup = session.get("project_setup", {}) or {}
              affects_version = (
                  project_setup.get("project_version")
                  or project_setup.get("version")
                  or project_setup.get("project_name")
                  or "Unspecified"
              )
              tester = get_tester(tester_id)
              tester_name = tester.name if tester else tester_id
              all_bugs = session.get("bug_reports_data", [])
              existing_bugs = [dict_to_bug(b) for b in all_bugs]

              # Resolve "Re-run failed only" scope by trimming selected_ids
              if scope == "failed":
                  last_run_results = (test_runs[-1]["results"]
                                       if test_runs else [])
                  failed_ids = [r["item_id"] for r in last_run_results
                                if r.get("status") in ("Failed", "Blocked")]
                  if failed_ids:
                      selected_ids = list(set(selected_ids) & set(failed_ids)) \
                                      if selected_ids else failed_ids

              # ── Optional Playwright run (Web / Mobile Web only, when
              #    base_url provided). One automation pass is shared across
              #    web-style envs because Playwright drives the same URL.
              automation_assets: dict[str, dict] = {}
              wants_automation = bool(base_url) and source == "test_cases" and any(
                  et in ("web", "mobile_web") for et in env_types
              )
              if wants_automation:
                  try:
                      from engine.automation_qa import scripts_from_session
                      from engine.automation_runner import AutomationRunner
                      from routes.automation import STORAGE_ROOT
                      scripts = scripts_from_session(items_data, base_url)
                      runner = AutomationRunner(
                          storage_root=STORAGE_ROOT,
                          base_url=base_url,
                          headless=headless,
                          record_video=record_video,
                          credentials=credentials if credentials.is_active() else None,
                      )
                      auto_report = runner.run(scripts)
                      # Index by case ID so the per-env loop below can pull
                      # screenshots / videos / status into the right run row.
                      for r in (auto_report.results or []):
                          cid = getattr(r, "case_id", None) or getattr(r, "id", None)
                          if cid:
                              automation_assets[cid] = {
                                  "status": getattr(r, "status", ""),
                                  "video": getattr(r, "video_path", "") or "",
                                  "screenshots": list(getattr(r, "screenshots", []) or []),
                                  "duration_ms": getattr(r, "duration_ms", 0),
                              }
                      session["automation_report"] = {
                          "passed": auto_report.passed,
                          "failed": auto_report.failed,
                          "blocked": auto_report.blocked,
                          "run_id": auto_report.run_id,
                      }
                  except Exception as exc:
                      log.warning("Automation pass failed (non-fatal): %s", exc)

              # ── Per-environment runs ──
              run_summaries = []
              bug_total = 0
              for et in env_types:
                  environment = _build_env_string(et)

                  # Open a run row in Postgres BEFORE execution so the
                  # auto-generated bugs and per-case results can attach to it.
                  db_run_id = None
                  try:
                      pid = ensure_active_project()
                      if pid:
                          db_run_id = _db.start_execution_run(
                              pid,
                              env_payload={
                                  "env_type": et,
                                  "environment": environment,
                                  "tester_id": tester_id,
                                  "tester_name": tester_name,
                                  "testing_types": testing_types,
                                  "source": source,
                                  "site_url": site_url,
                              },
                              browser_visibility=("headless" if headless else "visible"),
                              record_video=bool(record_video),
                              base_url=base_url,
                          )
                  except Exception as exc:  # pragma: no cover — best-effort
                      log.warning("start_execution_run failed: %s", exc)

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

                  # Promote pending bug IDs and stamp ISTQB metadata.
                  # Single-pass allocator: build the "all bugs so far" list
                  # exactly once, then increment locally — avoids O(N²)
                  # rebuild on every new bug for large runs.
                  bug_id_map: dict[str, str] = {}
                  running_bugs = list(existing_bugs) + [
                      dict_to_bug(b) for b in all_bugs[len(existing_bugs):]
                  ]
                  for bug_dict in execution["bugs"]:
                      new_id = generate_bug_id(running_bugs)
                      bug_dict["id"] = new_id
                      if not bug_dict.get("affects_version"):
                          bug_dict["affects_version"] = affects_version
                      bug_dict["environment"] = environment
                      bug_id_map[bug_dict.get("linked_item_id", "")] = new_id
                      # Mirror to Postgres with execution context attached.
                      _persist_bug(bug_dict, source="execution",
                                   run_id=db_run_id)
                      all_bugs.append(bug_dict)
                      # Cheap appends keep generate_bug_id stable for
                      # subsequent iterations without re-marshalling dicts.
                      try:
                          running_bugs.append(dict_to_bug(bug_dict))
                      except Exception:
                          # If reconstructing fails (rare — schema drift),
                          # rebuild defensively next iteration.
                          running_bugs = list(existing_bugs) + [
                              dict_to_bug(b) for b in all_bugs[len(existing_bugs):]
                          ]
                  for r in execution["results"]:
                      if r["bug_id"].startswith("__pending_"):
                          r["bug_id"] = bug_id_map.get(r["item_id"], r["bug_id"])
                      # Decorate with automation assets when available
                      asset = automation_assets.get(r["item_id"])
                      if asset and et in ("web", "mobile_web"):
                          if asset.get("video"):
                              r["video"] = asset["video"]
                          if asset.get("screenshots"):
                              r["screenshots"] = asset["screenshots"]

                  # Stream per-case results into Postgres.
                  if db_run_id is not None:
                      for r in execution["results"]:
                          try:
                              _db.save_case_result(
                                  db_run_id,
                                  case_external_id=r.get("item_id"),
                                  case_kind=("test_case"
                                             if item_type == "test_cases"
                                             else "checklist_item"),
                                  status=r.get("status"),
                                  evidence_path=(r.get("video")
                                                  or (r.get("screenshots") or [None])[0]),
                                  notes=r.get("comment"),
                              )
                          except Exception as exc:  # pragma: no cover
                              log.warning("save_case_result failed: %s", exc)
                      try:
                          _db.finish_execution_run(
                              db_run_id, status="completed",
                              stats=execution.get("stats") or {},
                          )
                      except Exception as exc:  # pragma: no cover
                          log.warning("finish_execution_run failed: %s", exc)

                  # Session-side run_record stores only the lightweight
                  # bits the dashboard / Bug Reports page needs. Full per-case
                  # results + screenshots / videos already live in
                  # execution_case_result + bug_report tables, so duplicating
                  # them here would only bloat the session pickle (the source
                  # of the 500s we saw on 100+ items).
                  results_summary = [
                      {"item_id": r.get("item_id"),
                       "status":  r.get("status"),
                       "bug_id":  r.get("bug_id"),
                       "duration_ms": r.get("duration_ms")}
                      for r in (execution.get("results") or [])
                  ]
                  run_record = {
                      "run_id": len(test_runs) + 1,
                      "db_run_id": db_run_id,
                      "source": source,
                      "tester_id": tester_id,
                      "tester_name": tester_name,
                      "environment": environment,
                      "env_type": et,
                      "testing_types": ", ".join(testing_types),
                      "results": results_summary,
                      "stats": execution["stats"],
                      "bug_count": len(execution["bugs"]),
                      "site_url": site_url,
                      "base_url": base_url,
                      "headless": headless,
                      "record_video": record_video,
                      "automation_used": (et in ("web", "mobile_web") and wants_automation),
                      "created_at": datetime.now().isoformat(),
                  }
                  test_runs.append(run_record)
                  run_summaries.append((environment, execution["stats"], len(execution["bugs"])))
                  bug_total += len(execution["bugs"])

              # Cap the session-side history at the last 20 runs. Older
              # runs remain queryable through the execution_run table; this
              # keeps the in-memory session payload small enough to write
              # quickly (avoids filesystem-session lock contention when two
              # browser tabs run in parallel).
              test_runs = test_runs[-20:]
              session["bug_reports_data"] = all_bugs
              session["test_runs"] = test_runs

              # Aggregated flash message: one line per env + grand totals
              parts = [g.t.get("te_results_saved",
                                "Test execution results saved successfully") + "."]
              for env_str, stats, bug_n in run_summaries:
                  parts.append(
                      f"[{env_str}] {stats['passed']} P / {stats['failed']} F / "
                      f"{stats['blocked']} B ({stats['pass_rate']}%)"
                      + (f", {bug_n} bug(s)" if bug_n else "")
                      + "."
                  )
              if base_url and wants_automation:
                  parts.append(
                      f"Playwright session ran against {base_url} "
                      f"({'headless' if headless else 'visible'}, "
                      f"{'video on' if record_video else 'no video'})."
                  )
              elif not base_url:
                  parts.append(
                      "No Base URL configured — results are deterministic "
                      "simulations. Add Base URL to enable Playwright-driven runs."
                  )
              if bug_total:
                  parts.append(
                      f"{bug_total} bug report(s) auto-created for Failed/Blocked "
                      f"items — see the Bug Reports page."
                  )
              flash(" ".join(parts), "success")
              return redirect(url_for("test_execution_page"))
          except Exception as exc:
            # Heavy POST: 100+ items, multi-env, optional Playwright —
            # any single failure used to bubble to a 500 page. Catch it,
            # log a stack trace, and flash a friendly message so the
            # user can adjust the run config and retry.
            log.exception("Test Execution POST failed: %s", exc)
            flash(
                "Something went wrong while running the test pack: "
                f"{type(exc).__name__}. Try a smaller selection or fewer "
                "environments and check server logs for details.",
                "error",
            )
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
                               # Pre-fill Base URL from the previous run so
                               # the tester doesn't retype it. Empty string
                               # is fine — the template falls back to the
                               # first resource URL.
                               last_base_url=session.get(
                                   "automation_base_url", ""),
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

        bug_d = bug_to_dict(bug)
        bugs.append(bug_d)
        session["bug_reports_data"] = bugs
        _persist_bug(bug_d, source="manual")

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
