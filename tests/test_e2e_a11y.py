"""Accessibility gate: axe-core over the pages the WCAG AA restyle touched.

Zero **serious** or **critical** violations of the WCAG 2.0/2.1 A and AA
rules, on six screens: the dashboard, test cases, bug reports, the search
results with the AI answer block showing, the Ask panel open with an
answer in it, and the organisation settings as an admin. Moderate and
minor findings are reported in the failure text when something else fails
but do not fail the gate — that is the line the restyle was held to.

**axe is vendored, not fetched.** ``tests/vendor/axe-core/axe.min.js`` is
axe-core 4.13.0 (MPL-2.0, used unmodified — the licence file sits beside
it), pinned by the sha256 in ``VERSION`` and checked before every run, so
an edited or swapped bundle fails loudly instead of auditing with
different rules. It is injected with ``page.evaluate`` rather than a
``<script>`` tag: the app's CSP (``script-src 'self' 'nonce-…'``) would
rightly refuse an inline tag, and loosening the CSP to test the page would
test a different page.

The pages are rendered the way they ship: authenticated, CSRF on, search
and the AI flag on, ``/api/ask`` stubbed at the browser (no model).
Conventions and fixtures are ``tests/test_e2e_golden_paths.py``'s.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright not installed")

from tests.test_e2e_golden_paths import (  # noqa: E402,F401 — fixtures
    WAIT, _create_project, _project_id, actors, browser, live_server,
    search_on, team,
)
from tests.test_e2e_search import ANSWER_STREAM, AskStub  # noqa: E402

VENDOR = pathlib.Path(__file__).resolve().parent / "vendor" / "axe-core"
AXE_JS = VENDOR / "axe.min.js"

#: WCAG 2.0 + 2.1, levels A and AA — the restyle's target.
TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]
BLOCKING = {"serious", "critical"}


def _pinned_sha256() -> str:
    for line in (VENDOR / "VERSION").read_text(encoding="utf-8").splitlines():
        if line.startswith("sha256 "):
            return line.split()[1]
    raise AssertionError("tests/vendor/axe-core/VERSION has no sha256 line")


@pytest.fixture(scope="module")
def axe_source() -> str:
    raw = AXE_JS.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == _pinned_sha256(), (
        f"tests/vendor/axe-core/axe.min.js is not the pinned build "
        f"(sha256 {digest}); update VERSION only with a reviewed upgrade")
    return raw.decode("utf-8")


def _settle(page) -> None:
    """Audit the page people look at, not a frame mid-transition.

    The dashboard test waited for ``#main-content`` only, so axe could run
    while the hints card was still arriving or a fade was half-way —
    colour contrast measured on a partly transparent element. Green 30/30
    on CI and 10/10 locally, then red on run 1 of 10 (CI, 2026-10-12) with
    no code change. A real violation survives this wait; a transient one
    does not. Infinite animations (spinners) are left out — they never
    finish — and the whole wait is capped.
    """
    page.wait_for_load_state("networkidle")
    page.evaluate(
        """() => Promise.race([
            Promise.all(document.getAnimations()
                .filter(a => {
                    const t = a.effect && a.effect.getComputedTiming();
                    return t && t.endTime !== Infinity;
                })
                .map(a => a.finished.catch(() => null))),
            new Promise(r => setTimeout(r, 3000)),
        ])""")


def _audit(page, axe_source: str) -> list[dict]:
    _settle(page)
    if not page.evaluate("() => typeof window.axe === 'object'"):
        page.evaluate(axe_source)
    return page.evaluate(
        """async (tags) => {
            const r = await axe.run(document, {
                runOnly: {type: 'tag', values: tags},
                resultTypes: ['violations'],
            });
            return r.violations.map(v => ({
                id: v.id, impact: v.impact, help: v.help,
                nodes: v.nodes.map(n => ({
                    target: n.target.join(' '),
                    summary: (n.failureSummary || '').slice(0, 300),
                })),
            }));
        }""", TAGS)


def _assert_clean(page, axe_source: str, where: str) -> None:
    violations = _audit(page, axe_source)
    blocking = [v for v in violations if v["impact"] in BLOCKING]
    assert not blocking, (
        f"{where}: {len(blocking)} serious/critical axe violation(s):\n"
        + json.dumps(blocking, indent=1, ensure_ascii=False)[:6000]
        + "\n(non-blocking: "
        + ", ".join(f"{v['id']}[{v['impact']}]" for v in violations
                    if v["impact"] not in BLOCKING) + ")")


@pytest.fixture
def admin(actors, team, search_on):
    """The admin, signed in, on an indexed golden project with a bug and a
    test-case list to render (an empty page audits almost nothing)."""
    from engine.search import indexer
    from tests._search_seed import seed_golden

    person = actors(team["admin"]["email"]).sign_in()
    name = f"A11y {team['tag']}"
    _create_project(person, name)
    pid = _project_id(team["org"], name)
    seed_golden(pid)
    indexer.reindex_project(pid)
    return person


class TestNoSeriousViolations:
    def test_dashboard(self, admin, axe_source):
        admin.go("/?lang=en")
        admin.page.wait_for_selector("#main-content", timeout=WAIT)
        _assert_clean(admin.page, axe_source, "dashboard")

    def test_test_cases(self, admin, axe_source):
        admin.go("/test-cases?lang=en")
        # With the hints card open, as a first visit renders it.
        admin.page.wait_for_selector(".tf-hints-card .tf-hint", timeout=WAIT)
        _assert_clean(admin.page, axe_source, "test cases")

    def test_bug_reports(self, admin, axe_source):
        admin.go("/bug-reports?lang=en")
        admin.page.wait_for_selector(".tf-hints-card .tf-hint", timeout=WAIT)
        _assert_clean(admin.page, axe_source, "bug reports")

    def test_search_results_with_the_ai_block(self, admin, axe_source):
        page = admin.page
        AskStub(page, ANSWER_STREAM)
        admin.go("/test-cases?lang=en")
        page.locator("body").press("/")
        page.locator("#tf-search-input").fill("password")
        page.locator("#tf-search-input").press("Enter")
        page.wait_for_selector("#tf-results .tf-hit", timeout=WAIT)
        page.click("#tf-ai [data-ai-get]")
        page.wait_for_selector("#tf-ai-text a.tf-cite", timeout=WAIT)
        page.click("#tf-results-meta .tf-how-toggle")
        page.wait_for_selector("#tf-results-how .tf-how-table", timeout=WAIT)
        _assert_clean(page, axe_source, "search results + AI block")

    def test_ask_panel_open(self, admin, axe_source):
        page = admin.page
        AskStub(page, ANSWER_STREAM)
        admin.go("/test-cases?lang=en")
        page.click("[data-ask-open]")
        page.wait_for_selector("#tf-ask:not([hidden])", timeout=WAIT)
        page.fill("#tf-ask-input", "Is a valid login covered?")
        page.keyboard.press("Enter")
        page.wait_for_selector("#tf-ask .tf-ask-meta", timeout=WAIT)
        _assert_clean(page, axe_source, "Ask panel")

    def test_org_settings_as_admin(self, admin, axe_source):
        admin.go("/org/settings?lang=en")
        # The admin-only form, so the page audited is the admin's page.
        admin.page.wait_for_selector("#org-name", timeout=WAIT)
        _assert_clean(admin.page, axe_source, "organisation settings")


#: Every other page in the signed-in admin's navigation (2026-10-14: the
#: gate covered six screens of twelve, and the one live contrast failure
#: was on a page it never opened). Path, what to wait for, label.
OTHER_PAGES = [
    ("/estimation?lang=en", "#main-content", "estimation"),
    ("/checklist?lang=en", "#main-content", "checklist"),
    ("/test-execution?lang=en", "#main-content", "test execution"),
    ("/test-execution/runs?lang=en", "#main-content", "runs"),
    ("/test-execution/live?lang=en", "#live-toolbar", "live automation view"),
    ("/automation?lang=en", "#main-content", "automation"),
    ("/guide?lang=en", "#main-content", "guide"),
    ("/org/members?lang=en", "#main-content", "team members"),
]


class TestEveryPageInTheNavigation:
    @pytest.mark.parametrize("path, ready, where", OTHER_PAGES,
                             ids=[p[2] for p in OTHER_PAGES])
    def test_page(self, admin, axe_source, path, ready, where):
        admin.go(path)
        admin.page.wait_for_selector(ready, timeout=WAIT)
        _assert_clean(admin.page, axe_source, where)


class TestTheGateWouldNotice:
    def test_axe_finds_a_planted_violation(self, admin, axe_source):
        """A gate that cannot fail is not a gate: plant an image without
        alt text and a button without a name, and axe must say so."""
        page = admin.page
        admin.go("/?lang=en")
        page.evaluate("""() => {
            const img = document.createElement('img');
            img.src = 'data:image/gif;base64,R0lGODlhAQABAAAAACw=';
            document.getElementById('main-content').appendChild(img);
            const b = document.createElement('button');
            document.getElementById('main-content').appendChild(b);
        }""")
        ids = {v["id"] for v in _audit(page, axe_source)
               if v["impact"] in BLOCKING}
        assert {"image-alt", "button-name"} <= ids, ids
