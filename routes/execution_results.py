"""TestFortge — the page a finished run lands on.

  * GET /test-execution/results/<run_id> — render a completed run

One endpoint, and it is the second half of the Stage 7 split: Phase A took
``/bug-reports`` and ``/test-execution/live*`` out of
``routes/execution.py`` and left the main flow behind, whereupon the file
grew back past the size the split was for. This is the other cut Phase A
named and deferred.

Why this route and not another. It is the largest single view in the
module, and it is the **only** caller of four of the five helpers that now
live in :mod:`engine.run_results` — the call graph, not the line count,
says these belong together. What is left in ``routes/execution.py`` is
what the name promises: configuring a run and dispatching it.

The view is unchanged. Moving a Flask view between modules is invisible to
everything that matters as long as the function name survives, because the
endpoint is the function name: ``url_for("test_execution_results")`` still
resolves, ``engine.route_policy.POLICY`` still gates it at ``user``, and
the URL is still the one in the decorator. Registered after
``execution`` for the same reason ``execution_live`` is — URL-rule order
is stable and some tests match the first-defined rule.
"""

from __future__ import annotations

from datetime import datetime

from flask import Flask, flash, g, redirect, session, url_for

from engine import db as _db
from engine.bug_report import bug_to_dict, dict_to_bug, generate_bug_id
from engine.log import get_logger
from engine.qa_testers import execute_items, get_tester
from engine.run_results import (
    aggregate_broken_image_findings,
    dedupe_bugs_by_root_cause,
    reconcile_with_automation,
    reconstruct_partial_payload,
)

from ._shared import (ensure_active_project, mirror_pack, pack_bugs,
                      pack_checklist, pack_runs, pack_test_cases)
# Each TC-driven / walkthrough / LiveExecutor bug is mirrored into the DB
# through the same writer the bug pages use — see routes/bugs.py.
from .bugs import _persist_bug

log = get_logger(__name__)


def register(app: Flask) -> None:
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

        def _close_dispatched_runs(status: str, note: str = "") -> None:
            """Close the rows the dispatcher opened for this config.

            Read from the config file rather than from ``config_echo``,
            because every early return below happens before the echo has
            been parsed — and two of them happen when there is no result
            file to parse an echo out of at all. Those returns used to
            abandon the row: a run the worker had explicitly reported as
            failed went on counting against the one-browser-run cap until
            the staleness window expired half an hour later.
            """
            try:
                with open(config_path, "r", encoding="utf-8") as _cf:
                    _cfg = json.load(_cf) or {}
            except Exception:
                return
            for _rid in (_cfg.get("db_run_ids") or {}).values():
                try:
                    _db.close_execution_run_if_open(
                        int(_rid), status=status,
                        stats={"note": note} if note else None)
                except Exception as _exc:  # pragma: no cover — best-effort
                    log.warning("could not close run %s: %s", _rid, _exc)
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
            payload = reconstruct_partial_payload(
                run_id, config_path, STORAGE_ROOT, live)
            if payload is None:
                flash(
                    "Run is missing a result file and no on-disk "
                    "artifacts could be salvaged. Open /test-execution/"
                    f"diag and check the worker log "
                    f"({run_id}.log) for the failure cause.",
                    "error",
                )
                _close_dispatched_runs("failed", "no salvageable artefacts")
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
                _close_dispatched_runs("failed", f"unreadable result: {exc}")
                return redirect(url_for("test_execution_page"))

        if payload.get("status") in ("failed", "terminated"):
            flash(
                "Automation run failed: "
                f"{payload.get('error', 'unknown error')}. "
                "Open /test-execution/diag for details.",
                "error",
            )
            _close_dispatched_runs(str(payload.get("status")),
                                   str(payload.get("error", ""))[:200])
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
        # Stage 4: LiveExecutor early-exit (OOM / wall-clock) is
        # surfaced as one infrastructure bug. The reason string is
        # produced inside ``engine.live_executor.LiveExecutor.run``
        # and copied onto ``payload`` by ``runner_worker`` — empty
        # string means the run finished normally.
        early_exit_reason = (payload.get("early_exit_reason") or "").strip()

        # Re-run the post-processing using the same logic the synchronous
        # path used to run inline. Most fields come from the worker's
        # config echo (so the user's selection of envs / tester etc. is
        # honoured even though we're in a different request now).
        items_data = (cfg.get("items_data") or pack_test_cases()
                      or pack_checklist())
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
            dict_to_bug(b) for b in pack_bugs()
            if b.get("id")
        ]
        all_bugs = list(pack_bugs())
        test_runs = list(pack_runs())

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
        # Stage 4: one infra bug per run even when multiple env
        # checkboxes are ticked. Attaches alongside the walkthrough
        # findings (same env), but is independent of whether any
        # findings were produced — an OOM can fire on a clean page.
        early_exit_attached = False
        for et in env_types:
            environment = (envs_meta.get(et, {}) or {}).get("environment") \
                or et.title()
            # Adopt the row the dispatch path opened for this env, rather
            # than opening a second one. Dispatch registers a run per
            # env_type so the Runs register and the concurrency gate can
            # see it while it is still in flight; without this lookup the
            # import would double every automated run in the register.
            #
            # ``or None`` because an id of 0 is not a row, and older
            # pending configs written before E11 have no db_run_ids key at
            # all — those still fall through to opening one here.
            #
            # Re-applied here by hand during the E11 rebase: the commit
            # that wrote it edited this loop while it still lived in
            # routes/execution.py, and Stage 7 Phase B moved the whole
            # view into this module in between. Git saw a delete against
            # an edit and could only offer the conflict; the two halves of
            # that commit — open the row at dispatch, adopt it at import —
            # are what make each other correct, so dropping either would
            # have left the double-registration it was written to remove.
            db_run_id = ((cfg.get("db_run_ids") or {}).get(et)
                         or (cfg.get("db_run_ids") or {}).get(str(et))
                         or None)
            try:
                pid = ensure_active_project()
                if pid and db_run_id is None:
                    db_run_id = _db.start_execution_run(
                        pid,
                        env_payload={
                            "env_type": et,
                            "environment": environment,
                            "tester_id": tester_id,
                            "tester_name": tester_name,
                            "testing_types": testing_types,
                            "source": source,
                            # The run mode belongs in the row, not only in
                            # the session record: /bug-reports filters by
                            # it and a run read back from the database
                            # could not say how it had been executed.
                            "mode": run_mode,
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
            reconcile_with_automation(execution, automation_assets, et)
            # Dedupe carbon-copy bugs (same defect_class + final_url)
            # so a 62-TC failure run produces 3-5 actionable bugs
            # instead of 62 identical ones — operator-reported on
            # 2026-05-05.
            dedupe_bugs_by_root_cause(execution, automation_assets)
            _bug_aliases = execution.get("_bug_alias") or {}

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
                _row_id = _persist_bug(bug_dict, source="execution",
                                       run_id=db_run_id)
                if _row_id:
                    bug_row_ids[bug_dict.get("linked_item_id", "")] = _row_id
                all_bugs.append(bug_dict)
                try:
                    running_bugs.append(dict_to_bug(bug_dict))
                except Exception:
                    running_bugs = list(existing_bugs) + [
                        dict_to_bug(b) for b in all_bugs[len(existing_bugs):]
                    ]

            # PR-3 / Stage 4: convert walkthrough findings into bugs the
            # same way the TC-driven loop above converts
            # ``execution["bugs"]``. Findings live on ``payload`` (not in
            # ``execution``) because neither WalkthroughRunner nor
            # LiveExecutor drives the simulator. Each finding becomes a
            # bug via ``bug_report.create_bug_from_walkthrough_finding``
            # — synthetic ``WALK-...`` / ``LIVE-PAGE-...`` TC-id,
            # ``defect:<class>`` + ``source:walkthrough`` labels — and
            # gets persisted through the same ``_persist_bug`` path so
            # bug-reports listing and /bug-reports filtering still work.
            #
            # Stage 4: ``mode == "live"`` (LiveExecutor, the default
            # since Stage 3) carries the same ``walkthrough_findings``
            # shape as legacy ``mode == "walkthrough"``. Without
            # accepting both here, Stage 3 silently lost bug-creation
            # versus Sprint 5 — findings hit ``result.json`` but never
            # the Bug Reports board.
            walkthrough_bugs_count = 0
            walkthrough_dedup_skipped = 0
            walkthrough_aggregated = 0
            if (run_mode in ("walkthrough", "live")
                    and walkthrough_findings
                    and not walkthrough_attached):
                from engine.bug_report import (
                    create_bug_from_walkthrough_finding as _create_wt_bug,
                )
                from engine.db import (
                    find_bug_id_by_signature as _find_existing_bug,
                    bump_bug_occurrence as _bump_occurrence,
                )

                # PR-H: page-level aggregation for broken-image findings.
                # The heuristic emits one finding per broken <img>; a
                # marketing site with 12 broken graphics on one page
                # was filing 12 bugs ("BUG-053 … BUG-064"). Collapse
                # findings sharing ``(defect_class='broken_image',
                # url)`` into a single aggregate finding whose body
                # lists every affected filename + element. Other
                # defect classes (axe, JS errors) are NOT aggregated
                # — they need per-element resolution.
                processed_findings = aggregate_broken_image_findings(
                    walkthrough_findings,
                )
                walkthrough_aggregated = (
                    len(walkthrough_findings) - len(processed_findings)
                )

                # Active project id for the cross-run dedup lookup. Uses the
                # module-level import; a local ``from routes._shared import
                # ensure_active_project`` here made the name a local of the
                # whole enclosing function, so the *earlier* call at the top
                # of the loop raised "cannot access local variable
                # 'ensure_active_project' where it is not associated with a
                # value" — and that call is the one that creates the
                # execution_run row. It was swallowed by a log.warning, so
                # every walkthrough and live run existed only in the
                # session: nothing on /bug-reports could be filtered by it
                # and nothing survived a restart. Found by reading runs back
                # from the database in E3.4.
                active_pid = ensure_active_project()

                for finding in processed_findings:
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

                    # ── PR-H cross-run dedup ──
                    # Same defect on same page across runs → bump the
                    # existing bug's occurrence_count and skip INSERT.
                    # Without this every re-run on an unchanged site
                    # piles another ~65 duplicates onto the project.
                    existing_id = None
                    if active_pid and bug.dedup_signature:
                        try:
                            existing_id = _find_existing_bug(
                                active_pid, bug.dedup_signature,
                            )
                        except Exception as exc:  # pragma: no cover
                            log.warning(
                                "walkthrough: dedup query failed: %s",
                                exc,
                            )
                            existing_id = None
                    if existing_id is not None:
                        try:
                            _bump_occurrence(existing_id)
                        except Exception as exc:  # pragma: no cover
                            log.warning(
                                "walkthrough: dedup bump failed: %s",
                                exc,
                            )
                        walkthrough_dedup_skipped += 1
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
                if walkthrough_dedup_skipped or walkthrough_aggregated:
                    log.info(
                        "walkthrough: filed %d bugs (skipped %d duplicates, "
                        "aggregated %d broken-image findings)",
                        walkthrough_bugs_count,
                        walkthrough_dedup_skipped,
                        walkthrough_aggregated,
                    )

            # Stage 4: LiveExecutor early-exit → one infrastructure
            # bug. Independent of findings: the OOM guard can fire on
            # a leak that emits zero walkthrough findings, and the
            # wall-clock deadline can fire mid-run on a healthy site.
            # Attached to the first env (same rule as walkthrough
            # bugs) so the multi-env operator doesn't see N copies.
            # ``early_exit_bugs_count`` is reset every iteration so
            # envs past the first contribute 0 to ``run_bug_count``.
            early_exit_bugs_count = 0  # ALWAYS reset (NameError guard)
            if (run_mode == "live"
                    and early_exit_reason
                    and not early_exit_attached):
                from engine.bug_report import (
                    create_bug_from_early_exit as _create_ee_bug,
                )
                report_dict = payload.get("report") or {}
                try:
                    bug = _create_ee_bug(
                        early_exit_reason,
                        run_id=report_dict.get("run_id", "")
                            or payload.get("config_id", ""),
                        base_url=base_url,
                        environment_str=environment,
                        tester_name=tester_name,
                    )
                    bug_dict = bug_to_dict(bug)
                    new_id = generate_bug_id(running_bugs)
                    bug_dict["id"] = new_id
                    if not bug_dict.get("affects_version"):
                        bug_dict["affects_version"] = affects_version
                    _persist_bug(bug_dict, source="live_executor",
                                 run_id=db_run_id)
                    all_bugs.append(bug_dict)
                    try:
                        running_bugs.append(dict_to_bug(bug_dict))
                    except Exception:
                        running_bugs = list(existing_bugs) + [
                            dict_to_bug(b) for b
                            in all_bugs[len(existing_bugs):]
                        ]
                    early_exit_bugs_count = 1
                except Exception as exc:
                    log.warning(
                        "live: early-exit bug conversion skipped: %s",
                        exc)
                early_exit_attached = True

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
                            bug_report_id=bug_row_ids.get(r.get("item_id")),
                            source=r.get("source"),
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
            # PR-3 / Stage 4: walkthrough/live findings + TC bindings
            # live on the first env's run_record so the template's
            # findings subtab has somewhere to read from.
            # ``walkthrough_bugs_count`` already counts the bugs created
            # above (zero for envs past the first).
            attached_findings = (
                walkthrough_findings
                if (run_mode in ("walkthrough", "live")
                    and walkthrough_attached
                    and walkthrough_bugs_count)
                else []
            )
            attached_bindings = (
                walkthrough_tc_bindings
                if (run_mode in ("walkthrough", "live")
                    and walkthrough_attached
                    and walkthrough_bugs_count)
                else []
            )
            run_bug_count = (len(execution["bugs"])
                             + walkthrough_bugs_count
                             + early_exit_bugs_count)
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
            # Into the row as well as the session record. Both are computed
            # after start_execution_run, so they are merged into the run's
            # env_payload here rather than passed at creation — and without
            # this, a walkthrough run read back from the database could not
            # say what it had attached, which is the one thing that
            # distinguishes it from any other run.
            if db_run_id is not None:
                try:
                    _db.merge_run_env(db_run_id, {
                        "walkthrough_findings": attached_findings,
                        "walkthrough_tc_bindings": attached_bindings,
                    })
                except Exception as exc:      # pragma: no cover
                    log.warning("merge_run_env failed: %s", exc)
            test_runs.append(run_record)
            run_summaries.append(
                (environment, execution["stats"], run_bug_count))
            bug_total += run_bug_count

        test_runs = test_runs[-20:]
        mirror_pack("bug_reports_data", all_bugs)
        mirror_pack("test_runs", test_runs)
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
        # Stage 4: surface the LiveExecutor early-exit reason as a
        # separate warning flash. Operators need a louder signal than
        # one row inside "Bug Reports" — losing half a run silently
        # was the whole point of recording ``early_exit_reason`` in
        # Stage 3 in the first place.
        if early_exit_reason:
            flash(
                f"Live executor stopped early: {early_exit_reason}. "
                "A bug report has been filed under "
                "'Test Run Infrastructure'.",
                "warning",
            )

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


__all__ = ["register"]
