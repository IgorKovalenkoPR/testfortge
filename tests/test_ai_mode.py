"""AUTOMATION_AI_MODE — engine/ai_mode.py and its six callers (ADR 0003 §4.9).

* ``off`` makes **zero** model calls across generation, even with a key
  configured and even when a caller insists (``force_llm``) — while the
  rule engines still return a result;
* ``assist`` runs the rules first and calls the model only for the gap they
  leave or when the operator pressed "Improve with AI";
* ``full`` is the old behaviour;
* an org can lower the mode and never raise it.

Calls are counted at every module's ``call_messages`` name — the boundary
each caller actually crosses — not inferred from the output.
"""
from __future__ import annotations

import secrets
from types import SimpleNamespace

import pytest

from engine import ai_mode
from engine import auth as _auth
from engine import db as _db
from engine import llm_client, llm_context
from engine import permissions as _perm

_CALLER_MODULES = ("engine.tc_author", "engine.checklist_author",
                   "engine.site_recon", "engine.test_strategy",
                   "engine.session_segmenter", "engine.mockup_vision")


@pytest.fixture(autouse=True)
def _ready(monkeypatch):
    _db.init_db()
    monkeypatch.delenv("AUTOMATION_AI_MODE", raising=False)
    monkeypatch.delenv("AI_ASSIST_MIN_COVERAGE", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "t" * 60)


class _Counter:
    """Counts every model call and answers with an outage, so each caller
    falls back to its rules and the test still gets a result."""

    def __init__(self):
        self.kinds: list[str] = []

    def __call__(self, **kwargs):
        self.kinds.append(kwargs.get("kind"))
        raise llm_client.LLMUnavailable("counted, not called")


@pytest.fixture
def calls(monkeypatch):
    import importlib
    counter = _Counter()
    monkeypatch.setattr(llm_client, "call_messages", counter)
    for name in _CALLER_MODULES:
        monkeypatch.setattr(importlib.import_module(name), "call_messages",
                            counter)
    return counter


# ── Fixtures for a crawl whose rules cover everything ──────────────

_FORM = {"method": "post", "action": "/signup", "fields": [
    {"name": "email", "label": "Email", "type": "email", "required": True},
    {"name": "password", "label": "Password", "type": "password",
     "required": True},
]}


def _analysis():
    from engine.site_crawler import PageInfo, SiteAnalysis
    return SiteAnalysis(
        base_url="https://shop.example.com", domain="shop.example.com",
        page_count=1, nav_items=["Home"], has_auth=True, has_forms=True,
        has_payment=True, site_type="ecommerce",
        pages=[PageInfo(url="https://shop.example.com/signup",
                        title="Sign up", h1="Create account",
                        forms=[_FORM], buttons=["Create account"])])


def _artifacts():
    from engine import tc_author
    return tc_author.Artifacts(
        url="https://shop.example.com/signup",
        pages=[{"url": "https://shop.example.com/signup", "title": "Sign up",
                "h1": "Create account", "forms": [_FORM],
                "buttons": ["Create account"]}])


def _steps():
    from engine.automation_qa import AutomationStep
    return [AutomationStep(action="goto", target="https://app/login"),
            AutomationStep(action="fill", target="label=Email", value="x"),
            AutomationStep(action="click", target="role=button[name=Go]"),
            AutomationStep(action="click", target="role=link[name=Home]")]


# ── Resolution ─────────────────────────────────────────────────────

class TestResolution:
    def test_default_is_assist(self):
        assert ai_mode.env_mode() == "assist"

    def test_an_unknown_value_reads_as_the_default(self, monkeypatch):
        monkeypatch.setenv("AUTOMATION_AI_MODE", "turbo")
        assert ai_mode.env_mode() == "assist"

    @pytest.mark.parametrize("env,org,want", [
        ("full", "off", "off"), ("full", "assist", "assist"),
        ("assist", "off", "off"),
        # Never raised above the env value.
        ("assist", "full", "assist"), ("off", "full", "off"),
        ("off", "assist", "off"),
        ("assist", None, "assist"), ("full", "nonsense", "full"),
    ])
    def test_an_org_can_lower_but_not_raise(self, monkeypatch, env, org,
                                            want):
        monkeypatch.setenv("AUTOMATION_AI_MODE", env)
        org_id = _db.create_organization("Mode " + secrets.token_hex(4))
        _db.update_org_settings(org_id, {ai_mode.ORG_SETTING: org})
        assert ai_mode.effective_mode(org_id) == want
        with llm_context.bound(org_id=org_id):
            assert ai_mode.effective_mode() == want

    def test_assist_decision(self, monkeypatch):
        monkeypatch.setenv("AI_ASSIST_MIN_COVERAGE", "0.6")
        assert ai_mode.llm_allowed(coverage=1.0) is False
        assert ai_mode.llm_allowed(coverage=0.5) is True
        assert ai_mode.llm_allowed() is False
        assert ai_mode.llm_allowed(low_confidence=True) is True
        assert ai_mode.llm_allowed(explicit=True) is True
        with ai_mode.enhanced():
            assert ai_mode.llm_allowed(coverage=1.0) is True

    def test_off_refuses_even_an_explicit_request(self, monkeypatch):
        monkeypatch.setenv("AUTOMATION_AI_MODE", "off")
        with ai_mode.enhanced():
            assert ai_mode.llm_allowed(explicit=True,
                                       low_confidence=True) is False

    def test_the_request_is_carried_into_a_job(self):
        seen = []
        job = ai_mode.bind_enhance(
            lambda: seen.append(ai_mode.enhance_requested()), True)
        job()
        assert seen == [True]
        assert ai_mode.enhance_requested() is False


# ── off: zero calls, rules still answer ────────────────────────────

class TestOffMakesNoCalls:
    @pytest.fixture(autouse=True)
    def _off(self, monkeypatch):
        monkeypatch.setenv("AUTOMATION_AI_MODE", "off")

    def test_tc_author(self, calls):
        from engine import tc_author
        out = tc_author.author_test_cases(artifacts=_artifacts(),
                                          force_llm=True)
        assert calls.kinds == []
        assert out.source == "rules" and out.cases

    def test_checklist_author(self, calls):
        from engine import checklist_author
        out = checklist_author.author_checklist(
            artifacts=checklist_author.Artifacts(
                url="https://shop.example.com/signup",
                pages=_artifacts().pages),
            force_llm=True)
        assert calls.kinds == []
        assert out.source == "deterministic" and out.total

    def test_recon_and_strategy(self, calls):
        from engine import site_recon, test_strategy
        profile = site_recon.recon_site(_analysis(), force_llm=True)
        strategy = test_strategy.build_strategy(profile, force_llm=True)
        assert calls.kinds == []
        assert profile.source == "rule_based"
        assert strategy.total_checks() > 0

    def test_segmenter(self, calls):
        from engine import session_segmenter
        out = session_segmenter.segment(_steps())
        assert calls.kinds == []
        assert out

    def test_mockup_vision(self, calls):
        from engine import mockup_vision
        text, _raw, error = mockup_vision._call_claude_vision([b"png"], "")
        assert calls.kinds == []
        assert text == "" and "turned off" in error

    def test_the_generation_routes(self, client, calls, monkeypatch):
        from engine import site_crawler
        monkeypatch.setattr(site_crawler, "crawl_site",
                            lambda url, *a, **k: _analysis())
        monkeypatch.setattr(site_crawler, "_fetch_page",
                            lambda url: ("", "stubbed: no network"))
        for path in ("/test-cases", "/checklist"):
            for text in ("User can sign up with email and password",
                         "Check https://shop.example.com/signup"):
                resp = client.post(path, data={"input_text": text,
                                               "enhance": "1"})
                assert resp.status_code in (200, 302), (path, text)
        assert calls.kinds == []


# ── assist: rules first ────────────────────────────────────────────

class TestAssist:
    def test_rules_that_cover_it_make_no_call(self, calls, monkeypatch):
        from engine import site_crawler
        from routes.generation import _run_site_aware
        monkeypatch.setattr(site_crawler, "crawl_site",
                            lambda url, *a, **k: _analysis())
        out = _run_site_aware("https://shop.example.com/signup", None, "")
        assert out and out["tc_dicts"] and out["cl_dicts"]
        assert calls.kinds == []

    def test_improve_with_ai_calls_the_model(self, calls, monkeypatch):
        from engine import site_crawler
        from routes.generation import _run_site_aware
        monkeypatch.setattr(site_crawler, "crawl_site",
                            lambda url, *a, **k: _analysis())
        with ai_mode.enhanced():
            _run_site_aware("https://shop.example.com/signup", None, "")
        assert "authoring" in calls.kinds
        assert "analysis" in calls.kinds

    def test_a_gap_calls_the_model(self, calls):
        """Prompt-only input has no inventory for the rules to enumerate."""
        from engine import tc_author
        tc_author.author_test_cases(artifacts=tc_author.Artifacts(
            requirements=["User can reset a password"]))
        assert calls.kinds == ["authoring"]

    def test_a_generic_profile_is_low_confidence(self, calls):
        from engine import site_recon
        from engine.site_crawler import SiteAnalysis
        site_recon.recon_site(SiteAnalysis(base_url="https://x.test",
                                           domain="x.test", site_type=""))
        assert calls.kinds == ["analysis"]
        calls.kinds.clear()
        site_recon.recon_site(_analysis())
        assert calls.kinds == []

    def test_a_split_session_needs_no_model(self, calls):
        from engine import session_segmenter
        from engine.automation_qa import AutomationStep
        steps = [AutomationStep(action="goto", target="https://a/login"),
                 AutomationStep(action="fill", target="label=E", value="x"),
                 AutomationStep(action="submit", target="form"),
                 AutomationStep(action="goto", target="https://a/other"),
                 AutomationStep(action="click", target="role=link[name=A]"),
                 AutomationStep(action="click", target="role=link[name=B]")]
        out = session_segmenter.segment(steps)
        assert len(out) == 2
        assert calls.kinds == []

    def test_an_unsplit_session_asks_the_model(self, calls):
        from engine import session_segmenter
        session_segmenter.segment(_steps())
        assert calls.kinds == ["segmentation"]

    def test_full_is_the_old_behaviour(self, calls, monkeypatch):
        monkeypatch.setenv("AUTOMATION_AI_MODE", "full")
        from engine import tc_author
        tc_author.author_test_cases(artifacts=_artifacts())
        assert calls.kinds == ["authoring"]


class TestEnhanceThroughTheRoute:
    """The button's value reaches the job that does the authoring."""

    @pytest.fixture
    def seen(self, monkeypatch):
        from engine import tc_author
        record: list[bool] = []

        def _spy(**kwargs):
            record.append(ai_mode.enhance_requested())
            return tc_author.AuthoringResult(cases=[], source="deterministic")
        monkeypatch.setattr(tc_author, "author_test_cases", _spy)
        return record

    def test_generate_does_not_ask(self, client, seen):
        client.post("/test-cases", data={"input_text": "User can log in",
                                         "enhance": "0"})
        assert seen == [False]

    def test_improve_with_ai_asks(self, client, seen):
        # The no-script submit sends the hidden "0" and the button's "1".
        client.post("/test-cases", data={"input_text": "User can log in",
                                         "enhance": ["0", "1"]})
        assert seen == [True]

    def test_the_button_renders_only_in_assist(self, client, monkeypatch):
        body = client.get("/test-cases").get_data(as_text=True)
        assert 'id="tc-enhance-button"' in body
        assert 'class="btn-group"' in body
        monkeypatch.setenv("AUTOMATION_AI_MODE", "off")
        assert 'id="tc-enhance-button"' not in \
            client.get("/test-cases").get_data(as_text=True)
        monkeypatch.setenv("AUTOMATION_AI_MODE", "full")
        assert 'id="cl-enhance-button"' not in \
            client.get("/checklist").get_data(as_text=True)


# ── The org setting ────────────────────────────────────────────────

def _email() -> str:
    return f"aim-{secrets.token_hex(6)}@example.com"


@pytest.fixture
def team(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    org = _db.create_organization(f"AI mode {secrets.token_hex(4)}")
    pwd = _auth.hash_password("a perfectly good passphrase")
    admin = _db.create_user(_email(), password_hash=pwd)
    user = _db.create_user(_email(), password_hash=pwd)
    _db.add_org_member(org, admin, "admin")
    _db.add_org_member(org, user, "user")
    return {"org": org, "admin": admin, "user": user}


def _as(client, team, who):
    with client.session_transaction() as sess:
        sess.clear()
        sess[_perm.SESSION_USER_KEY] = team[who]
        sess[_perm.SESSION_ORG_KEY] = team["org"]


def _flashes(client):
    with client.session_transaction() as sess:
        return sess.get("_flashes") or []


class TestOrgSetting:
    def test_admin_lowers_the_mode(self, client, team):
        _as(client, team, "admin")
        resp = client.post("/org/settings/ai-mode",
                           data={"automation_ai_mode": "off"})
        assert resp.status_code == 302
        assert ai_mode.org_mode(team["org"]) == "off"
        assert ai_mode.effective_mode(team["org"]) == "off"

    def test_admin_cannot_raise_it(self, client, team, monkeypatch):
        monkeypatch.setenv("AUTOMATION_AI_MODE", "assist")
        _as(client, team, "admin")
        client.post("/org/settings/ai-mode",
                    data={"automation_ai_mode": "full"})
        assert ai_mode.org_mode(team["org"]) is None
        assert any(cat == "error" for cat, _ in _flashes(client))

    def test_an_empty_choice_follows_the_server(self, client, team):
        _db.update_org_settings(team["org"], {ai_mode.ORG_SETTING: "off"})
        _as(client, team, "admin")
        client.post("/org/settings/ai-mode", data={"automation_ai_mode": ""})
        assert ai_mode.org_mode(team["org"]) is None

    def test_a_plain_user_cannot_change_it(self, client, team):
        _as(client, team, "user")
        resp = client.post("/org/settings/ai-mode",
                           data={"automation_ai_mode": "off"})
        assert resp.status_code == 403
        assert ai_mode.org_mode(team["org"]) is None

    def test_the_card_renders_with_a_label(self, client, team):
        _as(client, team, "admin")
        body = client.get("/org/settings").get_data(as_text=True)
        assert 'action="/org/settings/ai-mode"' in body
        assert 'for="aim-mode"' in body and 'id="aim-mode"' in body
        # The ceiling is assist, so "full" is not offered.
        assert 'value="full"' not in body.split('id="aim-mode"')[1] \
            .split("</select>")[0]

    def test_an_org_set_to_off_makes_no_calls_under_full(
            self, client, team, calls, monkeypatch):
        monkeypatch.setenv("AUTOMATION_AI_MODE", "full")
        _db.update_org_settings(team["org"], {ai_mode.ORG_SETTING: "off"})
        _as(client, team, "user")
        resp = client.post("/test-cases", data={
            "input_text": "User can sign up with email and password",
            "enhance": "1"})
        assert resp.status_code in (200, 302)
        assert calls.kinds == []

    def test_the_change_is_audited(self, client, team):
        _as(client, team, "admin")
        client.post("/org/settings/ai-mode",
                    data={"automation_ai_mode": "off"})
        with _db.session_scope() as sess:
            assert sess.query(_db.AuditLog).filter(
                _db.AuditLog.org_id == team["org"],
                _db.AuditLog.action == "set_ai_mode").count() == 1


def test_mode_is_declared_in_the_blueprint():
    import os
    import yaml
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "render.yaml"), encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    for svc in doc.get("services") or []:
        keys = {e.get("key"): e.get("value") for e in svc.get("envVars") or []}
        if "LLM_ORG_BUDGET_USD" in keys:
            assert keys.get("AUTOMATION_AI_MODE") == "assist", svc["name"]
            assert keys.get("LLM_RESULT_CACHE_DAYS") == "30", svc["name"]
