"""Which level of the pyramid a test file sits at (not a test module).

``tests/conftest.py`` calls :func:`level_for` at collection time and adds
the answer as a marker, so ``pytest -m unit`` / ``-m "not e2e"`` select by
level without anyone having to remember to decorate a new file.

The level comes from the **file name** first — an explicit mapping, because
a name like ``test_execute_e2e.py`` says what its author meant better than
any heuristic — and only for names the mapping does not cover from what the
file *uses*: a test that takes the Flask client or touches the database is
an integration test, anything else is a unit test.

Levels: ``unit``, ``integration``, ``functional``, ``e2e``, ``smoke``.
``load`` is registered for the load suite the programme has not written
yet; nothing maps to it today.
"""
from __future__ import annotations

import re

LEVELS = ("unit", "integration", "functional", "e2e", "smoke", "load")

SMOKE = frozenset({"test_load_smoke", "test_basic_gate",
                   "test_cold_start_recovery"})

E2E_EXACT = frozenset({"test_te_e2e_playwright", "test_execute_e2e"})

FUNCTIONAL_EXACT = frozenset({"test_pipeline_e2e",
                              "test_every_template_is_reachable"})

UNIT_EXACT = frozenset({"test_gherkin", "test_glossary", "test_search_text",
                        "test_search_embed", "test_search_memory",
                        "test_ai_mode", "test_design_tokens",
                        "test_model_switcher"})

#: Fixtures that stand up the Flask app or a request context.
APP_FIXTURES = frozenset({"app", "client", "anon_client", "fresh_org",
                          "make_project", "sign_in", "as_user", "auth_on",
                          "auth_off", "forget_workspace", "live_server"})

#: Source that means "talks to the database or the app" when no fixture
#: says so (module-level helpers, ``app.test_client()`` built by hand).
_USES_APP_OR_DB = re.compile(
    r"from engine import [^\n]*\bdb\b|from engine\.db import|"
    r"import engine\.db|\.test_client\(|from app import|session_scope\(")


def level_by_name(stem: str) -> str | None:
    """The level the file name decides, or ``None`` when it does not."""
    if stem in SMOKE:
        return "smoke"
    if stem.startswith("test_e2e") or stem.endswith("_browser") \
            or stem in E2E_EXACT:
        return "e2e"
    if stem.startswith("test_functional") or stem in FUNCTIONAL_EXACT \
            or stem.startswith("test_tedgie_"):
        return "functional"
    if stem.endswith("_unit") or stem in UNIT_EXACT or "rules" in stem \
            or stem.startswith("test_llm_"):
        return "unit"
    return None


def level_for(stem: str, fixturenames, source: str) -> str:
    """The level for a file's tests: by name, else ``integration`` when the
    file uses the app or the database (*fixturenames* is every fixture any
    test in the file requests), else ``unit``."""
    by_name = level_by_name(stem)
    if by_name:
        return by_name
    if APP_FIXTURES.intersection(fixturenames or ()):
        return "integration"
    if _USES_APP_OR_DB.search(source or ""):
        return "integration"
    return "unit"
