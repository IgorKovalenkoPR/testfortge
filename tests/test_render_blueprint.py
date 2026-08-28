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
import re

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
    # ── Added by the E0.6 audit ──────────────────────────────────────
    "BROWSER_CONTROL_ENABLED": (
        "gates the MCP-driven browser driver (PR-F); code default is 0, "
        "so an instance that turned it on in the dashboard would lose it"
    ),
    "TC_AUTHOR_ENABLED": (
        "the LLM test-case author; code default is 1, so dropping it "
        "cannot be used to turn the author off — the value has to exist "
        "here for that to be possible at all"
    ),
    "CL_AUTHOR_ENABLED": "the same, for the checklist author",
    "TESTFORTGE_SNAPSHOT_WORKER": (
        "the daily metric-snapshot thread; code default is 1"
    ),
    "LEGACY_EXECUTOR": (
        "routes execution back to the pre-Stage-3 runner. Pinned at 0 so "
        "a hand-set 1 cannot survive a sync unnoticed — the legacy path "
        "is kept for rollback, not for running on"
    ),
    "SSRF_ALLOWLIST_BYPASS": (
        "disables the SSRF allowlist. Declared **so a sync resets it**: "
        "the failure mode of a forgotten 1 is an open request surface"
    ),
    "TESTFORTGE_ALLOW_SQLITE_PROD": (
        "downgrades the refuse-to-boot-on-SQLite guard to a warning. "
        "Same argument: a forgotten 1 is a production database on SQLite"
    ),
}

#: Not booleans, and still must be declared. Same reason, different shape:
#: a Manual Sync deletes what the blueprint does not name, and for a token
#: that lives in the dashboard the symptom is a 403 nobody expected.
MUST_BE_DECLARED: dict[str, str] = {
    "BACKUP_TOKEN": (
        "gates POST /api/backup/run (E8.4). Set by hand in the dashboard, "
        "so it is exactly the shape that disappears on a sync — and the "
        "weekly workflow would then fail with 403 and no other symptom"
    ),
    "OPS_ENDPOINTS_TOKEN": (
        "guards /metrics. Optional by design and dangerous when unset: "
        "engine/route_policy.py leaves `metrics` out of the machine "
        "exemption precisely because an instance with no token publishes "
        "operator telemetry to anyone who asks"
    ),
    "ORG_QUOTA_ROWS": "the per-organisation row quota (E0.12)",
    "TESTFORTGE_BASIC_PUBLIC_PATHS": (
        "the allowlist on the outer HTTP Basic gate. Widening it by hand "
        "with no record is how a perimeter stops being one"
    ),
    "STORAGE_S3_SECURE": (
        "completes the STORAGE_S3_* set; four of five declared is the "
        "shape where the fifth is the one that disappears"
    ),
}

#: Switches that disable a guard, and the value they must carry.
#:
#: Declaring one is only half the job. Mutation testing made the point: with
#: ``SSRF_ALLOWLIST_BYPASS`` flipped to ``"1"`` in the blueprint, every other
#: check in this file still passed — the var was declared, carried an
#: explicit boolean, and was read by the code. A declaration that accepts
#: either value pins nothing, which is the opposite of why these two were
#: added to render.yaml at all.
PINNED_OFF: dict[str, str] = {
    "SSRF_ALLOWLIST_BYPASS": (
        "turns off the SSRF allowlist, so the engine will fetch RFC1918 "
        "and metadata addresses on request. Useful against an operator's "
        "own staging box, and never something a deploy should carry"
    ),
    "TESTFORTGE_ALLOW_SQLITE_PROD": (
        "downgrades the refuse-to-boot-on-SQLite guard to a warning. The "
        "guard exists because gunicorn workers plus the detached runner "
        "deadlock on one file under load"
    ),
    "LEGACY_EXECUTOR": (
        "routes execution back to the pre-Stage-3 runner. Kept for "
        "rollback; running on it is not a state to arrive at by drift"
    ),
}

#: Env-var names that read as a gate or a credential. Used to derive the
#: check below rather than to restate a list.
#:
#: ``_TOKENS`` is deliberately not matched by ``_TOKEN`` — ``ANTHROPIC_MAX_TOKENS``
#: is a size, not a secret.
GATE_OR_SECRET = re.compile(
    r"(_ENABLED|_TOKEN|_SECRET|_PASSWORD|_BYPASS)$|^(SECRET_KEY|MAIL_FROM)$")


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


class TestProgrammeFlagsAreDeclared:
    """Every flag in the registry must also exist in the blueprint.

    Derived from ``engine.features.FLAGS`` rather than a second hand-kept
    list, so the failure mode this whole file exists to prevent cannot
    recur for programme flags: adding a flag to the registry and
    forgetting the blueprint now fails the build instead of surviving
    until a Manual Sync quietly deletes it.
    """

    @staticmethod
    def _registry() -> dict:
        from engine import features
        return features.FLAGS

    def test_registry_is_not_empty(self):
        # A green suite because the registry got emptied would be the
        # least useful kind of green.
        assert self._registry()

    def test_web_service_declares_every_registry_flag(self, web_service):
        env = _env_map(web_service)
        missing = sorted(set(self._registry()) - set(env))
        assert not missing, (
            f"declared in engine/features.py but not in render.yaml: "
            f"{missing}. A Manual Sync deletes undeclared vars, so a flag "
            f"turned on in the dashboard would silently revert."
        )

    def test_registry_flags_carry_an_explicit_boolean(self, web_service):
        env = _env_map(web_service)
        for flag in sorted(self._registry()):
            value = env.get(flag)
            assert str(value) in ("0", "1"), (
                f"{flag} must carry an explicit \"0\" or \"1\" so a fresh "
                f"environment reproduces production rather than the code "
                f"default. Got {value!r}."
            )

    def test_session_backend_is_declared(self, web_service):
        # Not a boolean, so it sits outside the loop above — but it is the
        # single most consequential value in this file: "filesystem" loses
        # every session on restart, "db" does not.
        value = _env_map(web_service).get("SESSION_BACKEND")
        assert value in ("filesystem", "db"), (
            f"SESSION_BACKEND must be declared as \"filesystem\" or "
            f"\"db\"; got {value!r}"
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


# ── E0.6: derived from the code, not from a list ─────────────────────

REPO = pathlib.Path(__file__).resolve().parent.parent

#: Where the application reads its environment. ``mcp_server`` is separated
#: because it is a **different Render service** with its own env block, and
#: a var declared on the wrong one is declared nowhere useful.
_WEB_ROOTS = ("engine", "routes")
_WEB_FILES = ("app.py", "config.py")
_MCP_ROOT = "mcp_server"

_ENV_READ = re.compile(
    r'environ(?:\.get)?\(\s*["\']([A-Z][A-Z0-9_]{2,})["\']'
    r'|environ\[\s*["\']([A-Z][A-Z0-9_]{2,})["\']')


def _env_names(paths) -> dict[str, set[str]]:
    """name -> the files that read it."""
    out: dict[str, set[str]] = {}
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in _ENV_READ.finditer(text):
            name = match.group(1) or match.group(2)
            out.setdefault(name, set()).add(str(path.relative_to(REPO)))
    return out


def _web_env() -> dict[str, set[str]]:
    paths = [p for root in _WEB_ROOTS for p in (REPO / root).rglob("*.py")]
    paths += [REPO / f for f in _WEB_FILES]
    return _env_names(paths)


def _mcp_env() -> dict[str, set[str]]:
    return _env_names((REPO / _MCP_ROOT).rglob("*.py"))


class TestEveryGateOrCredentialIsDeclared:
    """The check that would have caught this audit's own findings.

    E0.6 swept every ``os.environ`` read in the tree against the blueprint
    and found five that gate behaviour or carry a credential and were
    declared nowhere: ``BACKUP_TOKEN``, ``OPS_ENDPOINTS_TOKEN``,
    ``BROWSER_CONTROL_ENABLED``, ``TC_AUTHOR_ENABLED`` and
    ``CL_AUTHOR_ENABLED`` — plus ``STORAGE_S3_SECURE``, which was left out
    of a set of five added four days earlier.

    So the rule is derived from the **names the code actually reads**
    rather than from a list somebody remembers to extend. It is a
    heuristic and it says so: it recognises the shapes
    (``*_ENABLED``, ``*_TOKEN``, ``*_SECRET``, ``*_PASSWORD``, ``*_BYPASS``)
    and cannot know that ``LEGACY_EXECUTOR`` is a gate. Those live in
    :data:`BEHAVIOUR_GATING_FLAGS` above with their reasons, and the real
    fix for that residue is for new gates to go in
    ``engine/features.py``, which is derived completely.
    """

    def test_the_scanner_finds_something(self):
        """A green run because the regex stopped matching would be the
        least useful kind of green — the failure this file exists for."""
        assert len(_web_env()) > 40, (
            "the environment scan found almost nothing; the pattern has "
            "probably stopped matching how the code reads os.environ")

    def test_web_service_declares_every_gate_or_credential_it_reads(
            self, web_service):
        env = _env_map(web_service)
        # SOMETHING is the example name in engine/features.py's docstring.
        missing = sorted(
            name for name in _web_env()
            if GATE_OR_SECRET.search(name) and name != "SOMETHING"
            and name not in env)
        assert not missing, (
            f"the web service reads {missing} and render.yaml does not "
            f"declare them. A Manual Sync deletes what the blueprint does "
            f"not name, so a value set in the dashboard disappears with no "
            f"error anywhere. Declare each with a value, or with "
            f"`sync: false` when the dashboard holds it.")

    def test_mcp_service_declares_every_gate_or_credential_it_reads(
            self, blueprint):
        mcp = next(s for s in blueprint["services"]
                   if s["name"] == "testfortge-mcp")
        env = _env_map(mcp)
        missing = sorted(
            name for name in _mcp_env()
            if GATE_OR_SECRET.search(name) and name not in env)
        assert not missing, (
            f"the MCP service reads {missing} and its own env block does "
            f"not declare them. Declaring it on the web service does not "
            f"help — they are separate services.")

    def test_no_credential_carries_a_literal_value(self, blueprint):
        """Derived, where ``TestSecretsAreNotCommitted`` names six by hand.

        A secret added tomorrow is covered by this and not by that list.
        """
        offenders = []
        for svc in blueprint["services"]:
            for entry in svc.get("envVars") or []:
                key = entry.get("key") or ""
                if not re.search(r"(_TOKEN|_SECRET|_PASSWORD|_PAT|_KEY)$", key):
                    continue
                if key in ("STORAGE_S3_ACCESS_KEY",):
                    # An access key id is not a secret on its own, and R2
                    # shows it in its own console. Still `sync: false`
                    # here; listed so the exception is visible.
                    pass
                if "value" in entry:
                    offenders.append(f"{svc['name']}.{key}")
        assert not offenders, (
            f"{offenders} carry a literal value in render.yaml. Use "
            f"`sync: false` (the dashboard holds it) or "
            f"`generateValue: true` (Render mints it).")


class TestTheDeclaredSetStaysHonest:
    """The other direction: things declared that nothing reads.

    A stale entry is quieter than a missing one and still costs — it is a
    value an operator can set and nothing consults.

    **The name is searched for anywhere in the source, not only inside an
    ``os.environ`` call**, and that widening is the point rather than
    laziness. Written the narrow way first, this failed on twelve entries
    that are all read correctly: every ``engine.features`` flag reaches the
    environment through ``is_enabled(name)``, ``TESTFORTGE_ENCRYPTION_KEY``
    through the ``ENCRYPTION_KEY_ENV`` constant, and the session timeouts
    through their own module constants. Indirection is how a well-factored
    codebase reads configuration, so a scanner that only recognises the
    literal call is a scanner that reports good code as dead.

    That limitation is worth naming for the audit as a whole: the sweep
    behind E0.6 can prove a declared var is *mentioned*, and cannot prove
    an undeclared one is *unused*.
    """

    #: Consumed by the platform or the container, never by our source.
    NOT_READ_BY_US = {
        "PORT",              # Render injects it; the dockerCommand uses it
        "PYTHON_VERSION", "POETRY_VERSION",
    }

    @staticmethod
    def _all_source() -> str:
        chunks = []
        for root in (*_WEB_ROOTS, _MCP_ROOT):
            for path in (REPO / root).rglob("*.py"):
                chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
        for name in _WEB_FILES:
            chunks.append((REPO / name).read_text(encoding="utf-8"))
        return chr(10).join(chunks)

    def test_every_declared_var_is_named_somewhere_in_the_code(self,
                                                               blueprint):
        source = self._all_source()
        stale = []
        for svc in blueprint["services"]:
            for entry in svc.get("envVars") or []:
                key = entry.get("key")
                if not key or key in self.NOT_READ_BY_US:
                    continue
                if f'"{key}"' in source or f"'{key}'" in source:
                    continue
                stale.append(f"{svc['name']}.{key}")
        assert not stale, (
            f"{stale} are declared in render.yaml and the name appears "
            f"nowhere in the code. Either the reader was removed and the "
            f"declaration is stale, or the name is a typo — in which case "
            f"the real one is undeclared and a sync will delete it.")


class TestTheHandKeptListsAreThemselvesChecked:
    """A list of things that must be declared is only useful if it is."""

    @pytest.mark.parametrize("name", sorted(MUST_BE_DECLARED))
    def test_it_is_declared(self, web_service, name):
        assert name in _env_map(web_service), (
            f"{name} must be declared: {MUST_BE_DECLARED[name]}")

    @pytest.mark.parametrize("name", sorted(MUST_BE_DECLARED))
    def test_it_carries_a_reason(self, name):
        assert MUST_BE_DECLARED[name].strip()

    def test_the_two_lists_do_not_overlap(self):
        both = set(BEHAVIOUR_GATING_FLAGS) & set(MUST_BE_DECLARED)
        assert not both, (
            f"{both} are in both lists; the boolean one asserts a \"0\"/"
            f"\"1\" value and the other does not, so an entry in both is "
            f"one of them being ignored")


class TestTheSafetySwitchesArePinnedOff:
    """Declared **and** held at "0".

    Found by mutation: flipping ``SSRF_ALLOWLIST_BYPASS`` to ``"1"`` in the
    blueprint passed every other check in this file. Declaring a switch
    that disables a guard, without asserting which way it points, buys the
    visibility and none of the protection.

    The value matters more here than for an ordinary flag, because the
    direction of the mistake is asymmetric: a forgotten ``"1"`` is an open
    request surface or a production database on SQLite, and neither
    announces itself.
    """

    @pytest.mark.parametrize("name", sorted(PINNED_OFF))
    def test_it_is_declared_and_off(self, web_service, name):
        value = _env_map(web_service).get(name)
        assert value == "0", (
            f"{name} must be declared as \"0\" in render.yaml; got "
            f"{value!r}. It {PINNED_OFF[name]}. Turning it on is an "
            f"operator's deliberate, temporary act in the dashboard — a "
            f"blueprint that ships it on makes it permanent and invisible.")

    @pytest.mark.parametrize("name", sorted(PINNED_OFF))
    def test_the_code_still_reads_it(self, name):
        """A pin on a name nothing consults is a comment."""
        source = TestTheDeclaredSetStaysHonest._all_source()
        assert f'"{name}"' in source or f"'{name}'" in source


class TestThePerimeterAllowlistStaysNarrow:
    """``TESTFORTGE_BASIC_PUBLIC_PATHS`` decides what skips the outer gate.

    Declaring it is not enough, for the reason the safety switches above
    make: mutation flipped this to ``"/"`` — every path public, the HTTP
    Basic perimeter gone entirely — and every other check in this file
    still passed, because the var was present and read.

    The value is pinned to the two probes. Widening it is a deliberate
    edit here **and** in this test, which is the point: an allowlist on a
    perimeter should cost two lines of thought, not one.
    """

    PROBES = {"/healthz", "/readyz"}

    def test_it_is_pinned_to_the_probes(self, web_service):
        value = _env_map(web_service).get("TESTFORTGE_BASIC_PUBLIC_PATHS")
        assert value, "the allowlist must be declared, not left to the default"
        entries = {p.strip() for p in str(value).split(",") if p.strip()}
        assert entries == self.PROBES, (
            f"expected exactly {sorted(self.PROBES)}, got {sorted(entries)}. "
            f"Every path listed here skips the HTTP Basic gate.")

    def test_no_entry_opens_the_whole_app(self, web_service):
        """The failure this is really about. ``/`` is a prefix of every
        path, so one character turns an allowlist into an off switch."""
        value = str(_env_map(web_service).get(
            "TESTFORTGE_BASIC_PUBLIC_PATHS") or "")
        entries = {p.strip() for p in value.split(",") if p.strip()}
        assert "/" not in entries, (
            "'/' matches every request — the gate would be off for the "
            "whole application while still looking configured")


class TestTheBrowserMemoryBudgetFitsThePlan:
    """``MEMORY_BUDGET_MB`` is load-bearing on a 512 MB container.

    Declared rather than derived here because the number is worth reading
    in one place, and asserted because a declaration nobody checks is a
    number that drifts. E5.2 measured the arithmetic: the largest jump a
    single page step made between two polls was 122 MB, so the budget has
    to sit at least that far below the ceiling or the guard can be
    overtaken inside one step.
    """

    FREE_PLAN_MB = 512

    def test_it_is_declared(self, web_service):
        assert "MEMORY_BUDGET_MB" in _env_map(web_service)

    def test_it_leaves_room_for_the_worst_single_step(self, web_service):
        from engine.live_executor import STEP_HEADROOM_MB
        budget = int(_env_map(web_service)["MEMORY_BUDGET_MB"])
        assert budget + STEP_HEADROOM_MB <= self.FREE_PLAN_MB, (
            f"a {budget} MB budget plus the {STEP_HEADROOM_MB} MB a single "
            f"step was measured to add exceeds the {self.FREE_PLAN_MB} MB "
            f"container. The guard would fire after the kernel, which is a "
            f"guard that only writes the epitaph.")

    def test_it_is_not_so_small_the_browser_cannot_start(self, web_service):
        budget = int(_env_map(web_service)["MEMORY_BUDGET_MB"])
        assert budget >= 200, (
            f"{budget} MB is below what Chromium needs to open one page "
            f"(measured: 267 MB tree at new_page), so every run would exit "
            f"before doing anything")
# ── Staging (E10 entry criterion) ────────────────────────────────────


@pytest.fixture(scope="module")
def staging_service(blueprint) -> dict:
    svc = next((s for s in blueprint["services"]
                if s["name"] == "testfortge-staging"), None)
    assert svc is not None, (
        "the staging service must be declared — E10's entry criteria name a "
        "deployed staging, and three verification zones (load, "
        "accessibility, real mail delivery) have no other home")
    return svc


class TestStagingIsActuallyStaging:
    """A second service is not a staging environment by itself.

    What makes it one is the set of properties below, and each is here
    because getting it wrong turns staging into either a second production
    or a hazard to the first one.
    """

    def test_it_runs_the_mode_the_product_ships_in(self, staging_service):
        """Prod currently runs with authentication off, so the shipping
        mode has never had real traffic. That is the gap staging closes,
        and it closes nothing if it copies prod's flags."""
        env = _env_map(staging_service)
        assert env.get("AUTH_ENABLED") == "1"
        assert env.get("ORG_MODE") == "1"

    def test_sessions_survive_the_sleep(self, staging_service):
        """Pinned to "db", and the value is the whole point.

        Staging is a free instance that sleeps after ~15 idle minutes.
        Under SESSION_BACKEND=filesystem the dyno's disk goes with it and
        every session on it — so a walk through any multi-step feature
        was interrupted by a sign-in, and four deploys in one afternoon
        meant four more. Measured 2026-08-28 while walking the recorder.

        Asserted rather than left to the "filesystem" or "db" check
        above, because that one passes for either value and this service
        is only useful at one of them. Prod stays on filesystem
        deliberately: it does not sleep, and that migration is its own
        decision.
        """
        assert _env_map(staging_service).get("SESSION_BACKEND") == "db"

    def test_the_basic_gate_is_off_and_that_is_safe_here(self,
                                                        staging_service):
        """Off **because** AUTH_ENABLED is 1 — the interlock in
        engine/basic_auth.py refuses the other combination, which is what
        stops this from being a public instance with no login at all."""
        env = _env_map(staging_service)
        assert env.get("BASIC_GATE_ENABLED") == "0"
        assert env.get("AUTH_ENABLED") == "1", (
            "gate off with auth off would be a fully public instance; the "
            "interlock would override it, and a blueprint that asks for it "
            "is still a blueprint nobody should copy")

    def test_it_can_be_signed_into_when_its_database_is_empty(
            self, staging_service):
        """A new database has no users, and an empty database cannot issue
        itself an invitation. Without the bootstrap variables this service
        is unreachable by design.

        The reason was stronger when staging ran on an ephemeral SQLite file
        and *every deploy* produced an empty database. It still holds: the
        database is empty once — on the day it is created — and that is the
        one day nobody can invite anybody.
        """
        env = _env_map(staging_service)
        for key in ("BOOTSTRAP_ADMIN_EMAIL", "BOOTSTRAP_ADMIN_PASSWORD"):
            assert env.get(key) == "__dashboard__", (
                f"{key} must be declared with sync: false — a fresh database "
                f"has no users, so without it nobody can sign in to the "
                f"instance whose purpose is being signed into")

    def test_it_does_not_touch_the_production_database(self, staging_service):
        """The one property with no acceptable exception. E8.5 deletes a
        project's rows and blobs; a deletion test on staging pointed at
        prod's database would delete prod's data.

        Staging now has a database of its own, so the rule can no longer be
        "declares no DATABASE_URL" — the earlier and cruder form. What
        matters is the *shape* of the declaration: dashboard-managed is a
        string somebody pasted, `fromDatabase` is a link to the database
        this service must never see.
        """
        env = _env_map(staging_service)
        assert env.get("DATABASE_URL") == "__dashboard__", (
            f"staging's DATABASE_URL is {env.get('DATABASE_URL')!r}. It must "
            f"be declared `sync: false` — a linked value ('__linked__') is "
            f"production's database, and a literal value would put a "
            f"password in git")

    def test_its_database_is_not_the_one_production_uses(self):
        """Read from the text rather than the parsed map, because the parse
        flattens every link to the same marker: this has to fail on the
        *name* `testfortge-db` appearing anywhere in staging's block, not
        merely on the key being linked."""
        blueprint_text = RENDER_YAML.read_text(encoding="utf-8")
        staging_block = blueprint_text.split("name: testfortge-staging", 1)[1]
        offenders = [line.strip() for line in staging_block.splitlines()
                     if "testfortge-db" in line and not line.strip().startswith("#")]
        assert not offenders, (
            f"staging references production's database: {offenders}. E8.5 "
            f"deletes a project's rows, and a deletion test would delete "
            f"production's.")

    def test_it_can_still_boot_before_the_url_is_pasted(self, staging_service):
        """A blueprint sync creates a `sync: false` key with no value, and an
        empty DATABASE_URL falls back to SQLite. Without the escape hatch the
        guard in engine/db.py raises at boot and the service never starts —
        so the first deploy after this change would fail, on a Sunday, for a
        reason nobody would connect to a database that had not been pasted
        in yet."""
        env = _env_map(staging_service)
        assert env.get("TESTFORTGE_ALLOW_SQLITE_PROD") == "1", (
            "without the escape hatch an unset DATABASE_URL takes the "
            "service down instead of degrading to SQLite")

    def test_an_empty_url_falls_back_instead_of_connecting_to_nothing(
            self, monkeypatch):
        """The other half of the sentence above, and the half that is a claim
        about the code rather than about the file.

        The blueprint now declares a key whose value arrives later, by hand.
        Everything above assumes the app treats *present but empty* the same
        as absent — and `database_url()` does, because it coerces with
        ``or ""`` and strips. If it ever read ``"DATABASE_URL" in os.environ``
        instead, a sync would point staging at the empty string and the
        service would die on a connection to nowhere. Asserted against the
        real function, not a copy of the rule.
        """
        from engine.db import database_url
        for value in ("", "   "):
            monkeypatch.setenv("DATABASE_URL", value)
            monkeypatch.delenv("TESTFORTGE_DB", raising=False)
            url = database_url()
            assert url.startswith("sqlite:"), (
                f"DATABASE_URL={value!r} resolved to {url!r}. An empty "
                f"dashboard value has to read as absent — a blueprint sync "
                f"creates the key that way.")

    def test_it_does_not_touch_the_production_bucket(self, staging_service):
        env = _env_map(staging_service)
        assert env.get("STORAGE_BACKEND") == "local", (
            "staging must not point at production object storage: E8.5 "
            "deletes by prefix, and a deletion test would remove real "
            "evidence. When staging gets a bucket it gets its own")

    def test_its_tokens_and_keys_are_its_own(self, staging_service):
        """A staging upload that lands in production's run history is
        worse than no staging: it corrupts the data people trust."""
        env = _env_map(staging_service)
        assert env.get("AUTOMATION_INGEST_TOKEN") == "__generated__"
        assert env.get("SECRET_KEY") == "__generated__"
        assert env.get("TESTFORTGE_ENCRYPTION_KEY") == "__generated__"

    def test_it_has_no_keepalive(self, staging_service):
        """Free instance-hours are per **account**: 750 a month, shared.
        Prod's working-hours window already spends ~264 of them, and a
        cold start on staging costs nobody anything."""
        env = _env_map(staging_service)
        assert "KEEPALIVE_URL" not in env, (
            "a keep-alive on staging spends production's free hours")

    #: Keys production declares and staging deliberately does not, each
    #: with the reason. Everything else must be declared on both.
    #:
    #: The first version of this test compared only ``engine.features``
    #: flags, and passed while staging was missing ``BEHIND_HTTPS`` — on
    #: HTTPS, with real logins, that means a session cookie not marked
    #: Secure. Found by diffing the two services (24 keys against 54)
    #: rather than by reading either, which is why the rule is now about
    #: **every** key and the exceptions have to be named.
    DELIBERATE_DIVERGENCES = {
        # DATABASE_URL is no longer here: staging declares it too, as a
        # dashboard-managed string rather than a link to production's
        # database. The invariant moved from "staging has no database" to
        # "staging's database is its own" — see
        # test_its_database_is_not_the_one_production_uses.
        "STORAGE_S3_ENDPOINT": "nor its bucket — E8.5 deletes by prefix",
        "STORAGE_S3_BUCKET": "as above",
        "STORAGE_S3_ACCESS_KEY": "as above",
        "STORAGE_S3_SECRET_KEY": "as above",
        "STORAGE_S3_REGION": "as above",
        "STORAGE_S3_SECURE": "as above",
        "TESTFORTGE_BASIC_USER": "no shared password in front of staging",
        "TESTFORTGE_BASIC_PASSWORD": "as above",
        "TESTFORTGE_BASIC_PUBLIC_PATHS": "as above — no gate, no exceptions",
        "GOOGLE_CLIENT_ID": "the OAuth client's redirect URI is per host, so "
                            "production's cannot be reused; Google sign-in "
                            "is off on staging until it gets its own",
        "GOOGLE_CLIENT_SECRET": "as above",
        "FIGMA_PAT": "a personal token; nothing on staging needs it",
    }

    def test_it_declares_everything_production_declares(
            self, staging_service, web_service):
        """Not the same *values* — the same *set*, minus named exceptions.

        A key missing here is a key whose staging behaviour is whatever the
        code happens to default to, which is the one thing staging exists
        to stop being a surprise.
        """
        prod = set(_env_map(web_service))
        staging = set(_env_map(staging_service))
        missing = sorted(prod - staging - set(self.DELIBERATE_DIVERGENCES))
        assert not missing, (
            f"staging does not declare {missing}. Production does, so their "
            f"behaviour differs by omission rather than by decision. Declare "
            f"them, or add each to DELIBERATE_DIVERGENCES with the reason.")

    def test_the_divergence_list_has_no_stale_entries(self):
        """An exception that no longer diverges is a claim about the two
        services that is not true any more."""
        blueprint_text = RENDER_YAML.read_text(encoding="utf-8")
        staging_block = blueprint_text.split("testfortge-staging", 1)[1]
        stale = sorted(name for name in self.DELIBERATE_DIVERGENCES
                       if f"- key: {name}" in staging_block)
        assert not stale, (
            f"{stale} are listed as deliberate divergences and staging "
            f"declares them after all")

    def test_it_is_served_over_https_like_production(self, staging_service):
        """The absence that mattered, pinned by name as well as by the rule
        above: without it the session cookie on a service with real logins
        is not Secure, and CSRF loses its SSL strictness."""
        assert _env_map(staging_service).get("BEHIND_HTTPS") == "1"

    def test_the_browser_pass_is_off_here_too(self, staging_service):
        """Same 512 MB, same measurement (E5.2: ~390 MB of Chromium over
        Flask). Staging having it on would only prove the OOM again."""
        env = _env_map(staging_service)
        assert env.get("TESTFORTGE_BROWSER_ENABLED") == "0"

    def test_it_is_on_the_free_plan(self, staging_service):
        """Stated so an upgrade is a decision somebody makes rather than
        something a copy-paste does: this is the $0 tier, and the business
        plan prices the alternative at $7."""
        assert staging_service.get("plan") == "free"


# ── Build minutes (free plan, 500/month) ─────────────────────────────


class TestNotEveryPushDeservesThreeBuilds:
    """The nearest free-tier ceiling, and it was on no pricing page.

    The free plan allows **500 build minutes a month**. This blueprint
    declares three web services from one Dockerfile, none of which had a
    build filter, so **one push to main cost three Docker builds**.
    Measured on 2026-08-12: three pushes, nine builds — and six of them
    rebuilt the image for commits that touched only ``docs/``. At four
    minutes a build the ceiling lands on the 41st push of the month.

    The dangerous half of the fix is the filter itself. An ignore list that
    grows to cover something the running app depends on does not fail: the
    build is simply skipped, the old image keeps serving, and the change is
    "deployed" in git and absent in production. Nothing errors, no log
    line, no failing test — which is why there is one here.
    """

    #: Real files whose change must always produce a build. Each is checked
    #: to exist, so this list cannot rot into a set of phantoms that match
    #: nothing and pass.
    LOAD_BEARING = (
        "app.py",
        "Dockerfile",
        "requirements.txt",
        "render.yaml",
        "engine/db.py",
        "engine/features.py",
        "engine/capacity.py",
        "engine/i18n/ua.py",
        "routes/settings.py",
        "routes/execution.py",
        "templates/base.html",
        "templates/org_settings.html",
        "templates/guide/_sections_ua.html",
        "static/css/style.css",
        "scripts/verify_storage.py",
        "mcp_server/server.py",
        "mcp_server/requirements.txt",
        "tests/test_render_blueprint.py",
    )

    @staticmethod
    def _matches(pattern: str, path: str) -> bool:
        """Deliberately **more** permissive than any real glob.

        Both ``**`` and ``*`` become ``.*``, so a single star is allowed to
        cross directory separators. Render's exact semantics are not the
        point: a pattern that could plausibly swallow a load-bearing file
        under any reading is one this repo should not carry, and a guard
        that depends on a vendor's glob dialect is a guard with a footnote.
        """
        regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", ".*")
        return re.fullmatch(regex, path) is not None

    def _filters(self, blueprint) -> dict[str, list[str]]:
        return {svc["name"]: (svc.get("buildFilter") or {}).get("ignoredPaths")
                for svc in blueprint["services"] if svc.get("type") == "web"}

    def test_every_web_service_has_one(self, blueprint):
        missing = sorted(name for name, paths in self._filters(blueprint).items()
                         if not paths)
        assert not missing, (
            f"{missing} rebuild for every commit, including documentation. "
            f"Three services × 500 build minutes is the ceiling this filter "
            f"exists to move.")

    def test_the_three_services_agree(self, blueprint):
        """Divergence here is not a safety problem, it is a comprehension
        one: two services skipping a commit while a third rebuilds is a
        state nobody can reason about from the dashboard."""
        distinct = {tuple(paths) for paths in self._filters(blueprint).values()}
        assert len(distinct) == 1, (
            f"the services ignore different paths: {distinct}")

    def test_the_load_bearing_files_are_real(self):
        """A guard whose subjects do not exist reports success for a list it
        never checked."""
        absent = [name for name in self.LOAD_BEARING
                  if not (REPO_ROOT / name).is_file()]
        assert not absent, f"this test is guarding files that are gone: {absent}"

    @pytest.mark.parametrize("path", LOAD_BEARING)
    def test_no_filter_can_swallow_something_the_app_runs_on(
            self, blueprint, path):
        for name, patterns in self._filters(blueprint).items():
            for pattern in patterns or ():
                assert not self._matches(pattern, path), (
                    f"{name} ignores {pattern!r}, which covers {path}. A "
                    f"change there would be skipped: git would say deployed, "
                    f"production would keep the old image, and nothing would "
                    f"report it.")

    def test_the_matcher_would_catch_the_mistake(self):
        """The pattern most likely to be added in a hurry, and the one that
        would hurt most."""
        assert self._matches("templates/**", "templates/base.html")
        assert self._matches("**/*.py", "engine/db.py")
        assert self._matches("*", "app.py")
        assert not self._matches("docs/**", "engine/db.py")

    def test_nobody_added_a_positive_path_list(self, blueprint):
        """``paths:`` inverts the default. Then every new directory is
        excluded until somebody remembers to add it — the same failure as
        above, arriving by omission instead of by a pattern."""
        offenders = [svc["name"] for svc in blueprint["services"]
                     if (svc.get("buildFilter") or {}).get("paths")]
        assert not offenders, (
            f"{offenders} declare buildFilter.paths. Keep the default at "
            f"'build' and list exceptions, so a forgotten directory fails "
            f"loudly rather than silently not deploying.")

    def test_documentation_is_actually_ignored(self, blueprint):
        """The measured win, asserted rather than assumed: six of nine
        builds on 2026-08-12 were docs-only."""
        for name, patterns in self._filters(blueprint).items():
            assert any(self._matches(p, "docs/plans/free_tier_mvp.md")
                       for p in patterns or ()), (
                f"{name} still rebuilds for a documentation change")
