"""The role matrix, driven against the real URL map — E2.3.

``tests/test_permissions.py`` proves the resolver is right and that every
endpoint is classified. This file proves the classification is *enforced*
on the actual application: every gated endpoint is walked as an anonymous
caller, as a plain user and as an admin, and each is checked against what
the table promises.

Generated from ``engine.route_policy.POLICY`` rather than hand-listed, so
a route added later is covered the moment it is classified — a hand-kept
copy of the same list would drift, and a drifted security test is worse
than none because it reads as coverage.
"""

import secrets

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import permissions as _perm
from engine import route_policy


@pytest.fixture(autouse=True)
def _db_ready():
    _db.init_db()


@pytest.fixture(autouse=True)
def _full_auth(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")


def _email() -> str:
    return f"rp-{secrets.token_hex(6)}@example.com"


@pytest.fixture(scope="module")
def _people():
    """One org with an admin and a plain user, made once for the module.

    Argon2 is deliberately slow, so hashing a password per parametrised
    case would dominate the run time of this file — and there are roughly
    two hundred cases.
    """
    _db.init_db()
    org = _db.create_organization(f"Matrix {secrets.token_hex(4)}")
    pwd = _auth.hash_password("a perfectly good passphrase")
    admin = _db.create_user(_email(), password_hash=pwd)
    user = _db.create_user(_email(), password_hash=pwd)
    _db.add_org_member(org, admin, "admin")
    _db.add_org_member(org, user, "user")
    return {"org": org, "admin": admin, "user": user}


def _sample_url(app, endpoint: str) -> str | None:
    """A concrete URL for *endpoint*, filling any path parameters.

    Values are chosen to be well-formed but non-existent: the point is to
    reach the access check, and a handler that then 404s has already told
    us the gate let us through. ``None`` when the rule needs a converter
    we do not know how to fill, so those endpoints are reported rather
    than silently skipped.
    """
    fillers = {
        "int": 1,
        "path": "nope/none.txt",
        "string": "0" * 32,
        "default": "0" * 32,
    }
    for rule in app.url_map.iter_rules():
        if rule.endpoint != endpoint:
            continue
        values = {}
        for name, converter in rule._converters.items():
            kind = type(converter).__name__.replace("Converter", "").lower()
            values[name] = fillers.get(kind, fillers["default"])
        try:
            return rule.build(values, append_unknown=False)[1]
        except Exception:
            return None
    return None


def _gated_endpoints():
    """(endpoint, minimum_role) for everything the table gates."""
    return sorted(route_policy.POLICY.items())


GATED = _gated_endpoints()
#: Which HTTP verb to probe with. GET where the rule allows it, else POST.
def _verb(app, endpoint: str) -> str:
    for rule in app.url_map.iter_rules():
        if rule.endpoint == endpoint:
            return "GET" if "GET" in rule.methods else "POST"
    return "GET"


def _probe(client, app, endpoint: str, uid: str | None = None,
           org: str | None = None):
    url = _sample_url(app, endpoint)
    assert url, f"could not build a URL for {endpoint}"
    with client.session_transaction() as sess:
        sess.clear()
        if uid:
            sess[_perm.SESSION_USER_KEY] = uid
        if org:
            sess[_perm.SESSION_ORG_KEY] = org
    verb = _verb(app, endpoint)
    # JSON, so the refusal is a status code rather than a redirect to the
    # sign-in page — the code is what we are asserting on.
    headers = {"Accept": "application/json"}
    return (client.get(url, headers=headers) if verb == "GET"
            else client.post(url, headers=headers))


def _refused_by_the_gate(resp) -> bool:
    """True when the *authorization layer* refused, not just anything 403.

    Necessary because a handler may 403 for its own unrelated reasons —
    ``test_cases_update_step_kind`` answers
    ``{"error": "recorder_disabled"}`` when RECORDER_ENABLED is off, and
    treating that as an access denial made the matrix assert the opposite
    of what it meant. The gate's refusals are the only ones carrying
    ``error: forbidden`` (see ``engine.permissions._deny_forbidden``).
    """
    if resp.status_code != 403:
        return False
    try:
        return (resp.get_json() or {}).get("error") == "forbidden"
    except Exception:
        return False


def _reached_the_handler(resp) -> bool:
    """True when the gate let the request through.

    Anything that is not a gate refusal counts, including a 400 or 404
    from the handler: having reached the handler at all is the evidence
    that authorization allowed it.
    """
    return not _refused_by_the_gate(resp) and resp.status_code != 401


class TestTheTableCoversTheApp:
    def test_there_is_something_to_test(self):
        # A parametrised suite that silently generated zero cases would be
        # the most convincing possible false green.
        assert len(GATED) > 50, f"only {len(GATED)} gated endpoints"

    def test_every_gated_endpoint_has_a_buildable_url(self, client):
        # Otherwise the matrix below would skip it without saying so.
        app = client.application
        unbuildable = [e for e, _ in GATED if not _sample_url(app, e)]
        assert not unbuildable, (
            f"cannot build probe URLs for {unbuildable} — extend the "
            f"converter fillers in _sample_url rather than leaving these "
            f"outside the matrix."
        )


@pytest.mark.parametrize("endpoint,minimum", GATED,
                         ids=[e for e, _ in GATED])
class TestMatrix:
    """Three callers against every gated endpoint."""

    def test_anonymous_is_refused(self, client, endpoint, minimum):
        resp = _probe(client, client.application, endpoint)
        # 401, never 200. A redirect would also be acceptable in a browser,
        # but we asked for JSON.
        assert resp.status_code == 401, (
            f"{endpoint} answered {resp.status_code} to an anonymous "
            f"caller; it must require sign-in"
        )

    def test_a_plain_user_is_allowed_exactly_where_the_table_says(
            self, client, endpoint, minimum, _people):
        resp = _probe(client, client.application, endpoint,
                      uid=_people["user"], org=_people["org"])
        if minimum == "admin":
            assert _refused_by_the_gate(resp), (
                f"{endpoint} is admin-only but a plain user got "
                f"{resp.status_code}"
            )
        else:
            assert _reached_the_handler(resp), (
                f"{endpoint} needs only {minimum!r} but a plain user was "
                f"refused with {resp.status_code}"
            )

    def test_an_admin_is_never_forbidden(self, client, endpoint, minimum,
                                         _people):
        resp = _probe(client, client.application, endpoint,
                      uid=_people["admin"], org=_people["org"])
        assert _reached_the_handler(resp), (
            f"an admin was refused {endpoint} with {resp.status_code}"
        )

    def test_a_non_member_is_refused(self, client, endpoint, minimum,
                                     _people):
        # Signed in, but in somebody else's organisation. None means no
        # access, and must never resolve to a default role.
        outsider = _db.create_user(_email())
        resp = _probe(client, client.application, endpoint,
                      uid=outsider, org=_people["org"])
        if minimum == "login":
            # The shell is deliberately reachable — that is where the
            # "you are not on a team" message lives.
            assert _reached_the_handler(resp)
        else:
            assert _refused_by_the_gate(resp), (
                f"{endpoint} let a non-member through with "
                f"{resp.status_code}"
            )


class TestBugDeletionNeedsAdmin:
    """The one action-level check inside an otherwise user-level route."""

    def _project_with_bug(self, org: str) -> tuple[str, int]:
        pid = _db.upsert_project(name=f"P-{secrets.token_hex(4)}")
        _db.set_project_org(pid, org)
        bug_id = _db.save_bug(pid, {
            "id": "BUG-001", "title": "Something is wrong",
            "severity": "Major", "priority": "High", "status": "Open",
        })
        return pid, bug_id

    def test_a_plain_user_cannot_bulk_delete(self, client, _people):
        pid, bug_id = self._project_with_bug(_people["org"])
        with client.session_transaction() as sess:
            sess.clear()
            sess[_perm.SESSION_USER_KEY] = _people["user"]
            sess[_perm.SESSION_ORG_KEY] = _people["org"]
            sess["project_id"] = pid
        client.post("/bugs/bulk",
                    data={"action": "delete", "bug_ids": str(bug_id)})
        # Still there: the evidence a tester gathered is not a user-level
        # thing to destroy.
        assert len(_db.list_bugs(pid)) == 1

    def test_a_plain_user_can_still_close(self, client, _people):
        pid, bug_id = self._project_with_bug(_people["org"])
        with client.session_transaction() as sess:
            sess.clear()
            sess[_perm.SESSION_USER_KEY] = _people["user"]
            sess[_perm.SESSION_ORG_KEY] = _people["org"]
            sess["project_id"] = pid
        client.post("/bugs/bulk",
                    data={"action": "close", "bug_ids": str(bug_id)})
        assert len(_db.list_bugs(pid)) == 1

    def test_an_admin_can_bulk_delete(self, client, _people):
        pid, bug_id = self._project_with_bug(_people["org"])
        with client.session_transaction() as sess:
            sess.clear()
            sess[_perm.SESSION_USER_KEY] = _people["admin"]
            sess[_perm.SESSION_ORG_KEY] = _people["org"]
            sess["project_id"] = pid
        client.post("/bugs/bulk",
                    data={"action": "delete", "bug_ids": str(bug_id)})
        assert _db.list_bugs(pid) == []


class TestProjectAccessAcrossOrgs:
    """``_require_project_owner`` after E2.3 — membership, not cookies."""

    def test_a_member_of_another_org_cannot_load_the_project(self, client,
                                                             _people):
        from routes.projects import _require_project_owner

        theirs = _db.create_organization(f"Theirs {secrets.token_hex(4)}")
        pid = _db.upsert_project(name=f"P-{secrets.token_hex(4)}")
        _db.set_project_org(pid, theirs)

        app = client.application
        with app.test_request_context("/"):
            from flask import session
            session[_perm.SESSION_USER_KEY] = _people["user"]
            with pytest.raises(Exception) as exc:
                _require_project_owner(pid)
            assert "403" in str(exc.value)

    def test_a_member_of_the_owning_org_may(self, client, _people):
        from routes.projects import _require_project_owner

        pid = _db.upsert_project(name=f"P-{secrets.token_hex(4)}")
        _db.set_project_org(pid, _people["org"])

        app = client.application
        with app.test_request_context("/"):
            from flask import session
            session[_perm.SESSION_USER_KEY] = _people["user"]
            assert _require_project_owner(pid)["id"] == pid

    def test_a_legacy_project_still_answers_to_its_session(self, client):
        """Every project that exists today is in this state.

        Removing the owner_sid branch would lock the current users out of
        their own work, so it stays until E1.6 has claimed them.
        """
        from routes._shared import get_session_id
        from routes.projects import _require_project_owner

        app = client.application
        with app.test_request_context("/"):
            sid = get_session_id()
            pid = _db.upsert_project(name=f"L-{secrets.token_hex(4)}",
                                     owner_sid=sid)
            assert _db.get_project(pid)["org_id"] is None
            assert _require_project_owner(pid)["id"] == pid

    def test_a_legacy_project_is_not_readable_by_another_session(self, client):
        from routes.projects import _require_project_owner

        pid = _db.upsert_project(name=f"L-{secrets.token_hex(4)}",
                                 owner_sid="someone-elses-session")
        app = client.application
        with app.test_request_context("/"):
            with pytest.raises(Exception) as exc:
                _require_project_owner(pid)
            assert "403" in str(exc.value)
