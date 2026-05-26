"""PR-H regression — smart bug filing (cross-run dedup + page-level
aggregation + Reset Project + annotation_status surface).

Five separate problem reports from the ART/AT/A projects converged
on the same underlying issue: every Test Execution run created
~65 walkthrough bugs even when the site was unchanged between runs,
and there was no in-product way to clean up the accumulated pile.
By the time the user ran twice on project "A" the bug count hit 130
in 60 TCs — clearly not a useful triage surface.

This module pins the four PR-H behaviours that solve it:

  1. ``compute_dedup_signature`` is stable across runs, ignores
     query strings, treats list-and-string element formats the
     same, and disambiguates by page URL.
  2. ``_aggregate_broken_image_findings`` collapses N broken images
     on the same page into 1 aggregate finding with a filename
     list — other defect classes pass through unchanged.
  3. The DB helpers (``find_bug_id_by_signature``,
     ``bump_bug_occurrence``, ``delete_bugs_for_project``) round-trip
     correctly with the new ``extra`` shape.
  4. The factory carries ``defect_class``, ``page_url``,
     ``dedup_signature``, ``occurrence_count``, and
     ``annotation_status`` through into the BugReport record so
     ``_persist_bug`` lands them in ``BugReport.extra``.
"""

from __future__ import annotations

import uuid

import pytest

from engine import db as _db
from engine.bug_report import (
    compute_dedup_signature,
    _normalise_element_for_dedup,
    _normalise_url_for_dedup,
    create_bug_from_walkthrough_finding,
    bug_to_dict, dict_to_bug,
)


# ── 1. Dedup signature ────────────────────────────────────────────


class TestComputeDedupSignature:
    def test_same_inputs_yield_same_signature(self):
        a = compute_dedup_signature(
            "broken_image", "img[src='hero.svg']", "https://artest.com/jobs",
        )
        b = compute_dedup_signature(
            "broken_image", "img[src='hero.svg']", "https://artest.com/jobs",
        )
        assert a == b
        assert len(a) == 16, "signature must be a 16-char hex digest"

    def test_query_strings_are_normalised_away(self):
        """Two runs on the same page often differ only in cache
        busters / analytics ids. Dedup must group them."""
        a = compute_dedup_signature(
            "broken_image", "img[src='x.svg']",
            "https://artest.com/jobs?run=1&_=abc",
        )
        b = compute_dedup_signature(
            "broken_image", "img[src='x.svg']",
            "https://artest.com/jobs?run=99&_=def",
        )
        assert a == b

    def test_trailing_slash_normalised(self):
        a = compute_dedup_signature("axe_critical", "#nav", "https://x.com/jobs")
        b = compute_dedup_signature("axe_critical", "#nav", "https://x.com/jobs/")
        assert a == b

    def test_list_and_string_element_are_equivalent(self):
        """axe targets are lists; other heuristics use strings. The
        same defect must compute the same signature regardless."""
        sig_list = compute_dedup_signature(
            "axe_critical", ["#All-vacancies"], "https://artest.com/jobs",
        )
        sig_str = compute_dedup_signature(
            "axe_critical", "#All-vacancies", "https://artest.com/jobs",
        )
        assert sig_list == sig_str

    def test_different_pages_yield_different_signatures(self):
        a = compute_dedup_signature(
            "broken_image", "img[src='x.svg']", "https://artest.com/careers",
        )
        b = compute_dedup_signature(
            "broken_image", "img[src='x.svg']", "https://artest.com/jobs",
        )
        assert a != b

    def test_different_defect_classes_yield_different_signatures(self):
        a = compute_dedup_signature(
            "broken_image", "img.hero", "https://artest.com/",
        )
        b = compute_dedup_signature(
            "axe_critical", "img.hero", "https://artest.com/",
        )
        assert a != b

    def test_empty_inputs_do_not_raise(self):
        # Robust to partial findings.
        sig = compute_dedup_signature("", "", "")
        assert len(sig) == 16

    def test_normalise_element_list_takes_first_non_empty(self):
        assert _normalise_element_for_dedup(
            ["", " ", "#All-vacancies", "#other"]
        ) == "#All-vacancies"

    def test_normalise_url_drops_query_and_fragment(self):
        assert _normalise_url_for_dedup(
            "https://x.com/jobs?a=1#section"
        ) == "https://x.com/jobs"


# ── 2. Page-level aggregation ─────────────────────────────────────


class TestAggregateBrokenImageFindings:
    def test_collapses_n_broken_images_on_one_page(self):
        from routes.execution import _aggregate_broken_image_findings
        findings = [
            {
                "severity": "Major", "area": "Images",
                "defect_class": "broken_image",
                "message": (
                    f"Broken image on the page — {fn} did not load "
                    "(visitors see an empty slot)"
                ),
                "url": "https://artest.com/careers",
                "element": f'img[src="{fn}"]',
            }
            for fn in ("a.svg", "b.svg", "c.svg")
        ]
        result = _aggregate_broken_image_findings(findings)
        assert len(result) == 1
        agg = result[0]
        assert agg["aggregated_count"] == 3
        assert set(agg["aggregated_filenames"]) == {"a.svg", "b.svg", "c.svg"}
        # Aggregate message names the count for the title transform.
        assert "3 broken images" in agg["message"]

    def test_groups_split_by_url(self):
        from routes.execution import _aggregate_broken_image_findings
        findings = []
        for fn in ("c1.svg", "c2.svg", "c3.svg"):
            findings.append({
                "defect_class": "broken_image",
                "message": (
                    f"Broken image on the page — {fn} did not load"
                ),
                "url": "https://artest.com/careers",
                "element": f'img[src="{fn}"]',
            })
        for fn in ("j1.svg", "j2.svg"):
            findings.append({
                "defect_class": "broken_image",
                "message": (
                    f"Broken image on the page — {fn} did not load"
                ),
                "url": "https://artest.com/jobs",
                "element": f'img[src="{fn}"]',
            })
        result = _aggregate_broken_image_findings(findings)
        assert len(result) == 2
        counts = {r["url"]: r["aggregated_count"] for r in result}
        assert counts == {
            "https://artest.com/careers": 3,
            "https://artest.com/jobs": 2,
        }

    def test_single_finding_passes_through_without_aggregation(self):
        """A page with only 1 broken image isn't worth aggregating;
        the heuristic's per-image bug is still useful."""
        from routes.execution import _aggregate_broken_image_findings
        findings = [{
            "defect_class": "broken_image",
            "message": (
                "Broken image on the page — lonely.svg did not load"
            ),
            "url": "https://artest.com/contact",
            "element": 'img[src="lonely.svg"]',
        }]
        result = _aggregate_broken_image_findings(findings)
        assert len(result) == 1
        # No aggregation markers added.
        assert "aggregated_count" not in result[0]

    def test_axe_findings_pass_through_unchanged(self):
        """Aggregation is broken-image-only — axe findings need
        per-element resolution for engineering triage."""
        from routes.execution import _aggregate_broken_image_findings
        axe1 = {
            "defect_class": "axe_critical",
            "message": "Select element must have an accessible name",
            "url": "https://artest.com/jobs",
            "element": "#All-vacancies",
        }
        axe2 = {
            "defect_class": "axe_critical",
            "message": "Color contrast insufficient",
            "url": "https://artest.com/jobs",
            "element": ".cta-link",
        }
        result = _aggregate_broken_image_findings([axe1, axe2])
        assert len(result) == 2, "axe findings must not be aggregated"
        assert all("aggregated_count" not in r for r in result)

    def test_empty_input_returns_empty_list(self):
        from routes.execution import _aggregate_broken_image_findings
        assert _aggregate_broken_image_findings([]) == []


# ── 3. DB helpers ─────────────────────────────────────────────────


class TestDbDedupHelpers:
    def test_find_bug_id_by_signature_round_trips(self):
        sid = f"sid-{uuid.uuid4().hex}"
        pid = _db.upsert_project(name="dedup-rt", owner_sid=sid)
        bug_id = _db.save_bug(pid, {
            "id": "BUG-001",
            "title": "[Images] A page graphic is missing",
            "severity": "Major",
            "dedup_signature": "abc123def4567890",
            "defect_class": "broken_image",
            "page_url": "https://artest.com/careers",
            "occurrence_count": 1,
        }, source="walkthrough")

        # Found by matching signature.
        assert _db.find_bug_id_by_signature(
            pid, "abc123def4567890",
        ) == bug_id
        # Different signature → None.
        assert _db.find_bug_id_by_signature(
            pid, "0000000000000000",
        ) is None
        # Wrong project → None (cross-project leakage guard).
        other_pid = _db.upsert_project(name="other", owner_sid=sid)
        assert _db.find_bug_id_by_signature(
            other_pid, "abc123def4567890",
        ) is None

    def test_find_signature_ignores_manual_bugs(self):
        """Manual bugs (operator-created) don't carry a dedup
        signature; they should not be candidates for the dedup
        check even if the JSON column accidentally has one.
        """
        sid = f"sid-{uuid.uuid4().hex}"
        pid = _db.upsert_project(name="manual-only", owner_sid=sid)
        _db.save_bug(pid, {
            "id": "BUG-001",
            "title": "Manual entry",
            "dedup_signature": "manual_sig_1234",
        }, source="manual")  # source matters — query filters it out.
        assert _db.find_bug_id_by_signature(
            pid, "manual_sig_1234",
        ) is None

    def test_bump_bug_occurrence_increments_counter(self):
        sid = f"sid-{uuid.uuid4().hex}"
        pid = _db.upsert_project(name="bump-test", owner_sid=sid)
        bug_id = _db.save_bug(pid, {
            "id": "BUG-001",
            "title": "Recurring defect",
            "dedup_signature": "bump_me_now_aaaa",
            "occurrence_count": 1,
        }, source="walkthrough")
        assert _db.bump_bug_occurrence(bug_id) == 2
        assert _db.bump_bug_occurrence(bug_id) == 3
        # Refresh row and verify persistence. ``occurrence_count``
        # lives in ``BugReport.extra`` (JSON column) because it's
        # outside the canonical column set save_bug knows about.
        rows = _db.list_bugs(pid)
        assert (rows[0].get("extra") or {}).get("occurrence_count") == 3

    def test_bump_missing_count_starts_at_two(self):
        """A bug filed BEFORE PR-H lands has no occurrence_count
        field. Bumping must produce a sane "2" rather than 0/1.
        """
        sid = f"sid-{uuid.uuid4().hex}"
        pid = _db.upsert_project(name="legacy", owner_sid=sid)
        bug_id = _db.save_bug(pid, {
            "id": "BUG-001",
            "title": "Pre-PR-H bug",
            # No occurrence_count, no dedup_signature.
        }, source="walkthrough")
        assert _db.bump_bug_occurrence(bug_id) == 2

    def test_delete_bugs_for_project_returns_count_deleted(self):
        sid = f"sid-{uuid.uuid4().hex}"
        pid = _db.upsert_project(name="reset-target", owner_sid=sid)
        for i in range(4):
            _db.save_bug(pid, {
                "id": f"BUG-{i:03d}",
                "title": f"To be deleted #{i}",
            }, source="walkthrough")
        # Bugs from OTHER projects must survive the reset.
        other_pid = _db.upsert_project(name="reset-survivor", owner_sid=sid)
        _db.save_bug(other_pid, {
            "id": "BUG-099", "title": "Keep me",
        }, source="walkthrough")

        n = _db.delete_bugs_for_project(pid)
        assert n == 4
        assert _db.list_bugs(pid) == []
        # Other project's bug untouched.
        assert len(_db.list_bugs(other_pid)) == 1

    def test_delete_bugs_for_missing_project_returns_zero(self):
        assert _db.delete_bugs_for_project("does-not-exist") == 0
        assert _db.delete_bugs_for_project("") == 0


# ── 5. PR-J: Reset button visible on empty projects ───────────────


class TestResetButtonVisibilityOnEmptyProject:
    """PR-H originally rendered the Reset Project button inside the
    outer ``{% if bugs %}`` gate on /bug-reports. On a fresh project
    with zero bugs the button vanished, and operators (rightly)
    reported the feature as missing. PR-J moved the button + modal
    above the gate so they appear for every project. This regression
    test pins that contract: even with zero bugs in the project, the
    Reset button + modal markup render.
    """

    def test_reset_button_renders_on_empty_project(
        self, client, monkeypatch,
    ):
        sid = f"sid-{uuid.uuid4().hex}"
        pid = _db.upsert_project(name="empty-reset-test", owner_sid=sid)
        # Pin the session id helpers ``ensure_active_project`` /
        # ``_require_project_owner`` chain through so the test session
        # owns ``pid`` without going through a real login.
        monkeypatch.setattr(
            "routes.projects.get_session_id",
            lambda *_a, **_kw: sid,
        )
        monkeypatch.setattr(
            "routes.execution.get_session_id",
            lambda *_a, **_kw: sid,
        )
        with client.session_transaction() as sess:
            sess["project_id"] = pid

        resp = client.get("/bug-reports")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        # The Reset button + confirm-modal markup MUST be present
        # even though the project has zero bugs.
        assert 'id="bug-reset-open"' in body, (
            "Reset Project button must render on empty projects"
        )
        assert 'id="bug-reset-modal"' in body, (
            "Reset confirm modal must be in the DOM on empty projects"
        )
        # And the empty-state body should still render its own
        # message — the button addition mustn't have broken the
        # "No bug reports created yet" branch.
        assert "No bug reports created yet" in body or "bug_no_bugs" in body


# ── 4. BugReport carries the PR-H metadata round-trip ─────────────


class TestBugDataclassRoundtrip:
    def test_walkthrough_factory_populates_new_fields(self):
        bug = create_bug_from_walkthrough_finding({
            "severity": "Critical", "area": "Accessibility",
            "defect_class": "axe_critical",
            "message": "Select element must have an accessible name",
            "url": "https://artest.com/jobs",
            "element": ["#All-vacancies"],  # axe-style list
            "annotation_status": "annotated:/path/page_finding01_annotated.png",
        }, environment_str="Web", tester_name="ci")
        assert bug.defect_class == "axe_critical"
        assert bug.page_url == "https://artest.com/jobs"
        assert bug.dedup_signature
        assert len(bug.dedup_signature) == 16
        assert bug.occurrence_count == 1
        assert bug.annotation_status.startswith("annotated:")

    def test_bug_to_dict_and_dict_to_bug_round_trip(self):
        original = create_bug_from_walkthrough_finding({
            "severity": "Major", "area": "Images",
            "defect_class": "broken_image",
            "message": (
                "Broken image on the page — hero.svg did not load"
            ),
            "url": "https://artest.com/",
            "element": 'img[src="hero.svg"]',
            "annotation_status": "skipped:bbox_none",
        }, environment_str="Web", tester_name="ci")
        d = bug_to_dict(original)
        # The dict exposes the new fields so save_bug → extra works.
        assert d["dedup_signature"] == original.dedup_signature
        assert d["defect_class"] == "broken_image"
        assert d["annotation_status"] == "skipped:bbox_none"

        # Round-trip preserves the signature.
        restored = dict_to_bug(d)
        assert restored.dedup_signature == original.dedup_signature
        assert restored.defect_class == "broken_image"
        assert restored.occurrence_count == 1
        assert restored.annotation_status == "skipped:bbox_none"

    def test_dict_to_bug_tolerates_pre_pr_h_snapshots(self):
        """Sessions / exports created before PR-H lack the new
        fields. ``dict_to_bug`` must default them so the legacy
        snapshot still loads cleanly."""
        legacy = {
            "id": "BUG-001", "title": "Old",
            "severity": "Minor", "priority": "Low", "status": "Open",
            # No defect_class / page_url / dedup_signature / etc.
        }
        bug = dict_to_bug(legacy)
        assert bug.defect_class == ""
        assert bug.page_url == ""
        assert bug.dedup_signature == ""
        assert bug.occurrence_count == 1
        assert bug.annotation_status == ""
