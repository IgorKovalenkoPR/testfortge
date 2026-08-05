"""Expired-session recovery on the generation endpoints.

Reproduces the production bug reported on /test-cases: the modal showed
"Could not reach the server — retrying directly." with Elapsed 0s, and
the Retry button never recovered.

What actually happened:
  * Render's free plan sleeps the service after ~15 min and
    ``SESSION_TYPE=filesystem`` sits on an ephemeral disk, so every cold
    start wipes the session store (render.yaml documents this).
  * A tab opened before the restart still holds the old ``csrf_token``.
  * ``POST /test-cases/run-async`` therefore raised CSRFError, and the
    handler answered 400 ``text/plain``.
  * The page script called ``r.json()`` unconditionally; the SyntaxError
    landed in its network ``.catch()``, which reported a connectivity
    failure that had not occurred — and its Retry re-posted the same
    dead token.

conftest sets ``WTF_CSRF_ENABLED = False`` for the whole suite, which is
exactly why this class of bug ships green. Every test here flips it back
on for the duration.
"""
from __future__ import annotations

import re

import pytest

from app import app as flask_app


@pytest.fixture
def csrf_client(sign_in):
    """A client with CSRF enforcement ON, like production."""
    prev = flask_app.config.get("WTF_CSRF_ENABLED")
    flask_app.config["WTF_CSRF_ENABLED"] = True
    flask_app.config["TESTING"] = True
    try:
        with flask_app.test_client() as c:
            # Signed in when the run is authenticated: this file is about
            # CSRF token expiry, and an auth redirect would pre-empt every
            # assertion in it.
            sign_in(c)
            yield c
    finally:
        flask_app.config["WTF_CSRF_ENABLED"] = prev


def _token_from_page(client, path: str = "/test-cases") -> str:
    resp = client.get(path)
    assert resp.status_code == 200, resp.status_code
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"',
                  resp.get_data(as_text=True))
    assert m, "test-cases page must embed a csrf_token field"
    return m.group(1)


# ── The server side ──────────────────────────────────────────────────

class TestCsrfErrorShape:
    def test_json_client_gets_json_not_text_plain(self, csrf_client):
        """A fetch caller must be able to parse the rejection.

        This is the assertion that would have caught the bug: the body
        has to survive JSON.parse so the page can branch on it instead
        of throwing.
        """
        resp = csrf_client.post(
            "/test-cases/run-async",
            data={"input_text": "A requirement"},
            headers={"Accept": "application/json",
                     "X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 400
        assert resp.mimetype == "application/json", resp.mimetype
        body = resp.get_json()
        assert body["error"] == "csrf"
        assert body["reload_required"] is True
        assert "session" in body["message"].lower()

    def test_browser_form_post_still_gets_plain_text(self, csrf_client):
        # Classic form posts have no JSON client to satisfy; keep the
        # human-readable body they have always had.
        resp = csrf_client.post("/test-cases",
                                data={"input_text": "A requirement"})
        assert resp.status_code == 400
        assert resp.mimetype == "text/plain"

    def test_html_accept_header_is_not_treated_as_a_json_client(
            self, csrf_client):
        # Browsers send Accept: text/html,...,application/json;q=0.9 on
        # navigations. That must not flip the response to JSON.
        resp = csrf_client.post(
            "/test-cases",
            data={"input_text": "A requirement"},
            headers={"Accept": "text/html,application/xhtml+xml,"
                               "application/json;q=0.9,*/*;q=0.8"},
        )
        assert resp.status_code == 400
        assert resp.mimetype == "text/plain"


class TestCsrfTokenEndpoint:
    def test_returns_a_token_without_needing_one(self, csrf_client):
        resp = csrf_client.get("/api/csrf-token",
                               headers={"Accept": "application/json"})
        assert resp.status_code == 200
        assert resp.mimetype == "application/json"
        token = resp.get_json()["token"]
        assert token and isinstance(token, str) and len(token) > 20

    def test_minted_token_is_accepted_by_the_generation_endpoint(
            self, csrf_client):
        """The whole point: recovery works without a manual reload."""
        # Simulate the wiped-session tab: post with a stale token.
        stale = csrf_client.post(
            "/test-cases/run-async",
            data={"input_text": "A requirement", "csrf_token": "stale-token"},
            headers={"Accept": "application/json",
                     "X-Requested-With": "XMLHttpRequest"},
        )
        assert stale.status_code == 400
        assert stale.get_json()["error"] == "csrf"

        # Now do what the page script does: fetch a fresh token, replay.
        fresh = csrf_client.get(
            "/api/csrf-token",
            headers={"Accept": "application/json"}).get_json()["token"]
        replay = csrf_client.post(
            "/test-cases/run-async",
            data={"input_text": "A requirement", "csrf_token": fresh},
            headers={"Accept": "application/json",
                     "X-Requested-With": "XMLHttpRequest"},
        )
        # Accepted — whatever the route then decides (202 job accepted,
        # or 400/429 on its own validation), it is no longer a CSRF
        # rejection, which is what "recovered" means here.
        assert replay.status_code != 400 or \
            (replay.get_json() or {}).get("error") != "csrf"

    def test_endpoint_is_get_only(self, csrf_client):
        resp = csrf_client.post("/api/csrf-token")
        assert resp.status_code == 405


class TestPageEmbeddedTokenWorks:
    def test_token_rendered_on_the_page_is_accepted(self, csrf_client):
        token = _token_from_page(csrf_client)
        resp = csrf_client.post(
            "/test-cases/run-async",
            data={"input_text": "A requirement", "csrf_token": token},
            headers={"Accept": "application/json",
                     "X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code != 400 or \
            (resp.get_json() or {}).get("error") != "csrf"


# ── The client side ──────────────────────────────────────────────────
#
# Guards on the rendered JS. These are deliberately string assertions:
# there is no JS test runner in this project, and the specific mistake
# being prevented — calling r.json() on a response that may not be JSON —
# is visible in the source.

#: Every *reachable* page that submits a form with fetch(). All three
#: had the same copy-pasted r.json() mistake.
#:
#: templates/automation.html carries the same pattern but is dead code —
#: Automation QA was merged into Test Execution and GET /automation now
#: redirects, so the template is never rendered.
FETCH_SUBMIT_PAGES = ("/test-cases", "/checklist", "/estimation")


class TestSharedHelper:
    @pytest.fixture
    def app_js(self, client):
        resp = client.get("/static/js/app.js")
        assert resp.status_code == 200
        return resp.get_data(as_text=True)

    def test_helper_reads_text_then_parses(self, app_js):
        assert "TFG.readResponse" in app_js
        assert "r.text()" in app_js
        assert "JSON.parse(" in app_js

    def test_helper_detects_csrf_and_can_mint_a_token(self, app_js):
        assert "TFG.isCsrfFailure" in app_js
        assert "TFG.refreshCsrfToken" in app_js
        assert "/api/csrf-token" in app_js

    def test_helper_sends_json_signalling_headers(self, app_js):
        assert "X-Requested-With" in app_js
        assert "application/json" in app_js


class TestGenerationScriptHardening:
    @pytest.fixture
    def script(self, client):
        return client.get("/test-cases").get_data(as_text=True)

    @pytest.mark.parametrize("path", FETCH_SUBMIT_PAGES)
    def test_no_unguarded_r_json_call_on_any_fetch_page(self, client, path):
        # Match the call site, not the prose: the comments explaining
        # this bug legitimately mention r.json().
        body = client.get(path).get_data(as_text=True)
        assert "return r.json()" not in body, (
            f"{path} calls r.json() on an unknown response type — that is "
            f"what turned an expired session into a bogus "
            f"'Could not reach the server'")

    @pytest.mark.parametrize("path", FETCH_SUBMIT_PAGES)
    def test_every_fetch_page_uses_the_shared_helper(self, client, path):
        body = client.get(path).get_data(as_text=True)
        assert "TFG.readResponse" in body or "window.TFG" in body, (
            f"{path} must route response parsing through window.TFG so the "
            f"CSRF recovery path cannot be forgotten in a copy")

    def test_wires_to_the_shared_helper(self, script):
        assert "window.TFG.readResponse" in script
        assert "window.TFG.isCsrfFailure" in script
        assert "window.TFG.refreshCsrfToken" in script

    def test_offers_reload_not_a_doomed_retry(self, script):
        assert "showReloadRequired" in script
        assert "window.location.reload()" in script

    def test_transport_message_no_longer_claims_a_retry_it_cannot_do(
            self, script):
        # The old copy promised "retrying directly" on submit failure
        # while the code had already stopped retrying.
        assert "Could not reach the server — retrying directly." not in script

    def test_token_refresh_is_capped_at_once_per_submit(self, script):
        assert "csrfRetried" in script
