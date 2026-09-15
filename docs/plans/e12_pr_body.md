# E12 — Execute repair

Reported from staging on 2026-09-15:

> There is a problem with generating automated tests (BDD). There is no way
> to edit the generated BDD test cases. Look at the whole Execute section —
> the logic is very confusing and the functionality does not work fully.
> I don't understand why Test Execution has an "Automated — built-in
> engine" run mode when there is a separate Automation module. No test run
> starts at all, neither by test case nor by checklist. It never even got
> as far as bug reports.

All of it is one system, and the answer to the architecture question is the
first defect: **there were three names for two engines, and the one the
operator reaches first ran nothing at all.**

22 defects, each reproduced against the running product before it was
fixed and each pinned by a test. The full measurement is in
[`docs/plans/e12_execute_repair.md`](docs/plans/e12_execute_repair.md).

## Why no run ever started

Four omissions in a chain, none of which raised anything.

**1. "Automated" selected your items and ran none of them.** `tc_driven` and
`walkthrough` both resolved to `mode="live"` unless `LEGACY_EXECUTOR=1`,
which no deployment sets. That engine is a crawler: it executes a case only
where `walkthrough_tc_match.match_tcs_for_url` binds one to a URL it landed
on, and that function never selects a case whose `trigger` is `"manual"` —
the default for every case the generator and the editor create.

```python
>>> match_tcs_for_url([{"id": "SC4_003", "trigger": "manual",
...                     "url_pattern": "https://testfort.com/awards"}],
...                   "https://testfort.com/awards")
[]
```

With no walkthrough block to read it also ran with `max_pages=50` and an
eight-minute budget: fifty pages of Chromium on a 512 MB instance, to run
zero test cases.

**2. A run could not end.** `engine/runner_worker.py` never called
`finish_execution_run`. The only writer of `finished_at` was
`GET /test-execution/results/<id>` — and it could not adopt the row anyway,
because the dispatcher stores the ids at `config_payload["db_run_ids"]`, the
import reads them from `payload["config_echo"]`, and the worker assembled
that echo key by key and never copied this one. So a **successful** run
opened a second row, closed the second, and left the first running for ever.
The inversion is exact: the crash-salvage path passes the raw config, so a
*crashed* run closed its row correctly and a successful one did not. Each
one then held the one-browser-run cap for thirty minutes, and the lock
re-armed on the next attempt.

**3. The gate refused runs it was not about.** It was evaluated three hundred
lines before `wants_automation`. A Test-Cases or Checklist run with no URL
never launches Chromium — it falls through to the deterministic simulator —
and was refused by a message about browser memory. That is "neither by test
case nor by checklist".

**4. And a run that got through filed nothing.**
`reconcile_with_automation` calls a runner failure genuine only when the
asset bucket carries a `failure_step`, and `_build_automation_assets`
recorded one **only for a step whose failure screenshot existed on disk as a
non-empty file**. A step Playwright had just watched fail, without a
surviving picture, became *"Automation could not execute this item —
recorded as Blocked (not a product defect)"*. Measured before:
`failed: 0, cannot_execute: 1, bugs: 0`. After: `failed: 1,
cannot_execute: 0`, `BUG-001` carrying the browser's own sentence.

## The architecture question, answered

| | What it does | Where it runs |
|---|---|---|
| Test Execution → **Automated** | runs the items **you selected**, in order, no crawl | here, in a detached process |
| Test Execution → **QA walkthrough** | crawls from the Base URL and raises findings of its own | here |
| **Automation QA** | **runs nothing** — writes a TypeScript + Playwright suite and ingests Allure results | your machine, or CI |

`tc_driven` gets its own dispatch back. The radio labels now say what each
one does, and the Automation QA page opens with a block answering the
question directly instead of leaving it to be inferred from two similar
names.

## The rest

* **Cancel.** `POST /test-execution/runs/<id>/cancel` — there was no way to
  end a run at all. It frees the concurrency slot; it cannot kill the
  subprocess (the dispatcher discards the handle) and the confirm text says
  so. Scoped through `_authorise` like every other run route, and tested
  under production CSRF in both directions.
* **The register stops being a dead end.** Results link, P/F/B counts, a
  `simulated` badge, the mode read as the operator chose it rather than as
  `live`, and a tester finally sees their own automated runs — the "assigned
  to me" scope filtered on `assignee_id`, which only the manual walk writes.
* **The worker is spawned where it can import itself.** All three spawn
  sites passed `dirname(STORAGE_ROOT)` as the cwd, which is the application
  root only while artefacts sit inside the checkout. Point `STORAGE_ROOT` at
  a volume — the one thing it exists for — and every dispatch died with
  `No module named 'engine'` while the POST flashed "✓ dispatched".
* **`queued` gets an upper bound**, and `terminated` is handled in both
  pollers. A worker killed at exec painted "● running" for ever.
* **A simulated verdict says so.** A URL-less run was filing invented Major
  defects — *"SQL metacharacters in search are not escaped"* against a site
  no browser had opened — with a reporter and steps to reproduce. Every
  Failed item still carries a bug id, as asked; the bug now carries
  `[simulated]` in its title and a `source:simulated` label.
* **The BDD view is editable.** `TestCase.gherkin` has been in the schema
  since its migration and `ensure_gherkin` has always preferred it; the field
  was simply absent from the editor's allowlist, so the hint "edit the case,
  not this" described an impossibility. The `.feature` export now reads the
  same rule — it derived unconditionally, so an edit would have been visible
  on the page and silently absent from the download.
* **Generation says why it stopped.** The poll loop interpreted one status
  code; a **401 `session_expired`** counted as an unstable server, which is
  the modal in the report. "retrying directly" described a fallback removed
  long ago. And the async worker — the path the UI takes — asked for no
  crawl diagnostics and returned none.
* **The two run-limit dials are declared in the blueprint.** They were the
  documented escape hatch from this exact lockout, and a Manual Sync deletes
  what the blueprint does not declare.

## Verification

```
5660 passed, 56 skipped, 49 warnings in 623.21s
```

Baseline at the start of the work: `5594 passed, 56 skipped`. **+66 tests,
no regressions.**

Seven test files carry the repair. The one that matters most is
`tests/test_execute_end_to_end.py`: every test that touched this path used
to stop at a boundary — the route tests faked `Popen`, the worker tests ran
the worker, the results tests were handed a payload written by hand — so all
four defects above lived *between* two things the suite already checked
separately, and were individually fatal while it stayed green. That file
drives `POST → worker → closed row → the next run admitted → import adopts
rather than duplicates → a watched failure on the bug board with its run
link and the browser's own words`.

## Two things for the owner

1. **The BDD editor needs `EDITORS_ENABLED` + `WORKSPACE_DB_FIRST`.** The
   blueprint sets both to `1` on staging and `0` on production, so the fix
   is visible where it was reported and will not appear on production until
   those are turned on. Same decision that gates editing a test case's
   summary today.
2. **Deliberately not done**, and named in the plan rather than left to be
   discovered: the `LEGACY_EXECUTOR` branches and the `WalkthroughRunner`
   class are still present though double-gated to unreachable; the two
   near-identical 250-line post-processing blocks in `routes/execution.py`
   still have to be kept in step by hand (the Stage 7 Phase B cut);
   `/test-execution/auto-run` and the MCP `trigger_test_execution` tool still
   open no run rows, so their runs are invisible in Runs and uncancellable.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
