"""Retrieval — the query pipeline of ADR 0003 §4.5. No LLM, no network.

::

    q → normalise / lang → ID fast path → tokens → stems → UA/RU→EN
      → did-you-mean (vocabulary of the scopes searched)
      → lexical list   PG: tsv @@ to_tsquery   | else: Python BM25
      → vector list    PG: pgvector <=>        | else: Python cosine (vec_blob)
      → RRF fuse → boosts → group by (project, kind, ref) → filters (+relax)
      → chips, snippets, "how this was found"

Tenancy (§4.2) is enforced, not advisory
----------------------------------------
Every statement that reads ``search_doc`` is built here and every one of
them takes its WHERE from :func:`_scope_clause` — global documents, plus the
project documents of the ids the *caller* computed from
``routes._shared.visible_projects`` / ``project_access_with_meta``. The
function refuses ``None`` (there is no "no filter" spelling of it), and an
empty list means "global documents only". Nothing in this module decides
who may see what; it only guarantees that what the route decided is applied
to every query. ``tests/test_search_tenancy.py`` counts the calls.

Determinism
-----------
Ranking uses only ranks (RRF, k=60), so SQLite's Python BM25 and Postgres's
``ts_rank_cd`` order results the same way up to their own list order. Ties
break on ``source_updated_at`` then ``doc_id``. The explanation, the chips
and the typo correction are pure functions of the index and the query.
"""
from __future__ import annotations

import html
import math
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from engine.log import get_logger

log = get_logger(__name__)

# ── Tunables (env, read per call so tests and the dashboard can move them) ──

#: Reciprocal-rank-fusion constant (ADR §4.5).
RRF_K = 60
#: Depth of each ranked list fed to the fusion.
LIST_DEPTH = 100
#: A vector neighbour below this cosine is not evidence of anything — the
#: trigram block alone gives unrelated short texts ~0.1.
DEFAULT_VEC_MIN = 0.22
#: ADR §4.10.
DEFAULT_PY_VECTOR_MAX_DOCS = 20_000
#: Boosts (ADR §4.5): pinned id, title match, open bug. The ADR sketched
#: +0.005 / +0.002; measured on the golden set, 0.005 is ~20 RRF ranks near
#: the top and buried checklist items, whose "title" is only a section name
#: (recall@5 0.927 → 0.958 at these values). Overridable with
#: SEARCH_BOOST_TITLE / SEARCH_BOOST_OPEN_BUG for the golden-set experiment.
BOOST_ID = 1.0
BOOST_TITLE = 0.002
BOOST_OPEN_BUG = 0.0005
#: Two adjacent query words adjacent in the document too ("Base URL",
#: "live executor"), counted at most twice; and a Guide section whose
#: multi-word module name the question spells out ("Test Execution").
#: Chosen on the ADR gate-3 questions with the golden set as the guard
#: (tests/test_search_ask_quality.py).
BOOST_PHRASE = 0.008
BOOST_MODULE = 0.01
#: BM25.
BM25_K1 = 1.2
BM25_B = 0.75
#: A title occurrence counts this many body occurrences.
TITLE_WEIGHT = 2.0
#: Quick mode (typeahead): results per group and in total.
QUICK_PER_GROUP = 3
QUICK_LIMIT = 8
#: Full mode: results per kind in ``groups``.
GROUP_SIZE = 5
#: At most this many projects are lag-checked per request.
MAX_LAG_CHECKS = 25

#: Facets a caller may filter on (besides kind and project).
FACET_FILTERS = ("status", "priority", "severity")

#: ID prefix → the kind whose ``ref`` carries it.
ID_KINDS = {"TC": "test_case", "CL": "checklist_item", "BUG": "bug",
            "RUN": "run"}

_OPEN_STATUSES = frozenset({"open", "new", "reopened", "in progress",
                            "in_progress", "active", "to do", "todo"})

_REF_NUM = re.compile(r"^(?:run:)?([A-Za-z]*)[-_#]?0*(\d+)$")


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


#: The hash vectors are a weak signal — lexical-only recall equals hybrid
#: on the golden set — and at equal weight any document in *both* lists
#: outranked the best lexical hit without a vector neighbour: the bug
#: titled "Live executor stopped early" came 12th for "live executor
#: stopping early" (ADR gate 3, 2026-10-15).
DEFAULT_W_VEC = 0.4


def weights() -> tuple[float, float]:
    """``(SEARCH_W_LEX, SEARCH_W_VEC)`` — 1.0 and :data:`DEFAULT_W_VEC`."""
    return (max(0.0, _env_float("SEARCH_W_LEX", 1.0)),
            max(0.0, _env_float("SEARCH_W_VEC", DEFAULT_W_VEC)))


def py_vector_max_docs() -> int:
    return int(max(0, _env_float("SEARCH_PY_VECTOR_MAX_DOCS",
                                 DEFAULT_PY_VECTOR_MAX_DOCS)))


def vec_min() -> float:
    return _env_float("SEARCH_VEC_MIN", DEFAULT_VEC_MIN)


# ── Tenancy ───────────────────────────────────────────────────────────

class TenancyError(RuntimeError):
    """A search was attempted without an explicit list of project ids."""


def _scope_clause(allowed: Sequence[str] | None, *,
                  include_global: bool = True):
    """The one WHERE every ``search_doc`` read goes through (ADR §4.2).

    ``(scope = 'global' AND project_id IS NULL) OR (scope = 'project' AND
    project_id IN (:allowed))``. *allowed* must be a list the caller
    computed from what the user may see; ``None`` raises, an empty list
    leaves only global documents (or nothing, with *include_global* off).
    """
    from sqlalchemy import and_, false, or_

    from engine.db import SearchDoc
    if allowed is None or isinstance(allowed, (str, bytes)):
        raise TenancyError("search requires an explicit list of project ids")
    ids = sorted({str(a) for a in allowed if a})
    conds = []
    if include_global:
        conds.append(and_(SearchDoc.scope == "global",
                          SearchDoc.project_id.is_(None)))
    if ids:
        conds.append(and_(SearchDoc.scope == "project",
                          SearchDoc.project_id.in_(ids)))
    if not conds:
        return false()
    return or_(*conds) if len(conds) > 1 else conds[0]


def _filtered(stmt, allowed, kinds, *, include_global=True):
    """*stmt* with the scope clause and, when given, the kind filter."""
    from engine.db import SearchDoc
    stmt = stmt.where(_scope_clause(allowed, include_global=include_global))
    if kinds:
        stmt = stmt.where(SearchDoc.kind.in_(sorted(kinds)))
    return stmt


# ── Query analysis ────────────────────────────────────────────────────

@dataclass
class Term:
    stem: str
    label: str          # what the explanation shows: "вхід→login", "logn→login"
    source: str         # "query" | "translation" | "typo"
    weight: float = 1.0


@dataclass
class ParsedQuery:
    raw: str
    normalised: str
    lang: str
    tokens: list[str]
    stems: list[str]
    translated: dict[str, str]
    corrections: dict[str, str]
    terms: list[Term]
    id_query: tuple[str, int] | None
    embed_text: str
    #: Per query word, every stem that stands for it (its own, its
    #: translation's, its correction's) — "how much of the query does this
    #: title cover" counts words, not stems.
    token_groups: list[set[str]] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    #: The same groups without what only a typo correction contributed —
    #: "слон→сон" is a guess, not a word the user wrote, so it is no
    #: evidence that the corpus knows what they asked about.
    direct_groups: list[set[str]] = field(default_factory=list)

    def covered_words(self, stems: set[str]) -> list[int]:
        """Indices of the query words *stems* match directly (own stem or
        its translation, never a typo fix)."""
        return [i for i, g in enumerate(self.direct_groups) if g & stems]

    @property
    def word_count(self) -> int:
        return sum(1 for g in self.token_groups if g)

    def title_coverage(self, title_stems: set[str]) -> float:
        groups = [g for g in self.token_groups if g]
        if not groups:
            return 0.0
        return sum(1 for g in groups if g & title_stems) / len(groups)

    @property
    def did_you_mean(self) -> str | None:
        if not self.corrections:
            return None
        out = self.normalised
        for typo, fix in self.corrections.items():
            out = re.sub(rf"(?<![\w']){re.escape(typo)}(?![\w'])", fix, out)
        return out if out != self.normalised else None

    @property
    def phrases(self) -> list[str]:
        """Adjacent query stems as ``" a b "`` — the stored lex keeps the
        document's word order (stop-words dropped on both sides), so
        "Base URL" in a question is found as "base url" in a page."""
        out: list[str] = []
        for a, b in zip(self.stems, self.stems[1:]):
            pair = f" {a} {b} "
            if a and b and a != b and pair not in out:
                out.append(pair)
        return out

    @property
    def stem_sequence(self) -> str:
        """The query's own stems in order, space-padded."""
        return f" {' '.join(s for s in self.stems if s)} "

    def term_stems(self) -> list[str]:
        seen: list[str] = []
        for t in self.terms:
            if t.stem not in seen:
                seen.append(t.stem)
        return seen


def parse_query(raw: str, corrector: "Vocabulary | None" = None
                ) -> ParsedQuery:
    from engine.search.text import (analyse, normalise, parse_id_query,
                                    stem, translate_word)
    norm = normalise(raw)
    a = analyse(raw)
    terms: list[Term] = []
    seen: set[str] = set()

    groups: list[set[str]] = [set() for _ in a.tokens]
    direct: list[set[str]] = [set() for _ in a.tokens]

    def add(stem_: str, label: str, source: str, w: float = 1.0,
            token: int | None = None, guessed: bool = False) -> None:
        if token is not None and stem_:
            groups[token].add(stem_)
            if not guessed:
                direct[token].add(stem_)
        if stem_ and stem_ not in seen:
            seen.add(stem_)
            terms.append(Term(stem_, label, source, w))

    for i, tok in enumerate(a.tokens):
        add(a.stems[i], tok, "query", token=i)
        for tr in a.token_translations[i]:
            word = a.mapping.get(tok, tr)
            add(tr, f"{tok}→{word}", "translation", token=i)

    corrections: dict[str, str] = {}
    if corrector is not None and not corrector.empty:
        for i, tok in enumerate(a.tokens):
            if tok.isdigit() or len(tok) < 3 or a.token_translations[i]:
                continue
            if corrector.knows(tok, a.stems[i]):
                continue
            fix = corrector.suggest(tok)
            if not fix or fix == tok:
                continue
            corrections[tok] = fix
            fix_stem = stem(fix, a.lang)
            add(fix_stem, f"{tok}→{fix}", "typo", token=i, guessed=True)
            hit = translate_word(fix, a.lang)
            if hit:
                for word in hit.split():
                    add(stem(word, "en"), f"{fix}→{word}", "translation",
                        token=i, guessed=True)
    embed_text = norm
    if corrections:
        embed_text = f"{norm} {' '.join(corrections.values())}"
    return ParsedQuery(
        raw=raw, normalised=norm, lang=a.lang, tokens=list(a.tokens),
        stems=list(a.stems), translated=dict(a.mapping),
        corrections=corrections, terms=terms,
        id_query=parse_id_query(raw), embed_text=embed_text,
        token_groups=groups, direct_groups=direct)


# ── "Did you mean" vocabulary, cached per scope version ───────────────

class Vocabulary:
    """The merged vocabulary of the scopes searched, with a
    :class:`~engine.search.text.TypoCorrector` over it and the set of its
    stems — a token whose *stem* is known ("passwords" when the data says
    "password") is not a typo."""

    def __init__(self, terms: Mapping[str, int]):
        from engine.search.text import TypoCorrector, stem
        self.terms = dict(terms)
        self.empty = False   # the dictionary fallback below always exists
        self._corrector = TypoCorrector(self.terms) if self.terms else None
        self.stems: set[str] = set()
        for w in self.terms:
            self.stems.add(stem(w, "en" if w.isascii() else "ua"))
            if not w.isascii():
                self.stems.add(stem(w, "ru"))

    def knows(self, token: str, token_stem: str) -> bool:
        return token in self.terms or token_stem in self.stems

    def suggest(self, token: str) -> str | None:
        """The project's own words first. Failing that, a word of the UA/RU
        dictionary at edit distance 1 — so a Cyrillic typo ("ркгистрация")
        becomes a word the dictionary translates even when the project's
        data is all English. Distance 1 only there: at 2, "занят" (taken)
        became "запит" (request)."""
        from engine.search.text import levenshtein
        fix = self._corrector.suggest(token) if self._corrector else None
        if fix:
            return fix
        if token.isascii():
            return None
        fix = _lexicon_corrector().suggest(token)
        if fix and levenshtein(token, fix, 1) <= 1:
            return fix
        return None


#: Merged vocabularies of more than this many terms keep the most frequent.
VOCAB_MERGE_MAX = 40_000
_VOCAB_CACHE: "OrderedDict[tuple, Vocabulary]" = OrderedDict()
_VOCAB_CACHE_SIZE = 4
_VOCAB_LOCK = threading.Lock()


def _scope_states(scope_keys: Iterable[str]) -> dict[str, int]:
    """``{scope_key: version}`` for the scopes that have state."""
    from engine import db as _db
    from engine.db import SearchScopeState
    keys = sorted({k for k in scope_keys if k})
    if not keys:
        return {}
    out: dict[str, int] = {}
    with _db.session_scope() as sess:
        for start in range(0, len(keys), 500):
            for k, v in sess.query(SearchScopeState.scope_key,
                                   SearchScopeState.version).filter(
                    SearchScopeState.scope_key.in_(keys[start:start + 500])):
                out[k] = int(v or 0)
    return out


def vocabulary_for(states: Mapping[str, int]) -> Vocabulary:
    """The cached vocabulary for exactly these ``{scope: version}`` (an LRU
    of :data:`_VOCAB_CACHE_SIZE`, ADR §4.11's "≤4 projects")."""
    key = tuple(sorted(states.items()))
    with _VOCAB_LOCK:
        hit = _VOCAB_CACHE.get(key)
        if hit is not None:
            _VOCAB_CACHE.move_to_end(key)
            return hit
    from engine import db as _db
    from engine.db import SearchScopeState
    merged: dict[str, int] = {}
    if states:
        with _db.session_scope() as sess:
            rows = sess.query(SearchScopeState.vocab_json).filter(
                SearchScopeState.scope_key.in_(sorted(states))).all()
        for (vj,) in rows:
            terms = (vj or {}).get("terms") if isinstance(vj, dict) else None
            if not isinstance(terms, dict):
                continue
            for w, n in terms.items():
                try:
                    merged[w] = merged.get(w, 0) + int(n)
                except (TypeError, ValueError):
                    continue
    if len(merged) > VOCAB_MERGE_MAX:
        keep = sorted(merged, key=lambda w: (-merged[w], w))[:VOCAB_MERGE_MAX]
        merged = {w: merged[w] for w in keep}
    vocab = Vocabulary(merged)
    with _VOCAB_LOCK:
        _VOCAB_CACHE[key] = vocab
        while len(_VOCAB_CACHE) > _VOCAB_CACHE_SIZE:
            _VOCAB_CACHE.popitem(last=False)
    return vocab


_LEXICON_CORRECTOR = None


def _lexicon_corrector():
    """A corrector over the UA/RU dictionary's own words, built once."""
    global _LEXICON_CORRECTOR
    if _LEXICON_CORRECTOR is None:
        from engine.search.text import TypoCorrector, xlate_tables
        words: set[str] = set()
        for table in xlate_tables().values():
            words.update(w for w in table.by_word if len(w) >= 3)
        _LEXICON_CORRECTOR = TypoCorrector(sorted(words))
    return _LEXICON_CORRECTOR


def clear_caches() -> None:
    with _VOCAB_LOCK:
        _VOCAB_CACHE.clear()


# ── Candidate rows ────────────────────────────────────────────────────

@dataclass
class Cand:
    id: int
    project_id: str | None
    kind: str
    ref: str
    chunk_no: int
    lex_title: str
    lex_body: str
    vec_version: str | None
    vec_blob: bytes | None
    source_updated_at: Any


def _load_candidates(allowed, kinds, *, with_vectors: bool) -> list[Cand]:
    from sqlalchemy import select

    from engine import db as _db
    from engine.db import SearchDoc
    cols = [SearchDoc.id, SearchDoc.project_id, SearchDoc.kind, SearchDoc.ref,
            SearchDoc.chunk_no, SearchDoc.lex_title, SearchDoc.lex_body,
            SearchDoc.vec_version, SearchDoc.source_updated_at]
    if with_vectors:
        cols.append(SearchDoc.vec_blob)
    stmt = _filtered(select(*cols), allowed, kinds)
    out: list[Cand] = []
    with _db.session_scope() as sess:
        for r in sess.execute(stmt):
            out.append(Cand(
                id=r[0], project_id=r[1], kind=r[2], ref=r[3], chunk_no=r[4],
                lex_title=r[5] or "", lex_body=r[6] or "", vec_version=r[7],
                vec_blob=(r[9] if with_vectors else None),
                source_updated_at=r[8]))
    return out


# ── Lexical: Python BM25 ──────────────────────────────────────────────

def _bm25_py(cands: list[Cand], terms: list[Term], *, prefix_last: bool,
             corpus: tuple[int, Mapping[str, int], float | None] | None = None
             ) -> tuple[list[tuple[int, float]], dict[int, set[str]]]:
    """``([(doc_id, score)] best first, {doc_id: matched stems})``.

    BM25 with a title field weighted :data:`TITLE_WEIGHT`. With
    *prefix_last*, the last query term also matches as a prefix when it has
    no exact occurrence anywhere (what ``last:*`` does on Postgres) —
    "regist" finds "registr".

    *cands* is the whole scope on SQLite. On Postgres it is only the pool
    the FTS index found, and *corpus* — ``(documents, {stem: df},
    average length)`` of the whole scope — supplies the statistics a pool
    cannot.
    """
    if not cands or not terms:
        return [], {}
    stems = []
    for t in terms:
        if t.stem not in stems:
            stems.append(t.stem)
    w = {t.stem: t.weight for t in terms}
    split = [(c.lex_title.split(), c.lex_body.split()) for c in cands]
    total_len = 0
    for ti, bo in split:
        total_len += len(ti) * TITLE_WEIGHT + len(bo)
    n_pool = len(cands)
    avgdl = total_len / n_pool
    n = n_pool
    corpus_df: Mapping[str, int] = {}
    if corpus is not None:
        n = max(n_pool, int(corpus[0] or 0))
        corpus_df = corpus[1] or {}
        if corpus[2]:
            avgdl = float(corpus[2])
    avgdl = avgdl or 1.0

    prefix_term = None
    if prefix_last and stems:
        last = stems[-1]
        if len(last) >= 3 and not any(last in ti or last in bo
                                      for ti, bo in split):
            prefix_term = last

    tfs: dict[str, list[float]] = {}
    df: dict[str, int] = {}
    for s in stems:
        col = [0.0] * n_pool
        cnt = 0
        for i, (ti, bo) in enumerate(split):
            if s == prefix_term:
                f = (TITLE_WEIGHT * sum(1 for x in ti if x.startswith(s))
                     + sum(1 for x in bo if x.startswith(s)))
            else:
                f = TITLE_WEIGHT * ti.count(s) + bo.count(s)
            if f:
                col[i] = f
                cnt += 1
        tfs[s] = col
        # The scope's count, never below what the pool itself shows (a
        # prefix match has no exact-stem count in the scope).
        df[s] = max(cnt, int(corpus_df.get(s, 0)))
    scores: dict[int, float] = {}
    matched: dict[int, set[str]] = {}
    for s in stems:
        if not df[s]:
            continue
        idf = bm25_idf(n, df[s])
        col = tfs[s]
        for i, f in enumerate(col):
            if not f:
                continue
            ti, bo = split[i]
            dl = len(ti) * TITLE_WEIGHT + len(bo)
            norm = BM25_K1 * (1 - BM25_B + BM25_B * dl / avgdl)
            did = cands[i].id
            scores[did] = scores.get(did, 0.0) + \
                w.get(s, 1.0) * idf * (f * (BM25_K1 + 1)) / (f + norm)
            matched.setdefault(did, set()).add(s)
    order = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return order[:LIST_DEPTH], matched


# ── Lexical: Postgres FTS ─────────────────────────────────────────────

_TSQ_SAFE = re.compile(r"[^0-9a-zа-яёіїєґ]+")


def tsquery_text(terms: Sequence[Term], *, prefix_last: bool = True) -> str:
    """``t1 | t2 | last:*`` with every term reduced to word characters
    (the stored lex forms contain nothing else, so nothing is lost)."""
    parts: list[str] = []
    for t in terms:
        for piece in _TSQ_SAFE.split(t.stem.lower()):
            if piece and piece not in parts:
                parts.append(piece)
    if not parts:
        return ""
    if prefix_last and len(parts[-1]) >= 3:
        parts[-1] = parts[-1] + ":*"
    return " | ".join(parts)


#: On Postgres the full-text index only *finds* the lexical candidates —
#: this many — and :func:`_bm25_py` orders them with the scope's document
#: frequencies, so both engines rank alike and the golden set (SQLite)
#: measures production. The pool is chosen by the summed IDF of the query
#: terms a document contains, not by ``ts_rank_cd``: that has no IDF, and a
#: page that said "project" often would push the one with "Base URL" out.
PG_LEX_POOL = 400


def term_tsquery(stem_: str) -> str:
    """One stem as a tsquery: its word-character pieces ANDed — the stored
    lex is split the same way by ``to_tsvector('simple', …)``."""
    parts = [p for p in _TSQ_SAFE.split(stem_.lower()) if p]
    return " & ".join(dict.fromkeys(parts))


def bm25_idf(n: int, df: int) -> float:
    return math.log(1.0 + (n - df + 0.5) / (df + 0.5))


def fts_statement(allowed, kinds, tsq: str, *,
                  weights: Sequence[tuple[str, float]] = (),
                  limit: int = PG_LEX_POOL):
    """The Postgres full-text statement (scope clause included): the
    candidate rows with their lex. With *weights* — ``(tsquery, idf)`` per
    term — the best are those whose matching terms are rarest; without,
    ``ts_rank_cd``. Separate so a test can compile it against the PG
    dialect without a server.

    Every parameter goes through ``text()``: ``literal_column("… :tsq …")``
    handed the colon-name to the driver verbatim, so every FTS query since
    the ADR 0003 build raised a syntax error and search fell back to Python
    BM25 unseen (found live 2026-10-15, explain said "python-bm25").
    """
    from sqlalchemy import select, text

    from engine.db import SearchDoc
    params = {"tsq": tsq}
    if weights:
        parts = []
        for i, (q, w) in enumerate(weights):
            parts.append(f"(CASE WHEN search_doc.tsv @@ to_tsquery('simple', "
                         f":w{i}) THEN {float(w)!r} ELSE 0 END)")
            params[f"w{i}"] = q
        rank = text("(" + " + ".join(parts) + ") DESC")
    else:
        rank = text("ts_rank_cd(search_doc.tsv, to_tsquery('simple', :tsq), "
                    "32) DESC")
    stmt = select(SearchDoc.id, SearchDoc.project_id, SearchDoc.kind,
                  SearchDoc.ref, SearchDoc.chunk_no, SearchDoc.lex_title,
                  SearchDoc.lex_body, SearchDoc.source_updated_at)
    stmt = _filtered(stmt, allowed, kinds)
    stmt = stmt.where(text("search_doc.tsv @@ to_tsquery('simple', :tsq)"))
    return stmt.order_by(rank, SearchDoc.id).limit(limit).params(**params)


#: ``search_doc``'s length in tokens, the way :func:`_bm25_py` counts it
#: (title weighted :data:`TITLE_WEIGHT`): spaces + 1, 0 for an empty field.
_PG_DOC_LEN = (
    "(CASE WHEN coalesce(search_doc.lex_title, '') = '' THEN 0 ELSE "
    "length(search_doc.lex_title) - length(replace(search_doc.lex_title, "
    "' ', '')) + 1 END) * {tw} + "
    "(CASE WHEN coalesce(search_doc.lex_body, '') = '' THEN 0 ELSE "
    "length(search_doc.lex_body) - length(replace(search_doc.lex_body, "
    "' ', '')) + 1 END)")


def corpus_stats_statement(allowed, kinds, stems: Sequence[str]):
    """One row: documents in scope, their average length, and the document
    frequency of each stem (``df0``, ``df1``, … in *stems* order)."""
    from sqlalchemy import func, literal_column, select, text

    from engine.db import SearchDoc
    cols = [func.count().label("n"),
            literal_column(f"avg({_PG_DOC_LEN.format(tw=TITLE_WEIGHT)})")
            .label("avgdl")]
    params = {}
    for i, s in enumerate(stems):
        cols.append(func.count().filter(text(
            f"search_doc.tsv @@ to_tsquery('simple', :q{i})")).label(f"df{i}"))
        params[f"q{i}"] = term_tsquery(s)
    stmt = _filtered(select(*cols).select_from(SearchDoc), allowed, kinds)
    return stmt.params(**params)


def _lex_pg(allowed, kinds, terms
            ) -> tuple[list[tuple[int, float]], dict[int, set[str]]]:
    from engine import db as _db
    tsq = tsquery_text(terms)
    if not tsq:
        return [], {}
    stems = [s for s in dict.fromkeys(t.stem for t in terms)
             if term_tsquery(s)]
    with _db.session_scope() as sess:
        row = sess.execute(corpus_stats_statement(allowed, kinds,
                                                  stems)).one()
        n = int(row[0] or 0)
        df = {s: int(row[2 + i] or 0) for i, s in enumerate(stems)}
        weights = [(term_tsquery(s), bm25_idf(n, df[s])) for s in stems
                   if df[s]]
        pool = [Cand(id=int(r[0]), project_id=r[1], kind=r[2], ref=r[3],
                     chunk_no=r[4], lex_title=r[5] or "", lex_body=r[6] or "",
                     vec_version=None, vec_blob=None, source_updated_at=r[7])
                for r in sess.execute(fts_statement(allowed, kinds, tsq,
                                                    weights=weights))]
    if not pool:
        return [], {}
    avgdl = float(row[1]) if row[1] is not None else None
    return _bm25_py(pool, terms, prefix_last=True, corpus=(n, df, avgdl))


# ── Vectors ───────────────────────────────────────────────────────────

def _vec_py(cands: list[Cand], qvec, version: str, *, minimum: float
            ) -> list[tuple[int, float]]:
    """Cosine over ``vec_blob`` (unit vectors → dot product), using only
    the query's non-zero dimensions — a query touches a few dozen of 288."""
    from engine.search.embed import from_blob
    nz = [(i, v) for i, v in enumerate(qvec) if v]
    if not nz:
        return []
    out: list[tuple[int, float]] = []
    for c in cands:
        if not c.vec_blob or c.vec_version != version:
            continue
        d = from_blob(c.vec_blob)
        if len(d) != len(qvec):
            continue
        s = 0.0
        for i, v in nz:
            s += v * d[i]
        if s >= minimum:
            out.append((c.id, s))
    out.sort(key=lambda kv: (-kv[1], kv[0]))
    return out[:LIST_DEPTH]


def vector_statement(allowed, kinds, qvec_literal: str, *,
                     limit: int = LIST_DEPTH):
    """The pgvector statement (scope clause included)."""
    from sqlalchemy import bindparam, cast, column, select, table
    from sqlalchemy.types import UserDefinedType

    from engine.db import SearchDoc

    class _Vector(UserDefinedType):
        cache_ok = True

        def get_col_spec(self, **_kw):
            return "vector"

    sv = table("search_vector", column("doc_id"), column("embedding"))
    # A real bind parameter: ":qv" inside literal_column() reached the
    # driver verbatim (see fts_statement).
    dist = sv.c.embedding.op("<=>")(cast(bindparam("qv"), _Vector()))
    stmt = select(SearchDoc.id, (1 - dist).label("cos")).select_from(
        SearchDoc.__table__.join(sv, sv.c.doc_id == SearchDoc.id))
    stmt = _filtered(stmt, allowed, kinds)
    return stmt.order_by(dist, SearchDoc.id).limit(limit).params(
        qv=qvec_literal)


def _vec_pg(allowed, kinds, qvec, *, minimum: float, wide: bool
            ) -> list[tuple[int, float]]:
    from sqlalchemy import text

    from engine import db as _db
    lit = "[" + ",".join(repr(float(x)) for x in qvec) + "]"
    with _db.session_scope() as sess:
        if wide:
            # Org-wide: the HNSW index with a wider beam (ADR §4.1). Best
            # effort — an older pgvector without the GUC simply ignores it.
            try:
                with sess.begin_nested():
                    sess.execute(text("SET LOCAL hnsw.ef_search = 200"))
            except Exception:   # noqa: BLE001
                pass
        rows = sess.execute(vector_statement(allowed, kinds, lit)).all()
    return [(int(r[0]), float(r[1])) for r in rows
            if r[1] is not None and float(r[1]) >= minimum]


# ── Fusion ────────────────────────────────────────────────────────────

def rrf(lists: Sequence[tuple[float, Sequence[tuple[int, float]]]],
        k: int = RRF_K) -> dict[int, float]:
    """``score(d) = Σ w_i / (k + rank_i(d))`` over weighted ranked lists
    (rank 1-based)."""
    out: dict[int, float] = {}
    for w, lst in lists:
        if not w:
            continue
        for rank, (did, _s) in enumerate(lst, 1):
            out[did] = out.get(did, 0.0) + w / (k + rank)
    return out


# ── Hydration, snippets ───────────────────────────────────────────────

@dataclass
class Doc:
    id: int
    project_id: str | None
    scope: str
    kind: str
    ref: str
    chunk_no: int
    title: str
    body: str
    lex_title: str
    url: str | None
    facets: dict
    lang: str | None
    source_updated_at: Any
    lex_body: str = ""


def _hydrate(ids: Iterable[int], allowed) -> dict[int, Doc]:
    from sqlalchemy import select

    from engine import db as _db
    from engine.db import SearchDoc
    ids = list(dict.fromkeys(int(i) for i in ids))
    out: dict[int, Doc] = {}
    with _db.session_scope() as sess:
        for start in range(0, len(ids), 500):
            stmt = _filtered(select(
                SearchDoc.id, SearchDoc.project_id, SearchDoc.scope,
                SearchDoc.kind, SearchDoc.ref, SearchDoc.chunk_no,
                SearchDoc.title, SearchDoc.body, SearchDoc.lex_title,
                SearchDoc.url, SearchDoc.facets, SearchDoc.lang,
                SearchDoc.source_updated_at, SearchDoc.lex_body),
                allowed, None).where(
                    SearchDoc.id.in_(ids[start:start + 500]))
            for r in sess.execute(stmt):
                out[r[0]] = Doc(id=r[0], project_id=r[1], scope=r[2],
                                kind=r[3], ref=r[4], chunk_no=r[5] or 0,
                                title=r[6] or "", body=r[7] or "",
                                lex_title=r[8] or "", url=r[9],
                                facets=dict(r[10] or {}), lang=r[11],
                                source_updated_at=r[12],
                                lex_body=r[13] or "")
    return out


_WORDISH = re.compile(r"[0-9A-Za-zЀ-ӿ]+(?:['’ʼ][0-9A-Za-zЀ-ӿ]+)*")
SNIPPET_CHARS = 180


def snippet(text: str, stems: set[str], lang: str | None,
            width: int = SNIPPET_CHARS) -> str:
    """An HTML-safe excerpt of *text* around the first word whose stem is
    in *stems*, matched words wrapped in ``<mark>``. Everything else is
    escaped — the body is user data."""
    from engine.search.text import normalise, stem, translate_word
    text = (text or "").strip()
    if not text:
        return ""
    lang = lang or "en"
    spans: list[tuple[int, int]] = []
    for m in _WORDISH.finditer(text):
        word = normalise(m.group(0))
        if not word:
            continue
        s = stem(word, lang)
        hit = s in stems
        if not hit and not word.isascii():
            tr = translate_word(word, lang)
            hit = bool(tr) and any(stem(t, "en") in stems
                                   for t in tr.split())
        if hit:
            spans.append((m.start(), m.end()))
    if spans:
        first = spans[0][0]
        start = max(0, first - width // 3)
        if start:
            sp = text.rfind(" ", 0, start)
            start = sp + 1 if sp >= 0 and start - sp < 30 else start
    else:
        start = 0
    end = min(len(text), start + width)
    if end < len(text):
        sp = text.rfind(" ", start, end)
        end = sp if sp > start + width // 2 else end
    out: list[str] = []
    pos = start
    for a, b in spans:
        if b <= start or a >= end:
            continue
        a, b = max(a, start), min(b, end)
        out.append(html.escape(text[pos:a]))
        out.append(f"<mark>{html.escape(text[a:b])}</mark>")
        pos = b
    out.append(html.escape(text[pos:end]))
    body = "".join(out).replace("\n", " ")
    return ("…" if start > 0 else "") + body + ("…" if end < len(text) else "")


def _utc_ts(dt) -> float:
    if not isinstance(dt, datetime):
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def ref_number(ref: str) -> tuple[str, int] | None:
    """``"TC-012"`` → ``("TC", 12)``, ``"CL_001"`` → ``("CL", 1)``,
    ``"run:7"`` → ``("RUN", 7)``."""
    if not ref:
        return None
    if ref.startswith("run:") and ref[4:].isdigit():
        return "RUN", int(ref[4:])
    m = _REF_NUM.match(ref.strip())
    if not m or not m.group(1):
        return None
    return m.group(1).upper(), int(m.group(2))


def _id_matches(allowed, kinds, id_query: tuple[str, int]) -> list[int]:
    """Doc ids (chunk 0) whose ref *is* the queried id — "TC-12" finds
    "TC-012", "cl1" finds "CL_001", "run 7" finds "run:7"."""
    from sqlalchemy import select

    from engine import db as _db
    from engine.db import SearchDoc
    prefix, num = id_query
    kind = ID_KINDS.get(prefix)
    if kind is None or (kinds and kind not in kinds):
        return []
    stmt = _filtered(select(SearchDoc.id, SearchDoc.ref),
                     allowed, [kind], include_global=False).where(
        SearchDoc.chunk_no == 0)
    if kind == "run":
        stmt = stmt.where(SearchDoc.ref == f"run:{num}")
    else:
        stmt = stmt.where(SearchDoc.ref.ilike(f"{prefix}%"))
    out = []
    with _db.session_scope() as sess:
        for did, ref in sess.execute(stmt):
            parsed = ref_number(ref)
            if parsed and parsed == (prefix, num):
                out.append(int(did))
    return out


#: Kinds whose ``ref`` is the team's own external id.
REF_KINDS = ("test_case", "checklist_item", "bug")


def _ref_matches(allowed, kinds, ref: str) -> list[int]:
    """Doc ids (chunk 0) whose ref *is* the queried external id, ignoring
    case — "lgn_010" finds the test case or checklist item LGN_010."""
    from sqlalchemy import func, select

    from engine import db as _db
    from engine.db import SearchDoc
    want = [k for k in REF_KINDS if not kinds or k in kinds]
    if not want:
        return []
    stmt = _filtered(select(SearchDoc.id), allowed, want,
                     include_global=False).where(
        SearchDoc.chunk_no == 0, func.upper(SearchDoc.ref) == ref.upper())
    with _db.session_scope() as sess:
        return [int(did) for (did,) in sess.execute(stmt)]


def _exact_id_hits(allowed, kinds, raw: str,
                   id_query: tuple[str, int] | None) -> list[int]:
    """The ID fast path: TC/CL/BUG/RUN numbers, else the team's own ids."""
    from engine.search.text import parse_ref_query
    if id_query:
        return _id_matches(allowed, kinds, id_query)
    ref = parse_ref_query(raw)
    return _ref_matches(allowed, kinds, ref) if ref else []


# ── The search itself ─────────────────────────────────────────────────

@dataclass
class SearchParams:
    q: str
    allowed: list[str]
    kinds: list[str] | None = None
    filters: dict[str, str] = field(default_factory=dict)
    project_filter: str | None = None
    linked: str | None = None
    limit: int = 20
    explain: bool = True
    istqb: bool = True
    scope: str = "org"
    project_names: dict[str, str] = field(default_factory=dict)


def _norm_facet(v: Any) -> str:
    return str(v).strip().lower() if v is not None else ""


def _facet_ok(facets: dict, name: str, want: str) -> bool:
    have = _norm_facet((facets or {}).get(name))
    want = _norm_facet(want)
    if name == "status" and want == "open":
        return have in _OPEN_STATUSES
    return have == want


def _caps() -> dict:
    from engine.search import caps
    return caps


def _backend() -> str | None:
    try:
        from engine import db as _db
        return _db.get_engine().dialect.name
    except Exception:   # noqa: BLE001
        return None


def _linked_refs(allowed, ref: str) -> set[tuple[str, str, str]]:
    """``{(project_id, kind, ref)}`` linked to *ref* by the data model:
    a test case's bugs (``bug_report.related_case_id``) and its failed
    results, or a bug's test case. Only inside *allowed*."""
    from engine import db as _db
    from engine.db import BugReport, TestCase
    ids = sorted({str(a) for a in allowed if a})
    if not ids or not ref:
        return set()
    out: set[tuple[str, str, str]] = set()
    with _db.session_scope() as sess:
        if ref.upper().startswith("BUG"):
            rows = (sess.query(BugReport.project_id, TestCase.external_id)
                    .join(TestCase, TestCase.id == BugReport.related_case_id)
                    .filter(BugReport.project_id.in_(ids),
                            BugReport.external_id == ref).all())
            out |= {(p, "test_case", r) for p, r in rows if r}
        else:
            rows = (sess.query(BugReport.project_id, BugReport.external_id)
                    .join(TestCase, TestCase.id == BugReport.related_case_id)
                    .filter(TestCase.project_id.in_(ids),
                            TestCase.external_id == ref).all())
            out |= {(p, "bug", r) for p, r in rows if r}
    return out


def _doc_ids_for(allowed, entities: set[tuple[str, str, str]]) -> list[int]:
    """Chunk-0 doc ids of ``(project_id, kind, ref)`` entities."""
    from sqlalchemy import and_, or_, select

    from engine import db as _db
    from engine.db import SearchDoc
    if not entities:
        return []
    conds = [and_(SearchDoc.project_id == p, SearchDoc.kind == k,
                  SearchDoc.ref == r) for p, k, r in sorted(entities)[:200]]
    stmt = _filtered(select(SearchDoc.id), allowed, None,
                     include_global=False).where(
        SearchDoc.chunk_no == 0).where(or_(*conds))
    with _db.session_scope() as sess:
        return [int(r[0]) for r in sess.execute(stmt)]


def _chips(hits: list[dict], pq: ParsedQuery, params: SearchParams,
           facet_counts: dict, linked_counts: dict) -> list[dict]:
    """Deterministic follow-up chips from the facets of what was found
    (ADR §4.6 — never from an LLM). At most four, most specific first."""
    chips: list[dict] = []
    q = pq.raw

    def chip(cid, label, **kw):
        if len(chips) < 4 and all(c["id"] != cid for c in chips):
            chips.append({"id": cid, "label": label, "q": kw.pop("q", q),
                          **kw})

    if pq.did_you_mean:
        chip("did_you_mean", f"Search for “{pq.did_you_mean}”",
             q=pq.did_you_mean)
    for key, (label, payload) in linked_counts.items():
        chip(key, label, **payload)
    bug_status = facet_counts.get("bug_status", {})
    open_bugs = sum(n for s, n in bug_status.items() if s in _OPEN_STATUSES)
    if open_bugs and open_bugs < sum(bug_status.values()) \
            and params.filters.get("status") != "open":
        chip("open_bugs", "Only open bugs", kinds=["bug"],
             filters={"status": "open"})
    kinds = facet_counts.get("kind", {})
    if len(kinds) > 1 and not params.kinds:
        label = {"test_case": "Only test cases", "bug": "Only bugs",
                 "checklist_item": "Only checklist items",
                 "run": "Only runs", "case_result": "Only failed results",
                 "guide": "Only the Guide", "istqb": "Only ISTQB"}
        for k, _n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))[:2]:
            chip(f"kind:{k}", label.get(k, f"Only {k}"), kinds=[k])
    prios = facet_counts.get("priority", {})
    if len(prios) > 1 and "priority" not in params.filters:
        for p in ("critical", "blocker", "high"):
            if p in prios:
                chip(f"priority:{p}", f"Only {p} priority",
                     filters={"priority": p})
                break
    projects = facet_counts.get("project", {})
    if len(projects) > 1 and not params.project_filter:
        pid, _n = sorted(projects.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        name = params.project_names.get(pid) or pid[:8]
        chip(f"project:{pid}", f"Only in {name}", project_id=pid)
    return chips


#: Words that name a kind of item. "What does the registration checklist
#: cover?" is answered by checklist items whose text never says
#: "checklist" — the word names what they are. The Ask coverage gate counts
#: it as covered by an item of that kind; ranking does not use it.
_KIND_WORDS = {
    "checklist_item": ("checklist",),
    "test_case": ("test", "case"),
    "bug": ("bug", "defect"),
    "run": ("run",),
    "guide": ("guide",),
}


@lru_cache(maxsize=16)
def kind_stems(kind: str) -> frozenset[str]:
    from engine.search.text import stem
    return frozenset(stem(w, "en") for w in _KIND_WORDS.get(kind, ()))


_GUIDE_TITLE_PREFIX = re.compile(r"^.*?·\s*")


@lru_cache(maxsize=256)
def module_phrase(title: str) -> str | None:
    """A Guide section's module name as space-padded stems — "Guide · Test
    Execution" → " test execut " — when it has two words or more. One-word
    names ("Projects", "Runs", "Settings") are words every question uses.
    """
    from engine.search.text import analyse
    name = _GUIDE_TITLE_PREFIX.sub("", title or "", count=1)
    stems = [x for x in analyse(name).stems if x]
    if len(stems) < 2:
        return None
    return f" {' '.join(stems)} "


def search(params: SearchParams) -> dict:
    """Full search: ranked, grouped, explained. See the module docstring."""
    from engine.search.embed import NotConfigured, get_embedder

    t0 = time.perf_counter()
    timings: dict[str, float] = {}
    allowed = list(params.allowed) if params.allowed is not None else None
    if allowed is None:
        raise TenancyError("search requires an explicit list of project ids")
    kinds = sorted(set(params.kinds)) if params.kinds else None

    # 1. Parse (+ the did-you-mean vocabulary of the scopes searched).
    states = _scope_states([*allowed, "global"])
    vocab = vocabulary_for(states)
    pq = parse_query(params.q, vocab)
    timings["parse"] = (time.perf_counter() - t0) * 1000

    # 2. Embed.
    t = time.perf_counter()
    embedder = None
    qvec = None
    try:
        embedder = get_embedder()
        qvec = embedder.embed(pq.embed_text)
        pq.concepts = embedder.concepts_of(pq.embed_text) \
            if hasattr(embedder, "concepts_of") else []
    except NotConfigured as exc:
        log.warning("search: %s — lexical only", exc)
        embedder = None
    timings["embed"] = (time.perf_counter() - t) * 1000

    # 3. Retrieve — possibly twice, when a kind filter finds nothing.
    t = time.perf_counter()
    relaxed: list[dict] = []
    run = _retrieve(allowed, kinds, pq, embedder, qvec)
    if not run["fused"] and kinds:
        relaxed.append({"filter": "kinds", "value": kinds,
                        "message": "Nothing of the requested kind matched; "
                                   "showing every kind."})
        kinds = None
        run = _retrieve(allowed, None, pq, embedder, qvec)
    timings["retrieve"] = (time.perf_counter() - t) * 1000

    # 4. Rerank: boosts, grouping, filters, snippets.
    t = time.perf_counter()
    fused: dict[int, float] = run["fused"]
    pinned = set(run["id_hits"])
    # Linked items (a chip's "Linked bugs for TC-012") are candidates even
    # when their text shares nothing with the query — the link is the
    # match. Still read through the scope clause.
    linked_set: set | None = None
    if params.linked:
        linked_set = _linked_refs(allowed, params.linked)
        for did in _doc_ids_for(allowed, linked_set):
            fused.setdefault(did, 0.0)
    # Pinned ids first: an exact id hit may share no text with the query
    # (fused 0.0) and must not be cut by the depth limit before its boost.
    docs = _hydrate(sorted(fused, key=lambda d: (d not in pinned,
                                                 -fused[d]))[:LIST_DEPTH * 3],
                    allowed)
    lex_rank = {d: i for i, (d, _s) in enumerate(run["lex"], 1)}
    vec_rank = {d: i for i, (d, _s) in enumerate(run["vec"], 1)}
    lex_score = dict(run["lex"])
    vec_score = dict(run["vec"])
    matched = run["matched"]
    term_stems = set(pq.term_stems())

    scored: list[tuple[float, float, int]] = []
    boosts: dict[int, list[str]] = {}
    boost_title = _env_float("SEARCH_BOOST_TITLE", BOOST_TITLE)
    boost_open = _env_float("SEARCH_BOOST_OPEN_BUG", BOOST_OPEN_BUG)
    boost_phrase = _env_float("SEARCH_BOOST_PHRASE", BOOST_PHRASE)
    boost_module = _env_float("SEARCH_BOOST_MODULE", BOOST_MODULE)
    phrases = pq.phrases
    query_seq = pq.stem_sequence
    for did, base in fused.items():
        doc = docs.get(did)
        if doc is None:
            continue
        s = base
        b: list[str] = []
        if did in pinned:
            s += BOOST_ID
            b.append("id_exact")
        # "Title match" = the title alone covers at least half the query's
        # words. A single shared word is already in BM25's title weight;
        # boosting it again buried checklist items, whose title is only
        # their section name.
        if term_stems and pq.title_coverage(
                set(doc.lex_title.split())) >= 0.5:
            s += boost_title
            b.append("title_match")
        if boost_module and doc.kind == "guide" and query_seq:
            name = module_phrase(doc.title)
            if name and name in query_seq:
                s += boost_module
                b.append("module")
        if boost_phrase and phrases:
            lex = f" {doc.lex_title} {doc.lex_body} "
            hits = sum(1 for p in phrases if p in lex)
            if hits:
                s += boost_phrase * min(hits, 2)
                b.append("phrase")
        if doc.kind == "bug" and \
                _norm_facet(doc.facets.get("status")) in _OPEN_STATUSES:
            s += boost_open
            b.append("open_bug")
        boosts[did] = b
        scored.append((s, _utc_ts(doc.source_updated_at), did))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))

    # Group by entity: the best chunk wins.
    groups_seen: dict[tuple, int] = {}
    entities: list[tuple[float, Doc]] = []
    for s, _ts, did in scored:
        doc = docs[did]
        key = (doc.project_id, doc.kind, doc.ref)
        if key in groups_seen:
            continue
        groups_seen[key] = did
        entities.append((s, doc))

    # Facet counts over everything found, before the facet filters.
    def counts(items) -> dict:
        c: dict[str, dict[str, int]] = {"kind": {}, "status": {},
                                        "priority": {}, "project": {},
                                        "bug_status": {}}
        for _s, d in items:
            c["kind"][d.kind] = c["kind"].get(d.kind, 0) + 1
            for f in ("status", "priority"):
                v = _norm_facet(d.facets.get(f))
                if v:
                    c[f][v] = c[f].get(v, 0) + 1
            if d.kind == "bug":
                v = _norm_facet(d.facets.get("status"))
                if v:
                    c["bug_status"][v] = c["bug_status"].get(v, 0) + 1
            if d.project_id:
                c["project"][d.project_id] = \
                    c["project"].get(d.project_id, 0) + 1
        return c

    facet_counts = counts(entities)
    before = len(entities)

    # Filters, each relaxed (and said so) when it alone empties the list.
    applied: dict[str, Any] = {}
    current = entities
    steps: list[tuple[str, Any, Any]] = []
    for name in FACET_FILTERS:
        want = params.filters.get(name)
        if want:
            steps.append((name, want,
                          lambda d, n=name, w=want: _facet_ok(d.facets, n, w)))
    if params.project_filter:
        pf = params.project_filter
        steps.append(("project", pf, lambda d, p=pf: d.project_id == p))
    if linked_set is not None:
        steps.append(("linked", params.linked,
                      lambda d, ls=linked_set:
                      (d.project_id, d.kind, d.ref) in ls))
    for name, want, pred in steps:
        narrowed = [(s, d) for s, d in current if pred(d)]
        if narrowed or not current:
            current = narrowed
            applied[name] = want
        else:
            relaxed.append({
                "filter": name, "value": want,
                "message": f"No results with {name} = {want}; the filter "
                           f"was relaxed."})
    after = len(current)

    limit = max(1, min(int(params.limit or 20), 100))
    top = current[:limit]

    def result(s: float, d: Doc, *, with_why: bool) -> dict:
        hit_stems = matched.get(d.id, set())
        out = {
            "doc_id": d.id, "kind": d.kind, "ref": d.ref,
            "title": d.title, "url": d.url, "chunk_no": d.chunk_no,
            "snippet": snippet(d.body, hit_stems or term_stems, d.lang),
            "project": ({"id": d.project_id,
                         "name": params.project_names.get(d.project_id)}
                        if d.project_id else None),
            "facets": d.facets, "score": round(s, 6),
            # Evidence beyond vector closeness: a lexical hit (own words,
            # translation, synonym, typo fix) or an exact id. The Ask gate
            # reads it — hash vectors always have a "closest" document.
            "lexical": d.id in lex_rank or d.id in pinned,
            # Which query words this document matches directly, and how
            # many words the query has: the Ask gate wants the results
            # together to cover enough of the question, not one stray word.
            "covered": pq.covered_words(hit_stems | kind_stems(d.kind)),
            "words": pq.word_count,
            "pinned": d.id in pinned,
        }
        if with_why:
            labels = [t.label for t in pq.terms if t.stem in hit_stems]
            doc_concepts: list[str] = []
            if embedder is not None and pq.concepts and \
                    hasattr(embedder, "concepts_of"):
                dc = set(embedder.concepts_of(f"{d.title}\n{d.body}"))
                doc_concepts = [c for c in pq.concepts if c in dc]
            out["why"] = {
                "lex_rank": lex_rank.get(d.id),
                "vec_rank": vec_rank.get(d.id),
                "lex_score": (round(lex_score[d.id], 4)
                              if d.id in lex_score else None),
                "vec_score": (round(vec_score[d.id], 4)
                              if d.id in vec_score else None),
                "rrf": round(fused.get(d.id, 0.0), 6),
                "matched_terms": labels,
                "concepts": doc_concepts,
                "typo": {k: v for k, v in pq.corrections.items()
                         if any(t.source == "typo" and t.stem in hit_stems
                                and t.label.startswith(f"{k}→")
                                for t in pq.terms)},
                "boosts": boosts.get(d.id, []),
                "mode": run["mode"],
            }
        return out

    results = [result(s, d, with_why=params.explain) for s, d in top]
    groups: dict[str, list[dict]] = {}
    for s, d in current:
        g = groups.setdefault(d.kind, [])
        if len(g) < GROUP_SIZE:
            g.append(result(s, d, with_why=False))
    group_order = list(groups)

    # Linked-items chips: one query, only for the top hit.
    linked_chips: dict[str, tuple[str, dict]] = {}
    if top and not params.linked:
        _s, d0 = top[0]
        if d0.kind in ("test_case", "bug") and d0.project_id:
            links = _linked_refs([d0.project_id], d0.ref)
            if links:
                if d0.kind == "test_case":
                    label = f"Linked bugs for {d0.ref} ({len(links)})"
                    linked_chips[f"linked:{d0.ref}"] = (label, {
                        "q": d0.ref, "kinds": ["bug"], "linked": d0.ref,
                        "project_id": d0.project_id})
                else:
                    ref = sorted(r for _p, _k, r in links)[0]
                    label = f"Linked test for {d0.ref}: {ref}"
                    linked_chips[f"linked:{d0.ref}"] = (label, {
                        "q": ref, "kinds": ["test_case"],
                        "project_id": d0.project_id})
    chips = _chips([r for r in results], pq, params, facet_counts,
                   linked_chips)

    # The ISTQB textbook half, as its own group (ADR §4.3).
    t_istqb = time.perf_counter()
    if params.istqb and (not params.kinds or "istqb" in params.kinds):
        try:
            from engine import istqb_rag
            book = [h for h in istqb_rag.search(params.q, k=6)
                    if h.get("source") != "syllabus"][:3]
            if book:
                groups["istqb_book"] = [{
                    "source": h.get("source"), "page": h.get("page"),
                    "excerpt": istqb_rag._excerpt(h),
                    "score": h.get("score")} for h in book]
        except Exception as exc:   # noqa: BLE001 — a nicety, never fatal
            log.debug("search: istqb group unavailable: %s", exc)
    timings["istqb"] = (time.perf_counter() - t_istqb) * 1000
    timings["rerank"] = (time.perf_counter() - t) * 1000 - timings["istqb"]
    timings["total"] = (time.perf_counter() - t0) * 1000

    facets_out = {k: v for k, v in facet_counts.items() if k != "bug_status"}
    w_lex, w_vec = weights()
    response = {
        "query": {
            "raw": pq.raw, "normalised": pq.normalised, "lang": pq.lang,
            "translated": pq.translated, "did_you_mean": pq.did_you_mean,
            "corrections": pq.corrections,
            "id": (f"{pq.id_query[0]}-{pq.id_query[1]}"
                   if pq.id_query else None),
        },
        "mode": run["mode"],
        "corpus_version": sum(states.values()),
        "results": results,
        "groups": groups,
        "group_order": group_order,
        "facets": facets_out,
        "filters": {"requested": _requested(params), "applied": applied,
                    "relaxed": relaxed},
        "chips": chips,
        "total": after,
    }
    if params.explain:
        response["explain"] = {
            "tokens": pq.tokens, "stems": pq.stems,
            "terms": [{"stem": t.stem, "label": t.label, "source": t.source}
                      for t in pq.terms],
            "translated": pq.translated, "corrections": pq.corrections,
            "concepts": pq.concepts,
            "id_query": response["query"]["id"],
            "id_hits": len(pinned),
            "mode": run["mode"], "lexical": run["lex_engine"],
            "vector": run["vec_engine"],
            "weights": {"lex": w_lex, "vec": w_vec, "rrf_k": RRF_K},
            "lists": {"lexical": len(run["lex"]), "vector": len(run["vec"]),
                      "candidates": run["candidates"]},
            "filters": {"before": before, "after": after,
                        "applied": applied, "relaxed": relaxed},
            "timings_ms": {k: round(v, 2) for k, v in timings.items()},
        }
    return response


def _requested(params: SearchParams) -> dict:
    out: dict[str, Any] = {}
    if params.kinds:
        out["kinds"] = sorted(params.kinds)
    for k in FACET_FILTERS:
        if params.filters.get(k):
            out[k] = params.filters[k]
    if params.project_filter:
        out["project"] = params.project_filter
    if params.linked:
        out["linked"] = params.linked
    return out


def _retrieve(allowed, kinds, pq: ParsedQuery, embedder, qvec) -> dict:
    """The two ranked lists and their fusion, on whichever engines the
    database offers. Every read goes through :func:`_scope_clause`."""
    caps = _caps()
    backend = _backend()
    pg = backend == "postgresql"
    out: dict[str, Any] = {"lex": [], "vec": [], "matched": {},
                           "fused": {}, "id_hits": [], "mode": "lexical",
                           "lex_engine": None, "vec_engine": None,
                           "candidates": None}
    out["id_hits"] = _exact_id_hits(allowed, kinds, pq.raw, pq.id_query)

    version = getattr(embedder, "version", None) if embedder else None
    use_pg_fts = pg and caps.get("fts")
    use_pg_vec = pg and caps.get("pgvector") and qvec is not None
    cands: list[Cand] | None = None
    cands_have_vectors = False

    def load(with_vectors: bool) -> list[Cand]:
        nonlocal cands, cands_have_vectors
        if cands is None or (with_vectors and not cands_have_vectors):
            cands = _load_candidates(allowed, kinds, with_vectors=with_vectors)
            cands_have_vectors = with_vectors
            out["candidates"] = len(cands)
        return cands

    # Lexical.
    lex: list[tuple[int, float]] = []
    matched: dict[int, set[str]] = {}
    if pq.terms:
        if use_pg_fts:
            try:
                lex, matched = _lex_pg(allowed, kinds, pq.terms)
                out["lex_engine"] = "postgres-fts+bm25"
            except Exception as exc:   # noqa: BLE001 — fall back, never fail
                log.warning("search: FTS failed (%s); Python BM25", exc)
                use_pg_fts = False
        if not use_pg_fts:
            need_vec = (qvec is not None and not use_pg_vec)
            lex, matched = _bm25_py(load(need_vec), pq.terms,
                                    prefix_last=True)
            out["lex_engine"] = "python-bm25"
    # Vectors.
    vec: list[tuple[int, float]] = []
    if qvec is not None and any(qvec):
        minimum = vec_min()
        if use_pg_vec:
            try:
                vec = _vec_pg(allowed, kinds, qvec, minimum=minimum,
                              wide=len(allowed) > 1)
                out["vec_engine"] = "pgvector"
                out["mode"] = "hybrid"
            except Exception as exc:   # noqa: BLE001
                log.warning("search: pgvector query failed (%s); Python "
                            "cosine", exc)
                use_pg_vec = False
        if not use_pg_vec:
            pool = load(True)
            with_vec = sum(1 for c in pool
                           if c.vec_blob and c.vec_version == version)
            if with_vec and with_vec <= py_vector_max_docs():
                vec = _vec_py(pool, qvec, version, minimum=minimum)
                out["vec_engine"] = "python-cosine"
                out["mode"] = "hybrid-py"
            else:
                out["vec_engine"] = ("skipped: too many documents"
                                     if with_vec else
                                     "skipped: no current vectors")
    # Matched terms for PG-FTS hits (the explanation needs them too).
    if lex and not matched:
        matched = _matched_terms([d for d, _s in lex], allowed, pq.terms)
    w_lex, w_vec = weights()
    fused = rrf([(w_lex, lex), (w_vec, vec)])
    for did in out["id_hits"]:
        fused.setdefault(did, 0.0)
    out.update(lex=lex, vec=vec, matched=matched, fused=fused)
    return out


def _matched_terms(ids: list[int], allowed, terms: list[Term]
                   ) -> dict[int, set[str]]:
    from sqlalchemy import select

    from engine import db as _db
    from engine.db import SearchDoc
    stems = {t.stem for t in terms}
    out: dict[int, set[str]] = {}
    if not ids:
        return out
    with _db.session_scope() as sess:
        stmt = _filtered(select(SearchDoc.id, SearchDoc.lex_title,
                                SearchDoc.lex_body), allowed, None).where(
            SearchDoc.id.in_(ids))
        for did, lt, lb in sess.execute(stmt):
            words = set((lt or "").split()) | set((lb or "").split())
            hit = stems & words
            if hit:
                out[int(did)] = hit
    return out


# ── Quick mode (typeahead) ────────────────────────────────────────────

def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def quick_search(q: str, allowed: list[str], *, kinds=None,
                 limit: int = QUICK_LIMIT,
                 per_group: int = QUICK_PER_GROUP,
                 project_names: Mapping[str, str] | None = None) -> dict:
    """Typeahead: ID and title (prefix) matches only, lexical, no
    explanation, never queues a reindex (ADR §4.8). One indexed-column
    LIKE scan over chunk-0 titles, scored in Python."""
    from sqlalchemy import or_, select

    from engine import db as _db
    from engine.db import SearchDoc
    from engine.search.text import analyse, normalise, parse_id_query

    t0 = time.perf_counter()
    if allowed is None:
        raise TenancyError("search requires an explicit list of project ids")
    names = dict(project_names or {})
    kinds = sorted(set(kinds)) if kinds else None
    norm = normalise(q)
    a = analyse(q)
    terms: list[str] = []
    for i, s in enumerate(a.stems):
        if s and s not in terms:
            terms.append(s)
        for tr in a.token_translations[i]:
            if tr not in terms:
                terms.append(tr)
    # The last word as typed is a prefix in progress: "regi" must find
    # "registration" although its stem is not "regi".
    if a.tokens and a.tokens[-1] not in terms and len(a.tokens[-1]) >= 2:
        terms.append(a.tokens[-1])
    id_query = parse_id_query(q)
    id_hits = set(_exact_id_hits(allowed, kinds, q, id_query))

    rows = []
    if terms or id_hits:
        conds = [SearchDoc.lex_title.like(f"%{_like_escape(t)}%", escape="\\")
                 for t in terms[:6]]
        if id_hits:
            conds.append(SearchDoc.id.in_(sorted(id_hits)))
        stmt = _filtered(select(
            SearchDoc.id, SearchDoc.project_id, SearchDoc.kind,
            SearchDoc.ref, SearchDoc.title, SearchDoc.lex_title,
            SearchDoc.url, SearchDoc.facets, SearchDoc.source_updated_at),
            allowed, kinds).where(SearchDoc.chunk_no == 0).where(
                or_(*conds)).limit(400)
        with _db.session_scope() as sess:
            rows = sess.execute(stmt).all()

    scored = []
    for r in rows:
        did, pid, kind, ref, title, lex_title, url, facets, upd = r
        words = (lex_title or "").split()
        hit = 0
        for t in terms:
            if any(w.startswith(t) for w in words):
                hit += 1
        s = hit / max(1, len(terms))
        tnorm = normalise(title or "")
        bare = tnorm.split(" · ", 1)[-1]
        if norm and (bare.startswith(norm) or tnorm.startswith(norm)):
            s += 0.25
        if did in id_hits:
            s += BOOST_ID * 10
        if s <= 0:
            continue
        scored.append((s, _utc_ts(upd), did, {
            "doc_id": did, "kind": kind, "ref": ref, "title": title,
            "url": url, "facets": facets or {},
            "project": ({"id": pid, "name": names.get(pid)} if pid else None),
            "score": round(s, 4)}))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    results: list[dict] = []
    groups: dict[str, list[dict]] = {}
    for _s, _ts, _did, item in scored:
        if len(results) >= limit:
            break
        g = groups.setdefault(item["kind"], [])
        if len(g) >= per_group:
            continue
        g.append(item)
        results.append(item)
    return {
        "query": {"raw": q, "normalised": norm, "lang": a.lang,
                  "id": (f"{id_query[0]}-{id_query[1]}" if id_query
                         else None)},
        "mode": "lexical", "quick": True, "results": results,
        "groups": groups, "group_order": list(groups),
        "took_ms": round((time.perf_counter() - t0) * 1000, 2),
    }


# ── Similar items (for contextual hints) ──────────────────────────────

def similar_to(project_id: str, kind: str, ref: str, *, allowed: list[str],
               k: int = 3, minimum: float = 0.75) -> list[dict]:
    """Vector neighbours of one document inside its own project — the
    "possible duplicate" / "related bug" hints (ADR §4.8). Zero tokens.
    *allowed* must contain *project_id*; otherwise nothing comes back."""
    from sqlalchemy import select

    from engine import db as _db
    from engine.db import SearchDoc
    from engine.search.embed import NotConfigured, from_blob, get_embedder
    if project_id not in set(allowed or []):
        return []
    try:
        version = get_embedder().version
    except NotConfigured:
        return []
    with _db.session_scope() as sess:
        rows = sess.execute(_filtered(select(
            SearchDoc.id, SearchDoc.kind, SearchDoc.ref, SearchDoc.title,
            SearchDoc.url, SearchDoc.facets, SearchDoc.vec_blob,
            SearchDoc.vec_version), [project_id], None,
            include_global=False).where(SearchDoc.chunk_no == 0)).all()
    me = next((r for r in rows if r[1] == kind and r[2] == ref), None)
    if me is None or not me[6] or me[7] != version:
        return []
    qv = from_blob(me[6])
    nz = [(i, v) for i, v in enumerate(qv) if v]
    out = []
    for r in rows:
        if r[0] == me[0] or not r[6] or r[7] != version:
            continue
        d = from_blob(r[6])
        if len(d) != len(qv):
            continue
        s = sum(v * d[i] for i, v in nz)
        if s >= minimum:
            out.append((s, r))
    out.sort(key=lambda x: (-x[0], x[1][0]))
    return [{"kind": r[1], "ref": r[2], "title": r[3], "url": r[4],
             "facets": r[5] or {}, "similarity": round(s, 3)}
            for s, r in out[:k]]


__all__ = ["search", "quick_search", "similar_to", "SearchParams",
           "TenancyError", "parse_query", "rrf", "snippet", "weights",
           "tsquery_text", "fts_statement", "corpus_stats_statement",
           "vector_statement", "module_phrase",
           "ref_number", "vocabulary_for", "clear_caches", "Vocabulary",
           "ID_KINDS", "FACET_FILTERS"]
