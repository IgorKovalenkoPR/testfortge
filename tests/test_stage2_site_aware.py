"""Stage-2 site-aware pipeline tests.

Covers:
  * engine.site_recon — rule-based fallback + LLM happy path
  * engine.test_strategy — rule-based fallback + LLM happy path with
    ISTQB-RAG grounding mock
  * engine.testcase_generator.generate_from_strategy — TC/CL routing
  * engine.db.save_site_profile / load / list — round-trip
  * routes.generation._detect_first_url + _run_site_aware — wire-up

The tests never hit the network; ``call_messages`` is monkeypatched.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from engine import db as _db
from engine import site_recon as _recon
from engine import test_strategy as _strat
from engine.site_crawler import PageInfo, SiteAnalysis
from engine.testcase_generator import generate_from_strategy


# ── Helpers ────────────────────────────────────────────────────────

def _make_analysis(**kwargs):
    """Minimal SiteAnalysis with sensible defaults."""
    defaults = dict(
        base_url="https://shop.example.com",
        domain="shop.example.com",
        page_count=3,
        nav_items=["Home", "Products", "Cart", "Login"],
        has_auth=True,
        has_search=True,
        has_forms=True,
        has_payment=True,
        site_type="ecommerce",
        pages=[
            PageInfo(url="https://shop.example.com", title="Shop", h1="Welcome"),
            PageInfo(url="https://shop.example.com/cart", title="Cart"),
            PageInfo(url="https://shop.example.com/login", title="Sign in"),
        ],
    )
    defaults.update(kwargs)
    return SiteAnalysis(**defaults)


class _FakeResp:
    """Stand-in for an Anthropic SDK response — only ``content`` and
    ``usage`` are inspected by the recon/strategy modules."""

    def __init__(self, text: str):
        self.content = [SimpleNamespace(text=text)]
        self.usage = SimpleNamespace(
            input_tokens=10, output_tokens=20,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )


# ── Site Recon ─────────────────────────────────────────────────────

class TestSiteRecon:
    def test_rule_based_when_no_api_key(self, monkeypatch):
        """No API key → deterministic rule-based profile, source flag set."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        sa = _make_analysis()
        profile = _recon.recon_site(sa)
        assert profile.source == "rule_based"
        assert profile.site_type == "ecommerce"
        assert "auth" in profile.primary_flows
        assert "checkout" in profile.primary_flows
        assert profile.has_auth is True
        assert profile.has_payment is True
        # Key pages include homepage + login by title detection.
        roles = {p["role"] for p in profile.key_pages}
        assert "homepage" in roles
        assert "login" in roles

    def test_llm_happy_path(self, monkeypatch):
        """LLM returns valid JSON → profile carries ``source='llm'``."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        llm_text = (
            '{"site_type": "saas", '
            ' "primary_flows": ["auth", "subscription"], '
            ' "tech_hints": ["react-spa"], '
            ' "has_auth": true, "has_payment": false, '
            ' "has_search": false, "has_forms": true, '
            ' "key_pages": [{"url": "https://x.test", "role": "homepage"}], '
            ' "description": "A SaaS dashboard for QA teams.", '
            ' "target_audience": "QA engineers and SDETs"}'
        )
        monkeypatch.setattr(_recon, "call_messages",
                            lambda **kw: _FakeResp(llm_text))
        sa = _make_analysis(has_payment=False, has_search=False)
        profile = _recon.recon_site(sa, force_llm=True)
        assert profile.source == "llm"
        assert profile.site_type == "saas"
        assert "subscription" in profile.primary_flows
        assert profile.tech_hints == ["react-spa"]
        assert profile.description.startswith("A SaaS dashboard")

    def test_llm_unparseable_falls_back(self, monkeypatch):
        """Garbage LLM output → rule-based fallback, no exception."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setattr(_recon, "call_messages",
                            lambda **kw: _FakeResp("not json at all"))
        profile = _recon.recon_site(_make_analysis(), force_llm=True)
        assert profile.source == "rule_based"

    def test_llm_unavailable_falls_back(self, monkeypatch):
        """``LLMUnavailable`` from the wrapper → rule-based fallback."""
        from engine.llm_client import LLMUnavailable
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        def _boom(**_):
            raise LLMUnavailable("simulated outage")
        monkeypatch.setattr(_recon, "call_messages", _boom)
        profile = _recon.recon_site(_make_analysis(), force_llm=True)
        assert profile.source == "rule_based"

    def test_crawler_floor_preserved(self, monkeypatch):
        """LLM cannot turn a crawler-detected payment flag off — we
        trust the crawler's structural evidence as the floor."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        llm_text = (
            '{"site_type": "landing", "primary_flows": [], '
            ' "tech_hints": [], '
            ' "has_auth": false, "has_payment": false, '
            ' "has_search": false, "has_forms": false, '
            ' "key_pages": [], "description": "", "target_audience": ""}'
        )
        monkeypatch.setattr(_recon, "call_messages",
                            lambda **kw: _FakeResp(llm_text))
        sa = _make_analysis()  # crawler saw payment
        profile = _recon.recon_site(sa, force_llm=True)
        assert profile.has_payment is True


# ── Test Strategy ──────────────────────────────────────────────────

class TestTestStrategy:
    def test_rule_based_when_no_api_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        profile = _recon.SiteProfile(
            url="https://shop.example.com", site_type="ecommerce",
            primary_flows=["auth", "checkout", "payment"],
            has_auth=True, has_payment=True, has_forms=True,
        )
        s = _strat.build_strategy(profile)
        assert s.source == "rule_based"
        assert "Functional" in s.matrix
        assert "Accessibility" in s.matrix
        # Ecommerce + payment must add the checkout-flow checks.
        func_objectives = [c.objective for c in s.matrix["Functional"]]
        assert any("checkout" in o.lower() for o in func_objectives)

    def test_llm_happy_path(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        llm_text = (
            '{"rationale": "Focused on conversion-critical flows.", '
            ' "matrix": {"Functional": ['
            '   {"objective": "Verify hero CTA navigates to /signup", '
            '    "priority": "High", "url_pattern": "/", '
            '    "istqb_technique": "Use Case"}'
            '  ], "Accessibility": ['
            '   {"objective": "Verify hero CTA has visible focus ring", '
            '    "priority": "Medium"}'
            '  ]}}'
        )
        monkeypatch.setattr(_strat, "call_messages",
                            lambda **kw: _FakeResp(llm_text))
        profile = _recon.SiteProfile(url="https://x.test", site_type="landing")
        s = _strat.build_strategy(profile, force_llm=True)
        assert s.source == "llm"
        assert s.rationale.startswith("Focused on conversion")
        assert len(s.matrix["Functional"]) == 1
        assert s.matrix["Functional"][0].url_pattern == "/"

    def test_llm_unknown_category_dropped(self, monkeypatch):
        """LLM hallucinating an off-vocab category does not corrupt
        the matrix — the unknown key is dropped, known ones survive."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        llm_text = (
            '{"rationale": "x", "matrix": {'
            ' "Functional": [{"objective": "y", "priority": "High"}],'
            ' "Vibes":      [{"objective": "z", "priority": "Low"}]'
            '}}'
        )
        monkeypatch.setattr(_strat, "call_messages",
                            lambda **kw: _FakeResp(llm_text))
        profile = _recon.SiteProfile(url="https://x.test")
        s = _strat.build_strategy(profile, force_llm=True)
        assert "Functional" in s.matrix
        assert "Vibes" not in s.matrix


# ── generate_from_strategy ─────────────────────────────────────────

class TestGenerateFromStrategy:
    def test_high_priority_goes_to_tc_medium_to_checklist(self):
        # Summaries start with ``Verify`` so the Team Lead's
        # voice-normaliser leaves them untouched — we want to assert
        # on identity, not on canonicalised wording.
        profile = _recon.SiteProfile(url="https://x.test",
                                     site_type="saas")
        strategy = _strat.TestStrategy(
            site_url="https://x.test",
            matrix={
                "Functional": [
                    _strat.CheckSpec(objective="Verify hi-1 works",
                                     priority="High"),
                    _strat.CheckSpec(objective="Verify mid-1 works",
                                     priority="Medium"),
                ],
                "Accessibility": [
                    _strat.CheckSpec(objective="Verify hi-a11y works",
                                     priority="High"),
                    _strat.CheckSpec(objective="Verify low-a11y works",
                                     priority="Low"),
                ],
            },
            source="rule_based",
        )
        tcs, cls = generate_from_strategy(profile, strategy)
        tc_summaries = {tc.summary for tc in tcs}
        cl_objectives = {cl.objective for cl in cls}
        assert any("hi-1" in s for s in tc_summaries)
        assert any("hi-a11y" in s for s in tc_summaries)
        assert any("mid-1" in o for o in cl_objectives)
        assert any("low-a11y" in o for o in cl_objectives)
        # Functional TC gets SA1_NNN (SA prefix keeps site-aware IDs
        # disjoint from legacy SC*_NNN; Functional is section 1).
        func_tc = next(tc for tc in tcs if "hi-1" in tc.summary)
        assert func_tc.id == "SA1_001"
        assert func_tc.section == "Functional"
        # Accessibility CL gets SA_A11Y_NNN.
        a11y_cl = next(cl for cl in cls if "low-a11y" in cl.objective)
        assert a11y_cl.id.startswith("SA_A11Y_")

    def test_url_pattern_drives_trigger(self):
        profile = _recon.SiteProfile(url="https://x.test")
        strategy = _strat.TestStrategy(
            site_url="https://x.test",
            matrix={"Functional": [
                _strat.CheckSpec(objective="Verify bound URL is reachable",
                                 priority="High",
                                 url_pattern="/checkout/*"),
                _strat.CheckSpec(objective="Verify any-page rule applies",
                                 priority="High",
                                 url_pattern=""),
            ]},
            source="rule_based",
        )
        tcs, _ = generate_from_strategy(profile, strategy)
        bound_tc = next(tc for tc in tcs if "bound URL" in tc.summary)
        any_tc = next(tc for tc in tcs if "any-page" in tc.summary)
        assert bound_tc.trigger == "walkthrough_url_match"
        assert bound_tc.url_pattern == "/checkout/*"
        assert any_tc.trigger == "manual"

    def test_empty_strategy_returns_empty(self):
        profile = _recon.SiteProfile(url="https://x.test")
        tcs, cls = generate_from_strategy(profile, None)
        assert tcs == [] and cls == []
        empty = _strat.TestStrategy(site_url="https://x.test",
                                     matrix={}, source="rule_based")
        tcs, cls = generate_from_strategy(profile, empty)
        assert tcs == [] and cls == []


# ── DB site_profile round-trip ─────────────────────────────────────

class TestSiteProfileDB:
    def test_save_load_list_upsert(self):
        pid = _db.upsert_project(
            "Stage2_DB_Test",
            owner_sid="sid_stage2_" + os.urandom(4).hex(),
        )
        url = "https://stage2.test/" + os.urandom(4).hex()
        rid1 = _db.save_site_profile(pid, url, {"site_type": "saas"})
        assert isinstance(rid1, int) and rid1 > 0

        row = _db.load_site_profile_by_url(pid, url)
        assert row is not None
        assert row["profile"]["site_type"] == "saas"
        assert row["strategy"] is None

        # Upsert with strategy — same row id, fields updated.
        rid2 = _db.save_site_profile(pid, url,
                                      {"site_type": "ecommerce"},
                                      {"source": "llm"})
        assert rid2 == rid1
        row2 = _db.load_site_profile_by_url(pid, url)
        assert row2["profile"]["site_type"] == "ecommerce"
        assert row2["strategy"]["source"] == "llm"

        listed = _db.list_site_profiles(pid)
        assert any(r["url"] == url for r in listed)


# ── routes/_run_site_aware wire-up ────────────────────────────────

class TestRunSiteAware:
    def test_detect_first_url(self):
        from routes.generation import _detect_first_url
        assert _detect_first_url(["a", "https://x.test", "b"]) == "https://x.test"
        assert _detect_first_url(["plain"]) is None
        # Trailing punctuation should be stripped.
        assert _detect_first_url(["see https://x.test."]) == "https://x.test"

    def test_run_site_aware_persists_both_packs(self, monkeypatch):
        """End-to-end on the helper: crawl is mocked, both packs land
        in the DB, the site_profile row is written."""
        from routes import generation as g

        # Mock crawl_site by patching the import inside _run_site_aware.
        sa = _make_analysis()
        import engine.site_crawler as _sc
        monkeypatch.setattr(_sc, "crawl_site", lambda url: sa)

        # Disable LLM — exercise the rule-based path top to bottom.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        pid = _db.upsert_project(
            "Stage2_E2E", owner_sid="sid_e2e_" + os.urandom(4).hex(),
        )
        url = "https://shop.example.com/" + os.urandom(4).hex()
        result = g._run_site_aware(url, pid, "")
        assert result is not None
        assert result["tc_dicts"], "expected TC pack"
        assert result["cl_dicts"], "expected checklist pack"
        assert result["profile"]["source"] == "rule_based"
        assert result["strategy"]["source"] == "rule_based"

        # The site profile row exists and carries both blobs. The
        # strategy.site_url comes from the SiteProfile.url, which is
        # the crawler's base_url — we assert on the DB row's ``url``
        # key (the (project, url) lookup key) instead.
        row = _db.load_site_profile_by_url(pid, url)
        assert row is not None
        assert row["url"] == url
        assert row["strategy"]["source"] == "rule_based"
        assert row["profile"]["site_type"] == "ecommerce"
