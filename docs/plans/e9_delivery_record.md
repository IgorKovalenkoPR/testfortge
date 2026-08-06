# E9.3–E9.7 — delivery record

Closes E9.3, E9.4, E9.5 and E9.7 (2026-08-06). With E9.1/2/6/8/9 already
done, **E9 is closed in full.**

A record of what was built and what it found, kept beside
[e9_test_strategy.md](e9_test_strategy.md) — which says what the levels are
*for* — and [e9_security_pass.md](e9_security_pass.md), which is the same
kind of document for E9.8. The strategy is the standing statement; this is
what happened when it was carried out.

**Three defects fixed here, and none of them was found by reading the code.**
Two came out of running the migration on a database with rows in it; the
third came out of CI, and it is the most serious of the three because it is
a production failure mode that had no symptom.

---

## E9.3 — integration: the migration on a copy with data in it

`tests/test_schema_migration.py` already covered the clean half — build the schema, drop a column, put it back. This adds the half the acceptance criterion names separately, *a populated production copy*, and it is not decoration: three of the programme's migrations only do anything **because** rows are already there. The editing metadata back-fills existing artefacts, the renumbering exists solely for rows written before uniqueness was enforced, and the unique index is creatable on an empty table no matter what.

`tests/test_migration_populated_copy.py` fills a database through the product's own writers, takes the schema back to its pre-programme shape, and boots the app again — on both engines.

### Two Postgres-only defects

Both were invisible in CI, because CI's Postgres database is created fresh every run and so the ALTERs never fire:

- **`ai_generated BOOLEAN NOT NULL DEFAULT 1`** — there is no assignment cast from integer to boolean, so Postgres refuses the statement. `_ensure_editable_columns` catches and logs that, which means the column is simply never added and the ORM's next read fails — on upgraded instances only. Quoted now, which is what `create_all` already emits for the same column on both engines.
- **`project.settings` added as `TEXT`** against a `JSON` model column. SQLAlchemy decodes JSON itself on SQLite and leaves it to the driver on Postgres, where psycopg2 only parses columns whose declared type really is json — so `get_project_setting` would have called `.get` on the string `'{}'`.

Both are now also caught **statically, without a server**, by comparing each ALTER against the type its model declares. Those two guards fail on the old DDL and pass on the fixed one (verified by stashing the fix).

### An invitation is one seat

`consume_invite` read `used_at` and then set it — safe under SQLite, which serialises writers, and unsafe under Postgres READ COMMITTED, where two people opening one forwarded link both read NULL and both join. It claims the token with **one conditional UPDATE** now, before the membership is written, so the two failure orderings agree: a claim that cannot grant the seat leaves the link working.

---

## E9.4 — functional: every POST answers for its token, every module for its roles

The acceptance criterion is "each new POST has a test with `WTF_CSRF_ENABLED=True`". Written as sixty-one hand-kept tests that is one endpoint away from being wrong, and it reads as coverage while it is — the failure mode `engine/route_policy.py` exists to prevent. So `tests/test_csrf_on_every_post.py` derives the list from the URL map and fails closed: an endpoint is either refused without a token, or named in `EXEMPT` with the credential that authenticates it instead. Six are exempt today, all machine callers with no session and therefore no session-scoped token; each is then **watched refusing an anonymous caller**, so the exemption is a claim under test rather than a note.

`tests/test_functional_module_matrix.py` asks the question the three surrounding access-control files do not: **did the thing happen, and did the page say so?** Every defect this programme lost time to answered 200 and rendered a page that looked right. So each module is performed as an administrator and checked in the database *and* in the returned text, then performed as a plain user and checked that **nothing changed** — the assertion a gate-level 403 cannot make. Projects, members, organisation settings, dashboard targets and the bug toolbar, every request carrying a token fetched the way the browser fetches it.

Two product rules the first draft mis-stated, now written where a test keeps them honest: closing a bug is the admin's sign-off and a tester marks Resolved instead, and the KPI the dashboard stores a target for is `exec_pass_rate`.

---

## E9.5 — E2E: five golden paths and two people in one team

`tests/test_e2e_golden_paths.py` — a real Chromium signing in through the form. Five journeys rather than one, because one long journey fails at step six and says nothing about steps one to five: claiming an invitation, creating a project, uploading a pack, walking it manually one verdict at a time, and filing a bug the dashboard then counts. Plus the pair that only mean anything with two sessions open at once — a tester's work showing up in the admin's session, and another team's project answering 403 to a URL typed straight into the address bar.

Authenticated with CSRF enforcement **on**, which makes this the one file in the suite where the tokens the templates emit are read out of the HTML by something that has to find them.

Two things the browser taught the tests:

- the run-mode radio is painted as a card and the input itself is never visible, so `check()` waits forever — the label is what a person clicks;
- the first draft guessed `/projects/db/load/<id>`, which does not exist, which 404s, which would have satisfied "a non-member cannot see it" **without the access check existing at all**. It asserts the 403 now.

Anti-flaky gate is ten consecutive runs, per the strategy's no-quarantine policy, in its own CI job because it needs a Chromium the test matrix has no use for.

---

## E9.7 — load smoke: ten people in one organisation

A smoke and no more, as the strategy says out loud. What it answers is the narrower question the architecture raises: everything here is scoped to an organisation, and until now nothing had ever put ten sessions in one scope at the same moment.

Real HTTP against a threaded server, not the test client — the test client runs the view in the calling thread and is therefore a fine way to test a view and a useless way to test concurrency. Ten journeys released from one barrier, and a **second barrier immediately before the write** so the ten filings genuinely collide instead of being staggered by whatever their earlier requests happened to cost.

Every run prints its own percentiles, so whoever next changes the budget can see what it was set against without re-deriving it.

---

## The defect CI found, and why it took fourteen files to say so

The first push was red on all three interpreters while the `e2e` job — the same browser file, run alone — was green. That combination is the finding.

Diagnosing it needed one change first: the Actions logs endpoint **refuses an unauthenticated caller on a public repository**, so a red build published exactly one line, "Process completed with exit code 1", and the failing test names lived only in a log nobody without a token can open. Both legs now echo pytest's summary as `::error::` workflow commands, so the names land in the checks annotations and in the API. That is worth having regardless of this bug.

With that, the names arrived immediately: all fourteen tests of `test_e2e_golden_paths.py`, erroring at setup with

```
It looks like you are using Playwright Sync API inside the asyncio loop.
```

Reproduced locally by pointing `PLAYWRIGHT_BROWSERS_PATH` at an empty directory — same fourteen errors — and bisected to `tests/test_crawl_error_surfaced.py`, fourteen files earlier.

### The cause

`BrowserTestRunner.__enter__` started Playwright's driver and then launched Chromium as two bare statements. `__exit__` never runs when `__enter__` raises, so a failed launch left the driver started — and the driver is not merely a leaked subprocess. The sync API runs its event loop **in the calling thread**, so the thread was left inside a running loop and every later `sync_playwright()` anywhere in the process raised the message above instead of whatever the real problem had been.

Two things kept it hidden:

- `qa_persona` calls this inside `except Exception: pass`, so the launch failure is never reported at all — generation carries on with no browser findings. That is this programme's signature shape: **the wrong behaviour looks exactly like the right one.**
- On any machine where Chromium is installed the launch succeeds and nothing leaks, which is every development box here.

**On a dyno this is the OOM path.** Chromium is killed for memory, one run goes quiet, and every subsequent browser run in that worker fails for a reason that has nothing to do with it — precisely the failure `OomGuard` exists to make legible.

`__enter__` unwinds now, and `__exit__` guards each half separately: a browser that fails to close must not skip stopping the driver, which is the half that holds the loop. Three of the four tests in `tests/test_browser_tester_lifecycle.py` fail without the fix, and none of them needs a browser — the failure is the subject.

---

## Numbers

| | |
|---|---|
| Suite, flags off | **3883 passed**, 49 skipped |
| Suite, `AUTH_ENABLED=1 ORG_MODE=1` | **3883 passed**, 49 skipped |
| Suite with the browsers removed | **3854 passed**, 78 skipped — was 14 errors |
| E9.5 anti-flaky gate | **10 / 10 green** locally (~31 s each) and in CI |
| E9.7 pages p95 | **324 ms** (p50 101 ms), no 5xx, no `database is locked`, all ten writes landed |
| E9.7 sign-in p95 | 2.9 s — ten Argon2 verifications arriving together, which is the hash doing its job |
| CI | 3.11 / 3.12 / 3.13 + real Postgres, e2e ×10, all green |
| Diff | 13 files, +3122 / −43 |

The load budget is 3000 ms — about nine times the measured p95. The multiple is not generosity: the file also runs on a shared two-core runner, and a performance gate that fails on somebody else's noisy neighbour teaches people to rerun the build. Three seconds still fails on an N+1 across the bug list or a per-request model call.

## What this does not prove

- **The Postgres leg runs only in CI.** There is no Postgres or Docker on the machine this was written on, so everything engine-specific in E9.3 is verified by the service container on push — now confirmed green. What can be checked without a server is checked without one: the two static ALTER-versus-model guards are what caught both defects locally.
- **No soak, no ramp, no second organisation.** E9.7 measures ten concurrent users in one organisation and nothing else.
- **Chromium only.** A browser matrix triples the cost of every run and is a decision for whoever needs it.

## One finding left open on purpose

`create_bug_report` mints public ids "one past the highest" by reading the project's bugs and then writing, with nothing in between and no unique index behind it — the exact shape E4.4a closed for test cases and checklist items and never extended to bug reports. Ten simultaneous filings produced ten distinct ids in seven runs, which is a fact about SQLite's single writer at this scale rather than a property of the code. The assertion stays in `test_load_smoke.py` so a faster engine or a bigger team says so here, instead of a client finding two `BUG-004`s in a report. Extending E4.4a's index and retry is its own change.

## Also in here

- `docs/plans/e9_test_strategy.md` — the levels section now says what the populated-copy upgrade actually found; the risk matrix gains the rows this closed and loses the stale claim that E9.8 had not been run; two honest entries added to "what this does not cover".
- `docs/plans/e9_security_pass.md` — **`BEHIND_HTTPS` verified in effect on the live host, 2026-08-06.** The July check found no `Strict-Transport-Security`; today `curl -sSI https://testfortge.onrender.com/healthz` returns it (two years, `includeSubDomains; preload`) and the session cookie comes back `Secure`. The same flag gates `Secure` and `WTF_CSRF_SSL_STRICT`, so a missing HSTS header was never only a missing HSTS header. Allow ~60 s for the first request — the free plan sleeps, and a 25-second timeout reads as an outage when it is a cold start.
- `.github/workflows/tests.yml` — the `e2e` job, and the failure annotations described above.

## Commits

Oldest last, on `claude/e9-testing-integration-roles-e2e-83a1e8` off
`1b31777`. 13 files, +3122 / −43.

```
fix(browser): a Chromium that will not start no longer poisons the process
ci: put the failing test names in the annotations, not only in the log
docs(e9.8): BEHIND_HTTPS is in effect on the live host — checked, not assumed
test(e9.7): count both reads of the bug list, and say why sign-in is not a page
docs(e9): record what E9.3–E9.7 measured, and where each number came from
test(e9.7): ten people in one organisation, over real HTTP, with the numbers printed
test(e9.5): five golden paths and two people in one team, in a real browser
test(e9.4): every POST answers for its token, and every module for its roles
test(e9.3): walk the migration on a copy with data in it, and one link, two people
```
