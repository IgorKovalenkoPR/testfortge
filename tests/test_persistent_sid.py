"""PR-L regression — persistent signed SID cookie survives Render's
filesystem-session wipe.

Root cause we're pinning here: Render free tier wipes /app/flask_session
on every redeploy. Pre-PR-L the SID lived inside the filesystem-backed
session file (``session.sid`` or ``session["_tf_sid"]``), so the file
wipe orphaned every project the user had ever created — the dropdown
showed a brand-new "Untitled project YYYY-MM-DD HH:MM" instead of the
user's actual work. Operator-reported repeatedly across the work
session that produced PRs A–J.

The fix surfaces the SID as a SEPARATE signed cookie (``_tfg_sid_v1``)
signed by ``SECRET_KEY``. Render persists the secret key across
redeploys; the browser keeps the cookie; the SID stays stable; and
``ensure_active_project`` re-derives the project_id from Postgres via
``list_projects(owner_sid=sid)`` after the session wipe.

These tests verify:

  1. The signed-cookie round-trip is sound (sign → ship → re-read).
  2. ``get_session_id`` prefers the persistent cookie when present.
  3. The after-request hook mints the cookie when missing and the
     session has a SID worth preserving.
  4. A tampered cookie is rejected and the function falls back to
     the session-stored SID without raising.
"""

from __future__ import annotations

import uuid

import pytest
from itsdangerous import URLSafeSerializer

from app import app as flask_app
from routes import _shared as shared


@pytest.fixture(autouse=True)
def _stable_secret_key():
    """Pin SECRET_KEY for the duration of the test so the cookie
    signature is deterministic. Restored after the test so unrelated
    suites stay isolated.
    """
    original = flask_app.config.get("SECRET_KEY")
    flask_app.config["SECRET_KEY"] = "pr-l-test-secret-key-stable"
    yield
    flask_app.config["SECRET_KEY"] = original


def _serializer():
    """Build a serializer matching the one ``_shared`` uses."""
    return URLSafeSerializer(
        flask_app.config["SECRET_KEY"],
        salt=shared._PERSISTENT_SID_SALT,
    )


# ── 1. Signed cookie round-trip ──────────────────────────────────


class TestSignedCookieRoundTrip:
    def test_signed_payload_round_trips(self):
        with flask_app.test_request_context("/"):
            sid = "abc123def456"
            payload = shared._persistent_sid_cookie_value(sid)
            recovered = _serializer().loads(payload)
            assert recovered == sid

    def test_tampered_cookie_is_rejected(self):
        with flask_app.test_request_context("/") as ctx:
            sid = "tamper-target"
            payload = shared._persistent_sid_cookie_value(sid)
            # Surgically corrupt the signature portion (after the dot).
            tampered = payload[:-3] + "AAA"
            # Build a fresh request context with the tampered cookie
            # and confirm ``_read_persistent_sid_cookie`` returns None
            # rather than raising.
            with flask_app.test_request_context(
                "/", headers={"Cookie": f"_tfg_sid_v1={tampered}"}
            ):
                assert shared._read_persistent_sid_cookie() is None

    def test_missing_cookie_returns_none(self):
        with flask_app.test_request_context("/"):
            assert shared._read_persistent_sid_cookie() is None


# ── 2. get_session_id prefers the persistent cookie ──────────────


class TestGetSessionIdPrefersPersistentCookie:
    def test_cookie_present_returns_cookie_sid(self):
        sid_in_cookie = uuid.uuid4().hex
        with flask_app.test_request_context("/"):
            cookie_value = shared._persistent_sid_cookie_value(sid_in_cookie)
        with flask_app.test_request_context(
            "/", headers={"Cookie": f"_tfg_sid_v1={cookie_value}"}
        ):
            from flask import session
            # Pre-existing session has a DIFFERENT SID — the cookie
            # must win, simulating the post-redeploy state where the
            # filesystem session was wiped but the cookie survived.
            session["_tf_sid"] = "stale-session-sid"
            assert shared.get_session_id() == sid_in_cookie
            # And the function mirrors the cookie SID into the session
            # so other code paths reading ``session["_tf_sid"]``
            # converge on the same identifier.
            assert session["_tf_sid"] == sid_in_cookie

    def test_cookie_absent_falls_back_to_session_backend_sid(self):
        """When the persistent cookie is missing, ``get_session_id``
        falls through to ``session.sid`` (the Flask-Session backend
        identifier) — that's the migration path for users who had a
        valid session BEFORE PR-L landed. After-request hook then
        promotes ``session.sid`` to the persistent cookie so the next
        redeploy doesn't lose them.
        """
        with flask_app.test_request_context("/"):
            from flask import session
            # Pre-existing session might have either a backend sid
            # (filesystem) or just _tf_sid. The contract is: the
            # function returns *some* stable identifier and mirrors
            # it into _tf_sid for downstream readers.
            sid = shared.get_session_id()
            assert sid
            assert session.get("_tf_sid") == sid, (
                "get_session_id must mirror the resolved SID back "
                "into session['_tf_sid'] for code paths that read "
                "the session dict directly"
            )

    def test_no_cookie_no_session_mints_fresh_uuid(self):
        with flask_app.test_request_context("/"):
            from flask import session
            session.clear()
            sid = shared.get_session_id()
            assert sid
            assert len(sid) >= 32  # hex UUID
            # And it's now stored in the session for the next request.
            assert session["_tf_sid"] == sid


# ── 3. After-request hook mints the cookie when missing ──────────


class TestPersistentCookieAfterRequest:
    def test_needs_cookie_when_session_has_sid_and_no_cookie(self):
        with flask_app.test_request_context("/"):
            from flask import session
            session["_tf_sid"] = "ready-to-promote"
            assert shared.needs_persistent_sid_cookie() is True

    def test_does_not_need_cookie_when_one_already_present(self):
        with flask_app.test_request_context("/"):
            sid = "already-set"
            value = shared._persistent_sid_cookie_value(sid)
        with flask_app.test_request_context(
            "/", headers={"Cookie": f"_tfg_sid_v1={value}"}
        ):
            from flask import session
            session["_tf_sid"] = sid
            assert shared.needs_persistent_sid_cookie() is False

    def test_does_not_need_cookie_when_session_empty(self):
        """A first-request-on-static-asset shouldn't trigger a
        Set-Cookie for an SID that doesn't exist yet."""
        with flask_app.test_request_context("/"):
            from flask import session
            session.clear()
            assert shared.needs_persistent_sid_cookie() is False

    def test_set_cookie_writes_signed_value(self):
        """The hook's ``set_persistent_sid_cookie`` mutates the
        response in-place; subsequent reads must verify the cookie
        is signed correctly and carries the SID.
        """
        with flask_app.test_request_context("/"):
            from flask import session
            session["_tf_sid"] = "fresh-sid-to-promote"

            from flask import make_response
            resp = make_response("OK")
            shared.set_persistent_sid_cookie(resp)

            # The Set-Cookie header should mention the cookie name and
            # carry an HttpOnly / SameSite hardening flag.
            set_cookie_headers = resp.headers.getlist("Set-Cookie")
            cookie_header = next(
                (h for h in set_cookie_headers
                 if "_tfg_sid_v1" in h), None,
            )
            assert cookie_header, set_cookie_headers
            assert "HttpOnly" in cookie_header
            assert "SameSite=Lax" in cookie_header
            # And the value round-trips back to our SID.
            value = cookie_header.split("_tfg_sid_v1=", 1)[1].split(";", 1)[0]
            recovered = _serializer().loads(value)
            assert recovered == "fresh-sid-to-promote"


# ── 4. End-to-end via test client: redeploy simulation ──────────


class TestRedeploySimulation:
    """Simulate the Render redeploy scenario: first request writes a
    persistent cookie, the session backend is then wiped (analogous
    to /app/flask_session being deleted), a fresh request with the
    same cookie still resolves to the original SID.
    """

    def test_sid_survives_simulated_filesystem_wipe(self):
        """End-to-end: a SID encoded in the persistent cookie
        resolves to the same value through ``get_session_id`` even
        when the server-side session is empty (analogous to the
        post-redeploy state where /app/flask_session was wiped).
        """
        original_sid = "redeploy-survivor-sid"
        with flask_app.test_request_context("/"):
            cookie_value = shared._persistent_sid_cookie_value(
                original_sid
            )

        # Second "visit" AFTER simulated filesystem wipe: new empty
        # session, but the browser still has the persistent cookie.
        with flask_app.test_request_context(
            "/", headers={"Cookie": f"_tfg_sid_v1={cookie_value}"},
        ):
            from flask import session
            # Explicitly clear the session to mimic the wiped state.
            session.clear()
            recovered = shared.get_session_id()
            assert recovered == original_sid, (
                f"persistent cookie must keep SID stable across "
                f"server-side session wipe; got {recovered!r}, "
                f"expected {original_sid!r}"
            )
            # And the SID is mirrored into the (now-recovered)
            # session for downstream readers.
            assert session["_tf_sid"] == original_sid
