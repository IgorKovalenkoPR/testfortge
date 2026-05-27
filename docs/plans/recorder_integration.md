# Web Recorder Integration — TestLum-Inspired Recording for Manual QA

**Source inspiration:** TestLum IDE Recorder (https://github.com/TestlumFramework/Testlum).
**Target:** TestForTge `engine/automation_qa.py` + `engine/automation_runner.py`
+ `engine/live_executor.py` + Test Cases UI.

**Verdict:** **demo-first vertical slice** — ship Recorder MVP behind a feature
flag in one PR, then layer multi-locator infrastructure under it, then close
the loop with Assertion Mode. Three small PRs, ~70 h total.

**Recommended order:** PR-B (Recorder MVP) → PR-A (multi-locator) → PR-C (Assertion).

---

## Why this and not the full TestLum surface

TestLum bundles XML DSL + Java engine + Recorder + multi-protocol (DB/Kafka/AWS)
+ desktop IDE. Of that, only **Recorder + multi-locator Page Object** addresses
a real TestForTge gap: today `engine/automation_qa.py:parse_manual_step` *guesses*
locators from narrative text (`"Click Submit"` → `role=button[name=/submit/i]`),
which is the largest flake source in Live Executor. Recorder replaces guessing
with deterministic capture; multi-locator gives the runner alternates when the
primary drifts. The rest of TestLum (XML DSL, Java engine, DB/queue blocks) is
out of scope — we keep Python + JSON + Playwright.

---

## Honest take — the architectural unknown

Stage 3's `OomGuard` caps the parent process at **`MEMORY_BUDGET_MB = 400`** on
Render free tier (512 MB ceiling). Spike (2026-05-27) confirmed the guard is
*advisory* (polls parent RSS only, executor decides graceful exit) — a codegen
subprocess Chromium would live in a separate PID and slip past the guard, but
total container RSS still has to clear ~480 MB before OS-level OOM kill. A
short recording session next to an active runner is *tight but possible*
(~150 MB runner + ~200 MB recording Chromium + ~80 MB kernel ≈ 430 MB).

**Resolution:** Recorder ships as a **local CLI helper** as the default path
— eliminates the memory tightrope, gives testers a real Chromium on their
machine, and reuses the existing MCP server. CLI uses Playwright codegen and
posts parsed steps to TestForTge via MCP (PRs #15 / #17 shipped write-tools
and HTTP/SSE). The web UI surfaces a "🎬 Record" button that copies a one-line
command to the clipboard with pre-filled `--tc` / `--project` args.

**v2 (post-pilot) options to revisit:** (a) server-side `/test-cases/<id>/record`
route with a per-project recording lock and 10-min auto-close timeout — viable
on Render with care, and (b) browser extension — eliminates the `pip install`
step but costs Chrome Web Store maintenance.

---

## PR-B — Sprint 1: Recorder MVP (~32 h)

### Goal

One-line CLI `tfg record --tc SC1_002 --project <pid>` opens Chromium, tester
clicks through the scenario, on browser close the captured steps land in
TestForTge against the target TC.

### Files

#### New

- **`engine/recorder.py`** (~80 LOC) — `run_codegen(url, target_path) -> Path`
  wrapper around `playwright codegen --target python-async --output ...`.
  Returns when subprocess exits. Times out at 30 min (config: `RECORDER_TIMEOUT_S`).
- **`engine/recorder_parser.py`** (~120 LOC) — parses codegen Python output
  into `list[AutomationStep]`. Handles `page.goto`, `page.click`, `page.fill`,
  `page.press`, `page.select_option`, `page.locator(...).click()`. Quoted-string
  extraction reuses `_extract_quoted` from `engine/automation_qa.py`.
- **`tools/tfg_record.py`** (~100 LOC, packaged as `pip install -e .` entry point
  or stand-alone .py) — argparse CLI. Flow:
  1. Resolve TC: GET `/api/projects/<pid>/test-cases/<tc_id>` via MCP HTTP.
  2. Pick `base_url` from project (`session['project_setup']['base_url']`).
  3. Run `engine.recorder.run_codegen(base_url, tmp_file,
     test_id_attributes="data-testid,data-test,data-qa")` — codegen emits
     `page.get_by_test_id(...)` selectors for elements that carry any of the
     listed attributes, which costs nothing extra here and pre-stages
     PR-A's locator hierarchy (testid sits at the top of the ranking).
  4. Parse → `list[AutomationStep]` → serialise.
  5. POST to MCP server `record_steps_attach(project_id, tc_id, steps)`.
  6. Print summary: "Attached N steps to SC1_002. View at <TestForTge URL>/test-cases#SC1_002".
- **`mcp_server/server.py`** — new `@mcp.tool() record_steps_attach(project_id,
  tc_id, steps: list[dict]) -> dict` function appended after
  `trigger_test_execution` (~40 LOC). Existing convention keeps every MCP tool
  in `server.py`, no `tools/` subdir. Updates `TestCase.automation_steps_json`
  via a new `engine.db.update_tc_automation_steps()` helper. The 3-concurrent
  cap (`MCP_MAX_CONCURRENT_RUNS`) does *not* apply — it gates execution
  triggers, not data writes.
- **`tests/test_recorder_parser.py`** (~120 LOC) — 6 golden fixtures: a codegen
  output file + expected `list[AutomationStep]` for each (goto+click+fill,
  press keys, dropdown select, nested locators, fill with quoted apostrophes,
  multi-page). Pure-function tests, no Playwright runtime.
- **`tests/test_record_steps_mcp.py`** (~60 LOC) — POST to MCP `record_steps_attach`
  → `TestCase.automation_steps_json` populated; cross-project leak guard
  (project A pid can't write to project B TC).

#### Modified

- **`engine/db.py`** (Sprint 1 of Stage 3 added `automation_runs`; same pattern):
  add column `automation_steps_json TEXT NULL` to `TestCase` via Alembic-style
  inline migration in `init_db()`.
- **`engine/automation_runner.py:_run_script`** — when `tc.automation_steps_json`
  exists and non-empty, prefer those steps over `tc_to_script(tc, ...)` heuristic.
  Single `if` branch, no behaviour change for TCs without recorded steps.
- **`templates/test_cases.html`** — per-TC row: add **🎬 Record** button that
  reveals a small inline panel with the CLI command pre-filled, an
  "📋 Copy command" button, and a "Has recorded steps ✓" badge when
  `tc.automation_steps_json` is populated.
- **`README.md`** — new section *"Recording test steps"* with CLI install +
  feature flag note.

### Sketch — `engine/recorder_parser.py`

```python
# engine/recorder_parser.py
import ast
import re
from engine.automation_qa import AutomationStep

# Codegen emits one statement per action, e.g.:
#   page.goto("https://app.example.com/login")
#   page.get_by_role("button", name="Sign in").click()
#   page.get_by_label("Email").fill("user@example.com")
#   page.get_by_placeholder("Password").press("Tab")

_ACTION_NODES = {"click", "fill", "press", "select_option", "check", "uncheck"}
_NAV_NODES    = {"goto"}

def parse_codegen_output(src: str) -> list[AutomationStep]:
    tree = ast.parse(src)
    steps: list[AutomationStep] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        method = node.func.attr
        if method in _NAV_NODES:
            url = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else ""
            steps.append(AutomationStep(action="goto", target=url, raw=ast.unparse(node)))
        elif method in _ACTION_NODES:
            locator = _extract_locator(node.func.value)
            value = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else ""
            steps.append(AutomationStep(
                action=_codegen_to_internal(method),
                target=locator,
                value=value,
                raw=ast.unparse(node),
            ))
    return steps

def _extract_locator(value_node) -> str:
    """Walk back through .get_by_role(...).get_by_text(...) chains."""
    # see fixtures for exact mapping; codegen produces stable patterns
    ...
```

### Tests

1. Codegen golden → 8 steps parsed → identical `AutomationStep[]` (deep equals).
2. CLI dry-run (`--no-browser`) over a captured fixture → MCP `record_steps_attach`
   called with expected payload.
3. `_run_script` with recorded steps → ignores `tc.test_steps` text, walks
   `automation_steps_json`.
4. Backward compat: TC without `automation_steps_json` → identical behaviour
   to today (regression guard).
5. Feature flag `RECORDER_ENABLED=0` (default) → CLI shows guidance message
   and exits 1; MCP `record_steps_attach` returns 403.

### Risks

- **Codegen output drift across Playwright versions.** Lock minor version in
  `requirements.txt`; AST parser pinned to ≥ 1.40 codegen output shape. Add
  CI job that runs codegen against a stable demo page and diffs the AST shape.
- **Tester machine setup.** Codegen needs `playwright install chromium`
  (~150 MB). Add `pip install testfortge-record` package that runs it on
  first invocation. Document in README.
- **MCP auth.** `record_steps_attach` is a write tool — gate behind PR #15's
  existing API token; reject on missing project membership (post-Sprint-4
  roles will tighten further).

**Estimate:** 32 h. **Depends on:** nothing new — MCP write tools (PR #15)
and HTTP/SSE (PR #17) already shipped.

---

## PR-A — Sprint 2: Multi-locator ranking + Page Object library (~22 h)

### Goal

Replace `AutomationStep.target: str` (one locator) with a primary + ordered
alternates. Runner falls back automatically; a project-scoped Locator table
remembers which strategy actually worked last time.

### Files

#### New

- **`engine/locator_registry.py`** (~150 LOC):
  ```python
  @dataclass
  class LocatorCandidate:
      strategy: str  # "testid"|"id"|"role"|"label"|"text"|"placeholder"|"css"|"xpath"
      value: str
      score: int     # priority, 100=testid, 80=id, 60=role, 40=text, 20=css, 10=xpath

  def candidates_from_codegen(raw_locator: str) -> list[LocatorCandidate]: ...
  def record_success(project_id, label, strategy) -> None: ...
  def record_failure(project_id, label, strategy) -> None: ...
  def best_alternates(project_id, label) -> list[LocatorCandidate]: ...
  ```
- **`tests/test_locator_registry.py`** (~120 LOC) — ranking, success/failure
  learning loop, project isolation.

#### Modified

- **`engine/automation_qa.py:AutomationStep`** — add fields:
  ```python
  target_alternates: list[str] = field(default_factory=list)
  locator_label: str = ""  # tester-friendly key, e.g. "login.signIn"
  ```
  `parse_manual_step` keeps producing single-target steps (unchanged for
  text-authored TCs); `recorder_parser` populates alternates from candidates.
- **`engine/db.py`** — new table:
  ```sql
  CREATE TABLE locator (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id VARCHAR(32) NOT NULL,
    label VARCHAR(120) NOT NULL,
    candidates_json TEXT NOT NULL,
    last_success_strategy VARCHAR(20),
    success_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    last_seen TIMESTAMP WITH TIME ZONE NOT NULL,
    UNIQUE (project_id, label)
  );
  ```
- **`engine/automation_runner.py:_run_script`** — wrap each step's locator
  resolution in `try_locator_chain(page, step)`:
  ```python
  def try_locator_chain(page, step):
      tried = []
      for target in [step.target, *step.target_alternates]:
          try:
              loc = page.locator(target)
              loc.wait_for(state="visible", timeout=2000)
              if step.locator_label:
                  record_success(project_id, step.locator_label,
                                 _strategy_of(target))
              return loc
          except TimeoutError:
              tried.append(target)
              continue
      if step.locator_label:
          record_failure(project_id, step.locator_label, "all")
      raise AssertionError(f"All locators failed for step: {step.raw}\n"
                          f"Tried: {tried}")
  ```
- **`engine/recorder_parser.py`** (PR-B output) — emit 3-5 candidates per
  click/fill via `LocatorCandidate`. Pull from codegen's `get_by_role` /
  `get_by_label` / `get_by_text` chains + DOM attributes scraped via a
  Playwright `evaluate()` hook injected during codegen.

### Tests

1. Primary locator fails → first alternate succeeds → step status `passed`,
   `locator.last_success_strategy` updated.
2. All alternates fail → `AssertionError`, run continues with next step,
   `locator.fail_count` incremented.
3. Project A's locator for label `"login.signIn"` invisible to project B.
4. Recorder writes 5 candidates per element from codegen + DOM probe fixture.
5. Existing TCs (no `target_alternates`) → behaviour unchanged.

### Risks

- **Codegen doesn't expose alternates natively** — Playwright `getByRole` is
  one of many strategies. To enumerate, inject a small `evaluate()` script
  during recording that collects `data-testid` / `id` / `aria-label` /
  computed XPath off the *next clicked element* and posts them to a sidecar
  file. Adds ~200 LOC to `engine/recorder.py`.
- **Locator table grows unbounded.** Add daily prune in the existing snapshot
  worker (`app.py` already has a daily catch-up thread): drop rows with
  `success_count + fail_count < 2` older than 90 d.
- **`last_success_strategy` race** under concurrent runs of the same TC —
  use `UPDATE ... WHERE strategy = ?` so two parallel updates don't clobber
  each other's last-good signal (single-statement atomicity is enough; no
  explicit lock).

**Estimate:** 22 h. **Depends on:** PR-B (uses `AutomationStep` + recorder
output).

---

## PR-C — Sprint 3: Assertion Mode + per-step kind editor (~16 h)

### Goal

Separate `action` steps from `assertion` steps in the model, expose a per-step
**Action / Assert visible / Assert text / Assert URL** dropdown in the TC editor,
and let the Recorder CLI capture assertions via a hot key during recording.

### Files

#### New

- **`tests/test_assertion_steps.py`** (~120 LOC) — runner behaviour for each
  assertion kind: visible (locator passes within 5 s), text (`get_by_text`
  resolves), URL (`page.url` matches via `fnmatch`).

#### Modified

- **`engine/automation_qa.py:AutomationStep`** — add:
  ```python
  kind: Literal["action", "assertion"] = "action"
  assertion_type: Literal["", "visible", "text", "url"] = ""
  ```
  Default keeps every existing step in the `action` lane.
- **`engine/automation_runner.py:_run_script`** — branch on `step.kind`:
  ```python
  if step.kind == "assertion":
      if step.assertion_type == "visible":
          loc = try_locator_chain(page, step)
          loc.wait_for(state="visible", timeout=5000)
      elif step.assertion_type == "text":
          expect(page.get_by_text(step.value, exact=False)).to_be_visible(timeout=5000)
      elif step.assertion_type == "url":
          expect(page).to_have_url(re.compile(fnmatch.translate(step.target)), timeout=5000)
  else:
      # existing action dispatch — click/fill/select/...
      ...
  ```
- **`tools/tfg_record.py`** — add `--assert-mode` flag and `Ctrl+Shift+A`
  in-browser hot key (injected via codegen pre-script) that toggles capture
  mode. Toggle state shown in a small overlay div.
- **`templates/test_cases.html`** — per-step row in the editor: new dropdown
  with 4 options, value field reshapes per assertion type (target=URL pattern
  for `url`, value=expected text for `text`, target=label for `visible`).
- **`static/js/test-execution.js`** — render assertion steps with a distinct
  bullet (▸ for action, ◇ for assertion) in the live view.

### Tests

1. `assertion + visible` on element that appears within 5 s → step passes.
2. `assertion + visible` on missing element → step fails, run continues.
3. `assertion + text` with substring match → passes.
4. `assertion + url` with glob `https://app.example.com/dashboard*` → passes
   regardless of query string.
5. Editor dropdown round-trip: change kind to assertion → POST → reload →
   correct kind/type persisted.
6. Backward compat: every existing TC step deserialises with
   `kind="action"` default.

### Risks

- **Live executor live-feed JSON shape** — `info.json.ts` ring (Stage 3
  contract) doesn't care about `kind`, but `templates/test_execution_live.html`
  rendering does. Add a defensive default in the template so older runs
  (pre-PR-C) without `kind` still render.
- **Recorder hot-key capture inside codegen** — codegen has its own keyboard
  hooks; need to verify Ctrl+Shift+A doesn't collide. If it does, fall back
  to a small overlay button injected via `--init-script`.

**Estimate:** 16 h. **Depends on:** PR-B (recorder shape), PR-A
(`try_locator_chain` used by `assertion + visible`).

---

## Sequencing + total

| Order | PR | Hours | Visible win |
|---|---|---:|---|
| 1 | PR-B Recorder MVP | 32 | Day ~5: working `tfg record` CLI + Record button in TC list |
| 2 | PR-A Multi-locator | 22 | Day ~10: invisible — flake rate of recorded TCs drops in metrics |
| 3 | PR-C Assertion Mode | 16 | Day ~15: per-step kind dropdown + assertion hot key in recorder |
| **Sum** | | **70** | |

### What to defer if pilot on fire

- **PR-C is the most self-contained.** If schedule slips, ship B+A only;
  assertions stay as today (synthetic `expect_text` from `expected_result`).
- **PR-A's Locator DB** can degrade to "primary-only + log alternates":
  keep `target_alternates` in the step model, skip the DB table and learning
  loop, revisit when flake metrics call for it. Saves ~10 h.
- **PR-B's MCP path** can fall back to writing a JSON file to the project's
  storage dir and adding a "📂 Import recording" button in the TC editor —
  sidesteps MCP auth entirely if PR #15 / #17 turn out to have gaps the
  recorder triggers. Saves ~4 h.

### Feature flag rollout

- All three PRs gated on `RECORDER_ENABLED=0` (default off).
- Pilot project: one internal project (Swatchbox or the TestForTge dogfood
  project itself). Flip the flag for that project's owner SID only via a new
  `engine/feature_flags.py` per-project override map.
- Promote to global default after 2 weeks of pilot with no P1 incidents.

---

## Out of scope (explicit)

- **Browser extension recorder.** Considered for v2 if the CLI proves too
  high-friction. Adds Chrome Web Store review + MV3 lifetime maintenance.
- **Visual baseline diff with tolerance %.** TestLum's 95%-match screenshot
  assertion. Tracked for Tier-2 backlog; depends on `pixelmatch-py` and a
  baseline-management UI that needs its own design pass.
- **Editable timeline UI** with drag-to-reorder steps. Today's `test_cases.html`
  textarea suffices for the pilot. Revisit after manual QA dogfoods PR-B.
- **Mobile recording via Appium.** Different runtime, different device
  provisioning stack. Out of pilot scope.
- **XML scenario DSL.** TestLum's storage format. We keep JSON in
  `automation_steps_json`.
