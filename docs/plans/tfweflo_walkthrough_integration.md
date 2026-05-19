# TFWefloLab QA Walkthrough → TestForTge Test Execution

**Source:** TFWefloLab (Node.js + Playwright) at `F:\ClaudeProjects\Webflow\Testing`.
**Target:** TestForTge `engine/automation_runner.py` + Test Execution route.

**Verdict:** **(B)** — adopt as an additional Test Execution mode, **port heuristics to
Python**, do not shell out to Node. Drive walkthrough by URL-pattern-matched TCs
so the existing TC/CL audit trail stays intact.

**Estimate:** ~52 h, three small PRs.

---

## What TFWefloLab's "QA walkthrough" actually does

- **Entry point**: one 1 059-line Playwright spec, `F:\ClaudeProjects\Webflow\Testing\tests\walkthrough.spec.js`. Registered in dashboard as the first test type at `F:\ClaudeProjects\Webflow\Testing\server\server.js:75`. CLI alias `npm run test:walkthrough`.
- **One test per (site × device)**, hard-capped at **8 min** per device (`walkthrough.spec.js:170`). Outer loop iterates `config/urls.json`. Output is one linear video per device.
- **Eight or nine sequential `test.step()` blocks**:
  1. `goto(site.url)` with 45 s `domcontentloaded` budget (`:225–:253`).
  2. Full-page scroll via `scrollPageVisible`; flags `<img>` with `naturalWidth === 0`, captures annotated red-box screenshot per broken image (`:263–:328`).
  3. Header/menu: hamburger tap on mobile/tablet with before/after `countVisibleNavLinks`; hover-click each `.w-dropdown-toggle` on desktop (`:330–:435`).
  4. Up to 5 internal links via `collectInternalLinks` (top-level header nav only); click + fallback `goto()` (`:440–:517`).
  5. Footer + social-link sanity: `footer a[href]` regex-filtered, flags placeholder hosts + missing `rel="noopener"` (`:525–:557`).
  6. Search field probe: opens trigger, fills `"test"`, polls for result/suggestion container (`:561–:655`).
  7. Forms: fills every visible `input/textarea` in up to 5 forms with type-aware sample data (`qa+test@example.com`, `+10000000000`…), does NOT submit (`:660–:708`).
  8. CTAs: enumerates buttons + `.w-button`, flags `href="#"`, missing destination, sub-24px tap targets (`:713–:746`).
  9. axe-core scan via `@axe-core/playwright`, mapped to plain-language impact via `AXE_IMPACT` table (`:132–:147`, `:751–:790`).
  10. Landscape rotation (mobile/tablet only): flips viewport, re-runs `collectPageFindings`, screenshot (`:795–:822`).
- **Findings** accumulate into in-memory `findings[]`, written as Playwright attachment `findings.json` (`:905`). Server harvests in `tryParseFindings` (`server.js:341`). Dashboard renders via `renderWalkthroughs` (`app.js:855`) with cross-device dedup pass (`server.js:516–:560`).
- **Pass/fail policy is "report, don't block":** `expect.soft` + hard fail only on Critical. High/Medium/Low never fail the run.
- **Webflow-specific selectors baked in**: `.w-nav-button`, `.w-dropdown-toggle`, `.w-dropdown-list`, `.w-form`, `.w-form-done`, `.w-form-fail`, `data-w-id`. `humanizeJsError` (`:64–:128`) is Webflow IX2-tuned.
- **It does not read or know about test cases / checklists.** No TC-ID, no precondition matching, no "section" concept. Walkthrough is an *autonomous explorer*; TCs are an *imperative script*. Different mental models.

---

## Capability overlap with TestForTge

| Capability | TFWefloLab | TestForTge | State |
|---|---|---|---|
| Playwright orchestration | `playwright.config.js` × `walkthrough.spec.js` (Node) | `engine/automation_runner.py` via `sync_playwright()` (`:338, :459`) | wired |
| Autonomous page crawl | `collectInternalLinks` (`:1037`) + sitemap in `audit/` | `engine/site_crawler.py::crawl_site` (`:590`), MAX_PAGES=50 | **dormant** for Test Execution (Estimation only) |
| TC/CL → executable script | n/a | `engine/automation_qa.py::parse_manual_step` → `AutomationScript` (`automation_runner.py:581`) | wired |
| Per-step screenshot + live frame | `installCursorVisualizer` + `highlightAndScreenshot` | `_live_pump`, `_live_dir/latest.png`, filmstrip (`automation_runner.py:359–:425`) | wired |
| **Broken-image scan** | `naturalWidth === 0` + annotated PNG (`:279–:325`) | — | **missing** |
| **Hamburger / dropdown probe** | `:336–:434` | — | **missing** |
| **Form auto-fill with type-aware samples** | `:667–:707` | — | **missing** |
| **Footer / social-link sanity** | `:525–:557` | — | **missing** |
| **Search-field probe** | `:561–:655` | — | **missing** |
| **CTA / tap-target audit** | `:713–:746` | — | **missing** |
| **axe-core a11y** | `@axe-core/playwright` (`:751–:790`) | — | **missing** |
| **Landscape rotation re-check** | `:795–:822` | — | **missing** |
| Plain-language error humanisation | `humanizeJsError` + `AXE_IMPACT` (`:64–:147`) | `engine/bug_template.py:ERROR_CLASS_PATTERNS` (`:66–:79`) — Playwright-error-fragment driven | partial |
| Severity/priority assignment | string constants in spec | `bug_template.severity_priority` (`:207`), testsigma-aligned; area weights (`:142–:158`) | wired (richer than TFWefloLab) |
| **Cross-device bug dedup** | `server.js:516–:560` | — | **missing** |
| Findings render UI | `renderWalkthroughCard` (`app.js:855–:1000`) | `templates/test_execution.html` per-env tabs | different model |
| Detached subprocess | Express → Playwright child | `routes/execution.py:1018` → `python -m engine.runner_worker` | wired |

**Takeaway:** TestForTge already has the runner, live view, severity engine, and a
dormant Python crawler. What's missing is the **exploration heuristics** (broken
images, dropdowns, search, forms, CTAs, axe, landscape) — most of TFWefloLab's
value. Plus cross-env bug dedup. Other infrastructure is duplicate.

---

## Why option (B), why Python port

Five reasons for **(B)** with a Python port:

1. **(A) is a bad trade.** Current TC/CL runner is the product's differentiator — TC trace, manual_statuses, manual_bug_refs, Recent projects — all keyed on TC IDs. Walkthrough has zero TC awareness.
2. **Walkthrough does not naturally accept TC input.** `walkthrough.spec.js:160–:520` has hardcoded steps 1–8; only "input" is `site.url`. Bolting a TC list onto it = rewriting most of the spec, at which point you're writing a new runner.
3. **(C) is wrong.** 8 missing defect classes are real product gaps.
4. **Cross-stack subprocess is a real hazard.** Spawn Node from Python means: Node on PATH (no guarantee on Render), TWO `playwright install` browser caches (~600 MB each on a free-tier disk that already retention-purges per `automation_runner.py:30–43`), version skew between `playwright==1.49.1` Python and `@playwright/test@^1.48.0` Node, two ways to fail. **Python port using existing `site_crawler.py` + `sync_playwright` is one stack, one binary, no node_modules.**
5. **(B) lets the TC binding be optional.** Mode toggle on the form. Walkthrough mode OFF → today's behaviour byte-identical. ON → walks autonomously + opportunistically runs any TC whose `url_pattern` matches the page. Backward compat at file level — `_run_script` untouched.

---

## Integration design

### Subprocess shape — same shape, new Python module

`routes/execution.py:1018` already spawns `python -m engine.runner_worker`. Add
`engine/walkthrough_runner.py` (~600 LOC) mirroring `automation_runner.py`'s
public surface (`run() → RunReport`) but executing a walkthrough instead of TC
scripts. `runner_worker.py` dispatches by new `mode` field in config JSON
(`"tc_driven"` default, `"walkthrough"` new). Reuses existing `sync_playwright`
import, run-id, retention purge, live filmstrip, `result.json` writer.

**No Node, no npm, no extra browser binaries.**

### TC/CL binding — **URL-pattern (default)** + "ignore TCs" fallback

For each page the walkthrough visits, it runs:

1. **TFWefloLab-style heuristic battery** (broken-image, dropdowns, forms, CTAs, axe, console-error humaniser).
2. **Any TC whose `url_pattern` matches the current URL** — those TCs run through the *existing* `_run_script` so step list executes deterministically and gets full TC-ID traceability.

URL-pattern beats AI-matching (deterministic, debuggable, no API cost) and
beats section-binding (sections aren't currently tagged on `test_case`).

### Data-model change

Two new fields on `test_case`:
```
+ url_pattern: str = ""              # regex/glob; empty = always run when walkthrough lands
+ trigger: str = "manual"            # manual | walkthrough_url_match | always
                                      # default "manual" preserves today's behaviour
```

Migration is additive — older TCs default `trigger="manual"` and never auto-fire.

### Result mapping

Walkthrough findings flow through `engine/bug_report.create_bug_from_failed_item`
(`bug_report.py:110`) using a new defect-class table extension in
`bug_template.py::CLASS_SEVERITY`:

```python
"broken_image":        "Major"       # routes to "Images" area weight
"hamburger_dead":      "Critical"    # routes to "navigation"
"axe_serious":         "Major"
"axe_critical":        "Critical"
"clipped_text":        "Minor"
"placeholder_social":  "Major"
"cta_no_destination":  "Major"
"console_js_error":    "Major"
```

Bug fingerprinting follows TFWefloLab's `server.js:540` (severity + area +
message-normalised + element).

TestForTge does NOT currently dedup across envs — add `engine/walkthrough_dedup.py`
(~80 LOC) and call once at end of per-env loop in `routes/execution.py:1097+`.

### Config-JSON shape

`_pending/<cfg>.json` — add three fields, leave everything else alone:
```json
"mode": "walkthrough",                  // or "tc_driven" (default)
"walkthrough": {
  "max_pages": 6,
  "max_form_fills": 5,
  "device_timeout_ms": 480000,
  "axe_enabled": true
},
"tc_binding": "url_pattern"             // or "ignore"
```

### UI

Single radio + `<details>` block in `templates/test_execution.html` above the
env-type tabs (around line 660):
- ◉ "TC / Checklist-driven" (default, current behaviour)
- ◯ "QA walkthrough — autonomous + opportunistic TC matching"

**Do NOT** add walkthrough as a fifth env-type tab — env types are about *where*
you test (Web/Mobile/iOS/Android), not *how*. Walkthrough mode is orthogonal:
runs *inside* each selected env.

### Migration

Gated behind `WALKTHROUGH_MODE_ENABLED` env var, default `false` on Render.
Existing TC-driven runs don't deserialise the new fields. Existing TCs default
`trigger="manual"`. Zero regression surface for current happy path.

---

## What will break and how to mitigate

| Risk | File:line | Mitigation |
|---|---|---|
| Subprocess wall-clock — 8 min/device × 4 envs × 2 sites = 64 min | `routes/execution.py` polling loop | New `walkthrough.device_timeout_ms` (default 480000); outer wall-clock kill |
| Playwright version conflict | N/A under Python port (rejected if Node) | Python port elimimates risk; if Node: 2 managed caches, `playwright install --with-deps` needs OS packages |
| Reporting paths must match | `automation_runner.py:344-:347` | `walkthrough_runner` reuses `_purge_old_automation_runs` + same `run_dir` shape |
| Bug-dedup `create_bug_from_failed_item` expects `tc_id` | `bug_report.py:110` | Synthesise `tc_id = f"WALK-{slug(area)}-{n}"`, add `bug.source = "walkthrough"` |
| Multi-env loop produces N copies of `html-has-lang` | `routes/execution.py:1097+` | Cross-env dedup pass (`walkthrough_dedup.py`) |
| Live-view filmstrip stalls during 8s scrolls | `automation_runner.py::_live_pump` | Call `_live_pump` from inside scroll tick loop |
| Headed mode × >2 envs × walkthrough = unwatchable | `routes/execution.py:1097+` | UI warns if combo selected |
| Findings rendering different from TC results | `templates/test_execution.html` | New "Findings" subtab inside each env panel (~200 lines Jinja, mirror `renderWalkthroughCard`) |
| Recent-projects widget needs activity bump | `routes/execution.py` `last_run_at` writes | Ensure `walkthrough_runner` updates same fields |
| Credentials still needed for TC-matching mode | `execution.py:981–:1006` | Continue passing; walkthrough ignores when no TC fires |

**Risk to current TC-driven path:** **low** — feature flag, additive code, no
edits to `_run_script`. Mandatory edits: `runner_worker.py` (mode dispatch),
`routes/execution.py` (UI radio + config field), `templates/test_execution.html`
(UI radio + findings subtab). All additive.

---

## Estimate

Mid-level Python dev, target = green CI, deployed to staging.

| Slice | Hours |
|---|---:|
| `engine/walkthrough_runner.py` (Python port of TFWefloLab heuristics) | 16 |
| `engine/walkthrough_dedup.py` + bug-template extensions | 4 |
| `runner_worker.py` mode dispatch + config schema | 2 |
| `routes/execution.py` UI plumbing + outer timeout + feature flag | 4 |
| `templates/test_execution.html` radio + findings subtab | 4 |
| `test_case` model: `url_pattern` + `trigger` fields + defaults | 3 |
| `engine/site_crawler.py` glue (sitemap discovery exists; just wire) | 2 |
| axe-core in Python via `page.add_script_tag` (no native lib) | 3 |
| Unit tests (mocked playwright, fixture site) + integration | 8 |
| Documentation + Guide page | 2 |
| Buffer (disk pressure, retention tuning) | 4 |
| **Total** | **~52 h** (~7 working days) |

### PR plan — three small PRs

1. **PR-1** — `walkthrough_runner.py` scaffold + dispatch in `runner_worker.py` behind feature flag. ONLY runs walkthrough via debug endpoint. No UI. **~18 h. Mergeable, zero user-visible change.**
2. **PR-2** — `test_case` schema extension (`url_pattern`, `trigger`) + dedup + bug-template extensions. Still no UI; tested via debug endpoint. **~12 h.**
3. **PR-3** — UI radio in `test_execution.html`, findings subtab, Guide entry, feature flag flipped on. User-visible. **~14 h + 8 h buffer.**

Staging means PR-1 and PR-2 ship without disrupting active /test-execution path.

---

## Verdict

Adopt **(B)** but reimplement TFWefloLab's walkthrough heuristics natively in
Python on top of TestForTge's existing `site_crawler.py` and `automation_runner.py`
— do not shell out to the Node spec. TC/CL steering is best done with a
`url_pattern` field on `test_case`, with walkthrough mode firing matching TCs
through the existing `_run_script` so today's deterministic TC pipeline stays
untouched. Risk to current Test Execution behaviour is low (additive code paths,
feature-flagged, no edits to `_run_script`), and the integration buys TestForTge
eight new defect classes (broken images, dead hamburgers, axe a11y, dropdown
probes, CTA audits, search probes, footer sanity, console-error humanisation)
plus cross-env bug dedup — none of which exist today.
