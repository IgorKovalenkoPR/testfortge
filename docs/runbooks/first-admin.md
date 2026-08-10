# The first administrator

**What this is for:** turning on authentication for the first time. Without
this step it cannot be done — see below for why, because the reason is not
obvious and it is worth reading once.

**Time:** two variables and a redeploy.

---

## 1. Why a fresh instance cannot let anybody in

Measured on the live deployment, 2026-08-10:

* an account is created in exactly two places, and **both consume an
  invitation** — `/auth/accept/<token>` and the Google callback (which
  `engine.oauth.decide` refuses without one);
* an invitation is issued by an **admin**, at `/org/invite`;
* `db.create_organization` is called from **no route at all** — there is no
  self-service "create a team";
* and the Basic-gate interlock keeps the gate up while `AUTH_ENABLED=0`, so
  "turn the gate off instead" is not a way in either.

So `AUTH_ENABLED=1` on a database with no users locks everybody out,
permanently and silently: the sign-in page renders, and no password on earth
opens it. Every one of those four pieces is correct on its own, which is why
no test caught it — the suite creates its users through `engine.db`, the way
a fixture can and an operator cannot.

## 2. Setting it up

**Render → your `testfortge` service → Environment.** All three keys are
already declared in `render.yaml` with `sync: false`, so a Manual Sync will
not delete them.

| Key | Value |
|---|---|
| `BOOTSTRAP_ADMIN_EMAIL` | the address you will sign in with |
| `BOOTSTRAP_ADMIN_PASSWORD` | at least 12 characters — a short phrase of a few words is both stronger and easier to type than a scrambled word |
| `BOOTSTRAP_ORG_NAME` | your team's name (optional; defaults to *My team*) |

Save, then **Manual Deploy → Deploy latest commit** (or wait for the next
push). On boot the log says one of three things:

```
first-admin bootstrap created you@example.com as an admin of 'My team'.
first-admin bootstrap: 1 account(s) already exist, doing nothing.
BOOTSTRAP_ADMIN_EMAIL/BOOTSTRAP_ADMIN_PASSWORD: only one of the two is set…
```

Then, in the same Environment tab:

| Key | Value | Why |
|---|---|---|
| `AUTH_ENABLED` | `1` | the product's own login |
| `ORG_MODE` | `1` | teams and roles |
| `BASIC_GATE_ENABLED` | `0` | drops the shared Basic password — safe now, because the product's own login is up |

**Order matters, and the interlock enforces it:** setting
`BASIC_GATE_ENABLED=0` while `AUTH_ENABLED=0` is refused, and the gate stays
up. That refusal is deliberate — the alternative is a fully public instance
one typo away.

## 3. What it will not do

* **It never touches a database that already has a user.** Every boot after
  the first logs "already exist, doing nothing".
* **It never creates a weak admin.** The password goes through the product's
  own policy (`engine.auth.MIN_PASSWORD_LEN`); a short one creates nothing
  and says so in the log.
* **It never blocks boot.** A bad address, an unreachable database, anything
  at all — it logs and the service starts. A misconfigured variable must not
  be the reason the site is down.
* **It writes to the audit trail** (`user / bootstrap_admin`). Minting an
  administrator is the most privileged thing this codebase does without a
  human in the loop, so it leaves a record.

## 4. The one thing to decide consciously

The condition is **"the database has no users"**, not "the first boot ever".

That is deliberate: on the free plan the Postgres instance is deleted and
recreated roughly monthly (see
[database-on-a-free-plan.md](database-on-a-free-plan.md)), and an instance
that re-locks itself after every reset is not a usable instance.

The cost of that choice: **while these variables are set, any empty database
will acquire this administrator.** If that is not what you want — for
example on a production instance whose data you never expect to lose —
remove `BOOTSTRAP_ADMIN_PASSWORD` once you can sign in. Nothing else depends
on it, and the account you already have keeps working.

Whoever can read those variables has your Render dashboard, so this grants
no access that did not already exist. It is a convenience with a stated
price, not a hole.

## 5. Afterwards

* Invite the rest of the team from **Team → Invite someone**. Without a mail
  provider the invitation is a link the page hands you to pass on; with
  `RESEND_API_KEY` set it is emailed (see
  [object-storage-setup.md](object-storage-setup.md) for the sibling
  runbook's shape, and the Email card in Settings for what the instance
  currently does).
* If you are the only admin, the Team page says so and refuses to let you
  demote or remove yourself — the last-admin guard.
