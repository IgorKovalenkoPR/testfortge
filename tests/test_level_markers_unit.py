"""The level markers: the mapping, and that collection actually applies it.

The mapping is a table, so it is checked as one — every name the
programme's list mentions, with the level it must get. The second half
runs a real collection in a subprocess, because a hook that maps correctly
and is never called leaves every test unmarked, and ``-m unit`` would then
select nothing and pass.
"""
from __future__ import annotations

import configparser
import pathlib
import subprocess
import sys

import pytest

from tests._levels import LEVELS, level_by_name, level_for

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("stem,level", [
    # unit
    ("test_automation_unit", "unit"), ("test_estimation_unit", "unit"),
    ("test_unit", "unit"),
    ("test_gherkin", "unit"), ("test_glossary", "unit"),
    ("test_tc_rules", "unit"), ("test_checklist_rules", "unit"),
    ("test_tc_rules_grids", "unit"),
    ("test_search_text", "unit"), ("test_search_embed", "unit"),
    ("test_search_memory", "unit"), ("test_ai_mode", "unit"),
    ("test_llm_client", "unit"), ("test_llm_result_cache", "unit"),
    ("test_design_tokens", "unit"), ("test_model_switcher", "unit"),
    # functional
    ("test_functional", "functional"),
    ("test_functional_module_matrix", "functional"),
    ("test_pipeline_e2e", "functional"),
    ("test_every_template_is_reachable", "functional"),
    ("test_tedgie_eval", "functional"), ("test_tedgie_mentoring", "functional"),
    # e2e
    ("test_e2e", "e2e"), ("test_e2e_golden_paths", "e2e"),
    ("test_e2e_search", "e2e"), ("test_e2e_a11y", "e2e"),
    ("test_recorder_locator_ladder_browser", "e2e"),
    ("test_te_e2e_playwright", "e2e"), ("test_execute_e2e", "e2e"),
    # smoke
    ("test_load_smoke", "smoke"), ("test_basic_gate", "smoke"),
    ("test_cold_start_recovery", "smoke"),
])
def test_the_name_decides(stem, level):
    assert level_by_name(stem) == level
    # And the name wins over what the file uses.
    assert level_for(stem, {"client"}, "from engine import db") == level


def test_an_unmapped_file_is_integration_when_it_uses_the_app_or_db():
    assert level_by_name("test_bug_workflow") is None
    assert level_for("test_bug_workflow", {"client"}, "") == "integration"
    assert level_for("test_x", set(), "from engine import db as _db\n") \
        == "integration"
    assert level_for("test_x", set(), "c = app.test_client()") \
        == "integration"


def test_an_unmapped_file_that_uses_neither_is_unit():
    assert level_for("test_summary_voice", {"monkeypatch"},
                     "from engine import summary_voice") == "unit"


def test_every_level_and_network_is_registered():
    ini = configparser.ConfigParser()
    ini.read(ROOT / "pytest.ini", encoding="utf-8")
    declared = {line.split(":")[0].split("(")[0].strip()
                for line in ini["pytest"]["markers"].splitlines()
                if line.strip()}
    assert set(LEVELS) | {"network"} <= declared, declared


def _collect(*args: str) -> list[str]:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", "-p", "no:xdist", *args],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        timeout=300)
    assert out.returncode in (0, 5), out.stdout[-2000:] + out.stderr[-2000:]
    return [line for line in out.stdout.splitlines() if "::" in line]


class TestCollectionAppliesTheMarkers:
    def test_m_selects_by_level(self):
        unit = _collect("tests/test_glossary.py", "tests/test_csp_headers.py",
                        "-m", "unit")
        integ = _collect("tests/test_glossary.py",
                         "tests/test_csp_headers.py", "-m", "integration")
        assert unit and all("test_glossary.py" in t for t in unit), unit
        assert integ and all("test_csp_headers.py" in t for t in integ), \
            integ

    def test_not_network_drops_exactly_the_live_fetches(self):
        everything = _collect("tests/test_functional.py")
        offline = _collect("tests/test_functional.py", "-m", "not network")
        dropped = sorted(set(everything) - set(offline))
        assert [d.rsplit("::", 1)[1] for d in dropped] == [
            "test_post_instruction_filtered",
            "test_post_url_generates_professional_checklist",
            "test_post_url_generates_test_cases",
        ], dropped
