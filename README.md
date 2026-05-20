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

## Metrics history

The trend chart on `/test-metrics` reads `DashboardMetricSnapshot` rows
through the `/metrics/history` JSON endpoint. Snapshots are written
from three places:

1. **Dashboard load** — at most once per project per hour, opportunistic.
2. **Run completion** — the detached `runner_worker` subprocess writes
   a snapshot right after `done.flag` so the chart picks up the new
   pass-rate / defect-density without waiting for the next visit.
3. **Daily catch-up thread** — started by `app.py` after `init_db()`.
   Iterates projects every 24 h and snapshots anything that hasn't
   had one in the last 23 h. Default ON; disable with
   `TESTFORTGE_SNAPSHOT_WORKER=0` (recommended when you scale
   gunicorn beyond `--workers 1` — move the pass to an external cron
   to avoid N parallel writes).

For a fresh historical fill on an existing deployment:

```bash
FLASK_DEBUG=1 python tools/backfill_metric_snapshots.py
```

prints one line per project. Idempotent and safe to re-run.

## Production hardening

TestForTge ships two operations endpoints — `/healthz` (liveness probe)
and `/metrics` (job-queue + DB counts). `/healthz` is **always open** so
container orchestrators (k8s, Docker healthcheck, uptime pingers) can
reach it without secrets. `/metrics` is open by default too, but should
be locked down on any deployment reachable from the public internet.

There are three ways to lock it down — pick at least one:

### Option 1 — Ops token (lightest)

```bash
export OPS_ENDPOINTS_TOKEN="$(openssl rand -hex 32)"
```

`/metrics` then requires the matching header:

```bash
curl -H "X-Ops-Token: $OPS_ENDPOINTS_TOKEN" https://your.host/metrics
```

`/healthz` is intentionally not gated by this token — probes stay
secret-free. Constant-time comparison (`hmac.compare_digest`) is used.

### Option 2 — HTTP Basic Auth (covers the whole app)

```bash
export TESTFORTGE_BASIC_USER=ops
export TESTFORTGE_BASIC_PASSWORD="$(openssl rand -hex 24)"
```

This puts every route — `/`, `/test-cases`, `/metrics`, etc. — behind a
Basic Auth gate before any other middleware runs. Use this when the
deployment is on the public internet and you want a single password
in front of everything.

### Option 3 — Reverse-proxy IP allowlist

If `/metrics` is scraped by an internal Prometheus / Datadog agent on a
known IP range, the cleanest answer is to allowlist it at the proxy
(nginx `allow` / k8s `NetworkPolicy`) and reject everyone else.
TestForTge itself doesn't do source-IP filtering — that's the proxy's
job.

### Boot warning

When `BEHIND_HTTPS=1` is set but **neither** `TESTFORTGE_BASIC_USER`
**nor** `OPS_ENDPOINTS_TOKEN` is configured, the app logs a single
`SECURITY:` warning at boot. That's the signal that `/metrics` is
publicly reachable — fix one of the three options above or take the
proxy approach.
