"""A HEAD request to the sign-in page was processed as a login attempt.

Found on the live staging service while answering "is it ready for a manual
walkthrough": ``HEAD /auth/login`` answered **401** where ``GET`` answers
200. Reproduced with the test client and no network at all, so it is the
application rather than Cloudflare or Render.

The cause is one operator. The view read

    if request.method == "GET":
        return render_template("auth_login.html", …)
    email = (request.form.get("email") or "").strip()
    …

and Werkzeug routes HEAD to a GET rule. ``request.method`` is ``"HEAD"``,
which is not ``"GET"``, so the request fell through into the sign-in logic
and was treated as an attempt with an empty address.

Three consequences, and the third is the one that matters:

* **401 on a page that works.** An uptime monitor — HEAD is a common
  default — reports the sign-in page as failing.
* **A log line per probe**: ``login failed for '': no_such_user``. Real
  attempts get buried in noise that looks like credential stuffing.
* **115 ms against 5 ms**, measured. ``verify_login`` calls
  ``_burn_equivalent_time()`` for an address it cannot find, deliberately,
  so that a missing account costs what a wrong password costs. On a free
  dyno with ``--workers 1`` that makes an unauthenticated HEAD cost a
  password hash.

No lockout state is written — ``verify_login`` returns ``no_such_user``
before touching a row — so this is noise and cost, not a lockout denial of
service. Checked rather than assumed.

``!= "POST"`` rather than adding ``"HEAD"`` to the equality: OPTIONS is
routed here too, and the fail-safe direction is that only a real POST is an
attempt.

The enumeration below is the gate, because the equality form is the natural
way to write this and `/auth/login` was the only route in the whole URL map
that had it — one instance is exactly when a rule is cheap.
"""
from __future__ import annotations

import pathlib
import re

import pytest


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")


class TestTheSignInPage:

    def test_head_answers_what_get_answers(self, anon_client):
        assert anon_client.head("/auth/login").status_code == \
            anon_client.get("/auth/login").status_code == 200

    def test_head_does_not_report_a_failed_login(self, anon_client,
                                                 caplog):
        import logging
        with caplog.at_level(logging.INFO, logger="routes.auth"):
            anon_client.head("/auth/login")
        failed = [r for r in caplog.records if "login failed" in r.message]
        assert not failed, [r.message for r in failed]

    def test_a_real_post_is_still_an_attempt(self, anon_client, caplog):
        """The control. A fix that stopped the POST branch running would
        satisfy both tests above and break signing in."""
        import logging
        with caplog.at_level(logging.INFO, logger="routes.auth"):
            response = anon_client.post(
                "/auth/login",
                data={"email": "nobody@example.invalid",
                      "password": "wrong"})
        assert response.status_code == 401
        assert [r for r in caplog.records if "login failed" in r.message]

    def test_head_does_not_reach_the_password_check(self, anon_client,
                                                    monkeypatch):
        """The cost, asserted as a mechanism rather than as a duration.

        The first version of this test measured wall clock — ``elapsed <
        0.05`` — and failed the moment a second suite ran on the same
        machine. That is the flake class this file's own subject belongs
        to, so it has no business being the assertion: what matters is
        that ``verify_login`` is not reached, and 115 ms against 5 ms was
        only how that showed up.
        """
        from routes import auth as _routes_auth

        calls = []
        real = _routes_auth._auth.verify_login
        monkeypatch.setattr(_routes_auth._auth, "verify_login",
                            lambda *a, **k: calls.append(a) or real(*a, **k))

        anon_client.head("/auth/login")
        assert calls == [], (
            "HEAD reached the password check, which spends "
            "_burn_equivalent_time() on an address it cannot find")

        anon_client.post("/auth/login", data={"email": "x@example.invalid",
                                              "password": "y"})
        assert calls, "a real POST must still reach it"


class TestNoRouteTreatsHeadAsSomethingElse:
    """The enumeration, kept as the rule."""

    @staticmethod
    def _disagreements(client, app):
        out = []
        for rule in app.url_map.iter_rules():
            if "GET" not in (rule.methods or set()):
                continue
            path = str(rule.rule)
            if "<" in path:            # needs an argument; skipped
                continue
            get = client.get(path).status_code
            head = client.head(path).status_code
            if get != head:
                out.append(f"{path} GET {get} / HEAD {head}")
        return out

    def test_the_scan_reaches_the_map(self, client, app):
        checked = [r for r in app.url_map.iter_rules()
                   if "GET" in (r.methods or set()) and "<" not in str(r.rule)]
        assert len(checked) > 20, f"only {len(checked)} argument-free GETs"

    def test_head_agrees_with_get_everywhere(self, client, app):
        bad = self._disagreements(client, app)
        assert not bad, (
            "HEAD is routed to the GET rule, so a view that branches on "
            "`request.method == \"GET\"` runs its POST body for a HEAD "
            "probe. Branch on `!= \"POST\"` instead: " + ", ".join(bad))


def test_the_equality_form_is_gone_from_this_view():
    """Pinning the shape as well as the behaviour: the status test above
    would also pass if somebody added ``or request.method == "HEAD"``,
    which leaves OPTIONS and anything else routed here falling through."""
    source = (pathlib.Path(__file__).resolve().parent.parent / "routes"
              / "auth.py").read_text(encoding="utf-8")
    view = source.split("def auth_login():")[1].split("def ")[0]
    assert 'request.method != "POST"' in view
    assert not re.search(r'request\.method\s*==\s*"GET"', view)
