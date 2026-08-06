# E9.1 — Test strategy for the team-platform programme

Written after E1–E7 and E9.9 shipped, not before, and that is deliberate: a
strategy drafted at the start would have been a list of test levels copied
from a textbook. This one is built from what actually went wrong in this
programme, which is a different and shorter list.

## The one finding that shapes everything else

Across E4, E5, E6 and E9 the defects that survived longest shared a single
property:

> **The wrong behaviour looked exactly like the right one.**

Not one of them raised, logged, or rendered anything unusual. Some examples,
all measured:

| Defect | What it looked like |
|---|---|
| A run in project A rendered project B's content | a normal walk |
| One verdict closed two items | "2 of 2, finished" |
| A project vanished from the picker when created | an empty project list |
| `'bug report'` opened the filing form on "where do I see the bug reports?" | a bug form |
| Comparative questions answered as single-term lookups | a confident definition |
| The open-runs card silently absent (`NameError` in a `try`) | a page that renders |

So the strategy's first rule is not about coverage percentages:

**Assert the reported outcome, never the status code alone.** A test that
checks `200` would have passed for every row in that table. The E5.7 journey
tests assert the pass rate, the filed bug, the stats and the page text,
because those are what a person acts on.

## Coverage targets, and what they are for

The programme's targets from §8 stand — ≥85% on new code, 100% branches on
permissions and crypto — with one addition learned the hard way:

**Coverage is a floor for "did anyone exercise this", not evidence of
correctness.** `engine/manual_run.py` was at high coverage while
`compute_progress` keyed on the wrong tuple, because every test used one
item kind. The gap was in the *data*, not in the lines.

So the targets are paired with an obligation: when a module has a
composite key, an ordering, or two id spaces, a test must use **at least two
distinct values of each** — the shared-id case that found that defect is now
`TestAnItemIsIdentifiedByKindAndId`.

**Measured 2026-08-05**, branch coverage, whole suite: **93%** across the
nineteen modules the gate watches, every one of them at or above 85%.
`permissions` 99% (the one partial branch is a `pragma: no cover` defensive
`except`), `auth` 100%, `route_policy` 98%, `bug_workflow` / `public_ids` /
`tc_steps` complete. The floor in CI is set at the measured value rather
than below it: a gate set under what is already true only fires once a
regression has been sitting there for a while.

| Area | Target | Why this level |
|---|---|---|
| `permissions`, `route_policy` | 100% branch | a missed branch is an access-control hole, and the fail-closed table is the only thing standing between a new route and the public |
| `auth`, password/crypto paths | 100% branch | timing, lockout and rotation are each a security property with no visible symptom when broken |
| `editable`, repositories, `db` write paths | ≥ 90% | optimistic locking and provenance; a lost update is silent |
| New engine modules (`run_limits`, `mentoring`, `manual_run`, `dashboard_*`) | ≥ 85% | the programme's stated bar |
| Routes | exercised by a functional test each | the role matrix, with CSRF **on** |
| Templates | asserted through rendered text | six defects in this programme were only visible on the page |

## The levels, and what each one is for here

**Unit** — inside each task, not afterwards. The rule that made this work is
that a unit test may not mock the thing it is testing the behaviour of: the
severity recommender's tests read the pack, `run_limits` tests build real
run rows.

**Integration** — the DB layer against **both** engines. Production is
Postgres, migrations are hand-written SQL, and verifying them only on SQLite
verifies them on the wrong engine. CI already runs a Postgres service for
exactly this.

**Functional (HTTP)** — every module, every role, **CSRF enabled**. The
project has a standing rule from an earlier sprint: a new POST that is
csrf-exempt for a machine caller needs a regression test that flips
`WTF_CSRF_ENABLED=True`, because a form endpoint passes its tests and 400s
in production otherwise.

**Both flag modes** — E9.9's contribution and the strategy's second rule:

> **A test that depends on the mode must name the mode.**

The suite runs with the flags off *and* on. Tests that describe the
flags-off deployment say `auth_off`; those whose subject is the
unauthenticated case say `anon_client`. Without that, 405 tests failed in
the mode the product ships in — and the first green authenticated run
immediately found a real defect.

**E2E** — two axes, deliberately separate. `test_pipeline_e2e.py` walks the
generation chain (markup → checklist → cases → bundle → ingest → bugs);
`test_execute_e2e.py` walks the two execution paths to the number a person
reads. Golden paths with a real browser (E9.5) belong on top of these, not
instead of them.

## Risk matrix

Ordered by what this programme has actually shown, not by intuition.

| Risk | Likelihood | Impact | What holds it | Status |
|---|---|---|---|---|
| A silent wrong answer that looks right | **high** | high | outcome assertions, not status codes; browser-checking every UI change | active practice |
| Flags-on behaviour diverging from flags-off | high | high | E9.9: both legs in CI | ✅ closed |
| Access control regressing on a new route | medium | **critical** | fail-closed `route_policy` + `test_every_route_declares_a_policy` | ✅ closed |
| A migration verified on the wrong engine | medium | high | Postgres service in CI + the guard that the Postgres leg really ran | ✅ closed |
| Two testers corrupting one run | medium | high | E5.4 project scope; per-person only with auth on, stated plainly | 🟡 partial by design |
| OOM from concurrent browser runs | high | medium | E5.5 fair use, one run per org | ✅ closed |
| Chat advice contradicting the generator | medium | medium | E6 packs quote `wording_rules.yaml`; asserted in `test_tedgie_mentoring` | ✅ closed |
| Quality of Tedgie's answers rotting on a prompt edit | high | medium | E6.7 golden-set gate at 100%/100% | ✅ closed |
| Free-tier resource limits | high | medium | E5.2′ moved browsers to Actions; quotas in E0.12 | 🟡 quotas open |
| A test passing for the wrong reason | **medium** | high | see below | active practice |
| Load: 10 concurrent users per org | unknown | medium | E9.7 — not yet measured | ⏳ open |
| Security: OWASP ASVS-lite on auth/RBAC/upload | unknown | **critical** | E9.8 — not yet run | ⏳ open |

## Tests that pass for the wrong reason

This has happened three times in the programme and is worth its own
practice, because it is invisible by construction — the build is green.

1. **A 403 from the gate, not from the rule.** A test patched
   `current_user_id` to a made-up user, who is a member of nothing, so the
   route policy refused before the view ran. The test asserted 403 and
   passed while checking nothing. Fixed by the `as_user` fixture, which
   patches the gate open so only the rule under test can refuse.
2. **A 404 satisfying "does not show project B".** An authorisation check
   made the page 404, which also satisfies the content assertion. The
   content property is now asserted against the pack loader directly, where
   the substitution happened, so it would fail even if the check were
   removed.
3. **A best-effort `except` hiding a `NameError`.** The open-runs lookup was
   wrapped, the feature never worked once, and the page rendered fine.
   Fixed by removing the catch: best-effort around code that has never
   worked hides the bug instead of surviving it.

The practice: when a test could pass for two reasons, split it so each
reason has its own assertion, and prefer asserting at the layer where the
defect would live.

## Zero-flaky policy

No quarantine list, no reruns. A flaky test is triaged as a defect in the
test or in the code, and the two causes this project has actually seen are
both real bugs rather than noise:

* **shared state across tests** — the suite's projects live in one
  organisation, so an open run from one file counted against another
  (`fresh_org` exists for that);
* **shared state across runs** — the scratch database is not always deleted
  on Windows, and `upsert_project` keys on the slug, so a second run
  asserted against the first run's rows. Test data is keyed per run
  (`_RUN = secrets.token_hex(4)`).

Both were found as "flaky locally, green in CI", and both were the code
telling the truth about isolation.

## What this strategy does not cover

Named so the gaps are decisions:

* **Performance beyond a smoke** — E9.7 measures 10 concurrent users per
  organisation; there is no sustained-load or soak testing, and on a free
  tier there would be nowhere to run it.
* **Cross-browser** — Chromium only, in CI and in the generated suites. A
  matrix triples the cost of every run and is a decision for whoever needs
  it.
* **The remaining 7%** — concentrated in `security` (85%), `editable`,
  `dashboard_config` and `dashboard_metrics` (88% each). What is left in
  each is defensive: `except` arms around best-effort DB reads and the
  branches that only fire on a torn-down engine. They are worth covering
  eventually and are not worth mocking a database failure for today.
* **Accessibility of the app's own UI** — the product tests *other* sites
  for accessibility (axe-core in the walkthrough runner); its own pages are
  not audited. That is a real gap, not a deliberate exclusion.
