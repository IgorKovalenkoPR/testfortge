# E11 — Triage of the browser-extension walkthrough (2026-08-21)

An external QA pass drove staging through a 9-phase walkthrough and filed 30
findings (TFG-01…TFG-30) plus four Sprint-1 regression verdicts. This is the
code-side triage: every claim checked against the source, then fixed,
reclassified, or refuted.

The tester's report is unusually honest — it carries its own corrections log
retracting four mid-session conclusions. That habit is why this triage was
cheap: the report says where it is unsure, so the reading effort went to the
claims that mattered.

**Six of the thirty do not survive contact with the code**, and two of those
six were the report's highest-severity items. **Three findings that the
report treats as separate are one root cause.** And the finding with the
largest consequence is one the tester could not see from a browser at all.

---

## 1. The headline: one dead keyword argument

`TFG-05` was filed as "URL crawl extracts no site-specific features". It is
much worse than that, and the cause is one line.

```python
# engine/site_crawler.py:1064, before
with _security.safe_opener().open(req, timeout=FETCH_TIMEOUT, context=ctx) as resp:
```

`safe_opener()` returns an `OpenerDirector`. Its method is
`open(fullurl, data=None, timeout=...)` — **there is no `context`
parameter**; that belongs to `urllib.request.urlopen`. The kwarg was carried
over when these call sites moved off `urlopen` to pick up the SSRF redirect
policy (commit `1b31777`, "the SSRF policy now applies to every hop").

So every fetch raised `TypeError` during argument binding — before DNS,
before a socket — and the bare `except Exception` at `site_crawler.py:1092`
turned it into `("", "...")`. Zero pages made `landing` the winning site
type (`site_crawler.py:1389`, `len(pages) <= 2`), and the estimator
seeded `web_general` unconditionally, so the output looked like a
plausible low-signal result rather than a failure:

> Features / Test Case Coverage (1) · Global — web_general · 6 test cases
> "No strong architecture signals — defaulting to 'landing'"

**The entire non-Playwright HTTP path was dead in all three call sites** —
`site_crawler.py:1064`, `site_tester.py:417`, `site_tester.py:503` — for
every URL on the internet. Not a network problem: the `TypeError` is raised
identically on a connected and an air-gapped host, which is also how it was
confirmed offline.

### Why the suite stayed green at 4 716 passing

`tests/test_ssrf_redirects.py` calls `safe_opener().open(url, timeout=5)` —
**without** `context=`. It exercised a call shape no production site uses.
The guard beside it asserted the literal string `"safe_opener()"` appeared
in the source, which is satisfied by broken code.

This is the same failure mode already recorded twice in this repo:
a gate measuring the wrong chain, and a green test named for a property it
never checks.

### Consequence for regression T1

T1 (SSRF) was reported ⚠ INCONCLUSIVE, and that verdict was right for a
better reason than the tester could give. The guard **works** — both
`169.254.169.254` and `127.0.0.1:5432` are refused before any socket, and
no internal data can leak. But a blocked host and a working public site
produced byte-identical output, because the fetcher failed for both. The
distinguishing evidence existed only in `analysis.crawl_errors`
(`"blocked: …"` vs `"OpenerDirector.open() …"`) and **no UI surface reads
that field** — see §4.

With the fetch fixed, a public URL now yields real pages, so T1 regains
discriminating power on output alone. **T1 must be re-run.**

### The fix has a consequence worth planning for

A working crawler is slow. `tests/test_e2e.py::test_instructions_not_in_checklist`
caught it immediately: it posts a real URL to `/checklist`, and the run went
from instant to 69 seconds, then started returning **302** instead of 200.
Nothing was broken — generation now exceeds `SYNC_GEN_BUDGET_S`, and the
route falls back to the async path exactly as designed. That fallback had
simply been unreachable for URL input, because the crawl always failed
instantly.

So **URL-driven generation will now routinely land on the async path**,
where the user is redirected with *"Generation is still running in the
background."* That is correct behaviour meeting an unlucky gap: per §4, the
async drains discard `crawl_errors`, so a user who gets bounced there also
gets no crawl feedback at all. Fixing §4 matters more now than it did
before this change.

The test was asserting the fast-because-broken behaviour. It is now stubbed
at `_fetch_page` — the property it exists for is instruction filtering, not
crawling, and it should never have depended on a live network fetch (13 s
for the file now, down from 78 s).

---

## 2. A heuristic that has never once run

`TFG-11` reported that TestForTge files its own crashes as customer
defects, quoting `BUG-004`:

> `[Walkthrough] Heuristic 'ctas' raised Error: Page.evaluate: SyntaxError: missing ) after argument list`

The tester read this as a process defect. It is also a real code bug, in
`engine/walkthrough_runner.py:1357`:

```js
document.querySelectorAll(
    'button:not([type=hidden]), .w-button, a.button, '
    '[role="button"], input[type=submit]');
```

Two adjacent string literals with no `+` — **Python's implicit
concatenation, written inside a JavaScript string.** `node --check`
reproduces the bug's exact message.

So the `ctas` heuristic has thrown on `page.evaluate` for every page of
every run since it was written, and has never produced a finding. The only
output it ever generated was a bug reporting its own failure — filed
against the client's site.

Nothing caught it because these constants are opaque strings to Python:
they are syntax-checked by the browser, at runtime, inside a `try` that
converts the exception into an ordinary finding. `tests/test_embedded_js_syntax.py`
now parses all 8 embedded JS constants with `node --check`, plus a
parser-free check for this specific trap so the guard still runs where node
is absent.

---

## 3. Three findings, one root cause

The report files these separately. They are one chain.

```
TFG-04  automated runs create no DB run row
   │      routes/execution.py:1399 returns before the only dispatch-path
   │      start_execution_run at :1450
   ▼
T5      the concurrency gate counts run rows, so it counts zero, forever
   │      run_limits.check() → db.list_open_runs()  (run_limits.py:151)
   │      gate at routes/execution.py:832 admits every run
   ▼
TFG-03  two Chromiums under one 380 MB ceiling → OOM → worker restart
   │      no pre-launch admission check; first over_budget() poll is
   │      after engine_obj.launch() at live_executor.py:683
   ▼
TFG-02  Render's edge answers new connections with 503  ← reported as
TFG-18  in-flight static requests die with the worker      "export is broken"
```

The code's own comment at `routes/execution.py:825` predicts the outcome:
*"A browser run is a Chromium on a box with half a gigabyte, and two at
once are OOM-killed rather than queued."* That is exactly what happened.

### T5's verdict needs restating

Reported ❌ FAIL, on the evidence "no cap warning; 2nd run accepted; 3rd
returned 502". Both halves need correcting, in opposite directions:

- **"2nd run accepted" is a genuine defect** — but not because a counter is
  wrong. The gate that should have refused it reads `db.list_open_runs()`,
  and TFG-04 means an automated run has no row to count. The refusal
  message at `run_limits.py:95` is unreachable in normal operation.
- **"3rd returned 502" is not the cap.** There are two independent caps:

  | Where | Env var | Default | Guards |
  |---|---|---|---|
  | `config.py:125` | `MAX_CONCURRENT_RUNS` | **3** | job-queue kinds, blocks the *4th* |
  | `engine/run_limits.py:56` | `TESTFORTGE_MAX_CONCURRENT_RUNS` | **1** | browser runs — the dead one |

  Runs A, B and C being accepted is the *specified* behaviour of the cap
  that actually runs. The tester never reached a 4th run, so the cap was
  never exercised. C's 502 is TFG-03.

Note also the config drift: `config.py:119` claims "the rest of the
cap-enforcing code defers to" `MAX_CONCURRENT_RUNS`, but `run_limits.py`
reads a differently-named variable with a different default. Setting the
documented one on Render does nothing for browser runs.

### TFG-02 is not an export defect

Refuted as written. No code path in any export route can emit 503: the only
503 emitters in the app are `/healthz` and `/readyz` (`routes/ops.py:118`,
`:140`). `Sec-Fetch-Mode` appears nowhere in the repo. Rate limits return
429.

The export buttons are plain `<a href>` links, so Chrome issues the
download **on a fresh connection**, while the tester's XHR reused the
page's warm keep-alive socket. During a restart window the warm socket
still reaches the live worker while any new connection gets Render's own
503. Connection provenance, not request headers — which also explains why
size was irrelevant (62 KB and 89 KB failed alike).

**Fix belongs upstream (TFG-04/03), not in the exporters.** One export-side
change is still worth making: `fetch()` + blob download so a platform 503
becomes a dismissible toast instead of replacing the page with Render's
error page and losing unsaved editor state.

---

## 4. No crawler failure of any kind can reach a user

Independent of TFG-05, and the reason it survived so long unnoticed.

- **/estimation** never reads `analysis.crawl_errors`
  (`engine/estimation_service.py:137`). The one warning that could have
  fired — *"Crawled … but no testable features were extracted"* — is
  unreachable, because `features_from_site_analysis` always emits
  `Global — web_general` (`site_crawler.py:1308` seeds it
  unconditionally), so features are never empty.
- **/test-cases and /checklist** do flash `crawl_partial`
  (`routes/generation.py:1027`, `:1249`) — but only on the **synchronous
  POST render**, which the UI never uses. Every surface posts to
  `/…/run-async`, and all four drain functions copy the results into the
  session while dropping `crawl_errors` entirely.

The Guide promises an SSRF-blocked host "flash[es] a yellow banner"
(`templates/guide/_sections_en.html:252`). That is unmet on every path the
UI actually takes.

`tests/test_crawl_error_surfaced.py` mocks `crawl_site` outright, and its
route assertion accepts `"background" in body.lower()` as proof of a
banner — a word the async in-flight page contains regardless. It passes
with no banner rendered.

---

## 5. The finding a browser could not see

`TFG-22` reported that Tedgie answered a prompt-injection attempt with a
verbatim ISTQB paragraph cited to `_Book · page 224_`, and flagged it for
legal review. Measured against the corpus, the exposure is larger than the
symptom suggested:

| | |
|---|---|
| `engine/istqb_corpus.json` | 1.19 MB, committed to the repo, shipped to production |
| Chunks | 2 826 total — **2 330 from `source: "book"`**, 496 syllabus |
| Book coverage | **404 distinct pages of a 409-page book**, 816 506 chars ≈ 136 000 words |
| Per-answer cap | **none** — `istqb_rag.py:232` emitted `h["text"]` whole; largest chunk 2 594 chars |
| Build step | `tools/build_istqb_corpus.py:66` strips `^© \d{4}` lines as "noise" |

The source is named in the builder's own docstring: the Stapp / Roman /
Pilaeten *ISTQB Certified Tester Foundation Level* self-study textbook — a
commercial title. The freely-redistributable ISTQB syllabus is the *other*
496 chunks.

There was also **no refusal path**. RAG is the last branch before
clarification (`chatbot.py:1877`), gated only on `≥3` tokens and a
relevance score. An injection string clears both, and its tokens
("instructions", "verbatim", "system") score against the corpus — so a
retrieval answer is the *designed* response to hostile input. Nothing
leaked only because with no API key there is no system prompt in the path
to leak. **The resistance the tester observed was a coincidence, not a
control**, and it stops holding the moment a key is set.

### What is fixed here, and what is not

Fixed: an explicit refusal ahead of the RAG branch, and a 60-word cap on
any quoted excerpt, marked where it was cut
(`istqb_rag.MAX_EXCERPT_WORDS`). Precision was checked against the six
questions the Guide promises Tedgie answers, plus "how do I test for prompt
injection?" — none is refused.

**Decided: the book half is out of the shipped corpus.** A runtime cap
reduces how much of a copyrighted work any one answer reproduces; it does
not make shipping 404 pages of that work in a repository acceptable. So:

- `engine/istqb_corpus.json` is now **syllabus-only** — 2 826 → 449 chunks,
  1.19 MB → ~200 KB. The 496 syllabus chunks are redistributable under
  ISTQB's terms.
- `tools/build_istqb_corpus.py` excludes the book unless
  `ISTQB_BOOK_CORPUS=1`. The gate is in the **builder**, not the loader,
  because the exposure is the committed artefact rather than the runtime
  read — a flag that still wrote the book into the JSON would not have
  helped.
- A test asserts the shipped artefact carries no `source: "book"` chunk, so
  a rebuild cannot quietly restore it.
- The source PDFs were never tracked (`uploads/` is gitignored), so the
  derived JSON was the only committed copy.

⚠ **Two things this does not do.** It does not remove the book from **git
history** — that needs a history rewrite, which is a separate decision and
a disruptive operation. And it does not undo the `©`-stripping already
applied to the extracted text.

### The defect the textbook was hiding

Dropping it made Tedgie visibly worse, which turned out to be a *second*
pre-existing defect rather than a cost of the change: **116 of the 496
syllabus chunks were table-of-contents rows or running-header lines.**
Real prose had always outscored them. With the book gone they started
winning retrieval:

| Question | Answer before cleanup |
|---|---|
| "What is statement coverage?" | `White-Box Test Techniques .......................` |
| "What is risk-based testing?" | `A Practitioner's Approach, 9th ed., McGraw Hill` |
| "…equivalence partitioning vs BVA?" | `Foundation Level v4.0.1 Page 6 of 78  2024-09-15` |

`NOISE_PATTERNS` missed them because ToC rows and the inline running header
are not bare page numbers, and the `©` pattern needs a leading glyph that
extraction had already mangled. Three patterns and a minimum chunk length
now catch them (449 chunks after filtering), applied to the artefact
through the builder's own `clean_chunk_text` so the two cannot drift. The
same questions now return real prose.

**Known residue**, filed rather than fixed blind: a few References-section
chunks still surface for broad queries, and the extraction carries mojibake
(`�` where `'` and `©` were). Both need the source PDF to fix properly, and
neither is in this repository.

---

## 6. Findings that do not survive the code

| Finding | Reported | Actual |
|---|---|---|
| **TFG-01** Critical | "Session-vs-DB hydration hides real data"; `/bug-reports` reads the session pack | **Hypothesis refuted.** `/bug-reports` is already DB-first (`routes/bugs.py:74`); a cold start renders all 24. Two real defects underneath, below. |
| **TFG-02** Critical | Every export button 503s | **Refuted as an app defect.** Render's edge; symptom of TFG-03. §3 |
| **TFG-21** Medium | Switch does not re-render the module | **Refuted server-side.** The post-switch GET renders the new pack in both flag modes. Real defect underneath: the "Restored N test cases" flash fires on *every* GET under `WORKSPACE_DB_FIRST`, because `had_in_session` reads a session key that mode never populates (`routes/generation.py:1107`). That recurring message is what made the pack look late. |
| **TFG-23** Low | The page restores a deep scroll offset | **Refuted.** No scroll-restoration code exists for this page; that is default browser back/forward behaviour. The clicks landing wrong are consistent with an automation client using fixed viewport coordinates. Real improvement underneath: make the picker sticky. |
| **TFG-12** Major | "No de-duplication across runs" | **Partly refuted.** Cross-run dedup exists and is project-wide (`db.find_bug_id_by_signature`). Real gap: TC-driven and early-exit bugs never compute a signature, so those two paths can never dedupe — which is where the duplicate pair came from. |
| **TFG-10** Major | Severity and priority set by independent rule sets that never reconcile | **Diagnosis wrong, defect real.** They do reconcile — `severity_priority()` derives priority from its own severity table, and the two agree for 17 of 18 defect classes. The cause is `_area_weight` (`bug_template.py:229`) falling back to weight 1 for any hint without an auth/checkout/search/nav/homepage keyword, and weight 1 maps `Critical→Low`, everything else `→Lowest`. Reproduces the observed spread exactly. `engine/site_findings.py:110` already documents this trap and fixes it for the site-tester path only. |

TFG-30 (14 guide cards vs 9 expected) is confirmed as not a defect.

Two claims the tester withdrew were right to withdraw, and one retraction
went too far: T7's XLSX path, listed as "not verified", **is** covered —
`tests/test_exporter_injection.py::test_xlsx_cell_value_starts_with_apostrophe`
passes. T7 is a clean ✅ on both paths.

---

## 7. What was fixed

All with tests; full suite green at 4 740+ before these, and the new files
each fail without their fix.

| Finding | Fix |
|---|---|
| **TFG-05** + T1 | `safe_opener(context=…)` takes the TLS context so the trap has nowhere to live; three call sites corrected. `tests/test_crawler_fetch_shape.py` drives a real loopback server in the **production call shape**; the SSRF guard test now asserts `context=` never reaches `.open(`. |
| **TFG-11**(1) | The missing `+`. `tests/test_embedded_js_syntax.py` parses all 8 embedded JS constants. |
| **TFG-07**(a) | `bug_field_edit` takes and forwards `choices`; severity/priority get the vocabularies the route already supplies. |
| **TFG-07**(b) | `showValueFor(actionSel.value)` on load — the browser restores the *select*, not just the checkboxes — plus a server-side refusal when a value-taking action arrives with no value, so a broken submission can never write NULL again. |
| **TFG-08**, **TFG-17**(b) | `_insert_bug` backfills `external_id` to the id the row is already *displayed* under, so the mint and the display can no longer disagree and the unique index covers the row. This also populates the Bug ID column in run results, which was blank for the same reason. |
| **TFG-17**(a) | `case_result_to_dict` emits `item_type` beside `kind`. The template compared `r.item_type` against an Undefined for every DB-sourced row, so every Summary cell rendered empty — silently, since a missing key is not an error in Jinja. |
| **TFG-09**(b) | `TCTemplate.user_story_id`, stamped once for everything the story path produces, and carried into `TestCase`. The traceability matrix's Test Cases and Categories columns were empty because `generate_traceability` had nothing to join on. |
| **TFG-20** | The switch flash counts what was read from the database, not session keys `mirror_pack` never writes under `WORKSPACE_DB_FIRST`. |
| **TFG-15** | 403 with a matching body, instead of a 401 carrying 403's wording and no `WWW-Authenticate`. 403 is right because `X-Ops-Token` is not an HTTP auth scheme, so there is no scheme to name. |
| **TFG-22** | Injection refusal ahead of the RAG branch; 60-word excerpt cap. Corpus decision outstanding — §5. |
| **TFG-26** | Italics in `mdToHtml` (`*…*` and `_…_`), narrow enough to leave `snake_case` alone. |
| **TFG-27** | Component falls back to the `[Component]` title prefix, then `bug_area`. Runner bugs titled `[Authentication] …` were all grouped under "(unspecified)". |
| **TFG-29** | The live-view diagnostic pointed at `browser_pool: ok`, a field `/healthz` has never had. Replaced with `uptime_seconds`, which actually diagnoses the restart that empties the live view. |
| **TFG-24** | `Brooks\` → `Brooks's-law communication overhead.` |
| **TFG-04** + T5 | Run rows opened at dispatch and adopted at import — §8.1. This revives the concurrency gate, so the second browser run is now refused instead of racing the first into the OOM. |
| — | Duplicate `@app.route("/export/<fmt>")` decorator removed. |

---

## 8. What is left, in the order I would take it

1. ~~**TFG-04**~~ — **done.** The dispatch path now opens one run row per
   `env_type` before spawning the worker, writes the ids into
   `config_payload["db_run_ids"]`, and the results endpoint *adopts* them
   instead of opening a second set (which would have doubled every
   automated run in the register). Rows are left `running` on a crash on
   purpose: `run_limits.split_by_age` ignores anything past the staleness
   window, so a dead run stops blocking the cap by itself instead of
   wedging it.

   The test that was missing is in `tests/test_run_limits.py`. Worth noting
   *why* it was missing: every route test in that file pre-seeded the
   blocking run by calling `start_execution_run` directly, establishing by
   hand the precondition the product was failing to establish. The gate was
   correct and covered; nothing exercised the path that never fed it. The
   new test drives the dispatch and asserts both halves — a row exists
   afterwards, and the next request is refused because of it. Confirmed to
   fail without the fix, with exactly this message:

   ```
   AssertionError: a dispatched automated run left no row — the Runs
   register cannot show it and the concurrency gate cannot count it
   ```

   Still open in this area: per-environment tabs (now unblocked, but the
   register template has no tab markup), and reconciling the two caps into
   one knob.
2. **TFG-03** — a pre-launch admission check in `runner_worker.py` before
   `LiveExecutor(...)`, refusing when available memory is under a launch
   reserve. Reconcile the two caps into one knob while there.
3. **TFG-09**(a) — **the systemic one.** CSP has no `'unsafe-inline'`, and a
   nonce does not whitelist inline event-handler attributes, so **26
   `onclick=` attributes across `templates/` are dead** — including the
   Traceability tab and the category filter bar. `static/js/test-execution.js:250`
   is the established fix; the existing guard test only scans
   `_inline_edit.html` and should glob all of `templates/`.
4. **§4** — surface `crawl_errors` through the four async drains, with
   distinct outcomes for *blocked host* / *fetched but empty* / *fetch
   failed*, and assert on that string so the SSRF regression can gate a
   release. Fix `test_crawl_error_surfaced.py`'s assertion while there.
5. **TFG-01 underneath** — (a) `_hydrate_bugs` silently falls back to the
   stale session mirror when `list_bugs` raises, while the Reset label
   counts via a separate query: that pairing is how "2 cards beside
   Reset (24)" happens, and on free-tier Postgres a connection blip is the
   everyday trigger. Return `[]` and say so instead. (b) `#bug-create` sits
   inside `{% if bugs %}`, so a project with zero bugs offers no way to
   file the first one, and its empty state prints the *test-case* string
   `te_no_data`.
6. **TFG-06** — upload run artefacts through `engine.blobs` at
   finalisation. Nothing currently puts them in the storage backend, and
   `_purge_old_automation_runs` deletes them by age and count regardless of
   restarts. Flag unresolvable attachment links instead of rendering dead
   images.
7. **TFG-11**(2), **TFG-10**, **TFG-12** — adopt the pattern
   `engine/site_findings.py` already implements: tool failures do not
   become site defects, a real page-role hint reaches `severity_priority`,
   and the TC-driven and early-exit factories compute a dedup signature.
8. **TFG-13/14** — generator quality. Gate the boundary-value triple on
   evidence of a constrained input; route every summary slice through the
   existing `_excerpt()` helper instead of raw `[:120]`; drop the
   `.lower()`; `\b`-anchor the area-keyword match per requirement (today a
   spec containing "assortment" buys the whole Search pack, because "sort"
   is a substring); renumber sections once after both `extend` calls.
   ⚠ While in here: `qa_persona.py:991` **deletes every SQL-injection case
   whenever a URL is in scope**, so the security case the tester praised
   silently vanishes on the URL path. Its second clause compares a string
   against a one-element list and is always true.
9. Remainder: TFG-19 (unknown-status fallthrough, `age_s` frozen server-side,
   `_live/` a single global slot shared by concurrent runs), TFG-18
   (`SEND_FILE_MAX_AGE_DEFAULT`; 14 requests/sec from the live poller),
   TFG-25 (`.te-assignee` has no CSS rule at all), TFG-28 (the handler
   takes no pack argument — fix it or correct the Guide), TFG-16 (reword to
   "Jira-importable CSV", or emit the XML).

## 9. Environment note

The report's §2 is accurate and matters for reading §5 of *its* findings:
no `ANTHROPIC_API_KEY` is configured on staging, confirmed at
`engine/llm_keys.py:255`, and `/org/settings` says so. **Every one of the
373 test cases and 478 checklist items the report grades was rule-engine
output**, and Tedgie answered from retrieval. TFG-13's quality complaints
are therefore about the degraded path, not the product's intended one —
worth fixing on their own terms, but not evidence about LLM-assisted
generation, which was never exercised. T6 is correctly marked not testable.
