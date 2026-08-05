"""
TestForTge — Tedgie's mentoring packs.

The ISTQB layer answers "what is severity". This layer answers "what
severity is a footer typo", which is a different question and was, before
this module, answered with the definition of severity.

── Why a deterministic layer at all ──────────────────────────────────

Because the answers are house rules, and a house rule that the model
paraphrases is no longer a rule. "Click on" and not "press", "drop-down"
and not "dropdown", "Verify that" and never "should" — these are measured
from the team's 4,808-case corpus and they are the same rules
``tc_author`` generates test cases with. If chat advice and generation
disagree, the tester is told one thing and shown another, and the advice
loses. So the naming and process answers come out of the same YAML the
generator reads, verbatim, with no model in the path.

── Why it runs BEFORE the ISTQB layer, and how it avoids stealing from it ──

``istqb_knowledge.detect_topic`` matches on a bare keyword: the word
"severity" anywhere in a message routes to the definition of severity.
That is why the severity *recommender* already in ``chatbot`` was
unreachable for almost every concrete question. Putting mentoring after it
would leave the defect in place.

But definitional questions genuinely belong to the ISTQB layer, which
quotes the syllabus verbatim for people revising for the exam. So
mentoring stands down when the message is definitional in *shape* — "what
is", "explain", "difference between", "що таке" — and an ISTQB topic or
glossary term is present. Shape and subject both, because "what do I
attach to a bug" is a definitional shape about nothing in the syllabus,
and "severity of a typo" is a syllabus subject in no definitional shape.

The golden set is the oracle for that boundary: SEV-008 and the twelve
THY items assert the ISTQB layer keeps its questions, and thirty-eight
pack items assert it stops taking the others.

── Matching ──────────────────────────────────────────────────────────

An entry declares ``any`` (at least one must appear) and optionally
``all`` (groups, each needing one hit) and ``none`` (vetoes). The
best-scoring entry wins; score counts distinct matched triggers, and a
longer matched trigger breaks a tie, because the more specific phrase is
the better-targeted entry. ``weight`` exists so a pack can carry a
catch-all that only fires when nothing specific matched.

Deliberately not embeddings: with ~15 entries per pack and triggers taken
from the words people actually use, a scored keyword match is auditable —
when an answer is wrong you can see which trigger fired and fix that
trigger. The BM25 layer over the ISTQB corpus is the cautionary tale:
it answered "click or press" with a Java Selenium snippet at a score well
above its own relevance floor, and there is nothing to fix in a number.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from engine.log import get_logger

log = get_logger(__name__)

PACK_DIR = Path(__file__).resolve().parent / "qa_knowledge" / "mentoring"

#: Packs in load order. A message is offered to every pack and the best
#: match across all of them wins, so order only settles exact ties.
PACKS: tuple[str, ...] = ("severity_priority", "layer", "naming", "process")

#: Definitional *shape* cues. Presence of one of these plus a syllabus
#: subject hands the message back to the ISTQB layer.
_DEFINITIONAL = (
    "what is", "what are", "what's the difference", "whats the difference",
    "difference between", "define", "definition", "meaning of", "explain",
    "what does", "що таке", "яка різниця", "чим відрізня", "визначення",
    "поясни", "розкажи про",
)


@dataclass(frozen=True)
class Entry:
    id: str
    pack: str
    any_of: tuple[str, ...]
    all_of: tuple[tuple[str, ...], ...] = ()
    none_of: tuple[str, ...] = ()
    weight: float = 0.0
    answer: str = ""
    follow_up: tuple[str, ...] = ()
    source: str = ""


@dataclass(frozen=True)
class Pack:
    name: str
    lang: str
    label: str
    entries: tuple[Entry, ...]


@dataclass
class Match:
    entry: Entry
    score: float
    hits: tuple[str, ...] = ()

    @property
    def longest_hit(self) -> int:
        return max((len(h) for h in self.hits), default=0)


@dataclass
class MentorAnswer:
    """What the chat layer renders."""

    text: str
    pack: str
    entry_id: str
    follow_up: tuple[str, ...] = ()
    source: str = ""
    #: True when the entry answers "the method" rather than a named case.
    #: Carried on the answer so callers never need a list of catch-all ids
    #: to keep in step with the packs — the first version of this did keep
    #: such a list in chatbot, and it had one id spelled wrong, which meant
    #: the process catch-all was never actually declined.
    is_catch_all: bool = False
    #: True when the pack for the requested language was missing and the
    #: English one answered. Surfaced rather than hidden so a half-done
    #: localisation is visible in the eval instead of reading as fluent.
    fell_back_to_english: bool = False

    @property
    def route(self) -> str:
        return f"pack:{self.pack}"


class PackError(ValueError):
    """A pack file is malformed. Raised at load time, with the entry id."""


_CACHE: dict[tuple[str, str], Pack | None] = {}


# ── Loading ───────────────────────────────────────────────────────────

def _strings(raw: Any, what: str, entry_id: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise PackError(f"{entry_id}: `{what}` must be a string or list")
    out = tuple(str(x).strip().lower() for x in raw if str(x).strip())
    for term in out:
        if len(term) < 3:
            # Same lesson as the golden set: under substring matching a
            # two-character trigger fires on unrelated words.
            raise PackError(f"{entry_id}: trigger {term!r} is too short to mean anything")
    return out


def _entry_from(raw: dict[str, Any], pack: str) -> Entry:
    entry_id = str(raw.get("id") or "").strip()
    if not entry_id:
        raise PackError(f"{pack}: an entry has no id")
    answer = str(raw.get("answer") or "").strip()
    if not answer:
        raise PackError(f"{entry_id}: no answer")
    any_of = _strings(raw.get("any"), "any", entry_id)
    all_raw = raw.get("all") or []
    if isinstance(all_raw, str):
        all_raw = [[all_raw]]
    all_of = tuple(_strings(g, "all", entry_id) for g in all_raw)
    if not any_of and not all_of:
        raise PackError(f"{entry_id}: no triggers — it would match everything")
    return Entry(
        id=entry_id,
        pack=pack,
        any_of=any_of,
        all_of=all_of,
        none_of=_strings(raw.get("none"), "none", entry_id),
        weight=float(raw.get("weight") or 0.0),
        answer=answer,
        follow_up=tuple(str(f).strip() for f in (raw.get("follow_up") or []) if str(f).strip()),
        source=str(raw.get("source") or "").strip(),
    )


def load_pack(name: str, lang: str = "en") -> Pack | None:
    """Load one pack, falling back to English when the localised file is
    absent. Returns None when neither exists."""
    key = (name, lang)
    if key in _CACHE:
        return _CACHE[key]
    path = PACK_DIR / f"{name}.{lang}.yaml"
    if not path.exists() and lang != "en":
        path = PACK_DIR / f"{name}.en.yaml"
    if not path.exists():
        _CACHE[key] = None
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_entries = data.get("entries") or []
    if not isinstance(raw_entries, list) or not raw_entries:
        raise PackError(f"{path.name}: no entries")
    pack = Pack(
        name=str(data.get("pack") or name),
        lang=str(data.get("lang") or "en"),
        label=str(data.get("label") or name),
        entries=tuple(_entry_from(r, name) for r in raw_entries),
    )
    ids = [e.id for e in pack.entries]
    if len(ids) != len(set(ids)):
        raise PackError(f"{path.name}: duplicate entry ids")
    _CACHE[key] = pack
    return pack


def load_all(lang: str = "en") -> tuple[Pack, ...]:
    return tuple(p for p in (load_pack(n, lang) for n in PACKS) if p)


def clear_cache() -> None:
    _CACHE.clear()


# ── Matching ──────────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    """Lower-case, drop quotes, collapse whitespace.

    Quotes are stripped rather than normalised because people quote the very
    words the triggers are written from: "Should I write 'click' or 'press'
    in a step?" did not match a `click or press` trigger, and "Can I write
    'should' in an expected result?" matched the wrong entry. Both were
    measured against the golden set, and both were invisible in testing that
    used unquoted phrasings.
    """
    low = (text or "").lower()
    low = low.replace("’", "'").replace("«", "").replace("»", "")
    low = low.replace("'", "").replace('"', "").replace("`", "")
    return re.sub(r"\s+", " ", low)


def is_definitional(message: str) -> bool:
    """True when the message asks what something *is*.

    Shape only. The caller pairs it with a subject check, because a
    definitional shape about something outside the syllabus ("what do I
    attach to a bug report") still belongs to mentoring.
    """
    low = _normalise(message)
    return any(cue in low for cue in _DEFINITIONAL)


def match_entry(message: str, pack: Pack) -> Match | None:
    low = _normalise(message)
    best: Match | None = None
    for entry in pack.entries:
        if any(v in low for v in entry.none_of):
            continue
        hits: list[str] = []
        satisfied = True
        for group in entry.all_of:
            group_hits = [t for t in group if t in low]
            if not group_hits:
                satisfied = False
                break
            hits.extend(group_hits)
        if not satisfied:
            continue
        any_hits = [t for t in entry.any_of if t in low]
        if entry.any_of and not any_hits:
            continue
        hits.extend(any_hits)
        score = len(set(hits)) + entry.weight
        candidate = Match(entry=entry, score=score, hits=tuple(dict.fromkeys(hits)))
        if best is None or (candidate.score, candidate.longest_hit) > (best.score, best.longest_hit):
            best = candidate
    return best


def answer(message: str, lang: str = "en", *, packs: Iterable[str] | None = None) -> MentorAnswer | None:
    """Best mentoring answer for *message*, or None to fall through.

    None means "not ours" — the caller keeps its existing chain. That is
    the whole contract: this layer never guesses, because a mentor who
    guesses about severity is worse than one who says nothing and lets the
    syllabus answer.
    """
    names = tuple(packs) if packs else PACKS
    best: Match | None = None
    best_pack: Pack | None = None
    for name in names:
        pack = load_pack(name, lang)
        if not pack:
            continue
        m = match_entry(message, pack)
        if m and (best is None or (m.score, m.longest_hit) > (best.score, best.longest_hit)):
            best, best_pack = m, pack
    if not best or not best_pack:
        return None
    return MentorAnswer(
        text=best.entry.answer,
        pack=best.entry.pack,
        entry_id=best.entry.id,
        follow_up=best.entry.follow_up,
        source=best.entry.source,
        is_catch_all=best.entry.weight < 0,
        fell_back_to_english=(lang != "en" and best_pack.lang == "en"),
    )
