"""TestFortge — the manual execution walk.

Executing a pack by hand used to mean one bulk form: every item on screen
with a status drop-down beside it, submitted in one go. That form is a
record of a decision, not a way to make one — a tester filling it in has
already done the work somewhere else, and nothing in the product helped
them do it.

This module drives the other shape: one item at a time, with its
preconditions, its numbered steps and its expected result in front of the
tester, and one click per verdict.

State lives in the database, not the session
--------------------------------------------
The queue is stored in the run's ``env_payload``; "where am I" is derived
from which items already have an :class:`ExecutionCaseResult` row. Nothing
is kept in the Flask session.

That matters more than it sounds. A manual walk through 60 checks takes
long enough to span a laptop sleep, a browser restart, or a hand-off to a
colleague on a different machine — and a session-backed cursor loses the
walk in all three. Deriving the position from the results also means the
cursor cannot disagree with them, which a separate counter eventually
would.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from engine.log import get_logger

_logger = get_logger(__name__)

#: The verdicts a tester can record, in the vocabulary the reference
#: corpus uses. "Passed but" marks a pass with a filed cosmetic defect;
#: "Skipped" records that the item was consciously not run, which is a
#: different statement from "Blocked" (could not be run).
VERDICTS = ("Passed", "Passed but", "Failed", "Blocked", "Skipped")

#: Verdicts that count as the item having been exercised. A Skipped item
#: was never looked at, so counting it either way would misreport the run —
#: the same rule the automation ingest applies to its own skips.
EXECUTED_VERDICTS = ("Passed", "Passed but", "Failed", "Blocked")

#: Verdicts that should offer to file a bug.
DEFECT_VERDICTS = ("Failed", "Passed but")


@dataclass
class QueueItem:
    """One row of the walk, flattened from a test case or a checklist item."""
    external_id: str
    kind: str                      # "test_case" | "checklist"
    summary: str = ""
    section: str = ""
    preconditions: str = ""
    steps: list[str] = field(default_factory=list)
    test_data: str = ""
    expected_result: str = ""
    priority: str = ""
    category: str = ""
    #: Hierarchical number, for a checklist item.
    item_num: str = ""

    def to_dict(self) -> dict:
        return {"external_id": self.external_id, "kind": self.kind}


@dataclass
class Progress:
    total: int = 0
    done: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    #: Index of the first item with no verdict yet, or ``total`` when the
    #: walk is finished.
    cursor: int = 0

    @property
    def finished(self) -> bool:
        return self.done >= self.total and self.total > 0

    @property
    def percent(self) -> int:
        return int(round(100 * self.done / self.total)) if self.total else 0

    @property
    def executed(self) -> int:
        return sum(n for verdict, n in self.counts.items()
                   if verdict in EXECUTED_VERDICTS)

    @property
    def pass_rate(self) -> float:
        """Over EXECUTED items only — a skip is not a pass or a failure."""
        passed = self.counts.get("Passed", 0) + self.counts.get("Passed but", 0)
        return round(100.0 * passed / self.executed, 1) if self.executed else 0.0


_STEP_SPLIT_RE = re.compile(r"\n+")
_LEADING_NUM_RE = re.compile(r"^\s*(?:\d+[.)]\s*|[-*•]\s*)")


def split_steps(raw: str) -> list[str]:
    """A ``test_steps`` blob into displayable steps, numbering stripped.

    The exporter owns numbering, so a step arrives either already numbered
    ("1. Go to …") or as one of several lines. Both are handled, and a
    single line carrying the whole numbered list is split on the numbers.
    """
    text = (raw or "").strip()
    if not text:
        return []
    lines = [l for l in _STEP_SPLIT_RE.split(text) if l.strip()]
    if len(lines) == 1:
        parts = re.split(r"(?:^|\s)(?=\d+[.)]\s)", text)
        if len(parts) > 1:
            lines = [p for p in parts if p.strip()]
    return [_LEADING_NUM_RE.sub("", l).strip() for l in lines
            if _LEADING_NUM_RE.sub("", l).strip()]


def build_queue(test_cases: Iterable[Any] = (),
                checklist: Iterable[Any] = (),
                selected: Iterable[str] | None = None) -> list[QueueItem]:
    """Flatten the selected test cases and checklist items into one walk.

    Order is test cases then checklist items, each in the order the pack
    already has them — a tester walking a surface top to bottom is what the
    section ordering was built for, and resorting here would fight it.
    """
    wanted = {str(s) for s in (selected or []) if str(s).strip()}
    out: list[QueueItem] = []

    for tc in test_cases or []:
        ext = str(getattr(tc, "id", "") or "").strip()
        if not ext or (wanted and ext not in wanted):
            continue
        out.append(QueueItem(
            external_id=ext,
            kind="test_case",
            summary=str(getattr(tc, "summary", "") or ""),
            section=str(getattr(tc, "section", "") or ""),
            preconditions=str(getattr(tc, "preconditions", "") or ""),
            steps=split_steps(str(getattr(tc, "test_steps", "") or "")),
            test_data=str(getattr(tc, "test_data", "") or ""),
            expected_result=str(getattr(tc, "expected_result", "") or ""),
            priority=str(getattr(tc, "priority", "") or ""),
            category=str(getattr(tc, "category", "") or ""),
        ))

    for cl in checklist or []:
        ext = str(getattr(cl, "id", "") or "").strip()
        if not ext or (wanted and ext not in wanted):
            continue
        out.append(QueueItem(
            external_id=ext,
            kind="checklist",
            # A checklist row IS the observation — it has no steps and no
            # separate expected result, so the objective carries both.
            summary=str(getattr(cl, "objective", "") or ""),
            section=str(getattr(cl, "section", "") or ""),
            expected_result=str(getattr(cl, "objective", "") or ""),
            priority=str(getattr(cl, "priority", "") or ""),
            category=str(getattr(cl, "category", "") or ""),
            item_num=str(getattr(cl, "item_num", "") or ""),
        ))
    return out


def queue_to_payload(queue: Iterable[QueueItem]) -> list[dict]:
    """The queue as stored in ``ExecutionRun.env_payload['manual_queue']``.

    Only identity is persisted. The content is re-read from the pack on
    every render, so an item edited mid-walk shows its current text rather
    than a copy frozen when the run started.
    """
    return [q.to_dict() for q in queue]


def restore_queue(payload: Iterable[dict],
                  test_cases: Iterable[Any] = (),
                  checklist: Iterable[Any] = ()) -> list[QueueItem]:
    """Rebuild the queue from the stored ids plus the current pack."""
    by_id: dict[tuple[str, str], QueueItem] = {}
    for item in build_queue(test_cases, checklist):
        by_id[(item.kind, item.external_id)] = item

    out: list[QueueItem] = []
    for entry in payload or []:
        if not isinstance(entry, dict):
            continue
        key = (str(entry.get("kind") or "test_case"),
               str(entry.get("external_id") or ""))
        found = by_id.get(key)
        if found is not None:
            out.append(found)
        elif key[1]:
            # The item was deleted from the pack after the run started.
            # Keep a placeholder rather than silently shortening the walk —
            # the run's own totals would otherwise stop adding up.
            out.append(QueueItem(
                external_id=key[1], kind=key[0],
                summary="(this item is no longer in the pack)"))
    return out


def compute_progress(queue: Iterable[QueueItem],
                     results: Iterable[dict]) -> Progress:
    """Where the walk is, derived from the results already recorded."""
    queue = list(queue)
    # Last write wins: a corrected verdict replaces the first one.
    verdicts: dict[str, str] = {}
    for row in results or []:
        ext = str((row or {}).get("case_external_id") or "")
        status = str((row or {}).get("status") or "")
        if ext and status:
            verdicts[ext] = status

    progress = Progress(total=len(queue))
    for item in queue:
        verdict = verdicts.get(item.external_id)
        if verdict:
            progress.done += 1
            progress.counts[verdict] = progress.counts.get(verdict, 0) + 1

    progress.cursor = len(queue)
    for ix, item in enumerate(queue):
        if item.external_id not in verdicts:
            progress.cursor = ix
            break
    return progress


def verdicts_by_item(results: Iterable[dict]) -> dict[str, dict]:
    """``{external_id: {status, notes, bug_report_id}}``, last write wins."""
    out: dict[str, dict] = {}
    for row in results or []:
        ext = str((row or {}).get("case_external_id") or "")
        if not ext:
            continue
        out[ext] = {
            "status": (row or {}).get("status") or "",
            "notes": (row or {}).get("notes") or "",
            "bug_report_id": (row or {}).get("bug_report_id"),
            "evidence_path": (row or {}).get("evidence_path") or "",
        }
    return out


def coerce_verdict(value: Any) -> str:
    """Normalise a submitted verdict, or ``""`` when it is not one."""
    raw = str(value or "").strip()
    for verdict in VERDICTS:
        if raw.lower() == verdict.lower():
            return verdict
    return ""


def run_stats(progress: Progress) -> dict:
    """The ``ExecutionRun.stats`` payload for a finished manual walk."""
    return {
        "mode": "manual",
        "total": progress.total,
        "executed": progress.executed,
        "passed": progress.counts.get("Passed", 0),
        "passed_but": progress.counts.get("Passed but", 0),
        "failed": progress.counts.get("Failed", 0),
        "blocked": progress.counts.get("Blocked", 0),
        "skipped": progress.counts.get("Skipped", 0),
        "pass_rate": progress.pass_rate,
    }


__all__ = [
    "VERDICTS", "EXECUTED_VERDICTS", "DEFECT_VERDICTS",
    "QueueItem", "Progress",
    "split_steps", "build_queue", "queue_to_payload", "restore_queue",
    "compute_progress", "verdicts_by_item", "coerce_verdict", "run_stats",
]
