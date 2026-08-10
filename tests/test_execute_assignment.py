"""
Run assignment and the Runs page (E5.3).

The property worth defending here is that scope is an **access rule, not a
default**. A tester who passes ``?scope=all`` sees their own runs anyway —
a filter a URL parameter can widen is a filter, and the epic's acceptance
("a user sees what is assigned to them, an admin sees everything") needs a
rule.

The other half is honesty with authentication off, which is the default
deployment. There is no identity to assign to, so the page lists everything
and says why. An empty "assigned to me" with no explanation would read as
"my runs are gone", which is worse than the truth.
"""
from __future__ import annotations

import re

import pytest

from engine import db as _db
from engine.testcase_generator import ChecklistItem, TestCase
from routes._shared import SERVER_START_TIME, cl_to_dict, tc_to_dict


def _seed(pid):
    _db.save_test_cases(pid, [tc_to_dict(TestCase(
        id="TC_001", section="S", section_num=1, summary="Verify that it works",
        preconditions="", test_steps="1. do it", test_data="",
        expected_result="it works", priority="High", category="Positive"))])
    _db.save_checklist(pid, [cl_to_dict(ChecklistItem(
        id="HDR_001", section="Header", objective="Verify that the logo shows",
        item_num="1.1", priority="High", category="Positive"))])


def _activate(client, pid):
    with client.session_transaction() as sess:
        sess["_session_active_since"] = SERVER_START_TIME
        sess["project_id"] = pid
        sess["project_setup"] = {"project_name": "runs"}
        sess.pop("test_cases_data", None)
        sess.pop("checklist_data", None)


@pytest.fixture
def project(client, request):
    pid = _db.upsert_project(f"assign-{request.node.name}")
    _seed(pid)
    _activate(client, pid)
    return pid


def _rows(page: str) -> set[int]:
    """The run ids the page actually lists.

    Not ``f"#{run_id}" in page``, which is what these tests used to do and
    what parallel CI exposed: with a fresh database the ids are single
    digits, and ``"#3"`` matches ``&#39;`` inside Tedgie's greeting. The
    test passed for years only because the full suite had created enough
    runs to push the ids past two digits — a green that depended on
    execution order rather than on the rule under test.
    """
    return {int(n) for n in re.findall(r"<td>#(\d+)</td>", page)}


def _start(client, **extra) -> int:
    data = {"run_mode": "manual", "tester": "alice"}
    data.update(extra)
    resp = client.post("/test-execution/manual/start", data=data)
    return int(re.search(r"/manual/(\d+)", resp.headers["Location"]).group(1))


class TestTheRunsPage:
    def test_it_lists_the_projects_runs(self, client, project):
        run_id = _start(client)
        page = client.get("/test-execution/runs").get_data(as_text=True)
        assert run_id in _rows(page)

    def test_it_does_not_list_another_projects_runs(self, client, project,
                                                   request):
        other = _db.upsert_project(f"assign-other-{request.node.name}")
        _seed(other)
        _activate(client, other)
        other_run = _start(client)
        _activate(client, project)
        page = client.get("/test-execution/runs").get_data(as_text=True)
        assert other_run not in _rows(page)

    def test_it_says_why_everything_is_listed_when_auth_is_off(
            self, auth_off, client, project):
        _start(client)
        page = client.get("/test-execution/runs").get_data(as_text=True)
        assert "needs authentication" in page

    def test_an_open_run_offers_to_resume(self, client, project):
        run_id = _start(client)
        page = client.get("/test-execution/runs").get_data(as_text=True)
        assert f"/manual/{run_id}/resume" in page
        assert "Resume" in page

    def test_a_closed_run_offers_review_rather_than_resume(self, client,
                                                           project):
        run_id = _start(client)
        _db.finish_execution_run(run_id, status="completed")
        page = client.get("/test-execution/runs").get_data(as_text=True)
        assert "Review" in page

    def test_a_run_with_no_walk_is_not_offered_a_resume_link(
            self, auth_off, client, project):
        # Live and walkthrough runs share ExecutionRun and have no queue, so
        # a resume link would 404 — worse than no link.
        live_id = _db.start_execution_run(project, {"mode": "live"})
        page = client.get("/test-execution/runs").get_data(as_text=True)
        assert live_id in _rows(page)
        assert f"/manual/{live_id}/resume" not in page

    def test_no_project_redirects_rather_than_erroring(self, client,
                                                       forget_workspace):
        forget_workspace(client)
        resp = client.get("/test-execution/runs")
        assert resp.status_code == 302
        assert "/test-execution" in resp.headers["Location"]

    def test_the_empty_state_offers_a_way_to_start_one(self, auth_off,
                                                       client, project):
        page = client.get("/test-execution/runs").get_data(as_text=True)
        assert "no runs yet" in page
        assert "/test-execution" in page


class TestScopeIsAnAccessRuleWithAuthOn:
    """With authentication on, a non-admin is *restricted* to their own runs
    however the URL is spelled."""

    @pytest.fixture
    def two_testers(self, client, project, monkeypatch):
        run_mine = _start(client)
        run_theirs = _start(client)
        # Stamp the assignees directly: the point under test is the scoping,
        # and driving two real logins here would test the auth flow instead.
        for rid, uid in ((run_mine, "u-me"), (run_theirs, "u-them")):
            _db.assign_run(rid, uid)
        return {"mine": run_mine, "theirs": run_theirs}

    def _as(self, as_user, *, user="u-me", admin=False):
        """Become *user*. See the ``as_user`` fixture for why the gate is
        patched open as well as the view's own identity functions."""
        return as_user(user, admin=admin)

    def test_a_tester_sees_only_their_own(self, client, two_testers,
                                         as_user):
        self._as(as_user)
        page = client.get("/test-execution/runs").get_data(as_text=True)
        assert two_testers["mine"] in _rows(page)
        assert two_testers["theirs"] not in _rows(page)

    def test_a_tester_cannot_widen_the_scope_by_url(self, client, two_testers,
                                                    as_user):
        self._as(as_user)
        page = client.get(
            "/test-execution/runs?scope=all").get_data(as_text=True)
        assert two_testers["theirs"] not in _rows(page)

    def test_a_tester_is_not_offered_the_switch(self, client, two_testers,
                                               as_user):
        self._as(as_user)
        page = client.get("/test-execution/runs").get_data(as_text=True)
        assert "scope=all" not in page

    def test_an_admin_defaults_to_their_own(self, client, two_testers,
                                           as_user):
        # Defaults to "mine" so an admin's own queue is the landing view;
        # everything is one click away.
        self._as(as_user, admin=True)
        page = client.get("/test-execution/runs").get_data(as_text=True)
        assert two_testers["mine"] in _rows(page)
        assert two_testers["theirs"] not in _rows(page)

    def test_an_admin_can_see_everything(self, client, two_testers,
                                        as_user):
        self._as(as_user, admin=True)
        page = client.get(
            "/test-execution/runs?scope=all").get_data(as_text=True)
        assert two_testers["mine"] in _rows(page)
        assert two_testers["theirs"] in _rows(page)

    def test_the_empty_mine_state_says_it_is_about_assignment(
            self, client, two_testers, as_user):
        self._as(as_user, user="u-nobody")
        page = client.get("/test-execution/runs").get_data(as_text=True)
        assert "assigned to you" in page


class TestAssigningARun:
    def test_with_auth_off_no_assignee_is_recorded(self, auth_off, client,
                                                  project):
        # Asserted so the empty field is a decision rather than an accident:
        # there is no identity to attach when authentication is off.
        run = _db.get_execution_run(_start(client))
        assert run["env_payload"]["assignee_id"] == ""

    def test_a_run_belongs_to_whoever_started_it(self, client, project,
                                                as_user):
        as_user("u-me", name="Me")
        run = _db.get_execution_run(_start(client, tester=""))
        assert run["env_payload"]["assignee_id"] == "u-me"
        # The display name fills the free-text tester field when the form
        # left it empty, so the bug reports the run files have a reporter.
        assert run["env_payload"]["tester"] == "Me"

    def test_a_tester_cannot_assign_work_to_someone_else(self, client, project,
                                                         as_user):
        as_user("u-me")
        run = _db.get_execution_run(_start(client, assignee_id="u-them"))
        # Without this check "assign" would be a way to write into another
        # tester's queue.
        assert run["env_payload"]["assignee_id"] == "u-me"

    def test_an_admin_can_assign_work_to_someone_else(self, client, project,
                                                      as_user):
        as_user("u-admin", admin=True)
        run = _db.get_execution_run(_start(client, assignee_id="u-them"))
        assert run["env_payload"]["assignee_id"] == "u-them"

    def test_the_assignee_picker_is_absent_when_auth_is_off(
            self, auth_off, client, project):
        page = client.get("/test-execution").get_data(as_text=True)
        # A disabled select that explains nothing is worse than no select.
        assert 'name="assignee_id"' not in page


class TestOnlyTheAssigneeCanRecordVerdicts:
    @pytest.fixture
    def assigned(self, client, project, monkeypatch):
        # has_role is patched open on purpose: without it the route-policy
        # gate refuses these requests with a 403 of its own, and
        # `test_somebody_else_cannot` would pass for the wrong reason —
        # asserting the policy hook works rather than the assignee check.
        from engine import permissions as perm
        monkeypatch.setattr(perm, "auth_active", lambda: True)
        monkeypatch.setattr(perm, "current_user_id", lambda: "u-me")
        monkeypatch.setattr(perm, "current_user", lambda: {"id": "u-me"})
        monkeypatch.setattr(perm, "is_admin", lambda: False)
        monkeypatch.setattr(perm, "has_role", lambda minimum: True)
        return _start(client)

    def test_the_assignee_can(self, client, assigned):
        client.post(f"/test-execution/manual/{assigned}/verdict",
                    data={"external_id": "TC_001", "kind": "test_case",
                          "verdict": "Passed"})
        assert len(_db.list_case_results(assigned)) == 1

    def test_somebody_else_cannot(self, client, assigned, monkeypatch):
        from engine import permissions as perm
        monkeypatch.setattr(perm, "current_user_id", lambda: "u-them")
        resp = client.post(f"/test-execution/manual/{assigned}/verdict",
                           data={"external_id": "TC_001", "kind": "test_case",
                                 "verdict": "Failed"})
        # 403, not 404: the run is in a project this caller can see, so
        # pretending it does not exist would be a lie they can disprove.
        assert resp.status_code == 403
        assert _db.list_case_results(assigned) == []

    def test_an_admin_can_unblock_it(self, client, assigned, monkeypatch):
        # Somebody has to be able to close a walk whose owner went on leave.
        from engine import permissions as perm
        monkeypatch.setattr(perm, "current_user_id", lambda: "u-admin")
        monkeypatch.setattr(perm, "is_admin", lambda: True)
        client.post(f"/test-execution/manual/{assigned}/verdict",
                    data={"external_id": "TC_001", "kind": "test_case",
                          "verdict": "Blocked"})
        assert len(_db.list_case_results(assigned)) == 1
