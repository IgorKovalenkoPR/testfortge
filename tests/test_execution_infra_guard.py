"""All-failed / cannot-execute infrastructure guard in
``routes.execution._reconcile_with_automation``.

Investigation 2026-07-15: a Test Execution run was scoring 0 pass /
255 fail / 0 blocked and auto-filing one bug per test case. Root cause
(reproduced): the deterministic simulator caps its fail rate at 45%
and can NEVER produce an all-failed run — the all-failed verdict is
imposed by ``_reconcile_with_automation`` copying the automation
runner's per-item status 1:1 and synthesizing a bug for each, with no
"cannot-execute" state, no evidence gate, and no all-failed guard.

QA-correct behaviour (pinned here):

* Runner ``"blocked"`` — or a ``"failed"`` with no failure evidence —
  is a *cannot-execute* condition → the item is recorded **Blocked**
  (ISTQB: could not be executed) and files **no** bug.
* A genuine, evidence-backed ``"failed"`` still promotes to **Failed**
  and keeps/synthesises its bug.
* When NOTHING passes across a substantial suite (>= ``_INFRA_GUARD_
  MIN_SUITE``), the run could not validate the app at all: every
  remaining Failed is reclassified **Blocked** and the per-item bug
  pile is replaced by exactly **one** infrastructure summary bug.
* Small suites and partial-failure runs are left untouched, so genuine
  defects are never hidden.
"""

from __future__ import annotations

from engine.qa_testers import execute_items
from routes.execution import (
    _reconcile_with_automation, _dedupe_bugs_by_root_cause,
    _make_cannot_execute_summary_bug, _INFRA_GUARD_MIN_SUITE,
)


# ── Helpers ──────────────────────────────────────────────────────


def _items(n: int) -> list[dict]:
    return [{"id": f"CL_{i:03d}",
             "objective": f"Verify behaviour number {i} works as specified",
             "section": "CRUD"} for i in range(1, n + 1)]


def _exec(items):
    """Fresh simulator execution (no site → all simulated) that we then
    reconcile against synthetic automation assets."""
    return execute_items(items, "checklist", "mid_1",
                         "Mobile Web / iOS / Safari", ["Regression"],
                         site_url="")


def _asset(status, *, evidence=False, url="https://site/"):
    return {
        "status": status,
        "failure_step": ({"index": 1, "action": "expect_text",
                          "comment": "Expected text not found"}
                         if evidence else None),
        "failure_screenshots": (["s.png"] if evidence else []),
        "screenshots": (["s.png"] if evidence else []),
        "final_url": url,
        "duration_ms": 1,
    }


def _reconcile(ex, assets):
    _reconcile_with_automation(ex, assets, "mobile_web")
    _dedupe_bugs_by_root_cause(ex, assets)
    return ex


# ── 1. The headline guard ────────────────────────────────────────


class TestAllFailedGuard:
    def test_all_failed_no_evidence_becomes_blocked_plus_one_bug(self):
        n = _INFRA_GUARD_MIN_SUITE + 2
        items = _items(n)
        ex = _exec(items)
        assets = {it["id"]: _asset("failed", evidence=False) for it in items}
        _reconcile(ex, assets)
        s = ex["stats"]
        assert s["passed"] == 0
        assert s["failed"] == 0
        assert s["blocked"] == n
        assert s["cannot_execute"] == n
        assert s["pass_rate"] == 0.0
        assert len(ex["bugs"]) == 1
        assert all(r["status"] == "Blocked" for r in ex["results"])

    def test_all_failed_WITH_screenshots_still_guarded(self):
        """The prose-assertion failure path writes a failure screenshot,
        so a per-item evidence gate would NOT catch it. The aggregate
        guard (0 pass across the suite) must still fire."""
        n = _INFRA_GUARD_MIN_SUITE + 5
        items = _items(n)
        ex = _exec(items)
        assets = {it["id"]: _asset("failed", evidence=True,
                                   url="https://site/")
                  for it in items}
        _reconcile(ex, assets)
        assert ex["stats"]["passed"] == 0
        assert ex["stats"]["failed"] == 0
        assert ex["stats"]["blocked"] == n
        assert len(ex["bugs"]) == 1

    def test_summary_bug_is_runner_sourced_and_singular(self):
        items = _items(_INFRA_GUARD_MIN_SUITE)
        ex = _exec(items)
        assets = {it["id"]: _asset("failed", evidence=False) for it in items}
        _reconcile(ex, assets)
        assert len(ex["bugs"]) == 1
        bug = ex["bugs"][0]
        # Buckets into the runner-sourced side of the /bug-reports
        # source filter, and is clearly labelled infrastructure.
        assert bug["linked_item_type"] == "live_executor"
        assert "source:live_executor" in bug["labels"]
        assert "defect:cannot_execute" in bug["labels"]
        assert "infrastructure" in bug["title"].lower() \
            or "could not validate" in bug["title"].lower()


# ── 2. Cannot-execute vs genuine failure ─────────────────────────


class TestCannotExecuteClassification:
    def test_runner_blocked_never_files_a_bug(self):
        n = _INFRA_GUARD_MIN_SUITE + 1
        items = _items(n)
        ex = _exec(items)
        assets = {it["id"]: _asset("blocked") for it in items}
        _reconcile(ex, assets)
        assert ex["stats"]["blocked"] == n
        assert ex["stats"]["failed"] == 0
        # All blocked → guard collapses to one summary bug (not n).
        assert len(ex["bugs"]) == 1

    def test_partial_run_keeps_genuine_failures_and_bugs(self):
        """Half pass, half evidence-backed fail → guard must NOT fire;
        the real failures keep Failed status and their bugs."""
        n = 12
        items = _items(n)
        ex = _exec(items)
        assets = {}
        for idx, it in enumerate(items, 1):
            if idx % 2 == 0:
                assets[it["id"]] = _asset("passed")
            else:
                assets[it["id"]] = _asset(
                    "failed", evidence=True, url=f"https://site/p{idx}")
        _reconcile(ex, assets)
        s = ex["stats"]
        assert s["passed"] == n // 2
        assert s["failed"] == n // 2
        assert s["blocked"] == 0
        assert s["cannot_execute"] == 0
        # Genuine failures still produce bugs (not collapsed to one).
        assert len(ex["bugs"]) >= 1
        assert not any(b.get("linked_item_type") == "live_executor"
                       and "infrastructure" in (b.get("title") or "").lower()
                       for b in ex["bugs"])

    def test_small_all_failed_suite_is_left_alone(self):
        """A sub-threshold suite that legitimately fails all its items
        is a real signal, not infrastructure noise — no guard."""
        n = _INFRA_GUARD_MIN_SUITE - 1
        items = _items(n)
        ex = _exec(items)
        assets = {it["id"]: _asset("failed", evidence=True,
                                   url=f"https://site/{it['id']}")
                  for it in items}
        _reconcile(ex, assets)
        s = ex["stats"]
        assert s["passed"] == 0
        assert s["failed"] == n          # stays Failed, not reclassified
        assert s["blocked"] == 0
        # Not collapsed to a single infra bug.
        assert not any(b.get("linked_item_type") == "live_executor"
                       for b in ex["bugs"])


# ── 3. Summary-bug helper shape ──────────────────────────────────


class TestSummaryBugHelper:
    def test_helper_fills_mandatory_fields(self):
        bug = _make_cannot_execute_summary_bug(
            n_blocked=42, site_url="https://shop.example.com/",
            reporter="Olena Marchenko")
        for field in ("title", "severity", "priority", "status",
                      "preconditions", "steps_to_reproduce",
                      "actual_result", "expected_result"):
            assert (bug.get(field) or "").strip(), f"{field} must be set"
        assert "42" in bug["title"]
        assert "shop.example.com" in bug["actual_result"]
        assert bug["reporter"] == "Olena Marchenko"
