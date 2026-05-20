"""
TestFortge — Bug Report Module (Jira-style, ISTQB-aligned)

Generates structured bug reports following ISTQB defect reporting standards
and Jira-style formatting. Supports linking to failed test cases or
checklist items and exporting to Markdown.

ISTQB-mandatory fields (per ISTQB Foundation Level syllabus, Defect
Management chapter): identifier, title, severity, priority, status,
environment, preconditions, **steps to reproduce**, actual result,
expected result, frequency, found-in build, affects version, attachments,
linked items, reporter. We always emit non-empty values for the
mandatory ones — auto-generated bugs from a failed checklist item used
to ship empty ``preconditions`` and ``steps_to_reproduce`` which made
them un-actionable; that's now closed in :func:`engine.qa_testers`.

Jira best-practice fields layered on top: assignee, component, labels,
comment, reporter, created. Issue type is implicitly "Bug" — every
record in this module is a defect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone


# ── Constants ──────────────────────────────────────────────────────

BUG_SEVERITIES = ["Critical", "Major", "Minor", "Trivial"]

BUG_PRIORITIES = ["Highest", "High", "Medium", "Low", "Lowest"]

BUG_STATUSES = ["Open", "In Progress", "Resolved", "Closed", "Reopened"]

# How often the defect can be reproduced. ISTQB-mandatory field.
# Auto-generated bugs from deterministic real_check / simulator runs are
# always "Always" because the same input produces the same status.
BUG_FREQUENCIES = ["Always", "Sometimes", "Rarely", "Once"]


# ── Data model ─────────────────────────────────────────────────────

@dataclass
class BugReport:
    """A single defect, ISTQB-aligned and Jira-friendly.

    Mandatory ISTQB fields are required positional/keyword args; Jira
    nice-to-haves (assignee, labels, comment, etc.) default to empty so
    the manual create-bug form can submit a partial record without
    breaking auto-generated bugs that fill everything.
    """

    id: str                    # e.g. "BUG-001"
    title: str                 # Short bug title (negated active-voice statement)
    severity: str              # "Critical", "Major", "Minor", "Trivial"
    priority: str              # "Highest", "High", "Medium", "Low", "Lowest"
    status: str                # "Open", "In Progress", "Resolved", "Closed", "Reopened"
    environment: str           # "Windows / Chrome / Desktop / 1920x1080"
    preconditions: str         # State the system must be in before step 1
    steps_to_reproduce: str    # Numbered steps, e.g. "1. Open ...\n2. Click ..."
    actual_result: str         # What actually happened (observed)
    expected_result: str       # What should have happened (per requirement)
    # ── ISTQB-mandatory metadata ──
    frequency: str = "Always"          # Reproducibility: Always/Sometimes/Rarely/Once
    affects_version: str = ""          # Product version where the defect was found
    found_in_build: str = ""           # Build identifier (env + ISO timestamp)
    # ── Linkage / traceability ──
    attachments: list[str] = field(default_factory=list)
    linked_item_id: str = ""   # linked test case or checklist item ID
    linked_item_type: str = "" # "test_case" or "checklist"
    # ── Jira workflow metadata ──
    reporter: str = ""         # tester name (the person who filed the bug)
    assignee: str = ""         # developer who will fix it
    created_at: str = ""       # ISO 8601 UTC
    component: str = ""        # e.g. "Authentication", "Search", "UI"
    labels: list[str] = field(default_factory=list)
    comment: str = ""
    # Optional int row id from the DB (engine.db.BugReport.id). Used by
    # the bulk-edit toolbar on /bug-reports — checkboxes carry this so
    # POST /bugs/bulk can address rows without dragging the slug-style
    # external id through SQL. Defaults to 0 so older sessions and the
    # auto-bug factory don't have to pass it explicitly.
    db_id: int = 0


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


# ── Factory: create bug from a walkthrough finding ─────────────────


def create_bug_from_walkthrough_finding(
    finding: dict,
    *,
    environment_str: str = "",
    tester_name: str = "",
    base_url: str = "",
) -> BugReport:
    """Synthesise a :class:`BugReport` from a walkthrough finding dict.

    Walkthrough mode runs without test cases — the heuristics emit
    free-text findings (see :mod:`engine.walkthrough_runner` module
    docstring for the schema). This factory converts each finding into
    a bug record:

    * ``linked_item_type = "walkthrough"`` so the bug-listing UI can
      filter the audit trail by source
    * ``linked_item_id``  = the synthetic ``WALK-NNN`` id the runner
      attached (stable across reruns of the same walkthrough)
    * ``severity`` / ``priority`` come from
      :func:`engine.bug_template.severity_priority` with the area name
      + url joined as the area-hint blob, so weighted priority still
      kicks in (auth/checkout urls → Highest priority)
    * ``steps_to_reproduce`` is synthesised from the URL: "1. Open URL.
      2. Observe the defect on the page" — adequate for cosmetic and
      a11y findings, augmented with the finding's ``fix_hint`` for
      interaction-shape findings (hamburger / search / form)
    * ``labels`` carries ``defect:<class>`` + ``source:walkthrough`` so
      backlog filtering by failure mode works the same as for the
      TC-driven path

    The bug returned has ``id=""`` — callers assign the next id via
    :func:`generate_bug_id` exactly like the TC-driven factory.
    """
    # Local import keeps the cross-module dependency at the call site
    # instead of the file header; bug_template imports bug_report-side
    # constants in a future revision and we want the import graph to
    # stay acyclic.
    from engine.bug_template import severity_priority

    area     = str(finding.get("area") or "")
    message  = str(finding.get("message") or "")
    cls      = str(finding.get("defect_class") or "unknown")
    sev_from_finding = str(finding.get("severity") or "")
    url      = str(finding.get("url") or "")
    element  = str(finding.get("element") or "")
    fix_hint = str(finding.get("fix_hint") or "")
    user_impact = str(finding.get("user_impact") or "")
    dev_detail  = str(finding.get("dev_detail") or "")
    screenshot  = str(finding.get("screenshot") or "")
    tc_id    = str(finding.get("tc_id") or "")

    sev_computed, pri_computed = severity_priority(cls, area, url)
    # Prefer the finding's explicit severity when it lines up with the
    # ladder (Critical/Major/Minor/Trivial); fall back to the computed
    # one otherwise. The heuristics already set severity per defect
    # class so this normally short-circuits to ``sev_from_finding``.
    severity = sev_from_finding if sev_from_finding in (
        "Critical", "Major", "Minor", "Trivial",
    ) else sev_computed

    title_area = area or "Page"
    title = (
        f"[{title_area}] {message}"
        if message else f"[{title_area}] Walkthrough finding"
    )

    str_lines = [f"1. Open {url or base_url or '<application URL>'}."]
    if element:
        str_lines.append(f"2. Locate the element matching: {element}")
        observe_idx = 3
    else:
        observe_idx = 2
    str_lines.append(
        f"{observe_idx}. Observe the defect described in the actual "
        "result."
    )
    steps_to_reproduce = "\n".join(str_lines)

    actual_lines = [message] if message else []
    if user_impact:
        actual_lines.append(f"User impact: {user_impact}")
    if dev_detail:
        actual_lines.append(f"Developer detail: {dev_detail}")
    actual_result = "\n\n".join(actual_lines) or "Walkthrough finding"

    expected_lines = [
        "The element should render and behave correctly according to "
        "the design and accessibility expectations."
    ]
    if fix_hint:
        expected_lines.append(f"Suggested fix direction: {fix_hint}")
    expected_result = "\n\n".join(expected_lines)

    labels = [f"defect:{cls}", "source:walkthrough"]
    if area:
        labels.append(f"area:{area.lower().replace(' ', '_')}")

    attachments = [screenshot] if screenshot else []

    return BugReport(
        id="",
        title=title,
        severity=severity,
        priority=pri_computed,
        status="Open",
        environment=environment_str,
        preconditions=(
            f"Walkthrough run starting at {base_url}."
            if base_url else "Walkthrough run."
        ),
        steps_to_reproduce=steps_to_reproduce,
        actual_result=actual_result,
        expected_result=expected_result,
        frequency="Always",
        attachments=attachments,
        linked_item_id=tc_id,
        linked_item_type="walkthrough",
        reporter=tester_name,
        assignee="",
        created_at=datetime.now(timezone.utc).isoformat(),
        component=area,
        labels=labels,
        comment="",
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
        "frequency": bug.frequency,
        "affects_version": bug.affects_version,
        "found_in_build": bug.found_in_build,
        "attachments": list(bug.attachments),
        "linked_item_id": bug.linked_item_id,
        "linked_item_type": bug.linked_item_type,
        "reporter": bug.reporter,
        "assignee": bug.assignee,
        "created_at": bug.created_at,
        "component": bug.component,
        "labels": list(bug.labels),
        "comment": bug.comment,
        "db_id": bug.db_id,
    }


def dict_to_bug(d: dict) -> BugReport:
    """Reconstruct a BugReport from a dictionary.

    Tolerates older snapshots that pre-date the ISTQB metadata fields:
    ``frequency`` / ``affects_version`` / ``found_in_build`` default to
    empty (or "Always" for frequency) when missing so a project saved
    before this revision still loads cleanly.
    """
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
        frequency=d.get("frequency", "Always"),
        affects_version=d.get("affects_version", ""),
        found_in_build=d.get("found_in_build", ""),
        attachments=d.get("attachments", []),
        linked_item_id=d.get("linked_item_id", ""),
        linked_item_type=d.get("linked_item_type", ""),
        reporter=d.get("reporter", ""),
        assignee=d.get("assignee", ""),
        created_at=d.get("created_at", ""),
        component=d.get("component", ""),
        labels=d.get("labels", []),
        comment=d.get("comment", ""),
        db_id=int(d.get("db_id") or 0),
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

    # Summary table — ISTQB-mandatory metadata first, Jira workflow second.
    lines.append("| Field | Value |")
    lines.append("|-------|-------|")
    lines.append(f"| Severity | {bug.severity} |")
    lines.append(f"| Priority | {bug.priority} |")
    lines.append(f"| Status | {bug.status} |")
    lines.append(f"| Frequency | {bug.frequency or 'Always'} |")
    lines.append(f"| Environment | {bug.environment} |")
    if bug.affects_version:
        lines.append(f"| Affects Version | {bug.affects_version} |")
    if bug.found_in_build:
        lines.append(f"| Found in Build | {bug.found_in_build} |")
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


# ── Step-list normalisation ────────────────────────────────────────

_STEP_PREFIX_RE = re.compile(r"^\s*(?:\d+[.)]\s*|[-*]\s+|step\s*\d+\s*[:.\-]?\s*)",
                             re.IGNORECASE)


def normalize_steps_to_numbered_list(text: str) -> str:
    """Coerce any free-form steps blob into a clean ``1. … 2. …`` list.

    The QA Team Lead module hands us steps as either a single multi-line
    string with leading numbers (``"1. Open\\n2. Click"``), a bullet list,
    or a single sentence. ISTQB and Jira both expect a numbered list, so
    we normalise here once and trust the rest of the pipeline.

    Empty/whitespace-only input returns ``""`` so callers can decide
    whether to synthesise a fallback.
    """
    if not text or not text.strip():
        return ""

    # Split on hard newlines first; then on " 1. " / "; " etc. fallbacks
    # only if the whole blob is one line.
    raw_lines = [ln for ln in (text.splitlines()) if ln.strip()]
    if len(raw_lines) <= 1:
        # Try inline numbered split: "1. foo 2. bar 3. baz".
        single = raw_lines[0] if raw_lines else text.strip()
        inline = re.split(r"(?<!\d)\b(\d+)[.)]\s+", " " + single)
        # re.split with capturing group returns [pre, num, body, num, body, ...]
        if len(inline) >= 5:
            steps = []
            i = 1
            while i < len(inline) - 1:
                body = (inline[i + 1] or "").strip()
                if body:
                    steps.append(body)
                i += 2
            if steps:
                return "\n".join(f"{idx}. {s.rstrip('.')}" for idx, s in enumerate(steps, 1))
        # Otherwise treat the whole thing as a single step.
        body = _STEP_PREFIX_RE.sub("", single).strip().rstrip(".")
        return f"1. {body}" if body else ""

    cleaned = []
    for ln in raw_lines:
        body = _STEP_PREFIX_RE.sub("", ln).strip().rstrip(".")
        if body:
            cleaned.append(body)
    if not cleaned:
        return ""
    return "\n".join(f"{i}. {s}" for i, s in enumerate(cleaned, 1))
