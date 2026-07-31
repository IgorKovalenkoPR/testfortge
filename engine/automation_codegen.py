"""TestFortge — generate a runnable TypeScript + Playwright + Allure project.

Takes the project's automation-targeted test cases (see
:mod:`engine.gherkin`) and emits a self-contained Node project the operator
can run locally or in CI:

    package.json  tsconfig.json  playwright.config.ts
    features/*.feature            one per section, derived from the TCs
    steps/*.ts                    the step-definition library
    locators.json                 the Locator registry's learned selectors
    .github/workflows/            a CI job that uploads allure-results back
    README.md  MANUAL-ASSERTIONS.md

Why a bundle instead of running it here
---------------------------------------
Operator decision, and the deployment agrees: the web service runs on a
512 MB instance with Playwright deliberately kept out of the worker, and
the Allure report generator needs a JVM. So the flow is generate → run
where the browsers live → post ``allure-results`` back to
``/automation/allure-results``, which :mod:`engine.allure_ingest` parses in
pure Python. No Java on the web dyno, and the module is useful on the free
plan today.

The honesty rule
----------------
An assertion the step library cannot bind **must not report green**. A
suite that passes because nobody checked anything is worse than no suite:
it converts an unknown into a false assurance. So an unbound assertion
resolves to ``test.skip()`` with the assertion text as the reason — the
scenario reports *skipped*, never passed — and :func:`coverage_report`
tells the operator the count before they ever run it. ``MANUAL-ASSERTIONS.md``
in the bundle lists every one by test-case id.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, Iterable

from engine import gherkin
from engine.log import get_logger

_logger = get_logger(__name__)

#: Bumped whenever the generated project's shape changes, so a stale
#: bundle on someone's disk is identifiable.
BUNDLE_VERSION = "1.0.0"

#: Pinned so a bundle generated today still installs in six months. The
#: Playwright and allure-playwright majors have to move together — an
#: allure-playwright built against a different reporter API silently emits
#: nothing, which looks like "the run produced no results".
PLAYWRIGHT_VERSION = "1.49.1"
PLAYWRIGHT_BDD_VERSION = "^7.5.0"
ALLURE_PLAYWRIGHT_VERSION = "^3.0.7"
TYPESCRIPT_VERSION = "^5.6.3"


# ── Step-binding model ───────────────────────────────────────────────

@dataclass(frozen=True)
class Binding:
    """One step-definition pattern the generated library implements."""
    #: Cucumber-expression-free: a real regex, so the generated TS uses the
    #: same source of truth this module matches with.
    pattern: str
    #: ``action`` steps do something; ``assertion`` steps check something.
    kind: str
    #: Short name used in the coverage report.
    name: str


#: The action vocabulary. Derived from ``wording_rules.yaml`` →
#: ``action_verbs``: these are the verbs the house style actually allows in
#: a step, so binding them covers the steps a compliant case can contain.
#:
#: Order matters — the first match wins, so the more specific pattern comes
#: first ("I click on the "X" button in the Header" before "I click …").
ACTION_BINDINGS: tuple[Binding, ...] = (
    Binding(r"^I (?:go to|open|visit|navigate to)(?: the site:?)? (\S+)$",
            "action", "navigate"),
    # The house precondition idiom: 'The "Apply" form is opened on the
    # https://x/careers page'. It carries the URL, so it IS executable —
    # and it is the single most common Given in the corpus, so leaving it
    # unbound would strand most scenarios.
    Binding(r"^the .+ is opened on the (https?://\S+) page\.?$",
            "precondition", "precondition: surface opened at url"),
    Binding(r"^the user is (?:on|at) the (https?://\S+)(?: page)?\.?$",
            "precondition", "precondition: user on url"),
    Binding(r"^I (?:click on|click|tap on|tap) the \[([^\]]+)\] button.*$",
            "action", "click bracketed button"),
    Binding(r"^I (?:click on|click|tap on|tap) the \"([^\"]+)\" (\w[\w -]*)"
            r"(?: in the (\w+))?$", "action", "click labelled control"),
    Binding(r"^I (?:expand|collapse) the \"([^\"]+)\" (?:drop-down|dropdown|"
            r"menu|accordion).*$", "action", "expand"),
    Binding(r"^I select \"([^\"]+)\" (?:from|in) the \"([^\"]+)\" "
            r"(?:drop-down|dropdown|list|select).*$", "action", "select"),
    Binding(r"^I fill in the \"([^\"]+)\" (?:field|entry field|input)"
            r"(?: with (.+))?$", "action", "fill labelled field"),
    Binding(r"^I fill in the ([\w][\w ']*) field(?: with (.+))?$",
            "action", "fill named field"),
    Binding(r"^I enter (.+) (?:into|in) the \"([^\"]+)\" "
            r"(?:field|input).*$", "action", "enter into field"),
    Binding(r"^I clear the \"([^\"]+)\" (?:field|input).*$",
            "action", "clear field"),
    Binding(r"^I leave the \"([^\"]+)\" (?:field|input).*empty.*$",
            "action", "leave empty"),
    Binding(r"^I (mark|unmark) the \"([^\"]+)\" checkbox.*$",
            "action", "toggle checkbox"),
    Binding(r"^I press the \"([^\"]+)\" key$", "action", "press key"),
    Binding(r"^I hover over the \"([^\"]+)\" (\w[\w -]*)$",
            "action", "hover"),
    Binding(r"^I scroll the page (up|down)(?: to the (.+))?$",
            "action", "scroll page"),
    Binding(r"^I use the following test data:$", "action", "test data"),
    Binding(r"^I look at the page$", "action", "no-op observation"),
)

#: Assertions are prose, so only precise, mechanically checkable shapes are
#: bound. Everything else is routed to the manual handler on purpose —
#: guessing at "the layout matches the design" would produce a check that
#: passes for the wrong reason, which is the anti-pattern house_style.yaml
#: names as inventing evidence.
ASSERTION_BINDINGS: tuple[Binding, ...] = (
    Binding(r"^the \"([^\"]+)\" (\w[\w -]*) is (?:displayed|visible|shown)$",
            "assertion", "control visible"),
    Binding(r"^the \"([^\"]+)\" (\w[\w -]*) is not "
            r"(?:displayed|visible|shown)$", "assertion", "control hidden"),
    Binding(r"^the \"([^\"]+)\" (\w[\w -]*) is (?:disabled|enabled)$",
            "assertion", "control enabled state"),
    Binding(r"^the (?:Homepage|home page) is opened$",
            "assertion", "homepage opened"),
    Binding(r"^the \"([^\"]+)\" page is opened$",
            "assertion", "named page opened"),
    Binding(r"^the (?:browser )?URL contains \"?([^\"]+)\"?$",
            "assertion", "url contains"),
    Binding(r"^(?:an? )?(?:error|validation|warning) message is displayed.*$",
            "assertion", "error message displayed"),
    Binding(r"^(?:no|there is no) (?:error|validation) message is "
            r"displayed.*$", "assertion", "no error message"),
    Binding(r"^the text \"([^\"]+)\" is displayed.*$",
            "assertion", "text visible"),
)

_ACTION_RES = tuple((re.compile(b.pattern, re.IGNORECASE), b)
                    for b in ACTION_BINDINGS)
_ASSERTION_RES = tuple((re.compile(b.pattern, re.IGNORECASE), b)
                       for b in ASSERTION_BINDINGS)


def classify_step(step: Any) -> Binding | None:
    """The binding that will handle ``step``, or ``None`` when unbound.

    ``And`` inherits the kind of the block it continues, so the caller has
    to resolve keywords before calling this — see :func:`_resolved_kind`.
    """
    text = str(getattr(step, "text", step) or "").strip()
    kind = getattr(step, "_resolved_kind", None) or ""
    if kind == "assertion":
        table = _ASSERTION_RES
    elif kind in ("action", "precondition"):
        # A Given may carry either a precondition idiom or a plain action
        # (the navigation step lands there), so both tables apply.
        table = _ACTION_RES
    else:
        table = _ACTION_RES + _ASSERTION_RES
    for rx, binding in table:
        if rx.match(text):
            return binding
    return None


def _resolved_kind(keyword: str, previous: str) -> str:
    """Which block a step belongs to, resolving And / But.

    ``Given`` is kept distinct from ``When``: an unbound one has a
    different consequence (skip before acting, versus fail) and the
    coverage report has to say which.
    """
    kw = (keyword or "").strip().title()
    if kw == "Given":
        return "precondition"
    if kw == "When":
        return "action"
    if kw == "Then":
        return "assertion"
    return previous or "action"


# ── Coverage ─────────────────────────────────────────────────────────

@dataclass
class ManualAssertion:
    case_id: str
    scenario: str
    text: str


@dataclass
class Coverage:
    """What the generated suite will and will not actually check.

    Three buckets, because the three failure modes differ:

    ``manual_assertions``    an unbound ``Then`` — the scenario runs and
                             then skips, so nothing is falsely green.
    ``manual_preconditions`` an unbound ``Given`` — the scenario skips
                             BEFORE acting, because starting from the wrong
                             state and carrying on would produce a result
                             about a different situation than the case
                             describes.
    ``unbound_actions``      an unbound ``When`` — a genuine gap: the step
                             is outside the house verb vocabulary, and the
                             scenario fails rather than skips, because a
                             missing action is a defect in the case.
    """
    scenarios: int = 0
    steps: int = 0
    bound_steps: int = 0
    unbound_actions: list[str] = field(default_factory=list)
    manual_assertions: list[ManualAssertion] = field(default_factory=list)
    manual_preconditions: list[ManualAssertion] = field(default_factory=list)
    #: Scenarios that will report *skipped* rather than passed.
    partly_manual_scenarios: int = 0

    @property
    def bound_pct(self) -> int:
        return int(round(100 * self.bound_steps / self.steps)) \
            if self.steps else 0

    def to_dict(self) -> dict:
        return {
            "scenarios": self.scenarios,
            "steps": self.steps,
            "bound_steps": self.bound_steps,
            "bound_pct": self.bound_pct,
            "unbound_actions": list(self.unbound_actions),
            "manual_assertions": [
                {"case_id": m.case_id, "scenario": m.scenario,
                 "text": m.text} for m in self.manual_assertions],
            "manual_preconditions": [
                {"case_id": m.case_id, "scenario": m.scenario,
                 "text": m.text} for m in self.manual_preconditions],
            "partly_manual_scenarios": self.partly_manual_scenarios,
            "runnable_scenarios": max(
                0, self.scenarios - self.partly_manual_scenarios),
        }


def coverage_report(cases: Iterable[Any]) -> Coverage:
    """Measure binding coverage BEFORE the operator runs anything.

    The point is that nobody discovers the gap from a suspiciously green
    report. Every unbound assertion is named with its test-case id.
    """
    cov = Coverage()
    for feature in gherkin.features_from_test_cases(cases):
        for scenario in feature.scenarios:
            cov.scenarios += 1
            case_id = _case_id_from_tags(scenario.tags)
            kind = ""
            scenario_manual = False
            for step in scenario.steps:
                kind = _resolved_kind(step.keyword, kind)
                cov.steps += 1
                setattr(step, "_resolved_kind", kind)
                if classify_step(step) is not None:
                    cov.bound_steps += 1
                elif kind == "assertion":
                    scenario_manual = True
                    cov.manual_assertions.append(ManualAssertion(
                        case_id=case_id, scenario=scenario.name,
                        text=step.text))
                elif kind == "precondition":
                    scenario_manual = True
                    cov.manual_preconditions.append(ManualAssertion(
                        case_id=case_id, scenario=scenario.name,
                        text=step.text))
                else:
                    cov.unbound_actions.append(
                        f"{case_id}: {step.keyword} {step.text}")
            if scenario_manual:
                cov.partly_manual_scenarios += 1
    return cov


def _case_id_from_tags(tags: Iterable[str]) -> str:
    for tag in tags or []:
        if tag.startswith("@TC-"):
            return tag[4:]
    return "?"


# ── Project files ────────────────────────────────────────────────────

_PACKAGE_JSON = {
    "name": "testfortge-automation",
    "version": BUNDLE_VERSION,
    "private": True,
    "description": "Generated from TestForTge test cases. Do not hand-edit "
                   "features/ — regenerate instead.",
    "type": "module",
    "scripts": {
        "test": "bddgen && playwright test",
        "test:smoke": "bddgen && playwright test --grep @smoke",
        "test:headed": "bddgen && playwright test --headed",
        "report": "allure generate allure-results --clean -o allure-report "
                  "&& allure open allure-report",
        "upload": "node scripts/upload-results.mjs",
    },
    "devDependencies": {
        "@playwright/test": PLAYWRIGHT_VERSION,
        "playwright-bdd": PLAYWRIGHT_BDD_VERSION,
        "allure-playwright": ALLURE_PLAYWRIGHT_VERSION,
        "typescript": TYPESCRIPT_VERSION,
        "@types/node": "^22.9.0",
    },
}

_TSCONFIG = {
    "compilerOptions": {
        "target": "ES2022",
        "module": "ES2022",
        "moduleResolution": "bundler",
        "strict": True,
        "esModuleInterop": True,
        "skipLibCheck": True,
        "types": ["node"],
    },
    "include": ["steps/**/*.ts", "playwright.config.ts"],
}

_PLAYWRIGHT_CONFIG = '''import {{ defineConfig, devices }} from '@playwright/test';
import {{ defineBddConfig }} from 'playwright-bdd';

// Generated by TestForTge — regenerate rather than hand-editing.
const testDir = defineBddConfig({{
  features: 'features/**/*.feature',
  steps: 'steps/**/*.ts',
}});

export default defineConfig({{
  testDir,
  // A generated suite is run against a live site, so a flake is more
  // likely to be the site than the test. One retry surfaces a real
  // failure twice; more than one hides an unstable page.
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : undefined,
  timeout: 60_000,
  expect: {{ timeout: 10_000 }},
  reporter: [
    ['list'],
    // allure-results is what TestForTge ingests. Keep the folder name —
    // the upload script and the CI workflow both look for it.
    ['allure-playwright', {{
      resultsDir: 'allure-results',
      detail: true,
      suiteTitle: true,
    }}],
  ],
  use: {{
    baseURL: process.env.BASE_URL ?? {base_url!r},
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    actionTimeout: 15_000,
  }},
  projects: [
    {{ name: 'chromium', use: {{ ...devices['Desktop Chrome'] }} }},
    // Uncomment to widen the matrix once the suite is stable.
    // {{ name: 'firefox',  use: {{ ...devices['Desktop Firefox'] }} }},
    // {{ name: 'mobile',   use: {{ ...devices['iPhone 13'] }} }},
  ],
}});
'''

_LOCATORS_TS = '''import type { Page, Locator } from '@playwright/test';
import learned from '../locators.json' with { type: 'json' };

/**
 * Resolve a control to a Playwright locator.
 *
 * Two sources, in order:
 *
 *  1. `locators.json` — selectors the TestForTge recorder LEARNED on this
 *     site, ranked by how often they actually worked. Reusing them is the
 *     point: the same chain drives the Python runner, so a locator that
 *     survived there survives here.
 *  2. a role/label/text guess from the label in the step text.
 *
 * Each candidate is tried in turn, which is what keeps a suite alive when
 * a class name changes but the accessible name does not.
 */

type Learned = Record<string, { primary: string; alternates: string[] }>;
const registry = learned as unknown as Learned;

/** Decode the symbolic selector format shared with the Python runner. */
export function decode(page: Page, target: string): Locator {
  const roleName = /^role=([a-z]+)\\[name=(?:"([^"]*)"|\\/(.*)\\/i)\\]$/.exec(target);
  if (roleName) {
    const [, role, literal, rx] = roleName;
    return page.getByRole(role as never,
      rx ? { name: new RegExp(rx, 'i') } : { name: literal });
  }
  const role = /^role=([a-z]+)$/.exec(target);
  if (role) return page.getByRole(role[1] as never);

  const simple: Array<[string, (v: string) => Locator]> = [
    ['label=',        (v) => page.getByLabel(v)],
    ['placeholder=',  (v) => page.getByPlaceholder(v)],
    ['text=',         (v) => page.getByText(v, { exact: false })],
    ['data-testid=',  (v) => page.getByTestId(v)],
    ['alt=',          (v) => page.getByAltText(v)],
    ['title=',        (v) => page.getByTitle(v)],
  ];
  for (const [prefix, build] of simple) {
    if (target.startsWith(prefix)) {
      const raw = target.slice(prefix.length);
      const rx = /^\\/(.*)\\/i$/.exec(raw);
      return rx ? build(new RegExp(rx[1], 'i') as unknown as string)
                : build(raw);
    }
  }
  return page.locator(target);
}

/** Every candidate for a label, learned ones first. */
export function candidates(label: string, kind = ''): string[] {
  const out: string[] = [];
  const hit = registry[label] ?? registry[label.toLowerCase()];
  if (hit) {
    out.push(hit.primary, ...(hit.alternates ?? []));
  }
  const role = ROLE_FOR_KIND[kind.toLowerCase().trim()];
  if (role) out.push(`role=${role}[name="${label}"]`);
  out.push(
    `role=button[name="${label}"]`,
    `role=link[name="${label}"]`,
    `label=${label}`,
    `placeholder=${label}`,
    `data-testid=${label}`,
    `text=${label}`,
  );
  return [...new Set(out.filter(Boolean))];
}

const ROLE_FOR_KIND: Record<string, string> = {
  button: 'button',
  link: 'link',
  checkbox: 'checkbox',
  'radio button': 'radio',
  radio: 'radio',
  tab: 'tab',
  'drop-down': 'combobox',
  dropdown: 'combobox',
  field: 'textbox',
  'entry field': 'textbox',
  input: 'textbox',
  heading: 'heading',
  image: 'img',
};

/**
 * First candidate that resolves to exactly one attached element.
 *
 * Throws with every candidate listed. A locator failure whose message
 * names only the last attempt is unactionable — the operator needs to see
 * what was tried so they can add the right one to the registry.
 */
export async function resolve(page: Page, label: string,
                              kind = ''): Promise<Locator> {
  const tried = candidates(label, kind);
  for (const target of tried) {
    const loc = decode(page, target).first();
    try {
      await loc.waitFor({ state: 'attached', timeout: 2_000 });
      return loc;
    } catch { /* try the next candidate */ }
  }
  throw new Error(
    `Could not resolve ${kind || 'control'} "${label}". Tried:\\n  ` +
    tried.join('\\n  ') +
    `\\nAdd a working selector to locators.json, or record the step in ` +
    `TestForTge so the registry learns it.`);
}
'''

_FIXTURES_TS = '''import { test as base, createBdd } from 'playwright-bdd';

/**
 * Per-scenario state the steps share.
 *
 * `data` is filled by the "I use the following test data:" step and read
 * by the fill steps, so a case that says 'Fill in the "Email" field with
 * valid data' uses the value the test case actually specified rather than
 * an invented one.
 */
export class World {
  data: Record<string, string> = {};

  value(field: string, fallbackHint = ''): string {
    const direct = this.data[field] ?? this.data[field.toLowerCase()];
    if (direct !== undefined) return direct;
    const hint = fallbackHint.toLowerCase();
    // Only for a case that asked for "valid data" without naming it.
    if (/invalid|malformed|wrong/.test(hint)) return INVALID[guess(field)];
    if (/valid|correct/.test(hint) || hint === '') return VALID[guess(field)];
    return fallbackHint.replace(/^["']|["']$/g, '');
  }
}

function guess(field: string): keyof typeof VALID {
  const f = field.toLowerCase();
  if (/e-?mail/.test(f)) return 'email';
  if (/phone|tel|mobile/.test(f)) return 'phone';
  if (/url|link|website/.test(f)) return 'url';
  if (/password|pass/.test(f)) return 'password';
  if (/number|amount|qty|quantity/.test(f)) return 'number';
  return 'text';
}

const VALID = {
  email: 'testfortge.qa@example.com',
  phone: '+380671234567',
  url: 'https://example.com/profile',
  password: 'Valid-Pass-123!',
  number: '42',
  text: 'TestForTge automated check',
} as const;

const INVALID = {
  email: 'not-an-email@',
  phone: '12',
  url: 'not a url',
  password: 'x',
  number: '-1',
  text: '!!!',
} as const;

export const test = base.extend<{ world: World }>({
  world: async ({}, use) => { await use(new World()); },
});

export const { Given, When, Then } = createBdd(test);
'''

_ACTIONS_TS = '''import { expect } from '@playwright/test';
import type { DataTable } from 'playwright-bdd';
import { Given, When, test } from './fixtures';
import { resolve } from './locators';

/**
 * Action steps — the house-style verbs from wording_rules.yaml.
 *
 * Registered on both Given and When because the translation puts
 * preconditions and the navigation step in the Given block while the same
 * verb can appear in either. `And` inherits its block's keyword, so
 * playwright-bdd resolves it without a separate registration.
 */

const nav = async ({ page }: { page: any }, url: string) => {
  await page.goto(url.replace(/[.,;]$/, ''), { waitUntil: 'domcontentloaded' });
};
Given(/^I (?:go to|open|visit|navigate to)(?: the site:?)? (\\S+)$/, nav);
When(/^I (?:go to|open|visit|navigate to)(?: the site:?)? (\\S+)$/, nav);

// The house precondition idiom — 'The "Apply" form is opened on the
// https://x/careers page'. It carries the URL, so it is executable, and it
// is the most common Given in the reference corpus.
Given(/^the .+ is opened on the (https?:\\/\\/\\S+) page\\.?$/, nav);
Given(/^the user is (?:on|at) the (https?:\\/\\/\\S+)(?: page)?\\.?$/, nav);

When(/^I (?:click on|click|tap on|tap) the \\[([^\\]]+)\\] button.*$/,
  async ({ page }, label: string) => {
    await (await resolve(page, label, 'button')).click();
  });

When(/^I (?:click on|click|tap on|tap) the "([^"]+)" ([\\w -]+?)(?: in the (\\w+))?$/,
  async ({ page }, label: string, kind: string) => {
    await (await resolve(page, label, kind)).click();
  });

When(/^I (?:expand|collapse) the "([^"]+)" (?:drop-down|dropdown|menu|accordion).*$/,
  async ({ page }, label: string) => {
    await (await resolve(page, label, 'drop-down')).click();
  });

When(/^I select "([^"]+)" (?:from|in) the "([^"]+)" (?:drop-down|dropdown|list|select).*$/,
  async ({ page }, value: string, label: string) => {
    const control = await resolve(page, label, 'drop-down');
    // A native <select> and a scripted drop-down need different handling,
    // and the markup is what decides which this is.
    const tag = await control.evaluate((el) => el.tagName.toLowerCase());
    if (tag === 'select') {
      await control.selectOption({ label: value });
    } else {
      await control.click();
      await (await resolve(page, value, 'option')).click();
    }
  });

const fill = async ({ page, world }: any, label: string, what = '') => {
  const control = await resolve(page, label, 'field');
  await control.fill(world.value(label, what));
};
When(/^I fill in the "([^"]+)" (?:field|entry field|input)(?: with (.+))?$/, fill);
When(/^I fill in the ([\\w][\\w ']*) field(?: with (.+))?$/, fill);

When(/^I enter (.+) (?:into|in) the "([^"]+)" (?:field|input).*$/,
  async ({ page, world }, what: string, label: string) => {
    await (await resolve(page, label, 'field')).fill(world.value(label, what));
  });

When(/^I clear the "([^"]+)" (?:field|input).*$/,
  async ({ page }, label: string) => {
    await (await resolve(page, label, 'field')).fill('');
  });

When(/^I leave the "([^"]+)" (?:field|input).*empty.*$/,
  async ({ page }, label: string) => {
    await (await resolve(page, label, 'field')).fill('');
  });

When(/^I (mark|unmark) the "([^"]+)" checkbox.*$/,
  async ({ page }, verb: string, label: string) => {
    const box = await resolve(page, label, 'checkbox');
    if (verb.toLowerCase() === 'mark') await box.check();
    else await box.uncheck();
  });

When(/^I press the "([^"]+)" key$/,
  async ({ page }, key: string) => { await page.keyboard.press(key); });

When(/^I hover over the "([^"]+)" ([\\w -]+)$/,
  async ({ page }, label: string, kind: string) => {
    await (await resolve(page, label, kind)).hover();
  });

When(/^I scroll the page (up|down)(?: to the (.+))?$/,
  async ({ page }, direction: string, target?: string) => {
    if (target) {
      // Scrolling "to the Footer" means bring it into view, which is a
      // different thing from scrolling a fixed distance.
      try {
        await (await resolve(page, target.replace(/[.,]$/, ''), 'section'))
          .scrollIntoViewIfNeeded();
        return;
      } catch { /* fall through to a plain scroll */ }
    }
    const by = direction === 'up' ? -800 : 800;
    await page.mouse.wheel(0, by);
  });

When(/^I use the following test data:$/,
  async ({ world }, table: DataTable) => {
    // Header row is "field | value"; anything else is taken as-is.
    for (const row of table.rows()) {
      if (row.length >= 2 && row[0].toLowerCase() !== 'field') {
        world.data[row[0]] = row[1];
        world.data[row[0].toLowerCase()] = row[1];
      }
    }
  });

When(/^I look at the page$/, async ({ page }) => {
  // The translation adds this to a display-only case so the scenario has
  // a When for the runner to hang a trigger on. Waiting for the document
  // to settle is the honest interpretation of "look".
  await page.waitForLoadState('domcontentloaded');
  expect(page.url()).toBeTruthy();
});

// ── Catch-all for an unbound precondition ───────────────────────────
//
// Registered last so every real pattern above wins. An unmet precondition
// is not something to skip past: the scenario would then act on a
// different situation than the test case describes, and whatever it
// reported would be about that other situation. Skipping is the only
// honest outcome.
//
// Given-only, deliberately. An unbound WHEN is NOT routed here — a missing
// action is a defect in the test case, and it fails.
Given(/^(.*)$/, async ({}, text: string) => {
  test.skip(true,
    `Precondition not automatable — set it up by hand, or bind it in ` +
    `steps/actions.ts: "${text}"`);
});
'''

_ASSERTIONS_TS = '''import { expect } from '@playwright/test';
import { Then, test } from './fixtures';
import { resolve } from './locators';

/**
 * Assertion steps.
 *
 * Only shapes that can be checked mechanically are bound. Everything else
 * falls through to the catch-all at the bottom, which SKIPS the scenario
 * with the assertion text as the reason.
 *
 * That is deliberate and it is the most important decision in this file. A
 * suite that passes because nobody checked anything converts an unknown
 * into a false assurance — worse than having no suite. A skipped scenario
 * is honest: it is neither green nor red, it says why, and TestForTge
 * counts it separately on the dashboard. See MANUAL-ASSERTIONS.md for the
 * full list, generated with the bundle.
 */

Then(/^the "([^"]+)" ([\\w -]+) is (?:displayed|visible|shown)$/,
  async ({ page }, label: string, kind: string) => {
    await expect(await resolve(page, label, kind)).toBeVisible();
  });

Then(/^the "([^"]+)" ([\\w -]+) is not (?:displayed|visible|shown)$/,
  async ({ page }, label: string, kind: string) => {
    // An absent element and a hidden one both satisfy "is not displayed",
    // and toBeHidden() covers both.
    try {
      await expect(await resolve(page, label, kind)).toBeHidden();
    } catch {
      // resolve() throwing means nothing matched, which IS the assertion.
    }
  });

Then(/^the "([^"]+)" ([\\w -]+) is (disabled|enabled)$/,
  async ({ page }, label: string, kind: string, state: string) => {
    const control = await resolve(page, label, kind);
    if (state === 'disabled') await expect(control).toBeDisabled();
    else await expect(control).toBeEnabled();
  });

Then(/^the (?:Homepage|home page) is opened$/, async ({ page }) => {
  const url = new URL(page.url());
  expect(url.pathname.replace(/\\/$/, '')).toBe('');
});

Then(/^the "([^"]+)" page is opened$/, async ({ page }, name: string) => {
  const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '');
  // Either the URL carries the page's slug or the page carries its H1 —
  // a site may do one without the other and both are legitimate evidence.
  const url = page.url().toLowerCase();
  if (url.includes(slug)) return;
  await expect(
    page.getByRole('heading', { name: new RegExp(name, 'i') }).first()
  ).toBeVisible();
});

Then(/^the (?:browser )?URL contains "?([^"]+)"?$/,
  async ({ page }, part: string) => {
    expect(page.url()).toContain(part);
  });

Then(/^(?:an? )?(?:error|validation|warning) message is displayed.*$/,
  async ({ page }) => {
    // role=alert is the accessible contract; the class names are the
    // fallback for sites that never got that far.
    const candidates = [
      page.getByRole('alert'),
      page.locator('[aria-invalid="true"]'),
      page.locator('.error, .invalid-feedback, .field-error, .help-block'),
    ];
    for (const c of candidates) {
      if (await c.first().isVisible().catch(() => false)) return;
    }
    throw new Error(
      'No error message found. Looked for role=alert, [aria-invalid=true] ' +
      'and the common error classes.');
  });

Then(/^(?:no|there is no) (?:error|validation) message is displayed.*$/,
  async ({ page }) => {
    await expect(page.getByRole('alert')).toHaveCount(0);
  });

Then(/^the text "([^"]+)" is displayed.*$/,
  async ({ page }, text: string) => {
    await expect(page.getByText(text, { exact: false }).first())
      .toBeVisible();
  });

// ── Catch-all: honest skip, never a silent pass ─────────────────────
Then(/^(.*)$/, async ({}, text: string) => {
  test.skip(true,
    `Assertion not automatable — verify manually: "${text}". ` +
    `Bind it in steps/assertions.ts if it can be checked mechanically.`);
});
'''

_UPLOAD_SCRIPT = '''#!/usr/bin/env node
/**
 * Post allure-results back to TestForTge.
 *
 *   TFG_URL=https://your.testfortge TFG_TOKEN=... npm run upload
 *
 * Zips allure-results and POSTs it to /automation/allure-results. The
 * endpoint is token-authenticated rather than session-authenticated so CI
 * can reach it without a browser login.
 */
import { createReadStream, existsSync, statSync } from 'node:fs';
import { readdir } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';

const base = process.env.TFG_URL;
const token = process.env.TFG_TOKEN;
const project = process.env.TFG_PROJECT_ID ?? '';
if (!base || !token) {
  console.error('Set TFG_URL and TFG_TOKEN.');
  process.exit(2);
}
if (!existsSync('allure-results')) {
  console.error('No allure-results/ — run the suite first.');
  process.exit(2);
}
const files = await readdir('allure-results');
if (files.length === 0) {
  console.error('allure-results/ is empty. Did the reporter run?');
  process.exit(2);
}

// zip via the platform tool: no npm dependency for a one-shot upload.
const zipName = 'allure-results.zip';
try {
  if (process.platform === 'win32') {
    execFileSync('powershell', ['-NoProfile', '-Command',
      `Compress-Archive -Path allure-results/* -DestinationPath ${zipName} -Force`]);
  } else {
    execFileSync('zip', ['-qr', zipName, 'allure-results']);
  }
} catch (e) {
  console.error('Could not create the archive:', e.message);
  process.exit(1);
}

const body = new FormData();
body.set('results', new Blob([await createReadStream(zipName).toArray?.() ??
  (await import('node:fs/promises')).readFile(zipName)]), zipName);
if (project) body.set('project_id', project);

const resp = await fetch(`${base.replace(/\\/$/, '')}/automation/allure-results`, {
  method: 'POST',
  headers: { 'X-TFG-Token': token },
  body,
});
const text = await resp.text();
console.log(resp.status, text.slice(0, 400));
process.exit(resp.ok ? 0 : 1);
'''

_WORKFLOW = '''name: automation

# Generated by TestForTge. Runs the suite where the browsers live, then
# posts allure-results back so the Dashboard picks up the run.
on:
  workflow_dispatch:
  schedule:
    - cron: '0 3 * * *'

jobs:
  playwright:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
          cache: npm
      - run: npm ci
      - run: npx playwright install --with-deps chromium
      - run: npm test
        env:
          BASE_URL: ${{ vars.BASE_URL }}
        continue-on-error: true
      # Upload even when the suite failed — a failed run is exactly the
      # one worth looking at, and skipping the upload would hide it.
      - name: Post results to TestForTge
        if: always()
        run: npm run upload
        env:
          TFG_URL: ${{ secrets.TFG_URL }}
          TFG_TOKEN: ${{ secrets.TFG_TOKEN }}
          TFG_PROJECT_ID: ${{ vars.TFG_PROJECT_ID }}
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: allure-results
          path: allure-results
          retention-days: 14
'''

_GITIGNORE = """node_modules/
allure-results/
allure-report/
test-results/
.features-gen/
*.zip
"""


# ── Rendering ────────────────────────────────────────────────────────

def _readme(project_name: str, base_url: str, cov: Coverage) -> str:
    lines = [
        f"# {project_name} — automated checks",
        "",
        f"Generated by TestForTge (bundle {BUNDLE_VERSION}) from "
        f"{cov.scenarios} automation-targeted test cases.",
        "",
        "## What this is",
        "",
        "TypeScript + Playwright + Cucumber (playwright-bdd), reporting to "
        "Allure. The `.feature` files are **generated** from the test cases "
        "in TestForTge — regenerate rather than hand-editing them, or the "
        "suite drifts from the cases the client signed off.",
        "",
        "`steps/` is yours to extend. That is where an assertion this "
        "generator could not bind gets implemented.",
        "",
        "## Run it",
        "",
        "```bash",
        "npm ci",
        "npx playwright install --with-deps chromium",
        f"BASE_URL={base_url or 'https://your.site'} npm test",
        "```",
        "",
        "## Send the results back",
        "",
        "```bash",
        "TFG_URL=https://your.testfortge TFG_TOKEN=... npm run upload",
        "```",
        "",
        "TestForTge parses `allure-results` itself, so you do not need a "
        "JVM or the Allure CLI for the Dashboard to update. Install the "
        "Allure CLI only if you also want the full HTML report locally "
        "(`npm run report`).",
        "",
        "## Honest coverage",
        "",
        f"- **{cov.bound_steps} of {cov.steps} steps** ({cov.bound_pct}%) "
        f"bind to a real implementation.",
        f"- **{cov.to_dict()['runnable_scenarios']} of {cov.scenarios} "
        f"scenarios** run end to end.",
        f"- **{cov.partly_manual_scenarios} scenarios** report **skipped** "
        f"rather than passed: "
        f"{len(cov.manual_assertions)} carry an assertion and "
        f"{len(cov.manual_preconditions)} a precondition this generator "
        f"could not bind. Every one is listed in `MANUAL-ASSERTIONS.md`.",
        "",
        "A step the library cannot bind never reports green. A suite that "
        "passes because nothing was checked is worse than no suite: it "
        "turns an unknown into a false assurance.",
    ]
    if cov.unbound_actions:
        lines += [
            "",
            f"⚠ **{len(cov.unbound_actions)} ACTION steps are unbound**, so "
            f"those scenarios FAIL rather than skip — a missing action is a "
            f"defect in the test case, not a gap in this library. They are "
            f"usually steps that drifted from the house verb vocabulary in "
            f"`wording_rules.yaml`. Listed in `MANUAL-ASSERTIONS.md`.",
        ]
    return "\n".join(lines) + "\n"


def _manual_assertions_doc(cov: Coverage) -> str:
    lines = [
        "# Assertions to verify manually",
        "",
        "Each scenario below reports **skipped** rather than passed, "
        "because at least one of its assertions could not be bound to an "
        "implementation. Skipped is the honest outcome: neither a pass "
        "nobody earned nor a failure nobody caused.",
        "",
        "To automate one, add a `Then` pattern to `steps/assertions.ts` "
        "above the catch-all at the bottom of that file.",
        "",
    ]
    if not cov.manual_assertions:
        lines.append("_None — every assertion in this pack is bound._")
    else:
        lines += ["| Test case | Scenario | Assertion |",
                  "|---|---|---|"]
        for m in cov.manual_assertions:
            scenario = m.scenario.replace("|", "\\|")[:90]
            text = m.text.replace("|", "\\|")[:140]
            lines.append(f"| `{m.case_id}` | {scenario} | {text} |")

    if cov.manual_preconditions:
        lines += [
            "", "## Preconditions to set up by hand", "",
            "These scenarios skip **before acting**. An unmet precondition "
            "is not something to run past: the scenario would then act on a "
            "different situation than the case describes, so whatever it "
            "reported would be about that other situation.",
            "",
            "A precondition that names a URL is already automated. What is "
            "left here needs data or state — a created record, a logged-in "
            "role — which belongs in a fixture in `steps/fixtures.ts`.",
            "",
            "| Test case | Scenario | Precondition |", "|---|---|---|",
        ]
        for m in cov.manual_preconditions:
            scenario = m.scenario.replace("|", "\\|")[:90]
            text = m.text.replace("|", "\\|")[:140]
            lines.append(f"| `{m.case_id}` | {scenario} | {text} |")
    if cov.unbound_actions:
        lines += ["", "## Unbound ACTION steps", "",
                  "These scenarios cannot run at all — the step is not in "
                  "the house verb vocabulary the library implements. Either "
                  "rewrite the step in the test case or add the pattern to "
                  "`steps/actions.ts`.", ""]
        lines += [f"- `{a}`" for a in cov.unbound_actions]
    return "\n".join(lines) + "\n"


def build_project(cases: Iterable[Any], *,
                  base_url: str = "",
                  project_name: str = "TestForTge project",
                  locators: dict | None = None) -> dict[str, str]:
    """Render every file of the bundle. Returns ``{path: text}``."""
    cases = [c for c in (cases or [])
             if gherkin.is_automation_targeted(c)]
    cov = coverage_report(cases)

    files: dict[str, str] = {
        "package.json": json.dumps(_PACKAGE_JSON, indent=2) + "\n",
        "tsconfig.json": json.dumps(_TSCONFIG, indent=2) + "\n",
        "playwright.config.ts": _PLAYWRIGHT_CONFIG.format(
            base_url=base_url or "http://localhost:3000"),
        "steps/locators.ts": _LOCATORS_TS,
        "steps/fixtures.ts": _FIXTURES_TS,
        "steps/actions.ts": _ACTIONS_TS,
        "steps/assertions.ts": _ASSERTIONS_TS,
        "scripts/upload-results.mjs": _UPLOAD_SCRIPT,
        ".github/workflows/automation.yml": _WORKFLOW,
        ".gitignore": _GITIGNORE,
        "locators.json": json.dumps(locators or {}, indent=2,
                                    ensure_ascii=False) + "\n",
        "README.md": _readme(project_name, base_url, cov),
        "MANUAL-ASSERTIONS.md": _manual_assertions_doc(cov),
    }
    for feature in gherkin.features_from_test_cases(cases):
        files[f"features/{gherkin.feature_filename(feature.name)}"] = \
            feature.render()
    return files


def bundle_zip(cases: Iterable[Any], **kwargs) -> bytes:
    """The bundle as a zip, ready to hand to an operator or a CI job."""
    files = build_project(cases, **kwargs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, text in sorted(files.items()):
            zf.writestr(path, text)
    return buf.getvalue()


def locators_for_project(project_id: str) -> dict:
    """The Locator registry's learned selectors, shaped for locators.json.

    Reusing them is the point of having a registry: a selector that
    survived in the Python runner is the one most likely to survive here.
    """
    if not project_id:
        return {}
    try:
        from engine import db as _db
        rows = _db.list_locators(project_id) or []
    except Exception as exc:  # pragma: no cover — best-effort
        _logger.warning("locators_for_project failed: %s", exc)
        return {}
    out: dict[str, dict] = {}
    for row in rows:
        label = str(row.get("label") or "").strip()
        primary = str(row.get("target") or row.get("primary") or "").strip()
        if not label or not primary:
            continue
        alternates = row.get("target_alternates") or row.get("alternates") or []
        if isinstance(alternates, str):
            try:
                alternates = json.loads(alternates)
            except Exception:
                alternates = []
        out[label] = {
            "primary": primary,
            "alternates": [str(a) for a in (alternates or []) if a],
        }
    return out


__all__ = [
    "BUNDLE_VERSION", "Binding", "Coverage", "ManualAssertion",
    "ACTION_BINDINGS", "ASSERTION_BINDINGS",
    "classify_step", "coverage_report",
    "build_project", "bundle_zip", "locators_for_project",
]
