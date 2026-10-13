"""Text processing for platform search (ADR 0003 §4.5) — no LLM, no network.

Pipeline, in the order the query and the indexer both apply it::

    normalise → detect_lang → tokens → stems → translate (UA/RU → EN)
                                               └ appended, never replacing

Everything here is deterministic and pure Python. The only dependency is
``snowballstemmer`` (EN + RU); Ukrainian gets the small in-house suffix
stripper :func:`stem_ua`, because Snowball has no Ukrainian algorithm.

Also home of the BM25 tokeniser :mod:`engine.istqb_rag` has always used
(:func:`tokenize`, :data:`TOKEN_RE`, :data:`ISTQB_STOPWORDS`). It moved
here so search and the ISTQB retriever share one definition; its
behaviour is unchanged byte for byte, which ``tests/test_search_text.py``
pins against the regex and stop-word set it had before the move.

Lexicons
--------
``lexicon/xlate_ua.json`` and ``lexicon/xlate_ru.json`` map *words* (as a
QA analyst would write them) to an English canonical word. They are
stemmed at load time with the same stemmer the documents go through, so
the authors never have to think about stems. Cyrillic keys are stemmed
with **both** Cyrillic stemmers: a short phrase without і/ї/є/ґ or ы/э/ъ
cannot be told apart, and a dictionary hit should not depend on that
guess.

Bump :data:`LEXICON_VERSION` whenever a lexicon file or a stemming rule
changes: it is part of every stored document's ``content_hash`` and of the
embedder's ``vec_version``, so the bump is what invalidates the index.
"""
from __future__ import annotations

import json
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping

#: Part of ``content_hash`` and ``vec_version``. Bump on any change to the
#: lexicons, the stop-words, the stemmers or the tokeniser below.
LEXICON_VERSION = 1

LEXICON_DIR = Path(__file__).resolve().parent / "lexicon"


# ── The ISTQB BM25 tokeniser (moved from engine/istqb_rag.py) ─────────
#
# Do not "improve" these three: istqb_rag ranks a 2,826-chunk corpus with
# them and its thresholds (MIN_RELEVANCE) were tuned on exactly this
# behaviour. Search has its own tokeniser further down.

# Allow word characters from both Latin and Cyrillic alphabets.
TOKEN_RE = re.compile(r"[A-Za-zЀ-ӿ][A-Za-z0-9Ѐ-ӿ\-]{1,}",
                      re.UNICODE)

# Stop-words that drown out signal in BM25. Kept short — broad
# stop-word lists actually hurt recall on terminology-heavy queries.
ISTQB_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "is", "are", "of", "to", "and", "or", "in",
    "for", "on", "at", "with", "by", "as", "be", "it", "from", "this",
    "that", "these", "those", "what", "which", "how",
    "є", "та", "і", "у", "в", "що", "як", "це", "до", "на", "по",
    "the", "of",
})


def tokenize(text: str) -> list[str]:
    """The ISTQB retriever's tokeniser: Latin/Cyrillic words of 2+ chars,
    lower-cased, minus :data:`ISTQB_STOPWORDS`. No normalisation, no
    stemming — exactly what ``istqb_rag._tokenize`` always did."""
    return [t.lower() for t in TOKEN_RE.findall(text or "")
            if t.lower() not in ISTQB_STOPWORDS]


# ── Normalisation ─────────────────────────────────────────────────────

#: Every apostrophe a Ukrainian keyboard, a Word document or a phone
#: produces, folded to ASCII ``'``. "об’єкт" and "об'єкт" must be one word.
_APOSTROPHES = str.maketrans({c: "'" for c in "ʼ’‘`´′ʹ"})
_WS = re.compile(r"\s+")


def normalise(text: str) -> str:
    """NFKC, lower case, ``ё → е``, apostrophes folded, whitespace collapsed.

    NFKC first so full-width and compatibility forms (``ＬＯＧＩＮ``, ``ﬁ``)
    become plain letters before anything compares them.
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", text).lower()
    out = out.replace("ё", "е").translate(_APOSTROPHES)
    return _WS.sub(" ", out).strip()


# ── Language detection ────────────────────────────────────────────────

_UA_ONLY = frozenset("іїєґ")
_RU_ONLY = frozenset("ыэъё")
_CYR = re.compile(r"[а-яёіїєґ]")
_LAT = re.compile(r"[a-z]")

#: Below this share of the letters, the minority script does not make a
#: text "mixed" — a Ukrainian bug title quoting one English button label is
#: still Ukrainian.
_MIXED_SHARE = 0.25


def detect_lang(text: str) -> str:
    """``"en" | "ua" | "ru" | "mixed"`` from script and marker letters.

    * No Cyrillic → ``en`` (also for an empty string).
    * Cyrillic with і/ї/є/ґ → ``ua``; with ы/э/ъ/ё → ``ru``; with both →
      ``mixed``.
    * Cyrillic with neither marker → ``ru``. Ukrainian prose of more than a
      couple of words almost always contains an і; Russian never does.
    * Both scripts each above a quarter of the letters → ``mixed``.

    Works on raw or normalised text: ё is checked before normalisation
    would have folded it.
    """
    if not text:
        return "en"
    low = unicodedata.normalize("NFKC", text).lower()
    cyr = len(_CYR.findall(low))
    lat = len(_LAT.findall(low))
    if cyr == 0:
        return "en"
    letters = cyr + lat
    if lat and min(cyr, lat) / letters >= _MIXED_SHARE:
        return "mixed"
    if lat > cyr:
        return "en"
    chars = set(low)
    ua = bool(chars & _UA_ONLY)
    ru = bool(chars & _RU_ONLY)
    if ua and ru:
        return "mixed"
    if ua:
        return "ua"
    return "ru"


def _token_lang(token: str, doc_lang: str) -> str:
    """The language one token is stemmed as."""
    if not _CYR.search(token):
        return "en"
    chars = set(token)
    if chars & _UA_ONLY:
        return "ua"
    if chars & _RU_ONLY:
        return "ru"
    return doc_lang if doc_lang in ("ua", "ru") else "ru"


# ── Search tokeniser ──────────────────────────────────────────────────

#: A word is letters/digits, optionally joined by an inner apostrophe
#: (об'єкт, can't). Hyphens and underscores split: "e-mail" → "mail",
#: "drop-down" → "drop", "down"; IDs have their own fast path.
_WORD = re.compile(r"[0-9a-zа-яёіїєґ]+(?:'[0-9a-zа-яёіїєґ]+)*")

#: Function words in the three languages. Deliberately includes "не"/"not":
#: negation is noise to a retriever, and leaving it in made every
#: "не працює …" query look alike.
SEARCH_STOPWORDS: frozenset[str] = frozenset("""
a an the is are was were be been being am of to and or in into on at by for
with from as it its this that these those there here what which who whom how
when where why do does did not no can could should would will shall may might
must have has had i you he she we they me my your our their them his her
than then so if but also just very too any all each some such via per vs
etc i'm it's don't doesn't can't won't isn't aren't cannot unable
і й та а але або чи що як це цей ця ці те той та у в на до по з із зі за
від для про при над під між без не ні так же ж би б є був була було були
бути ще вже лише тільки якщо коли де чому хто який яка яке які його її їх
мій моя моє мої наш наша наше наші ваш ваша ваше ваші свій своя
и в во на с со к ко о об от до по за из из-за для при над под между без не
ни да же бы ли есть был была было были быть еще уже только если когда где
почему кто какой какая какое какие его ее их мой моя мое мои наш наша наше
наши ваш ваша ваше ваши свой своя это этот эта эти тот та то те как что
""".split())


#: English compounds QA text writes three ways ("log in", "log-in",
#: "login"). Joined before tokenising, because "in"/"out"/"on" are
#: stop-words and "log in" would otherwise reach the index as "log" — the
#: same word as "error log".
_COMPOUNDS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), repl) for pattern, repl in (
        (r"\blog[\s-]*(?:in|on)\b", "login"),
        (r"\blog[\s-]*(?:out|off)\b", "logout"),
        (r"\bsign[\s-]*in\b", "signin"),
        (r"\bsign[\s-]*up\b", "signup"),
        (r"\bsign[\s-]*out\b", "signout"),
        (r"\be-mail", "email"),
        (r"\bdrop[\s-]*down\b", "dropdown"),
        (r"\bpop[\s-]*up\b", "popup"),
        (r"\bcheck[\s-]*box\b", "checkbox"),
    ))


def search_tokens(text: str) -> list[str]:
    """Normalised word tokens for search, stop-words removed, 2+ chars
    (single digits are kept: "step 3", "error 5")."""
    norm = normalise(text)
    for pattern, repl in _COMPOUNDS:
        norm = pattern.sub(repl, norm)
    out: list[str] = []
    for tok in _WORD.findall(norm):
        if tok in SEARCH_STOPWORDS:
            continue
        if len(tok) < 2 and not tok.isdigit():
            continue
        out.append(tok)
    return out


# ── Stemming ──────────────────────────────────────────────────────────

_STEMMER_LOCK = threading.Lock()
_STEMMERS: dict[str, object] = {}


def _snowball(lang: str):
    """A cached Snowball stemmer. Imported lazily: the ISTQB path imports
    this module for :func:`tokenize` and never needs one."""
    stemmer = _STEMMERS.get(lang)
    if stemmer is None:
        with _STEMMER_LOCK:
            stemmer = _STEMMERS.get(lang)
            if stemmer is None:
                import snowballstemmer
                stemmer = snowballstemmer.stemmer(lang)
                _STEMMERS[lang] = stemmer
    return stemmer


#: Ukrainian inflectional suffixes, longest first. The first suffix whose
#: removal leaves at least :data:`_UA_MIN_STEM` letters is stripped — once.
#: Light by design: an aggressive stemmer conflates more QA terms than it
#: unites, and the dictionary covers the irregular forms (вхід/входу).
_UA_SUFFIXES: tuple[str, ...] = tuple(sorted({
    # verbs. Deliberately no bare infinitive (-ати/-ити/-ти) or past-tense
    # (-ла/-ли/-ала/-ив) endings: they collide with the noun forms QA titles
    # are made of — "оплати" (of payment) is not "опл", "файли" is not
    # "фай". Verbs a search needs are in the dictionary by exact form.
    "уватися", "юватися", "ування", "ювання", "увати", "ювати", "ватися",
    "ватись", "ання", "яння", "ення", "іння", "ться", "тися", "тись",
    "ують", "юють", "ають", "яють", "уєте", "уємо", "уєш",
    "ють", "уть", "ять", "ать", "ить",
    "емо", "ємо", "имо", "ете", "єте", "ите", "іть", "ує", "ює", "ає", "яє",
    # nouns and adjectives
    "ість", "ості", "істю", "ами", "ями", "ові", "еві", "ого", "ому",
    "ими", "іми", "іх", "их", "ою", "ею", "єю", "ією", "ію", "ія", "ії",
    "ий", "ій", "ої", "ах", "ях", "ам", "ям", "ом", "ем", "ів", "їв", "ей",
    "а", "я", "о", "е", "є", "у", "ю", "і", "и", "ї", "й", "ь",
}, key=lambda s: (-len(s), s)))

_UA_MIN_STEM = 3


@lru_cache(maxsize=20_000)
def stem_ua(word: str) -> str:
    """Light Ukrainian stemmer: strip one inflectional suffix, then a soft
    sign. ``оплатою → оплат``, ``реєстрації → реєстрац``, ``паролем →
    парол``, ``працює → прац``. Words under four letters are untouched."""
    if len(word) < 4:
        return word
    for suffix in _UA_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= _UA_MIN_STEM:
            word = word[: -len(suffix)]
            break
    if word.endswith("ь") and len(word) > _UA_MIN_STEM:
        word = word[:-1]
    return word


@lru_cache(maxsize=20_000)
def stem(token: str, lang: str = "en") -> str:
    """Stem one normalised token. *lang* is the document's language; each
    token's own script decides the stemmer, so an English word inside a
    Ukrainian sentence is stemmed as English."""
    tl = _token_lang(token, lang)
    if tl == "ua":
        return stem_ua(token)
    if tl == "ru":
        return _snowball("russian").stemWord(token)
    if token.isdigit():
        return token
    return _snowball("english").stemWord(token)


def _cyrillic_stems(word: str) -> set[str]:
    """Both Cyrillic stems of *word*: used to index lexicon keys whose
    language a short query cannot reveal."""
    return {stem_ua(word), _snowball("russian").stemWord(word)}


# ── Lexicons ──────────────────────────────────────────────────────────

def _read_json(name: str) -> dict:
    with (LEXICON_DIR / name).open(encoding="utf-8") as fh:
        return json.load(fh)


@dataclass(frozen=True)
class _Xlate:
    """One language's dictionary: exact normalised word and its stems,
    each mapping to an English canonical phrase."""
    by_word: Mapping[str, str]
    by_stem: Mapping[str, str]


def _build_xlate(name: str) -> _Xlate:
    raw = _read_json(name)
    entries = raw.get("entries", raw)
    by_word: dict[str, str] = {}
    by_stem: dict[str, str] = {}
    for key, value in entries.items():
        if key.startswith("_"):
            continue
        word = normalise(key)
        target = normalise(value)
        # Single words only: the lookup is per token, so a phrase key could
        # never match and would only pollute the stem table.
        if not word or not target or " " in word:
            continue
        by_word.setdefault(word, target)
        for s in _cyrillic_stems(word):
            # First writer wins: the files list the most specific sense
            # of an ambiguous stem first.
            by_stem.setdefault(s, target)
    return _Xlate(by_word=by_word, by_stem=by_stem)


@lru_cache(maxsize=1)
def xlate_tables() -> dict[str, _Xlate]:
    """``{"ua": …, "ru": …}``, loaded once per process."""
    return {"ua": _build_xlate("xlate_ua.json"),
            "ru": _build_xlate("xlate_ru.json")}


@lru_cache(maxsize=20_000)
def translate_word(token: str, lang: str = "ua") -> str | None:
    """English canonical for one normalised Cyrillic token, or ``None``.

    Exact word first, then stem, in the token's own language and then the
    other Cyrillic dictionary — "вход" is Russian, but a Ukrainian user
    types it too.
    """
    if not _CYR.search(token):
        return None
    tl = _token_lang(token, lang)
    order = ("ua", "ru") if tl == "ua" else ("ru", "ua")
    tables = xlate_tables()
    for code in order:
        hit = tables[code].by_word.get(token)
        if hit:
            return hit
    stems = _cyrillic_stems(token)
    primary = stem_ua(token) if tl == "ua" else _snowball("russian").stemWord(token)
    for code in order:
        hit = tables[code].by_stem.get(primary)
        if hit:
            return hit
        for s in stems:
            hit = tables[code].by_stem.get(s)
            if hit:
                return hit
    return None


def translate(tokens: Iterable[str], lang: str = "ua"
              ) -> tuple[list[str], dict[str, str]]:
    """Translate UA/RU tokens to English.

    Returns ``(english_tokens, mapping)``. ``english_tokens`` holds the
    English words for every token that had a translation (multi-word
    targets are split), in order; ``mapping`` is ``{original: english}``
    for the "Translated: вхід → login" line in the UI. Tokens without a
    translation, and Latin tokens, are not in either.
    """
    english: list[str] = []
    mapping: dict[str, str] = {}
    for tok in tokens:
        hit = translate_word(tok, lang)
        if not hit:
            continue
        mapping.setdefault(tok, hit)
        english.extend(hit.split())
    return english, mapping


def format_translation(mapping: Mapping[str, str]) -> str:
    """``"вхід → login, оплата → payment"`` — the UI's "Translated:" line."""
    return ", ".join(f"{src} → {dst}" for src, dst in mapping.items())


# ── Analysis: everything the indexer and the query need, in one pass ──

@dataclass
class Analysis:
    """The result of running :func:`analyse` over one text."""
    text: str
    lang: str
    tokens: list[str] = field(default_factory=list)
    stems: list[str] = field(default_factory=list)
    #: English words the UA/RU tokens translate to (unstemmed) …
    translated: list[str] = field(default_factory=list)
    #: … and their English stems, aligned with ``translated``.
    translated_stems: list[str] = field(default_factory=list)
    #: ``{original token: english}`` for the UI.
    mapping: dict[str, str] = field(default_factory=dict)
    #: For each token, the stems of its translation (``[]`` when none).
    #: Lets the embedder count one concept hit per *word*, not per stream.
    token_translations: list[list[str]] = field(default_factory=list)

    @property
    def all_stems(self) -> list[str]:
        """Original stems followed by the translated English stems —
        appended, never replacing, so Cyrillic data is still found."""
        return self.stems + self.translated_stems

    def lex(self) -> str:
        """The space-joined term string stored in ``lex_title``/``lex_body``
        and fed to Postgres ``to_tsvector('simple', …)``."""
        return " ".join(self.all_stems)


def analyse(text: str, lang: str | None = None) -> Analysis:
    """Normalise, detect, tokenise, stem and translate *text*."""
    norm = normalise(text)
    lang = lang or detect_lang(text)
    result = Analysis(text=norm, lang=lang)
    for tok in search_tokens(norm):
        result.tokens.append(tok)
        result.stems.append(stem(tok, lang))
        hit = translate_word(tok, lang)
        per_token: list[str] = []
        if hit:
            result.mapping.setdefault(tok, hit)
            for word in hit.split():
                if word in SEARCH_STOPWORDS:
                    continue
                s = stem(word, "en")
                result.translated.append(word)
                result.translated_stems.append(s)
                per_token.append(s)
        result.token_translations.append(per_token)
    return result


def lex_terms(text: str, lang: str | None = None) -> str:
    """Shortcut: the stored lexical form of *text*."""
    return analyse(text, lang).lex()


# ── ID fast path ──────────────────────────────────────────────────────

_ID_RE = re.compile(r"^\s*(TC|CL|BUG|RUN)\s*[-_#]?\s*(\d+)\s*$", re.I)


def parse_id_query(query: str) -> tuple[str, int] | None:
    """``"bug-7"`` / ``"TC012"`` / ``"run #3"`` → ``("BUG", 7)`` etc.

    The ID fast path (ADR 0003 §4.5): a query that *is* an id is an exact
    lookup on ``ref``, pinned to the top. Anything else returns ``None``.
    """
    m = _ID_RE.match(query or "")
    if not m:
        return None
    return m.group(1).upper(), int(m.group(2))


#: A team's own ids — "LGN_010", "SC2-7", "E2E_002": letters first, a
#: separator, a digit last, one word. TC/CL/BUG/RUN are taken first by
#: :func:`parse_id_query`.
_REF_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*[-_][A-Za-z0-9_-]*\d)\s*$")
REF_QUERY_MAX = 40


def parse_ref_query(query: str) -> str | None:
    """``"lgn_010"`` → ``"LGN_010"``: a query that is one of the team's own
    ids is an exact lookup on ``ref`` (the item's external id), pinned to
    the top like the TC/CL/BUG/RUN fast path. Without it the lexical side
    splits the id into "lgn" and "010", and ``SC2_010`` outranks the item
    that is literally called ``LGN_010``."""
    if parse_id_query(query) is not None:
        return None
    m = _REF_RE.match(query or "")
    if not m or len(m.group(1)) > REF_QUERY_MAX:
        return None
    return m.group(1).upper()


# ── Typo correction ───────────────────────────────────────────────────

def levenshtein(a: str, b: str, limit: int | None = None) -> int:
    """Edit distance. With *limit*, returns ``limit + 1`` as soon as the
    distance is known to exceed it (the common case for candidates)."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if limit is not None and len(a) - len(b) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        row_min = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            val = min(previous[j] + 1, current[j - 1] + 1,
                      previous[j - 1] + cost)
            current.append(val)
            if val < row_min:
                row_min = val
        if limit is not None and row_min > limit:
            return limit + 1
        previous = current
    return previous[-1]


def trigrams(word: str) -> set[str]:
    """Character trigrams of ``#word#``."""
    padded = f"#{word}#"
    return {padded[i:i + 3] for i in range(len(padded) - 2)}


class TypoCorrector:
    """"Did you mean" over a supplied vocabulary (ADR 0003 §4.5).

    *vocabulary* is either an iterable of words or a ``{word: df}`` mapping;
    with frequencies, a tie on distance goes to the commoner word. The
    trigram index narrows the candidates, Levenshtein decides: distance ≤ 1
    for words of up to 4 letters (a two-edit "fix" of a short word is a
    different word), ≤ 2 above that.
    """

    #: Candidates checked with Levenshtein per token, best trigram overlap
    #: first. Bounds the cost on a 20k-term vocabulary.
    MAX_CANDIDATES = 60

    def __init__(self, vocabulary: Iterable[str] | Mapping[str, int]):
        if isinstance(vocabulary, Mapping):
            self.freq: dict[str, int] = {w: int(n) for w, n in vocabulary.items()
                                         if w}
        else:
            self.freq = {}
            for w in vocabulary:
                if w:
                    self.freq[w] = self.freq.get(w, 0) + 1
        self._index: dict[str, list[str]] = {}
        for word in self.freq:
            for tri in trigrams(word):
                self._index.setdefault(tri, []).append(word)

    def __contains__(self, word: str) -> bool:
        return word in self.freq

    @staticmethod
    def max_distance(word: str) -> int:
        return 1 if len(word) <= 4 else 2

    def suggest(self, word: str) -> str | None:
        """The closest vocabulary word, or ``None`` (also when *word* is
        already in the vocabulary, or shorter than three letters)."""
        if not word or word in self.freq or len(word) < 3:
            return None
        limit = self.max_distance(word)
        overlap: dict[str, int] = {}
        for tri in trigrams(word):
            for cand in self._index.get(tri, ()):
                if abs(len(cand) - len(word)) <= limit:
                    overlap[cand] = overlap.get(cand, 0) + 1
        ranked = sorted(overlap, key=lambda c: (-overlap[c], c))
        best: tuple[int, int, str] | None = None
        for cand in ranked[: self.MAX_CANDIDATES]:
            dist = levenshtein(word, cand, limit)
            if dist > limit:
                continue
            key = (dist, -self.freq.get(cand, 0), cand)
            if best is None or key < best:
                best = key
        return best[2] if best else None

    def correct(self, tokens: Iterable[str]) -> dict[str, str]:
        """``{typo: fix}`` for every token missing from the vocabulary that
        has a close enough neighbour. Digits are never "corrected"."""
        out: dict[str, str] = {}
        for tok in tokens:
            if tok in out or tok.isdigit():
                continue
            fix = self.suggest(tok)
            if fix:
                out[tok] = fix
        return out


def did_you_mean(tokens: Iterable[str],
                 vocabulary: Iterable[str] | Mapping[str, int]
                 ) -> dict[str, str]:
    """One-shot form of :meth:`TypoCorrector.correct`."""
    return TypoCorrector(vocabulary).correct(tokens)


__all__ = [
    "LEXICON_VERSION", "TOKEN_RE", "ISTQB_STOPWORDS", "tokenize",
    "normalise", "detect_lang", "search_tokens", "SEARCH_STOPWORDS",
    "stem", "stem_ua", "translate", "translate_word", "format_translation",
    "Analysis", "analyse", "lex_terms", "parse_id_query", "parse_ref_query",
    "levenshtein", "trigrams", "TypoCorrector", "did_you_mean",
    "xlate_tables",
]
