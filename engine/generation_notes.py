"""Which limits one generation run hit — said to the user, not just logged.

Users asked to know where generation stops (2026-10-21). The Guide's
"Limits" sections list every limit; this is the per-run half: each
generator records what a limit cut (the crawler, the WBS, the form and
heading budgets, the authors, the balancer), and the run turns those
counts into notes the page flashes after the result.

A note is ``{"key": <i18n key>, "params": {...}, "for": "tc"|"cl"|"both"}``
— data, because the workers run on a queue thread with no request and so
no language; :func:`render` translates them when the page shows them.
``for`` matters because one site-aware run builds both packs: the form
budget only shapes test cases, the heading budget only the checklist.
"""
from __future__ import annotations

TC, CL, BOTH = "tc", "cl", "both"


def note(key: str, for_: str = BOTH, **params) -> dict:
    return {"key": key, "params": params, "for": for_}


def crawl(analysis) -> list[dict]:
    """The page budget: linked pages the crawl never opened."""
    from engine import site_crawler as sc
    read, left = sc.unread_links(analysis)
    if not left:
        return []
    return [note("gen_limit_crawl", read=read, total=read + left, left=left,
                 limit=sc.MAX_PAGES)]


def wbs(tree) -> list[dict]:
    """The structure budgets: pages that got no folder of their own."""
    from engine import wbs as _wbs
    cut = getattr(tree, "cut", None) or {}
    out = []
    for budget, key, limit in (
            ("page_nodes", "gen_limit_wbs_pages", _wbs.MAX_PAGE_NODES),
            ("footer", "gen_limit_wbs_footer", _wbs.MAX_FOOTER_PAGES),
            ("other", "gen_limit_wbs_other", _wbs.MAX_OTHER_PAGES)):
        if cut.get(budget):
            out.append(note(key, n=len(cut[budget]), limit=limit))
    return out


def artifacts(arts, for_: str) -> list[dict]:
    """What an author's input recorded while the run used it."""
    from engine import tc_rules
    from engine.llm_safety import MAX_REQUIREMENT_LINES
    limits = getattr(arts, "limits", None) or {}
    out = []
    if limits.get("forms_skipped"):
        out.append(note("gen_limit_forms", TC, n=limits["forms_skipped"],
                        limit=tc_rules.MAX_FORMS))
    if limits.get("requirements"):
        out.append(note("gen_limit_requirements", for_,
                        total=limits["requirements"],
                        limit=MAX_REQUIREMENT_LINES))
    return out


def headings(checklist) -> list[dict]:
    """The checklist's shared heading budget."""
    from engine import checklist_rules as clr
    cut = getattr(checklist, "heading_cut", None) or {}
    if not cut:
        return []
    return [note("gen_limit_headings", CL, budget=clr.HEADING_BUDGET, **cut)]


def balance(report: dict, for_: str) -> list[dict]:
    """The evenness trim (engine.wbs_baseline.balance)."""
    from engine import wbs_baseline as bl
    if not (report or {}).get("trimmed"):
        return []
    return [note("gen_limit_balance", for_, n=report["trimmed"],
                 cap=report["cap"], median=f'{float(report["median"]):g}',
                 band=int(bl.BAND))]


def prompt(original_chars: int) -> list[dict]:
    """Additional Instructions cut to ``MAX_CUSTOM_PROMPT_CHARS``."""
    from engine.llm_safety import MAX_CUSTOM_PROMPT_CHARS
    if original_chars <= MAX_CUSTOM_PROMPT_CHARS:
        return []
    return [note("gen_limit_prompt", total=original_chars,
                 limit=MAX_CUSTOM_PROMPT_CHARS)]


def merge(into: list[dict], notes) -> list[dict]:
    """Append ``notes``, one per key and audience — the sync and async
    halves of a run may both see the same cut."""
    seen = {(n["key"], n["for"]) for n in into}
    for n in notes or []:
        if isinstance(n, dict) and (n.get("key"), n.get("for")) not in seen:
            into.append(n)
            seen.add((n["key"], n["for"]))
    return into


def text(n: dict, kind: str, t: dict) -> str:
    """One note in the user's language, pointing at the Guide's Limits
    section for the page it shows on; "" when it cannot be rendered."""
    template = t.get(n.get("key") or "")
    if not template:
        return ""
    try:
        body = template % (n.get("params") or {})
    except (KeyError, TypeError, ValueError):
        return ""
    guide = t.get("gen_limit_see_guide_tc" if kind == TC
                  else "gen_limit_see_guide_cl", "")
    return f"{body} {guide}".strip()


def for_page(notes, kind: str) -> list[dict]:
    """The notes a ``kind`` ("tc" / "cl") page shows."""
    return [n for n in notes or []
            if isinstance(n, dict) and n.get("for") in (kind, BOTH)]


def render(notes, kind: str, t: dict) -> list[str]:
    """Every note a ``kind`` page shows, rendered."""
    return [m for m in (text(n, kind, t) for n in for_page(notes, kind)) if m]
