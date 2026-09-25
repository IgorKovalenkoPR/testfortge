"""TestForTge — the shape of a written test case, in one place.

``qa_team_lead.review_test_cases`` runs this over every generated pack, so
the conventions below hold whoever wrote the case: the LLM author, the
deterministic rule engine, the knowledge-base packs, or a flow template.

They come from two operator reviews of generated output on 2026-09-14 —
the ``SC2_004`` card on staging, then the pack regenerated from it — and
each one is a rule about a *column*, not about a sentence:

1. The page the case starts on is declared in the Preconditions, in
   EXACTLY one wording: ``<absolute URL with scheme> is open``. Not "is
   reachable", "is loaded", "is available", "User can reach <url>", and
   never a bare host. Three spellings of this one fact shipped inside a
   single pack; a deliverable carries one, and so does a linter.
2. That entry point is stated ONCE. A step that only opens the page the
   preconditions already declare open is deleted, and the steps begin at
   the first real action. See :func:`drop_redundant_entry_step` for what
   this cost and how the loss was covered.
3. Preconditions and Expected Result are numbered lists when they carry
   more than one fact, exactly as the steps column already is. One fact
   per line is what lets a failed row point at a single line.
4. Test Data is filled only when the case needs credentials. Everything
   else the reference corpus encodes in the title and the steps, where the
   tester reads it while executing, instead of in a column they have to
   join by eye.
5. Expected results are written with "should" / "should be" rather than
   the declarative "is" / "are".
6. A control label is never listed twice in one step — see
   :func:`dedupe_labels`.

Rule 5 continues the ruling recorded at ``tc_author._WEAK_MODAL_RE``:
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


# ── 1b. One canonical way to say "the page is open" ──────────────────
#
# The operator's review of 2026-09-14 found three spellings of the same
# fact inside one pack — "testfort.com is open.", "User can reach
# https://testfort.com from a fresh browser session.", and
# "https://testfort.com is loaded." A deliverable cannot carry three, and
# neither can a linter: a synonym defeats every grep-based duplicate check
# across a four-thousand-row plan. There is exactly one form:
#
#     <absolute URL, with scheme> is open
#
# It is also the string the automation codegen binds a `goto` to, which is
# only safe because this module — not an author, not the model — writes it
# from one template. See engine/automation_codegen.py ACTION_BINDINGS.
CANONICAL_OPEN = "{url} is open"

_URL_RE = re.compile(
    r"\b(?:https?://[^\s\"'<>,;)]+"
    r"|(?<![\w.@-])(?:[\w-]+\.)+(?:com|org|net|io|dev|app|ua|co|uk|gov|edu)"
    r"(?:/[^\s\"'<>,;)]*)?)",
    re.IGNORECASE,
)

# Verbs that all mean "the page can be looked at".
_AVAILABILITY_RE = re.compile(
    r"\b(?:is|are)\s+(?:open|opened|loaded|reachable|available|accessible)\b"
    r"|\bcan\s+(?:be\s+)?reach(?:ed)?\b"
    r"|\bcan\s+reach\b",
    re.IGNORECASE,
)

# Words that carry no fact once the URL and the verb are removed. If
# nothing else survives, the sentence was only ever saying "the page is
# open" and is replaced wholesale by the canonical form; if something does
# survive ("and the browser cache is cleared"), only the verb and the URL
# are rewritten, because the rest is a real precondition.
_FILLER = {
    "a", "an", "the", "it", "its", "this", "that",
    "site", "website", "page", "url", "address", "application", "app",
    "system", "product", "under", "test", "user", "users", "browser",
    "session", "fresh", "clean", "new", "default", "public", "internet",
    "in", "on", "at", "of", "from", "for", "to", "with", "over", "via",
    "and", "is", "are", "be", "been", "open", "opened", "loaded",
    "reachable", "available", "accessible", "can", "reach", "reached",
    "successfully", "correctly", "properly", "first", "initial",
}


def absolute_url(url: str) -> str:
    """``"testfort.com"`` -> ``"https://testfort.com"``.

    A bare host silently assumes a scheme, and the assumed one differs
    between a staging box and production — so the deliverable states it.
    """
    url = (url or "").strip().rstrip(".,;")
    if not url:
        return url
    if re.match(r"^[a-z][a-z0-9+.-]*://", url, re.IGNORECASE):
        return url
    return f"https://{url}"


def first_url(text: str) -> str:
    """The first URL or bare host in *text*, absolutised. ``""`` if none."""
    match = _URL_RE.search(text or "")
    return absolute_url(match.group(0)) if match else ""


def _is_only_page_open(clause: str) -> bool:
    """True when *clause* says nothing but "this page is open"."""
    remainder = _AVAILABILITY_RE.sub(" ", _URL_RE.sub(" ", clause))
    words = [w for w in re.split(r"[^\w-]+", remainder.lower()) if w]
    return bool(words is not None) and all(w in _FILLER for w in words)


def canonical_page_facts(fact: str) -> list[str]:
    """One precondition fact, in the house's single page-open wording.

    Returns a list because a sentence often welds the page-open fact to a
    real precondition — "https://x.com is open **and** the browser cache
    is cleared". Those are two facts, and the column now numbers them.
    A fact that is not about a page being open comes back unchanged.
    """
    fact = (fact or "").strip()
    if not fact:
        return []
    match = _URL_RE.search(fact)
    if not match or not _AVAILABILITY_RE.search(fact):
        return [fact]
    url = absolute_url(match.group(0))

    if _is_only_page_open(fact):
        return [CANONICAL_OPEN.format(url=url)]

    # "<page is open> and <something else>" — split rather than reword, so
    # the canonical sentence survives intact and the rest keeps its meaning.
    for cut, _ in _cut_points_on(fact, r"\s+and\s+"):
        left, right = fact[:cut].strip(), fact[cut:].strip()
        right = re.sub(r"^and\s+", "", right, flags=re.IGNORECASE).strip()
        # A right-hand clause that opens with a verb is sharing the left's
        # subject ("… is reachable and is not blocked from indexing"), and
        # cutting there strands a predicate with nothing to attach to.
        if re.match(r"(?:is|are|was|were|does|do|has|have|can|could|"
                    r"should|must|will|exposes|serves|carries|holds)\b",
                    right, re.IGNORECASE):
            continue
        if left and right and _is_only_page_open(left) \
                and _AVAILABILITY_RE.search(left):
            return [CANONICAL_OPEN.format(url=url),
                    right[:1].upper() + right[1:]]

    # It carries something else in a shape this cannot safely split — keep
    # it, but say the shared part the one sanctioned way.
    out = _AVAILABILITY_RE.sub("is open", fact, count=1)
    return [out.replace(match.group(0), url, 1).strip()]


def _cut_points_on(text: str, pattern: str) -> list[tuple[int, int]]:
    """Matches of *pattern* in *text* that sit outside quotes and brackets."""
    legal = {start for start, _ in _cut_points(text)}
    out: list[tuple[int, int]] = []
    depth = 0
    in_quote = False
    for m in re.finditer(pattern, text, re.IGNORECASE):
        prefix = text[:m.start()]
        in_quote = prefix.count(_PAIRED_QUOTE) % 2 == 1
        depth = sum(prefix.count(o) - prefix.count(c)
                    for o, c in _OPENERS.items())
        if not in_quote and depth <= 0 and m.start() not in legal:
            out.append((m.start(), m.end()))
    return out


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


# Openers and closers tracked while scanning for a split point. A cut is
# only legal at depth zero — outside every quote and bracket.
#
# This is a scanner and not a set of regexes because the defect it fixes
# was a regex that could not see depth: a page title quoted inside an
# expected result, `("Software Testing Solutions | Manual, Auto, AI")`,
# contains " | ", which was the pipe separator this module splits on. The
# assertion was cut in half mid-quote and shipped as two numbered facts,
# the second of which began `Manual, Auto, AI") and H1 …`.
_OPENERS = {"(": ")", "[": "]", "“": "”", "«": "»"}
_CLOSERS = {v: k for k, v in _OPENERS.items()}

_PAIRED_QUOTE = '"'


def _quotes_balanced(text: str) -> bool:
    """True when the END of *text* is not inside a quoted span.

    Used to decide whether a match sits inside a quoted product message.
    The scanner in :func:`_cut_points` tracks brackets as well; this one
    only needs quotes, because the thing it protects — the copula rewrite
    — is only ever wrong inside quoted text.
    """
    return text.count(_PAIRED_QUOTE) % 2 == 0 and \
        text.count("“") == text.count("”")


def _cut_points(text: str) -> list[tuple[int, int]]:
    """Legal split points as ``(start, end)`` spans of the separator.

    Walks the string once, tracking bracket depth and double-quote parity,
    and offers a cut only at depth zero. Separators, strongest first: a
    newline, " | ", "; ", and a sentence-ending ". ".
    """
    cuts: list[tuple[int, int]] = []
    depth = 0
    in_quote = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == _PAIRED_QUOTE:
            in_quote = not in_quote
            i += 1
            continue
        if not in_quote:
            if ch in _OPENERS:
                depth += 1
                i += 1
                continue
            if ch in _CLOSERS:
                depth = max(0, depth - 1)
                i += 1
                continue
        if depth == 0 and not in_quote:
            if ch == "\n":
                j = i
                while j < n and text[j] in "\r\n \t":
                    j += 1
                cuts.append((i, j))
                i = j
                continue
            if text.startswith(" | ", i):
                cuts.append((i, i + 3))
                i += 3
                continue
            if ch in ".;" and i + 1 < n and text[i + 1] in " \t":
                j = i + 1
                while j < n and text[j] in " \t":
                    j += 1
                if _is_sentence_end(text, i, j):
                    cuts.append((i, j))
                    i = j
                    continue
        i += 1
    return cuts


def _is_sentence_end(text: str, dot: int, resume: int) -> bool:
    """True when the ``.`` or ``;`` at *dot* really ends a fact.

    A semicolon always does — it joins two independent clauses and the
    second may open lowercase. A period has to be followed by something
    that can start a sentence, and must not be closing an abbreviation.
    """
    if text[dot] == ";":
        return True
    word = re.split(r"[\s(\[\"]", text[:dot])[-1].lower()
    if word in _ABBREVIATIONS:
        return False
    nxt = text[resume:resume + 1]
    return bool(nxt) and (nxt.isupper() or nxt.isdigit()
                          or nxt in "\"“'([")


def split_facts(text: str) -> list[str]:
    """One field as the list of independent facts it states.

    Splits on a newline, on " | ", on "; " and on a sentence-ending ". ",
    but only where that separator sits outside every quote and bracket.
    An existing number ("1. ", "2) ") is a marker, not content, and is
    stripped from each fact.

    A fact that already carries its own numbering is still sentence-split:
    an earlier version returned such a list verbatim, which is how three
    assertions came back merged into one numbered line after the pipe
    defect above had cut the first one in half.
    """
    text = (text or "").strip()
    if not text:
        return []

    facts: list[str] = []
    start = 0
    for cut, resume in _cut_points(text):
        piece = text[start:cut].strip()
        if len(_ITEM_MARKER_RE.sub("", piece)) < _MIN_FACT_LENGTH:
            # Too short to be a fact of its own; let it run on rather than
            # shipping a fragment as a numbered line.
            continue
        facts.append(piece)
        start = resume
    tail = text[start:].strip()
    if tail:
        facts.append(tail)

    out = [_ITEM_MARKER_RE.sub("", f).strip() for f in facts]
    return [f for f in out if f]


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


# ── 1c. The entry point is stated once ───────────────────────────────
#
# A case used to say where it starts twice: `Preconditions: https://x.com
# is open` and `Steps: 1. Open https://x.com`. The operator's review of
# 2026-09-14 called it what it is — the same fact in two columns.
#
# The Preconditions keep it, and the step goes. Note that this is the
# opposite of what a strict reading of the reference corpus would do (the
# reviewing team lead's own note says "steps start from the main URL"),
# and the QA-manager review of 2026-09-14 argued for dropping the
# PRECONDITION instead. The operator ruled the other way, twice and
# explicitly. What the team lead's rule was protecting — a tester who can
# always find the entry point, and a case that does not rot when a deep
# link changes — is preserved by :func:`entry_point_url` writing that URL
# into ``TestCase.url_pattern``, which is also what every automation
# consumer now reads. See engine/automation_qa.py ``tc_to_script``.
# A trailing qualifier that says HOW the page is opened rather than WHAT
# else the step does. These are the ones the packs write, and each is
# already carried by a precondition of its own ("The viewport is
# 1280×800", "DevTools responsive mode is available"), so the step adds
# nothing. Anything outside this list — "Open <url> and one content page"
# — is a second action and the step stays.
_NAV_QUALIFIER = (
    r"(?:\s+(?:in|at|with|using)\s+"
    r"(?:a|the)?\s*(?:browser|browser\s+devtools[\w\s]*|\d+\s*[x×]\s*\d+"
    r"|devtools[\w\s]*)"
    r"|\s+at\s+\d+\s*[x×]\s*\d+)?"
)

_NAV_STEP_RE = re.compile(
    r"^\s*(?:open|go\s+to|navigate\s+to|visit|launch)\s+"
    r"(?:the\s+)?(?:url:?\s*)?(?P<url>\S+)"
    + _NAV_QUALIFIER + r"\s*\.?\s*$",
    re.IGNORECASE,
)

# The same step written without the URL: "Open the homepage in a browser".
# Only the generic nouns — "Open the login page" names a DIFFERENT page and
# is a real action even when the entry point is already declared.
_GENERIC_NAV_STEP_RE = re.compile(
    r"^\s*(?:open|go\s+to|navigate\s+to|visit|launch)\s+"
    r"(?:the\s+)?(?:homepage|home\s+page|page|site|website|application|app)"
    r"(?:\s+(?:in|with|using)\s+(?:a|the)\s+browser)?\s*\.?\s*$",
    re.IGNORECASE,
)


def entry_point_url(preconditions: str) -> str:
    """The URL a case starts from, read off its canonical precondition."""
    for fact in split_facts(preconditions or ""):
        match = re.match(r"^(?P<url>\S+)\s+is\s+open\s*\.?$", fact.strip(),
                         re.IGNORECASE)
        if match:
            return absolute_url(match.group("url"))
    return ""


def drop_redundant_entry_step(steps_blob: str, preconditions: str) -> str:
    """Remove a first step that only opens a page already declared open.

    Only the FIRST step, and only when its URL is the one the
    preconditions name — a mid-case navigation to a different page is a
    real action and stays.
    """
    from engine import tc_steps

    entry = entry_point_url(preconditions)
    if not entry:
        return steps_blob
    steps = tc_steps.parse(steps_blob)
    if len(steps) < 3:
        # Two steps minus the navigation is one step, and a one-step case
        # is a checklist item filed in the wrong pack. Where opening the
        # page IS most of what the case does — a display check — the step
        # carries its weight and stays.
        return steps_blob
    if _GENERIC_NAV_STEP_RE.match(steps[0]):
        return tc_steps.render(steps[1:])
    match = _NAV_STEP_RE.match(steps[0])
    if not match:
        return steps_blob
    if absolute_url(match.group("url").rstrip(".,;")) != entry:
        return steps_blob
    return tc_steps.render(steps[1:])


def dedupe_labels(labels) -> list[str]:
    """Distinct control labels, in the order the crawler saw them.

    A page routinely carries the same visible label twice — a desktop
    header and a mobile drawer both holding "Menu" — and the crawler
    reports both. Listed verbatim, the step reads `Locate each of the
    following controls: "Menu", "Menu", "Send", "Get a quote", "Send"`,
    which is what the operator caught on 2026-09-14: the label IS the
    locator in a manual case, so a repeated one names nothing and a
    failure against it cannot be triaged.

    De-duplication and not qualification ("Menu" in the header / in the
    drawer) because the crawler hands this module a flat list of strings
    with no structural context to qualify them WITH. Inventing one would
    put a region in the case that nobody verified.
    """
    seen: set[str] = set()
    out: list[str] = []
    for label in labels or []:
        text = str(label or "").strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


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
    """Rules 1, 1b and 2, in that order.

    The voice is deliberately left declarative: a precondition states what
    already holds before the tester starts, so "should" would be wrong
    there in a way it is not wrong in an expected result.
    """
    facts: list[str] = []
    for fact in split_facts(open_not_reachable(text)):
        facts.extend(canonical_page_facts(fact.rstrip(".;").strip()))
    facts = [f for f in facts if f]
    # A pack that mentions the same page twice in one cell says it once.
    seen: set[str] = set()
    unique: list[str] = []
    for fact in facts:
        key = fact.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(fact)
    if not unique:
        return ""
    if len(unique) == 1:
        return unique[0]
    return "\n".join(f"{i}. {_open_capital(f)}"
                     for i, f in enumerate(unique, start=1))


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
