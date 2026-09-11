"""Team and Settings are admin modules — the sidebar and the URLs agree.

The owner's requirement: *a user with the ``user`` role must not be shown
the Teams and Settings modules.* That reverses §5.1 #4, under which both
pages were readable by every member and writable only by admins, and it
has two halves that fail independently:

* **The link is gone.** ``templates/base.html`` renders the two ``<li>``
  entries only for an admin.
* **The URL refuses.** Hiding a link is not removing a module. A page
  that still serves its content to anyone who types ``/org/members`` has
  been hidden from the polite and left open to everyone else.

Tested at four levels, because each one can be green while the next is
broken:

``TestTheRoleRule``      unit — ``is_admin`` is what the template asks.
``TestTheRoutesRefuse``  integration — the two endpoints, by role.
``TestTheSidebar``       functional — the rendered navigation.
``TestTheWholeWalk``     end to end — sign in with a password, walk the
                         product as each role, and check what is on
                         screen and what answers.

One thing deliberately *not* closed: the "you are not on a team yet"
card. Somebody with no organisation has no role either, so gating that
would answer "this needs the admin role" to a person whose real problem
is that nobody has invited them. ``TestTheEmptyStateSurvives`` pins it,
because it is exactly the kind of exception a later tidy-up removes.
"""

import re
import secrets

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import permissions as _perm

PASSWORD = "a long enough passphrase"

#: Both modules, and the sidebar label each is known by.
MODULES = (("/org/members", "Team"), ("/org/settings", "Settings"))


@pytest.fixture(autouse=True)
def _db_ready():
    _db.init_db()


@pytest.fixture(autouse=True)
def _full_auth(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")


def _email() -> str:
    return f"aom-{secrets.token_hex(6)}@example.com"


@pytest.fixture
def team():
    """One organisation, one admin, one plain user."""
    org = _db.create_organization(f"Team {secrets.token_hex(4)}")
    out = {"org": org}
    for role in ("admin", "user"):
        email = _email()
        uid = _db.create_user(email,
                              password_hash=_auth.hash_password(PASSWORD),
                              email_verified=True)
        _db.add_org_member(org, uid, role)
        out[role] = uid
        out[role + "_email"] = email
    return out


def _as(client, team, role):
    with client.session_transaction() as sess:
        sess.clear()
        sess[_perm.SESSION_USER_KEY] = team[role]
        sess[_perm.SESSION_ORG_KEY] = team["org"]


def _sidebar(html: str) -> str:
    """Just the navigation list, so a link elsewhere cannot stand in.

    The guide names both modules in its prose and its cards; searching the
    whole document for "Team" would find those and report a sidebar entry
    that is not there.
    """
    start = html.index('<ul class="nav-steps">')
    return html[start:html.index("</ul>", start)]


# ── Unit: the value the template asks for ─────────────────────────

class TestTheRoleRule:
    """``is_admin()`` decides, so it is worth asserting on its own.

    The template calls it through the context processor. If it answered
    True for a plain user every other test here would still describe the
    product correctly and the sidebar would be wrong for one role only —
    the role nobody tests by hand.
    """

    def test_an_admin_is_an_admin(self, app, team):
        with app.test_request_context():
            from flask import session
            session[_perm.SESSION_USER_KEY] = team["admin"]
            session[_perm.SESSION_ORG_KEY] = team["org"]
            assert _perm.is_admin() is True
            assert _perm.current_role() == "admin"

    def test_a_plain_user_is_not(self, app, team):
        with app.test_request_context():
            from flask import session
            session[_perm.SESSION_USER_KEY] = team["user"]
            session[_perm.SESSION_ORG_KEY] = team["org"]
            assert _perm.is_admin() is False
            assert _perm.has_role("admin") is False

    def test_the_template_context_carries_it(self, app, team):
        # The sidebar condition is ``org_active and is_admin``; both keys
        # have to arrive, under those names.
        with app.test_request_context():
            from flask import session
            session[_perm.SESSION_USER_KEY] = team["user"]
            session[_perm.SESSION_ORG_KEY] = team["org"]
            ctx = _perm.template_context()
            assert ctx["org_active"] is True
            assert ctx["is_admin"] is False

    def test_a_demoted_admin_stops_being_one(self, app, team):
        """Role is read per request, not cached at sign-in.

        An admin demoted mid-session keeping the modules until they signed
        out would be the same defect wearing a clock.
        """
        with app.test_request_context():
            from flask import session
            session[_perm.SESSION_USER_KEY] = team["admin"]
            session[_perm.SESSION_ORG_KEY] = team["org"]
            assert _perm.is_admin() is True
            # Promote the other member first: the last-admin guard refuses
            # a demotion that would leave the organisation with none.
            _db.change_org_role(team["org"], team["user"], "admin")
            _db.change_org_role(team["org"], team["admin"], "user")
            assert _perm.is_admin() is False


# ── Integration: the endpoints ────────────────────────────────────

class TestTheRoutesRefuse:
    @pytest.mark.parametrize("path,_label", MODULES)
    def test_a_plain_user_gets_403(self, client, team, path, _label):
        _as(client, team, "user")
        assert client.get(path).status_code == 403

    @pytest.mark.parametrize("path,_label", MODULES)
    def test_an_admin_gets_the_page(self, client, team, path, _label):
        _as(client, team, "admin")
        assert client.get(path).status_code == 200

    @pytest.mark.parametrize("path,_label", MODULES)
    def test_an_anonymous_caller_is_sent_to_sign_in(self, anon_client, path,
                                                    _label):
        """Still 302, not 403. The refusal must not start leaking which
        URLs exist to people who are not signed in at all."""
        resp = anon_client.get(path)
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["Location"]

    @pytest.mark.parametrize("path,_label", MODULES)
    def test_a_fetch_gets_json_not_a_page(self, client, team, path, _label):
        # The refusal is the shared one, so an in-page fetch keeps getting
        # a machine-readable answer rather than an HTML document.
        _as(client, team, "user")
        resp = client.get(path, headers={"Accept": "application/json"})
        assert resp.status_code == 403
        assert resp.get_json()["required_role"] == "admin"

    def test_the_refusal_names_admin_not_user(self, client, team):
        """The 403 page says which role is needed. Saying "user" — the
        role they already hold — reads as a bug in the product."""
        _as(client, team, "user")
        body = client.get("/org/members").get_data(as_text=True)
        assert "admin" in body

    @pytest.mark.parametrize("path,_label", MODULES)
    def test_the_page_body_is_not_served_alongside_the_403(
            self, client, team, path, _label):
        """A refusal that still renders the page is a status code, not a
        gate. Checked on content the module owns rather than on a word
        that appears in the shell around it."""
        _as(client, team, "user")
        body = client.get(path).get_data(as_text=True)
        assert team["admin_email"] not in body
        assert "/org/settings/general" not in body
        assert "/org/members/invite" not in body


class TestTheEmptyStateSurvives:
    """No organisation means no role, and this card is the only page that
    explains that state. It is checked before the admin gate on purpose."""

    @pytest.fixture
    def orphan(self, client):
        uid = _db.create_user(_email(), email_verified=True)
        with client.session_transaction() as sess:
            sess.clear()
            sess[_perm.SESSION_USER_KEY] = uid
            sess.pop(_perm.SESSION_ORG_KEY, None)
        return client

    def test_the_team_page_still_explains_itself(self, orphan):
        resp = orphan.get("/org/members")
        assert resp.status_code == 200
        assert b"not on a team yet" in resp.data

    def test_the_settings_page_still_explains_itself(self, orphan):
        resp = orphan.get("/org/settings")
        assert resp.status_code == 200
        assert b"No team selected" in resp.data


# ── Functional: what the navigation shows ─────────────────────────

class TestTheSidebar:
    """Rendered from a page every role can reach, so the sidebar is the
    only thing that differs between the two runs."""

    def test_a_plain_user_sees_neither_module(self, client, team):
        _as(client, team, "user")
        nav = _sidebar(client.get("/guide").get_data(as_text=True))
        assert "/org/members" not in nav
        assert "/org/settings" not in nav

    def test_an_admin_sees_both(self, client, team):
        _as(client, team, "admin")
        nav = _sidebar(client.get("/guide").get_data(as_text=True))
        assert "/org/members" in nav
        assert "/org/settings" in nav

    def test_the_labels_go_with_the_links(self, client, team):
        # Hiding the anchor and leaving the word is a list item that looks
        # like a broken link.
        _as(client, team, "user")
        nav = _sidebar(client.get("/guide").get_data(as_text=True))
        assert "Team" not in nav
        assert "Settings" not in nav

    def test_the_rest_of_the_navigation_is_untouched(self, client, team):
        """A rule that hid the whole sidebar from a plain user would pass
        both tests above while deleting the product."""
        _as(client, team, "user")
        nav = _sidebar(client.get("/guide").get_data(as_text=True))
        for path in ("/test-cases", "/checklist", "/test-execution",
                     "/bug-reports", "/guide"):
            assert path in nav, f"{path} disappeared from the sidebar"

    def test_it_is_hidden_on_every_page_not_just_one(self, client, team):
        """The sidebar comes from ``base.html``, so one page would in
        principle be enough — unless a page overrides the block, which is
        the case this cannot see without looking."""
        _as(client, team, "user")
        for path in ("/", "/test-cases", "/checklist", "/bug-reports",
                     "/guide"):
            resp = client.get(path)
            if resp.status_code != 200:
                continue
            nav = _sidebar(resp.get_data(as_text=True))
            assert "/org/members" not in nav, f"{path} still offers Team"
            assert "/org/settings" not in nav, f"{path} still offers Settings"


# ── End to end: sign in and walk ──────────────────────────────────

class TestTheWholeWalk:
    """Through the real sign-in form, as the person in the screenshot.

    Everything above installs a session by hand. This one starts where a
    user starts — an email address and a password — so the role the
    product resolves after a genuine login is the role being tested.
    """

    @staticmethod
    def _sign_in(client, email):
        """Sign in, and prove it took.

        Asserted rather than assumed because the failure mode is silent
        and inverts the test: a POST that does not authenticate leaves
        whatever session the client already had. The suite's ``client``
        fixture arrives signed in as an *admin*, so a walk that quietly
        failed to sign in as a plain user would have walked as an admin
        and reported the modules present — a red test for the wrong
        reason on a good day, and a green one on a bad one.
        """
        resp = client.post("/auth/login",
                           data={"email": email, "password": PASSWORD},
                           follow_redirects=True)
        assert resp.status_code == 200
        with client.session_transaction() as sess:
            assert sess.get(_perm.SESSION_USER_KEY), (
                "the sign-in POST did not authenticate anybody")
        return resp

    def test_a_user_signs_in_and_the_modules_are_not_there(self, anon_client,
                                                           team):
        client = anon_client
        resp = self._sign_in(client, team["user_email"])
        assert resp.status_code == 200

        nav = _sidebar(client.get("/guide").get_data(as_text=True))
        assert "/org/members" not in nav
        assert "/org/settings" not in nav

        # …and the URLs they are not being offered do not work either.
        assert client.get("/org/members").status_code == 403
        assert client.get("/org/settings").status_code == 403

        # …while the work they signed in to do is untouched.
        assert client.get("/test-cases").status_code == 200
        assert client.get("/bug-reports").status_code == 200

    def test_an_admin_signs_in_and_has_both(self, anon_client, team):
        client = anon_client
        self._sign_in(client, team["admin_email"])

        nav = _sidebar(client.get("/guide").get_data(as_text=True))
        assert "/org/members" in nav and "/org/settings" in nav
        assert client.get("/org/members").status_code == 200
        assert client.get("/org/settings").status_code == 200

    def test_a_promoted_user_gets_the_modules_without_signing_in_again(
            self, anon_client, team):
        """The mirror of the demotion unit test, end to end: the gate is
        the current role, not what it was at sign-in."""
        client = anon_client
        self._sign_in(client, team["user_email"])
        assert client.get("/org/members").status_code == 403

        _db.change_org_role(team["org"], team["user"], "admin")

        assert client.get("/org/members").status_code == 200
        nav = _sidebar(client.get("/guide").get_data(as_text=True))
        assert "/org/members" in nav

    def test_the_page_a_user_lands_on_offers_them_no_way_in(self, anon_client,
                                                            team):
        """Not only the sidebar. Any anchor to either module anywhere in
        the document is a door this requirement says should not be there —
        the guide's cards are modals and carry no href, so a hit here is a
        real link."""
        client = anon_client
        self._sign_in(client, team["user_email"])
        for path in ("/", "/guide", "/test-cases"):
            resp = client.get(path)
            if resp.status_code != 200:
                continue
            html = resp.get_data(as_text=True)
            hrefs = re.findall(r'href="([^"]*)"', html)
            offered = [h for h in hrefs
                       if h.startswith(("/org/members", "/org/settings"))]
            assert not offered, f"{path} links to {offered}"
