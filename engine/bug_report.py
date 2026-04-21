"""
TestFortge — Bug Report Module (Jira-style, ISTQB-aligned)

Generates structured bug reports following ISTQB defect reporting standards
and Jira-style formatting. Supports linking to failed test cases or
checklist items and exporting to Markdown.

Fields follow the standard Jira bug template:
  ID | Title | Severity | Priority | Status | Environment
  Preconditions | Steps to Reproduce | Actual Result | Expected Result
  Attachments | Linked Items | Reporter | Assignee | Component | Labels
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


# ── Constants ──────────────────────────────────────────────────────

BUG_SEVERITIES = ["Critical", "Major", "Minor", "Trivial"]

BUG_PRIORITIES = ["Highest", "High", "Medium", "Low", "Lowest"]

BUG_STATUSES = ["Open", "In Progress", "Resolved", "Closed", "Reopened"]


# ── Data model ─────────────────────────────────────────────────────

@dataclass
class BugReport:
    id: str                    # e.g. "BUG-001"
    title: str                 # Short bug title
    severity: str              # "Critical", "Major", "Minor", "Trivial"
    priority: str              # "Highest", "High", "Medium", "Low", "Lowest"
    status: str                # "Open", "In Progress", "Resolved", "Closed", "Reopened"
    environment: str           # "Windows / Chrome / Desktop 1920x1080"
    preconditions: str
    steps_to_reproduce: str    # numbered steps
    actual_result: str
    expected_result: str
    attachments: list[str] = field(default_factory=list)
    linked_item_id: str = ""   # linked test case or checklist item ID
    linked_item_type: str = "" # "test_case" or "checklist"
    reporter: str = ""         # tester name
    assignee: str = ""
    created_at: str = ""
    component: str = ""        # e.g. "Authentication", "Search", "UI"
    labels: list[str] = field(default_factory=list)
    comment: str = ""


# ── ID generation ──────────────────────────────────────────────────

def generate_bug_id(existing_bugs: list[BugReport]) -> str:
    """Return the next sequential bug ID like 'BUG-001', 'BUG-002', etc."""
    if not existing_bugs:
        return "BUG-001"

    max_num = 0
    for bug in existing_bugs:
        try:
            num = int(bug.id.split("-", 1)[1])
            if num > max_num:
                max_num = num
        except (IndexError, ValueError):
            continue

    return f"BUG-{max_num + 1:03d}"


# ── Factory: create bug from a failed item ─────────────────────────

_PRIORITY_TO_SEVERITY = {
    "High": "Major",
    "Medium": "Minor",
    "Low": "Trivial",
}


def create_bug_from_failed_item(
    item,
    item_type: str,
    environment_str: str = "",
    tester_name: str = "",
    comment: str = "",
) -> BugReport:
    """Create a BugReport pre-filled from a failed test case or checklist item.

    Parameters
    ----------
    item : TestCase | ChecklistItem
        The item that failed during execution.
    item_type : str
        Either ``"test_case"`` or ``"checklist"``.
    environment_str : str
        Free-text environment description, e.g. "Windows / Chrome / Desktop".
    tester_name : str
        Name of the reporter / tester.
    comment : str
        Optional comment or additional context.

    Returns
    -------
    BugReport
        A new bug report with status "Open" and auto-filled fields.
    """
    severity = _PRIORITY_TO_SEVERITY.get(
        getattr(item, "priority", "Medium"), "Minor"
    )

    if item_type == "test_case":
        title = f"[BUG] {item.summary}"
        steps = getattr(item, "test_steps", "")
        expected = getattr(item, "expected_result", "")
        preconditions = getattr(item, "preconditions", "")
    else:
        # checklist
        title = f"[BUG] {item.objective}"
        steps = ""
        expected = ""
        preconditions = ""

    return BugReport(
        id="",  # assigned later via generate_bug_id
        title=title,
        severity=severity,
        priority=getattr(item, "priority", "Medium"),
        status="Open",
        environment=environment_str,
        preconditions=preconditions,
        steps_to_reproduce=steps,
        actual_result="",
        expected_result=expected,
        attachments=[],
        linked_item_id=getattr(item, "id", ""),
        linked_item_type=item_type,
        reporter=tester_name,
        assignee="",
        created_at=datetime.now(timezone.utc).isoformat(),
        component=getattr(item, "section", ""),
        labels=[],
        comment=comment,
    )


# ── Serialisation helpers ──────────────────────────────────────────

def bug_to_dict(bug: BugReport) -> dict:
    """Convert a BugReport to a plain dictionary for session storage."""
    return {
        "id": bug.id,
        "title": bug.title,
        "severity": bug.severity,
        "priority": bug.priority,
        "status": bug.status,
        "environment": bug.environment,
        "preconditions": bug.preconditions,
        "steps_to_reproduce": bug.steps_to_reproduce,
        "actual_result": bug.actual_result,
        "expected_result": bug.expected_result,
        "attachments": list(bug.attachments),
        "linked_item_id": bug.linked_item_id,
        "linked_item_type": bug.linked_item_type,
        "reporter": bug.reporter,
        "assignee": bug.assignee,
        "created_at": bug.created_at,
        "component": bug.component,
        "labels": list(bug.labels),
        "comment": bug.comment,
    }


def dict_to_bug(d: dict) -> BugReport:
    """Reconstruct a BugReport from a dictionary."""
    return BugReport(
        id=d.get("id", ""),
        title=d.get("title", ""),
        severity=d.get("severity", "Minor"),
        priority=d.get("priority", "Medium"),
        status=d.get("status", "Open"),
        environment=d.get("environment", ""),
        preconditions=d.get("preconditions", ""),
        steps_to_reproduce=d.get("steps_to_reproduce", ""),
        actual_result=d.get("actual_result", ""),
        expected_result=d.get("expected_result", ""),
        attachments=d.get("attachments", []),
        linked_item_id=d.get("linked_item_id", ""),
        linked_item_type=d.get("linked_item_type", ""),
        reporter=d.get("reporter", ""),
        assignee=d.get("assignee", ""),
        created_at=d.get("created_at", ""),
        component=d.get("component", ""),
        labels=d.get("labels", []),
        comment=d.get("comment", ""),
    )


# ── Markdown export ────────────────────────────────────────────────

def export_bug_report_markdown(bug: BugReport) -> str:
    """Export a single bug report as Jira-style Markdown.

    Returns a formatted string suitable for inclusion in a larger
    Markdown document or for standalone display.
    """
    lines: list[str] = []

    # Header
    lines.append(f"## {bug.id}: {bug.title}")
    lines.append("")

    # Summary table
    lines.append("| Field | Value |")
    lines.append("|-------|-------|")
    lines.append(f"| Severity | {bug.severity} |")
    lines.append(f"| Priority | {bug.priority} |")
    lines.append(f"| Status | {bug.status} |")
    lines.append(f"| Environment | {bug.environment} |")
    if bug.component:
        lines.append(f"| Component | {bug.component} |")
    if bug.reporter:
        lines.append(f"| Reporter | {bug.reporter} |")
    if bug.assignee:
        lines.append(f"| Assignee | {bug.assignee} |")
    if bug.created_at:
        lines.append(f"| Created | {bug.created_at} |")
    if bug.labels:
        lines.append(f"| Labels | {', '.join(bug.labels)} |")
    lines.append("")

    # Preconditions
    if bug.preconditions:
        lines.append("### Preconditions")
        lines.append(bug.preconditions)
        lines.append("")

    # Steps to reproduce
    if bug.steps_to_reproduce:
        lines.append("### Steps to Reproduce")
        lines.append(bug.steps_to_reproduce)
        lines.append("")

    # Actual result
    if bug.actual_result:
        lines.append("### Actual Result")
        lines.append(bug.actual_result)
        lines.append("")

    # Expected result
    if bug.expected_result:
        lines.append("### Expected Result")
        lines.append(bug.expected_result)
        lines.append("")

    # Attachments
    if bug.attachments:
        lines.append("### Attachments")
        for att in bug.attachments:
            lines.append(f"- {att}")
        lines.append("")

    # Linked items
    if bug.linked_item_id:
        lines.append("### Linked Items")
        lines.append(
            f"- {bug.linked_item_id} ({bug.linked_item_type}) \u2014 Failed"
        )
        lines.append("")

    # Comment
    if bug.comment:
        lines.append("### Comment")
        lines.append(bug.comment)
        lines.append("")

    return "\n".join(lines)
