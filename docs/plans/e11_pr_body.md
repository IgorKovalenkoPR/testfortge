Triage of the 30-finding browser-extension walkthrough of staging (report
2026-08-21). Every claim checked against the source, then fixed,
reclassified, or refuted. Full record: `docs/plans/e11_walkthrough_triage.md`.

**Suite: 4716 → 4776 passing, 56 skipped, 0 failing — in both flag modes.**
CI green on 3.11/3.12/3.13 against real Postgres, including the coverage
gate, the `AUTH_ENABLED=1 ORG_MODE=1` leg, and the ×10 consecutive e2e job.
Each new test file fails without its fix; three were confirmed to fail by
reverting or by CI.

Three commits. The second and third fix defects the **first one
introduced** — see *Two defects this PR caused* below. Worth reading before
the rest, because both say something about the shape of the tests here.

---

## Two defects worth the read

### The entire HTTP crawl path was dead

```python
# engine/site_crawler.py:1064, before
with _security.safe_opener().open(req, timeout=FETCH_TIMEOUT, context=ctx) as resp:
```

`safe_opener()` returns an `OpenerDirector`, whose method is
`open(fullurl, data=None, timeout=...)`. There is no `context` parameter —
that belongs to `urllib.request.urlopen`. The kwarg was carried over when
these call sites moved off `urlopen` to pick up the every-hop SSRF policy
(`1b31777`).

So every fetch raised `TypeError` during argument binding — before DNS,
before a socket — the bare `except Exception` turned it into `("", "...")`,
and zero pages made `landing` the winning site type. **Every URL on the
internet** returned "No strong architecture signals — defaulting to
'landing'", in all three fetch sites, silently, with a fallback that looked
like a successful low-signal result.

`safe_opener` now takes the context itself, so the mistake has nowhere to
live.

**This is why regression T1 (SSRF) was inconclusive.** The guard works —
`169.254.169.254` and `127.0.0.1:5432` are refused before any socket, and
nothing leaks. But a blocked host and a working public site produced
byte-identical output, because the fetcher failed for both. **T1 needs
re-running now that output can discriminate.**

### A heuristic that had never once run

```js
document.querySelectorAll(
    'button:not([type=hidden]), .w-button, a.button, '
    '[role="button"], input[type=submit]');
```

Two adjacent string literals with no `+` — Python's implicit concatenation,
written inside a JavaScript string. The `ctas` walkthrough heuristic threw on
`page.evaluate` for every page of every run and never produced a finding; its
only ever output was `BUG-004`, reporting its own failure **against the
customer's site**. `node --check` reproduces that bug's exact message.

### Why 4716 passing tests saw neither

The guards measured the wrong thing. `tests/test_ssrf_redirects.py` called
`safe_opener().open(url, timeout=5)` — a shape no production caller uses —
and asserted the literal string `"safe_opener()"`, which broken code
satisfies. `tests/test_crawl_error_surfaced.py` mocks `crawl_site` outright
and accepts `"background" in body.lower()` as proof of a banner, a word the
async page contains regardless.

---

## Three reported findings, one root cause

```
TFG-04  automated runs create no DB run row
   │      routes/execution.py returned before the only dispatch-path
   │      start_execution_run
   ▼
T5      the gate counts open rows → counts zero, forever → admits every run
   ▼
TFG-03  two Chromiums under one 380 MB ceiling → OOM → worker restart
   ▼
TFG-02  Render's edge 503s new connections   ← reported as "export is broken"
TFG-18  in-flight static requests die with the worker
```

The file's own comment predicted it: *"two at once are OOM-killed rather than
queued."* Rows are now opened per `env_type` at dispatch and **adopted** at
import rather than opened twice. Left `running` on a crash deliberately —
`run_limits.split_by_age` ignores anything past the staleness window, so a
dead run stops blocking the cap by itself.

The route test this needed was missing for an instructive reason: every
existing route test in `test_run_limits.py` pre-seeded the blocking run by
calling `start_execution_run` directly — establishing by hand the
precondition the product was failing to establish. The gate was correct and
covered; nothing exercised the path that never fed it.

---

## The ISTQB corpus

`engine/istqb_corpus.json` shipped **2 330 chunks from a commercial
textbook** — 404 distinct pages of a 409-page book, ~136 000 words — served
verbatim with page citations, with **no excerpt cap** and **no refusal path**
ahead of retrieval. So a prompt-injection attempt was answered with a page of
the book. Nothing leaked only because with no API key there is no system
prompt in the path to leak; the resistance was a coincidence, not a control.
The builder also strips `© YYYY` lines as "noise".

Now **syllabus-only: 2 826 → 449 chunks**, gated behind
`ISTQB_BOOK_CORPUS=1` in the **builder** — the exposure is the committed
artefact, not the runtime read, so a load-time flag would not have helped.
Plus a 60-word excerpt cap and an explicit injection refusal (precision
checked against the six questions the Guide promises Tedgie answers).

Dropping the book exposed a defect it had been masking: **116 of the 496
syllabus chunks were table-of-contents rows and running headers.** Real prose
had always outscored them; with the book gone they started winning —
"What is risk-based testing?" answered with a McGraw Hill bibliography line.
Fixed via the builder's own `clean_chunk_text` so artefact and builder cannot
drift.

⚠ **Not addressed here: the book remains in git history.** Removal needs a
history rewrite — a separate decision.

---

## Six findings that do not survive the code

| Finding | Reported | Actual |
|---|---|---|
| **TFG-02** Critical | Every export button 503s | **Refuted as an app defect.** No export path can emit 503; the only 503s in the app are `/healthz`/`/readyz`. `Sec-Fetch-Mode` appears nowhere in the repo. The buttons are `<a href>`, so Chrome downloads on a *fresh connection* while the XHR reused the warm socket — Render's router answers the new one. Explains the size-independence too. |
| **TFG-01** Critical | Session-vs-DB hydration hides real data | **Hypothesis refuted.** `/bug-reports` is already DB-first; a cold start renders all 24. Two different real defects underneath (silent session fallback on a DB read failure; a zero-bug project offering no way to file the first bug, with the *test-case* empty-state string). |
| **TFG-21** | Switch does not re-render | **Refuted server-side.** Real defect underneath: the "Restored N test cases" flash fires on *every* GET under `WORKSPACE_DB_FIRST`. |
| **TFG-23** | Page restores a deep scroll offset | **Refuted.** No scroll-restoration code exists; that is default browser behaviour. |
| **TFG-12** | No cross-run dedup | **Partly refuted.** Dedup exists and is project-wide. Real gap: TC-driven and early-exit bugs never compute a signature. |
| **TFG-10** | Severity/priority set by rule sets that never reconcile | **Diagnosis wrong, defect real.** They agree for 17 of 18 classes; the cause is `_area_weight` collapsing to 1, which maps `Critical→Low`. |

One retraction went too far: T7's XLSX path **is** sanitized
(`test_xlsx_cell_value_starts_with_apostrophe` passes). T7 is a clean pass on
both paths.

---

## Also fixed

- Bulk triage wrote **NULL** over severity/priority: the value field was
  re-disabled on load while the browser restored the action select, so Apply
  posted `action=severity` with no value. Audit log read `severity -> None`
  while the card still showed `Minor`, because `workspace.py` renders
  `row.severity or "Minor"`. Server now refuses a value-taking action with no
  value.
- Inline choice editors offered **one option** — the macro never forwarded
  `choices`, so `inline-edit.js` built the `<select>` from an empty list.
- An id-less bug was **displayed** under `BUG-{row.id}` while the mint counted
  only *stored* ids, so manual creation reissued `BUG-001` and the unique
  index could not object (NULL is unconstrained). Now backfilled, which also
  populates the run-results Bug ID column.
- Run-results **Summary column was empty for every row**: the template
  compares `r.item_type`, the DB reader emitted only `kind`, and Jinja makes
  `Undefined == 'test_case'` false — silently.
- **Traceability matrix** had nothing to join on: `generate_test_cases` never
  set `user_story_id`, though `_make_tc` in the same module always has.
- Project-switch flash counted session keys `mirror_pack` never writes under
  `WORKSPACE_DB_FIRST` → `(0 TC · 0 CL)` beside a picker reading 62.
- `/metrics` answered **401 with 403's wording** and no `WWW-Authenticate`
  (RFC 9110). Now 403 — `X-Ops-Token` is not an HTTP auth scheme, so there is
  no scheme to name.
- Tedgie cited a `/healthz` field (`browser_pool`) that has never existed, and
  grouped `[Component]`-titled bugs under "(unspecified)".
- Italics rendered as literal asterisks (`mdToHtml` had no italic pass at all).
- `"— it feeds Brooks\"` lost its law.
- Duplicate `@app.route("/export/<fmt>")` decorator.

---

## One test changed for a real reason

`test_instructions_not_in_checklist` posts a **live URL**, so a working
crawler pushed generation past `SYNC_GEN_BUDGET_S` and the route fell back to
async with a 302 — correct behaviour, previously unreachable. The test had
been asserting fast-because-broken behaviour; now stubbed at `_fetch_page`
(78 s → 13 s for the file).

The consequence is real, though: **URL-driven generation will now routinely
land on the async path**, where the drains discard `crawl_errors`. Surfacing
those matters more than it did before this change.

---

## Two defects this PR caused

Both were introduced by `17df1f7` and fixed in follow-up commits. Neither
was found by a test, and that is the part worth a reviewer's attention.

### `1da5ed5` — the backfilled id could collide, and the retry could not help

`17df1f7` gave an id-less bug the public id it was already *displayed*
under, closing the duplicate-`BUG-001` defect. But two mints share that
namespace: `generate_bug_id` counts a project's bugs, while the backfill
uses the row's sequence id. A project holding `BUG-001`..`BUG-022` with row
ids 1..22 is one deletion away from the sequence handing out a number some
row already stores — and `save_bug`'s retry re-mints only when the *caller*
supplied an id, so the `IntegrityError` escaped:

```
RAISED: IntegrityError UNIQUE constraint failed:
        bug_report.project_id, bug_report.external_id
```

That insert **used to succeed** (storing NULL), so the first commit was
strictly worse than the defect it fixed. Now: read the ids the project
already uses, step forward to a free one, and do the UPDATE inside a
savepoint — losing a race to a concurrent writer leaves the row NULL, the
pre-backfill behaviour, rather than failing the write.

Found by re-reading the backfill against `save_bug`'s retry while waiting
on CI. 4775 local tests and three green CI jobs passed over it, because the
test written alongside the backfill covered only the clean case.

### `b80efbf` — a fake worker never closes the run it opened

`17df1f7` made dispatch open an `ExecutionRun` row *before* spawning the
worker, so the register can see a run in flight and the gate has something
to count. A real worker closes its row on results import; the
`patched_subprocess` fixture's `_FakePopen` cannot, so every test using it
leaves an open browser run behind.

Under `ORG_MODE=1` that is fatal to the *next* test: `_run_limit_scope`
resolves through `current_org_id()`, so the whole organisation's projects
are in scope, the abandoned run trips the limit, and the route flashes and
redirects instead of dispatching. Six tests failed as `KeyError: 'argv'` —
a key never captured because `Popen` was never reached.

Invisible locally, because org mode is off by default. **This is precisely
what the auth-on matrix leg exists to catch**, and it caught it. The cap is
now lifted in that fixture rather than worked around per test: those tests
cover run-mode parsing and TC projection, and `tests/test_run_limits.py`
owns the cap including the route-level refusal — so this removes an
interaction, not coverage.

### What the pattern says

Five defects in this PR — the dead `context=`, the CTA `+`, the id
collision, the abandoned run, and the empty Summary column — were all
invisible to a large green suite, because each guard asserted the shape its
author had in mind rather than the one that breaks. Two were caught by
reading, one by a matrix leg, none by the tests that nominally covered the
code. The new guards are written against the failure, not the intent: the
production call shape, the parsed JS, the collision, the dispatch.

---

## Next, and not in this PR

1. **26 inline `onclick=` handlers across `templates/` are dead** under the
   nonce CSP — a nonce does not whitelist inline event-handler attributes.
   Includes the Traceability tab the tester reported as inert and the category
   filter bar. Fix pattern exists at `static/js/test-execution.js:250`; the
   existing guard test only scans `_inline_edit.html`.
2. Surface `crawl_errors` through the four async drains, with distinct
   outcomes for *blocked host* / *fetched but empty* / *fetch failed*, so the
   SSRF regression can gate a release.
3. A pre-launch memory admission check, and reconcile the two concurrency
   caps — `MAX_CONCURRENT_RUNS` (default 3) and
   `TESTFORTGE_MAX_CONCURRENT_RUNS` (default 1) are different variables with
   different defaults, and `config.py` claims the first is authoritative.
4. Run artefacts to object storage — nothing currently `put`s them, so every
   screenshot link 404s after a restart or a purge.
5. ⚠ `qa_persona.py:991` **deletes every SQL-injection case whenever a URL is
   in scope**, so the security case the tester praised silently vanishes on
   the URL path. Its second clause compares a string to a one-element list
   and is always true.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
