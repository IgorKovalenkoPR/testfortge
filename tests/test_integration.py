"""
Integration Tests — TestFortge
Tests the pipeline: raw input → split → stories → test cases / checklist.

Verifies modules work together correctly.
"""

import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.file_parser import split_into_requirements
from engine.user_story_generator import generate_user_stories
from engine.testcase_generator import generate_test_cases, generate_checklist, generate_traceability


class TestFullPipeline:
    """End-to-end pipeline: raw text → requirements → stories → TC/CL."""

    def test_requirements_to_test_cases(self):
        lines = [
            "1. User can log in with email and password",
            "2. User can search products by keyword",
            "3. User can add items to shopping cart",
        ]
        reqs = split_into_requirements(lines)
        assert len(reqs) == 3

        stories = generate_user_stories(reqs)
        assert len(stories) == 3

        raw_reqs = [{"id": r.id, "text": r.text} for r in reqs]
        tc_list = generate_test_cases(stories, raw_requirements=raw_reqs)
        assert len(tc_list) >= 6  # at least 2 per requirement area

    def test_requirements_to_checklist(self):
        lines = [
            "1. User can log in with email and password",
            "2. User can search products by keyword",
        ]
        reqs = split_into_requirements(lines)
        stories = generate_user_stories(reqs)
        raw_reqs = [{"id": r.id, "text": r.text} for r in reqs]
        cl_list = generate_checklist(stories, raw_requirements=raw_reqs)
        assert len(cl_list) >= 10

    def test_traceability_links_stories_to_cases(self):
        lines = ["1. User can register an account"]
        reqs = split_into_requirements(lines)
        stories = generate_user_stories(reqs)
        raw_reqs = [{"id": r.id, "text": r.text} for r in reqs]
        tc_list = generate_test_cases(stories, raw_requirements=raw_reqs)
        trace = generate_traceability(stories, tc_list)
        assert len(trace) >= 1
        assert trace[0]["user_story_id"] == stories[0].id


class TestURLPipeline:
    """URL-only input must produce professional output without stories."""

    def test_url_produces_checklist(self):
        raw_reqs = [{"id": "RAW-1", "text": "https://testfort.com/software-testing-services"}]
        cl_list = generate_checklist([], "", raw_requirements=raw_reqs)
        assert len(cl_list) >= 50
        sections = {cl.section for cl in cl_list}
        assert "Header & Navigation" in sections

    def test_url_checklist_has_responsive_section(self):
        raw_reqs = [{"id": "RAW-1", "text": "https://example.com"}]
        cl_list = generate_checklist([], "", raw_requirements=raw_reqs)
        sections = {cl.section for cl in cl_list}
        assert "Responsive Design" in sections

    def test_url_checklist_has_security_section(self):
        raw_reqs = [{"id": "RAW-1", "text": "https://example.com"}]
        cl_list = generate_checklist([], "", raw_requirements=raw_reqs)
        sections = {cl.section for cl in cl_list}
        assert "Security (Basic)" in sections


class TestConversationalPipeline:
    """Conversational transcript input must be filtered properly."""

    def test_pure_conversation_yields_no_reqs(self):
        lines = [
            "так, ну шо, Юра?",
            "ну от, короче, я думаю",
            "окей, давай зустрінемось завтра",
        ]
        reqs = split_into_requirements(lines)
        assert len(reqs) == 0

    def test_conversation_with_features_extracts_features(self):
        lines = [
            "так, ну шо, Юра?",
            "ось тут є login button",
            "а тут search працює",
            "окей, давай",
        ]
        reqs = split_into_requirements(lines)
        assert len(reqs) >= 2

    def test_instruction_lines_filtered_in_checklist(self):
        """Instructions like 'Мають бути покриті...' must NOT appear in output."""
        raw_reqs = [
            {"text": "Мають бути покриті позитивні, негативні та едж сценарії"},
            {"text": "https://example.com"},
        ]
        cl_list = generate_checklist([], "", raw_requirements=raw_reqs)
        objectives = " ".join(cl.objective for cl in cl_list)
        assert "покриті" not in objectives


class TestCustomPromptPipeline:
    """Custom prompt must affect generation."""

    def test_positive_only_filter(self):
        raw_reqs = [{"text": "https://example.com"}]
        cl_list = generate_checklist([], "positive only", raw_requirements=raw_reqs)
        categories = {cl.category for cl in cl_list}
        assert categories == {"Positive"}

    def test_negative_only_filter(self):
        raw_reqs = [{"text": "https://example.com"}]
        cl_list = generate_checklist([], "negative only", raw_requirements=raw_reqs)
        categories = {cl.category for cl in cl_list}
        assert categories == {"Negative"}


class TestDeduplication:
    """EN/UA synonyms must be deduplicated in the pipeline."""

    def test_login_en_ua_deduplication(self):
        lines = [
            "тут є login button де можна увійти в акаунт",
        ]
        reqs = split_into_requirements(lines)
        texts = [r.text for r in reqs]
        login_count = sum(1 for t in texts if "log in" in t.lower() or "увійти" in t.lower())
        assert login_count <= 1, f"Login duplicated: {texts}"
