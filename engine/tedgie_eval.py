"""
TestForTge — scoring for Tedgie's mentoring answers.

This module owns the *contract*, not the judgement: it loads
``qa_knowledge/eval/golden_set.yaml`` and decides whether a given answer
satisfies a given item. The CI harness (E6.7) and the local runner both
call it, so a change to what "passing" means happens in one place.

Three decisions in here are load-bearing, and each one is a deliberate
rejection of an easier design:

1. **A requirement is a list of alternatives, satisfied by any one.**
   Grading on an exact phrase would fail a correct answer for wording it
   differently, which turns the eval into a prompt-regression test — it
   would go red every time the persona is reworded and stay green while
   the knowledge rots.

2. **A single ``avoid`` hit fails the item outright**, regardless of how
   many requirements were met. Wrong advice delivered fluently is worse
   than no answer, because the asker acts on it. A weighted score would
   let a confidently wrong answer pass on volume.

3. **Route is scored separately from content.** An item can be answered
   correctly by the LLM while the pack that owns the question is still
   unreachable. Folding route into the pass/fail would hide exactly the
   defect the golden set was written to expose — the severity recommender
   that already exists and that ``detect_topic`` shadows.

Substring matching is intentional: requirements are written as stems
("localis", "priorit", "відтвор") so one alternative covers inflections
in both languages without a stemmer per locale.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml


DEFAULT_PATH = Path(__file__).resolve().parent / "qa_knowledge" / "eval" / "golden_set.yaml"

#: Packs a golden-set item may declare. Kept as a closed set so a typo
#: in a new item is a load error rather than a silently orphaned pack
#: that nobody ever scores.
PACKS: tuple[str, ...] = (
    "severity_priority",
    "layer",
    "naming",
    "process",
    "theory",
    "product",
)

#: Route namespaces. ``pack:`` is a new mentoring pack, ``istqb:`` the
#: existing syllabus knowledge, ``fast_path:`` the rule layer, and ``ai``
#: means "no deterministic owner, the model may answer freely" — used for
#: nothing yet, and deliberately available so that an item nobody owns
#: can be recorded honestly instead of being assigned a false owner.
ROUTE_NAMESPACES: tuple[str, ...] = ("pack", "istqb", "fast_path", "ai")


@dataclass(frozen=True)
class Item:
    """One golden-set question and the contract its answer must meet."""

    id: str
    pack: str
    lang: str
    question: str
    route: str
    require: tuple[tuple[str, ...], ...]
    avoid: tuple[str, ...] = ()
    why: str = ""

    @property
    def requirement_count(self) -> int:
        return len(self.require)


@dataclass
class ItemResult:
    """The outcome of grading one answer."""

    item: Item
    answer: str
    route: str | None = None
    met: tuple[int, ...] = ()          # indices of satisfied requirements
    missed: tuple[int, ...] = ()       # indices of unsatisfied ones
    violations: tuple[str, ...] = ()   # `avoid` strings that appeared

    @property
    def route_ok(self) -> bool:
        """True when the answer came from the path that owns the question.

        A missing route is *unknown*, not wrong: callers that cannot
        observe routing (a plain text answer pasted in by hand) should not
        be told their routing is broken.
        """
        return self.route is None or self.route == self.item.route

    @property
    def content_ok(self) -> bool:
        return not self.missed and not self.violations

    @property
    def passed(self) -> bool:
        return self.content_ok

    @property
    def score(self) -> float:
        """Fraction of requirements met, zeroed by any violation.

        Reported alongside pass/fail so a regression can be seen shrinking
        before it crosses the line — "3 of 4 requirements" is actionable,
        "failed" is not.
        """
        if self.violations:
            return 0.0
        if not self.item.require:
            return 1.0
        return len(self.met) / len(self.item.require)

    def explain(self) -> str:
        """A one-line reason, for CI output and the local runner."""
        if self.violations:
            return f"said {', '.join(repr(v) for v in self.violations)}"
        if self.missed:
            missing = [" / ".join(self.item.require[i]) for i in self.missed]
            return "missing " + "; ".join(f"[{m}]" for m in missing)
        if not self.route_ok:
            return f"answered by {self.route}, expected {self.item.route}"
        return "ok"


@dataclass
class Report:
    """Aggregate of a whole run, with the breakdown CI needs to be useful."""

    results: list[ItemResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def route_rate(self) -> float:
        scored = [r for r in self.results if r.route is not None]
        if not scored:
            return 0.0
        return sum(1 for r in scored if r.route_ok) / len(scored)

    @property
    def violations(self) -> list[ItemResult]:
        """Items that said something they were told never to say.

        Surfaced as its own list because these fail differently: a missing
        requirement is an incomplete answer, a violation is a harmful one,
        and a CI summary that mixes them buries the ones worth waking up
        for.
        """
        return [r for r in self.results if r.violations]

    def by_pack(self) -> dict[str, tuple[int, int]]:
        """pack -> (passed, total). Per-pack, because a whole-set average
        hides one dead pack behind five healthy ones."""
        out: dict[str, list[int]] = {}
        for r in self.results:
            slot = out.setdefault(r.item.pack, [0, 0])
            slot[1] += 1
            if r.passed:
                slot[0] += 1
        return {k: (v[0], v[1]) for k, v in out.items()}

    def by_lang(self) -> dict[str, tuple[int, int]]:
        out: dict[str, list[int]] = {}
        for r in self.results:
            slot = out.setdefault(r.item.lang, [0, 0])
            slot[1] += 1
            if r.passed:
                slot[0] += 1
        return {k: (v[0], v[1]) for k, v in out.items()}

    def failures(self) -> list[ItemResult]:
        return [r for r in self.results if not r.passed]


# ── Loading ───────────────────────────────────────────────────────────

class GoldenSetError(ValueError):
    """The golden set itself is malformed.

    Raised eagerly at load time, with the offending item id, because a
    silently skipped item is a test that stops measuring without ever
    going red.
    """


def _as_alternatives(raw: Any, item_id: str, index: int) -> tuple[str, ...]:
    if isinstance(raw, str):
        # A bare string is a single-alternative requirement. Allowed
        # because forcing `- [x]` for one-word requirements makes the
        # YAML noisier than it is precise.
        alts: Sequence[Any] = [raw]
    elif isinstance(raw, (list, tuple)):
        alts = raw
    else:
        raise GoldenSetError(
            f"{item_id}: requirement {index} is {type(raw).__name__}, "
            "expected a string or a list of alternatives"
        )
    out = tuple(str(a).strip().lower() for a in alts if str(a).strip())
    if not out:
        raise GoldenSetError(f"{item_id}: requirement {index} is empty")
    return out


def _item_from(raw: dict[str, Any]) -> Item:
    item_id = str(raw.get("id") or "").strip()
    if not item_id:
        raise GoldenSetError("an item has no id")
    pack = str(raw.get("pack") or "").strip()
    if pack not in PACKS:
        raise GoldenSetError(
            f"{item_id}: unknown pack {pack!r}; known packs are {', '.join(PACKS)}"
        )
    question = str(raw.get("q") or "").strip()
    if not question:
        raise GoldenSetError(f"{item_id}: no question")
    route = str(raw.get("route") or "").strip()
    namespace = route.split(":", 1)[0]
    if namespace not in ROUTE_NAMESPACES:
        raise GoldenSetError(
            f"{item_id}: route {route!r} is not in a known namespace "
            f"({', '.join(ROUTE_NAMESPACES)})"
        )
    require_raw = raw.get("require") or []
    if not isinstance(require_raw, (list, tuple)):
        raise GoldenSetError(f"{item_id}: `require` must be a list")
    require = tuple(
        _as_alternatives(r, item_id, i) for i, r in enumerate(require_raw)
    )
    if not require:
        raise GoldenSetError(f"{item_id}: no requirements — it would pass on anything")
    avoid_raw = raw.get("avoid") or []
    if isinstance(avoid_raw, str):
        avoid_raw = [avoid_raw]
    avoid = tuple(str(a).strip().lower() for a in avoid_raw if str(a).strip())
    return Item(
        id=item_id,
        pack=pack,
        lang=str(raw.get("lang") or "en").strip().lower(),
        question=question,
        route=route,
        require=require,
        avoid=avoid,
        why=str(raw.get("why") or "").strip(),
    )


def load(path: Path | str | None = None) -> tuple[Item, ...]:
    """Load and validate the golden set."""
    p = Path(path) if path else DEFAULT_PATH
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    raw_items = data.get("items") or []
    if not isinstance(raw_items, list):
        raise GoldenSetError("`items` must be a list")
    items = tuple(_item_from(r) for r in raw_items)
    seen: dict[str, int] = {}
    for i, it in enumerate(items):
        if it.id in seen:
            raise GoldenSetError(
                f"duplicate id {it.id!r} at positions {seen[it.id]} and {i}"
            )
        seen[it.id] = i
    return items


# ── Scoring ───────────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    """Lower-case and collapse whitespace.

    Markdown emphasis is stripped so a requirement of "minor" still
    matches an answer that renders it as ``**Minor**`` — the rule layer
    bolds its recommendations, and grading that as a miss would measure
    formatting, not knowledge.
    """
    low = (text or "").lower()
    low = low.replace("*", "").replace("_", " ").replace("`", "")
    return re.sub(r"\s+", " ", low)


def score(item: Item, answer: str, route: str | None = None) -> ItemResult:
    """Grade one answer against one item."""
    hay = _normalise(answer)
    met: list[int] = []
    missed: list[int] = []
    for i, alternatives in enumerate(item.require):
        if any(alt in hay for alt in alternatives):
            met.append(i)
        else:
            missed.append(i)
    violations = tuple(a for a in item.avoid if a in hay)
    return ItemResult(
        item=item,
        answer=answer,
        route=route,
        met=tuple(met),
        missed=tuple(missed),
        violations=violations,
    )


def score_all(answers: Iterable[tuple[Item, str, str | None]]) -> Report:
    """Grade a whole run. Each entry is (item, answer, route_or_None)."""
    return Report(results=[score(i, a, r) for i, a, r in answers])


# ── Running the golden set against the rule layer ─────────────────────

#: Chat intents mapped onto golden-set route names. The rule layer's
#: intents predate the golden set and are not going to be renamed for it:
#: routes/chat.py and the front-end both read them, and several are
#: asserted by existing tests.
_INTENT_ROUTES: dict[str, str] = {
    "greeting": "fast_path:greeting",
    "gratitude": "fast_path:gratitude",
    "help_menu": "fast_path:default_help",
    "bug_form": "fast_path:bug_form",
    "clarify_requirement": "fast_path:clarify",
    # chatbot_guide's severity decision tree. It composes over cue
    # combinations no single pack entry covers, so it legitimately answers
    # a subset of the severity questions and is counted as that pack.
    "severity_recommendation": "pack:severity_priority",
}


def route_of(intent: str | None) -> str | None:
    """Normalise a ChatReply intent to a golden-set route name."""
    if not intent:
        return None
    if intent.startswith(("pack:", "istqb:", "fast_path:")):
        return intent
    if intent in _INTENT_ROUTES:
        return _INTENT_ROUTES[intent]
    # The glossary path tags its intent with the term it matched
    # ("istqb_glossary:failure"). The term is useful in the transcript and
    # noise in a route, so it collapses to one owner.
    if intent.startswith("istqb_glossary"):
        return "istqb:glossary"
    if intent.startswith("troubleshoot_"):
        return "fast_path:troubleshoot"
    if intent.startswith("help_") or intent.startswith("module_"):
        return "fast_path:module_help"
    if intent.startswith("diag_") or intent.startswith("bug_summary"):
        return "fast_path:troubleshoot"
    return f"fast_path:{intent}"


def run_rule_layer(items: Iterable[Item] | None = None) -> Report:
    """Score the golden set against the deterministic chain only.

    No LLM, no network, no API key — which is what makes this runnable in
    CI on every push.

    Both halves of the deterministic chain are exercised, in the order
    ``respond()`` uses them when no API key is present: ``try_fast_path``
    first, then ``rule_based_fallback``. Measuring only the fast path was a
    real mistake in the first version of this harness — eight of the twelve
    product-help items failed, and the reason was not that Tedgie cannot
    answer them but that module help lives in the fallback. A harness that
    measures half the chain reports defects that are not there, which is
    worse than measuring nothing.
    """
    from engine import chatbot  # imported here so loading this module stays cheap

    graded: list[tuple[Item, str, str | None]] = []
    for item in items if items is not None else load():
        lang = "ua" if item.lang == "uk" else item.lang
        try:
            reply = chatbot.try_fast_path(item.question, lang)
            if reply is None:
                reply = chatbot.rule_based_fallback(item.question, lang)
        except Exception:  # pragma: no cover — a crash is a failure, not an error
            reply = None
        text = reply.text if reply else ""
        graded.append((item, text, route_of(reply.intent if reply else None)))
    return score_all(graded)


def format_report(report: Report, *, verbose: bool = False) -> str:
    """Render a report for CI logs and the local runner."""
    lines = [
        f"golden set: {report.passed}/{report.total} passed "
        f"({report.rate:.0%}), routing {report.route_rate:.0%}",
    ]
    for pack, (ok, total) in sorted(report.by_pack().items()):
        lines.append(f"  {pack:19} {ok:3}/{total:<3} {ok / total:.0%}")
    for lang, (ok, total) in sorted(report.by_lang().items()):
        lines.append(f"  lang {lang:14} {ok:3}/{total:<3} {ok / total:.0%}")
    if report.violations:
        lines.append(f"  wrong advice: {len(report.violations)}")
        for r in report.violations:
            lines.append(f"    {r.item.id}: {r.explain()}")
    if verbose:
        for r in report.failures():
            if r not in report.violations:
                lines.append(f"    {r.item.id} [{r.item.pack}] {r.explain()}")
    return "\n".join(lines)


def by_pack(items: Iterable[Item]) -> dict[str, list[Item]]:
    out: dict[str, list[Item]] = {}
    for it in items:
        out.setdefault(it.pack, []).append(it)
    return out
