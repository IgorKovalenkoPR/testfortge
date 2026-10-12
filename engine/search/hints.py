"""Contextual hints — ADR 0003 §4.6 / §4.8 ``GET /api/hints``. No LLM.

Three deterministic sources, in this order:

1. **Curated tips** for the page (:data:`TIPS`): two or three short
   sentences per page and language that a QA lead would tell a new
   colleague, each with a "learn more" link into the Guide card that
   explains it.
2. **The Guide card** for the page: its title and the opening of its
   "What it is" text, read from ``templates/guide/_sections_{lang}.html``
   (the same corpus the indexer serves as ``kind='guide'``).
3. **Similar items** when the caller names a context document
   (``context=test_case:TC-012``): vector neighbours inside the same
   project — "possible duplicate" above 0.9 cosine — via
   :func:`engine.search.retrieve.similar_to`, which enforces the caller's
   project list like every other read.

Everything is a pure function of the repository and the index. Unknown
pages get the generic tips, never an error.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

#: Page aliases → canonical page id. Accepts the canonical id, the route's
#: endpoint name and its path, so a template can pass whichever it has.
PAGE_ALIASES: dict[str, str] = {
    "test_cases": "test_cases", "test_cases_page": "test_cases",
    "/test-cases": "test_cases", "test-cases": "test_cases",
    "checklist": "checklist", "checklist_page": "checklist",
    "/checklist": "checklist",
    "bug_reports": "bug_reports", "bug_reports_page": "bug_reports",
    "/bug-reports": "bug_reports", "bug-reports": "bug_reports",
    "bugs": "bug_reports",
    "executions": "executions", "test_execution_page": "executions",
    "/test-execution": "executions", "test-execution": "executions",
    "execution": "executions", "runs": "executions",
    "manual_runs_page": "executions", "test_execution_results": "executions",
    "estimation": "estimation", "estimation_page": "estimation",
    "/estimation": "estimation",
    "automation": "automation", "automation_page": "automation",
    "/automation": "automation",
    "settings": "settings", "settings_page": "settings",
    "org_settings": "settings", "/settings": "settings",
}

#: Canonical page → Guide sections (``data-content`` ids), most relevant
#: first.
PAGE_GUIDE: dict[str, tuple[str, ...]] = {
    "test_cases": ("test-cases", "workflow"),
    "checklist": ("checklist", "workflow"),
    "bug_reports": ("bug-reports", "test-execution"),
    "executions": ("test-execution", "runs"),
    "estimation": ("estimation", "workflow"),
    "automation": ("automation", "test-execution"),
    "settings": ("settings", "team"),
    "_default": ("workflow", "tips"),
}

#: Curated tips: page → lang → [(text, guide section)]. Short, imperative,
#: true of this product. English is the fallback for any other language.
TIPS: dict[str, dict[str, list[tuple[str, str]]]] = {
    "test_cases": {
        "en": [
            ("Search by id: type TC-12 to jump straight to a test case.",
             "test-cases"),
            ("Write expected results as facts (\"The dashboard opens\"), "
             "not wishes — every step must be checkable.", "test-cases"),
            ("Before adding a case, search for it: a near-duplicate is "
             "flagged as a possible duplicate here.", "tips"),
        ],
        "ua": [
            ("Пошук за ідентифікатором: введіть TC-12, щоб одразу відкрити "
             "тест-кейс.", "test-cases"),
            ("Очікуваний результат пишіть як факт («Відкривається "
             "дашборд»), а не побажання.", "test-cases"),
            ("Перед створенням кейсу пошукайте схожий — майже дублікати "
             "позначаються тут.", "tips"),
        ],
    },
    "checklist": {
        "en": [
            ("Keep one check per line; a line with \"and\" is two checks.",
             "checklist"),
            ("Use the checklist for quick passes and the test cases for "
             "anything that needs steps.", "workflow"),
        ],
        "ua": [
            ("Одна перевірка — один рядок; рядок з «і» — це дві перевірки.",
             "checklist"),
            ("Чекліст — для швидких проходів, тест-кейси — для всього, що "
             "потребує кроків.", "workflow"),
        ],
    },
    "bug_reports": {
        "en": [
            ("Search the bug title before filing: \"Only open bugs\" shows "
             "whether it is already reported.", "bug-reports"),
            ("Steps to reproduce, actual and expected result are what a "
             "developer reads first — fill all three.", "bug-reports"),
            ("Link the bug to its test case so the case shows the defect.",
             "bug-reports"),
        ],
        "ua": [
            ("Перед створенням бага пошукайте назву: «Лише відкриті баги» "
             "покаже, чи його вже зареєстровано.", "bug-reports"),
            ("Кроки відтворення, фактичний і очікуваний результат — перше, "
             "що читає розробник.", "bug-reports"),
            ("Пов'яжіть баг із тест-кейсом, щоб кейс показував дефект.",
             "bug-reports"),
        ],
    },
    "executions": {
        "en": [
            ("A failed or blocked result can be searched later — write the "
             "reason in the note.", "test-execution"),
            ("Search \"run 12\" to open a run by its number.", "runs"),
        ],
        "ua": [
            ("Невдалий або заблокований результат потім можна знайти — "
             "пишіть причину в примітці.", "test-execution"),
            ("Пошук «run 12» відкриває прогін за номером.", "runs"),
        ],
    },
    "estimation": {
        "en": [
            ("The estimate is a starting point: adjust the scope, then "
             "re-run it rather than editing the total.", "estimation"),
            ("Send the estimate to test cases to start from its scope.",
             "estimation"),
        ],
        "ua": [
            ("Оцінка — відправна точка: змініть обсяг і перерахуйте, а не "
             "правте підсумок.", "estimation"),
            ("Передайте оцінку в тест-кейси, щоб почати з її обсягу.",
             "estimation"),
        ],
    },
    "automation": {
        "en": [
            ("Automate the stable, repeated checks first; keep exploratory "
             "work manual.", "automation"),
            ("A flaky automated case is a bug in the test — fix or "
             "quarantine it.", "automation"),
        ],
        "ua": [
            ("Спершу автоматизуйте стабільні повторювані перевірки; "
             "дослідницьке тестування лишайте ручним.", "automation"),
            ("Нестабільний автотест — це баг у тесті: виправте або "
             "ізолюйте його.", "automation"),
        ],
    },
    "settings": {
        "en": [
            ("Only admins change team settings; members see what applies "
             "to them.", "settings"),
            ("Invite colleagues from Team so everyone works in the same "
             "projects.", "team"),
        ],
        "ua": [
            ("Налаштування команди змінюють лише адміністратори.",
             "settings"),
            ("Запросіть колег у розділі «Команда», щоб усі працювали в "
             "тих самих проєктах.", "team"),
        ],
    },
    "_default": {
        "en": [
            ("Search covers everything in your projects: test cases, "
             "checklists, bugs and runs.", "workflow"),
        ],
        "ua": [
            ("Пошук охоплює все у ваших проєктах: тест-кейси, чеклісти, "
             "баги та прогони.", "workflow"),
        ],
    },
}

GUIDE_DIR = Path(__file__).resolve().parents[2] / "templates" / "guide"
_GUIDE_CACHE: dict[str, dict[str, dict[str, str]]] = {}
_GUIDE_LOCK = threading.Lock()
_EXCERPT_CHARS = 220
_HEADINGS = {"what it is", "what this card is", "що це", "що це таке",
             "що це за картка"}


def canonical_page(page: str | None) -> str:
    key = (page or "").strip()
    return PAGE_ALIASES.get(key) or PAGE_ALIASES.get(key.lower()) \
        or "_default"


def _lang(lang: str | None) -> str:
    lang = (lang or "en").lower()
    return "ua" if lang in ("ua", "uk") else "en"


def guide_cards(lang: str) -> dict[str, dict[str, str]]:
    """``{section: {"title", "excerpt", "url"}}`` for one Guide language,
    parsed once per process."""
    lang = _lang(lang)
    with _GUIDE_LOCK:
        hit = _GUIDE_CACHE.get(lang)
        if hit is not None:
            return hit
    from engine.search.indexer import _TEMPLATE, _html_text
    out: dict[str, dict[str, str]] = {}
    path = GUIDE_DIR / f"_sections_{lang}.html"
    if not path.is_file():
        path = GUIDE_DIR / "_sections_en.html"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    try:
        from engine.i18n import get_lang
        table = get_lang(lang) or {}
    except Exception:   # noqa: BLE001 — titles are a nicety
        table = {}
    for section, fragment in _TEMPLATE.findall(raw):
        lines = [ln for ln in _html_text(fragment).splitlines() if ln]
        if lines and lines[0].strip().lower() in _HEADINGS:
            lines = lines[1:]
        text = re.sub(r"\s+", " ", " ".join(lines)).strip()
        if len(text) > _EXCERPT_CHARS:
            cut = text.rfind(" ", 0, _EXCERPT_CHARS)
            text = text[: cut if cut > 80 else _EXCERPT_CHARS].rstrip(
                ",;:") + "…"
        key = f"guide_card_{section.replace('-', '_')}_title"
        title = str(table.get(key) or section.replace("-", " ").title())
        out[section] = {"title": title, "excerpt": text,
                        "url": f"/guide#{section}"}
    with _GUIDE_LOCK:
        _GUIDE_CACHE[lang] = out
    return out


def _learn_more(section: str, cards: dict) -> dict:
    card = cards.get(section) or {}
    return {"title": card.get("title") or section.replace("-", " ").title(),
            "url": card.get("url") or f"/guide#{section}"}


def hints_for(page: str | None, *, lang: str | None = "en",
              project_id: str | None = None, context: str | None = None,
              allowed: list[str] | None = None) -> dict[str, Any]:
    """The hints for *page*. With *context* ``"kind:REF"`` and a
    *project_id* that is in *allowed*, similar items are added."""
    lang = _lang(lang)
    canon = canonical_page(page)
    cards = guide_cards(lang)
    tips_table = TIPS.get(canon) or TIPS["_default"]
    tips = tips_table.get(lang) or tips_table["en"]
    hints: list[dict] = []
    for text, section in tips:
        hints.append({"type": "tip", "text": text,
                      "learn_more": _learn_more(section, cards)})
    for section in PAGE_GUIDE.get(canon, PAGE_GUIDE["_default"])[:1]:
        card = cards.get(section)
        if card:
            hints.append({"type": "guide", "topic": card["title"],
                          "excerpt": card["excerpt"], "url": card["url"]})
    if context and project_id and allowed is not None:
        kind, _, ref = context.partition(":")
        if kind and ref:
            from engine.search.retrieve import similar_to
            for item in similar_to(project_id, kind, ref, allowed=allowed):
                label = ("Possible duplicate" if item["similarity"] >= 0.9
                         else "Similar")
                if item["kind"] == "bug" and kind != "bug":
                    htype, label = "related_bug", "Related bug"
                else:
                    htype = "similar"
                hints.append({"type": htype, "label": label, **item})
    return {"page": canon, "lang": lang, "hints": hints}


__all__ = ["hints_for", "canonical_page", "guide_cards", "TIPS",
           "PAGE_ALIASES", "PAGE_GUIDE"]
