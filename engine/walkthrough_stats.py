"""Walkthrough findings statistics — tuning aid for future prod runs.

PR #12 shipped the heuristic battery in
:mod:`engine.walkthrough_runner` with conservative initial severities
in :data:`engine.bug_template.CLASS_SEVERITY`. Some of those rules are
likely too noisy (e.g. axe-core Critical → Critical bug is fine in
theory, but a rendered React error overlay can yield 12 Critical findings
on a single page) — but we won't know which rules need tuning until
real walkthrough runs ship on prod and operators report what looked
like false positives.

This module is the tooling that turns the eventual tuning session from
"grep result.json by hand" into "one command summarises the run":

    python -m engine.walkthrough_stats <result.json> [<result.json> ...]

For each ``walkthrough.*.result.json`` file under ``automation_runs/``
(or whichever paths the operator passes), it prints a table per
``defect_class`` with finding count, severity distribution, and 2–3
sample messages. The deduped view is preferred when both raw +
deduped lists are present, matching the operator-facing UI.

The module also exposes a programmatic API
(:func:`summarise_findings`) so a future ops route (e.g.
``/debug/walkthrough/stats``) can render the same data without
rewriting the aggregation.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Sequence


def _load_findings_from_payload(payload: dict) -> list[dict]:
    """Pick the deduped view when present, raw otherwise.

    The walkthrough worker always writes both lists to ``result.json``;
    falling back to the raw list keeps this tool usable even when
    ``walkthrough_dedup`` errored mid-run and only the raw findings
    landed (see ``runner_worker.py`` exception swallow for that
    path).
    """
    deduped = payload.get("walkthrough_findings_deduped")
    if isinstance(deduped, list) and deduped:
        return [f for f in deduped if isinstance(f, dict)]
    raw = payload.get("walkthrough_findings")
    if isinstance(raw, list):
        return [f for f in raw if isinstance(f, dict)]
    return []


def summarise_findings(
    findings: Iterable[dict],
    *,
    max_samples: int = 3,
) -> dict:
    """Aggregate a findings list into a tuning-friendly summary.

    Returns a dict with this shape::

        {
            "total":      27,
            "by_class": {
                "broken_image": {
                    "count":     12,
                    "severity":  Counter({"Critical": 12}),
                    "areas":     Counter({"Images": 12}),
                    "samples":   ["Broken image on hero", "Broken image ..."],
                },
                ...
            },
            "by_severity": Counter({"Critical": 12, "Major": 9, "Minor": 6}),
            "by_url":      Counter({"https://example.com/": 8, ...}),
        }

    ``samples`` is a small deterministic sample of messages — enough
    for a human eyeballing the table to recognise "yes, those are
    the noisy ones".
    """
    by_class: dict[str, dict] = defaultdict(
        lambda: {
            "count":    0,
            "severity": Counter(),
            "areas":    Counter(),
            "samples":  [],
        }
    )
    by_severity: Counter = Counter()
    by_url: Counter = Counter()
    total = 0
    for f in findings:
        if not isinstance(f, dict):
            continue
        total += 1
        cls = str(f.get("defect_class") or "unknown")
        sev = str(f.get("severity") or "Unknown")
        area = str(f.get("area") or "")
        url = str(f.get("url") or "")
        message = str(f.get("message") or "")

        bucket = by_class[cls]
        bucket["count"] += 1
        bucket["severity"][sev] += 1
        if area:
            bucket["areas"][area] += 1
        if message and len(bucket["samples"]) < max_samples and \
                message not in bucket["samples"]:
            bucket["samples"].append(message)

        by_severity[sev] += 1
        if url:
            by_url[url] += 1

    return {
        "total":       total,
        "by_class":    dict(by_class),
        "by_severity": by_severity,
        "by_url":      by_url,
    }


def format_summary(summary: dict) -> str:
    """Render a summary as a plain-text table.

    Designed for terminal output, not a fancy renderer — three
    sections (per-class, severity totals, top URLs) separated by
    blank lines. Each per-class block lists the severity histogram
    inline so the tuner sees at a glance whether one defect class
    is single-severity-dominant or scattered.
    """
    out: list[str] = []
    total = summary.get("total", 0)
    out.append(f"# Walkthrough findings summary — {total} total\n")

    by_class = summary.get("by_class") or {}
    if not by_class:
        out.append("(no findings to summarise)")
        return "\n".join(out)

    # Sort by descending count so the loudest classes appear at top —
    # the tuner usually cares about those first.
    ordered = sorted(by_class.items(), key=lambda kv: -kv[1]["count"])
    out.append("## By defect class")
    out.append(f"{'defect_class':<28} {'count':>6}  severity histogram")
    out.append("-" * 78)
    for cls, b in ordered:
        sev_hist = ", ".join(
            f"{name}={n}"
            for name, n in sorted(b["severity"].items(),
                                   key=lambda kv: -kv[1])
        ) or "—"
        out.append(f"{cls:<28} {b['count']:>6}  {sev_hist}")
        if b["areas"]:
            top_areas = ", ".join(
                f"{a}={n}" for a, n
                in b["areas"].most_common(3)
            )
            out.append(f"{'  areas:':<28} {'':>6}  {top_areas}")
        for sample in b["samples"]:
            preview = sample if len(sample) <= 64 else sample[:61] + "..."
            out.append(f"{'  · ' + preview:<78}")
        out.append("")

    out.append("## Severity totals")
    for sev, n in summary["by_severity"].most_common():
        out.append(f"  {sev:<10} {n}")

    if summary.get("by_url"):
        out.append("\n## Top URLs (defect-hit count)")
        for url, n in summary["by_url"].most_common(10):
            preview = url if len(url) <= 70 else url[:67] + "..."
            out.append(f"  {n:>4}  {preview}")

    return "\n".join(out)


#: Most a single ``result.json`` may be before it is skipped unread.
#:
#: A real one is tens of kilobytes; the largest measured walkthrough run is
#: under two. The cap is not about those — it is about ``read_text()``
#: having no upper bound at all, so *any* path handed to this function was
#: pulled into memory in full before the JSON parse could reject it. On the
#: 512 MB dyno that is one argument away from an OOM, and a file that never
#: ends (a device, a fifo) is one away from a hang. Bounded the way
#: ``engine.allure_ingest`` bounds an archive member: read one byte past the
#: cap and skip on overflow, rather than asking ``stat()`` first — a size
#: taken before the read is a size that can be wrong by the time of it, and
#: on a fifo there is no size to take.
MAX_RESULT_BYTES = 25 * 1024 * 1024


def summarise_files(paths: Sequence[str | Path]) -> dict:
    """Load one or more ``result.json`` files and produce a combined
    summary. Files that don't parse or don't contain findings are
    silently skipped — the goal is "show me what I have", not strict
    schema validation.

    Any path is accepted, deliberately: the module docstring's contract is
    "or whichever paths the operator passes", and an operator at their own
    shell pointing this at a result.json in Downloads is the normal case.
    The caller that is *not* an operator at a shell —
    ``mcp_server.walkthrough_findings_stats``, reached over the network by
    an agent — confines the paths itself before calling here.
    """
    findings: list[dict] = []
    for raw_path in paths:
        path = Path(raw_path)
        try:
            with path.open("r", encoding="utf-8") as handle:
                raw = handle.read(MAX_RESULT_BYTES + 1)
        except OSError:
            continue
        if len(raw) > MAX_RESULT_BYTES:
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        findings.extend(_load_findings_from_payload(payload))
    return summarise_findings(findings)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        sys.stderr.write(
            "usage: python -m engine.walkthrough_stats "
            "<result.json> [<result.json> ...]\n"
            "       Reads walkthrough findings from one or more "
            "automation_runs/*.result.json files\n"
            "       and prints a tuning-friendly summary.\n"
        )
        return 2
    summary = summarise_files(args)
    sys.stdout.write(format_summary(summary) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover — entry point
    sys.exit(main())
