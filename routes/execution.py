"""TestFortge — Manual test execution + bug report routes.

  * GET/POST /test-execution                 — configure and run test items
  * POST     /test-execution/generate-account — throw-away test account
  * POST     /create-bug-report              — create a bug entry
  * GET      /bug-reports                    — list and manage bugs
  * GET      /export-bug-reports             — markdown export
"""

from __future__ import annotations

from datetime import datetime

from flask import (Flask, Response, current_app, flash, g, jsonify, redirect,
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
    BugReport, BUG_SEVERITIES, BUG_PRIORITIES, BUG_STATUSES, BUG_FREQUENCIES,
    generate_bug_id, bug_to_dict, dict_to_bug,
    export_bug_report_markdown,
)
from engine.test_credentials import (
    credentials_from_form, credentials_from_session, credentials_to_session,
    generate_test_account,
)

from engine import db as _db

from ._shared import extract_resource_urls, ensure_active_project, get_session_id
from .projects import _require_project_owner

log = get_logger(__name__)


def _bug_row_to_session_dict(row: dict) -> dict:
    """Convert a row returned by :func:`engine.db.list_bugs` into the
    session-flat shape :func:`dict_to_bug` consumes.

    Three notable mappings:
      * ``row.id`` (int DB row id) → ``db_id`` on the session BugReport
      * ``row.external_id`` (e.g. ``"BUG-001"``) → display ``id``
      * ``row.extra.assignee`` → first-class ``assignee`` field so the
        existing template renders it without an attribute lookup hack

    Anything in ``row.extra`` that ``BugReport`` already knows about
    (``frequency``, ``component``, etc.) is unpacked too so the JSON
    blob doesn't shadow first-class fields after a re-render.
    """
    extra = row.get("extra") or {}
    out = {
        "id":                 row.get("external_id") or f"BUG-{int(row.get('id') or 0):03d}",
        "db_id":              int(row.get("id") or 0),
        "title":              row.get("title") or "",
        "severity":           row.get("severity") or "Minor",
        "priority":           row.get("priority") or "Medium",
        "status":             row.get("status") or "Open",
        "environment":        row.get("environment") or "",
        "preconditions":      extra.get("preconditions", ""),
        "steps_to_reproduce": row.get("steps_to_reproduce") or "",
        "actual_result":      row.get("actual_result") or "",
        "expected_result":    row.get("expected_result") or "",
        "frequency":          extra.get("frequency", "Always"),
        "affects_version":    row.get("version") or "",
        "found_in_build":     extra.get("found_in_build", ""),
        "attachments":        extra.get("attachments") or [],
        "linked_item_id":     extra.get("linked_item_id", ""),
        "linked_item_type":   extra.get("linked_item_type", ""),
        "reporter":           row.get("reporter") or "",
        "assignee":           extra.get("assignee", ""),
        "created_at":         (row.get("created_at") or "")
                                if isinstance(row.get("created_at"), str)
                                else (row.get("created_at").isoformat()
                                      if row.get("created_at") else ""),
        "component":          extra.get("component", ""),
        "labels":             extra.get("labels") or [],
        "comment":            row.get("comment") or "",
    }
    return out


def _hydrate_bugs(project_id: str | None) -> list:
    """Return the list of ``BugReport`` dataclass instances to render
    on ``/bug-reports``. DB-first so ``db_id`` is populated; falls
    back to session ``bug_reports_data`` only when the DB read fails
    or no project is active yet (legacy / pre-project flow).
    """
    if project_id:
        try:
            rows = _db.list_bugs(project_id) or []
        except Exception as exc:  # pragma: no cover — DB outage shouldn't 500
            log.warning("hydrate_bugs: list_bugs failed: %s", exc)
            rows = []
        if rows:
            # DB list_bugs is ordered desc by created_at; the template
            # has always shown newest-first, so we keep that order.
            return [dict_to_bug(_bug_row_to_session_dict(r)) for r in rows]
    # Session fallback — older flow that wrote bug_reports_data directly.
    bugs_data = session.get("bug_reports_data", []) or []
    return [dict_to_bug(b) for b in bugs_data]


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



def _dedupe_bugs_by_root_cause(execution: dict,
                                automation_assets: dict) -> None:
    """Group bugs that share the same root cause so the operator
    triages 3-5 unique defects instead of 60+ carbon copies.

    Operator-reported on 2026-05-05: a 62-TC run produced 62 bug
    reports, mostly identical "Locator.click: Timeout" messages on
    the same URL. Dedup key is ``(defect_class, final_url)`` —
    bugs sharing both fields collapse into a single primary bug
    whose ``linked_test_cases`` list holds every affected TC.

    Side effect: writes ``execution["_bug_alias"]`` mapping every
    merged TC's ``linked_item_id`` -> the primary's. The per-env
    loop downstream uses it to keep result rows pointed at the
    consolidated bug instead of the now-removed dupes.
    """
    bugs = execution.get("bugs") or []
    if len(bugs) < 2:
        return
    try:
        from engine.bug_template import classify_error
    except Exception:
        # Without classify_error the dedup key collapses to "unknown",
        # which would over-merge. Skip dedup defensively.
        return

    groups: dict[tuple, list[int]] = {}
    for i, b in enumerate(bugs):
        linked = b.get("linked_item_id", "")
        ev = automation_assets.get(linked) if linked else None
        if not ev:
            # No Playwright evidence — keep as its own group so
            # simulator-only bugs don't get over-merged.
            key = ("__no_ev__", str(i))
        else:
            fstep = ev.get("failure_step") or {}
            comment = (fstep.get("comment") or "").strip()
            defect = classify_error(comment) if comment else "unknown"
            url = (ev.get("final_url") or "").strip()
            # Truncate URL to path-only so query strings don't split
            # otherwise-identical bugs across pages.
            try:
                from urllib.parse import urlparse
                parsed = urlparse(url)
                url_key = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            except Exception:
                url_key = url
            key = (defect, url_key)
        groups.setdefault(key, []).append(i)

    new_bugs: list[dict] = []
    aliases: dict[str, str] = {}
    for (defect_key, url_key), indices in groups.items():
        primary = bugs[indices[0]]
        merged_tcs: list[str] = []
        for j in indices[1:]:
            merged_linked = bugs[j].get("linked_item_id", "")
            if merged_linked:
                merged_tcs.append(merged_linked)
                aliases[merged_linked] = primary.get(
                    "linked_item_id", "")
        if merged_tcs:
            primary_linked = primary.get("linked_item_id", "") or ""
            all_tcs = [primary_linked] + merged_tcs
            primary["linked_test_cases"] = [t for t in all_tcs if t]
            count = len(primary["linked_test_cases"])
            existing_title = (primary.get("title") or "").rstrip()
            primary["title"] = (
                f"{existing_title} — affects {count} test cases")
            existing_comment = primary.get("comment") or ""
            note = (
                f"Same root cause was observed across {count} test "
                f"cases: {', '.join(primary['linked_test_cases'])}. "
                f"Fixing the primary should resolve all of them — "
                f"verify each linked TC after the fix."
            )
            primary["comment"] = (existing_comment + "\n\n" + note).strip()
        new_bugs.append(primary)

    execution["bugs"] = new_bugs
    execution["_bug_alias"] = aliases


def _reconstruct_partial_payload(run_id: str, config_path: str,
                                  storage_root: str,
                                  live: dict) -> dict | None:
    """Best-effort partial-results reconstructor.

    Used when the worker died before writing result.json (typically an
    OOM-kill on Render free tier). Walks the run's on-disk artifacts:

      * ``<storage>/automation_runs/<run_id>/<TC>/step_NN_after.png``
      * ``<storage>/automation_runs/<run_id>/<TC>/step_NN_failure.png``

    and reconstructs an ``automation_assets`` dict in the same shape
    the worker would have produced. Returns ``None`` when the config
    file is missing too (no signal at all to work with).

    Status heuristic: a TC directory containing ANY ``*_failure.png``
    is treated as ``failed``; otherwise — if the directory has
    screenshots — ``passed``. Cases that never produced a directory
    are simply absent from the returned assets dict, which the
    per-env loop interprets as "no automation evidence available".
    """
    import os, json, glob
    if not os.path.isfile(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except Exception:
        return None
    runs_root = os.path.join(storage_root, "automation_runs")
    # Find the actual run directory the worker created. The runner
    # stamps its own timestamp+uuid run_id distinct from the config_id
    # we use for dispatch tracking, so we look for the most recent
    # directory whose mtime is >= the config file's.
    run_dirs = []
    try:
        cfg_mtime = os.path.getmtime(config_path)
        for entry in os.listdir(runs_root):
            if entry in ("_live", "_pending"):
                continue
            p = os.path.join(runs_root, entry)
            if os.path.isdir(p) and os.path.getmtime(p) >= cfg_mtime - 60:
                run_dirs.append((p, entry))
    except OSError:
        pass
    if not run_dirs:
        return None
    # Pick the most recent directory (the worker's actual run).
    run_dirs.sort(key=lambda t: os.path.getmtime(t[0]), reverse=True)
    run_dir, runner_run_id = run_dirs[0]
    automation_assets: dict = {}
    passed = failed = blocked = 0
    try:
        for tc_id in sorted(os.listdir(run_dir)):
            tc_dir = os.path.join(run_dir, tc_id)
            if not os.path.isdir(tc_dir):
                continue
            shots: list[str] = []
            fail_shots: list[str] = []
            failure_step: dict | None = None
            prev_after = ""
            for fname in sorted(os.listdir(tc_dir)):
                if fname.startswith("step_") and fname.endswith("_after.png"):
                    rel = os.path.relpath(
                        os.path.join(tc_dir, fname), storage_root
                    ).replace(os.sep, "/")
                    if os.path.getsize(os.path.join(tc_dir, fname)) > 0:
                        shots.append(rel)
                        prev_after = rel
                elif fname.startswith("step_") and fname.endswith("_failure.png"):
                    rel = os.path.relpath(
                        os.path.join(tc_dir, fname), storage_root
                    ).replace(os.sep, "/")
                    if os.path.getsize(os.path.join(tc_dir, fname)) > 0:
                        fail_shots.append(rel)
                        if failure_step is None:
                            # Step index encoded as step_NN_*
                            try:
                                idx = int(fname.split("_")[1])
                            except (ValueError, IndexError):
                                idx = 0
                            failure_step = {
                                "index": idx,
                                "action": "",
                                "comment": "Worker died before this step "
                                            "could be reported. "
                                            "Failure screenshot exists "
                                            "on disk; details unavailable.",
                                "screenshot": rel,
                                "context_screenshot": prev_after,
                                "console_errors": [],
                            }
            if not shots and not fail_shots:
                continue
            status = "failed" if fail_shots else "passed"
            if status == "passed":
                passed += 1
            else:
                failed += 1
            automation_assets[tc_id] = {
                "status": status,
                "video": "",
                "screenshots": shots,
                "failure_screenshots": fail_shots,
                "failure_step": failure_step,
                "final_url": "",
                "duration_ms": 0,
            }
    except Exception as exc:
        # Catch the full superclass — OSError + PermissionError +
        # anything else the directory walk surfaces. Return None so
        # the caller flashes the friendly "no salvage possible" path.
        log.warning("partial-payload reconstruction failed: %s", exc)
        return None
    if not automation_assets:
        return None
    report = {
        "run_id": runner_run_id,
        "started_at": "",
        "finished_at": "",
        "base_url": cfg.get("base_url", ""),
        "headless": bool(cfg.get("headless", True)),
        "total": passed + failed + blocked,
        "passed": passed,
        "failed": failed,
        "blocked": blocked,
        "duration_ms": 0,
        "scripts": [],
    }
    return {
        "status": "partial",
        "config_id": run_id,
        "report": report,
        "automation_assets": automation_assets,
        "config_echo": cfg,
        "finished_at": "",
        "_partial_reason": (
            f"Worker died after {len(automation_assets)} case(s); "
            f"reconstructed from on-disk artifacts at {run_dir}."
        ),
    }


def _reconcile_with_automation(execution: dict,
                                automation_assets: dict,
                                env_type: str) -> None:
    """Mutate ``execution`` so the simulator's verdict and bug list
    are reconciled with Playwright's actual observations.

    Operator-reported architectural smell (2026-05-04): the simulator
    in :func:`engine.qa_testers.execute_items` produces a verdict
    independent of what Playwright sees. When automation actually ran,
    Playwright is the authoritative source — its screenshots are the
    evidence the user is looking at. Without reconciliation a TC can
    show "Failed" with a bug whose description doesn't match the
    attached screenshot, because:

      * Simulator decided Failed for one reason (e.g. heuristic match
        on the TC summary), and
      * Playwright actually Passed (or failed for a different reason).

    Reconciliation rules (applied only for Web/Mobile-Web envs where
    Playwright actually drove the browser):

      1. If Playwright's per-TC status is **passed** → override the
         simulator verdict to Passed and drop any bug the simulator
         created for that TC. The page worked; there's nothing to
         report.

      2. If Playwright's status is **failed/blocked** → keep / promote
         to that status, mark the existing bug for rewrite (the bug-
         template will replace its content with the actual Playwright
         failure context). If the simulator said Passed but Playwright
         failed, mint a synthetic bug placeholder so the
         downstream bug-rewrite produces real content.

      3. Stats (passed/failed/blocked totals + pass_rate) are
         recomputed after the override so the UI matches the bug
         list.

    No-op for non-web envs (iOS/Android natives don't run through
    Playwright). Mutates execution in place.
    """
    if env_type not in ("web", "mobile_web"):
        return
    if not automation_assets:
        return
    results = execution.get("results") or []
    bugs = execution.get("bugs") or []
    bugs_by_item = {b.get("linked_item_id"): b for b in bugs}
    new_bugs: list[dict] = []
    drop_item_ids: set[str] = set()
    promote_to_failed: dict[str, str] = {}  # item_id -> "Failed"/"Blocked"

    # Status mapping: runner uses lowercase, simulator uses Title.
    runner_to_sim = {
        "passed":  "Passed",
        "failed":  "Failed",
        "blocked": "Blocked",
    }

    for r in results:
        item_id = r.get("item_id") or ""
        ev = automation_assets.get(item_id) if item_id else None
        if not ev:
            continue
        runner_status_raw = (ev.get("status") or "").lower()
        runner_status = runner_to_sim.get(runner_status_raw)
        if not runner_status:
            continue
        sim_status = r.get("status") or ""
        if runner_status == sim_status:
            continue  # already aligned, no work
        if runner_status == "Passed":
            # Drop the bug if simulator created one — page worked fine.
            if sim_status in ("Failed", "Blocked"):
                drop_item_ids.add(item_id)
            r["status"] = "Passed"
            r["comment"] = (
                "Playwright observed the scenario passing — overrode "
                "simulator verdict.")
            r["source"] = "real_check"
            # Clear any pending bug reference.
            if r.get("bug_id", "").startswith("__pending_"):
                r["bug_id"] = ""
            # Audit fix (2026-05-04): Wipe failure_step + failure
            # screenshots from the asset bucket so the per-env
            # decoration loop downstream doesn't render the (now
            # orphaned) annotated shot next to a Passed status.
            ev["failure_step"] = None
            ev["failure_screenshots"] = []
        else:
            # Playwright says Failed/Blocked but simulator said
            # otherwise. Promote the verdict; ensure a bug exists.
            r["status"] = runner_status
            promote_to_failed[item_id] = runner_status
            fstep = ev.get("failure_step") or {}
            comment = (fstep.get("comment") or "").strip()
            if comment:
                r["comment"] = comment
            r["source"] = "real_check"
            if item_id not in bugs_by_item:
                # Synthesize a placeholder; bug_template.rewrite will
                # fill it in with proper title/STR/AR/ER from the
                # Playwright failure context downstream.
                placeholder = {
                    "id": "",
                    "title": f"{item_id} — automated failure",
                    "severity": "Major",
                    "priority": "High",
                    "status": "Open",
                    "environment": "",
                    "preconditions": "",
                    "steps_to_reproduce": "",
                    "actual_result": comment or "Playwright run failed.",
                    "expected_result": "",
                    "frequency": "Always",
                    "affects_version": "",
                    "found_in_build": "",
                    "attachments": [],
                    "linked_item_id": item_id,
                    "linked_item_type": r.get("item_type", "test_case"),
                    "reporter": r.get("tester_name", ""),
                    "assignee": "",
                    "created_at": r.get("timestamp", ""),
                    "component": "",
                    "labels": [r.get("item_type", "test_case"), "auto-synthesized"],
                    "comment": comment,
                }
                new_bugs.append(placeholder)
                # Tag the result with a pending marker the per-env
                # loop later replaces with the assigned bug ID.
                r["bug_id"] = f"__pending_synth_{item_id}"

    # Drop simulator bugs for TCs Playwright passed.
    if drop_item_ids:
        execution["bugs"] = [
            b for b in bugs
            if b.get("linked_item_id") not in drop_item_ids
        ]
        bugs = execution["bugs"]
    # Add synth bugs for Playwright-only failures.
    if new_bugs:
        execution["bugs"] = list(bugs) + new_bugs
    # Recompute stats from the reconciled results.
    passed = sum(1 for r in results if r.get("status") == "Passed")
    failed = sum(1 for r in results if r.get("status") == "Failed")
    blocked = sum(1 for r in results if r.get("status") == "Blocked")
    total = passed + failed + blocked
    execution["stats"] = {
        "total": total,
        "passed": passed,
        "failed": failed,
        "blocked": blocked,
        "pass_rate": round(passed / total * 100, 1) if total else 0,
        "sources": (execution.get("stats") or {}).get("sources") or {},
        "site_url": (execution.get("stats") or {}).get("site_url") or "",
        "reconciled_with_automation": True,
    }


def _maybe_restore_pack_from_db() -> None:
    """If the session has no TC / CL pack but the active project does,
    rehydrate the session keys from the DB so the run page shows what
    the user uploaded earlier (Phase 2 persistence).

    No-op when:
      * session already carries a pack (avoid clobbering);
      * neither session nor owner_sid resolves a project;
      * DB read raises or returns empty.

    Recovery path (added 2026-05-04 after operator reported their
    project + TC pack vanished after a Render dyno restart): when
    ``session["project_id"]`` is missing, fall through to
    :func:`ensure_active_project` which now consults
    ``list_projects(owner_sid=...)`` before auto-creating. That re-pins
    the original project and lets us load its pack from Postgres.

    Best-effort — never raises. Failures are debug-logged.
    """
    if session.get("test_cases_data") or session.get("checklist_data"):
        return
    pid = session.get("project_id")
    candidate_pids: list[str] = []
    if pid:
        candidate_pids.append(pid)
    # Owner_sid-wide search ALWAYS runs (even when session.project_id
    # exists) — operator-reported case 2026-05-04: their session pinned
    # a freshly-created "Untitled project" that had no TCs while the
    # actual generated pack lived under a different project for the
    # same owner_sid. We try the active one first, then fall back to
    # other owned projects (most-recent first, with non-zero counts
    # preferred) until we find one with content.
    try:
        from engine import db as _db
        from routes._shared import get_session_id
        sid = get_session_id(session)
        if hasattr(_db, "list_projects"):
            existing = _db.list_projects(owner_sid=sid) or []
            # Sort by has-content first, then by recency. list_projects
            # already returns updated_at desc, so a stable sort that
            # ranks projects with TCs above empty ones gives us the
            # right order.
            ranked = sorted(
                existing,
                key=lambda p: (
                    -(int(p.get("test_cases_count", 0) or 0)
                       + int(p.get("checklist_count", 0) or 0)),
                ),
            )
            for p in ranked:
                p_id = p.get("id") if isinstance(p, dict) else None
                if p_id and p_id not in candidate_pids:
                    candidate_pids.append(p_id)
    except Exception as exc:
        log.debug("restore: owner_sid project lookup failed: %s", exc)
    if not candidate_pids:
        return
    chosen_pid = ""
    chosen_tc: list = []
    chosen_cl: list = []
    try:
        from engine import db as _db
        for cand in candidate_pids:
            try:
                tc = (_db.load_test_cases(cand)
                      if hasattr(_db, "load_test_cases") else [])
                cl = (_db.load_checklist(cand)
                      if hasattr(_db, "load_checklist") else [])
            except Exception as exc:
                log.debug("restore: load failed for %s: %s", cand, exc)
                continue
            if tc or cl:
                chosen_pid = cand
                chosen_tc = tc
                chosen_cl = cl
                break
    except Exception as exc:  # pragma: no cover
        log.debug("restore: db read failed: %s", exc)
        return
    if not chosen_pid:
        return
    if chosen_pid != session.get("project_id"):
        # Re-pin the active project to the one with content. Without
        # this, the next request would see project_id pointing at the
        # empty project again and recovery would loop indefinitely.
        session["project_id"] = chosen_pid
        log.info("restore: re-pinned active project to %s "
                 "(had %d TC + %d CL)",
                 chosen_pid, len(chosen_tc), len(chosen_cl))
    if chosen_tc:
        session["test_cases_data"] = chosen_tc
        log.info("restore: rehydrated %d test cases from project %s",
                 len(chosen_tc), chosen_pid)
    if chosen_cl:
        session["checklist_data"] = chosen_cl
        log.info("restore: rehydrated %d checklist items from project %s",
                 len(chosen_cl), chosen_pid)


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
              # PR-3: orthogonal Run Mode — "tc_driven" (default) keeps
              # every existing path byte-identical; "walkthrough" hands
              # the run to ``engine/walkthrough_runner.py`` so the QA
              # walkthrough's 8 heuristics + axe-core sweep produce
              # findings that get persisted as bugs (see results
              # endpoint below). The radio lives at the top of the form
              # so the field always posts; missing/invalid values fall
              # back to "tc_driven" for safety.
              run_mode = (request.form.get("run_mode") or "tc_driven").strip().lower()
              if run_mode not in ("tc_driven", "walkthrough"):
                  run_mode = "tc_driven"
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
                  # Dedupe carbon-copy bugs (same defect_class +
                  # final_url) so a 62-TC failure run produces 3-5
                  # actionable bugs instead of 62 identical ones.
                  # Operator-reported on 2026-05-05.
                  _dedupe_bugs_by_root_cause(execution, automation_assets)
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
        return render_template("test_execution.html",
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
        out["session_test_cases"] = len(session.get("test_cases_data") or [])
        out["session_checklist"]  = len(session.get("checklist_data") or [])
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

    @app.route("/test-execution/live", methods=["GET"])
    def test_execution_live():
        # When the user lands here without a ?run_id= query param (back-
        # button, bookmark, manual nav after a Render restart), surface
        # the most recent pending runs so they can pick up where they
        # left off — especially the case where the worker subprocess
        # is still chewing through cases but the browser tab lost the
        # query string.
        import os, glob, time, json
        from routes.automation import STORAGE_ROOT
        pending_dir = os.path.join(STORAGE_ROOT, "automation_runs", "_pending")
        live_info_path = os.path.join(STORAGE_ROOT, "automation_runs",
                                       "_live", "info.json")
        # Read live info.json once so per-run stall checks share its ts.
        live_ts = 0
        try:
            if os.path.isfile(live_info_path):
                with open(live_info_path, "r", encoding="utf-8") as f:
                    live_ts = int((json.load(f) or {}).get("ts", 0))
        except Exception:
            pass
        recent_runs: list[dict] = []
        try:
            if os.path.isdir(pending_dir):
                config_files = sorted(
                    glob.glob(os.path.join(pending_dir, "*.json")),
                    key=lambda p: os.path.getmtime(p),
                    reverse=True,
                )[:5]
                for cf in config_files:
                    rid = os.path.splitext(os.path.basename(cf))[0]
                    if rid.endswith(".result"):
                        # skip the result-file mirrors
                        continue
                    started = os.path.isfile(
                        os.path.join(pending_dir, f"{rid}.started.flag"))
                    done = os.path.isfile(
                        os.path.join(pending_dir, f"{rid}.done.flag"))
                    has_result = os.path.isfile(
                        os.path.join(pending_dir, f"{rid}.result.json"))
                    if done and has_result:
                        rstatus = "done"
                    elif started:
                        # Stall check — the worker pings _live/info.json
                        # constantly while running. >120 s with no ping
                        # almost always means OOM-kill on free tier.
                        # Without this the table claimed "running" for
                        # a 9-min-dead worker (operator-reported).
                        live_age = (time.time() - live_ts / 1000.0
                                     if live_ts else 9999)
                        rstatus = "stalled" if live_age > 120 else "running"
                    else:
                        rstatus = "queued"
                    recent_runs.append({
                        "run_id": rid,
                        "status": rstatus,
                        "started_at": int(os.path.getmtime(cf)),
                        "age_s": int(time.time() - os.path.getmtime(cf)),
                    })
        except Exception as exc:
            log.debug("recent_runs scan failed: %s", exc)
        return render_template("test_execution_live.html",
                               recent_runs=recent_runs)

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
            live_info = os.path.join(STORAGE_ROOT, "automation_runs",
                                      "_live", "info.json")
            try:
                if os.path.isfile(live_info):
                    with open(live_info, "r", encoding="utf-8") as f:
                        live = json.load(f) or {}
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

    @app.route("/test-execution/results/<run_id>", methods=["GET"])
    def test_execution_results(run_id):
        """Load the detached worker's result.json, run the per-env loop
        + bug rewrite + session writes (the FAST part of the original
        request), and render test_execution.html with the run visible.

        Idempotent: hitting this URL twice for the same run_id repeats
        the merge but doesn't double-bug because we delete the result
        file after a successful merge.
        """
        import os, json, glob, time
        from routes.automation import STORAGE_ROOT
        if not run_id.replace("_", "").replace("-", "").isalnum():
            flash("Invalid run id.", "error")
            return redirect(url_for("test_execution_page"))
        pending_dir = os.path.join(STORAGE_ROOT, "automation_runs", "_pending")
        result_path = os.path.join(pending_dir, f"{run_id}.result.json")
        config_path = os.path.join(pending_dir, f"{run_id}.json")
        # ── Stalled-run handling ──────────────────────────────────
        # If result.json never landed but the worker DID write some
        # screenshots before being OOM-killed, salvage what we can. The
        # config file persists alongside started.flag so we still know
        # what TC pack the user dispatched. Without this branch the
        # operator's only option after a crash is "your run is gone";
        # with it they at least see status for the cases that finished.
        if not os.path.isfile(result_path):
            live_info_path = os.path.join(STORAGE_ROOT, "automation_runs",
                                           "_live", "info.json")
            live = {}
            try:
                if os.path.isfile(live_info_path):
                    with open(live_info_path, "r", encoding="utf-8") as f:
                        live = json.load(f) or {}
            except Exception:
                live = {}
            live_age_s = (time.time() - (int(live.get("ts", 0)) / 1000.0)
                           if live.get("ts") else 9999)
            if live_age_s < 120 and os.path.isfile(
                    os.path.join(pending_dir, f"{run_id}.started.flag")):
                # Worker is still alive — bounce to live view.
                flash(
                    "Results not ready yet — the worker is still running. "
                    "Watch /test-execution/live and try again in a minute.",
                    "warning",
                )
                return redirect(
                    url_for("test_execution_live") + f"?run_id={run_id}")
            # Worker died. Try to reconstruct from on-disk artifacts.
            payload = _reconstruct_partial_payload(
                run_id, config_path, STORAGE_ROOT, live)
            if payload is None:
                flash(
                    "Run is missing a result file and no on-disk "
                    "artifacts could be salvaged. Open /test-execution/"
                    f"diag and check the worker log "
                    f"({run_id}.log) for the failure cause.",
                    "error",
                )
                return redirect(url_for("test_execution_page"))
            flash(
                f"Run did not finish cleanly (worker likely OOM-killed). "
                f"Showing partial results for "
                f"{len(payload.get('automation_assets') or {})} case(s) "
                f"that completed before the crash.",
                "warning",
            )
        else:
            try:
                with open(result_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception as exc:
                flash(f"Cannot read run results: {exc}", "error")
                return redirect(url_for("test_execution_page"))

        if payload.get("status") == "failed":
            flash(
                "Automation run failed: "
                f"{payload.get('error', 'unknown error')}. "
                "Open /test-execution/diag for details.",
                "error",
            )
            return redirect(url_for("test_execution_page"))

        report = payload.get("report") or {}
        automation_assets = payload.get("automation_assets") or {}
        cfg = payload.get("config_echo") or {}
        # PR-3: walkthrough findings ride alongside automation_assets.
        # The worker emits two parallel views — raw + deduped (per
        # ``walkthrough_dedup.fingerprint``); the deduped one is what
        # the operator sees in the UI and what gets converted to bugs.
        run_mode = (payload.get("mode") or cfg.get("mode") or "tc_driven")
        run_mode = str(run_mode).strip().lower() or "tc_driven"
        walkthrough_findings = list(
            payload.get("walkthrough_findings_deduped")
            or payload.get("walkthrough_findings")
            or []
        )
        walkthrough_tc_bindings = list(payload.get("walkthrough_tc_bindings") or [])

        # Re-run the post-processing using the same logic the synchronous
        # path used to run inline. Most fields come from the worker's
        # config echo (so the user's selection of envs / tester etc. is
        # honoured even though we're in a different request now).
        items_data = cfg.get("items_data") or session.get(
            "test_cases_data", []) or session.get("checklist_data", [])
        env_types = cfg.get("env_types") or ["web"]
        manual_statuses = cfg.get("manual_statuses") or {}
        manual_bug_refs = cfg.get("manual_bug_refs") or {}
        selected_ids = cfg.get("selected_ids") or []
        tester_id = cfg.get("tester_id") or "mid_1"
        tester_obj = get_tester(tester_id)
        tester_name = (tester_obj.name if tester_obj
                       else cfg.get("tester_name", tester_id))
        testing_types = cfg.get("testing_types") or ["Regression"]
        source = cfg.get("source") or "test_cases"
        item_type = cfg.get("item_type") or "test_case"
        site_url = cfg.get("site_url") or ""
        base_url = cfg.get("base_url") or ""
        headless = bool(cfg.get("headless", True))
        record_video = bool(cfg.get("record_video", False))
        affects_version = cfg.get("affects_version", "")

        existing_bugs = [
            dict_to_bug(b) for b in session.get("bug_reports_data", [])
            if b.get("id")
        ]
        all_bugs = list(session.get("bug_reports_data", []) or [])
        test_runs = list(session.get("test_runs", []) or [])

        run_summaries = []
        bug_total = 0
        envs_meta = cfg.get("envs") or {}
        # PR-3: the walkthrough runner runs once at a single viewport,
        # so its findings + bugs should be attached to exactly one
        # run_record — the first env in the iteration. Subsequent envs
        # get an empty findings list. This avoids inflating bug counts
        # when the operator ticks multiple env checkboxes for a
        # walkthrough run (which the UI warns against but doesn't
        # forbid).
        walkthrough_attached = False
        for et in env_types:
            environment = (envs_meta.get(et, {}) or {}).get("environment") \
                or et.title()
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
                        record_video=record_video,
                        base_url=base_url,
                    )
            except Exception as exc:
                log.warning("start_execution_run (results) failed: %s", exc)

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
            # Verdict reconciliation — for Web/Mobile-Web envs where
            # Playwright actually drove the browser, its observation
            # is the authoritative source. The simulator's heuristic
            # verdict gets overridden, simulator bugs for Passed
            # cases are dropped, and Playwright-only failures get
            # synthesised bugs (their content is filled in by the
            # rewrite step below). Without this, a TC could ship a
            # bug whose description has nothing to do with the
            # screenshots attached to it.
            _reconcile_with_automation(execution, automation_assets, et)
            # Dedupe carbon-copy bugs (same defect_class + final_url)
            # so a 62-TC failure run produces 3-5 actionable bugs
            # instead of 62 identical ones — operator-reported on
            # 2026-05-05.
            _dedupe_bugs_by_root_cause(execution, automation_assets)
            _bug_aliases = execution.get("_bug_alias") or {}

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
                # Propagate the alias map so every TC merged into this
                # bug lands on the same bug_id when result rows are
                # rewritten further down.
                for alias_tc, primary_tc in _bug_aliases.items():
                    if primary_tc == bug_dict.get("linked_item_id", ""):
                        bug_id_map[alias_tc] = new_id
                linked = bug_dict.get("linked_item_id", "")
                ev = automation_assets.get(linked) if linked else None
                if ev:
                    existing_atts = list(bug_dict.get("attachments") or [])
                    fstep = ev.get("failure_step") or {}
                    targeted: list[str] = []
                    if fstep.get("context_screenshot"):
                        targeted.append(fstep["context_screenshot"])
                    if fstep.get("screenshot"):
                        targeted.append(fstep["screenshot"])
                    if not targeted:
                        targeted = list((ev.get("screenshots") or [])[-3:])
                    for shot in targeted:
                        if shot and shot not in existing_atts:
                            existing_atts.append(shot)
                    v = ev.get("video")
                    if v and v not in existing_atts:
                        existing_atts.append(v)
                    bug_dict["attachments"] = existing_atts
                    if fstep:
                        bug_dict["_automation_failure"] = {
                            "step_index": fstep.get("index"),
                            "step_action": fstep.get("action"),
                            "comment": fstep.get("comment"),
                            "console_errors": fstep.get(
                                "console_errors") or [],
                            "final_url": ev.get("final_url") or "",
                        }
                # Rule-driven rewrite using bug_template (mirrors the
                # synchronous path).
                try:
                    from engine.bug_template import (
                        rewrite_bug_from_automation as _rewrite_bug,
                    )
                    linked_item = next(
                        (it for it in items_data
                         if it.get("id") == linked), None) if linked else None
                    tc_fields = {
                        "tc_summary": (linked_item or {}).get("summary")
                            or (linked_item or {}).get("objective", ""),
                        "tc_steps": (linked_item or {}).get("test_steps", ""),
                        "tc_preconditions": (linked_item or {}).get(
                            "preconditions", ""),
                        "tc_expected": (linked_item or {}).get(
                            "expected_result", ""),
                        "tc_section": (linked_item or {}).get("section", "")
                            or bug_dict.get("component", ""),
                    }
                    _rewrite_bug(
                        bug_dict,
                        automation_failure=bug_dict.pop(
                            "_automation_failure", None),
                        base_url=base_url,
                        **tc_fields,
                    )
                except Exception as _rw_exc:
                    log.warning("results: bug rewrite skipped: %s", _rw_exc)
                _persist_bug(bug_dict, source="execution",
                             run_id=db_run_id)
                all_bugs.append(bug_dict)
                try:
                    running_bugs.append(dict_to_bug(bug_dict))
                except Exception:
                    running_bugs = list(existing_bugs) + [
                        dict_to_bug(b) for b in all_bugs[len(existing_bugs):]
                    ]

            # PR-3: convert walkthrough findings into bugs the same way
            # the TC-driven loop above converts ``execution["bugs"]``.
            # Findings live on ``payload`` (not in ``execution``) because
            # the walkthrough runner doesn't drive the simulator. Each
            # finding becomes a bug via
            # ``bug_report.create_bug_from_walkthrough_finding`` —
            # synthetic ``WALK-...`` TC-id, ``defect:<class>`` +
            # ``source:walkthrough`` labels — and gets persisted through
            # the same ``_persist_bug`` path so bug-reports listing and
            # /bug-reports filtering still work.
            walkthrough_bugs_count = 0
            if (run_mode == "walkthrough"
                    and walkthrough_findings
                    and not walkthrough_attached):
                from engine.bug_report import (
                    create_bug_from_walkthrough_finding as _create_wt_bug,
                )
                for finding in walkthrough_findings:
                    if not isinstance(finding, dict):
                        continue
                    try:
                        bug = _create_wt_bug(
                            finding,
                            environment_str=environment,
                            tester_name=tester_name,
                            base_url=base_url,
                        )
                    except Exception as exc:
                        log.warning(
                            "walkthrough: bug conversion skipped: %s", exc)
                        continue
                    bug_dict = bug_to_dict(bug)
                    new_id = generate_bug_id(running_bugs)
                    bug_dict["id"] = new_id
                    if not bug_dict.get("affects_version"):
                        bug_dict["affects_version"] = affects_version
                    _persist_bug(bug_dict, source="walkthrough",
                                 run_id=db_run_id)
                    all_bugs.append(bug_dict)
                    try:
                        running_bugs.append(dict_to_bug(bug_dict))
                    except Exception:
                        running_bugs = list(existing_bugs) + [
                            dict_to_bug(b) for b
                            in all_bugs[len(existing_bugs):]
                        ]
                    walkthrough_bugs_count += 1
                walkthrough_attached = True

            for r in execution["results"]:
                if r["bug_id"].startswith("__pending_"):
                    r["bug_id"] = bug_id_map.get(r["item_id"], r["bug_id"])
                asset = automation_assets.get(r["item_id"])
                if asset and et in ("web", "mobile_web"):
                    if asset.get("video"):
                        r["video"] = asset["video"]
                    # Pick a shot list that matches the TC verdict:
                    #   * Failed/Blocked → lead with the annotated
                    #     failure shot + the previous step's "after"
                    #     (context). Operator-reported: "I see Failed
                    #     status but no red box anywhere on the
                    #     attached shots."
                    #   * Passed → clean per-step gallery.
                    fstep = asset.get("failure_step") or {}
                    is_failure = (r.get("status") in ("Failed", "Blocked"))
                    if is_failure and fstep.get("screenshot"):
                        gallery: list[str] = []
                        if fstep.get("context_screenshot"):
                            gallery.append(fstep["context_screenshot"])
                        gallery.append(fstep["screenshot"])
                        # Tail: the rest of clean shots so the
                        # operator sees the run's progression.
                        for s in (asset.get("screenshots") or []):
                            if s and s not in gallery:
                                gallery.append(s)
                        r["screenshots"] = gallery
                    elif asset.get("screenshots"):
                        r["screenshots"] = asset["screenshots"]

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
                                            or (r.get("screenshots")
                                                or [None])[0]),
                            notes=r.get("comment"),
                        )
                    except Exception as exc:
                        log.warning("save_case_result (results) failed: %s", exc)
                try:
                    _db.finish_execution_run(
                        db_run_id, status="completed",
                        stats=execution.get("stats") or {},
                    )
                except Exception as exc:
                    log.warning("finish_execution_run (results) failed: %s", exc)

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
            # PR-3: walkthrough findings + TC bindings live on the
            # first env's run_record so the template's findings subtab
            # has somewhere to read from. ``walkthrough_bugs_count``
            # already counts the bugs created above (zero for envs
            # past the first).
            attached_findings = (
                walkthrough_findings
                if (run_mode == "walkthrough"
                    and walkthrough_attached
                    and walkthrough_bugs_count)
                else []
            )
            attached_bindings = (
                walkthrough_tc_bindings
                if (run_mode == "walkthrough"
                    and walkthrough_attached
                    and walkthrough_bugs_count)
                else []
            )
            run_bug_count = len(execution["bugs"]) + walkthrough_bugs_count
            run_record = {
                "run_id": len(test_runs) + 1,
                "db_run_id": db_run_id,
                "source": source,
                "mode": run_mode,
                "tester_id": tester_id,
                "tester_name": tester_name,
                "environment": environment,
                "env_type": et,
                "testing_types": ", ".join(testing_types),
                "results": results_summary,
                "stats": execution["stats"],
                "bug_count": run_bug_count,
                "site_url": site_url,
                "base_url": base_url,
                "headless": headless,
                "record_video": record_video,
                "automation_used": (et in ("web", "mobile_web")),
                "created_at": datetime.now().isoformat(),
                # PR-3 fields — empty lists for TC-driven runs and
                # envs past the first in a walkthrough run.
                "walkthrough_findings": attached_findings,
                "walkthrough_tc_bindings": attached_bindings,
            }
            test_runs.append(run_record)
            run_summaries.append(
                (environment, execution["stats"], run_bug_count))
            bug_total += run_bug_count

        test_runs = test_runs[-20:]
        session["bug_reports_data"] = all_bugs
        session["test_runs"] = test_runs
        session["automation_report"] = {
            "passed":  int(report.get("passed", 0)),
            "failed":  int(report.get("failed", 0)),
            "blocked": int(report.get("blocked", 0)),
            "run_id":  report.get("run_id", run_id),
        }

        parts = [g.t.get("te_results_saved",
                          "Test execution results saved successfully") + "."]
        for env_str, stats, bug_n in run_summaries:
            parts.append(
                f"[{env_str}] {stats['passed']} P / {stats['failed']} F / "
                f"{stats['blocked']} B ({stats['pass_rate']}%)"
                + (f", {bug_n} bug(s)" if bug_n else "")
                + "."
            )
        if base_url:
            parts.append(
                f"Playwright session ran against {base_url} "
                f"({'headless' if headless else 'visible'}, "
                f"{'video on' if record_video else 'no video'})."
            )
        if bug_total:
            parts.append(
                f"{bug_total} bug report(s) auto-created — see Bug Reports."
            )
        flash(" ".join(parts), "success")

        # Clean up pending files so subsequent visits to this URL
        # don't double-bug. Best-effort.
        for suffix in (".done.flag", ".started.flag", ".result.json", ".json", ".log"):
            p = os.path.join(pending_dir, f"{run_id}{suffix}")
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass

        # Drop the in-page run-progress widget marker — results are
        # imported, the banner is no longer relevant.
        session.pop("active_automation_run", None)

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
        # Sprint 4 task 4.2: prefer the DB as source of truth so the
        # ``db_id`` field is populated on every rendered card (the
        # bulk-edit checkboxes need it). Session ``bug_reports_data``
        # stays as the fallback for the legacy / pre-project flow.
        pid = ensure_active_project()
        bugs = _hydrate_bugs(pid)

        stats = {
            "total": len(bugs),
            "open": sum(1 for b in bugs if b.status == "Open"),
            "critical": sum(1 for b in bugs if b.severity == "Critical"),
            "major": sum(1 for b in bugs if b.severity == "Major"),
        }

        return render_template("bug_reports.html", bugs=bugs, stats=stats,
                               severities=BUG_SEVERITIES, priorities=BUG_PRIORITIES,
                               statuses=BUG_STATUSES, frequencies=BUG_FREQUENCIES)

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
