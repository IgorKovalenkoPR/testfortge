"""Embeddings for platform search (ADR 0003 §3 option (d), §4.5).

The default provider is :class:`HashEmbedder`: a deterministic
feature-hashing vector, pure Python, ≈0 MB, 0 tokens. It is two blocks:

* **Concept block** — one dimension per QA concept in
  ``lexicon/concepts.json`` (login/auth, payment, cart, …), valued
  ``log1p(hits)``. A word counts once per concept even when both it and its
  translation match ("вхід" and "login" are one hit, not two).
* **Trigram block** — 256 signed buckets over the character trigrams of
  ``#stem#``, taken from the original stems **and** the translated English
  stems. ``bucket = blake2b(trigram, 2 bytes) % 256``, the sign is one more
  bit of the same digest.

Each block is L2-normalised, weighted 0.5/0.5, and the whole vector is
normalised again — so cosine similarity is a plain dot product.

**Never Python's ``hash()``.** It is salted per process
(``PYTHONHASHSEED``), so the same text would get a different vector after
every restart and the stored index would silently stop matching queries.
``tests/test_search_embed.py`` compares vectors across two subprocesses
with different seeds.

Other providers plug in through :class:`Embedder` and
``EMBEDDINGS_PROVIDER``; ``voyage`` is reserved (ADR §3 (b)) and raises
:class:`NotConfigured` until it is built.

Vectors are ``array('f')`` — 288 × 4 = 1,152 bytes, the exact layout of
``search_doc.vec_blob`` — never NumPy (ADR §4.11).
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
import threading
from array import array
from dataclasses import dataclass
from typing import Iterable, Protocol, runtime_checkable

from engine.log import get_logger
from engine.search import provider_name
from engine.search.text import (LEXICON_DIR, LEXICON_VERSION, Analysis,
                                 _cyrillic_stems, analyse, search_tokens, stem,
                                 trigrams)

log = get_logger(__name__)

#: Signed hashed character-trigram buckets.
TRIGRAM_DIMS = 256

#: Bump on any change to how a vector is computed from an :class:`Analysis`
#: (block weights, hashing, the trigram source). Lexicon changes bump
#: :data:`engine.search.text.LEXICON_VERSION` instead; both feed
#: :attr:`HashEmbedder.version`, which is what stored vectors are checked
#: against.
HASH_ALGO_VERSION = 1

_CONCEPT_WEIGHT = 0.5
_TRIGRAM_WEIGHT = 0.5

#: The longest multi-word concept keyword matched as an n-gram.
_MAX_NGRAM = 3


class NotConfigured(RuntimeError):
    """The configured embeddings provider cannot be used as configured."""


@runtime_checkable
class Embedder(Protocol):
    """What the indexer and the query pipeline need from a provider."""

    #: Short provider id: ``"hash"``, ``"voyage"``.
    name: str
    #: Vector length. Also the ``vector(N)`` of ``search_vector``.
    dims: int
    #: Stored with every vector (``search_doc.vec_version``); a vector whose
    #: version differs from the active one is stale and is not compared.
    version: str

    def embed(self, text: str) -> array: ...

    def embed_many(self, texts: Iterable[str]) -> list[array]: ...


# ── Concepts ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Concept:
    id: str
    label: str
    unigrams: frozenset[str]
    ngrams: frozenset[tuple[str, ...]]


def _keyword_stem_variants(keyword: str, lang: str) -> list[tuple[str, ...]]:
    """Every stem sequence a keyword can appear as in a document.

    English: one sequence. Cyrillic: one per Cyrillic stemmer, because a
    short document's language is a guess (see ``text._cyrillic_stems``).
    """
    tokens = search_tokens(keyword)
    if not tokens:
        return []
    if lang == "en":
        return [tuple(stem(t, "en") for t in tokens)]
    variants: set[tuple[str, ...]] = set()
    for code in ("ua", "ru"):
        variants.add(tuple(stem(t, code) for t in tokens))
    if len(tokens) == 1:
        variants |= {(s,) for s in _cyrillic_stems(tokens[0])}
    return sorted(variants)


def _load_concepts() -> tuple[Concept, ...]:
    with (LEXICON_DIR / "concepts.json").open(encoding="utf-8") as fh:
        raw = json.load(fh)
    out: list[Concept] = []
    for entry in raw["concepts"]:
        uni: set[str] = set()
        grams: set[tuple[str, ...]] = set()
        for lang in ("en", "ua", "ru"):
            for kw in entry.get(lang, ()):
                for seq in _keyword_stem_variants(kw, lang):
                    if len(seq) == 1:
                        uni.add(seq[0])
                    elif len(seq) <= _MAX_NGRAM:
                        grams.add(seq)
        out.append(Concept(id=entry["id"], label=entry.get("label", entry["id"]),
                           unigrams=frozenset(uni), ngrams=frozenset(grams)))
    ids = [c.id for c in out]
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate concept ids in concepts.json: {ids}")
    return tuple(out)


# ── The hash embedder ─────────────────────────────────────────────────

def _bucket(trigram: str) -> tuple[int, float]:
    """``(bucket, sign)`` from a stable digest — not ``hash()``."""
    digest = hashlib.blake2b(trigram.encode("utf-8"), digest_size=2).digest()
    value = int.from_bytes(digest, "big")
    return value % TRIGRAM_DIMS, (1.0 if (value >> 8) & 1 else -1.0)


def _l2(values: list[float]) -> float:
    return math.sqrt(sum(v * v for v in values))


class HashEmbedder:
    """Deterministic concept + hashed-trigram embedder (ADR 0003 §3 (d))."""

    name = "hash"

    def __init__(self) -> None:
        self.concepts: tuple[Concept, ...] = _load_concepts()
        self.concept_dims = len(self.concepts)
        self.dims = self.concept_dims + TRIGRAM_DIMS
        self.version = f"hash-v{LEXICON_VERSION}.{HASH_ALGO_VERSION}-{self.dims}"
        self._bucket_cache: dict[str, tuple[int, float]] = {}
        # Inverted indexes: stem (or stem n-gram) → concept positions, so a
        # document costs one dict lookup per word rather than one per
        # word per concept.
        uni: dict[str, list[int]] = {}
        grams: dict[tuple[str, ...], list[int]] = {}
        for ci, concept in enumerate(self.concepts):
            for s in concept.unigrams:
                uni.setdefault(s, []).append(ci)
            for g in concept.ngrams:
                grams.setdefault(g, []).append(ci)
        self._uni_index = {k: tuple(v) for k, v in uni.items()}
        self._ngram_index = {k: tuple(v) for k, v in grams.items()}

    # -- concepts ------------------------------------------------------

    def concept_hits(self, analysis: Analysis) -> list[int]:
        """Hits per concept, one per matching word or n-gram."""
        hits = [0] * self.concept_dims
        stems = analysis.stems
        translations = analysis.token_translations
        uni = self._uni_index
        for i, s in enumerate(stems):
            touched = set(uni.get(s, ()))
            for t in translations[i]:
                touched.update(uni.get(t, ()))
            for ci in touched:
                hits[ci] += 1
        grams = self._ngram_index
        if grams:
            for n in range(2, _MAX_NGRAM + 1):
                for i in range(len(stems) - n + 1):
                    for ci in grams.get(tuple(stems[i:i + n]), ()):
                        hits[ci] += 1
        return hits

    def concepts_of(self, text: str) -> list[str]:
        """Ids of the concepts *text* touches, strongest first — the
        ``why.concepts`` of a search result."""
        hits = self.concept_hits(analyse(text))
        order = sorted((i for i, h in enumerate(hits) if h),
                       key=lambda i: (-hits[i], i))
        return [self.concepts[i].id for i in order]

    # -- vectors -------------------------------------------------------

    def _trigram_bucket(self, tri: str) -> tuple[int, float]:
        hit = self._bucket_cache.get(tri)
        if hit is None:
            hit = _bucket(tri)
            # Bounded: a long-running indexer must not grow this forever.
            if len(self._bucket_cache) < 200_000:
                self._bucket_cache[tri] = hit
        return hit

    def embed_analysis(self, analysis: Analysis) -> array:
        concept = [math.log1p(h) for h in self.concept_hits(analysis)]
        tri_block = [0.0] * TRIGRAM_DIMS
        for s in analysis.all_stems:
            for tri in trigrams(s):
                bucket, sign = self._trigram_bucket(tri)
                tri_block[bucket] += sign

        out: list[float] = []
        for block, weight in ((concept, _CONCEPT_WEIGHT),
                              (tri_block, _TRIGRAM_WEIGHT)):
            norm = _l2(block)
            scale = weight / norm if norm else 0.0
            out.extend(v * scale for v in block)
        total = _l2(out)
        if total:
            out = [v / total for v in out]
        return array("f", out)

    def embed(self, text: str) -> array:
        """The vector of *text*: L2 norm 1, or all zeros for text with no
        indexable words."""
        return self.embed_analysis(analyse(text or ""))

    def embed_many(self, texts: Iterable[str]) -> list[array]:
        return [self.embed(t) for t in texts]


class VoyageEmbedder:
    """Reserved: Voyage AI ``voyage-3.5-lite`` (ADR 0003 §3 (b)).

    Switched on only if the golden set shows the hash embedder below the
    threshold, and only with a separate key stored in ``OrgSecret``. Not
    built yet — selecting it is a configuration error, reported as such
    rather than as a silent fall-back to another provider.
    """

    name = "voyage"
    dims = 512
    version = "voyage-3.5-lite-512"

    def __init__(self) -> None:
        raise NotConfigured(
            "EMBEDDINGS_PROVIDER=voyage is reserved (ADR 0003 §3 (b)) and not "
            "implemented yet. Unset EMBEDDINGS_PROVIDER or set it to 'hash'.")

    def embed(self, text: str) -> array:   # pragma: no cover — unreachable
        raise NotConfigured("voyage provider is not implemented")

    def embed_many(self, texts: Iterable[str]) -> list[array]:  # pragma: no cover
        raise NotConfigured("voyage provider is not implemented")


_PROVIDERS = {"hash": HashEmbedder, "voyage": VoyageEmbedder}
_INSTANCES: dict[str, Embedder] = {}
_LOCK = threading.Lock()


def get_embedder(provider: str | None = None) -> Embedder:
    """The embedder for *provider* (default: ``EMBEDDINGS_PROVIDER``,
    else ``hash``). One instance per provider per process.

    Raises :class:`NotConfigured` for an unknown or unavailable provider —
    never falls back silently, because vectors from two providers are not
    comparable and a quiet swap would corrupt the index.
    """
    name = (provider or provider_name()).strip().lower()
    inst = _INSTANCES.get(name)
    if inst is not None:
        return inst
    factory = _PROVIDERS.get(name)
    if factory is None:
        raise NotConfigured(
            f"unknown EMBEDDINGS_PROVIDER {name!r}; expected one of "
            f"{sorted(_PROVIDERS)}")
    with _LOCK:
        inst = _INSTANCES.get(name)
        if inst is None:
            inst = factory()
            _INSTANCES[name] = inst
    return inst


def active_dims() -> int:
    """Dimensions of the configured provider, for the ``vector(N)`` DDL.

    Used at boot by ``engine.db._ensure_search_schema``, which must never
    fail: a misconfigured provider is logged and the hash embedder's size
    is used, so the schema still comes up (search will run lexical-only
    until the provider is fixed).
    """
    try:
        return get_embedder().dims
    except NotConfigured as exc:
        log.warning("search: %s — sizing search_vector for the hash embedder",
                    exc)
        return get_embedder("hash").dims


# ── Vector helpers (pure Python; no NumPy) ────────────────────────────

def cosine(a: Iterable[float], b: Iterable[float]) -> float:
    """Cosine similarity. For unit vectors (everything :func:`embed`
    returns) this is the dot product; the norms are still divided out so a
    caller can pass anything."""
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if not na or not nb:
        return 0.0
    return dot / math.sqrt(na * nb)


def to_blob(vec: array | Iterable[float]) -> bytes:
    """``search_doc.vec_blob`` bytes: float32, little-endian on every host."""
    arr = vec if isinstance(vec, array) and vec.typecode == "f" else array("f", vec)
    if sys.byteorder == "big":   # pragma: no cover — no big-endian CI host
        arr = array("f", arr)
        arr.byteswap()
    return arr.tobytes()


def from_blob(blob: bytes) -> array:
    """Inverse of :func:`to_blob`."""
    arr = array("f")
    arr.frombytes(blob)
    if sys.byteorder == "big":   # pragma: no cover
        arr.byteswap()
    return arr


__all__ = ["Embedder", "HashEmbedder", "VoyageEmbedder", "NotConfigured",
           "get_embedder", "active_dims", "cosine", "to_blob", "from_blob",
           "TRIGRAM_DIMS", "HASH_ALGO_VERSION"]
