"""`/bug-reports?run=` filter — scope the listing to a single run.

Operators who hammer Test Execution repeatedly on the same project
end up with hundreds of auto-filed bugs that read like they came from
several projects. The listing has always been per-project (see
``test_bug_reports_source_filter``); this filter narrows it further to
a single Test Execution run so the operator can look at just the pass
they care about.

Behaviour pinned here:

* ``list_bugs(pid, run_id=X)`` / ``count_bugs_by_run`` engine helpers.
* ``?run=<id>`` shows only that run's bugs; stats reflect the filtered
  count; manually-filed (``run_id IS NULL``) bugs are excluded.
* ``?run=latest`` tracks the newest run that filed a bug.
* absent / ``?run=all`` / a stale-or-invalid id → unscoped (all bugs).
* run + source filters compose (AND).
* the dropdown only lists runs that actually filed a bug.
"""

from __future__ import annotations

import uuid

import pytest

from engine import db as _db


# ── Seed helper ──────────────────────────────────────────────────


def _seed_project_two_runs(owner_sid: str) -> dict:
    """Project with two runs + a manual (run-less) bug.

    Run A (older, testfort.com): 1 walkthrough + 1 TC bug.
    Run B (newer, example.com):  1 walkthrough bug.
    Manual bug: no run_id.

    Returns the ids the tests assert against.
    """
    pid = _db.upsert_project(name=f"runfilter-{uuid.uuid4().hex[:8]}",
                             owner_sid=owner_sid)

    run_a = _db.start_execution_run(pid, {"source": "walkthrough"},
                                    base_url="https://testfort.com/")
    run_b = _db.start_execution_run(pid, {"source": "walkthrough"},
                                    base_url="https://example.com/")

    def _bug(title, run_id, *, walkthrough=False, source="execution"):
        payload = {
            "id": f"BUG-{uuid.uuid4().hex[:6]}",
            "title": title,
            "severity": "Major",
            "priority": "High",
            "status": "Open",
            "environment": "Web",
            "steps_to_reproduce": "1. open",
            "actual_result": "broken",
            "expected_result": "works",
            "run_id": run_id,
        }
        if walkthrough:
            payload["linked_item_type"] = "walkthrough"
            payload["labels"] = ["source:walkthrough"]
        return _db.save_bug(pid, payload, source=source)

    a_wt = _bug("RunA walkthrough hero image", run_a,
                walkthrough=True, source="walkthrough")
    a_tc = _bug("RunA TC login timeout", run_a, source="execution")
    b_wt = _bug("RunB walkthrough footer link", run_b,
                walkthrough=True, source="walkthrough")
    manual = _bug("Manual receipt PDF blank", None, source="manual")

    return {
        "pid": pid, "run_a": run_a, "run_b": run_b,
        "a_wt": a_wt, "a_tc": a_tc, "b_wt": b_wt, "manual": manual,
    }


def _pin_sid(monkeypatch, sid: str) -> None:
    monkeypatch.setattr("routes._shared.get_session_id", lambda s=None: sid)


# ── 1. Engine helpers ───────────────────────────────────────────


class TestListBugsRunScope:
    def test_run_id_scopes_to_that_run_only(self):
        seed = _seed_project_two_runs(f"sid-{uuid.uuid4().hex}")
        rows = _db.list_bugs(seed["pid"], run_id=seed["run_a"])
        titles = {r["title"] for r in rows}
        assert titles == {"RunA walkthrough hero image",
                          "RunA TC login timeout"}

    def test_no_run_id_returns_every_bug(self):
        seed = _seed_project_two_runs(f"sid-{uuid.uuid4().hex}")
        rows = _db.list_bugs(seed["pid"])
        # 3 run-linked + 1 manual = 4.
        assert len(rows) == 4

    def test_run_and_source_compose(self):
        seed = _seed_project_two_runs(f"sid-{uuid.uuid4().hex}")
        rows = _db.list_bugs(seed["pid"], run_id=seed["run_a"],
                             source="execution")
        titles = {r["title"] for r in rows}
        # Only the TC bug in run A carries source="execution".
        assert titles == {"RunA TC login timeout"}


class TestCountBugsByRun:
    def test_counts_group_by_run_including_manual(self):
        seed = _seed_project_two_runs(f"sid-{uuid.uuid4().hex}")
        counts = _db.count_bugs_by_run(seed["pid"])
        assert counts.get(seed["run_a"]) == 2
        assert counts.get(seed["run_b"]) == 1
        assert counts.get(None) == 1  # the manual, run-less bug

    def test_empty_project_id_returns_empty(self):
        assert _db.count_bugs_by_run("") == {}


# ── 2. Route filtering ──────────────────────────────────────────


class TestRunFilterRoute:
    def test_specific_run_shows_only_its_bugs(self, client, monkeypatch):
        sid = f"sid-{uuid.uuid4().hex}"
        _pin_sid(monkeypatch, sid)
        seed = _seed_project_two_runs(sid)
        with client.session_transaction() as s:
            s["project_id"] = seed["pid"]
        body = client.get(f"/bug-reports?run={seed['run_a']}").get_data(
            as_text=True)
        assert "RunA walkthrough hero image" in body
        assert "RunA TC login timeout" in body
        assert "RunB walkthrough footer link" not in body
        assert "Manual receipt PDF blank" not in body

    def test_stats_reflect_filtered_run(self, client, monkeypatch):
        sid = f"sid-{uuid.uuid4().hex}"
        _pin_sid(monkeypatch, sid)
        seed = _seed_project_two_runs(sid)
        with client.session_transaction() as s:
            s["project_id"] = seed["pid"]
        body = client.get(f"/bug-reports?run={seed['run_b']}").get_data(
            as_text=True)
        import re
        nums = re.findall(r'stat-value">(\d+)<', body)
        total = int(nums[0])
        assert total == 1  # run B filed exactly one bug

    def test_latest_run_tracks_newest(self, client, monkeypatch):
        sid = f"sid-{uuid.uuid4().hex}"
        _pin_sid(monkeypatch, sid)
        seed = _seed_project_two_runs(sid)
        with client.session_transaction() as s:
            s["project_id"] = seed["pid"]
        body = client.get("/bug-reports?run=latest").get_data(as_text=True)
        # Run B is the most recent run → its bug shows, run A's don't.
        assert "RunB walkthrough footer link" in body
        assert "RunA TC login timeout" not in body

    def test_run_all_shows_everything(self, client, monkeypatch):
        sid = f"sid-{uuid.uuid4().hex}"
        _pin_sid(monkeypatch, sid)
        seed = _seed_project_two_runs(sid)
        with client.session_transaction() as s:
            s["project_id"] = seed["pid"]
        body = client.get("/bug-reports?run=all").get_data(as_text=True)
        for title in ("RunA walkthrough hero image", "RunB walkthrough "
                      "footer link", "Manual receipt PDF blank"):
            assert title in body

    def test_stale_run_id_falls_back_to_all(self, client, monkeypatch):
        """A run id that isn't in this project's option set must never
        silently show zero — it falls back to the unscoped view."""
        sid = f"sid-{uuid.uuid4().hex}"
        _pin_sid(monkeypatch, sid)
        seed = _seed_project_two_runs(sid)
        with client.session_transaction() as s:
            s["project_id"] = seed["pid"]
        body = client.get("/bug-reports?run=999999").get_data(as_text=True)
        # All four bugs visible — invalid id didn't narrow anything.
        assert "RunA TC login timeout" in body
        assert "RunB walkthrough footer link" in body
        assert "Manual receipt PDF blank" in body

    def test_reset_count_shows_true_total_under_filter(
            self, client, monkeypatch):
        """Reset deletes EVERY bug on the project, ignoring the active
        filter. So the reset button + confirm dialog must show the
        unfiltered project total (4 here), never the filtered
        ``stats.total`` (1 while scoped to run B) — otherwise the
        operator thinks they're wiping 1 and loses all 4."""
        import re
        sid = f"sid-{uuid.uuid4().hex}"
        _pin_sid(monkeypatch, sid)
        seed = _seed_project_two_runs(sid)
        with client.session_transaction() as s:
            s["project_id"] = seed["pid"]
        body = client.get(f"/bug-reports?run={seed['run_b']}").get_data(
            as_text=True)
        # Filtered view genuinely shows 1 (run B's bug count).
        nums = re.findall(r'stat-value">(\d+)<', body)
        assert int(nums[0]) == 1
        # …but the reset affordance references the true total, 4.
        assert re.search(r"Reset Project bugs\s*\(4\)", body)
        assert re.search(r"<strong>4</strong>[\s\S]{0,40}will be deleted",
                         body)

    def test_run_and_source_compose_on_route(self, client, monkeypatch):
        sid = f"sid-{uuid.uuid4().hex}"
        _pin_sid(monkeypatch, sid)
        seed = _seed_project_two_runs(sid)
        with client.session_transaction() as s:
            s["project_id"] = seed["pid"]
        # Run A + walkthrough → only the walkthrough bug of run A.
        body = client.get(
            f"/bug-reports?run={seed['run_a']}&source=walkthrough"
        ).get_data(as_text=True)
        assert "RunA walkthrough hero image" in body
        assert "RunA TC login timeout" not in body   # TC bug filtered by source
        assert "RunB walkthrough footer link" not in body  # other run


# ── 3. Dropdown UI ──────────────────────────────────────────────


class TestRunFilterUi:
    def test_dropdown_lists_runs_with_counts(self, client, monkeypatch):
        sid = f"sid-{uuid.uuid4().hex}"
        _pin_sid(monkeypatch, sid)
        seed = _seed_project_two_runs(sid)
        with client.session_transaction() as s:
            s["project_id"] = seed["pid"]
        body = client.get("/bug-reports").get_data(as_text=True)
        assert "All runs" in body
        assert "Latest run" in body
        # Each run option carries its id and a host label.
        assert f'value="{seed["run_a"]}"' in body
        assert f'value="{seed["run_b"]}"' in body
        assert "testfort.com" in body
        assert "example.com" in body

    def test_dropdown_hidden_when_no_run_has_bugs(self, client, monkeypatch):
        """A project whose only bug is manual (no run_id) shows no run
        dropdown — nothing to scope by."""
        sid = f"sid-{uuid.uuid4().hex}"
        _pin_sid(monkeypatch, sid)
        pid = _db.upsert_project(name=f"manualonly-{uuid.uuid4().hex[:8]}",
                                 owner_sid=sid)
        _db.save_bug(pid, {"id": "BUG-M1", "title": "Manual only",
                           "severity": "Minor", "status": "Open"},
                     source="manual")
        with client.session_transaction() as s:
            s["project_id"] = pid
        body = client.get("/bug-reports").get_data(as_text=True)
        assert 'id="bug-run-select"' not in body

    def test_empty_state_when_run_plus_source_narrow_to_zero(
            self, client, monkeypatch):
        """Run B has only a walkthrough bug; adding source=manual_tc
        empties the listing. The filter-aware empty state (not the
        cold 'no bugs yet' copy) must render with a clear link."""
        sid = f"sid-{uuid.uuid4().hex}"
        _pin_sid(monkeypatch, sid)
        seed = _seed_project_two_runs(sid)
        with client.session_transaction() as s:
            s["project_id"] = seed["pid"]
        body = client.get(
            f"/bug-reports?run={seed['run_b']}&source=manual_tc"
        ).get_data(as_text=True)
        assert "No bug reports match the current filter" in body
        assert "Clear filter" in body
        assert "Run test execution to report bugs from failed test cases" \
               not in body
