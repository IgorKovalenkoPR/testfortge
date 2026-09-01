"""``/metrics/history`` served any project's trend to anyone who named it.

The last route in the URL map taking a caller-supplied ``project_id`` from
the query string, and the only one left that did not check it:

    pid = (request.args.get("project_id")
           or session.get("project_id") or "").strip()
    if not pid:
        return jsonify({"snapshots": []})

Measured with two organisations: a signed-in member of one team requested
another team's project id and got 200 with its snapshots — pass rate, defect
density, test-case total, bug total and executions, one row per capture.
Volume and quality over time is most of what anybody would want out of
somebody else's QA instance, and it needed no membership, only an id.

The docstring's justification was real but scoped wrongly. "No friction" was
written for the *anonymous visitor lands on /test-metrics* path, where the
caller names nothing; it was being applied to a caller who names somebody
else's project. Those are different requests.

404 rather than 403, the answer its two siblings already give
(``/automation/runs/<id>``, ``/api/edit/*``): saying the project exists but
is not yours is the one thing this route should not say. The chart pays
nothing for the honest status — it already renders its empty state on any
non-200 (``r.ok ? r.json() : {snapshots: []}``).

Scoped by organisation membership only, so every test here runs with
``ORG_MODE`` on. With it off the predicate is a no-op, and that is
deliberate: it is what makes the check safe to add to a route production is
already serving.
"""
from __future__ import annotations

import json
import secrets

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import permissions as _perm
from engine import session_timeout as _timeout

PASS_RATE = 0.91
TC_TOTAL = 4242          # distinctive, so a leak is unmistakable in a body


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    _db.init_db()


@pytest.fixture
def two_teams():
    """Alice in one team; a project with a snapshot in the other."""
    pwd = _auth.hash_password("a perfectly good passphrase")
    mine = _db.create_organization(f"Mine {secrets.token_hex(4)}")
    theirs = _db.create_organization(f"Theirs {secrets.token_hex(4)}")
    alice = _db.create_user(f"a-{secrets.token_hex(5)}@example.com",
                            password_hash=pwd)
    _db.add_org_member(mine, alice, "user")
    my_pid = _db.upsert_project(name=f"M-{secrets.token_hex(4)}", org_id=mine)
    their_pid = _db.upsert_project(name=f"T-{secrets.token_hex(4)}",
                                   org_id=theirs)
    _db.save_metric_snapshot(their_pid, {
        "exec_pass_rate": PASS_RATE, "tc_total": TC_TOTAL,
        "bug_total": 8, "exec_total": 35})
    return {"alice": alice, "mine": mine, "my_pid": my_pid,
            "their_pid": their_pid}


def _client(app, teams, *, project=None):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess[_perm.SESSION_USER_KEY] = teams["alice"]
        sess[_perm.SESSION_ORG_KEY] = teams["mine"]
        if project:
            sess["project_id"] = project
        else:
            sess.pop("project_id", None)
        _timeout.stamp(sess)
    return c


def _snapshots(response):
    return json.loads(response.get_data(as_text=True))["snapshots"]


class TestAnotherTeamsTrendIsNotServed:

    def test_naming_their_project_is_a_404(self, app, two_teams):
        c = _client(app, two_teams, project=two_teams["my_pid"])
        r = c.get(f"/metrics/history?project_id={two_teams['their_pid']}")
        assert r.status_code == 404, r.status_code

    def test_no_numbers_come_back_with_it(self, app, two_teams):
        """The status is the contract; the body is the leak. Assert the
        payload too, or a 404 page that happened to embed the JSON would
        satisfy the test above."""
        c = _client(app, two_teams, project=two_teams["my_pid"])
        body = c.get(
            f"/metrics/history?project_id={two_teams['their_pid']}"
        ).get_data(as_text=True)
        for leaked in (str(TC_TOTAL), "defect_density", "exec_total"):
            assert leaked not in body, leaked

    def test_with_no_project_of_their_own_either(self, app, two_teams):
        """The pin is not what protects it. A caller with nothing active
        was the easiest way in — no state to arrange, one query string."""
        c = _client(app, two_teams)
        r = c.get(f"/metrics/history?project_id={two_teams['their_pid']}")
        assert r.status_code == 404

    def test_the_snapshot_really_is_there_to_be_leaked(self, two_teams):
        """The premise. If the fixture stopped saving anything, every test
        above would pass against an endpoint that fixed nothing."""
        rows = _db.list_metric_snapshots(two_teams["their_pid"])
        assert rows, "no snapshot to leak — the tests above prove nothing"


class TestTheChartStillWorks:

    def _mine_with_a_snapshot(self, teams, tc_total=7):
        _db.save_metric_snapshot(teams["my_pid"], {
            "exec_pass_rate": 0.5, "tc_total": tc_total,
            "bug_total": 0, "exec_total": 1})
        return teams["my_pid"]

    def test_the_callers_own_project_is_served(self, app, two_teams):
        pid = self._mine_with_a_snapshot(two_teams)
        c = _client(app, two_teams, project=pid)
        r = c.get("/metrics/history")
        assert r.status_code == 200
        assert _snapshots(r)[0]["tc_total"] == 7

    def test_naming_it_explicitly_works_too(self, app, two_teams):
        """The chart passes ``?project_id=`` from a data attribute, so the
        named path is the ordinary one and must not have been closed."""
        pid = self._mine_with_a_snapshot(two_teams, tc_total=11)
        c = _client(app, two_teams, project=pid)
        r = c.get(f"/metrics/history?project_id={pid}")
        assert r.status_code == 200
        assert _snapshots(r)[0]["tc_total"] == 11

    def test_naming_nothing_is_still_frictionless(self, app, two_teams):
        """The path the docstring's "no friction" was actually written
        for: nothing named, nothing active, an empty list and a 200."""
        c = _client(app, two_teams)
        r = c.get("/metrics/history")
        assert r.status_code == 200
        assert _snapshots(r) == []

    def test_an_id_that_belongs_to_nobody_is_not_a_404(self, app,
                                                       two_teams):
        """``belongs_to_another_org`` answers False for a project with no
        organisation, deliberately — this route must not become a way to
        ask whether an id exists."""
        c = _client(app, two_teams, project=two_teams["my_pid"])
        r = c.get("/metrics/history?project_id=" + "f" * 32)
        assert r.status_code == 200
        assert _snapshots(r) == []
