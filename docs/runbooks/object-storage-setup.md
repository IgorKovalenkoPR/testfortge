# Getting the object-storage credentials (E0.5)

**What this is for:** artefacts — screenshots, videos, bug attachments,
export bundles — currently live on the dyno's own disk, which is wiped on
every restart. This runbook produces the five values that move them to
storage that survives a redeploy.

**Who it is for:** whoever owns the Cloudflare and Render accounts. It
assumes no prior knowledge of S3 or R2, and it names what each value *is*
rather than only where to paste it.

**Time:** about ten minutes, most of it waiting for pages to load.

---

## 0. The three words, first

| Word | What it actually is |
|---|---|
| **Bucket** | The storage container. Like a top-level folder with a name you choose. All this application's files go inside one bucket, under keys like `org/<team>/project/<id>/bug/42/screenshot.png` |
| **Access Key ID** | A public identifier for a set of credentials — the "username" a program uses. Safe-ish to see; useless on its own |
| **Secret Access Key** | The password that goes with it. **Cloudflare shows it exactly once**, at creation. Lose it and you make a new pair; there is no "show again" |

Together the two keys are one credential, the way a username and password
are. The endpoint tells the application *which* R2 account to talk to.

---

## 1. Turn on R2

1. Go to <https://dash.cloudflare.com> and sign in.
2. **R2 is not a top-level item.** In the current dashboard it lives under
   **Storage & databases → R2 → Overview**. Looking for "R2" in the root
   sidebar and not finding it is the normal first experience, and it is
   what prompted this section.

   The direct URL is faster:
   `https://dash.cloudflare.com/<your-account-id>/r2/overview` — the
   account id is the long hex string already in your dashboard URL.
3. R2 has to be **subscribed to** before it appears as usable: Cloudflare
   runs a short checkout flow to add the R2 subscription to the account.
   The free allowance is 10 GB of storage, 1 million writes a month and —
   the reason this project chose R2 — **$0 egress**, which is what makes
   serving screenshots straight from the bucket free rather than metered.

> Whether that checkout requires a card on file is not something this
> runbook can state from the documentation, and it is the step people get
> stuck on. If it asks for one and that is a blocker, say so: the
> application speaks plain S3 and does not care which provider answers.
> Backblaze B2, Wasabi, AWS S3 and a self-hosted MinIO all work with the
> same five values.

## 2. Create the bucket

1. Click **Create bucket**.
2. **Name:** `testfortge` (any name works — whatever you type here is the
   value for `STORAGE_S3_BUCKET`). Lower case, no spaces.
3. **Location:** *Automatic* is fine. If you prefer, pick the hint closest
   to your users — the Render service is in Frankfurt, so EU is sensible.
4. Click **Create bucket**.

That is `STORAGE_S3_BUCKET` — **the name you just typed**. There is nothing
to find; you chose it.

## 3. Create the API token — this is where the two keys appear

Still in R2:

1. On the R2 overview page find **Account details**, and next to
   **API Tokens** click **Manage**. (Older guides call this "Manage R2 API
   Tokens" and put it on the right-hand side.)
2. Click **Create API token** (may be called "Create Account API token").
3. **Token name:** anything, e.g. `testfortge-render`.
4. **Permissions:** choose **Object Read & Write** — the level that can
   write and delete objects over the S3 API, scoped to buckets you pick.
   (*Admin Read & Write* also works and grants more than this needs.)
   *Read-only is not enough* — the application uploads, and the
   verification in §5 will fail on the write step and tell you so.
5. **Specify bucket** (if offered): scope it to the bucket from §2. This is
   optional and worth doing: a token that can only touch one bucket cannot
   damage anything else.
6. Click **Create API Token**.

**The next screen is the only time you see the secret.** It shows:

| On screen | Goes into |
|---|---|
| **Access Key ID** | `STORAGE_S3_ACCESS_KEY` |
| **Secret Access Key** | `STORAGE_S3_SECRET_KEY` |
| **Endpoint for S3 clients** — `https://<long-hex-id>.r2.cloudflarestorage.com` | `STORAGE_S3_ENDPOINT` |

Copy all three now, into a password manager. If you navigate away before
copying the secret, you cannot retrieve it — create a new token and use the
new pair.

> Cloudflare changes this interface from time to time. If a label does not
> match, look for the words **Access Key ID**, **Secret Access Key** and an
> endpoint ending in `r2.cloudflarestorage.com`. Those three are what
> matter, whatever the surrounding page is called this month.

## 4. Put them into Render

**Render → your `testfortge` service → Environment.** All six keys already
exist there (declared in `render.yaml` with `sync: false`, which is what
stops a Manual Sync from deleting them). Fill in the values:

| Key | Value |
|---|---|
| `STORAGE_S3_ENDPOINT` | the endpoint from §3 |
| `STORAGE_S3_BUCKET` | the name you chose in §2 |
| `STORAGE_S3_ACCESS_KEY` | Access Key ID from §3 |
| `STORAGE_S3_SECRET_KEY` | Secret Access Key from §3 |
| `STORAGE_S3_REGION` | **leave empty** — R2 has no regions. (AWS S3 would need a real one) |
| `STORAGE_S3_SECURE` | `1` |

Save. Do **not** flip `STORAGE_BACKEND` here — see §6.

## 5. Prove it works before switching anything

```bash
STORAGE_S3_ENDPOINT="https://<id>.r2.cloudflarestorage.com" \
STORAGE_S3_BUCKET="testfortge" \
STORAGE_S3_ACCESS_KEY="<access key id>" \
STORAGE_S3_SECRET_KEY="<secret>" \
STORAGE_S3_SECURE=1 \
python scripts/verify_storage.py
```

Seven checks: write, read back byte for byte, stat, fetch a presigned URL
over the network, list a prefix, the application's own connection check,
and delete. Everything it writes lives under `_verify/` and is removed at
the end, including on failure.

Expect seven `[PASS]` and *"All checks passed."*

Common failures and what they mean:

| Message | Cause |
|---|---|
| `write an object` fails with 403 | The token is read-only. Make a new one with **Object Read & Write** |
| `'<name>' is not a valid bucket name` | Typo, or the name has capitals/spaces |
| `there is no bucket called '<name>'` | The credentials are fine and the name does not match — check §2 |
| `Could not reach …` | The endpoint is wrong. It ends in `r2.cloudflarestorage.com` and contains your account id |
| `delete what it wrote` fails | The token can write but not delete. E8.5 ("delete this project's data") would then be a promise the deployment cannot keep — fix the token |

## 6. Switch the backend — in the repository, not the dashboard

`STORAGE_BACKEND` is declared in `render.yaml` with `value: "local"`.
Changing it in the dashboard works until the next Manual Sync, which resets
it to whatever the blueprint says — the failure mode E0.6 exists to
prevent. So the switch is a commit:

```yaml
      - key: STORAGE_BACKEND
        value: "s3"
```

Then deploy. After that, upload an attachment to a bug, redeploy the
service, and check the attachment still renders. That is E0.5's acceptance
criterion — *artefacts survive a redeploy* — and it is worth doing by hand
once.

## 7. Afterwards

- **`STORAGE_BACKEND_CONFIGURABLE`** can be reconsidered. It is `0` because
  ADR 0002's third gate wants a run against a live bucket before each team
  is offered its own storage. §5 is that run.
- **Retention changes by itself.** `engine/retention.py` keeps run artefacts
  for 1 day / 5 runs on the ephemeral disk and 30 days / 50 runs on durable
  storage. Nothing to set; the policy reads the backend.
- **Backups** (`BACKUP_TOKEN`, `.github/workflows/backup.yml`) start landing
  in the bucket rather than on the disk they were about to lose.
