"""The model switcher — engine/llm_prefs.py, llm_models.model_for, and the two
surfaces that set it (ADR 0003 §4.7).

The properties, in the order a bill depends on them:

* precedence: operator pin > legacy pin > user > org per-kind > org default
  > env tier > KIND_TIER (Haiku);
* the org's allow-list bounds what a user may pick, and ``users_may_choose``
  can switch user choice off;
* Sonnet and Opus on the **platform** key need
  ``LLM_ALLOW_PREMIUM_ON_PLATFORM_KEY``; a BYOK org needs only its admin;
* a non-switchable kind ignores every preference;
* with ``MODEL_SWITCHER`` off nothing of the above applies — routing is
  the environment plus Haiku defaults, and the endpoints are 404.
"""
from __future__ import annotations

import re
import secrets

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import llm_keys, llm_models, llm_prefs
from engine import permissions as _perm


GOOD_KEY = "sk-ant-" + "c" * 60
HAIKU, SONNET, OPUS = (llm_models.TIER_DEFAULTS[t]
                       for t in ("haiku", "sonnet", "opus"))


@pytest.fixture(autouse=True)
def _db_ready():
    _db.init_db()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for var in ("ANTHROPIC_MODEL", "ANTHROPIC_MODEL_SONNET",
                "ANTHROPIC_MODEL_HAIKU", "ANTHROPIC_MODEL_OPUS",
                llm_prefs.PREMIUM_ENV, llm_keys.ENCRYPTION_KEY_ENV,
                "ANTHROPIC_API_KEY", "CLAUDE_KEY"):
        monkeypatch.delenv(var, raising=False)
    for kind in llm_models.KINDS:
        monkeypatch.delenv(f"LLM_MODEL_FOR_{kind.upper()}", raising=False)
        monkeypatch.delenv(f"LLM_{kind.upper()}_TIER", raising=False)
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    monkeypatch.setenv("MODEL_SWITCHER", "1")
    llm_prefs.clear_cache()
    yield
    llm_prefs.clear_cache()


def _fernet_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def _email() -> str:
    return f"ms-{secrets.token_hex(6)}@example.com"


@pytest.fixture
def team():
    org = _db.create_organization(f"Switch {secrets.token_hex(4)}")
    pwd = _auth.hash_password("a perfectly good passphrase")
    admin = _db.create_user(_email(), password_hash=pwd)
    user = _db.create_user(_email(), password_hash=pwd)
    _db.add_org_member(org, admin, "admin")
    _db.add_org_member(org, user, "user")
    return {"org": org, "admin": admin, "user": user}


def _policy(org, **kw):
    base = {"default_tier": None, "per_kind": {},
            "allowed_tiers": ["haiku", "sonnet", "opus"],
            "users_may_choose": True}
    base.update(kw)
    llm_prefs.save_org_policy(org, base)


def _byok(monkeypatch, org):
    monkeypatch.setenv(llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
    llm_keys.set_org_key(org, GOOD_KEY)


def _route(kind, team, who="user"):
    return llm_models.model_for(kind, org_id=team["org"],
                                user_id=team[who])


# ── Precedence ────────────────────────────────────────────────────

class TestPrecedence:
    """Each step is checked by setting it *and every step below it* to
    something different, so a step that is skipped shows."""

    @pytest.fixture(autouse=True)
    def _everything_affordable(self, monkeypatch, team):
        _byok(monkeypatch, team["org"])

    def test_default_is_haiku(self, team):
        assert _route("consult", team) == HAIKU

    def test_env_tier_beats_kind_tier(self, team, monkeypatch):
        monkeypatch.setenv("LLM_CONSULT_TIER", "sonnet")
        assert _route("consult", team) == SONNET

    def test_org_default_beats_env_tier(self, team, monkeypatch):
        monkeypatch.setenv("LLM_CONSULT_TIER", "sonnet")
        _policy(team["org"], default_tier="opus")
        assert _route("consult", team) == OPUS

    def test_org_per_kind_beats_org_default(self, team):
        _policy(team["org"], default_tier="opus",
                per_kind={"consult": "sonnet"})
        assert _route("consult", team) == SONNET
        assert _route("authoring", team) == OPUS

    def test_user_beats_org(self, team):
        _policy(team["org"], default_tier="opus",
                per_kind={"consult": "opus"})
        llm_prefs.save_user_pref(team["user"], {"*": "sonnet"})
        assert _route("consult", team) == SONNET

    def test_user_per_kind_beats_user_star(self, team):
        _policy(team["org"])
        llm_prefs.save_user_pref(team["user"],
                                 {"*": "sonnet", "consult": "opus"})
        assert _route("consult", team) == OPUS
        assert _route("authoring", team) == SONNET

    def test_preferences_are_per_user(self, team):
        _policy(team["org"])
        llm_prefs.save_user_pref(team["user"], {"*": "opus"})
        assert _route("consult", team, "admin") == HAIKU

    def test_legacy_pin_beats_user_and_org(self, team, monkeypatch):
        _policy(team["org"], default_tier="opus")
        llm_prefs.save_user_pref(team["user"], {"*": "sonnet"})
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-legacy-x")
        assert _route("consult", team) == "claude-legacy-x"

    def test_operator_pin_beats_everything(self, team, monkeypatch):
        _policy(team["org"], default_tier="opus")
        llm_prefs.save_user_pref(team["user"], {"*": "sonnet"})
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-legacy-x")
        monkeypatch.setenv("LLM_MODEL_FOR_CONSULT", "claude-pinned-y")
        assert _route("consult", team) == "claude-pinned-y"

    def test_tier_var_repoints_a_switched_tier(self, team, monkeypatch):
        _policy(team["org"], default_tier="sonnet")
        monkeypatch.setenv("ANTHROPIC_MODEL_SONNET", "claude-sonnet-custom")
        assert _route("consult", team) == "claude-sonnet-custom"

    def test_a_non_switchable_kind_ignores_preferences(self, team):
        _policy(team["org"], default_tier="opus")
        llm_prefs.save_user_pref(team["user"], {"*": "opus"})
        assert _route("segmentation", team) == HAIKU
        assert _route("query_rewrite", team) == HAIKU

    def test_no_org_means_no_switching(self, team):
        llm_prefs.save_user_pref(team["user"], {"*": "opus"})
        assert llm_models.model_for("consult", user_id=team["user"]) == HAIKU


# ── The org's allow-list ──────────────────────────────────────────

class TestOrgAllowList:
    @pytest.fixture(autouse=True)
    def _byok_org(self, monkeypatch, team):
        _byok(monkeypatch, team["org"])

    def test_unsaved_policy_allows_haiku_only(self, team):
        llm_prefs.save_user_pref(team["user"], {"*": "opus"})
        assert _route("consult", team) == HAIKU
        assert llm_prefs.org_policy(team["org"])["allowed_tiers"] == ["haiku"]

    def test_a_user_choice_outside_the_allow_list_is_skipped(self, team):
        _policy(team["org"], allowed_tiers=["haiku", "sonnet"],
                default_tier="sonnet")
        llm_prefs.save_user_pref(team["user"], {"*": "opus"})
        # Not substituted with the nearest tier: the next candidate (the
        # org default) answers instead.
        assert _route("consult", team) == SONNET

    def test_users_may_choose_off_ignores_preferences(self, team):
        _policy(team["org"], default_tier="sonnet", users_may_choose=False)
        llm_prefs.save_user_pref(team["user"], {"*": "opus"})
        assert _route("consult", team) == SONNET

    def test_policy_refuses_a_default_outside_the_allow_list(self):
        with pytest.raises(ValueError, match="ms_err_default_not_allowed"):
            llm_prefs.validate_policy("opus", {}, ["haiku"], True)

    def test_policy_refuses_a_per_kind_outside_the_allow_list(self):
        with pytest.raises(ValueError, match="ms_err_kind_not_allowed"):
            llm_prefs.validate_policy(None, {"consult": "opus"},
                                      ["haiku"], True)

    def test_policy_refuses_an_empty_allow_list(self):
        with pytest.raises(ValueError, match="ms_err_no_tiers"):
            llm_prefs.validate_policy(None, {}, [], True)

    def test_a_raw_model_id_is_never_a_tier(self):
        with pytest.raises(ValueError):
            llm_prefs.validate_user_pref({"*": "claude-opus-5-5"})
        with pytest.raises(ValueError):
            llm_prefs.validate_policy(None, {}, ["claude-opus-5-5"], True)

    def test_a_non_switchable_kind_cannot_be_stored(self):
        with pytest.raises(ValueError, match="ms_err_unknown_kind"):
            llm_prefs.validate_user_pref({"segmentation": "opus"})

    def test_a_corrupt_stored_policy_degrades_to_the_default(self, team):
        _db.update_org_settings(team["org"], {llm_prefs.POLICY_KEY: "junk"})
        llm_prefs.invalidate(org_id=team["org"])
        assert llm_prefs.org_policy(team["org"])["allowed_tiers"] == ["haiku"]
        assert _route("consult", team) == HAIKU


# ── Money: premium on the platform key ────────────────────────────

class TestPremiumOnPlatformKey:
    @pytest.fixture(autouse=True)
    def _platform_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-platform-test")

    def test_premium_is_refused_on_the_platform_key(self, team):
        _policy(team["org"], default_tier="opus")
        llm_prefs.save_user_pref(team["user"], {"*": "sonnet"})
        assert _route("consult", team) == HAIKU

    def test_the_operator_can_allow_it(self, team, monkeypatch):
        monkeypatch.setenv(llm_prefs.PREMIUM_ENV, "1")
        _policy(team["org"], default_tier="opus")
        assert _route("consult", team) == OPUS

    def test_byok_allows_any_tier_the_admin_allows(self, team, monkeypatch):
        _byok(monkeypatch, team["org"])
        _policy(team["org"], default_tier="opus")
        assert _route("consult", team) == OPUS

    def test_the_operator_s_own_env_tier_is_not_gated(self, team, monkeypatch):
        # The operator chose it and pays for it; the premium rule guards
        # org and user choices, not the operator's.
        monkeypatch.setenv("LLM_CONSULT_TIER", "sonnet")
        assert _route("consult", team) == SONNET

    def test_the_ui_state_names_the_reason(self, team):
        _policy(team["org"])
        state = llm_prefs.switcher_state(org_id=team["org"],
                                         user_id=team["user"])
        reasons = {o["tier"]: o["reason"] for o in state["options"]}
        assert reasons == {"haiku": None,
                           "sonnet": llm_prefs.REASON_PREMIUM,
                           "opus": llm_prefs.REASON_PREMIUM}


# ── Operator pin disables the control ─────────────────────────────

class TestOperatorPin:
    def test_a_pinned_kind_disables_the_switcher(self, team, monkeypatch):
        monkeypatch.setenv("LLM_MODEL_FOR_CONSULT", "claude-pinned-y")
        _policy(team["org"])
        state = llm_prefs.switcher_state(org_id=team["org"],
                                         user_id=team["user"])
        assert state["pinned"] is True
        assert state["enabled"] is False
        assert "consult" in state["pinned_kinds"]
        assert all(o["reason"] == llm_prefs.REASON_PINNED
                   for o in state["options"])
        assert state["effective_model"] == "claude-pinned-y"

    def test_the_legacy_pin_disables_it_too(self, team, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-legacy-x")
        state = llm_prefs.switcher_state(org_id=team["org"],
                                         user_id=team["user"])
        assert state["pinned"] is True and state["enabled"] is False

    def test_the_api_refuses_a_choice_while_pinned(self, client, team,
                                                   monkeypatch):
        monkeypatch.setenv("LLM_MODEL_FOR_CONSULT", "claude-pinned-y")
        _as(client, team, "user")
        resp = client.put("/api/me/model-preference", json={"*": "haiku"})
        assert resp.status_code == 409
        assert resp.get_json()["reason"] == llm_prefs.REASON_PINNED


# ── Flag off = today's behaviour with Haiku defaults ──────────────

class TestFlagOff:
    @pytest.fixture(autouse=True)
    def _off(self, monkeypatch):
        monkeypatch.setenv("MODEL_SWITCHER", "0")

    def test_routing_ignores_org_and_user(self, team, monkeypatch):
        _byok(monkeypatch, team["org"])
        _policy(team["org"], default_tier="opus")
        llm_prefs.save_user_pref(team["user"], {"*": "sonnet"})
        assert _route("consult", team) == HAIKU
        monkeypatch.setenv("LLM_CONSULT_TIER", "sonnet")
        assert _route("consult", team) == SONNET

    def test_effective_tier_reads_nothing(self, team, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("queried the database with the flag off")
        monkeypatch.setattr(llm_prefs, "org_policy", _boom)
        monkeypatch.setattr(llm_prefs, "user_pref", _boom)
        assert llm_prefs.effective_tier("consult", org_id=team["org"],
                                        user_id=team["user"]) is None

    def test_the_flag_needs_org_mode(self, team, monkeypatch):
        monkeypatch.setenv("MODEL_SWITCHER", "1")
        monkeypatch.setenv("ORG_MODE", "0")
        assert llm_prefs.switcher_on() is False
        monkeypatch.setenv("ORG_MODE", "1")
        monkeypatch.setenv("AUTH_ENABLED", "0")
        # ORG_MODE without AUTH_ENABLED is off, so the switcher is too.
        assert llm_prefs.switcher_on() is False

    def test_endpoints_are_404(self, client, team):
        _as(client, team, "admin")
        assert client.get("/api/me/model-preference").status_code == 404
        assert client.put("/api/me/model-preference",
                          json={"*": "haiku"}).status_code == 404
        assert client.post("/org/settings/models",
                           data={"allowed_tiers": ["haiku"]}).status_code == 404

    def test_nothing_renders(self, client, team):
        _as(client, team, "admin")
        assert b"model-switcher" not in client.get("/guide").data
        assert b"/org/settings/models" not in client.get("/org/settings").data


# ── HTTP surfaces ─────────────────────────────────────────────────

def _as(client, team, who):
    with client.session_transaction() as sess:
        sess.clear()
        sess[_perm.SESSION_USER_KEY] = team[who]
        sess[_perm.SESSION_ORG_KEY] = team["org"]


class TestUserPreferenceApi:
    @pytest.fixture(autouse=True)
    def _byok_org(self, monkeypatch, team):
        _byok(monkeypatch, team["org"])
        _policy(team["org"], allowed_tiers=["haiku", "sonnet"])

    def test_get_describes_the_control(self, client, team):
        _as(client, team, "user")
        body = client.get("/api/me/model-preference").get_json()
        assert body["enabled"] is True
        assert body["pref"] == {}
        reasons = {o["tier"]: o["reason"] for o in body["options"]}
        assert reasons == {"haiku": None, "sonnet": None,
                           "opus": llm_prefs.REASON_ORG}

    def test_put_stores_and_routes(self, client, team):
        _as(client, team, "user")
        resp = client.put("/api/me/model-preference", json={"*": "sonnet"})
        assert resp.status_code == 200, resp.data
        assert resp.get_json()["effective_model"] == SONNET
        assert _route("consult", team) == SONNET

    def test_put_refuses_a_tier_the_org_does_not_allow(self, client, team):
        _as(client, team, "user")
        resp = client.put("/api/me/model-preference", json={"*": "opus"})
        assert resp.status_code == 409
        assert resp.get_json()["reason"] == llm_prefs.REASON_ORG
        assert llm_prefs.user_pref(team["user"]) == {}

    def test_put_refuses_garbage(self, client, team):
        _as(client, team, "user")
        assert client.put("/api/me/model-preference",
                          json={"*": "gpt-9"}).status_code == 400
        assert client.put("/api/me/model-preference",
                          json=["opus"]).status_code == 400

    def test_empty_clears_the_choice(self, client, team):
        _as(client, team, "user")
        client.put("/api/me/model-preference", json={"*": "sonnet"})
        resp = client.put("/api/me/model-preference", json={"*": ""})
        assert resp.status_code == 200
        assert resp.get_json()["pref"] == {}
        assert _route("consult", team) == HAIKU

    def test_anonymous_is_refused(self, anon_client):
        assert anon_client.get(
            "/api/me/model-preference").status_code in (302, 401)

    def test_the_sidebar_renders_the_control(self, client, team):
        _as(client, team, "user")
        body = client.get("/guide").get_data(as_text=True)
        assert 'id="model-switcher"' in body
        assert '<label for="model-switcher">' in body
        # Opus is listed, disabled, with its reason — not hidden.
        opus = re.search(r'<option value="opus"[^>]*>([^<]*)</option>', body)
        assert opus and "disabled" in opus.group(0)
        assert "not allowed by your org" in opus.group(1)


class TestCsrf:
    """Session-authenticated JSON writes keep CSRF protection (the editors
    and the chat do the same) — the browser sends ``X-CSRFToken``."""

    @pytest.fixture(autouse=True)
    def _csrf_on(self, app, monkeypatch):
        # Depends on ``app`` so it runs after conftest switches CSRF off —
        # without that ordering both tests below passed with CSRF disabled.
        monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", True)

    def test_csrf_is_really_on(self, app):
        assert app.config["WTF_CSRF_ENABLED"] is True

    def test_put_without_a_token_is_refused(self, client, team, monkeypatch):
        _byok(monkeypatch, team["org"])
        _policy(team["org"])
        _as(client, team, "user")
        resp = client.put("/api/me/model-preference", json={"*": "sonnet"})
        assert resp.status_code == 400
        assert llm_prefs.user_pref(team["user"]) == {}

    def test_put_with_the_page_token_is_accepted(self, client, team,
                                                 monkeypatch):
        _byok(monkeypatch, team["org"])
        _policy(team["org"])
        _as(client, team, "user")
        page = client.get("/guide").get_data(as_text=True)
        token = re.search(r'<meta name="csrf-token" content="([^"]+)"',
                          page).group(1)
        resp = client.put("/api/me/model-preference", json={"*": "sonnet"},
                          headers={"X-CSRFToken": token})
        assert resp.status_code == 200, resp.data


class TestOrgPolicyForm:
    def test_admin_saves_a_policy(self, client, team):
        _as(client, team, "admin")
        resp = client.post("/org/settings/models", data={
            "allowed_tiers": ["haiku", "sonnet"], "default_tier": "sonnet",
            "kind_consult": "haiku", "users_may_choose": "1"})
        assert resp.status_code == 302
        policy = llm_prefs.org_policy(team["org"])
        assert policy == {"default_tier": "sonnet",
                          "per_kind": {"consult": "haiku"},
                          "allowed_tiers": ["haiku", "sonnet"],
                          "users_may_choose": True}

    def test_saving_is_audited(self, client, team):
        _as(client, team, "admin")
        client.post("/org/settings/models",
                    data={"allowed_tiers": ["haiku"]})
        with _db.session_scope() as sess:
            rows = sess.query(_db.AuditLog).filter(
                _db.AuditLog.org_id == team["org"],
                _db.AuditLog.action == "set_model_policy").all()
            assert len(rows) == 1

    def test_an_invalid_policy_is_not_saved(self, client, team):
        _as(client, team, "admin")
        client.post("/org/settings/models",
                    data={"allowed_tiers": ["haiku"], "default_tier": "opus"})
        assert llm_prefs.org_policy(team["org"])["default_tier"] is None
        with client.session_transaction() as sess:
            flashes = sess.get("_flashes") or []
        assert any(cat == "error" for cat, _ in flashes)

    def test_a_plain_user_cannot_save(self, client, team):
        _as(client, team, "user")
        resp = client.post("/org/settings/models",
                           data={"allowed_tiers": ["opus"],
                                 "default_tier": "opus"})
        assert resp.status_code == 403
        assert llm_prefs.org_policy(team["org"])["default_tier"] is None

    def test_the_card_renders_for_an_admin(self, client, team):
        _as(client, team, "admin")
        body = client.get("/org/settings").get_data(as_text=True)
        assert 'action="/org/settings/models"' in body
        for control in ("ms-allow-haiku", "ms-default-tier",
                        "ms-kind-consult", "ms-users-choose"):
            assert f'for="{control}"' in body, control
