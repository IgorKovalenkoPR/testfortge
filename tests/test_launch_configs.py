"""The preview config for the authenticated product could not be signed into.

``.claude/launch.json`` carries the dev previews, and ``qa-forge-auth`` exists
to look at the product in the mode it ships in — ``AUTH_ENABLED=1``,
``ORG_MODE=1``. It set neither ``BOOTSTRAP_ADMIN_EMAIL`` nor
``BOOTSTRAP_ADMIN_PASSWORD``, so on a fresh database it stopped at the sign-in
page for ever. Measured both ways on a throwaway database: with the variables
the first-admin bootstrap mints a verified admin and an organisation; without
them the database still has **zero users** after boot, and the sign-in page
says the product is invite-only — an invitation needs an admin, and an admin
needs an invitation.

That is exactly the deadlock ``engine/bootstrap.py`` was written to end, one
level over: the module fixed it for the deployment and the developer's own
preview kept it. Its docstring calls the shape "a mechanism whose first caller
was never written", which is what this config was missing.

The two previews also shared one database file, so the authenticated one
inherited projects owned by an anonymous browser session and the
unauthenticated one saw an organisation it could not belong to. They have
their own files now.

This file is a gate rather than a fix: any preview added later that turns
authentication on needs the same two variables, and nothing else would say
so until somebody tried to sign in.
"""
from __future__ import annotations

import json
import pathlib

import pytest

CONFIG_PATH = pathlib.Path(".claude/launch.json")


def _configurations():
    if not CONFIG_PATH.exists():        # pragma: no cover — checkout without it
        pytest.skip("no .claude/launch.json in this checkout")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["configurations"]


def _authenticated():
    return [c for c in _configurations()
            if (c.get("env") or {}).get("AUTH_ENABLED") == "1"]


class TestEveryAuthenticatedPreviewCanBeSignedInTo:

    def test_there_is_one_to_check(self):
        """The two tests below pass vacuously if the filter matches nothing —
        which is what a renamed variable would do to them."""
        assert _authenticated(), "no preview runs with AUTH_ENABLED=1"

    @pytest.mark.parametrize("variable", ["BOOTSTRAP_ADMIN_EMAIL",
                                          "BOOTSTRAP_ADMIN_PASSWORD"])
    def test_the_bootstrap_variable_is_set(self, variable):
        missing = [c["name"] for c in _authenticated()
                   if not (c.get("env") or {}).get(variable)]
        assert not missing, (
            f"{missing} run with authentication on and no {variable}, so a "
            f"fresh database has no way to acquire its first administrator "
            f"— see engine/bootstrap.py and docs/runbooks/first-admin.md")

    def test_the_password_clears_the_policy(self):
        """``engine.auth`` refuses anything under twelve characters, and the
        bootstrap logs its refusal rather than raising — so a short one here
        would fail silently at boot and look exactly like no variable at all.
        """
        from engine import auth as _auth

        for config in _authenticated():
            password = (config.get("env") or {})["BOOTSTRAP_ADMIN_PASSWORD"]
            assert len(password) >= _auth.MIN_PASSWORD_LEN, config["name"]

    def test_the_credentials_say_they_are_not_secret(self):
        """This file is committed. The convention it already follows for
        ``SECRET_KEY`` is a value that cannot be mistaken for a real one."""
        for config in _authenticated():
            env = config.get("env") or {}
            assert "not-a-secret" in env.get("SECRET_KEY", "") or \
                "not a secret" in env.get("SECRET_KEY", ""), config["name"]
            assert "not a secret" in env["BOOTSTRAP_ADMIN_PASSWORD"], \
                config["name"]


class TestThePreviewsDoNotShareState:

    def test_no_two_configs_write_the_same_database(self):
        """They did. ``qa-forge-auth`` and ``qa-forge-editors`` both pointed
        at ``storage/devpreview.db``, so the authenticated preview inherited
        projects owned by an anonymous browser session, and whichever ran
        second saw the other's idea of who the caller was."""
        seen: dict[str, str] = {}
        clashes = []
        for config in _configurations():
            database = (config.get("env") or {}).get("TESTFORTGE_DB")
            if not database:
                continue
            if database in seen:
                clashes.append(f"{seen[database]} and {config['name']} "
                               f"both use {database}")
            seen[database] = config["name"]
        assert not clashes, clashes

    def test_no_two_configs_share_a_port(self):
        ports = [c.get("port") for c in _configurations() if c.get("port")]
        assert len(ports) == len(set(ports)), ports
