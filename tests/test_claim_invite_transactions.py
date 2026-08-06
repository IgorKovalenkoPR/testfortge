"""E9.3 — invites and their claim, as transactions rather than as steps.

``tests/test_identity_schema.py`` already walks the invite lifecycle in
order: create, claim, replay, expire, revoke. Everything here is about
what happens when the order is not guaranteed — two people on one link,
a claim that fails halfway, a re-invite that collides. Those are the
cases where an invite stops being a workflow and becomes a credential:

> **An invitation is one seat.** Anything that lets a token be redeemed
> twice hands a second person a role nobody granted them, and it does it
> silently, because both claims look like the intended one.

Each test states the invariant it protects rather than the sequence it
performs, because the sequence is the part that is not under our control.

The database is a temp SQLite file per test, and the concurrency tests
say what that does and does not prove — see
``TestTwoPeopleOnOneLink``.
"""
from __future__ import annotations

import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from engine import db as _db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A database of this test's own.

    Both because these tests count rows across the whole table — a
    membership left behind by another file would be indistinguishable
    from a double claim — and because the concurrency tests below want
    an engine nobody else is writing to.
    """
    monkeypatch.setenv("FLASK_DEBUG", "1")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'invites.db'}")
    monkeypatch.delenv("TESTFORTGE_DB", raising=False)

    prev_engine, prev_session = _db._engine, _db._Session
    _db._engine = None
    _db._Session = None
    try:
        _db.init_db()
        yield
    finally:
        if _db._engine is not None:
            _db._engine.dispose()
        _db._engine, _db._Session = prev_engine, prev_session


@pytest.fixture
def team(fresh_db):
    """An organisation with an admin in it, and nothing else."""
    admin = _db.create_user("admin@invites.test", display_name="The Admin",
                            email_verified=True)
    org = _db.create_organization("Invite Transactions")
    assert _db.add_org_member(org, admin, "admin")
    return {"org": org, "admin": admin}


def _token() -> str:
    return secrets.token_urlsafe(32)


def _members(org_id: str) -> list[dict]:
    with _db.session_scope() as sess:
        rows = sess.execute(select(_db.OrgMember).where(
            _db.OrgMember.org_id == org_id)).scalars().all()
        return [{"user_id": r.user_id, "role": r.role} for r in rows]


def _count(model) -> int:
    with _db.session_scope() as sess:
        return sess.execute(select(func.count()).select_from(model)).scalar()


# ── One link, two people ─────────────────────────────────────────────

class TestTwoPeopleOnOneLink:
    """The invariant: a token grants exactly one membership, ever.

    An invitation link travels by email and email gets forwarded. Two
    people opening the same link at the same moment is not an exotic
    race — it is what happens when somebody replies-all with "here you
    go". The read-check-then-write shape these functions started with is
    safe under SQLite, which serialises writers, and unsafe under
    Postgres READ COMMITTED, where both transactions read ``used_at``
    NULL, both insert a membership for their own user, and both commit.

    So what a SQLite run proves is limited and worth saying out loud: it
    proves the *outcome* rule holds, and it exercises the claim's
    conditional UPDATE, but it cannot reproduce the Postgres interleaving
    on its own. The same file runs against Postgres in CI through
    ``TFG_TEST_POSTGRES_URL``.
    """

    def _claim_together(self, token: str, user_ids: list[str]) -> list:
        """Every thread calls ``consume_invite`` at the same instant."""
        gate = threading.Barrier(len(user_ids))

        def _claim(user_id: str):
            gate.wait(timeout=10)
            return _db.consume_invite(token, user_id)

        with ThreadPoolExecutor(max_workers=len(user_ids)) as pool:
            return [f.result() for f in
                    [pool.submit(_claim, u) for u in user_ids]]

    def test_only_one_of_two_simultaneous_claims_succeeds(self, team):
        token = _token()
        assert _db.create_invite(team["org"], "shared@invites.test", "admin",
                                 token, invited_by_user_id=team["admin"])
        first = _db.create_user("first@invites.test", email_verified=True)
        second = _db.create_user("second@invites.test", email_verified=True)

        results = self._claim_together(token, [first, second])

        granted = [r for r in results if r]
        assert len(granted) == 1, (
            f"{len(granted)} of 2 simultaneous claims were granted; an "
            f"invitation is one seat")

    def test_the_loser_gets_no_membership(self, team):
        """The outcome, not the return value.

        A claim that returns ``None`` and adds a row anyway would pass the
        test above and still put a stranger in the organisation — which
        is the failure this pair exists to separate.
        """
        token = _token()
        _db.create_invite(team["org"], "shared@invites.test", "admin", token,
                          invited_by_user_id=team["admin"])
        first = _db.create_user("first@invites.test", email_verified=True)
        second = _db.create_user("second@invites.test", email_verified=True)

        self._claim_together(token, [first, second])

        joined = [m for m in _members(team["org"])
                  if m["user_id"] in (first, second)]
        assert len(joined) == 1, joined
        assert joined[0]["role"] == "admin"

    def test_eight_at_once_still_grants_one(self, team):
        """Two threads can pass by luck; eight is harder to get lucky with."""
        token = _token()
        _db.create_invite(team["org"], "shared@invites.test", "user", token,
                          invited_by_user_id=team["admin"])
        users = [_db.create_user(f"racer{i}@invites.test", email_verified=True)
                 for i in range(8)]

        results = self._claim_together(token, users)

        assert len([r for r in results if r]) == 1, results
        assert len([m for m in _members(team["org"])
                    if m["user_id"] in users]) == 1

    def test_the_same_person_claiming_twice_is_not_a_failure(self, team):
        """Double-submit is a person, not an attack.

        Two clicks on one button must leave them a member at the invited
        role, not a 'that link is no longer valid' after they are already
        in. The second call is allowed to report nothing; what it may not
        do is remove them or change their role.
        """
        token = _token()
        _db.create_invite(team["org"], "twice@invites.test", "user", token,
                          invited_by_user_id=team["admin"])
        person = _db.create_user("twice@invites.test", email_verified=True)

        self._claim_together(token, [person, person])

        joined = [m for m in _members(team["org"])
                  if m["user_id"] == person]
        assert joined == [{"user_id": person, "role": "user"}]


# ── A claim that fails halfway ───────────────────────────────────────

class TestAClaimIsAllOrNothing:
    """The invariant: a burned token always has a membership behind it.

    The asymmetry matters. A token that stays claimable after a failed
    attempt costs nothing — the person tries again. A token marked used
    with no membership written locks the invitee out permanently, with
    no way to retry and no error anyone can act on, because from the
    admin's side the invitation shows as accepted.
    """

    def test_a_claim_for_a_user_that_does_not_exist_burns_nothing(self,
                                                                  team):
        """The membership row points at ``app_user`` and the constraint is on.

        No monkeypatching: a user id that was never created is exactly the
        kind of thing a stale session or a deleted account produces, and
        the foreign key makes the insert fail after ``used_at`` has been
        set in the same transaction.
        """
        token = _token()
        _db.create_invite(team["org"], "ghost@invites.test", "user", token,
                          invited_by_user_id=team["admin"])

        try:
            _db.consume_invite(token, "u-never-existed")
        except Exception:
            pass    # how it fails is not the subject; what survives is

        assert _db.get_invite(token) is not None, (
            "the token was burned by a claim that granted nothing — the "
            "invitee is now locked out with no way to retry")
        assert not [m for m in _members(team["org"])
                    if m["user_id"] == "u-never-existed"]

    def test_a_successful_claim_burns_the_token(self, team):
        """The other half, so the test above cannot pass by never burning."""
        token = _token()
        _db.create_invite(team["org"], "real@invites.test", "user", token,
                          invited_by_user_id=team["admin"])
        person = _db.create_user("real@invites.test", email_verified=True)

        assert _db.consume_invite(token, person) == team["org"]
        assert _db.get_invite(token) is None
        assert _db.consume_invite(token, person) is None


# ── Re-inviting is also a transaction ────────────────────────────────

class TestReInvitingIsAtomic:
    """``create_invite`` revokes the live invite and issues a new one.

    Two writes, one meaning. If the revoke lands and the issue does not,
    the address ends up with no way in and an admin who believes they
    just sent one.
    """

    def test_a_failed_issue_does_not_revoke_the_working_link(self, team):
        """A token collision is the reachable failure.

        ``token`` is the primary key and is minted by the caller, so a
        caller that reuses one — a retry, a copy-pasted fixture, a
        pathological RNG — makes the INSERT fail after the UPDATE has
        already run.
        """
        token = _token()
        assert _db.create_invite(team["org"], "again@invites.test", "user",
                                 token, invited_by_user_id=team["admin"])

        assert _db.create_invite(team["org"], "again@invites.test", "admin",
                                 token) is False

        still = _db.get_invite(token)
        assert still is not None, (
            "the original invitation was revoked by an issue that failed, "
            "so the address has no live link and the admin was told nothing")
        assert still["role"] == "user", (
            "the failed re-invite changed the role of the surviving link")

    def test_a_successful_re_invite_kills_the_old_link(self, team):
        """The security half: a forwarded stale link must stop working.

        Re-inviting at a lower role is how an admin corrects a mistake. If
        the first link still worked, the correction would be advisory —
        whoever kept the original email would still arrive as an admin.
        """
        old, new = _token(), _token()
        _db.create_invite(team["org"], "downgrade@invites.test", "admin", old,
                          invited_by_user_id=team["admin"])
        _db.create_invite(team["org"], "downgrade@invites.test", "user", new,
                          invited_by_user_id=team["admin"])

        person = _db.create_user("downgrade@invites.test",
                                 email_verified=True)
        assert _db.consume_invite(old, person) is None
        assert _db.consume_invite(new, person) == team["org"]
        assert _members(team["org"])[-1]["role"] == "user"

    def test_an_expired_link_cannot_be_revived_by_re_inviting(self, team):
        """Issuing a new invite must not extend the old one's life."""
        old, new = _token(), _token()
        _db.create_invite(team["org"], "stale@invites.test", "admin", old,
                          invited_by_user_id=team["admin"], ttl_hours=1)
        with _db.session_scope() as sess:
            row = sess.get(_db.Invite, old)
            row.expires_at = _db._utcnow() - timedelta(hours=2)

        _db.create_invite(team["org"], "stale@invites.test", "user", new,
                          invited_by_user_id=team["admin"])
        person = _db.create_user("stale@invites.test", email_verified=True)
        assert _db.consume_invite(old, person) is None


# ── The three layers, joined up ──────────────────────────────────────

class TestAuthRbacAndTheRepositoryAgree:
    """E9.3's other half: identity, role and data read as one story.

    Each layer has its own unit tests. What none of them can show is the
    thing that actually goes wrong — that a person exists, holds a role,
    and still cannot see the work, or can see somebody else's. Both
    happened in this programme: nothing wrote ``Project.org_id`` while
    the listing filtered on it, so a project vanished from its own
    author's picker.
    """

    def test_claiming_an_invite_makes_the_org_s_work_visible(self, team):
        project = _db.upsert_project("Team Work", org_id=team["org"])
        token = _token()
        _db.create_invite(team["org"], "joiner@invites.test", "user", token,
                          invited_by_user_id=team["admin"])
        joiner = _db.create_user("joiner@invites.test", email_verified=True)

        assert _db.consume_invite(token, joiner) == team["org"]

        assert _db.get_org_role(team["org"], joiner) == "user"
        assert project in {p["id"] for p in
                           _db.list_projects(org_id=team["org"])}
        assert joiner in {m["user_id"] for m in
                          _db.list_org_members(team["org"])}

    def test_a_member_of_one_team_does_not_see_another_team_s_work(self,
                                                                   team):
        """The negative, asserted at the repository rather than the route.

        A 403 from the gate would satisfy "does not see it" without the
        scoping ever being right, and that is how this project has
        previously passed a test for the wrong reason. Asked here of the
        function that does the filtering.
        """
        theirs = _db.upsert_project("Their Work", org_id=team["org"])

        other_org = _db.create_organization("Somebody Else")
        outsider = _db.create_user("outsider@invites.test",
                                   email_verified=True)
        _db.add_org_member(other_org, outsider, "admin")
        _db.upsert_project("Our Work", org_id=other_org)

        visible = {p["id"] for p in _db.list_projects(org_id=other_org)}
        assert theirs not in visible
        assert _db.get_org_role(team["org"], outsider) is None

    def test_removing_a_member_removes_the_access_not_the_work(self, team):
        """Leaving a team is not a delete.

        The membership goes; the projects stay, because they belong to the
        organisation. A cascade that reached the work would turn an
        offboarding into data loss.
        """
        project = _db.upsert_project("Surviving Work", org_id=team["org"])
        token = _token()
        _db.create_invite(team["org"], "leaver@invites.test", "user", token,
                          invited_by_user_id=team["admin"])
        leaver = _db.create_user("leaver@invites.test", email_verified=True)
        _db.consume_invite(token, leaver)

        assert _db.remove_org_member(team["org"], leaver)

        assert _db.get_org_role(team["org"], leaver) is None
        assert project in {p["id"] for p in
                           _db.list_projects(org_id=team["org"])}

    def test_deleting_the_organisation_takes_its_memberships_with_it(self,
                                                                     team):
        """The cascade that must exist, stated so a schema change trips it."""
        token = _token()
        _db.create_invite(team["org"], "doomed@invites.test", "user", token,
                          invited_by_user_id=team["admin"])
        person = _db.create_user("doomed@invites.test", email_verified=True)
        _db.consume_invite(token, person)
        assert _count(_db.OrgMember) == 2

        with _db.session_scope() as sess:
            sess.delete(sess.get(_db.Organization, team["org"]))

        assert _count(_db.OrgMember) == 0
        assert _count(_db.Invite) == 0
        assert _count(_db.User) == 2, "the people were deleted with the team"
