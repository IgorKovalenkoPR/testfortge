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
            If omitted or empty, glob every ``*.result.json`` under the
            repo's ``automation_runs/`` directory.

    The :class:`collections.Counter` values are serialised as plain
    dicts so the MCP layer can JSON-encode the response.
    """
    if not paths:
        repo_root = Path(__file__).resolve().parent.parent
        runs_dir = repo_root / "automation_runs"
        if not runs_dir.is_dir():
            return {"total": 0, "by_class": {}, "by_severity": {}, "by_url": {}}
        paths = [str(p) for p in sorted(runs_dir.glob("*.result.json"))]
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
        cleaned.append({
            "action": action,
            "target": str(item.get("target") or ""),
            "value": str(item.get("value") or ""),
            "raw": str(item.get("raw") or ""),
            "comment": str(item.get("comment") or ""),
        })

    ok = db.update_tc_automation_steps(
        str(project_id).strip(),
        str(tc_external_id).strip(),
        cleaned,
    )
    if not ok:
        return {"ok": False, "reason": "tc_not_found",
                "project_id": project_id, "tc_external_id": tc_external_id}
    return {
        "ok": True,
        "project_id": project_id,
        "tc_external_id": tc_external_id,
        "steps_count": len(cleaned),
    }


# ── Entry ──────────────────────────────────────────────────────────

def main() -> int:
    """Boot the MCP server on stdio. Blocks until the client disconnects."""
    mcp.run()
    return 0
