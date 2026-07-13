"""CSP header guard — script-src must use a per-request nonce, not
'unsafe-inline'. Regression target: re-introducing 'unsafe-inline'
re-opens XSS injection paths that the Sprint 2 Task 2.4 refactor
closed by moving every inline <script> + onclick= to external files
and data-action= delegation.
"""

import re

import pytest


@pytest.fixture
def client():
    from app import app as flask_app
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    return flask_app.test_client()


def _csp(resp):
    return resp.headers.get("Content-Security-Policy", "")


class TestCSPNonce:
    def test_script_src_contains_nonce(self, client):
        resp = client.get("/")
        csp = _csp(resp)
        assert "script-src" in csp, csp
        assert re.search(r"script-src[^;]*'nonce-[A-Za-z0-9_\-]+'", csp), csp

    def test_script_src_does_not_allow_unsafe_inline(self, client):
        resp = client.get("/")
        csp = _csp(resp)
        # Locate the script-src directive and confirm 'unsafe-inline'
        # isn't in it. The policy still has 'unsafe-inline' on
        # style-src (Lucide + Jinja inline styles), so a plain
        # `"'unsafe-inline' not in csp"` would be a false-negative.
        m = re.search(r"script-src[^;]*", csp)
        assert m, f"no script-src directive: {csp}"
        assert "'unsafe-inline'" not in m.group(0), m.group(0)

    def test_nonce_rotates_per_request(self, client):
        n1 = re.search(r"'nonce-([A-Za-z0-9_\-]+)'", _csp(client.get("/")))
        n2 = re.search(r"'nonce-([A-Za-z0-9_\-]+)'", _csp(client.get("/")))
        assert n1 and n2
        assert n1.group(1) != n2.group(1), \
            "nonce reused across requests — defeats CSP"

    def test_template_inline_script_carries_matching_nonce(self, client):
        resp = client.get("/test-execution?lang=en")
        body = resp.get_data(as_text=True)
        nonce_match = re.search(r"'nonce-([A-Za-z0-9_\-]+)'", _csp(resp))
        assert nonce_match, "no nonce in response CSP"
        nonce = nonce_match.group(1)
        # Every inline <script> in the rendered HTML must carry that
        # nonce. We assert at least one script with the nonce is
        # present (proves the context processor + template wiring
        # land it). External <script src> tags also get the nonce
        # for consistency.
        assert f'nonce="{nonce}"' in body, \
            "rendered template did not interpolate csp_nonce"


class TestHSTS:
    """Strict-Transport-Security must ride only on HTTPS-fronted deploys.
    Emitting it over plain HTTP (local dev) would wrongly pin loopback to
    HTTPS; withholding it on prod (BEHIND_HTTPS=1) leaves a downgrade gap.
    ``_apply_security_headers`` reads ``config.BEHIND_HTTPS`` per request,
    so monkeypatching the module attribute flips the branch cleanly.
    """

    def test_absent_when_not_behind_https(self, client, monkeypatch):
        monkeypatch.setattr("config.BEHIND_HTTPS", False)
        resp = client.get("/healthz")
        assert "Strict-Transport-Security" not in resp.headers

    def test_present_when_behind_https(self, client, monkeypatch):
        monkeypatch.setattr("config.BEHIND_HTTPS", True)
        resp = client.get("/healthz")
        hsts = resp.headers.get("Strict-Transport-Security", "")
        assert "max-age=63072000" in hsts
        assert "includeSubDomains" in hsts
        assert "preload" in hsts
