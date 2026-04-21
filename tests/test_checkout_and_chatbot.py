"""Unit tests for the Checkout-flow playbook expansion and the
interactive QA assistant (chatbot)."""
from __future__ import annotations

import pytest


# ── Checkout-flow intent expansion ───────────────────────────────────

class TestCheckoutFlow:
    def test_flow_trigger_detected(self):
        from engine.qa_persona import detect_flows
        assert "checkout_flow" in detect_flows("create checkout flow test cases")
        # Ukrainian trigger
        assert "checkout_flow" in detect_flows("оформлення замовлення")

    def test_flow_trigger_ignores_unrelated_text(self):
        from engine.qa_persona import detect_flows
        assert detect_flows("user can search products") == []

    def test_analyze_input_force_adds_flow_areas(self):
        from engine.qa_persona import analyze_input
        # Bare requirement that says nothing about payment — the custom
        # prompt alone must still trigger the full checkout playbook.
        r = analyze_input(
            [{"text": "Home page with articles"}],
            custom_prompt="Create Checkout flow test cases",
        )
        assert "checkout_flow" in r.flows
        # Flow intent must add payment+auth+forms regardless of input.
        assert {"payment", "auth", "forms"}.issubset(set(r.areas))

    def test_test_cases_include_all_flow_phases(self):
        from engine.knowledge_base import FLOW_PLAYBOOKS
        from engine.qa_persona import (analyze_input,
                                       generate_professional_test_cases)

        r = analyze_input([{"text": "E-commerce site"}],
                          custom_prompt="Create checkout flow test cases")
        cases = generate_professional_test_cases(r, [],
                                                 "create checkout flow")
        # At least one TC per playbook phase
        sections = {c.section for c in cases}
        for phase_block in FLOW_PLAYBOOKS["checkout_flow"]["required_phases"]:
            phase = phase_block["phase"]
            hits = [s for s in sections if phase in s]
            assert hits, f"No test case generated for phase {phase!r}"

    def test_test_case_count_scales_with_playbook(self):
        """Flow expansion should produce at least 30 phase-specific TCs,
        substantially more than the 13 baseline before the playbook."""
        from engine.qa_persona import (analyze_input,
                                       generate_professional_test_cases)
        r = analyze_input([{"text": "Shop"}],
                          custom_prompt="Create checkout flow tests")
        cases = generate_professional_test_cases(r, [], "checkout flow")
        flow_cases = [c for c in cases
                      if "Checkout Flow" in (c.section or "")]
        assert len(flow_cases) >= 30

    def test_checklist_covers_security_phase(self):
        from engine.qa_persona import (analyze_input,
                                       generate_professional_checklist)
        r = analyze_input([{"text": "Shop"}],
                          custom_prompt="checkout flow")
        items = generate_professional_checklist(r, "checkout flow")
        security = [i for i in items
                    if "Security & Compliance" in (i.section or "")]
        assert security, "Checklist missing security-compliance phase"
        # Every item in that section must be flagged Security category
        assert all(i.category == "Security" for i in security)

    def test_non_checkout_prompt_does_not_expand_flow(self):
        from engine.qa_persona import (analyze_input,
                                       generate_professional_test_cases)
        r = analyze_input([{"text": "Login form"}],
                          custom_prompt="Create login test cases")
        assert r.flows == []
        cases = generate_professional_test_cases(r, [], "login")
        # No checkout-flow sections should appear
        assert not any("Checkout Flow" in (c.section or "") for c in cases)


# ── Chatbot dispatcher ───────────────────────────────────────────────

class TestChatbot:
    def test_greeting_en(self):
        from engine.chatbot import respond
        r = respond("hi", "en")
        assert r.intent == "greeting"
        assert r.suggestions, "Greeting should offer quick replies"

    def test_greeting_ua(self):
        from engine.chatbot import respond
        r = respond("привіт", "ua")
        assert r.intent == "greeting"
        assert "TestFortge" in r.text

    def test_checkout_flow_help(self):
        from engine.chatbot import respond
        r = respond("What is the checkout flow?", "en")
        assert r.intent in ("help_checkout_flow",)
        assert "Checkout" in r.text or "checkout" in r.text.lower()

    def test_checkout_flow_phases_listed(self):
        from engine.chatbot import respond
        r = respond("which phases does the checkout flow have?", "en")
        assert r.intent == "help_checkout_flow"
        # Must list at least 5 playbook phases
        for phase in ("Cart", "Payment", "Confirmation"):
            assert phase in r.text

    def test_module_help_routes_estimation(self):
        from engine.chatbot import respond
        r = respond("how does estimation work?", "en")
        assert r.intent == "help_estimation"
        assert "Estimation" in r.text or "estimation" in r.text.lower()

    def test_module_help_ua_automation(self):
        from engine.chatbot import respond
        r = respond("розкажи про автоматизацію", "ua")
        assert r.intent == "help_automation"

    def test_troubleshooting_intent(self):
        from engine.chatbot import respond
        r = respond("Automation is not working, it fails on login", "en")
        assert r.intent.startswith("troubleshoot")

    def test_requirement_clarification_with_follow_ups(self):
        from engine.chatbot import respond
        r = respond("As a user I want to export reports", "en")
        assert r.intent == "clarify_requirement"
        assert len(r.follow_up) >= 4  # at least role/goal/inputs/success

    def test_empty_message_gives_menu(self):
        from engine.chatbot import respond
        r = respond("   ", "en")
        assert r.intent == "help_menu"

    def test_respond_dict_shape(self):
        from engine.chatbot import respond_dict
        d = respond_dict("hello", "en")
        assert set(d.keys()) == {"text", "intent", "suggestions", "follow_up"}


# ── Flask route integration ─────────────────────────────────────────

class TestChatRoute:
    @pytest.fixture
    def client(self):
        import app as flask_app
        flask_app.app.config["TESTING"] = True
        flask_app.app.config["WTF_CSRF_ENABLED"] = False
        with flask_app.app.test_client() as c:
            yield c

    def test_chat_endpoint_returns_json(self, client):
        resp = client.post("/chat", json={"message": "hi", "lang": "en"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["intent"] == "greeting"
        assert "text" in body

    def test_chat_endpoint_rejects_empty(self, client):
        resp = client.post("/chat", json={"message": "", "lang": "en"})
        assert resp.status_code == 400

    def test_chat_reset_clears_history(self, client):
        client.post("/chat", json={"message": "hi", "lang": "en"})
        resp = client.post("/chat/reset")
        assert resp.status_code == 200
        h = client.get("/chat/history").get_json()
        assert h["history"] == []
