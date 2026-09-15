# E12 — Execute repair

Measured 2026-09-15 against the running product, not read off the code.
Every finding below was reproduced — through the Flask test client, the
worker's own subprocess, or a direct call to the function under suspicion —
and each one is pinned by a test rather than by prose, so it cannot rot
away from the code.

The property they share is the one that made them expensive: **nothing
raised.** The page said "✓ dispatched". The register said "in progress".
The board showed Major defects with steps to reproduce. The suite was
green — 5 594 passing on a product where no run had ever completed.

## The operator's report

> There is a problem with generating automated tests (BDD). There is no way
> to edit the generated BDD test cases. Look at the whole Execute section —
> the logic is very confusing and the functionality does not work fully.
> I don't understand why Test Execution has an "Automated — built-in
> engine" run mode when there is a separate Automation module. No test run
> starts at all, neither by test case nor by checklist. It never even got
> as far as bug reports.

All four are the same root system. The answer to "why are there two
automated modes" is that there were in fact **three names for two engines,
and the one the operator reached first ran nothing at all.**

## Findings

| # | Finding | Severity | How it presented | Status |
|---|---|---|---|---|
| D1 | The "Automated" mode never runs the items you select | Critical | run completes, zero cases executed | fixed |
| D2 | The worker never tells the database it finished | Critical | "a browser run is already in progress" for a run that ended | fixed |
| D3 | The dispatcher's run ids are dropped from `config_echo` | Critical | two rows per run, one open for ever | fixed |
| D4 | The fair-use gate refuses runs that launch no browser | Critical | a checklist run with no URL refused for browser memory | fixed |
| D5 | There is no way to cancel a run | Critical | wait 30 min, or run SQL | fixed |
| D6 | The worker is spawned where `engine` cannot be imported | High | every dispatch dies at exec, success flash on screen | fixed |
| D7 | `queued` has no upper bound | High | "● running" for ever on a process that never existed | fixed |
| D8 | A simulated verdict becomes an unmarked bug report | High | invented Major defects on the board | fixed |
| D9 | The cap is per-project, so switching project bypasses it | Medium | two Chromiums under one 512 MB ceiling | fixed |
| D10 | A manual-walk bug gets no public id and no run | Medium | blank ID cell, reachable from no filter | fixed |
| D11 | The Runs register is a dead end for every automated run | Medium | no results link, no counts, raw `live` as the mode | fixed |
| D12 | A tester cannot see their own automated runs | Medium | "Nothing is assigned to you" after starting a run | fixed |
| D13 | The BDD view has no writer | High | "Edit the case, not this" described an impossibility | fixed |
| D14 | The `.feature` export ignores a hand-edited scenario | High | edit visible on screen, absent from the download | fixed |
| D15 | A mid-run session expiry reports "Server returned errors" | Medium | the generation modal in the operator's screenshot | fixed |
| D16 | "retrying directly" — nothing retries | Medium | the operator waited for a retry that was removed | fixed |
| D17 | `terminated` is unhandled in both pollers | Medium | the most common free-tier ending spins for ever | fixed |
| D18 | `/automation/run` launches Chromium in the web worker | Medium | UI-less, ungated, uncounted | fixed |
| D19 | The two run-limit dials are absent from the blueprint | Low | the documented escape hatch a Manual Sync deletes | fixed |
| D20 | The refusal timestamp is UTC, unlabelled | Low | a three-hour-dead run reads as one that just started | fixed |
| D21 | A failure the browser watched is filed as "not a product defect" | Critical | the run completes, the board stays empty | fixed |
| D22 | The async generation path reports no crawl failure | Medium | a thinner pack, nothing on screen to explain it | fixed |

### D1 — "Automated" selected your items and ran none of them

`templates/test_execution.html` offers *"Automated — built-in engine:
Playwright drives the items you select below."* That posts
`run_mode=tc_driven`, and `engine/runner_worker.py` resolved it — along with
`walkthrough` — to `mode="live"` unless `LEGACY_EXECUTOR=1`, which no
deployment sets. Both radio buttons were the same engine.

That engine is a crawler. `LiveExecutor` walks pages and executes a test
case only where `walkthrough_tc_match.match_tcs_for_url` binds one to the
URL it landed on, and that function never selects a case whose `trigger` is
`"manual"` — the default for every case the generator and the editor create:

```python
>>> match_tcs_for_url([{"id": "SC4_003", "trigger": "manual",
...                     "url_pattern": "https://testfort.com/awards"}],
...                   "https://testfort.com/awards")
[]
```

So the selection was a candidate pool the crawler was free to ignore, and it
ignored all of it. With no walkthrough block to read, the same run also took
`max_pages=50` and an eight-minute budget from the defaults — fifty pages of
Chromium on a 512 MB instance, to run zero test cases. That is D2's
lock-holder as well as this defect.

Fixed by giving `tc_driven` back its own dispatch: one script per selected
item, executed in order, no crawl. `walkthrough` keeps the exploratory
executor, which is the mode that wants one. The labels now say which is
which, and say that the Automation QA module is neither.

### D2, D3 — a run could not end

`engine/runner_worker.py` contained no reference to `finish_execution_run`.
It wrote `result.json` and `done.flag` and exited. The only writer of
`finished_at` for an automated run was `GET /test-execution/results/<id>`,
reached by an auto-redirect in `static/js/test-execution.js` that fires
**only** on `status === 'done'`, in the tab that dispatched the run, while
`session["active_automation_run"]` survives.

And it could not adopt the row even then. The dispatcher stores the ids it
opened at `config_payload["db_run_ids"]`; the import reads them from
`payload["config_echo"]`; and the worker assembles `config_echo` by hand,
key by key, and never copied that one. So a **successful** run opened a
second row, closed the second, and left the first running for ever.

The inversion is exact: the crash-salvage path passes the raw config file as
the echo, so `db_run_ids` *is* present there. **A crashed run closed its row
correctly. A successful one did not.**

`engine/run_limits` then counted that row against a cap of one for thirty
minutes — and the next attempt, if it got through, opened another. Every
successful run locked the team out, and the lock re-armed itself. From the
operator's chair that is indistinguishable from "runs never start".

Fixed in four places: the worker closes its own rows on success, failure and
SIGTERM; `config_echo` carries the ids; every early return in the import
closes what it abandoned; and `close_abandoned_runs` turns the staleness
window from a read filter into a repair, so the register stops advertising a
run that has been dead for hours.

### D4 — the refusal was about a resource the run did not use

The gate ran at the top of the POST handler. `base_url` is not read for
another hundred lines, and `wants_automation` — the flag that actually
decides whether Chromium is launched — not for three hundred. A Test-Cases
or Checklist run with no URL, or on an iOS/Android environment, never opens
a browser: it falls through to the deterministic simulator. It was refused
anyway, by a message about browser memory.

This is the other half of "no test run starts at all, neither by test case
nor by checklist". The runs being refused were the kind that cost nothing,
and the thing holding the slot was a browser run that had already finished.

### D5 — there was no way out

No route matching cancel, abort, stop or kill. No control on the Runs page
for anything but a manual walk. The dispatcher discards the subprocess
handle, so there is no pid to signal. The operator's entire remedy was to
wait out the staleness window, change an environment variable that the
blueprint does not declare (D19), or run SQL.

`POST /test-execution/runs/<id>/cancel` closes the row and frees the slot.
It does not kill the subprocess and says so.

### D6 — the worker was spawned where it could not import itself

All three spawn sites passed `dirname(STORAGE_ROOT)` as the worker's cwd.
That equals the application root only while artefacts sit inside the
checkout — the one arrangement `STORAGE_ROOT` exists to let a deployment
change. Point it at a mounted volume and every dispatch dies at exec:

```
python.exe: Error while finding module specification for
'engine.runner_worker' (ModuleNotFoundError: No module named 'engine')
```

…while the POST flashes "✓ Playwright pass dispatched". Docker is safe
today only because `WORKDIR /app` happens to equal `dirname(/app/storage)`.
The test suite is *not*: `tests/conftest.py` puts `STORAGE_ROOT` under a
per-process temp root, so the suite has always run in the broken
configuration and could never have exercised a dispatch end to end.

### D8 — the simulator was filing defects about a site nobody opened

A run with no Base URL executes nothing. `engine/qa_testers._compute_status`
assigns Passed/Failed from a hash of the summary against an invented fail
probability, and `execute_items` filed a bug report for each Failed — with a
severity, a priority, a reporter name and a steps-to-reproduce block.
Twenty test cases produced "13 P / 7 F, 65% pass rate" and seven Major
defects, one of them *"SQL metacharacters in search are not escaped"*.
Nothing on the board, in the register or in the CSV said any of it was
invented, and re-running the pack filed them again.

The contract that every Failed item carries a bug id stands — it was asked
for, and withholding the id would break the run summary. What could not
stand is a bug the board cannot tell from one somebody saw. Simulated
verdicts now carry `[simulated]` in the comment and in the bug title, the
bug carries a `source:simulated` label, and the Runs register badges a run
whose every verdict came from the simulator.

### D13, D14 — the BDD view was designed, migrated, read, and never written

`TestCase.gherkin` has been a column since its migration.
`engine.gherkin.ensure_gherkin` has always preferred it over the derived
text and says so in its docstring: *"the column holds only text an operator
hand-edited… Hand-edited text always wins."* The page rendered it in a
`<pre>` under the hint *"Derived from the columns above. Edit the case, not
this"*, which reads as a policy and was in fact a description of an
impossibility — the field was absent from `engine/editable.py`'s allowlist,
so the PATCH endpoint answered 400 and no route in the product accepted an
edited scenario.

The second half mattered as much: `scenario_from_test_case` derived every
scenario from the manual columns and never consulted the column, so even
with a writer an edit would have been visible on the page and silently
absent from the `.feature` download. That rule now lives in
`scenario_from_test_case` itself, so every route that renders a `.feature`
inherits it instead of having to remember.

`tc_format` is editable too, because the export's own 409 told the operator
to *"switch individual cases to BDD in the editor"* and there was no such
control.

### D15, D16 — the generation modal in the screenshot

The poll loop interpreted exactly one status code, 404. Everything else
incremented a counter, and at ten in a row printed a fixed sentence with the
server's own explanation discarded. `engine/session_timeout` answers a JSON
caller with **401 `session_expired`** and a message saying what happened;
the submit path knew how to handle that and the poll loop did not. So a
session that ended mid-run — 85 seconds in, which is what the operator's
screenshot shows — reported "Server returned errors".

And the sentence was untrue in its own right. The synchronous fallback it
describes was deliberately removed (it tied up the single worker for the
whole LLM run and reliably 502'd); the string "retrying directly" stayed.
The operator read it and waited for a retry that nothing was performing.

### D21 — the last joint: a watched failure that files nothing

The chain was still broken after the run itself was fixed, and this is the
one that answers *"it never even got as far as bug reports"* literally.

`run_results.reconcile_with_automation` distinguishes a genuine product
failure from a cannot-execute condition — a distinction added by the
2026-07-15 investigation into "0 pass / all fail / one bug per item", and a
correct one. Its test is `ev.get("failure_step") or
ev.get("failure_screenshots")`. And `runner_worker._build_automation_assets`
recorded a `failure_step` **only for a step whose failure screenshot named a
file that existed on disk with a non-zero size.**

So a step Playwright had just watched fail, whose screenshot did not
survive — the capture failed, which `live_executor` logs at WARNING because
it happens; retention swept the artefacts; the ephemeral disk was recycled
between the worker and the import — produced no `failure_step`, and
reconciliation wrote:

> Automation could not execute this item — recorded as Blocked (not a
> product defect).

…dropped the simulator's bug for that item, and left the board empty after a
run that had found something. Measured end to end: `failed: 0,
cannot_execute: 1, bugs: 0` for a run whose report carried an explicit
`status="failed"` step with the comment *"Timed out waiting for the
confirmation panel"*.

A picture is corroboration. It is not what makes a failure real, and it is
the part most likely to be missing. `failure_step` is now the first step
whose own status is `failed`, with the screenshot attached when there is
one. The same measurement afterwards: `failed: 1, cannot_execute: 0`,
`BUG-001` linked to the run, its actual result carrying the browser's own
sentence.

The cannot-execute guard is untouched and tested in both directions: a
script that ran no step, and one whose runner reported `blocked` without a
failed step, are still Blocked with no bug.

## One thing the operator has to decide

The BDD editor only exists where the inline editors do, and they are gated
on `EDITORS_ENABLED`, which `engine/features.py` makes conditional on
`WORKSPACE_DB_FIRST` (ADR 0001: editing a Flask session edits a private copy
of shared team data). The blueprint sets both to `"1"` on **staging** and
`"0"` on **production**.

So the fix is visible where it was reported — staging — and will not appear
on production until those two flags are turned on there. That is a decision
about the workspace, not about this repair, and it is the same decision that
gates editing a test case's summary today.

## What is deliberately *not* done

* **`LEGACY_EXECUTOR` and `WalkthroughRunner` are still there.** The class is
  double-gated to unreachable and the comment says "kept for one release";
  that release has passed. Deleting 1 500 lines belongs in its own change
  with its own test run, not in a repair the operator is waiting on.
* **`routes/execution.py` still holds two near-identical 250-line
  post-processing blocks** (the in-process per-env loop and the import). They
  must be kept in step by hand. Stage 7 Phase B named this cut and deferred
  it; it is still the right cut and still deferred.
* **`/test-execution/auto-run` writes no database rows at all**, so its runs
  are invisible everywhere. Either wire it up or delete it — but decide,
  rather than leaving a third dispatch path nobody counts.
* **The MCP `trigger_test_execution` tool opens no run row and consults no
  cap**, so its runs cannot be seen in Runs and cannot be cancelled. It also
  tells clients to "poll via `list_execution_runs`", which will never show
  them.
* **The Automation QA page still has no controls** with an ordinary pack: the
  bundle 409s without BDD cases and ingest 403s without a token. Both
  refusals are correct and both are now explained on the page, but a first
  visit still offers nothing to press.

## Verification

* `tests/test_execute_run_lifecycle.py` — the worker closes its own rows, the
  echo carries them, a cancel is not overwritten by a late worker, the reaper
  closes a stale run and never a manual walk, and `tc_driven` no longer
  reaches the crawler.
* `tests/test_execute_runs_register.py` — cancel frees the slot, is scoped
  like every other run route, refuses to overwrite a finished verdict; the
  register links to results, labels the mode and shows counts.
* `tests/test_execute_dispatch_honesty.py` — the worker is spawned where it
  can import itself, a dispatch that never started is reported failed and its
  row closed, a simulated verdict and its bug say so, a manual-walk bug can
  be cited.
* `tests/test_bdd_is_editable.py` — the field is writable, stored text wins
  on the page *and* in the download, clearing it reverts to deriving.
* `tests/test_execute_end_to_end.py` — the sequence nothing had ever driven:
  POST → worker → closed row → the next run admitted → import adopts rather
  than duplicates → a watched failure on the bug board with its run link and
  the browser's own words. Every defect above lived *between* two things the
  suite already tested separately.
* `tests/test_generation_says_why.py` — a session that ended is not reported
  as a server fault, nothing claims to be retrying, and the async worker
  reports what it could not crawl.
* `tests/test_run_limits.py` — the existing cap tests, plus the case that was
  missing: a run that launches no browser is not refused.
* `tests/conftest.py` gained an autouse fixture that closes leftover open
  runs. With no organisation the cap's scope is the whole instance, which it
  has to be (D9) — and the suite shares one database, so a test that seeded a
  blocking run and did not close it refused the *next* test's dispatch.
