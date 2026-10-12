"""The search layer's memory promise (ADR 0003 §4.11).

The launchpad container has 512 MB shared with Chromium, which is why the
ADR rejected an in-process neural model. These tests hold the two things
that decision rests on:

* ``import engine.search`` (and its text and embedding modules) pulls in
  neither NumPy nor onnxruntime — a transitive import of either would be
  tens of MB of RSS before a single query.
* Embedding 2,000 documents peaks under 40 MB of Python allocations,
  lexicon loading included.

Both run in a fresh subprocess. In-process, the answer would depend on
whatever the other ~5,000 tests had already imported or cached — a clean
interpreter is the only honest measurement.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(code: str) -> str:
    env = dict(os.environ, PYTHONUTF8="1")
    env.pop("EMBEDDINGS_PROVIDER", None)
    res = subprocess.run([sys.executable, "-c", code, str(ROOT)],
                         capture_output=True, text=True, env=env,
                         timeout=300, encoding="utf-8")
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def test_import_pulls_in_no_numpy_or_onnxruntime():
    out = _run(
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "import engine.search, engine.search.text, engine.search.embed;"
        "engine.search.embed.get_embedder().embed('не можу увійти');"
        "heavy = sorted(m for m in sys.modules if m.split('.')[0] in "
        "('numpy', 'onnxruntime', 'torch', 'fastembed', 'sentence_transformers'));"
        "print(','.join(heavy) or 'none')")
    assert out == "none", f"engine.search imported {out}"


def test_istqb_rag_does_not_load_a_stemmer_it_never_uses():
    # istqb_rag now imports its tokeniser from engine.search.text; the
    # Snowball stemmers there are lazy, so the ISTQB path stays as cheap as
    # it was.
    out = _run(
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "import engine.istqb_rag as r; r._tokenize('boundary value analysis');"
        "print('snowballstemmer' in sys.modules)")
    assert out == "False"


def test_embedding_2k_documents_peaks_under_40_mb():
    out = _run(
        "import sys, tracemalloc; sys.path.insert(0, sys.argv[1]);"
        "tracemalloc.start();"
        "from engine.search.embed import get_embedder, to_blob;"
        "e = get_embedder();"
        "topics = ['login fails with valid password', 'не працює оплата карткою',"
        " 'корзина не обновляется', 'export report to PDF is slow',"
        " 'session timeout logs the user out', 'помилка 500 при збереженні'];"
        "blobs = [to_blob(e.embed(f'BUG-{i} {topics[i % 6]} on page {i} step {i % 9}"
        " expected result differs, browser chrome {i % 7}')) for i in range(2000)];"
        "_cur, peak = tracemalloc.get_traced_memory();"
        "print(len(blobs), peak)")
    count, peak = (int(x) for x in out.split())
    assert count == 2000
    assert peak < 40 * 1024 * 1024, f"peak {peak / 1048576:.1f} MB"
