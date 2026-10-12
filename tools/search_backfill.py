"""TestForTge — build or refresh the platform search index (ADR 0003 §4.4).

Usage::

    python -m tools.search_backfill --global            # Guide + ISTQB syllabus
    python -m tools.search_backfill --all               # every project
    python -m tools.search_backfill --project ID [--project ID2 ...]
    python -m tools.search_backfill --all --rebuild     # rewrite every document
    python -m tools.search_backfill --to-pgvector       # vec_blob → search_vector

Each project pass is the same full-scope, content-hashed pass the indexing
hooks queue: unchanged documents are skipped, so re-running is cheap and
safe. ``--rebuild`` ignores the hashes and rewrites everything (use after a
lexicon change that did not bump ``LEXICON_VERSION``). ``--to-pgvector``
copies the stored vectors into ``search_vector`` without recomputing them —
for when an admin ran ``CREATE EXTENSION vector`` after the data was
indexed.

Refuses to write while ``PLATFORM_SEARCH`` is effectively off (it requires
``WORKSPACE_DB_FIRST``) unless ``--force`` is given, so a stray run cannot
fill a database nobody turned the feature on for.

Projects are processed one at a time; memory is bounded by the indexer's
batches of 500, not by the size of the database. Exit code 0 on success,
1 if any scope failed, 2 on bad arguments or a refused run.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the repo importable when run as ``python tools/search_backfill.py``.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m tools.search_backfill",
        description="Build or refresh the platform search index.")
    ap.add_argument("--project", action="append", default=[], metavar="ID",
                    help="index this project (repeatable)")
    ap.add_argument("--all", action="store_true",
                    help="index every project in the database")
    ap.add_argument("--global", dest="global_", action="store_true",
                    help="index the shared corpora (Guide, ISTQB syllabus)")
    ap.add_argument("--rebuild", action="store_true",
                    help="rewrite every document, ignoring content hashes")
    ap.add_argument("--to-pgvector", action="store_true",
                    help="copy stored vectors into search_vector (pgvector)")
    ap.add_argument("--force", action="store_true",
                    help="run even though PLATFORM_SEARCH is off")
    ap.add_argument("--json", action="store_true",
                    help="print one JSON line per scope instead of text")
    args = ap.parse_args(argv)
    if not (args.project or args.all or args.global_ or args.to_pgvector):
        ap.error("nothing to do: pass --project, --all, --global or "
                 "--to-pgvector")
    return args


def _all_project_ids() -> list[str]:
    from engine import db as _db
    from engine.db import Project
    with _db.session_scope() as sess:
        return [pid for (pid,) in sess.query(Project.id).order_by(Project.id)]


def main(argv: list[str] | None = None, *, out=None) -> int:
    out = out or sys.stdout
    try:
        args = _parse(argv)
    except SystemExit as exc:
        return int(exc.code or 0) and 2

    from engine import db as _db
    from engine import features
    from engine.search import indexer

    _db.init_db()
    if not args.force and not features.effective("PLATFORM_SEARCH"):
        print("PLATFORM_SEARCH is off (it also requires WORKSPACE_DB_FIRST); "
              "refusing to write. Pass --force to index anyway.", file=out)
        return 2

    def emit(label: str, payload: dict) -> None:
        if args.json:
            print(json.dumps({"scope": label, **payload}, default=str),
                  file=out)
        else:
            print(f"{label}: {payload}", file=out)

    failed = 0
    if args.global_:
        try:
            res = indexer.index_global(force=True, rebuild=args.rebuild,
                                       ignore_flag=True)
            for corpus, r in res.items():
                emit(f"global:{corpus}", r if isinstance(r, dict)
                     else {"result": r})
        except Exception as exc:   # noqa: BLE001 — report and carry on
            failed += 1
            emit("global", {"error": f"{type(exc).__name__}: {exc}"})

    pids: list[str] = list(dict.fromkeys(args.project))
    if args.all:
        pids.extend(p for p in _all_project_ids() if p not in pids)
    for pid in pids:
        try:
            emit(pid, indexer.reindex_project(pid, rebuild=args.rebuild,
                                              ignore_flag=True))
        except Exception as exc:   # noqa: BLE001
            failed += 1
            emit(pid, {"error": f"{type(exc).__name__}: {exc}"})

    if args.to_pgvector:
        try:
            emit("pgvector", {"copied": indexer.fill_pgvector()})
        except Exception as exc:   # noqa: BLE001
            failed += 1
            emit("pgvector", {"error": f"{type(exc).__name__}: {exc}"})

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
