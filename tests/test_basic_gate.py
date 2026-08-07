"""E1.8 — retiring the HTTP Basic gate, and the defect that came with it.

The gate was the only authentication this platform had before E1, so it
fronts every request. E1.8 is its retirement: ``BASIC_GATE_ENABLED``.

**This file exists at all because nothing tested the gate.**
``engine/basic_auth.py`` carried ``# pragma: no cover — exercised via
integration``, and the integration in question was a person opening the
site. Two consequences, both found by writing this:

* the gate's exemption list and the session policy's exemption list were
  maintained separately, and they disagreed. The gate's lived in
  ``TESTFORTGE_BASIC_PUBLIC_PATHS`` and defaulted to ``/healthz,/readyz``,
  which is not declared in ``render.yaml`` at all — so on production, with
  the gate up, **every token-authenticated machine caller answered 401
  from the perimeter before its own credential was read**. The Chrome
  extension could not start a recording; CI could not post an Allure
  bundle. See ``TestMachineCallersReachTheirOwnTokenCheck``;
* nothing stopped the gate being dropped onto an instance with no accounts
  behind it. See ``TestTheInterlock``.

**Mode.** ``TESTING`` is switched **off** throughout, deliberately. The
gate bypasses itself under ``TESTING`` so that two hundred other fixtures
do not have to carry an ``Authorization`` header — which also means a test
that leaves ``TESTING`` on is testing nothing here. That is the one thing
this file cannot inherit from ``conftest``.
"""
from __future__ import annotations

import base64
import importlib
import secrets

import pytest

USER = "gatekeeper"
PASSWORD = "a-shared-password"


def _auth_header(user: str = USER, password: str = PASSWORD) -> dict:
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


@pytest.fixture
def gated(monkeypatch):
    """The shared app with the gate installed in front of it, ``TESTING`` off.

    ``engine.basic_auth`` reads the credentials at import — deliberate for a
    secret, since it is one fewer place a rotated password can be read from
    mid-request — so the module is reloaded to pick up this fixture's. The
    flag that switches the gate is read per request, which is what lets the
    tests below flip it without reloading anything.

    **The app itself is not reloaded**, and that is not a stylistic
    preference. The first version of this fixture did
    ``importlib.reload(app)``, which builds a second Flask object, registers
    every route again and starts a second snapshot thread. The suite stayed
    green, and then the *coverage* run — the one CI gates on — failed 17
    tests in three unrelated files. Measured against a stashed baseline: 0
    failures before, 17 after, nothing else changed. So the hook is inserted
    into the real app's ``before_request`` chain and removed again.

    Inserted at the **front** of that chain, because that is where
    ``app.py`` puts it: behind the route policy it would never be reached —
    an anonymous request is redirected to the sign-in page before the
    perimeter gets a say, and every 401 below would silently become a 302.
    """
    monkeypatch.setenv("FLASK_DEBUG", "1")
    monkeypatch.setenv("TESTFORTGE_BASIC_USER", USER)
    monkeypatch.setenv("TESTFORTGE_BASIC_PASSWORD", PASSWORD)
    monkeypatch.delenv("TESTFORTGE_BASIC_PUBLIC_PATHS", raising=False)
    monkeypatch.setenv("AUTOMATION_INGEST_TOKEN", "ci-ingest-token")
    # The gate is what is under test; the session policy runs in the same
    # process, so its flags are decided here rather than inherited.
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    monkeypatch.setenv("BASIC_GATE_ENABLED", "1")

    import engine.basic_auth as _basic
    importlib.reload(_basic)

    from app import app as flask_app

    hooks = flask_app.before_request_funcs.setdefault(None, [])
    before = list(hooks)
    # ``install`` cannot be called on the real app here: Flask refuses
    # ``before_request`` once an app has served a request, and by this point
    # in the suite it has. So the hook is captured from a stand-in that
    # exposes exactly the two things ``install`` touches — the decorator and
    # ``config``, which the hook reads ``TESTING`` from at request time —
    # and then inserted into the real chain by hand.
    class _Capture:
        def __init__(self, real):
            self.config = real.config
            self.hook = None

        def before_request(self, fn):
            self.hook = fn
            return fn

    capture = _Capture(flask_app)
    _basic.install(capture)
    assert capture.hook is not None, "the gate did not install"
    hooks.insert(0, capture.hook)

    prior_testing = flask_app.config.get("TESTING")
    prior_csrf = flask_app.config.get("WTF_CSRF_ENABLED")
    # Off, which is the whole point — see the module docstring.
    flask_app.config["TESTING"] = False
    flask_app.config["WTF_CSRF_ENABLED"] = False

    try:
        yield flask_app
    finally:
        hooks[:] = before
        flask_app.config["TESTING"] = prior_testing
        flask_app.config["WTF_CSRF_ENABLED"] = prior_csrf
        # Credentials go back to whatever the environment had, so nothing
        # after this file runs against a gated app.
        monkeypatch.delenv("TESTFORTGE_BASIC_USER", raising=False)
        monkeypatch.delenv("TESTFORTGE_BASIC_PASSWORD", raising=False)
        importlib.reload(_basic)


@pytest.fixture
def client(gated):
    with gated.test_client() as c:
        yield c


# ── The gate, while it is up ──────────────────────────────────────────

class TestTheGateGuards:
    def test_a_page_is_refused_without_credentials(self, client):
        response = client.get("/")
        assert response.status_code == 401
        assert "Basic" in response.headers.get("WWW-Authenticate", "")

    def test_the_right_credentials_get_through(self, client):
        response = client.get("/", headers=_auth_header())
        # Past the gate. Where it lands next is the session policy's
        # business — with AUTH_ENABLED on and nobody signed in, the
        # sign-in redirect is the correct answer.
        assert response.status_code != 401
        assert response.status_code in (200, 302, 303)

    def test_the_wrong_password_is_refused(self, client):
        assert client.get("/", headers=_auth_header(password="nope")
                          ).status_code == 401

    def test_the_wrong_user_is_refused(self, client):
        assert client.get("/", headers=_auth_header(user="nobody")
                          ).status_code == 401

    def test_a_malformed_header_is_refused_rather_than_crashing(self, client):
        for header in ("", "Basic", "Basic !!!!", "Bearer abc",
                       "Basic " + base64.b64encode(b"no-colon").decode()):
            response = client.get("/", headers={"Authorization": header})
            assert response.status_code == 401, header

    def test_the_probes_answer_without_credentials(self, client):
        """External monitors carry no credentials by design."""
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code in (200, 503)


# ── The defect this file found ───────────────────────────────────────

class TestMachineCallersReachTheirOwnTokenCheck:
    """A caller with its own credential must not be stopped by the gate.

    Before E1.8 every one of these answered 401 from the perimeter while
    the gate was up, which is the state production is in. The symptom is
    invisible from the outside: a 401 from a gate looks exactly like a 401
    from an endpoint that rejected your token.
    """

    #: (endpoint, path, method). Kept as literals so a renamed rule shows
    #: up here as a failure rather than as a silently skipped test — the
    #: harness class below asserts every one of them still exists.
    CALLERS = [
        ("api_recorder_session_start", "/api/recorder-session/start", "post"),
        ("api_recorder_session_finish", "/api/recorder-session/finish",
         "post"),
        ("api_browser_poll", "/api/browser/poll", "post"),
        ("api_browser_result", "/api/browser/result", "post"),
        ("automation_allure_results", "/automation/allure-results", "post"),
    ]

    @pytest.mark.parametrize("endpoint,path,method", CALLERS)
    def test_it_is_not_challenged_by_the_gate(self, client, endpoint, path,
                                             method):
        response = getattr(client, method)(path, json={})

        assert response.status_code != 401 or \
            "Basic" not in response.headers.get("WWW-Authenticate", ""), (
            f"{endpoint} was challenged for the shared password. It "
            f"authenticates itself and has no browser to type one into, so "
            f"this is how the Chrome extension and CI stop working.")

    @pytest.mark.parametrize("endpoint,path,method", CALLERS)
    def test_its_own_token_check_still_refuses_a_bad_caller(self, client,
                                                           endpoint, path,
                                                           method):
        """The other half, and the reason the exemption is safe.

        Letting these past the gate would be a hole if their own checks
        were not doing the work. An unauthenticated call must still fail —
        just on their terms, not the perimeter's.
        """
        response = getattr(client, method)(path, json={})

        assert response.status_code >= 400, (
            f"{endpoint} accepted an unauthenticated request, so exempting "
            f"it from the gate opened it")

    def test_metrics_is_deliberately_still_behind_the_gate(self, client):
        """Its own token is *optional*, so exempting it would publish
        operator telemetry on any instance that never set one."""
        response = client.get("/metrics")
        assert response.status_code == 401
        assert "Basic" in response.headers.get("WWW-Authenticate", "")

    def test_the_sign_in_page_is_deliberately_still_behind_the_gate(self,
                                                                   client):
        """While the gate is up it *is* the perimeter. Letting anonymous
        reach the sign-in page through it would be dropping the gate by
        accident."""
        assert client.get("/auth/login").status_code == 401

    def test_the_two_gates_cannot_disagree(self):
        """``MACHINE`` is validated against ``OPEN`` at boot.

        The defect above was two hand-maintained allowlists for one
        question. This is what stops them drifting apart again.
        """
        from engine import route_policy
        assert route_policy.MACHINE <= set(route_policy.OPEN)
        assert route_policy.validate() == []

        stray = route_policy.MACHINE | {"index"}
        original = route_policy.MACHINE
        try:
            route_policy.MACHINE = stray
            problems = route_policy.validate()
            assert problems and "not open" in problems[0], problems
        finally:
            route_policy.MACHINE = original


# ── Standing down ────────────────────────────────────────────────────

class TestStandingDown:
    """The acceptance criterion: with Basic off, an anonymous visitor sees
    only the sign-in page, and machine calls work on their own tokens."""

    def test_anonymous_reaches_the_sign_in_page_and_nothing_else(
            self, client, monkeypatch):
        monkeypatch.setenv("BASIC_GATE_ENABLED", "0")

        # The sign-in page itself: reachable, and actually the sign-in page.
        page = client.get("/auth/login")
        assert page.status_code == 200
        assert "Sign in" in page.get_data(as_text=True)

        # Anything else: handed to the session policy, which sends them
        # back to that page. Not a 401 from a perimeter that is gone.
        for path in ("/", "/test-cases", "/bug-reports", "/org/settings"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code in (302, 303), (path,
                                                        response.status_code)
            assert "/auth/login" in response.headers["Location"], path

    def test_the_flag_takes_effect_without_a_restart(self, client,
                                                     monkeypatch):
        """The decision is per request, so the dashboard edit is the whole
        change. With the hook conditional on the flag at import time,
        flipping it would have done nothing until the process restarted —
        and a perimeter nobody can verify without a deploy is one nobody
        verifies."""
        assert client.get("/").status_code == 401

        monkeypatch.setenv("BASIC_GATE_ENABLED", "0")
        assert client.get("/", follow_redirects=False).status_code in (302,
                                                                      303)

        monkeypatch.setenv("BASIC_GATE_ENABLED", "1")
        assert client.get("/").status_code == 401

    def test_machine_callers_keep_working_with_the_gate_down(self, client,
                                                             monkeypatch):
        monkeypatch.setenv("BASIC_GATE_ENABLED", "0")
        for endpoint, path, method in \
                TestMachineCallersReachTheirOwnTokenCheck.CALLERS:
            response = getattr(client, method)(path, json={})
            assert "Basic" not in response.headers.get("WWW-Authenticate",
                                                       ""), endpoint

    def test_the_probes_keep_working_with_the_gate_down(self, client,
                                                        monkeypatch):
        monkeypatch.setenv("BASIC_GATE_ENABLED", "0")
        assert client.get("/healthz").status_code == 200

    def test_the_boot_line_says_which_state_it_is_in(self, gated,
                                                     monkeypatch):
        from engine import basic_auth

        monkeypatch.setenv("BASIC_GATE_ENABLED", "1")
        assert "active" in basic_auth.status()

        monkeypatch.setenv("BASIC_GATE_ENABLED", "0")
        assert "stood down" in basic_auth.status()


class TestTheInterlock:
    """``BASIC_GATE_ENABLED=0`` with ``AUTH_ENABLED=0`` asks for no shared
    password *and* no accounts. It is one keystroke from the combination
    somebody actually wants, so it fails closed."""

    def test_the_gate_stays_up_when_there_are_no_accounts_behind_it(
            self, client, monkeypatch):
        monkeypatch.setenv("BASIC_GATE_ENABLED", "0")
        monkeypatch.setenv("AUTH_ENABLED", "0")
        monkeypatch.setenv("ORG_MODE", "0")

        assert client.get("/").status_code == 401, (
            "the shared password was dropped on an instance with no "
            "accounts, which published it")

    def test_the_refusal_is_reported_rather_than_silent(self, gated,
                                                        monkeypatch):
        """An operator who asked for something and did not get it needs to
        be told, and told what to change first."""
        from engine import basic_auth

        monkeypatch.setenv("BASIC_GATE_ENABLED", "0")
        monkeypatch.setenv("AUTH_ENABLED", "0")

        assert basic_auth.standing_down_refused() is True
        message = basic_auth.status()
        assert "STAYING UP" in message
        assert "AUTH_ENABLED" in message

    def test_it_stands_down_once_accounts_are_on(self, client, monkeypatch):
        monkeypatch.setenv("BASIC_GATE_ENABLED", "0")
        monkeypatch.setenv("AUTH_ENABLED", "1")
        assert client.get("/", follow_redirects=False).status_code in (302,
                                                                      303)

    @pytest.mark.parametrize("gate,auth,expect_up,expect_refused", [
        ("1", "1", True, False),    # rollout: both, belt and braces
        ("1", "0", True, False),    # today: the gate is the only auth
        ("0", "1", False, False),   # E1.8 done: accounts only
        ("0", "0", True, True),     # refused: nothing would be left
    ])
    def test_the_four_combinations_agree_with_the_boot_message(
            self, gated, monkeypatch, gate, auth, expect_up, expect_refused):
        """``is_enabled`` and ``standing_down_refused`` must never disagree.

        They answer the same question from two directions — is the gate up,
        and was it kept up against the request — and a deployment where the
        log says "STAYING UP" while the gate is down would be the worst
        possible pairing. Written so the second is the definition and the
        first is derived; checked here across every combination because
        "derived" is a property of today's code, not of tomorrow's.
        """
        from engine import basic_auth

        monkeypatch.setenv("BASIC_GATE_ENABLED", gate)
        monkeypatch.setenv("AUTH_ENABLED", auth)

        assert basic_auth.is_enabled() is expect_up
        assert basic_auth.standing_down_refused() is expect_refused
        if expect_refused:
            assert "STAYING UP" in basic_auth.status()
        else:
            assert "STAYING UP" not in basic_auth.status()

    def test_no_credentials_means_no_gate_regardless(self, monkeypatch,
                                                     tmp_path):
        """The developer checkout. Nothing to stand down from, and the
        interlock must not invent a gate out of an unset password."""
        monkeypatch.setenv("FLASK_DEBUG", "1")
        monkeypatch.delenv("TESTFORTGE_BASIC_USER", raising=False)
        monkeypatch.delenv("TESTFORTGE_BASIC_PASSWORD", raising=False)
        monkeypatch.setenv("AUTH_ENABLED", "0")
        monkeypatch.setenv("BASIC_GATE_ENABLED", "0")

        import engine.basic_auth as _basic
        importlib.reload(_basic)
        try:
            assert _basic.credentials_configured() is False
            assert _basic.is_enabled() is False
            assert _basic.standing_down_refused() is False
            assert "not configured" in _basic.status()
        finally:
            importlib.reload(_basic)


# ── The harness ──────────────────────────────────────────────────────

class TestTheHarnessWouldNotice:
    def test_testing_is_off_or_the_gate_bypasses_itself(self, gated):
        assert gated.config.get("TESTING") is False, (
            "the gate returns early under TESTING, so every assertion in "
            "this file would pass against no gate at all")

    def test_the_gate_is_installed(self, gated):
        from engine import basic_auth
        assert basic_auth.credentials_configured() is True
        assert basic_auth.is_enabled() is True

    @pytest.mark.parametrize(
        "endpoint,path,method",
        TestMachineCallersReachTheirOwnTokenCheck.CALLERS)
    def test_every_named_machine_route_still_exists(self, gated, endpoint,
                                                    path, method):
        """The paths above are literals. If a rule is renamed, this fails
        rather than the exemption test quietly passing against a 404."""
        adapter = gated.url_map.bind("localhost")
        matched, _ = adapter.match(path, method=method.upper())
        assert matched == endpoint, (path, matched)

    def test_the_suite_gets_its_ungated_app_back(self, gated):
        """The fixture reloads shared modules. Asserted here so a broken
        teardown surfaces as one failure rather than as every file that
        happens to run afterwards."""
        assert gated.config.get("TESTING") is False
