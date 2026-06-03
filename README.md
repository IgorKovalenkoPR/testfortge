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

## Recording test steps (pilot — PR-B)

A manual QA can record a Playwright session and attach the captured
steps to a Test Case so the next run plays them back deterministically,
instead of relying on the heuristic parse of the case's text steps.

### Opt in

Recorder is gated on `RECORDER_ENABLED=1` everywhere — the CLI, the
MCP write tool, and the per-TC "🎬 Record steps" block on
`/test-cases`. Hosts without the env var see no Recorder surface at
all. Flip it on per-host:

```bash
export RECORDER_ENABLED=1
```

### One-time setup

```bash
pip install playwright
python -m playwright install chromium
```

### Recording a TC

From a TestForTge checkout, run:

```bash
RECORDER_ENABLED=1 python -m tools.tfg_record \
    --project <project_id> --tc <TC_ID> --url <start_url>
```

Codegen opens a Chromium window. Click through the scenario, close the
window — the CLI parses the captured Python, writes the steps to
`TestCase.automation_steps_json`, and the next `/test-execution` run
uses them verbatim (skipping the heuristic text parse).

The `/test-cases` page surfaces a per-TC "🎬 Record steps" panel with
the same command pre-filled. Tester pastes a Start URL, clicks
**📋 Copy command**, runs it locally.

### Re-importing a capture

If codegen output already exists (saved from another run, or a
teammate's checkout), skip the browser launch and import directly:

```bash
RECORDER_ENABLED=1 python -m tools.tfg_record \
    --project <project_id> --tc <TC_ID> --from-file path/to/captured.py
```

### Notes

- The CLI talks to the **local DB** directly. Use it from the same
  checkout that's serving TestForTge (or pointing at the same
  `DATABASE_URL`). For cross-machine flows the MCP server exposes
  `record_steps_attach` — a future CLI release will wrap it behind an
  `--mcp-url` flag.
- PR-A populates each step's `target_alternates` so the runner walks a
  ranked candidate list when the primary locator drifts. The Page
  Object DB (`Locator` table) is populated from the same recording so
  the next run promotes the last-success strategy automatically. No
  re-record needed for drift recovery.
- `RECORDER_TIMEOUT_S` (default 1800 s) caps a single recording
  session — bump for slow accessibility regressions, but the floor of
  60 s prevents a typo from disabling the safety net entirely.

### Capturing assertions (PR-C — Assertion Mode)

Playwright codegen's toolbar has three **Assert** buttons (visibility,
text, URL). When you click them mid-recording, codegen emits
`expect(...)` lines that `engine.recorder_parser` recognises and
materialises as `AutomationStep` rows with `kind="assertion"`. The
runner then branches on `step.kind`:

| Assertion         | Codegen output                                       | Runner behaviour                                                          |
| ----------------- | ---------------------------------------------------- | ------------------------------------------------------------------------- |
| Assert visible    | `expect(loc).to_be_visible()`                        | Walks PR-A locator chain, then `wait_for(state="visible", timeout=5s)`.   |
| Assert text       | `expect(loc).to_contain_text("X")` / `to_have_text`  | `page.get_by_text(X)` + content-scan fallback for attribute-hosted text.  |
| Assert URL        | `expect(page).to_have_url("https://app/...*")`       | `fnmatch` glob (or substring when no glob chars) on `page.url`.           |

The `/test-cases` editor surfaces a per-step **Action / Assert visible
/ Assert text / Assert URL** dropdown next to every recorded step, so
you can flip a captured click into an assertion without re-recording.
Edits POST to `/test-cases/<id>/automation-step-kind` and persist to
`automation_steps_json` immediately. Pre-PR-C recordings deserialise as
plain action steps — backward-compatible.

### Web Recorder browser extension (PR-E)

The full no-CLI flow: click **🎬 Start session recording** on
`/test-cases`, walk through the SUT in the new tab, click **Stop** on
the floating overlay, confirm the auto-segmented TCs in the review
screen. See [extension/README.md](extension/README.md) for the install
walkthrough (Developer mode → Load unpacked).

Backend endpoints the extension uses:

| Endpoint | Purpose |
|---|---|
| `POST /api/recorder-session/start` | Mint a one-shot token bound to the active project |
| `POST /api/recorder-session/finish` | Receive captured steps, run PR-D segmenter + classifier, return `review_url` |

CORS is `*` because the extension's content-script runs in the SUT's
origin, which we can't pre-list. Token auth replaces origin-checking.
Both endpoints gated on `RECORDER_ENABLED`.
