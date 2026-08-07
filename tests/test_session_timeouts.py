"""E1.5 — idle and absolute session timeouts.

The rest of E1.5 was already covered: session-id rotation against fixation
lives in ``tests/test_auth_password.py::TestSessionFixation`` and
``tests/test_access_control_branches.py``, and "sign out everywhere" in
``tests/test_server_session.py``. This file is the two clocks.

**Nothing here sleeps.** A test that waits four hours cannot exist and a
test that waits two seconds is a flaky test in waiting, so the decision is
a pure function of ``(stamps, now)`` and the tests hand it a ``now`` — or
write a stale stamp into the session, which is what a returning browser
presents anyway. The project's policy is zero flaky tests with no
quarantine, and a timeout is the classic place a suite loses that.

Three properties beyond "it expires", each of which is a way the feature
could be present and still wrong:

* **an absolute clock that activity cannot refresh.** An idle timeout alone
  bounds nothing against a stolen cookie, because the thief's own requests
  are activity. So there is a test where the session is busy right up to
  the moment it dies;
* **a session with no stamps is left alone.** Every session live at the
  moment this deploys has no clocks. Expiring those would sign out the
  whole customer base on the deploy that adds the feature;
* **the reason survives.** On the free plan the store is wiped whenever the
  dyno sleeps, so "sign in again" is a routine event with nothing to do
  with a timeout. If the page said the same thing for both, it would
  routinely blame a user for the platform's nap. Three arrivals, three
  sentences — see ``TestTheSignInPageSaysWhy``.
"""
from __future__ import annotations

import secrets

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import permissions as _perm
from engine import session_timeout as _timeout

GOOD_PASSWORD = "correct horse battery staple"

#: A round, obviously-synthetic clock reading. Real timestamps work too;
#: a fixed one makes the arithmetic in each test readable.
NOW = 1_800_000_000.0

HOUR = 3600.0


@pytest.fixture(autouse=True)
def _db_ready():
    _db.init_db()


@pytest.fixture
def auth_env(monkeypatch):
    """Real accounts switched on through the flags, not through patches.

    The hook reads ``permissions.auth_active()``, which reads the flag, so
    patching the environment exercises the same path production does.
    """
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    return True


@pytest.fixture
def account():
    """A user who can actually sign in, with an organisation."""
    email = f"timeout-{secrets.token_hex(5)}@example.com"
    uid = _db.create_user(email, display_name="Clock Watcher",
                          password_hash=_auth.hash_password(GOOD_PASSWORD),
                          email_verified=True)
    org = _db.create_organization(f"Clocks {secrets.token_hex(4)}")
    _db.add_org_member(org, uid, "admin")
    return {"email": email, "user_id": uid, "org_id": org}


def _sign_in(client, account) -> None:
    response = client.post("/auth/login",
                           data={"email": account["email"],
                                 "password": GOOD_PASSWORD})
    assert response.status_code in (302, 303), response.status_code


def _rewind(client, *, auth_at=None, seen_at=None) -> None:
    """Age the session's clocks, the way a returning browser would.

    This is the whole reason nothing sleeps: a session that was last used
    five hours ago is a session with a five-hour-old stamp, and writing
    that stamp is indistinguishable — to every line of production code —
    from having waited.
    """
    with client.session_transaction() as sess:
        if auth_at is not None:
            sess[_timeout.AUTH_AT_KEY] = auth_at
        if seen_at is not None:
            sess[_timeout.SEEN_AT_KEY] = seen_at


def _now(client) -> float:
    """``time.time()`` as the session sees it — read from its own stamp."""
    with client.session_transaction() as sess:
        return float(sess[_timeout.SEEN_AT_KEY])


# ── The decision itself ──────────────────────────────────────────────

class TestClassify:
    """A pure function, so every case below is exact rather than timed."""

    def test_a_fresh_session_survives(self):
        stamps = {_timeout.AUTH_AT_KEY: NOW, _timeout.SEEN_AT_KEY: NOW}
        assert _timeout.classify(stamps, NOW) is None

    def test_inactivity_past_the_window_expires_it(self):
        stamps = {_timeout.AUTH_AT_KEY: NOW, _timeout.SEEN_AT_KEY: NOW}
        assert _timeout.classify(stamps, NOW + 5 * HOUR,
                                 idle=4 * 3600, absolute=24 * 3600) \
            == _timeout.IDLE

    def test_activity_keeps_it_alive_inside_the_absolute_window(self):
        # Twelve hours old but used a moment ago: the idle clock is what
        # activity resets, and it should not have fired.
        stamps = {_timeout.AUTH_AT_KEY: NOW,
                  _timeout.SEEN_AT_KEY: NOW + 12 * HOUR}
        assert _timeout.classify(stamps, NOW + 12 * HOUR,
                                 idle=4 * 3600, absolute=24 * 3600) is None

    def test_the_absolute_window_ends_a_session_that_never_went_idle(self):
        """The property an idle timeout cannot provide.

        A stolen cookie is used constantly, so its idle clock never
        expires. Without this test the feature could ship as idle-only and
        look complete.
        """
        stamps = {_timeout.AUTH_AT_KEY: NOW,
                  _timeout.SEEN_AT_KEY: NOW + 25 * HOUR}
        assert _timeout.classify(stamps, NOW + 25 * HOUR,
                                 idle=4 * 3600, absolute=24 * 3600) \
            == _timeout.ABSOLUTE

    def test_when_both_have_passed_the_harder_limit_is_reported(self):
        # "You were away" invites the user to conclude they merely stepped
        # out, when in fact the session had run its whole life.
        stamps = {_timeout.AUTH_AT_KEY: NOW, _timeout.SEEN_AT_KEY: NOW}
        assert _timeout.classify(stamps, NOW + 30 * HOUR,
                                 idle=4 * 3600, absolute=24 * 3600) \
            == _timeout.ABSOLUTE

    def test_a_session_with_no_stamps_is_left_alone(self):
        """The deploy-day property.

        Every session live when this ships has no clocks, and the reading
        "no stamp means infinitely old" would sign out every customer at
        once. They get stamped at their next sign-in.
        """
        assert _timeout.classify({}, NOW + 10_000 * HOUR) is None

    def test_only_one_stamp_still_decides_on_the_one_it_has(self):
        # Half-written session: honour what is there rather than ignoring
        # both, which would leave a session unbounded.
        assert _timeout.classify({_timeout.SEEN_AT_KEY: NOW},
                                 NOW + 5 * HOUR, idle=4 * 3600,
                                 absolute=24 * 3600) == _timeout.IDLE
        assert _timeout.classify({_timeout.AUTH_AT_KEY: NOW},
                                 NOW + 25 * HOUR, idle=4 * 3600,
                                 absolute=24 * 3600) == _timeout.ABSOLUTE

    def test_a_stamp_that_is_not_a_number_is_not_evidence_of_anything(self):
        # A corrupt value must not raise inside a before_request hook —
        # that is a 500 on every page — and must not be read as expiry.
        for junk in ("", "yesterday", None, True, [], {}):
            assert _timeout.classify({_timeout.SEEN_AT_KEY: junk},
                                     NOW + 99 * HOUR) is None, junk

    def test_a_stamp_written_as_a_string_or_a_date_is_understood(self):
        # Sessions round-trip through JSON in one backend and pickle in
        # the other, and an older build may have written either shape.
        assert _timeout.classify({_timeout.SEEN_AT_KEY: str(NOW)},
                                 NOW + 5 * HOUR, idle=4 * 3600,
                                 absolute=24 * 3600) == _timeout.IDLE
        assert _timeout.classify(
            {_timeout.SEEN_AT_KEY: "2020-01-01T00:00:00+00:00"},
            NOW, idle=4 * 3600, absolute=24 * 3600) == _timeout.IDLE

    def test_the_boundary_itself_is_not_expiry(self):
        # Exactly at the window is inside it. Stated because "> or >=" is
        # the kind of thing a later edit flips without noticing.
        stamps = {_timeout.SEEN_AT_KEY: NOW}
        assert _timeout.classify(stamps, NOW + 4 * HOUR,
                                 idle=4 * 3600, absolute=24 * 3600) is None


class TestTheWindowsAreOperable:
    """An operator has to be able to change these without a deploy."""

    def test_the_idle_window_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("SESSION_IDLE_MINUTES", "15")
        assert _timeout.idle_seconds() == 15 * 60

    def test_the_absolute_window_comes_from_the_environment(self,
                                                            monkeypatch):
        monkeypatch.setenv("SESSION_ABSOLUTE_HOURS", "8")
        assert _timeout.absolute_seconds() == 8 * 3600

    def test_the_defaults_apply_when_unset(self, monkeypatch):
        monkeypatch.delenv("SESSION_IDLE_MINUTES", raising=False)
        monkeypatch.delenv("SESSION_ABSOLUTE_HOURS", raising=False)
        assert _timeout.idle_seconds() == \
            _timeout.IDLE_DEFAULT_MINUTES * 60
        assert _timeout.absolute_seconds() == \
            _timeout.ABSOLUTE_DEFAULT_HOURS * 3600

    def test_zero_is_refused_rather_than_locking_everyone_out(self,
                                                             monkeypatch):
        """``SESSION_IDLE_MINUTES=0`` read literally expires every session
        on arrival — nobody types that on purpose, and a typo must not be
        able to take a running deployment down."""
        monkeypatch.setenv("SESSION_IDLE_MINUTES", "0")
        assert _timeout.idle_seconds() == _timeout.IDLE_DEFAULT_MINUTES * 60
        monkeypatch.setenv("SESSION_ABSOLUTE_HOURS", "-3")
        assert _timeout.absolute_seconds() == \
            _timeout.ABSOLUTE_DEFAULT_HOURS * 3600

    def test_nonsense_falls_back_instead_of_raising(self, monkeypatch):
        monkeypatch.setenv("SESSION_IDLE_MINUTES", "four hours")
        assert _timeout.idle_seconds() == _timeout.IDLE_DEFAULT_MINUTES * 60


class TestTheIdleClockIsThrottled:
    """One page load fires roughly a dozen parallel requests here.

    Stamping the session on every one of them marks it modified on every
    one of them — the exact shape that made ``db.session_save`` need an
    IntegrityError-then-UPDATE retry. So the write is throttled, and that
    is a correctness property rather than a micro-optimisation.
    """

    def test_a_second_request_in_the_same_moment_does_not_write(self):
        session = {_timeout.SEEN_AT_KEY: NOW}
        assert _timeout.touch(session, NOW + 1) is False
        assert session[_timeout.SEEN_AT_KEY] == NOW

    def test_a_request_past_the_granularity_does_write(self):
        session = {_timeout.SEEN_AT_KEY: NOW}
        later = NOW + _timeout.SEEN_WRITE_GRANULARITY_SECONDS + 1
        assert _timeout.touch(session, later) is True
        assert session[_timeout.SEEN_AT_KEY] == later

    def test_an_unstamped_session_gets_a_stamp(self):
        session: dict = {}
        assert _timeout.touch(session, NOW) is True
        assert session[_timeout.SEEN_AT_KEY] == NOW

    def test_the_granularity_is_far_finer_than_the_window(self):
        # Otherwise the throttle would itself be a source of early or late
        # expiry.
        assert _timeout.SEEN_WRITE_GRANULARITY_SECONDS * 10 < \
            _timeout.idle_seconds()


# ── The clocks are started, once, for every way in ───────────────────

class TestSigningInStartsTheClocks:
    def test_a_password_sign_in_stamps_both(self, anon_client, auth_env,
                                            account):
        _sign_in(anon_client, account)
        with anon_client.session_transaction() as sess:
            assert sess.get(_timeout.AUTH_AT_KEY), "no absolute clock"
            assert sess.get(_timeout.SEEN_AT_KEY), "no idle clock"

    def test_the_stamp_lives_in_login_user_so_no_path_can_forget(self,
                                                                monkeypatch):
        """There are four ways in — password, invite, Google, and an invite
        redeemed through Google. All four call ``permissions.login_user``,
        so stamping there is what makes "every session has clocks" true by
        construction rather than by four copies of one line."""
        import inspect
        from routes import auth as _routes_auth

        source = inspect.getsource(_routes_auth)
        assert source.count("_perm.login_user(") == 4, (
            "the number of sign-in paths changed — check they all still "
            "route through login_user, which is where the clocks start")
        # The module does import session_timeout, for the sign-in page's
        # explanation. What it must not do is stamp: that would be the
        # per-path chance to forget one that login_user exists to remove.
        assert ".stamp(" not in source, (
            "a route started stamping the clocks itself — put it in "
            "permissions.login_user, which every sign-in path goes through")


# ── Enforcement, on a real request ───────────────────────────────────

class TestAnExpiredSessionStopsWorking:
    def test_an_idle_session_is_sent_to_sign_in_with_the_reason(
            self, anon_client, auth_env, account):
        _sign_in(anon_client, account)
        now = _now(anon_client)
        _rewind(anon_client, seen_at=now - 5 * HOUR)

        response = anon_client.get("/", follow_redirects=False)

        assert response.status_code in (302, 303), response.status_code
        location = response.headers["Location"]
        assert "/auth/login" in location
        assert f"{_timeout.REASON_PARAM}={_timeout.IDLE}" in location

    def test_the_session_is_actually_gone_not_merely_redirected(
            self, anon_client, auth_env, account):
        """A redirect that leaves the session alive is cosmetic — the next
        request to a route that does not redirect would still be signed
        in."""
        _sign_in(anon_client, account)
        now = _now(anon_client)
        _rewind(anon_client, seen_at=now - 5 * HOUR)
        anon_client.get("/", follow_redirects=False)

        assert anon_client.get("/auth/me").get_json()["authenticated"] \
            is False
        with anon_client.session_transaction() as sess:
            assert not sess.get(_perm.SESSION_USER_KEY)

    def test_an_absolute_expiry_reports_itself_differently(
            self, anon_client, auth_env, account):
        _sign_in(anon_client, account)
        now = _now(anon_client)
        # Busy right up to this instant, and still finished.
        _rewind(anon_client, auth_at=now - 30 * HOUR, seen_at=now)

        response = anon_client.get("/", follow_redirects=False)

        assert f"{_timeout.REASON_PARAM}={_timeout.ABSOLUTE}" in \
            response.headers["Location"]

    def test_a_live_session_is_not_disturbed(self, anon_client, auth_env,
                                            account):
        _sign_in(anon_client, account)
        assert anon_client.get("/auth/me").get_json()["authenticated"] is True
        assert anon_client.get("/", follow_redirects=False).status_code == 200

    def test_the_server_side_row_is_dropped_too(self, anon_client, auth_env,
                                                account, monkeypatch):
        """An emptied dict under the same id leaves the cookie replayable
        against whatever a later request writes there. ``login_user``
        deletes the pre-login row for the same reason on the way in."""
        deleted: list[str] = []
        real = _db.session_delete
        monkeypatch.setattr(_db, "session_delete",
                            lambda sid: (deleted.append(sid), real(sid))[1])

        _sign_in(anon_client, account)
        # Emptied *after* signing in, and that is the whole point of the
        # line: ``login_user`` deletes the pre-login row to defeat fixation,
        # so a version of this test that asserted on the accumulated list
        # passed with the expiry's own delete removed. Found by mutation.
        deleted.clear()

        now = _now(anon_client)
        _rewind(anon_client, seen_at=now - 5 * HOUR)
        anon_client.get("/", follow_redirects=False)

        assert deleted, (
            "the expired session's row was left addressable — an emptied "
            "dict under a live id means the cookie can be replayed against "
            "whatever a later request writes there")

    def test_a_fetch_gets_401_and_a_code_it_can_act_on(
            self, anon_client, auth_env, account):
        """A tab polling a progress endpoint needs to tell "your session
        ended" from "you were never signed in" — the first deserves a
        sentence, the second a sign-in page. A redirect to HTML would give
        it neither.
        """
        _sign_in(anon_client, account)
        now = _now(anon_client)
        _rewind(anon_client, seen_at=now - 5 * HOUR)

        response = anon_client.get("/metrics/history",
                                   headers={"X-Requested-With":
                                            "XMLHttpRequest"})

        assert response.status_code == 401, response.status_code
        body = response.get_json()
        assert body["error"] == "session_expired", (
            "indistinguishable from never having signed in")
        assert body["reason"] == _timeout.IDLE
        assert "without activity" in body["message"]

    def test_a_fetch_whose_session_never_existed_is_not_called_expired(
            self, anon_client, auth_env):
        """The other half of the distinction: a caller with no session at
        all gets the plain refusal, not a claim that something of theirs
        ran out."""
        response = anon_client.get("/metrics/history",
                                   headers={"X-Requested-With":
                                            "XMLHttpRequest"})

        assert response.status_code == 401
        assert response.get_json()["error"] == "auth_required"

    def test_the_sign_in_page_does_not_redirect_to_itself(
            self, anon_client, auth_env, account):
        """The loop this would otherwise be: expired session asks for
        /auth/login, hook redirects it to /auth/login, forever."""
        _sign_in(anon_client, account)
        now = _now(anon_client)
        _rewind(anon_client, seen_at=now - 5 * HOUR)

        response = anon_client.get("/auth/login", follow_redirects=False)

        assert response.status_code == 200, response.headers.get("Location")

    def test_the_health_probes_keep_answering(self, anon_client, auth_env,
                                              account):
        _sign_in(anon_client, account)
        now = _now(anon_client)
        _rewind(anon_client, seen_at=now - 5 * HOUR)
        assert anon_client.get("/healthz").status_code == 200

    def test_activity_across_the_gap_keeps_a_session_alive(
            self, anon_client, auth_env, account):
        """The idle clock has to actually be refreshed by requests, or the
        absolute window would be the only one that ever mattered and this
        would silently be a one-clock feature."""
        _sign_in(anon_client, account)
        now = _now(anon_client)
        # Three hours idle — inside the four-hour window.
        _rewind(anon_client, seen_at=now - 3 * HOUR)
        assert anon_client.get("/", follow_redirects=False).status_code == 200
        # …and the request moved the clock, so three more hours is fine too.
        refreshed = _now(anon_client)
        assert refreshed > now - 3 * HOUR, "the request did not touch the clock"

    def test_a_session_without_clocks_is_not_thrown_out(
            self, anon_client, auth_env, account):
        """The deploy-day property at HTTP level.

        Built the way a session written by the previous build looks — a
        user id, an organisation, and no clocks at all. Every live session
        is shaped like this on the deploy that adds the feature, and
        reading "no stamp" as "infinitely old" would sign the whole
        customer base out at once.
        """
        with anon_client.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = account["user_id"]
            sess[_perm.SESSION_ORG_KEY] = account["org_id"]

        assert anon_client.get("/", follow_redirects=False).status_code == 200
        assert anon_client.get("/auth/me").get_json()["authenticated"] is True


class TestWithAuthenticationOff:
    def test_nothing_expires_when_there_is_no_sign_in_to_return_to(
            self, anon_client, monkeypatch):
        """Named mode: the deployment as it ships today. With AUTH_ENABLED
        off there is no identity, and a redirect to a sign-in page would be
        a regression invented by a feature that does not apply."""
        monkeypatch.setenv("AUTH_ENABLED", "0")
        monkeypatch.setenv("ORG_MODE", "0")
        with anon_client.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = "u-someone"
            sess[_timeout.SEEN_AT_KEY] = NOW  # ancient
            sess[_timeout.AUTH_AT_KEY] = NOW

        assert anon_client.get("/", follow_redirects=False).status_code == 200


# ── What the page says, and why it matters that it differs ───────────

class TestTheSignInPageSaysWhy:
    def test_an_idle_timeout_is_explained_in_words(self, anon_client,
                                                  auth_env):
        body = anon_client.get(
            f"/auth/login?{_timeout.REASON_PARAM}={_timeout.IDLE}"
        ).get_data(as_text=True)
        assert "without activity" in body
        assert "saved work is untouched" in body

    def test_an_absolute_timeout_gets_a_different_sentence(self, anon_client,
                                                          auth_env):
        idle = anon_client.get(
            f"/auth/login?{_timeout.REASON_PARAM}={_timeout.IDLE}"
        ).get_data(as_text=True)
        absolute = anon_client.get(
            f"/auth/login?{_timeout.REASON_PARAM}={_timeout.ABSOLUTE}"
        ).get_data(as_text=True)
        assert "even active ones" in absolute
        assert idle != absolute, (
            "both timeouts render the same page, so the distinction the "
            "reason parameter carries is thrown away at the last step")

    def test_the_window_in_the_sentence_follows_the_configuration(
            self, anon_client, auth_env, monkeypatch):
        """Prose with a hard-coded number contradicts the operator the
        first time they widen the window."""
        monkeypatch.setenv("SESSION_ABSOLUTE_HOURS", "8")
        body = anon_client.get(
            f"/auth/login?{_timeout.REASON_PARAM}={_timeout.ABSOLUTE}"
        ).get_data(as_text=True)
        assert "8 hours" in body

    def test_a_vanished_session_is_not_blamed_on_the_user(self, anon_client,
                                                          auth_env):
        """The distinction this feature is required to make.

        The free dyno sleeps and takes the session store with it, so a
        cookie arrives naming nothing. That person was not idle and their
        session did not run out — saying either would be false, and the
        true answer is that the platform dropped it.
        """
        anon_client.set_cookie("session", "a-cookie-naming-nothing")

        body = anon_client.get("/auth/login").get_data(as_text=True)

        assert "could not find your session" in body
        assert "sleeps when idle" in body
        assert "without activity" not in body, (
            "a session the server lost was reported as the user's "
            "inactivity")

    def test_a_first_visit_is_given_no_invented_reason(self, anon_client,
                                                      auth_env):
        body = anon_client.get("/auth/login").get_data(as_text=True)
        assert "could not find your session" not in body
        assert "without activity" not in body

    def test_a_visitor_who_has_only_picked_a_language_is_not_told_that(
            self, anon_client, auth_env):
        """The vanished-store test keys on an entirely empty session, so an
        anonymous browser that has been here before must not trip it."""
        anon_client.get("/auth/login?lang=ua")
        body = anon_client.get("/auth/login").get_data(as_text=True)
        assert "could not find your session" not in body

    def test_a_forged_reason_is_ignored_rather_than_echoed(self, anon_client,
                                                          auth_env):
        """The reason arrives in a query parameter, so it is attacker text
        reaching a template. Matched against a closed set, not printed."""
        body = anon_client.get(
            f"/auth/login?{_timeout.REASON_PARAM}=<b>whatever</b>"
        ).get_data(as_text=True)
        assert "whatever" not in body
        assert _timeout.valid_reason("<b>whatever</b>") is None
        assert _timeout.valid_reason(_timeout.IDLE) == _timeout.IDLE

    def test_explain_declines_to_narrate_a_reason_it_does_not_know(self):
        assert _timeout.explain(None) is None
        assert _timeout.explain("made-up") is None
