"""Structural checks on the recorder extension's content script.

**These are not behavioural tests, and should not be read as coverage.**
There is no JavaScript runner in CI, and the extension has never been
unit-tested — ``tests/test_recorder_session_api.py`` says as much. What
a Python test can hold is that a guard which was added for a reason has
not been quietly deleted; whether it *works* is established by walking
the recorder in a browser, which is how the defect below was found in
the first place.

The defect: a walk of two clicks on staging (2026-08-28) produced four
click steps, each duplicated back to back. A replayed pack that clicks
twice per step is worse than one missing a step — on a submit button it
orders twice.

The cause was never established. The listener is registered once, in
capture phase, and the manifest injects the file once; the page also
carried five other extensions, any of which re-dispatching an event
would produce the same result. So the extension defends the property
rather than a theory of the cause, in two places, and this file pins
both:

* an install guard, so a second copy of the script cannot double every
  listener — the failure that would look exactly like this one;
* a duplicate-event window, so one interaction delivered twice is
  recorded once, whatever delivered it.

A second defect from the same walk has its own class below: every
recorded locator was a raw CSS path, because the ladder was being handed
the ``<span>`` under the pointer instead of the ``<a>`` around it. That
one's cause *was* established, and the fix is aimed at it.
"""
from __future__ import annotations

import pathlib
import re

import pytest


CONTENT_JS = (pathlib.Path(__file__).resolve().parent.parent
              / "extension" / "content.js")


@pytest.fixture(scope="module")
def source() -> str:
    assert CONTENT_JS.is_file(), f"{CONTENT_JS} is missing"
    return CONTENT_JS.read_text(encoding="utf-8")


class TestTheInstallGuard:
    def test_the_script_refuses_to_install_twice(self, source):
        assert "__tfgRecorderContentInstalled" in source

    def test_the_guard_runs_before_any_listener_is_registered(self, source):
        """Order is the whole point.

        A guard placed after the handlers would let the second copy
        register its listeners and only then decline to continue, which
        is the bug it exists to prevent.
        """
        guard = source.index("__tfgRecorderContentInstalled")
        first_listener = source.index("addEventListener")
        assert guard < first_listener, (
            "the install guard must run before the first addEventListener")


class TestTheDuplicateWindow:
    def test_the_click_handler_consults_it(self, source):
        handler = source[source.index("document.addEventListener('click'"):]
        body = handler[:handler.index("}, true);")]
        assert "isDuplicate(" in body

    def test_the_change_handler_consults_it_too(self, source):
        # A doubled fill is not merely redundant: an input that appends
        # rather than replaces turns it into corrupt test data.
        handler = source[source.index("document.addEventListener('change'"):]
        body = handler[:handler.index("}, true);")]
        assert "isDuplicate(" in body

    def test_the_window_is_tight_enough_to_keep_real_double_clicks(
            self, source):
        """A human cannot double-click faster than roughly 60 ms.

        Widening this window past that would silently collapse a genuine
        double-click into one step — trading a duplicated step for a
        missing one, which is the same class of bug pointing the other
        way.
        """
        match = re.search(r"DUPLICATE_WINDOW_MS\s*=\s*(\d+)", source)
        assert match, "DUPLICATE_WINDOW_MS is gone"
        assert int(match.group(1)) <= 60, (
            f"{match.group(1)} ms is long enough to eat a real "
            f"double-click")

    def test_it_compares_the_element_not_just_its_locator(self, source):
        """Two different elements can share a CSS path.

        Comparing derived locator strings would collapse clicks on two
        genuinely different controls; comparing the element identity
        cannot.
        """
        window = source[source.index("function isDuplicate"):]
        window = window[:window.index("\n  }")]
        assert "lastEvent.target === e.target" in window


class TestTheClickTargetIsResolved:
    """The locator ladder can only be as good as the element it is given.

    Measured on staging 2026-08-29: a click on a nav link recorded as
    ``ul > li:nth-of-type(1) > a > span:nth-of-type(1)``. The ladder
    (testid > id > role+name > placeholder > alt > title > css) was
    working correctly — it was being handed the ``<span>`` inside the
    ``<a>``, because ``e.target`` is the deepest element under the
    pointer. A span has no role, no id and no accessible name, so every
    rung failed and the CSS last resort won.

    Resolving to the nearest actionable ancestor turns that same click
    into ``role=link[name="Company"]``, which survives a markup change
    and reads like something a person did.
    """

    def test_the_click_handler_resolves_before_deriving(self, source):
        handler = source[source.index("document.addEventListener('click'"):]
        body = handler[:handler.index("}, true);")]
        assert "deriveCandidates(actionableTarget(e.target))" in body, (
            "the raw event target is being handed to the ladder again")

    def test_the_walk_is_bounded(self, source):
        """An unbounded closest() is worse than the bug it fixes.

        A page that wraps its content in [role="main"] would turn every
        click on plain text into a locator for the whole page.
        """
        match = re.search(r"ACTIONABLE_MAX_DEPTH\s*=\s*(\d+)", source)
        assert match, "the depth bound is gone"
        assert 1 <= int(match.group(1)) <= 6, match.group(1)

    def test_the_selector_covers_the_controls_people_click(self, source):
        block = source[source.index("ACTIONABLE_SELECTOR"):]
        block = block[:block.index("].join(',')")]
        for needed in ("a[href]", "button", "input", "select", "textarea",
                       "[role]", "[data-testid]"):
            assert needed in block, needed

