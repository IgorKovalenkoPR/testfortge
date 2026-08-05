# Real browser runs in CI (E5.2′)

Requirement 8 asks for real Playwright passes. The original plan enabled the
browsers inside the web service; that needs RAM the free tier does not have,
and paying for it was ruled out. So the browsers run in GitHub Actions,
where the minutes are free, and the results are posted back to the ingest
endpoint the app already had.

Nothing in the application changed for this.
`POST /automation/allure-results` has always accepted a zipped
`allure-results` directory from a token-authenticated caller — this is that
caller.

## Setting it up, once

**1. Get the suite into a repository.**

In the app: **Automation → Download bundle**. The zip contains
`package.json`, `tsconfig.json`, `playwright.config.ts` and the generated
`steps/`. Commit it, or unzip it into an existing repo. The workflow's
`suite_path` input points at wherever it lands (default `automation`).

The bundle ships without a `package-lock.json`, so the workflow runs
`npm install` when there is no lockfile and `npm ci` when there is.
Committing a lockfile is worth doing: it makes the install reproducible and
lets the Actions cache work.

**2. Add two repository secrets** under Settings → Secrets and variables →
Actions:

| Secret | Value |
|---|---|
| `TFG_INGEST_URL` | `https://<your-app>/automation/allure-results` |
| `TFG_INGEST_TOKEN` | the same value as `AUTOMATION_INGEST_TOKEN` on the service |

If `AUTOMATION_INGEST_TOKEN` is not set on the service, ingestion is
disabled and the endpoint answers 403 — deliberately, because an open ingest
endpoint would let anyone write run history into somebody's project.

**3. Run it.** Actions → *playwright* → Run workflow. Inputs:

| Input | Meaning |
|---|---|
| `base_url` | overrides the suite's own default, so one suite can test staging and production |
| `suite_path` | where the suite lives in the repo |
| `project_id` | which TestForTge project the results belong to |
| `label` | shown in the app's run history |
| `grep` | Playwright `--grep`, to run a subset |

## What the workflow guarantees, and why

**It checks the secrets before installing the browsers.** A green suite whose
results are silently dropped is the worst outcome available: the app shows no
run and nobody knows why. Failing in ten seconds with a message naming the
missing secret is better than failing in four minutes with a curl error.

**A failing suite still reports.** The test step is
`continue-on-error: true` on purpose. A failing suite is the normal reason to
run one, and a non-zero exit there would skip the ingest — so the app would
show nothing exactly when it has something to show. The job still turns red
afterwards, in a final step that runs only once the results are safely in.

**The results are uploaded as an artifact before the POST.** If the ingest
fails, the run is recoverable from the artifact rather than needing to be
repeated.

**Chromium only, and a 20-minute timeout.** A cross-browser matrix triples
install time and cache size, and a hung suite would otherwise spend the
month's allowance on one run. Both are decisions for whoever needs them, not
the default cost of every run.

## Checking that a run landed

In the app: **Automation → run history**. A successful ingest also writes a
job summary in Actions with the run number and the pass/fail counts, so the
two can be compared without opening the app.

## Failure modes, and what each one means

| Symptom | Cause |
|---|---|
| `Missing repository secret(s)` | step 2 was skipped |
| `No package.json under '<path>'` | the suite is not committed, or `suite_path` is wrong |
| `The suite produced no allure-results` | no test matched `grep`, or `allure-playwright` is missing from the reporter list in `playwright.config.ts` |
| HTTP 401 | `TFG_INGEST_TOKEN` does not equal `AUTOMATION_INGEST_TOKEN` |
| HTTP 403 | `AUTOMATION_INGEST_TOKEN` is not set on the service at all |
| HTTP 422 | the archive parsed to zero results — the zip is there but has no Allure JSON in it |
| Job red, results visible in the app | the suite found failures. That is the workflow working |

## Triggering it from somewhere else

The workflow also listens for a `repository_dispatch` of type
`tfg-playwright-run`, with the same keys in `client_payload` as the manual
inputs:

```bash
curl -X POST \
  -H "Authorization: Bearer <a PAT with actions:write>" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/<owner>/<repo>/dispatches \
  -d '{"event_type":"tfg-playwright-run",
       "client_payload":{"base_url":"https://staging.example.com",
                         "project_id":"<id>","label":"nightly"}}'
```

That is the hook a queue would use (E5.5). It is deliberately not wired up
from the app yet: it needs a GitHub personal access token stored on the
service, which is a credential the operator has to create and decide to
trust, not something to configure on their behalf.
