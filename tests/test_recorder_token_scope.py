"""Four requests, no credentials, a test case in someone else's project.

``/api/recorder-session/start`` was in ``route_policy.OPEN`` with the reason
"extension token auth". It is the one route on the recorder surface with no
token to check, because it is the route that *mints* the token — and it took
``project_id`` from the request body. With ``Access-Control-Allow-Origin: *``
on top, the chain was:

    POST /api/recorder-session/start  {"project_id": <pid>}   → 200 + token
    POST /api/recorder-session/finish {"token": …, "steps": …} → 200 + draft
    GET  /test-cases/review-session/<draft token>              → 200
    POST /test-cases/review-session/<draft token>              → 200, written

from a caller with no session, no cookie and no membership, knowing only a
project id. Measured on this suite before the fix, with ``AUTH_ENABLED`` and
``ORG_MODE`` on — the mode the product ships in.

Three stated guards were false at once, which is why it survived review:

* ``route_policy.OPEN``'s reason, above;
* ``_recorder_cors_headers``' docstring — "the endpoints carry their own
  auth (the per-session token from /start)". True of ``/finish``, and
  ``/start`` is the exception that issues what the sentence relies on;
* the review-save route's ``if active_pid and active_pid != draft[...]``,
  which reads as an ownership check and is a *mismatch* check: ``active_pid``
  is empty for a caller with no project, so it never fires for one. That
  route stays open on purpose — the token is the credential, for a browser
  that may never sign in — so ownership belongs where the token is minted,
  which is what the fix does.

The extension is not the loser here, and that is the evidence the fix rests
on: ``extension/popup.js`` explains that Start deliberately routes *through
the page*, because the endpoint resolves the project from a session cookie a
cross-site fetch could not carry. The only caller was always a signed-in,
same-origin page — and it posts an empty body, never a ``project_id``.
"""
from __future__ import annotations

import secrets

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import permissions as _perm
from engine import session_timeout as _timeout

PLANTED = "PLANTED-BY-A-STRANGER"
JSON_HEADERS = {"Accept": "application/json"}

STEPS = [
    {"action": "goto", "target": "https://sut.test/", "value": "",
     "selector_kind": "url"},
    {"action": "click", "target": "#submit", "value": "",
     "selector_kind": "css"},
]


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    monkeypatch.setenv("RECORDER_ENABLED", "1")
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    _db.init_db()


@pytest.fixture
def two_teams():
    pwd = _auth.hash_password("a perfectly good passphrase")
    mine = _db.create_organization(f"Mine {secrets.token_hex(4)}")
    theirs = _db.create_organization(f"Theirs {secrets.token_hex(4)}")
    alice = _db.create_user(f"a-{secrets.token_hex(5)}@example.com",
                            password_hash=pwd)
    _db.add_org_member(mine, alice, "user")
    return {
        "alice": alice, "mine": mine, "theirs": theirs,
        "my_pid": _db.upsert_project(name=f"M-{secrets.token_hex(4)}",
                                     org_id=mine),
        "their_pid": _db.upsert_project(name=f"T-{secrets.token_hex(4)}",
                                        org_id=theirs),
    }


def _member(app, two_teams, *, project=None):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess[_perm.SESSION_USER_KEY] = two_teams["alice"]
        sess[_perm.SESSION_ORG_KEY] = two_teams["mine"]
        if project:
            sess["project_id"] = project
        _timeout.stamp(sess)
    return c


def _walk_the_chain(client, project_id=None):
    """start → finish → save, returning the last response of the chain.

    Stops and returns as soon as a step refuses, so a test can assert on
    *which* step refused rather than on a cascade.
    """
    body = {} if project_id is None else {"project_id": project_id}
    started = client.post("/api/recorder-session/start", json=body,
                          headers=JSON_HEADERS)
    if started.status_code != 200:
        return "start", started
    token = started.get_json()["token"]

    finished = client.post("/api/recorder-session/finish",
                           json={"token": token, "steps": STEPS},
                           headers=JSON_HEADERS)
    if finished.status_code != 200:
        return "finish", finished
    draft_token = finished.get_json()["review_url"].rsplit("/", 1)[-1]

    saved = client.post(f"/test-cases/review-session/{draft_token}",
                        json={"selected": [{"idx": 0, "suite": "Smoke",
                                            "summary_override": PLANTED}]},
                        headers=JSON_HEADERS)
    return "save", saved


class TestAnAnonymousCaller:

    def test_cannot_mint_a_token_at_all(self, app, two_teams):
        step, resp = _walk_the_chain(app.test_client(),
                                     two_teams["their_pid"])
        assert step == "start"
        assert resp.status_code == 401, resp.get_data(as_text=True)

    def test_nothing_is_written_to_the_project(self, app, two_teams):
        """The assertion that matters: the status code says the request was
        refused, the row count says nothing was written anyway."""
        _walk_the_chain(app.test_client(), two_teams["their_pid"])
        assert _db.load_test_cases(two_teams["their_pid"]) == []
        assert _db.list_pending_session_drafts(two_teams["their_pid"]) == []

    def test_it_is_refused_without_naming_a_project_too(self, app, two_teams):
        """The body's ``project_id`` is not what made it reachable — the
        missing gate was. So the empty-body call has to be refused as well,
        or the fix is only half of one."""
        step, resp = _walk_the_chain(app.test_client())
        assert step == "start"
        assert resp.status_code == 401

    def test_the_answer_is_json_rather_than_a_redirect(self, app, two_teams):
        """A 302 to the sign-in page would be worse than a 401 here, and not
        for tidiness: ``fetch`` follows the redirect, ``resp.ok`` becomes
        true, and the page reads an undefined token off an HTML body. On the
        free plan a wiped session store makes that the ordinary case for a
        tab left open, which is why the page now sends ``Accept``."""
        resp = app.test_client().post("/api/recorder-session/start", json={},
                                      headers=JSON_HEADERS)
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "auth_required"

    def test_the_page_asks_for_json(self):
        """Asserted on the template, because the header is what turns the
        redirect into the 401 above. A future edit that drops it would leave
        every test here passing."""
        import pathlib
        body = pathlib.Path("templates/test_cases.html").read_text(
            encoding="utf-8")
        # Anchored on the fetch, not on the path: the path also appears in
        # four comments above it, and splitting on that matched prose.
        marker = "fetch('/api/recorder-session/start'"
        assert marker in body, "the page no longer calls the endpoint"
        call = body.split(marker, 1)[1][:900]
        assert "'Accept': 'application/json'" in call, call


class TestASignedInMemberOfAnotherTeam:
    """The gate is a role gate, so it lets any member through. The
    caller-named project is what has to be checked after it."""

    def test_cannot_name_another_teams_project(self, app, two_teams):
        c = _member(app, two_teams, project=two_teams["my_pid"])
        step, resp = _walk_the_chain(c, two_teams["their_pid"])
        assert step == "start"
        assert resp.status_code == 404, resp.get_data(as_text=True)
        assert _db.load_test_cases(two_teams["their_pid"]) == []

    def test_a_project_that_does_not_exist_is_the_same_answer(self, app,
                                                             two_teams):
        """Same 404 either way: telling the caller that a project exists but
        is not theirs confirms an id they should not be able to test."""
        c = _member(app, two_teams, project=two_teams["my_pid"])
        step, resp = _walk_the_chain(c, "f" * 32)
        assert step == "start"
        assert resp.status_code == 404


class TestTheLegitimateFlow:
    """The control. Every test above would pass on a route that refuses
    everybody, and the recorder is the feature this endpoint exists for."""

    def test_a_member_records_into_their_own_project(self, app, two_teams):
        c = _member(app, two_teams, project=two_teams["my_pid"])
        step, resp = _walk_the_chain(c)
        assert (step, resp.status_code) == ("save", 200), \
            resp.get_data(as_text=True)
        stored = _db.load_test_cases(two_teams["my_pid"])
        assert [t.get("summary") for t in stored] == [PLANTED]

    def test_naming_their_own_project_explicitly_also_works(self, app,
                                                            two_teams):
        """The documented parameter still does what it documents."""
        c = _member(app, two_teams)
        step, resp = _walk_the_chain(c, two_teams["my_pid"])
        assert (step, resp.status_code) == ("save", 200), \
            resp.get_data(as_text=True)
        assert len(_db.load_test_cases(two_teams["my_pid"])) == 1

    def test_finish_stays_open_for_the_extension(self, app, two_teams):
        """``/finish`` is called by the extension's content script from the
        site under test, with no cookie. It must keep working without a
        session — only ``/start`` moved."""
        c = _member(app, two_teams, project=two_teams["my_pid"])
        started = c.post("/api/recorder-session/start", json={},
                         headers=JSON_HEADERS)
        token = started.get_json()["token"]
        anon = app.test_client()          # the extension, no session
        finished = anon.post("/api/recorder-session/finish",
                             json={"token": token, "steps": STEPS},
                             headers=JSON_HEADERS)
        assert finished.status_code == 200, finished.get_data(as_text=True)
        assert finished.headers.get("Access-Control-Allow-Origin") == "*"


class TestThePolicyTableAgrees:
    """The table is where somebody looks to find out what a route needs, so
    the entry and the decorator must not tell two stories."""

    def test_the_route_is_no_longer_open(self):
        from engine import route_policy
        assert "api_recorder_session_start" not in route_policy.OPEN
        assert "api_recorder_session_start" not in route_policy.MACHINE

    def test_it_is_self_enforcing_instead(self, app):
        view = app.view_functions["api_recorder_session_start"]
        assert getattr(view, "_required_role", None) == "user"

    def test_the_table_is_still_internally_consistent(self):
        """``MACHINE`` is derived from ``OPEN`` and validated at boot —
        removing an entry from one and not the other refuses to start."""
        from engine import route_policy
        assert route_policy.validate() == []

    def test_the_route_is_still_classified(self, app):
        """Fail-closed means an unclassified endpoint is refused outright.
        Self-enforcing counts, and this asserts it counts *here*."""
        from engine import route_policy
        assert "api_recorder_session_start" not in \
            route_policy.unclassified(app)
