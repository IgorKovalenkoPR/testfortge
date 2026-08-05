# E5.1 — Execute audit

Measured 2026-08-05 against the live routes, not read off the code. Every
finding below was reproduced with a real project, a real run and a real
HTTP request; the repro is a test in
[`tests/test_execute_isolation.py`](../../tests/test_execute_isolation.py)
rather than prose, so it cannot rot away from the code.

The property they share is what makes them worth the effort: **the wrong
behaviour looked exactly like the right one on screen.** None of them
raised, logged or rendered anything unusual.

## Findings

| # | Finding | Severity | Measured | Status |
|---|---|---|---|---|
| D1 | A run in project A renders project B's content | Critical | A's summary absent, B's shown | fixed |
| D2 | One verdict closes two items | Major | `done=2 of 2, finished=True` after one POST | fixed |
| D3 | Any caller who knows a run id can write into it | Critical | empty session: GET 200, POST wrote a row | fixed |
| D4 | An interrupted walk cannot be found again | Major | no `list_open_runs`, no reference in `execution.py` | fixed |
| D5 | An item deleted mid-walk renders as a blank card | Minor | placeholder unit-tested, invisible on screen | fixed |
| D6 | An empty item renders as a blank card | Minor | seen on a real walk in the browser | fixed |

### D1 — the session's project decided the run's content

`routes/execution_manual._pack()` read the *session* pack first and the
run's project only as a fallback. The session holds whatever project the
browser currently has active, so a tester who switched projects and came
back to an open run walked the other project's content under this run's
item ids.

Not an edge case: item ids are per-project sequences, so `TC_001` exists in
every project and the ids collide **by construction**. The verdicts went to
the run's own project, against text the tester never saw.

Fixed by `_run_pack(run)`, which reads the run's project from the database
and never consults the session. The pre-E3 session fallback survives only
when the run's project *is* the active one, so it can no longer introduce
another project's content.

### D2 — results were keyed on the item id alone

The walk flattens test cases and checklist items into one queue, and the
two id spaces are separate sequences — a test case `X_001` and a checklist
item `X_001` are different items sharing a label. `compute_progress`,
`verdicts_by_item` and the verdict route all keyed on the id, so one
verdict marked both done: **a two-item run reported "2 of 2, finished"
after a single click**, with half of it never looked at. The pass rate, the
run stats and the dashboard all inherit that number.

Fixed by keying on `(kind, external_id)` throughout — `QueueItem.key`, the
hidden `kind` field in the verdict form, and a `case_kind` argument on
`db.update_case_result` so a correction lands on the right row.

### D3 — no ownership and no scope

`manual_run_page` and `manual_run_verdict` checked nothing. A session with
no project at all returned 200 for any run id, and a POSTed verdict was
written.

Fixed in `_authorise()`, and the resolution is not simply "refuse". Two
properties were in tension and both are load-bearing: the walk must survive
a lost session — that is why the cursor lives in the database — and a run
must belong to its project. The rule:

* **a different project active → 404**, read or write. That is the
  accidental case and the one that corrupts data;
* **no project active → adopt it on a read.** Following a run link selects
  the run's project, which is what a hand-off to a colleague on another
  machine needs. This grants nothing: with authentication off the project
  picker is already open to every session, so refusing would be theatre at
  the price of a documented workflow;
* **no project active → refuse a write.** A verdict is what damages data,
  and the hand-off flow loads the page first, which adopts;
* **auth on → the assignee or an admin.** The only real per-person
  boundary. Without authentication there is no identity to enforce one
  against, and E5.4's "two testers in one project do not see each other's
  verdicts" is only true in that mode — worth saying plainly rather than
  claiming the guarantee holds everywhere.

### D4 — resumable but unfindable

The state to resume a walk had been in the database since the walk was
built, and nothing listed it: an interrupted run was reachable only from
browser history. Resumable and findable are different properties and only
the first one existed.

Fixed with `db.list_open_runs(project_id, mode=…)`, an "Unfinished manual
runs" card above the run configuration on the Execute page, and
`GET /test-execution/manual/<id>/resume`, which lands on the first item
without a verdict so the template does not have to duplicate the cursor
derivation.

### D5, D6 — cards with nothing to judge

Both found by opening a real walk in a browser, and neither was visible in
the code or in the existing tests.

An item **deleted from the pack** mid-walk stays in the queue as a
placeholder — deliberately, because shortening the run would overstate
coverage — but rendered as an ordinary item it is a blank card with five
verdict buttons. The placeholder had a unit test and no presence on screen.

An **empty item** does the same. That one is common: the editors' "add"
button writes a row with empty fields for the author to fill in, and a walk
started before anyone fills it shows an id and nothing else.

Fixed as two distinct notices, because the actions differ — a deleted item
should be Skipped, an empty one should be written. `QueueItem.missing` and
`QueueItem.empty` carry the distinction rather than the template
string-matching a sentinel summary.

## What the audit did not cover

Named so the gaps are decisions rather than oversights:

* **Playwright execution** — E5.2′ moves it to GitHub Actions, so auditing
  the in-process path would measure something being replaced.
* **Concurrent writes by two testers into one run.** The verdict path is
  last-write-wins by design and per-tester attribution needs
  authentication; with `AUTH_ENABLED=0` there is nothing to attribute to.
  E5.4's isolation is therefore project-level in the default deployment,
  and per-assignee only when auth is on.
* **The live and walkthrough modes**, which share `ExecutionRun` but not
  the manual queue. `list_open_runs` takes a `mode` filter for that reason;
  their own resume affordances are not built.
