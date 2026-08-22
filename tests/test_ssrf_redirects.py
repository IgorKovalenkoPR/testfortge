"""
The SSRF policy has to apply to every hop (E9.8, High).

The guard itself was solid — sixteen probes including decimal-encoded
loopback (`http://2130706433/`), the short form (`http://127.1/`),
IPv4-mapped IPv6 and the cloud-metadata hostnames were all refused. The
finding came from reading what happens *after* it passes:
``urllib.request.urlopen`` follows up to ten redirects by default, and
nothing re-checked them. So an allowed host answering

    302 Location: http://169.254.169.254/latest/meta-data/

had that target fetched, with every guard in ``engine/security.py`` having
run and passed. The check was on the first hop; the attack is on the second.

These tests use a real redirect server on the loopback interface, because
the property is about what the HTTP client does and mocking the client would
assert the mock. The first hop is allowed through an explicit bypass — it has
to be, since the test server is itself on 127.0.0.1 — and the assertion is
about the **second** hop being refused.
"""
from __future__ import annotations

import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from engine import security


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Redirector(BaseHTTPRequestHandler):
    """Answers every path with a 302 to whatever ``target`` is set to."""

    target = "http://169.254.169.254/latest/meta-data/"

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's contract
        if self.path == "/final":
            body = b"arrived"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(302)
        self.send_header("Location", type(self).target)
        self.end_headers()

    def log_message(self, *_args):  # keep pytest output clean
        pass


@pytest.fixture
def redirect_server():
    try:
        port = _free_port()
        server = ThreadingHTTPServer(("127.0.0.1", port), _Redirector)
    except OSError:  # pragma: no cover — sandbox refuses binds
        pytest.skip("cannot bind a loopback socket in this environment")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def allow_first_hop(monkeypatch):
    """Let the loopback test server itself through.

    Without this the first request is refused and the redirect never
    happens, so the test could not reach the behaviour it is about. Only
    the exact test origin is allowed; everything else, including the
    redirect target, goes through the real policy.
    """
    real = security.is_safe_external_url

    def _allow(origin: str):
        def patched(url: str) -> bool:
            if url.startswith(origin):
                return True
            return real(url)
        monkeypatch.setattr(security, "is_safe_external_url", patched)

    return _allow


class TestARedirectCannotEscapeThePolicy:
    def test_the_metadata_endpoint_is_refused_on_the_second_hop(
            self, redirect_server, allow_first_hop, monkeypatch):
        allow_first_hop(redirect_server)
        monkeypatch.setattr(_Redirector, "target",
                            "http://169.254.169.254/latest/meta-data/")
        with pytest.raises(security.UnsafeUrlError):
            security.safe_opener().open(redirect_server + "/start", timeout=5)

    def test_loopback_is_refused_on_the_second_hop(
            self, redirect_server, allow_first_hop, monkeypatch):
        allow_first_hop(redirect_server)
        monkeypatch.setattr(_Redirector, "target", "http://127.0.0.1:1/")
        with pytest.raises(security.UnsafeUrlError):
            security.safe_opener().open(redirect_server + "/start", timeout=5)

    def test_an_rfc1918_address_is_refused_on_the_second_hop(
            self, redirect_server, allow_first_hop, monkeypatch):
        allow_first_hop(redirect_server)
        monkeypatch.setattr(_Redirector, "target", "http://10.0.0.5/")
        with pytest.raises(security.UnsafeUrlError):
            security.safe_opener().open(redirect_server + "/start", timeout=5)

    def test_a_file_scheme_target_is_refused(
            self, redirect_server, allow_first_hop, monkeypatch):
        allow_first_hop(redirect_server)
        monkeypatch.setattr(_Redirector, "target", "file:///etc/passwd")
        with pytest.raises(
                (security.UnsafeUrlError, urllib.error.URLError, ValueError)):
            security.safe_opener().open(redirect_server + "/start", timeout=5)

    def test_the_bare_urlopen_would_have_followed_it(
            self, redirect_server, allow_first_hop, monkeypatch):
        """The bypass, demonstrated rather than described.

        ``urlopen`` follows the redirect and fails only because the metadata
        address is unroutable from here — a network error, not a refusal. On
        a cloud host it is routable and answers. This is the test that says
        why ``safe_opener`` exists, and it is the one that would go green
        again if somebody replaced a call site with ``urlopen``.
        """
        allow_first_hop(redirect_server)
        monkeypatch.setattr(_Redirector, "target", "http://169.254.169.254/x")
        with pytest.raises(Exception) as caught:
            urllib.request.urlopen(redirect_server + "/start", timeout=3)
        # Not a policy refusal: it got as far as trying to reach it.
        assert not isinstance(caught.value, security.UnsafeUrlError)

    def test_a_redirect_to_an_allowed_target_still_works(
            self, redirect_server, allow_first_hop, monkeypatch):
        """The guard must not break ordinary redirects.

        A crawler that refused every 302 would be useless — http→https and
        trailing-slash redirects are most of the web.
        """
        allow_first_hop(redirect_server)
        monkeypatch.setattr(_Redirector, "target", redirect_server + "/final")
        with security.safe_opener().open(redirect_server + "/start",
                                         timeout=5) as resp:
            assert resp.read() == b"arrived"


class TestTheBypassStillApplies:
    def test_the_operator_opt_out_covers_redirects_too(
            self, redirect_server, monkeypatch):
        """``SSRF_ALLOWLIST_BYPASS=1`` is how an operator tests a staging box
        on 192.168.x.y. It has to cover the redirect check as well, or the
        opt-out works for the first request and fails on the second — which
        would read as an intermittent crawl failure rather than as policy."""
        monkeypatch.setenv("SSRF_ALLOWLIST_BYPASS", "1")
        monkeypatch.setattr(_Redirector, "target", redirect_server + "/final")
        with security.safe_opener().open(redirect_server + "/start",
                                         timeout=5) as resp:
            assert resp.read() == b"arrived"


class TestEveryFetchSiteUsesIt:
    """A guard that one call site skips is not a guard.

    Asserted against the source rather than by driving each crawler,
    because the failure mode is somebody adding a fourth fetch with
    ``urlopen`` — which no behavioural test would notice until it was
    exploited.
    """

    @pytest.mark.parametrize("module", ["site_crawler", "site_tester"])
    def test_no_bare_urlopen_remains(self, module):
        import importlib
        import inspect
        src = inspect.getsource(importlib.import_module(f"engine.{module}"))
        offenders = [line.strip() for line in src.splitlines()
                     if "urllib.request.urlopen(" in line
                     and not line.strip().startswith("#")]
        assert not offenders, offenders

    @pytest.mark.parametrize("module", ["site_crawler", "site_tester"])
    def test_the_validating_opener_is_used(self, module):
        import importlib
        import inspect
        src = inspect.getsource(importlib.import_module(f"engine.{module}"))
        # "safe_opener(" not "safe_opener()" — the TLS context is passed as
        # a keyword argument now, so the empty-parens spelling no longer
        # appears. Asserting the exact literal made this guard describe one
        # call shape rather than the property it is about.
        assert "safe_opener(" in src

    @pytest.mark.parametrize("module", ["site_crawler", "site_tester"])
    def test_no_call_site_passes_a_context_to_open(self, module):
        """``OpenerDirector.open`` takes no ``context``; ``urlopen`` does.

        All three fetch sites carried that keyword over when they moved off
        ``urlopen`` to pick up the redirect policy above. Every call then
        raised ``TypeError`` during argument binding — before DNS, before a
        socket — and a bare ``except Exception`` turned it into an empty
        page with an error string nothing rendered. The crawler reported
        "No strong architecture signals" for every site on the internet.

        The sibling test above could not see it: it calls
        ``safe_opener().open(url, timeout=5)``, a shape no production call
        site uses. So this asserts on the shape they *do* use.
        """
        import importlib
        import inspect
        src = inspect.getsource(importlib.import_module(f"engine.{module}"))
        offenders = []
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            call = stripped.find(".open(")
            if call == -1:
                continue
            # Only a ``context=`` that falls *after* ``.open(`` is inside
            # its argument list. The correct form puts it before, on the
            # same line — ``safe_opener(context=ctx).open(req, ...)`` — so
            # a naive "both substrings present" check flags the fix itself.
            if "context=" in stripped[call:]:
                offenders.append(stripped)
        assert not offenders, (
            f"context= passed to OpenerDirector.open in {module}: {offenders}. "
            "Pass it to safe_opener(context=...) instead.")
