# Test quarantine

Tests listed here are excluded from the blocking CI gate and run in a
separate, non-blocking job. Every entry has an owner, a reason and a review
date. On the review date the owner either makes the test independent of the
thing that put it here, or deletes it. An entry is not extended silently.

## How it works

* The tests carry `@pytest.mark.network` (registered in `pytest.ini`).
* The blocking `pytest` job in `.github/workflows/tests.yml` runs with
  `PYTEST_ADDOPTS="-n auto --dist loadgroup -m 'not network'"`, so the matrix,
  the coverage gate and the authenticated leg all skip them.
* The `network` job runs `pytest tests/ -m network` with
  `continue-on-error: true`. A failure shows as a warning annotation naming
  the test; it does not fail the workflow.
* Locally: `pytest -m "not network"` matches CI, `pytest -m network` runs
  only these.

## Relation to the zero-flaky policy

`docs/plans/e9_test_strategy.md` says there is no quarantine list for
**flaky** tests, and that stays true. A flaky test is still treated as a
defect. The entries below are not flaky. They pass or fail the same way
every time, but the result depends on a third-party website that this code
does not control. So they cannot gate a change to this code.

## Entries

| Test | Owner | Reason | Added | Review by |
|---|---|---|---|---|
| `tests/test_functional.py::TestTestCasesRoute::test_post_url_generates_test_cases` | QA lead | POSTs `https://testfort.com/software-testing-services` as the input. The route fetches the page live, and the assertion (`status_code == 200`) depends on what the site returns. When the site redirects, or the runner is offline, the route answers 302 and the test fails without any change to this code. | 2026-10-12 | 2026-11-12 |
| `tests/test_functional.py::TestChecklistRoute::test_post_url_generates_professional_checklist` | QA lead | Same live fetch of `https://testfort.com/software-testing-services` for the checklist route. It also asserts on the generated rows (`HDR_001`), which follow the site's current markup. | 2026-10-12 | 2026-11-12 |
| `tests/test_functional.py::TestChecklistRoute::test_post_instruction_filtered` | QA lead | The input embeds `https://testfort.com/`, so the checklist route fetches the live home page before filtering the instruction text. It fails with 302 vs 200 in the same way. | 2026-10-12 | 2026-11-12 |

## Exit criteria (what "fixed" means for these three)

Serve the page from a fixture instead of the live site, for example a
recorded copy of the HTML behind a stubbed fetch or a local HTTP server.
Then remove the `network` mark. The behaviour the tests are named for
(URL input produces test cases or checklist rows, and instructions are
filtered out) does not need the real site. Only the input does.

## Not quarantined, but worth a look at the same review

`test_checklist_has_many_items` and `test_cl_preserved_after_lang_switch` in
the same file POST `https://example.com`. They were not reported failing
and are not marked. If they also fetch live, they have the same dependency
on the network and should get the same fixture.
