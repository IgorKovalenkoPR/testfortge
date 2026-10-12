"""Guards of the release load-test kit (tools/loadtest).

The cleanup deletes rows on a shared production database, so what is tested
here is mostly what it must *refuse*: any organisation name that is not
exactly a load-test tenant, an ambiguous tenant, and a seeded-looking user
who also belongs to a real team. The round trip at the end proves the seed
is idempotent and that the cleanup leaves another organisation untouched.
"""
from __future__ import annotations

import io

import pytest

from tools.loadtest import _common as C
from tools.loadtest import cleanup_loadtest, seed_loadtest

PASSWORD = "loadtest-unit-pass-2026"


def _org_ids(name: str) -> list[str]:
    return seed_loadtest.find_org_ids(name)


def _purge_loadtest_orgs() -> None:
    """Remove every LOADTEST* org by hand — the ambiguous-org test leaves a
    state the cleanup (correctly) refuses to touch."""
    from engine import db as _db
    from engine.db import Organization, OrgMember, Project, User
    with _db.session_scope() as sess:
        oids = [o for (o,) in sess.query(Organization.id).filter(
            Organization.name.in_(C.ORG_NAMES))]
        pids = [p for (p,) in sess.query(Project.id).filter(
            Project.org_id.in_(oids))] if oids else []
    for pid in pids:
        _db.delete_bugs_for_project(pid)
        _db.delete_project(pid)
    with _db.session_scope() as sess:
        if oids:
            sess.query(OrgMember).filter(OrgMember.org_id.in_(oids)).delete(
                synchronize_session=False)
            sess.query(Organization).filter(Organization.id.in_(oids)).delete(
                synchronize_session=False)
        for user in sess.query(User).all():
            if C.is_loadtest_email(user.email):
                sess.query(OrgMember).filter(
                    OrgMember.user_id == user.id).delete(
                        synchronize_session=False)
                sess.delete(user)


@pytest.fixture
def clean(app):
    from engine import db as _db
    _db.init_db()
    _purge_loadtest_orgs()
    yield
    _purge_loadtest_orgs()


def _run(module, *argv) -> tuple[int, str]:
    out = io.StringIO()
    code = module.main(list(argv), out=out)
    return code, out.getvalue()


# ── The name guard ────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["LOADTEST", "LOADTEST-B"])
def test_org_name_guard_accepts_only_the_two_tenants(name):
    assert C.check_org_name(name) == name


@pytest.mark.parametrize("name", [
    "loadtest", "LOADTEST ", " LOADTEST", "LOADTEST-prod", "LOADTEST-C",
    "Suite Org", "QAREA", "", None, "LOADTEST\n", "LOAD TEST"])
def test_org_name_guard_refuses_everything_else(name):
    with pytest.raises(C.RefusedError):
        C.check_org_name(name)


@pytest.mark.parametrize("email,ours", [
    ("lt-admin@loadtest.testforge.invalid", True),
    ("lt-user-07@loadtest.testforge.invalid", True),
    ("lt-b-user@loadtest.testforge.invalid", True),
    ("LT-Admin@LoadTest.TestForge.invalid", True),
    ("lt-admin@loadtest.testforge.invalid.example.com", False),
    ("admin@loadtest.testforge.invalid", False),
    ("lt-admin@qarea.com", False),
    ("someone@example.com", False),
    ("", False), (None, False)])
def test_loadtest_email_predicate(email, ours):
    assert C.is_loadtest_email(email) is ours


# ── Refusals of the scripts themselves ────────────────────────────────

def test_cleanup_refuses_a_foreign_org_name_before_touching_the_db(clean):
    from engine import db as _db
    real = _db.create_organization("Real Team for cleanup guard")
    code, out = _run(cleanup_loadtest, "--org", "Real Team for cleanup guard",
                     "--yes")
    assert code == 2
    assert "REFUSED" in out
    assert _db.get_organization(real) is not None


@pytest.mark.parametrize("name", ["loadtest", "LOADTEST-prod"])
def test_cleanup_refuses_near_miss_names(clean, name):
    code, out = _run(cleanup_loadtest, "--org", name, "--yes")
    assert code == 2 and "REFUSED" in out


def test_cleanup_refuses_an_ambiguous_tenant_and_deletes_nothing(clean):
    from engine import db as _db
    a = _db.create_organization(C.ORG_NAME)
    b = _db.create_organization(C.ORG_NAME)          # same name, new slug
    pid = _db.upsert_project("ambiguous tenant project", org_id=a)
    code, out = _run(cleanup_loadtest, "--org", C.ORG_NAME, "--yes")
    assert code == 2 and "refusing to guess" in out
    assert _db.get_organization(a) and _db.get_organization(b)
    assert _db.get_project(pid) is not None


def test_seed_refuses_without_a_password(clean, monkeypatch):
    monkeypatch.delenv("LOADTEST_PASSWORD", raising=False)
    code, out = _run(seed_loadtest, "--users", "1", "--tcs", "1",
                     "--checklist", "1", "--bugs", "1")
    assert code == 2 and "LOADTEST_PASSWORD" in out
    assert _org_ids(C.ORG_NAME) == []


def test_seed_refuses_a_password_the_product_would_refuse(clean, monkeypatch):
    monkeypatch.setenv("LOADTEST_PASSWORD", "short")
    code, out = _run(seed_loadtest, "--users", "1")
    assert code == 2 and "REFUSED" in out
    assert _org_ids(C.ORG_NAME) == []


def test_seed_refuses_an_ambiguous_tenant(clean, monkeypatch):
    from engine import db as _db
    monkeypatch.setenv("LOADTEST_PASSWORD", PASSWORD)
    _db.create_organization(C.ORG_NAME)
    _db.create_organization(C.ORG_NAME)
    code, out = _run(seed_loadtest, "--users", "1", "--tcs", "1",
                     "--checklist", "1", "--bugs", "1")
    assert code == 2 and "refusing to guess" in out


# ── Round trip ────────────────────────────────────────────────────────

def _counts_for(pid):
    return seed_loadtest._counts(pid)


def test_seed_is_idempotent_and_cleanup_leaves_other_orgs_alone(
        clean, monkeypatch):
    from engine import db as _db
    monkeypatch.setenv("LOADTEST_PASSWORD", PASSWORD)

    # Somebody's real team, with work in it.
    real_org = _db.create_organization("Real Team for round trip")
    real_user = _db.create_user("real.person@example.com")
    _db.add_org_member(real_org, real_user, "admin")
    real_pid = _db.upsert_project("Real Team project", org_id=real_org)
    _db.save_test_cases(real_pid, [{"id": "TC-001", "summary": "real",
                                    "expected_result": "kept"}])
    _db.save_bug(real_pid, {"id": "BUG-001", "title": "a real bug"})
    # A seeded-looking address that also belongs to the real team: the
    # cleanup may detach it from LOADTEST but must not delete it.
    shared = _db.create_user(C.user_email(2))
    _db.add_org_member(real_org, shared, "user")

    argv = ("--users", "2", "--tcs", "12", "--checklist", "6", "--bugs", "4")
    code, out = _run(seed_loadtest, *argv)
    assert code == 2 and "outside the load test" in out   # refused: shared
    _db.remove_org_member(real_org, shared)
    code, out = _run(seed_loadtest, *argv)
    assert code == 0, out

    org_a = _org_ids(C.ORG_NAME)
    assert len(org_a) == 1 and len(_org_ids(C.ORG_NAME_B)) == 1
    big = next(p["id"] for p in _db.list_projects_for_org(org_a[0])
               if p["name"] == C.BIG_PROJECT)
    expected = {"test_cases": 12, "checklist_items": 6, "bugs": 4}
    assert _counts_for(big) == expected
    members = len(_db.list_org_members(org_a[0]))
    assert members == 3                       # admin + 2 users

    # Idempotent: a second run converges, it does not duplicate.
    code, out = _run(seed_loadtest, *argv)
    assert code == 0, out
    assert _org_ids(C.ORG_NAME) == org_a
    assert _counts_for(big) == expected
    assert len(_db.list_org_members(org_a[0])) == members

    # Later, a real team adds one of the seeded addresses. The cleanup may
    # detach it from LOADTEST but must not delete it.
    _db.add_org_member(real_org, shared, "user")

    # Dry run deletes nothing.
    code, out = _run(cleanup_loadtest)
    assert code == 0 and "dry run" in out
    assert _org_ids(C.ORG_NAME) == org_a

    code, out = _run(cleanup_loadtest, "--yes")
    assert code == 0, out
    assert "OK: every other organisation is unchanged" in out
    assert _org_ids(C.ORG_NAME) == [] and _org_ids(C.ORG_NAME_B) == []
    assert _db.get_project(big) is None
    assert _db.get_user_by_email(C.admin_email()) is None
    # The shared address survives, still in the real team.
    assert _db.get_user_by_email(C.user_email(2)) is not None
    assert _db.get_org_role(real_org, shared) == "user"
    # The real team is untouched.
    assert _db.get_project(real_pid) is not None
    assert _counts_for(real_pid) == {"test_cases": 1, "checklist_items": 0,
                                     "bugs": 1}
    assert _db.get_user(real_user) is not None

    # And a second cleanup is a no-op.
    code, out = _run(cleanup_loadtest, "--yes")
    assert code == 0 and "nothing to do" in out
