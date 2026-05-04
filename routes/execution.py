"""TestFortge — Manual test execution + bug report routes.

  * GET/POST /test-execution                 — configure and run test items
  * POST     /test-execution/generate-account — throw-away test account
  * POST     /create-bug-report              — create a bug entry
  * GET      /bug-reports                    — list and manage bugs
  * GET      /export-bug-reports             — markdown export
"""

from __future__ import annotations

from datetime import datetime

from flask import (Flask, Response, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)

from engine.log import get_logger
from engine.qa_testers import (
    TESTERS, PLATFORMS, BROWSERS, DEVICES, MOBILE_WEB,
    SCREEN_SIZES, TESTING_TYPES,
    WEB_PLATFORMS, WEB_BROWSERS, MOBILE_WEB_OSES, MOBILE_WEB_BROWSERS,
    MOBILE_RESOLUTIONS, IOS_DEVICES, ANDROID_DEVICES,
    # Feature #5 + #6: versioned OS list + engine-matrix resolver
    WEB_PLATFORMS_VERSIONED, MOBILE_OS_VERSIONS,
    resolve_platform_browser,
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



def _maybe_restore_pack_from_db() -> None:
    """If the session has no TC / CL pack but the active project does,
    rehydrate the session keys from the DB so the run page shows what
    the user uploaded earlier (Phase 2 persistence).

    No-op when:
      * session already carries a pack (avoid clobbering);
      * project_id is missing (fresh visitor);
      * DB read raises or returns empty.

    Best-effort — never raises. Failures are debug-logged.
    """
    pid = session.get("project_id")
    if not pid:
        return
    if session.get("test_cases_data") or session.get("checklist_data"):
        return
    try:
        from engine import db as _db
        # The DB layer exposes `load_test_cases` / `load_checklist` for
        # this rehydration path; both return empty lists on miss.
        tc = _db.load_test_cases(pid) if hasattr(_db, "load_test_cases") else []
        cl = _db.load_checklist(pid) if hasattr(_db, "load_checklist") else []
    except Exception as exc:  # pragma: no cover
        log.debug("restore: db read failed: %s", exc)
        return
    if tc:
        session["test_cases_data"] = tc
    if cl:
        session["checklist_data"] = cl


def register(app: Flask) -> None:
    @app.route("/test-execution", methods=["GET", "POST"])
    def test_execution_page():
        # Restore pack from Postgres when the session is empty but the
        # active project saved one earlier. The previous upload route
        # writes the parsed pack to DB via _persist_test_cases /
        # _persist_checklist, but the session is request-scoped — a
        # fresh tab or post-restart visit would otherwise show the
        # empty form even though the DB still has the cases.
        if request.method == "GET":
            try:
                _maybe_restore_pack_from_db()
            except Exception as exc:  # pragma: no cover
                log.debug("pack restore skipped: %s", exc)

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
                  # Web (default). Feature #5 introduces a versioned OS
                  # selector ("Windows 11", "macOS Sonoma (14)", ...).
                  # We honour it when present and fall back to the
                  # coarse "web_platform" value posted by older clients.
                  os_version = (request.form.get("web_os_version", "") or "").strip()
                  platform = (request.form.get("web_platform", "") or "").strip()
                  if not platform:
                      # Reverse-look the coarse family from the version.
                      for fam, versions in WEB_PLATFORMS_VERSIONED.items():
                          if os_version in versions:
                              platform = fam; break
                      platform = platform or "Windows"
                  browser = request.form.get("web_browser", "Chrome").strip() or "Chrome"
                  version = (request.form.get("web_version", "") or "").strip()
                  display_os = os_version or platform
                  bits = [f"Web · {display_os}", browser]
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
              # Operator-feedback fix: when the form's Base URL is empty
              # but the session has any resource_urls (extracted from
              # the original generation prompt or attached files), use
              # the first one as Base URL so Playwright always runs
              # against a real target. Without this every run that
              # didn't explicitly retype the URL fell back to the
              # deterministic simulator — no live preview, no video,
              # no bug-report attachments.
              if not base_url and resource_urls:
                  for cand in resource_urls:
                      if cand.startswith(("http://", "https://")):
                          base_url = cand
                          log.info(
                              "auto-base_url: using %s from resource_urls", cand)
                          break
              # The "Re-run failed only" path used to live as a Scope
              # dropdown — UI was confusing, so it's now an implicit
              # default of "all selected" for new runs and a per-row
              # button on completed runs (added separately). Backend
              # still accepts scope=failed for backward-compat callers.
              scope = (request.form.get("scope") or "all").strip().lower()
              # Headless is auto-detected: true on every cloud deployment
              # (no DISPLAY) so we never try to launch a visible window
              # on Render and time out. The legacy form flag is honoured
              # only when the operator forces it from a local POST.
              import os as _os
              has_display = bool(_os.environ.get("DISPLAY")) and _os.name != "nt"
              headless = (request.form.get("headless", "1") == "1") if "headless" in request.form else (not has_display)
              # Video defaults ON when there's a target URL — operator
              # video was clearly puzzled by the "no video" outcome
              # because the form's default for record_video was "0
              # (faster)". With a URL, Playwright runs and recording
              # adds 5–15 s of context-shutdown but the visual proof
              # is what testers came here for. The user can still
              # explicitly opt out via the form's "Record video" radio.
              _vid_form = request.form.get("record_video")
              if _vid_form is None:
                  record_video = bool(base_url)
              else:
                  record_video = (_vid_form == "1")
              # Hard-coded "fast" speed preset — the dropdown was removed
              # because all three options shipped with subtle bugs and
              # operators just want it to be fast and clear. Element
              # actions still get a 3 s ceiling, but page navigation
              # gets a separate 15 s budget so a real cold-start website
              # doesn't blow up the whole run at goto.
              speed_full_page = False
              speed_before_steps = False
              # Tightened again after the 10 min/4 cases report. 1 s
              # for elements in the DOM (slow selectors are TC bugs and
              # should fail fast), 6 s for page navigation — any real
              # cold-start site loads within that; beyond it we mark
              # Blocked rather than burn the run-budget waiting.
              speed_timeout_ms = 1000
              speed_nav_timeout_ms = 6000
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
              # Operator complaint 2026-05-02: 'Live shows nothing even
              # though Base URL was set'. The earlier check only fired
              # for source=='test_cases', so a checklist run silently
              # fell back to the simulator. Both branches now invoke
              # Playwright + a single decision log makes server-side
              # debugging much easier.
              automation_assets: dict[str, dict] = {}
              wants_automation = (
                  bool(base_url)
                  and source in ("test_cases", "checklist")
                  and any(et in ("web", "mobile_web") for et in env_types)
              )
              log.info(
                  "automation-decision: wants=%s base_url=%r source=%r "
                  "env_types=%r selected=%d",
                  wants_automation, base_url, source, env_types,
                  len(selected_ids or []),
              )
              # Pre-flight live-info write: even if the runner fails
              # before its first internal status='starting' write, the
              # live page will at least transition idle → starting →
              # idle (or done). Operator will see SOME activity and not
              # think the page is broken.
              if wants_automation:
                  try:
                      from routes.automation import STORAGE_ROOT as _SR
                      import json as _json, time as _time, os as _os
                      live_dir = _os.path.join(
                          _SR, "automation_runs", "_live")
                      _os.makedirs(live_dir, exist_ok=True)
                      info_path = _os.path.join(live_dir, "info.json")
                      tmp = info_path + ".tmp"
                      with open(tmp, "w", encoding="utf-8") as f:
                          _json.dump({
                              "status": "starting",
                              "step": 0,
                              "cases_done": 0,
                              "cases_total": len(selected_ids or items_data or []),
                              "current_tc": "",
                              "base_url": base_url,
                              "headless": True,
                              "elapsed_ms": 0,
                              "avg_ms_per_case": 0,
                              "cases_per_minute": 0,
                              "ts": int(_time.time() * 1000),
                          }, f)
                      _os.replace(tmp, info_path)
                      log.info("live-preflight: status=starting written")
                  except Exception as _exc:
                      log.warning("live-preflight failed: %s", _exc)
              # Operator-visibility — when a Web / Mobile-Web env was
              # picked but no base_url was supplied, the run silently
              # falls back to the deterministic simulator (no live
              # preview, no webm, no screenshots in bug reports). Flash
              # explains the trade-off so the next run can be configured
              # correctly without operator surprise.
              if (not base_url
                  and source == "test_cases"
                  and any(et in ("web", "mobile_web") for et in env_types)):
                  # Reaches here only when no resource_urls existed
                  # either — completely URL-less run, simulator only.
                  flash(
                      g.t.get(
                          "te_no_base_url_warning",
                          "Run started without any URL — Live preview, video, "
                          "and bug-report screenshots are disabled. Add a URL "
                          "to the requirements (or set Base URL) to enable "
                          "real Playwright execution."
                      ),
                      "warning",
                  )
              if wants_automation:
                  try:
                      from engine.automation_qa import scripts_from_session
                      from engine.automation_runner import AutomationRunner
                      from routes.automation import STORAGE_ROOT
                      # Honour the per-item selection. Without this filter
                      # Playwright drives EVERY test case in the project,
                      # ignoring whatever the operator unchecked in the UI.
                      automation_items = (
                          [it for it in items_data if it.get("id") in selected_ids]
                          if selected_ids else items_data
                      )
                      scripts = scripts_from_session(automation_items, base_url)

                      # Feature #6 — pick the Playwright engine + UA +
                      # viewport that match the OS/browser the tester
                      # selected. For the Mobile Web env we use the
                      # mobile-OS version + mobile browser instead of
                      # the desktop pair.
                      mw_active = "mobile_web" in env_types and "web" not in env_types
                      sel_os_ver = (
                          (request.form.get("mw_os_version", "")
                           or request.form.get("mw_os", "")).strip()
                          if mw_active else
                          (request.form.get("web_os_version", "")
                           or request.form.get("web_platform", "")).strip()
                      )
                      sel_browser = (
                          request.form.get("mw_browser", "Chrome").strip()
                          if mw_active else
                          request.form.get("web_browser", "Chrome").strip()
                      )
                      pb = resolve_platform_browser(sel_os_ver, sel_browser)
                      log.info(
                          "automation: engine=%s ua_short=%s viewport=%dx%d "
                          "(os_version=%r browser=%r)",
                          pb["engine"], pb["ua"][:40] + "...",
                          pb["viewport"][0], pb["viewport"][1],
                          sel_os_ver, sel_browser,
                      )
                      runner = AutomationRunner(
                          storage_root=STORAGE_ROOT,
                          base_url=base_url,
                          headless=headless,
                          record_video=record_video,
                          default_timeout_ms=speed_timeout_ms,
                          navigation_timeout_ms=speed_nav_timeout_ms,
                          screenshot_full_page=speed_full_page,
                          screenshot_before_steps=speed_before_steps,
                          credentials=credentials if credentials.is_active() else None,
                          engine_kind=pb["engine"],
                          user_agent=pb["ua"],
                          viewport_override=pb["viewport"],
                      )
                      # ── ARCHITECTURAL DEBT ────────────────────────
                      # The proper fix for 502/503 on long runs (100+
                      # cases) is to dispatch this to engine.job_queue
                      # (already used by /automation/run-async) and
                      # have the request return immediately — the live
                      # view polls until done, then a dedicated results
                      # endpoint reads the JSON-on-disk and renders.
                      #
                      # Until that lands, we run the Playwright pass on
                      # a daemon thread so a gunicorn force-kill of the
                      # request thread doesn't murder Chromium mid-run:
                      # the runner keeps writing _live/latest.png and
                      # storage/automation_runs/<run_id>/* until natural
                      # completion. The live filmstrip therefore stays
                      # responsive even after the originating browser
                      # tab has timed out and the operator has refreshed
                      # /test-execution/live in a new tab.
                      import threading as _threading
                      import time as _time
                      from concurrent.futures import (Future as _Future,
                                                     TimeoutError as _FutTimeout)
                      _fut: _Future = _Future()
                      def _bg_run():
                          try:
                              _fut.set_result(runner.run(scripts))
                          except BaseException as _exc:
                              _fut.set_exception(_exc)
                      _t0 = _time.time()
                      _t = _threading.Thread(
                          target=_bg_run, name="tf-execution-runner",
                          daemon=True)
                      _t.start()
                      # Block this request thread until the runner
                      # finishes — gunicorn --timeout (now 1800 s, see
                      # render.yaml) is the wall-clock ceiling. If the
                      # ceiling is breached the daemon keeps running in
                      # the background, writes artifacts to disk, and
                      # the operator picks up results via the live view.
                      # log.info every 30 s as a heartbeat so Render's
                      # logs show progress instead of silence.
                      while True:
                          try:
                              auto_report = _fut.result(timeout=30)
                              break
                          except _FutTimeout:
                              log.info(
                                  "automation: still running after %d s "
                                  "(waiter heartbeat)",
                                  int(_time.time() - _t0))
                              continue
                      # Index by case ID so the per-env loop below can pull
                      # screenshots / videos / status into the right run row.
                      # NOTE: RunReport.scripts (NOT .results) holds the
                      # per-case ScriptResult objects, and screenshots are
                      # nested in each ScriptResult.steps[*].screenshot_after
                      # — extracting them here is what feeds the post-run
                      # gallery in templates/test_execution.html.
                      import os as _os
                      for r in (getattr(auto_report, "scripts", []) or []):
                          cid = getattr(r, "tc_id", None) or getattr(r, "case_id", None)
                          if not cid:
                              continue
                          # Only include screenshots that actually exist
                          # on disk and are non-zero size — otherwise the
                          # post-run gallery shows a broken-image icon.
                          # Empty screenshots can happen when page.
                          # screenshot raised mid-run (e.g. navigation
                          # interrupted).
                          #
                          # Two parallel collections from May 2026:
                          #  * shots[]   — clean per-step "after" frames.
                          #                Goes into the test-execution
                          #                gallery so Passed cases never
                          #                show red error banners.
                          #  * fail_shots[] + failure_step (idx, comment)
                          #                — annotated copies (red banner
                          #                + arrow + bbox highlight).
                          #                Used as bug-report evidence on
                          #                the FAILED step plus the prior
                          #                step's "after" shot for context.
                          shots: list[str] = []
                          fail_shots: list[str] = []
                          failure_step: dict | None = None
                          prev_after: str = ""
                          for step in (getattr(r, "steps", []) or []):
                              after = getattr(step, "screenshot_after", "") or ""
                              if after:
                                  abs_p = _os.path.join(STORAGE_ROOT,
                                                         after.replace("/", _os.sep))
                                  try:
                                      if (_os.path.isfile(abs_p)
                                              and _os.path.getsize(abs_p) > 0):
                                          shots.append(after)
                                  except OSError:
                                      pass
                              fail = getattr(step, "screenshot_failure", "") or ""
                              if fail:
                                  abs_f = _os.path.join(STORAGE_ROOT,
                                                         fail.replace("/", _os.sep))
                                  try:
                                      if (_os.path.isfile(abs_f)
                                              and _os.path.getsize(abs_f) > 0):
                                          fail_shots.append(fail)
                                          # First failure wins — the run
                                          # broke here; subsequent steps
                                          # didn't execute.
                                          if failure_step is None:
                                              failure_step = {
                                                  "index": getattr(step, "index", 0),
                                                  "action": getattr(step, "action", ""),
                                                  "comment": getattr(step, "comment", ""),
                                                  "screenshot": fail,
                                                  "context_screenshot": prev_after,
                                                  "console_errors": list(
                                                      getattr(step, "console_errors", []) or []
                                                  )[:5],
                                              }
                                  except OSError:
                                      pass
                              if after:
                                  prev_after = after
                          # Validate video too — Playwright finalises the
                          # webm only on context close, so partial /
                          # failed contexts can leave a 0-byte file.
                          video = getattr(r, "video_path", "") or ""
                          if video:
                              abs_v = _os.path.join(STORAGE_ROOT, video.replace("/", _os.sep))
                              try:
                                  if not (_os.path.isfile(abs_v) and _os.path.getsize(abs_v) > 0):
                                      video = ""
                              except OSError:
                                  video = ""
                          automation_assets[cid] = {
                              "status": getattr(r, "status", ""),
                              "video": video,
                              "screenshots": shots,
                              "failure_screenshots": fail_shots,
                              "failure_step": failure_step,
                              "final_url": getattr(r, "final_url", "") or "",
                              "duration_ms": getattr(r, "duration_ms", 0),
                          }
                      session["automation_report"] = {
                          "passed": auto_report.passed,
                          "failed": auto_report.failed,
                          "blocked": auto_report.blocked,
                          "run_id": auto_report.run_id,
                      }
                      log.info(
                          "automation-done: cases=%d passed=%d failed=%d "
                          "blocked=%d duration_ms=%d run_id=%s",
                          auto_report.total, auto_report.passed,
                          auto_report.failed, auto_report.blocked,
                          auto_report.duration_ms, auto_report.run_id,
                      )
                      flash(
                          f"✓ Playwright executed {auto_report.total} case(s) "
                          f"on {base_url} ({auto_report.passed} passed, "
                          f"{auto_report.failed} failed, "
                          f"{auto_report.blocked} blocked). "
                          "Live view shows the recorded frames; bug reports "
                          "carry annotated screenshots and video.",
                          "success",
                      )
                  except Exception as exc:
                      log.exception("Automation pass failed (non-fatal): %s", exc)
                      # Stamp the phase + reason into info.json so the
                      # /test-execution/diag endpoint shows the operator
                      # exactly what blew up — without forcing them to
                      # dig through Render logs.
                      try:
                          import json as _j
                          import time as _time
                          from routes.automation import STORAGE_ROOT as _SR
                          info_path = os.path.join(
                              _SR, "automation_runs", "_live", "info.json")
                          payload = {}
                          try:
                              with open(info_path, "r", encoding="utf-8") as _f:
                                  payload = _j.load(_f) or {}
                          except Exception:
                              pass
                          payload["status"] = "failed"
                          payload["phase"] = payload.get("phase", "route-catch")
                          payload["phase_error"] = (
                              f"{type(exc).__name__}: {str(exc)[:280]}")
                          payload["ts"] = int(_time.time() * 1000)
                          tmp = info_path + ".tmp"
                          with open(tmp, "w", encoding="utf-8") as _f:
                              _j.dump(payload, _f)
                          os.replace(tmp, info_path)
                      except Exception:
                          pass
                      flash(
                          "⚠ Automation pass failed: "
                          f"{type(exc).__name__} — {str(exc)[:300]}. "
                          "Results shown are deterministic simulations only — "
                          "no live preview, no video, no bug-report screenshots. "
                          "Open /test-execution/diag for the exact phase the runner stopped at.",
                          "warning",
                      )

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
                      # Pull screenshots + video from the automation
                      # evidence we collected for this exact case so the
                      # bug carries reproduction artefacts. Each entry is
                      # a path under STORAGE_ROOT served by
                      # /automation/asset/<path>; templates resolve them
                      # via url_for('automation_asset', path=p).
                      linked = bug_dict.get("linked_item_id", "")
                      ev = automation_assets.get(linked) if linked else None
                      if ev:
                          existing_atts = list(bug_dict.get("attachments") or [])
                          # Prefer evidence that actually demonstrates
                          # what went wrong: the FAILED step's annotated
                          # screenshot + the prior step's "after" frame
                          # for context. Operator complaint: "screenshots
                          # don't match the bug description". Reason was
                          # we attached every clean step shot, which
                          # reads as "here's the working flow" instead
                          # of "here's the bug". Fall back to the clean
                          # gallery only when no failure shot landed
                          # (e.g. case marked Failed by the simulator
                          # but Playwright never raised — rare).
                          fstep = ev.get("failure_step") or {}
                          targeted: list[str] = []
                          if fstep.get("context_screenshot"):
                              targeted.append(fstep["context_screenshot"])
                          if fstep.get("screenshot"):
                              targeted.append(fstep["screenshot"])
                          if not targeted:
                              # No annotated failure on disk — surface
                              # at most the last 3 clean shots as best-
                              # effort evidence.
                              targeted = list((ev.get("screenshots") or [])[-3:])
                          for shot in targeted:
                              if shot and shot not in existing_atts:
                                  existing_atts.append(shot)
                          v = ev.get("video")
                          if v and v not in existing_atts:
                              existing_atts.append(v)
                          bug_dict["attachments"] = existing_atts
                          # Stash structured failure context onto the bug
                          # for the bug-template to consume — see
                          # engine/bug_template.py for the rules.
                          if fstep:
                              bug_dict["_automation_failure"] = {
                                  "step_index": fstep.get("index"),
                                  "step_action": fstep.get("action"),
                                  "comment": fstep.get("comment"),
                                  "console_errors": fstep.get(
                                      "console_errors") or [],
                                  "final_url": ev.get("final_url") or "",
                              }

                      # Rewrite the bug via the rule-driven template now
                      # that automation context (if any) is attached.
                      # Falls back to a light-touch scrub when no
                      # Playwright failure landed for this item — keeps
                      # simulator-only bugs but strips banned filler.
                      try:
                          from engine.bug_template import (
                              rewrite_bug_from_automation as _rewrite_bug,
                          )
                          # Pull TC fields from the source items_data so
                          # the rewrite has full context.
                          linked_item = next(
                              (it for it in items_data
                               if it.get("id") == linked), None) if linked else None
                          tc_fields = {
                              "tc_summary": (linked_item or {}).get(
                                  "summary")
                              or (linked_item or {}).get("objective", ""),
                              "tc_steps": (linked_item or {}).get(
                                  "test_steps", ""),
                              "tc_preconditions": (linked_item or {}).get(
                                  "preconditions", ""),
                              "tc_expected": (linked_item or {}).get(
                                  "expected_result", ""),
                              "tc_section": (linked_item or {}).get(
                                  "section", "") or bug_dict.get(
                                  "component", ""),
                          }
                          _rewrite_bug(
                              bug_dict,
                              automation_failure=bug_dict.pop(
                                  "_automation_failure", None),
                              base_url=base_url,
                              **tc_fields,
                          )
                      except Exception as _rw_exc:
                          log.warning(
                              "bug rewrite skipped (%s): %s",
                              type(_rw_exc).__name__, _rw_exc)
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
                  # Carry through evidence (screenshots + recorded video) so
                  # the post-run gallery in the UI can render thumbnails and
                  # an embedded video player. We cap screenshots at 6 per
                  # case to keep the session pickle bounded — the full set
                  # is still on disk under storage/automation_runs/<run_id>/.
                  results_summary = [
                      {"item_id": r.get("item_id"),
                       "status":  r.get("status"),
                       "source":  r.get("source", "auto"),
                       "bug_id":  r.get("bug_id"),
                       "comment": r.get("comment", ""),
                       "duration_ms": r.get("duration_ms"),
                       "video": r.get("video", ""),
                       "screenshots": (r.get("screenshots") or [])[:6]}
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
                               web_platforms_versioned=WEB_PLATFORMS_VERSIONED,
                               mobile_os_versions=MOBILE_OS_VERSIONS,
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

    @app.route("/test-execution/auto-run", methods=["GET", "POST"])
    def test_execution_auto_run():
        """Server-side auto-run path for JS-disabled testers.

        Triggered by the upload route's ?auto_run=1 redirect when JS
        isn't available to click the Run button on /test-execution. We
        dispatch a default execute_items() run with sensible defaults:
        first tester, generic Web environment, deterministic simulator
        (no Playwright / no Base URL), all selected items.
        """
        _maybe_restore_pack_from_db()
        tc_data = session.get("test_cases_data", []) or []
        cl_data = session.get("checklist_data", []) or []

        if not tc_data and not cl_data:
            flash(g.t.get(
                "te_no_pack_for_autorun",
                "Nothing to run yet — upload or generate a pack first."),
                "error",
            )
            return redirect(url_for("test_execution_page"))

        # Pick source: TC > CL when both exist (matches the form's
        # default radio button).
        if tc_data:
            items_data = tc_data
            item_type = "test_case"
            source = "test_cases"
        else:
            items_data = cl_data
            item_type = "checklist"
            source = "checklist"

        # Default execution context. Operator can rerun with different
        # settings via the regular form — auto-run's job is just to
        # show that the pack works end-to-end.
        tester_id = (TESTERS[0].id if TESTERS else "tester_1")
        tester = get_tester(tester_id)
        tester_name = tester.name if tester else tester_id
        environment = "Web · Auto · " + tester_name
        testing_types = ["Functional"]

        try:
            execution = execute_items(
                items=items_data,
                item_type=item_type,
                tester_id=tester_id,
                environment=environment,
                testing_types=testing_types,
                selected_ids=None,
            )
        except Exception as exc:  # pragma: no cover — surfaces on UI
            log.exception("auto-run failed: %s", exc)
            flash(
                g.t.get("te_auto_run_failed",
                        "Auto-run failed: ") + f"{type(exc).__name__}",
                "error",
            )
            return redirect(url_for("test_execution_page"))

        # Build a run record matching the shape produced by the regular
        # POST handler so the existing UI list renders it correctly.
        results_summary = [
            {"item_id": r.get("item_id"),
             "status":  r.get("status"),
             "source":  r.get("source", "auto"),
             "bug_id":  r.get("bug_id"),
             "comment": r.get("comment", ""),
             "duration_ms": r.get("duration_ms"),
             "video": r.get("video", ""),
             "screenshots": (r.get("screenshots") or [])[:6]}
            for r in (execution.get("results") or [])
        ]
        test_runs = session.get("test_runs", [])
        run_record = {
            "run_id": len(test_runs) + 1,
            "db_run_id": None,
            "source": source,
            "tester_id": tester_id,
            "tester_name": tester_name,
            "environment": environment,
            "env_type": "web",
            "testing_types": ", ".join(testing_types),
            "results": results_summary,
            "stats": execution.get("stats") or {},
            "bug_count": len(execution.get("bugs") or []),
            "site_url": "",
            "base_url": "",
            "headless": True,
            "record_video": False,
            "automation_used": False,
            "created_at": datetime.now().isoformat(),
        }
        test_runs.append(run_record)
        session["test_runs"] = test_runs

        # Bug reports also flow into the session so the Bug Reports
        # page picks them up. Mirrors the normal POST handler's
        # treatment without the per-bug ISTQB stamping (auto-run is a
        # quick smoke, not a release-grade run).
        bugs_session = session.get("bug_reports_data", [])
        for bug_dict in execution.get("bugs", []) or []:
            new_id = generate_bug_id([dict_to_bug(b) for b in bugs_session])
            bug_dict["id"] = new_id
            bug_dict.setdefault("environment", environment)
            bugs_session.append(bug_dict)
        session["bug_reports_data"] = bugs_session

        stats = run_record["stats"] or {}
        passed = stats.get("passed", 0)
        failed = stats.get("failed", 0)
        blocked = stats.get("blocked", 0)
        flash(
            g.t.get("te_auto_run_done",
                    f"Auto-run finished: {passed} passed, {failed} failed, "
                    f"{blocked} blocked."),
            "success",
        )
        return redirect(url_for("test_execution_page"))

    # ── Live-view of an in-progress automation run ──────────────────
    # On cloud deployments (Render, etc.) the operator cannot see the
    # browser window because there is no display server attached to the
    # container. Instead we mirror every Playwright screenshot to a
    # well-known location and the page below polls it once per second so
    # the operator gets a "live filmstrip" of what the bot is seeing.
    @app.route("/test-execution/diag", methods=["GET"])
    def test_execution_diag():
        """Operator-facing diagnostic JSON. Reports playwright import
        status, browser binary paths, _live directory presence, last
        info.json, strip frame count, and recent run dirs.

        Hit `/test-execution/diag` from a browser when live view stays
        empty — it answers in one place whether Playwright was even
        invoked and what artefacts landed on disk."""
        import os, json, glob
        from routes.automation import STORAGE_ROOT
        out = {}
        try:
            from playwright.sync_api import sync_playwright as _pw
            out["playwright_importable"] = True
            try:
                with _pw() as pw:
                    out["chromium_path"] = getattr(pw.chromium, "executable_path", "—")
                    out["firefox_path"]  = getattr(pw.firefox, "executable_path", "—")
                    out["webkit_path"]   = getattr(pw.webkit, "executable_path", "—")
            except Exception as exc:
                out["playwright_open_error"] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            out["playwright_importable"] = False
            out["playwright_import_error"] = str(exc)

        live_dir = os.path.join(STORAGE_ROOT, "automation_runs", "_live")
        out["live_dir"] = live_dir
        out["live_dir_exists"] = os.path.isdir(live_dir)
        if out["live_dir_exists"]:
            info_path = os.path.join(live_dir, "info.json")
            out["info_json_exists"] = os.path.isfile(info_path)
            if out["info_json_exists"]:
                try:
                    out["info_json"] = json.load(open(info_path, encoding="utf-8"))
                except Exception as exc:
                    out["info_json_error"] = str(exc)
            out["latest_png_exists"] = os.path.isfile(
                os.path.join(live_dir, "latest.png"))
            strip_files = sorted(glob.glob(
                os.path.join(live_dir, "strip", "*.png")))
            out["strip_frame_count"] = len(strip_files)
        runs_dir = os.path.join(STORAGE_ROOT, "automation_runs")
        if os.path.isdir(runs_dir):
            entries = [e for e in os.listdir(runs_dir) if e != "_live"]
            out["recent_runs"] = sorted(entries)[-5:]
        out["session_test_cases"] = len(session.get("test_cases_data") or [])
        out["session_checklist"]  = len(session.get("checklist_data") or [])
        return jsonify(out)

    @app.route("/test-execution/live", methods=["GET"])
    def test_execution_live():
        return render_template("test_execution_live.html")

    @app.route("/test-execution/live/frame")
    def test_execution_live_frame():
        """Serve the most recent frame from automation_runs/_live/latest.png
        with strict no-cache headers so the browser always re-fetches."""
        import os
        from flask import send_file, abort
        from routes.automation import STORAGE_ROOT
        live_dir = os.path.join(STORAGE_ROOT, "automation_runs", "_live")
        path = os.path.join(live_dir, "latest.png")
        if not os.path.isfile(path):
            # Serve a tiny built-in placeholder so the <img> tag never 404s
            # mid-poll. The placeholder is a 1x1 transparent PNG.
            from io import BytesIO
            tiny = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
                    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
                    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe"
                    b"\x02\xfe\xa75\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82")
            resp = Response(tiny, mimetype="image/png")
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            return resp
        resp = send_file(path, mimetype="image/png", max_age=0)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp


    @app.route("/test-execution/live/strip/<int:slot>")
    def test_execution_live_strip(slot):
        """Serve the filmstrip ring-buffer slot 00..11. Each call is
        cheap because the runner pre-writes the PNG via os.replace —
        we just stream the file with no-cache headers."""
        import os
        from flask import send_file
        from routes.automation import STORAGE_ROOT
        if slot < 0 or slot >= 12:
            from io import BytesIO
            tiny = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
                    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
                    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe"
                    b"\x02\xfe\xa75\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82")
            r = Response(tiny, mimetype="image/png")
            r.headers["Cache-Control"] = "no-store"
            return r
        path = os.path.join(STORAGE_ROOT, "automation_runs", "_live",
                            "strip", f"{slot:02d}.png")
        if not os.path.isfile(path):
            from io import BytesIO
            tiny = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
                    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
                    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe"
                    b"\x02\xfe\xa75\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82")
            r = Response(tiny, mimetype="image/png")
            r.headers["Cache-Control"] = "no-store"
            return r
        r = send_file(path, mimetype="image/png", max_age=0)
        r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return r

    @app.route("/test-execution/live/info")
    def test_execution_live_info():
        """Serve the live progress JSON consumed by the polling page."""
        import json
        import os
        from routes.automation import STORAGE_ROOT
        live_dir = os.path.join(STORAGE_ROOT, "automation_runs", "_live")
        path = os.path.join(live_dir, "info.json")
        if not os.path.isfile(path):
            payload = {"status": "idle", "step": 0, "cases_done": 0,
                       "cases_total": 0, "current_tc": "", "ts": 0}
        else:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception:
                payload = {"status": "idle"}
        resp = Response(json.dumps(payload), mimetype="application/json")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

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
