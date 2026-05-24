"""Unit tests for ``suggest_team_size`` and the corresponding
``EstimationResult.suggested_team_size`` field.

The suggestion is shown in the UI as a read-only badge — overriding
remains possible, but the badge value must remain a deterministic
function of (total_hours, complexity_tier) so users don't see it flip
between runs of an unchanged estimate.
"""
import pytest

from engine.qa_estimator import (
    Feature, compute_estimation, suggest_team_size,
)


@pytest.mark.parametrize("hours, tier, expected", [
    # Brochure / landing — solo tester is enough
    (10,   "simple", 1),
    (49,   "simple", 1),
    # Small project — pair
    (50,   "simple", 2),
    (100,  "simple", 2),
    (199,  "simple", 2),
    # Mid-sized — small team
    (200,  "simple", 3),
    (300,  "medium", 3),
    (499,  "simple", 3),
    # Large engagement — 5-person team
    (500,  "simple", 5),
    (999,  "simple", 5),
    # Multi-quarter / enterprise
    (1000, "simple", 7),
    (5000, "simple", 7),
    # Complex tier always adds +1
    (300,  "complex", 4),
    (700,  "complex", 6),
])
def test_suggest_team_size_heuristic(hours, tier, expected):
    assert suggest_team_size(hours, tier) == expected


def test_suggest_team_size_handles_none():
    # Defensive default — None / 0 should not crash and should bias
    # toward "single tester" since there's no real estimate yet.
    assert suggest_team_size(None) == 1
    assert suggest_team_size(0) == 1


def test_suggest_team_size_caps_at_twelve():
    # Defensive ceiling — current heuristic tops out below 12, but the
    # min() cap protects future tweaks (e.g. higher tiers) from ever
    # overflowing the badge.
    assert suggest_team_size(100_000, "complex") <= 12


def test_compute_estimation_populates_suggested_team_size():
    # End-to-end: an EstimationResult must carry the suggestion
    # regardless of whether the caller passed team_size= explicitly.
    features = [Feature(name="Module", test_cases=250)]
    result = compute_estimation(features=features, rate_usd=30.0)
    # one_plat_total_expected should be a positive number for 250 TCs
    assert result.one_plat_total_expected > 0
    # The suggestion is a function of the result, not user input.
    expected = suggest_team_size(
        result.one_plat_total_expected, result.complexity_tier,
    )
    assert result.suggested_team_size == expected
    assert 1 <= result.suggested_team_size <= 12


def test_suggested_team_size_default_when_no_features():
    # No features → zero hours → suggestion stays at 1 (solo).
    result = compute_estimation(features=[], rate_usd=30.0)
    assert result.suggested_team_size == 1
