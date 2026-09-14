"""TestForTge — the shape of a written test case, in one place.

``qa_team_lead.review_test_cases`` runs this over every generated pack, so
the conventions below hold whoever wrote the case: the LLM author, the
deterministic rule engine, the knowledge-base packs, or a flow template.

They come from an operator review of generated output on 2026-09-14 (the
``SC2_004`` card on staging), and each one is a rule about a *column*, not
about a sentence:

1. Preconditions never say a URL "is reachable". Step 1 of every case
   opens that URL, so reachability is not prior state — the state the
   tester needs stated is that the page **is open**.
2. Preconditions and Expected Result are numbered lists when they carry
   more than one fact, exactly as the steps column already is. One fact
   per line is what lets a failed row point at a single line.
3. Test Data is filled only when the case needs credentials. Everything
   else the reference corpus encodes in the title and the steps, where the
   tester reads it while executing, instead of in a column they have to
   join by eye.
4. Expected results are written with "should" / "should be" rather than
   the declarative "is" / "are".

Rule 4 continues the ruling recorded at ``tc_author._WEAK_MODAL_RE``:
"should" was already the permitted modal and the voice this generator
writes; this module makes the plain copula fall into that voice too, so a
pack stops mixing "is displayed" and "should be displayed" row by row.

Nothing here runs on text a human typed. The editors call
``tc_author.house_style_findings`` instead, which advises and never
rewrites — a reviewer who signed a sentence off outranks a convention.
"""
from __future__ import annotations

import re

# ── 1. "is reachable" is not a precondition ──────────────────────────
#
# Two shapes occur in the shipped packs: the subject-first
# "<url> is reachable in the browser" and the trailing
# "Site is reachable at <url>". The second is rewritten subject-first
# first, so the blanket rule below only ever sees the first.
_REACHABLE_AT_RE = re.compile(
    r"\b(?:the\s+)?(?:site|application(?:\s+under\s+test)?|app|system|page)\s+"
    r"is\s+reachable\s+at\s+(?P<url>\S+?)(?P<tail>[.,;]?)(?=\s|$)",
    re.IGNORECASE,
)
_REACHABLE_RE = re.compile(r"\b(is|are)\s+reachable\b", re.IGNORECASE)


def open_not_reachable(text: str) -> str:
    """``"<url> is reachable"`` -> ``"<url> is open"``.

    Reachability is what step 1 proves by opening the page; repeating it
    as prior state tells the tester nothing they are not about to do.
    """
    if not text:
        return text or ""

    def _at(match: re.Match) -> str:
        return f"{match.group('url')} is open{match.group('tail')}"

    out = _REACHABLE_AT_RE.sub(_at, text)
    out = _REACHABLE_RE.sub(lambda m: f"{m.group(1)} open", out)
    return out


# ── 2. One fact per line ─────────────────────────────────────────────

# Words that end in a period without ending a sentence. Without these,
# "malformed values (e.g. invalid email)" splits at "e.g.".
_ABBREVIATIONS = {
    "e.g", "i.e", "etc", "vs", "cf", "no", "fig", "approx", "resp",
    "incl", "min", "max", "sec", "ms", "vol", "ver", "st", "mr", "mrs",
    "dr", "inc", "ltd", "co", "al",
}

# A marker this module wrote, or a hand-numbered field it is re-reading:
# "1. ", "2) ", "3 - ".
_ITEM_MARKER_RE = re.compile(r"^\s*\d+\s*[.)\-:]\s+")
_ALREADY_NUMBERED_RE = re.compile(r"^\s*1\s*[.)\-:]\s+\S")

# Splitting is only worth it when both halves are sentences; a
# six-character fragment is punctuation noise, not a fact.
_MIN_FACT_LENGTH = 12


def _quotes_balanced(text: str) -> bool:
    """True when a position is not inside a quoted string.

    Expected results quote the product's own messages, and those carry
    sentence-final periods of their own: 'The "You must define an Applied
    Job." warning is displayed' has to stay one fact.
    """
    return text.count('"') % 2 == 0 and text.count("“") == text.count("”")


def _preceding_word(text: str, end: int) -> str:
    chunk = text[:end].rstrip(".;")
    return re.split(r"[\s(]", chunk)[-1].lower() if chunk else ""


def split_facts(text: str) -> list[str]:
    """One field as the list of independent facts it states.

    Splits on explicit separators (a newline, ``|``, an existing number)
    and on sentence ends. A semicolon may be followed by a lowercase word
    — it joins two clauses — while a period must be followed by something
    that can open a sentence, or it is an abbreviation or a decimal.
    """
    text = (text or "").strip()
    if not text:
        return []

    # Already a list: re-read it rather than re-split its sentences, so a
    # numbered fact that happens to contain two sentences stays one fact.
    if "\n" in text or _ALREADY_NUMBERED_RE.match(text) or " | " in text:
        parts = re.split(r"\n+|\s+\|\s+", text)
        if len(parts) == 1:
            parts = re.split(r"(?<=\s)(?=\d+\s*[.)]\s+\S)", text)
        items = [_ITEM_MARKER_RE.sub("", p).strip() for p in parts]
        items = [i for i in items if i]
        if len(items) > 1:
            return items
        text = items[0] if items else text

    facts: list[str] = []
    start = 0
    for match in re.finditer(r"[.;]\s+", text):
        cut = match.start()
        piece = text[start:cut].strip()
        if len(piece) < _MIN_FACT_LENGTH:
            continue
        if not _quotes_balanced(text[:cut]):
            continue
        if text[cut] == ".":
            if _preceding_word(text, cut) in _ABBREVIATIONS:
                continue
            nxt = text[match.end():match.end() + 1]
            # A sentence opens with a capital, a digit, a quote or a
            # bracket. Anything else is an abbreviation this module has
            # not met yet, and running two facts together beats cutting
            # one in half.
            if not nxt or not (nxt.isupper() or nxt.isdigit()
                               or nxt in "\"“'(["):
                continue
        facts.append(piece)
        start = match.end()
    tail = text[start:].strip()
    if tail:
        facts.append(tail)
    return [f for f in (x.strip() for x in facts) if f]


# A fact that opens with a URL, a snake_case identifier or a host name
# keeps its case: capitalising "testfort.com" names a different host to a
# reader skimming the column.
_KEEPS_CASE_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|[\w-]+\.[a-z]{2,}|_)")


def _open_capital(fact: str) -> str:
    if not fact or not fact[0].islower():
        return fact
    if _KEEPS_CASE_RE.match(fact):
        return fact
    return fact[0].upper() + fact[1:]


def as_numbered_list(text: str) -> str:
    """A multi-fact field rendered the way the steps column is.

    A single fact is returned as it came in: numbering one line makes a
    list of one, which is noise.
    """
    facts = split_facts(text)
    if len(facts) < 2:
        return (text or "").strip()
    return "\n".join(
        f"{i}. {_open_capital(fact.rstrip('.;').strip())}"
        for i, fact in enumerate(facts, start=1))


# ── 3. Test Data is for credentials ──────────────────────────────────
#
# Everything else the reference corpus puts in the title and repeats in
# the steps ("with Deadline in the past", 'Submit the query: "x"'), where
# a tester reads it while executing. A column that lists the field names
# the steps already name is a second place to keep in sync.
_CREDENTIAL_RE = re.compile(
    r"\b(?:"
    r"passwords?|passphrases?|credentials?|username|user\s?name|"
    r"logins?|sign[-\s]?in\s+(?:details|data)|"
    r"api[\s_-]?keys?|access\s+keys?|secret\s+keys?|tokens?|"
    r"otp|one[-\s]time\s+(?:code|password)|2fa|mfa|"
    r"pin\s*(?:code)?|card\s+numbers?|cvv|cvc|iban|account\s+numbers?|"
    r"test\s+accounts?|service\s+accounts?"
    r")\b",
    re.IGNORECASE,
)

_EMPTY_MARKERS = {"-", "—", "n/a", "none", "–"}


def credentials_only(text: str) -> str:
    """Keep Test Data when it carries credentials; drop it otherwise."""
    text = (text or "").strip()
    if not text or text.lower() in _EMPTY_MARKERS:
        return ""
    return text if _CREDENTIAL_RE.search(text) else ""


# ── 4. The expected result is written with "should" ──────────────────

# Only the first copula in a fact is rewritten: it is the one the
# assertion hangs on. A second "is" is almost always a subordinate clause
# stating a condition ("… is displayed when the user is signed out"), and
# a condition is not something the product should do.
_COPULA_RE = re.compile(r"\b(is|are)(\s+not)?\b", re.IGNORECASE)
_HAS_MODAL_RE = re.compile(
    r"\b(should|must|shall|can|cannot|could|will|would|may|might)\b|can't",
    re.IGNORECASE,
)


def should_voice(text: str) -> str:
    """``"The field is highlighted"`` -> ``"The field should be highlighted"``.

    A fact that already carries a modal is left alone — "User cannot
    create the record" is the house title grammar, not something to
    modalise twice.
    """
    text = (text or "").strip()
    if not text or _HAS_MODAL_RE.search(text):
        return text
    for match in _COPULA_RE.finditer(text):
        if not _quotes_balanced(text[:match.start()]):
            continue            # inside a quoted product message
        replacement = "should not be" if match.group(2) else "should be"
        return text[:match.start()] + replacement + text[match.end():]
    return text


# ── The pass the generators run ──────────────────────────────────────

def format_preconditions(text: str) -> str:
    """Rules 1 and 2, in that order.

    The voice is deliberately left declarative: a precondition states what
    already holds before the tester starts, so "should" would be wrong
    there in a way it is not wrong in an expected result.
    """
    return as_numbered_list(open_not_reachable(text))


def format_expected_result(text: str) -> str:
    """Rules 4 and 2 — voice per fact, then one fact per line."""
    facts = split_facts(text)
    if not facts:
        return (text or "").strip()
    facts = [f for f in (should_voice(f.rstrip(".;").strip())
                         for f in facts) if f]
    if not facts:
        return (text or "").strip()
    if len(facts) == 1:
        # Prose, not a list of one. The sentence keeps the full stop it
        # arrived with, because this column is read as a sentence.
        only = facts[0]
        return f"{only}." if (text or "").rstrip().endswith(".") else only
    return "\n".join(f"{i}. {_open_capital(f)}"
                     for i, f in enumerate(facts, start=1))


__all__ = [
    "as_numbered_list", "credentials_only", "format_expected_result",
    "format_preconditions", "open_not_reachable", "should_voice",
    "split_facts",
]
