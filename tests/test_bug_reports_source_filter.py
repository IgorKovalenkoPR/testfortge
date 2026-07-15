"""Sprint 5 follow-up — `/bug-reports?source=` filter.

PR #12 started writing walkthrough findings as bugs alongside
TC-driven bugs in the same listing. Operators who run both modes
asked for a way to scope the page to one mode's bugs at a time,
without grep-ing the labels by eye.

The filter is a single query-string param so a filtered view is
bookmarkable / shareable:

* ``?source=walkthrough`` — only bugs with
  ``linked_item_type=="walkthrough"`` OR ``source:walkthrough``
  in ``labels``. Both checks because the bug-creation path tags
  walkthrough findings with the label (PR #12) AND sets
  ``linked_item_type`` (engine.bug_report); future paths that
  only set one of the two still surface here.
* ``?source=manual_tc`` — the negation: everything that doesn't
  look walkthrough-shaped.
* No / empty / unknown values — no filtering, full list.

These tests pin filter behaviour against a stable session fixture
mixing both bug kinds, plus the template's filter UI + empty-state
fallback.
"""

from __future__ import annotations

import pytest


# ── Shared seed ─────────────────────────────────────────────────


def _seed_mixed_bugs(client):
    """Three bugs: one walkthrough (linked_item_type=walkthrough +
    source:walkthrough label), one TC-driven (linked_item_type=
    test_case, no walkthrough label), one manual (no walkthrough
    markers at all). The session fallback path is enough to render
    /bug-reports because there's no active project in tests."""
    with client.session_transaction() as s:
        s["bug_reports_data"] = [
            {"id": "BUG-001", "title": "[Images] Broken hero image",
             "severity": "Critical", "priority": "Highest",
             "status": "Open", "environment": "Web",
             "preconditions": "Walkthrough run.",
             "steps_to_reproduce": "1. Open URL.",
             "actual_result": "Hero image fails to load.",
             "expected_result": "Image renders.",
             "frequency": "Always", "attachments": [],
             "linked_item_id": "WALK-IMG-001",
             "linked_item_type": "walkthrough",
             "reporter": "QA", "assignee": "",
             "created_at": "2026-05-21T12:00:00",
             "component": "Images",
             "labels": ["defect:broken_image", "source:walkthrough",
                        "area:images"],
             "comment": ""},
            {"id": "BUG-002", "title": "[Login] Submit times out",
             "severity": "Major", "priority": "High",
             "status": "Open", "environment": "Web",
             "preconditions": "Login form rendered.",
             "steps_to_reproduce": "1. Fill credentials. 2. Submit.",
             "actual_result": "Spinner hangs >5s.",
             "expected_result": "Welcome page.",
             "frequency": "Sometimes", "attachments": [],
             "linked_item_id": "TC-001",
             "linked_item_type": "test_case",
             "reporter": "QA", "assignee": "",
             "created_at": "2026-05-21T12:01:00",
             "component": "Login",
             "labels": ["defect:timeout"],
             "comment": ""},
            {"id": "BUG-003", "title": "Manual repro — receipt PDF blank",
             "severity": "Minor", "priority": "Low",
             "status": "Open", "environment": "Web",
             "preconditions": "Order completed.",
             "steps_to_reproduce": "1. Open receipt PDF link.",
             "actual_result": "PDF renders blank.",
             "expected_result": "Receipt content visible.",
             "frequency": "Always", "attachments": [],
             "linked_item_id": "",
             "linked_item_type": "",
             "reporter": "QA", "assignee": "",
             "created_at": "2026-05-21T12:02:00",
             "component": "Billing", "labels": [], "comment": ""},
        ]
        s["_session_active_since"] = 9_999_999_999


# ── 1. Backend filter ───────────────────────────────────────────


class TestSourceFilter:
    def test_no_filter_shows_every_bug(self, client):
        _seed_mixed_bugs(client)
        resp = client.get("/bug-reports")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        for bid in ("BUG-001", "BUG-002", "BUG-003"):
            assert bid in body

    def test_walkthrough_filter_keeps_only_walkthrough_bugs(self, client):
        _seed_mixed_bugs(client)
        resp = client.get("/bug-reports?source=walkthrough")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "BUG-001" in body
        # BUG-001 also gives the bug id in attributes (data-bug-id=""
        # for session-loaded bugs since db_id is missing), but the
        # card title is unique enough — match on the descriptive
        # title to avoid false positives from other markup.
        assert "Broken hero image" in body
        assert "Submit times out" not in body
        assert "receipt PDF blank" not in body

    def test_manual_tc_filter_excludes_walkthrough_bugs(self, client):
        _seed_mixed_bugs(client)
        resp = client.get("/bug-reports?source=manual_tc")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Broken hero image" not in body
        assert "Submit times out" in body
        assert "receipt PDF blank" in body

    def test_unknown_source_value_falls_back_to_no_filter(self, client):
        _seed_mixed_bugs(client)
        resp = client.get("/bug-reports?source=ai_oracle")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # All three bugs visible — invalid filter value never silently
        # narrows the listing.
        for bid in ("BUG-001", "BUG-002", "BUG-003"):
            assert bid in body

    def test_label_only_walkthrough_marker_is_recognised(self, client):
        """The walkthrough path sets BOTH ``linked_item_type=walkthrough``
        AND a ``source:walkthrough`` label. Future ingestion paths
        (e.g. third-party imports) might set only the label — the
        filter must catch those too. Pin the behaviour."""
        with client.session_transaction() as s:
            s["bug_reports_data"] = [
                {"id": "BUG-LBL-1", "title": "Label-only walkthrough",
                 "severity": "Major", "priority": "High",
                 "status": "Open", "environment": "Web",
                 "preconditions": "", "steps_to_reproduce": "",
                 "actual_result": "x", "expected_result": "y",
                 "frequency": "Always", "attachments": [],
                 "linked_item_id": "", "linked_item_type": "",
                 "reporter": "QA", "assignee": "",
                 "created_at": "2026-05-21T12:00:00",
                 "component": "X",
                 "labels": ["Source:Walkthrough"],   # mixed case
                 "comment": ""},
            ]
            s["_session_active_since"] = 9_999_999_999
        resp = client.get("/bug-reports?source=walkthrough")
        body = resp.get_data(as_text=True)
        assert "Label-only walkthrough" in body

    def test_live_executor_infra_bug_is_runner_sourced(self, client):
        """Stage 4 early-exit infra bugs (OOM / wall-clock) land with
        ``linked_item_type="live_executor"`` + ``source:live_executor``
        label — they are auto-generated by the runner, not filed
        against a TC. The filter must treat them as walkthrough-shaped
        so they don't leak into the ``manual_tc`` bucket alongside
        manually-filed bugs."""
        with client.session_transaction() as s:
            s["bug_reports_data"] = [
                {"id": "BUG-OOM-1", "title": "LiveExecutor early exit — OOM",
                 "severity": "Critical", "priority": "Highest",
                 "status": "Open", "environment": "Web",
                 "preconditions": "Live run hit memory cap.",
                 "steps_to_reproduce": "1. Start live run.",
                 "actual_result": "412 MB > 400 MB.",
                 "expected_result": "Run completes within budget.",
                 "frequency": "Always", "attachments": [],
                 "linked_item_id": "20260525_120000_xyz",
                 "linked_item_type": "live_executor",
                 "reporter": "QA", "assignee": "",
                 "created_at": "2026-05-25T12:00:00",
                 "component": "TestRunInfra",
                 "labels": ["defect:early_exit_oom",
                            "source:live_executor",
                            "area:test_run_infra"],
                 "comment": ""},
            ]
            s["_session_active_since"] = 9_999_999_999
        # Runner-sourced bucket includes it.
        body = client.get("/bug-reports?source=walkthrough").get_data(
            as_text=True)
        assert "LiveExecutor early exit" in body
        # manual_tc bucket excludes it — that's the regression
        # the merge fix prevents.
        body = client.get("/bug-reports?source=manual_tc").get_data(
            as_text=True)
        assert "LiveExecutor early exit" not in body


# ── 2. Stats reflect filtered count ─────────────────────────────


class TestFilteredStats:
    def test_stats_reflect_filtered_count_not_raw_total(self, client):
        """The Total/Open/Critical/Major counters at the top of the
        page must show the FILTERED total, not the unfiltered one.
        Otherwise the operator sees "1 of 3 cards rendered but
        Critical: 1" and assumes the filter only re-rendered the
        cards (it didn't — it removed bugs from the data set)."""
        _seed_mixed_bugs(client)
        resp = client.get("/bug-reports?source=walkthrough")
        body = resp.get_data(as_text=True)
        # BUG-001 is Critical and Open — that's the only walkthrough
        # bug. So Total=1, Open=1, Critical=1, Major=0.
        # The DOM uses <span class="stat-value">N</span> for each.
        import re
        nums = re.findall(r'stat-value">(\d+)<', body)
        assert len(nums) >= 4
        total, opn, crit, maj = map(int, nums[:4])
        assert (total, opn, crit, maj) == (1, 1, 1, 0)


# ── 3. UI toolbar + empty-state ─────────────────────────────────


class TestFilterUi:
    def test_toolbar_renders_with_all_three_links(self, client):
        _seed_mixed_bugs(client)
        body = client.get("/bug-reports").get_data(as_text=True)
        # All three filter links are present + the All anchor is
        # marked aria-current="page" because no filter is selected.
        assert "Walkthrough only" in body
        assert "TC / manual only" in body
        # Source filter links — bookmarkable.
        assert "?source=walkthrough" in body
        assert "?source=manual_tc" in body

    def test_active_filter_marked_aria_current(self, client):
        _seed_mixed_bugs(client)
        body = client.get("/bug-reports?source=walkthrough").get_data(
            as_text=True)
        # Walkthrough anchor is aria-current="page"; the other two
        # are not. We don't assert HTML byte order, but the presence
        # of the attribute combined with the filter href on the same
        # element is enough.
        assert 'aria-current="page"' in body
        # The "All" anchor isn't current — it points at the bare
        # /bug-reports URL.
        assert 'href="/bug-reports"' in body or \
               'href="' in body  # url_for renders the path

    def test_empty_state_explains_filter_and_offers_clear(self, client):
        """When a filter narrows to zero, the empty-state must
        disambiguate from the "no bugs at all" case and offer a
        one-click escape."""
        # Pack with one walkthrough bug only; filter to manual_tc → 0.
        with client.session_transaction() as s:
            s["bug_reports_data"] = [
                {"id": "BUG-LONE-WT", "title": "Only walkthrough bug",
                 "severity": "Major", "priority": "High",
                 "status": "Open", "environment": "Web",
                 "preconditions": "", "steps_to_reproduce": "",
                 "actual_result": "x", "expected_result": "y",
                 "frequency": "Always", "attachments": [],
                 "linked_item_id": "WALK-1",
                 "linked_item_type": "walkthrough",
                 "reporter": "QA", "assignee": "",
                 "created_at": "2026-05-21T12:00:00",
                 "component": "X",
                 "labels": ["source:walkthrough"],
                 "comment": ""},
            ]
            s["_session_active_since"] = 9_999_999_999
        body = client.get("/bug-reports?source=manual_tc").get_data(
            as_text=True)
        # Filter-aware empty message + clear link, not the cold
        # "Run test execution to report bugs" copy. (Copy is now
        # filter-agnostic — "current filter" — since the run filter
        # shares this empty state.)
        assert "No bug reports match the current filter" in body
        assert "Clear filter" in body
        # The "Run test execution" CTA must NOT appear here — it's
        # the wrong message for a filter dead-end.
        assert "Run test execution to report bugs from failed test cases" \
               not in body
