"""Model routing, cost accounting and BYOK — E0.7 / E0.8 / E0.9.

Covers engine/llm_models.py, engine/llm_cost.py, engine/llm_keys.py and the
three of them meeting inside engine/llm_client.call_messages.
"""

import secrets
from datetime import datetime, timedelta, timezone

import pytest

from engine import db as _db
from engine import llm_client, llm_cost, llm_keys, llm_models


@pytest.fixture(autouse=True)
def _db_ready():
    _db.init_db()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Routing reads the environment at call time; start from a blank slate."""
    for var in ("ANTHROPIC_MODEL", "ANTHROPIC_MODEL_SONNET",
                "ANTHROPIC_MODEL_HAIKU", "LLM_ORG_BUDGET_USD",
                llm_keys.ENCRYPTION_KEY_ENV):
        monkeypatch.delenv(var, raising=False)
    for kind in llm_models.KINDS:
        monkeypatch.delenv(f"LLM_MODEL_FOR_{kind.upper()}", raising=False)
        monkeypatch.delenv(f"LLM_{kind.upper()}_TIER", raising=False)


def _org(budget_usd=None) -> str:
    org = _db.create_organization(f"Team {secrets.token_hex(4)}")
    if budget_usd is not None:
        _db.update_org_settings(org, {"llm_budget_usd": budget_usd})
    return org


class _FakeUsage:
    def __init__(self, i=0, o=0, cr=0, cw=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = cr
        self.cache_creation_input_tokens = cw


class _FakeResponse:
    def __init__(self, usage=None):
        self.usage = usage
        self.content = []


# ── E0.8 routing ──────────────────────────────────────────────────

class TestModelRouting:
    def test_every_kind_resolves_to_a_model(self):
        for kind in llm_models.KINDS:
            assert llm_models.model_for(kind)

    def test_authoring_is_sonnet_and_segmentation_is_haiku(self):
        # The one downgrade taken without an eval harness, and the reason
        # is that its output is schema-validated with a deterministic
        # fallback — verifiably good enough, not hopefully.
        assert "sonnet" in llm_models.model_for("authoring")
        assert "haiku" in llm_models.model_for("segmentation")

    def test_consult_stays_on_sonnet_until_an_eval_exists(self):
        # Deliberate. Moving Tedgie to Haiku is the biggest single saving
        # available and it stays unclaimed until E6.7 can show the
        # golden-set score holding.
        assert "sonnet" in llm_models.model_for("consult")

    def test_unknown_kind_raises_rather_than_guessing(self):
        with pytest.raises(llm_models.UnknownKind):
            llm_models.model_for("authorring")

    def test_per_kind_override_wins(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL_FOR_CONSULT", "claude-experimental-9")
        assert llm_models.model_for("consult") == "claude-experimental-9"
        assert llm_models.model_for("authoring") != "claude-experimental-9"

    def test_a_kind_can_be_moved_to_another_tier(self, monkeypatch):
        monkeypatch.setenv("LLM_CONSULT_TIER", "haiku")
        assert "haiku" in llm_models.model_for("consult")

    def test_an_unknown_tier_falls_back_and_warns(self, monkeypatch):
        monkeypatch.setenv("LLM_CONSULT_TIER", "platinum")
        assert "sonnet" in llm_models.model_for("consult")
        assert any("platinum" in w for w in llm_models.routing_warnings())

    def test_legacy_tier_var_still_repoints_a_tier(self, monkeypatch):
        # An existing deployment that set this for cost reasons must not
        # be silently overridden by the new defaults.
        monkeypatch.setenv("ANTHROPIC_MODEL_SONNET", "claude-sonnet-4-6")
        assert llm_models.model_for("authoring") == "claude-sonnet-4-6"

    def test_legacy_global_pin_is_honoured_but_reported(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        assert llm_models.model_for("authoring") == "claude-sonnet-4-5"
        assert llm_models.model_for("segmentation") == "claude-sonnet-4-5"
        # …and says so, rather than leaving someone wondering why
        # LLM_CONSULT_TIER did nothing.
        assert any("ANTHROPIC_MODEL" in w
                   for w in llm_models.routing_warnings())

    def test_no_warnings_on_a_clean_environment(self):
        assert llm_models.routing_warnings() == []

    @pytest.mark.parametrize("model,tier", [
        ("claude-haiku-4-5-20251001", "haiku"),
        ("claude-sonnet-5", "sonnet"),
        ("claude-opus-5", "opus"),
        ("some-other-model", None),
        ("", None),
    ])
    def test_tier_reverse_lookup(self, model, tier):
        assert llm_models.tier_of(model) == tier


# ── E0.7 pricing ──────────────────────────────────────────────────

class TestPricing:
    def test_usage_is_extracted_from_a_response(self):
        u = llm_cost.extract_usage(_FakeResponse(_FakeUsage(100, 50, 20, 10)))
        assert u == llm_cost.Usage(100, 50, 20, 10)

    def test_a_response_without_usage_is_all_zeroes(self):
        assert llm_cost.extract_usage(_FakeResponse()) == llm_cost.Usage(0, 0, 0, 0)
        assert llm_cost.extract_usage(object()) == llm_cost.Usage(0, 0, 0, 0)

    def test_a_million_tokens_costs_the_published_rate(self):
        # Sonnet 5: $2 in / $10 out per million.
        cost = llm_cost.cost_micros("claude-sonnet-5",
                                    llm_cost.Usage(1_000_000, 0, 0, 0))
        assert cost == pytest.approx(2 * llm_cost.MICROS_PER_USD, rel=1e-6)
        cost = llm_cost.cost_micros("claude-sonnet-5",
                                    llm_cost.Usage(0, 1_000_000, 0, 0))
        assert cost == pytest.approx(10 * llm_cost.MICROS_PER_USD, rel=1e-6)

    def test_haiku_is_half_of_sonnet(self):
        usage = llm_cost.Usage(1_000_000, 1_000_000, 0, 0)
        assert (llm_cost.cost_micros("claude-haiku-4-5-20251001", usage) * 2
                == pytest.approx(
                    llm_cost.cost_micros("claude-sonnet-5", usage), rel=1e-6))

    def test_cache_reads_are_a_tenth_and_writes_are_more_than_input(self):
        # Collapsing these into "input" gets the cost wrong in both
        # directions at once, and prompt caching creates mostly reads.
        read = llm_cost.cost_micros("claude-sonnet-5",
                                    llm_cost.Usage(0, 0, 1_000_000, 0))
        plain = llm_cost.cost_micros("claude-sonnet-5",
                                     llm_cost.Usage(1_000_000, 0, 0, 0))
        write = llm_cost.cost_micros("claude-sonnet-5",
                                     llm_cost.Usage(0, 0, 0, 1_000_000))
        assert read * 10 == pytest.approx(plain, rel=1e-6)
        assert write > plain

    def test_an_unknown_model_prices_as_the_most_expensive_tier(self):
        # Understating spend is the wrong direction to be wrong in.
        usage = llm_cost.Usage(1_000_000, 0, 0, 0)
        assert (llm_cost.cost_micros("mystery-model-7", usage)
                == llm_cost.cost_micros("claude-opus-5", usage))

    def test_a_tiny_call_never_rounds_to_free(self):
        # A thousand calls each rounding down to nothing is a meter that
        # reads empty while the bill is real.
        assert llm_cost.cost_micros("claude-sonnet-5",
                                    llm_cost.Usage(1, 0, 0, 0)) >= 1

    def test_zero_usage_costs_zero(self):
        assert llm_cost.cost_micros("claude-sonnet-5",
                                    llm_cost.Usage(0, 0, 0, 0)) == 0

    def test_small_amounts_are_not_displayed_as_zero(self):
        assert llm_cost.format_usd(0) == "$0.00"
        assert llm_cost.format_usd(1_500_000) == "$1.50"
        assert llm_cost.format_usd(500) == "$0.0005"


# ── E0.7 metering + budget ────────────────────────────────────────

class TestUsageAccounting:
    def test_a_call_is_recorded_and_summarised(self):
        org = _org()
        assert _db.record_llm_usage(
            kind="authoring", model="claude-sonnet-5", org_id=org,
            input_tokens=1000, output_tokens=500, cost_micros=7000)
        summary = _db.llm_usage_summary(org)
        assert summary["calls"] == 1
        assert summary["total_micros"] == 7000
        assert summary["by_kind"]["authoring"]["input_tokens"] == 1000
        assert summary["by_model"]["claude-sonnet-5"]["calls"] == 1

    def test_spend_is_scoped_to_the_organisation(self):
        a, b = _org(), _org()
        _db.record_llm_usage(kind="authoring", model="claude-sonnet-5",
                             org_id=a, cost_micros=5000)
        assert _db.org_spend_micros(a) == 5000
        assert _db.org_spend_micros(b) == 0

    def test_byok_spend_is_excluded_from_the_platform_total(self):
        # The budget caps what the operator pays. An org spending its own
        # key's money must not count against it.
        org = _org()
        _db.record_llm_usage(kind="authoring", model="claude-sonnet-5",
                             org_id=org, key_source="org", cost_micros=99_000)
        assert _db.org_spend_micros(org, key_source="platform") == 0
        assert _db.org_spend_micros(org, key_source=None) == 99_000

    def test_metering_never_raises(self):
        assert _db.record_llm_usage(kind="", model="") is None

    def test_old_rows_are_purgeable(self):
        org = _org()
        _db.record_llm_usage(kind="authoring", model="claude-sonnet-5",
                             org_id=org, cost_micros=1)
        with _db.session_scope() as sess:
            row = sess.query(_db.LlmUsage).filter(
                _db.LlmUsage.org_id == org).one()
            row.at = datetime.now(timezone.utc) - timedelta(days=500)
        assert _db.purge_llm_usage(older_than_days=400) >= 1
        assert _db.org_spend_micros(org, key_source=None) == 0


class TestBudget:
    def test_default_budget_comes_from_the_environment_at_call_time(self, monkeypatch):
        # Read at call time, not at import: a cap captured at import cannot
        # be changed without a redeploy, and cannot be tested at all.
        monkeypatch.setenv("LLM_ORG_BUDGET_USD", "9")
        assert llm_cost.default_budget_usd() == 9.0
        assert llm_cost.org_budget_micros({}) == 9 * llm_cost.MICROS_PER_USD
        monkeypatch.setenv("LLM_ORG_BUDGET_USD", "0")
        assert llm_cost.org_budget_micros({}) == 0

    def test_a_nonsense_default_falls_back_with_a_warning(self, monkeypatch):
        monkeypatch.setenv("LLM_ORG_BUDGET_USD", "plenty")
        assert llm_cost.default_budget_usd() > 0

    def test_an_org_setting_overrides_the_default(self):
        assert llm_cost.org_budget_micros({"llm_budget_usd": 12}) == \
            12 * llm_cost.MICROS_PER_USD

    def test_zero_means_unlimited(self):
        state = llm_cost.budget_state(_org(), {"llm_budget_usd": 0})
        assert state["over"] is False
        assert state["limit_micros"] == 0

    def test_a_nonsense_budget_falls_back_rather_than_crashing(self):
        assert llm_cost.org_budget_micros({"llm_budget_usd": "lots"}) >= 0

    def test_over_budget_is_detected(self):
        org = _org(budget_usd=1)
        _db.record_llm_usage(kind="authoring", model="claude-sonnet-5",
                             org_id=org, cost_micros=2 * llm_cost.MICROS_PER_USD)
        state = llm_cost.budget_state(org, {"llm_budget_usd": 1})
        assert state["over"] is True
        assert state["ratio"] > 1

    def test_a_byok_org_is_never_over_budget(self):
        org = _org(budget_usd=1)
        _db.record_llm_usage(kind="authoring", model="claude-sonnet-5",
                             org_id=org, key_source="org",
                             cost_micros=100 * llm_cost.MICROS_PER_USD)
        state = llm_cost.budget_state(org, {"llm_budget_usd": 1},
                                      key_source="org")
        assert state["over"] is False


# ── E0.9 BYOK ─────────────────────────────────────────────────────

def _fernet_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


_GOOD_KEY = "sk-ant-" + "a" * 60


class TestByokKeyShape:
    @pytest.mark.parametrize("bad,expect", [
        ("", "empty"),
        ("not-a-key-at-all-but-long-enough-to-pass-length", "starts with"),
        ("sk-ant-short", "truncated"),
        ("sk-ant-" + "a" * 30 + " " + "b" * 30, "whitespace"),
    ])
    def test_obvious_mistakes_are_named(self, bad, expect):
        problem = llm_keys.validate_key_shape(bad)
        assert problem and expect in problem

    def test_a_plausible_key_passes(self):
        assert llm_keys.validate_key_shape(_GOOD_KEY) is None

    def test_redaction_shows_only_the_tail(self):
        assert llm_keys.redact(_GOOD_KEY) == "…aaaa"
        assert _GOOD_KEY not in llm_keys.redact(_GOOD_KEY)
        assert llm_keys.redact(None) == "—"


class TestByokStorage:
    def test_storing_is_refused_without_an_encryption_key(self):
        # Refusing beats storing a customer credential in the clear.
        assert llm_keys.is_configured() is False
        with pytest.raises(llm_keys.BYOKUnavailable):
            llm_keys.set_org_key(_org(), _GOOD_KEY)

    def test_round_trip_with_encryption_configured(self, monkeypatch):
        monkeypatch.setenv(llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        org = _org()
        llm_keys.set_org_key(org, _GOOD_KEY)
        assert llm_keys.get_org_key(org) == _GOOD_KEY

    def test_the_stored_value_is_not_the_plaintext(self, monkeypatch):
        monkeypatch.setenv(llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        org = _org()
        llm_keys.set_org_key(org, _GOOD_KEY)
        stored = _db.get_org_secret(org, "anthropic_api_key")
        assert stored and _GOOD_KEY not in stored

    def test_a_bad_shape_is_refused_before_encryption(self, monkeypatch):
        monkeypatch.setenv(llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        with pytest.raises(ValueError):
            llm_keys.set_org_key(_org(), "nonsense")

    def test_a_rotated_encryption_key_degrades_instead_of_raising(self, monkeypatch):
        monkeypatch.setenv(llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        org = _org()
        llm_keys.set_org_key(org, _GOOD_KEY)
        monkeypatch.setenv(llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        # Reads must not break the request — the platform key takes over.
        assert llm_keys.get_org_key(org) is None

    def test_clearing_falls_back_to_the_platform_key(self, monkeypatch):
        monkeypatch.setenv(llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-platform")
        org = _org()
        llm_keys.set_org_key(org, _GOOD_KEY)
        assert llm_keys.resolve_key(org) == (_GOOD_KEY, "org")
        assert llm_keys.clear_org_key(org) is True
        assert llm_keys.resolve_key(org) == ("sk-ant-platform", "platform")

    def test_an_arbitrary_long_secret_is_accepted_and_derived(self, monkeypatch):
        # Render's generateValue mints a random string, not a Fernet key.
        # Rejecting it would have left BYOK silently off in production
        # while the blueprint said the secret was configured.
        monkeypatch.setenv(llm_keys.ENCRYPTION_KEY_ENV, "z" * 48)
        org = _org()
        llm_keys.set_org_key(org, _GOOD_KEY)
        assert llm_keys.get_org_key(org) == _GOOD_KEY

    def test_derivation_is_stable_across_calls(self, monkeypatch):
        # Otherwise a restart would orphan every stored key.
        monkeypatch.setenv(llm_keys.ENCRYPTION_KEY_ENV, "q" * 40)
        org = _org()
        llm_keys.set_org_key(org, _GOOD_KEY)
        assert llm_keys.get_org_key(org) == _GOOD_KEY
        assert llm_keys.get_org_key(org) == _GOOD_KEY

    def test_a_short_secret_is_refused_rather_than_stretched(self, monkeypatch):
        # A 6-character secret would derive a valid-looking key with far
        # less entropy than it appears to have.
        monkeypatch.setenv(llm_keys.ENCRYPTION_KEY_ENV, "hunter2")
        with pytest.raises(llm_keys.BYOKUnavailable):
            llm_keys.set_org_key(_org(), _GOOD_KEY)

    def test_existence_can_be_checked_without_decrypting(self, monkeypatch):
        monkeypatch.setenv(llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        org = _org()
        assert _db.has_org_secret(org, "anthropic_api_key") is False
        llm_keys.set_org_key(org, _GOOD_KEY)
        assert _db.has_org_secret(org, "anthropic_api_key") is True


class TestKeyResolutionOrder:
    def test_no_key_anywhere_reports_none(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert llm_keys.resolve_key(None) == (None, "none")

    def test_platform_key_is_used_when_the_org_has_none(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-platform")
        assert llm_keys.resolve_key(_org()) == ("sk-ant-platform", "platform")

    def test_org_key_beats_the_platform_key(self, monkeypatch):
        monkeypatch.setenv(llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-platform")
        org = _org()
        llm_keys.set_org_key(org, _GOOD_KEY)
        key, source = llm_keys.resolve_key(org)
        assert (key, source) == (_GOOD_KEY, "org")


# ── The three meeting inside call_messages ────────────────────────

class TestCallMessagesIntegration:
    @pytest.fixture
    def captured(self, monkeypatch):
        """Replace the SDK client with a recorder."""
        # A platform key, so these exercise the production shape rather
        # than the no-key-configured one.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-platform-test")
        seen: dict = {}

        class _Messages:
            @staticmethod
            def create(**kwargs):
                seen.update(kwargs)
                return _FakeResponse(_FakeUsage(1000, 500, 0, 0))

        class _Client:
            messages = _Messages()

        monkeypatch.setattr(llm_client, "_build_client",
                            lambda api_key=None: _Client())
        monkeypatch.setattr(llm_client, "_retryable_exception_types",
                            lambda: ())
        return seen

    def test_kind_selects_the_model(self, captured):
        llm_client.call_messages(kind="segmentation", max_tokens=10,
                                 messages=[])
        assert "haiku" in captured["model"]

    def test_an_explicit_model_still_wins(self, captured):
        # Backwards compatibility: the older call shape must be untouched.
        llm_client.call_messages(model="claude-sonnet-5", max_tokens=10,
                                 messages=[])
        assert captured["model"] == "claude-sonnet-5"

    def test_kind_is_not_forwarded_to_the_sdk(self, captured):
        # It is our routing dimension, not an Anthropic parameter.
        llm_client.call_messages(kind="authoring", max_tokens=10, messages=[])
        assert "kind" not in captured
        assert "org_id" not in captured

    def test_a_successful_call_is_metered_against_the_org(self, captured):
        org = _org()
        llm_client.call_messages(kind="authoring", org_id=org, max_tokens=10,
                                 messages=[])
        summary = _db.llm_usage_summary(org)
        assert summary["calls"] == 1
        assert summary["by_kind"]["authoring"]["output_tokens"] == 500
        assert summary["total_micros"] > 0

    def test_a_response_without_usage_records_no_row(self, monkeypatch):
        class _Messages:
            @staticmethod
            def create(**kwargs):
                return _FakeResponse()          # no usage at all

        class _Client:
            messages = _Messages()

        monkeypatch.setattr(llm_client, "_build_client",
                            lambda api_key=None: _Client())
        monkeypatch.setattr(llm_client, "_retryable_exception_types",
                            lambda: ())
        org = _org()
        llm_client.call_messages(kind="authoring", org_id=org, messages=[])
        # A meter full of zero rows reads like "a thousand free calls".
        assert _db.llm_usage_summary(org)["calls"] == 0

    def test_over_budget_raises_the_unavailable_subclass(self, captured):
        # Every existing caller catches LLMUnavailable and falls through to
        # its deterministic path, so this degrades the platform instead of
        # erroring it.
        org = _org(budget_usd=1)
        _db.record_llm_usage(kind="authoring", model="claude-sonnet-5",
                             org_id=org,
                             cost_micros=2 * llm_cost.MICROS_PER_USD)
        with pytest.raises(llm_client.LLMBudgetExceeded):
            llm_client.call_messages(kind="authoring", org_id=org, messages=[])
        assert issubclass(llm_client.LLMBudgetExceeded,
                          llm_client.LLMUnavailable)

    def test_a_byok_org_is_not_stopped_by_the_budget(self, captured, monkeypatch):
        monkeypatch.setenv(llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        org = _org(budget_usd=1)
        llm_keys.set_org_key(org, _GOOD_KEY)
        _db.record_llm_usage(kind="authoring", model="claude-sonnet-5",
                             org_id=org, key_source="org",
                             cost_micros=99 * llm_cost.MICROS_PER_USD)
        llm_client.call_messages(kind="authoring", org_id=org, messages=[])
        assert captured["model"]

    def test_a_call_with_no_org_is_not_budget_gated(self, captured):
        # Anonymous / legacy usage keeps working while ORG_MODE is off.
        llm_client.call_messages(kind="authoring", messages=[])
        assert captured["model"]

    def test_the_gate_is_armed_even_with_no_platform_key(self, monkeypatch):
        # Regression: the check used to require key_source == "platform",
        # so an instance with no ANTHROPIC_API_KEY set skipped the budget
        # entirely — the guard was only armed when a key happened to exist.
        class _Client:
            class messages:
                @staticmethod
                def create(**kwargs):
                    return _FakeResponse(_FakeUsage(10, 10, 0, 0))

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(llm_client, "_build_client",
                            lambda api_key=None: _Client())
        monkeypatch.setattr(llm_client, "_retryable_exception_types",
                            lambda: ())
        org = _org(budget_usd=1)
        _db.record_llm_usage(kind="authoring", model="claude-sonnet-5",
                             org_id=org,
                             cost_micros=2 * llm_cost.MICROS_PER_USD)
        with pytest.raises(llm_client.LLMBudgetExceeded):
            llm_client.call_messages(kind="authoring", org_id=org, messages=[])

    def test_missing_key_still_raises_unavailable(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(llm_client.LLMUnavailable):
            llm_client.call_messages(kind="authoring", messages=[])
