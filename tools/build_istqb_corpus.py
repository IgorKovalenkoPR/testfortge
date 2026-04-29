"""TestForTge — Build the ISTQB corpus consumed by the RAG retriever.

Run once (or whenever the source PDFs change) to (re)generate
``engine/istqb_corpus.json``. The output is what
``engine.istqb_rag`` loads at runtime; the PDFs themselves are NOT
shipped to production.

Usage::

    python tools/build_istqb_corpus.py

Sources (paths can be overridden by ``ISTQB_SYLLABUS_PDF`` and
``ISTQB_BOOK_PDF`` env vars):

  * ``uploads/ISTQB_CTFL_Syllabus_v4.0.1.pdf`` — official v4.0.1 syllabus.
  * ``uploads/ISTQB Certified Tester Foundation Level_book.pdf`` —
    self-study textbook by Stapp / Roman / Pilaeten.

The output JSON is a list of chunks::

    {"id": int, "source": "syllabus" | "book",
     "page": int, "text": str}

Each chunk targets ~400 characters, split on paragraph boundaries when
possible. We drop chunks that look like running headers / footers,
page numbers, ToC noise, etc. — the runtime retriever assumes every
chunk is real content.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Iterator

try:
    import pypdf
except ImportError:
    sys.exit("pypdf is required — pip install pypdf")


# ── Paths ──────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SYLLABUS = ROOT / "uploads" / "ISTQB_CTFL_Syllabus_v4.0.1.pdf"
DEFAULT_BOOK = ROOT / "uploads" / "ISTQB Certified Tester Foundation Level_book.pdf"
OUTPUT_PATH = ROOT / "engine" / "istqb_corpus.json"


# ── Tunables ───────────────────────────────────────────────────────

# Target chunk size in characters. Picked to be small enough that
# BM25 highlights tight passages, and large enough to retain context.
CHUNK_TARGET = 420
CHUNK_MAX = 600
CHUNK_MIN = 60     # below this, drop — likely a stray heading

# Soft skip rules — anything matching is dropped.
NOISE_PATTERNS = [
    re.compile(r"^v?\d+(?:\.\d+){1,3}\s*$"),                      # version-only
    re.compile(r"^Page\s+\d+(\s+of\s+\d+)?$", re.IGNORECASE),
    re.compile(r"^\d+\s*/\s*\d+$"),                                # 12/78 page numbers
    re.compile(r"^\d{1,3}\s*$"),                                   # bare numbers
    re.compile(r"^©\s*\d{4}\b.*$"),                                # copyright
    re.compile(r"^\s*ISTQB\s*®?\s*$", re.IGNORECASE),
    re.compile(r"^\s*Certified Tester\s*$", re.IGNORECASE),
    re.compile(r"^\s*Foundation Level Syllabus\s*$", re.IGNORECASE),
]


# ── PDF → page text → paragraphs → chunks ──────────────────────────

def _pages(path: Path) -> Iterator[tuple[int, str]]:
    reader = pypdf.PdfReader(str(path))
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        text = _clean_page(text)
        if text:
            yield i, text


def _clean_page(text: str) -> str:
    """Strip page-level boilerplate without losing content."""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            lines.append("")
            continue
        if any(p.match(line) for p in NOISE_PATTERNS):
            continue
        # Crude header / footer detection — short ALL CAPS lines often
        # are headers/footers (e.g. "FUNDAMENTALS OF TESTING").
        # We keep them only when ≥3 words to avoid losing real headings.
        if (len(line) <= 50
                and line == line.upper()
                and len(line.split()) <= 2
                and re.search(r"[A-Z]", line)):
            continue
        lines.append(line)
    out = "\n".join(lines)
    # Collapse multi-blank-lines.
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _split_paragraphs(text: str) -> list[str]:
    """Greedy paragraph split — empty line is the strongest cue."""
    raw = re.split(r"\n\s*\n", text)
    out = []
    for p in raw:
        p = p.strip()
        if not p:
            continue
        # If a paragraph runs WAY too long, sentence-split inside it.
        if len(p) > CHUNK_MAX * 2:
            out.extend(re.split(r"(?<=[.!?])\s+(?=[A-ZА-ЯІЇЄ])", p))
        else:
            out.append(p)
    return [p.strip() for p in out if p.strip()]


def _pack_chunks(paragraphs: list[str]) -> list[str]:
    """Greedy pack of consecutive paragraphs up to CHUNK_TARGET."""
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for p in paragraphs:
        # Single huge paragraph → emit alone, possibly split further.
        if len(p) > CHUNK_MAX:
            if buf:
                chunks.append(" ".join(buf).strip())
                buf, buf_len = [], 0
            # Sentence-split
            sentences = re.split(r"(?<=[.!?])\s+", p)
            curr = []
            curr_len = 0
            for s in sentences:
                if curr and curr_len + len(s) + 1 > CHUNK_MAX:
                    chunks.append(" ".join(curr).strip())
                    curr, curr_len = [], 0
                curr.append(s)
                curr_len += len(s) + 1
            if curr:
                chunks.append(" ".join(curr).strip())
            continue

        if buf_len + len(p) + 1 > CHUNK_TARGET and buf:
            chunks.append(" ".join(buf).strip())
            buf, buf_len = [], 0
        buf.append(p)
        buf_len += len(p) + 1

    if buf:
        chunks.append(" ".join(buf).strip())
    # Filter chunks shorter than CHUNK_MIN — they rarely help retrieval.
    return [c for c in chunks if len(c) >= CHUNK_MIN]


def _chunks_for_pdf(path: Path, source_label: str,
                    starting_id: int) -> list[dict]:
    out: list[dict] = []
    next_id = starting_id
    for page_no, page_text in _pages(path):
        paras = _split_paragraphs(page_text)
        for chunk in _pack_chunks(paras):
            out.append({
                "id": next_id,
                "source": source_label,
                "page": page_no,
                "text": chunk,
            })
            next_id += 1
    return out


# ── Entry-point ────────────────────────────────────────────────────

def main() -> None:
    syllabus = Path(os.environ.get("ISTQB_SYLLABUS_PDF") or DEFAULT_SYLLABUS)
    book = Path(os.environ.get("ISTQB_BOOK_PDF") or DEFAULT_BOOK)

    all_chunks: list[dict] = []
    for path, label in [(syllabus, "syllabus"), (book, "book")]:
        if not path.is_file():
            print(f"  skip: {path} (not found)")
            continue
        print(f"  parsing {label}: {path}")
        chunks = _chunks_for_pdf(path, label, len(all_chunks))
        print(f"    -> {len(chunks)} chunks")
        all_chunks.extend(chunks)

    if not all_chunks:
        sys.exit("No chunks produced — check that the PDFs exist.")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump({"chunks": all_chunks}, f, ensure_ascii=False)

    avg = sum(len(c["text"]) for c in all_chunks) / len(all_chunks)
    print(f"\nWrote {len(all_chunks)} chunks to {OUTPUT_PATH}")
    print(f"  avg chunk length: {avg:.0f} chars")
    print(f"  output size: {OUTPUT_PATH.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
