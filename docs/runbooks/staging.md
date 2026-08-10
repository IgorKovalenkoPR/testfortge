# Staging

**What it is for:** seeing a release before production does, and running the
three verification zones that need a live instance which is not the one
people use — load, accessibility, and real mail delivery. E10's entry
criteria name a deployed staging; this is it.

**What it costs:** $0. Free plan, no extra database, no keep-alive.

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
| database | Postgres (free) | SQLite, ephemeral |
| storage | local disk | local disk |
| keep-alive | working hours | none — it sleeps |

That first column is why staging matters more here than in most projects:
the mode the product ships in has **never had real traffic**, because prod
still runs with authentication off behind a Basic password. Staging is where
it gets that traffic.

The Basic gate is off here **because** authentication is on. The interlock in
`engine/basic_auth.py` refuses the other combination — gate off with auth off
would be a fully public instance, and it keeps the gate up rather than obey.

## 2. Creating it

The service is declared in `render.yaml`, so:

1. **Render → Blueprints → your blueprint → Manual Sync.** Render reads the
   file and creates `testfortge-staging`.
2. Fill two variables on the new service (**Environment**):

| Key | Value |
|---|---|
| `BOOTSTRAP_ADMIN_EMAIL` | your address |
| `BOOTSTRAP_ADMIN_PASSWORD` | a phrase of a few words, ≥12 characters |

   These are not optional here. Staging's database is ephemeral, so it is
   empty after every deploy — and an empty database cannot issue itself an
   invitation ([first-admin.md](first-admin.md)). Without them the service
   starts and nobody can sign in.
3. Optionally `RESEND_API_KEY` + `MAIL_FROM` if you want to test that an
   invitation actually arrives, and `ANTHROPIC_API_KEY` if you want AI
   generation on staging (BYOK per team also works).

It lands on `testfortge-staging.onrender.com`.

## 3. What it cannot test, and why

**The database is SQLite on the container's disk.** Render's free tier allows
**one** free Postgres per account and production holds it, so staging either
shares production's database — which E8.5's "delete this project's data"
makes unacceptable — or runs on SQLite. It runs on SQLite.

What that buys:

* **every deploy is a clean first run.** That path had no first caller at all
  until `engine/bootstrap.py` existed; staging re-proves it on every push,
  which is the strongest possible regression test for it;
* nothing to clean up, ever.

What that costs, named rather than discovered:

| Not testable on staging today | Why |
|---|---|
| schema migrations against Postgres | different engine; CI covers this with a real Postgres service container |
| "yesterday's data" | the database resets on deploy |
| concurrent-write behaviour under load | SQLite's locking is not Postgres's — a load test here measures the wrong engine |
| artefact durability | local disk, same as prod until E0.5 |

**Two ways out, when one of those matters:**

1. **Render Postgres Basic-256mb, ~$6/month** — add a second `databases:`
   entry and wire `DATABASE_URL` on the staging service. Simplest, and the
   business plan already prices it;
2. **a second free database from another provider** — free, and a different
   discussion from the one closed on 2026-08-06 about moving *production* off
   Render, which is not being reopened here.

Either way, the invariant that must survive: **staging never points at
production's database or bucket.** `tests/test_render_blueprint.py::
TestStagingIsActuallyStaging` fails the build if it does.

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
5. **A load pass**, with the caveat in §3: it measures SQLite, so treat the
   numbers as a floor rather than a forecast.
