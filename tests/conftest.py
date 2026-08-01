"""Shared fixtures for all test levels."""

import os
import sys
import tempfile
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
if not os.environ.get("DATABASE_URL") and not os.environ.get("TESTFORTGE_DB"):
    _scratch_db = os.path.join(tempfile.gettempdir(), "testfortge-pytest.db")
    for _suffix in ("", "-journal", "-wal", "-shm"):
        try:
            os.unlink(_scratch_db + _suffix)
        except OSError:
            pass  # absent, or held open on Windows — a stale file is
                  # the thing we are avoiding, not a reason to fail boot
    os.environ["TESTFORTGE_DB"] = _scratch_db

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


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c
