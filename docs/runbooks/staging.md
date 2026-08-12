# Staging

**What it is for:** seeing a release before production does, and running the
three verification zones that need a live instance which is not the one
people use — load, accessibility, and real mail delivery. E10's entry
criteria name a deployed staging; this is it.

**What it costs:** $0. Free plan, no extra database, no keep-alive.

**Status:** live since 2026-08-11 at `testfortge-staging.onrender.com`. On
its own Postgres since 2026-08-12 — before that its SQLite file was wiped on
every redeploy, which cost somebody the projects they had just created (§3). The first-admin path was walked by hand on the day it was created: the two `BOOTSTRAP_ADMIN_*` variables were filled, and the account signs in with the `admin` role. Externally verified from outside the dashboard: `/` redirects to the login page (so authentication is on and the Basic gate is off), the session cookie carries `Secure`, HSTS and CSP are present, and the Google button is absent as declared.

---

## 1. It runs the mode the product ships in — production does not

| | production | staging |
|---|---|---|
| `AUTH_ENABLED` | `0` | **`1`** |
| `ORG_MODE` | `0` | **`1`** |
| `BASIC_GATE_ENABLED` | `1` (shared password in front) | **`0`** |
| `EDITORS_ENABLED` | `0` | **`1`** |
| `DASHBOARD_V2` | `0` | **`1`** |
| `WORKSPACE_DB_FIRST` | `0` | **`1`** |
| database | Postgres (Render free, `fromDatabase`) | **its own Postgres**, dashboard-managed |
| storage | local disk | local disk |
| keep-alive | working hours | none — it sleeps |

That first column is why staging matters more here than in most projects:
the mode the product ships in has **never had real traffic**, because prod
still runs with authentication off behind a Basic password. Staging is where
it gets that traffic.

The Basic gate is off here **because** authentication is on. The interlock in
`engine/basic_auth.py` refuses the other combination — gate off with auth off
would be a fully public instance, and it keeps the gate up rather than obey.

## 1a. Before the first Manual Sync — the one check worth making

A sync reconciles **every** service against `render.yaml`, and **deletes
environment variables the blueprint does not declare**. That is the failure
mode E0.6 exists for: on 2026-07-30 `RECORDER_ENABLED=1` was live on the web
service and absent from the file, and a sync would have switched the Web
Recorder off in production with no error, no log and no failing test.

**And this blueprint auto-syncs.** Measured 2026-08-12: the sync history
shows the reconciliation attached to commit `1e28aac`, and a Manual Sync run
afterwards answered "Resources already up to date". So the deletion hazard
does not wait for anybody to click Sync — **a push to `main` is a sync**, and
the check below is something to do *before pushing* rather than before
clicking. Verified the same day across all three services: no live variable
is missing from the blueprint.

So before clicking Sync, open **`testfortge` → Environment** and compare the
key names there with the blueprint's. Anything in the dashboard that is not
in `render.yaml` will be gone after the sync.

What the **next** sync will do, if the blueprint and the dashboard agree:

* **add** `DATABASE_URL` to `testfortge-staging` — dashboard-managed, so it
  arrives empty and the service keeps booting on SQLite until you paste a
  connection string into it (§2 step 2);
* **change nothing else** — no existing value is rewritten, because
  `sync: false` keys are left to the dashboard and every other key already
  carries the value it has now.

What the first sync did, for the record: created `testfortge-staging` on the
free plan and added `BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD` and
`BOOTSTRAP_ORG_NAME` to `testfortge`. It deleted nothing — checked against
the dashboard first, which is the point of this section.

## 2. Creating it

The service is declared in `render.yaml`, so:

1. **Render → Blueprints → your blueprint → Manual Sync.** Render reads the
   file and creates `testfortge-staging`.
2. **Give it a database of its own.** Create a free Postgres somewhere
   that is not Render's free tier — Render allows one free database per
   account and production holds it — and paste the connection string into
   `DATABASE_URL` on the staging service. Keep `sslmode=require` in the
   string if the provider needs it; without the parameter psycopg2 is
   refused after the deploy rather than during it.

   The blueprint declares the key as `sync: false`, so it exists with no
   value until you fill it, and **an empty value falls back to SQLite
   silently**. The check is on the product, not in the dashboard:
   **Settings → Capacity** names the engine in force. If it says `sqlite`,
   the string did not take.

   No migration to run — `init_db()` creates the schema on first start.
   For provider-specific detail — which connection string to copy, why
   `sslmode=require` matters, how to move existing rows with `pg_dump` —
   see [database-on-a-free-plan.md](database-on-a-free-plan.md) §1. That
   runbook was written for a different question (moving *production* off
   Render's expiring free database, a decision the owner closed on
   2026-08-06) and its mechanics are the same ones staging needs.

3. Fill two more variables on the new service (**Environment**):

| Key | Value |
|---|---|
| `BOOTSTRAP_ADMIN_EMAIL` | your address |
| `BOOTSTRAP_ADMIN_PASSWORD` | a phrase of a few words, ≥12 characters |

   These are not optional even now that the database persists: a brand-new
   database is empty, and an empty database cannot issue itself an
   invitation ([first-admin.md](first-admin.md)). Without them the service
   starts and nobody can sign in. They stay set afterwards, harmlessly —
   `claim_first_admin()` no-ops once the database has any user at all.
4. Optionally `RESEND_API_KEY` + `MAIL_FROM` if you want to test that an
   invitation actually arrives, and `ANTHROPIC_API_KEY` if you want AI
   generation on staging (BYOK per team also works).

It lands on `testfortge-staging.onrender.com`.

## 3. What it can and cannot test

**The database is staging's own Postgres**, managed in the dashboard. It was
SQLite on the container's disk until 2026-08-12, and the reason it changed is
worth keeping: somebody created projects on a Monday and a redeploy took
them. The free plan has no persistent disk, so "ephemeral" meant *gone on
every deploy*, and an environment where nothing survives cannot be used to
walk a week of work.

Render allows **one** free database per account and production holds it, so
staging's is from another provider. The invariant that replaced the old one:
staging's `DATABASE_URL` is a pasted string, **never** a `fromDatabase` link
to `testfortge-db`. `tests/test_render_blueprint.py::
TestStagingIsActuallyStaging` fails the build on either mistake — the link,
or a literal connection string committed to git.

What that buys, beyond data that stays:

| Now testable on staging | Why it was not before |
|---|---|
| schema migrations against Postgres | the engine is the same one production runs |
| "yesterday's data" | the rows are still there tomorrow |
| a load pass that means something | SQLite's locking is not Postgres's; the numbers were measuring the wrong engine |
| **E8.5 — "delete this project's data"** | the deletion now happens in a database that is nobody's production |

**What it cost, named rather than discovered later:** every deploy used to be
a clean first run, which made staging a live regression test for the
first-admin path — the path that had no caller at all until
`engine/bootstrap.py` existed. That property is gone. `tests/
test_bootstrap_admin.py` covers the same ground with eleven tests, so the
*coverage* did not move; what was lost is the free re-proof on real
infrastructure. To get it back deliberately, drop the staging database's
tables and redeploy: the bootstrap variables are still set, so the instance
mints its first admin again.

Still not testable here:

| Not testable | Why |
|---|---|
| production's own data volumes | staging's database is small and its own |
| artefact durability | local disk until E0.5 gives it a bucket; the invariant is that it never gets production's |

## 4. Promoting a release

Today both services deploy from `main`, so a push reaches production and
staging at the same time — which makes staging a mirror, not a gate.

**Recommended, and a dashboard setting rather than a file change:** switch
production to **manual deploys** (Render → `testfortge` → Settings → Auto-Deploy
→ Off). Then:

```
push to main  →  staging deploys automatically  →  you look at it
              →  Render → testfortge → Manual Deploy → Deploy latest commit
```

Left as a recommendation rather than committed to `render.yaml` because it
changes how production behaves, and that is your call, not a side effect of
reading this file.

## 5. Free instance-hours — the one number to watch

Render's free instance-hours are **per account (750/month), not per service**.
Production's keep-alive window spends about 264 of them. Staging therefore has
**no keep-alive on purpose**: it sleeps after ~15 minutes, and a cold start on
a staging box costs nobody anything. Do not add `KEEPALIVE_URL` here — a test
enforces that too.

With three free services (prod, MCP, staging) and one keep-alive, the account
stays well inside the allowance. Adding a second keep-alive is what would
push it over, and Render stops the *whole account* when it does.

## 6. What to do on staging that you cannot do on production

1. **The first-run path.** Deploy, sign in with the bootstrap account, invite
   a second address, accept the invitation, check the roles — the whole R1/R2
   surface that production has switched off.
2. **The editors and dashboard-v2**, which are `0` in production.
3. **Real mail** — set `RESEND_API_KEY` and see whether the invitation
   arrives, which no test can answer.
4. **Accessibility.** Point axe or Lighthouse at it; a11y was never audited
   (M-3), and doing it against production means auditing a Basic-auth prompt.
5. **A load pass.** Since 2026-08-12 this measures Postgres, the engine
   production runs, so the numbers are worth comparing rather than
   discounting — read §3 for what is still different (a small database of
   its own, and a free instance that sleeps).
6. **Destructive things.** E8.5's "delete this project's data" is a
   deletion in a database that is nobody's production. That was the one
   test the shared-database option would have made unsafe, and it is why
   the invariant in §3 is worded the way it is.
