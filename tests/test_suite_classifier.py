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


class TestAQueryStringIsNotEvidence:
    """Measured on staging 2026-08-29, from a real recorded walk.

    A GET form puts **every field name** into the query on submit,
    touched or not. The walk filled a text input and a textarea on the
    Selenium practice form and never went near its password field; the
    submit landed on ``…/submitted-form.html?my-text=…&my-password=…``
    and the classifier reported "Flow touches 'password' — treated as
    business-critical regression".

    It was reading the page's form definition and reporting it as
    evidence about the flow.
    """

    SUBMITTED = ("https://app/submitted-form.html"
                 "?my-text=regression+walk&my-password=&my-textarea=hello")

    def test_a_field_name_echoed_into_the_query_does_not_trigger(self):
        v = classify([
            AutomationStep(action="goto", target="https://app/web-form.html"),
            AutomationStep(action="fill", target="#my-text-id", value="walk"),
            AutomationStep(action="click",
                            target='role=button[name="Submit"]'),
            AutomationStep(action="goto", target=self.SUBMITTED),
        ])
        assert v.tag != SUITE_REGRESSION, v.rationale

    def test_the_raw_line_is_searched_the_same_way(self):
        """``raw`` carries the whole URL too.

        Stripping only ``target`` would leave the same word in
        ``page.goto("…?my-password=")`` and change nothing.
        """
        v = classify([
            AutomationStep(action="goto", target="https://app/one",
                            raw=f'page.goto("{self.SUBMITTED}")'),
            AutomationStep(action="click", target="#go"),
        ])
        assert v.tag != SUITE_REGRESSION, v.rationale

    def test_that_flow_is_classified_on_what_it_actually_did(self):
        """Not merely "not Regression" — the right answer instead.

        Two pages and a form submission is an E2E journey, which is what
        the operator should have been offered in the first place.
        """
        v = classify([
            AutomationStep(action="goto", target="https://app/web-form.html"),
            AutomationStep(action="fill", target="#my-text-id", value="walk"),
            AutomationStep(action="click",
                            target='role=button[name="Submit"]'),
            AutomationStep(action="goto", target=self.SUBMITTED),
        ])
        assert v.tag == SUITE_E2E, v.rationale

    def test_the_path_still_triggers(self):
        """The rule itself must survive the fix.

        A login page is a login page whether or not it carries a query,
        and a fix that stopped seeing paths would pass every test above.
        """
        v = classify([
            AutomationStep(action="goto",
                            target="https://app/login?next=%2Fdashboard"),
        ])
        assert v.tag == SUITE_REGRESSION
        assert "login" in v.rationale.lower()

    def test_a_hash_route_still_triggers(self):
        """SPA routes live in the fragment.

        Dropping the fragment along with the query would silently stop
        classifying every hash-routed application.
        """
        v = classify([
            AutomationStep(action="goto",
                            target="https://app/?ref=email#/checkout/step-1"),
        ])
        assert v.tag == SUITE_REGRESSION
        assert "checkout" in v.rationale.lower()

    def test_a_typed_value_is_still_evidence(self):
        """``value`` is what a person entered, not what a form echoed."""
        v = classify([
            AutomationStep(action="goto", target="https://app/settings"),
            AutomationStep(action="fill", target="#field", value="my-otp-code"),
        ])
        assert v.tag == SUITE_REGRESSION
        assert "otp" in v.rationale.lower()

    def test_a_locator_naming_a_password_field_is_still_evidence(self):
        """The signal the false positive was impersonating.

        A flow that really touches a password field says so in the
        locator, and that must keep firing.
        """
        v = classify([
            AutomationStep(action="goto", target="https://app/account"),
            AutomationStep(action="fill",
                            target='role=textbox[name="Password"]',
                            value="x"),
        ])
        assert v.tag == SUITE_REGRESSION
        assert "password" in v.rationale.lower()
