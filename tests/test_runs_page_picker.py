"""The Runs page said "in this project" and never showed which one.

``/test-execution/runs`` was the only module page without the project
picker. Its copy refers to the project three times —

    Every run in this project.
    Runs assigned to you.
    Nothing is assigned to you in this project yet.

— and its route resolves the active project, redirecting with "Select or
create a project first." when there is none. So the page already depended
on a fact it never displayed, the run ids on it are globally sequential
(``#1``, ``#2``, ``#3`` say nothing about whose they are), and switching
project meant leaving the page.

Measured while adding the picker: the partial's ``next`` is
``request.path``, so a switch came back to ``/test-execution/runs`` without
``?scope=all`` — an operator who had deliberately chosen "All runs" got
"Nothing is assigned to you" instead, which reads as their runs having
gone. The partial now takes an optional ``picker_next``, defaulting to
``request.path``, and this page passes ``request.full_path``.

``path`` stays the default on purpose. Dropping the query string is usually
right: /bug-reports' ``?run=`` names a run belonging to the project being
left. It is wrong only where the parameter is about the reader rather than
the project, which is this page.
"""
from __future__ import annotations

import pathlib
import re
import secrets

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import manual_run as mr
from engine import permissions as _perm
from engine import session_timeout as _timeout
from engine.testcase_generator import TestCase
from routes._shared import tc_to_dict

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"


def _case(**kwargs):
    base = dict(id="TC-001", section="S", section_num=1, summary="A case",
                preconditions="", test_steps="1. Do it", test_data="",
                expected_result="It works", issues="", comment="",
                user_story_id="", category="Positive", priority="High",
                status="Unchecked")
    base.update(kwargs)
    return TestCase(**base)


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    _db.init_db()


@pytest.fixture
def two_projects(app):
    """An admin with two projects, one of them holding a run."""
    org = _db.create_organization(f"Org {secrets.token_hex(3)}")
    uid = _db.create_user(
        f"u-{secrets.token_hex(4)}@example.test",
        password_hash=_auth.hash_password("a perfectly good passphrase"))
    _db.add_org_member(org, uid, "admin")
    busy = _db.upsert_project(name=f"Busy {secrets.token_hex(3)}",
                              org_id=org)
    quiet = _db.upsert_project(name=f"Quiet {secrets.token_hex(3)}",
                               org_id=org)
    _db.save_test_cases(busy, [tc_to_dict(_case())])
    run_id = _db.start_execution_run(busy, {
        "mode": "manual",
        "manual_queue": mr.queue_to_payload(
            mr.build_queue([_case()], [], [])),
        "environment": "", "tester": "walker"})
    client = app.test_client()
    with client.session_transaction() as sess:
        sess[_perm.SESSION_USER_KEY] = uid
        sess[_perm.SESSION_ORG_KEY] = org
        sess["project_id"] = busy
        _timeout.stamp(sess)
    return {"client": client, "busy": busy, "quiet": quiet,
            "run_id": run_id, "org": org}


def _page(client, query="?scope=all"):
    response = client.get("/test-execution/runs" + query)
    assert response.status_code == 200, response.status_code
    return response.get_data(as_text=True)


class TestThePageNamesItsProject:

    def test_the_picker_is_there(self, two_projects):
        assert 'id="pp-select"' in _page(two_projects["client"])

    def test_both_projects_are_offered(self, two_projects):
        body = _page(two_projects["client"])
        for pid in (two_projects["busy"], two_projects["quiet"]):
            assert pid in body, pid

    def test_the_runs_are_still_listed(self, two_projects):
        """A fix that broke the page would satisfy the tests above."""
        assert f"#{two_projects['run_id']}" in _page(two_projects["client"])

    def test_the_list_is_still_scoped_to_the_active_project(self,
                                                            two_projects):
        """Adding a switcher must not have widened what it lists."""
        client = two_projects["client"]
        with client.session_transaction() as sess:
            sess["project_id"] = two_projects["quiet"]
        body = _page(client)
        assert f"#{two_projects['run_id']}" not in body
        assert "no runs yet" in body.lower(), body[:300]


class TestASwitchComesBackHereWithItsScope:

    def test_the_next_field_carries_the_query_string(self, two_projects):
        body = _page(two_projects["client"], "?scope=all")
        values = re.findall(r'name="next" value="([^"]*)"', body)
        assert values, "the picker rendered no next field"
        assert all("/test-execution/runs" in v for v in values), values
        assert any("scope=all" in v for v in values), values

    def test_switching_lands_back_on_all_runs(self, two_projects):
        client = two_projects["client"]
        response = client.post(
            f"/projects/db/select/{two_projects['quiet']}",
            data={"next": "/test-execution/runs?scope=all"},
            follow_redirects=False)
        assert response.status_code in (302, 303), response.status_code
        assert response.headers["Location"].endswith(
            "/test-execution/runs?scope=all"), response.headers["Location"]

    def test_a_hostile_next_is_still_refused(self, two_projects):
        """The field carries more than it used to, so the guard that
        validates it is worth one assertion here rather than trust."""
        client = two_projects["client"]
        for hostile in ("//evil.example/steal", "https://evil.example/x",
                        "javascript:alert(1)"):
            response = client.post(
                f"/projects/db/select/{two_projects['quiet']}",
                data={"next": hostile}, follow_redirects=False)
            location = response.headers.get("Location", "")
            assert "evil.example" not in location, (hostile, location)
            assert "javascript:" not in location, (hostile, location)


class TestTheDefaultIsUnchangedForEveryOtherPage:

    def test_the_partial_still_defaults_to_path(self):
        """``picker_next`` is an opt-in. A partial that switched every
        page to ``full_path`` would start preserving /bug-reports' ``?run=``
        across a project switch, which names a run in the project being
        left."""
        source = (TEMPLATES / "_project_picker.html").read_text(
            encoding="utf-8")
        assert "picker_next|default(request.path, true)" in source

    def test_only_the_runs_page_opts_in(self):
        """The partial is excluded because the string appears in its own
        usage note, which is where the next page that needs it will read
        about it."""
        opted = sorted(
            path.name for path in TEMPLATES.rglob("*.html")
            if path.name != "_project_picker.html"
            and "{% set picker_next" in path.read_text(encoding="utf-8"))
        assert opted == ["test_execution_runs.html"], opted

    def test_another_page_still_returns_without_its_query(self, app,
                                                          two_projects):
        body = two_projects["client"].get(
            "/bug-reports?source=walkthrough").get_data(as_text=True)
        values = re.findall(r'name="next" value="([^"]*)"', body)
        assert values, "the picker rendered no next field on /bug-reports"
        assert all("source=" not in v for v in values), values
