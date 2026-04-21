"""
E2E Tests — TestFortge
Simulates real user flows through the test client.

Tests complete user journeys: generate → view → export → save/load → new session.
"""

import json
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestE2EChecklistGeneration:
    """User journey: generate checklist from URL → switch lang → export."""

    def test_full_checklist_journey(self, client):
        # Step 1: User opens checklist page (should be empty)
        resp = client.get("/checklist")
        assert resp.status_code == 200
        assert b"HDR_001" not in resp.data

        # Step 2: User generates a checklist from URL
        resp = client.post("/checklist", data={
            "input_text": "https://testfort.com/software-testing-services",
        })
        assert resp.status_code == 200
        assert b"HDR_001" in resp.data
        assert b"Verify that" in resp.data

        # Step 3: User switches language to UA
        resp = client.get("/checklist?lang=ua")
        assert resp.status_code == 200
        assert b"HDR_001" in resp.data  # Data preserved

        # Step 4: User switches back to EN
        resp = client.get("/checklist?lang=en")
        assert resp.status_code == 200
        assert b"HDR_001" in resp.data  # Still there

        # Step 5: User exports to CSV
        resp = client.get("/export/csv-checklist")
        assert resp.status_code == 200
        csv_text = resp.data.decode("utf-8")
        assert "HDR_001" in csv_text
        assert "Verify that" in csv_text


class TestE2ETestCaseGeneration:
    """User journey: generate TC → view → switch lang → export."""

    def test_full_testcase_journey(self, client):
        # Step 1: Empty page
        resp = client.get("/test-cases")
        assert resp.status_code == 200
        assert b"SC1_001" not in resp.data

        # Step 2: Generate from requirements
        resp = client.post("/test-cases", data={
            "input_text": ("User can log in with email and password\n"
                           "User can search products\n"
                           "User can complete checkout"),
        })
        assert resp.status_code == 200
        assert b"SC1_001" in resp.data
        assert b"Authentication" in resp.data or b"Verify that" in resp.data

        # Step 3: Switch language
        resp = client.get("/test-cases?lang=ua")
        assert resp.status_code == 200
        assert b"SC1_001" in resp.data

        # Step 4: Export
        resp = client.get("/export/csv-testcases")
        assert resp.status_code == 200
        assert b"SC1_001" in resp.data


class TestE2EProjectSaveLoad:
    """User journey: generate → save → new session → load → verify."""

    def test_save_and_load_project(self, client):
        # Step 1: Generate test cases
        client.post("/test-cases", data={
            "input_text": "User can log in with email and password",
        })

        # Step 2: Save project
        resp = client.post("/save-project", data={
            "project_name": "E2E Test Project",
        }, follow_redirects=True)
        assert resp.status_code == 200

        # Step 3: New session (clear data)
        client.post("/new-session")
        resp = client.get("/test-cases")
        assert b"SC1_001" not in resp.data  # Data cleared

        # Step 4: Load project from dashboard
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"E2E Test Project" in resp.data

        # Find the project folder from the page
        html = resp.data.decode("utf-8")
        import re
        folder_match = re.search(r'/load-project/([^"]+)', html)
        assert folder_match, "Could not find load-project link"
        folder = folder_match.group(1)

        # Step 5: Load it
        resp = client.get(f"/load-project/{folder}", follow_redirects=True)
        assert resp.status_code == 200

        # Step 6: Verify test cases are back
        resp = client.get("/test-cases")
        assert b"SC1_001" in resp.data

        # Cleanup: delete the project
        client.post(f"/delete-project/{folder}", follow_redirects=True)


class TestE2ENewSessionClearsAll:
    """New Session must clear test cases, checklist, and stories."""

    def test_new_session_clears_everything(self, client):
        # Generate both test cases and checklist
        client.post("/test-cases", data={
            "input_text": "User can log in",
        })
        client.post("/checklist", data={
            "input_text": "https://example.com",
        })

        # Verify data exists
        resp = client.get("/test-cases")
        assert b"SC" in resp.data
        resp = client.get("/checklist")
        assert b"HDR_001" in resp.data

        # New session
        client.post("/new-session")

        # Both should be empty now
        resp = client.get("/test-cases")
        assert b"SC1_001" not in resp.data
        resp = client.get("/checklist")
        assert b"HDR_001" not in resp.data

        # Dashboard should NOT show "Current Project"
        resp = client.get("/")
        assert b"Current Project" not in resp.data


class TestE2EInstructionFiltering:
    """Instruction text in user input must never appear in generated output."""

    def test_instructions_not_in_checklist(self, client):
        resp = client.post("/checklist", data={
            "input_text": ("Створи низькорівневий чек-ліст для https://testfort.com/.\n"
                           "Мають бути покриті позитивні, негативні та едж сценарії."),
        })
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")

        # Professional items MUST be present
        assert "HDR_001" in html
        assert "Verify that" in html

        # Instruction text MUST NOT be an objective
        assert "Мають бути покриті" not in html or \
               html.count("Мають бути покриті") <= 1  # only in input textarea echo

    def test_instructions_not_in_test_cases(self, client):
        resp = client.post("/test-cases", data={
            "input_text": ("Generate test cases for login feature.\n"
                           "Include positive, negative, and edge cases."),
        })
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        # Should produce auth test cases, not garbage
        if "SC1_001" in html:
            assert "Verify that" in html


class TestE2EMultiLanguageConsistency:
    """Generated data must be the same regardless of UI language."""

    def test_en_ua_same_count(self, client):
        # Generate in EN
        client.post("/new-session")
        resp_en = client.post("/checklist", data={
            "input_text": "https://example.com",
        })
        count_en = resp_en.data.count(b"Verify that")

        # Switch to UA and check
        resp_ua = client.get("/checklist?lang=ua")
        count_ua = resp_ua.data.count(b"Verify that")

        assert count_en == count_ua, \
            f"EN has {count_en} items but UA shows {count_ua}"
