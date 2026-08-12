"""DATABASE_URL: what a person actually pastes, and what must never be logged.

Both halves of this file come from one incident, 2026-08-12. Staging was
given a Neon connection string through the Render dashboard and the value
arrived without its ``postgresql://`` prefix — the consoles show the URI with
the scheme in front, and selecting the visible text by hand can start at the
role name.

What happened next is the part worth testing:

1. ``create_engine`` raised ``ArgumentError``, ``init_db`` never published an
   engine, and **every page answered 500** — while ``/healthz`` kept
   answering 200, because it is deliberately DB-free so a database outage
   cannot make Render stop routing traffic. So the dashboard said the
   service was Live and the product was completely down.
2. SQLAlchemy's ``ArgumentError`` message **quotes the string it could not
   parse**. That message went into the log verbatim, so the database
   password sat in Render's log viewer — and then in a screenshot of it.

A credential in a log outlives the outage that produced it: the outage ends
when the value is fixed, the log line ends when somebody rotates the
password. So the rule this file enforces is absolute — **no code path may
put a connection string into a message**, whether it succeeds, warns, or
refuses.
"""
from __future__ import annotations

import logging

import pytest
from sqlalchemy.engine import url as sa_url

from engine import db as _db


SECRET = "npg_notTheRealPassword_9x"
HOST = "ep-cool-math-b1zrjtom-pooler.c-5.eu-central-1.aws.neon.tech"
QUERY = "?sslmode=require&channel_binding=require"

#: Exactly what the Render environment held during the incident.
AS_PASTED = f"neondb_owner:{SECRET}@{HOST}/neondb{QUERY}"
CORRECT = f"postgresql://{AS_PASTED}"


@pytest.fixture
def env(monkeypatch):
    """Set DATABASE_URL and nothing else — TESTFORTGE_DB would win the
    fallback and hide what is being tested."""
    def _set(value: str):
        monkeypatch.setenv("DATABASE_URL", value)
        monkeypatch.delenv("TESTFORTGE_DB", raising=False)
        return _db.database_url()
    return _set


class TestTheStringPeopleActuallyPaste:

    def test_the_correct_uri_resolves(self, env):
        parsed = sa_url.make_url(env(CORRECT))
        assert parsed.drivername == "postgresql+psycopg2"
        assert parsed.host == HOST
        assert parsed.database == "neondb"

    def test_the_query_parameters_survive(self, env):
        """Neon needs ``sslmode=require``; it also hands out
        ``channel_binding=require``, which libpq 15+ understands. Dropping
        either during normalisation would fail at connect time, far from
        here."""
        parsed = sa_url.make_url(env(CORRECT))
        assert parsed.query.get("sslmode") == "require"
        assert parsed.query.get("channel_binding") == "require"

    def test_a_missing_scheme_is_repaired_rather_than_refused(self, env):
        """The incident value. Postgres is the only non-SQLite backend this
        app supports, so ``user:pass@host/db`` is unambiguous — and the
        alternative measured itself: 500 on every page while the platform
        reported healthy."""
        parsed = sa_url.make_url(env(AS_PASTED))
        assert parsed.drivername == "postgresql+psycopg2"
        assert parsed.host == HOST
        assert parsed.database == "neondb"

    def test_the_repair_says_what_it_assumed(self, env, caplog):
        with caplog.at_level(logging.WARNING, logger="engine.db"):
            env(AS_PASTED)
        warnings = [r.getMessage() for r in caplog.records
                    if r.levelno >= logging.WARNING]
        assert any("no scheme" in w for w in warnings), warnings
        assert any("postgresql://" in w for w in warnings), (
            "the warning has to name the fix, or the operator leaves the "
            "variable in a shape that only works by guess")

    def test_legacy_postgres_scheme_still_works(self, env):
        """Render and Heroku hand out ``postgres://``."""
        parsed = sa_url.make_url(env(f"postgres://neondb_owner:{SECRET}@{HOST}/neondb"))
        assert parsed.drivername == "postgresql+psycopg2"

    def test_an_empty_value_falls_back_instead_of_raising(self, env):
        """A `sync: false` key exists with no value until somebody fills it,
        so empty has to read as absent — see test_render_blueprint.py."""
        assert env("   ").startswith("sqlite:")


class TestTheMistakesGetNamed:
    """Each message is what an operator reads at the moment they are stuck,
    so each one says which mistake this is and what to do."""

    def test_a_psql_command_is_recognised(self, env):
        with pytest.raises(RuntimeError, match="psql"):
            env(f"psql 'postgresql://neondb_owner:{SECRET}@{HOST}/neondb'")

    def test_quotes_around_the_value_are_recognised(self, env):
        """The dashboard stores the value literally, so a stray quote becomes
        part of the host name and the error would otherwise be a DNS
        failure for a host with an apostrophe in it."""
        with pytest.raises(RuntimeError, match="quotes"):
            env(f"'postgresql://neondb_owner:{SECRET}@{HOST}/neondb'")

    def test_something_that_is_not_a_url_at_all(self, env):
        with pytest.raises(RuntimeError, match="no scheme"):
            env("my-database")

    def test_the_refusal_happens_before_sqlalchemy_sees_it(self, env):
        """Deliberate ordering: SQLAlchemy's own ArgumentError quotes the
        whole string, so letting it raise is the leak. Ours raises first."""
        with pytest.raises(RuntimeError) as caught:
            env("my-database")
        assert "ArgumentError" not in str(caught.value)


class TestNoPathLeaksTheCredential:
    """The rule with no exceptions."""

    BAD_VALUES = (
        AS_PASTED,
        f"psql 'postgresql://neondb_owner:{SECRET}@{HOST}/neondb'",
        f"'postgresql://neondb_owner:{SECRET}@{HOST}/neondb'",
        f"postgresql://neondb_owner:{SECRET}@{HOST}:notaport/neondb",
    )

    @pytest.mark.parametrize("value", BAD_VALUES)
    def test_neither_the_error_nor_the_log_contains_the_password(
            self, env, caplog, value):
        with caplog.at_level(logging.DEBUG, logger="engine.db"):
            try:
                env(value)
            except RuntimeError as exc:
                assert SECRET not in str(exc), (
                    "the password is in the exception message, and every "
                    "traceback of it lands in the log")
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert SECRET not in logged, (
            "the password is in a log line — this is the leak the whole "
            "file exists for, and it outlives the outage")

    def test_redaction_keeps_what_is_useful(self):
        redacted = _db.redact_url(CORRECT)
        assert SECRET not in redacted
        assert "neondb_owner" not in redacted, "the role name is a credential half"
        assert HOST in redacted, (
            "a redaction that hides the host too is useless for diagnosis — "
            "'wrong region' and 'wrong project' are read off the host")
        assert "sslmode" not in redacted, "query parameters stay out of logs"

    def test_redaction_handles_a_value_with_no_credentials(self):
        assert _db.redact_url("sqlite:////tmp/x.db") == "sqlite:////tmp/x.db"
        assert _db.redact_url("") == "(empty)"

    def test_redaction_handles_the_schemeless_shape(self):
        redacted = _db.redact_url(AS_PASTED)
        assert SECRET not in redacted and HOST in redacted
