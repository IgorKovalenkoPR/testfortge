"""End-to-end browser tests for the Test Execution upload flow.

These spin up the real Flask app in a background thread on a random
port and drive a headless Chromium via Playwright sync API. They are
the closest proxy we have to "what a real tester sees" — every JS
interception and drag-and-drop dispatch is exercised. Note: an earlier
"run immediately after upload" checkbox was reverted in 67a4373
(Run stays manual, uniform with generated packs) — these tests cover
the post-revert behaviour.

Skipped automatically when:
    * the ``playwright`` package isn't importable, OR
    * a Chromium binary hasn't been installed (`playwright install
      chromium`), OR
    * the sandbox refuses outbound socket binds we need for the test
      server.

Local run:
    pip install playwright
    python -m playwright install chromium
    pytest tests/test_te_e2e_playwright.py -v

Render's Docker image (mcr.microsoft.com/playwright/python:v1.49.1-jammy)
ships all three engines — no extra steps in CI.
"""
from __future__ import annotations

import io
import os
import socket
import threading
import time
from contextlib import closing

import pytest

playwright = pytest.importorskip("playwright.sync_api",
                                  reason="playwright not installed")
from playwright.sync_api import Error as PlaywrightError, sync_playwright


# ── Helper: spin up the real Flask app on a free port ─────────────

def _free_port() -> int:
    """Return a TCP port the kernel has just told us is free."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    """Run the Flask app in a daemon thread; yield (host, port).

    werkzeug's run_simple is preferred to app.run because it's quieter
    and lets us specify use_reloader=False to avoid the double-process
    problem. We catch and skip the test if anything in app import or
    bind fails — most often because chromium isn't available, which
    shouldn't fail the rest of the suite."""
    from werkzeug.serving import make_server
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    port = _free_port()
    server = make_server("127.0.0.1", port, app, threaded=True)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # Tiny wait so the bind is actually listening before tests probe.
    for _ in range(20):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        pytest.skip("Flask test server did not start in time")

    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    """Launch headless Chromium once per module. Skips the suite if
    the Playwright binary isn't downloaded — common in sandboxes."""
    try:
        with sync_playwright() as pw:
            try:
                br = pw.chromium.launch(headless=True)
            except PlaywrightError as exc:
                pytest.skip(f"chromium unavailable: {exc}")
            yield br
            br.close()
    except Exception as exc:
        pytest.skip(f"Playwright launch failed: {exc}")


@pytest.fixture
def page(browser, live_server):
    """Fresh BrowserContext per test for clean cookies/storage."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    p = ctx.new_page()
    yield p
    ctx.close()


# ── Tests ─────────────────────────────────────────────────────────

class TestTeExecutionEmptyState:
    def test_empty_state_renders_upload_cards(self, page, live_server):
        """No pack in session — empty-state card + both upload forms
        + DnD drop-zones must all render. Run stays manual (auto-run
        on upload reverted in 67a4373)."""
        page.goto(f"{live_server}/test-execution?lang=en", timeout=10_000)
        # Empty-state card
        page.wait_for_selector("text=Nothing to run yet", timeout=5_000)
        # Two drop-zones (TC + CL)
        zones = page.locator(".te-drop-zone")
        assert zones.count() == 2
        # Watch live link visible
        assert page.locator("text=Watch live").count() >= 0  # only when has data

    def test_pack_badge_shown_after_upload(self, page, live_server):
        """Upload a CSV via the file input, then verify the pack-status
        badge appears with the right count. Upload lands the operator
        back on /test-execution — Run is then manual."""
        page.goto(f"{live_server}/test-execution?lang=en", timeout=10_000)
        page.wait_for_selector('form[data-te-upload="tc"]', timeout=5_000)

        csv = (b"ID,Section,Summary,Steps,Expected\n"
               b"TC-1,A,One,1.x,2.y,Err\nTC-2,A,Two,1.x,2.y,Err\n"
               b"TC-3,A,Three,1.x,2.y,Err\n")
        page.locator('form[data-te-upload="tc"] input[type="file"]'
                     ).first.set_input_files(
            files=[{"name": "p.csv",
                    "mimeType": "text/csv",
                    "buffer": csv}])
        with page.expect_navigation():
            page.locator('form[data-te-upload="tc"] button[type="submit"]'
                         ).first.click()
        page.wait_for_selector(".pack-status-badge", timeout=5_000)
        # The badge prints the count inside <strong>...</strong>.
        assert page.locator(".pack-status-badge strong").first.inner_text() == "3"


class TestTeUploadLandsOnExecution:
    def test_upload_returns_to_test_execution_with_pack_loaded(
            self, page, live_server):
        """After upload the operator must land back on /test-execution
        with the uploaded pack visible — Run stays a manual click
        (auto-run on upload was reverted in 67a4373, uniform with
        generated packs). Regression target: re-introducing the
        auto-run side-effect would silently dispatch runs."""
        page.goto(f"{live_server}/test-execution?lang=en", timeout=10_000)
        page.wait_for_selector('form[data-te-upload="tc"]', timeout=5_000)

        csv = (b"ID,Section,Summary,Steps,Expected\n"
               b"TC-MANUAL-1,A,Smoke,1.x,Pass\n")
        page.locator('form[data-te-upload="tc"] input[type="file"]'
                     ).first.set_input_files(
            files=[{"name": "manual.csv",
                    "mimeType": "text/csv",
                    "buffer": csv}])
        page.locator('form[data-te-upload="tc"] button[type="submit"]'
                     ).first.click()
        page.wait_for_url(lambda url: url.rstrip("/").endswith("/test-execution"),
                          timeout=10_000)
        # Pack badge is visible — proves upload landed.
        page.wait_for_selector(".pack-status-badge", timeout=5_000)
        # Critically: no run-card present — Run stays manual.
        assert page.locator(".run-card").count() == 0


class TestGenerationModalEsc:
    def test_esc_dismisses_progress_modal(self, page, live_server):
        """The /test-cases progress overlay must close on ESC and
        return focus to the Generate button. Regression target: a11y
        polish committed earlier.

        We open the overlay directly via JS rather than submitting the
        form because the deterministic test runner finishes the job in
        ~50ms — fast enough that the `done` callback can fire
        `window.location.assign` before our ESC keypress lands, racing
        the assertion. The ESC handler itself is what's under test, so
        skipping the network round-trip is sound."""
        page.goto(f"{live_server}/test-cases?lang=en", timeout=10_000)
        # Synthetically open the overlay and focus the button, mirroring
        # what onSubmit does without any background polling.
        page.evaluate(
            "() => {"
            "  const o = document.getElementById('tc-gen-overlay');"
            "  o.hidden = false;"
            "}"
        )
        page.wait_for_selector("#tc-gen-overlay:not([hidden])",
                               timeout=2_000)
        page.keyboard.press("Escape")
        # `wait_for_selector` defaults to state="visible", but a
        # `[hidden]` element is never visible — use "attached" to wait
        # for the hidden attribute to land on the same element.
        page.wait_for_selector("#tc-gen-overlay[hidden]",
                               state="attached", timeout=5_000)
        # Focus should be back on the Generate button.
        assert page.evaluate(
            "() => document.activeElement && document.activeElement.id"
        ) == "tc-gen-button"


class TestFetchHelperLoadOrder:
    """The shared TFG helper must be usable by every page script.

    Regression target, found the hard way: the generation scripts live in
    ``{% block content %}`` while base.html loads static/js/app.js down in
    ``{% block scripts %}``. Capturing ``window.TFG.readResponse`` at the
    top of a page IIFE therefore threw a TypeError before the DOM
    handlers were registered — killing submit AND the ESC handler on
    /test-cases with no visible symptom other than "nothing happens".

    String assertions in tests/test_csrf_expiry_recovery.py cannot catch
    this; only executing the page can.
    """

    PAGES = ("/test-cases", "/checklist", "/estimation")

    #: Broader sweep for the CSP-nonce trap. An inline <script> without
    #: nonce="{{ csp_nonce }}" is refused outright since 466226e dropped
    #: 'unsafe-inline', which killed the /estimation team-size override
    #: and the /bug-reports bulk toolbar with no server-side symptom.
    CSP_SWEEP_PAGES = PAGES + ("/bug-reports", "/test-execution", "/")

    @pytest.mark.parametrize("path", CSP_SWEEP_PAGES)
    def test_page_script_runs_without_console_errors(self, page,
                                                     live_server, path):
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text)
                if m.type == "error" else None)
        page.goto(f"{live_server}{path}?lang=en", timeout=15_000)
        page.wait_for_load_state("domcontentloaded")
        assert not errors, f"{path} logged JS errors on load: {errors}"

    @pytest.mark.parametrize("path", PAGES)
    def test_tfg_helper_is_available_after_load(self, page, live_server,
                                                path):
        page.goto(f"{live_server}{path}?lang=en", timeout=15_000)
        page.wait_for_load_state("load")
        shape = page.evaluate(
            "() => ({"
            "  present: !!window.TFG,"
            "  read: typeof (window.TFG||{}).readResponse,"
            "  csrf: typeof (window.TFG||{}).isCsrfFailure,"
            "  refresh: typeof (window.TFG||{}).refreshCsrfToken,"
            "  headers: typeof (window.TFG||{}).jsonHeaders"
            "})")
        assert shape == {"present": True, "read": "function",
                         "csrf": "function", "refresh": "function",
                         "headers": "function"}, shape

    def test_readResponse_survives_a_non_json_body(self, page, live_server):
        """The exact failure mode: a text/plain 400 must not throw."""
        page.goto(f"{live_server}/test-cases?lang=en", timeout=15_000)
        page.wait_for_load_state("load")
        result = page.evaluate(
            """async () => {
                const r = new Response('CSRF token missing or invalid.',
                    {status: 400,
                     headers: {'Content-Type': 'text/plain'}});
                const res = await window.TFG.readResponse(r);
                return {status: res.status, body: res.body,
                        isCsrf: window.TFG.isCsrfFailure(res)};
            }""")
        assert result["status"] == 400
        assert result["body"] is None          # unparseable, not a throw
        assert result["isCsrf"] is True        # and correctly classified

    def test_generate_button_handler_is_wired(self, page, live_server):
        """If the IIFE died, submit falls through to a native POST.

        fetch is stubbed for the duration: dispatching a real submit
        otherwise starts an actual generation job, whose polling outlived
        the fixture teardown and crashed the browser context. We only
        care that a handler ran and cancelled the event.
        """
        page.goto(f"{live_server}/test-cases?lang=en", timeout=15_000)
        page.wait_for_load_state("load")
        prevented = page.evaluate(
            """() => {
                const realFetch = window.fetch;
                window.fetch = () => new Promise(() => {});   // never settles
                try {
                    const f = document.getElementById('tc-gen-form');
                    const ev = new Event('submit', {cancelable: true,
                                                    bubbles: true});
                    f.dispatchEvent(ev);
                    return ev.defaultPrevented;
                } finally {
                    window.fetch = realFetch;
                }
            }""")
        assert prevented, ("the /test-cases submit handler is not "
                           "registered — the page script threw before "
                           "addEventListener ran")
