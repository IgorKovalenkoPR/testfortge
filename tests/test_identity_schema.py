"""Identity, tenancy, invites and audit — engine/db.py (E1.1 / E2.1 / E2.7).

Schema-level tests for the tables the whole programme hangs off. They run
against whatever ``DATABASE_URL`` / ``TESTFORTGE_DB`` the suite is
pointed at, so the same file is the Postgres check when CI supplies one
(the acceptance criterion for E1.1 is "idempotent on SQLite *and*
Postgres").
"""

import secrets
from datetime import datetime, timedelta, timezone

import pytest

from engine import db as _db


@pytest.fixture(autouse=True)
def _db_ready():
    _db.init_db()


def _email() -> str:
    """A fresh address per test — the suite shares one database file."""
    return f"user-{secrets.token_hex(6)}@example.com"


def _token() -> str:
    return secrets.token_urlsafe(32)


class TestMigrationIdempotence:
    def test_init_db_twice_is_a_noop(self):
        # The acceptance criterion for every migration in this programme:
        # a second boot must not fail. Render restarts constantly on the
        # free tier, so this runs many times a day in production.
        _db.init_db()
        _db.init_db()
        assert _db.ping() is True

    def test_project_carries_the_org_column(self):
        from sqlalchemy import inspect
        cols = {c["name"] for c in
                inspect(_db.get_engine()).get_columns("project")}
        assert "org_id" in cols

    def test_user_table_avoids_the_reserved_word(self):
        # Named app_user precisely so the hand-written ALTER statements
        # this module issues never have to quote a reserved identifier.
        assert _db.User.__tablename__ == "app_user"


class TestUsers:
    def test_create_and_fetch_by_email(self):
        email = _email()
        uid = _db.create_user(email, display_name="Test Tester")
        assert uid
        row = _db.get_user_by_email(email)
        assert row["id"] == uid
        assert row["display_name"] == "Test Tester"
        # Nothing has verified this address yet, and no route may assume
        # it has.
        assert row["email_verified"] is False
        assert row["is_active"] is True

    def test_email_is_normalised_on_write_and_on_read(self):
        email = _email()
        uid = _db.create_user(email.upper())
        assert _db.get_user_by_email(f"  {email.upper()}  ")["id"] == uid
        assert _db.get_user(uid)["email"] == email.lower()

    def test_duplicate_email_returns_none_rather_than_raising(self):
        email = _email()
        assert _db.create_user(email)
        # The second sign-up for the same address is an ordinary branch at
        # the call site, not an exception to catch.
        assert _db.create_user(email) is None

    def test_case_differing_duplicate_is_still_a_duplicate(self):
        # The bug this prevents: signing up as Bob@x.com then again as
        # bob@x.com and getting two empty workspaces.
        email = _email()
        assert _db.create_user(email.lower())
        assert _db.create_user(email.upper()) is None

    def test_google_only_account_has_no_password(self):
        uid = _db.create_user(_email(), email_verified=True)
        assert _db.get_user(uid)["password_hash"] is None

    def test_blank_email_is_refused(self):
        assert _db.create_user("   ") is None
        assert _db.create_user(None) is None


class TestExternalIdentities:
    def test_link_and_look_up_by_provider_subject(self):
        uid = _db.create_user(_email())
        sub = f"sub-{secrets.token_hex(6)}"
        assert _db.link_identity(uid, "google", sub, email="x@example.com")
        assert _db.get_user_by_identity("google", sub)["id"] == uid

    def test_linking_the_same_identity_twice_is_idempotent(self):
        uid = _db.create_user(_email())
        sub = f"sub-{secrets.token_hex(6)}"
        assert _db.link_identity(uid, "google", sub)
        assert _db.link_identity(uid, "google", sub)

    def test_identity_cannot_be_stolen_by_a_second_user(self):
        # Re-pointing an existing (provider, subject) at a different user
        # would hand one person's whole workspace to another.
        first = _db.create_user(_email())
        second = _db.create_user(_email())
        sub = f"sub-{secrets.token_hex(6)}"
        assert _db.link_identity(first, "google", sub)
        assert _db.link_identity(second, "google", sub) is False
        assert _db.get_user_by_identity("google", sub)["id"] == first

    def test_unknown_identity_reads_as_none(self):
        assert _db.get_user_by_identity("google", "nobody") is None


class TestOrganisationsAndRoles:
    def test_membership_role_round_trips(self):
        org = _db.create_organization("QA Team")
        uid = _db.create_user(_email())
        assert _db.add_org_member(org, uid, "admin")
        assert _db.get_org_role(org, uid) == "admin"

    def test_non_member_has_no_role_at_all(self):
        # None means no access. A caller that defaulted this to "user"
        # would be letting a stranger into the workspace.
        org = _db.create_organization("QA Team")
        assert _db.get_org_role(org, _db.create_user(_email())) is None

    def test_unknown_role_is_refused(self):
        org = _db.create_organization("QA Team")
        uid = _db.create_user(_email())
        assert _db.add_org_member(org, uid, "superadmin") is False
        assert _db.get_org_role(org, uid) is None

    def test_re_adding_a_member_changes_their_role(self):
        org = _db.create_organization("QA Team")
        uid = _db.create_user(_email())
        _db.add_org_member(org, uid, "user")
        assert _db.add_org_member(org, uid, "admin")
        assert _db.get_org_role(org, uid) == "admin"
        assert len(_db.list_org_members(org)) == 1

    def test_two_orgs_may_share_a_name_but_not_a_slug(self):
        a = _db.create_organization("QA")
        b = _db.create_organization("QA")
        assert a != b

    def test_members_are_listed_admins_first(self):
        org = _db.create_organization("QA Team")
        u1 = _db.create_user("zz-" + _email())
        u2 = _db.create_user("aa-" + _email())
        _db.add_org_member(org, u1, "admin")
        _db.add_org_member(org, u2, "user")
        assert [m["role"] for m in _db.list_org_members(org)] == \
            ["admin", "user"]

    def test_role_rank_orders_admin_above_user(self):
        assert _db.ROLE_RANK["admin"] > _db.ROLE_RANK["user"]
        assert set(_db.ORG_ROLES) == {"admin", "user"}


class TestLastAdminGuard:
    def test_the_only_admin_cannot_be_removed(self):
        # Enforced in the data layer so no route can forget it and strand
        # an org with nobody able to change its settings.
        org = _db.create_organization("QA Team")
        admin = _db.create_user(_email())
        _db.add_org_member(org, admin, "admin")
        assert _db.remove_org_member(org, admin) is False
        assert _db.get_org_role(org, admin) == "admin"

    def test_an_admin_can_be_removed_once_a_second_one_exists(self):
        org = _db.create_organization("QA Team")
        a, b = _db.create_user(_email()), _db.create_user(_email())
        _db.add_org_member(org, a, "admin")
        _db.add_org_member(org, b, "admin")
        assert _db.count_org_admins(org) == 2
        assert _db.remove_org_member(org, a) is True
        assert _db.count_org_admins(org) == 1

    def test_a_plain_user_can_always_be_removed(self):
        org = _db.create_organization("QA Team")
        admin, user = _db.create_user(_email()), _db.create_user(_email())
        _db.add_org_member(org, admin, "admin")
        _db.add_org_member(org, user, "user")
        assert _db.remove_org_member(org, user) is True


class TestProjectOwnership:
    def test_project_attaches_to_an_org(self):
        org = _db.create_organization("QA Team")
        pid = _db.upsert_project(name=f"P-{secrets.token_hex(4)}")
        assert _db.set_project_org(pid, org)
        assert [p["id"] for p in _db.list_projects_for_org(org)] == [pid]

    def test_a_nonexistent_org_is_refused(self):
        # Standing in for the foreign key the column cannot carry: without
        # this check a typo'd org_id makes a project no membership query
        # can ever reach, i.e. silently orphaned data.
        pid = _db.upsert_project(name=f"P-{secrets.token_hex(4)}")
        assert _db.set_project_org(pid, "deadbeef" * 4) is False

    def test_legacy_projects_have_no_org(self):
        # Every project that exists today predates organisations. Routes
        # must read NULL as "legacy, owner_sid governs", never as public.
        pid = _db.upsert_project(name=f"P-{secrets.token_hex(4)}")
        assert _db.get_project(pid)["org_id"] is None


class TestInvites:
    def test_create_then_claim_grants_the_invited_role(self):
        org = _db.create_organization("QA Team")
        tok = _token()
        assert _db.create_invite(org, "  New.Person@Example.COM ", "user", tok)
        invite = _db.get_invite(tok)
        # Normalised on the way in, so the claim can match it however the
        # invitee's browser autofills the address. Casing and surrounding
        # whitespace only — syntactic validation is the form's job (E1.2),
        # because quietly mangling what someone typed is worse than saying
        # no to it.
        assert invite["email"] == "new.person@example.com"
        uid = _db.create_user(_email())
        assert _db.consume_invite(tok, uid) == org
        assert _db.get_org_role(org, uid) == "user"

    def test_a_claimed_invite_cannot_be_replayed(self):
        org = _db.create_organization("QA Team")
        tok = _token()
        _db.create_invite(org, _email(), "user", tok)
        assert _db.consume_invite(tok, _db.create_user(_email())) == org
        assert _db.get_invite(tok) is None
        assert _db.consume_invite(tok, _db.create_user(_email())) is None

    def test_re_inviting_revokes_the_previous_link(self):
        # A forwarded stale invite must not be usable to join at a role
        # that was since downgraded.
        org = _db.create_organization("QA Team")
        email = _email()
        old, new = _token(), _token()
        _db.create_invite(org, email, "admin", old)
        _db.create_invite(org, email, "user", new)
        assert _db.get_invite(old) is None
        assert _db.get_invite(new)["role"] == "user"

    def test_someone_who_left_can_be_invited_again(self):
        # The reason this schema has no UNIQUE (org_id, email): a used row
        # would block the seat forever.
        org = _db.create_organization("QA Team")
        email = _email()
        first, second = _token(), _token()
        uid = _db.create_user(email)
        _db.create_invite(org, email, "admin", first)
        _db.consume_invite(first, uid)
        # A second admin so the guard lets the first one go.
        other = _db.create_user(_email())
        _db.add_org_member(org, other, "admin")
        assert _db.remove_org_member(org, uid) is True
        assert _db.create_invite(org, email, "user", second) is True
        assert _db.consume_invite(second, uid) == org

    def test_an_expired_invite_is_not_claimable(self):
        org = _db.create_organization("QA Team")
        tok = _token()
        _db.create_invite(org, _email(), "user", tok, ttl_hours=1)
        with _db.session_scope() as sess:
            row = sess.get(_db.Invite, tok)
            row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        assert _db.get_invite(tok) is None
        assert _db.consume_invite(tok, _db.create_user(_email())) is None

    def test_a_revoked_invite_is_not_claimable(self):
        org = _db.create_organization("QA Team")
        tok = _token()
        _db.create_invite(org, _email(), "user", tok)
        assert _db.revoke_invite(tok) is True
        assert _db.consume_invite(tok, _db.create_user(_email())) is None

    def test_unknown_role_and_unknown_org_are_refused(self):
        org = _db.create_organization("QA Team")
        assert _db.create_invite(org, _email(), "root", _token()) is False
        assert _db.create_invite("nope" * 8, _email(), "user", _token()) is False

    def test_pending_list_never_leaks_the_token(self):
        # A pending invite's token is a bearer credential for somebody
        # else's seat, and this list renders on the members page.
        org = _db.create_organization("QA Team")
        tok = _token()
        _db.create_invite(org, _email(), "user", tok)
        pending = _db.list_pending_invites(org)
        assert len(pending) == 1
        assert "token" not in pending[0]
        assert tok not in str(pending)


class TestAuditLog:
    def test_a_mutation_is_recorded_and_readable_back(self):
        org = _db.create_organization("QA Team")
        uid = _db.create_user(_email())
        pid = _db.upsert_project(name=f"P-{secrets.token_hex(4)}")
        assert _db.append_audit(entity="test_case", action="update",
                                user_id=uid, org_id=org, project_id=pid,
                                entity_id="TC-001",
                                diff={"priority": ["Low", "High"]})
        rows = _db.list_audit(project_id=pid)
        assert len(rows) == 1
        assert rows[0]["entity_id"] == "TC-001"
        assert rows[0]["diff"]["priority"] == ["Low", "High"]

    def test_scoping_by_entity_filters_the_noise(self):
        pid = _db.upsert_project(name=f"P-{secrets.token_hex(4)}")
        _db.append_audit(entity="bug", action="update", project_id=pid)
        _db.append_audit(entity="test_case", action="update", project_id=pid)
        assert len(_db.list_audit(project_id=pid, entity="bug")) == 1

    def test_auditing_never_raises(self):
        # An audit failure must not be the reason a user's edit fails.
        assert _db.append_audit(entity="", action="update") is None

    def test_newest_first(self):
        pid = _db.upsert_project(name=f"P-{secrets.token_hex(4)}")
        _db.append_audit(entity="bug", action="create", project_id=pid,
                         entity_id="BUG-1")
        _db.append_audit(entity="bug", action="update", project_id=pid,
                         entity_id="BUG-2")
        rows = _db.list_audit(project_id=pid)
        assert [r["entity_id"] for r in rows] == ["BUG-2", "BUG-1"]
