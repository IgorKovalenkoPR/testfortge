"""Seeding helpers shared by the search tests (not a test module).

``seed_golden(pid)`` writes the golden fixture's test cases, checklist items
and bugs into a project through the product's own persistence functions —
the same path a page save takes — and returns the fixture. Bugs that name a
``related_case`` are linked through ``bug_report.related_case_id``.
"""
from __future__ import annotations

import json
from pathlib import Path

GOLDEN_PATH = Path(__file__).resolve().parent / "fixtures" / "search_golden.json"


def load_golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def seed_golden(project_id: str, *, extra_cases: list | None = None) -> dict:
    from engine import db as _db
    from engine.db import TestCase

    data = load_golden()
    _db.save_test_cases(project_id, [dict(tc) for tc in data["test_cases"]]
                        + [dict(tc) for tc in (extra_cases or [])])
    _db.save_checklist(project_id, [dict(cl) for cl in data["checklist"]])
    with _db.session_scope() as sess:
        case_ids = {ext: cid for cid, ext in sess.query(
            TestCase.id, TestCase.external_id).filter(
                TestCase.project_id == project_id)}
    for bug in data["bugs"]:
        payload = {k: v for k, v in bug.items() if k != "related_case"}
        if bug.get("related_case"):
            payload["related_case_id"] = case_ids.get(bug["related_case"])
        _db.save_bug(project_id, payload)
    return data


def bulk_seed(project_id: str, n: int) -> None:
    """*n* synthetic test cases (for latency measurements)."""
    from engine import db as _db
    topics = ["login", "payment", "cart", "search", "profile", "upload",
              "export", "registration", "password", "filter", "checkout",
              "notification", "report", "settings", "session", "dashboard"]
    verbs = ["fails", "works", "is slow", "shows an error", "validates",
             "crashes", "redirects", "saves", "rejects", "loads"]
    cases = []
    for i in range(n):
        t = topics[i % len(topics)]
        v = verbs[(i // len(topics)) % len(verbs)]
        cases.append({
            "id": f"TC-{i + 1:04d}",
            "summary": f"{t.capitalize()} {v} for variant {i}",
            "section": t.capitalize(),
            "preconditions": f"Data set {i % 7}",
            "test_steps": f"1. Open the {t} page\n2. Perform step {i}\n"
                          f"3. Check the {topics[(i + 3) % len(topics)]} area",
            "expected_result": f"The {t} result is correct for case {i}",
            "priority": ["High", "Medium", "Low"][i % 3],
            "status": ["Passed", "Failed", "Unchecked"][i % 3]})
    _db.save_test_cases(project_id, cases)
