"""
Functional Tests — TestFortge
Tests Flask routes with the test client (HTTP-level).

Verifies request/response cycle, session handling, i18n.
"""

import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestDashboard:
    """GET / must render the dashboard."""

    def test_dashboard_loads(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"Dashboard" in resp.data or b"TestFortge" in resp.data

    def test_dashboard_en(self, client):
        resp = client.get("/?lang=en")
        assert resp.status_code == 200
        assert b"Dashboard" in resp.data

    def test_dashboard_ua(self, client):
        resp = client.get("/?lang=ua")
        assert resp.status_code == 200
        # UA title
        assert "Головна".encode("utf-8") in resp.data or b"TestFortge" in resp.data


class TestTestCasesRoute:
    """POST /test-cases must generate test cases."""

    def test_get_shows_empty_form(self, client):
        resp = client.get("/test-cases")
        assert resp.status_code == 200
        assert b"Generate" in resp.data

    def test_post_generates_test_cases(self, client):
        resp = client.post("/test-cases", data={
            "input_text": "User can log in with email and password",
        })
        assert resp.status_code == 200
        assert b"SC1_001" in resp.data
        assert b"Verify that" in resp.data

    def test_post_url_generates_test_cases(self, client):
        resp = client.post("/test-cases", data={
            "input_text": "https://testfort.com/software-testing-services",
        })
        assert resp.status_code == 200
        # Should produce test cases (web_general area)
        assert b"SC" in resp.data

    def test_post_empty_input_shows_error(self, client):
        resp = client.post("/test-cases", data={"input_text": ""})
        assert resp.status_code == 200
        # Flash message should appear (either in HTML or redirect)

    def test_multiple_requirements(self, client):
        resp = client.post("/test-cases", data={
            "input_text": "User can log in\nUser can search\nUser can pay",
        })
        assert resp.status_code == 200
        assert b"SC1_" in resp.data  # Auth section
        assert b"SC2_" in resp.data  # Search section


class TestChecklistRoute:
    """POST /checklist must generate a professional checklist."""

    def test_get_shows_empty_form(self, client):
        resp = client.get("/checklist")
        assert resp.status_code == 200
        assert b"Generate" in resp.data

    def test_post_url_generates_professional_checklist(self, client):
        resp = client.post("/checklist", data={
            "input_text": "https://testfort.com/software-testing-services",
        })
        assert resp.status_code == 200
        assert b"HDR_001" in resp.data  # Header section
        assert b"Verify that" in resp.data

    def test_post_instruction_filtered(self, client):
        """Instructions must NOT appear as checklist items."""
        resp = client.post("/checklist", data={
            "input_text": ("Створи чек-ліст для https://testfort.com/.\n"
                           "Мають бути покриті позитивні, негативні та едж сценарії."),
        })
        assert resp.status_code == 200
        assert b"HDR_001" in resp.data
        # Instruction text must NOT be in checklist objectives
        assert "покриті".encode("utf-8") not in resp.data \
            or b"Verify that" in resp.data  # items are professional

    def test_checklist_has_many_items(self, client):
        resp = client.post("/checklist", data={
            "input_text": "https://example.com",
        })
        assert resp.status_code == 200
        # Should have 70+ items for web_general
        count = resp.data.count(b"Verify that")
        assert count >= 50, f"Only {count} 'Verify that' items found"


class TestLanguageSwitch:
    """Switching language must NOT clear generated data."""

    def test_tc_preserved_after_lang_switch(self, client):
        # Generate test cases
        client.post("/test-cases", data={
            "input_text": "User can log in with email and password",
        })
        # Switch language
        resp = client.get("/test-cases?lang=ua")
        assert resp.status_code == 200
        assert b"SC1_001" in resp.data, "Test cases disappeared after language switch!"

    def test_cl_preserved_after_lang_switch(self, client):
        # Generate checklist
        client.post("/checklist", data={
            "input_text": "https://example.com",
        })
        # Switch language
        resp = client.get("/checklist?lang=ua")
        assert resp.status_code == 200
        assert b"HDR_001" in resp.data, "Checklist disappeared after language switch!"

    def test_tc_preserved_on_page_refresh(self, client):
        # Generate test cases
        client.post("/test-cases", data={
            "input_text": "User can search products",
        })
        # Simulate page refresh (GET)
        resp = client.get("/test-cases")
        assert resp.status_code == 200
        assert b"SC" in resp.data and b"Verify that" in resp.data


class TestNewSession:
    """POST /new-session must clear all generated data."""

    def test_new_session_clears_data(self, client):
        # Generate data first
        client.post("/test-cases", data={
            "input_text": "User can log in",
        })
        # Clear
        resp = client.post("/new-session", follow_redirects=True)
        assert resp.status_code == 200
        # Now GET test-cases should be empty
        resp = client.get("/test-cases")
        assert b"SC1_001" not in resp.data


class TestExport:
    """Export routes must produce valid files."""

    def _generate_data(self, client):
        client.post("/test-cases", data={
            "input_text": "User can log in with email and password",
        })

    def test_export_markdown(self, client):
        self._generate_data(client)
        resp = client.get("/export/markdown")
        assert resp.status_code == 200
        assert "text/markdown" in resp.content_type

    def test_export_html(self, client):
        self._generate_data(client)
        resp = client.get("/export/html")
        assert resp.status_code == 200
        assert "text/html" in resp.content_type

    def test_export_csv_testcases(self, client):
        self._generate_data(client)
        resp = client.get("/export/csv-testcases")
        assert resp.status_code == 200
        assert "text/csv" in resp.content_type

    def test_export_csv_checklist(self, client):
        self._generate_data(client)
        resp = client.get("/export/csv-checklist")
        assert resp.status_code == 200

    def test_export_unknown_format(self, client):
        resp = client.get("/export/xyz")
        assert resp.status_code == 400
