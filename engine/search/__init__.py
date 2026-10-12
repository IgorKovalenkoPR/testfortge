"""TestForTge — platform search (ADR 0003, epic E13).

This package is the retrieval layer over the platform's own data. T3 lays
the foundation only: text processing (:mod:`engine.search.text`), the
deterministic embedder (:mod:`engine.search.embed`), the lexicons, and the
capability record below. T4 adds the indexer
(:mod:`engine.search.indexer`) and the ORM hooks that keep it current
(:mod:`engine.search.hooks`); retrieval and endpoints come later.

Kept import-light on purpose. ``import engine.search`` must not pull in
NumPy, onnxruntime or any database driver (ADR 0003 §4.11, guarded by
``tests/test_search_memory.py``), and :mod:`engine.db` imports this module
from ``init_db`` — so nothing here imports :mod:`engine.db` at module level.

Capabilities
------------
``caps`` is a plain dict, updated **in place** so that a module holding
``from engine.search import caps`` sees later updates::

    {"fts": bool,                 # Postgres tsvector column + GIN exist
     "pgvector": bool,            # extension present AND search_vector made
     "pgvector_version": str|None,
     "provider": "hash"|"voyage"|...,
     "backend": "sqlite"|"postgresql"|None,
     "checked": bool}             # False until init_db (or detect) ran

``_ensure_search_schema`` in :mod:`engine.db` publishes it at boot.
:func:`detect_caps` re-reads it from a live engine and is what a later
``/api/search/status`` uses to notice an extension the admin enabled after
boot. Neither ever raises: SQLite, a Postgres without the extension, and a
database that cannot be reached all come back as "lexical only".
"""
from __future__ import annotations

import os
from typing import Any

from engine.log import get_logger

log = get_logger(__name__)

#: The provider used when ``EMBEDDINGS_PROVIDER`` is unset or blank.
DEFAULT_PROVIDER = "hash"


def provider_name() -> str:
    """The configured embeddings provider, read at call time (tests and the
    Render dashboard change the environment after import)."""
    return ((os.environ.get("EMBEDDINGS_PROVIDER") or "").strip().lower()
            or DEFAULT_PROVIDER)


def _blank_caps() -> dict[str, Any]:
    return {"fts": False, "pgvector": False, "pgvector_version": None,
            "provider": provider_name(), "backend": None, "checked": False}


#: The published capability record. Mutated in place, never rebound.
caps: dict[str, Any] = _blank_caps()


def publish_caps(**values: Any) -> dict[str, Any]:
    """Overwrite ``caps`` in place with *values* (unknown keys are kept,
    so a later task can add a field without touching this function)."""
    fresh = _blank_caps()
    fresh.update(values)
    fresh["checked"] = True
    caps.clear()
    caps.update(fresh)
    return caps


def reset_caps() -> None:
    """Back to "never checked". For tests that swap the database."""
    caps.clear()
    caps.update(_blank_caps())


def detect_caps(engine: Any, *, publish: bool = True) -> dict[str, Any]:
    """Read the capabilities off a live engine. Never raises.

    * SQLite (or anything that is not Postgres): no FTS, no pgvector.
    * Postgres: ``fts`` when ``search_doc.tsv`` exists; ``pgvector`` when
      the extension is installed **and** ``search_vector`` exists — an
      extension nobody built the table for is not a usable vector index.

    Any error (no connection, missing catalog privileges, a driver that
    does not speak the query) degrades to the lexical-only record and is
    logged at debug level.
    """
    found: dict[str, Any] = {"fts": False, "pgvector": False,
                             "pgvector_version": None, "backend": None}
    try:
        backend = engine.dialect.name
        found["backend"] = backend
        if backend == "postgresql":
            from sqlalchemy import text
            with engine.connect() as conn:
                found["fts"] = bool(conn.execute(text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'search_doc' AND column_name = 'tsv' "
                    "AND table_schema = current_schema()")).scalar())
                version = conn.execute(text(
                    "SELECT extversion FROM pg_extension "
                    "WHERE extname = 'vector'")).scalar()
                table = conn.execute(text(
                    "SELECT to_regclass('search_vector') IS NOT NULL")).scalar()
                found["pgvector_version"] = version
                found["pgvector"] = bool(version) and bool(table)
    except Exception as exc:   # noqa: BLE001 — capability probe, never fatal
        log.debug("search capability probe failed: %s", exc)
        found.update(fts=False, pgvector=False, pgvector_version=None)
    if publish:
        return publish_caps(**found)
    out = _blank_caps()
    out.update(found)
    out["checked"] = True
    return out


__all__ = ["caps", "detect_caps", "publish_caps", "reset_caps",
           "provider_name", "DEFAULT_PROVIDER"]
