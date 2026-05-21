"""Sprint 5 follow-up #3 — engine.walkthrough_stats CLI/aggregator.

The walkthrough heuristic battery shipped in PR #12 will need
severity tuning after prod runs surface false-positive patterns.
:mod:`engine.walkthrough_stats` is the tooling that turns that
future tuning from "grep result.json by hand" into "one summary
command".

These tests pin the aggregation correctness against fixture data so
a later refactor of the printer can't silently change the counts the
operator is supposed to act on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ── 1. Pure-function aggregation ─────────────────────────────────


class TestSummariseFindings:
    def test_total_count_matches_input(self):
        from engine.walkthrough_stats import summarise_findings
        findings = [
            {"defect_class": "broken_image", "severity": "Critical",
             "area": "Images", "url": "https://example.com/",
             "message": "Broken image on hero"},
            {"defect_class": "broken_image", "severity": "Critical",
             "area": "Images", "url": "https://example.com/about",
             "message": "Broken image in footer"},
            {"defect_class": "axe_serious", "severity": "Major",
             "area": "Accessibility", "url": "https://example.com/",
             "message": "Form input missing label"},
        ]
        summary = summarise_findings(findings)
        assert summary["total"] == 3

    def test_per_class_count_and_severity_histogram(self):
        from engine.walkthrough_stats import summarise_findings
        findings = [
            {"defect_class": "broken_image", "severity": "Critical",
             "message": "img a"},
            {"defect_class": "broken_image", "severity": "Critical",
             "message": "img b"},
            {"defect_class": "broken_image", "severity": "Major",
             "message": "img c"},
            {"defect_class": "axe_serious", "severity": "Major",
             "message": "axe a"},
        ]
        s = summarise_findings(findings)
        assert s["by_class"]["broken_image"]["count"] == 3
        # Severity histogram counts both Critical (2) and Major (1).
        assert s["by_class"]["broken_image"]["severity"]["Critical"] == 2
        assert s["by_class"]["broken_image"]["severity"]["Major"] == 1
        assert s["by_class"]["axe_serious"]["count"] == 1

    def test_severity_totals_collapse_across_classes(self):
        from engine.walkthrough_stats import summarise_findings
        findings = [
            {"defect_class": "a", "severity": "Critical"},
            {"defect_class": "b", "severity": "Critical"},
            {"defect_class": "c", "severity": "Major"},
        ]
        s = summarise_findings(findings)
        assert s["by_severity"]["Critical"] == 2
        assert s["by_severity"]["Major"] == 1

    def test_samples_are_deduplicated_and_capped(self):
        """Avoid 50 copies of the same headline in the samples list —
        the human eyeballing the table wants variety, not noise."""
        from engine.walkthrough_stats import summarise_findings
        findings = [
            {"defect_class": "broken_image", "severity": "Critical",
             "message": "Broken image"} for _ in range(10)
        ] + [
            {"defect_class": "broken_image", "severity": "Critical",
             "message": "Another broken image"},
        ]
        s = summarise_findings(findings, max_samples=3)
        samples = s["by_class"]["broken_image"]["samples"]
        assert len(samples) == 2  # dedup collapsed identical, kept distinct
        assert "Broken image" in samples
        assert "Another broken image" in samples

    def test_unknown_severity_or_class_falls_under_unknown(self):
        from engine.walkthrough_stats import summarise_findings
        findings = [
            {"message": "no class no severity"},
            {"severity": "Critical", "defect_class": ""},
        ]
        s = summarise_findings(findings)
        # Both have empty/missing defect_class → "unknown".
        assert s["by_class"]["unknown"]["count"] == 2
        # First finding's missing severity → "Unknown" bucket.
        assert s["by_severity"]["Unknown"] == 1
        assert s["by_severity"]["Critical"] == 1

    def test_non_dict_entries_are_skipped(self):
        """Defensive: result.json corruption shouldn't crash the
        summariser — just skip the bad rows."""
        from engine.walkthrough_stats import summarise_findings
        # Type-checker complains about the mixed types; the function
        # exists to be defensive at runtime so we pass a noisy list.
        s = summarise_findings([
            {"defect_class": "ok", "severity": "Major"},
            "garbage",  # type: ignore[list-item]
            None,        # type: ignore[list-item]
            42,          # type: ignore[list-item]
        ])
        assert s["total"] == 1


# ── 2. File loading + dedup preference ──────────────────────────


class TestSummariseFiles:
    def _write(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_prefers_deduped_view_when_present(self, tmp_path):
        from engine.walkthrough_stats import summarise_files
        # raw list has 4 entries (one duplicate), deduped has 3 —
        # the summariser must pick the deduped view to mirror what
        # the operator sees in the UI.
        result_path = tmp_path / "run.result.json"
        self._write(result_path, {
            "walkthrough_findings": [
                {"defect_class": "broken_image", "severity": "Critical"},
                {"defect_class": "broken_image", "severity": "Critical"},
                {"defect_class": "broken_image", "severity": "Critical"},
                {"defect_class": "axe_serious", "severity": "Major"},
            ],
            "walkthrough_findings_deduped": [
                {"defect_class": "broken_image", "severity": "Critical"},
                {"defect_class": "axe_serious", "severity": "Major"},
                {"defect_class": "cta_no_destination",
                 "severity": "Major"},
            ],
        })
        s = summarise_files([str(result_path)])
        assert s["total"] == 3
        # cta_no_destination is only in the deduped list — if the
        # tool fell back to raw, this class wouldn't appear.
        assert "cta_no_destination" in s["by_class"]

    def test_falls_back_to_raw_when_deduped_empty_or_missing(self, tmp_path):
        from engine.walkthrough_stats import summarise_files
        # Deduped list is empty — the worker's dedup path swallowed
        # an exception and only the raw list landed.
        result_path = tmp_path / "raw.result.json"
        self._write(result_path, {
            "walkthrough_findings": [
                {"defect_class": "broken_image", "severity": "Critical"},
            ],
            "walkthrough_findings_deduped": [],
        })
        s = summarise_files([str(result_path)])
        assert s["total"] == 1

    def test_multi_file_aggregation(self, tmp_path):
        from engine.walkthrough_stats import summarise_files
        a = tmp_path / "a.result.json"
        b = tmp_path / "b.result.json"
        self._write(a, {"walkthrough_findings_deduped": [
            {"defect_class": "broken_image", "severity": "Critical"},
        ]})
        self._write(b, {"walkthrough_findings_deduped": [
            {"defect_class": "axe_serious", "severity": "Major"},
            {"defect_class": "axe_serious", "severity": "Major"},
        ]})
        s = summarise_files([str(a), str(b)])
        assert s["total"] == 3
        assert s["by_class"]["axe_serious"]["count"] == 2

    def test_missing_or_corrupt_file_is_skipped(self, tmp_path):
        from engine.walkthrough_stats import summarise_files
        good = tmp_path / "good.json"
        bad = tmp_path / "bad.json"
        nope = tmp_path / "does-not-exist.json"
        self._write(good, {"walkthrough_findings_deduped": [
            {"defect_class": "broken_image", "severity": "Critical"},
        ]})
        bad.write_text("not json at all", encoding="utf-8")
        # nope is intentionally absent.
        s = summarise_files([str(good), str(bad), str(nope)])
        assert s["total"] == 1


# ── 3. CLI entry point ───────────────────────────────────────────


class TestCliEntryPoint:
    def test_main_with_no_args_prints_usage_and_returns_2(self, capsys):
        from engine.walkthrough_stats import main
        rc = main([])
        captured = capsys.readouterr()
        assert rc == 2
        # Usage line lands on stderr; stdout stays clean so scripts
        # piping output don't see noise.
        assert "usage:" in captured.err.lower()
        assert captured.out == ""

    def test_main_with_one_file_prints_summary(self, tmp_path, capsys):
        from engine.walkthrough_stats import main
        result_path = tmp_path / "run.result.json"
        result_path.write_text(json.dumps({
            "walkthrough_findings_deduped": [
                {"defect_class": "broken_image", "severity": "Critical",
                 "area": "Images", "message": "Hero image broken",
                 "url": "https://example.com/"},
                {"defect_class": "axe_serious", "severity": "Major",
                 "area": "Accessibility",
                 "message": "Form input missing label",
                 "url": "https://example.com/contact"},
            ]
        }), encoding="utf-8")
        rc = main([str(result_path)])
        captured = capsys.readouterr()
        assert rc == 0
        # Output sections — defect_class block + severity totals +
        # top URLs, all rendered for the operator.
        assert "broken_image" in captured.out
        assert "axe_serious" in captured.out
        assert "Severity totals" in captured.out
        assert "Top URLs" in captured.out
