"""CSP header guard — script-src must use a per-request nonce, not
'unsafe-inline'. Regression target: re-introducing 'unsafe-inline'
re-opens XSS injection paths that the Sprint 2 Task 2.4 refactor
closed by moving every inline <script> + onclick= to external files
and data-action= delegation.

That last claim had no test, and 22 on*="" attributes came back into the
templates afterwards — dead under this very CSP: the test-case tabs and
filters did nothing and no destructive form asked for confirmation (live
audit, 2026-10-16). TestNoInlineHandlers pins it now; the browser-side
proof is tests/test_e2e_csp_handlers.py.
"""

import pathlib
import re

import pytest


@pytest.fixture
def client(sign_in):
    from app import app as flask_app
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    c = flask_app.test_client()
    sign_in(c)
    return c


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


ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCRIPT = re.compile(r"<script\b[^>]*>.*?</script>", re.S | re.I)
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
_TAG = re.compile(r"<[a-zA-Z][^<>]*>", re.S)
_HANDLER = re.compile(r"""\son[a-z]+\s*=\s*["']""", re.I)
#: The same thing built as a string in JS (innerHTML, template literals).
_JS_HANDLER = re.compile(r"""<[a-zA-Z][^<>'"`]*\son[a-z]+\s*=""", re.I)


def inline_handlers(html: str) -> list[str]:
    """on*= attributes on tags, outside <script> bodies and Jinja comments."""
    body = _SCRIPT.sub("", _JINJA_COMMENT.sub("", html))
    return [t[:80] for t in _TAG.findall(body) if _HANDLER.search(t)]


class TestNoInlineHandlers:
    def test_no_template_carries_an_on_attribute(self):
        found = {}
        templates = sorted((ROOT / "templates").rglob("*.html"))
        assert len(templates) > 30          # the scan reached the tree
        for path in templates:
            hits = inline_handlers(path.read_text(encoding="utf-8"))
            if hits:
                found[path.relative_to(ROOT).as_posix()] = hits
        assert not found, (
            f"on*= never runs under the CSP nonce — use data-call / "
            f"data-confirm / data-autosubmit (static/js/app.js): {found}")

    def test_no_script_builds_an_on_attribute(self):
        files = sorted((ROOT / "static" / "js").rglob("*.js"))
        assert files
        found = {p.name: _JS_HANDLER.findall(p.read_text(encoding="utf-8"))
                 for p in files}
        assert not {k: v for k, v in found.items() if v}

    @pytest.mark.parametrize("snippet", [
        '<button class="x" onclick="go()">',
        "<form\n   onsubmit='return confirm(\"x\")'>",
        '<select ONCHANGE = "this.form.submit()">',
    ])
    def test_the_scan_sees_every_spelling(self, snippet):
        assert inline_handlers(snippet)

    def test_the_scan_skips_script_bodies_and_data_attributes(self):
        assert not inline_handlers(
            '<script nonce="n">el.onclick = f; // onclick="x"</script>'
            '<button data-call="showTab" data-confirm="Sure?">')
