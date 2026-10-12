"""engine.search.embed — the deterministic hash embedder (ADR 0003 §3 (d)).

Pure unit tests: no database, no Flask app (ADR 0003 §7.1).
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from array import array
from pathlib import Path

import pytest

from engine.search import embed as E
from engine.search import text as T

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def emb() -> E.HashEmbedder:
    return E.get_embedder("hash")


def _cos(emb, a: str, b: str) -> float:
    return E.cosine(emb.embed(a), emb.embed(b))


class TestShape:
    def test_dimensions_and_version(self, emb):
        concepts = json.loads((T.LEXICON_DIR / "concepts.json")
                              .read_text(encoding="utf-8"))["concepts"]
        assert len(concepts) == emb.concept_dims == 32
        assert emb.dims == 32 + E.TRIGRAM_DIMS == 288
        assert emb.version == (f"hash-v{T.LEXICON_VERSION}."
                               f"{E.HASH_ALGO_VERSION}-288")
        # search_doc.vec_version is VARCHAR(24).
        assert len(emb.version) <= 24

    def test_every_concept_has_keywords_in_all_three_languages(self):
        concepts = json.loads((T.LEXICON_DIR / "concepts.json")
                              .read_text(encoding="utf-8"))["concepts"]
        for c in concepts:
            for lang in ("en", "ua", "ru"):
                assert c.get(lang), (c["id"], lang)

    def test_vector_is_float32_unit_length(self, emb):
        v = emb.embed("Login fails with a valid password")
        assert isinstance(v, array) and v.typecode == "f"
        assert len(v) == emb.dims
        assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0,
                            rel_tol=1e-5)

    def test_empty_text_is_the_zero_vector(self, emb):
        v = emb.embed("")
        assert len(v) == emb.dims and not any(v)
        assert E.cosine(v, emb.embed("login")) == 0.0

    def test_blob_round_trip(self, emb):
        v = emb.embed("Cart total is wrong after a coupon")
        blob = E.to_blob(v)
        assert len(blob) == emb.dims * 4 == 1152
        assert E.from_blob(blob) == v

    def test_embed_many(self, emb):
        texts = ["login", "cart"]
        assert emb.embed_many(texts) == [emb.embed(t) for t in texts]

    def test_satisfies_the_protocol(self, emb):
        assert isinstance(emb, E.Embedder)


class TestDeterminism:
    #: Snapshot of a few components of one vector. A change here means
    #: every stored vector is stale: bump LEXICON_VERSION or
    #: HASH_ALGO_VERSION in the same commit, which invalidates the index.
    SNAPSHOT_TEXT = "Не працює вхід: login fails with HTTP 500"

    def test_same_text_same_vector(self, emb):
        assert emb.embed(self.SNAPSHOT_TEXT) == emb.embed(self.SNAPSHOT_TEXT)

    def test_identical_across_processes_with_different_hash_seeds(self, emb):
        """Python's ``hash()`` is salted per process. A vector built from it
        would differ after every restart and the stored index would stop
        matching queries without a single error (ADR 0003 §5 risks)."""
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "from engine.search.embed import get_embedder, to_blob;"
            "print(to_blob(get_embedder('hash').embed(sys.argv[2])).hex())")
        outputs = set()
        for seed in ("1", "2", "random"):
            env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONUTF8="1")
            env.pop("EMBEDDINGS_PROVIDER", None)
            res = subprocess.run(
                [sys.executable, "-c", code, str(ROOT), self.SNAPSHOT_TEXT],
                capture_output=True, text=True, env=env, timeout=120,
                encoding="utf-8")
            assert res.returncode == 0, res.stderr
            outputs.add(res.stdout.strip())
        assert len(outputs) == 1
        assert outputs.pop() == E.to_blob(emb.embed(self.SNAPSHOT_TEXT)).hex()

    def test_bucket_is_a_stable_digest(self):
        # blake2b, two bytes: pinned so an accidental switch to hash() or to
        # another digest fails here rather than in production.
        assert E._bucket("#lo") == E._bucket("#lo")
        import hashlib
        v = int.from_bytes(hashlib.blake2b(b"log", digest_size=2).digest(),
                           "big")
        assert E._bucket("log") == (v % 256, 1.0 if (v >> 8) & 1 else -1.0)


class TestSimilarity:
    def test_login_in_three_languages(self, emb):
        """ADR 0003 §7.1: "вхід"/"вход"/"login" have cosine > 0.5."""
        assert _cos(emb, "вхід", "login") > 0.5
        assert _cos(emb, "вход", "login") > 0.5
        assert _cos(emb, "вхід", "вход") > 0.5

    def test_ua_query_lands_near_its_english_bug(self, emb):
        query = "не можу увійти"
        near = _cos(emb, query, "login fails")
        for unrelated in ("export report to pdf",
                          "cart quantity is not updated",
                          "search results are sorted in the wrong order",
                          "product image upload is slow"):
            assert near > _cos(emb, query, unrelated) + 0.3, unrelated
        assert near > 0.5

    @pytest.mark.parametrize("query, related, unrelated", [
        ("user cannot log in with valid password",
         "login fails with correct credentials",
         "product image upload is slow"),
        ("payment by card fails at checkout",
         "checkout rejects a valid credit card payment",
         "profile avatar is not displayed"),
        ("add item to cart", "cart quantity does not update after adding",
         "password reset email is never sent"),
        ("оплата карткою не проходить", "payment by card fails at checkout",
         "search results are sorted wrong"),
        ("не работает поиск", "search returns no results",
         "logout button is misaligned"),
        ("помилка 500 при збереженні профілю",
         "error 500 when saving the profile settings",
         "cart discount coupon not applied"),
    ])
    def test_related_qa_phrases_score_higher(self, emb, query, related,
                                             unrelated):
        assert _cos(emb, query, related) > _cos(emb, query, unrelated)

    def test_concepts_explain_a_match(self, emb):
        assert emb.concepts_of("не можу увійти") == ["auth"]
        assert set(emb.concepts_of("login fails")) == {"auth", "error"}
        assert emb.concepts_of("") == []

    def test_a_word_and_its_translation_count_once(self, emb):
        # "вхід" matches the UA keyword and its translation "login" matches
        # the EN one — one word, one hit.
        hits = emb.concept_hits(T.analyse("вхід"))
        assert sum(hits) == 1


class TestProviders:
    def test_default_is_hash(self, monkeypatch):
        monkeypatch.delenv("EMBEDDINGS_PROVIDER", raising=False)
        assert E.get_embedder().name == "hash"
        monkeypatch.setenv("EMBEDDINGS_PROVIDER", "  HASH ")
        assert E.get_embedder().name == "hash"

    def test_voyage_is_a_clear_configuration_error(self, monkeypatch):
        monkeypatch.setenv("EMBEDDINGS_PROVIDER", "voyage")
        with pytest.raises(E.NotConfigured, match="voyage"):
            E.get_embedder()

    def test_unknown_provider_is_refused_not_swapped(self, monkeypatch):
        monkeypatch.setenv("EMBEDDINGS_PROVIDER", "fastembed")
        with pytest.raises(E.NotConfigured, match="fastembed"):
            E.get_embedder()

    def test_active_dims_survives_a_bad_provider(self, monkeypatch):
        # The schema migration sizes vector(N) from this and must not fail
        # the boot over a typo in the environment.
        monkeypatch.setenv("EMBEDDINGS_PROVIDER", "voyage")
        assert E.active_dims() == 288

    def test_cosine_handles_unnormalised_input(self):
        assert math.isclose(E.cosine([2.0, 0.0], [5.0, 0.0]), 1.0)
        assert E.cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
