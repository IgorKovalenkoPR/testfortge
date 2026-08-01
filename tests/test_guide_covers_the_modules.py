"""Every module in the sidebar needs a Guide entry.

Why this file exists: the Automation QA module shipped, took its place
in the sidebar, and was never given a Guide card. On prod the module was
one click away in the navigation and entirely undocumented — a reader
who opened the Guide to learn about it found Dashboard, Estimation, Test
Cases, Checklist, Test Execution, Bug Reports, Tedgie, Projects and Pro
tips, with the new module simply absent.

Nothing failed. Both templates were valid, every route worked, and the
omission is only visible by comparing two files nobody compares.

The card→panel half of the check matters for a different reason: a card
whose ``data-section`` has no matching ``<template data-content>`` opens
an empty modal, because ``openSection`` returns silently when the lookup
misses.
"""
from __future__ import annotations

import pathlib
import re

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE_HTML = REPO_ROOT / "templates" / "base.html"
GUIDE_HTML = REPO_ROOT / "templates" / "guide.html"

#: Sidebar endpoint → the ``data-section`` its Guide card must use.
#: ``guide_page`` is deliberately absent: the Guide does not document
#: itself. Add a row here when a module joins the sidebar — that is the
#: point of the test.
MODULE_SECTIONS = {
    "index": "dashboard",
    "estimation_page": "estimation",
    "test_cases_page": "test-cases",
    "checklist_page": "checklist",
    "test_execution_page": "test-execution",
    "automation_page": "automation",
    "bug_reports_page": "bug-reports",
}


@pytest.fixture(scope="module")
def guide_source() -> str:
    return GUIDE_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def cards(guide_source) -> set[str]:
    return set(re.findall(r'class="guide-card"\s+data-section="([^"]+)"',
                          guide_source))


@pytest.fixture(scope="module")
def panels(guide_source) -> set[str]:
    return set(re.findall(r'<template\s+data-content="([^"]+)"',
                          guide_source))


def test_the_sidebar_is_readable() -> None:
    """Guard the guard — a renamed template would make this vacuous."""
    nav = BASE_HTML.read_text(encoding="utf-8")
    assert "sidebar" in nav
    assert "automation_page" in nav


@pytest.mark.parametrize("endpoint,section", sorted(MODULE_SECTIONS.items()))
def test_every_sidebar_module_has_a_guide_card(
    endpoint: str, section: str, cards: set[str]
) -> None:
    nav = BASE_HTML.read_text(encoding="utf-8")
    if f"url_for('{endpoint}')" not in nav:
        pytest.skip(f"{endpoint} is no longer in the sidebar")
    assert section in cards, (
        f"{endpoint} is in the sidebar but has no Guide card "
        f"(data-section=\"{section}\"). A module nobody can read about "
        f"is a module nobody uses correctly."
    )


def test_every_card_opens_something(cards: set[str], panels: set[str]) -> None:
    orphans = sorted(cards - panels)
    assert not orphans, (
        f"guide-card(s) with no matching <template data-content>: "
        f"{orphans}. openSection() returns silently, so the card looks "
        f"clickable and opens an empty modal."
    )


def test_no_panel_is_unreachable(cards: set[str], panels: set[str]) -> None:
    unreachable = sorted(panels - cards)
    assert not unreachable, (
        f"<template data-content> with no card to open it: {unreachable}. "
        f"Reachable only by a /guide#hash deep link."
    )
