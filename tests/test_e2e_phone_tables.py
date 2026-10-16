"""Estimation and Test Cases fit a 375 px screen (2026-10-16).

At 375 px the estimation breakdown table, the estimation source tabs and
each test case's label/value table pushed the page sideways — the whole
page scrolled horizontally. Now the estimation tables scroll inside a
focusable region of their own, the tabs wrap, and the detail table's
labels narrow while long values wrap. Fixtures and the overflow check are
tests/test_e2e_search.py's (the check itself is proven there to fail on a
planted 600 px element).
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright not installed")

from tests.test_e2e_search import (  # noqa: E402,F401 — fixtures
    WAIT, _create_project, _no_horizontal_overflow, _project_id, actors,
    browser, live_server, phone, search_on, team,
)


def test_estimation_and_test_cases_fit_a_phone(phone, team, search_on):
    from tests._search_seed import seed_golden

    person = phone(team["admin"]["email"]).sign_in()
    name = f"Phone tables {team['tag']}"
    _create_project(person, name)
    seed_golden(_project_id(team["org"], name))
    page = person.page

    person.go("/estimation?lang=en")
    page.wait_for_selector(".est-table", timeout=WAIT)
    _no_horizontal_overflow(page)
    wraps = page.evaluate("""() => [...document.querySelectorAll('.table-wrap')]
        .filter(w => w.querySelector('.est-table'))
        .map(w => [w.getAttribute('role'), w.getAttribute('tabindex'),
                   !!w.getAttribute('aria-label')])""")
    # A region that scrolls must be reachable by keyboard (axe
    # scrollable-region-focusable) and named.
    assert wraps and all(w == ["region", "0", True] for w in wraps), wraps

    person.go("/test-cases?lang=en")
    page.wait_for_selector(".tc-detail-table", timeout=WAIT)
    _no_horizontal_overflow(page)
