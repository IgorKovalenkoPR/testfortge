"""render.yaml must declare every feature flag that gates real behaviour.

Why this file exists: the service is Blueprint-managed, so a Manual Sync
reconciles the live service against render.yaml — and **deletes env vars
the blueprint does not declare**.

On 2026-07-30 `RECORDER_ENABLED=1` was live on the web service but absent
from render.yaml. A sync would have removed it, the code default is "0",
and the Web Recorder would have switched off in production with no other
symptom: no error, no log, no failing test. The flag was found only by
diffing the dashboard against the blueprint by hand before running a
sync.

These flags are exactly the ones where "undeclared" and "declared with
the intended value" produce different user-visible behaviour, so each one
must be explicit. Anything genuinely optional (tuning knobs like
TESTFORTGE_BROWSER_PAGES, JOB_RETENTION_SECONDS) is deliberately out of
scope — a default is fine there.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"


#: flag -> why it must be declared rather than left to its code default
BEHAVIOUR_GATING_FLAGS = {
    "RECORDER_ENABLED": (
        "gates the Web Recorder endpoints, the /test-cases recorder UI "
        "and `tfg record`; code default is 0, so dropping it turns a "
        "shipped feature off silently"
    ),
    "TESTFORTGE_BROWSER_ENABLED": (
        "keeps Playwright out of the 512 MB web worker; code default is "
        "1, so dropping it brings back the OOM kill that lost generation "
        "jobs mid-run"
    ),
    "WALKTHROUGH_MODE_ENABLED": (
        "gates walkthrough execution; the MCP service needs the same "
        "gate or trigger_test_execution(mode='walkthrough') reaches a "
        "runner that cannot handle it"
    ),
}


@pytest.fixture(scope="module")
def blueprint() -> dict:
    assert RENDER_YAML.is_file(), f"{RENDER_YAML} is missing"
    with RENDER_YAML.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def web_service(blueprint) -> dict:
    svc = next((s for s in blueprint["services"] if s["name"] == "testfortge"),
               None)
    assert svc is not None, "the testfortge web service must be declared"
    return svc


def _env_map(service: dict) -> dict:
    """key -> declared value, or a marker for dashboard-managed keys."""
    out = {}
    for entry in service.get("envVars") or []:
        key = entry.get("key")
        if not key:
            continue
        if entry.get("sync") is False:
            out[key] = "__dashboard__"
        elif entry.get("generateValue"):
            out[key] = "__generated__"
        elif "fromDatabase" in entry or "fromService" in entry:
            out[key] = "__linked__"
        else:
            out[key] = entry.get("value")
    return out


class TestBlueprintParses:
    def test_yaml_is_valid(self, blueprint):
        assert isinstance(blueprint, dict)
        assert blueprint.get("services"), "no services declared"

    def test_both_services_are_declared(self, blueprint):
        names = {s["name"] for s in blueprint["services"]}
        assert {"testfortge", "testfortge-mcp"} <= names, names

    def test_no_duplicate_keys_within_a_service(self, blueprint):
        for svc in blueprint["services"]:
            keys = [e["key"] for e in (svc.get("envVars") or []) if e.get("key")]
            dupes = {k for k in keys if keys.count(k) > 1}
            assert not dupes, f"{svc['name']} declares {dupes} twice"


class TestBehaviourGatingFlagsAreDeclared:
    @pytest.mark.parametrize("flag", sorted(BEHAVIOUR_GATING_FLAGS))
    def test_web_service_declares_flag(self, web_service, flag):
        env = _env_map(web_service)
        assert flag in env, (
            f"{flag} is not declared for the web service. A blueprint "
            f"Manual Sync deletes undeclared vars, and here that matters: "
            f"{BEHAVIOUR_GATING_FLAGS[flag]}."
        )

    @pytest.mark.parametrize("flag", sorted(BEHAVIOUR_GATING_FLAGS))
    def test_flag_carries_an_explicit_value(self, web_service, flag):
        env = _env_map(web_service)
        value = env.get(flag)
        assert value not in (None, "", "__dashboard__"), (
            f"{flag} must carry an explicit value in render.yaml so a "
            f"fresh environment reproduces production, not the code "
            f"default. Got {value!r}."
        )
        assert str(value) in ("0", "1"), (
            f"{flag} is a boolean gate; expected \"0\" or \"1\", got "
            f"{value!r}"
        )


class TestRecorderFlagMatchesProduction:
    def test_web_recorder_is_on(self, web_service):
        # Read off the live dashboard on 2026-07-30. If this is ever
        # turned off deliberately, change it here in the same commit so
        # the blueprint stays the source of truth.
        assert _env_map(web_service)["RECORDER_ENABLED"] == "1"

    def test_mcp_recorder_is_off(self, blueprint):
        # mcp_server gates record_steps_attach on the same flag but the
        # var has never been set on that service, so the tool is off.
        mcp = next(s for s in blueprint["services"]
                   if s["name"] == "testfortge-mcp")
        assert _env_map(mcp).get("RECORDER_ENABLED") == "0", (
            "declare the MCP recorder gate explicitly; flipping it on "
            "without the web service leaves half the recorder wired"
        )


class TestBrowserPassStaysOffOnTheFreePlan:
    def test_playwright_is_disabled_in_the_web_worker(self, web_service):
        assert _env_map(web_service)["TESTFORTGE_BROWSER_ENABLED"] == "0", (
            "the free plan has 512 MB and one gunicorn worker; Chromium "
            "in-process is what OOM-killed generation mid-run. Flip this "
            "to \"1\" only together with a paid instance size."
        )


class TestAutomationIngestIsActuallyEnabled:
    """`sync: false` reads as "configured" and behaves as "off".

    An unset AUTOMATION_INGEST_TOKEN disables POST /automation/allure-results
    entirely, so the module reports "Disabled" on prod while the blueprint
    looks complete — which is exactly what happened between the module
    shipping and 2026-08-01. `generateValue` is the difference between
    declaring the var and the var having a value.
    """

    def test_token_is_generated_rather_than_dashboard_managed(
        self, web_service
    ) -> None:
        assert _env_map(web_service).get("AUTOMATION_INGEST_TOKEN") == (
            "__generated__"
        ), (
            "use generateValue: true — with sync: false the var ships "
            "empty and CI result ingestion stays silently off"
        )


class TestSecretsAreNotCommitted:
    @pytest.mark.parametrize("secret", ["ANTHROPIC_API_KEY", "SECRET_KEY",
                                        "TESTFORTGE_BASIC_PASSWORD",
                                        "MCP_BEARER_TOKEN", "FIGMA_PAT",
                                        "AUTOMATION_INGEST_TOKEN"])
    def test_secret_has_no_literal_value(self, blueprint, secret):
        for svc in blueprint["services"]:
            entry = next((e for e in (svc.get("envVars") or [])
                          if e.get("key") == secret), None)
            if entry is None:
                continue
            assert "value" not in entry, (
                f"{secret} must never carry a literal value in "
                f"render.yaml ({svc['name']}); use sync: false or "
                f"generateValue: true"
            )
