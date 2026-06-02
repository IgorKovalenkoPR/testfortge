"""PR-D — heuristic suite classifier tests.

Pins the rule order from :mod:`engine.suite_classifier`:

  Regression > E2E > Smoke

Each test pairs an input step list with the expected verdict + a
spot-check of the rationale so the review UI's hint stays accurate.
"""
from __future__ import annotations

from engine.automation_qa import AutomationStep
from engine.suite_classifier import (SUITE_E2E, SUITE_REGRESSION,
                                       SUITE_SMOKE, classify)


class TestRegressionTrigger:
    def test_login_keyword_triggers_regression(self):
        v = classify([
            AutomationStep(action="goto", target="https://app/login"),
            AutomationStep(action="fill", target="label=Email",
                            value="a@b.c"),
        ])
        assert v.tag == SUITE_REGRESSION
        assert "login" in v.rationale.lower()

    def test_checkout_keyword_triggers_regression(self):
        v = classify([
            AutomationStep(action="goto", target="https://shop.io/"),
            AutomationStep(action="click",
                            target='role=link[name="Checkout"]',
                            raw="click checkout link"),
        ])
        assert v.tag == SUITE_REGRESSION

    def test_payment_keyword_in_raw_triggers_regression(self):
        v = classify([
            AutomationStep(action="click", target="role=button",
                            raw="click payment confirm"),
        ])
        assert v.tag == SUITE_REGRESSION

    def test_substring_collision_does_not_misfire(self):
        """`cart` keyword shouldn't match `cartograph` — word-boundary
        rule in _REGRESSION_RE."""
        v = classify([
            AutomationStep(action="goto",
                            target="https://app/cartographer"),
        ])
        assert v.tag == SUITE_SMOKE


class TestE2ETrigger:
    def test_multi_page_form_submit_is_e2e(self):
        v = classify([
            AutomationStep(action="goto", target="https://app/contact"),
            AutomationStep(action="fill", target="label=Name", value="x"),
            AutomationStep(action="click",
                            target='role=button[name="Send"]'),
            AutomationStep(action="goto", target="https://app/thanks"),
        ])
        assert v.tag == SUITE_E2E
        assert "form submission" in v.rationale.lower()

    def test_single_page_form_is_smoke_not_e2e(self):
        v = classify([
            AutomationStep(action="goto", target="https://app/"),
            AutomationStep(action="fill", target="label=Search", value="x"),
            AutomationStep(action="click",
                            target='role=button[name="Go"]'),
        ])
        # One page, no second goto → not E2E.
        assert v.tag == SUITE_SMOKE


class TestSmokeFallback:
    def test_short_root_nav_is_smoke(self):
        v = classify([
            AutomationStep(action="goto", target="https://app/"),
            AutomationStep(action="click",
                            target='role=link[name="About"]'),
        ])
        assert v.tag == SUITE_SMOKE
        assert "smoke" in v.rationale.lower()

    def test_empty_steps_default_to_smoke(self):
        v = classify([])
        assert v.tag == SUITE_SMOKE
