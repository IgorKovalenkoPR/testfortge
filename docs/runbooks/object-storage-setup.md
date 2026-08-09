# Getting the object-storage credentials (E0.5)

**What this is for:** artefacts — screenshots, videos, bug attachments,
export bundles — currently live on the dyno's own disk, which is wiped on
every restart. This runbook produces the six values that move them to
storage that survives a redeploy.

**Who it is for:** whoever owns the Render account and is willing to create
one storage account. It assumes no prior knowledge of S3, and it names what
each value *is* rather than only where to paste it.

**Time:** about ten minutes, most of it waiting for pages to load.

> **Read this paragraph before you start, because the last version of this
> runbook cost a walk.** It was written for Cloudflare R2, and R2 turns out
> to require payment details to activate — a card, at $0/month. This version
> is written for **Backblaze B2**, whose own sign-up page says *"No credit
> card required"* and whose own blog says *"you don't need to give us a
> credit card to create an account"*. That is the vendor's word, not a walk,
> and the difference between those two is exactly what went wrong last time.
>
> **So the rule of this runbook outranks its steps: the moment any screen
> asks for a card, PayPal, Apple Pay or Google Pay — stop.** Close the tab,
> report what asked, and take the next candidate from
> [ADR 0002 §4.7](../plans/adr/0002-artifact-storage.md). Nothing here is
> worth paying for; the application does not care which provider answers.

---

## 0. The three words, first

| Word | What it actually is |
|---|---|
| **Bucket** | The storage container. Like a top-level folder with a name you choose. All this application's files go inside one bucket, under keys like `org/<team>/project/<id>/bug/42/screenshot.png` |
| **Access Key ID** (B2 calls it *keyID*) | A public identifier for a set of credentials — the "username" a program uses. Safe-ish to see; useless on its own |
| **Secret Access Key** (B2 calls it *applicationKey*) | The password that goes with it. **It is shown exactly once**, at creation. Lose it and you make a new pair; there is no "show again" |

Together the two keys are one credential, the way a username and password
are. The endpoint tells the application *which* account and region to talk
to.

---

## 1. Create the account and turn B2 on

1. Go to <https://www.backblaze.com/sign-up/s3> — the page for B2 Cloud
   Storage, which is the S3-compatible product. The **B2 Cloud Storage**
   product is the one you want; *Computer Backup* is a different product.
2. Create the account. The sign-up page states **"No credit card
   required."** If a later screen contradicts that, apply the rule at the
   top of this runbook and stop.
3. **Enabling B2 may ask for a mobile number and send an SMS code.**
   Backblaze's support article on enabling B2 says being able to receive
   SMS is required; their newer documentation does not mention it, so you
   may or may not see it. It is not payment data and it does not break the
   free-services constraint — but it is a real step, and knowing about it
   in advance is the point of this paragraph.
4. If the account already exists: **user menu → My Settings → Enabled
   Products → B2 Cloud Storage**, accept the terms.

## 2. Create the bucket

1. **Buckets → Create a Bucket.**
2. **Name:** `testfortge` — any name works; whatever you type here is the
   value for `STORAGE_S3_BUCKET`. Lower case, no spaces, 3–63 characters.
   The name is global across all of Backblaze, so a taken name means "pick
   another", not "something is wrong".
3. **Files in bucket are:** **Private.** The application hands out
   time-limited presigned URLs (ADR §4.4); a public bucket would make every
   screenshot in it world-readable by anyone who guesses a key.
4. Default encryption and object lock: leave off. Object lock in particular
   would make "delete this project's data" (E8.5) impossible to honour.
5. Create it.

That is `STORAGE_S3_BUCKET` — **the name you just typed**. There is nothing
to find; you chose it.

**Write down the endpoint shown for the bucket.** It looks like
`s3.eu-central-003.backblazeb2.com`. Two values come out of that one string:

| From the endpoint | Goes into |
|---|---|
| the whole host, `s3.eu-central-003.backblazeb2.com` | `STORAGE_S3_ENDPOINT` |
| the middle part, `eu-central-003` | `STORAGE_S3_REGION` |

The region is **not** optional here, unlike R2, which had no regions at all.

## 3. Create the application key — this is where the two keys appear

1. **Account → Application Keys → Add a New Application Key.**
2. **Name:** anything, e.g. `testfortge-render`.
3. **Allow access to Bucket(s):** the bucket from §2. Scoping it is worth
   the extra click: a key that can only touch one bucket cannot damage
   anything else.
4. **Type of Access: Read and Write.** Read-only is not enough — the
   application uploads, and §5 will fail on the first check and say so.
5. Leave the file-name prefix and duration empty.
6. Create it.

**The next screen is the only time you see the secret.** It shows:

| On screen | Goes into |
|---|---|
| **keyID** | `STORAGE_S3_ACCESS_KEY` |
| **applicationKey** | `STORAGE_S3_SECRET_KEY` |
| **Endpoint** — `s3.<region>.backblazeb2.com` | `STORAGE_S3_ENDPOINT` (and its middle part into `STORAGE_S3_REGION`) |

Copy all of them now, into a password manager. If you navigate away before
copying the secret, you cannot retrieve it — create a new key and use the
new pair.

> Providers change these interfaces. If a label does not match, look for the
> words **keyID** / **Access Key ID**, **applicationKey** / **Secret Access
> Key**, and an endpoint host. Those three are what matter, whatever the
> surrounding page is called this month.

## 4. Put them into Render

**Render → your `testfortge` service → Environment.** All six keys already
exist there (declared in `render.yaml` with `sync: false`, which is what
stops a Manual Sync from deleting them). Fill in the values:

| Key | Value |
|---|---|
| `STORAGE_S3_ENDPOINT` | the endpoint host from §2/§3 |
| `STORAGE_S3_BUCKET` | the name you chose in §2 |
| `STORAGE_S3_ACCESS_KEY` | keyID from §3 |
| `STORAGE_S3_SECRET_KEY` | applicationKey from §3 |
| `STORAGE_S3_REGION` | the middle part of the endpoint, e.g. `eu-central-003` |
| `STORAGE_S3_SECURE` | `1` |

Save. Do **not** flip `STORAGE_BACKEND` here — see §6.

## 5. Prove it works before switching anything

```bash
STORAGE_S3_ENDPOINT="s3.eu-central-003.backblazeb2.com" STORAGE_S3_BUCKET="testfortge" STORAGE_S3_ACCESS_KEY="<keyID>" STORAGE_S3_SECRET_KEY="<applicationKey>" STORAGE_S3_REGION="eu-central-003" STORAGE_S3_SECURE=1 python scripts/verify_storage.py
```

Seven checks: write, read back byte for byte, stat, fetch a presigned URL
over the network, list a prefix, the application's own connection check,
and delete. Everything it writes lives under `_verify/` and is removed at
the end, including on failure.

Expect seven `[PASS]` and *"All checks passed."*

The script is itself exercised by the suite against a real S3 server
(`tests/test_storage_failures.py::TestTheScriptTheOwnerWillRun`), so a
failure here is about the bucket, not about the script.

Common failures, and the message you will actually see:

| Message | Cause |
|---|---|
| `The credentials are valid and '<name>' exists, but this key is not allowed to write. Grant it s3:PutObject` | The key is read-only. Make a new one with **Read and Write** |
| `'<name>' is not a valid bucket name` | Typo, or the name has capitals/spaces |
| `there is no bucket called '<name>'` | The credentials are fine and the name does not match — check §2 |
| `rejected the credentials. Check the access key and the secret` | A truncated secret is the usual cause; B2 shows it once, so people copy it in a hurry. Or `STORAGE_S3_REGION` does not match the endpoint |
| `Could not reach …` | The endpoint is wrong. It is a bare host, `s3.<region>.backblazeb2.com`, with no bucket name in it |
| `delete what it wrote` fails | The key can write but not delete. E8.5 ("delete this project's data") would then be a promise the deployment cannot keep — fix the key |

If it fails, report **the message text**, not the keys. Nobody debugging
this needs the credentials, and a key pasted into a chat log is a key that
has to be rotated.

## 6. Switch the backend — in the repository, not the dashboard

`STORAGE_BACKEND` is declared in `render.yaml` with `value: "local"`.
Changing it in the dashboard works until the next Manual Sync, which resets
it to whatever the blueprint says — the failure mode E0.6 exists to
prevent. So the switch is a commit:

```yaml
      - key: STORAGE_BACKEND
        value: "s3"
      - key: ARTEFACT_MAX_RUNS
        value: "25"
```

**The second line is not optional, and it is arithmetic rather than
caution.** Durable retention keeps 30 days *or* 50 runs, and one run is
50–200 MB — so 50 runs is up to 10 GB of run artefacts alone, which is the
entire free allowance, before a single attachment or backup bundle. 25 runs
caps the worst case at about 5 GB and leaves the rest for E8.4 and E4.5a.
Raise it once you have watched real usage; the number is a guess about run
size, and the bucket will tell you the true one.

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
  storage. Nothing to set beyond the cap in §6; the policy reads the
  backend.
- **Backups** (`BACKUP_TOKEN`, `.github/workflows/backup.yml`) start landing
  in the bucket rather than on the disk they were about to lose.
- **Watch the free allowance.** B2's free tier is 10 GB stored, with egress
  free up to three times the average stored volume per month. The Settings
  page shows usage; the bucket's own dashboard is the authority.

## 8. If Backblaze asks for a card after all

Then it joins R2, and the next candidate is in
[ADR 0002 §4.7](../plans/adr/0002-artifact-storage.md): iDrive e2 (10 GB),
then a self-hosted MinIO (the `docker-compose.yml` in this repository
already runs one — see [self-hosting.md](self-hosting.md)), and finally the
honest option of staying on `local`.

**Staying on `local` is a decision, not a failure**, and it costs exactly
three things, which are worth saying out loud rather than discovering:

* bug attachments and run screenshots disappear on every restart — and on
  Render's free plan a restart happens daily, from sleep;
* export bundles (E8.4) are written to the same disk they exist to survive,
  so a backup is only real while the process that wrote it is still alive;
* retention stays at 1 day / 5 runs, because those numbers are the disk
  talking, not a policy.

Everything else in the product works unchanged, which is why this is an
acceptable outcome and not a blocker.
