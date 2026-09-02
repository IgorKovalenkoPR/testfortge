"""The diagnostics link on /estimation opened a 403 in a new tab.

Same enumeration as ``tests/test_settings_storage_admin_only.py``, widened
from ``<form action=…>`` to ``<a href=…>``: every ``url_for`` in
``templates/`` naming an endpoint that ``engine.route_policy.POLICY`` gates
above "user", checked for an enclosing ``{% if is_admin %}``. One real hit —
the rest were "login"-role links, which is just "be signed in".

Measured with a ``user``-role member:

    GET /estimation             200, the mockup-diagnostics strip renders
                                with its "diagnostics JSON" link
    GET /estimation/diag        403

``target="_blank"``, so the member gets a bare 403 in a new tab with the
half-filled estimation form still open behind it.

The failure flash said it too. Any unhandled error in ``estimation_run``
told every caller to "open /estimation/diag for diagnostics" — advice that
only works for an admin, given to somebody who is already looking at a
failure. It is now appended only when the caller can act on it.

The strip's badges stay for everyone. They are already rendered, and knowing
that poppler is missing is exactly what stops a member re-running a pass
that cannot work; the link is the part that leads somewhere they cannot go.
"""
from __future__ import annotations

import pathlib
import re
import secrets

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import permissions as _perm
from engine import route_policy as _policy
from engine import session_timeout as _timeout

LINK = re.compile(
    r"""<a[^>]*href="\{\{\s*url_for\(\s*['"]([a-z_0-9]+)['"]""")


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    _db.init_db()


def _client(app, role):
    org = _db.create_organization(f"Org {secrets.token_hex(3)}")
    uid = _db.create_user(
        f"u-{secrets.token_hex(4)}@example.test",
        password_hash=_auth.hash_password("a perfectly good passphrase"))
    _db.add_org_member(org, uid, role)
    pid = _db.upsert_project(name=f"P {secrets.token_hex(3)}", org_id=org)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess[_perm.SESSION_USER_KEY] = uid
        sess[_perm.SESSION_ORG_KEY] = org
        sess["project_id"] = pid
        _timeout.stamp(sess)
    return c


def _page(app, role):
    response = _client(app, role).get("/estimation")
    assert response.status_code == 200
    return response.get_data(as_text=True)


class TestTheLink:

    def test_a_member_is_not_offered_it(self, app):
        assert "/estimation/diag" not in _page(app, "user")

    def test_an_admin_still_is(self, app):
        assert "/estimation/diag" in _page(app, "admin")

    def test_the_diagnostics_strip_survives_for_a_member(self, app):
        """The badges are the useful half and stay. A fix that removed the
        whole strip would satisfy the first test and take away the reason
        a member stops retrying."""
        body = _page(app, "user")
        assert "poppler:" in body

    def test_the_route_is_still_the_boundary(self, app):
        assert _client(app, "user").get(
            "/estimation/diag").status_code == 403
        assert _client(app, "admin").get(
            "/estimation/diag").status_code == 200


class TestTheFlashThatSaidTheSameThing:

    def test_the_advice_is_admin_only(self):
        """Read from the source rather than provoked: the sentence lives
        in a top-level ``except`` around the whole estimation run, and
        forcing one would be testing the exception handler, not this.

        Matched on the whole conditional with whitespace collapsed, so it
        survives reformatting but not somebody unhooking the condition —
        a window-of-N-characters version of this passed while the check
        sat on the line *after* the string it guards."""
        source = " ".join(
            pathlib.Path("routes/estimation.py").read_text(
                encoding="utf-8").split())
        assert "open /estimation/diag for diagnostics" in source, (
            "the advice went away entirely — an admin should still get it")
        assert ('open /estimation/diag for diagnostics" '
                'if _perm_mod.is_admin() else ""' in source), (
            "the diagnostics advice is not behind an is_admin() check")

    def test_the_rest_of_the_message_is_unconditional(self):
        source = pathlib.Path("routes/estimation.py").read_text(
            encoding="utf-8")
        assert "Try a different source (Text / Mockups / URL)" in source


class TestNoOtherTemplateLinksAnAdminOnlyRoute:
    """The enumeration, kept. Its ``<form>`` half lives in
    ``tests/test_settings_storage_admin_only.py``; this is the ``<a>``
    half, and the two found one defect each."""

    @staticmethod
    def _ungated():
        admin_only = {endpoint for endpoint, role in _policy.POLICY.items()
                      if role == "admin"}
        found = []
        for path in sorted(pathlib.Path("templates").rglob("*.html")):
            body = re.sub(
                r"\{#.*?#\}", "",
                path.read_text(encoding="utf-8", errors="replace"),
                flags=re.S)
            for match in LINK.finditer(body):
                if match.group(1) not in admin_only:
                    continue
                if not _guarded_before(body[:match.start()]):
                    found.append(f"{path.as_posix()} → {match.group(1)}")
        return found

    def test_none(self):
        ungated = self._ungated()
        assert not ungated, (
            "these links point at an endpoint route_policy gates at "
            "'admin', with no enclosing `{% if is_admin %}` — a member who "
            "clicks one gets 403:\n  " + "\n  ".join(ungated))

    def test_the_scan_still_matches_links(self):
        """Without this, a reformatted ``<a`` or a renamed attribute makes
        the gate above pass by finding nothing at all."""
        total = 0
        for path in pathlib.Path("templates").rglob("*.html"):
            body = path.read_text(encoding="utf-8", errors="replace")
            total += len(LINK.findall(body))
        assert total > 20, f"the link scan only matched {total} anchors"


def _guarded_before(prefix: str) -> bool:
    """True when an ``{% if is_admin %}`` is still open at this point.

    Depth-counted, so a *closed* admin block earlier in the file does not
    vouch for a link below it. Same rule as the form scan in
    ``tests/test_settings_storage_admin_only.py``.
    """
    depth = 0
    guard_depth = None
    for token, expression in re.findall(
            r"\{%-?\s*(if|elif|endif)\s*([^%]*)%\}", prefix):
        if token == "if":
            depth += 1
            if "is_admin" in expression:
                guard_depth = depth
        elif token == "elif":
            if "is_admin" in expression:
                guard_depth = depth
            elif guard_depth == depth:
                guard_depth = None
        else:
            if guard_depth == depth:
                guard_depth = None
            depth -= 1
    return guard_depth is not None
