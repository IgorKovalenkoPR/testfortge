"""Tests covering the Test Plan ↔ input-block wire-up.

Before this Sprint the Test Plan generator ignored ``raw_lines`` /
files / URLs from the input block entirely — it only consumed
``custom_prompt`` + features from previously-generated TCs. These
tests pin the new contract: site_analysis enriches Section 6 (Features)
and Section 4.1 (In Scope); empty input still produces a domain-default
scaffold; and the route flashes a warning on partial crawl failures.
"""
from types import SimpleNamespace
from unittest.mock import patch

from engine.advisor import ProjectContext
from engine.test_plan_generator import generate_test_plan


def _ctx(domain: str = "ecommerce", platform: str = "web") -> ProjectContext:
    return ProjectContext(
        project_name="WireUp Test", domain=domain, platform=platform,
    )


def _fake_site_analysis(features=("login", "search"),
                       nav=("Home", "Products", "Cart"),
                       errors=()):
    """Stand-in for engine.site_crawler.SiteAnalysis — only the fields
    test_plan_generator actually reads."""
    return SimpleNamespace(
        features_detected=list(features),
        nav_items=list(nav),
        pages=[],
        crawl_errors=list(errors),
    )


# ── Generator-level: backward-compat + enrichment ─────────────────

def test_empty_input_falls_back_to_domain_defaults():
    plan = generate_test_plan(_ctx(domain="ecommerce"))
    sec6 = next(s for s in plan.sections if s.number == "6")
    # Domain defaults for ecommerce should fill the Features section.
    assert sec6.content
    assert "Below, there is a list" in sec6.content


def test_site_analysis_features_appear_in_section_6():
    plan = generate_test_plan(
        _ctx(),
        site_analysis=_fake_site_analysis(features=("auth", "search")),
    )
    sec6 = next(s for s in plan.sections if s.number == "6")
    assert "auth" in sec6.content
    assert "search" in sec6.content
    assert "from URL crawl" in sec6.content


def test_raw_lines_bullets_appear_in_section_6():
    plan = generate_test_plan(
        _ctx(),
        raw_lines=["User can log in with email", "Cart updates total"],
    )
    sec6 = next(s for s in plan.sections if s.number == "6")
    assert "log in" in sec6.content
    assert "Cart updates total" in sec6.content


def test_site_analysis_nav_items_enrich_in_scope():
    plan = generate_test_plan(
        _ctx(), site_analysis=_fake_site_analysis(nav=("Home", "Login")),
    )
    sec4 = next(s for s in plan.sections if s.number == "4")
    in_scope = next(s for s in sec4.subsections if s["title"].startswith("4.1"))
    assert "Navigation flows" in in_scope["content"]
    assert "Login" in in_scope["content"]


def test_url_lines_skipped_from_features():
    # A URL in raw_lines is for the crawler, not a feature bullet.
    plan = generate_test_plan(
        _ctx(),
        raw_lines=["https://example.com/", "Manual feature line"],
    )
    sec6 = next(s for s in plan.sections if s.number == "6")
    assert "https://example.com" not in sec6.content
    assert "Manual feature line" in sec6.content


# ── Route-level: POST /test-plan with URL crawl + warning ─────────

def test_test_plan_post_with_url_calls_crawler(client):
    fake = _fake_site_analysis(
        features=("checkout", "wishlist"),
        errors=("https://shop.example/x: 503",),
    )
    # ``routes/test_plan.py`` did ``from engine.test_plan_generator
    # import generate_test_plan`` at module load, so the reference we
    # need to intercept lives on ``routes.test_plan``, not the engine
    # module. Patching the engine attribute is a no-op here.
    with patch("routes.test_plan._crawl_cached", return_value=fake), \
         patch("routes.test_plan.generate_test_plan",
               wraps=generate_test_plan) as wrapped:
        with client.session_transaction() as s:
            s["active_project_id"] = "tp-wire-proj"
            s["project_setup"] = {"project_name": "Demo",
                                   "domain": "ecommerce"}
        resp = client.post(
            "/test-plan",
            data={"input_text": "https://shop.example/"},
            follow_redirects=True,
        )
    assert resp.status_code == 200
    # Confirm the generator got the site_analysis.
    assert wrapped.called
    kwargs = wrapped.call_args.kwargs
    assert kwargs.get("site_analysis") is fake
    # Confirm the partial-crawl warning is flashed.
    body = resp.get_data(as_text=True).lower()
    assert "could not be crawled" in body or "503" in body


def test_test_plan_post_empty_input_still_renders(client):
    """Empty input must produce a valid plan — fallback contract."""
    with client.session_transaction() as s:
        s["active_project_id"] = "tp-wire-empty"
        s["project_setup"] = {"project_name": "Demo",
                               "domain": "ecommerce"}
    resp = client.post("/test-plan", data={"input_text": ""},
                       follow_redirects=True)
    assert resp.status_code == 200
    # Generator ran and a plan is in the session for the GET render.
    with client.session_transaction() as s:
        assert s.get("test_plan_data") is not None
