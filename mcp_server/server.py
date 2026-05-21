"""TestFortge MCP tools — read-only v1.

Six tools, all backed by :mod:`engine.db`:

* :func:`list_projects` — every project the DB knows about
* :func:`list_test_cases` — TCs for a project (filterable by status / trigger)
* :func:`list_bug_reports` — bugs (filterable by project / severity / status / source)
* :func:`list_execution_runs` — recent runs for a project
* :func:`get_execution_run` — one run plus its per-case results
* :func:`walkthrough_findings_stats` — summary of one or more result.json files

The tools return plain JSON-serialisable dicts/lists so the MCP layer
ships them to the client without further conversion. Datetime columns
arrive already ISO-formatted (see :func:`engine.db._row_to_dict`).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy import select

from engine import db
from engine import walkthrough_stats

mcp = FastMCP("testfortge")


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


# ── Entry ──────────────────────────────────────────────────────────

def main() -> int:
    """Boot the MCP server on stdio. Blocks until the client disconnects."""
    mcp.run()
    return 0
