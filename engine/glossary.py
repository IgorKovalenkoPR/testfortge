"""TestFortge — UI terminology glossary + wording linter.

Loads the team's own terminology reference
(``engine/qa_knowledge/glossary/ui_terms.en.yaml`` — the 91 rows of
Glossary.xlsx, merged to 87 unique terms) and the wording rules distilled
from the reviewing team lead's comments on the training deliverable
(``engine/qa_knowledge/style/wording_rules.yaml``).

Two consumers, deliberately:

* **Prompt material.** :func:`glossary_text` and :func:`wording_rules_text`
  hand the raw YAML to the authoring agents so the model sees the
  ``reviewer:`` quotes — the part that stops it treating a house
  convention as negotiable.

* **Code enforcement.** :func:`lint_text` and :func:`normalise_text` apply
  the mechanical half of the same rules with no model in the loop, so the
  free / no-API-key path produces text a reviewer would accept too.

Rewrite discipline
------------------
:func:`normalise_text` only makes changes that cannot lose information:

1. Page-region names get capitalised (Header, Footer, Homepage …) —
   the reviewer's single most repeated comment.
2. An alias is rewritten to its canonical term ONLY when the two are the
   same word differing in spacing, hyphenation or case ("dropdown" →
   "drop-down", "scrollbar" → "scroll bar", "check box" → "checkbox").

Everything semantic — "burger menu" for Hamburger button, "lightbox" for
Modal — is reported as a lint finding with a suggestion and left alone.
A silent semantic rename would be a guess, and a wrong guess here breaks
the locator the label is standing in for.

Nothing inside double quotes is ever touched. Quoted strings are
on-screen labels; rewriting one turns a working locator into a broken
one.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any, Iterable

from engine.log import get_logger

_logger = get_logger(__name__)

_HERE = os.path.dirname(__file__)
_GLOSSARY_PATH = os.path.join(_HERE, "qa_knowledge", "glossary",
                              "ui_terms.en.yaml")
_WORDING_PATH = os.path.join(_HERE, "qa_knowledge", "style",
                             "wording_rules.yaml")

# Page regions the reviewer insisted on capitalising. Kept in code rather
# than derived from the glossary because "Homepage" is not a glossary term
# — it is the name of a surface, and the comment ("Main/Homepage should be
# started from a capital letter") is about surfaces specifically.
#
# Exactly the three the reviewer named, and no more. An earlier draft also
# listed Sidebar / Navigation / Breadcrumbs; auditing the shipped templates
# showed that produced 14 false positives on ordinary English ("the main
# navigation menu", "the breadcrumb trail") where no region is meant. If a
# region belongs here, a reviewer comment has to name it.
PAGE_REGIONS: tuple[str, ...] = ("Header", "Footer", "Homepage")

# "header" and "footer" also name things that are not the page region: a
# column header in a grid, an HTTP request header, a table footer row, a
# grid's header checkbox. Capitalising those is wrong — it broke four grid
# tests when a blanket rule first shipped here.
#
# So the predicate is an ALLOW-list, not a block-list: capitalise only when
# the word is confidently the region, which is when it is followed by a
# function word, a verb, or one of the children a region actually owns.
# Anything else — "header row", "header checkbox", "footer note" — is left
# alone. Under-capitalising is a silent no-op; mis-capitalising corrupts a
# term the reader is using as a locator.
_REGION_LEFT_BLOCK = frozenset({
    "column", "columns", "table", "tables", "row", "rows", "grid", "grids",
    "request", "response", "http", "https", "email", "mail", "section",
    "sections", "card", "cards", "list", "message", "packet", "file", "csv",
    "sticky", "modal", "sheet", "report", "invoice", "pdf", "xlsx",
})
_REGION_RIGHT_ALLOW = frozenset({
    "",
    # function words
    "is", "are", "was", "were", "of", "on", "in", "and", "or", "to", "for",
    "at", "from", "with", "that", "which", "after", "before", "while",
    "when", "by", "via", "as", "but", "than", "if", "until", "so",
    # verbs a region is the subject of
    "does", "do", "has", "have", "contains", "contain", "shows", "show",
    "displays", "display", "collapses", "collapse", "opens", "open",
    "loads", "load", "becomes", "remains", "stays", "appears", "appear",
    "renders", "render", "sticks", "keeps", "hides", "expands",
    # children a page region genuinely owns
    "logo", "menu", "navigation", "links", "link", "area", "section",
    "region", "block", "contacts", "copyright", "sitemap", "search",
})

# Openers the reference corpora never use in a title or objective.
_BAD_OPENERS = ("validate", "check", "ensure", "test that", "make sure",
                "confirm that", "verifying")


# ── asset loading ────────────────────────────────────────────────────

def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        _logger.warning("glossary: cannot read %s: %s",
                        os.path.basename(path), exc)
        return ""


@lru_cache(maxsize=1)
def glossary_text() -> str:
    """Raw ``ui_terms.en.yaml`` for the authoring agents' system block."""
    return _read(_GLOSSARY_PATH)


@lru_cache(maxsize=1)
def wording_rules_text() -> str:
    """Raw ``wording_rules.yaml`` for the authoring agents' system block."""
    return _read(_WORDING_PATH)


@lru_cache(maxsize=1)
def _glossary_doc() -> dict:
    raw = glossary_text()
    if not raw:
        return {}
    try:
        import yaml
        doc = yaml.safe_load(raw)
    except Exception as exc:  # pragma: no cover — defensive
        _logger.warning("glossary: YAML parse failed: %s", exc)
        return {}
    return doc if isinstance(doc, dict) else {}


@lru_cache(maxsize=1)
def _wording_doc() -> dict:
    raw = wording_rules_text()
    if not raw:
        return {}
    try:
        import yaml
        doc = yaml.safe_load(raw)
    except Exception as exc:  # pragma: no cover — defensive
        _logger.warning("wording_rules: YAML parse failed: %s", exc)
        return {}
    return doc if isinstance(doc, dict) else {}


def terms() -> list[dict]:
    """Every glossary entry, in file order."""
    raw = _glossary_doc().get("terms")
    return [t for t in (raw or []) if isinstance(t, dict) and t.get("term")]


def banned_phrases(bucket: str) -> tuple[str, ...]:
    """A named bucket from ``wording_rules.yaml`` → ``banned_phrases``."""
    bag = _wording_doc().get("banned_phrases") or {}
    vals = bag.get(bucket) if isinstance(bag, dict) else None
    if not isinstance(vals, list):
        return ()
    return tuple(str(v).strip().lower() for v in vals if str(v).strip())


def approved_verbs() -> tuple[str, ...]:
    """The step-verb whitelist from ``wording_rules.yaml``."""
    block = _wording_doc().get("action_verbs") or {}
    vals = block.get("approved") if isinstance(block, dict) else None
    if not isinstance(vals, list):
        return ()
    out: list[str] = []
    for v in vals:
        v = str(v).strip()
        # "Leave … empty" is a pattern, not a leading verb — keep only the
        # first word for prefix matching.
        if v:
            out.append(v)
    return tuple(out)


# ── alias index ──────────────────────────────────────────────────────

def _spelling_key(text: str) -> str:
    """Collapse spacing / hyphenation / case so spelling variants match.

    ``"drop-down"``, ``"drop down"`` and ``"dropdown"`` all key to
    ``"dropdown"``. This is the test that decides whether an alias may be
    rewritten silently.
    """
    return re.sub(r"[^a-z0-9]+", "", text.lower())


@lru_cache(maxsize=1)
def _index() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Return ``(lookup, rewrites, avoid)``.

    ``lookup``   every lowercase alias, ``avoid`` entry and canonical term
                 → canonical term. Used to resolve a name to the glossary.
    ``rewrites`` only aliases that are pure spelling variants of their
                 canonical term (same :func:`_spelling_key`). These are the
                 only names :func:`normalise_text` rewrites silently.
    ``avoid``    names the linter flags with the canonical term as the
                 suggestion. Declared per-term in the YAML so a reviewer
                 decides what counts as wrong, not this module.
    """
    lookup: dict[str, str] = {}
    rewrites: dict[str, str] = {}
    avoid: dict[str, str] = {}
    for entry in terms():
        canonical = str(entry.get("term") or "").strip()
        if not canonical:
            continue
        lookup.setdefault(canonical.lower(), canonical)
        can_key = _spelling_key(canonical)
        for alias in (entry.get("aliases") or []):
            alias = str(alias or "").strip()
            # Skip only a byte-identical repeat. An alias that differs from
            # the canonical term in CASE alone ("iframe" vs "IFrame") is
            # exactly what the rewriter exists to fix, so it must not be
            # filtered out here.
            if not alias or alias == canonical:
                continue
            lookup.setdefault(alias.lower(), canonical)
            if _spelling_key(alias) == can_key:
                rewrites[alias.lower()] = canonical
        for bad in (entry.get("avoid") or []):
            bad = str(bad or "").strip()
            if not bad or bad == canonical:
                continue
            lookup.setdefault(bad.lower(), canonical)
            # A name in `avoid` that is only a spelling variant still gets
            # rewritten mechanically — no point flagging what we can fix.
            if _spelling_key(bad) == can_key:
                rewrites[bad.lower()] = canonical
            else:
                avoid[bad.lower()] = canonical
    return lookup, rewrites, avoid


def canonical_term(text: str) -> str:
    """Canonical glossary term for ``text``, or ``""`` if unknown."""
    lookup, _, _ = _index()
    return lookup.get((text or "").strip().lower(), "")


def suggest(text: str) -> str:
    """Alias of :func:`canonical_term`, kept for call-site readability."""
    return canonical_term(text)


def element_terms() -> list[dict]:
    """Glossary entries a tester can point at (``kind: element``)."""
    return [t for t in terms() if str(t.get("kind") or "") == "element"]


def control_type_for(term: str) -> str:
    """The noun that follows a quoted label for ``term`` ("button", …)."""
    canonical = canonical_term(term) or (term or "").strip()
    for entry in terms():
        if str(entry.get("term") or "").lower() == canonical.lower():
            return str(entry.get("control_type") or "")
    return ""


# ── quote-safe rewriting ─────────────────────────────────────────────

# Split on double-quoted runs, keeping the delimiters. Odd indices of the
# result are the quoted segments (including their quotes) and are never
# modified — they are on-screen labels.
_QUOTED_RE = re.compile(r'("[^"]*")')


def _outside_quotes(text: str, fn) -> str:
    parts = _QUOTED_RE.split(text or "")
    for i in range(0, len(parts), 2):
        parts[i] = fn(parts[i])
    return "".join(parts)


def _iter_unquoted(text: str) -> Iterable[str]:
    parts = _QUOTED_RE.split(text or "")
    return (parts[i] for i in range(0, len(parts), 2))


# Both quote styles, for checks that must not fire on a citation. The
# corpus quotes on-screen labels with either ('Show password', "Sign In"),
# and a bug title derived from an objective reproduces the criterion it
# cites verbatim — a modal inside that citation belongs to the quoted
# text, not to the sentence being written.
_ANY_QUOTED_RE = re.compile(r"(\"[^\"]*\"|'[^']*')")


def _iter_unquoted_any(text: str) -> Iterable[str]:
    parts = _ANY_QUOTED_RE.split(text or "")
    return (parts[i] for i in range(0, len(parts), 2))


# ── normalisation ────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _region_re() -> re.Pattern[str]:
    alt = "|".join(re.escape(r) for r in PAGE_REGIONS)
    # Capture the neighbouring words so _is_page_region can veto matches
    # that name a column header or an HTTP header rather than the region.
    return re.compile(
        rf"(?:(?P<left>[\w-]+)\s+)?\b(?P<word>{alt})\b(?:\s+(?P<right>[\w-]+))?",
        re.IGNORECASE)


def _is_page_region(m: re.Match[str]) -> bool:
    """True when a Header / Footer / Homepage match really means the region."""
    left = (m.group("left") or "").lower()
    if left in _REGION_LEFT_BLOCK:
        return False
    return (m.group("right") or "").lower() in _REGION_RIGHT_ALLOW


@lru_cache(maxsize=1)
def _rewrite_re() -> re.Pattern[str] | None:
    _, rewrites, _ = _index()
    return _phrase_re(rewrites)


@lru_cache(maxsize=1)
def _avoid_re() -> re.Pattern[str] | None:
    """One combined pattern for every flagged name.

    Built once instead of running ~300 separate searches per field — a
    500-case generation lints ~2,500 strings and the per-alias loop this
    replaced dominated the run.
    """
    _, _, avoid = _index()
    return _phrase_re(avoid)


def _is_proper_noun(term: str) -> bool:
    """True for WYSIWYG / IFrame / Open Graph — a capital after the first.

    Common nouns that the glossary happens to title-case (Accordion,
    Drop-down) mirror the author's case instead, so "dropdown" mid-sentence
    becomes "drop-down" and not "Drop-down".
    """
    return any(c.isupper() for c in term[1:])


def capitalise_regions(text: str) -> str:
    """Capitalise page-region names outside quotes.

    Reviewer: "Footer should be started from a capital letter".
    """
    if not text:
        return text
    canon = {r.lower(): r for r in PAGE_REGIONS}

    def _sub(m: re.Match[str]) -> str:
        word = m.group("word")
        if not _is_page_region(m):
            return m.group(0)
        fixed = canon.get(word.lower(), word)
        return m.group(0)[:m.start("word") - m.start(0)] + fixed \
            + m.group(0)[m.end("word") - m.start(0):]

    return _outside_quotes(text, lambda chunk: _region_re().sub(_sub, chunk))


def canonicalise_spellings(text: str) -> str:
    """Rewrite spelling-variant aliases to the canonical term.

    Only variants with an identical :func:`_spelling_key` are touched, and
    only outside quotes. Capitalisation of the replacement follows the
    original: a variant that started mid-sentence in lower case stays
    lower case unless the canonical term is itself capitalised in the
    glossary.
    """
    if not text:
        return text
    pattern = _rewrite_re()
    if pattern is None:
        return text
    _, rewrites, _ = _index()

    def _sub(m: re.Match[str]) -> str:
        found = m.group(1)
        canonical = rewrites.get(found.lower(), found)
        if _is_proper_noun(canonical):
            return canonical
        if found[:1].isupper():
            return canonical[:1].upper() + canonical[1:]
        return canonical[:1].lower() + canonical[1:]

    return _outside_quotes(text, lambda chunk: pattern.sub(_sub, chunk))


def strip_trailing_period(text: str) -> str:
    """Drop a single trailing period from a title / objective.

    Reviewer: "Remove dot at the end". Ellipses and abbreviations that
    genuinely end in a period ("etc.") are left alone.
    """
    s = (text or "").rstrip()
    if s.endswith("...") or s.endswith("…"):
        return s
    if s.endswith(".") and not re.search(r"\b(etc|e\.g|i\.e)\.$", s, re.I):
        return s[:-1].rstrip()
    return s


def tidy_spacing(text: str) -> str:
    """Collapse double spaces and drop space before punctuation.

    Reviewer: "extra space".
    """
    s = re.sub(r"[ \t]{2,}", " ", text or "")
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    return s.strip()


#: Third-person singular for the regular verbs this corpus uses. Kept
#: here rather than in a caller so the deterministic generator and the
#: LLM normaliser inflect identically.
_IRREGULAR_THIRD_PERSON = {"be": "is", "have": "has", "do": "does",
                           "go": "goes"}


def third_person(verb: str) -> str | None:
    """Bare infinitive → third-person singular, or None if unsure."""
    v = (verb or "").strip().lower()
    if not re.fullmatch(r"[a-z]+", v):
        return None
    if v in _IRREGULAR_THIRD_PERSON:
        return _IRREGULAR_THIRD_PERSON[v]
    if re.search(r"(s|sh|ch|x|z|o)$", v):
        return v + "es"
    if re.search(r"[^aeiou]y$", v):
        return v[:-1] + "ies"
    return v + "s"


#: Past participles the "+ed" rule gets wrong. Every irregular verb the
#: coverage model actually uses, plus the handful a rule author is most
#: likely to reach for next. An unlisted irregular returns None rather
#: than "leaved" — a missing rewrite is visible, a wrong one is not.
_IRREGULAR_PARTICIPLE = {
    "leave": "left", "run": "run", "go": "gone", "set": "set",
    "reset": "reset", "put": "put", "read": "read", "send": "sent",
    "keep": "kept", "find": "found", "hide": "hidden", "show": "shown",
    "write": "written", "choose": "chosen", "give": "given",
    "take": "taken", "make": "made", "see": "seen", "hold": "held",
    "cut": "cut", "split": "split", "be": "been", "do": "done",
    "have": "had", "get": "got", "upload": "uploaded", "undo": "undone",
}

#: Regular verbs whose final consonant doubles: "submit" → "submitted".
#: Restricted to a listed set for the same reason — the general rule
#: (stress on the last syllable) is not something a regex can decide.
_DOUBLING_PARTICIPLE = {
    "submit", "commit", "omit", "permit", "admit", "cancel", "label",
    "scroll", "stop", "drop", "tap", "plan", "refer", "prefer", "defer",
}


def past_participle(verb: str) -> str | None:
    """Bare infinitive → past participle, or None when unsure.

    Used to turn an imperative objective into the passive. Returning
    None is the safe answer: the caller then keeps the objective as
    written rather than emitting a word that is not English.
    """
    v = (verb or "").strip().lower()
    if not re.fullmatch(r"[a-z]+", v):
        return None
    if v in _IRREGULAR_PARTICIPLE:
        return _IRREGULAR_PARTICIPLE[v]
    if v in _DOUBLING_PARTICIPLE:
        return v + v[-1] + "ed"
    if v.endswith("e"):
        return v + "d"
    if re.search(r"[^aeiou]y$", v):
        return v[:-1] + "ied"
    return v + "ed"


#: Determiners that force the singular no matter what follows.
_SINGULAR_DETERMINER = re.compile(r"^(a|an|each|every|one)\b", re.I)
#: …and the ones that force the plural.
_PLURAL_DETERMINER = re.compile(r"^(all|both|several|multiple|two|three)\b",
                                re.I)


def reads_plural(phrase: str) -> bool:
    """Whether a noun phrase takes "are" rather than "is".

    Deliberately shallow: the corpus's object phrases are short and
    determiner-led. When nothing settles it, singular is the answer —
    it is the overwhelming majority here, so a wrong guess is rare and
    the sentence still parses.
    """
    text = (phrase or "").strip()
    if not text:
        return False
    if _PLURAL_DETERMINER.match(text):
        return True
    if _SINGULAR_DETERMINER.match(text):
        return False
    head = re.sub(r"^(the)\s+", "", text, flags=re.I).split()
    if not head:
        return False
    word = re.sub(r"[^a-z]", "", head[0].lower())
    # "characters" plural; "address"/"status" singular despite the s.
    return bool(word) and word.endswith("s") and not word.endswith("ss")


# "User can save the record" -> "User saves the record"
# "User cannot save the record" -> "User does not save the record"
#
# Both are mechanical and information-preserving, so they are rewritten
# rather than merely reported (operator ruling 2026-08-01: a summary
# carries no modal). The other modals — may, might, will, should — change
# meaning when removed, so lint_text reports those and leaves them alone.
_SUMMARY_CAN_RE = re.compile(r"\bcan\s?not\b\s+(?P<verb>[a-z]+)\b", re.I)
_SUMMARY_CANNOT_RE = re.compile(r"\bcan\s+(?P<verb>[a-z]+)\b", re.I)


def strip_summary_modal(text: str) -> str:
    """Remove "can" / "cannot" from a summary, outside quotes."""
    def _fix(chunk: str) -> str:
        chunk = _SUMMARY_CAN_RE.sub(
            lambda m: f"does not {m.group('verb').lower()}", chunk)

        def _inflect(m: re.Match[str]) -> str:
            form = third_person(m.group("verb"))
            return form if form else m.group(0)

        return _SUMMARY_CANNOT_RE.sub(_inflect, chunk)

    return _outside_quotes(text or "", _fix)


def normalise_text(text: str, *, kind: str = "prose") -> str:
    """Apply every information-preserving fix.

    ``kind="title"`` additionally strips the trailing period (titles and
    checklist objectives only — a multi-sentence expected result keeps
    its punctuation) and removes a "can" / "cannot" modal.
    """
    if not text:
        return text
    out = tidy_spacing(text)
    out = canonicalise_spellings(out)
    out = capitalise_regions(out)
    if kind in ("title", "objective"):
        out = strip_trailing_period(out)
    if kind in ("title", "objective", "expected"):
        # "should" is the one modal the ruling permits, and only in an
        # expected result — it is left alone everywhere by this helper
        # and reported by lint_text where it does not belong. "can" and
        # "cannot" are removed wherever they appear in these three
        # fields, because the rewrite is mechanical either way.
        out = strip_summary_modal(out)
    return out


# ── linting ──────────────────────────────────────────────────────────

def _phrase_re(phrases: Iterable[str]) -> re.Pattern[str] | None:
    items = [p for p in phrases if p]
    if not items:
        return None
    alt = "|".join(re.escape(p) for p in sorted(items, key=len, reverse=True))
    return re.compile(rf"(?<![\w-])({alt})(?![\w-])", re.IGNORECASE)


@lru_cache(maxsize=8)
def _banned_re(bucket: str) -> re.Pattern[str] | None:
    return _phrase_re(banned_phrases(bucket))


def _hits(text: str, pattern: re.Pattern[str] | None) -> list[str]:
    if pattern is None or not text:
        return []
    found: list[str] = []
    for chunk in _iter_unquoted(text):
        for m in pattern.finditer(chunk):
            token = m.group(1)
            if token.lower() not in [f.lower() for f in found]:
                found.append(token)
    return found


def lint_text(text: str, *, kind: str = "prose",
              check_opener: bool | None = None) -> list[str]:
    """Return human-readable wording problems in ``text``.

    ``kind`` selects which rules apply:

    ``title`` / ``objective``  opener, trailing period, grading words
    ``step``                   generic steps, vague objects, assertions
    ``expected``               grading words, banned modals
    ``prose``                  the cross-cutting rules only

    ``check_opener`` overrides the default (on for titles and objectives).
    Pass ``False`` for the corpus's dedicated error-message grammar
    ("<Surface>: <attempted action>"), which is a sanctioned alternate
    title form and legitimately does not open with "Verify".
    """
    issues: list[str] = []
    if not (text or "").strip():
        return issues
    raw = text
    if check_opener is None:
        check_opener = kind in ("title", "objective")

    # Cross-cutting: graded outcomes and non-canonical element names.
    for word in _hits(raw, _banned_re("grading")):
        issues.append(
            f'graded outcome "{word}" — name the action or the observable '
            f"state instead (reviewer: do not use correct/incorrect)")

    if kind in ("title", "objective"):
        stripped = raw.strip()
        low = stripped.lower()
        if check_opener and not low.startswith("verify"):
            for bad in _BAD_OPENERS:
                if low.startswith(bad):
                    issues.append(
                        f'opens with "{stripped.split()[0]}" — every title '
                        f'and objective opens with "Verify"')
                    break
            else:
                issues.append('does not open with "Verify"')
        if strip_trailing_period(stripped) != stripped:
            issues.append("trailing period — remove the dot at the end")

    if kind == "step":
        for phrase in _hits(raw, _banned_re("generic_step")):
            issues.append(
                f'generic step "{phrase}" — name the real control and the '
                f"action that operates it")
        for phrase in _hits(raw, _banned_re("vague_object")):
            issues.append(
                f'"{phrase}" does not say what it acts on — name the object '
                f'(reviewer: "What do you scroll?")')
        if re.match(r"\s*(verify|check|ensure|make sure)\b", raw, re.I):
            issues.append(
                "assertion inside a step — use \"Pay attention to …\" or "
                "\"Try to find …\" and put the assertion in the expected "
                "result")

    if kind in ("expected", "prose", "title", "objective"):
        for word in _hits(raw, _banned_re("modal")):
            issues.append(
                f'"{word}" reads as a requirement, not an observation — '
                f"state what happens")

    # Operator ruling 2026-08-01: a summary — a test-case summary, a
    # checklist objective or a bug title — carries no modal verb at all.
    # "should" is the single exception and only in an expected result,
    # where the team's own reviewed deliverable uses it throughout.
    #
    # Applied to titles and objectives only. ``prose`` is excluded
    # because it covers free text (preconditions, comments) where a
    # modal can be a legitimate part of a quoted requirement.
    if kind in ("title", "objective", "expected"):
        # In a summary no modal is allowed at all. In an expected result
        # exactly one is — "should" / "should be" — so it is excluded
        # from the check there and nowhere else.
        allowed = {"should"} if kind == "expected" else set()
        where = ("an expected result" if kind == "expected"
                 else "a summary")
        pattern = _banned_re("modal_summary_only")
        seen: set[str] = set()
        for chunk in _iter_unquoted_any(raw):
            for word in _hits(chunk, pattern):
                low = word.lower()
                if low in allowed or low in seen:
                    continue
                seen.add(low)
                issues.append(
                    f'modal "{word}" in {where} — "should" is the only '
                    f"modal the house style permits, and only in an "
                    f"expected result. State what happens")

    if kind in ("title", "objective"):
        # Active voice with the tester as the subject. The check is about
        # the product, not about who clicked — the operator's ruling
        # (2026-08-01, wording_rules.yaml → voice) asks for the passive
        # with the thing under test in the subject position.
        #
        # Only "User <verb>" is flagged, not active voice in general: a
        # stative "the counter matches the visible row count" is fine and
        # no regex separates the two reliably. This is the shape the
        # generators produced and the one a reviewer keeps striking out.
        actor = re.search(
            r"\b(?:the\s+)?[Uu]ser\s+(?!is\b|are\b|was\b|were\b|can\b)"
            r"([a-z]+(?:ies|es|s))\b", raw)
        if actor:
            issues.append(
                f'"User {actor.group(1)}" — a summary is written in the '
                f"passive voice with the thing under test as the subject, "
                f"not with the tester as the actor")

    # Non-canonical element naming. Suggestion only — normalise_text
    # deliberately does not guess a semantic rename.
    _, _, avoid = _index()
    for found in _hits(raw, _avoid_re()):
        canonical = avoid.get(found.lower())
        if canonical:
            issues.append(
                f'"{found}" is not the glossary term — use "{canonical}"')

    # Region capitalisation.
    for chunk in _iter_unquoted(raw):
        for m in _region_re().finditer(chunk):
            word = m.group("word")
            if word in PAGE_REGIONS or not _is_page_region(m):
                continue
            canonical = next(r for r in PAGE_REGIONS
                             if r.lower() == word.lower())
            issues.append(
                f'"{word}" is a page region — capitalise it '
                f'("{canonical}")')
            break

    # De-duplicate, preserve order.
    seen: set[str] = set()
    out: list[str] = []
    for issue in issues:
        if issue not in seen:
            seen.add(issue)
            out.append(issue)
    return out


def lint_steps(steps: Iterable[Any]) -> list[str]:
    """Lint an ordered step list, prefixing each finding with its index."""
    out: list[str] = []
    for i, step in enumerate(steps or [], 1):
        for issue in lint_text(str(step or ""), kind="step"):
            out.append(f"step {i}: {issue}")
    return out


def starts_from_entry_point(steps: Iterable[Any]) -> bool:
    """True unless step 1 jumps straight to an in-page state.

    Reviewer: "Let`s imagine if this URL will be changed - how other
    tester or developer could navigate to it? Please, specify all steps
    and preconditions steps, starting from main URL."

    Only the mechanically decidable half of that rule lives here. A URL
    carrying a **fragment or a query string** in step 1 means the author
    jumped to a state rather than to a page — no navigation the tester can
    repeat, and nothing to fall back on when the anchor is renamed.

    Path DEPTH is deliberately not flagged. Whether ``/services/mobile``
    should be reached by clicking through a menu or opened directly is a
    judgement about that product, and the reference corpus opens deep
    pages directly ("Go to the site: https://qarea.com/projects"). That
    half of the rule is stated in full in ``wording_rules.yaml`` →
    ``navigation_from_entry_point`` and left to the authoring agent, which
    can see the control inventory; guessing it from the URL alone
    misfires on every crawler-derived case.
    """
    first = ""
    for step in steps or []:
        first = str(step or "").strip()
        if first:
            break
    if not first:
        return True
    m = re.search(r"https?://[^\s\"'<>)]+", first)
    if not m:
        return True
    url = m.group(0).rstrip(".,;")
    tail = re.sub(r"^https?://[^/]*", "", url)
    return "#" not in tail and "?" not in tail


__all__ = [
    "PAGE_REGIONS",
    "glossary_text", "wording_rules_text",
    "terms", "element_terms", "banned_phrases", "approved_verbs",
    "canonical_term", "suggest", "control_type_for",
    "capitalise_regions", "canonicalise_spellings", "strip_trailing_period",
    "tidy_spacing", "normalise_text",
    "lint_text", "lint_steps", "starts_from_entry_point",
]
