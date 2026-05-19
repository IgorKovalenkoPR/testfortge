"""Tests for engine.estimation_service — the unified pipeline both
routes share."""
from __future__ import annotations

import pytest

from engine.estimation_service import (
    EstimationInput, EstimationOutput, run_estimation,
)


_TEXT = """Take Notes
- Writing and editing notes
- Audio transcription
Notifications
- Calendar integration
- Push delivery
Settings
- Theme switcher
- Profile editing
"""


def _base_input(**overrides) -> EstimationInput:
    base = EstimationInput(
        source_choice="text",
        text_input=_TEXT,
        project_name="Test",
        rate_usd=30.0,
        additional_platforms=9,
        minutes_per_tc=5,
        buffer=1.12,
        primary_platform="Windows 10",
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def test_text_branch_produces_features():
    out = run_estimation(_base_input())
    assert isinstance(out, EstimationOutput)
    assert out.features_count > 0
    assert out.result_dict["total_tc"] > 0
    assert out.source_label == "text"


def test_no_features_raises_runtime_error():
    with pytest.raises(RuntimeError):
        run_estimation(_base_input(text_input=""))


def test_text_fallback_recovers_when_chosen_tab_empty():
    # source=url with no URL → fallback to pasted text
    out = run_estimation(_base_input(
        source_choice="url", url="", text_input=_TEXT))
    assert out.features_count > 0
    # warnings should mention the missing URL
    assert any("URL" in w for w in out.warnings)


def test_team_size_flows_through_to_brooks_penalty():
    # Brooks's-law overhead must be zero for a single tester and
    # positive once team_size > 1. This is the bug the form was
    # missing — the service must read team_size from EstimationInput.
    solo = run_estimation(_base_input(team_size=1))
    team = run_estimation(_base_input(team_size=4))

    assert solo.result_dict["brooks_overhead_hours"] == 0.0
    assert team.result_dict["brooks_overhead_hours"] > 0.0
    assert team.result_dict["team_size"] == 4


def test_custom_coefficients_pass_through():
    out = run_estimation(_base_input(
        compatibility_rate=0.005,
        bug_report_rate=0.20,
        pm_overhead=0.12,
        max_testing_stretch=1.8,
    ))
    rd = out.result_dict
    assert rd["compatibility_rate"] == 0.005
    assert rd["bug_report_rate"] == 0.20
    assert rd["pm_overhead"] == 0.12
    assert rd["max_testing_stretch"] == 1.8


def test_attachment_path_missing_falls_back_to_text():
    out = run_estimation(_base_input(
        source_choice="text",
        attachment_path="/nonexistent/path/should-not-exist.txt",
        text_input=_TEXT,
    ))
    assert out.features_count > 0


def test_features_count_matches_non_section_features():
    out = run_estimation(_base_input())
    non_section = [f for f in out.result_dict["features"]
                   if not f.get("is_section")]
    assert out.features_count == len(non_section)
