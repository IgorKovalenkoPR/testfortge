"""E9.4 — every POST is CSRF-protected, or is a declared exemption.

The programme's acceptance criterion is "each new POST has a test with
``WTF_CSRF_ENABLED=True``". Written one test per endpoint that would be
sixty-one tests and a sixty-second endpoint arriving with none — the
failure mode ``engine/route_policy.py`` was built to prevent for access
control. So it is written the same way: derive the list from the URL map,
require every entry to be classified, and fail closed on anything that is
neither protected nor explicitly excused.

The suite as a whole runs with ``WTF_CSRF_ENABLED = False`` (see
``tests/conftest.py``) because the clients post without rendering the
templates that supply a token. That is a convenience with a cost, and the
cost is this exact class of bug: an endpoint passes every test it has and
returns 400 in production, or — worse in the other direction — an
endpoint that was quietly made csrf-exempt for one machine caller keeps
that exemption after it grows a browser-facing form. Both have happened
on this project, which is why the standing rule exists.

Two properties, and the second is the one that makes the first safe:

1. every POST endpoint refuses a request with no token;
2. every endpoint excused from (1) authenticates its callers some other
   way — proven by watching it refuse an unauthenticated request rather
   than take our word for it.
"""
from __future__ import annotations

import re

import pytest

from app import app as flask_app


#: endpoint → why it is csrf-exempt, and what authenticates it instead.
#:
#: A dict rather than a set so the reason travels with the entry: an
#: allowlist of bare names accretes entries nobody can justify or remove.
#: Each of these carries its own bearer/token/flag check, which is the
#: right control for a caller that has no session at all — the Chrome
#: extension, CI, the MCP service. A session-scoped CSRF token is not
#: something they could obtain.
EXEMPT: dict[str, str] = {
    "api_recorder_session_start":
        "the browser extension posts with a recorder token; it has no "
        "session and therefore no session-scoped CSRF token",
    "api_recorder_session_finish": "same recorder token",
    "api_browser_poll":
        "extension short-poll, authenticated by the browser-control token",
    "api_browser_result": "same browser-control token",
    "automation_allure_results":
        "CI posts results with AUTOMATION_INGEST_TOKEN; there is no "
        "browser and no origin to forge from",
    "api_backup_run":
        "the scheduled backup job (E8.4) posts with BACKUP_TOKEN from a "
        "GitHub Actions runner — no browser, no session, and the endpoint "
        "refuses outright when the token is unset",
    "debug_walkthrough_dispatch":
        "a local one-line curl diagnostic, and registered only when the "
        "debug routes are",
}

#: What the CSRF refusal actually says. Asserted rather than the bare 400
#: because a route can answer 400 for its own reasons — a missing field, a
#: bad id — and a test that accepted any 400 would pass for an endpoint
#: that never checked a token at all.
REFUSAL = "Your session expired"


def _post_endpoints() -> list:
    out = []
    for rule in flask_app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        if "POST" not in (rule.methods or set()):
            continue
        out.append(rule)
    return sorted(out, key=lambda r: r.endpoint)


def _url(rule) -> str:
    """A concrete URL for a rule, with placeholder values.

    The values never have to exist: CSRF is checked before the view runs,
    so a 404 from a made-up id would mean the token was not checked, which
    is the finding rather than a fixture problem.
    """
    def _fill(match: re.Match) -> str:
        converter = match.group(1) or ""
        return "1" if "int" in converter else "x"

    return re.sub(r"<(?:([^:>]+):)?([^>]+)>", _fill, rule.rule)


@pytest.fixture
def csrf_client(sign_in, monkeypatch):
    """A client with enforcement on, like production.

    Signed in when the run is authenticated, so the route policy does not
    pre-empt the thing under test: a 302 to the sign-in page would satisfy
    "did not succeed" without a token ever being examined. That is the
    passes-for-the-wrong-reason shape this project has met three times.

    ``monkeypatch.setitem`` rather than a ``try/finally``, because the
    config it edits belongs to the module-level app object every other
    test file shares. A restore that only runs on the happy path would
    leave enforcement on for the rest of the session, and roughly two
    hundred files would then fail with 400s that have nothing to do with
    them. pytest unwinds this one whatever happens here.
    """
    monkeypatch.setitem(flask_app.config, "WTF_CSRF_ENABLED", True)
    monkeypatch.setitem(flask_app.config, "TESTING", True)
    with flask_app.test_client() as c:
        sign_in(c)
        yield c


class TestEveryPostIsClassified:
    def test_there_is_something_to_check(self):
        assert len(_post_endpoints()) > 40, \
            "the URL map looks empty — the rest of this file proves nothing"

    def test_no_exemption_names_a_route_that_no_longer_exists(self):
        """A stale entry is an exemption nobody is looking at.

        The next endpoint to take that name inherits it silently.
        """
        live = {r.endpoint for r in _post_endpoints()}
        stale = sorted(set(EXEMPT) - live)
        assert not stale, stale

    def test_every_exemption_carries_a_reason(self):
        assert all((reason or "").strip() for reason in EXEMPT.values())


@pytest.mark.parametrize(
    "rule", [pytest.param(r, id=r.endpoint) for r in _post_endpoints()])
class TestTheGate:
    def test_a_post_without_a_token_is_refused(self, csrf_client, rule):
        """The property, for one endpoint, derived rather than written out."""
        if rule.endpoint in EXEMPT:
            pytest.skip(f"declared exempt: {EXEMPT[rule.endpoint]}")
        response = csrf_client.post(_url(rule), data={})
        body = response.get_data(as_text=True)
        assert response.status_code == 400, (
            f"{rule.endpoint} accepted a POST with no CSRF token "
            f"({response.status_code})")
        assert REFUSAL in body or '"error": "csrf"' in body, (
            f"{rule.endpoint} answered 400 for some other reason, so this "
            f"test would pass even with the token check removed: {body[:200]}")


class TestTheExemptionsAreSafe:
    """An exemption is a claim that something else authenticates the caller.

    Checked, not trusted. If one of these ever starts doing its work for
    an anonymous caller, it is a public write endpoint and the entry above
    is the only thing that says otherwise.
    """

    @pytest.mark.parametrize(
        "endpoint", [pytest.param(e, id=e) for e in sorted(EXEMPT)])
    def test_an_unauthenticated_caller_is_still_refused(self, csrf_client,
                                                        endpoint):
        rule = next(r for r in _post_endpoints() if r.endpoint == endpoint)
        response = csrf_client.post(_url(rule), data={})
        assert response.status_code in (401, 403, 404), (
            f"{endpoint} is csrf-exempt and answered {response.status_code} "
            f"to a caller with no credential of any kind")


class TestTheHarnessWouldNotice:
    """Guards the guard.

    Every assertion above depends on enforcement actually being switched
    on for the duration. If the fixture stopped working the whole file
    would report a clean pass while checking nothing — the same shape as
    the suite-wide ``WTF_CSRF_ENABLED = False`` that made this file
    necessary.
    """

    def test_enforcement_is_on_inside_the_fixture(self, csrf_client):
        assert flask_app.config["WTF_CSRF_ENABLED"] is True

    def test_a_valid_token_gets_through_the_gate(self, csrf_client):
        """The positive case, or "refused" could just mean "always refused".

        ``/new-session`` is the cheapest protected POST: it clears the
        caller's own workspace and redirects, touching nothing else.
        """
        token = csrf_client.get("/api/csrf-token").get_json()["token"]
        response = csrf_client.post("/new-session",
                                    data={"csrf_token": token})
        assert response.status_code != 400, response.get_data(as_text=True)[:200]

    def test_a_token_from_another_session_does_not_work(self, csrf_client):
        """A token is a session's, not the application's.

        Without this, "the endpoint checked a token" and "the endpoint
        checked *this caller's* token" are the same assertion, and a
        signing scheme with no session binding would satisfy both.
        """
        stolen = csrf_client.get("/api/csrf-token").get_json()["token"]
        with flask_app.test_client() as other:
            response = other.post("/new-session", data={"csrf_token": stolen})
        assert response.status_code == 400, response.status_code
        assert REFUSAL in response.get_data(as_text=True)
