"""Four sweepers, written and exported and called by nothing.

``engine.db`` has a ``purge_expired_*`` helper for session drafts,
browser-control rows, auth tokens and server sessions. Every one of them was
in ``__all__`` and none of them had a caller. Two of the docstrings say what
should have been calling them:

* ``purge_expired_session_drafts`` — "Sweeper for the snapshot worker (or a
  manual admin call)". The snapshot worker did not call it.
* ``purge_expired_auth_tokens`` — "Kept for a month after expiry … so the
  table stays bounded". Nothing enforced the month, so a reset token issued
  on the first day of the deployment is still there.

``engine.server_session`` had already met the class and said so — "nothing
in this codebase currently calls any of the ``purge_expired_*`` helpers, so
expired rows have simply been accumulating" — and solved it for its own
table with an opportunistic vacuum instead of for the four.

This is **not** an access-control question, and the tests say so on purpose:
every reader checks expiry for itself, and a draft somebody opens is deleted
lazily on that read. What accumulates is exactly what nobody comes back to,
which on a free-tier 256 MB Postgres holding recorded step payloads is the
database filling up — the failure mode
``docs``/``project_render_free_db_expiry`` already knows by name.
"""
from __future__ import annotations

import secrets
from datetime import timedelta

import pytest

from engine import db as _db
from engine import retention


def _past():
    return _db._utcnow() - timedelta(hours=48)


def _future():
    return _db._utcnow() + timedelta(hours=48)


@pytest.fixture(autouse=True)
def _ready():
    _db.init_db()


@pytest.fixture
def project():
    return _db.upsert_project(name=f"sweep-{secrets.token_hex(4)}")


def _draft(project_id, *, expires_at):
    token = secrets.token_urlsafe(24)
    _db.create_session_draft(project_id, token, [{"summary": "a case"}])
    with _db.session_scope() as sess:
        row = sess.query(_db.SessionDraft).filter(
            _db.SessionDraft.token == token).one()
        row.expires_at = expires_at
    return token


class TestTheSweep:

    def test_an_expired_draft_goes(self, project):
        token = _draft(project, expires_at=_past())
        assert retention.sweep_expired()["session drafts"] >= 1
        with _db.session_scope() as sess:
            assert sess.query(_db.SessionDraft).filter(
                _db.SessionDraft.token == token).one_or_none() is None

    def test_a_live_draft_stays(self, project):
        """The control, and the one that matters most: a sweep that takes
        the recording somebody is about to review is worse than no sweep."""
        token = _draft(project, expires_at=_future())
        retention.sweep_expired()
        assert _db.get_session_draft(token) is not None

    def test_an_expired_control_session_goes(self, project):
        token = secrets.token_urlsafe(24)
        assert _db.create_browser_control_session(project, token) is not None
        with _db.session_scope() as sess:
            row = sess.query(_db.BrowserControlSession).filter(
                _db.BrowserControlSession.token == token).one()
            row.expires_at = _past()
        assert retention.sweep_expired()["browser control"] >= 1
        with _db.session_scope() as sess:
            assert sess.query(_db.BrowserControlSession).filter(
                _db.BrowserControlSession.token == token
            ).one_or_none() is None

    def test_a_live_control_session_stays(self, project):
        token = secrets.token_urlsafe(24)
        assert _db.create_browser_control_session(project, token) is not None
        retention.sweep_expired()
        assert _db.get_browser_control_session(token) is not None

    def test_a_long_expired_auth_token_goes(self):
        """Its own docstring sets the window at thirty days after expiry —
        long enough to answer "why did my link stop working", bounded enough
        that the table does not grow for ever. Nothing enforced it."""
        uid = _db.create_user(f"s-{secrets.token_hex(5)}@example.com")
        token = secrets.token_urlsafe(24)
        assert _db.create_auth_token("reset", uid,
                                     _db.get_user(uid)["email"], token)
        with _db.session_scope() as sess:
            row = sess.get(_db.AuthToken, token)
            row.expires_at = _db._utcnow() - timedelta(days=40)
        assert retention.sweep_expired()["auth tokens"] >= 1
        with _db.session_scope() as sess:
            assert sess.get(_db.AuthToken, token) is None

    def test_a_recently_expired_auth_token_stays(self):
        """Expired is not the same as sweepable. A link that stopped working
        yesterday is the one somebody is about to ask about."""
        uid = _db.create_user(f"s-{secrets.token_hex(5)}@example.com")
        token = secrets.token_urlsafe(24)
        assert _db.create_auth_token("reset", uid,
                                     _db.get_user(uid)["email"], token)
        with _db.session_scope() as sess:
            sess.get(_db.AuthToken, token).expires_at = _past()
        retention.sweep_expired()
        with _db.session_scope() as sess:
            assert sess.get(_db.AuthToken, token) is not None

    def test_every_declared_sweeper_exists(self):
        """The table is names-as-strings, so a rename in ``engine.db`` would
        otherwise turn a sweeper into a silent no-op — which is the exact
        state this file exists to end."""
        missing = [name for name, _ in retention._SWEEPERS
                   if not hasattr(_db, name)]
        assert not missing, missing

    def test_one_failing_sweeper_does_not_stop_the_others(self, project,
                                                          monkeypatch):
        def _boom():
            raise RuntimeError("database is away")

        monkeypatch.setattr(_db, "purge_expired_session_drafts", _boom)
        token = secrets.token_urlsafe(24)
        assert _db.create_browser_control_session(project, token) is not None
        with _db.session_scope() as sess:
            sess.query(_db.BrowserControlSession).filter(
                _db.BrowserControlSession.token == token
            ).one().expires_at = _past()

        counts = retention.sweep_expired()
        assert counts["session drafts"] == 0
        assert counts["browser control"] >= 1


class TestItIsActuallyCalled:
    """The half that was missing. Every assertion above passed before the
    fix too — the helpers always worked; nothing ran them."""

    def test_the_daily_thread_calls_the_sweep(self):
        """Read off the compiled loop rather than grepped out of the file:
        a comment mentioning ``sweep_expired`` would satisfy a text search
        and change nothing at runtime."""
        import app as _app

        loop = next(
            const for const in
            _app._start_snapshot_catchup_thread.__code__.co_consts
            if hasattr(const, "co_names") and const.co_name == "_loop")
        assert "sweep_expired" in loop.co_names
        assert "retention" in loop.co_names

    def test_the_sweep_runs_before_the_snapshots(self):
        """Order is the point, not taste: a database that is full stops the
        snapshot writes too, so the pass that frees space goes first."""
        import app as _app

        loop = next(
            const for const in
            _app._start_snapshot_catchup_thread.__code__.co_consts
            if hasattr(const, "co_names") and const.co_name == "_loop")
        names = list(loop.co_names)
        assert names.index("sweep_expired") < names.index("list_projects")
