"""The crawler's HTTP fetch has to work in the shape production calls it.

E11: every URL crawl returned "No strong architecture signals — defaulting
to 'landing'" with zero pages, for every site on the internet. The cause was
one keyword argument:

    _security.safe_opener().open(req, timeout=..., context=ctx)

``safe_opener`` returns an ``OpenerDirector``, whose ``open`` is
``open(fullurl, data=None, timeout=...)``. ``context=`` belongs to
``urllib.request.urlopen``, and was carried over when these call sites were
moved off ``urlopen`` to pick up the redirect policy. So the call raised
``TypeError`` during argument binding — before DNS, before a socket — and the
bare ``except Exception`` in ``_fetch_page`` turned it into ``("", "...")``.
Zero pages then made ``landing`` the winning site type, and every downstream
module reported a plausible-looking low-signal result.

Why the existing suite stayed green: ``tests/test_ssrf_redirects.py`` calls
``safe_opener().open(url, timeout=5)`` — no ``context=``. It exercises a call
shape no production site uses, so it could not see this. These tests assert
on the shape the crawler actually issues, against a real loopback server,
because the property is about what the HTTP client does and mocking the
client would assert the mock.
"""
from __future__ import annotations

import socket
import ssl
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from engine import security, site_crawler


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_PAGE = b"""<!doctype html>
<html><head><title>Assay Office Portal</title></head>
<body>
  <h1>Submit a packet</h1>
  <nav><a href="/services">Services</a><a href="/contact">Contact</a></nav>
  <form action="/login" method="post">
    <input name="username"><input name="password" type="password">
    <button type="submit">Sign In</button>
  </form>
</body></html>
"""


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's contract
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_PAGE)))
        self.end_headers()
        self.wfile.write(_PAGE)

    def log_message(self, *_args):
        pass


@pytest.fixture
def page_server():
    try:
        port = _free_port()
        server = ThreadingHTTPServer(("127.0.0.1", port), _Page)
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
def allow_test_origin(monkeypatch):
    """Let the loopback test server through the SSRF policy.

    It has to be allowed explicitly — the server is on 127.0.0.1, which the
    policy exists to refuse. Only this exact origin is allowed; everything
    else still goes through the real check, so the guard is not disabled.
    """
    real = security.is_safe_external_url

    def _allow(origin: str):
        def patched(url: str) -> bool:
            return True if url.startswith(origin) else real(url)
        monkeypatch.setattr(security, "is_safe_external_url", patched)
        monkeypatch.setattr(site_crawler._security,
                            "is_safe_external_url", patched)

    return _allow


class TestTheOpenerAcceptsATlsContext:
    """The trap itself: passing a context must not be a TypeError."""

    def test_open_rejects_a_context_keyword(self):
        # Documents *why* safe_opener takes the context — so that a future
        # edit moving it back to .open() fails here rather than in a bare
        # except in production.
        opener = security.safe_opener()
        with pytest.raises(TypeError, match="context"):
            opener.open(urllib.request.Request("http://127.0.0.1:1/"),
                        timeout=1, context=ssl.create_default_context())

    def test_safe_opener_takes_the_context_instead(self):
        opener = security.safe_opener(context=ssl.create_default_context())
        assert any(isinstance(h, urllib.request.HTTPSHandler)
                   for h in opener.handlers)

    def test_the_redirect_policy_survives_a_context(self):
        # The context must not displace the handler the SSRF fix installed.
        opener = security.safe_opener(context=ssl.create_default_context())
        assert any(isinstance(h, security._ValidatingRedirectHandler)
                   for h in opener.handlers)


class TestFetchPageReachesAServer:
    def test_a_served_page_comes_back_with_no_error(
            self, page_server, allow_test_origin):
        allow_test_origin(page_server)
        html, err = site_crawler._fetch_page(page_server + "/")
        assert err == "", f"fetch reported an error: {err!r}"
        assert "Assay Office Portal" in html

    def test_a_blocked_host_is_still_refused(self):
        # The guard has to keep working, and its error has to stay
        # distinguishable from a transport failure — that distinction is
        # what makes the SSRF regression testable at all.
        html, err = site_crawler._fetch_page(
            "http://169.254.169.254/latest/meta-data/")
        assert html == ""
        assert err.startswith("blocked:"), err


class TestCrawlProducesRealSignals:
    def test_a_crawl_finds_the_page_rather_than_defaulting_to_landing(
            self, page_server, allow_test_origin):
        allow_test_origin(page_server)
        # Each test gets a fresh port, so the per-URL crawl memo cannot
        # serve a previous test's result here.
        analysis = site_crawler.crawl_site(page_server + "/")
        assert analysis.page_count >= 1, (
            "zero pages crawled — the fetch path is broken again; every "
            "site then reports 'defaulting to landing'")
        assert not analysis.crawl_errors, analysis.crawl_errors
        # The form on the page is the signal the estimator prices; a login
        # form reaching the analysis is the whole point of crawling. This
        # is the assertion that separates "fetched something" from "fetched
        # and understood it" — page_count alone would pass on an empty body.
        assert analysis.has_forms, "the served login form was not detected"
        assert analysis.forms_found, "no form reached forms_found"
        assert analysis.has_auth, (
            "a username+password form did not register as an auth signal")
        # And the site type has to come from evidence rather than the
        # zero-page fallback. A single page genuinely has no strong
        # architecture signals, so that note is legitimate here — what must
        # not happen is 'landing', which site_crawler picks whenever
        # len(pages) <= 2 and which every site on the internet got while
        # the fetch was broken. With an auth form present this is 'app'.
        assert analysis.site_type != "landing", (
            "still classifying by the zero-page fallback")
