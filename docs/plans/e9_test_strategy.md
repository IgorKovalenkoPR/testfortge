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

E9.3 added the half a clean database cannot reach:
`test_migration_populated_copy.py` fills a database through the product's
own writers, takes the schema back to its pre-programme shape and boots
again. Three migrations only do anything when rows are already there — the
editing metadata back-fills them, the renumbering exists for them, and the
unique index is creatable on an empty table no matter what. It found two
Postgres-only defects on its first run, both invisible in CI because CI's
Postgres database is created fresh and the ALTERs therefore never fired: a
boolean column given an integer default (Postgres refuses the statement, the
helper logs it, the column never appears) and a JSON model column added as
TEXT (psycopg2 hands back the string `'{}'`).

The other integration axis is transactional rather than structural.
`consume_invite` read `used_at` and then set it — safe on SQLite, which
serialises writers, and not on Postgres READ COMMITTED, where two people
opening one forwarded invitation both read NULL and both join. It claims the
token with one conditional UPDATE now.

**Functional (HTTP)** — every module, every role, **CSRF enabled**. The
project has a standing rule from an earlier sprint: a new POST that is
csrf-exempt for a machine caller needs a regression test that flips
`WTF_CSRF_ENABLED=True`, because a form endpoint passes its tests and 400s
in production otherwise.

Written as a table rather than as one test per endpoint, for the reason
`route_policy` is: sixty-one hand-kept tests are one endpoint away from
being wrong, and read as coverage while they are.
`test_csrf_on_every_post.py` derives the list from the URL map and fails
closed — an endpoint is either refused without a token or named in `EXEMPT`
with the credential that authenticates it instead, and each exemption is
then watched refusing an anonymous caller.

Alongside it, `test_functional_module_matrix.py` asks the question the
access-control files do not: *did the thing happen, and did the page say
so?* Each module is performed as an administrator and checked in the
database and in the returned text, then performed as a plain user and
checked that **nothing changed** — which is the assertion a gate-level 403
cannot make.

**Both flag modes** — E9.9's contribution and the strategy's second rule:

> **A test that depends on the mode must name the mode.**

The suite runs with the flags off *and* on. Tests that describe the
flags-off deployment say `auth_off`; those whose subject is the
unauthenticated case say `anon_client`. Without that, 405 tests failed in
the mode the product ships in — and the first green authenticated run
immediately found a real defect.

**E2E** — three axes, deliberately separate. `test_pipeline_e2e.py` walks the
generation chain (markup → checklist → cases → bundle → ingest → bugs);
`test_execute_e2e.py` walks the two execution paths to the number a person
reads. `test_e2e_golden_paths.py` (E9.5) sits on top of both, not instead of
them: a real Chromium signing in through the form, claiming an invitation,
creating a project, uploading a pack, walking it one verdict at a time and
filing a bug the dashboard then counts — plus the two properties that only
exist with two sessions open at once.

Five journeys rather than one, because one long journey fails at step six
and says nothing about steps one to five. The browser is also the only
client in the suite that reads the CSRF token out of the rendered HTML,
which is what keeps the templates honest.

**Load** — E9.7, `test_load_smoke.py`: ten people in one organisation over
real HTTP against a threaded server, released from a barrier, with a second
barrier immediately before the write so the filings genuinely collide.
Measured 2026-08-06: pages p95 **324 ms** (p50 101 ms), no 5xx, no
`database is locked`, all ten writes landed. The budget is 3000 ms — about
nine times the measured p95, because the same file runs on a shared two-core
runner and a performance gate that fails on somebody else's noisy neighbour
teaches people to rerun the build. Signing in has its own ceiling: ten Argon2
verifications arriving together took 2.9 s at p95, and that is the hash doing
its job rather than the app being slow.

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
| An invitation redeemed twice | medium | **critical** | E9.3: the claim is one conditional UPDATE, not a read then a write | ✅ closed |
| A migration that only fails on Postgres | medium | high | E9.3: the populated-copy upgrade runs on both engines, plus a static check that each ALTER matches the type the model declares | ✅ closed |
| A new POST shipping without CSRF | medium | high | E9.4: the list comes off the URL map and fails closed | ✅ closed |
| Load: 10 concurrent users per org | unknown | medium | E9.7 — measured: pages p95 324 ms, no 5xx, no lost writes | ✅ closed |
| Two simultaneous bug filings sharing a public id | low | medium | nothing but timing; E4.4a's index and retry were never extended to `bug_report`, and `test_load_smoke.py` asserts the property so a change of engine or scale says so | 🟡 guarded, not fixed |
| Security: OWASP ASVS-lite on auth/RBAC/upload | unknown | **critical** | E9.8 — run 2026-08-05, one High found and closed (`e9_security_pass.md`) | ✅ closed |

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

* **Performance beyond a smoke** — E9.7 measures ten concurrent users in one
  organisation and nothing else. There is no sustained-load or soak testing,
  no ramp, and no second organisation; on a free tier there would be nowhere
  to run one. The budget it enforces is a ceiling on the shape of the answer
  — "a page still comes back promptly with ten people on it" — and would not
  notice a 20% regression, which is a benchmark, and a benchmark on a shared
  runner is a flaky test with a graph.
* **The Postgres leg runs only in CI** — there is no Postgres on the
  development machines this was written on, so everything engine-specific in
  E9.3 is verified by the service container on push. What can be checked
  without a server is checked without one: each ALTER is compared against
  the type its model declares, which is what caught both defects locally.
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
