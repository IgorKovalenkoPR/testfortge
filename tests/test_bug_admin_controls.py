"""Two destructive controls on /bug-reports were offered to people who
cannot use them, and one of them answered with a bare 403.

Measured with a ``user``-role member of an organisation:

    bulk toolbar → "Delete"        POST /bugs/bulk    302 + flash
                                   "Deleting bug reports is limited to
                                    admins. Close them instead…"
    "Reset Project bugs" → modal
      "2 bug(s) will be deleted"
      → "Yes, delete all bugs"     POST /bugs/reset   403, no flash

The second is the bad one: a red button, a confirmation dialog quoting a
count, and then an error page. The operator is off /bug-reports with
nothing explained and nothing deleted.

The rule this breaks is written down three times in this codebase, once in
this very template 160 lines below the toolbar:

    Closing is admin-only and the option is simply absent for a user —
    the server checks again, because a hidden option is UX, not a
    permission.                                 (templates/bug_reports.html)

    Hiding the form is politeness, not security — a plain user who posts
    here gets 403 either way — but offering an action the server will
    refuse is a worse first day than not offering it.
                                              (templates/_project_picker.html)

    Filtered by role as well as by transition, so the control cannot offer
    a move the server will refuse.                (app._template_bug_status_options)

Both server checks stay exactly as they are — they are the permission, and
the two layers are different (``routes/bugs.py`` refuses ``action=delete``
inline, ``engine/route_policy`` gates ``bugs_reset`` at "admin"). This
closes the UI half.

``is_admin`` is true whenever ``ORG_MODE`` is off, so the single-tenant UI
production runs today is unchanged — which is also what makes the fix safe
to ship.
"""
from __future__ import annotations

import re
import secrets

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import permissions as _perm
from engine import session_timeout as _timeout


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    _db.init_db()


def _member(role):
    org = _db.create_organization(f"Org {secrets.token_hex(3)}")
    uid = _db.create_user(
        f"u-{secrets.token_hex(4)}@example.test",
        password_hash=_auth.hash_password("a perfectly good passphrase"))
    _db.add_org_member(org, uid, role)
    pid = _db.upsert_project(name=f"P {secrets.token_hex(3)}", org_id=org)
    _db.save_bug(pid, {"id": "BUG-001", "title": "A defect",
                       "severity": "Minor", "priority": "High",
                       "status": "Open"})
    return {"org": org, "uid": uid, "pid": pid}


def _client(app, who):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess[_perm.SESSION_USER_KEY] = who["uid"]
        sess[_perm.SESSION_ORG_KEY] = who["org"]
        sess["project_id"] = who["pid"]
        _timeout.stamp(sess)
    return c


def _page(app, who):
    response = _client(app, who).get("/bug-reports")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _has_reset_form(body):
    return bool(re.search(r'<form[^>]*action="[^"]*bugs/reset', body))


class TestAUserIsNotOfferedThem:

    def test_the_bulk_delete_option_is_absent(self, app):
        assert 'value="delete"' not in _page(app, _member("user"))

    def test_the_reset_control_is_absent(self, app):
        body = _page(app, _member("user"))
        assert not _has_reset_form(body)
        # The *element*, not the bare string: the id also appears in this
        # page's CSS and in the script that wires the modal, and both stay
        # for everyone. The script opens with ``if (!modal) return;``, so
        # no console error follows the markup out.
        assert 'id="bug-reset-open"' not in body, "the button is still there"
        assert 'id="bug-reset-modal"' not in body, (
            "the modal DOM is still there — its own comment says it exists "
            "so the button always has something to open")

    def test_the_rest_of_the_toolbar_survives(self, app):
        """A fix that removed the toolbar would satisfy both tests above
        and take away everything a user is allowed to do."""
        body = _page(app, _member("user"))
        for kept in ("close", "status", "severity", "priority",
                     "fix_version", "assign"):
            assert f'value="{kept}"' in body, kept

    def test_the_bug_is_still_shown(self, app):
        assert "BUG-001" in _page(app, _member("user"))


class TestAnAdminStillGetsThem:

    def test_the_bulk_delete_option_is_there(self, app):
        assert 'value="delete"' in _page(app, _member("admin"))

    def test_the_reset_control_is_there(self, app):
        body = _page(app, _member("admin"))
        assert _has_reset_form(body)
        assert 'id="bug-reset-open"' in body


class TestTheServerIsStillTheBoundary:
    """Hiding a control is politeness. Both checks below are the
    permission, and they are in two different places — an inline role
    check for one action, the policy table for the whole route."""

    def test_a_user_posting_bulk_delete_is_refused(self, app):
        who = _member("user")
        client = _client(app, who)
        before = len(_db.list_bugs(who["pid"]))
        client.post("/bugs/bulk", data={"action": "delete",
                                        "bug_ids": ["1"]})
        assert len(_db.list_bugs(who["pid"])) == before

    def test_a_user_posting_reset_is_refused(self, app):
        who = _member("user")
        client = _client(app, who)
        response = client.post("/bugs/reset", data={"confirm": "yes"})
        assert response.status_code == 403
        assert len(_db.list_bugs(who["pid"])) == 1

    def test_an_admin_posting_reset_still_works(self, app):
        """The control for both: a route that refused everybody would pass
        the two tests above."""
        who = _member("admin")
        response = _client(app, who).post("/bugs/reset",
                                          data={"confirm": "yes"})
        assert response.status_code == 302
        assert _db.list_bugs(who["pid"]) == []
