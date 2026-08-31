"""Two ways to read another organisation's automation runs.

Both found by walking the Automation module and asking the question that
gave the pin defect: *what exactly is being compared?*

1. ``/automation/runs/<int:run_id>`` compared nothing. It took a sequential
   integer, loaded the run and rendered it — label, case names, failure
   messages — for any signed-in member of any team. The listing it is
   reached from is project-scoped, so nothing legitimate ever needed the
   wider answer; the route simply never asked.

2. ``/automation`` with **no** active project rendered every run on the
   instance. ``list_automation_runs(None)`` adds no ``WHERE`` clause, an
   unresolved active project is the empty string, and the empty string is
   falsy — so the wide answer arrived through a call that reads like the
   narrow one. ``routes/dashboard.py`` had already grown a hand-written
   guard against the same edge, which is the clue this file was written
   from: one footgun with one hand-guard has a second call site somewhere.

Scoped by organisation membership only — ``belongs_to_another_org`` — which
is why every test here runs with ``ORG_MODE`` on. With it off the predicate
is a no-op, and that is deliberate: it is what makes the check safe to add
to a route production is already serving.
"""
import secrets

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import permissions as _perm
from engine import session_timeout as _timeout

LABEL = "SECRET-LABEL-XYZ"
CASE = "SECRET-CASE-NAME-XYZ"
MESSAGE = "SECRET-FAILURE-TEXT-XYZ"


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    _db.init_db()


def _summary():
    return {
        "total": 2, "passed": 1, "failed": 1, "skipped": 0, "flaky": 0,
        "duration_ms": 12,
        "cases": [{"name": CASE, "status": "failed", "message": MESSAGE}],
    }


@pytest.fixture
def two_teams():
    """Alice in one team; a run belonging to a project in the other."""
    pwd = _auth.hash_password("a perfectly good passphrase")
    mine = _db.create_organization(f"Mine {secrets.token_hex(4)}")
    theirs = _db.create_organization(f"Theirs {secrets.token_hex(4)}")
    alice = _db.create_user(f"a-{secrets.token_hex(5)}@example.com",
                            password_hash=pwd)
    _db.add_org_member(mine, alice, "user")
    my_pid = _db.upsert_project(name=f"M-{secrets.token_hex(4)}", org_id=mine)
    their_pid = _db.upsert_project(name=f"T-{secrets.token_hex(4)}",
                                   org_id=theirs)
    their_run = _db.save_automation_run(their_pid, _summary(), origin="ci",
                                        label=LABEL)
    return {"alice": alice, "mine": mine, "theirs": theirs,
            "my_pid": my_pid, "their_pid": their_pid,
            "their_run": their_run}


def _client(app, two_teams, *, project=None):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess[_perm.SESSION_USER_KEY] = two_teams["alice"]
        sess[_perm.SESSION_ORG_KEY] = two_teams["mine"]
        if project:
            sess["project_id"] = project
        else:
            sess.pop("project_id", None)
        _timeout.stamp(sess)
    return c


def _leaks(body: str) -> bool:
    return any(token in body for token in (LABEL, CASE, MESSAGE))


class TestTheRunDetail:

    def test_another_orgs_run_is_not_readable(self, app, two_teams):
        c = _client(app, two_teams, project=two_teams["my_pid"])
        r = c.get(f"/automation/runs/{two_teams['their_run']}")
        assert r.status_code == 404
        assert not _leaks(r.get_data(as_text=True))

    def test_the_control_a_members_own_run_is(self, app, two_teams):
        """Without this the test above passes on a route that 404s always."""
        run_id = _db.save_automation_run(two_teams["my_pid"], _summary(),
                                         origin="ci", label=LABEL)
        c = _client(app, two_teams, project=two_teams["my_pid"])
        r = c.get(f"/automation/runs/{run_id}")
        assert r.status_code == 200
        assert LABEL in r.get_data(as_text=True)

    def test_it_does_not_depend_on_which_project_is_active(self, app,
                                                          two_teams):
        """The run names its own project. Reading the *active* one here would
        be the same mistake in a new place: it would let a member of the
        owning team be refused their own run, and it would let anybody who
        pinned the right project read a run belonging to another."""
        run_id = _db.save_automation_run(two_teams["my_pid"], _summary(),
                                         origin="ci", label=LABEL)
        c = _client(app, two_teams)          # no active project at all
        assert c.get(f"/automation/runs/{run_id}").status_code == 200

    def test_a_run_with_no_project_is_still_readable(self, app, two_teams):
        """Nothing owns it, and no listing shows it — refusing it would take
        away the only way to reach a run somebody just posted."""
        run_id = _db.save_automation_run(None, _summary(), origin="local",
                                         label=LABEL)
        c = _client(app, two_teams, project=two_teams["my_pid"])
        assert c.get(f"/automation/runs/{run_id}").status_code == 200

    def test_a_run_that_does_not_exist_is_a_404(self, app, two_teams):
        c = _client(app, two_teams, project=two_teams["my_pid"])
        assert c.get("/automation/runs/999999").status_code == 404


class TestTheAutomationPage:

    def test_with_no_active_project_it_lists_nothing(self, app, two_teams):
        c = _client(app, two_teams)
        body = c.get("/automation").get_data(as_text=True)
        assert not _leaks(body)

    def test_with_a_project_it_lists_that_projects_runs(self, app, two_teams):
        _db.save_automation_run(two_teams["my_pid"], _summary(), origin="ci",
                                label=LABEL)
        c = _client(app, two_teams, project=two_teams["my_pid"])
        assert LABEL in c.get("/automation").get_data(as_text=True)

    def test_with_a_project_it_does_not_list_another_teams(self, app,
                                                          two_teams):
        c = _client(app, two_teams, project=two_teams["my_pid"])
        assert not _leaks(c.get("/automation").get_data(as_text=True))


class TestTheHelperItself:
    """The trap in the shape of a default, stated where it lives."""

    def test_none_still_means_every_run_on_the_instance(self, two_teams):
        """Not changed — two callers guard it now, and this records what
        they are guarding against, so a third caller's author can see it."""
        wide = _db.list_automation_runs(None)
        assert any(row["id"] == two_teams["their_run"] for row in wide)

    def test_the_empty_string_is_the_same_as_none(self, two_teams):
        """The actual mechanism: an unresolved active project is ``""``, and
        ``if project_id:`` cannot tell that from "give me everything"."""
        assert (len(_db.list_automation_runs("")) ==
                len(_db.list_automation_runs(None)))
