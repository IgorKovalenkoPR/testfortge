# Self-hosting TestForTge

**Epic:** E8.6 · **Acceptance criterion:** *a person from outside brings an
instance up from these instructions, from scratch.*

That criterion is why this document is written the way it is. It assumes you
have not read the source, do not know which environment variables exist, and
will stop at the first command that does not work. Everything you need is
here or is one link away; nothing says "configure as appropriate".

> **What was verified, and what was not.** Every value, path and command name
> below was checked against the repository — the variables are ones the code
> actually reads, the files exist, the flags are declared in
> `engine/features.py`. The `docker compose up` sequence itself has **not**
> been executed on a machine with Docker, because the environment this was
> written in has none. So: the configuration is verified, the orchestration
> is reviewed. If step 3 fails for you, that is the gap, and
> [§9](#9-if-it-does-not-come-up) is written for it.

---

## 0. What you are about to run

Four containers, three of which stay up:

| | | |
|---|---|---|
| `db` | PostgreSQL 16 | Required. Not a preference — see [§1](#1-why-postgres-is-not-optional). |
| `storage` | MinIO | S3-compatible object storage for screenshots, videos and attachments. |
| `storage-init` | MinIO client | Creates the bucket, then exits. |
| `app` | Flask + gunicorn + Playwright | TestForTge itself. |

**You need:** a machine with Docker and the Compose plugin, about 4 GB of
RAM, and 10 GB of disk. Chromium is inside the app image, which is why it is
large (~2 GB) and why the first build is slow.

---

## 1. Why Postgres is not optional

TestForTge refuses to start on SQLite unless `FLASK_DEBUG=1`
(`engine/db.py::_assert_prod_safety`). This is deliberate and it is not
conservatism: gunicorn runs several workers, the test runner is a *detached
subprocess*, and both write concurrently. On one SQLite file that deadlocks
under load rather than failing cleanly.

There is an escape hatch — `TESTFORTGE_ALLOW_SQLITE_PROD=1` downgrades the
refusal to a warning — and this runbook does not use it. If you are running
one instance for yourself and accept the risk, it exists.

---

## 2. Get the code and make a `.env`

```bash
git clone https://github.com/IgorKovalenkoPR/testfortge.git
cd testfortge
cp .env.example .env
```

`.env` is where every secret lives, and `docker-compose.yml` refuses to start
without four of them. That refusal is on purpose: a missing `SECRET_KEY` does
not fail, it silently means "nobody stays signed in across a restart", which
looks like a bug in the product rather than a gap in the deployment.

Generate all four at once and append them:

```bash
python3 - <<'EOF' >> .env
import secrets
print(f"SECRET_KEY={secrets.token_urlsafe(48)}")
print(f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}")
print(f"TESTFORTGE_ENCRYPTION_KEY={secrets.token_urlsafe(48)}")
print(f"STORAGE_S3_ACCESS_KEY={secrets.token_hex(10)}")
print(f"STORAGE_S3_SECRET_KEY={secrets.token_urlsafe(32)}")
EOF
```

Then delete the empty `SECRET_KEY=` line the example file ships with, so the
value you just generated is the one that wins.

**What each is for:**

| Variable | What breaks without it |
|---|---|
| `SECRET_KEY` | Session cookies. Change it later and everyone is signed out. |
| `POSTGRES_PASSWORD` | The database. |
| `TESTFORTGE_ENCRYPTION_KEY` | Per-team secrets — a team's own Anthropic key and its own storage credentials. Without it the app runs and *refuses to store either*, which is the correct refusal and a confusing first impression. **Rotating it makes everything stored under it unreadable**, so treat it like the database password. |
| `STORAGE_S3_ACCESS_KEY` / `_SECRET_KEY` | MinIO's own root credentials, which the app then uses as its S3 credentials. |

---

## 3. Bring it up

```bash
docker compose up -d --build
```

The first build downloads the Playwright base image. Expect ten minutes and
a couple of gigabytes.

```bash
docker compose ps
curl http://localhost:5000/healthz
```

`healthz` should return `200`. `docker compose ps` should show `db`,
`storage` and `app` as `healthy`, and `storage-init` as `exited (0)` — that
one is meant to exit; it creates the bucket and stops.

Open <http://localhost:5000>. You will be asked for a password: see the next
section.

---

## 4. Who gets in

Out of the box the whole application sits behind one shared HTTP Basic
password. Set it in `.env`:

```
TESTFORTGE_BASIC_USER=qa
TESTFORTGE_BASIC_PASSWORD=<something long>
```

That is the perimeter until you turn on real accounts. To do that:

```
AUTH_ENABLED=1
ORG_MODE=1
```

then restart (`docker compose up -d`), create your first account through the
sign-up page, and only **then**:

```
BASIC_GATE_ENABLED=0
```

**In that order.** Setting `BASIC_GATE_ENABLED=0` while `AUTH_ENABLED=0`
means no shared password and no accounts either — an open instance.
`engine/basic_auth.py` refuses that combination and keeps the gate up, so the
mistake is survivable, but the order above avoids the argument.

Sign-up needs email to send anything (password resets, invitations). Without
`RESEND_API_KEY` and `MAIL_FROM` the app falls back to showing the link on
screen for an admin to pass along, which is workable for a small team and
tedious for a large one.

---

## 5. Where your data lives

Five Docker volumes, and knowing which is which is the difference between an
upgrade and an incident:

| Volume | Holds | Losing it means |
|---|---|---|
| `testfortge-db` | Everything: projects, test cases, bugs, runs, users | Total loss |
| `testfortge-objects` | Screenshots, videos, attachments | Evidence gone, rows still reference it |
| `testfortge-uploads` | Files people uploaded to generate from | Re-uploadable |
| `testfortge-storage` | Run artefacts in progress | Nothing, once runs finish |
| `testfortge-sessions` | Only used if you set `SESSION_BACKEND=filesystem` | Everyone signed out |

Back up the first two. The database with `pg_dump`:

```bash
docker compose exec -T db pg_dump -U testfortge testfortge | gzip > backup-$(date +%F).sql.gz
```

Objects with the MinIO client:

```bash
docker compose run --rm storage-init \
  mc mirror --overwrite local/testfortge /backup
```

The application also backs itself up (E8.4). Set `BACKUP_TOKEN` in `.env`,
then either press **Back up** next to a project or POST to
`/api/backup/run` with that token — `.github/workflows/backup.yml` does the
latter weekly if you set `BACKUP_URL` and the same token as repository
secrets. Each bundle is a zip with a `manifest.json` of SHA-256 checksums,
and **Restore** rebuilds it into a *new* project rather than overwriting
anything.

Two limits worth knowing before you rely on it:

* **a backup does not survive deleting its project.** Deleting a project
  deletes its bundles, because a deletion that leaves a restorable copy is
  not a deletion. For "somebody removed the wrong project", the answer is
  the **Export** button, whose zip is held by whoever downloaded it;
* **run history is in the bundle and is not restored** — test cases,
  checklist, estimation, bugs and files come back; execution and automation
  runs do not. The restore says so when it finishes.

The `pg_dump` above is still worth taking: bundles cover a project, not the
users, teams and audit trail around them.

---

## 6. Putting it on the internet

The compose file publishes port 5000 in plain HTTP. Do not expose that
directly. Put a reverse proxy in front that terminates TLS — Caddy, nginx,
Traefik, whichever you already run — and then set:

```
BEHIND_HTTPS=1
```

That is not cosmetic. It is what marks the session cookie `Secure`; without
it the cookie travels over any connection the browser will make, and a proxy
in front of an instance that still says `BEHIND_HTTPS=0` is exactly the
configuration where that matters.

A minimal Caddy front:

```
qa.example.com {
    reverse_proxy localhost:5000
}
```

Caddy obtains the certificate itself. With nginx you supply one.

Two things already published deliberately narrowly, which you should leave
alone: Postgres has no published port at all, and the MinIO console is bound
to `127.0.0.1` so it is reachable through an SSH tunnel and not from
outside.

---

## 7. Upgrading

```bash
git pull
docker compose up -d --build
```

Schema changes are applied at boot; there is no separate migration command.
Take a `pg_dump` first anyway — see [§5](#5-where-your-data-lives).

Sessions survive an upgrade because `SESSION_BACKEND=db` is the default in
this compose file. If you overrode it to `filesystem`, every upgrade signs
everyone out.

---

## 8. Turning things on and off

Every flag below is declared in `engine/features.py`, which is the list to
read if you want the full set. These are the ones a self-hoster changes:

| Flag | Default here | What it does |
|---|---|---|
| `AUTH_ENABLED` | `0` | Real accounts, email + Google sign-in |
| `ORG_MODE` | `0` | Teams and the admin/user split. Needs `AUTH_ENABLED` |
| `BASIC_GATE_ENABLED` | `1` | The shared password in front of everything |
| `STORAGE_BACKEND` | `s3` | `local` puts artefacts in a container volume instead |
| `TESTFORTGE_BROWSER_ENABLED` | `1` | Real Chromium runs. Turn off on a small box |
| `SESSION_BACKEND` | `db` | `filesystem` loses sessions on restart |
| `STORAGE_BACKEND_CONFIGURABLE` | `0` | Lets each team pick its own bucket. Untested against a live bucket (E8.7) — leave it off |
| `BACKUP_TOKEN` | unset | Enables `POST /api/backup/run`. Unset means the endpoint refuses outright |
| `BACKUP_KEEP` | `7` | Bundles kept per project; older ones are pruned on each new backup |

Changes to any of these need `docker compose up -d` to take effect, because
they are container environment. Inside a running process the app re-reads
them per request, which is what makes them dashboard settings on a hosted
deployment.

---

## 9. If it does not come up

**`docker compose up` exits complaining about a variable.** That is the
`:?` guard, and it names the variable. Something in `.env` is missing or the
file is not where compose is looking (same directory as
`docker-compose.yml`).

**`app` restarts in a loop.** Look at why:

```bash
docker compose logs app --tail=50
```

- `RuntimeError: TestForTge starting with SQLite in non-debug mode` — the
  app is not seeing `DATABASE_URL`. It is set by the compose file from
  `POSTGRES_PASSWORD`; an empty password produces a URL that does not
  connect.
- `could not connect to server` — `db` is not healthy yet. `depends_on`
  waits for the healthcheck, so this usually means Postgres itself is
  failing; check `docker compose logs db`.

**`storage-init` exits non-zero and `app` never starts.** The bucket was not
created, and `app` waits for that on purpose. Check
`docker compose logs storage-init`; the usual cause is MinIO credentials
containing characters your shell ate on the way into `.env` — quote them.

**Setting up a hosted bucket instead of MinIO?** [object-storage-setup.md](object-storage-setup.md) walks Cloudflare R2 from zero, including where the two keys appear and the one screen that shows the secret once.

**Before you trust a bucket, verify it.** `python scripts/verify_storage.py`
reads the same `STORAGE_S3_*` variables the app does and runs the operations
the product performs — write, read back byte for byte, stat, fetch a
presigned URL over the network, list by prefix, and delete. It cleans up
after itself and exits non-zero on the first thing that is not true. Run it
after creating the bucket and again after any bucket-policy change; a key
that can write but not delete passes every casual check and then makes
"delete this project's data" a promise you cannot keep.

**Uploads fail with "the storage could not write".** The app is up but
cannot reach the bucket. With `AUTH_ENABLED=1` there is a **Test connection**
button under *Settings → Storage* that writes a file, reads it back and
deletes it, and names which of the five things is wrong. Use that before
guessing.

**Runs fail or the container is OOM-killed.** Chromium is the expensive
part. Either raise `TESTFORTGE_MEMORY_LIMIT` (default `2g`) or set
`TESTFORTGE_BROWSER_ENABLED=0` and use the manual walkthrough mode.

---

## 10. What this deployment does not give you

Stated because a runbook that only lists what works is a runbook that gets
believed:

- **The S3 adapter is tested against a mock, not against your provider.**
  The suite runs it over real HTTP against `moto`, which implements S3
  semantics but does not enforce signatures and has no bucket policies. Run
  `scripts/verify_storage.py` against your own bucket — that is the check
  that covers the difference.
- **Backups are per project, and never verified against your bucket.** The
  bundle format and the restore path are tested end to end (a project is
  built, backed up, destroyed and restored, and a restored screenshot is
  fetched), but against local disk — whether *your* S3 returns the bytes it
  was given is E8.7 and needs credentials. Restore one bundle by hand after
  the first scheduled run. A backup nobody has restored from is a
  hypothesis.
- **No per-team storage choice.** The flag exists and the code behind it is
  written, but it has never run against a real bucket (E8.7), so it stays
  off.
- **No email unless you configure it.** Resets and invitations fall back to
  a link on screen.
- **One instance.** No clustering, no failover. The single container is the
  single point of failure, which for a QA tool is usually the right trade
  and should be a decision rather than a discovery.
