"""Toggletips in a real browser (owner ruling 2026-10-16).

The "?" beside a control must, per WCAG 2.1 SC 1.4.13 and the owner's
ask: open on mouse hover, on keyboard focus and on a tap; stay open while
the pointer moves onto the note; never time out; close on Escape without
the pointer or focus moving; and fit a 375 px screen. Server tests cannot
see any of it — tests/test_toggletips.py covers the markup.

Runs on /test-execution with a pack loaded, the page that carries most
notes. Fixtures are tests/test_e2e_golden_paths.py's.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright not installed")

from tests.test_e2e_golden_paths import (  # noqa: E402,F401 — fixtures
    WAIT, Actor, _create_project, _upload_pack, actors, browser,
    live_server, search_on, team,
)

FIT = """() => [...document.querySelectorAll('.tf-tip-bubble:not([hidden])')]
    .map(b => { const r = b.getBoundingClientRect();
                return [r.left, r.right, document.documentElement.clientWidth]; })"""


@pytest.fixture
def admin(actors, team, search_on):
    person = actors(team["admin"]["email"]).sign_in()
    _create_project(person, f"Tips {team['tag']}")
    _upload_pack(person)
    person.go("/test-execution?lang=en")
    person.page.wait_for_selector("#tip-te-video", state="attached", timeout=WAIT)
    person.errors = []
    person.page.on("pageerror", lambda e: person.errors.append(str(e)))
    return person


def _tip(page, tid):
    btn = page.locator(f'[data-tip]:has(#tip-{tid}) .tf-tip-btn')
    btn.scroll_into_view_if_needed()
    return btn, page.locator(f"#tip-{tid}")


def _centre(loc):
    bb = loc.bounding_box()
    return bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2


class TestPointer:
    def test_hover_opens_and_the_note_itself_is_hoverable(self, admin):
        page = admin.page
        btn, note = _tip(page, "te-video")
        assert note.is_hidden()
        btn.hover()
        assert note.is_visible()
        assert btn.get_attribute("aria-expanded") == "true"
        page.mouse.move(*_centre(note))
        page.wait_for_timeout(600)          # past the 300 ms close delay
        assert note.is_visible(), "moving onto the note closed it"
        page.mouse.move(2, 2)
        page.wait_for_timeout(600)
        assert note.is_hidden(), "the note stayed after the pointer left"

    def test_it_never_times_out(self, admin):
        btn, note = _tip(admin.page, "te-env")
        btn.hover()
        admin.page.wait_for_timeout(3000)
        assert note.is_visible()

    def test_escape_closes_in_place_and_hover_does_not_reopen(self, admin):
        page = admin.page
        btn, note = _tip(page, "te-video")
        btn.hover()
        page.keyboard.press("Escape")
        assert note.is_hidden()
        page.mouse.move(*_centre(btn))      # still over the "?"
        page.wait_for_timeout(200)
        assert note.is_hidden(), "reopened without the pointer leaving"

    def test_a_click_pins_it_and_a_click_outside_closes_it(self, admin):
        page = admin.page
        btn, note = _tip(page, "te-sweep")
        btn.click()
        page.mouse.move(2, 2)
        page.wait_for_timeout(600)
        assert note.is_visible(), "a pinned note closed on pointer leave"
        page.mouse.click(2, 400)
        assert note.is_hidden()

    def test_one_note_at_a_time(self, admin):
        page = admin.page
        _tip(page, "te-env")[0].click()
        _tip(page, "te-video")[0].click()
        assert page.locator(".tf-tip-bubble:not([hidden])").count() == 1


class TestKeyboard:
    def test_focus_opens_escape_closes_and_keeps_focus(self, admin):
        page = admin.page
        btn, note = _tip(page, "te-video")
        btn.focus()
        assert note.is_visible()
        page.keyboard.press("Escape")
        assert note.is_hidden()
        assert page.evaluate(
            "() => document.activeElement.getAttribute('aria-controls')") == "tip-te-video"

    def test_tabbing_away_closes_it(self, admin):
        page = admin.page
        btn, note = _tip(page, "te-video")
        btn.focus()
        page.keyboard.press("Tab")
        assert note.is_hidden()

    def test_the_page_tips_load_into_the_title_note(self, admin):
        page = admin.page
        page.locator(".tf-tip--page .tf-tip-btn").click()
        page.wait_for_selector(".tf-tip--page .tf-hint", timeout=WAIT)
        assert page.locator(".tf-tip--page .tf-hint-link").count() >= 1


class TestTheControlsAroundIt:
    def test_a_note_beside_a_choice_does_not_make_the_choice(self, admin):
        """The run-mode cards and the sweep checkbox are labels: a "?"
        that ticked them on the way would change the run."""
        page = admin.page
        manual = page.locator('input[name="run_mode"][value="manual"]')
        sweep = page.locator('input[name="site_sweep"]')
        was = sweep.is_checked()
        _tip(page, "te-mode-manual")[0].click()
        page.keyboard.press("Escape")
        _tip(page, "te-sweep")[0].click()
        assert not manual.is_checked()
        assert sweep.is_checked() == was

    def test_the_notes_fit_the_desktop_viewport(self, admin):
        page = admin.page
        ids = page.evaluate(
            "() => [...document.querySelectorAll('.tf-tip-bubble')].map(e => e.id)")
        assert len(ids) >= 15
        off = []
        for i in ids:
            btn = page.locator(f'[data-tip]:has(#{i}) .tf-tip-btn')
            if not btn.is_visible():
                continue
            btn.scroll_into_view_if_needed()
            btn.click()
            off += [(i, l, r) for l, r, vw in page.evaluate(FIT) if l < 0 or r > vw]
            page.keyboard.press("Escape")
        assert not off, off
        assert not admin.errors, admin.errors


class TestPhone:
    @pytest.fixture
    def thumb(self, browser, live_server, admin):
        person = Actor(browser, live_server, admin.email)
        person.context.close()
        person.context = browser.new_context(
            viewport={"width": 375, "height": 812}, has_touch=True,
            storage_state=admin.context.storage_state())
        person.page = person.context.new_page()
        person.go("/test-execution?lang=en")
        yield person
        person.close()

    @pytest.mark.parametrize("tid", ["te-video", "te-base-url", "te-mode-wt",
                                     "pp-active", "te-clear-pack"])
    def test_a_tap_opens_it_on_screen_and_a_second_tap_closes_it(self, thumb, tid):
        page = thumb.page
        btn, note = _tip(page, tid)
        btn.tap()
        assert note.is_visible()
        off = [(l, r, vw) for l, r, vw in page.evaluate(FIT) if l < 0 or r > vw]
        assert not off, f"{tid} leaves the screen: {off}"
        btn.tap()
        assert note.is_hidden()
        width = page.evaluate(
            "() => [document.documentElement.scrollWidth, window.innerWidth]")
        assert width[0] <= width[1], width
