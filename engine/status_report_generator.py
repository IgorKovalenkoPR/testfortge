"""
TestFortge — Status Report Generator (TestFort Template)

Generates a daily/weekly status report based on:
  - Actual project stats (test cases written/executed, coverage)
  - User-provided data (features tested, platforms, bugs)
  - Uploaded attachments (parsed content)
  - Custom prompt instructions
"""

from dataclasses import dataclass, field
from datetime import date


@dataclass
class StatusReport:
    report_date: str
    what_done: list[str] = field(default_factory=list)
    what_planned: list[str] = field(default_factory=list)
    bugs_summary: str = ""
    custom_notes: str = ""
    stats_summary: str = ""


def generate_status_report(
    project_name: str,
    report_date: str = "",
    features_tested: list[str] = None,
    platforms_tested: list[str] = None,
    bugs_found: list[str] = None,
    next_steps: list[str] = None,
    custom_prompt: str = "",
    project_stats: dict = None,
    extra_info: str = "",
) -> StatusReport:
    """Generate a status report based on real project data and user input."""
    if not report_date:
        report_date = date.today().strftime("%Y-%m-%d")
    if project_stats is None:
        project_stats = {}

    # ── Build "What has been done" from real data ──
    done_items = []

    # Add real stats from session
    tc_count = project_stats.get("tc_count", 0)
    cl_count = project_stats.get("cl_count", 0)
    stories_count = project_stats.get("stories_count", 0)
    tc_passed = project_stats.get("tc_passed", 0)
    tc_failed = project_stats.get("tc_failed", 0)
    tc_blocked = project_stats.get("tc_blocked", 0)
    tc_passed_but = project_stats.get("tc_passed_but", 0)
    tc_executed = tc_passed + tc_failed + tc_blocked + tc_passed_but

    if stories_count:
        done_items.append(f"Analyzed {stories_count} user stories from project requirements;")
    if tc_count:
        done_items.append(f"Created {tc_count} test cases covering positive, negative, and edge case scenarios;")
    if cl_count:
        done_items.append(f"Created {cl_count} checklist items for quick smoke/regression testing;")
    if tc_executed:
        done_items.append(
            f"Executed {tc_executed} test cases: "
            f"{tc_passed} Passed, {tc_failed} Failed, "
            f"{tc_passed_but} Passed but, {tc_blocked} Blocked;"
        )

    # Add user-provided features/platforms
    if features_tested:
        features_str = ", ".join(features_tested)
        if platforms_tested:
            platforms_str = " and ".join(platforms_tested)
            done_items.append(f"Testing of {features_str} on {platforms_str};")
        else:
            done_items.append(f"Testing of {features_str};")

    if not done_items:
        done_items.append("Testing of the core application functionalities;")

    done_items.append("Proceeded with creating checklist and bug reports for investigated issues.")

    # ── Build "What's planned" ──
    planned_items = next_steps if next_steps else []
    if tc_failed:
        planned_items.insert(0, f"Retest {tc_failed} failed test case(s) after bug fixes.")
    if tc_blocked:
        planned_items.insert(0, f"Unblock {tc_blocked} blocked test case(s) — resolve dependencies.")
    if not planned_items:
        planned_items.append("Continue testing the functionality listed above after bug fixes.")
        planned_items.append("Update test documentation based on new findings.")

    # ── Build bugs summary ──
    bugs_text = ""
    if bugs_found:
        bug_refs = ", ".join(bugs_found)
        bugs_text = f"New bugs raised: {bug_refs}."
        if tc_failed:
            bugs_text += f" Total failed test cases: {tc_failed}."
    elif tc_failed:
        bugs_text = f"Total failed test cases: {tc_failed}. Bug reports pending."
    else:
        bugs_text = "No new bugs raised."

    # ── Stats summary line ──
    stats_parts = []
    if tc_count:
        coverage_pct = round(tc_executed / tc_count * 100) if tc_count > 0 else 0
        stats_parts.append(f"Test execution: {coverage_pct}% ({tc_executed}/{tc_count})")
    if tc_passed and tc_executed:
        pass_rate = round(tc_passed / tc_executed * 100)
        stats_parts.append(f"Pass rate: {pass_rate}%")
    stats_summary = " | ".join(stats_parts)

    # ── Append extra info from uploaded files ──
    if extra_info.strip():
        done_items.append(f"Additional data from attachments: {extra_info[:500]}")

    return StatusReport(
        report_date=report_date,
        what_done=done_items,
        what_planned=planned_items,
        bugs_summary=bugs_text,
        custom_notes=custom_prompt,
        stats_summary=stats_summary,
    )
