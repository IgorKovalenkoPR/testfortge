"""What the REC overlay says once the extension is no longer there.

Reloading the extension while a recording is running orphans every
content script already injected into a page. The script keeps running --
its listeners, its timers and its overlay all survive -- but the bridge
it talks over is gone: ``chrome.runtime.sendMessage`` throws
``Extension context invalidated.`` **synchronously**, and
``chrome.runtime.id`` goes undefined.

Observed on staging 2026-08-29 at 21:11:33. The badge went on blinking
REC, the counter held at the last number it had managed to fetch, and
Stop still looked like a button that would save the session. None of
that was true: ``emitStep`` threw before it could reach
``updateOverlayCount``, so the counter froze at exactly the value that
made the lie plausible, and Stop threw the same way -- its callback never
ran, so it could not even fall back to "Retry". Every step the tester
took after that moment was discarded by a UI that said it was recording
them.

An overlay that stops working is a nuisance. An overlay that keeps
claiming to work is a lost test session, because the tester has no
reason to stop and start over.

These tests run the real content script in a real browser against a stub
that dies the way Chrome dies.
"""
from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright not installed")

from playwright.sync_api import sync_playwright            # noqa: E402


CONTENT_JS = (pathlib.Path(__file__).resolve().parent.parent
              / "extension" / "content.js")

# Same shape as the ladder harness's stub, plus a kill switch that
# reproduces invalidation exactly: `sendMessage` throws where it is
# called (not into a callback), and `chrome.runtime.id` disappears.
# Chrome offers no event for this -- the id and the throw are the only
# two signals a content script gets, so they are the only two the stub
# provides.
STUB = """
window.__steps = [];
window.__dead = false;
window.chrome = {
  runtime: {
    id: 'stub-extension-id',
    lastError: undefined,
    sendMessage: function (msg, cb) {
      if (window.__dead) {
        throw new Error('Extension context invalidated.');
      }
      if (msg && msg.type === 'append_step') { window.__steps.push(msg.step); }
      var reply = {};
      if (msg && msg.type === 'is_active_for_tab') { reply = {active: true}; }
      if (msg && msg.type === 'get_state') {
        reply = {active: true, steps_buffer: window.__steps};
      }
      if (cb) setTimeout(function () { cb(reply); }, 0);
    },
    onMessage: {addListener: function () {}},
  },
};
window.__kill = function () {
  window.__dead = true;
  delete window.chrome.runtime.id;
};
// Not decoration. `page.evaluate` invokes its result when that result is
// a function, which is how `"() => 1"` works -- and the completion value
// of the assignment above is the kill switch itself. Without a trailing
// statement Playwright pulls the trigger during setup, and every test
// below runs against a context that died before the recording started.
void 0;
"""

PAGE = """
<!doctype html><meta charset=utf-8>
<button id=one>One</button>
<button id=two>Two</button>
"""

# Reading the overlay means reaching into its shadow root, the same way
# a tester reads it with their eyes: whatever text is actually painted.
READ_OVERLAY = """
() => {
  const host = document.getElementById('testfortge-recorder-host');
  if (!host || !host.shadowRoot) return null;
  const sr = host.shadowRoot;
  const dot = sr.querySelector('.dot');
  const btn = sr.querySelector('button');
  return {
    label: (sr.querySelector('.label') || {}).textContent || '',
    count: (sr.querySelector('.count') || {}).textContent || '',
    button: btn ? btn.textContent : null,
    button_disabled: btn ? btn.disabled : null,
    dot_animation: dot ? getComputedStyle(dot).animationName : '',
    // The badge, not the stylesheet that dresses it -- see
    // WAIT_LOST below for what reading the whole root cost.
    all_text: (sr.querySelector('.ov') || {}).textContent || '',
  };
}
"""

# The overlay has settled into its dead state.
#
# Reads the badge, not the shadow root. A shadow root's textContent
# includes its ``<style>`` element, and the dead state is styled by rules
# named ``.ov.lost`` — so the first version of this matched "lost" in the
# stylesheet and was true from the moment the overlay mounted. Eight
# waits that never waited. Every test that used it still passed, because
# each one clicked first and the click flipped the badge synchronously,
# so the assertions afterwards were reading the right thing for the wrong
# reason. The first test to actually need the wait — the one where
# nothing is clicked and a timer does the work — is the one it failed.
WAIT_LOST = """
() => {
  const h = document.getElementById('testfortge-recorder-host');
  const sr = h && h.shadowRoot;
  const ov = sr && sr.querySelector('.ov');
  return !!ov && /lost|disconnect/i.test(ov.textContent);
}
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


@pytest.fixture()
def recorded(browser):
    """A page mid-recording: overlay up, steps already captured."""
    page = browser.new_page()
    try:
        page.set_content(PAGE)
        page.evaluate(STUB)
        page.add_script_tag(content=CONTENT_JS.read_text(encoding="utf-8"))
        page.click("#one")
        page.wait_for_function("window.__steps.length > 0", timeout=5000)
        # The overlay must be up and honest before the interesting part,
        # otherwise the tests below would pass on a page that never
        # recorded anything at all.
        page.wait_for_function(
            """() => {
              const h = document.getElementById('testfortge-recorder-host');
              const sr = h && h.shadowRoot;
              const c = sr && sr.querySelector('.count');
              return !!c && /2 steps\\b/.test(c.textContent);
            }""",
            timeout=5000)
        yield page
    finally:
        page.close()


def _overlay(page) -> dict:
    return page.evaluate(READ_OVERLAY)


def test_overlay_is_honest_while_the_extension_is_alive(recorded):
    # The control. Everything below is a claim about what changes when
    # the context dies, and it is only a claim if this is true first.
    before = _overlay(recorded)
    assert before is not None, "overlay never mounted -- harness is broken"
    assert "REC" in before["label"]
    # goto + the click. The number matters only because the
    # frozen version of it is the lie under test.
    assert before["count"].strip() == "2 steps"
    assert before["dot_animation"] == "blink"
    assert before["button"].strip() == "Stop"
    assert before["button_disabled"] is False
    # And the condition the other tests wait on must be FALSE here.
    # Without this line the wait can quietly become vacuous again — it
    # already did once, matching ".ov.lost" inside the stylesheet — and a
    # wait that returns immediately turns every timing assertion below it
    # into a coin toss that usually lands the right way up.
    assert recorded.evaluate(WAIT_LOST) is False, (
        "WAIT_LOST is true on a live badge — the waits below wait for "
        "nothing")


def test_the_badge_corrects_itself_without_being_touched(recorded):
    """Measured on staging 2026-08-30, walking the practice form.

    The extension was reloaded mid-recording and the badge went on
    saying ``REC`` over ``7 steps`` — until the next click, which
    flipped it to ``Recording lost`` correctly. Detection was lazy: the
    context is only found dead when something next tries to use it, and
    every test in this file clicked first, so none of them could see the
    window in between.

    A tester who reloads their extension and then *looks* at the badge
    gets the original lie for as long as they keep looking. Whether they
    happen to click next is not a property of the fix. So the script
    watches the bridge while the overlay is up: reading
    ``chrome.runtime.id`` costs no message and no round trip, and it is
    the same signal ``sendToBackground`` already trusts.
    """
    # Drain the content script's own pending work first. The DOM
    # snapshot is debounced 500 ms behind the opening goto, and if it is
    # still in flight when the context dies it throws, lands in
    # `onContextLost`, and corrects the badge by itself. That is a real
    # path and a welcome one -- but it is not the one under test, and
    # the first version of this test could not tell the two apart: it
    # passed with `startAliveWatch` deleted, riding a timer it never
    # mentioned. Measured, not reasoned about.
    recorded.wait_for_timeout(1200)
    assert recorded.evaluate(WAIT_LOST) is False, (
        "the badge died before the context did")

    # Belt to that brace, and the sharper of the two. The watch reads
    # `chrome.runtime.id`; it sends nothing. So a badge that corrects
    # itself while this counter is still zero was corrected by the
    # watch, and by nothing else that could have been in flight.
    recorded.evaluate("""() => {
      window.__calls = 0;
      const inner = window.chrome.runtime.sendMessage;
      window.chrome.runtime.sendMessage = function (m, cb) {
        window.__calls++;
        return inner(m, cb);
      };
    }""")
    recorded.evaluate("window.__kill()")
    # Deliberately no click, no keystroke, no scroll. The page is left
    # exactly as the tester left it.
    recorded.wait_for_function(WAIT_LOST, timeout=8000)
    assert recorded.evaluate("window.__calls") == 0, (
        "something tried to send and found the bridge gone -- that is "
        "the lazy path, not the watch")
    after = _overlay(recorded)
    assert "TestForTge REC" not in after["label"], after
    assert after["dot_animation"] == "none", after


#: Neuters `setInterval` before the content script loads, so the watch
#: registers a timer that can never fire. Whatever corrects the badge
#: after this is not the timer -- which is the whole point, because in
#: the browser the timer is exactly what stops being allowed to run.
NO_TIMERS = """
window.__intervalsRegistered = 0;
window.setInterval = function () { window.__intervalsRegistered++; return 0; };
void 0;
"""


@pytest.fixture()
def recorded_without_timers(browser):
    """Mid-recording, with the watch's heartbeat disabled."""
    page = browser.new_page()
    try:
        page.set_content(PAGE)
        page.evaluate(STUB)
        page.evaluate(NO_TIMERS)
        page.add_script_tag(content=CONTENT_JS.read_text(encoding="utf-8"))
        page.click("#one")
        page.wait_for_function("window.__steps.length > 0", timeout=5000)
        page.wait_for_timeout(1200)          # drain the snapshot debounce
        assert page.evaluate("window.__intervalsRegistered") >= 1, (
            "the watch registered no timer at all -- this fixture proves "
            "nothing about the listener if the watch never armed")
        assert page.evaluate(WAIT_LOST) is False
        yield page
    finally:
        page.close()


def test_coming_back_to_the_tab_corrects_the_badge_at_once(
        recorded_without_timers):
    """The timer is not allowed to be the only answer.

    Reloading an extension happens on chrome://extensions, so the
    recording tab is hidden at the moment its context dies -- and Chrome
    throttles timers in hidden tabs, intensively so (once a minute)
    after five minutes out of sight. Measured while setting up the live
    check on 2026-08-30: the tab sat at ``visibilityState: "hidden"``
    and even synthetic clicks stopped landing on it.

    The tester's next act is to come back and look. That is when the
    badge has to be right, and returning to a tab is not a click.
    """
    page = recorded_without_timers
    page.evaluate("window.__kill()")
    # Returning to the tab. Not an interaction with the page: no
    # listener the recorder installs for capturing steps hears this.
    page.evaluate("""() => {
      document.dispatchEvent(new Event('visibilitychange'));
    }""")
    page.wait_for_function(WAIT_LOST, timeout=3000)
    after = _overlay(page)
    assert "TestForTge REC" not in after["label"], after
    assert after["dot_animation"] == "none", after


def test_coming_back_to_the_window_counts_too(recorded_without_timers):
    # `visibilitychange` does not fire when the tab was already the
    # active one and the whole *window* lost focus -- alt-tabbing to
    # Chrome's own extensions window and back, which is the exact route
    # a tester takes to reload an extension. Without this the badge
    # waits for the throttled timer in the one workflow that produces
    # this bug most often.
    page = recorded_without_timers
    page.evaluate("window.__kill()")
    page.evaluate("() => window.dispatchEvent(new Event('focus'))")
    page.wait_for_function(WAIT_LOST, timeout=3000)
    assert _overlay(page)["dot_animation"] == "none"


def test_a_hidden_page_is_not_mistaken_for_a_visible_one(
        recorded_without_timers):
    # `visibilitychange` fires on the way out as well as the way in.
    # Checking on the way out is harmless but pointless, and a handler
    # that ignores the state is a handler that will do the wrong thing
    # when something else is hung off it later.
    page = recorded_without_timers
    page.evaluate("""() => {
      window.__checked = 0;
      Object.defineProperty(document, 'visibilityState',
          {configurable: true, get: () => 'hidden'});
    }""")
    page.evaluate("window.__kill()")
    page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_timeout(400)
    assert page.evaluate(WAIT_LOST) is False, (
        "the badge was corrected while the page was still hidden")


def test_overlay_stops_claiming_to_record_after_invalidation(recorded):
    recorded.evaluate("window.__kill()")
    recorded.click("#two")
    recorded.wait_for_function(WAIT_LOST, timeout=5000)
    after = _overlay(recorded)
    # The badge must not still read as a live recording.
    assert "TestForTge REC" not in after["label"], (
        f"overlay still claims to be recording: {after!r}")


def test_frozen_counter_is_replaced_not_left_standing(recorded):
    # The specific lie: "2 steps" is a true statement about the past and
    # a false one about the present. It has to go, not stay put.
    recorded.evaluate("window.__kill()")
    recorded.click("#two")
    recorded.wait_for_function(WAIT_LOST, timeout=5000)
    assert "step" not in _overlay(recorded)["count"].lower()


def test_the_blinking_stops(recorded):
    recorded.evaluate("window.__kill()")
    recorded.click("#two")
    recorded.wait_for_function(WAIT_LOST, timeout=5000)
    assert _overlay(recorded)["dot_animation"] == "none"


def test_nothing_in_the_dead_badge_still_wears_the_recording_colour(recorded):
    # Colour carries the claim faster than the words do: a tester
    # glancing at the corner of a page sees orange-and-red and reads
    # "recording", whatever the label says. Every element that used the
    # live palette has to leave it.
    live = recorded.evaluate("""() => {
      const sr = document.getElementById('testfortge-recorder-host')
          .shadowRoot;
      return ['.dot', '.label', '.stop'].map(
          s => getComputedStyle(sr.querySelector(s))[
              s === '.label' ? 'color' : 'backgroundColor']);
    }""")
    recorded.evaluate("window.__kill()")
    recorded.click("#two")
    recorded.wait_for_function(WAIT_LOST, timeout=5000)
    dead = recorded.evaluate("""() => {
      const sr = document.getElementById('testfortge-recorder-host')
          .shadowRoot;
      return ['.dot', '.label', '.stop'].map(
          s => getComputedStyle(sr.querySelector(s))[
              s === '.label' ? 'color' : 'backgroundColor']);
    }""")
    for selector, was, now in zip(['.dot', '.label', '.stop'], live, dead):
        assert was != now, f"{selector} kept its live colour ({was})"


def test_stop_no_longer_offers_a_save_it_cannot_make(recorded):
    recorded.evaluate("window.__kill()")
    recorded.click("#two")
    recorded.wait_for_function(WAIT_LOST, timeout=5000)
    assert _overlay(recorded)["button"].strip() != "Stop"


def test_the_remaining_control_actually_works(recorded):
    # Whatever replaces Stop must not itself be a dead button -- it runs
    # in the same context that just lost its bridge, so it can only be
    # something the page can do alone.
    recorded.evaluate("window.__kill()")
    recorded.click("#two")
    recorded.wait_for_function(WAIT_LOST, timeout=5000)
    recorded.evaluate("""() => {
      document.getElementById('testfortge-recorder-host')
          .shadowRoot.querySelector('button').click();
    }""")
    recorded.wait_for_function(
        "() => document.getElementById('testfortge-recorder-host') === null",
        timeout=5000)


def test_a_transient_error_does_not_kill_a_live_recording(recorded):
    # The other way to get this wrong. An MV3 service worker that was
    # evicted answers the first message after it wakes with
    # `lastError: "Could not establish connection."` and is then
    # perfectly healthy. Treating any lastError as death would tear the
    # overlay down mid-session over a wake-up — a self-inflicted version
    # of the bug being fixed. The id is what separates the two, so a
    # context that still has one must survive.
    recorded.evaluate("""() => {
      const inner = window.chrome.runtime.sendMessage;
      window.chrome.runtime.sendMessage = function (m, cb) {
        inner(m, function (r) {
          window.chrome.runtime.lastError =
              {message: 'Could not establish connection.'};
          try { if (cb) cb(r); } finally {
            window.chrome.runtime.lastError = undefined;
          }
        });
      };
    }""")
    recorded.click("#two")
    recorded.wait_for_timeout(300)
    after = _overlay(recorded)
    assert "REC" in after["label"], (
        f"a woken service worker was mistaken for a dead one: {after!r}")
    assert after["dot_animation"] == "blink"


def test_no_steps_are_invented_after_the_bridge_dies(recorded):
    recorded.evaluate("window.__kill()")
    before = recorded.evaluate("window.__steps.length")
    recorded.click("#two")
    recorded.click("#one")
    assert recorded.evaluate("window.__steps.length") == before


def test_a_dead_context_is_not_retried_on_every_interaction(recorded):
    # Once the context is gone it never comes back, so the script must
    # stop calling into it -- not keep throwing on every click. A page
    # that throws per event is how a tester's console fills with noise
    # that hides the one message that mattered.
    recorded.evaluate("""() => {
      window.__calls = 0;
      const inner = window.chrome.runtime.sendMessage;
      window.chrome.runtime.sendMessage = function (m, cb) {
        window.__calls++;
        return inner(m, cb);
      };
    }""")
    recorded.evaluate("window.__kill()")
    recorded.click("#two")
    recorded.wait_for_function(WAIT_LOST, timeout=5000)
    calls_after_first = recorded.evaluate("window.__calls")
    recorded.click("#one")
    recorded.click("#two")
    assert recorded.evaluate("window.__calls") == calls_after_first, (
        "content script kept calling a context it knows is gone")
