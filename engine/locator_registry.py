"""PR-A — multi-locator Page Object library.

Sits between the recorder (which captures one locator chain per element)
and the runner (which needs alternates when the primary drifts). Single
authoring goal: a recorded ``page.get_by_role("button", name="Sign in")``
ought to also try the same button by ``data-testid``, by ``label``, by
text content, and only fall back to brittle CSS / XPath when nothing
else works. The runner's ``try_locator_chain`` walks the ranked list
top-down; the registry remembers which strategy actually resolved last
time so the next run promotes the winner to the front.

Pure data + DB shim — no Playwright runtime, safe to import in unit
tests. The Playwright wait/click logic lives in
``engine.automation_runner.AutomationRunner.try_locator_chain``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Iterable

from engine import db as _db
from engine.log import get_logger

_log = get_logger(__name__)


# Priority table — higher score = tried first by ``try_locator_chain``.
# ``testid`` sits at the top because the recorder already invokes
# codegen with ``--test-id-attribute data-testid,data-test,data-qa``
# (PR-B), which means any element instrumented with one of those
# attributes is the project's stable contract. ``id`` and explicit
# ``data-testid`` selectors come next, then accessibility-friendly
# strategies (role, label, placeholder, alt, title), then text content,
# and finally raw ``css`` / ``xpath`` which break on any DOM shuffle.
_STRATEGY_SCORE = {
    "testid":      100,
    "id":           90,
    "role":         70,
    "label":        60,
    "placeholder":  55,
    "alt":          50,
    "title":        48,
    "text":         40,
    "css":          20,
    "xpath":        10,
}


@dataclass
class LocatorCandidate:
    """One way to find an element. ``value`` is the Playwright selector
    string the runner hands straight to ``page.locator(...)`` (e.g.
    ``"data-testid=submit"``, ``"role=button[name='Sign in']"``,
    ``"label=Email"``). ``score`` carries the priority used to rank
    candidates inside :func:`rank_candidates`; callers can fall back to
    :data:`_STRATEGY_SCORE` when they don't want to override.

    The dataclass is the only public exchange shape — DB rows store
    serialised lists of these via ``asdict``, and the recorder hands
    fresh instances straight to ``register_candidates``.
    """
    strategy: str
    value: str
    score: int = 0

    def __post_init__(self) -> None:
        # If the caller didn't set a score, derive one from the strategy
        # so ranking still works. Unknown strategies fall to the bottom.
        if self.score == 0:
            self.score = _STRATEGY_SCORE.get(self.strategy, 0)


def strategy_of(selector: str) -> str:
    """Classify a Playwright selector string back into a strategy name.

    Mirrors the format the recorder parser emits in
    ``engine.recorder_parser._locator_part``: ``role=…``,
    ``label=…``, ``text=…``, ``data-testid=…``, ``placeholder=…``,
    ``alt=…``, ``title=…``. Anything else is assumed to be a CSS or
    XPath blob — ``xpath=...`` / leading ``//`` -> ``"xpath"``,
    everything else -> ``"css"``.

    Used by ``try_locator_chain`` to stamp ``last_success_strategy``
    from whichever target actually resolved, so the registry can
    promote that strategy on the next run.
    """
    s = (selector or "").strip()
    if not s:
        return ""
    if s.startswith("data-testid="):
        return "testid"
    if s.startswith("role="):
        return "role"
    if s.startswith("label="):
        return "label"
    if s.startswith("placeholder="):
        return "placeholder"
    if s.startswith("text="):
        return "text"
    if s.startswith("alt="):
        return "alt"
    if s.startswith("title="):
        return "title"
    if s.startswith("id=") or s.startswith("#"):
        return "id"
    if s.startswith("xpath=") or s.startswith("//"):
        return "xpath"
    return "css"


def rank_candidates(candidates: Iterable[LocatorCandidate]) -> list[LocatorCandidate]:
    """Return candidates sorted descending by score, dedup'd by value.

    Stable on ties so the recorder's original capture order survives —
    two equally-ranked role= selectors keep the order they appeared in
    the codegen chain. Empty / blank ``value`` entries are dropped so a
    truncated capture cannot poison the chain with empty selectors.
    """
    seen: set[str] = set()
    out: list[LocatorCandidate] = []
    # ``sorted`` is stable in CPython, so we keep insertion order on ties.
    for cand in sorted(candidates, key=lambda c: -c.score):
        v = (cand.value or "").strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(cand)
    return out


def candidates_to_targets(candidates: list[LocatorCandidate]) -> tuple[str, list[str]]:
    """Reduce a ranked list to (primary, alternates) for AutomationStep.

    Convenience used by the recorder parser — keeps the dataclass
    shape (LocatorCandidate) inside this module and hands the runner
    plain selector strings via the existing ``target`` /
    ``target_alternates`` fields. Empty input returns ``("", [])`` so
    callers never need to special-case that branch.
    """
    if not candidates:
        return "", []
    primary = candidates[0].value
    alternates = [c.value for c in candidates[1:]]
    return primary, alternates


def serialise(candidates: list[LocatorCandidate]) -> list[dict]:
    """Plain ``list[dict]`` for storage in ``Locator.candidates_json``."""
    return [asdict(c) for c in candidates]


def deserialise(payload: list[dict]) -> list[LocatorCandidate]:
    """Inverse of :func:`serialise`. Bad rows are dropped silently so a
    forward-compat schema change in storage doesn't crash the runner."""
    out: list[LocatorCandidate] = []
    if not isinstance(payload, list):
        return out
    for item in payload:
        if not isinstance(item, dict):
            continue
        strat = str(item.get("strategy") or "").strip()
        value = str(item.get("value") or "").strip()
        if not (strat and value):
            continue
        try:
            score = int(item.get("score") or _STRATEGY_SCORE.get(strat, 0))
        except (TypeError, ValueError):
            score = _STRATEGY_SCORE.get(strat, 0)
        out.append(LocatorCandidate(strategy=strat, value=value, score=score))
    return out


# ── DB-touching wrappers ──────────────────────────────────────────

def register_candidates(project_id: str, label: str,
                         candidates: list[LocatorCandidate]) -> int:
    """Persist the ranked list for ``(project_id, label)``. Returns the
    new/updated row id, or 0 on invalid input. Thin wrapper over
    :func:`engine.db.register_locator_candidates` so callers can stay
    in dataclass-land."""
    if not (project_id and label):
        return 0
    ranked = rank_candidates(candidates)
    return _db.register_locator_candidates(
        project_id, label, serialise(ranked))


def best_alternates(project_id: str, label: str,
                     defaults: list[str] | None = None) -> list[str]:
    """Return the ordered selector list the runner should try.

    Strategy:
      1. If a row exists for ``(project_id, label)`` AND it has a
         non-empty ``last_success_strategy``, promote the candidate with
         that strategy to position 0 — the runner short-circuits on the
         winner from last time.
      2. Otherwise return candidates in their stored rank order.
      3. When no row exists, fall back to ``defaults`` (the recorder's
         in-step ``target`` + ``target_alternates``). This is the path
         the runner takes for every step on the very first execution
         after a recording.
    """
    row = _db.get_locator(project_id, label) if (project_id and label) else None
    if row:
        cands = deserialise(row.get("candidates") or [])
        winner = (row.get("last_success_strategy") or "").strip()
        if winner:
            promoted = [c for c in cands if c.strategy == winner]
            rest = [c for c in cands if c.strategy != winner]
            cands = promoted + rest
        targets = [c.value for c in cands if c.value]
        if targets:
            return targets
    return list(defaults or [])


def record_success(project_id: str, label: str, selector: str) -> bool:
    """The runner calls this when a chain element resolved. ``selector``
    is the raw Playwright string that worked; we classify it via
    :func:`strategy_of` so the registry can think in strategy terms."""
    if not (project_id and label):
        return False
    strat = strategy_of(selector)
    return _db.record_locator_success(project_id, label, strat)


def record_failure(project_id: str, label: str, selector: str = "") -> bool:
    """Bump the fail counter when every alternate in the chain timed
    out. ``selector`` is captured for completeness — passed through to
    the DB helper but only the count actually changes today."""
    if not (project_id and label):
        return False
    strat = strategy_of(selector) if selector else "all"
    return _db.record_locator_failure(project_id, label, strat)


__all__ = [
    "LocatorCandidate",
    "rank_candidates",
    "candidates_to_targets",
    "serialise",
    "deserialise",
    "strategy_of",
    "register_candidates",
    "best_alternates",
    "record_success",
    "record_failure",
]
