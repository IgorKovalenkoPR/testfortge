"""Organisation members and invitations — routes/members.py (E2.4).

With registration invite-only, this page is the only door into the
platform. Two properties carry the most weight: an admin can **always**
get a usable invite link, and neither door to "this organisation has no
admin" is open — removing the last admin and demoting the last admin
reach the same unrecoverable state.

"Always" is the word that grew a test class. E0.4 built email, but it
only runs where the server has a provider configured, so on every other
instance the link exists in exactly one place: a flash message, which
survives one page load. An admin who clicked away had no route back to
it — the token is deliberately absent from the pending list — and the
only workaround was to know that re-inviting the same address mints a
new one. ``TestReissuing`` covers the button that makes that a button.
"""

import secrets

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import permissions as _perm
from routes.auth import new_invite_token


@pytest.fixture(autouse=True)
def _db_ready():
    _db.init_db()


@pytest.fixture(autouse=True)
def _full_auth(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")


def _email() -> str:
    return f"m-{secrets.token_hex(6)}@example.com"


def _team(*roles: str) -> tuple[str, list[str]]:
    """An org plus one user per role given."""
    org = _db.create_organization(f"Team {secrets.token_hex(4)}")
    ids = []
    for role in roles:
        uid = _db.create_user(_email(),
                              password_hash=_auth.hash_password("a passphrase here"))
        _db.add_org_member(org, uid, role)
        ids.append(uid)
    return org, ids


def _signed_in_as(client, org: str, uid: str):
    with client.session_transaction() as sess:
        sess[_perm.SESSION_USER_KEY] = uid
        sess[_perm.SESSION_ORG_KEY] = org


# ── Visibility ────────────────────────────────────────────────────

class TestPageAccess:
    def test_anonymous_callers_are_sent_to_sign_in(self, anon_client):
        resp = anon_client.get("/org/members")
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["Location"]

    def test_a_plain_user_sees_the_team_read_only(self, client):
        # The owner's decision (§5.1 #4): read-only produces fewer "where
        # do I look" messages than a 403 does.
        org, (admin, user) = _team("admin", "user")
        _signed_in_as(client, org, user)
        resp = client.get("/org/members")
        assert resp.status_code == 200
        assert b"Invite someone" not in resp.data

    def test_an_admin_sees_the_invite_form(self, client):
        org, (admin,) = _team("admin")
        _signed_in_as(client, org, admin)
        resp = client.get("/org/members")
        assert resp.status_code == 200
        assert b"Invite someone" in resp.data

    def test_a_user_with_no_org_gets_a_page_not_an_error(self, client):
        # A real state: an admin who deleted their last organisation.
        uid = _db.create_user(_email())
        with client.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = uid
            # …and no organisation: that is the state being described.
            sess.pop(_perm.SESSION_ORG_KEY, None)
        resp = client.get("/org/members")
        assert resp.status_code == 200
        assert b"not on a team yet" in resp.data

    def test_pending_invitations_are_admin_only(self, client):
        # The list of addresses somebody tried to recruit is exactly the
        # list worth phishing.
        org, (admin, user) = _team("admin", "user")
        invited = _email()
        _db.create_invite(org, invited, "user", new_invite_token())
        _signed_in_as(client, org, user)
        assert invited.encode() not in client.get("/org/members").data
        _signed_in_as(client, org, admin)
        assert invited.encode() in client.get("/org/members").data


# ── Inviting ──────────────────────────────────────────────────────

class TestInviting:
    def test_an_admin_creates_an_invitation_and_gets_a_link(self, client):
        # Email delivery is E0.4 and unbuilt, so a copy-and-paste link is
        # the mechanism, not a fallback.
        org, (admin,) = _team("admin")
        _signed_in_as(client, org, admin)
        invited = _email()
        resp = client.post("/org/members/invite",
                           data={"email": invited, "role": "user"},
                           follow_redirects=True)
        assert resp.status_code == 200
        assert b"/auth/accept/" in resp.data
        assert len(_db.list_pending_invites(org)) == 1

    def test_a_plain_user_cannot_invite(self, client):
        org, (admin, user) = _team("admin", "user")
        _signed_in_as(client, org, user)
        resp = client.post("/org/members/invite",
                           data={"email": _email(), "role": "admin"},
                           headers={"Accept": "application/json"})
        assert resp.status_code == 403
        assert _db.list_pending_invites(org) == []

    @pytest.mark.parametrize("bad", ["", "not-an-email", "@example.com",
                                     "user@", "two words@example.com"])
    def test_a_malformed_address_is_refused(self, client, bad):
        org, (admin,) = _team("admin")
        _signed_in_as(client, org, admin)
        client.post("/org/members/invite", data={"email": bad, "role": "user"})
        assert _db.list_pending_invites(org) == []

    def test_an_unknown_role_is_refused(self, client):
        org, (admin,) = _team("admin")
        _signed_in_as(client, org, admin)
        client.post("/org/members/invite",
                    data={"email": _email(), "role": "superuser"})
        assert _db.list_pending_invites(org) == []

    def test_inviting_an_existing_member_says_so_instead(self, client):
        org, (admin, user) = _team("admin", "user")
        member_email = _db.get_user(user)["email"]
        _signed_in_as(client, org, admin)
        resp = client.post("/org/members/invite",
                           data={"email": member_email, "role": "admin"},
                           follow_redirects=True)
        assert b"already on this team" in resp.data
        assert _db.list_pending_invites(org) == []

    def test_re_inviting_leaves_exactly_one_live_invitation(self, client):
        org, (admin,) = _team("admin")
        _signed_in_as(client, org, admin)
        invited = _email()
        client.post("/org/members/invite",
                    data={"email": invited, "role": "admin"})
        client.post("/org/members/invite",
                    data={"email": invited, "role": "user"})
        pending = _db.list_pending_invites(org)
        assert len(pending) == 1
        # The newer role wins, so a forwarded stale link cannot be used to
        # join at a role that was since downgraded.
        assert pending[0]["role"] == "user"

    def test_the_invite_is_audited(self, client):
        org, (admin,) = _team("admin")
        _signed_in_as(client, org, admin)
        client.post("/org/members/invite",
                    data={"email": _email(), "role": "user"})
        rows = _db.list_audit(org_id=org, entity="invite")
        assert rows and rows[0]["action"] == "create"


# ── Roles ─────────────────────────────────────────────────────────

class TestRoleChanges:
    def test_an_admin_promotes_a_user(self, client):
        org, (admin, user) = _team("admin", "user")
        _signed_in_as(client, org, admin)
        client.post(f"/org/members/{user}/role", data={"role": "admin"})
        assert _db.get_org_role(org, user) == "admin"

    def test_a_plain_user_cannot_promote_themselves(self, client):
        org, (admin, user) = _team("admin", "user")
        _signed_in_as(client, org, user)
        resp = client.post(f"/org/members/{user}/role",
                           data={"role": "admin"},
                           headers={"Accept": "application/json"})
        assert resp.status_code == 403
        assert _db.get_org_role(org, user) == "user"

    def test_the_change_is_audited_with_both_values(self, client):
        org, (admin, user) = _team("admin", "user")
        _signed_in_as(client, org, admin)
        client.post(f"/org/members/{user}/role", data={"role": "admin"})
        rows = _db.list_audit(org_id=org, entity="org_member")
        assert rows[0]["diff"]["role"] == ["user", "admin"]

    def test_someone_outside_the_team_cannot_be_given_a_role(self, client):
        org, (admin,) = _team("admin")
        outsider = _db.create_user(_email())
        _signed_in_as(client, org, admin)
        client.post(f"/org/members/{outsider}/role", data={"role": "admin"})
        assert _db.get_org_role(org, outsider) is None


class TestLastAdminGuard:
    def test_the_only_admin_cannot_demote_themselves(self, client):
        # Same unrecoverable state as removing them: nobody left who can
        # create a project, change settings, or promote anyone back.
        org, (admin,) = _team("admin")
        _signed_in_as(client, org, admin)
        resp = client.post(f"/org/members/{admin}/role",
                           data={"role": "user"}, follow_redirects=True)
        assert b"only admin" in resp.data
        assert _db.get_org_role(org, admin) == "admin"

    def test_the_only_admin_cannot_remove_themselves(self, client):
        org, (admin,) = _team("admin")
        _signed_in_as(client, org, admin)
        resp = client.post(f"/org/members/{admin}/remove",
                           follow_redirects=True)
        assert b"only admin" in resp.data
        assert _db.get_org_role(org, admin) == "admin"

    def test_demotion_works_once_a_second_admin_exists(self, client):
        org, (first, second) = _team("admin", "admin")
        _signed_in_as(client, org, first)
        client.post(f"/org/members/{first}/role", data={"role": "user"})
        assert _db.get_org_role(org, first) == "user"
        assert _db.get_org_role(org, second) == "admin"

    def test_the_page_warns_before_they_try(self, client):
        org, (admin,) = _team("admin")
        _signed_in_as(client, org, admin)
        assert b"only admin" in client.get("/org/members").data


# ── Removal ───────────────────────────────────────────────────────

class TestRemoval:
    def test_an_admin_removes_a_user(self, client):
        org, (admin, user) = _team("admin", "user")
        _signed_in_as(client, org, admin)
        client.post(f"/org/members/{user}/remove")
        assert _db.get_org_role(org, user) is None

    def test_removal_ends_their_sessions(self, client, monkeypatch):
        # Otherwise a removed colleague keeps working until their cookie
        # happens to expire, which is not what "removed" means.
        monkeypatch.setenv("SESSION_BACKEND", "db")
        org, (admin, user) = _team("admin", "user")
        sid = secrets.token_urlsafe(32)
        from datetime import datetime, timedelta, timezone
        _db.session_save(sid, "{}",
                         datetime.now(timezone.utc) + timedelta(days=1),
                         user_id=user)
        _signed_in_as(client, org, admin)
        client.post(f"/org/members/{user}/remove")
        assert _db.session_load(sid) is None

    def test_the_account_and_their_work_survive(self, client):
        # Their name still has to resolve on every test case they wrote;
        # a deleted row turns an audit trail into orphaned ids.
        org, (admin, user) = _team("admin", "user")
        _signed_in_as(client, org, admin)
        client.post(f"/org/members/{user}/remove")
        assert _db.get_user(user) is not None

    def test_removing_a_non_member_is_a_noop_not_an_error(self, client):
        # A double-submitted form should not scold.
        org, (admin,) = _team("admin")
        outsider = _db.create_user(_email())
        _signed_in_as(client, org, admin)
        resp = client.post(f"/org/members/{outsider}/remove",
                           follow_redirects=True)
        assert resp.status_code == 200

    def test_a_plain_user_cannot_remove_anyone(self, client):
        org, (admin, user) = _team("admin", "user")
        _signed_in_as(client, org, user)
        resp = client.post(f"/org/members/{admin}/remove",
                           headers={"Accept": "application/json"})
        assert resp.status_code == 403
        assert _db.get_org_role(org, admin) == "admin"


# ── Revoking an invitation ────────────────────────────────────────

class TestRevoking:
    def test_an_admin_cancels_an_invitation_by_address(self, client):
        # Keyed on email, not token: the token is a bearer credential for
        # that seat and never reaches the page.
        org, (admin,) = _team("admin")
        invited, token = _email(), new_invite_token()
        _db.create_invite(org, invited, "user", token)
        _signed_in_as(client, org, admin)
        client.post("/org/invites/revoke", data={"email": invited})
        assert _db.get_invite(token) is None
        assert _db.list_pending_invites(org) == []

    def test_another_teams_invitation_cannot_be_cancelled(self, client):
        theirs, _ = _team("user")
        mine, (admin,) = _team("admin")
        invited, token = _email(), new_invite_token()
        _db.create_invite(theirs, invited, "user", token)
        _signed_in_as(client, mine, admin)
        client.post("/org/invites/revoke", data={"email": invited})
        assert _db.get_invite(token) is not None

    def test_a_plain_user_cannot_revoke(self, client):
        org, (admin, user) = _team("admin", "user")
        invited, token = _email(), new_invite_token()
        _db.create_invite(org, invited, "user", token)
        _signed_in_as(client, org, user)
        resp = client.post("/org/invites/revoke", data={"email": invited},
                           headers={"Accept": "application/json"})
        assert resp.status_code == 403
        assert _db.get_invite(token) is not None

    def test_revoking_twice_is_harmless(self, client):
        org, (admin,) = _team("admin")
        invited = _email()
        _db.create_invite(org, invited, "user", new_invite_token())
        _signed_in_as(client, org, admin)
        client.post("/org/invites/revoke", data={"email": invited})
        resp = client.post("/org/invites/revoke", data={"email": invited},
                           follow_redirects=True)
        assert resp.status_code == 200


# ── What the page promises about email ────────────────────────────

class TestTheInviteNote:
    """The note has to describe *this* instance, not a past release.

    It read "TestForTge does not send email on this plan" from before
    E0.4 until now — which blamed the hosting plan for what is a server
    setting, and was simply false anywhere the setting is filled in. An
    admin who believes it will not go looking for the message, and an
    admin who is told mail works when it does not will wait for one that
    never comes. Both directions are asserted.
    """

    def _note(self, client, org, admin) -> str:
        _signed_in_as(client, org, admin)
        return client.get("/org/members").get_data(as_text=True)

    def test_without_a_provider_it_says_the_instance_cannot_send(
            self, client, monkeypatch):
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        monkeypatch.delenv("MAIL_FROM", raising=False)
        org, (admin,) = _team("admin")

        body = self._note(client, org, admin)

        assert "This instance cannot send email" in body
        assert "will be emailed the link" not in body

    def test_with_a_provider_it_does_not_deny_sending(
            self, client, monkeypatch):
        # Both halves set: a key with no verified sender is a 403 on every
        # send, so mailer.configured() requires the pair — and so does the
        # sentence that depends on it.
        monkeypatch.setenv("RESEND_API_KEY", "re_a_key_for_this_test")
        monkeypatch.setenv("MAIL_FROM", "qa@example.com")
        org, (admin,) = _team("admin")

        body = self._note(client, org, admin)

        assert "will be emailed the link" in body
        assert "cannot send email" not in body

    def test_a_key_without_a_sender_still_counts_as_cannot_send(
            self, client, monkeypatch):
        # The half that gets forgotten. Claiming delivery here would be
        # the same lie in a new place.
        monkeypatch.setenv("RESEND_API_KEY", "re_a_key_for_this_test")
        monkeypatch.delenv("MAIL_FROM", raising=False)
        org, (admin,) = _team("admin")

        body = self._note(client, org, admin)

        assert "This instance cannot send email" in body


# ── Getting the link back ─────────────────────────────────────────

class TestReissuing:
    """The link is shown once. This is the way back to one."""

    def _flash(self, client) -> str:
        """The flash text, read from the session rather than the page.

        The redirect target renders fine either way, so asserting on the
        landing page would pass whether or not anything was flashed —
        which is the failure this whole class exists to prevent.
        """
        with client.session_transaction() as sess:
            return " ".join(m for _, m in sess.get("_flashes", []))

    def test_it_hands_the_admin_a_working_link(self, client):
        org, (admin,) = _team("admin")
        invited = _email()
        _db.create_invite(org, invited, "user", new_invite_token())
        _signed_in_as(client, org, admin)

        client.post("/org/invites/reissue", data={"email": invited})

        # Not "a link was flashed" — the link that was flashed has to be
        # the one that actually opens the seat.
        flashed = self._flash(client)
        assert "/auth/accept/" in flashed
        token = flashed.split("/auth/accept/")[1].split()[0].rstrip(".,")
        invite = _db.get_invite(token)
        assert invite is not None
        assert invite["email"] == invited

    def test_the_previous_link_stops_working(self, client):
        # The cost of the button, and the reason the note says so.
        org, (admin,) = _team("admin")
        invited, first = _email(), new_invite_token()
        _db.create_invite(org, invited, "user", first)
        _signed_in_as(client, org, admin)

        client.post("/org/invites/reissue", data={"email": invited})

        assert _db.get_invite(first) is None
        assert len(_db.list_pending_invites(org)) == 1

    def test_the_role_comes_from_the_invitation_not_the_form(self, client):
        # A button meaning "let me have the link again" must not be a
        # promotion. The form field is ignored on purpose.
        org, (admin,) = _team("admin")
        invited = _email()
        _db.create_invite(org, invited, "user", new_invite_token())
        _signed_in_as(client, org, admin)

        client.post("/org/invites/reissue",
                    data={"email": invited, "role": "admin"})

        assert _db.list_pending_invites(org)[0]["role"] == "user"

    def test_another_teams_invitation_cannot_be_reissued(self, client):
        # Otherwise this is a way to mint a credential into a team you
        # are not an admin of.
        theirs, _ = _team("user")
        mine, (admin,) = _team("admin")
        invited, token = _email(), new_invite_token()
        _db.create_invite(theirs, invited, "user", token)
        _signed_in_as(client, mine, admin)

        client.post("/org/invites/reissue", data={"email": invited})

        assert _db.get_invite(token) is not None
        assert len(_db.list_pending_invites(theirs)) == 1

    def test_a_plain_user_cannot_reissue(self, client):
        org, (admin, user) = _team("admin", "user")
        invited, token = _email(), new_invite_token()
        _db.create_invite(org, invited, "user", token)
        _signed_in_as(client, org, user)

        resp = client.post("/org/invites/reissue", data={"email": invited},
                           headers={"Accept": "application/json"})

        assert resp.status_code == 403
        assert _db.get_invite(token) is not None

    def test_an_address_with_no_invitation_is_not_invited_by_it(self, client):
        # The button must not be a second, unlabelled invite form.
        org, (admin,) = _team("admin")
        _signed_in_as(client, org, admin)

        client.post("/org/invites/reissue", data={"email": _email()})

        assert _db.list_pending_invites(org) == []
        assert "no longer active" in self._flash(client)


# ── End to end: the only door into the platform ───────────────────

class TestOnboardingRoundTrip:
    def test_invite_then_accept_then_sign_in(self, client):
        org, (admin,) = _team("admin")
        _signed_in_as(client, org, admin)
        invited = _email()

        # 1. Admin invites.
        client.post("/org/members/invite",
                    data={"email": invited, "role": "user"})
        with _db.session_scope() as sess:
            token = sess.query(_db.Invite.token).filter(
                _db.Invite.email == invited).scalar()
        assert token

        # 2. The invitee accepts in a fresh browser.
        newcomer = client.application.test_client()
        password = "a perfectly fine passphrase"
        resp = newcomer.post(f"/auth/accept/{token}",
                             data={"password": password,
                                   "password_confirm": password,
                                   "display_name": "Newcomer"})
        assert resp.status_code == 302

        # 3. They are signed in, on the team, at the invited role.
        me = newcomer.get("/auth/me").get_json()
        assert me["authenticated"] is True
        assert me["role"] == "user"
        assert me["user"]["email"] == invited

        # 4. And they can sign in again later.
        newcomer.post("/auth/logout")
        again = newcomer.post("/auth/login",
                              data={"email": invited, "password": password})
        assert again.status_code == 302
        assert newcomer.get("/auth/me").get_json()["role"] == "user"

    def test_the_newcomer_cannot_do_admin_things(self, client):
        org, (admin,) = _team("admin")
        _signed_in_as(client, org, admin)
        invited = _email()
        client.post("/org/members/invite",
                    data={"email": invited, "role": "user"})
        with _db.session_scope() as sess:
            token = sess.query(_db.Invite.token).filter(
                _db.Invite.email == invited).scalar()

        newcomer = client.application.test_client()
        password = "another fine passphrase"
        newcomer.post(f"/auth/accept/{token}",
                      data={"password": password, "password_confirm": password})
        resp = newcomer.post("/org/members/invite",
                             data={"email": _email(), "role": "admin"},
                             headers={"Accept": "application/json"})
        assert resp.status_code == 403
