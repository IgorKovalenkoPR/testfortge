# QA Knowledge

YAML-backed checklist sections and test-case templates loaded at app
boot by `engine.qa_knowledge_loader.LOADER`. Edit these files to
change generated QA content — no Python required.

## Layout

```
engine/qa_knowledge/
  schema/
    checklist.schema.json   JSON Schema for files under checklists/
    testcase.schema.json    JSON Schema for files under testcases/
  checklists/
    <area>.<locale>.yaml    e.g. auth.en.yaml, payment.en.yaml
  testcases/
    <area>.<locale>.yaml    e.g. auth.en.yaml, seo.en.yaml
  style/
    house_style.yaml        writing standard for the LLM author agent
    coverage_rules.yaml     coverage model for the LLM author agent
```

`style/` is **not** loaded by `LOADER` and is not schema-validated — it
is prompt material, read as raw text by `engine.tc_author` and pasted
into a cached system block. See "Style assets" below.

The loader validates every file on construction. Bad YAML or a
violation of the schema raises `RuntimeError` at boot — broken
content never reaches a user request.

## Checklist file

```yaml
version: 1
area: auth                  # matches an area key in qa_persona._AREA_KEYWORDS
locale: en                  # 2-letter ISO; English is the fallback
sections:
  - name: "Login Form — UI" # the section column in the exported sheet
    prefix: LGN             # TestFort ID prefix (LGN_001, LGN_002, …)
    items:
      - objective: "Verify that the login form is displayed with …"
        category: Positive  # Positive | Negative | Edge Case | Security | Performance | Accessibility
        priority: High      # High | Medium | Low
```

`prefix:` is optional on a section — when omitted, the testcase
generator falls back to the `"GEN"` prefix. Sections that share a
prefix (e.g. `Login — Positive`, `Login — Negative`, `Login — Edge
Cases & Security` all use `LGN`) get continuous counters within
each generation run.

## Test-case file

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
    category: Positive       # required
    priority: High           # optional, defaults Medium
    section: Authentication  # optional, falls back to default_section
    testing_type: SEO        # optional — only set for SEO / Usability / Localization
                             # to bypass the heuristic in testcase_generator
```

## Style assets (`style/`)

Two files teach the **Test Case Author agent** (`engine/tc_author.py`)
how to write, and what to cover. Both were reverse-engineered from a
real accepted client deliverable — the Odoo Test Plan spreadsheet, 41
test sheets / 4,808 test-case rows / 8 modules — and every rule carries
the measured pattern that backs it as an inline `evidence:` note.

| File | Answers |
|---|---|
| `house_style.yaml` | **How** to write a case: title grammar, precondition idiom, step verbs, expected-result voice, section naming, category rules, anti-patterns. |
| `coverage_rules.yaml` | **Which** cases must exist: per-surface control enumeration, per-control-type scenario sets, mandatory positive/negative pairings, cross-cutting sweeps. |

Editing either changes generated output with no Python change. They are
loaded as **raw text**, not parsed YAML, because the `evidence:` comments
are the part that stops the model treating a house convention as
negotiable.

Two rules from `house_style.yaml` are also enforced in code, so they hold
on every path including the no-API-key fallback:

- **Declarative expected results.** `should` / `must` / `shall` are
  banned — they leave the tester unable to decide pass from fail. The
  reference corpus never uses one as the assertion verb.
  `engine.tc_author.normalise_expected_result` rewrites them, and
  `engine.qa_team_lead` review rule 2 applies it.
- **Negative cases assert the feedback too.** A refusal case must state
  both that the action was blocked *and* what the user sees. On the
  corpus's dedicated error-message sheet, 8 of 25 rows failed precisely
  on missing or misdirected feedback. Review rule 8 repairs cases that
  only assert the refusal.

`tests/test_tc_author.py` guards both, and asserts that no shipped
template under `testcases/` hedges.

## Conventions

- One file per `<area>.<locale>` pair.
- English (`en`) is mandatory and is the fallback for any locale.
- Strings that contain SQL-injection or XSS payloads MUST be quoted
  in YAML (`"\"' OR 1=1 --\""`) to survive a YAML round-trip.
- Keep summaries phrased as `Verify that …` — the test suite asserts
  this on every loaded case.
- Add a new area by:
  1. picking a key (used in `_AREA_KEYWORDS` for keyword detection),
  2. writing `checklists/<area>.en.yaml` and/or
     `testcases/<area>.en.yaml`,
  3. validating locally with
     `python -c "from engine.qa_knowledge_loader import LOADER; assert LOADER.areas()"`.

## How it ties into the rest of the app

- `engine.qa_persona.generate_professional_checklist` and
  `generate_professional_test_cases` pull all area content from the
  loader. Orchestration (input analysis, crawler enrichment, named
  flows, browser findings, story expansion) stays in Python.
- `engine.testcase_generator` reads section→prefix via
  `LOADER.get_section_prefix(section_name)`.
- `engine.qa_persona._SECTION_PREFIXES` is a read-only proxy over
  the loader's section→prefix map, kept for any external caller
  that imports it.

## CI guard

CI runs:

```
python -c "from engine.qa_knowledge_loader import LOADER; assert LOADER.areas()"
```

to catch malformed YAML before merge.
