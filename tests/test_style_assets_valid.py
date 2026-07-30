"""The style assets must be valid, well-shaped YAML — not just readable.

``engine/tc_author.py`` loads these files as **raw text** (deliberately:
the inline ``evidence:`` comments are prompt material and parsing would
drop them). The side effect is that nothing ever validated the syntax.

``coverage_rules.yaml`` shipped with an invalid ``per_control_type:
dropdown:`` entry that mixed a sequence with a ``note:`` mapping key at
the same level. It went unnoticed for as long as the file was only ever
pasted into a prompt, and only surfaced when the deterministic generator
needed the same file as data.

So: parse both on every run, and assert the shapes the rules engine
depends on.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

from engine import tc_author


STYLE_DIR = (pathlib.Path(__file__).resolve().parent.parent
             / "engine" / "qa_knowledge" / "style")

CATEGORIES = {"Positive", "Negative"}
PRIORITIES = {"High", "Medium", "Low"}


@pytest.fixture(scope="module")
def coverage() -> dict:
    return yaml.safe_load((STYLE_DIR / "coverage_rules.yaml")
                          .read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def house_style() -> dict:
    return yaml.safe_load((STYLE_DIR / "house_style.yaml")
                          .read_text(encoding="utf-8"))


class TestBothAssetsParse:
    @pytest.mark.parametrize("name", ["house_style.yaml",
                                      "coverage_rules.yaml"])
    def test_parses_as_yaml(self, name):
        raw = (STYLE_DIR / name).read_text(encoding="utf-8")
        doc = yaml.safe_load(raw)
        assert isinstance(doc, dict), f"{name} must parse to a mapping"
        assert doc.get("version"), f"{name} must declare a version"

    @pytest.mark.parametrize("name", ["house_style.yaml",
                                      "coverage_rules.yaml"])
    def test_still_loadable_as_raw_prompt_text(self, name):
        # The LLM path needs the comments, so raw reading must keep working.
        loader = (tc_author.house_style_text if name.startswith("house")
                  else tc_author.coverage_rules_text)
        text = loader()
        assert text and "version:" in text
        # Both carry their rationale as comments, which is the reason they
        # are read raw rather than parsed for the prompt.
        assert "#" in text

    def test_house_style_keeps_its_measured_evidence(self):
        # The `evidence:` notes are what stop a house convention reading
        # as negotiable. coverage_rules.yaml carries its rationale in
        # comments and `note:` instead, so this applies to house_style only.
        text = tc_author.house_style_text()
        assert "evidence:" in text
        assert "4,808" in text


class TestCoverageShape:
    """Shapes the deterministic engine indexes into."""

    def test_per_control_type_is_uniform(self, coverage):
        pct = coverage["create_form"]["per_control_type"]
        assert pct, "no control types declared"
        for name, spec in pct.items():
            assert isinstance(spec, dict), (
                f"{name} must be a mapping with a 'cases' list, not a bare "
                f"sequence — mixing the two is the invalid YAML this file "
                f"originally shipped with")
            assert isinstance(spec.get("cases"), list) and spec["cases"], (
                f"{name} needs a non-empty 'cases' list")
            if "note" in spec:
                assert isinstance(spec["note"], str)

    def test_every_case_has_an_objective_and_valid_category(self, coverage):
        pct = coverage["create_form"]["per_control_type"]
        for name, spec in pct.items():
            for case in spec["cases"]:
                assert case.get("objective"), f"{name}: case with no objective"
                assert case.get("category") in CATEGORIES, (
                    f"{name}: bad category {case.get('category')!r}")

    def test_the_control_types_the_engine_maps_onto_exist(self, coverage):
        # Crawler field types are mapped onto these keys; a rename here
        # silently drops coverage for that input type.
        pct = coverage["create_form"]["per_control_type"]
        required = {"text_input", "email_input", "numeric_input",
                    "date_input", "dropdown", "checkbox", "file_upload",
                    "rich_text"}
        assert required <= set(pct), sorted(required - set(pct))

    def test_form_level_checks_are_well_formed(self, coverage):
        for check in coverage["create_form"]["checks"]:
            assert check.get("objective")
            assert check.get("category") in CATEGORIES
            assert check.get("priority") in PRIORITIES

    def test_must_pair_rules_declare_when_and_derive(self, coverage):
        pairs = coverage["create_form"]["must_pair"]
        assert pairs
        for rule in pairs:
            assert rule.get("when") and rule.get("derive")

    @pytest.mark.parametrize("section", ["list_surface", "create_form",
                                         "detail_form", "state_machine",
                                         "permissions", "error_messages",
                                         "cross_cutting", "sizing"])
    def test_top_level_sections_present(self, coverage, section):
        assert section in coverage


class TestHouseStyleShape:
    def test_declares_the_rules_the_linter_enforces(self, house_style):
        for key in ("summary_grammar", "preconditions", "steps",
                    "expected_result", "categories", "anti_patterns"):
            assert key in house_style, key

    def test_anti_patterns_carry_a_reason(self, house_style):
        for item in house_style["anti_patterns"]:
            assert item.get("text") and item.get("why")
