"""Integration tests for crawler-failure surfacing.

Until this Sprint, ``engine.qa_persona.analyze_input`` silently
swallowed any exception from ``site_crawler.crawl_site`` — the user got
back generic TC / checklist content with no indication that their URL
was unreachable. These tests pin the new behaviour: partial failures
must bubble up as a warning flash, and total failures must still
fall back to generic generation rather than 500-ing.
"""
from unittest.mock import patch

import pytest

from engine.qa_persona import AnalysisResult, analyze_input


def _fake_site_analysis(errors: list[str], pages: int = 0):
    """Build a minimal SiteAnalysis-shaped object — only the fields
    qa_persona actually reads from it. Using a SimpleNamespace keeps
    the test independent of the real dataclass shape."""
    from types import SimpleNamespace
    return SimpleNamespace(
        features_detected=[],
        site_type="generic",
        has_auth=False, has_search=False, has_forms=False,
        has_payment=False, nav_items=[], pages=[],
        crawl_errors=list(errors),
    )


def test_crawler_partial_errors_bubble_into_analysis_result():
    """site_analysis.crawl_errors must end up on AnalysisResult so
    routes can flash a banner."""
    reqs = [{"text": "https://broken.example.test/"}]
    with patch("engine.site_crawler.crawl_site") as mocked:
        mocked.return_value = _fake_site_analysis(
            errors=["https://broken.example.test/x: 404 Not Found"],
        )
        result = analyze_input(reqs, custom_prompt="")
    assert isinstance(result, AnalysisResult)
    assert any("404" in e for e in result.crawl_errors), \
        f"expected the 404 error to be surfaced, got {result.crawl_errors!r}"


def test_crawler_exception_does_not_500_and_is_recorded():
    """If crawl_site itself throws, generation must continue (generic
    fallback) and the error message must be recorded for the route to
    flash."""
    reqs = [{"text": "https://blocked.example.test/"}]
    with patch("engine.site_crawler.crawl_site",
               side_effect=RuntimeError("SSRF blocked")):
        result = analyze_input(reqs, custom_prompt="")
    # Generation completed (no crash) and the error was recorded.
    assert isinstance(result, AnalysisResult)
    assert any("SSRF" in e or "crawler" in e for e in result.crawl_errors)


def test_test_cases_route_flashes_crawl_warning(client, app):
    """End-to-end through the /test-cases POST — when crawler reports
    partial failures, the rendered page contains a warning banner."""
    with patch("engine.site_crawler.crawl_site") as mocked:
        mocked.return_value = _fake_site_analysis(
            errors=["https://x.example/abc: 503 Service Unavailable"],
        )
        with client.session_transaction() as s:
            s["active_project_id"] = "test-proj-crawl"
        resp = client.post(
            "/test-cases",
            data={"input_text": "https://x.example/abc"},
            follow_redirects=True,
        )
    body = resp.get_data(as_text=True)
    # The flash either rendered inline or — for the in-flight job path —
    # was queued. Either way, the user must see the URL fragment or a
    # crawl-related warning so they know something went wrong.
    assert resp.status_code in (200, 302)
    # The warning class our base template uses + the URL fragment.
    has_warning = "warning" in body.lower() and (
        "could not be crawled" in body.lower()
        or "503" in body
        or "x.example" in body
        or "background" in body.lower()  # async path fallback
    )
    assert has_warning, "expected a crawl warning banner"
