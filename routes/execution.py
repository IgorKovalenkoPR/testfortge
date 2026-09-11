"""TestFortge — Test execution + run dispatch + results routes.

  * GET/POST /test-execution                  — configure and run test items
  * POST     /test-execution/generate-account — throw-away test account
  * POST     /test-execution/auto-run         — shortcut run (no form)
  * GET      /test-execution/diag             — testers / browsers debug
  * GET      /test-execution/run-status/<id>  — poll worker progress

What is left here is what the name promises: configuring a run and
getting it started. Stage 7 Phase A extracted ``/bug-reports`` and
friends into :mod:`routes.bugs` and ``/test-execution/live*`` into
:mod:`routes.execution_live`; the file then grew back past the size the
split was for, and Phase B is the cut Phase A named and deferred:

* :mod:`engine.run_results` — the five functions that read a finished
  run and decide what it means. They never touched Flask, and two test
  modules were already importing them straight out of this file, which
  is what a misplaced pure function looks like from the outside.
* :mod:`routes.execution_results` — ``/test-execution/results/<id>``,
  the only caller of four of those five.

``dedupe_bugs_by_root_cause`` is the one helper still called from here:
a run's own POST collapses duplicate bugs before storing them, and the
results page does it again over what the worker wrote.
"""

from __future__ import annotations

from datetime import datetime

from flask import (Flask, current_app, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)

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
    generate_bug_id, bug_to_dict, dict_to_bug,
)
from engine.run_results import dedupe_bugs_by_root_cause
from engine.test_credentials import (
    credentials_from_form, credentials_from_session, credentials_to_session,
    generate_test_account,
)

from engine import db as _db

from ._shared import (AUTOCREATED_KEY, extract_resource_urls,
                      ensure_active_project,
                      get_session_id, pack_bugs, pack_checklist,
                      pack_runs, pack_test_cases, mirror_pack)
# ``_persist_bug`` lives in routes/bugs.py since Stage 7 Phase A. The POST
# below mirrors each bug a run files into the DB through it, the same
# writer the bug pages use.
from .bugs import _persist_bug

log = get_logger(__name__)



def _run_limit_scope() -> list[str]:
    """The projects a browser run competes with.

    The organisation when there is one, because the memory is shared by the
    whole service — two projects in one team starting a run each costs
    exactly as much as one project starting two, and scoping per project
    would make the limit bypassable by switching project. With
    organisations off, the honest scope is the caller's own projects.
    """
    from engine import permissions as _perm_mod
    org_id = None
    try:
        org_id = _perm_mod.current_org_id()
    except Exception:  # pragma: no cover — defensive
        org_id = None
    try:
        if org_id:
            return [p["id"] for p in _db.list_projects_for_org(org_id)
                    if p.get("id")]
        owned = _db.list_projects(owner_sid=get_session_id(session)) or []
        ids = [p["id"] for p in owned if p.get("id")]
    except Exception as exc:  # pragma: no cover — best-effort
        log.warning("run limit scope lookup failed: %s", exc)
        ids = []
    active = ensure_active_project()
    if active and active not in ids:
        ids.append(active)
    return ids


def _run_limit_decision():
    """Whether another browser run may start now."""
    from engine import run_limits
    return run_limits.check(_run_limit_scope())


def _maybe_restore_pack_from_db() -> None:
    """Re-pin the active project to one that actually has a pack.

    Not cold-start recovery — the repository does that on its own now, and
    this function used to duplicate it in a third place (generation had
    ``_hydrate_from_db``, projects had an inline block). What is left is the
    part neither of those does, and which an operator reported on
    2026-05-04: the session can be pinned to a freshly auto-created
    "Untitled project" with nothing in it while the real pack sits under a
    different project owned by the same session. Recovery from Postgres
    cannot help, because it faithfully loads the empty project.

    So: if the active project has a pack, do nothing. Otherwise look through
    this owner's other projects, prefer ones with content, and re-pin. The
    accessors then read the pack from the project we just chose.

    Only over a project **this session invented**. Walked 2026-09-01:
    create a project from the picker, open /test-execution once, and the
    active project was silently back to the previous one — on the execution
    page and on every page after it. The flash had just said "Project 'X'
    created and activated". Emptiness was the wrong trigger: a project the
    operator has only just made is always empty, and that is not a mistake
    to correct. ``AUTOCREATED_KEY`` names the pin nobody chose, and it is
    the only one this function may overrule.

    Best-effort — never raises. Failures are debug-logged.
    """
    if session.get("project_id") != session.get(AUTOCREATED_KEY):
        return
    if pack_test_cases() or pack_checklist():
        return

    from engine import db as _db
    from engine import workspace as _workspace

    active = session.get("project_id") or ""
    try:
        owned = _db.list_projects(owner_sid=get_session_id(session)) or []
    except Exception as exc:
        log.debug("restore: owner project lookup failed: %s", exc)
        return

    # list_projects already returns updated_at desc, and its per-project
    # counts come from the same query — so ranking by "has content" is a
    # stable sort away, with no extra round-trip per candidate. The previous
    # version loaded every candidate's full pack to find out.
    ranked = sorted(
        (p for p in owned if p.get("id") and p.get("id") != active),
        key=lambda p: -(int(p.get("test_cases_count", 0) or 0)
                        + int(p.get("checklist_count", 0) or 0)),
    )
    for candidate in ranked:
        if (int(candidate.get("test_cases_count", 0) or 0)
                + int(candidate.get("checklist_count", 0) or 0)) <= 0:
            break                      # ranked, so the rest are empty too
        session["project_id"] = candidate["id"]
        # The accessors cache per request, and they cached the empty answer
        # for the project we just left.
        _workspace.invalidate()
        log.info("restore: re-pinned active project to %s (%d TC + %d CL)",
                 candidate["id"], candidate.get("test_cases_count", 0),
                 candidate.get("checklist_count", 0))
        return


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

        tc_data = pack_test_cases()
        cl_data = pack_checklist()
        has_tc = bool(tc_data)
        has_cl = bool(cl_data)
        resource_urls = extract_resource_urls()

        test_runs = pack_runs()

        if request.method == "POST":
          try:
              source = request.form.get("source", "test_cases")
              # PR-3: orthogonal Run Mode — "tc_driven" (default) keeps
              # every existing path byte-identical; "walkthrough" hands
              # the run to ``engine/walkthrough_runner.py`` so the QA
              # walkthrough's 8 heuristics + axe-core sweep produce
              # findings that get persisted as bugs (see results
              # endpoint below). The radio lives at the top of the form
              # so the field always posts; missing/invalid values fall
              # back to "tc_driven" for safety.
              run_mode = (request.form.get("run_mode") or "tc_driven").strip().lower()
              if run_mode not in ("tc_driven", "walkthrough", "manual"):
                  run_mode = "tc_driven"
              # PR-5: the manual walk is a different surface, not a
              # different runner — hand the whole POST to it and let it
              # open the run. 307 preserves the method AND the body, so
              # the selection, the environment and the CSRF token all
              # arrive intact and the mode still works with JS disabled.
              if run_mode == "manual":
                  return redirect(url_for("manual_run_start"), code=307)

              # Fair use, before anything expensive happens (E5.5). The
              # manual walk is above this line on purpose: it is a person
              # reading a page and costs nothing to have ten of. A browser
              # run is a Chromium on a box with half a gigabyte, and two at
              # once are OOM-killed rather than queued — which shows up as a
              # run that stops with no verdict and no explanation. Refusing
              # the second one with a message naming the first is the whole
              # improvement.
              gate = _run_limit_decision()
              if not gate.allowed:
                  flash(gate.message(), "warning")
                  return redirect(url_for("test_execution_page"))
              # Walkthrough sub-config — only consulted when run_mode ==
              # "walkthrough". Numeric coercion is permissive so a
              # missing/empty field falls back to the conservative
              # defaults that PR-1/PR-2 already exercise via the debug
              # endpoint.
              def _wt_int(field: str, default: int, lo: int = 0) -> int:
                  try:
                      v = int(request.form.get(field, default))
                  except (TypeError, ValueError):
                      v = default
                  return max(lo, v)
              walkthrough_cfg = {
                  "max_pages":         _wt_int("walkthrough_max_pages",         6,      1),
                  "max_form_fills":    _wt_int("walkthrough_max_form_fills",    5,      0),
                  "device_timeout_ms": _wt_int("walkthrough_device_timeout_ms", 480000, 60000),
                  "axe_enabled":       (request.form.get("walkthrough_axe_enabled", "1")
                                         not in ("0", "false", "no", "")),
              }
              tc_binding = (request.form.get("walkthrough_tc_binding")
                            or "url_pattern").strip().lower()
              if tc_binding not in ("url_pattern", "ignore"):
                  tc_binding = "url_pattern"
              # The Testing Types / Assigned Tester / Test Account UI was
              # removed — testing scope is now driven by the prompt that
              # produced the test cases (see Test Cases / Checklist pages).
              # We keep accepting the fields if posted (older bookmarks /
              # automation), but otherwise default sensibly.
              tester_id = request.form.get("tester_id", "mid_1")
              testing_types = request.form.getlist("testing_types") or ["Regression"]
              selected_ids = request.form.getlist("selected_items")
              # PR-3: walkthrough mode doesn't pick TCs via checkboxes —
              # binding is driven by ``url_pattern`` on TC records (or
              # "ignore" to run heuristics only). Force-clear so the
              # rest of the handler stops treating the missing
              # selection as an empty filter.
              if run_mode == "walkthrough":
                  selected_ids = []

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
              # operators just want it to be fast and clear.
              speed_full_page = False
              speed_before_steps = False
              # 2026-05-05 reset to sane production defaults.
              # Operator-reported a run of 62 TCs producing 0 Passed,
              # 27 Failed, 35 Blocked — every Blocked entry was
              # "Locator.click: Timeout 1000ms exceeded" or
              # "Page.goto: Timeout 6000ms exceeded". Those values
              # were the previous "tightened" preset and were way too
              # aggressive for real cold-start websites: a CMS-backed
              # marketing site routinely needs 2-4 s before its
              # interactive elements are clickable, and a fresh page
              # load over Render free-tier latency can take 8-15 s.
              # The new defaults match Playwright's documented
              # recommendations: 5 s for element actions, 20 s for
              # page navigation. This restores Pass-rate on TCs that
              # weren't actual defects.
              speed_timeout_ms = 5000
              speed_nav_timeout_ms = 20000
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
              all_bugs = pack_bugs()
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
              # PR-3: walkthrough mode dispatches into the same detached
              # worker as the TC-driven path but skips the source-pack
              # check — there is no checklist or test-cases pack to
              # "run", the runner walks the URL autonomously.
              wants_automation = (
                  bool(base_url)
                  and any(et in ("web", "mobile_web") for et in env_types)
                  and (
                      run_mode == "walkthrough"
                      or source in ("test_cases", "checklist")
                  )
              )
              log.info(
                  "automation-decision: wants=%s mode=%s base_url=%r source=%r "
                  "env_types=%r selected=%d",
                  wants_automation, run_mode, base_url, source, env_types,
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
                  # ── PER-SESSION SUBPROCESS CONCURRENCY CAP ────────
                  # Sprint 1 Task 5: a runaway tab can otherwise spam
                  # /test-execution and bury _pending/ in unfinished
                  # config JSONs, each spawning its own Chromium process.
                  # Render free-tier (512 MB) OOMs at ~3 concurrent
                  # browser contexts. We refuse the dispatch when the
                  # active count for this session already meets the cap.
                  # Subprocess runs live OUTSIDE JobQueue, so we count
                  # by scanning the pending dir directly.
                  try:
                      import os as _os_cap
                      from engine.job_queue import count_active_subprocess_runs as _cnt_sub
                      from routes._shared import get_session_id as _gsid_cap
                      from routes.automation import STORAGE_ROOT as _SR_CAP
                      _pending_dir_cap = _os_cap.path.join(
                          _SR_CAP, "automation_runs", "_pending")
                      _sid_cap = _gsid_cap(session)
                      _cap = int(current_app.config.get(
                          "MAX_CONCURRENT_RUNS", 3) or 3)
                      _active = _cnt_sub(_pending_dir_cap, _sid_cap)
                      if _active >= _cap:
                          flash(
                              f"You already have {_active} test-execution "
                              f"run(s) in flight (cap is {_cap}). Please "
                              f"wait for them to finish before starting "
                              f"another.",
                              "warning",
                          )
                          return redirect(url_for("test_execution_page"))
                  except Exception as _exc_cap:
                      # Counting is best-effort; never let a stat failure
                      # block the operator's run.
                      log.debug("subprocess cap check skipped: %s", _exc_cap)

                  # ── DETACHED SUBPROCESS DISPATCH ──────────────────
                  # Operator hit 502/503 around case 37 of 62 at the
                  # ~12-13 min mark — well below gunicorn --timeout.
                  # Root cause: Render's edge proxy closes the HTTP
                  # connection after several minutes of silence, and
                  # then subsequent requests pile up against gunicorn's
                  # 4-thread worker until it returns 503. Daemon-thread
                  # dispatch did NOT help because gunicorn force-kill
                  # tears down ALL threads in the worker process.
                  #
                  # Fix: spawn the Playwright pass as a fully detached
                  # subprocess (start_new_session=True). It survives
                  # any gunicorn restart, writes result.json + done.flag
                  # to <storage>/automation_runs/_pending/. The POST
                  # responds in <1 s with a redirect to the live view;
                  # JS auto-redirects to /test-execution/results/<id>
                  # when the worker is done. The results endpoint
                  # picks up the merged automation_assets, runs the
                  # per-env loop + bug rewrite, and renders the same
                  # post-run page operators are used to.
                  try:
                      from routes.automation import STORAGE_ROOT
                      import json as _json
                      import os as _os
                      import sys as _sys
                      import subprocess as _subprocess
                      import uuid as _uuid

                      automation_items = (
                          [it for it in items_data
                           if it.get("id") in selected_ids]
                          if selected_ids else items_data
                      )

                      # Resolve engine/UA/viewport ahead of dispatch so
                      # the worker only deals with primitives.
                      mw_active = ("mobile_web" in env_types
                                   and "web" not in env_types)
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

                      # Build a JSON-serialisable runner config.
                      runner_kwargs = {
                          "base_url": base_url,
                          "headless": headless,
                          "record_video": record_video,
                          "default_timeout_ms": speed_timeout_ms,
                          "navigation_timeout_ms": speed_nav_timeout_ms,
                          "screenshot_full_page": speed_full_page,
                          "screenshot_before_steps": speed_before_steps,
                          "engine_kind": pb["engine"],
                          "user_agent": pb["ua"],
                          "viewport_override": list(pb["viewport"]),
                      }
                      cred_payload = None
                      if credentials and credentials.is_active():
                          # TestCredentials is a dataclass — best-effort
                          # serialise the public fields only.
                          try:
                              from dataclasses import asdict as _asdict
                              cred_payload = _asdict(credentials)
                          except Exception:
                              cred_payload = None

                      # Per-env config the results endpoint will need.
                      # We snapshot env-specific form fields here so the
                      # results endpoint doesn't have to re-read the
                      # original POST.
                      envs_meta: dict = {}
                      for et in env_types:
                          envs_meta[et] = {"environment": _build_env_string(et)}

                      config_id = (datetime.now().strftime("%Y%m%d_%H%M%S_")
                                   + _uuid.uuid4().hex[:6])
                      pending_dir = _os.path.join(
                          STORAGE_ROOT, "automation_runs", "_pending")
                      _os.makedirs(pending_dir, exist_ok=True)
                      config_path = _os.path.join(pending_dir,
                                                   f"{config_id}.json")
                      worker_log = _os.path.join(pending_dir,
                                                  f"{config_id}.log")

                      # PR-3: project tc_data into the walkthrough.test_cases
                      # list when ``tc_binding == "url_pattern"`` so the
                      # runner's per-page TC-match step (see
                      # ``walkthrough_tc_match.select_tcs_for_url``) has
                      # something to chew on. Pre-PR-2 TCs default to
                      # ``trigger="manual"`` so they're filtered out
                      # here — only TCs explicitly opted-in (always /
                      # walkthrough_url_match) become candidates. With
                      # ``tc_binding == "ignore"`` we never pass any TC
                      # through, so the walkthrough runs heuristics-only
                      # regardless of what's in the pack.
                      walkthrough_tcs: list = []
                      if run_mode == "walkthrough" and tc_binding == "url_pattern":
                          for _tc in tc_data:
                              if not isinstance(_tc, dict):
                                  continue
                              _trig = (str(_tc.get("trigger") or "manual")
                                       .strip().lower())
                              if _trig in ("always", "walkthrough_url_match"):
                                  walkthrough_tcs.append(_tc)
                      walkthrough_block: dict = {}
                      if run_mode == "walkthrough":
                          walkthrough_block = {
                              "start_urls":         [base_url] if base_url else [],
                              "test_cases":         walkthrough_tcs,
                              **walkthrough_cfg,
                          }
                      config_payload = {
                          "config_id": config_id,
                          "storage_root": STORAGE_ROOT,
                          "base_url": base_url,
                          "site_url": site_url,
                          "items_data": automation_items,
                          "selected_ids": selected_ids,
                          "env_types": env_types,
                          "manual_statuses": manual_statuses or {},
                          "manual_bug_refs": manual_bug_refs or {},
                          "session_id": get_session_id(session)
                              if "get_session_id" in globals() else "",
                          # S3.3: passed to the detached worker so it can
                          # write a dashboard metric snapshot after the
                          # run finishes (subprocess has no Flask session).
                          "project_id": session.get("project_id") or "",
                          "tester_id": tester_id,
                          "tester_name": (get_tester(tester_id).name
                                           if get_tester(tester_id)
                                           else tester_id),
                          "testing_types": testing_types,
                          "headless": headless,
                          "record_video": record_video,
                          "affects_version": affects_version,
                          "source": source,
                          "item_type": item_type,
                          "envs": envs_meta,
                          "runner_kwargs": runner_kwargs,
                          "credentials": cred_payload,
                          # PR-3: orthogonal Run Mode + walkthrough config.
                          # ``mode`` is read by ``runner_worker.py:394``;
                          # ``walkthrough`` is read by the same dispatch
                          # block. ``tc_binding`` is echoed to the
                          # results endpoint so the post-run page knows
                          # whether to surface the TC-match panel.
                          "mode": run_mode,
                          "tc_binding": tc_binding,
                          "walkthrough": walkthrough_block,
                      }
                      with open(config_path, "w", encoding="utf-8") as _f:
                          _json.dump(config_payload, _f)

                      # Spawn detached worker. start_new_session=True is
                      # the Linux equivalent of double-fork: the child
                      # gets its own session and process group so it
                      # doesn't get reaped when gunicorn kills the
                      # parent worker. stdout/stderr go to a per-run
                      # log file so we can debug without tailing
                      # Render's combined stream.
                      log_fh = open(worker_log, "w", encoding="utf-8")
                      _proc = _subprocess.Popen(
                          [_sys.executable, "-m", "engine.runner_worker",
                           config_path],
                          stdout=log_fh,
                          stderr=_subprocess.STDOUT,
                          start_new_session=True,
                          close_fds=True,
                          cwd=_os.path.dirname(STORAGE_ROOT) or None,
                      )
                      log.info(
                          "automation: dispatched worker pid=%s config=%s "
                          "items=%d envs=%s",
                          _proc.pid, config_id, len(automation_items),
                          env_types,
                      )
                      # Store the run_id in session so the GET handler
                      # can render a polling status widget on the same
                      # page. Operator-reported UX bug: the previous
                      # iteration redirected to /test-execution/live,
                      # which conflicted with the existing "Open live
                      # view in new tab" button (operator opened the
                      # tab manually, then the form-tab also navigated
                      # away — duplicate live-view tabs). Now the
                      # form stays put and shows progress in-place;
                      # operator clicks Open-in-new-tab when they
                      # actually want frame-by-frame view.
                      session["active_automation_run"] = {
                          "run_id": config_id,
                          "case_count": len(automation_items),
                          "started_at": datetime.now().isoformat(),
                      }
                      flash(
                          f"✓ Playwright pass dispatched "
                          f"({len(automation_items)} case(s)). The form "
                          f"will show live progress; results auto-import "
                          f"when the worker is done. Use the "
                          f"\"Open live view in new tab\" button if you "
                          f"want frame-by-frame view.",
                          "success",
                      )
                      return redirect(url_for("test_execution_page"))
                  except Exception as exc:
                      log.exception("Automation dispatch failed: %s", exc)
                      # Stamp the phase + reason into info.json so the
                      # /test-execution/diag endpoint shows the operator
                      # exactly what blew up — without forcing them to
                      # dig through Render logs.
                      try:
                          # ``os`` is imported here, beside the others.
                          # It was the one name this block never bound: the
                          # module imports os locally as ``_os`` wherever it
                          # needs it, and these two lines reached for a bare
                          # ``os`` that does not exist at module scope. Every
                          # call raised NameError, the bare ``except`` below
                          # swallowed it, and the info.json this block exists
                          # to write was never written once — so
                          # /test-execution/diag had nothing to show an
                          # operator about a dispatch that had just failed,
                          # which is the situation it was built for. The
                          # comment four lines above this one, in this same
                          # file, warns about exactly this shape.
                          import os
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
                                  "mode": run_mode,
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
                      # Opt-in: file the site-wide Performance / Security /
                      # Accessibility findings the pack never asked about.
                      # Off unless the operator ticked it, because 20+
                      # unrequested findings on top of a run's own results
                      # is how a bug list stops being read.
                      site_sweep=(request.form.get("site_sweep") == "1"),
                  )

                  # Promote pending bug IDs and stamp ISTQB metadata.
                  # Single-pass allocator: build the "all bugs so far" list
                  # exactly once, then increment locally — avoids O(N²)
                  # rebuild on every new bug for large runs.
                  bug_id_map: dict[str, str] = {}
                  # item_id -> execution_case_result.bug_report_id. _persist_bug has
                  # always returned the row id and this code has always thrown it
                  # away, so the FK was never populated: "which bug did this failed
                  # item file" was answerable only from the session, and unanswerable
                  # once the session was gone. E3.4 made it visible by reading runs
                  # back from the database.
                  bug_row_ids: dict[str, int] = {}
                  running_bugs = list(existing_bugs) + [
                      dict_to_bug(b) for b in all_bugs[len(existing_bugs):]
                  ]
                  # Dedupe carbon-copy bugs (same defect_class +
                  # final_url) so a 62-TC failure run produces 3-5
                  # actionable bugs instead of 62 identical ones.
                  # Operator-reported on 2026-05-05.
                  dedupe_bugs_by_root_cause(execution, automation_assets)
                  _bug_aliases = execution.get("_bug_alias") or {}
                  for bug_dict in execution["bugs"]:
                      new_id = generate_bug_id(running_bugs)
                      bug_dict["id"] = new_id
                      if not bug_dict.get("affects_version"):
                          bug_dict["affects_version"] = affects_version
                      bug_dict["environment"] = environment
                      bug_id_map[bug_dict.get("linked_item_id", "")] = new_id
                      # Propagate the alias map: every TC that was
                      # merged into this bug should land on the same
                      # bug_id when the per-env loop rewrites
                      # __pending_X tokens further down.
                      for alias_tc, primary_tc in _bug_aliases.items():
                          if primary_tc == bug_dict.get(
                                  "linked_item_id", ""):
                              bug_id_map[alias_tc] = new_id
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
                      _row_id = _persist_bug(bug_dict, source="execution",
                                             run_id=db_run_id)
                      if _row_id:
                          bug_row_ids[bug_dict.get("linked_item_id", "")] = _row_id
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
                                  bug_report_id=bug_row_ids.get(
                                      r.get("item_id")),
                                  source=r.get("source"),
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
              mirror_pack("bug_reports_data", all_bugs)
              mirror_pack("test_runs", test_runs)

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
            b.get("id") for b in pack_bugs() if b.get("id")
        ]
        # An "active_automation_run" session entry — set by the POST
        # handler when a Playwright pass is dispatched — drives the
        # in-page progress widget. We auto-clear it once the worker is
        # done (the results endpoint pops it on import); leftover
        # entries from older runs are pruned here too so a stale entry
        # doesn't render a permanent banner.
        active_run = session.get("active_automation_run")
        if active_run:
            try:
                import os as _os
                from routes.automation import STORAGE_ROOT as _SR
                rid = active_run.get("run_id", "")
                done = _os.path.isfile(_os.path.join(
                    _SR, "automation_runs", "_pending",
                    f"{rid}.done.flag"))
                # Pruning rule: if the done.flag exists AND the
                # results endpoint has already cleaned the pending
                # files (no .json), the run is fully imported — drop.
                cfg_exists = _os.path.isfile(_os.path.join(
                    _SR, "automation_runs", "_pending", f"{rid}.json"))
                if done and not cfg_exists:
                    session.pop("active_automation_run", None)
                    active_run = None
            except Exception:
                pass
        # Manual walks that were started and never closed. The state to
        # resume one has been in the database since the walk was built, but
        # nothing listed it — so an interrupted run was reachable only from
        # browser history, and resumable-but-unfindable is not resumable.
        # Scoped to the active project, which is also the isolation
        # boundary the run pages enforce.
        # Who a walk can be handed to. Empty unless authentication is on and
        # the caller is an admin — with auth off there is no identity to
        # assign to, and a tester cannot assign work to a colleague.
        assignee_options = []
        try:
            from engine import permissions as _perm_mod
            if _perm_mod.auth_active() and _perm_mod.is_admin():
                _org = _perm_mod.current_org_id()
                if _org:
                    assignee_options = [
                        m for m in _db.list_org_members(_org)
                        if m.get("is_active")]
        except Exception as exc:  # pragma: no cover — best-effort
            log.warning("assignee options lookup failed: %s", exc)

        open_manual_runs = []
        _pid = ensure_active_project()
        if _pid:
            # Deliberately not wrapped in a broad try/except. The first
            # version was, and it turned a NameError in this very block into
            # a logged warning and an empty list — the page rendered fine
            # and the feature was simply absent. A best-effort catch around
            # code that has never worked once hides the bug instead of
            # surviving it.
            open_manual_runs = _db.list_open_runs(_pid, mode="manual", limit=5)

        return render_template("test_execution.html",
                               open_manual_runs=open_manual_runs,
                               assignee_options=assignee_options,
                               has_tc_data=has_tc, has_cl_data=has_cl,
                               tc_count=len(tc_data), cl_count=len(cl_data),
                               tc_items=tc_data, cl_items=cl_data,
                               test_runs=test_runs,
                               resource_urls=resource_urls,
                               active_automation_run=active_run,
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
        tc_data = pack_test_cases()
        cl_data = pack_checklist()

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
                g.t.get("te_auto_run_failed", "Auto-run failed: %(error)s")
                % {"error": type(exc).__name__},
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
        test_runs = pack_runs()
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
        mirror_pack("test_runs", test_runs)

        # Bug reports also flow into the session so the Bug Reports
        # page picks them up. Mirrors the normal POST handler's
        # treatment without the per-bug ISTQB stamping (auto-run is a
        # quick smoke, not a release-grade run).
        bugs_session = pack_bugs()
        for bug_dict in execution.get("bugs", []) or []:
            new_id = generate_bug_id([dict_to_bug(b) for b in bugs_session])
            bug_dict["id"] = new_id
            bug_dict.setdefault("environment", environment)
            bugs_session.append(bug_dict)
        mirror_pack("bug_reports_data", bugs_session)

        stats = run_record["stats"] or {}
        passed = stats.get("passed", 0)
        failed = stats.get("failed", 0)
        blocked = stats.get("blocked", 0)
        # Placeholders, not an f-string. An f-string fallback holds the
        # numbers already, so the day the key reached a dictionary the
        # message would have kept its words and lost its figures — which
        # is the whole content of this one.
        flash(
            g.t.get("te_auto_run_done",
                    "Auto-run finished: %(passed)d passed, %(failed)d "
                    "failed, %(blocked)d blocked.")
            % {"passed": passed, "failed": failed, "blocked": blocked},
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
            entries = [e for e in os.listdir(runs_dir) if e != "_live"
                       and e != "_pending"]
            out["recent_runs"] = sorted(entries)[-5:]
        else:
            out["recent_runs"] = []
        # _pending dir tells us whether a run was ever DISPATCHED — the
        # run_dir's existence tells us whether it ever STARTED writing
        # artifacts. The two answers are different and both matter for
        # debugging: a config in _pending without a started.flag means
        # the worker subprocess never spawned at all (Render restart
        # killed the dispatch before fork).
        pending_dir = os.path.join(runs_dir, "_pending")
        out["pending_dir_exists"] = os.path.isdir(pending_dir)
        if out["pending_dir_exists"]:
            try:
                pending_items = []
                import time as _time
                for fn in sorted(os.listdir(pending_dir)):
                    if fn.endswith(".json") and not fn.endswith(".result.json"):
                        rid = os.path.splitext(fn)[0]
                        pending_items.append({
                            "run_id": rid,
                            "started": os.path.isfile(
                                os.path.join(pending_dir, f"{rid}.started.flag")),
                            "done": os.path.isfile(
                                os.path.join(pending_dir, f"{rid}.done.flag")),
                            "has_result": os.path.isfile(
                                os.path.join(pending_dir, f"{rid}.result.json")),
                            "age_s": int(_time.time() - os.path.getmtime(
                                os.path.join(pending_dir, fn))),
                        })
                out["pending_runs"] = pending_items[-5:]
            except Exception as exc:
                out["pending_dir_error"] = str(exc)

        # Session pack — the "what's in front of the user right now".
        # Operator-reported on 2026-05-05: log showed session_test_cases
        # = 0 and the user got stuck on /test-execution/live without
        # any dispatch happening, because the form was empty.
        out["session_test_cases"] = len(pack_test_cases())
        out["session_checklist"]  = len(pack_checklist())
        out["session_active_run"] = (session.get(
            "active_automation_run") or {}).get("run_id", "")

        # Project context — without this, "session_test_cases: 0" is
        # ambiguous. Surface the active project + how many TCs it has
        # in the DB. Helps distinguish "no project" from "wrong project
        # selected" from "project has no TCs yet".
        out["session_project_id"] = session.get("project_id") or ""
        out["session_project_name"] = (
            session.get("project_setup") or {}).get("project_name", "")
        try:
            from engine import db as _db
            from routes._shared import get_session_id as _gsid
            sid = _gsid(session)
            out["session_id_short"] = (sid or "")[:8]
            if hasattr(_db, "list_projects"):
                owned = _db.list_projects(owner_sid=sid) or []
                out["projects_for_owner"] = [
                    {"id": (p.get("id") or "")[:8] + "…",
                     "name": p.get("name", ""),
                     "tc_count": p.get("test_cases_count", 0),
                     "cl_count": p.get("checklist_count", 0),
                     "bug_count": p.get("bug_count", 0)}
                    for p in owned[:10]
                ]
            pid = session.get("project_id")
            if pid and hasattr(_db, "load_test_cases"):
                out["active_project_db_tc_count"] = len(
                    _db.load_test_cases(pid) or [])
            if pid and hasattr(_db, "load_checklist"):
                out["active_project_db_cl_count"] = len(
                    _db.load_checklist(pid) or [])
        except Exception as exc:
            out["project_context_error"] = str(exc)[:200]

        # Actionable next-step suggestion so the operator can act on
        # the diag without parsing the whole blob.
        if out["session_test_cases"] == 0 and out["session_checklist"] == 0:
            if out.get("active_project_db_tc_count", 0) > 0:
                out["suggested_action"] = (
                    "Active project has test cases in the DB but not in "
                    "session. Visit /projects/db/select/<id> for the "
                    "active project to rehydrate, or click the picker's "
                    "Switch button.")
            elif out.get("projects_for_owner"):
                out["suggested_action"] = (
                    "No test cases in the active project. Switch to a "
                    "project that has TCs (see projects_for_owner) or "
                    "generate fresh ones at /test-cases.")
            else:
                out["suggested_action"] = (
                    "No projects, no test cases. Start at /test-cases "
                    "to generate, or /estimation to scope first.")
        elif not out.get("recent_runs") and not out.get("pending_runs"):
            out["suggested_action"] = (
                f"You have {out['session_test_cases']} TCs in session "
                "but no runs have been dispatched yet. Go to "
                "/test-execution and click 'Run Test Execution'.")

        return jsonify(out)

    @app.route("/test-execution/run-status/<run_id>")
    def test_execution_run_status(run_id):
        """Polled by /test-execution/live to know when the detached
        worker has finished. Returns:
          status: "queued" | "running" | "stalled" | "done" | "failed"
          error:  populated when status in ("failed","stalled")

        "stalled" means the subprocess was started but the live
        ``info.json`` hasn't been touched for >120 s. The most common
        cause on Render free tier is the OS OOM-killing Chromium when
        the dyno's 512 MB ceiling is hit — without this status the
        live view would poll forever on a dead process.
        """
        import os, json, time
        from routes.automation import STORAGE_ROOT
        # Reject path-traversal payloads.
        if not run_id.replace("_", "").replace("-", "").isalnum():
            return jsonify({"status": "missing"}), 400
        pending_dir = os.path.join(STORAGE_ROOT, "automation_runs", "_pending")
        done_path = os.path.join(pending_dir, f"{run_id}.done.flag")
        result_path = os.path.join(pending_dir, f"{run_id}.result.json")
        started_path = os.path.join(pending_dir, f"{run_id}.started.flag")
        error_path = os.path.join(pending_dir, f"{run_id}.error.flag")
        # Detached worker writes ``error.flag`` from its SIGTERM/SIGINT
        # handler before touching ``done.flag``. When both exist, the
        # worker was killed mid-run — surface "terminated" so the UI
        # stops spinning instead of waiting for the 120 s stall timer.
        if os.path.isfile(error_path) and os.path.isfile(done_path):
            try:
                with open(error_path, "r", encoding="utf-8") as f:
                    reason = f.read(512)[:500]
            except Exception as exc:
                reason = f"<unreadable error.flag: {exc}>"
            return jsonify({"status": "terminated", "error": reason})
        if os.path.isfile(done_path) and os.path.isfile(result_path):
            try:
                with open(result_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                return jsonify({
                    "status": payload.get("status", "done"),
                    "error":  payload.get("error", ""),
                })
            except Exception as exc:
                return jsonify({"status": "failed", "error": str(exc)})
        if os.path.isfile(started_path):
            # Stall detection — the runner pings _live/info.json every
            # _live_pump (default ~200 ms) and on every per-step
            # screenshot. If the latest ts is older than 120 s, the
            # subprocess is almost certainly dead. Surface as "stalled"
            # so the UI can stop spinning.
            # Only when the run holding the live directory is this
            # caller's. It is one directory per instance, so reading it
            # unconditionally meant another organisation's heartbeat decided
            # whether *this* run looked alive — and the message below quoted
            # their phase and case counters to whoever polled. An empty
            # answer falls through to "running", which is what this route
            # already says when no run is live at all; see
            # ``routes.execution_live.live_info_if_mine`` for what that
            # costs and what the durable fix is.
            from routes.execution_live import live_info_if_mine
            try:
                live = live_info_if_mine(STORAGE_ROOT)
                if live:
                    ts = int(live.get("ts") or 0) / 1000.0
                    age = time.time() - ts if ts else 0
                    if ts and age > 120:
                        return jsonify({
                            "status": "stalled",
                            "error": (
                                f"Worker has not updated info.json for "
                                f"{int(age)} s — the subprocess is most "
                                f"likely dead (OOM-kill on free tier is "
                                f"the common cause). Run id: {run_id}. "
                                f"Last known phase: "
                                f"{live.get('phase', 'unknown')}; "
                                f"cases done: "
                                f"{live.get('cases_done', '?')}/"
                                f"{live.get('cases_total', '?')}."
                            ),
                            "cases_done": live.get("cases_done", 0),
                            "cases_total": live.get("cases_total", 0),
                            "phase": live.get("phase", ""),
                        })
            except Exception as exc:
                log.debug("run-status: stall check failed: %s", exc)
            return jsonify({"status": "running"})
        return jsonify({"status": "queued"})



__all__ = ["register"]
