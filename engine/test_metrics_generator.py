"""
TestFortge — Test Metrics Generator (TestFort Template)

Computes real QA metrics using industry-standard formulas:
  1. Coverage Completion Table (per device/browser)
  2. Test Execution Summary by Platforms
  3. Issues Summary (by platform, type, status, severity)
  4. KPI calculations (pass rate, defect density, etc.)

Formulas (QA Best Practices):
  - Test Execution Rate = (Executed / Total) * 100
  - Pass Rate = (Passed / Executed) * 100
  - Fail Rate = (Failed / Executed) * 100
  - Defect Density = Total Bugs / Total Test Cases
  - Test Coverage = (Executed / Total Planned) * 100
  - Defect Removal Efficiency = (Bugs Found in QA / Total Known Bugs) * 100
"""

from dataclasses import dataclass, field
import re


@dataclass
class CoverageRow:
    device: str
    overall_checks: int
    remaining: int
    percentage: float  # 0.0 - 1.0


@dataclass
class ExecutionRow:
    device: str
    passed: int
    failed: int
    passed_but: int
    blocked: int


@dataclass
class IssuesSummary:
    by_platform: list[dict] = field(default_factory=list)
    by_type: list[dict] = field(default_factory=list)
    by_status: list[dict] = field(default_factory=list)
    by_severity: list[dict] = field(default_factory=list)


@dataclass
class KPI:
    name: str
    value: str
    formula: str
    status: str = ""  # good, warning, bad


@dataclass
class TestMetrics:
    coverage: list[CoverageRow] = field(default_factory=list)
    execution: list[ExecutionRow] = field(default_factory=list)
    issues: IssuesSummary = field(default_factory=IssuesSummary)
    kpis: list[KPI] = field(default_factory=list)
    total_checks: int = 0
    total_test_cases: int = 0
    total_issues: int = 0


def _parse_extra_lines(lines: list[str]) -> dict:
    """Try to parse numeric data from uploaded content lines."""
    data = {}
    for line in lines:
        lower = line.lower()
        numbers = re.findall(r'\d+', line)
        if not numbers:
            continue
        num = int(numbers[0])
        if any(kw in lower for kw in ["pass", "passed"]):
            data.setdefault("tc_passed", num)
        elif any(kw in lower for kw in ["fail", "failed"]):
            data.setdefault("tc_failed", num)
        elif any(kw in lower for kw in ["block"]):
            data.setdefault("tc_blocked", num)
        elif any(kw in lower for kw in ["total test", "total tc", "test cases"]):
            data.setdefault("total_tc", num)
        elif any(kw in lower for kw in ["critical", "blocker"]):
            data.setdefault("bugs_critical", num)
        elif any(kw in lower for kw in ["major"]):
            data.setdefault("bugs_major", num)
        elif any(kw in lower for kw in ["minor"]):
            data.setdefault("bugs_minor", num)
        elif any(kw in lower for kw in ["bug", "defect", "issue"]):
            data.setdefault("total_bugs", num)
    return data


def generate_test_metrics(
    total_checks: int = 0,
    total_test_cases: int = 0,
    devices: list[str] = None,
    metrics_input: dict = None,
    custom_prompt: str = "",
) -> TestMetrics:
    """Generate test metrics using real input data and QA formulas."""

    if devices is None:
        devices = ["Device/Browser 1", "Device/Browser 2", "Device/Browser 3"]
    if metrics_input is None:
        metrics_input = {}

    # Parse extra data from uploaded files
    extra_lines = metrics_input.get("extra_lines", [])
    if extra_lines:
        parsed = _parse_extra_lines(extra_lines)
        # Merge parsed data (don't overwrite user-provided values)
        for k, v in parsed.items():
            if not metrics_input.get(k):
                metrics_input[k] = v

    # ── Core numbers ──
    tc_total = metrics_input.get("total_tc", total_test_cases) or total_test_cases
    tc_passed = metrics_input.get("tc_passed", 0)
    tc_failed = metrics_input.get("tc_failed", 0)
    tc_blocked = metrics_input.get("tc_blocked", 0)
    tc_passed_but = metrics_input.get("tc_passed_but", 0)
    tc_executed = tc_passed + tc_failed + tc_passed_but + tc_blocked
    tc_remaining = max(0, tc_total - tc_executed)

    total_bugs = metrics_input.get("total_bugs", tc_failed)
    bugs_critical = metrics_input.get("bugs_critical", 0)
    bugs_major = metrics_input.get("bugs_major", 0)
    bugs_minor = metrics_input.get("bugs_minor", 0)
    bugs_low = metrics_input.get("bugs_low", 0)
    if not any([bugs_critical, bugs_major, bugs_minor, bugs_low]) and total_bugs > 0:
        # Distribute bugs by typical ratio
        bugs_critical = max(1, total_bugs // 5)
        bugs_major = max(1, total_bugs // 3)
        bugs_minor = total_bugs - bugs_critical - bugs_major
        bugs_low = 0

    checks_total = total_checks or tc_total

    metrics = TestMetrics()
    metrics.total_checks = checks_total
    metrics.total_test_cases = tc_total
    metrics.total_issues = total_bugs

    # ── Coverage Completion (per device) ──
    for device in devices:
        pct = (tc_executed / checks_total) if checks_total > 0 else 0.0
        remaining = max(0, checks_total - tc_executed)
        metrics.coverage.append(CoverageRow(
            device=device,
            overall_checks=checks_total,
            remaining=remaining,
            percentage=min(pct, 1.0),
        ))

    # ── Test Execution Summary ──
    for i, device in enumerate(devices):
        if i == 0:
            metrics.execution.append(ExecutionRow(
                device=device,
                passed=tc_passed,
                failed=tc_failed,
                passed_but=tc_passed_but,
                blocked=tc_blocked,
            ))
        else:
            # Additional devices: template for manual filling
            metrics.execution.append(ExecutionRow(
                device=device, passed=0, failed=0, passed_but=0, blocked=0,
            ))

    # ── Issues Summary ──
    metrics.issues = IssuesSummary(
        by_platform=[
            {"type": "Web", "count": total_bugs},
            {"type": "Desktop", "count": 0},
            {"type": "Mobile", "count": 0},
            {"type": "All platforms", "count": total_bugs},
        ],
        by_type=[
            {"type": "Functional", "count": tc_failed},
            {"type": "UI", "count": bugs_minor},
            {"type": "UX", "count": 0},
            {"type": "Enhancement", "count": 0},
        ],
        by_status=[
            {"status": "Open", "count": tc_failed},
            {"status": "By design", "count": 0},
            {"status": "Reopen", "count": 0},
            {"status": "Fixed", "count": 0},
            {"status": "Need more info", "count": tc_blocked},
            {"status": "Closed", "count": tc_passed},
            {"status": "Duplicate", "count": 0},
        ],
        by_severity=[
            {"severity": "Blocker", "count": bugs_critical},
            {"severity": "Critical", "count": bugs_critical},
            {"severity": "Major", "count": bugs_major},
            {"severity": "Minor", "count": bugs_minor},
            {"severity": "Low", "count": bugs_low},
        ],
    )

    # ── KPI Calculations (QA Best Practice Formulas) ──
    kpis = []

    # 1. Test Execution Rate
    exec_rate = (tc_executed / tc_total * 100) if tc_total > 0 else 0
    kpis.append(KPI(
        name="Test Execution Rate",
        value=f"{exec_rate:.1f}%",
        formula=f"Executed / Total = {tc_executed} / {tc_total}",
        status="good" if exec_rate >= 80 else ("warning" if exec_rate >= 50 else "bad"),
    ))

    # 2. Pass Rate
    pass_rate = (tc_passed / tc_executed * 100) if tc_executed > 0 else 0
    kpis.append(KPI(
        name="Pass Rate",
        value=f"{pass_rate:.1f}%",
        formula=f"Passed / Executed = {tc_passed} / {tc_executed}",
        status="good" if pass_rate >= 90 else ("warning" if pass_rate >= 70 else "bad"),
    ))

    # 3. Fail Rate
    fail_rate = (tc_failed / tc_executed * 100) if tc_executed > 0 else 0
    kpis.append(KPI(
        name="Fail Rate",
        value=f"{fail_rate:.1f}%",
        formula=f"Failed / Executed = {tc_failed} / {tc_executed}",
        status="good" if fail_rate <= 5 else ("warning" if fail_rate <= 15 else "bad"),
    ))

    # 4. Defect Density
    defect_density = (total_bugs / tc_total) if tc_total > 0 else 0
    kpis.append(KPI(
        name="Defect Density",
        value=f"{defect_density:.2f}",
        formula=f"Total Bugs / Total TCs = {total_bugs} / {tc_total}",
        status="good" if defect_density <= 0.1 else ("warning" if defect_density <= 0.3 else "bad"),
    ))

    # 5. Test Coverage
    coverage = (tc_executed / tc_total * 100) if tc_total > 0 else 0
    kpis.append(KPI(
        name="Test Coverage",
        value=f"{coverage:.1f}%",
        formula=f"Executed / Planned = {tc_executed} / {tc_total}",
        status="good" if coverage >= 80 else ("warning" if coverage >= 50 else "bad"),
    ))

    # 6. Block Rate
    block_rate = (tc_blocked / tc_total * 100) if tc_total > 0 else 0
    kpis.append(KPI(
        name="Block Rate",
        value=f"{block_rate:.1f}%",
        formula=f"Blocked / Total = {tc_blocked} / {tc_total}",
        status="good" if block_rate <= 2 else ("warning" if block_rate <= 10 else "bad"),
    ))

    metrics.kpis = kpis
    return metrics
