# Environment audit — what a Manual Sync would do (E0.6)

**Epic:** E0.6 · **Acceptance criterion:** *`test_render_blueprint` covers
every flag; a Manual Sync extinguishes nothing.*

Two halves, and only one of them is checkable from here.

**The code half is now enforced.** `render.yaml` declares every variable
that gates behaviour or carries a credential, and
`tests/test_render_blueprint.py` derives that requirement from the source
rather than from a list somebody has to remember to extend. It fails the
build if a new `*_ENABLED`/`*_TOKEN`/`*_SECRET`/`*_PASSWORD`/`*_BYPASS`
appears in the code and not in the blueprint, and it pins the switches that
disable a guard to `"0"`.

**The dashboard half is yours.** Render's Manual Sync reconciles the live
service against this file and **deletes every environment variable the
blueprint does not name**. Nothing in this repository can see what is
currently set on the service, so §3 below is a checklist to run once,
against the dashboard, before the next sync.

---

## 1. What changed in `render.yaml`

Twelve variables were read by the code and declared nowhere. Each would
have been deleted by the next sync, silently.

| Variable | Now | Why it matters |
|---|---|---|
| `BACKUP_TOKEN` | `sync: false` | Gates `POST /api/backup/run` (E8.4). Deleted → the weekly backup workflow fails 403 |
| `OPS_ENDPOINTS_TOKEN` | `sync: false` | Guards `/metrics`. **Unset means open** — `route_policy` leaves `metrics` out of the machine exemption for this reason |
| `STORAGE_S3_SECURE` | `sync: false` | The fifth of five `STORAGE_S3_*`; four were added and this was missed |
| `ORG_QUOTA_ROWS` | `sync: false` | Per-organisation row quota (E0.12) |
| `BROWSER_CONTROL_ENABLED` | `"0"` | The MCP browser driver |
| `TC_AUTHOR_ENABLED` | `"1"` | LLM test-case author — default on in code, so it could not be turned *off* without being declared |
| `CL_AUTHOR_ENABLED` | `"1"` | The same, for checklists |
| `TESTFORTGE_SNAPSHOT_WORKER` | `"1"` | The daily metric-snapshot thread |
| `LEGACY_EXECUTOR` | `"0"` | Pinned off — the pre-Stage-3 runner is for rollback |
| `SSRF_ALLOWLIST_BYPASS` | `"0"` | Pinned off — disables the SSRF allowlist |
| `TESTFORTGE_ALLOW_SQLITE_PROD` | `"0"` | Pinned off — disables the refuse-to-boot-on-SQLite guard |
| `TESTFORTGE_BASIC_PUBLIC_PATHS` | `/healthz,/readyz` | Pinned — every path listed skips the HTTP Basic gate |

The last four are **pinned rather than merely declared**, and that
distinction was earned: mutation testing flipped `SSRF_ALLOWLIST_BYPASS`
to `"1"` in the blueprint and every other check still passed. A declaration
that accepts either value buys visibility and no protection. The same
mutation set widened the Basic allowlist to `"/"` — the whole perimeter off
— and that passed too, until it did not.

## 2. What was deliberately left out

Roughly fifty variables are pure tuning with sane defaults —
`TC_RULES_MAX_GRIDS`, `LIVE_PAINT_MIN_MS`, `JOB_RETENTION_SECONDS`,
`ANTHROPIC_MAX_TOKENS` and the like. Declaring them would add noise to the
file that has to stay readable to be useful, and a deleted tuning knob
falls back to the value the code already ships.

The rule is written down in `tests/test_render_blueprint.py` rather than
here, so it is applied rather than remembered.

**One limitation, stated because it bounds the whole audit.** The sweep
reads `os.environ` calls out of the source. It cannot see an indirect
read — `engine.features` reaches the environment through `is_enabled(name)`,
and `TESTFORTGE_ENCRYPTION_KEY` through a module constant. So the audit can
prove a declared variable is *mentioned*; it cannot prove an undeclared one
is *unused*. The residue is handled by naming the known gates explicitly,
and the durable fix is for new gates to go in `engine/features.py`, which is
derived completely.

---

## 3. Before the next Manual Sync — check the dashboard

Run this once. It is the half no test can reach.

1. Open **Render → testfortge → Environment**.
2. For **every** variable listed there, ask: *is this key in
   `render.yaml`?* Search the file for the name.
3. Any key that is **not** in `render.yaml` will be **deleted** by the next
   sync. For each one, decide:
   - it matters → add it to `render.yaml` (with `sync: false` if the value
     should stay in the dashboard), commit, *then* sync;
   - it does not → delete it in the dashboard now, so the next person does
     not have to wonder.
4. Check the four pinned switches actually read `0` on the service:
   `SSRF_ALLOWLIST_BYPASS`, `TESTFORTGE_ALLOW_SQLITE_PROD`,
   `LEGACY_EXECUTOR`, and `TESTFORTGE_BASIC_PUBLIC_PATHS=/healthz,/readyz`.
   If any is different, the sync will change behaviour — know which before
   you press it, not after.
5. Repeat for **testfortge-mcp**. It is a separate service with its own
   environment block, and a variable declared on the web service does
   nothing for it.

**Known drift to look for specifically.** The programme's history records
at least one flag set by hand and never added to the blueprint
(`RECORDER_ENABLED`, found on 2026-07-30 by diffing the dashboard against
this file — which is what prompted the test that now exists). Expect
others.

## 4. What is still owner-only

These have no value in the blueprint on purpose, and the service does not
work as intended until they are set in the dashboard:

| Variable | Needed for |
|---|---|
| `RESEND_API_KEY`, `MAIL_FROM` | Password resets and invitations. Without them the app falls back to showing the link on screen |
| `STORAGE_S3_*` (6) | Object storage. Until then artefacts are on the ephemeral disk (E0.5). **Step-by-step: [object-storage-setup.md](object-storage-setup.md)** |
| `BACKUP_TOKEN` | The weekly backup workflow, with the same value as the repository secret |
| `OPS_ENDPOINTS_TOKEN` | Closing `/metrics` |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google sign-in |
| `ANTHROPIC_API_KEY` | LLM generation, unless every team brings its own key |

Repository **variables** (Settings → Secrets and variables → Actions):
`KEEPALIVE_URL`, `BACKUP_URL`. Repository **secret**: `BACKUP_TOKEN`. Both
workflows no-op without them rather than failing, so an unset one is silent.
