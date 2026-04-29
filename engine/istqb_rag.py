"""TestForTge — ISTQB RAG retriever (BM25 over the syllabus + book).

This module powers the "deep" branch of Tedgie's ISTQB Q&A: when a
user's question is too open-ended to match the curated topic / glossary
keyword tables in :mod:`engine.istqb_knowledge`, we fall back to a
BM25 retriever over a chunked corpus of the official syllabus and the
self-study textbook.

Design choices
--------------
* **Pure-Python BM25.** No FAISS, no embeddings API, no NumPy dep.
  BM25-Okapi is small (~50 LOC) and ranks well for term-heavy
  domain queries — exactly the kind of question Tedgie sees.

* **Lazy load.** The corpus JSON is loaded the first time
  :func:`search` is called, not at import time. That keeps cold-start
  cheap and avoids paying the cost when the user never asks an open
  question.

* **No external deps at runtime** — the corpus JSON is built offline
  by ``tools/build_istqb_corpus.py`` and committed to the repo.

Returned answers
----------------
:func:`answer` returns a :class:`ChatReply` (or ``None`` when no chunk
clears the relevance bar). The body is the verbatim chunk text — we
explicitly do *not* paraphrase so a candidate studying for the exam
gets words straight from the source. Source + page reference is shown
inline so they can verify and cross-check.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.log import get_logger

log = get_logger(__name__)


# ── Corpus location ────────────────────────────────────────────────

DEFAULT_PATH = Path(__file__).resolve().parent / "istqb_corpus.json"


# ── BM25 tunables ──────────────────────────────────────────────────

K1 = 1.5      # term-saturation
B = 0.75      # length-norm
# Score below which we surrender and let the chatbot fall through to
# the requirement-clarification branch — tuned empirically on the
# syllabus chunk distribution.
MIN_RELEVANCE = 4.0
# Maximum chunks to merge into a single answer when several score high.
TOP_K_MERGE = 1


# ── Tokeniser (UA + EN) ────────────────────────────────────────────

# Allow word characters from both Latin and Cyrillic alphabets.
_TOKEN = re.compile(r"[A-Za-zЀ-ӿ][A-Za-z0-9Ѐ-ӿ\-]{1,}",
                     re.UNICODE)

# Stop-words that drown out signal in BM25. Kept short — broad
# stop-word lists actually hurt recall on terminology-heavy queries.
_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "is", "are", "of", "to", "and", "or", "in",
    "for", "on", "at", "with", "by", "as", "be", "it", "from", "this",
    "that", "these", "those", "what", "which", "how",
    "є", "та", "і", "у", "в", "що", "як", "це", "до", "на", "по",
    "the", "of",
})


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text or "")
            if t.lower() not in _STOPWORDS]


# ── Index ──────────────────────────────────────────────────────────

@dataclass
class _Chunk:
    cid: int
    source: str
    page: int
    text: str
    tokens: list[str]
    length: int


_lock = threading.Lock()
_loaded: bool = False
_chunks: list[_Chunk] = []
_idf: dict[str, float] = {}
_avg_len: float = 0.0


def _load(path: Path | None = None) -> None:
    """Read JSON, tokenise, precompute IDF + avg doc length."""
    global _loaded, _chunks, _idf, _avg_len
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        target = path or DEFAULT_PATH
        if not target.is_file():
            log.warning("ISTQB corpus not found at %s — RAG disabled", target)
            _loaded = True
            return
        try:
            with target.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            log.warning("ISTQB corpus failed to parse: %s", exc)
            _loaded = True
            return

        chunks_raw = data.get("chunks") or []
        for entry in chunks_raw:
            tokens = _tokenize(entry.get("text", ""))
            if not tokens:
                continue
            _chunks.append(_Chunk(
                cid=int(entry.get("id", 0)),
                source=str(entry.get("source", "")),
                page=int(entry.get("page", 0)),
                text=str(entry.get("text", "")),
                tokens=tokens,
                length=len(tokens),
            ))

        if not _chunks:
            log.warning("ISTQB corpus is empty after tokenisation")
            _loaded = True
            return

        # IDF (BM25 variant): log((N − df + 0.5) / (df + 0.5) + 1)
        N = len(_chunks)
        df: dict[str, int] = {}
        for c in _chunks:
            for tok in set(c.tokens):
                df[tok] = df.get(tok, 0) + 1
        _idf = {
            tok: math.log((N - n + 0.5) / (n + 0.5) + 1.0)
            for tok, n in df.items()
        }
        _avg_len = sum(c.length for c in _chunks) / N
        _loaded = True
        log.info("ISTQB RAG ready: %d chunks, avg=%0.1f tokens", N, _avg_len)


# ── BM25 score ─────────────────────────────────────────────────────

def _score(query_tokens: list[str], chunk: _Chunk) -> float:
    if not query_tokens or chunk.length == 0:
        return 0.0
    tf = Counter(chunk.tokens)
    norm = K1 * (1 - B + B * (chunk.length / _avg_len))
    s = 0.0
    for tok in query_tokens:
        idf = _idf.get(tok)
        if not idf:
            continue
        f = tf.get(tok, 0)
        if f == 0:
            continue
        s += idf * (f * (K1 + 1)) / (f + norm)
    return s


def search(query: str, k: int = 3) -> list[dict[str, Any]]:
    """Return up to *k* highest-scoring chunks, sorted by score.

    Each result dict carries: ``text``, ``source``, ``page``, ``score``."""
    _load()
    if not _chunks:
        return []
    q_tokens = _tokenize(query)
    if not q_tokens:
        return []
    scored = [
        (_score(q_tokens, c), c) for c in _chunks
    ]
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, c in scored[:k]:
        if score <= 0.0:
            continue
        out.append({
            "text": c.text,
            "source": c.source,
            "page": c.page,
            "score": round(float(score), 3),
        })
    return out


# ── ChatReply integration ──────────────────────────────────────────

def answer(query: str, lang: str = "en") -> "Any | None":
    """Return a ``ChatReply``-shaped answer when a chunk clears
    :data:`MIN_RELEVANCE`. Otherwise ``None`` so the dispatcher can
    fall through to the next branch."""
    # Local import to avoid a circular dependency between chatbot and rag.
    try:
        from .chatbot import ChatReply
    except Exception:
        return None

    hits = search(query, k=TOP_K_MERGE)
    if not hits:
        return None
    if hits[0]["score"] < MIN_RELEVANCE:
        return None

    parts = []
    for h in hits[:TOP_K_MERGE]:
        ref = (
            f"_{h['source'].title()} · page {h['page']}_"
            if lang != "ua"
            else f"_{h['source'].title()} · стор. {h['page']}_"
        )
        parts.append(f"{h['text']}\n\n{ref}")

    body = "\n\n— — —\n\n".join(parts)
    title = "ISTQB material — closest match" if lang != "ua" \
            else "Матеріали ISTQB — найближчий збіг"

    text = f"**{title}**\n\n{body}"

    intent = f"istqb_rag:{hits[0]['source']}:{hits[0]['page']}"
    return ChatReply(text=text, intent=intent, suggestions=[])


__all__ = ["search", "answer", "MIN_RELEVANCE"]
