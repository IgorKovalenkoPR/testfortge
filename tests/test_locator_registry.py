"""PR-A — multi-locator Page Object library tests.

Pins the contract that the runner depends on:

* :func:`engine.locator_registry.rank_candidates` orders by score
  (testid > id > role > label > placeholder > alt > title > text > css >
  xpath) and dedups by selector value.
* :func:`engine.locator_registry.strategy_of` classifies raw selector
  strings back into the strategy taxonomy so the runner can stamp
  ``last_success_strategy`` from whichever candidate actually resolved.
* Recorder's parser populates ``AutomationStep.target_alternates`` AND
  ``locator_label`` from the codegen chain, with ≥ 2 candidates for any
  ``get_by_role`` call that carries a ``name=`` kwarg (role-only +
  text=name relaxations).
* DB-backed registry helpers (``register_candidates`` /
  ``record_success`` / ``record_failure`` / ``best_alternates``) round-
  trip cleanly and stay project-scoped — Project A's label is invisible
  to Project B.
* ``AutomationStep._decode_recorded_steps`` round-trips the new
  ``target_alternates`` + ``locator_label`` fields without a None
  surprise.
"""
from __future__ import annotations

import json

import pytest

from engine import db
from engine.automation_qa import AutomationStep, _decode_recorded_steps
from engine.locator_registry import (LocatorCandidate, best_alternates,
                                      candidates_to_targets, deserialise,
                                      rank_candidates, record_failure,
                                      record_success, register_candidates,
                                      serialise, strategy_of)
from engine.recorder_parser import parse_codegen_output


class TestRankCandidates:
    def test_orders_by_score_descending(self):
        ranked = rank_candidates([
            LocatorCandidate("xpath", "//button"),
            LocatorCandidate("css", "button.primary"),
            LocatorCandidate("role", 'role=button[name="Save"]'),
            LocatorCandidate("testid", "data-testid=save"),
            LocatorCandidate("id", "#save"),
        ])
        strategies = [c.strategy for c in ranked]
        # testid (100) > id (90) > role (70) > css (20) > xpath (10)
        assert strategies == ["testid", "id", "role", "css", "xpath"]

    def test_dedup_by_value(self):
        ranked = rank_candidates([
            LocatorCandidate("role", 'role=button[name="Save"]'),
            LocatorCandidate("role", 'role=button[name="Save"]'),
            LocatorCandidate("text", "text=Save"),
        ])
        # Duplicate role= dropped, dedup'd to one entry.
        values = [c.value for c in ranked]
        assert values.count('role=button[name="Save"]') == 1
        assert "text=Save" in values

    def test_blank_value_dropped(self):
        ranked = rank_candidates([
            LocatorCandidate("role", ""),
            LocatorCandidate("text", "text=Hi"),
        ])
        assert len(ranked) == 1
        assert ranked[0].value == "text=Hi"

    def test_stable_on_ties(self):
        # Two equally-ranked role= selectors should keep their input order.
        ranked = rank_candidates([
            LocatorCandidate("role", 'role=button[name="A"]'),
            LocatorCandidate("role", 'role=button[name="B"]'),
        ])
        assert [c.value for c in ranked] == [
            'role=button[name="A"]',
            'role=button[name="B"]',
        ]


class TestStrategyOf:
    @pytest.mark.parametrize("selector, expected", [
        ("data-testid=submit",            "testid"),
        ('role=button[name="Sign in"]',   "role"),
        ("role=link",                     "role"),
        ("label=Email",                   "label"),
        ("placeholder=Search",            "placeholder"),
        ("text=Welcome",                  "text"),
        ("alt=Logo",                      "alt"),
        ("title=Help",                    "title"),
        ("#sidebar",                      "id"),
        ("id=login",                      "id"),
        ("//div[@class='x']",             "xpath"),
        ("xpath=//div",                   "xpath"),
        (".btn-primary",                  "css"),
        ("button:has-text('Save')",       "css"),
        ("",                              ""),
    ])
    def test_classification(self, selector, expected):
        assert strategy_of(selector) == expected


class TestCandidatesToTargets:
    def test_empty_yields_empty(self):
        primary, alts = candidates_to_targets([])
        assert primary == ""
        assert alts == []

    def test_first_is_primary_rest_are_alternates(self):
        ranked = [
            LocatorCandidate("testid", "data-testid=save"),
            LocatorCandidate("role", "role=button"),
            LocatorCandidate("text", "text=Save"),
        ]
        primary, alts = candidates_to_targets(ranked)
        assert primary == "data-testid=save"
        assert alts == ["role=button", "text=Save"]


class TestSerialiseDeserialise:
    def test_round_trip(self):
        original = [
            LocatorCandidate("testid", "data-testid=submit"),
            LocatorCandidate("text", "text=Go"),
        ]
        payload = serialise(original)
        assert payload == [
            {"strategy": "testid", "value": "data-testid=submit", "score": 100},
            {"strategy": "text",   "value": "text=Go",            "score": 40},
        ]
        restored = deserialise(payload)
        assert [(c.strategy, c.value) for c in restored] == [
            ("testid", "data-testid=submit"),
            ("text",   "text=Go"),
        ]

    def test_deserialise_drops_bad_rows(self):
        cands = deserialise([
            {"strategy": "testid", "value": "data-testid=ok"},
            {"strategy": "", "value": "noop"},          # blank strategy
            {"strategy": "role", "value": ""},           # blank value
            {"random": "garbage"},                       # missing keys
            "not even a dict",
        ])
        assert [c.value for c in cands] == ["data-testid=ok"]

    def test_deserialise_non_list_returns_empty(self):
        assert deserialise({"not": "a list"}) == []  # type: ignore[arg-type]


class TestAutomationStepRoundTrip:
    """``AutomationStep`` must round-trip the new PR-A fields through
    the DB column without losing alternates or label."""

    def test_decode_preserves_alternates_and_label(self):
        payload = json.dumps([{
            "action": "click",
            "target": "data-testid=signin",
            "value": "",
            "raw": "page.get_by_test_id('signin').click()",
            "comment": "",
            "target_alternates": [
                'role=button[name="Sign in"]',
                "text=Sign in",
            ],
            "locator_label": "testid=signin",
        }])
        steps = _decode_recorded_steps(payload)
        assert len(steps) == 1
        s = steps[0]
        assert s.target == "data-testid=signin"
        assert s.target_alternates == [
            'role=button[name="Sign in"]',
            "text=Sign in",
        ]
        assert s.locator_label == "testid=signin"

    def test_decode_legacy_payload_has_empty_alternates(self):
        """Pre-PR-A recordings (PR-B shape) must still decode — empty
        alternates + empty label."""
        payload = json.dumps([{
            "action": "click",
            "target": "data-testid=submit",
            "value": "",
            "raw": "page.get_by_test_id('submit').click()",
            "comment": "",
        }])
        steps = _decode_recorded_steps(payload)
        assert steps[0].target_alternates == []
        assert steps[0].locator_label == ""

    def test_decode_bad_alternates_falls_back_to_empty(self):
        payload = json.dumps([{
            "action": "click",
            "target": "role=button",
            "target_alternates": "not a list",
        }])
        assert _decode_recorded_steps(payload)[0].target_alternates == []


class TestRecorderParserCandidates:
    """The codegen parser must populate alternates + label from a
    locator chain so the runner has something to fall back to without
    a Playwright DOM probe in the loop."""

    def test_role_with_name_yields_role_only_and_text_fallbacks(self):
        src = '''
async def run(playwright):
    await page.get_by_role("button", name="Sign in").click()
'''
        steps = parse_codegen_output(src)
        assert len(steps) == 1
        s = steps[0]
        assert s.target == 'role=button[name="Sign in"]'
        # Role-only and text=Sign in fallbacks should be present.
        assert "role=button" in s.target_alternates
        assert "text=Sign in" in s.target_alternates
        # Label captures leaf identity — same recording → same label.
        assert s.locator_label == "role=button:Sign in"

    def test_test_id_yields_stable_label(self):
        src = '''
async def run(playwright):
    await page.get_by_test_id("submit-btn").click()
'''
        steps = parse_codegen_output(src)
        assert steps[0].locator_label == "testid=submit-btn"

    def test_chained_locator_emits_leaf_and_ancestor_alternates(self):
        src = '''
async def run(playwright):
    await page.locator("#sidebar").get_by_text("Settings").click()
'''
        steps = parse_codegen_output(src)
        s = steps[0]
        assert s.target == "#sidebar >> text=Settings"
        # Leaf alone and ancestor alone are both viable fallbacks.
        assert "text=Settings" in s.target_alternates
        assert "#sidebar" in s.target_alternates

    def test_plain_label_has_no_fallback_alternates(self):
        """A ``get_by_label("Email")`` capture has no derivable
        alternates; we still emit the step with empty alternates."""
        src = '''
async def run(playwright):
    await page.get_by_label("Email").fill("user@x")
'''
        steps = parse_codegen_output(src)
        s = steps[0]
        assert s.target == "label=Email"
        assert s.target_alternates == []
        assert s.locator_label == "label=Email"


@pytest.fixture
def two_projects(app):
    """Two real projects so we can prove cross-project isolation in
    the locator table."""
    db.init_db()
    pid_a = db.upsert_project(name="A", base_url="https://a.test",
                               owner_sid="t-A")
    pid_b = db.upsert_project(name="B", base_url="https://b.test",
                               owner_sid="t-B")
    yield pid_a, pid_b
    db.delete_project(pid_a)
    db.delete_project(pid_b)


class TestRegistryDbRoundTrip:
    def test_register_creates_row(self, two_projects):
        pid_a, _ = two_projects
        candidates = [
            LocatorCandidate("testid", "data-testid=save"),
            LocatorCandidate("role", 'role=button[name="Save"]'),
        ]
        rid = register_candidates(pid_a, "login.signIn", candidates)
        assert rid > 0
        row = db.get_locator(pid_a, "login.signIn")
        assert row is not None
        assert row["label"] == "login.signIn"
        # First candidate (testid) is the highest-scored one.
        assert row["candidates"][0]["strategy"] == "testid"
        assert row["success_count"] == 0
        assert row["fail_count"] == 0
        assert row["last_success_strategy"] is None

    def test_record_success_bumps_count_and_strategy(self, two_projects):
        pid_a, _ = two_projects
        register_candidates(pid_a, "login.signIn", [
            LocatorCandidate("testid", "data-testid=save"),
            LocatorCandidate("role", 'role=button[name="Save"]'),
        ])
        # Simulate the second alternate winning.
        ok = record_success(pid_a, "login.signIn", 'role=button[name="Save"]')
        assert ok is True
        row = db.get_locator(pid_a, "login.signIn")
        assert row["success_count"] == 1
        assert row["last_success_strategy"] == "role"

        # Another success on the same strategy bumps the count again.
        record_success(pid_a, "login.signIn", 'role=button[name="Save"]')
        assert db.get_locator(pid_a, "login.signIn")["success_count"] == 2

    def test_record_failure_bumps_fail_count(self, two_projects):
        pid_a, _ = two_projects
        register_candidates(pid_a, "missing.element",
                            [LocatorCandidate("css", ".gone")])
        record_failure(pid_a, "missing.element")
        assert db.get_locator(pid_a, "missing.element")["fail_count"] == 1

    def test_record_success_on_unknown_row_returns_false(self, two_projects):
        pid_a, _ = two_projects
        # No register_candidates call → no row → record_success no-op.
        assert record_success(pid_a, "never.registered", "data-testid=x") is False

    def test_best_alternates_promotes_winning_strategy(self, two_projects):
        pid_a, _ = two_projects
        register_candidates(pid_a, "login.signIn", [
            LocatorCandidate("testid", "data-testid=save"),
            LocatorCandidate("role", 'role=button[name="Save"]'),
            LocatorCandidate("text", "text=Save"),
        ])
        # Pretend the role= selector was what worked on the last run.
        record_success(pid_a, "login.signIn", 'role=button[name="Save"]')
        promoted = best_alternates(pid_a, "login.signIn", defaults=["x"])
        # role= moves to front; testid + text follow.
        assert promoted[0] == 'role=button[name="Save"]'
        assert "data-testid=save" in promoted
        assert "text=Save" in promoted

    def test_best_alternates_falls_back_to_defaults_when_no_row(
            self, two_projects):
        pid_a, _ = two_projects
        # No registration for this label — caller's defaults survive.
        out = best_alternates(pid_a, "never.registered",
                              defaults=["role=button", "text=Save"])
        assert out == ["role=button", "text=Save"]

    def test_project_isolation(self, two_projects):
        pid_a, pid_b = two_projects
        register_candidates(pid_a, "login.signIn",
                            [LocatorCandidate("testid", "data-testid=A")])
        # Project B never registered "login.signIn".
        assert db.get_locator(pid_b, "login.signIn") is None
        # And best_alternates for B does NOT see A's candidates.
        out_b = best_alternates(pid_b, "login.signIn",
                                defaults=["fallback"])
        assert out_b == ["fallback"]

    def test_blank_project_or_label_is_noop(self):
        # No DB roundtrip should happen — these should all return False / 0.
        assert register_candidates("", "x", [LocatorCandidate("testid", "a")]) == 0
        assert register_candidates("pid", "", [LocatorCandidate("testid", "a")]) == 0
        assert record_success("", "x", "data-testid=a") is False
        assert record_failure("pid", "") is False
