"""E0.4 + E1.3 + E1.7 — sending mail, one-time tokens, and the pages.

Three epics, one file, because the interesting properties are all in the
seams between them: a token that is claimed but never sent, a page whose
wording leaks whether an account exists, an invitation that marks an
address proven when nothing proved it.

The four acceptance criteria, and where each is checked:

* **a message is never sent twice for one token** —
  ``TestOneEmailPerToken``. The claim is a conditional UPDATE, so a
  double-submitted form spends one of a hundred daily messages rather than
  two, and puts one live credential in an inbox rather than two;
* **absent, used, revoked and expired all refuse identically** —
  ``TestOneAnswerForEveryDeadToken``. Distinguishing them tells somebody
  spraying guesses which tokens were real, and a real token means a real
  account;
* **usable with no provider** — ``TestWithNoProvider``. This is the state
  of every developer checkout and of the deployment until somebody fills
  the dashboard in, so it is a supported mode and not a degraded one;
* **the daily ceiling is respected** — ``TestTheDailyCeiling``. Resend's
  free tier allows 100 a day, and a cap discovered by having mail silently
  dropped is worse than one the product knows about.

**Nothing here talks to a provider.** ``engine.mailer`` has two
provider-specific functions — ``_post_resend`` and ``_send_smtp`` — and the
tests replace one or the other with a recorder, so they run offline and
assert on what would have been sent. What that cannot catch — a wrong
endpoint, a payload Resend rejects, a relay that refuses the envelope — is
not something a mock would catch either. The SMTP tests go one level
deeper and replace ``smtplib`` itself, so the message that gets built is
the thing under test rather than the call that would have sent it.

**Mode.** Authenticated throughout: reset and verify act on accounts, and
accounts do not exist with the flags off.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from app import app as flask_app
from engine import auth as _auth
from engine import db as _db
from engine import mailer as _mailer
from engine import permissions as _perm
from routes import auth as _routes_auth

PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "a completely different passphrase"


@pytest.fixture(autouse=True)
def _authenticated(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    monkeypatch.setitem(flask_app.config, "TESTING", True)
    monkeypatch.setitem(flask_app.config, "WTF_CSRF_ENABLED", False)
    _db.init_db()
    return True


class Outbox:
    """Everything the mailer would have handed to the provider.

    Replaces ``_post_resend`` rather than ``send``, so the code under test
    is the whole of :func:`engine.mailer.send` — the address check, the
    configuration check, the daily count, and the audit row that the count
    is read back from. Patching ``send`` itself would leave all of that
    untested and still make the assertions pass.
    """

    def __init__(self):
        self.messages: list[dict] = []
        self.refuse: _mailer.Delivery | None = None

    def __call__(self, *, to, subject, text, html):
        if self.refuse is not None:
            return self.refuse
        self.messages.append({"to": to, "subject": subject, "text": text})
        return _mailer.SENT

    # — reading —

    def __len__(self) -> int:
        return len(self.messages)

    def to(self, address: str) -> list[dict]:
        return [m for m in self.messages if m["to"] == address]

    def links(self) -> list[str]:
        found = []
        for message in self.messages:
            found += [word for word in message["text"].split()
                      if word.startswith("http")]
        return found

    @property
    def last(self) -> dict:
        assert self.messages, "nothing was sent"
        return self.messages[-1]


def _forget_todays_email() -> None:
    """Reset the daily counter to zero.

    ``sent_today()`` counts audit rows across the whole database, which is
    exactly right in production and shared state in a suite: without this,
    the messages one test sends count against the next test's allowance, and
    the cap tests pass or fail depending on what ran before them.
    """
    with _db.session_scope() as sess:
        sess.query(_db.AuditLog).filter(
            _db.AuditLog.entity == _mailer.AUDIT_ENTITY).delete()


@pytest.fixture
def provider(monkeypatch):
    """A configured provider that records instead of sending."""
    monkeypatch.setenv("RESEND_API_KEY", "re_test_not_a_real_key")
    monkeypatch.setenv("MAIL_FROM", "TestForTge <qa@example.test>")
    monkeypatch.delenv("MAIL_DAILY_LIMIT", raising=False)
    _forget_todays_email()
    outbox = Outbox()
    monkeypatch.setattr(_mailer, "_post_resend", outbox)
    return outbox


@pytest.fixture
def no_provider(monkeypatch):
    """The deployment as it ships today, and every developer checkout."""
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("MAIL_FROM", raising=False)
    _forget_todays_email()
    outbox = Outbox()
    monkeypatch.setattr(_mailer, "_post_resend", outbox)
    return outbox


@pytest.fixture
def quiet_page(monkeypatch):
    """Freeze the CSRF token so two renders of a page compare equal.

    The same fixture, and the same reasoning, as
    ``tests/test_auth_security_vectors.py`` — where it was arrived at by
    measurement rather than by taste. ``base.html`` renders the token into a
    meta tag *and* a hidden input, the token is signed with a timestamp, and
    it reaches the template through three separate registrations, so
    freezing only one of them silently does nothing. Duplicated rather than
    imported because ``tests/`` is not a package.
    """
    import app as _app_module
    from flask_wtf import csrf as _wtf_csrf

    def frozen(*a, **k):
        return "FROZEN-FOR-COMPARISON"

    monkeypatch.setattr(_wtf_csrf, "generate_csrf", frozen)
    monkeypatch.setattr(_app_module, "generate_csrf", frozen)
    monkeypatch.setitem(flask_app.jinja_env.globals, "csrf_token", frozen)
    return True


#: The CSP nonce, minted per request on purpose.
_NONCE = re.compile('nonce="[^"]*"')


def _page(response) -> str:
    return _NONCE.sub('nonce="X"', response.get_data(as_text=True))


@pytest.fixture
def person():
    """An account that can sign in, in a team."""
    tag = secrets.token_hex(5)
    email = f"person-{tag}@example.test"
    uid = _db.create_user(
        email, display_name="A Person",
        password_hash=_auth.hash_password(PASSWORD, email=email),
        email_verified=True)
    org = _db.create_organization(f"Mailers {tag}")
    _db.add_org_member(org, uid, "admin")
    return {"email": email, "user_id": uid, "org_id": org}


@pytest.fixture
def client():
    with flask_app.test_client() as c:
        yield c


def _forgot(client, email: str):
    response = client.post("/auth/forgot", data={"email": email})
    # The dispatch is deliberately off the request path; wait for it rather
    # than sleeping, so the assertions are about what happened and not about
    # how fast this machine is.
    _routes_auth.wait_for_dispatch()
    return response


def _reset_token(user_id: str) -> str | None:
    """The live reset token for a user, read straight from the table.

    Tests that care about the *link* read it out of the outbox instead. This
    is for the ones that need a token without going through a send.
    """
    from sqlalchemy import select
    with _db.session_scope() as sess:
        return sess.execute(
            select(_db.AuthToken.token).where(
                _db.AuthToken.user_id == user_id,
                _db.AuthToken.purpose == "reset",
                _db.AuthToken.used_at.is_(None),
                _db.AuthToken.revoked_at.is_(None))
        ).scalars().first()


# ── E0.4: choosing a transport ───────────────────────────────────────

class FakeSMTP:
    """Stands in for ``smtplib.SMTP``/``SMTP_SSL`` and records the message.

    Replaces the library rather than ``_send_smtp``, so what is under test
    is the message this module builds — the headers above all, since those
    are the part that has a way of going wrong that JSON does not.
    """

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.implicit_tls = context is not None
        self.started_tls = False
        self.login_as: tuple | None = None
        self.messages: list = []
        self.raise_on_login: Exception | None = None
        self.raise_on_send: Exception | None = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, password):
        if self.raise_on_login is not None:
            raise self.raise_on_login
        self.login_as = (user, password)

    def send_message(self, message):
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.messages.append(message)

    # — reading —

    @classmethod
    def reset(cls):
        cls.instances = []

    @classmethod
    def only(cls):
        assert len(cls.instances) == 1, (
            f"expected one connection, saw {len(cls.instances)}")
        return cls.instances[0]

    @classmethod
    def sole_message(cls):
        return cls.only().messages[0]


@pytest.fixture
def smtp(monkeypatch):
    """A configured SMTP transport whose library is a recorder."""
    import smtplib

    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("MAIL_TRANSPORT", raising=False)
    monkeypatch.delenv("SMTP_SECURITY", raising=False)
    monkeypatch.delenv("SMTP_PORT", raising=False)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_USER", "postbox@example.test")
    monkeypatch.setenv("SMTP_PASSWORD", "an app password")
    monkeypatch.setenv("MAIL_FROM", "TestForTge <postbox@example.test>")
    monkeypatch.delenv("MAIL_DAILY_LIMIT", raising=False)
    _forget_todays_email()
    FakeSMTP.reset()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


class TestChoosingATransport:
    """Which one runs, and the rule that protects existing deployments."""

    def test_nothing_configured_is_no_transport(self, monkeypatch):
        for key in ("RESEND_API_KEY", "SMTP_HOST", "MAIL_TRANSPORT"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("MAIL_FROM", "q@example.test")
        assert _mailer.transport() == ""
        assert _mailer.configured() is False

    def test_a_resend_key_alone_still_means_resend(self, monkeypatch):
        # The property that matters most: adding SMTP support must not
        # re-route an instance that already sends.
        monkeypatch.delenv("SMTP_HOST", raising=False)
        monkeypatch.delenv("MAIL_TRANSPORT", raising=False)
        monkeypatch.setenv("RESEND_API_KEY", "re_x")
        monkeypatch.setenv("MAIL_FROM", "q@example.test")
        assert _mailer.transport() == "resend"

    def test_an_smtp_host_alone_means_smtp(self, monkeypatch):
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        monkeypatch.delenv("MAIL_TRANSPORT", raising=False)
        monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
        monkeypatch.setenv("MAIL_FROM", "q@example.test")
        assert _mailer.transport() == "smtp"
        assert _mailer.configured() is True

    def test_with_both_resend_wins_until_told_otherwise(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "re_x")
        monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
        monkeypatch.delenv("MAIL_TRANSPORT", raising=False)
        assert _mailer.transport() == "resend"
        monkeypatch.setenv("MAIL_TRANSPORT", "smtp")
        assert _mailer.transport() == "smtp"

    def test_a_transport_with_no_sender_is_not_configured(self, monkeypatch):
        # The half that gets forgotten, asserted for the new transport too.
        monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
        monkeypatch.delenv("MAIL_FROM", raising=False)
        assert _mailer.configured() is False

    def test_an_unknown_transport_name_falls_back_rather_than_breaking(
            self, monkeypatch):
        monkeypatch.setenv("MAIL_TRANSPORT", "sendmail")
        monkeypatch.setenv("RESEND_API_KEY", "re_x")
        monkeypatch.delenv("SMTP_HOST", raising=False)
        assert _mailer.transport() == "resend"


class TestSendingOverSMTP:
    def test_the_message_reaches_the_relay(self, smtp, person):
        outcome = _mailer.send(to=person["email"], kind="invite",
                               subject="Join the team", text="the link")

        assert outcome.sent is True
        message = smtp.sole_message()
        assert message["To"] == person["email"]
        assert message["Subject"] == "Join the team"
        assert message["From"] == "TestForTge <postbox@example.test>"
        assert "the link" in message.get_content()

    def test_it_authenticates_and_upgrades_the_connection(self, smtp,
                                                          person):
        _mailer.send(to=person["email"], kind="invite", subject="s",
                     text="t")

        connection = smtp.only()
        assert (connection.host, connection.port) == ("smtp.example.test",
                                                      587)
        assert connection.started_tls is True
        assert connection.login_as == ("postbox@example.test",
                                       "an app password")

    def test_implicit_tls_does_not_also_start_tls(self, smtp, person,
                                                  monkeypatch):
        # Port 465 is already encrypted; issuing STARTTLS on it is an error.
        monkeypatch.setenv("SMTP_SECURITY", "ssl")
        monkeypatch.setenv("SMTP_PORT", "465")

        _mailer.send(to=person["email"], kind="invite", subject="s",
                     text="t")

        connection = smtp.only()
        assert connection.port == 465
        assert connection.started_tls is False
        assert connection.implicit_tls is True

    def test_a_relay_that_wants_no_credentials_is_not_given_any(
            self, smtp, person, monkeypatch):
        # An internal relay accepts mail from inside the network; sending
        # AUTH to one that never offered it is an error, not a courtesy.
        monkeypatch.delenv("SMTP_USER", raising=False)
        monkeypatch.delenv("SMTP_PASSWORD", raising=False)

        _mailer.send(to=person["email"], kind="invite", subject="s",
                     text="t")

        assert smtp.only().login_as is None

    def test_a_rejected_password_is_reported_not_raised(self, smtp, person):
        """Sending is never the reason a user's action fails."""
        import smtplib

        def refuse(host, port, timeout=None, context=None):
            connection = FakeSMTP(host, port, timeout, context)
            connection.raise_on_login = smtplib.SMTPAuthenticationError(
                535, b"Username and Password not accepted")
            return connection

        smtp.reset()
        import engine.mailer  # noqa: F401  — patched below via smtplib
        monkey = pytest.MonkeyPatch()
        monkey.setattr(smtplib, "SMTP", refuse)
        try:
            outcome = _mailer.send(to=person["email"], kind="invite",
                                   subject="s", text="t")
        finally:
            monkey.undo()

        assert outcome.sent is False
        assert outcome.reason == "provider_refused"
        assert outcome.needs_fallback is True

    def test_an_unreachable_relay_is_reported_not_raised(self, smtp, person):
        import smtplib

        def unreachable(host, port, timeout=None, context=None):
            raise OSError("connection refused")

        monkey = pytest.MonkeyPatch()
        monkey.setattr(smtplib, "SMTP", unreachable)
        try:
            outcome = _mailer.send(to=person["email"], kind="invite",
                                   subject="s", text="t")
        finally:
            monkey.undo()

        assert outcome.sent is False
        assert outcome.reason == "provider_unreachable"

    def test_a_send_over_smtp_is_counted_against_the_daily_cap(self, smtp,
                                                              person):
        # The count comes from the audit row, so it has to be written on
        # this path too — otherwise the ceiling silently stops applying.
        before = _mailer.sent_today()
        _mailer.send(to=person["email"], kind="invite", subject="s",
                     text="t")
        assert _mailer.sent_today() == before + 1


class TestHeadersCannotBeSmuggled:
    """A subject built from a name somebody typed.

    Under Resend the subject lands in a JSON string and a newline is just a
    newline. Under SMTP the same newline ends ``Subject:`` and starts the
    next header, and the invite subject is
    ``f"You have been invited to {org_name} on TestForTge"``.
    """

    def test_a_newline_in_the_subject_does_not_become_a_header(self, smtp,
                                                               person):
        _mailer.send(
            to=person["email"], kind="invite", text="t",
            subject="Join Acme\r\nBcc: harvester@example.test")

        message = smtp.sole_message()
        assert message["Bcc"] is None
        assert "\n" not in message["Subject"]
        assert "harvester@example.test" in message["Subject"], (
            "flattened, not dropped — the subject still has to read")

    def test_the_flattener_leaves_an_ordinary_subject_alone(self):
        assert _mailer._header_safe("You have been invited to Acme") == \
            "You have been invited to Acme"

    def test_the_address_pattern_does_not_end_at_a_newline(self):
        # strip() in send() is what closes this today; the pattern should
        # not depend on somebody else's strip.
        assert _mailer._PLAUSIBLE.match("a@b.test\n") is None


# ── E0.4: the provider, and life without one ─────────────────────────

class TestTheProvider:
    def test_it_reports_itself_configured_only_with_both_settings(
            self, monkeypatch):
        """A key with no sender address 403s on every send, which reads as a
        broken integration rather than a missing setting."""
        monkeypatch.setenv("RESEND_API_KEY", "re_x")
        monkeypatch.delenv("MAIL_FROM", raising=False)
        assert _mailer.configured() is False
        monkeypatch.setenv("MAIL_FROM", "q@example.test")
        assert _mailer.configured() is True

    def test_a_send_records_the_kind_and_the_address_but_not_the_body(
            self, provider, person):
        """A reset email contains a working credential, and an audit trail is
        read by more people than an inbox is."""
        _mailer.send(to=person["email"], kind="reset", subject="s",
                     text="secret link inside", user_id=person["user_id"])

        rows = [r for r in _db.list_audit(limit=50)
                if r["entity"] == _mailer.AUDIT_ENTITY]
        assert rows, "the send left no record, so the daily count cannot work"
        diff = rows[0]["diff"]
        assert diff["kind"] == "reset"
        assert diff["to"] == person["email"]
        assert "secret link inside" not in str(diff)

    def test_an_implausible_address_is_refused_before_the_provider(
            self, provider):
        for bad in ("", "   ", "nope", "a@b", "two@addresses,here@x.test",
                    "with space@x.test"):
            outcome = _mailer.send(to=bad, kind="reset", subject="s",
                                   text="t")
            assert outcome.sent is False, bad
            assert outcome.reason == "invalid_address", bad
        assert len(provider) == 0

    def test_a_refusal_from_the_provider_is_reported_not_raised(
            self, provider, person):
        """Sending is never the reason a user's action fails."""
        provider.refuse = _mailer.Delivery(False, "provider_refused",
                                           "HTTP 403")
        outcome = _mailer.send(to=person["email"], kind="reset", subject="s",
                               text="t")
        assert outcome.sent is False
        assert outcome.needs_fallback is True

    def test_the_log_redacts_the_address(self):
        assert _mailer._redact("someone@example.test") == "s***@example.test"
        assert "@" in _mailer._redact("x@y.test")


class TestWithNoProvider:
    """The supported mode, not a degraded one."""

    def test_send_declines_with_a_reason_rather_than_raising(self, no_provider,
                                                            person):
        outcome = _mailer.send(to=person["email"], kind="reset", subject="s",
                              text="t")
        assert outcome.sent is False
        assert outcome.reason == "not_configured"
        assert outcome.needs_fallback is True

    def test_inviting_still_works_and_hands_over_the_link(self, no_provider,
                                                          person, client):
        """The pre-E0.4 behaviour, kept as the fallback rather than replaced
        by it."""
        with client.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = person["user_id"]
            sess[_perm.SESSION_ORG_KEY] = person["org_id"]
        invitee = f"newcomer-{secrets.token_hex(4)}@example.test"

        response = client.post("/org/members/invite",
                               data={"email": invitee, "role": "user"},
                               follow_redirects=True)
        body = response.get_data(as_text=True)

        assert "/auth/accept/" in body, "no link to send by hand"
        assert "cannot send email yet" in body
        assert invitee in {i["email"]
                           for i in _db.list_pending_invites(person["org_id"])}
        assert len(no_provider) == 0

    def test_the_reset_page_says_the_link_will_not_arrive(self, no_provider,
                                                          client):
        """Told to everybody who asks, because telling only real accounts
        would be the enumeration leak again. It is a fact about the
        instance, not about the address."""
        body = _forgot(client, "anyone@example.test").get_data(as_text=True)
        assert "cannot send email yet" in body

    def test_asking_to_confirm_an_address_says_it_is_impossible(
            self, no_provider, client, person):
        _db.set_user_active(person["user_id"], True)
        with _db.session_scope() as sess:
            sess.get(_db.User, person["user_id"]).email_verified = False
        with client.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = person["user_id"]
            sess[_perm.SESSION_ORG_KEY] = person["org_id"]

        response = client.post("/auth/verify/request", follow_redirects=True)

        assert "cannot send email yet" in response.get_data(as_text=True)
        assert len(no_provider) == 0


class TestTheDailyCeiling:
    def test_the_default_is_the_free_tier_limit(self, monkeypatch):
        monkeypatch.delenv("MAIL_DAILY_LIMIT", raising=False)
        assert _mailer.daily_limit() == 100 == _mailer.DEFAULT_DAILY_LIMIT

    def test_past_the_cap_sending_stops_with_a_reason(self, provider, person,
                                                     monkeypatch):
        monkeypatch.setenv("MAIL_DAILY_LIMIT", "2")
        for _ in range(2):
            assert _mailer.send(to=person["email"], kind="invite",
                                subject="s", text="t").sent is True

        blocked = _mailer.send(to=person["email"], kind="invite",
                               subject="s", text="t")

        assert blocked.sent is False
        assert blocked.reason == "daily_cap"
        assert len(provider) == 2, "the cap did not stop the third send"

    def test_zero_switches_sending_off_without_removing_the_key(
            self, provider, person, monkeypatch):
        monkeypatch.setenv("MAIL_DAILY_LIMIT", "0")
        outcome = _mailer.send(to=person["email"], kind="invite", subject="s",
                               text="t")
        assert outcome.reason == "sending_disabled"
        assert len(provider) == 0

    def test_the_count_is_a_rolling_window_not_a_calendar_day(self, provider,
                                                             person):
        """The provider's limit resets on its own clock in its own timezone;
        a local midnight would let a burst straddle the boundary."""
        _mailer.send(to=person["email"], kind="invite", subject="s", text="t")
        assert _mailer.sent_today() == 1

        # Age the record past the window.
        with _db.session_scope() as sess:
            for row in sess.query(_db.AuditLog).filter(
                    _db.AuditLog.entity == _mailer.AUDIT_ENTITY).all():
                row.at = datetime.now(timezone.utc) - timedelta(hours=25)

        assert _mailer.sent_today() == 0

    def test_an_unreadable_limit_falls_back_rather_than_blocking_mail(
            self, monkeypatch):
        monkeypatch.setenv("MAIL_DAILY_LIMIT", "lots")
        assert _mailer.daily_limit() == _mailer.DEFAULT_DAILY_LIMIT


# ── E1.3: the tokens ─────────────────────────────────────────────────

class TestOneEmailPerToken:
    """The acceptance criterion, and the ordinary way it gets broken."""

    def test_the_right_to_send_is_claimable_once(self, person):
        token = secrets.token_urlsafe(32)
        assert _db.create_auth_token("reset", person["user_id"],
                                     person["email"], token)
        assert _db.claim_auth_token_send(token) is True
        assert _db.claim_auth_token_send(token) is False

    def test_a_double_submitted_form_sends_one_message(self, provider,
                                                       person, client):
        """Somebody clicks "email me a link", nothing appears to move, so
        they click again. Two tokens are issued — the second revokes the
        first — but each spends exactly one message, and the older link is
        dead rather than a second live credential in the inbox."""
        _forgot(client, person["email"])
        _forgot(client, person["email"])

        assert len(provider.to(person["email"])) == 2, (
            "each request should send its own link")
        links = provider.links()
        first_token = links[0].rsplit("/", 1)[-1]
        # The first link no longer works: create_auth_token revokes the live
        # one, so the address has exactly one usable reset link at a time.
        assert _db.get_auth_token(first_token, "reset") is None
        assert _db.get_auth_token(links[1].rsplit("/", 1)[-1], "reset")

    def test_a_send_is_claimed_before_the_provider_is_called(self, provider,
                                                            person):
        """Ordering, and it is deliberate: a send that happens twice has
        spent two of a hundred daily messages and put two live credentials
        in an inbox, while a claim never followed by a send costs one
        retry."""
        provider.refuse = _mailer.Delivery(False, "provider_unreachable")
        token = secrets.token_urlsafe(32)
        _db.create_auth_token("reset", person["user_id"], person["email"],
                              token)
        assert _db.claim_auth_token_send(token) is True
        _mailer.send(to=person["email"], kind="reset", subject="s", text="t")
        # Still claimed, even though nothing was delivered.
        assert _db.claim_auth_token_send(token) is False


class TestOneAnswerForEveryDeadToken:
    """Absent, used, revoked, expired — one answer.

    "Expired" confirms the token was real, and a real token means a real
    account. So the four cases are indistinguishable from outside, and this
    is asserted at both levels: the helper returns ``None`` for all of them,
    and the page returns the same status and the same words.
    """

    def _make(self, person, **kw) -> str:
        token = secrets.token_urlsafe(32)
        assert _db.create_auth_token("reset", person["user_id"],
                                     person["email"], token, **kw)
        return token

    def test_the_helper_gives_nothing_away(self, person):
        never = secrets.token_urlsafe(32)

        used = self._make(person)
        _db.consume_auth_token(used, "reset")

        revoked = self._make(person)
        _db.revoke_auth_tokens(person["user_id"])

        expired = self._make(person, ttl_minutes=1)
        with _db.session_scope() as sess:
            sess.get(_db.AuthToken, expired).expires_at = (
                datetime.now(timezone.utc) - timedelta(minutes=5))

        for token in (never, used, revoked, expired):
            assert _db.get_auth_token(token, "reset") is None
            assert _db.consume_auth_token(token, "reset") is None

    def test_the_page_gives_nothing_away(self, person, client,
                                        quiet_page):
        never = secrets.token_urlsafe(32)
        used = self._make(person)
        _db.consume_auth_token(used, "reset")

        bodies, statuses = set(), set()
        for token in (never, used):
            response = client.get(f"/auth/reset/{token}")
            statuses.add(response.status_code)
            bodies.add(_page(response))

        assert statuses == {410}, statuses
        assert len(bodies) == 1, (
            "the page differs between a token that never existed and one "
            "that was used, which tells a guesser which tokens were real")

    def test_a_token_is_not_valid_for_the_other_purpose(self, person):
        """A verify link grants nothing; a reset link grants the account.
        Honouring one as the other would turn the cheap token into the
        expensive one."""
        token = secrets.token_urlsafe(32)
        _db.create_auth_token("verify", person["user_id"], person["email"],
                              token)
        assert _db.get_auth_token(token, "reset") is None
        assert _db.consume_auth_token(token, "reset") is None
        assert _db.get_auth_token(token, "verify") is not None

    def test_a_used_token_cannot_be_claimed_twice_by_two_callers(self,
                                                                person):
        token = secrets.token_urlsafe(32)
        _db.create_auth_token("reset", person["user_id"], person["email"],
                              token)
        assert _db.consume_auth_token(token, "reset") is not None
        assert _db.consume_auth_token(token, "reset") is None


# ── E1.7: the pages ──────────────────────────────────────────────────

class TestAskingForAReset:
    def test_the_answer_never_depends_on_what_was_found(self, provider,
                                                        person, client,
                                                        quiet_page):
        """The enumeration property, asserted on the whole page.

        ``engine.auth`` burns a dummy Argon2 hash to close this on the
        sign-in page. Reopening it one route along — where no password is
        needed, so it is easier to spray — would be a strange way to lose
        it.
        """
        real = client.post("/auth/forgot", data={"email": person["email"]})
        ghost = client.post("/auth/forgot",
                            data={"email": f"nobody-{secrets.token_hex(4)}"
                                           f"@example.test"})
        _routes_auth.wait_for_dispatch()

        assert real.status_code == ghost.status_code == 200
        assert _page(real) == _page(ghost), (
            "the page differs between an address with an account and one "
            "without, so the pair of responses enumerates accounts")

    def test_an_implausible_address_gets_the_same_page_too(self, provider,
                                                           person, client,
                                                           quiet_page):
        """"That is not an email address" would be fair and is also a free
        oracle: any response that varies with the input teaches a script
        something."""
        real = client.post("/auth/forgot", data={"email": person["email"]})
        junk = client.post("/auth/forgot", data={"email": "not-an-address"})
        _routes_auth.wait_for_dispatch()
        assert _page(real) == _page(junk)

    def test_a_real_address_gets_a_working_link(self, provider, person,
                                                client):
        _forgot(client, person["email"])

        assert len(provider.to(person["email"])) == 1
        link = provider.links()[0]
        assert "/auth/reset/" in link
        # Follow it the way the recipient would.
        assert client.get(link).status_code == 200

    def test_the_link_it_builds_resolves_to_the_route_it_names(self):
        """``_link`` spells the path out rather than reversing it from the
        endpoint, which couples it to the ``@app.route`` decorator. This is
        the check that stops the literal drifting out of step."""
        built = _routes_auth._link("http://x.test/", "/auth/reset/TOKEN")
        adapter = flask_app.url_map.bind("x.test")
        endpoint, args = adapter.match("/auth/reset/TOKEN")
        assert built == "http://x.test/auth/reset/TOKEN"
        assert endpoint == "auth_reset" and args == {"token": "TOKEN"}

        built = _routes_auth._link("http://x.test/", "/auth/verify/TOKEN")
        endpoint, args = adapter.match("/auth/verify/TOKEN")
        assert endpoint == "auth_verify" and args == {"token": "TOKEN"}

    def test_a_google_only_account_gets_no_reset_link(self, provider,
                                                      client):
        """Issuing one would let anyone holding the address convert an OIDC
        account into a password account — a way in, not a recovery."""
        email = f"google-{secrets.token_hex(4)}@example.test"
        _db.create_user(email, email_verified=True)   # no password_hash

        _forgot(client, email)

        assert len(provider.to(email)) == 0

    def test_a_deactivated_account_gets_no_reset_link(self, provider, person,
                                                      client):
        _db.set_user_active(person["user_id"], False)
        _forgot(client, person["email"])
        assert len(provider.to(person["email"])) == 0

    def test_a_signed_in_caller_is_sent_away(self, provider, person, client):
        """They can change their password from settings; emailing a live
        credential to solve a problem they do not have is not a service."""
        with client.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = person["user_id"]
            sess[_perm.SESSION_ORG_KEY] = person["org_id"]
        response = client.get("/auth/forgot", follow_redirects=False)
        assert response.status_code in (302, 303)

    def test_the_sign_in_page_offers_the_way_in(self, client):
        """Somebody who cannot sign in is looking at that screen and no
        other, so the link has to be on it."""
        body = client.get("/auth/login").get_data(as_text=True)
        assert "/auth/forgot" in body


class TestSettingANewPassword:
    def _link_for(self, provider, person, client) -> str:
        _forgot(client, person["email"])
        return provider.links()[-1]

    def test_the_new_password_works_and_the_old_one_does_not(self, provider,
                                                             person, client):
        link = self._link_for(provider, person, client)

        response = client.post(link, data={"password": NEW_PASSWORD,
                                           "password_confirm": NEW_PASSWORD},
                               follow_redirects=False)

        assert response.status_code in (302, 303), response.status_code
        assert _auth.verify_login(person["email"], NEW_PASSWORD).ok is True
        assert _auth.verify_login(person["email"], PASSWORD).ok is False

    def test_the_link_is_spent(self, provider, person, client):
        link = self._link_for(provider, person, client)
        client.post(link, data={"password": NEW_PASSWORD,
                                "password_confirm": NEW_PASSWORD})
        assert client.get(link).status_code == 410

    def test_a_get_does_not_spend_the_link(self, provider, person, client):
        """Some mail clients fetch every URL in a message to build a
        preview. Consuming on GET would burn the link before the user ever
        saw the page."""
        link = self._link_for(provider, person, client)

        assert client.get(link).status_code == 200
        assert client.get(link).status_code == 200

        assert client.post(link, data={"password": NEW_PASSWORD,
                                       "password_confirm": NEW_PASSWORD}
                           ).status_code in (302, 303)

    def test_a_mismatched_confirmation_does_not_spend_the_link(self, provider,
                                                               person,
                                                               client):
        """Otherwise a typo sends the user back to their inbox for a new
        link, which on a capped free tier also costs a message."""
        link = self._link_for(provider, person, client)

        bad = client.post(link, data={"password": NEW_PASSWORD,
                                      "password_confirm": "something else"})

        assert bad.status_code == 400
        assert client.get(link).status_code == 200, "the link was burned"
        assert client.post(link, data={"password": NEW_PASSWORD,
                                       "password_confirm": NEW_PASSWORD}
                           ).status_code in (302, 303)

    def test_a_weak_password_is_refused_and_changes_nothing(self, provider,
                                                            person, client):
        link = self._link_for(provider, person, client)
        response = client.post(link, data={"password": "short",
                                           "password_confirm": "short"})
        assert response.status_code == 400
        assert _auth.verify_login(person["email"], PASSWORD).ok is True

    def test_a_reset_ends_every_other_session(self, provider, person,
                                              client):
        """A reset is what somebody does when they think the account is
        compromised. Leaving the intruder's cookie working would make the
        whole exercise ceremony."""
        intruder = flask_app.test_client()
        with intruder.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = person["user_id"]
            sess[_perm.SESSION_ORG_KEY] = person["org_id"]
        _db.session_save(
            "a-live-session-row",
            '{"_user_id": "%s"}' % person["user_id"],
            datetime.now(timezone.utc) + timedelta(hours=1),
            user_id=person["user_id"])

        link = self._link_for(provider, person, client)
        client.post(link, data={"password": NEW_PASSWORD,
                                "password_confirm": NEW_PASSWORD})

        assert _db.session_load("a-live-session-row") in (None, "")

    def test_a_reset_cancels_the_other_live_links(self, provider, person,
                                                  client):
        """Any reset link still in flight was issued to whoever asked for
        it."""
        _forgot(client, person["email"])
        first = provider.links()[-1]
        _forgot(client, person["email"])
        second = provider.links()[-1]
        assert first != second

        client.post(second, data={"password": NEW_PASSWORD,
                                  "password_confirm": NEW_PASSWORD})

        assert client.get(first).status_code == 410
        assert client.get(second).status_code == 410

    def test_a_reset_clears_the_lockout(self, provider, person, client):
        """Proving control of the inbox is a stronger claim than five wrong
        guesses, and leaving them locked out afterwards is a support
        ticket."""
        for _ in range(_auth.MAX_FAILED_LOGINS):
            _auth.verify_login(person["email"], "wrong password here")
        assert _auth.verify_login(person["email"], PASSWORD).reason == "locked"

        link = self._link_for(provider, person, client)
        client.post(link, data={"password": NEW_PASSWORD,
                                "password_confirm": NEW_PASSWORD})

        assert _auth.verify_login(person["email"], NEW_PASSWORD).ok is True

    def test_the_reset_is_recorded(self, provider, person, client):
        link = self._link_for(provider, person, client)
        client.post(link, data={"password": NEW_PASSWORD,
                                "password_confirm": NEW_PASSWORD})
        actions = {r["action"] for r in _db.list_audit(limit=50)
                   if r["user_id"] == person["user_id"]}
        assert "password_reset" in actions


# ── E1.3: proving an address, and who needs to ───────────────────────

class TestAnEmailedInviteProvesTheAddress:
    """The trap this epic is warned about, and the reason ``emailed_at``
    exists.

    The invite-acceptance route used to mark every new account's address
    verified, reasoning that only the inbox's reader could have opened the
    link. That is true of an invitation we *emailed* — and while no
    provider existed it was false every single time, because the admin
    pasted the link into a chat. So the flag now follows the delivery.
    """

    def _invite(self, client, person, invitee: str) -> str:
        """Invite *invitee* and return the acceptance token.

        Taken from the link on the page rather than from
        ``list_pending_invites``, which omits the token deliberately — a
        pending invite's token is a bearer credential for somebody else's
        seat and has no business rendering on the members page. The admin's
        own flash is where it legitimately appears.
        """
        with client.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = person["user_id"]
            sess[_perm.SESSION_ORG_KEY] = person["org_id"]
        body = client.post("/org/members/invite",
                           data={"email": invitee, "role": "user"},
                           follow_redirects=True).get_data(as_text=True)
        marker = "/auth/accept/"
        assert marker in body, "the admin was given no link"
        return body.split(marker, 1)[1].split("<")[0].split()[0].strip()

    def _accept(self, invitee: str, token: str):
        fresh = flask_app.test_client()
        return fresh.post(f"/auth/accept/{token}",
                          data={"display_name": "Newcomer",
                                "password": PASSWORD,
                                "password_confirm": PASSWORD},
                          follow_redirects=True)

    def test_an_emailed_invite_marks_the_invitation_delivered(self, provider,
                                                              person, client):
        invitee = f"emailed-{secrets.token_hex(4)}@example.test"
        token = self._invite(client, person, invitee)

        assert len(provider.to(invitee)) == 1
        assert _db.get_invite(token)["emailed_at"] is not None

    def test_an_account_from_an_emailed_invite_needs_no_confirmation(
            self, provider, person, client):
        """The free-tier trap: do not send a confirmation where the address
        is already proven. 100 messages a day is the whole budget."""
        invitee = f"proven-{secrets.token_hex(4)}@example.test"
        token = self._invite(client, person, invitee)
        before = len(provider)

        self._accept(invitee, token)

        user = _db.get_user_by_email(invitee)
        assert user["email_verified"] is True
        assert len(provider) == before, (
            "a confirmation was sent to an address the invitation already "
            "proved — on a 100-a-day tier that is a message spent on nothing")

    def test_an_account_from_a_hand_delivered_invite_is_not_proven(
            self, no_provider, person, client):
        """Nothing established this address. Recording it as verified would
        be storing an assumption as a fact — which is what the code did
        before E0.4, on every single account."""
        invitee = f"byhand-{secrets.token_hex(4)}@example.test"
        token = self._invite(client, person, invitee)
        assert _db.get_invite(token)["emailed_at"] is None

        self._accept(invitee, token)

        user = _db.get_user_by_email(invitee)
        assert user["email_verified"] is False


class TestConfirmingAnAddress:
    def _unverified(self, person, client):
        with _db.session_scope() as sess:
            sess.get(_db.User, person["user_id"]).email_verified = False
        with client.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = person["user_id"]
            sess[_perm.SESSION_ORG_KEY] = person["org_id"]

    def test_an_unconfirmed_account_is_offered_the_way_to_fix_it(
            self, provider, person, client):
        """The caller the verify flow would otherwise not have. A mechanism
        with no trigger is the defect shape this programme keeps meeting."""
        self._unverified(person, client)
        body = client.get("/").get_data(as_text=True)
        assert "/auth/verify/request" in body
        assert "not confirmed" in body

    def test_a_confirmed_account_is_not_nagged(self, provider, person,
                                              client):
        with client.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = person["user_id"]
            sess[_perm.SESSION_ORG_KEY] = person["org_id"]
        assert "/auth/verify/request" not in client.get("/").get_data(
            as_text=True)

    def test_asking_sends_a_link_that_confirms(self, provider, person,
                                              client):
        self._unverified(person, client)

        client.post("/auth/verify/request", follow_redirects=True)
        _routes_auth.wait_for_dispatch()

        link = [l for l in provider.links() if "/auth/verify/" in l][-1]
        assert client.get(link).status_code == 200
        assert _db.get_user(person["user_id"])["email_verified"] is True

    def test_the_link_works_once(self, provider, person, client):
        self._unverified(person, client)
        client.post("/auth/verify/request", follow_redirects=True)
        _routes_auth.wait_for_dispatch()
        link = [l for l in provider.links() if "/auth/verify/" in l][-1]

        assert client.get(link).status_code == 200
        assert client.get(link).status_code == 410

    def test_a_link_whose_address_has_since_changed_confirms_nothing(
            self, provider, person, client):
        """A verify token proves the address it was **issued for**. Marking
        a newer, unproven address confirmed is the one thing this flag must
        never say falsely."""
        self._unverified(person, client)
        client.post("/auth/verify/request", follow_redirects=True)
        _routes_auth.wait_for_dispatch()
        link = [l for l in provider.links() if "/auth/verify/" in l][-1]

        with _db.session_scope() as sess:
            sess.get(_db.User, person["user_id"]).email = \
                f"moved-{secrets.token_hex(4)}@example.test"

        assert client.get(link).status_code == 410
        assert _db.get_user(person["user_id"])["email_verified"] is False

    def test_an_already_confirmed_caller_is_told_so_and_no_mail_goes(
            self, provider, person, client):
        with client.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = person["user_id"]
            sess[_perm.SESSION_ORG_KEY] = person["org_id"]
        before = len(provider)
        response = client.post("/auth/verify/request", follow_redirects=True)
        assert "already confirmed" in response.get_data(as_text=True)
        assert len(provider) == before


# ── The harness ──────────────────────────────────────────────────────

class TestTheHarnessWouldNotice:
    def test_the_run_is_authenticated(self):
        assert _perm.auth_active() and _perm.org_active()

    def test_the_provider_fixture_really_configures_one(self, provider):
        assert _mailer.configured() is True

    def test_the_no_provider_fixture_really_removes_it(self, no_provider):
        assert _mailer.configured() is False

    def test_nothing_reaches_the_real_resend(self, provider, person):
        """The recorder replaces the one provider-specific function, so a
        test that started making real HTTPS calls would be visible here."""
        assert _mailer._post_resend is provider
        _mailer.send(to=person["email"], kind="reset", subject="s", text="t")
        assert len(provider) == 1

    def test_the_dispatch_is_waited_for_rather_than_slept_on(self, provider,
                                                             person, client):
        _forgot(client, person["email"])
        assert len(provider.to(person["email"])) == 1, (
            "wait_for_dispatch returned before the send happened, so every "
            "assertion about an outbox in this file is a race")
