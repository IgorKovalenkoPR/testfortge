"""Shared fixtures for all test levels."""

import os
import shutil
import sys
import tempfile
import time
import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Tests run against SQLite by default. The production-safety guard in
# ``engine.db.init_db`` refuses to boot on SQLite unless FLASK_DEBUG=1
# (or the explicit TESTFORTGE_ALLOW_SQLITE_PROD escape hatch). Set the
# debug flag here, before any module that touches the DB gets imported,
# so the whole suite sees a consistent local-dev environment.
os.environ.setdefault("FLASK_DEBUG", "1")

# …and give the suite a database of its own, for the same reason and at
# the same moment: ``engine.db`` resolves its URL at import time.
#
# Without this the suite ran against ``storage/testfortge.db`` — the
# developer's actual local database. Two consequences, both real:
#
#   * it filled up. A working copy here had reached 45 MB and 12,231
#     bug rows, none of them the operator's;
#   * the suite was only green on a first run. ``upsert_project`` keys
#     on the project name, so a fixture that names its project after
#     the test reuses the same row on the next invocation and the bugs
#     accumulate — ``test_failed_item_can_file_a_bug_carrying_the_
#     testers_words`` asserts there is exactly one, and on a second run
#     there were two. CI never saw it because every CI run starts on a
#     clean checkout; locally it looks like a flaky test.
#
# A fresh file each run, not just a separate one: a scratch database
# that survives between invocations reproduces the accumulation bug in
# a new location. Removed here rather than in a session fixture because
# ``engine.db`` opens it during ``from app import app`` below.
#
# An explicit TESTFORTGE_DB or DATABASE_URL still wins and is never
# deleted — that is how tests/test_schema_migration.py points itself at
# Postgres, and how someone can inspect a run afterwards.
# …and everything it writes goes under a directory belonging to **this
# process** (M-1).
#
# The scratch database used to be one fixed path, and the session, upload
# and artefact directories were the working checkout's. That is fine for one
# run and wrong for two: E10's confirmation run started a background
# regression and the browser gate at the same time and got **9 failures**
# that were not regressions at all —
#
#     test_failed_item_can_file_a_bug_carrying_the_testers_words
#     test_the_limit_counts_across_the_whole_scope
#     test_a_vanished_session_is_not_blamed_on_the_user            …and six more
#
# — eight of them two suites inserting into one SQLite file and counting
# each other's rows, the ninth two suites sharing ``flask_session/``. The
# same run alone was 4 581 green. A suite that cannot run beside itself
# produces convincing false regressions inside the exercise that exists to
# find regressions, and it rules out ``pytest-xdist`` and any background
# run while you work.
#
# ``PYTEST_XDIST_WORKER`` first so an xdist worker is isolated from its
# siblings rather than from the controller only; the pid otherwise.
_RUN_TOKEN = os.environ.get("PYTEST_XDIST_WORKER") or f"pid-{os.getpid()}"
_RUN_ROOT = os.path.join(tempfile.gettempdir(),
                         f"testfortge-pytest-{_RUN_TOKEN}")

# A *fresh* root each run, not merely a private one: pids are reused, and a
# scratch directory that survives its process reproduces the accumulation
# bug described above in a new place. Wiped here rather than in a session
# fixture because ``engine.db`` and ``config`` open their paths during
# ``from app import app`` below.
shutil.rmtree(_RUN_ROOT, ignore_errors=True)

# Roots older than a day belong to runs that are over — pids are not
# reused fast enough for this to catch a live one, and without it a machine
# accumulates one directory per pytest invocation forever. Best effort: a
# temp directory that resists deletion is not a reason to fail a test run.
try:
    _cutoff = time.time() - 24 * 60 * 60
    for _name in os.listdir(tempfile.gettempdir()):
        if not _name.startswith("testfortge-pytest-"):
            continue
        _stale = os.path.join(tempfile.gettempdir(), _name)
        if os.path.isdir(_stale) and os.path.getmtime(_stale) < _cutoff:
            shutil.rmtree(_stale, ignore_errors=True)
except OSError:  # pragma: no cover — an unreadable temp dir
    pass

# **A value we set in a parent process is not an explicit value.** This
# stamp is the difference, and without it the fix above works for two
# separate ``pytest`` invocations and does nothing for ``pytest -n 4``:
# xdist workers are subprocesses that inherit the controller's environment,
# so every worker would find ``TESTFORTGE_DB`` already set — by the
# controller's own copy of this file — and keep it. Measured: four workers
# on one database, and ``test_the_restore_button_rebuilds_the_project``
# counting 31 projects where it created one.
#
# A value the *developer* exported still wins, because then no stamp is
# present. That is how tests/test_schema_migration.py points itself at
# Postgres and how someone can inspect a run afterwards.
_STAMP = "TFG_TEST_PATHS_OWNER"
_INHERITED = os.environ.get(_STAMP)
_RECLAIM = _INHERITED is not None and _INHERITED != _RUN_TOKEN


def _claim_path(var: str, value: str) -> None:
    if _RECLAIM or not os.environ.get(var):
        os.environ[var] = value


# The database. ``DATABASE_URL`` is left alone in both directions: the
# Postgres leg is one server for the whole run, so isolating *it* per worker
# is a different problem than this one and is not solved here.
if not os.environ.get("DATABASE_URL"):
    _claim_path("TESTFORTGE_DB", os.path.join(_RUN_ROOT, "scratch.db"))

# The directories the application writes to. ``config`` reads the first
# three names; ``engine.automation_paths`` reads ``STORAGE_ROOT``, which is
# also where ``LocalBackend`` serves artefacts and bug attachments from.
# Isolating the database alone would leave two runs deleting each other's
# evidence — ``tests/test_bug_attachments.py`` sweeps ``STORAGE_ROOT/project``
# when it finishes, and it cannot know those projects are another run's.
for _var, _leaf in (("SESSION_FILE_DIR", "flask_session"),
                    ("UPLOAD_FOLDER", "uploads"),
                    ("STORAGE_FOLDER", "storage"),
                    ("STORAGE_ROOT", "storage")):
    _claim_path(_var, os.path.join(_RUN_ROOT, _leaf))
    os.makedirs(os.environ[_var], exist_ok=True)

os.environ[_STAMP] = _RUN_TOKEN

from app import app as flask_app


@pytest.fixture
def app():
    flask_app.config["TESTING"] = True
    flask_app.config["SESSION_TYPE"] = "filesystem"
    # Disable CSRF checking during tests — the clients POST without
    # rendering the templates that supply the token, and we trust the
    # test client itself. Production keeps CSRF enabled.
    flask_app.config["WTF_CSRF_ENABLED"] = False
    yield flask_app


# ── Signing the test client in (E9.9) ────────────────────────────────
#
# The suite could only be run with the flags off. With
# ``AUTH_ENABLED=1 ORG_MODE=1`` it produced 405 failures and 61 errors,
# and almost all of them were the same thing: no fixture signs in, so the
# route policy redirects every request to /auth/login and the assertion
# reads `assert 302 == 307`. That meant the regressions of E1 and E2 could
# only be caught in the mode the product does *not* ship in.
#
# So ``client`` signs itself in — but only when authentication is actually
# active. With the flags off, ``_sign_in`` returns immediately and the
# 3,633 existing tests see a byte-identical session. That is the property
# that makes this safe to do in a shared fixture rather than in 200 files.
#
# The default identity is an **admin**, because the alternative is worse in
# both directions: as a plain user every admin-only route in the suite
# would 403 and hundreds of tests would need a role argument they do not
# care about, while the tests that genuinely check the role boundary set
# their own identity anyway (see tests/test_permissions.py::_as). A default
# that reaches everything makes role tests explicit and everything else
# quiet.

_TEST_IDENTITY: dict[str, str] = {}


def _auth_active() -> bool:
    # permissions.auth_active() rather than the flag directly: it is what
    # the route policy consults, so the fixture and the gate can never
    # disagree about whether this run is authenticated.
    from engine import permissions as _perm
    return bool(_perm.auth_active())


def _test_identity() -> dict[str, str]:
    """An admin user in an organisation, for whichever database is active.

    Cached, but the cache is *validated* rather than trusted: several test
    modules swap ``engine.db``'s engine for a temp database of their own
    (``fresh_db``), and a user id minted against the previous one does not
    exist there. The gate then finds no user and refuses — which looked
    exactly like the sign-in fixture not working at all, in files whose
    subject was metrics.
    """
    from engine import db as _db
    if _TEST_IDENTITY.get("user_id"):
        try:
            if _db.get_user(_TEST_IDENTITY["user_id"]):
                return _TEST_IDENTITY
        except Exception:  # pragma: no cover — a torn-down engine
            pass
        _TEST_IDENTITY.clear()
    email = "suite-admin@testfortge.test"
    user_id = _db.create_user(email, display_name="Suite Admin",
                              email_verified=True)
    if not user_id:
        # Already there — a second run against a surviving database, or a
        # test that created it first. Reuse rather than fail: the fixture
        # must not depend on being the first thing to touch the table.
        existing = _db.get_user_by_email(email) or {}
        user_id = existing.get("id")
    org_id = _db.create_organization("Suite Org")
    if user_id and org_id:
        _db.add_org_member(org_id, user_id, "admin")
    _TEST_IDENTITY.update({"user_id": user_id or "", "org_id": org_id or ""})
    return _TEST_IDENTITY


def _sign_in(test_client) -> None:
    """Put the suite's identity into *test_client*'s session.

    Writes the session keys directly rather than calling
    ``permissions.login_user``: that function exists to rotate the session
    id against fixation, it needs a request context, and it clears the
    session — all three are its own subject matter, tested in
    ``tests/test_auth_*.py``. A fixture that went through it would couple
    every test in the suite to the sign-in flow's internals.

    The **timeout stamps** are written, though, because those are not
    internals of the flow — they are part of the shape of a signed-in
    session, and two things read them. ``session_timeout.classify`` tolerates
    their absence (a session predating that feature must not be thrown out on
    one deploy) but ``permissions._revoked_before`` does not: a session that
    cannot show when it began cannot show it began after a revocation. Without
    the stamps here, one test performing a password reset on the suite's
    shared identity signed out every test that ran after it in the same
    worker — which is how this line got written.
    """
    if not _auth_active():
        return
    from engine import permissions as _perm
    from engine import session_timeout as _timeout
    identity = _test_identity()
    if not identity.get("user_id"):
        return
    with test_client.session_transaction() as sess:
        sess[_perm.SESSION_USER_KEY] = identity["user_id"]
        if identity.get("org_id"):
            sess[_perm.SESSION_ORG_KEY] = identity["org_id"]
        _timeout.stamp(sess)


@pytest.fixture
def fresh_org(client, request):
    """An organisation of this test's own, with the client signed into it.

    Needed by any test that reasons about **organisation-wide** state — the
    browser-run limit is the one that found this. All the suite's projects
    otherwise share one organisation, so an open run left behind by an
    earlier test counts against a later one and the later test fails for
    something that happened in a different file.

    Returns the org id, or ``""`` when authentication is off, where there
    is no organisation and no shared scope to escape.
    """
    if not _auth_active():
        return ""
    from engine import db as _db
    from engine import permissions as _perm
    identity = _test_identity()
    org_id = _db.create_organization(f"Org for {request.node.name}"[:120])
    if identity.get("user_id") and org_id:
        _db.add_org_member(org_id, identity["user_id"], "admin")
    with client.session_transaction() as sess:
        sess[_perm.SESSION_ORG_KEY] = org_id
    return org_id or ""


@pytest.fixture
def make_project():
    """``make_project("name")`` — a project the signed-in caller can see.

    ``db.upsert_project`` takes an ``org_id`` and the product now passes the
    caller's organisation on every creation path, because without it a
    project was invisible to its own author under ``ORG_MODE``. A test that
    calls ``upsert_project`` directly has to do the same, or it builds a
    project that the org-scoped listings correctly refuse to show — and
    then fails for a reason that has nothing to do with its subject.

    With authentication off this is exactly ``upsert_project(name)``.
    """
    from engine import db as _db

    def _make(name: str, **kwargs) -> str:
        if _auth_active():
            kwargs.setdefault("org_id", _test_identity().get("org_id") or None)
        return _db.upsert_project(name, **kwargs)

    return _make


@pytest.fixture
def sign_in():
    """``sign_in(client)`` — for a test module with its own client fixture.

    Three modules build their own client (a different app instance, a
    fresh database, a modified config) and so shadow the shared fixture.
    Without this they were the last cluster of failures in the
    authenticated run: every request 302'd to the sign-in page, in files
    whose subject had nothing to do with authentication.
    """
    return _sign_in


@pytest.fixture
def client(app):
    with app.test_client() as c:
        _sign_in(c)
        yield c


@pytest.fixture
def anon_client(app):
    """A client that is never signed in.

    For the tests whose subject *is* the unauthenticated case — the
    sign-in page, the redirect, the 401 on an API. Those must keep working
    when the flags are on, and with ``client`` now signed in they need a
    way to say so explicitly.
    """
    with app.test_client() as c:
        yield c


@pytest.fixture
def as_user(monkeypatch):
    """``as_user("u-me", admin=False)`` — become an arbitrary identity.

    For tests about *who* is acting: assignment, ownership, the role
    boundary. Patching the identity functions by hand is the obvious way to
    do this and it has a trap that cost real time twice: the route policy
    gate calls ``has_role``, which resolves the role from the database, so a
    made-up user id is a member of nothing and every request 403s before the
    view runs. The test then asserts a 403 that came from the gate rather
    than from the rule it meant to check — passing for the wrong reason in
    the negative cases, and failing inexplicably in the positive ones.

    So this patches the gate open and leaves the view's own checks to be
    exercised. The gate has its own tests in ``tests/test_permissions.py``
    and ``tests/test_route_policy_matrix.py``.
    """
    from engine import permissions as _perm

    def _become(user_id: str, *, admin: bool = False, name: str = "",
                org_id: str | None = None):
        monkeypatch.setattr(_perm, "auth_active", lambda: True)
        monkeypatch.setattr(_perm, "current_user_id", lambda: user_id)
        monkeypatch.setattr(_perm, "current_user",
                            lambda: {"id": user_id, "name": name or user_id})
        monkeypatch.setattr(_perm, "is_admin", lambda: admin)
        monkeypatch.setattr(_perm, "has_role", lambda minimum: True)
        if org_id is not None:
            monkeypatch.setattr(_perm, "org_active", lambda: True)
            monkeypatch.setattr(_perm, "current_org_id", lambda: org_id)
        return user_id

    return _become


@pytest.fixture
def auth_off(monkeypatch):
    """Force the flags-off deployment for one test.

    A test whose subject is mode-dependent behaviour has to name its mode,
    or it passes in one run configuration and fails in the other for
    reasons that have nothing to do with the thing it is checking. The
    Runs page is the clearest example: with authentication off it lists
    every run and explains that assignment needs authentication, and with
    it on that sentence is absent and correct to be absent.

    Patches ``permissions`` rather than the environment because the flag is
    read through those functions by both the route policy and the views, so
    one patch covers the gate and the page.
    """
    from engine import permissions as _perm
    monkeypatch.setattr(_perm, "auth_active", lambda: False)
    monkeypatch.setattr(_perm, "org_active", lambda: False)
    monkeypatch.setattr(_perm, "current_user_id", lambda: None)
    monkeypatch.setattr(_perm, "current_user", lambda: None)
    monkeypatch.setattr(_perm, "is_admin", lambda: False)
    return True


@pytest.fixture
def auth_on(monkeypatch):
    """Force the flags-on deployment for one test, as an admin.

    The mirror of :func:`auth_off`, so a test that describes the
    authenticated product can be written and run whichever way the suite
    itself is invoked.
    """
    from engine import permissions as _perm
    identity = _test_identity()
    monkeypatch.setattr(_perm, "auth_active", lambda: True)
    monkeypatch.setattr(_perm, "org_active", lambda: True)
    monkeypatch.setattr(_perm, "current_user_id",
                        lambda: identity.get("user_id") or "u-suite")
    monkeypatch.setattr(_perm, "current_user",
                        lambda: {"id": identity.get("user_id") or "u-suite",
                                 "name": "Suite Admin"})
    monkeypatch.setattr(_perm, "is_admin", lambda: True)
    monkeypatch.setattr(_perm, "has_role", lambda minimum: True)
    # current_org_id too, and not as an afterthought: org_active() alone
    # says "org mode is on" while the resolver still reads an unset session
    # key, so anything scoping by organisation sees None and the test
    # measures the flags-off path under an on-looking fixture.
    monkeypatch.setattr(_perm, "current_org_id",
                        lambda: identity.get("org_id") or None)
    return identity


@pytest.fixture
def forget_workspace():
    """``forget_workspace(client)`` — drop the workspace, keep the identity.

    Several tests clear the whole session to prove a property that has
    nothing to do with signing in: that the manual walk's cursor lives in
    the database, so a new browser resumes it. With the flags on,
    ``sess.clear()`` also signs the client out, and the test then measures
    the login redirect instead of the property it names.

    A fixture rather than a module-level helper because ``tests/`` is not an
    importable package — ``from conftest import …`` fails at collection —
    and because this way the call sites read the same as before.
    """
    from engine import permissions as _perm

    def _forget(test_client) -> None:
        keep = (_perm.SESSION_USER_KEY, _perm.SESSION_ORG_KEY,
                "_session_active_since")
        with test_client.session_transaction() as sess:
            carried = {k: sess.get(k) for k in keep
                       if sess.get(k) is not None}
            sess.clear()
            sess.update(carried)

    return _forget


@pytest.fixture
def suite_identity():
    """``{"user_id", "org_id"}`` for the signed-in suite user, or empty
    strings when authentication is off."""
    return dict(_test_identity()) if _auth_active() else {"user_id": "",
                                                          "org_id": ""}
