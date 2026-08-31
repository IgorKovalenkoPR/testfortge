"""TestFortge MCP tools — read + minimal write surface (v1.5).

Read tools (v1):

* :func:`list_projects` — every project the DB knows about
* :func:`list_test_cases` — TCs for a project (filterable by status / trigger)
* :func:`list_bug_reports` — bugs (filterable by project / severity / status / source)
* :func:`list_execution_runs` — recent runs for a project
* :func:`get_execution_run` — one run plus its per-case results
* :func:`walkthrough_findings_stats` — summary of one or more result.json files

Write tools (v1.5):

* :func:`create_bug_report` — persist a bug row via :func:`engine.db.save_bug`
* :func:`trigger_test_execution` — spawn a detached ``runner_worker``
  subprocess, mirroring the Flask ``/test-execution`` dispatch path

Browser-control tools (PR-F Phase 2 — active driver):

* :func:`browser_control_start` / :func:`browser_control_status` /
  :func:`browser_control_stop` — session lifecycle
* :func:`browser_navigate` / :func:`browser_read_page` /
  :func:`browser_click` / :func:`browser_fill` / :func:`browser_wait`
  — drive the operator's real browser through the recorder extension
  over the DB-backed command queue in :mod:`engine.db`

The tools return plain JSON-serialisable dicts/lists so the MCP layer
ships them to the client without further conversion. Datetime columns
arrive already ISO-formatted (see :func:`engine.db._row_to_dict`).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import select

from engine import chatbot
from engine import db
from engine import walkthrough_stats
from engine.automation_paths import STORAGE_ROOT
from engine.job_queue import count_active_subprocess_runs

# A stable session_id for the per-session concurrency cap that
# routes/execution.py also uses. All MCP-triggered runs share this
# label so a runaway agent can't outrun the cap by varying ids.
MCP_SESSION_ID = "mcp-server"

# Hard ceiling on how many subprocess runs the MCP surface may have in
# flight at once. Matches the Flask default (``MAX_CONCURRENT_RUNS=3``)
# but lives here as a constant so it works even when the MCP server
# boots without Flask config loaded.
MCP_MAX_CONCURRENT_RUNS = 3

# Most result.json files ``walkthrough_findings_stats`` will read in one
# call. A tuning session looks at a handful; a list long enough to matter
# is a client that has lost the plot, and each entry is a file read.
MAX_STATS_FILES = 50

# PR-F Phase 2 — active browser driver. The Flask instance base URL, used
# to build the operator-facing handoff link so the extension knows where
# to poll / post results. Optional: when unset, the extension falls back
# to the instance it already learned from a prior recording.
TFG_INSTANCE_URL = os.environ.get("TFG_INSTANCE_URL", "").rstrip("/")
# How long a browser_* tool waits for the extension to execute + return a
# result before giving up. A slow page load can eat several seconds; the
# extension polls on a ~1 s interval, so keep headroom.
BROWSER_CMD_TIMEOUT_S = float(os.environ.get("BROWSER_CMD_TIMEOUT_S", "20"))


def _build_transport_security() -> TransportSecuritySettings:
    """Return the DNS-rebinding allowlist for the HTTP transport.

    FastMCP enables DNS-rebinding protection by default and ships an
    empty ``allowed_hosts`` list, which silently translates to
    "only ``127.0.0.1`` and ``localhost``". On Render, the Host header
    is the public hostname (``testfortge-mcp.onrender.com``), so the
    default rejects every prod request with ``Invalid Host header``
    BEFORE the bearer-auth middleware gets a chance to validate the
    token — leaving an MCP client with a cryptic 421 instead of 200.

    Read the allowlist from ``MCP_ALLOWED_HOSTS`` (comma-separated).
    When unset, fall back to localhost so a stdio dev loop and a local
    HTTP boot still work without ceremony. The bearer middleware is the
    real auth boundary; this check is belt-and-suspenders against DNS
    rebinding (irrelevant against an authenticated API anyway, but
    upstream defaults the way it does, so we configure it explicitly).
    """
    raw = os.environ.get("MCP_ALLOWED_HOSTS", "") or ""
    hosts = [h.strip() for h in raw.split(",") if h.strip()]
    if not hosts:
        hosts = ["127.0.0.1", "localhost",
                  "127.0.0.1:8765", "localhost:8765"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
    )


mcp = FastMCP("testfortge", transport_security=_build_transport_security())


# ── Read tools ─────────────────────────────────────────────────────

@mcp.tool()
def list_projects() -> list[dict]:
    """List every TestFortge project with aggregate counts.

    Returns one dict per project including id, name, slug, base_url,
    description, owner_sid, created_at / updated_at, and the cached
    counts ``test_cases_count``, ``checklist_count``, ``bug_count``.
    Ordered by ``updated_at`` descending — most-recently-touched first.
    """
    return db.list_projects()


@mcp.tool()
def list_test_cases(
    project_id: str,
    status: str | None = None,
    trigger: str | None = None,
) -> list[dict]:
    """List test cases for a project.

    Args:
        project_id: TestFortge project id (32-char hex from list_projects).
        status: Optional case-insensitive filter on the ``status`` column
            (e.g. "Passed", "Failed", "Blocked", "Open").
        trigger: Optional filter on how the TC fires under walkthrough
            mode. One of ``manual``, ``walkthrough_url_match``, ``always``.

    Returns each TC with its full editable fields plus ``url_pattern``
    and ``trigger`` (the walkthrough binding metadata added in Sprint 5).
    """
    if not project_id:
        return []
    with db.session_scope() as sess:
        stmt = (
            select(db.TestCase)
            .where(db.TestCase.project_id == project_id)
            .order_by(db.TestCase.id.asc())
        )
        if trigger:
            stmt = stmt.where(db.TestCase.trigger == trigger)
        rows = sess.execute(stmt).scalars().all()
        out = [db._row_to_dict(r) for r in rows]
    if status:
        target = status.casefold()
        out = [r for r in out if (r.get("status") or "").casefold() == target]
    return out


@mcp.tool()
def list_bug_reports(
    project_id: str | None = None,
    severity: str | None = None,
    status: str | None = None,
    source: str | None = None,
) -> list[dict]:
    """List bug reports, newest first.

    Args:
        project_id: Restrict to a single project. Omit to query across
            all projects (Tedgie submissions can land project-less).
        severity: Case-insensitive filter (e.g. "Critical", "Major", "Minor").
        status: Case-insensitive filter (e.g. "Open", "Closed", "In Progress").
        source: One of ``tedgie``, ``execution``, ``manual``, ``import``.
            Invalid values are silently ignored.

    Each bug row includes title, severity, priority, status, env/browser
    metadata, repro steps, related_case_id, run_id, and the JSON ``extra``
    field (used for fields like assignee that aren't first-class columns).
    """
    if source and source not in db.VALID_BUG_SOURCES:
        source = None
    rows = db.list_bugs(project_id=project_id, source=source)
    if severity:
        target = severity.casefold()
        rows = [r for r in rows if (r.get("severity") or "").casefold() == target]
    if status:
        target = status.casefold()
        rows = [r for r in rows if (r.get("status") or "").casefold() == target]
    return rows


@mcp.tool()
def list_execution_runs(project_id: str, limit: int = 50) -> list[dict]:
    """List recent execution runs for a project, newest first.

    Args:
        project_id: TestFortge project id.
        limit: Max rows to return (default 50, hard-capped at 500).

    Returns each run's id, status (``running`` / ``completed`` / ``failed``),
    timestamps, browser_visibility, base_url, the ``env_payload`` (mode,
    walkthrough flag, depth, etc.) and ``stats`` summary. Per-case details
    live behind :func:`get_execution_run`.
    """
    if not project_id:
        return []
    limit = max(1, min(int(limit or 50), 500))
    return db.list_execution_runs(project_id, limit=limit)


@mcp.tool()
def get_execution_run(run_id: int) -> dict | None:
    """Fetch one execution run together with its per-case results.

    Args:
        run_id: Integer id from :func:`list_execution_runs`.

    Returns ``None`` if the run is unknown. Otherwise: every column from
    the ``execution_run`` row, plus a ``case_results`` list — one entry
    per ``execution_case_result`` row with case_external_id, case_kind,
    status, evidence_path (screenshot), bug_report_id and notes.
    """
    if not run_id:
        return None
    with db.session_scope() as sess:
        run = sess.get(db.ExecutionRun, int(run_id))
        if not run:
            return None
        result = db._row_to_dict(run)
        cases = sess.execute(
            select(db.ExecutionCaseResult)
            .where(db.ExecutionCaseResult.run_id == run.id)
            .order_by(db.ExecutionCaseResult.id.asc())
        ).scalars().all()
        result["case_results"] = [db._row_to_dict(c) for c in cases]
        return result


@mcp.tool()
def walkthrough_findings_stats(paths: list[str] | None = None) -> dict:
    """Summarise walkthrough findings from one or more ``result.json`` files.

    Mirrors the ``python -m engine.walkthrough_stats ...`` CLI: parses
    each payload, prefers the deduped findings list, returns a tuning
    summary with per-defect-class counts, severity histogram, top URLs,
    and a few sample messages per class.

    Args:
        paths: List of paths to ``automation_runs/*.result.json`` files.
            Confined to that directory and to that suffix — see below. If
            omitted or empty, glob every ``*.result.json`` under it.

    The paths are the caller's, and the caller here is an agent over a
    network rather than the operator at their own shell. The sentence above
    has always described the scope; nothing enforced it, so any
    findings-shaped JSON anywhere the process could read was summarised and
    its ``message`` strings came back in ``samples``. Measured, not
    supposed. ``engine.walkthrough_stats.summarise_files`` still accepts any
    path on purpose — the CLI's contract is "whichever paths the operator
    passes" and an operator pointing it at a file in Downloads is the normal
    case — so the confinement belongs here, at the boundary where the caller
    stops being that operator.

    A path outside the directory is **refused by name**, not skipped: a
    client that believes it summarised a file the tool ignored is worse off
    than one told it cannot.

    The :class:`collections.Counter` values are serialised as plain
    dicts so the MCP layer can JSON-encode the response.
    """
    runs_dir = (Path(__file__).resolve().parent.parent
                / "automation_runs").resolve()
    if not paths:
        if not runs_dir.is_dir():
            return {"total": 0, "by_class": {}, "by_severity": {}, "by_url": {}}
        paths = [str(p) for p in sorted(runs_dir.glob("*.result.json"))]
    else:
        if len(paths) > MAX_STATS_FILES:
            return {"error": "too_many_paths",
                    "message": (f"At most {MAX_STATS_FILES} files per call; "
                                f"{len(paths)} were given.")}
        refused: list[str] = []
        cleaned: list[str] = []
        for raw in paths:
            candidate = Path(str(raw))
            if not candidate.is_absolute():
                candidate = runs_dir / candidate
            try:
                resolved = candidate.resolve()
            except OSError:
                refused.append(str(raw))
                continue
            # ``resolve()`` first, so ``..`` and a symlink out of the
            # directory are both settled before the comparison — the check
            # ``routes/automation.py`` makes on its asset paths, for the
            # same reason.
            inside = (resolved == runs_dir
                      or runs_dir in resolved.parents)
            if not inside or not resolved.name.endswith(".result.json"):
                refused.append(str(raw))
                continue
            cleaned.append(str(resolved))
        if refused:
            return {
                "error": "path_not_allowed",
                "message": ("Only *.result.json files under automation_runs/ "
                            "can be summarised here. Refused: "
                            + ", ".join(refused[:5])
                            + ("…" if len(refused) > 5 else "")),
            }
        paths = cleaned
    summary = walkthrough_stats.summarise_files(paths)
    # Counter is dict-like but FastMCP's pydantic serialiser balks on
    # non-builtin types — coerce to plain dicts before returning.
    summary["by_severity"] = dict(summary.get("by_severity") or {})
    summary["by_url"] = dict(summary.get("by_url") or {})
    cleaned_by_class: dict[str, Any] = {}
    for cls, bucket in (summary.get("by_class") or {}).items():
        cleaned_by_class[cls] = {
            "count": bucket["count"],
            "severity": dict(bucket["severity"]),
            "areas": dict(bucket["areas"]),
            "samples": list(bucket["samples"]),
        }
    summary["by_class"] = cleaned_by_class
    return summary


# ── Write tools ────────────────────────────────────────────────────

@mcp.tool()
def create_bug_report(
    title: str,
    severity: str = "Major",
    priority: str = "High",
    status: str = "Open",
    environment: str = "Web",
    steps_to_reproduce: str = "",
    actual_result: str = "",
    expected_result: str = "",
    project_id: str | None = None,
    source: str = "manual",
    related_case_id: str | None = None,
    run_id: int | None = None,
    reporter: str | None = None,
    extra: dict | None = None,
) -> dict:
    """Persist a bug report. Returns the new row's db_id plus an echo
    of the key fields so the caller can confirm what landed.

    Wraps :func:`engine.db.save_bug` — same call the Flask
    ``/create-bug-report`` route uses under the hood, minus the
    session-bound bug-id generator (the DB row's auto id is returned
    instead of an external "BUG-NNN" label).

    Args:
        title: Bug summary. Required and trimmed; empty raises.
        severity: ``Critical`` / ``Major`` / ``Minor`` / ``Trivial``.
            Free-form string — TestFortge does not validate, but the
            listing UI groups by these exact labels.
        priority: ``Highest`` / ``High`` / ``Medium`` / ``Low``.
        status: Defaults ``Open``.
        environment: Free-form env tag (``Web``, ``iOS 17``, ``API``).
        steps_to_reproduce / actual_result / expected_result: Free-form
            multiline text. Mirror the manual bug-report form.
        project_id: 32-char project id from :func:`list_projects`.
            Omit for a project-less bug (e.g. the Tedgie chat path).
        source: One of ``tedgie`` / ``execution`` / ``manual`` /
            ``import``. Invalid values fall back to ``manual``.
        related_case_id: External TC id this bug is tied to.
        run_id: ``execution_run.id`` if the bug came from a run.
        reporter: Free-form reporter name.
        extra: Arbitrary dict — assignee, labels, frequency,
            found_in_build, etc. Anything not a first-class column is
            stored in the JSON ``extra`` field.

    Raises:
        ValueError: when ``title`` is missing / blank.
    """
    if not title or not str(title).strip():
        raise ValueError("create_bug_report: 'title' is required")

    bug_dict: dict[str, Any] = {
        "title": str(title).strip(),
        "severity": severity,
        "priority": priority,
        "status": status,
        "environment": environment,
        "steps_to_reproduce": steps_to_reproduce,
        "actual_result": actual_result,
        "expected_result": expected_result,
    }
    if related_case_id:
        bug_dict["related_case_id"] = related_case_id
    if run_id:
        bug_dict["run_id"] = int(run_id)
    if reporter:
        bug_dict["reporter"] = reporter
    if extra:
        for k, v in extra.items():
            bug_dict.setdefault(k, v)

    effective_source = source if source in db.VALID_BUG_SOURCES else "manual"
    row_id = db.save_bug(project_id or None, bug_dict, source=effective_source)
    return {
        "db_id": row_id,
        "title": bug_dict["title"],
        "severity": bug_dict["severity"],
        "priority": bug_dict["priority"],
        "status": bug_dict["status"],
        "project_id": project_id or None,
        "source": effective_source,
    }


@mcp.tool()
def trigger_test_execution(
    project_id: str,
    base_url: str = "",
    test_case_ids: list[str] | None = None,
    env_types: list[str] | None = None,
    mode: str = "tc_driven",
    headless: bool = True,
    walkthrough_config: dict | None = None,
) -> dict:
    """Spawn a detached test-execution run for ``project_id``.

    Mirrors the same subprocess dispatch the Flask
    ``/test-execution`` POST handler uses (see
    ``routes/execution.py``). Returns immediately with the
    ``config_id``; poll via :func:`list_execution_runs` /
    :func:`get_execution_run` once the worker writes the run row.

    Args:
        project_id: 32-char project id. Required.
        base_url: Site the runner points Playwright at. Required for
            ``tc_driven`` mode. Walkthrough mode falls back to
            ``walkthrough_config["start_urls"]`` if ``base_url`` is
            absent.
        test_case_ids: Subset of TC external_ids (``TC-001`` style).
            Omit / empty to run every TC in the project.
        env_types: Like ``["web"]`` or ``["mobile_web"]``. Defaults
            ``["web"]``.
        mode: ``tc_driven`` (default) or ``walkthrough``. Walkthrough
            also requires ``WALKTHROUGH_MODE_ENABLED`` in the host's
            environment — the worker raises otherwise.
        headless: Default ``True``. Production on Render always runs
            headless; an MCP client on a desktop can flip this off.
        walkthrough_config: Optional dict spliced into
            ``config_payload["walkthrough"]``: ``start_urls``,
            ``max_pages``, ``device_timeout_ms``, etc. See
            :class:`engine.walkthrough_runner.WalkthroughRunner`.

    Returns the ``config_id``, the worker PID, the resolved mode,
    item count, and the paths to the on-disk config + log files.

    Raises:
        ValueError: missing project_id, unknown mode, no matching TCs,
            or missing base_url / start_urls.
        RuntimeError: when the per-session concurrency cap
            (:data:`MCP_MAX_CONCURRENT_RUNS`) is already saturated.
    """
    if not project_id:
        raise ValueError("trigger_test_execution: project_id is required")
    mode = (mode or "tc_driven").strip().lower()
    if mode not in ("tc_driven", "walkthrough"):
        raise ValueError(
            f"trigger_test_execution: unknown mode '{mode}' — "
            "expected 'tc_driven' or 'walkthrough'"
        )
    env_types = list(env_types or ["web"])

    pending_dir = os.path.join(STORAGE_ROOT, "automation_runs", "_pending")
    active = count_active_subprocess_runs(pending_dir, MCP_SESSION_ID)
    if active >= MCP_MAX_CONCURRENT_RUNS:
        raise RuntimeError(
            f"trigger_test_execution: {active} MCP run(s) already in "
            f"flight (cap is {MCP_MAX_CONCURRENT_RUNS}). Wait for them "
            "to finish, or check the _pending/ directory for stuck configs."
        )

    items_data: list[dict] = []
    if mode == "tc_driven":
        all_tcs = db.load_test_cases(project_id) or []
        if test_case_ids:
            wanted = {str(x) for x in test_case_ids}
            items_data = [t for t in all_tcs if str(t.get("id")) in wanted]
        else:
            items_data = list(all_tcs)
        if not items_data:
            raise ValueError(
                "trigger_test_execution: no test cases matched the request "
                "— check project_id / test_case_ids"
            )
        if not base_url:
            raise ValueError(
                "trigger_test_execution: base_url is required for tc_driven mode"
            )

    walkthrough_block: dict = {}
    if mode == "walkthrough":
        cfg = dict(walkthrough_config or {})
        start_urls = list(cfg.get("start_urls") or
                          ([base_url] if base_url else []))
        if not start_urls:
            raise ValueError(
                "trigger_test_execution: walkthrough mode requires "
                "base_url or walkthrough_config.start_urls"
            )
        cfg["start_urls"] = start_urls
        cfg.setdefault("test_cases", [])
        walkthrough_block = cfg

    os.makedirs(pending_dir, exist_ok=True)
    config_id = (datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_")
                 + uuid.uuid4().hex[:6])
    config_path = os.path.join(pending_dir, f"{config_id}.json")
    worker_log = os.path.join(pending_dir, f"{config_id}.log")

    selected_ids = [it.get("id") for it in items_data] if items_data else []
    config_payload = {
        "config_id": config_id,
        "storage_root": STORAGE_ROOT,
        "base_url": base_url,
        "site_url": base_url,
        "items_data": items_data,
        "selected_ids": selected_ids,
        "env_types": env_types,
        "manual_statuses": {},
        "manual_bug_refs": {},
        "session_id": MCP_SESSION_ID,
        "project_id": project_id,
        "tester_id": "mcp",
        "tester_name": "MCP Client",
        "testing_types": [],
        "headless": bool(headless),
        "record_video": False,
        "affects_version": "",
        "source": "test_cases",
        "item_type": "test_cases",
        "envs": {et: {"environment": et} for et in env_types},
        "runner_kwargs": {
            "base_url": base_url,
            "headless": bool(headless),
            "record_video": False,
        },
        "credentials": None,
        "mode": mode,
        "tc_binding": "ignore",
        "walkthrough": walkthrough_block,
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config_payload, f)

    log_fh = open(worker_log, "w", encoding="utf-8")
    # ``start_new_session=True`` is the same flag the Flask dispatcher
    # uses to detach the worker on Linux. On Windows it's a no-op flag
    # for Popen — the subprocess still runs, just without POSIX session
    # detach semantics. Either way the worker survives this caller.
    proc = subprocess.Popen(
        [sys.executable, "-m", "engine.runner_worker", config_path],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
        cwd=os.path.dirname(STORAGE_ROOT) or None,
    )
    return {
        "config_id": config_id,
        "pid": proc.pid,
        "mode": mode,
        "project_id": project_id,
        "items": len(items_data),
        "env_types": env_types,
        "config_path": config_path,
        "log_path": worker_log,
    }


# ── Tedgie chat tool ───────────────────────────────────────────────

@mcp.tool()
def tedgie_ask(
    question: str,
    lang: str = "en",
    project_id: str | None = None,
) -> dict:
    """Ask the Tedgie QA assistant a question and get a single reply.

    Stateless — every call is a fresh request. The MCP client owns
    the conversation history; we do not retain anything between
    invocations. If the host has ``ANTHROPIC_API_KEY`` set the AI
    path is used (with the prompt-cached Tedgie persona system blocks
    from :mod:`engine.chatbot`); otherwise the rule-based dispatcher
    answers.

    Args:
        question: Free-form text. Empty / whitespace-only raises.
        lang: ``en`` or ``ua`` — the Tedgie persona ships both. Other
            values fall back to ``en`` inside the chatbot.
        project_id: Reserved for future project-aware prompts. The
            current chatbot persona is project-agnostic so this value
            is accepted but does not change the answer yet — kept on
            the signature so a future PR can wire it without a
            breaking API change.

    Returns the same dict the Flask ``/chat`` endpoint produces:
    ``{text, intent, suggestions, follow_up}``.
    """
    if not question or not str(question).strip():
        raise ValueError("tedgie_ask: 'question' is required")
    _ = project_id  # reserved (see docstring)
    return chatbot.respond_dict(str(question).strip(), lang or "en")


# ── Recorder write tool (PR-B) ─────────────────────────────────────

# Recorder integration is feature-flagged off by default. ``tfg record``
# CLI users have the flag flipped on for their host; the public Render
# deployment keeps it off until pilot graduates to global rollout.
def _recorder_enabled() -> bool:
    return os.environ.get("RECORDER_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on")


@mcp.tool()
def record_steps_attach(
    project_id: str,
    tc_external_id: str,
    steps: list[dict],
) -> dict:
    """Attach a recorded step list to a TC's ``automation_steps_json``.

    Receives the output of :mod:`engine.recorder_parser` (one
    AutomationStep payload per item), serialises it to JSON, and writes
    it through :func:`engine.db.update_tc_automation_steps`. The runner
    then prefers these over the heuristic text parse on the next run.

    Args:
        project_id: 32-char project id from :func:`list_projects`.
            Required.
        tc_external_id: TC external id (``TC-001`` / ``SC1_002`` style),
            as returned by :func:`list_test_cases`. Required.
        steps: ordered list of dicts shaped like
            ``{"action", "target", "value", "raw", "comment"}``. Each
            item must have a non-empty ``action`` — items without are
            silently dropped server-side as a defensive filter. Passing
            ``[]`` clears the recording, restoring the heuristic path.

    Returns:
        ``{"ok": True, "tc_external_id": ..., "steps_count": N}``
        when the TC was found and updated. ``{"ok": False, "reason":
        "tc_not_found"}`` if the lookup misses — caller surfaces this
        to the operator instead of treating it as an error.

    Raises:
        RuntimeError: when ``RECORDER_ENABLED`` is not flipped on. The
            production deployment refuses to accept recordings until
            pilot graduates — matches the CLI's own feature-flag check.
        ValueError: missing project_id / tc_external_id.
    """
    if not _recorder_enabled():
        raise RuntimeError(
            "record_steps_attach: RECORDER_ENABLED is not set — this "
            "host is not in the Recorder pilot. Flip the env var on "
            "the MCP server boot to opt in.")
    if not project_id or not str(project_id).strip():
        raise ValueError("record_steps_attach: project_id is required")
    if not tc_external_id or not str(tc_external_id).strip():
        raise ValueError("record_steps_attach: tc_external_id is required")
    if not isinstance(steps, list):
        raise ValueError("record_steps_attach: steps must be a list")

    cleaned: list[dict] = []
    for item in steps:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").strip()
        if not action:
            continue
        # PR-A fields — multi-locator fallback + Page Object registry key.
        # PR-C fields — kind/assertion_type for Assertion Mode.
        # Pass-through so the recorder's payload survives the MCP round-trip;
        # _decode_recorded_steps will apply safe defaults if any are missing.
        alts_raw = item.get("target_alternates") or []
        alts = [str(a) for a in alts_raw
                if isinstance(a, (str, int, float)) and str(a)] \
                    if isinstance(alts_raw, list) else []
        cleaned.append({
            "action": action,
            "target": str(item.get("target") or ""),
            "value": str(item.get("value") or ""),
            "raw": str(item.get("raw") or ""),
            "comment": str(item.get("comment") or ""),
            "target_alternates": alts,
            "locator_label": str(item.get("locator_label") or ""),
            "kind": str(item.get("kind") or "action"),
            "assertion_type": str(item.get("assertion_type") or ""),
        })

    project_id_norm = str(project_id).strip()
    ok = db.update_tc_automation_steps(
        project_id_norm,
        str(tc_external_id).strip(),
        cleaned,
    )
    if not ok:
        return {"ok": False, "reason": "tc_not_found",
                "project_id": project_id, "tc_external_id": tc_external_id}

    # PR-C — populate the Locator table so the runner can promote
    # last-success strategies across runs. Without this the table stays
    # empty and PR-A's chain walk works purely off step-carried
    # alternates (no learning). Best-effort: registry failures must NOT
    # roll back the attach.
    _register_recorder_locators(project_id_norm, cleaned)

    return {
        "ok": True,
        "project_id": project_id,
        "tc_external_id": tc_external_id,
        "steps_count": len(cleaned),
    }


def _register_recorder_locators(project_id: str, steps: list[dict]) -> int:
    """Feed step locator chains into the Page Object DB.

    For every step carrying both ``locator_label`` and at least one
    locator value, register the ranked candidate list. Returns the
    count of rows touched. Errors are swallowed and logged so a
    registry hiccup never blocks the attach.
    """
    try:
        from engine.locator_registry import (LocatorCandidate,
                                              register_candidates,
                                              strategy_of)
    except Exception:
        return 0
    if not project_id:
        return 0
    touched = 0
    for step in steps:
        label = (step.get("locator_label") or "").strip()
        primary = (step.get("target") or "").strip()
        alts = step.get("target_alternates") or []
        if not (label and primary):
            continue
        all_targets = [primary]
        for a in alts:
            a_s = str(a).strip()
            if a_s and a_s not in all_targets:
                all_targets.append(a_s)
        cands = [LocatorCandidate(strategy=strategy_of(t), value=t)
                  for t in all_targets]
        try:
            if register_candidates(project_id, label, cands):
                touched += 1
        except Exception:
            continue
    return touched


# ── PR-F Phase 2 — active browser driver ───────────────────────────
#
# These tools let an agent drive the operator's real, logged-in browser
# through the recorder extension — the same capability the Claude in
# Chrome extension exposes, built on the extension's existing CDP
# session. The channel is the DB-backed command queue in engine.db: a
# tool enqueues a command and blocks on its result row while the
# extension (polling Flask) executes it against the bound tab.
#
# Consent + safety: nothing is drivable until the operator opens the
# handoff URL from browser_control_start in a browser that has the
# extension installed and BROWSER_CONTROL_ENABLED set. Only structured
# verbs exist — there is deliberately no arbitrary-JS `eval`.


def _await_browser_command(command_id: str) -> dict:
    """Block until a queued command reaches a terminal state or times
    out. Returns ``{ok, result}`` or ``{ok: False, error}``."""
    import time as _t
    deadline = _t.monotonic() + BROWSER_CMD_TIMEOUT_S
    while True:
        cmd = db.get_browser_command(command_id)
        if cmd is None:
            return {"ok": False, "error": "command_lost"}
        if cmd["status"] == "done":
            return {"ok": True, "result": cmd.get("result") or {}}
        if cmd["status"] == "error":
            return {"ok": False, "error": cmd.get("error") or "command_failed"}
        if _t.monotonic() >= deadline:
            return {"ok": False, "error": "timeout",
                    "hint": "browser did not respond — is the control tab "
                            "still open and attached? Check browser_control_status."}
        _t.sleep(0.25)


def _enqueue_and_await(token: str, verb: str, params: dict) -> dict:
    """Validate the session is live, enqueue a command, await its result."""
    sess = db.get_browser_control_session(token)
    if sess is None:
        return {"ok": False, "error": "unknown_or_stopped_session"}
    if not sess.get("live"):
        return {"ok": False, "error": "browser_not_attached",
                "hint": "Ask the operator to open the open_url from "
                        "browser_control_start; then retry."}
    cmd_id = db.enqueue_browser_command(token, verb, params)
    if cmd_id is None:
        return {"ok": False, "error": "enqueue_failed"}
    return _await_browser_command(cmd_id)


@mcp.tool()
def browser_control_start(project_id: str, start_url: str) -> dict:
    """Begin a live browser-control session bound to a project.

    Mints a control token and returns an ``open_url`` — the operator
    opens it in Chrome (with the TestForTge Recorder extension, and the
    host running BROWSER_CONTROL_ENABLED=1). Once open, the extension
    attaches and starts executing the ``browser_*`` commands you issue
    with this token.

    Args:
        project_id: 32-char hex id from :func:`list_projects`.
        start_url: http(s) URL the control tab should open at.

    Returns ``{ok, token, open_url, next}`` — poll
    :func:`browser_control_status` until ``live`` is true, then drive.
    """
    if not project_id or not start_url:
        raise ValueError("browser_control_start: project_id and start_url required")
    if not start_url.lower().startswith(("http://", "https://")):
        raise ValueError("browser_control_start: start_url must be http(s)")
    import secrets as _secrets
    token = _secrets.token_urlsafe(32)
    if db.create_browser_control_session(project_id, token) is None:
        return {"ok": False, "error": "unknown_project"}
    frag = f"testfortge-control-token={token}"
    if TFG_INSTANCE_URL:
        frag += f"&testfortge-poll-url={TFG_INSTANCE_URL}/api/browser/poll"
        frag += f"&testfortge-result-url={TFG_INSTANCE_URL}/api/browser/result"
    sep = "&" if "#" in start_url else "#"
    open_url = f"{start_url}{sep}{frag}"
    return {
        "ok": True,
        "token": token,
        "open_url": open_url,
        "instance_url": TFG_INSTANCE_URL,
        "next": "Ask the operator to open open_url in Chrome (extension "
                "installed, BROWSER_CONTROL_ENABLED=1). Then call "
                "browser_control_status(token) until live=true.",
    }


@mcp.tool()
def browser_control_status(token: str) -> dict:
    """Report whether a control session is live (browser attached).

    ``live`` is true when the extension polled within the liveness window
    — i.e. the operator's tab is open and driving. ``last_seen_seconds``
    is how long ago the last poll was.
    """
    if not token:
        raise ValueError("browser_control_status: token required")
    sess = db.get_browser_control_session(token)
    if sess is None:
        return {"ok": False, "active": False, "error": "unknown_or_stopped"}
    return {
        "ok": True,
        "active": True,
        "live": sess.get("live", False),
        "project_id": sess.get("project_id", ""),
        "last_seen_seconds": sess.get("last_seen_seconds"),
    }


@mcp.tool()
def browser_navigate(token: str, url: str) -> dict:
    """Navigate the controlled tab to ``url`` (http(s)). Returns the
    landed URL + title once the page load settles."""
    if not url or not url.lower().startswith(("http://", "https://")):
        raise ValueError("browser_navigate: url must be http(s)")
    return _enqueue_and_await(token, "navigate", {"url": url})


@mcp.tool()
def browser_read_page(token: str) -> dict:
    """Snapshot the controlled page for the agent to reason over.

    Returns ``{url, title, elements: [{ref, role, name, text, tag}, ...],
    text_digest}``. Each ``ref`` (``ref_1`` …) is a handle you pass to
    :func:`browser_click` / :func:`browser_fill`. This is the analogue of
    the Claude in Chrome ``read_page`` accessibility tree.
    """
    return _enqueue_and_await(token, "read_page", {})


@mcp.tool()
def browser_click(token: str, ref: str) -> dict:
    """Click the element identified by ``ref`` (from browser_read_page)."""
    if not ref:
        raise ValueError("browser_click: ref required (from browser_read_page)")
    return _enqueue_and_await(token, "click", {"ref": ref})


@mcp.tool()
def browser_fill(token: str, ref: str, text: str) -> dict:
    """Type ``text`` into the input/textarea identified by ``ref``."""
    if not ref:
        raise ValueError("browser_fill: ref required (from browser_read_page)")
    return _enqueue_and_await(token, "fill", {"ref": ref, "text": text or ""})


@mcp.tool()
def browser_wait(token: str, ms: int = 1000) -> dict:
    """Pause the driver for ``ms`` milliseconds (clamped 0–30000) — e.g.
    to let an async view settle before the next browser_read_page."""
    ms = max(0, min(int(ms or 0), 30_000))
    return _enqueue_and_await(token, "wait", {"ms": ms})


@mcp.tool()
def browser_control_stop(token: str) -> dict:
    """End a control session: seal it and drop its pending commands. The
    operator can also just close the tab (the session then goes stale)."""
    if not token:
        raise ValueError("browser_control_stop: token required")
    return {"ok": bool(db.stop_browser_control_session(token))}


# ── Entry ──────────────────────────────────────────────────────────

def main() -> int:
    """Boot the MCP server on stdio. Blocks until the client disconnects."""
    mcp.run()
    return 0
