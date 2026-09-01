"""A member was shown a secret-key field the server would 403.

Found by enumerating rather than by looking: every ``<form action="{{
url_for('…') }}">`` in ``templates/`` whose endpoint is "admin" in
``engine.route_policy.POLICY``, checked for an enclosing ``{% if is_admin
%}``. Ten such forms; eight guarded; the two exceptions were both in
``org_settings.html``, both in the same section.

Measured with ``STORAGE_BACKEND_CONFIGURABLE=1``, as a ``user``-role member:

    GET  /org/settings          200, "your own storage" form rendered,
                                including <input type="password"
                                name="secret_key">
    POST /org/settings/storage  403, no flash

Typing a secret access key into a form that was never going to accept it is
the worst version of offering an action the server will refuse — the
operator has handled a credential for nothing, and the answer is an error
page rather than a sentence.

The reason is already written on this page, one card above, over the
team's own API key:

    Admins only: this is a credential management surface, not information
    a member needs.

Storage is the same kind of surface — endpoint, bucket, access key,
secret — and was the one section that did not say so. Five others on this
page are gated.

Latent today: both Render services set ``STORAGE_BACKEND_CONFIGURABLE=0``,
so the section renders for nobody, and the page correctly says the feature
is not available on this instance. That flag is a rollout gate, though, and
this is what was waiting behind it.

A member still sees the Storage card and its capacity readouts. Only the
configuration goes, which is what "members can read this page; only admins
can change it" already promised in the Guide.
"""
from __future__ import annotations

import re
import secrets

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import permissions as _perm
from engine import route_policy as _policy
from engine import session_timeout as _timeout

TEMPLATE_DIR = "templates"
FORM_ACTION = re.compile(
    r"""<form[^>]*action="\{\{\s*url_for\(\s*['"]([a-z_0-9]+)['"]""")


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    # The whole section is behind this. With it off — production today —
    # there is nothing to hide and nothing to test.
    monkeypatch.setenv("STORAGE_BACKEND_CONFIGURABLE", "1")
    _db.init_db()


def _client(app, role):
    org = _db.create_organization(f"Org {secrets.token_hex(3)}")
    uid = _db.create_user(
        f"u-{secrets.token_hex(4)}@example.test",
        password_hash=_auth.hash_password("a perfectly good passphrase"))
    _db.add_org_member(org, uid, role)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess[_perm.SESSION_USER_KEY] = uid
        sess[_perm.SESSION_ORG_KEY] = org
        _timeout.stamp(sess)
    return c


def _page(app, role):
    response = _client(app, role).get("/org/settings")
    assert response.status_code == 200, response.status_code
    return response.get_data(as_text=True)


class TestAMemberIsNotShownTheCredentials:

    def test_no_secret_key_field(self, app):
        assert 'name="secret_key"' not in _page(app, "user")

    def test_no_access_key_field(self, app):
        assert 'name="access_key"' not in _page(app, "user")

    def test_no_form_posts_to_the_storage_endpoints(self, app):
        body = _page(app, "user")
        assert "/org/settings/storage" not in body

    def test_the_rest_of_the_page_is_still_there(self, app):
        """A member can still read this page — that is what the Guide
        promises. A fix that hid the whole card, or the whole page, would
        satisfy the three tests above."""
        body = _page(app, "user")
        assert "settings-meter" in body, "the capacity readouts went too"
        assert len(body) > 8000, len(body)


class TestAnAdminStillConfiguresIt:

    def test_the_form_is_there(self, app):
        body = _page(app, "admin")
        assert 'name="secret_key"' in body
        assert "/org/settings/storage" in body

    def test_the_post_is_accepted(self, app):
        """Not a 403. The validation flash is the route's own business —
        what matters here is that the admin reaches it."""
        response = _client(app, "admin").post(
            "/org/settings/storage",
            data={"bucket": "b", "url": "https://example.invalid",
                  "access_key": "k", "secret_key": "s"})
        assert response.status_code != 403, "the admin lost the form too"


class TestTheServerIsStillTheBoundary:

    def test_a_member_posting_it_is_refused(self, app):
        response = _client(app, "user").post(
            "/org/settings/storage",
            data={"bucket": "b", "url": "https://example.invalid",
                  "access_key": "k", "secret_key": "s"})
        assert response.status_code == 403

    def test_clearing_it_is_refused_too(self, app):
        response = _client(app, "user").post("/org/settings/storage/clear")
        assert response.status_code == 403


class TestNoOtherTemplateOffersAnAdminOnlyForm:
    """The enumeration this file came from, kept as the gate. A list of
    today's two offenders would say nothing about the next one."""

    @staticmethod
    def _ungated():
        import pathlib

        admin_only = {endpoint for endpoint, role in _policy.POLICY.items()
                      if role == "admin"}
        found = []
        for path in sorted(pathlib.Path(TEMPLATE_DIR).rglob("*.html")):
            text = path.read_text(encoding="utf-8", errors="replace")
            # Jinja comments discuss endpoints; they do not render forms.
            body = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
            for match in FORM_ACTION.finditer(body):
                endpoint = match.group(1)
                if endpoint not in admin_only:
                    continue
                if not _guarded_before(body[:match.start()]):
                    found.append(f"{path.as_posix()} → {endpoint}")
        return found

    def test_none(self):
        ungated = self._ungated()
        assert not ungated, (
            "these forms post to an endpoint route_policy gates at "
            "'admin', with no enclosing `{% if is_admin %}` — a member is "
            "offered a control whose only possible answer is 403:\n  "
            + "\n  ".join(ungated))

    def test_the_scan_finds_the_guarded_ones(self):
        """Without this the test above passes on a scan that matches
        nothing — a renamed attribute, a reformatted `<form`, an empty
        POLICY."""
        import pathlib

        admin_only = {e for e, r in _policy.POLICY.items() if r == "admin"}
        assert admin_only, "no admin-only endpoints in POLICY"
        total = 0
        for path in pathlib.Path(TEMPLATE_DIR).rglob("*.html"):
            body = re.sub(r"\{#.*?#\}", "",
                          path.read_text(encoding="utf-8", errors="replace"),
                          flags=re.S)
            total += sum(1 for m in FORM_ACTION.finditer(body)
                         if m.group(1) in admin_only)
        assert total >= 8, f"the scan only found {total} admin-only forms"


def _guarded_before(prefix: str) -> bool:
    """True when an ``{% if is_admin %}`` is still open at this point.

    Counts depth rather than searching for the string, so a *closed*
    admin block earlier in the file does not vouch for a form below it.
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
            # An ``{% elif is_admin %}`` guards its own branch, and an
            # ``{% elif %}`` on a guarded ``if`` ends that guard.
            if "is_admin" in expression:
                guard_depth = depth
            elif guard_depth == depth:
                guard_depth = None
        else:
            if guard_depth == depth:
                guard_depth = None
            depth -= 1
    return guard_depth is not None
