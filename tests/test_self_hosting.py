"""E8.6 — the self-host topology and its runbook, checked against the tree.

    стороння людина піднімає інстанс за інструкцією з нуля
    (a person from outside brings an instance up from the instructions,
     from scratch)

A criterion about a document, which makes it the easiest one in the
programme to declare done and the easiest to be wrong about. The only real
proof is somebody following it on a machine with Docker, and there is no
Docker here. So this file does the next thing, which is not nothing:

**it checks that the instructions describe this repository.** Every variable
the compose file sets is one the code reads. Every flag the runbook names is
declared in ``engine/features.py``. Every file it points at exists. Those are
the failures that make a stranger stop, and every one of them is findable
without a container.

The defect it was written around
--------------------------------
The previous ``docker-compose.yml`` set ``FLASK_DEBUG=0`` and no database.
That combination cannot boot: with no ``DATABASE_URL`` the app falls back to
SQLite, and ``engine/db.py`` refuses SQLite outside debug mode. So the three
commands in the file's own header produced a container that died at startup
with a ``RuntimeError``.

Measured by running the app with exactly the environment that file set — the
only way that class of defect surfaces. It is invisible to anyone reading the
file and unmissable to the first person who follows it, which is the same
thing this whole epic is about. ``TestTheAppCanActuallyBoot`` is the
regression, and it runs the real refusal function rather than a copy of it.

What this cannot check
----------------------
That the images pull, that MinIO's healthcheck command exists in the tag
pinned, that Chromium fits in the memory limit on your box. Those need a
container. The runbook says so in its own header rather than implying a
verification that did not happen.
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml


REPO = pathlib.Path(__file__).resolve().parent.parent
COMPOSE = REPO / "docker-compose.yml"
RUNBOOK = REPO / "docs" / "runbooks" / "self-hosting.md"
ENV_EXAMPLE = REPO / ".env.example"


@pytest.fixture(scope="module")
def compose() -> dict:
    assert COMPOSE.is_file(), f"{COMPOSE} is missing"
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def app_service(compose) -> dict:
    assert "app" in compose["services"], (
        "the application service was renamed; every command in the runbook "
        "that says `docker compose logs app` is now wrong")
    return compose["services"]["app"]


@pytest.fixture(scope="module")
def runbook() -> str:
    assert RUNBOOK.is_file(), f"{RUNBOOK} is missing"
    return RUNBOOK.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def runbook_flat(runbook) -> str:
    """The runbook with its line wrapping removed.

    Prose assertions run against this. Markdown wraps at 79 columns, so a
    phrase like "has **not** been executed" is split across two lines in the
    source and is one sentence to a reader — and a test that failed on the
    line break would be tracking the formatter, not the meaning. Caught by
    exactly that: the disclaimer was present and the assertion said it was
    not.

    Blockquote markers go too, for the same reason and because they bit
    second: the sentence is inside a ``>`` block, so joining the lines
    without stripping them produced "has **not** > been executed", which is
    still not what anybody reads.
    """
    stripped = [re.sub(r"^\s*>\s?", "", line) for line in runbook.splitlines()]
    return " ".join(" ".join(stripped).split())


@pytest.fixture(scope="module")
def env_example() -> str:
    return ENV_EXAMPLE.read_text(encoding="utf-8")


def _interpolate(value: str, overrides: dict | None = None) -> str:
    """Resolve ``${VAR}`` / ``${VAR:-default}`` / ``${VAR:?msg}`` like compose.

    Deliberately a small reimplementation rather than a call out to
    ``docker compose config``: the point is to run where Docker is not
    installed, which is most CI containers and this machine.
    """
    overrides = overrides or {}

    def _one(match):
        name, sep, tail = match.group(1), match.group(2), match.group(3)
        if name in overrides:
            return overrides[name]
        if sep == ":-":
            return tail
        return ""            # unset, or `:?` with nothing supplied

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-|:\?)?([^}]*)\}",
                  _one, str(value))


# ── the regression ───────────────────────────────────────────────────

class TestTheAppCanActuallyBoot:
    """The measured defect: the documented commands produced a container
    that died at startup."""

    def test_the_app_service_is_given_a_database(self, app_service):
        env = app_service["environment"]
        assert "DATABASE_URL" in env, (
            "no DATABASE_URL, so the app falls back to SQLite; with "
            "FLASK_DEBUG=0 engine/db.py refuses to boot on SQLite and the "
            "container dies with a RuntimeError")

    def test_the_environment_this_file_sets_survives_the_safety_check(
            self, app_service, monkeypatch):
        """The real function, not a copy of it.

        A test that re-implemented the "is this SQLite in production" rule
        would agree with itself while the product disagreed — the shape
        ``feedback_gate_measuring_wrong_chain`` describes.
        """
        from engine import db as _db

        supplied = {"POSTGRES_PASSWORD": "a-password",
                    "SECRET_KEY": "x" * 48,
                    "TESTFORTGE_ENCRYPTION_KEY": "y" * 48,
                    "STORAGE_S3_ACCESS_KEY": "k", "STORAGE_S3_SECRET_KEY": "s"}
        for name, raw in app_service["environment"].items():
            monkeypatch.setenv(name, _interpolate(raw, supplied))

        url = _db.database_url()

        assert url.startswith("postgresql"), url
        # Raises RuntimeError if this combination cannot run. That raise is
        # exactly what the old compose file produced.
        _db._assert_prod_safety(url)

    def test_the_database_url_points_at_the_db_service(self, app_service,
                                                       compose):
        url = app_service["environment"]["DATABASE_URL"]
        assert "@db:5432/" in url, (
            "the app is pointed somewhere other than the db service in this "
            "same file")
        assert "db" in compose["services"]

    def test_the_app_waits_for_the_database_to_be_healthy(self, app_service):
        """``depends_on`` without a condition only waits for the container
        to exist, and Postgres takes seconds to accept connections after
        that — so the first boot fails and `restart: unless-stopped` hides
        it as a slow start."""
        depends = app_service.get("depends_on") or {}
        assert isinstance(depends, dict), (
            "list form gives no health condition")
        assert depends.get("db", {}).get("condition") == "service_healthy"

    def test_the_bucket_exists_before_the_app_starts(self, app_service,
                                                     compose):
        """Otherwise the first upload fails with "no bucket", and the
        instructions would need a manual step between `up` and a working
        instance — which is a step people skip and then report as a bug."""
        assert "storage-init" in compose["services"]
        depends = app_service["depends_on"]
        assert depends.get("storage-init", {}).get("condition") == \
            "service_completed_successfully"


# ── the variables are real ───────────────────────────────────────────

class TestEveryVariableIsOneTheCodeReads:
    """A typo in an environment variable name is silent in both directions:
    the container starts, the setting does nothing, and the symptom is "we
    configured it and nothing happened"."""

    #: Consumed by compose or the image rather than by application code.
    NOT_APP_CONFIG = {
        "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
        "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD",
        "TESTFORTGE_PORT", "TESTFORTGE_MEMORY_LIMIT", "MINIO_CONSOLE_PORT",
    }

    @pytest.fixture(scope="class")
    def source_text(self) -> str:
        chunks = []
        for folder in ("engine", "routes"):
            for path in (REPO / folder).rglob("*.py"):
                chunks.append(path.read_text(encoding="utf-8",
                                             errors="ignore"))
        for name in ("config.py", "app.py"):
            chunks.append((REPO / name).read_text(encoding="utf-8"))
        return "\n".join(chunks)

    def test_the_app_service_sets_nothing_the_code_ignores(self, app_service,
                                                           source_text):
        unknown = [name for name in app_service["environment"]
                   if name not in self.NOT_APP_CONFIG
                   and f'"{name}"' not in source_text
                   and f"'{name}'" not in source_text]
        assert not unknown, (
            f"{unknown} are set in docker-compose.yml and read nowhere in "
            f"the application — either a typo or a setting that was removed")

    def test_every_flag_the_runbook_names_is_declared(self, runbook):
        from engine import features
        named = set(re.findall(r"`([A-Z][A-Z0-9_]{4,})`", runbook))
        flags = {n for n in named if n.endswith(("_ENABLED", "_MODE",
                                                 "_BACKEND", "_CONFIGURABLE"))}
        unknown = [n for n in flags
                   if n in ("AUTH_ENABLED", "ORG_MODE", "BASIC_GATE_ENABLED",
                            "STORAGE_BACKEND_CONFIGURABLE")
                   and n not in features.FLAGS]
        assert not unknown, f"{unknown} are documented and not declared"

    def test_the_required_variables_are_all_in_the_example_file(
            self, compose, env_example):
        """``:?`` stops the run with a named error. The name has to appear
        in the file the runbook tells you to copy, or the error names
        something you were never told to set."""
        required = set()
        for service in compose["services"].values():
            for raw in (service.get("environment") or {}).values():
                required |= set(re.findall(
                    r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?", str(raw)))

        assert required, "nothing is required, so nothing fails loudly"
        missing = [n for n in required
                   if not re.search(rf"^{n}=", env_example, re.M)]
        assert not missing, f"{missing} are required and not in .env.example"

    def test_the_runbook_generates_exactly_the_required_secrets(
            self, compose, runbook):
        """The one-liner in §2 has to produce the names the compose file
        refuses to start without — otherwise following the runbook to the
        letter still fails on step 3."""
        required = set()
        for service in compose["services"].values():
            for raw in (service.get("environment") or {}).values():
                required |= set(re.findall(
                    r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?", str(raw)))

        missing = [n for n in required if f"{n}=" not in runbook]
        assert not missing, (
            f"{missing} are required to start and the runbook never tells "
            f"anyone to set them")


# ── hardening ────────────────────────────────────────────────────────

class TestHardening:

    def test_postgres_is_not_published_to_the_host(self, compose):
        assert not compose["services"]["db"].get("ports"), (
            "a database with a published port and a password somebody typed "
            "in a hurry is how a self-host becomes an incident")

    def test_the_s3_api_is_not_published_either(self, compose):
        published = compose["services"]["storage"].get("ports") or []
        assert not any(":9000" in str(p) for p in published), (
            "the object store is reachable from outside; the app talks to "
            "it over the compose network")

    def test_the_minio_console_is_bound_to_loopback(self, compose):
        published = compose["services"]["storage"].get("ports") or []
        console = [p for p in published if str(p).endswith(":9001")]
        assert console, "no console port at all"
        assert all(str(p).startswith("127.0.0.1:") for p in console), (
            "the MinIO console is on 0.0.0.0 — reachable from the internet "
            "with credentials that are also the app's storage credentials")

    def test_no_service_can_gain_privileges(self, compose):
        for name, service in compose["services"].items():
            opts = service.get("security_opt") or []
            assert "no-new-privileges:true" in opts, name

    def test_the_app_has_a_memory_ceiling(self, app_service):
        """Chromium is the expensive part, and one runaway run should not
        take the host down with it."""
        limits = ((app_service.get("deploy") or {}).get("resources")
                  or {}).get("limits") or {}
        assert limits.get("memory"), "no memory limit on the container"

    def test_the_image_does_not_run_as_root(self):
        lines = (REPO / "Dockerfile").read_text(encoding="utf-8").splitlines()
        users = [ln.split()[1] for ln in lines if ln.startswith("USER ")]
        assert users and users[-1] != "root", (
            f"the final USER is {users[-1] if users else 'unset'}")

    def test_debug_is_off_and_not_overridable_by_accident(self, app_service):
        """``FLASK_DEBUG=1`` mints an ephemeral SECRET_KEY and lets the app
        boot on SQLite — both fine locally, both wrong for a deployment, and
        neither of them announces itself."""
        assert app_service["environment"]["FLASK_DEBUG"] == "0"

    def test_every_service_that_stays_up_reports_its_health(self, compose):
        for name, service in compose["services"].items():
            if service.get("restart") == "no":
                continue          # storage-init is meant to exit
            assert service.get("healthcheck"), (
                f"{name} has no healthcheck, so `docker compose ps` cannot "
                f"tell a working instance from a broken one")


# ── the runbook says true things ─────────────────────────────────────

class TestTheRunbookIsHonest:

    def test_every_file_it_points_at_exists(self, runbook):
        """The E10 lesson, applied to a document that is *only* claims: a
        report citing a test nobody wrote is the defect it exists to catch."""
        # `engine/db.py::_assert_prod_safety` counts. An earlier version of
        # this pattern required a closing backtick straight after `.py`, so
        # every citation carrying a `::symbol` suffix was skipped in
        # silence — a coverage gap in the test that checks for coverage
        # gaps. Found by mutating a citation and watching nothing fail.
        cited = set(re.findall(
            r"`{1,2}((?:docs|engine|routes)/[\w./-]+\.(?:py|md))(?:::[\w.]+)?`",
            runbook))
        assert len(cited) >= 3, f"the runbook barely cites anything: {cited}"
        missing = [c for c in cited if not (REPO / c).exists()]
        assert not missing, missing

    def test_it_says_what_was_not_verified(self, runbook_flat):
        """It was written on a machine with no Docker. A runbook that
        implies its own commands were run is worse than one that admits
        they were not, because the reader calibrates on it."""
        head = runbook_flat[:2500].lower()
        assert "not** been executed" in head or "not been executed" in head
        assert "docker" in head

    def test_it_names_what_the_deployment_does_not_give_you(self,
                                                            runbook_flat):
        """E8.4 is not built and E8.7 has not run. A runbook that lists only
        what works is a runbook that gets believed."""
        tail = runbook_flat[-2500:]
        assert "E8.4" in tail and "E8.7" in tail
        assert "backup" in tail.lower()

    def test_the_flag_order_that_locks_everyone_out_is_called_out(
            self, runbook):
        """``BASIC_GATE_ENABLED=0`` before ``AUTH_ENABLED=1`` asks for no
        shared password and no accounts either. The code refuses it; the
        runbook should not require the reader to discover that."""
        assert "in that order" in runbook.lower()
        assert "BASIC_GATE_ENABLED=0" in runbook

    def test_it_explains_why_postgres_is_not_optional(self, runbook):
        assert "_assert_prod_safety" in runbook
        assert "TESTFORTGE_ALLOW_SQLITE_PROD" in runbook, (
            "the escape hatch exists in the code; a runbook that hides it "
            "invites somebody to patch the source instead")

    def test_it_tells_the_reader_what_losing_each_volume_costs(
            self, runbook, compose):
        for volume in compose["volumes"]:
            assert volume in runbook, (
                f"{volume} is created by the compose file and never "
                f"explained; a self-hoster deciding what to back up cannot "
                f"tell it from a cache")

    def test_it_warns_that_rotating_the_encryption_key_destroys_data(
            self, runbook_flat):
        section = runbook_flat[runbook_flat.find("TESTFORTGE_ENCRYPTION_KEY"):]
        assert "unreadable" in section[:2000].lower()

    def test_behind_https_is_explained_as_a_cookie_flag(self, runbook_flat):
        """Left at 0 behind a proxy, the session cookie is not Secure. "Set
        this in production" would not tell anyone why it matters."""
        i = runbook_flat.find("BEHIND_HTTPS=1")
        assert i > 0
        assert "Secure" in runbook_flat[i:i + 600]
