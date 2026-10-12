"""The search indexer — ADR 0003 §4.3 (what is indexed, chunking) and the
third layer of §4.4 (full-scope rehash).

One public verb per scope:

* :func:`reindex_project` — read every indexable row of one project in
  keyset batches of :data:`BATCH`, build the documents, compare each one's
  ``content_hash`` with what is stored, UPSERT only the changed ones and
  delete the ones whose source disappeared. Unchanged content is never
  rewritten, so running it on a project nobody touched is a read-only pass.
* :func:`index_global` — the same, for the read-only corpora every tenant
  shares (the Guide and the ISTQB syllabus). Keyed by a fingerprint of the
  source files, so an unchanged source is a no-op without even parsing it.
* :func:`drop_project` — the scope's documents and its state, gone.

Tenancy
-------
Every project document carries ``org_id`` and ``project_id`` copied from the
project row at index time; ``org_id`` is part of the content hash, so a
project that moves to an organisation has every document rewritten on the
next pass rather than keeping the old owner. Global documents have
``scope='global'``, ``project_id`` and ``org_id`` NULL and a ``kind`` from
:data:`GLOBAL_KINDS` — nothing else may be written with a NULL project.

Memory (ADR §4.11)
------------------
Source rows are read in keyset pages of 500 and expunged with the session
that read them; documents are written in batches of 500 in their own
transaction. The only per-scope structure held for the whole pass is the
``doc_key → (id, hash)`` map of what is already stored (≈100 bytes a
document). No NumPy, no model: the hash embedder is pure CPU.

Never calls an LLM or the network.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from engine.log import get_logger

log = get_logger(__name__)

#: Bump on any change to how a document is *built* from its source row
#: (fields, labels, chunking). Feeds the content hash, so a bump rewrites
#: every document on the next pass.
INDEXER_VERSION = 1

#: Rows read per keyset page and documents written per transaction.
BATCH = 500

#: Stored body is clipped to this many characters (ADR §4.1).
BODY_MAX = 4000
#: A body longer than this is split into windows …
CHUNK_THRESHOLD = 1500
#: … of about this size …
CHUNK_SIZE = 800
#: … overlapping by this much.
CHUNK_OVERLAP = 100
#: ``search_doc.title`` is VARCHAR(300).
TITLE_MAX = 300
#: ``search_doc.ref`` is VARCHAR(60).
REF_MAX = 60
#: A case result's note is usually a stack trace; the head is what matters.
CASE_NOTE_MAX = 600

PROJECT_KINDS: tuple[str, ...] = ("test_case", "checklist_item", "bug", "run",
                                  "case_result", "estimation")
#: The only kinds that may be stored with ``project_id`` NULL.
GLOBAL_KINDS: frozenset[str] = frozenset({"guide", "knowledge", "istqb"})

#: Case results worth finding: the ones somebody has to act on.
_CASE_RESULT_STATUSES = ("failed", "blocked")

_REPO = Path(__file__).resolve().parents[2]
GUIDE_DIR = _REPO / "templates" / "guide"
ISTQB_PATH = _REPO / "engine" / "istqb_corpus.json"


# ── Documents ─────────────────────────────────────────────────────────

@dataclass
class Draft:
    """One document before it is analysed: what a builder produces."""
    kind: str
    ref: str
    title: str
    body: str
    url: str | None = None
    facets: dict = field(default_factory=dict)
    chunk_no: int = 0
    source_updated_at: datetime | None = None


def doc_key(scope: str, project_id: str | None, kind: str, ref: str,
            chunk_no: int) -> str:
    """The upsert key — ``"{scope}:{project_id|-}:{kind}:{ref}:{chunk_no}"``."""
    return f"{scope}:{project_id or '-'}:{kind}:{ref}:{int(chunk_no)}"


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _join(*parts: Any) -> str:
    return "\n".join(p for p in (_clean(x) for x in parts) if p)


def _facets(**values: Any) -> dict:
    """Only the facets that have a value — ``None``/``""`` say nothing."""
    out: dict = {}
    for k, v in values.items():
        if v is None or v == "":
            continue
        out[k] = v
    return out


_BREAK = re.compile(r"(?<=[.!?…])\s+|\n+")


def chunk_body(body: str) -> list[str]:
    """Split *body* per ADR §4.3.

    At most :data:`CHUNK_THRESHOLD` characters → one chunk, unchanged.
    Longer → windows of about :data:`CHUNK_SIZE` characters, cut on a
    sentence or line boundary where one exists in the back half of the
    window, each starting :data:`CHUNK_OVERLAP` characters before the
    previous one ended. Always makes progress, so it terminates on text
    with no boundaries at all.
    """
    body = body or ""
    if len(body) <= CHUNK_THRESHOLD:
        return [body]
    chunks: list[str] = []
    start = 0
    n = len(body)
    while start < n:
        end = min(start + CHUNK_SIZE, n)
        if end < n:
            window = body[start:end]
            cut = None
            for m in _BREAK.finditer(window):
                if m.end() >= CHUNK_SIZE // 2:
                    cut = m.end()
            if cut:
                end = start + cut
        piece = body[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks or [body[:CHUNK_SIZE]]


def _expand(draft: Draft) -> Iterator[Draft]:
    """A draft → one draft per chunk, title repeated in each (ADR §4.3).
    The body is clipped to :data:`BODY_MAX` *before* chunking, so a 50 KB
    stack trace costs at most a handful of documents."""
    body = (draft.body or "")[:BODY_MAX]
    pieces = chunk_body(body)
    for i, piece in enumerate(pieces):
        yield Draft(kind=draft.kind, ref=draft.ref, title=draft.title,
                    body=piece, url=draft.url, facets=draft.facets,
                    chunk_no=i, source_updated_at=draft.source_updated_at)


def _active_vec_version() -> tuple[Any, str | None]:
    """``(embedder, version)`` — ``(None, None)`` when the configured
    provider cannot be used. The index then stores no vectors and search
    runs lexical, which is what ADR §3's fallback chain expects."""
    from engine.search.embed import NotConfigured, get_embedder
    try:
        emb = get_embedder()
        return emb, getattr(emb, "version", None)
    except NotConfigured as exc:
        log.warning("search indexer: %s — indexing without vectors", exc)
        return None, None


def content_hash(draft: Draft, *, org_id: str | None,
                 vec_version: str | None) -> str:
    """sha1 over everything a stored document is derived from.

    ADR §4.1 names title, body, facets and the lexicon version; this adds
    the URL, the tenant (so a project changing organisation rewrites its
    documents), the vector version (so a provider change re-embeds) and
    :data:`INDEXER_VERSION`.
    """
    from engine.search.text import LEXICON_VERSION
    payload = json.dumps(
        [draft.title, draft.body, draft.facets, draft.url, org_id,
         LEXICON_VERSION, vec_version, INDEXER_VERSION],
        sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _materialise(draft: Draft, embedder: Any) -> dict:
    """The analysed columns of one document: lang, lex forms, vector."""
    from engine.search.embed import to_blob
    from engine.search.text import analyse, detect_lang
    whole = f"{draft.title}\n{draft.body}"
    lang = detect_lang(whole) or "en"
    title_a = analyse(draft.title, lang)
    body_a = analyse(draft.body, lang)
    out: dict = {"lang": lang[:8], "lex_title": title_a.lex(),
                 "lex_body": body_a.lex(), "vec_blob": None, "_vec": None}
    if embedder is not None:
        try:
            vec = embedder.embed(whole)
            out["vec_blob"] = to_blob(vec)
            out["_vec"] = vec
        except Exception as exc:   # noqa: BLE001 — a vector is optional
            log.warning("search indexer: embedding failed for %s:%s: %s",
                        draft.kind, draft.ref, exc)
    return out


# ── Source readers (keyset-paged, never the whole table at once) ──────

def _paged(model, *filters, order_col=None) -> Iterator[list]:
    """Yield pages of at most :data:`BATCH` rows, keyset-paginated on the
    primary key, each page read in its own short session and detached."""
    from engine import db as _db
    col = order_col if order_col is not None else model.id
    last = None
    while True:
        with _db.session_scope() as sess:
            q = sess.query(model).filter(*filters)
            if last is not None:
                q = q.filter(col > last)
            rows = q.order_by(col).limit(BATCH).all()
            for r in rows:
                sess.expunge(r)
        if not rows:
            return
        yield rows
        last = getattr(rows[-1], col.key)
        if len(rows) < BATCH:
            return


def _ref(external_id: Any, row_id: Any) -> str:
    ref = _clean(external_id) or f"#{row_id}"
    return ref[:REF_MAX]


def _with_id(external_id: Any, text: Any) -> str:
    head = _clean(external_id)
    rest = _clean(text)
    return " · ".join(p for p in (head, rest) if p)[:TITLE_MAX]


def _iter_test_cases(project_id: str) -> Iterator[Draft]:
    from engine.db import TestCase
    for page in _paged(TestCase, TestCase.project_id == project_id):
        for tc in page:
            ref = _ref(tc.external_id, tc.id)
            yield Draft(
                kind="test_case", ref=ref,
                title=_with_id(tc.external_id, tc.summary),
                body=_join(tc.section, tc.preconditions, tc.test_steps,
                           tc.test_data, tc.expected_result, tc.issues,
                           tc.comment, tc.gherkin),
                url=f"/test-cases#{ref}",
                facets=_facets(status=tc.status, priority=tc.priority,
                               suite=tc.suite, category=tc.category,
                               testing_type=tc.testing_type,
                               section=tc.section),
                source_updated_at=tc.updated_at)


def _iter_checklist(project_id: str) -> Iterator[Draft]:
    from engine.db import ChecklistItem
    for page in _paged(ChecklistItem, ChecklistItem.project_id == project_id):
        for cl in page:
            ref = _ref(cl.external_id, cl.id)
            yield Draft(
                kind="checklist_item", ref=ref,
                title=_with_id(cl.external_id, cl.section),
                body=_join(cl.objective, cl.comments),
                url=f"/checklist#{ref}",
                facets=_facets(status=cl.status, priority=cl.priority,
                               depth=cl.depth, section=cl.section),
                source_updated_at=cl.updated_at)


def _iter_bugs(project_id: str) -> Iterator[Draft]:
    from engine.db import BugReport
    for page in _paged(BugReport, BugReport.project_id == project_id):
        for b in page:
            ref = _ref(b.external_id, b.id)
            yield Draft(
                kind="bug", ref=ref,
                title=_with_id(b.external_id, b.title),
                body=_join(b.steps_to_reproduce, b.actual_result,
                           b.expected_result, b.preconditions, b.environment,
                           b.comment),
                url=f"/bug-reports#{ref}",
                facets=_facets(severity=b.severity, priority=b.priority,
                               status=b.status, bug_area=b.bug_area,
                               run_id=b.run_id),
                source_updated_at=b.updated_at)


def _scalars(payload: Any, *, limit: int = 40) -> list[str]:
    """``key: value`` lines for the scalar entries of a JSON dict."""
    out: list[str] = []
    if not isinstance(payload, dict):
        return out
    for k in sorted(payload, key=str):
        v = payload[k]
        if isinstance(v, (str, int, float, bool)) and v != "":
            out.append(f"{k}: {v}")
        if len(out) >= limit:
            break
    return out


def _iter_runs(project_id: str) -> Iterator[Draft]:
    from engine.db import ExecutionRun
    for page in _paged(ExecutionRun, ExecutionRun.project_id == project_id):
        for r in page:
            env = r.env_payload or {}
            started = r.started_at.date().isoformat() if r.started_at else ""
            body = _join(r.base_url, *_scalars(env), *_scalars(r.stats or {}),
                         f"status: {r.status}" if r.status else "")
            yield Draft(
                kind="run", ref=f"run:{r.id}",
                title=" · ".join(p for p in (f"Run #{r.id}", started) if p),
                body=body,
                url=f"/test-execution#run-{r.id}",
                facets=_facets(status=r.status,
                               mode=(env.get("mode") if isinstance(env, dict)
                                     else None)),
                source_updated_at=r.finished_at or r.started_at)


def _iter_case_results(project_id: str) -> Iterator[Draft]:
    from sqlalchemy import func

    from engine.db import ExecutionCaseResult, ExecutionRun
    from engine import db as _db
    # Run ids first (small), then the failed/blocked results run by run.
    run_ids: list[int] = []
    with _db.session_scope() as sess:
        run_ids = [rid for (rid,) in sess.query(ExecutionRun.id).filter(
            ExecutionRun.project_id == project_id).order_by(ExecutionRun.id)]
    for start in range(0, len(run_ids), BATCH):
        ids = run_ids[start:start + BATCH]
        for page in _paged(
                ExecutionCaseResult,
                ExecutionCaseResult.run_id.in_(ids),
                func.lower(ExecutionCaseResult.status).in_(
                    _CASE_RESULT_STATUSES)):
            for cr in page:
                case = _clean(cr.case_external_id) or f"#{cr.case_id or cr.id}"
                yield Draft(
                    kind="case_result",
                    ref=f"run:{cr.run_id}:{case}"[:REF_MAX],
                    title=f"{case} · {cr.status}"[:TITLE_MAX],
                    body=_clean(cr.notes)[:CASE_NOTE_MAX],
                    url=f"/test-execution#run-{cr.run_id}",
                    facets=_facets(run_id=cr.run_id, status=cr.status),
                    source_updated_at=cr.created_at)


def _iter_estimation(project_id: str) -> Iterator[Draft]:
    from engine.db import Estimation
    from engine import db as _db
    with _db.session_scope() as sess:
        e = (sess.query(Estimation)
             .filter(Estimation.project_id == project_id)
             .order_by(Estimation.created_at.desc(), Estimation.id.desc())
             .first())
        if e is None:
            return
        sess.expunge(e)
    hours = e.total_hours
    title = "Estimate" + (f" · {hours:g} h" if isinstance(hours, (int, float))
                          else "")
    yield Draft(kind="estimation", ref="latest", title=title,
                body=_join(*_scalars(e.input_payload or {})),
                url="/estimation", facets={},
                source_updated_at=e.created_at)


_PROJECT_BUILDERS: dict[str, Callable[[str], Iterator[Draft]]] = {
    "test_case": _iter_test_cases,
    "checklist_item": _iter_checklist,
    "bug": _iter_bugs,
    "run": _iter_runs,
    "case_result": _iter_case_results,
    "estimation": _iter_estimation,
}


def iter_project_drafts(project_id: str) -> Iterator[Draft]:
    """Every chunked document of a project, streamed kind by kind."""
    for kind in PROJECT_KINDS:
        seen: set[str] = set()
        for draft in _PROJECT_BUILDERS[kind](project_id):
            # A duplicated public id would collide on doc_key and the second
            # row would silently overwrite the first. Unique indexes make it
            # rare; this makes it harmless.
            if draft.ref in seen:
                draft.ref = f"{draft.ref}~{len(seen)}"[:REF_MAX]
            seen.add(draft.ref)
            yield from _expand(draft)


# ── Global corpora ────────────────────────────────────────────────────

class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag, attrs) -> None:
        if tag in ("p", "li", "h3", "h4", "br", "tr", "pre"):
            self.parts.append("\n")


_TEMPLATE = re.compile(
    r'<template\s+data-content="([^"]+)"\s*>(.*?)</template>', re.S)
_JINJA = re.compile(r"\{#.*?#\}|\{%.*?%\}|\{\{.*?\}\}", re.S)


def _html_text(fragment: str) -> str:
    p = _TextOnly()
    p.feed(_JINJA.sub(" ", fragment))
    text = html.unescape("".join(p.parts))
    lines = (re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines())
    return "\n".join(ln for ln in lines if ln)


def _guide_files() -> list[tuple[str, Path]]:
    out = []
    for path in sorted(GUIDE_DIR.glob("_sections_*.html")):
        lang = path.stem.rsplit("_", 1)[-1]
        out.append((lang, path))
    return out


def _iter_guide() -> Iterator[Draft]:
    try:
        from engine.i18n import get_lang
    except Exception:   # noqa: BLE001 — titles are a nicety
        get_lang = None   # type: ignore[assignment]
    for lang, path in _guide_files():
        table = get_lang(lang) if get_lang else {}
        raw = path.read_text(encoding="utf-8")
        for section, fragment in _TEMPLATE.findall(raw):
            key = f"guide_card_{section.replace('-', '_')}_title"
            title = _clean((table or {}).get(key)) or section.replace("-", " ")
            yield from _expand(Draft(
                kind="guide", ref=f"{lang}:{section}"[:REF_MAX],
                title=f"Guide · {title}"[:TITLE_MAX],
                body=_html_text(fragment),
                url=f"/guide#{section}",
                facets=_facets(page=section, lang=lang)))


def _iter_istqb() -> Iterator[Draft]:
    """The **syllabus** half only (ADR §4.3): the textbook half stays in
    ``istqb_rag``'s BM25 with its 60-word excerpt cap and is never copied
    into the database."""
    if not ISTQB_PATH.is_file():
        return
    data = json.loads(ISTQB_PATH.read_text(encoding="utf-8"))
    for entry in data.get("chunks") or []:
        if str(entry.get("source") or "") != "syllabus":
            continue
        page = entry.get("page")
        cid = entry.get("id")
        yield from _expand(Draft(
            kind="istqb", ref=f"p{page}:{cid}"[:REF_MAX],
            title=f"ISTQB syllabus · p.{page}",
            body=_clean(entry.get("text")),
            url=None,
            facets=_facets(page=page, source="syllabus")))


#: corpus → (kinds it owns, its source files, its draft iterator)
GLOBAL_CORPORA: dict[str, tuple[frozenset[str], Callable[[], list[Path]],
                                Callable[[], Iterator[Draft]]]] = {
    "guide": (frozenset({"guide"}),
              lambda: [p for _l, p in _guide_files()], _iter_guide),
    "istqb": (frozenset({"istqb"}), lambda: [ISTQB_PATH], _iter_istqb),
}


def corpus_fingerprint(corpus: str) -> str:
    """sha1 over the corpus's source files plus every version that changes
    the documents built from them."""
    from engine.search.text import LEXICON_VERSION
    _kinds, files, _it = GLOBAL_CORPORA[corpus]
    _emb, vec_version = _active_vec_version()
    h = hashlib.sha1()
    h.update(f"{corpus}|{LEXICON_VERSION}|{vec_version}|{INDEXER_VERSION}"
             .encode())
    for path in files():
        h.update(path.name.encode())
        try:
            h.update(path.read_bytes())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()


# ── Writing ───────────────────────────────────────────────────────────

_SCOPE_LOCKS: dict[str, threading.Lock] = {}
_SCOPE_LOCKS_GUARD = threading.Lock()


def _scope_lock(key: str) -> threading.Lock:
    """One in-process lock per scope: two passes over the same project
    must not interleave their upserts (one would delete what the other
    just inserted as "gone")."""
    with _SCOPE_LOCKS_GUARD:
        lock = _SCOPE_LOCKS.get(key)
        if lock is None:
            lock = _SCOPE_LOCKS[key] = threading.Lock()
        return lock


def _vector_literal(vec: Iterable[float]) -> str:
    """pgvector's text input format: ``[0.1,0.2,…]``."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _mirror_vectors(sess, rows: list[tuple[int, str | None, Any]]) -> None:
    """Copy freshly written vectors into ``search_vector`` (Postgres with
    pgvector only). In a SAVEPOINT so a failure here cannot poison the
    transaction that holds the documents — ``vec_blob`` is the source of
    truth and the backfill's ``--to-pgvector`` can refill the mirror."""
    from engine.search import caps
    if not caps.get("pgvector") or not rows:
        return
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError
    stmt = text(
        "INSERT INTO search_vector (doc_id, project_id, embedding) "
        "VALUES (:id, :pid, CAST(:emb AS vector)) "
        "ON CONFLICT (doc_id) DO UPDATE SET "
        "embedding = EXCLUDED.embedding, project_id = EXCLUDED.project_id")
    try:
        with sess.begin_nested():
            for doc_id, pid, vec in rows:
                sess.execute(stmt, {"id": doc_id, "pid": pid,
                                    "emb": _vector_literal(vec)})
    except SQLAlchemyError as exc:
        log.warning("search indexer: pgvector mirror failed (%s); vec_blob "
                    "is intact, run search_backfill --to-pgvector",
                    str(exc).splitlines()[0] if str(exc) else exc)


@dataclass
class SyncResult:
    scope_key: str
    upserted: int = 0
    unchanged: int = 0
    deleted: int = 0
    doc_count: int = 0
    skipped: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return {"scope": self.scope_key, "upserted": self.upserted,
                "unchanged": self.unchanged, "deleted": self.deleted,
                "docs": self.doc_count, "skipped": self.skipped,
                "error": self.error}


def _existing_map(scope: str, project_id: str | None,
                  kinds: Iterable[str] | None) -> dict[str, tuple[int, str]]:
    from engine import db as _db
    from engine.db import SearchDoc
    out: dict[str, tuple[int, str]] = {}
    with _db.session_scope() as sess:
        q = sess.query(SearchDoc.doc_key, SearchDoc.id, SearchDoc.content_hash)
        if project_id is None:
            q = q.filter(SearchDoc.scope == scope,
                         SearchDoc.project_id.is_(None))
        else:
            q = q.filter(SearchDoc.project_id == project_id)
        if kinds is not None:
            q = q.filter(SearchDoc.kind.in_(list(kinds)))
        for key, did, h in q.yield_per(BATCH):
            out[key] = (did, h or "")
    return out


def _write_batch(batch: list[tuple[str, Draft, str]], *, scope: str,
                 project_id: str | None, org_id: str | None,
                 existing: dict[str, tuple[int, str]], embedder: Any,
                 vec_version: str | None) -> None:
    """UPSERT one batch of changed documents in one transaction."""
    from sqlalchemy.exc import IntegrityError

    from engine import db as _db
    from engine.db import SearchDoc
    now = datetime.now(timezone.utc)
    materialised = [(key, d, h, _materialise(d, embedder))
                    for key, d, h in batch]
    for attempt in (1, 2):
        try:
            with _db.session_scope() as sess:
                ids = [existing[k][0] for k, _d, _h, _m in materialised
                       if k in existing]
                rows = {}
                if ids:
                    rows = {r.doc_key: r for r in sess.query(SearchDoc)
                            .filter(SearchDoc.id.in_(ids))}
                written: list[tuple[SearchDoc, Any]] = []
                for key, d, h, m in materialised:
                    row = rows.get(key)
                    if row is None:
                        row = SearchDoc(doc_key=key)
                        sess.add(row)
                    row.scope = scope
                    row.org_id = org_id
                    row.project_id = project_id
                    row.kind = d.kind
                    row.ref = d.ref
                    row.chunk_no = d.chunk_no
                    row.title = (d.title or "")[:TITLE_MAX]
                    row.body = (d.body or "")[:BODY_MAX]
                    row.lang = m["lang"]
                    row.lex_title = m["lex_title"]
                    row.lex_body = m["lex_body"]
                    row.url = (d.url or None) and d.url[:300]
                    row.facets = d.facets or {}
                    row.content_hash = h
                    row.vec_version = vec_version if m["vec_blob"] else None
                    row.vec_blob = m["vec_blob"]
                    row.source_updated_at = d.source_updated_at
                    row.indexed_at = now
                    written.append((row, m.get("_vec")))
                sess.flush()
                for row, _vec in written:
                    existing[row.doc_key] = (row.id, row.content_hash or "")
                _mirror_vectors(sess, [(row.id, project_id, vec)
                                       for row, vec in written
                                       if vec is not None])
            return
        except IntegrityError:
            # Another writer (the backfill CLI next to the web worker)
            # inserted one of these keys between our read and our write.
            # Re-read the ids and retry once as updates.
            if attempt == 2:
                raise
            keys = [k for k, *_ in materialised]
            with _db.session_scope() as sess:
                for key, did, h in sess.query(
                        SearchDoc.doc_key, SearchDoc.id,
                        SearchDoc.content_hash).filter(
                            SearchDoc.doc_key.in_(keys)):
                    existing[key] = (did, h or "")


def _delete_ids(ids: list[int]) -> int:
    from engine import db as _db
    from engine.db import SearchDoc
    n = 0
    for start in range(0, len(ids), BATCH):
        chunk = ids[start:start + BATCH]
        with _db.session_scope() as sess:
            n += int(sess.query(SearchDoc).filter(SearchDoc.id.in_(chunk))
                     .delete(synchronize_session=False) or 0)
    return n


def _sync(*, scope: str, project_id: str | None, org_id: str | None,
          drafts: Iterable[Draft], kinds: Iterable[str] | None,
          result: SyncResult, rebuild: bool = False) -> None:
    """The heart of both passes: hash-compare, upsert changed, delete gone."""
    embedder, vec_version = _active_vec_version()
    existing = _existing_map(scope, project_id, kinds)
    seen: set[str] = set()
    pending: list[tuple[str, Draft, str]] = []

    def flush() -> None:
        if pending:
            _write_batch(pending, scope=scope, project_id=project_id,
                         org_id=org_id, existing=existing, embedder=embedder,
                         vec_version=vec_version)
            result.upserted += len(pending)
            pending.clear()

    for d in drafts:
        if scope == "global" and d.kind not in GLOBAL_KINDS:
            raise ValueError(f"kind {d.kind!r} cannot be indexed as global")
        key = doc_key(scope, project_id, d.kind, d.ref, d.chunk_no)
        if key in seen:
            continue
        seen.add(key)
        h = content_hash(d, org_id=org_id, vec_version=vec_version)
        stored = existing.get(key)
        if stored is not None and stored[1] == h and not rebuild:
            result.unchanged += 1
            continue
        pending.append((key, d, h))
        if len(pending) >= BATCH:
            flush()
    flush()
    gone = [did for key, (did, _h) in existing.items() if key not in seen]
    result.deleted += _delete_ids(gone)


def _utc(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes for timezone=True columns; treat a
    naive value as UTC so the two engines compare the same way."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _record_state(scope_key: str, *, started: datetime, changed: bool,
                  doc_filter, error: str = "",
                  vocab: dict | None = None) -> int:
    """Upsert the ``search_scope_state`` row; returns the doc count."""
    from sqlalchemy import func
    from sqlalchemy.exc import IntegrityError

    from engine import db as _db
    from engine.db import SearchDoc, SearchScopeState
    from engine.search import provider_name
    _emb, vec_version = _active_vec_version()
    for attempt in (1, 2):
        try:
            with _db.session_scope() as sess:
                count = 0
                if doc_filter is not None:
                    count = int(sess.query(func.count(SearchDoc.id))
                                .filter(*doc_filter).scalar() or 0)
                st = sess.get(SearchScopeState, scope_key)
                if st is None:
                    st = SearchScopeState(scope_key=scope_key, version=0)
                    sess.add(st)
                if changed or not st.version:
                    st.version = int(st.version or 0) + 1
                if not error:
                    # The time the pass *started* reading, not finished: a
                    # write that landed during the pass must still look newer
                    # than the index to the lag check.
                    st.indexed_at = started
                st.doc_count = count
                st.provider = provider_name()[:20]
                st.vec_version = vec_version
                st.last_error = error[:2000] if error else None
                if vocab is not None:
                    st.vocab_json = vocab
                return count
        except IntegrityError:
            if attempt == 2:
                raise
    return 0   # pragma: no cover


# ── "Did you mean" vocabulary (ADR §4.1 ``vocab_json``, §4.5 typo) ────

#: Most terms kept per scope (ADR §4.1: ≤20k), highest document frequency
#: first.
VOCAB_MAX = 20_000
#: Bump when the vocabulary's shape or tokenisation changes; a stored
#: vocabulary of another version is rebuilt on the next pass.
VOCAB_VERSION = 1


def build_vocab(doc_filter) -> dict:
    """``{"v": VOCAB_VERSION, "terms": {word: df}}`` over the stored
    documents matching *doc_filter* (SQLAlchemy criteria on ``SearchDoc``).

    Words, not stems: the corrector suggests something a person typed, and
    "registr" is not a word anybody wants offered back. Same tokeniser as
    the query side (:func:`engine.search.text.search_tokens`), so a query
    token and a vocabulary entry are comparable. Digits are left out —
    they are never corrected. Streams in pages; holds only the counter.
    """
    from engine import db as _db
    from engine.db import SearchDoc
    from engine.search.text import search_tokens
    df: dict[str, int] = {}
    with _db.session_scope() as sess:
        q = sess.query(SearchDoc.title, SearchDoc.body).filter(*doc_filter)
        for title, body in q.yield_per(BATCH):
            seen = set()
            for tok in search_tokens(f"{title or ''} {body or ''}"):
                if len(tok) < 3 or tok.isdigit() or tok in seen:
                    continue
                seen.add(tok)
                df[tok] = df.get(tok, 0) + 1
    if len(df) > VOCAB_MAX:
        keep = sorted(df, key=lambda w: (-df[w], w))[:VOCAB_MAX]
        df = {w: df[w] for w in keep}
    return {"v": VOCAB_VERSION, "terms": df}


def _vocab_current(scope_key: str) -> bool:
    """The scope's stored vocabulary exists and has this version."""
    from engine import db as _db
    from engine.db import SearchScopeState
    with _db.session_scope() as sess:
        st = sess.get(SearchScopeState, scope_key)
        vj = (st.vocab_json if st is not None else None) or {}
        return (isinstance(vj, dict) and vj.get("v") == VOCAB_VERSION
                and isinstance(vj.get("terms"), dict))


# ── Public API ────────────────────────────────────────────────────────

def _flag_on() -> bool:
    from engine import features
    return features.effective("PLATFORM_SEARCH")


def reindex_project(project_id: str, *, rebuild: bool = False,
                    ignore_flag: bool = False) -> dict:
    """Bring one project's documents in line with its rows (ADR §4.4 (3)).

    Returns the counts as a dict. A project that no longer exists has its
    documents and state removed. With ``PLATFORM_SEARCH`` off this writes
    nothing unless *ignore_flag* (the backfill CLI's ``--force``).
    """
    result = SyncResult(scope_key=project_id or "")
    if not project_id:
        result.skipped = "no project"
        return result.as_dict()
    if not ignore_flag and not _flag_on():
        result.skipped = "PLATFORM_SEARCH is off"
        return result.as_dict()
    from engine import db as _db
    from engine.db import Project, SearchDoc
    with _scope_lock(project_id):
        started = datetime.now(timezone.utc)
        with _db.session_scope() as sess:
            proj = sess.get(Project, project_id)
            org_id = proj.org_id if proj is not None else None
            exists = proj is not None
        if not exists:
            result.deleted = drop_project(project_id)
            result.skipped = "project gone"
            return result.as_dict()
        t0 = time.monotonic()
        try:
            _sync(scope="project", project_id=project_id, org_id=org_id,
                  drafts=iter_project_drafts(project_id), kinds=None,
                  result=result, rebuild=rebuild)
        except Exception as exc:   # noqa: BLE001 — record, then re-raise
            result.error = f"{type(exc).__name__}: {exc}"
            try:
                _record_state(project_id, started=started, changed=True,
                              doc_filter=None, error=result.error)
            except Exception:   # noqa: BLE001
                pass
            raise
        changed = bool(result.upserted or result.deleted)
        doc_filter = (SearchDoc.project_id == project_id,)
        # The "did you mean" vocabulary follows the documents: rebuilt when
        # they changed, or when the stored one is missing/outdated (a scope
        # indexed before the vocabulary existed). An unchanged pass stays
        # the read-only pass it was.
        vocab = None
        if changed or rebuild or not _vocab_current(project_id):
            vocab = build_vocab(doc_filter)
        result.doc_count = _record_state(
            project_id, started=started, changed=changed,
            doc_filter=doc_filter, vocab=vocab)
        log.info("search: indexed project %s in %.0f ms — %d written, "
                 "%d unchanged, %d deleted", project_id[:8],
                 (time.monotonic() - t0) * 1000, result.upserted,
                 result.unchanged, result.deleted)
    return result.as_dict()


def drop_project(project_id: str, sess=None) -> int:
    """Delete a project's documents and its scope state. Returns the number
    of documents removed. With *sess*, runs inside the caller's transaction
    (``retention.delete_project_data``)."""
    from engine import db as _db
    from engine.db import SearchDoc, SearchScopeState

    def _do(s) -> int:
        n = int(s.query(SearchDoc).filter(SearchDoc.project_id == project_id)
                .delete(synchronize_session=False) or 0)
        s.query(SearchScopeState).filter(
            SearchScopeState.scope_key == project_id).delete(
                synchronize_session=False)
        return n

    if not project_id:
        return 0
    if sess is not None:
        return _do(sess)
    with _db.session_scope() as s:
        return _do(s)


def index_global(*, force: bool = False, rebuild: bool = False,
                 ignore_flag: bool = False,
                 corpora: Iterable[str] | None = None) -> dict:
    """Index the shared read-only corpora. A corpus whose fingerprint
    (source bytes + versions) matches the stored one is skipped without
    being parsed; *force* or *rebuild* re-runs it anyway (the hash compare
    still skips unchanged documents unless *rebuild*)."""
    out: dict[str, dict] = {}
    if not ignore_flag and not _flag_on():
        return {"skipped": "PLATFORM_SEARCH is off"}
    from engine import db as _db
    from engine.db import SearchDoc, SearchScopeState
    with _scope_lock("global"):
        any_change = False
        started = datetime.now(timezone.utc)
        for corpus in (corpora or GLOBAL_CORPORA):
            kinds, _files, iterator = GLOBAL_CORPORA[corpus]
            state_key = f"global:{corpus}"
            fp = corpus_fingerprint(corpus)
            with _db.session_scope() as sess:
                st = sess.get(SearchScopeState, state_key)
                stored_fp = ((st.vocab_json or {}).get("source_hash")
                             if st is not None else None)
            res = SyncResult(scope_key=state_key)
            if stored_fp == fp and not (force or rebuild):
                res.skipped = "unchanged"
                out[corpus] = res.as_dict()
                continue
            _sync(scope="global", project_id=None, org_id=None,
                  drafts=iterator(), kinds=kinds, result=res, rebuild=rebuild)
            changed = bool(res.upserted or res.deleted)
            any_change = any_change or changed
            res.doc_count = _record_state(
                state_key, started=started, changed=changed,
                doc_filter=(SearchDoc.scope == "global",
                            SearchDoc.kind.in_(list(kinds))),
                vocab={"source_hash": fp})
            out[corpus] = res.as_dict()
        # The per-corpus rows keep their source fingerprint in vocab_json;
        # the corrector's vocabulary for all global kinds lives on "global".
        global_filter = (SearchDoc.scope == "global",)
        vocab = None
        if any_change or not _vocab_current("global"):
            vocab = build_vocab(global_filter)
        _record_state("global", started=started, changed=any_change,
                      doc_filter=global_filter, vocab=vocab)
    return out


def is_stale(project_id: str) -> bool:
    """The lag check (ADR §4.4 (2)): True when the project's index is
    missing, older than ``project.updated_at``, or built with another
    vector version. False for a project that does not exist."""
    from engine import db as _db
    from engine.db import Project, SearchScopeState
    _emb, vec_version = _active_vec_version()
    with _db.session_scope() as sess:
        proj = sess.get(Project, project_id)
        if proj is None:
            return False
        st = sess.get(SearchScopeState, project_id)
        if st is None or st.indexed_at is None:
            return True
        if (st.vec_version or None) != (vec_version or None):
            return True
        updated = _utc(proj.updated_at)
        return bool(updated and updated > _utc(st.indexed_at))


def fill_pgvector(*, project_id: str | None = None) -> int:
    """Copy ``vec_blob`` into ``search_vector`` for every document missing
    there — the ``--to-pgvector`` step, no re-embedding. Re-detects the
    capability first (the admin may have enabled the extension after
    boot). Returns rows copied; 0 without pgvector."""
    from sqlalchemy import text

    from engine import db as _db
    from engine.search import caps, detect_caps
    from engine.search.embed import from_blob
    detect_caps(_db.get_engine())
    if not caps.get("pgvector"):
        return 0
    _emb, vec_version = _active_vec_version()
    copied = 0
    last = 0
    while True:
        with _db.session_scope() as sess:
            params = {"last": last, "vv": vec_version, "lim": BATCH}
            where = ("d.id > :last AND d.vec_blob IS NOT NULL "
                     "AND d.vec_version = :vv AND v.doc_id IS NULL")
            if project_id:
                where += " AND d.project_id = :pid"
                params["pid"] = project_id
            rows = sess.execute(text(
                "SELECT d.id, d.project_id, d.vec_blob FROM search_doc d "
                "LEFT JOIN search_vector v ON v.doc_id = d.id "
                f"WHERE {where} ORDER BY d.id LIMIT :lim"), params).all()
            if not rows:
                return copied
            _mirror_vectors(sess, [(r[0], r[1], from_blob(bytes(r[2])))
                                   for r in rows])
            copied += len(rows)
            last = rows[-1][0]


__all__ = [
    "Draft", "SyncResult", "BATCH", "BODY_MAX", "PROJECT_KINDS",
    "GLOBAL_KINDS", "GLOBAL_CORPORA", "INDEXER_VERSION", "chunk_body",
    "content_hash", "doc_key", "drop_project", "fill_pgvector",
    "index_global", "iter_project_drafts", "is_stale", "reindex_project",
    "corpus_fingerprint", "build_vocab", "VOCAB_MAX", "VOCAB_VERSION",
]
