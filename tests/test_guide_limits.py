"""The Guide's "Limits" sections state the limits the code enforces.

Users asked to know how much one generation writes and where it stops
(2026-10-21). A number in a guide drifts the day someone changes the
constant, so each documented figure is read from its constant here and
looked for in both languages' Limits sections of the Test Cases and
Checklist cards.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

GUIDE = Path(__file__).parent.parent / "templates" / "guide"


def _section(lang: str, card: str) -> str:
    html = (GUIDE / f"_sections_{lang}.html").read_text(encoding="utf-8")
    start = html.index(f'<template data-content="{card}">')
    body = html[start:html.index("</template>", start)]
    head = re.search(r"<h3>(Limits|Обмеження)[^<]*</h3>", body)
    assert head, (lang, card)
    end = body.find("<h3>", head.end())
    return re.sub(r"\s+", " ", body[head.end():end if end > 0 else None])


def _shown(n: int, lang: str) -> list[str]:
    """How the guide writes a number: 1,000 in English, 1 000 in Ukrainian."""
    if n < 1000:
        return [str(n)]
    sep = "," if lang == "en" else " "
    return [f"{n:,}".replace(",", sep), str(n)]


def _tc_limits() -> dict:
    from config import MAX_CONTENT_LENGTH
    from engine import editable, tc_author, tc_rules, wbs, wbs_baseline
    from engine import site_crawler as sc
    from engine.db import FOLDER_MAX_DEPTH, FOLDER_NAME_MAX
    from engine.imports import MAX_IMPORT_ROWS
    from engine.llm_safety import MAX_CUSTOM_PROMPT_CHARS, MAX_REQUIREMENT_LINES
    from routes.generation import MAX_CONCURRENT_GEN_JOBS
    import routes.generation as gen
    sync_budget = int(re.search(r"SYNC_GEN_BUDGET_S = (\d+)", Path(
        gen.__file__).read_text(encoding="utf-8")).group(1))
    summary_max = editable.entity("test_case").fields["summary"].max_length
    steps_max = editable.entity("test_case").fields["test_steps"].max_length
    return {
        "pages": sc.MAX_PAGES, "fetch seconds": sc.FETCH_TIMEOUT,
        "page KB": sc.MAX_BODY_KB, "page folders": wbs.MAX_PAGE_NODES,
        "depth": wbs.MAX_DEPTH, "footer pages": wbs.MAX_FOOTER_PAGES,
        "other pages": wbs.MAX_OTHER_PAGES,
        "menu entries": sc.MAX_MENU_CHILDREN,
        "region links": sc.MAX_REGION_LINKS, "forms": tc_rules.MAX_FORMS,
        "cases per form": tc_rules.MAX_CASES_PER_FORM,
        "grids": tc_rules.MAX_GRIDS,
        "grids per page": tc_rules.MAX_GRIDS_PER_PAGE,
        "cases per grid": tc_rules.MAX_CASES_PER_GRID,
        "sections": tc_rules.MAX_PAGE_SECTIONS,
        "buttons": tc_rules.MAX_PAGE_BUTTONS,
        "band": int(wbs_baseline.BAND),
        "author cases": tc_author._MAX_CASES,
        "requirement lines": MAX_REQUIREMENT_LINES,
        "instructions": MAX_CUSTOM_PROMPT_CHARS,
        "text modules": wbs.MAX_TEXT_MODULES,
        "text pages": wbs.MAX_TEXT_PAGES,
        "text cases": wbs_baseline.MAX_TEXT_LINES,
        "concurrent runs": MAX_CONCURRENT_GEN_JOBS,
        "sync seconds": sync_budget,
        "summary chars": summary_max, "steps chars": steps_max,
        "folder depth": FOLDER_MAX_DEPTH, "folder name": FOLDER_NAME_MAX,
        "import rows": MAX_IMPORT_ROWS,
        "upload MB": MAX_CONTENT_LENGTH // (1024 * 1024),
    }


def _cl_limits() -> dict:
    from engine import checklist_author, checklist_rules as cr, wbs_baseline
    from engine import site_crawler as sc
    from engine.llm_safety import MAX_REQUIREMENT_LINES
    return {
        "sections": cr.MAX_CONTENT_SECTIONS, "pages": sc.MAX_PAGES,
        "heading budget": cr.HEADING_BUDGET,
        "min headings": cr.MIN_HEADINGS_PER_SECTION,
        "max headings": cr.MAX_HEADINGS_PER_SECTION,
        "field checks": cr.MAX_FIELD_CHECKS_PER_FORM,
        "band": int(wbs_baseline.BAND),
        "author checks": checklist_author._MAX_CHECKS,
        "requirement lines": MAX_REQUIREMENT_LINES,
    }


@pytest.mark.parametrize("lang", ["en", "ua"])
@pytest.mark.parametrize("card,limits", [("test-cases", _tc_limits),
                                         ("checklist", _cl_limits)])
def test_every_documented_limit_matches_the_code(lang, card, limits):
    text = _section(lang, card)
    missing = {name: value for name, value in limits().items()
               if not any(re.search(rf"(?<![\d,]){re.escape(s)}(?![\d,])",
                                    text) for s in _shown(value, lang))}
    assert missing == {}, (lang, card, missing)


def test_the_requirement_window_is_the_one_documented():
    """"Reads up to 80 requirement lines" — both authors slice to the one
    constant the figure above is read from."""
    for module in ("engine/tc_author.py", "engine/checklist_author.py"):
        src = (Path(__file__).parent.parent / module).read_text(
            encoding="utf-8")
        assert "lines[:MAX_REQUIREMENT_LINES]" in src, module
