"""TestFortge — what happens to a run's results after the run.

Five pieces of logic that read a finished (or half-finished) execution and
decide what it *means*: which findings collapse into one, which bugs are
the same defect twice, what a run that died mid-flight still has to show
for itself, and whether a suite that failed every item failed for a reason
that is about the site at all.

They lived in ``routes/execution.py`` until the Stage 7 Phase B split, and
the reason to move them is not the line count. **None of them touches
Flask** — no ``request``, no ``session``, no ``g``, no template. They are
functions over dicts, which is why ``tests/test_execution_infra_guard.py``
and ``tests/test_smart_filing.py`` were already importing them directly
and calling them with hand-built payloads. A pure function reached through
``from routes.execution import _private_name`` is a pure function whose
home is wrong, and the import told the truth about that before this module
existed.

Public names rather than the leading underscores they carried as
file-private helpers: an ``engine`` module's callers are elsewhere by
definition, and ``engine.run_results._reconcile_with_automation`` would be
asking two things at once.

Who calls what, because it is not symmetrical:

* ``dedupe_bugs_by_root_cause`` — the POST that starts a run *and* the
  page that renders it. The only one with two callers.
* ``aggregate_broken_image_findings``, ``reconstruct_partial_payload``,
  ``reconcile_with_automation`` — the results page only.
* ``make_cannot_execute_summary_bug`` and ``INFRA_GUARD_MIN_SUITE`` —
  ``reconcile_with_automation`` only; exported because the infra-guard
  tests drive them directly.
"""
from __future__ import annotations

from datetime import datetime

from engine.log import get_logger

log = get_logger(__name__)


# ── PR-H: page-level broken_image aggregation ─────────────────────


def aggregate_broken_image_findings(
    findings: list[dict],
) -> list[dict]:
    """Collapse findings of ``defect_class == 'broken_image'`` sharing
    the same ``url`` into a single aggregate finding.

    A marketing page with 12 broken graphics produced 12 bug rows
    pre-PR-H — operators saw "12 broken-image bugs on /careers"
    where one aggregate ("12 page graphics missing on Careers
    page") would have communicated the same impact for triage. We
    only fold ``broken_image`` here because other defect classes
    (axe, JS) need per-element resolution; collapsing them would
    hide actionable detail.

    The aggregate finding's ``message`` is replaced with an "N page
    graphics missing on …" form (the title transform in
    :func:`engine.bug_report._walkthrough_passive_title` handles the
    final passive-voice phrasing). The list of original filenames
    is carried through ``aggregated_filenames`` so the bug body /
    Developer Detail still names every affected asset.

    Other heuristics' findings pass through unchanged.
    """
    if not findings:
        return list(findings or [])

    # Bucket only broken_image findings; everything else flows
    # straight through.
    bucket: dict[tuple[str, str], list[dict]] = {}
    passthrough: list[dict] = []
    for f in findings:
        if not isinstance(f, dict):
            passthrough.append(f)
            continue
        cls = str(f.get("defect_class") or "").strip().lower()
        url = str(f.get("url") or "")
        if cls == "broken_image" and url:
            bucket.setdefault((cls, url), []).append(f)
        else:
            passthrough.append(f)

    result: list[dict] = list(passthrough)
    for (cls, url), group in bucket.items():
        if len(group) <= 1:
            # Solo finding — pass through, no aggregation needed.
            result.extend(group)
            continue
        # Multi-finding group → synthesise one aggregate. Use the
        # first finding as the template so severity / area /
        # tc_id are preserved; replace message + element with the
        # aggregate phrasing and list every affected filename in
        # ``aggregated_filenames`` so the bug body can render it.
        first = dict(group[0])
        filenames: list[str] = []
        elements: list[str] = []
        for member in group:
            el = member.get("element") or ""
            elements.append(str(el))
            # Best-effort filename extraction from the heuristic's
            # message ("Broken image on the page — foo.svg did not
            # load …"). The walkthrough heuristic always frames it
            # the same way; mismatches just leave the filename
            # blank rather than crashing.
            msg = str(member.get("message") or "")
            import re as _re
            m = _re.search(
                r"—\s*(?P<fn>[^\s]+?)\s+did not load", msg,
            )
            if m:
                filenames.append(m.group("fn"))
        count = len(group)
        # Replace ``message`` with a per-page aggregate phrase so
        # the title transform produces a clean headline. We don't
        # touch ``element`` of the first item so annotation can
        # still try the first selector — operators only need to
        # see *one* example overlay on the aggregate bug.
        first["message"] = (
            f"{count} broken images on the page — visitors see "
            f"empty slots or broken-image icons where graphics "
            f"should be"
        )
        first["aggregated_filenames"] = filenames
        first["aggregated_elements"] = elements
        first["aggregated_count"] = count
        # Append a summary block to dev_detail so the bug body
        # surfaces the original filenames for engineering triage.
        prior_detail = str(first.get("dev_detail") or "")
        filename_block = "\n".join(f"  • {fn}" for fn in filenames if fn)
        if filename_block:
            first["dev_detail"] = (
                f"{prior_detail}\n\nAffected assets ({count}):\n"
                f"{filename_block}"
            ).strip()
        result.append(first)
    return result


def dedupe_bugs_by_root_cause(execution: dict,
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


def reconstruct_partial_payload(run_id: str, config_path: str,
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


# ── Cannot-execute / all-failed infrastructure guard ──────────────
#
# Confirmed via reproduction (2026-07-15): a Test Execution run where
# the automation layer reports a non-passing status for EVERY item
# does NOT mean the suite found N distinct product defects. The
# simulator alone cannot produce an all-failed run — its fail
# probability is capped at 45% (engine/qa_testers.py:596); an
# all-failed outcome is imposed by reconcile_with_automation copying
# the runner's per-item status 1:1. When the whole suite fails to
# pass, the honest QA reading is an infrastructure / environment /
# coverage problem (Base URL unset or unreachable, UA/geo block, or a
# generic test pack that does not map to the site under test) — a
# cannot-execute condition (ISTQB: Blocked), not N real defects with N
# bugs. The guard below reclassifies such a run to Blocked and raises
# ONE summary bug instead of one misleading Failed + bug per item.
#
# A run smaller than this is left alone — a 3-item smoke test that
# legitimately fails all 3 is a real signal, not infrastructure noise.
INFRA_GUARD_MIN_SUITE = 10


def make_cannot_execute_summary_bug(n_blocked: int, site_url: str,
                                     reporter: str = "") -> dict:
    """Build the ONE summary bug that replaces the per-item bug pile
    when the all-failed infrastructure guard fires.

    Same dict shape the rest of the bug pipeline consumes; ``id`` and
    ``environment`` are filled in by the per-env loop downstream.
    """
    site = (site_url or "").strip() or "the application under test"
    return {
        "id": "",
        "title": (f"Test run could not validate the application — "
                  f"{n_blocked} item(s) unexecutable (infrastructure/"
                  f"environment issue, not distinct defects)"),
        "severity": "Major",
        "priority": "High",
        "status": "Open",
        "environment": "",
        "preconditions": (
            f"A Test Execution run was started against {site}."),
        "steps_to_reproduce": (
            "1. Start a Test Execution run against the target.\n"
            "2. Observe that no item produces a passing result.\n"
            "3. Review the per-item Blocked comments and the run "
            "environment (Base URL, reachability, credentials)."),
        "actual_result": (
            f"The run produced 0 passing checks across {n_blocked} "
            f"executed item(s) — every item failed to validate. A "
            f"whole-suite failure of this shape indicates the "
            f"application could not be reached or driven (wrong/unset "
            f"Base URL, environment down, UA/geo block, or a generic "
            f"test pack that does not map to {site}) — NOT {n_blocked} "
            f"distinct product defects. The items were recorded as "
            f"Blocked (could not be executed)."),
        "expected_result": (
            "The run should reach the application and validate items, "
            "producing a realistic pass/fail mix. Verify the Base URL, "
            "environment availability/credentials, and that the test "
            "pack matches the site under test, then re-run."),
        "frequency": "Always",
        "affects_version": "",
        "found_in_build": "",
        "attachments": [],
        "linked_item_id": "",
        "linked_item_type": "live_executor",
        "reporter": reporter or "",
        "assignee": "",
        "created_at": datetime.now().isoformat(),
        "component": "TestRunInfra",
        "labels": ["live_executor", "source:live_executor",
                   "defect:cannot_execute", "infra"],
        "comment": "",
    }


def reconcile_with_automation(execution: dict,
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
    # Items the runner could not genuinely execute (runner "blocked", or
    # a "failed" with no failure evidence). These become ISTQB Blocked
    # and NEVER auto-file a bug — see the split in the loop below.
    cannot_execute_ids: set[str] = set()

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
            # Runner reports Failed/Blocked while the simulator said
            # otherwise. We MUST distinguish a genuine, evidence-backed
            # product failure from a cannot-execute condition — the
            # latter is the root cause of the "0 pass / all fail / one
            # bug per item" runs (2026-07-15 investigation):
            #
            #   * runner "blocked"                      -> cannot-execute
            #   * runner "failed" WITH failure evidence -> genuine Failed
            #   * runner "failed" WITHOUT any evidence  -> cannot-execute
            #
            # Cannot-execute items are recorded as ISTQB Blocked and
            # NEVER auto-file a bug; any simulator bug they carried is
            # dropped. Only an evidence-backed Failed keeps/synthesizes
            # a bug.
            fstep = ev.get("failure_step") or {}
            comment = (fstep.get("comment") or "").strip()
            has_evidence = bool(
                ev.get("failure_step") or ev.get("failure_screenshots"))
            genuine_failure = (runner_status == "Failed" and has_evidence)

            if not genuine_failure:
                # Cannot-execute -> Blocked, no bug.
                r["status"] = "Blocked"
                r["comment"] = (
                    comment
                    or "Automation could not execute this item — "
                       "recorded as Blocked (not a product defect).")
                r["source"] = "real_check"
                cannot_execute_ids.add(item_id)
                # Drop any simulator bug for this item and clear a
                # pending reference so no bug is filed.
                drop_item_ids.add(item_id)
                if r.get("bug_id", "").startswith("__pending_"):
                    r["bug_id"] = ""
                continue

            # Genuine, evidence-backed failure — promote + ensure a bug.
            r["status"] = "Failed"
            promote_to_failed[item_id] = "Failed"
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

    # Drop simulator bugs for items Playwright passed OR could not
    # execute (cannot-execute items must never carry a bug).
    if drop_item_ids:
        execution["bugs"] = [
            b for b in bugs
            if b.get("linked_item_id") not in drop_item_ids
        ]
        bugs = execution["bugs"]
    # Add synth bugs for evidence-backed Playwright-only failures.
    if new_bugs:
        execution["bugs"] = list(bugs) + new_bugs
        bugs = execution["bugs"]

    # Count the reconciled verdicts.
    passed = sum(1 for r in results if r.get("status") == "Passed")
    failed = sum(1 for r in results if r.get("status") == "Failed")
    blocked = sum(1 for r in results if r.get("status") == "Blocked")
    total = passed + failed + blocked

    # ── All-failed infrastructure guard ──────────────────────────
    # If NOTHING passed across a substantial suite, the run did not
    # find N distinct defects — it could not validate the app at all
    # (unset/unreachable Base URL, environment down, UA/geo block, or a
    # generic pack that does not map to the site). Record every
    # remaining Failed as Blocked (ISTQB: could not be executed) and
    # replace the per-item bug pile with ONE summary infrastructure
    # bug. See make_cannot_execute_summary_bug + the 2026-07-15 note.
    if total >= INFRA_GUARD_MIN_SUITE and passed == 0:
        site_url = (execution.get("stats") or {}).get("site_url") or ""
        reporter = ""
        for r in results:
            if not reporter:
                reporter = r.get("tester_name", "") or ""
            if r.get("status") == "Failed":
                r["status"] = "Blocked"
                r["source"] = "real_check"
                cannot_execute_ids.add(r.get("item_id") or "")
                if not (r.get("comment") or "").strip():
                    r["comment"] = (
                        "Run could not validate this item — whole-suite "
                        "execution failure (see the run's summary bug).")
                if r.get("bug_id", "").startswith("__pending_"):
                    r["bug_id"] = ""
        blocked = failed + blocked
        failed = 0
        execution["bugs"] = [
            make_cannot_execute_summary_bug(
                n_blocked=blocked, site_url=site_url, reporter=reporter)
        ]
        execution["_bug_alias"] = {}

    execution["stats"] = {
        "total": total,
        "passed": passed,
        "failed": failed,
        "blocked": blocked,
        "pass_rate": round(passed / total * 100, 1) if total else 0,
        "cannot_execute": len(cannot_execute_ids),
        "sources": (execution.get("stats") or {}).get("sources") or {},
        "site_url": (execution.get("stats") or {}).get("site_url") or "",
        "reconciled_with_automation": True,
    }


__all__ = [
    "aggregate_broken_image_findings",
    "dedupe_bugs_by_root_cause",
    "reconstruct_partial_payload",
    "make_cannot_execute_summary_bug",
    "reconcile_with_automation",
    "INFRA_GUARD_MIN_SUITE",
]
