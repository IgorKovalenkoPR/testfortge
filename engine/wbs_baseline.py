"""TestFortge — even coverage across the WBS (owner rule, 2026-10-21).

"Unless the user gave specific generation instructions, every module,
submodule and page in the WBS gets covered evenly."

Two halves:

* **Baseline** — every leaf of the tree gets a fixed set of cases whether or
  not the crawler reached it: its navigation entry leads to it, it opens
  with its title and heading, it loads without console or HTTP errors, its
  links and calls to action work. Built from what the WBS knows (the label,
  the URL, the menu it sits in); a crawled page adds its content and form
  cases on top. Measured before this on testfort.com: 28 of 73 leaves had
  no test case at all — the Resources pages, Contact us, the footer pages —
  because the 50-page crawl never reached them.
* **Balance** — per-leaf counts stay within a band: at most ``BAND`` times
  the median. A leaf holding real form or grid coverage may exceed it (a
  form with twelve controls is twelve controls' worth of cases); anything
  else above the band is trimmed, generic rows first.

Explicit scope wins (:func:`user_scope`): the user's own instructions say
where coverage goes, and neither half second-guesses them.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from engine import wbs as _wbs

#: Per-leaf ceiling, as a multiple of the median leaf.
BAND = 3.0
#: Text mode: requirement lines turned into baseline cases per leaf.
MAX_TEXT_LINES = 4

_NARROWING = re.compile(
    r"\b(only|just|exclusively|solely|limit(?:ed)? to|focus(?:ed)? on|"
    r"тільки|лише|виключно|зосередь\w*|сфокусуй\w*|только|лишь)\b", re.I)


@dataclass
class Scope:
    """What the user's instructions said about where coverage goes."""
    #: The user narrowed the pack itself ("positive only", "security only")
    #: without naming parts of the product: no baseline, no balancing.
    reshaped: bool = False
    #: Nodes the instructions name. With ``narrowed``: the baseline goes
    #: only inside them. Either way they are exempt from the band.
    named: set = field(default_factory=set)
    narrowed: bool = False

    @property
    def free(self) -> bool:
        return not self.reshaped and not self.named


def user_scope(custom_prompt: str, raw_lines, wbs) -> Scope:
    """Read the generation instructions against the tree.

    The instruction text is the Additional Instructions field plus any input
    line the requirement splitter classifies as an instruction (the same
    ``is_instruction`` the generators use to drop them from requirements).
    """
    texts = [custom_prompt or ""]
    try:
        from engine.file_parser import is_instruction
        texts += [ln for ln in raw_lines or [] if ln and is_instruction(ln)]
    except Exception:  # pragma: no cover — defensive
        pass
    text = " ".join(t for t in texts if t).strip()
    if not text:
        return Scope()
    folded = text.casefold()
    named = set()
    if wbs is not None:
        for node in wbs.nodes.values():
            name = node.name.casefold()
            if node.kind == "crosscutting" or len(name) < 3:
                continue
            if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", folded):
                named.add(node.id)
    narrowed = bool(_NARROWING.search(text))
    return Scope(reshaped=narrowed and not named, named=named,
                 narrowed=narrowed)


def _in_scope(wbs, node_id: str, scope: Scope) -> bool:
    if scope.reshaped:
        return False
    if not (scope.narrowed and scope.named):
        return True
    cursor = node_id
    while cursor:
        if cursor in scope.named:
            return True
        cursor = wbs.nodes[cursor].parent if cursor in wbs.nodes else None
    return False


def leaves(wbs) -> list:
    """The leaves a tester walks: pages and childless modules, not the
    Cross-cutting topics or the chrome components."""
    parents = {n.parent for n in wbs.nodes.values() if n.parent}
    return [n for n in wbs.ordered()
            if n.id not in parents
            and n.kind not in ("crosscutting", "component", "other", "area")]


# ── baseline ─────────────────────────────────────────────────────────

def _where(wbs, node) -> tuple[str, str]:
    """``(step to reach the entry, where it sits)`` for a page's nav entry."""
    parent = wbs.nodes.get(node.parent) if node.parent else None
    if node.id == _wbs.HOME:
        return ("Click on the logo in the Header", "Header logo")
    if parent is None:
        return (f'Click on the "{node.name}" link in the Header', "Header")
    if parent.id == _wbs.FOOTER:
        return ("Scroll down to the Footer", "Footer")
    top = parent
    while top.parent and top.parent in wbs.nodes:
        top = wbs.nodes[top.parent]
    return (f'Open the "{top.name}" menu in the Header', f'"{top.name}" menu')


def _site_cases(wbs, node, base: str, crawled: bool) -> list[dict]:
    url = (base.rstrip("/") + node.url) if base and node.url else node.url
    reach, where = _where(wbs, node)
    page = node.name if node.name.casefold().endswith("page")         else f"{node.name} page"
    home = f"{base} is open" if base else "The site is open"
    opened = f"{url} is open" if url else f"The {page} is opened"
    entry = "the logo" if node.id == _wbs.HOME else f'the "{node.name}" entry'
    cases = [{
        "summary": f"Verify that {entry} in the {where.replace(' logo', '')} "
                   f"opens the {page}",
        "preconditions": home,
        "steps": [reach] if node.id == _wbs.HOME
        else [reach, f'Click on the "{node.name}" entry'],
        "expected": [f"The {page} is opened",
                     f"The address bar shows {url}" if url else
                     f"The {page} heading names {node.name}"],
        "priority": "High", "type": "Functional", "category": "Positive",
    }]
    if not crawled:
        cases.append({
            "summary": f"Verify that the {page} opens with its title and "
                       f"main heading",
            "preconditions": opened,
            "steps": ["Look at the browser tab title",
                      "Look at the main heading at the top of the page"],
            "expected": [f"The browser tab title names {node.name}",
                         f"The main heading names {node.name}"],
            "priority": "Medium", "type": "UI/UX", "category": "Positive",
        })
    cases += [{
        "summary": f"Verify that the {page} loads without console errors "
                   f"or failed requests",
        "preconditions": opened,
        "steps": ["Open the browser developer tools on the Console and "
                  "Network tabs", "Reload the page"],
        "expected": ["No JavaScript error is logged in the Console",
                     "No request on the page fails with a 4xx or 5xx status"],
        "priority": "Medium", "type": "Functional", "category": "Negative",
    }, {
        "summary": f"Verify that the links and calls to action on the "
                   f"{page} open working pages",
        "preconditions": opened,
        "steps": ["Click on every link and call-to-action button in the "
                  "page content, one by one",
                  "Return to the page after each one"],
        "expected": ["Every link opens a page that loads with HTTP 200",
                     "No link leads to a 404 page"],
        "priority": "Medium", "type": "Functional", "category": "Positive",
    }]
    return cases


def _text_cases(wbs, node) -> list[dict]:
    lines = [line for line, nid in wbs.by_line.items() if nid == node.id]
    lines = [l for l in lines if len(l) >= 12][:MAX_TEXT_LINES]
    if not lines:
        return [{
            "summary": f"Verify that {node.name} works as the document "
                       f"describes it",
            "preconditions": "The product is deployed and reachable",
            "steps": [f"Open {node.name}",
                      "Exercise every behaviour the document lists for it"],
            "expected": [f"{node.name} behaves as the document describes"],
            "priority": "Medium", "type": "Functional",
            "category": "Positive",
        }]
    out = []
    for line in lines:
        quoted = line[:110].rstrip(" .;,")
        out.append({
            "summary": f'Verify that {node.name} meets the requirement '
                       f'"{quoted}"',
            "preconditions": "The product is deployed and reachable",
            "steps": [f"Open {node.name}",
                      "Perform the behaviour the requirement describes"],
            "expected": [f'The requirement "{quoted}" is met',
                         "No error is displayed"],
            "priority": "Medium", "type": "Functional",
            "category": "Positive",
        })
    return out


def baseline(wbs, pages: list[dict], scope: Scope | None = None
             ) -> tuple[list[dict], list[dict]]:
    """``(test-case dicts, checklist dicts)`` — the baseline for every leaf
    in scope, hinted to its node so :func:`engine.wbs.stamp` files it."""
    scope = scope or Scope()
    crawled = {_wbs.norm_path(str(p.get("url") or ""), wbs.base)
               for p in pages or [] if isinstance(p, dict)}
    tcs: list[dict] = []
    checks: list[dict] = []
    for node in leaves(wbs):
        if not _in_scope(wbs, node.id, scope):
            continue
        if wbs.mode == "text":
            specs = _text_cases(wbs, node)
        elif node.url:
            specs = _site_cases(wbs, node, wbs.base, node.url in crawled)
        else:
            continue
        for spec in specs:
            n = len(tcs) + 1
            tcs.append({
                "id": f"BL1_{n:03d}", "section": f"{node.name} page"
                if wbs.mode != "text" else node.name,
                "section_num": 1,
                "summary": spec["summary"][:300],
                "preconditions": spec["preconditions"],
                "test_steps": "\n".join(f"{i}. {s}" for i, s in
                                        enumerate(spec["steps"], 1)),
                "test_data": "",
                "expected_result": "\n".join(
                    f"{i}. {s}" for i, s in enumerate(spec["expected"], 1))
                if len(spec["expected"]) > 1 else spec["expected"][0],
                "issues": "", "comment": "", "user_story_id": "",
                "category": spec["category"], "priority": spec["priority"],
                "status": "Unchecked", "testing_type": spec["type"],
                "url_pattern": "", "trigger": "manual",
                "wbs_hint": {"node": node.id, "surface": "baseline"},
            })
            m = len(checks) + 1
            checks.append({
                "id": f"BL_{m:03d}", "section": f"{node.name} page"
                if wbs.mode != "text" else node.name,
                "objective": spec["summary"][:300],
                "category": spec["category"], "priority": spec["priority"],
                "status": "Unchecked", "testing_type": spec["type"],
                "item_num": "", "depth": 2,
                "wbs_hint": {"node": node.id, "surface": "baseline"},
            })
    return tcs, checks


# ── balance ──────────────────────────────────────────────────────────

_PRIORITY = {"high": 0, "medium": 1, "low": 2}


def _keep_order(d: dict) -> tuple:
    """What a trimmed node keeps first: its baseline, then the generator
    that read the page (site-aware), then the template stream; within a
    stream, priority."""
    surface = d.get("_wbs_surface") or ""
    stream = 0 if surface == "baseline" else (
        1 if str(d.get("id") or "").startswith(("SA", "CNT", "HDR", "FTR"))
        else 2)
    return (stream, _PRIORITY.get(str(d.get("priority") or "").casefold(), 1))


def balance(dicts: list[dict], scope: Scope | None = None) -> dict:
    """Trim every leaf above ``BAND`` x the median, in place.

    Only nodes ``wbs.stamp`` recorded (``_wbs_node``) are counted;
    Cross-cutting is left alone (it is gated on its share, not its size).
    A node holding form or grid cases is exempt, and so is a node the
    user's instructions named. Returns ``{"median", "cap", "trimmed"}``.
    """
    scope = scope or Scope()
    report = {"median": 0, "cap": 0, "trimmed": 0}
    if scope.reshaped:
        return report
    by_node: dict[str, list[int]] = {}
    exempt: set[str] = set(scope.named)
    for index, d in enumerate(dicts):
        node = d.get("_wbs_node")
        if not node or node.startswith(_wbs.XC):
            continue
        by_node.setdefault(node, []).append(index)
        if d.get("_wbs_surface") in ("form", "grid"):
            exempt.add(node)
    if len(by_node) < 3:
        return report
    median = statistics.median(len(v) for v in by_node.values())
    cap = max(int(BAND * median), 1)
    drop: set[int] = set()
    for node, indexes in by_node.items():
        if node in exempt or len(indexes) <= cap:
            continue
        ranked = sorted(indexes, key=lambda i: (_keep_order(dicts[i]), i))
        drop.update(ranked[cap:])
    if drop:
        dicts[:] = [d for i, d in enumerate(dicts) if i not in drop]
    report.update(median=median, cap=cap, trimmed=len(drop))
    return report


def clear_marks(dicts: list[dict]) -> None:
    for d in dicts:
        d.pop("_wbs_node", None)
        d.pop("_wbs_surface", None)


__all__ = ["Scope", "user_scope", "leaves", "baseline", "balance",
           "clear_marks", "BAND"]
