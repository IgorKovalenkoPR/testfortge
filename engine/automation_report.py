"""
TestFortge — Automation Report & Metrics
"""
from __future__ import annotations
from dataclasses import asdict


def report_to_dict(report) -> dict:
    d = asdict(report)
    d["pass_rate"] = round(report.pass_rate, 1)
    return d


def compute_automation_metrics(report_dict: dict, total_tc: int) -> dict:
    total_auto = report_dict.get("total", 0)
    passed = report_dict.get("passed", 0)
    failed = report_dict.get("failed", 0)
    blocked = report_dict.get("blocked", 0)
    coverage = (total_auto / total_tc * 100) if total_tc else 0.0
    pass_rate = (passed / total_auto * 100) if total_auto else 0.0
    return {
        "automation_coverage": round(coverage, 1),
        "pass_rate": round(pass_rate, 1),
        "fail_rate": round((failed / total_auto * 100) if total_auto else 0.0, 1),
        "block_rate": round((blocked / total_auto * 100) if total_auto else 0.0, 1),
        "total_automated": total_auto,
        "total_manual": total_tc,
        "duration_ms": report_dict.get("duration_ms", 0),
    }


def detect_flaky(history: list[dict]) -> list[str]:
    """Given list of run dicts, return TC IDs that changed status between runs."""
    if len(history) < 2:
        return []
    per_tc: dict[str, set] = {}
    for run in history:
        for s in run.get("scripts", []):
            per_tc.setdefault(s["tc_id"], set()).add(s["status"])
    return [tc for tc, statuses in per_tc.items() if len(statuses) > 1]
