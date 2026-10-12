# Release testing, October 2026 — seed, smoke, load, cleanup

Target: **https://testforge.qarea.org** (QAREA launchpad). This runbook is the
order of operations for the release that turns on platform search, the AI
answer (Ask), the model switcher and AI-assisted automation. Every command is
written out; nothing here runs without the owner's go-ahead.

Kit: `tools/loadtest/` — `seed_loadtest.py`, `cleanup_loadtest.py`,
`smoke_live.py`, `locustfile.py`; tests in `tests/test_loadtest_tools.py`.

---

## 0. What the owner must do first

### 0.1 GitLab CI/CD variables (launchpad strips the `APP_` prefix)

Values are exactly `1` or `assist` — never a word copied from a column header
(`Visible` once silently turned `AUTH_ENABLED` off; see the deploy notes).

| Variable | Value | Why |
|---|---|---|
| `APP_WORKSPACE_DB_FIRST` | `1` | prerequisite of search, editors, dashboard v2 |
| `APP_PLATFORM_SEARCH` | `1` | `/api/search`, `/api/hints` (404 while off) |
| `APP_PLATFORM_SEARCH_AI` | `1` | `/api/ask` (404 while off; needs `PLATFORM_SEARCH`) |
| `APP_MODEL_SWITCHER` | `1` | model switcher (needs `ORG_MODE`) |
| `APP_AUTOMATION_AI_MODE` | `assist` | rules first, model fills gaps |
| `APP_EDITORS_ENABLED` | `1` | **the load test's write tasks need it**; check it is still set |
| `APP_AUTH_ENABLED`, `APP_ORG_MODE` | `1` | already on — confirm, do not change |
| `APP_CLAUDE_KEY` | the platform key | the smoke's one real Ask proves it |

Optional: `APP_RAG_ASK_DAILY_CAP_PER_ORG` (default 300 asks/org/day; the load
test's own budget is 60), `APP_RAG_ASK_USER_PER_MIN` (default 6).

A push to `main` redeploys. Afterwards: `GET /auth/me` → `auth_active: true`,
and the deploy job log shows `Prepared N runtime env var(s)`.

### 0.2 Database administrator

```sql
CREATE EXTENSION IF NOT EXISTS vector;   -- needs a superuser / the DB owner
```

Without it search still works (vectors are compared in Python) and `/readyz`
reports `"pgvector": false` — the smoke marks that **WARN**, a finding, not a
failure. After the extension exists, copy the stored vectors:
`python -m tools.search_backfill --to-pgvector` (in the container).

### 0.3 Running a command inside the container — **unknown, owner to supply**

The launchpad runs the image on the backend VM through a shell-executor
runner; there is no registry and nothing in this repository records the
container's name or how a person reaches that VM. Whoever has VM access:

```sh
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'      # find it
docker exec -it <container> sh -lc 'cat /proc/1/cmdline | tr "\0" " "; echo'
```

Every "in the container" command below is shown as
`docker exec -e … <container> python -m …`. If the VM is not reachable, the
seed and the cleanup can run from any machine that can reach the production
Postgres with the same code checked out: `DATABASE_URL=postgresql://… python -m
tools.loadtest.seed_loadtest …` — but `DATABASE_URL` is injected by the
backend and is not in CI, so the owner has to provide it.

### 0.4 Check the process model before the load run

The brief assumes **one gunicorn worker × 4 threads** on **port 8080**. The
`Dockerfile` on this branch (87039b8) still says `--workers 2`, port 5000; the
launchpad repo's Dockerfile was fixed separately (`WEB_CONCURRENCY` default 1,
port from `$PORT` = 8080). Confirm with the `/proc/1/cmdline` line above (or
`ps -ef` in the container). Two workers break async jobs (the JobQueue is
per process — a poll lands in the other worker and reports the job lost), so
**do not load-test a two-worker container**; the generation task would fail
for that reason alone.

---

## 1. What 512 MB and one worker mean for this test

* **4 requests at a time, total.** One process, 4 threads. Request 5 waits in
  the gunicorn backlog: under load you get latency, not errors, until the
  gunicorn timeout kills a worker (then 502s and a restart).
* **A streamed Ask holds a thread for the whole answer** (seconds). Two
  concurrent Asks are half the server. That is why Ask is 5 % of the mix,
  capped at 4/user/min and 60/run.
* **The JobQueue has 2 threads** (`JOB_QUEUE_WORKERS`), shared by TC
  generation **and search reindexing**. Every edit queues a debounced (3 s)
  reindex of the whole project — for `LOADTEST big` that is a pass over
  ~3,300 documents (unchanged ones skipped by hash). Writes therefore load the
  job threads well beyond the PATCH itself; watch `[gen] completion`.
* **Sign-in costs ~64 MiB transiently** (Argon2 default memory cost). Logins
  happen once per simulated user, staggered by the spawn rate (2/s).
* **Memory headroom is small.** The executor's own budget is 382 MB
  (512 − 130). A browser execution run with Chromium reaches ~443 MB by
  itself. `/healthz` exposes **no RSS** — the load test logs "memory stop
  condition is inactive", so memory must be watched on the VM:
  `docker stats <container>`.
* **A restart is invisible to HTTP status alone.** The load test watches
  `/healthz` `uptime_seconds`; if it goes backwards the process restarted
  (most likely an OOM kill) and the run stops.

---

## 2. Order of operations

Set once on the operator's machine (never commit these):

```sh
export BASE_URL=https://testforge.qarea.org
export LOADTEST_PASSWORD='<12+ characters, shared by the seeded accounts>'
python -m venv .venv-loadtest && . .venv-loadtest/bin/activate
pip install -r requirements-loadtest.txt
```

### Step 1 — seed (in the container)

```sh
docker exec -e LOADTEST_PASSWORD="$LOADTEST_PASSWORD" <container> \
  python -m tools.loadtest.seed_loadtest --users 20 --tcs 2000 \
  --checklist 1000 --bugs 300 --csv /app/data/loadtest_accounts.csv
docker cp <container>:/app/data/loadtest_accounts.csv ./loadtest_accounts.csv
```

Creates org `LOADTEST` (lt-admin + lt-user-01..20, all
`@loadtest.testforge.invalid` — an undeliverable domain), org `LOADTEST-B`
(lt-b-user, tenancy check only), project `LOADTEST big`
(2,000 TCs / 1,000 checklist items / 300 bugs in EN/UA/RU), project
`LOADTEST gen scratch` (generation writes here — a generated pack *replaces*
a project's test cases) and `LOADTEST-B tenancy`. Idempotent: re-running
converges. Exit 2 = a guard refused (message says which).

### Step 2 — backfill the search index (in the container)

```sh
docker exec <container> python -m tools.search_backfill --global --all
# after CREATE EXTENSION vector (0.2):
docker exec <container> python -m tools.search_backfill --to-pgvector
```

Refuses (exit 2) while `PLATFORM_SEARCH` is effectively off — that is the
flags check from 0.1.

### Step 3 — smoke (operator machine)

```sh
python -m tools.loadtest.smoke_live --json smoke_release.json
```

17 checks, PASS/FAIL/WARN/SKIP table, exit 1 on any FAIL. It makes **one**
real Ask (asserts `model_tier: haiku`), repeats it (must be `cached: true`,
no model call) and asks one nonsense question (must be `not_grounded`, no
model call). `SMOKE_ASK=0` skips the real Ask. Stop here on any FAIL other
than a WARN for `pgvector: false`.

Note on check 17: the **team** model policy (`/org/settings`) is admin-only;
the **personal** sidebar switcher is shown to every team member by design
(ADR 0003 §4.7). The smoke asserts the former and reports the latter.

### Step 4 — live E2E (manual, by a person)

Walk the release's user-visible features in a browser as `lt-admin` and as
`lt-user-01`: header search + results page, hints, Ask panel, model switcher,
AI-mode select, an inline edit, a TC generation with "Improve with AI". Not
automated by this kit.

### Step 5 — load, idle server (operator machine)

```sh
LT_ACCOUNTS_CSV=loadtest_accounts.csv LT_CONFIRM_HOST="$BASE_URL" \
LT_REPORT_DIR=./loadtest-reports \
locust -f tools/loadtest/locustfile.py --headless --host "$BASE_URL" \
  --only-summary 2>&1 | tee loadtest-idle.log
```

18 minutes: 1→5 users (2 min), 10 (5 min), 20 (10 min), down (1 min). The run
refuses any non-local host unless `LT_CONFIRM_HOST` equals `--host`. Report:
`loadtest-reports/loadtest_report_<ts>.{json,md}` with the SLO verdicts:

| Category | p95 SLO |
|---|---|
| pages (`[page] …`) | ≤ 800 ms |
| typeahead (`quick=1`) | ≤ 400 ms |
| full search | ≤ 1.2 s |
| writes (PATCH) | ≤ 1 s |
| Ask first chunk | ≤ 2.5 s |
| 5xx | < 0.5 % of requests |

`overall` is PASS, FAIL, or INCOMPLETE (a category had no samples — e.g. Ask
disabled by `no_key`). Exit code 2 = a stop condition fired, 1 = SLO failed.

Knobs: `ASK_BUDGET` (60), `LT_ASK=0`, `LT_GEN=0`, `LT_WRITES=0`,
`LT_MAX_USERS`, `LT_TIME_SCALE` (0.25 = a 4.5-minute rehearsal).

### Step 6 — load with one execution run

Repeat step 5 and, at about minute 8 (the start of the 20-user hold), start
**one** browser Test Execution run as `lt-admin` in `LOADTEST gen scratch`
against a small public target (a few test cases). Watch `docker stats`. This
is the memory question of the release: Chromium plus 20 users in 512 MB. An
`oom_budget_exceeded_*` early exit of the run is the guard working; a process
restart (uptime reset, the load test stops itself) is the failure.

### Step 7 — cleanup (in the container)

```sh
docker exec <container> python -m tools.loadtest.cleanup_loadtest          # dry run
docker exec <container> python -m tools.loadtest.cleanup_loadtest --yes
```

Deletes only organisations named exactly `LOADTEST` / `LOADTEST-B`, by id:
projects through `retention.delete_project_data` + `db.delete_project`
(content, blobs, search documents, AI caches), then the orgs' leftover search
docs, cached answers, usage and audit rows (`--keep-audit` keeps the audit),
memberships, invites, the seeded users and the org rows. A seeded-looking user
who also belongs to a real team is only detached. It prints every other
organisation's row counts before and after and exits 1 on any difference —
run it in a quiet window, since a real user's edit during the cleanup is also
a "difference" (re-run the dry run to see what moved). Also remove
`/app/data/loadtest_accounts.csv`.

---

## 3. Abort rules

Stop the run (Ctrl-C; the load test also stops itself) and do not continue to
the next step when:

| Signal | Automatic? | Threshold |
|---|---|---|
| `/healthz` fails | yes | 3 consecutive polls (every 10 s) |
| process restarted | yes | `/healthz` uptime goes backwards |
| 5xx rate | yes | > 2 % over the last 60 s |
| latency | yes | p95 > 5 s sustained for 2 min (Ask and generation excluded) |
| memory | **no** — `/healthz` has no RSS | `docker stats` > 450 MB → stop by hand |
| real users affected | no | anyone reports slowness → stop |
| Ask spend | budget | the run never exceeds `ASK_BUDGET` (60) calls |

After an abort: note the time, capture `docker stats` / container logs, run
the smoke again, then the cleanup.

---

## 4. Dry run (done locally, 2026-10-12)

Local Flask on SQLite (`TESTFORTGE_ALLOW_SQLITE_PROD=1`, all flags on, no
Anthropic key): seed (5 users, 200/100/30), backfill, smoke (13 PASS, 1 WARN
`pgvector false`, 2 SKIP, 1 FAIL — the real Ask, `no_key`, as expected
without a key), locust 3 users × 60 s through the scaled shape (0 failures,
Ask disabled itself on `no_key`, overall INCOMPLETE for want of an Ask
sample), the p95 stop condition forced with `LT_P95_LIMIT_MS=1`, the host
guard against an unconfirmed host (0 requests sent), and cleanup. **Not**
exercised: Postgres, pgvector, a real key, gunicorn, 512 MB, 20 users.
