"""
A project belongs to an organisation (found by E9.9).

``upsert_project`` had no ``org_id`` parameter at all, and nothing ever
wrote ``Project.org_id`` — while ``visible_projects`` lists *only* the
caller's organisation's projects once ``ORG_MODE`` is on. The two halves of
E2 shipped separately, so under org mode **a project disappeared from the
picker the moment it was created**, including for the person who created it.

Nobody had met it because the flag is off in production. It surfaced the
first time the suite was run the way the product will ship, which is the
entire argument for E9.9.

Two properties are defended here, and the second is the migration:

* every creation path stamps the caller's organisation;
* a project that predates the flag can still be reached — ``upsert_project``
  adopts an unowned row, and ``adopt_orphan_projects`` sweeps the rest, but
  only when there is exactly one organisation to sweep them into.
"""
from __future__ import annotations

import pytest

import secrets

from engine import db as _db
from routes._shared import SERVER_START_TIME

#: Unique per run. conftest deletes the scratch database at import, but on
#: Windows the file is sometimes still held open and the delete is skipped —
#: it says so itself. Names keyed only on the test would then collide with
#: the previous run's rows, and ``upsert_project`` keys on the slug, so the
#: second run would assert against the first run's organisation.
_RUN = secrets.token_hex(4)


class TestUpsertStampsTheOrganisation:
    def test_a_new_project_records_the_org(self, request):
        org = _db.create_organization(f"own-{_RUN}-{request.node.name}")
        pid = _db.upsert_project(f"own-{_RUN}-{request.node.name}", org_id=org)
        assert (_db.get_project(pid) or {}).get("org_id") == org

    def test_without_an_org_the_column_stays_empty(self, request):
        # The flags-off deployment, which is every deployment today.
        pid = _db.upsert_project(f"own-none-{_RUN}-{request.node.name}")
        assert not (_db.get_project(pid) or {}).get("org_id")

    def test_an_unowned_project_is_adopted_on_the_next_write(self, request):
        """The migration path for a project created before the flag.

        Refusing to stamp it would leave it permanently unreachable — the
        listing is org-scoped, so an orphan is invisible to everyone.
        """
        name = f"own-adopt-{_RUN}-{request.node.name}"
        pid = _db.upsert_project(name)
        org = _db.create_organization(f"org-{_RUN}-{request.node.name}")
        again = _db.upsert_project(name, org_id=org)
        assert again == pid
        assert (_db.get_project(pid) or {}).get("org_id") == org

    def test_a_project_is_never_moved_between_organisations(self, request):
        """A silent transfer between teams is the one thing this must not do.

        Adoption fills an *empty* field. Overwriting a populated one would
        hand one team's work to another as a side effect of a save.
        """
        name = f"own-keep-{_RUN}-{request.node.name}"
        first = _db.create_organization(f"org-a-{_RUN}-{request.node.name}")
        second = _db.create_organization(f"org-b-{_RUN}-{request.node.name}")
        pid = _db.upsert_project(name, org_id=first)
        _db.upsert_project(name, org_id=second)
        assert (_db.get_project(pid) or {}).get("org_id") == first


class TestTheProjectIsVisibleToItsCreator:
    """The defect itself, at the level a person would notice it."""

    def test_a_project_created_through_the_app_appears_in_the_listing(
            self, client, auth_on):
        from routes import _shared
        org = auth_on.get("org_id")
        with client.session_transaction() as sess:
            sess["_session_active_since"] = SERVER_START_TIME
        resp = client.post("/save-project",
                           data={"project_name": f"Visible To Its Author {_RUN}"},
                           follow_redirects=True)
        assert resp.status_code == 200
        names = [p.get("name") for p in _db.list_projects_for_org(org)]
        assert f"Visible To Its Author {_RUN}" in names

    def test_the_helper_returns_the_callers_org_only_in_org_mode(self, auth_on):
        from routes import _shared
        assert _shared.org_for_new_project() == auth_on.get("org_id")

    def test_the_helper_returns_nothing_with_the_flags_off(self, auth_off):
        from routes import _shared
        assert _shared.org_for_new_project() is None


class TestAdoptingOrphans:
    def test_it_assigns_unowned_projects(self, request):
        pid = _db.upsert_project(f"orphan-{_RUN}-{request.node.name}")
        org = _db.create_organization(f"sole-{_RUN}-{request.node.name}")
        moved = _db.adopt_orphan_projects(org, only_when_sole_org=False)
        assert moved >= 1
        assert (_db.get_project(pid) or {}).get("org_id") == org

    def test_it_leaves_owned_projects_alone(self, request):
        other = _db.create_organization(f"other-{_RUN}-{request.node.name}")
        pid = _db.upsert_project(f"owned-{_RUN}-{request.node.name}", org_id=other)
        target = _db.create_organization(f"target-{_RUN}-{request.node.name}")
        _db.adopt_orphan_projects(target, only_when_sole_org=False)
        assert (_db.get_project(pid) or {}).get("org_id") == other

    def test_it_refuses_when_more_than_one_org_exists(self, request):
        """With several teams there is no way to tell whose an orphan is.

        Guessing would hand one team's work to another, so the sweep stops
        and the operator has to say project by project. A slower answer and
        the only honest one.
        """
        _db.create_organization(f"amb-a-{_RUN}-{request.node.name}")
        second = _db.create_organization(f"amb-b-{_RUN}-{request.node.name}")
        pid = _db.upsert_project(f"amb-{_RUN}-{request.node.name}")
        assert _db.adopt_orphan_projects(second) == 0
        assert not (_db.get_project(pid) or {}).get("org_id")

    def test_no_org_is_a_no_op(self):
        assert _db.adopt_orphan_projects("") == 0
