# TestForTge

Web framework that automates QA documentation in the TestFort format —
test cases, checklists, bug reports, estimations, and execution runs.

See [`QA_FORGE_DESCRIPTION.md`](QA_FORGE_DESCRIPTION.md) for the full
functional description and [`REQUIREMENTS.md`](REQUIREMENTS.md) for
configuration / env reference.

## Quick start

```bash
pip install -r requirements.txt
FLASK_DEBUG=1 python app.py
```

## Deployment

Postgres is required in production. SQLite + WAL is fine for local dev
and unit tests, but concurrent writes from gunicorn workers plus the
detached `runner_worker` will deadlock under load on a single-file DB.

- Production: set `DATABASE_URL=postgresql://user:pass@host:5432/dbname`.
- Local dev / tests: set `FLASK_DEBUG=1` (SQLite is the default fallback).
- Solo VM self-host where you accept the risk:
  `TESTFORTGE_ALLOW_SQLITE_PROD=1` downgrades the safety raise to a warning.

The SQLite backend runs in WAL mode (`journal_mode=WAL`,
`synchronous=NORMAL`, `busy_timeout=5000`) so dev concurrency is
realistic. WAL is incompatible with NFS/SMB — keep the DB on a local
filesystem.
