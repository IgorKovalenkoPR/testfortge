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
    wording_rules.yaml      the reviewing team lead's own wording rules
  glossary/
    ui_terms.en.yaml        the team's UI terminology reference
```

`style/` and `glossary/` are **not** loaded by `LOADER` and are not
schema-validated — they are prompt material, read as raw text by
`engine.tc_author` and pasted into cached system blocks. See "Style
assets" below.

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

## Style assets (`style/`, `glossary/`)

Four files teach the **Test Case Author agent** (`engine/tc_author.py`)
how to write, what to cover, and what to call things.

| File | Answers | Provenance |
|---|---|---|
| `style/house_style.yaml` | **How** to write a case: title grammar, precondition idiom, step verbs, expected-result voice, section naming, category rules, anti-patterns. | Odoo Test Plan — 41 sheets / 4,808 rows / 8 modules |
| `style/coverage_rules.yaml` | **Which** cases must exist: per-surface control enumeration, per-control-type scenario sets, mandatory positive/negative pairings, cross-cutting sweeps. | same |
| `style/wording_rules.yaml` | **How to phrase it**: element naming, approved action verbs, entry-point navigation, graded-outcome ban, punctuation, opener. | `Training Plan_Horban Yaroslavna.xlsx` — the reviewing team lead's threaded comments |
| `glossary/ui_terms.en.yaml` | **What to call it**: 87 canonical UI terms with `kind`, `control_type`, acceptable `aliases`, and flagged `avoid` spellings. | `Glossary.xlsx` — 80 website + 11 mobile terms |
| `style/checklist_style.yaml` | **The low-level checklist**: section set, hierarchical numbering, per-surface and per-form derivation, objective grammar, status vocabulary. Read by `engine/checklist_author.py`. | `Training Plan_Horban Yaroslavna.xlsx` — the "Low-level checklist" sheet, 57 checks |

### Two authors, two floors

Each artefact has an LLM author and a deterministic generator, and the
deterministic one is the floor rather than a degraded mode:

| Artefact | Agent | Enumeration |
|---|---|---|
| Test cases | `engine/tc_author.py` | `engine/tc_rules.py` |
| Checklist | `engine/checklist_author.py` | `engine/checklist_rules.py` |

The agent buys judgement — section naming and order, which interactions
deserve a row, and reading requirements or an attached spec that no
crawler can see. It does **not** get to decide what counts as evidence or
how a row is worded: numbering is applied afterwards by
`checklist_rules.assign_numbers`, the terminology rules run over its output
in code, and a row quoting a label no artefact contains is dropped and
reported. Every failure path — no key, refused call, unparseable JSON,
empty result — lands on the enumeration.

Editing any of them changes generated output with no Python change. They
are loaded as **raw text**, not parsed YAML, because the `evidence:` and
`reviewer:` comments are the part that stops the model treating a house
convention as negotiable.

`wording_rules.yaml` is the only place the team's wording rules are
written down explicitly — they existed nowhere but as review comments on
a spreadsheet. Every rule in it quotes the comment that produced it under
`reviewer:`. **A rule with no `reviewer:` or `evidence:` line is an
invention and should be deleted.**

### Rules also enforced in code

So they hold on every path, including the no-API-key fallback:

- **Requirement voice.** `must` / `shall` / `ought to` are banned in the
  expected result — they state a requirement on the product rather than
  something a tester can observe. `engine.tc_author.normalise_expected_result`
  rewrites them; `engine.qa_team_lead` review rule 2 applies it.
- **One modal, one place.** A summary — a test-case summary, a checklist
  objective or a bug title — carries **no modal verb at all** and is
  written in active or passive voice. `should` / `should be` is the sole
  exception and belongs only in the **expected result** of a test case or
  a bug report. Operator ruling, 2026-08-01.

  | | summary / objective / bug title | expected result |
  |---|---|---|
  | `should` | flagged | **accepted** |
  | `can`, `cannot` | rewritten | rewritten |
  | `may`, `might`, `will`, `would`, `could` | flagged | flagged |
  | `must`, `shall`, `ought to` | flagged | flagged + rewritten |

  `can` / `cannot` are *rewritten* rather than merely reported because
  the fix is mechanical and loses nothing — "User can save" → "User
  saves", "User cannot save" → "User does not save". The rest change
  meaning when removed, so they are reported and left alone.

  Why `should` survives at all: the two corpora disagree. The Odoo
  client deliverable never writes it; the team's own reviewed training
  deliverable writes it throughout and the reviewing team lead let every
  instance stand. The operator settled it in favour of the training
  deliverable, then scoped it to the expected result. Full provenance in
  `engine/tc_author.py` at `_WEAK_MODAL_RE`; enforcement in
  `engine/glossary.py` against the `modal_summary_only` bucket. Nothing
  inside quotes is touched by either — a quoted criterion is a citation,
  not a claim the sentence is making.
- **Negative cases assert the feedback too.** A refusal case must state
  both that the action was blocked *and* what the user sees. On the
  corpus's dedicated error-message sheet, 8 of 25 rows failed precisely
  on missing or misdirected feedback. Review rule 8 repairs cases that
  only assert the refusal.
- **Terminology and wording** (`engine/glossary.py`): graded outcomes
  ("works correctly"), generic steps, objectless verbs ("Scroll down"),
  assertions inside steps, missing `Verify` opener, trailing periods,
  lower-case page regions, and non-canonical element names. Everything
  information-preserving is auto-fixed by `normalise_text`; a semantic
  rename is only ever *suggested*, because guessing wrong there breaks
  the locator the label stands in for.

`tests/test_glossary.py` guards each rule against the reviewer comment
that produced it, and asserts that **every shipped `testcases/` template
passes the linter** — those templates are the free-tier output, so a
finding there is a finding a client would see.

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
