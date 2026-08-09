# The memory threshold for a browser pass (E5.2)

**Acceptance criterion:** *a run against a real site is not OOM-killed; the
threshold is documented.*

This is the documentation half. The number below is measured, not chosen.

---

## 1. What a browser pass actually costs

Measured with a real Chromium against real pages, sampling this process and
every child:

| Stage | Python RSS | Whole tree | Step |
|---|---|---|---|
| baseline | 33 MB | 33 MB | — |
| `sync_playwright()` | 34 MB | 98 MB | +65 |
| `chromium.launch()` | 36 MB | 145 MB | +47 |
| `new_page()` | 36 MB | 267 MB | **+122** |
| example.com | 36 MB | 284 MB | +17 |
| python.org | 36 MB | 362 MB | +78 |
| a Wikipedia article | 36 MB | 393 MB | +31 |
| after close | 37 MB | 37 MB | — |

Two things fall out of this table, and both matter more than the totals.

**Python never moves.** It sits at 36 MB from launch to the last page while
the tree reaches 393 MB. Chromium is not in this process; Playwright runs it
as a separate tree.

**The largest single step is 122 MB.** The guard is polled *between* pages
and *between* test cases, never inside one, so whatever a single step can
add has to fit in the gap between the budget and the real ceiling.

## 2. The defect this found

`OomGuard` polled `psutil.Process().memory_info().rss` — this process only.
Against a 400 MB budget it reported **36 MB** and never fired, while the
container climbed past 390 MB toward its 512 MB limit and the kernel did the
stopping.

That is the symptom `render.yaml` already recorded without a cause: *"the
worker was being OOM-killed ~110 s into a /test-cases generation; because
JobQueue lives in that process's memory the job died with it,
`/test-cases/status/<id>` answered 404, and the user got 'The generation job
was lost' after a two-minute wait with nothing saved."*

The guard existed, was enabled, had a sensible-looking budget, and was
watching the one participant that does not grow.

Demonstrated against a real browser with a 300 MB budget:

```
example.com      old-guard saw   65 MB -> fire=False | new guard sees 312 MB -> fire=True
python.org       old-guard saw   65 MB -> fire=False | new guard sees 394 MB -> fire=True
wikipedia        old-guard saw   65 MB -> fire=False | new guard sees 420 MB -> fire=True
```

## 3. The threshold

    budget = container limit − STEP_HEADROOM_MB     (130 MB)

`STEP_HEADROOM_MB` is the worst single step measured (+122) with a little
over. `engine/live_executor.container_memory_limit_mb()` reads the real
ceiling from cgroup v2, then v1, then the host's RAM — because the same code
runs on a 512 MB dyno and on a 16 GB Actions runner, and one constant is
wrong on one of them by construction.

On Render free that arithmetic is **512 − 130 = 382**, and `render.yaml`
declares `MEMORY_BUDGET_MB=380` explicitly so the number is readable in one
place rather than inferred. An explicit value always wins over the
derivation.

Polling the tree costs **7.8 ms** against 13 µs for the single process,
measured. At one poll per page that is not a cost. It would be at one per
step, which is why it is not done there.

## 4. What this does *not* license

**`TESTFORTGE_BROWSER_ENABLED` stays `0` on the free plan**, and the
measurement is the argument for it rather than against it.

A pass needs ~390 MB of Chromium *on top of* Flask, SQLAlchemy, the LLM
client and whatever the request is already holding. The ceiling is 512 MB
for the whole container. The guard now stops the run cleanly instead of the
kernel killing it — which is the difference between "this feature does not
work here" and "this feature takes the service down with it" — but a run
that exits on the first page is not a working feature.

`cost_model.md` §Tier 3 puts in-app Playwright at 2 GB, ~$45/month. The
platform's budget is $0, so the automation half of Execute runs in GitHub
Actions and posts to `POST /automation/allure-results` (E5.2′) — free
minutes, 16 GB runners, and the ingest endpoint already exists.

**What changed is the failure mode, not the verdict.** If the flag is ever
turned on — a bigger plan, a self-hosted box with real RAM — the guard now
does what it always claimed to.

## 5. If you turn it on anyway

1. Set `MEMORY_BUDGET_MB` to `limit − 130`, or leave it unset and let the
   cgroup derivation do it.
2. Watch `early_exit_reason` in the run result. `oom_budget_exceeded` means
   the guard won; a truncated result with no reason means the kernel did.
3. Below roughly 600 MB of container, expect the guard to fire on the first
   page. That is correct behaviour and a useless run.
