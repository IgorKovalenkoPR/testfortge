# Sprint 2 — Refactoring Monoliths

**Goal:** Decompose three mega-files and unify a duplicated code path so future
work has a clean substrate.

**Estimate:** 36–49 h (midpoint ~42 h). One dev tight in 2 weeks at 70%
capacity, or two devs in parallel (A: 2.1+2.3 ≈ 20 h, B: 2.2+2.4 ≈ 22 h).

**Recommended order:** 2.1 → 2.3 → 2.2 → 2.4 (not the listed order).

**Suggested mode:** PR-per-task, four PRs. Each task internally has 3–8 commits.

---

## Task 2.1 — Split `routes/execution.py` (2 456 LOC)

**Why first:** largest file, most active bug-cluster surface, source of session-state
spaghetti. Doing it first reduces merge-conflict risk against later work.

### Target structure

| File | Role | LOC target |
|------|------|------|
| `routes/execution.py` | Flask handlers; session reads/writes; rendering; delegates to engines | 400–550 |
| `engine/execution_orchestrator.py` | Build runner_kwargs + config_payload, run-id, pre-flight info.json, Popen of `engine.runner_worker`, polling helpers | 500–650 |
| `engine/execution_postprocessor.py` | `_dedupe_bugs_by_root_cause`, `_reconcile_with_automation`, partial-payload reconstruction, per-env loop body | 400–550 |
| `engine/test_session_manager.py` | Form-parsing primitives, env_string builder, credentials adapter; returns typed `TestExecutionConfig` | 250–350 |
| `engine/execution_models.py` | Shared dataclasses (`TestExecutionConfig`, `RunRecord`, `RunSummary`) | ~80 |

### Function migration table

| Current `routes/execution.py` | Function | Lines | New location |
|---|---|---|---|
| top | `_bug_dict_to_db_row` | 45–76 | `execution_postprocessor.py` |
| top | `_persist_bug` | 79–92 | stay (Flask session/g/ensure_active_project) |
| top | `_dedupe_bugs_by_root_cause` | 96–176 | `execution_postprocessor.py` |
| top | `_reconstruct_partial_payload` | 179–319 | `execution_orchestrator.py` |
| top | `_reconcile_with_automation` | 322–477 | `execution_postprocessor.py` |
| top | `_maybe_restore_pack_from_db` | 480–576 | stay (touches session) |
| POST body | form/env parsing | 604–778 | `test_session_manager.parse_execution_form` |
| POST body | pre-flight info.json + subprocess dispatch | 837–1058 | `execution_orchestrator.dispatch_playwright_run` |
| POST body | per-env loop | 1097–1352 | `execution_postprocessor.run_environments` |
| `test_execution_results` | full body | 1973–2365 | thin route → orchestrator + postprocessor |
| `test_execution_diag` | full body | 1616–1760 | stay (operator diagnostic) |
| `test_execution_live`, `_live_*`, `_run_status` | 1762–1971 | stay (file-serving) |
| `test_execution_auto_run` | 1491–1608 | stays as Flask handler |

### Import graph (acyclic)

```
engine/execution_models     (leaf)
engine/test_session_manager → execution_models, engine.test_credentials
engine/execution_orchestrator → execution_models, engine.runner_worker, engine.qa_testers
engine/execution_postprocessor → execution_models, engine.bug_template, engine.bug_report, engine.qa_testers.execute_items, engine.db
routes/execution → all four + engine.db + routes._shared
```

**No engine imports `routes/*`** — today the orchestrator would reference
`routes.automation.STORAGE_ROOT`, creating a cycle. Pre-step: move `STORAGE_ROOT`
to `engine/automation_paths.py`.

### Commit plan (8 commits, each green)

1. **Move `STORAGE_ROOT` to `engine/automation_paths.py`** with re-export shim in `routes/automation.py`.
2. **Introduce `engine/execution_models.py`** with dataclasses, nothing imports yet.
3. **Extract pure post-processing** (`_dedupe_bugs_by_root_cause`, `_reconcile_with_automation`) with re-export shims in `routes/execution.py`. Unit tests for both.
4. **Extract `_reconstruct_partial_payload`** to orchestrator with shim. Unit test against fixture run dir.
5. **Extract form-parsing** to `test_session_manager`. Replace lines 604–778 with single `parse_execution_form(form, urls)` call. Test: round-trip multipart form, assert env_string for each env_type matches.
6. **Extract subprocess dispatch** — pull lines 837–1058 into `dispatch_playwright_run`. Returns `(config_id, warnings)`.
7. **Extract per-env loop** — both `test_execution_page` (1097–1352) and `test_execution_results` (2090–2318) now call `run_environments`. Eliminates ~250 LOC of duplication. Biggest-risk step.
8. **Final cleanup** — delete shim re-exports, confirm `routes/execution.py < 600` lines.

### Risks

- Sync POST loop (1097–1352) doesn't call `_reconcile_with_automation`;
  `test_execution_results` does (line 2138). The function already short-circuits
  on empty `automation_assets` (line 364), so merge is safe — preserve the
  short-circuit.
- `running_bugs` list rebuild on exception (lines 1268–1272 and 2225–2228) is
  identical. Move into shared loop.
- `dashboard.py` reads `session["test_runs"]` — keep `run_record` field shape
  unchanged.

### Tests

Existing: `tests/test_te_auto_run.py`, `test_te_e2e_playwright.py`,
`test_te_upload_extras.py`, `test_integration.py`. New:
- `tests/test_execution_postprocessor.py` — dedup grouping, reconciliation P/F matrix.
- `tests/test_test_session_manager.py` — form parsing per env_type.
- `tests/test_execution_orchestrator.py` — happy path + phase_error stamping.

**Estimate:** 14–18 h.

---

## Task 2.2 — Split `engine/qa_persona.py` (2 254 LOC → ~700)

**Goal:** Move every hardcoded ISTQB checklist and test-case template into
versioned YAML so QA leads update content without touching Python.

### Target structure

```
engine/
  qa_persona.py               (~600–800 LOC — orchestration only)
  qa_utils.py                 (~250 LOC — is_instruction, detect_flows, slug helpers)
  qa_knowledge_loader.py      (~200 LOC — load, cache, validate)
  qa_knowledge/
    schema/checklist.schema.json
    schema/testcase.schema.json
    checklists/{web_general,auth,payment,search,forms,crud_*,navigation}.en.yaml
    testcases/{web_general,auth,payment,search,forms,navigation,seo,usability,localization}.en.yaml
tools/migrate_qa_knowledge.py  (one-shot Python→YAML converter; deleted after use)
```

### YAML schema sketches

`engine/qa_knowledge/checklists/auth.en.yaml`:
```yaml
version: 1
area: auth
locale: en
sections:
  - name: "Login Form — UI"
    prefix: LGN
    items:
      - objective: "Verify that the login form is displayed with email/username and password fields"
        category: Positive
        priority: High
      - objective: "Verify that the password field is masked"
        category: Positive
        priority: High
```

`engine/qa_knowledge/testcases/auth.en.yaml`:
```yaml
version: 1
area: auth
locale: en
default_section: Authentication
cases:
  - summary: "Verify that login is completed successfully with valid credentials"
    preconditions: "Application is accessible. Test user account is created."
    steps:
      - "Navigate to the login page"
      - "Enter a valid email address"
      - "Enter a valid password"
      - "Click the 'Login' / 'Sign In' button"
    test_data: "Email: testuser@example.com, Password: ValidPass123!"
    expected_result: "User authenticated. Redirected to dashboard. Session cookie set."
    category: Positive
    priority: High
    section: Authentication
```

### Function migration (highlights)

| Current | Lines | New location |
|---|---|---|
| `is_instruction`, `detect_flows` | 145–181 | `qa_utils.py` |
| `_web_general_checks` | 375–474 | `qa_knowledge/checklists/web_general.en.yaml` |
| `_auth_checks` | 477–545 | `qa_knowledge/checklists/auth.en.yaml` |
| `_search_checks` | 548–587 | `qa_knowledge/checklists/search.en.yaml` |
| `_forms_checks` | 588–623 | `qa_knowledge/checklists/forms.en.yaml` |
| `_crud_checks(create|read|update|delete)` | 624–707 | one file per op |
| `_payment_checks` | 708–743 | `qa_knowledge/checklists/payment.en.yaml` |
| `_navigation_checks` | 744–750 | `qa_knowledge/checklists/navigation.en.yaml` |
| `_AREA_CHECKLIST_FN` dict | 760–771 | `qa_knowledge_loader.get_checklist(area)` |
| `_SECTION_PREFIXES` | 778–830 | merge into `prefix:` per section |
| `_auth_test_cases` | 1010–1086 | `qa_knowledge/testcases/auth.en.yaml` |
| `_search/forms/payment/navigation/web_general_test_cases` | 1089–1357 | `qa_knowledge/testcases/*.en.yaml` |
| `_seo/usability/localization_test_cases` | 1657–1819 | YAML (localization keeps code for `{domain}` placeholder fill) |
| `analyze_input`, `generate_professional_*`, `_browser_findings_to_*`, `_flow_test_cases` | various | stay (orchestration, not templates) |

### Loader API

```python
class QAKnowledgeLoader:
    def __init__(self, root: pathlib.Path, default_locale: str = "en") -> None: ...
    def get_checklist(self, area: str, locale: str | None = None) -> dict[str, list[CheckItem]]: ...
    def get_test_cases(self, area: str, locale: str | None = None) -> list[TCTemplate]: ...
    def section_for_area(self, area: str) -> str: ...
    def get_section_prefix(self, section_name: str) -> str | None: ...
    def reload(self) -> None: ...

LOADER = QAKnowledgeLoader(Path(__file__).parent / "qa_knowledge")
```

Loaded once on app boot. English fallback when non-English missing.

### Validation

`jsonschema` over `engine/qa_knowledge/schema/*.json`. Runs at loader construction
time — malformed YAML fails app boot, not a user request. CI guard:
`python -c "from engine.qa_knowledge_loader import LOADER; assert LOADER.areas()"`.

### Migration

`tools/migrate_qa_knowledge.py` imports current `qa_persona`, dumps YAMLs by
introspecting `_AREA_*_FN`. Round-trip test asserts deep-equal between YAML and
Python. Script deleted after merge.

### Commit plan (6 commits)

1. Loader + schema + first area (`auth`) migrated. Test loader output equals legacy Python output.
2. Migrate remaining areas in 1–N commits.
3. Extract utils to `qa_utils.py` with re-export shims. Grep affected: `routes/_shared.py:24`, `routes/generation.py`, `routes/chat.py`.
4. Delete deprecated Python functions (lines 375–851, 1010–1356, 1657–1819). File drops 2 254 → ~700 LOC.
5. Delete migration script + add CI guard.
6. `qa_knowledge/README.md` documenting schema for QA leads.

### Risks

- `engine/testcase_generator.py` consumes `_SECTION_PREFIXES` — re-point to `LOADER.get_section_prefix`.
- `engine/automation_qa.py`, `engine/qa_team_lead.py` may import `_AREA_KEYWORDS` — keep in `qa_persona.py` (only static *content* moves).
- YAML escaping for SQL-injection strings (`"' OR 1=1 --"`) requires `default_style='"'`.
- `tests/test_unit.py` lines 103–230 + `test_checkout_and_chatbot.py` exercise these — should pass unchanged (public API stable).

**Estimate:** 12–16 h.

---

## Task 2.3 — Unify estimation sync/async paths

**Goal:** Eliminate 200+ LOC duplicated feature-extraction logic, fix
`team_size` bug, remove dead JS.

### Target

New `engine/estimation_service.py` (~250 LOC):

```python
@dataclass
class EstimationInput:
    source_choice: str           # 'text' | 'attachment' | 'mockups' | 'url'
    url: str
    text_input: str
    figma_url: str
    mockup_context: str
    attachment_path: str
    mockup_paths: list[str]
    project_name: str
    rate_usd: float
    additional_platforms: int
    minutes_per_tc: int
    buffer: float
    primary_platform: str
    compatibility_rate: float
    bug_report_rate: float
    pm_overhead: float
    max_testing_stretch: float
    team_size: int               # NEW — read from form in both paths

@dataclass
class EstimationOutput:
    result_dict: dict
    extracted_text: str
    source_label: str
    source_ref: str
    warnings: list[str]

def run_estimation(inp: EstimationInput) -> EstimationOutput:
    """Single source of truth. Raises RuntimeError on hard failures."""
```

### Migration

- `routes/estimation.py::_estimation_run_inner` lines 365–520 → `run_estimation(_build_input(request))`.
- `routes/estimation.py::estimation_run_async._worker` lines 702–805 → same.
- `_build_input()` (new helper, ~40 LOC) reads `request.form`, saves uploads, returns `EstimationInput`.

### Bug fixes

1. **`team_size` missing in form.** Add `<input type="number" name="team_size" min="1" max="50" value="{{ last.team_size or 1 }}">` to `templates/estimation.html` near coefficient inputs (rows 64–104). Async path doesn't pass it today (lines 784–798) — fix.
2. **Dead progress-banner JS** at `templates/estimation.html` lines 709–777. References DOM IDs (`est-progress`, `est-progress-fill`) that don't exist. Real path is `est-gen-overlay` block at line 290+. Delete the dead script block.

### Commit plan (4 commits)

1. Add `engine/estimation_service.py` + `tests/test_estimation_service.py`. No route changes.
2. Switch sync route to service. Run `tests/test_estimation_unit.py` to confirm math identical.
3. Switch async route to service. Verify `team_size` is read from form (closes bug). Verify `_extracted_text` round-trip.
4. Add `team_size` form input + delete dead JS at lines 709–777.

### Risks

- Async worker swallowed exceptions silently in legacy fallback (lines 769–776).
  Move fallback inside `run_estimation` so both routes get it.
- `_persist_estimation` (lines 106–129) needs `features_count` — surface in `EstimationOutput`.
- `tests/test_estimation_unit.py` doesn't exercise `team_size` — add test that `team_size=4` produces non-zero Brooks penalty.

**Estimate:** 4–6 h.

---

## Task 2.4 — Extract inline JS from `templates/test_execution.html`

**Goal:** Move all four inline `<script>` blocks into static file so CSP
`script-src` can drop `'unsafe-inline'` for nonce.

### Migration

Four IIFE-scoped sections in `static/js/test-execution.js` (~600 LOC):
- Active-run progress widget (was lines 554–628)
- OS family mirror (was 801–824)
- Selection, env switcher, overlay (was 953–1269)
- Upload DnD + auto-run (was 1354–1435)

### Data shuttling Jinja → JS

| Today | New |
|---|---|
| `var infoUrl = "{{ url_for('test_execution_live_info') }}"` (565) | `<meta name="te-live-info-url" content="...">` |
| `dataset.runId` / `dataset.caseCount` | unchanged — already data-attrs |
| `t.get('te_overlay_slow', ...)` (1256) | `<meta name="te-i18n-overlay-slow" content="{{ t.get(...) }}">` |
| `t.get('te_results_saved', ...)` | same meta pattern |

### CSP migration

In `app.py::_apply_security_headers`:
```python
import secrets
nonce = secrets.token_urlsafe(16)
g.csp_nonce = nonce
resp.headers.setdefault(
    "Content-Security-Policy",
    "default-src 'self'; "
    "img-src 'self' data: blob:; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    f"script-src 'self' 'nonce-{nonce}' https://unpkg.com; "
    "connect-src 'self'; "
    "frame-ancestors 'none'",
)
```

Context processor exposes `csp_nonce`. Other inline `<script>` blocks get
`nonce="{{ csp_nonce }}"`.

### Inline `onclick=` audit (scope: only `test_execution.html`)

Three onclicks in `test_execution.html`:
- Line 276 — `onclick="toggleRunDetails('run-...')"`
- Lines 405–406 — `onclick="selectAll()"` / `selectNone()`

Replace with `data-action="toggle-run"` etc. + delegated listeners.

Other templates' onclicks (~21 instances across `checklist.html`, `recommendations.html`,
`requirements.html`, `test_cases.html`, `tools.html`, `user_stories.html`,
`test_metrics.html`) — backlog as Sprint 3 follow-up.

### Commit plan (3 commits)

1. Add `static/js/test-execution.js` with all four blocks + data-attr lift. Inline blocks commented out. Manual click-through. CSP unchanged.
2. Delete inline `<script>` blocks (554–628, 801–824, 953–1269, 1354–1435). Replace 3 `onclick=` with `data-action=` + listeners.
3. CSP nonce migration. Add `nonce="{{ csp_nonce }}"` to all other inline scripts site-wide. Test asserts header contains `nonce-` and not `'unsafe-inline'` in script-src.

### Risks

- Translatable strings (`te_overlay_slow` with apostrophe) — use `| e` for attribute escape.
- ~25 other inline `<script>` tags need nonce mark in Commit 3 — mechanical sed pass.

### Tests

- `tests/test_te_e2e_playwright.py` catches broken JS hookup.
- New `tests/test_csp_headers.py` — `script-src` contains `nonce-`, not `'unsafe-inline'`.

**Estimate:** 6–9 h.
