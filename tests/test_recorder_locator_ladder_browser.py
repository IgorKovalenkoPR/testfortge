"""The recorder's locator ladder, executed in a real browser.

Every other test of ``extension/content.js`` in this suite is a string
search over its source, and the file that holds them says so plainly:
"These are not behavioural tests, and should not be read as coverage."
That was true because nothing here could run the script. It is no longer
true — the e2e job already builds a Chromium, and the ladder needs no
extension host to exercise: a stub ``chrome.runtime`` that answers
``is_active_for_tab`` and collects ``append_step`` is the whole harness.

So this file loads the real content script into a real page, clicks real
elements, and reads the steps it actually produced. No production code
was changed to make it testable.

**The defect it was written for**, measured on staging 2026-08-29 by
walking the Selenium practice form. A textarea recorded as::

    click role=textbox
    fill  role=textbox = "checking the segmenter"

``role=textbox`` matches five elements on that page. Playwright's strict
mode rejects it; the runner uses ``.first``, so the fill would have
landed in the *text input two fields above the textarea* and the step
would have passed. A locator that is confidently wrong is worse than one
that is obviously ugly.

Two causes, both fixed, both pinned below:

* the textarea sits inside a wrapping ``<label>Textarea …</label>``,
  which names it under ARIA exactly as ``for=`` does — and
  ``pickAccessibleName`` only looked for ``for=``, so the name was lost;
* with no name, the ladder emitted a bare ``role=`` at full score, which
  outranked every unambiguous alternative.
"""
from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright not installed")

from playwright.sync_api import sync_playwright            # noqa: E402


CONTENT_JS = (pathlib.Path(__file__).resolve().parent.parent
              / "extension" / "content.js")

# Answers the two messages the content script needs to start recording,
# and keeps every step it ships. Nothing else about the extension host is
# simulated, because nothing else is under test here.
#
# Every callback is deferred, which is not a detail: `chrome.runtime`
# always answers asynchronously, and the first version of this stub
# answered inline. `is_active_for_tab` then resolved mid-evaluation and
# `mountOverlay()` ran before the `let overlayShadow` below it existed —
# "Cannot access 'overlayShadow' before initialization", zero steps
# captured, and seven red tests describing a defect that was in the
# harness. A fake that is more obliging than the real thing does not test
# the code, it tests the fake.
STUB = """
window.__steps = [];
window.chrome = {
  runtime: {
    lastError: undefined,
    sendMessage: function (msg, cb) {
      if (msg && msg.type === 'append_step') { window.__steps.push(msg.step); }
      var reply = (msg && msg.type === 'is_active_for_tab') ? {active: true} : {};
      if (cb) setTimeout(function () { cb(reply); }, 0);
    },
    onMessage: {addListener: function () {}},
  },
};
"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:                     # no Chromium build
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


def _record(browser, html: str, click_selector: str) -> list[dict]:
    """Load *html* with the real content script, click, return the steps."""
    page = browser.new_page()
    try:
        page.set_content(html)
        page.evaluate(STUB)
        page.add_script_tag(content=CONTENT_JS.read_text(encoding="utf-8"))
        page.click(click_selector)
        page.wait_for_function("window.__steps.length > 0", timeout=5000)
        return page.evaluate("window.__steps")
    finally:
        page.close()


def _clicks(steps: list[dict]) -> list[dict]:
    # The script emits a goto for the initial page; the click is the
    # subject here.
    return [s for s in steps if s.get("action") == "click"]


def _chain(step: dict) -> list[str]:
    return [step.get("target") or "", *(step.get("target_alternates") or [])]


# The shape that produced the defect: a wrapping label, and enough
# same-role controls that the role alone identifies nothing. Trimmed from
# the real page rather than invented — the disabled/readonly inputs are
# there because they count towards the role too, which is the part that
# is easy to forget when reasoning about it instead of measuring.
AMBIGUOUS_FORM = """
<label>Text input <input type="text" name="my-text"></label>
<label>Password <input type="password" name="my-password"></label>
<label>Textarea <textarea name="my-textarea" rows="3"></textarea></label>
<label>Disabled <input type="text" name="my-disabled" disabled></label>
<label>Readonly <input type="text" name="my-readonly" readonly></label>
<button type="submit">Submit</button>
"""


class TestAWrappingLabelNamesItsControl:
    def test_the_textarea_is_recorded_by_role_and_name(self, browser):
        steps = _clicks(_record(browser, AMBIGUOUS_FORM, "textarea"))
        assert steps, "no click step was recorded"
        assert steps[0]["target"] == 'role=textbox[name="Textarea"]', (
            steps[0]["target"])

    def test_the_control_own_text_is_not_part_of_its_name(self, browser):
        """A wrapping label around a <select> must not absorb its options.

        ``label.textContent`` includes every descendant, so the obvious
        implementation would name this control
        "Country United Kingdom Ukraine".
        """
        html = """
        <label>Country
          <select name="country">
            <option>United Kingdom</option><option>Ukraine</option>
          </select>
        </label>
        <select name="other"><option>x</option></select>
        """
        steps = _clicks(_record(browser, html, 'select[name="country"]'))
        assert steps[0]["target"] == 'role=combobox[name="Country"]', (
            steps[0]["target"])

    def test_an_explicit_label_still_wins(self, browser):
        # for= is the case that already worked; it must not regress.
        html = ('<label for="a">Explicit</label>'
                '<input id="a" type="text"><input type="text" name="b">')
        steps = _clicks(_record(browser, html, "#a"))
        assert 'role=textbox[name="Explicit"]' in _chain(steps[0])


class TestABareRoleIsOnlyOfferedWhenItIdentifiesOneElement:
    def test_the_ambiguous_bare_role_is_nowhere_in_the_chain(self, browser):
        """Not merely demoted — absent.

        The runner walks the chain and takes the first candidate that is
        visible, so a wrong locator further down is still a wrong locator
        that will be used the moment the one above it drifts.
        """
        steps = _clicks(_record(browser, AMBIGUOUS_FORM, "textarea"))
        assert "role=textbox" not in _chain(steps[0]), _chain(steps[0])

    def test_a_unique_bare_role_is_still_offered(self, browser):
        """The relaxation is a feature, not collateral.

        When one element on the page carries the role, ``role=button``
        survives a renamed or translated label — which is why it exists.
        A fix that removed it everywhere would pass the test above and
        quietly cost that.
        """
        html = '<button type="submit">Submit</button><p>text</p>'
        steps = _clicks(_record(browser, html, "button"))
        chain = _chain(steps[0])
        assert chain[0] == 'role=button[name="Submit"]', chain
        assert "role=button" in chain, chain

    def test_the_ambiguity_is_counted_not_assumed(self, browser):
        """Two buttons make ``role=button`` ambiguous; one does not.

        Same markup, same role, opposite answer — so the check is reading
        the page rather than applying a rule about which roles are
        "usually" unique.
        """
        two = ('<button type="submit">Submit</button>'
               '<button type="button">Cancel</button>')
        steps = _clicks(_record(browser, two, 'button[type="submit"]'))
        assert "role=button" not in _chain(steps[0]), _chain(steps[0])


class TestTheChainStaysUsable:
    def test_something_unambiguous_is_always_offered(self, browser):
        """Dropping a candidate must not leave the step unaddressable.

        Withholding the bare role is only correct because the CSS last
        resort is still there. If both were ever gated, a recording would
        replay against nothing.
        """
        steps = _clicks(_record(browser, AMBIGUOUS_FORM, "textarea"))
        chain = _chain(steps[0])
        assert any(c.startswith("css=") or c.startswith("#") or
                   c.startswith("textarea") or "textarea" in c
                   for c in chain), chain


class TestAClickOnNothingIsNotAStep:
    """Also from the staging walk of 2026-08-29.

    A click on empty space beside the form recorded
    ``click html.h-100 > body.d-flex.flex-column`` — a step that replays
    as a click on the document and asserts nothing.

    **What decides it is the cursor**, once the bounded walk has found no
    control. That is not a proxy for the truth: it is the same signal the
    tester acted on, since they clicked because the pointer said they
    could.

    The first attempt at this guard checked only for ``<body>`` and
    ``<html>``, reasoning that dropping a real step is worse than keeping
    a noisy one. Re-running the walk against the real page refuted it —
    the same stray click landed on ``div.col-md-4`` and was recorded, and
    a page without a container is the exception. The rule was widened on
    that evidence.
    """

    @staticmethod
    def _record_clicks(browser, html, actions):
        page = browser.new_page()
        try:
            page.set_content(html)
            page.evaluate(STUB)
            page.add_script_tag(content=CONTENT_JS.read_text(encoding="utf-8"))
            page.wait_for_timeout(200)
            actions(page)
            page.wait_for_timeout(200)
            return _clicks(page.evaluate("window.__steps") or [])
        finally:
            page.close()

    def test_clicking_the_page_background_records_nothing(self, browser):
        html = '<button type="button" style="width:60px">Real</button>'
        steps = self._record_clicks(
            browser, html, lambda pg: pg.mouse.click(600, 400))
        assert steps == [], [s["target"] for s in steps]

    def test_clicking_a_layout_container_records_nothing(self, browser):
        """The case the narrow first rule missed, and the reason for this one.

        On the real page the stray click never reached <body>: a
        full-width column was in the way.
        """
        html = ('<div style="height:400px;width:100%">'
                '<button type="button">Real</button></div>')
        steps = self._record_clicks(
            browser, html, lambda pg: pg.mouse.click(600, 300))
        assert steps == [], [s["target"] for s in steps]

    def test_a_div_that_looks_clickable_is_recorded(self, browser):
        """A custom control built from a <div>.

        No role, no onclick attribute, no tabindex — invisible to
        ACTIONABLE_SELECTOR — but it declares itself to the mouse, and
        that declaration is what the guard reads.
        """
        html = ('<div id="custom" style="width:120px;height:40px;'
                'cursor:pointer">Tap</div>')
        steps = self._record_clicks(browser, html,
                                     lambda pg: pg.click("#custom"))
        assert steps and steps[0]["target"] == "#custom", steps

    def test_the_cost_of_the_rule_is_stated_rather_than_hidden(self, browser):
        """What the widened rule gives up, asserted so it stays visible.

        A control with a listener, no ARIA, no tabindex and no pointer
        cursor is dropped. It is rare, and it is a control that offers
        assistive technology nothing and the mouse nothing either — but
        the loss is real, and a comment is easier to lose than a test.
        """
        html = ('<div id="custom" style="width:120px;height:40px">Tap</div>'
                '<script>document.getElementById("custom")'
                '.addEventListener("click", function () {});</script>')
        steps = self._record_clicks(browser, html,
                                     lambda pg: pg.click("#custom"))
        assert steps == [], (
            "if this now records the step, the guard has been narrowed "
            "again — update the reasoning above, do not delete this test")

    def test_a_real_control_is_still_recorded_afterwards(self, browser):
        """Dropping a click must not wedge what follows.

        A guard that returned before the duplicate bookkeeping would
        leave the recorder's idea of the last event stale.
        """
        html = '<button type="button" style="width:60px">Real</button>'

        def walk(pg):
            pg.mouse.click(600, 400)
            pg.click("button")

        steps = self._record_clicks(browser, html, walk)
        assert [s["target"] for s in steps] == ['role=button[name="Real"]']

    def test_an_anchor_inside_a_plain_container_still_records(self, browser):
        # The walk resolves to the <a>, so the cursor question never
        # arises — the common case must stay untouched.
        html = '<div><span><a href="#x">Company</a></span></div>'
        steps = self._record_clicks(browser, html,
                                     lambda pg: pg.click("span"))
        assert steps and steps[0]["target"] == 'role=link[name="Company"]', (
            steps)
