"""Inline event handlers under the real CSP — in a real browser.

``script-src`` carries a per-request nonce and no ``'unsafe-inline'``
(commit 466226e), and the browser refuses every ``on*=""`` attribute.
Twenty-two were still in the templates on 2026-10-16: the test-case tabs
and filters and the checklist filters did nothing, and every ``confirm()``
before a destructive POST was skipped — deleting a project asked nothing.
Server tests could not see it: they never execute a page's script.

These run Chromium against the app with its own headers, so the CSP is the
one production sends. Fixtures are ``tests/test_e2e_golden_paths.py``'s.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright not installed")

from tests.test_e2e_golden_paths import (  # noqa: E402,F401 — fixtures
    WAIT, _create_project, _project_id, actors, browser, live_server,
    search_on, team,
)
from engine import db as _db  # noqa: E402

REFUSED = "Refused to execute inline event handler"


@pytest.fixture
def admin(actors, team, search_on):
    from tests._search_seed import seed_golden

    person = actors(team["admin"]["email"]).sign_in()
    name = f"CSP {team['tag']}"
    _create_project(person, name)
    # The golden cases carry no category; the filter needs two kinds.
    extra = [{"id": f"CSP-{i}", "summary": f"Case {i}", "section": "CSP",
              "test_steps": "1. Open the page", "expected_result": "It opens",
              "priority": "Low", "category": cat}
             for i, cat in enumerate(("Negative", "Negative", "Positive"), 1)]
    seed_golden(_project_id(team["org"], name), extra_cases=extra)
    person.refused: list[str] = []
    person.page.on("console", lambda m: (
        person.refused.append(m.text) if REFUSED in m.text else None))
    return person


def _visible(page, selector: str) -> int:
    return page.evaluate(
        "(s) => [...document.querySelectorAll(s)]"
        ".filter(e => e.getBoundingClientRect().height > 0).length", selector)


class TestTheTestCasesPageReactsToClicks:
    def test_the_category_filter_hides_the_other_cards(self, admin):
        page = admin.page
        admin.go("/test-cases?lang=en")
        page.wait_for_selector(".tc-card", timeout=WAIT)
        total = _visible(page, ".tc-card")
        cats = page.evaluate(
            "() => [...document.querySelectorAll('.tc-card')]"
            ".map(c => c.dataset.category)")
        target = next(c for c in ("Negative", "Positive") if c in cats)
        page.click(f'.filter-btn[data-arg="{target}"]')
        shown = _visible(page, ".tc-card")
        assert shown == cats.count(target) < total
        page.click('.filter-btn[data-arg="all"]')
        assert _visible(page, ".tc-card") == total
        assert not admin.refused

    def test_the_traceability_tab_opens(self, admin):
        page = admin.page
        admin.go("/test-cases?lang=en")
        page.click('.tab[data-arg="traceability"]')
        assert page.evaluate(
            "() => document.getElementById('traceability')"
            ".classList.contains('active')")
        assert page.evaluate(
            "() => document.querySelector('.tab[data-arg=\"traceability\"]')"
            ".classList.contains('active')")
        assert not admin.refused


class TestDestructiveFormsAskFirst:
    def test_deleting_a_project_asks_and_cancel_keeps_it(self, admin, team):
        page = admin.page
        name = f"Keep {team['tag']}"
        _create_project(admin, name)
        pid = _project_id(team["org"], name)
        asked: list[str] = []

        def dismiss(dialog):
            asked.append(dialog.message)
            dialog.dismiss()

        page.on("dialog", dismiss)
        admin.go("/?lang=en")
        page.click(f'form[action$="/delete-project/{pid}"] button[type="submit"]')
        page.wait_for_timeout(500)
        assert asked, "no confirmation was asked before deleting a project"
        assert _db.get_project(pid) is not None, "cancel still deleted it"
        assert not admin.refused

    def test_no_rendered_element_carries_an_inline_handler(self, admin):
        """The rendered DOM, so attributes a script injects count too. A
        console check is not enough: the browser reports a refused handler
        only when it fires, and a page that merely loads stays quiet."""
        found = {}
        for path in ("/?lang=en", "/test-cases?lang=en", "/checklist?lang=en",
                     "/bug-reports?lang=en", "/test-execution?lang=en",
                     "/test-execution/runs?lang=en", "/estimation?lang=en",
                     "/guide?lang=en"):
            admin.go(path)
            admin.page.wait_for_load_state("networkidle")
            hits = admin.page.evaluate(
                "() => [...document.querySelectorAll('*')].flatMap(e =>"
                " [...e.attributes].filter(a => /^on[a-z]+$/.test(a.name))"
                ".map(a => e.tagName + '[' + a.name + ']'))")
            if hits:
                found[path] = hits[:5]
        assert not found, found
