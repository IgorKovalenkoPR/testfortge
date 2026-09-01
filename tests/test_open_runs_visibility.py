""""New session" took the only link to an open walk off the page.

The "Unfinished manual runs" card exists for one reason, and its own comment
says so:

    A walk over sixty checks spans laptop sleeps and hand-offs, and its
    position was always derivable from the database — but with nothing
    listing the open runs, getting back to one meant finding the URL in
    browser history.

It was rendered inside the ``{% else %}`` of
``{% if not has_tc_data and not has_cl_data %}`` — the has-a-pack branch. So
the card appeared only while the project still had a pack, and "New session"
clears the pack.

Walked on the auth preview, measured at each step:

    pack present          te-open-runs present, Resume link to run 1
    POST /new-session     card gone, page says "Nothing to run yet"
    GET  .../1/resume     200, "0 / 1", verdict buttons live

The walk was never closed. It was open, resumable, and unreachable except
from browser history — the exact state the card was added to end, now
reached by pressing a button instead of by not having the feature.

The fix is placement, not logic: the card moved above the branch, so it
renders in both. The stated intent survives it — "above the run
configuration on purpose", because offering to start a second walk first is
how a project ends up with six of them.
"""
from __future__ import annotations

import pytest

from engine import db as _db
from engine import manual_run as mr
from engine.testcase_generator import TestCase
from routes._shared import SERVER_START_TIME, tc_to_dict


def _case(**kwargs):
    base = dict(id="TC-001", section="Checkout", section_num=1,
                summary="Verify the discount applies", preconditions="",
                test_steps="1. Add an item", test_data="",
                expected_result="Total drops", issues="", comment="",
                user_story_id="", category="Positive", priority="High",
                status="Unchecked")
    base.update(kwargs)
    return TestCase(**base)


@pytest.fixture(autouse=True)
def _ready():
    _db.init_db()


def _own(client, make_project, name):
    """A project this browser owns, the way the product creates one.

    ``upsert_project`` without an ``owner_sid`` builds a project nothing
    links to the caller, and ``/new-session`` — which drops
    ``project_id`` — then has nothing to recover. The browser never
    reaches that state: every creation path stamps the session id, which
    is why the page still knew its project after a reset. A test that
    skipped the stamp would measure a different bug.

    The name is per-test because ``upsert_project`` is create-or-return
    by ``(owner_sid, slug)``: one name shared across this file would hand
    every test the same project, and the runs left open by earlier ones
    would keep the card on screen for the wrong reason.
    """
    client.get("/")
    with client.session_transaction() as sess:
        from routes._shared import get_session_id
        sid = get_session_id(sess)
        sess["_session_active_since"] = SERVER_START_TIME
    return make_project(name, owner_sid=sid)


def _start_walk(client, pid):
    with client.session_transaction() as sess:
        sess["project_id"] = pid
    return _db.start_execution_run(pid, {
        "mode": "manual",
        "manual_queue": mr.queue_to_payload(mr.build_queue([_case()], [], [])),
        "environment": "", "tester": "walker"})


@pytest.fixture
def walk(request, client, make_project):
    pid = _own(client, make_project, f"Open walk {request.node.name}")
    _db.save_test_cases(pid, [tc_to_dict(_case())])
    run_id = _start_walk(client, pid)
    return {"client": client, "pid": pid, "run_id": run_id}


def _page(client):
    response = client.get("/test-execution")
    assert response.status_code == 200
    return response.get_data(as_text=True)


class TestTheCardSurvivesANewSession:

    def test_it_is_there_to_begin_with(self, walk):
        """The state the card was always rendered in. If this ever fails
        the test below is measuring nothing."""
        body = _page(walk["client"])
        assert "te-open-runs" in body
        assert f"/manual/{walk['run_id']}/resume" in body

    def test_it_is_still_there_after_new_session(self, walk):
        walk["client"].post("/new-session")
        body = _page(walk["client"])
        assert "te-open-runs" in body, (
            'pressing "New session" hid the only link to an open walk')
        assert f"/manual/{walk['run_id']}/resume" in body

    def test_the_page_agrees_it_has_no_pack(self, walk):
        """The control for the test above: if "New session" stopped
        clearing the pack, the card would be visible for the old reason
        and the fix would be untested."""
        walk["client"].post("/new-session")
        body = _page(walk["client"])
        assert "te-empty-state" in body

    def test_the_walk_really_is_still_open(self, walk):
        """The premise. Hiding the card would be correct if "New session"
        closed the run — it does not, measured through the route rather
        than the table."""
        walk["client"].post("/new-session")
        resumed = walk["client"].get(
            f"/test-execution/manual/{walk['run_id']}/resume",
            follow_redirects=True)
        assert resumed.status_code == 200
        assert 'name="verdict"' in resumed.get_data(as_text=True)


class TestItStillBehavesEverywhereElse:

    def test_no_open_runs_means_no_card(self, request, client,
                                        make_project):
        """A fix that rendered the card unconditionally would pass every
        test above and put an empty table on every project."""
        pid = _own(client, make_project, f"No walks {request.node.name}")
        _db.save_test_cases(pid, [tc_to_dict(_case())])
        with client.session_transaction() as sess:
            sess["project_id"] = pid
        assert "te-open-runs" not in _page(client)

    def test_a_closed_walk_is_not_listed(self, walk):
        _db.finish_execution_run(walk["run_id"])
        assert "te-open-runs" not in _page(walk["client"])

    def test_another_project_s_walk_is_not_listed(self, request, client,
                                                  make_project, walk):
        """Project isolation, the rule this series keeps returning to: the
        card reads ``list_open_runs(active_project)``, and moving it out of
        one Jinja branch must not have widened what it lists."""
        other = _own(client, make_project, f"Other {request.node.name}")
        # With a pack of its own, because /test-execution re-pins an empty
        # active project to whichever of this owner's projects has content
        # (``_maybe_restore_pack_from_db``). An empty project here would
        # land the request back on the walk's project and the assertion
        # below would fail for a reason that is not this test's subject.
        _db.save_test_cases(other, [tc_to_dict(_case(id="TC-900"))])
        with client.session_transaction() as sess:
            sess["project_id"] = other
        body = _page(client)
        assert f"/manual/{walk['run_id']}/resume" not in body
        # No card at all: this project has a pack and no open walks, which
        # is the ordinary state and must stay quiet.
        assert "te-open-runs" not in body


class TestThePlacementTheCommentPromises:

    def test_the_card_comes_before_the_run_configuration(self, walk):
        """"This is above the run configuration on purpose: resuming is
        almost always the intent when one is open." Moving the block is
        the whole fix, so where it landed is worth pinning."""
        body = _page(walk["client"])
        assert body.index("te-open-runs") < body.index("exec-config-form")

    def test_the_card_comes_before_the_empty_state(self, walk):
        walk["client"].post("/new-session")
        body = _page(walk["client"])
        assert body.index("te-open-runs") < body.index("te-empty-state")
