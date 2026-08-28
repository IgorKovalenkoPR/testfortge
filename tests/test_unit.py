"""
Unit Tests — TestFortge
Tests individual functions/classes in isolation.

Covers:
  - qa_persona: instruction filtering, input analysis, checklist/TC generation
  - file_parser: split_into_requirements, conversational filter, feature extraction
  - user_story_generator: _extract_action, role/priority detection
  - testcase_generator: generate_checklist, generate_test_cases
"""

import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════════════
# qa_persona
# ═══════════════════════════════════════════════════════════════════

class TestInstructionFilter:
    """is_instruction() must detect commands TO the tool, not requirements."""

    def test_english_instruction_create(self):
        from engine.qa_persona import is_instruction
        assert is_instruction("Create a checklist for the following page")

    def test_english_instruction_generate(self):
        from engine.qa_persona import is_instruction
        assert is_instruction("Generate test cases for login feature")

    def test_ukrainian_instruction_create(self):
        from engine.qa_persona import is_instruction
        assert is_instruction("Створи чек-ліст для наступної сторінки")

    def test_ukrainian_instruction_coverage(self):
        from engine.qa_persona import is_instruction
        assert is_instruction("Мають бути покриті позитивні, негативні та едж сценарії")

    def test_requirement_not_instruction(self):
        from engine.qa_persona import is_instruction
        assert not is_instruction("User can log in with email and password")

    def test_url_not_instruction(self):
        from engine.qa_persona import is_instruction
        assert not is_instruction("https://testfort.com/software-testing-services")

    def test_short_text_not_instruction(self):
        from engine.qa_persona import is_instruction
        assert not is_instruction("login button")


class TestAnalyzeInput:
    """analyze_input() must correctly detect areas, URLs, and level."""

    def test_url_detection(self):
        from engine.qa_persona import analyze_input
        result = analyze_input([{"text": "https://testfort.com/services"}])
        assert result.url is not None
        assert "testfort.com" in result.url_domain

    def test_url_path_extraction(self):
        from engine.qa_persona import analyze_input
        result = analyze_input([{"text": "https://example.com/software-testing"}])
        assert "software testing" in result.url_path

    def test_auth_area_detection(self):
        from engine.qa_persona import analyze_input
        result = analyze_input([{"text": "User can log in with email"}])
        assert "auth" in result.areas

    def test_search_area_detection(self):
        from engine.qa_persona import analyze_input
        result = analyze_input([{"text": "User can search and filter products"}])
        assert "search" in result.areas

    def test_payment_area_detection(self):
        from engine.qa_persona import analyze_input
        result = analyze_input([{"text": "User can checkout and pay"}])
        assert "payment" in result.areas

    def test_multiple_areas(self):
        from engine.qa_persona import analyze_input
        result = analyze_input([
            {"text": "User can log in"},
            {"text": "User can search content"},
            {"text": "User can add to cart and checkout"},
        ])
        assert len(result.areas) >= 3

    def test_url_only_defaults_to_web_general(self):
        from engine.qa_persona import analyze_input
        result = analyze_input([{"text": "https://example.com"}])
        assert "web_general" in result.areas

    def test_low_level_detection(self):
        from engine.qa_persona import analyze_input
        result = analyze_input([{"text": "login"}], custom_prompt="low level checklist")
        assert result.level == "low"


class TestProfessionalChecklist:
    """generate_professional_checklist() must produce real QA checks."""

    def test_url_generates_many_checks(self):
        from engine.qa_persona import analyze_input, generate_professional_checklist
        analysis = analyze_input([{"text": "https://testfort.com"}])
        items = generate_professional_checklist(analysis)
        assert len(items) >= 50  # web_general has 76 checks

    def test_all_items_start_with_action_verb(self):
        """Every checklist objective must open with a concrete action verb.

        The original rule was ``startswith('Verify that')``; it was relaxed
        after bug-report #7 asked us to use real interaction verbs
        (``Click``, ``Tap``, ``Enter``, ``Open``, ``Rotate`` …) in the
        steps rather than dressing every line in ``Verify that``. The test
        now accepts any verb from the canonical action-verb whitelist, so
        legitimate verb-led phrasing ("Click every menu item and verify …")
        passes while abstract stubs like "System should behave correctly"
        still fail.
        """
        from engine.qa_persona import analyze_input, generate_professional_checklist

        ACTION_VERBS = {
            "Verify", "Click", "Tap", "Enter", "Type", "Fill", "Open", "Close",
            "Submit", "Select", "Choose", "Navigate", "Visit", "Resize",
            "Rotate", "Load", "Reload", "Refresh", "Scroll", "Hover", "Drag",
            "Drop", "Focus", "Press", "Attempt", "Log", "Sign", "Check",
            "Inspect", "Confirm", "Ensure", "Observe", "Read", "Count",
            "Compare", "Measure", "Test", "Try", "Paste", "Clear", "Toggle",
            "Simulate", "Capture", "Locate", "Search", "Crawl", "Fetch",
            "Switch", "Restart", "Disable", "Enable", "Record", "Run", "Set",
            "Apply", "Remove", "Add", "Download", "Upload",
        }
        analysis = analyze_input([{"text": "https://example.com"}])
        items = generate_professional_checklist(analysis)
        for item in items:
            first = item.objective.split(None, 1)[0] if item.objective else ""
            assert first in ACTION_VERBS, (
                f"Objective does not start with an action verb: "
                f"{item.objective[:80]!r}"
            )

    def test_auth_checks_included_for_login(self):
        from engine.qa_persona import analyze_input, generate_professional_checklist
        analysis = analyze_input([{"text": "User can log in"}])
        items = generate_professional_checklist(analysis)
        objectives = [i.objective for i in items]
        assert any("login" in o.lower() or "log in" in o.lower() for o in objectives)

    def test_has_multiple_categories(self):
        from engine.qa_persona import analyze_input, generate_professional_checklist
        analysis = analyze_input([{"text": "https://example.com"}])
        items = generate_professional_checklist(analysis)
        categories = {i.category for i in items}
        assert "Positive" in categories
        assert "Negative" in categories

    def test_has_multiple_priorities(self):
        from engine.qa_persona import analyze_input, generate_professional_checklist
        analysis = analyze_input([{"text": "https://example.com"}])
        items = generate_professional_checklist(analysis)
        priorities = {i.priority for i in items}
        assert "High" in priorities
        assert "Medium" in priorities

    def test_has_sections(self):
        from engine.qa_persona import analyze_input, generate_professional_checklist
        analysis = analyze_input([{"text": "https://example.com"}])
        items = generate_professional_checklist(analysis)
        sections = {i.section for i in items}
        assert len(sections) >= 5  # Header, Content, Links, Footer, Forms, etc.

    def test_instructions_filtered_from_requirements(self):
        from engine.qa_persona import analyze_input, generate_professional_checklist
        analysis = analyze_input([
            {"text": "Мають бути покриті позитивні та негативні сценарії"},
            {"text": "https://example.com"},
        ])
        items = generate_professional_checklist(analysis)
        objectives = " ".join(i.objective for i in items)
        assert "покриті" not in objectives
        assert "сценарії" not in objectives


class TestProfessionalTestCases:
    """generate_professional_test_cases() must produce real QA test cases."""

    def test_auth_produces_detailed_cases(self):
        from engine.qa_persona import analyze_input, generate_professional_test_cases
        analysis = analyze_input([{"text": "User can log in"}])
        cases = generate_professional_test_cases(analysis)
        assert len(cases) >= 5  # positive, negatives, security, logout

    def test_all_summaries_start_with_verify(self):
        from engine.qa_persona import analyze_input, generate_professional_test_cases
        analysis = analyze_input([{"text": "User can log in"}])
        cases = generate_professional_test_cases(analysis)
        for tc in cases:
            assert tc.summary.startswith("Verify that"), \
                f"Bad summary: {tc.summary[:80]}"

    def test_all_preconditions_are_passive(self):
        from engine.qa_persona import analyze_input, generate_professional_test_cases
        analysis = analyze_input([{"text": "User can log in"}])
        cases = generate_professional_test_cases(analysis)
        for tc in cases:
            if tc.preconditions:
                # Should NOT contain "User is at" style active voice
                assert "User is at " not in tc.preconditions, \
                    f"Active voice in preconditions: {tc.preconditions}"

    def test_cases_have_steps(self):
        from engine.qa_persona import analyze_input, generate_professional_test_cases
        analysis = analyze_input([{"text": "User can log in"}])
        cases = generate_professional_test_cases(analysis)
        for tc in cases:
            assert len(tc.steps) >= 2, f"Too few steps in: {tc.summary}"


# ═══════════════════════════════════════════════════════════════════
# file_parser
# ═══════════════════════════════════════════════════════════════════

class TestSplitIntoRequirements:
    """split_into_requirements() must filter conversation and extract features."""

    def test_numbered_requirements(self):
        from engine.file_parser import split_into_requirements
        lines = [
            "1. User can log in with email",
            "2. User can search products",
            "3. User can checkout",
        ]
        reqs = split_into_requirements(lines)
        assert len(reqs) == 3

    def test_bullet_requirements(self):
        from engine.file_parser import split_into_requirements
        lines = [
            "- Allow users to filter results by category",
            "- Support upload of PDF files up to 10 MB",
        ]
        reqs = split_into_requirements(lines)
        assert len(reqs) == 2

    def test_req_id_pattern(self):
        from engine.file_parser import split_into_requirements
        lines = ["REQ-001: User authentication via OAuth"]
        reqs = split_into_requirements(lines)
        assert len(reqs) == 1
        assert reqs[0].id == "REQ-001"

    def test_conversational_filtered(self):
        from engine.file_parser import split_into_requirements
        lines = [
            "так, ну шо? Юра, дивись",
            "ну от, бачиш, тут от прям такий цей",
            "окей, давай зустрінемось завтра",
        ]
        reqs = split_into_requirements(lines)
        assert len(reqs) == 0

    def test_mixed_input(self):
        from engine.file_parser import split_into_requirements
        lines = [
            "так, ну шо? Юра, дивись",
            "1. Allow users to filter results by category",
            "окей, давай тоді",
            "2. Support upload of PDF files up to 10 MB",
        ]
        reqs = split_into_requirements(lines)
        assert len(reqs) == 2

    def test_short_lines_filtered(self):
        from engine.file_parser import split_into_requirements
        lines = ["ok", "yes", "next"]
        reqs = split_into_requirements(lines)
        assert len(reqs) == 0

    def test_metadata_lines_filtered(self):
        from engine.file_parser import split_into_requirements
        lines = ["[Video attachment: demo.mp4 (MP4, 15.2 MB)]"]
        reqs = split_into_requirements(lines)
        assert len(reqs) == 0


class TestFeatureExtraction:
    """_extract_feature_requirements() must normalize features and deduplicate."""

    def test_english_login_keyword(self):
        from engine.file_parser import _extract_feature_requirements
        result = _extract_feature_requirements("there is a login button")
        assert len(result) >= 1
        feat_ids = [r[0] for r in result]
        assert "auth.login" in feat_ids

    def test_ukrainian_login_keyword(self):
        from engine.file_parser import _extract_feature_requirements
        result = _extract_feature_requirements("тут є кнопка для входу увійти")
        feat_ids = [r[0] for r in result]
        assert "auth.login" in feat_ids

    def test_en_ua_deduplication(self):
        from engine.file_parser import _extract_feature_requirements
        # Both login and увійти should map to same feature_id
        result = _extract_feature_requirements("login button де можна увійти")
        feat_ids = [r[0] for r in result]
        assert feat_ids.count("auth.login") == 1  # deduplicated


# ═══════════════════════════════════════════════════════════════════
# user_story_generator
# ═══════════════════════════════════════════════════════════════════

class TestExtractAction:
    """_extract_action() must clean up requirement text into action."""

    def test_strips_instruction_prefix(self):
        from engine.user_story_generator import _extract_action
        action = _extract_action("Create a checklist for login page")
        assert not action.lower().startswith("create a checklist")

    def test_strips_system_prefix(self):
        from engine.user_story_generator import _extract_action
        action = _extract_action("The system must allow users to register")
        assert "system" not in action.lower()
        assert "must" not in action.lower()

    def test_url_to_domain(self):
        from engine.user_story_generator import _extract_action
        action = _extract_action("https://testfort.com/software-testing-services")
        assert "testfort.com" in action
        assert "https://" not in action

    def test_lowercase_first_char(self):
        from engine.user_story_generator import _extract_action
        action = _extract_action("Login with email and password")
        assert action[0].islower()

    def test_preserves_meaningful_text(self):
        from engine.user_story_generator import _extract_action
        action = _extract_action("filter products by category and price")
        assert "filter" in action.lower()
        assert "products" in action.lower()


class TestDetectRole:
    """_detect_role() must identify user roles from text."""

    def test_admin_role(self):
        from engine.user_story_generator import _detect_role
        assert _detect_role("Administrator can manage users") == "administrator"

    def test_customer_role(self):
        from engine.user_story_generator import _detect_role
        assert _detect_role("Customer can place an order") == "customer"

    def test_default_role(self):
        from engine.user_story_generator import _detect_role
        assert _detect_role("filter products by price") == "user"


# ═══════════════════════════════════════════════════════════════════
# testcase_generator (output format)
# ═══════════════════════════════════════════════════════════════════

class TestChecklistOutput:
    """generate_checklist() must produce ChecklistItem with correct format."""

    def test_checklist_items_have_testfort_ids(self):
        from engine.testcase_generator import generate_checklist
        items = generate_checklist(
            [], "", raw_requirements=[{"text": "https://example.com"}]
        )
        assert len(items) > 0
        for item in items:
            assert "_" in item.id  # e.g. HDR_001
            parts = item.id.split("_")
            assert len(parts) == 2
            assert parts[1].isdigit()

    def test_checklist_items_have_sections(self):
        from engine.testcase_generator import generate_checklist
        items = generate_checklist(
            [], "", raw_requirements=[{"text": "https://example.com"}]
        )
        sections = {i.section for i in items}
        assert len(sections) >= 5

    def test_ids_are_unique(self):
        from engine.testcase_generator import generate_checklist
        items = generate_checklist(
            [], "", raw_requirements=[{"text": "https://example.com"}]
        )
        ids = [i.id for i in items]
        assert len(ids) == len(set(ids)), f"Duplicate IDs found: {len(ids)} vs {len(set(ids))}"


class TestTestCaseOutput:
    """generate_test_cases() must produce TestCase with correct format."""

    def test_tc_have_section_ids(self):
        from engine.testcase_generator import generate_test_cases
        cases = generate_test_cases(
            [], "", raw_requirements=[{"text": "User can log in"}]
        )
        assert len(cases) > 0
        for tc in cases:
            assert tc.id.startswith("SC")
            assert "_" in tc.id

    def test_tc_summaries_start_with_verify(self):
        from engine.testcase_generator import generate_test_cases
        cases = generate_test_cases(
            [], "", raw_requirements=[{"text": "User can log in"}]
        )
        for tc in cases:
            assert tc.summary.startswith("Verify that"), \
                f"Bad summary: {tc.summary[:80]}"

    def test_tc_have_steps(self):
        from engine.testcase_generator import generate_test_cases
        cases = generate_test_cases(
            [], "", raw_requirements=[{"text": "User can log in"}]
        )
        for tc in cases:
            assert tc.test_steps, f"Empty steps in {tc.id}"
            assert "1." in tc.test_steps

    def test_tc_ids_are_unique(self):
        from engine.testcase_generator import generate_test_cases
        cases = generate_test_cases(
            [], "", raw_requirements=[{"text": "User can log in and search"}]
        )
        ids = [tc.id for tc in cases]
        assert len(ids) == len(set(ids))


# ═══════════════════════════════════════════════════════════════════
# Page Number Detection (PDF artifact cleanup)
# ═══════════════════════════════════════════════════════════════════

class TestPageNumberDetection:
    """Page numbers from PDFs must be stripped from requirement text."""

    def test_standalone_page_number(self):
        from engine.file_parser import _is_page_number
        assert _is_page_number("93")
        assert _is_page_number("116")
        assert _is_page_number("1")
        assert _is_page_number("9999")

    def test_page_prefix(self):
        from engine.file_parser import _is_page_number
        assert _is_page_number("Page 5")
        assert _is_page_number("page 12")
        assert _is_page_number("p. 7")

    def test_dash_page_number(self):
        from engine.file_parser import _is_page_number
        assert _is_page_number("- 5 -")
        assert _is_page_number("— 12 —")

    def test_page_of_total(self):
        from engine.file_parser import _is_page_number
        assert _is_page_number("5 / 20")
        assert _is_page_number("5/20")

    def test_not_page_number(self):
        from engine.file_parser import _is_page_number
        assert not _is_page_number("User can log in")
        assert not _is_page_number("REQ-001: Login feature")
        assert not _is_page_number("12345")  # 5 digits = not a page

    def test_strip_trailing_page_number(self):
        from engine.file_parser import _strip_trailing_page_number
        assert _strip_trailing_page_number("packet Submission 93") == "packet Submission"
        assert _strip_trailing_page_number("packet Receipt 116") == "packet Receipt"

    def test_keep_meaningful_numbers(self):
        from engine.file_parser import _strip_trailing_page_number
        # Version numbers
        assert "v2" in _strip_trailing_page_number("module v2")
        # Error codes
        assert "404" in _strip_trailing_page_number("error code 404")
        # Step numbers
        assert "3" in _strip_trailing_page_number("step 3")

    def test_extract_action_strips_trailing_number(self):
        from engine.user_story_generator import _extract_action
        action = _extract_action("packet Submission 93")
        assert "93" not in action
        assert "submission" in action.lower() or "packet" in action.lower()


# ═══════════════════════════════════════════════════════════════════
# QA Team Lead Review
# ═══════════════════════════════════════════════════════════════════

class TestQATeamLeadReview:
    """QA Team Lead must catch and fix documentation quality issues."""

    def test_review_fixes_requirement_voiced_expected_result(self):
        """"must" / "shall" are rewritten onto the house voice.

        Neither reference corpus uses them: they state a requirement on
        the product rather than something a tester can record. They land
        on "should", which operator ruling 2026-08-28 made the voice the
        generator writes in an expected result — so one reviewed document
        holds one voice. See
        ``engine/qa_knowledge/style/house_style.yaml`` →
        ``expected_result``.
        """
        from engine.qa_team_lead import review_test_cases
        from types import SimpleNamespace
        tc = SimpleNamespace(
            id="SC1_001", summary="Verify that login works",
            expected_result="User must be authenticated. Session shall "
                            "be created.",
            preconditions="App is accessible.", test_steps="1. Login",
        )
        fixed, report = review_test_cases([tc])
        assert fixed[0].expected_result == ("User should be authenticated. "
                                            "Session should be created.")
        assert report.items_fixed > 0

    def test_review_keeps_should_voiced_expected_result(self):
        """"should" survives review untouched.

        The corpora disagree — the Odoo client deliverable never writes
        "should", the team's own reviewed training deliverable writes it
        throughout and the reviewing team lead let every instance stand.
        The operator settled it in favour of the training deliverable, so
        the reviewer neither rewrites nor flags it.
        """
        from engine.qa_team_lead import review_test_cases
        from types import SimpleNamespace
        tc = SimpleNamespace(
            id="SC1_001", summary="Verify that login works",
            expected_result="User should be authenticated. Session should "
                            "be created.",
            preconditions="App is accessible.", test_steps="1. Login",
        )
        fixed, report = review_test_cases([tc])
        assert fixed[0].expected_result == ("User should be authenticated. "
                                            "Session should be created.")
        assert not [f for f in report.findings
                    if f.category == "Expected Result Voice"]

    def test_review_leaves_declarative_expected_result_alone(self):
        from engine.qa_team_lead import review_test_cases
        from types import SimpleNamespace
        tc = SimpleNamespace(
            id="SC1_001", summary="Verify that login works",
            expected_result="User is authenticated. Session is created.",
            preconditions="App is accessible.",
            test_steps="1. Open the login page\n2. Submit valid credentials",
        )
        fixed, report = review_test_cases([tc])
        # Untouched. "should" is the voice the generator WRITES; it is not
        # a voice the reviewer imposes on text somebody else already wrote,
        # and the Odoo client corpus is declarative throughout.
        assert fixed[0].expected_result == ("User is authenticated. "
                                            "Session is created.")
        assert not [f for f in report.findings
                    if f.category == "Expected Result Voice"]

    def test_review_fixes_missing_verify_that(self):
        from engine.qa_team_lead import review_test_cases
        from types import SimpleNamespace
        tc = SimpleNamespace(
            id="SC1_001", summary="Login works correctly",
            expected_result="User should be logged in.",
            preconditions="App is accessible.", test_steps="1. Login",
        )
        fixed, report = review_test_cases([tc])
        assert fixed[0].summary.startswith("Verify that")

    def test_review_detects_page_number_in_summary(self):
        from engine.qa_team_lead import review_test_cases
        from types import SimpleNamespace
        tc = SimpleNamespace(
            id="SC3_021", summary="Verify that packet Submission 93 is handled correctly",
            expected_result="Should work.", preconditions="", test_steps="",
        )
        fixed, report = review_test_cases([tc])
        assert "93" not in fixed[0].summary
        assert any(f.category == "Page Number Artifact" for f in report.findings)

    def test_review_detects_duplicate_ids(self):
        from engine.qa_team_lead import review_test_cases
        from types import SimpleNamespace
        tc1 = SimpleNamespace(
            id="SC1_001", summary="Verify that A works",
            expected_result="Should work.", preconditions="", test_steps="",
        )
        tc2 = SimpleNamespace(
            id="SC1_001", summary="Verify that B works",
            expected_result="Should work.", preconditions="", test_steps="",
        )
        _, report = review_test_cases([tc1, tc2])
        assert any(f.category == "Duplicate ID" for f in report.findings)

    def test_checklist_review_fixes_verify_that(self):
        from engine.qa_team_lead import review_checklist
        from types import SimpleNamespace
        item = SimpleNamespace(
            id="HDR_001", objective="Logo is displayed",
            category="Positive", priority="High",
        )
        fixed, report = review_checklist([item])
        assert fixed[0].objective.startswith("Verify that")

    def test_review_strips_underscore_artifacts(self):
        from engine.qa_team_lead import review_test_cases
        from types import SimpleNamespace
        tc = SimpleNamespace(
            id="SC3_010",
            summary="Verify that tX Taxation _________________________________ is functioning as expected",
            expected_result="Should work.",
            preconditions="System is running.",
            test_steps="1. Navigate\n2. Perform the action: tX Taxation _______________________________________\n3. Observe",
            test_data="Valid input data",
        )
        fixed, report = review_test_cases([tc])
        assert "___" not in fixed[0].summary
        assert "___" not in fixed[0].test_steps
        assert any(f.category == "PDF Underscore Artifact" for f in report.findings)

    def test_extract_action_strips_underscores(self):
        from engine.user_story_generator import _extract_action
        action = _extract_action("tX Taxation _________________________________ some text")
        assert "___" not in action
        assert "  " not in action  # no double spaces

    def test_quality_score_calculation(self):
        from engine.qa_team_lead import review_test_cases
        from types import SimpleNamespace
        tc = SimpleNamespace(
            id="SC1_001", summary="Verify that login works",
            expected_result="User should be logged in.",
            preconditions="App is accessible.", test_steps="1. Login",
        )
        _, report = review_test_cases([tc])
        assert 0 <= report.quality_score <= 100


# ═══════════════════════════════════════════════════════════════════
# bug_report — ISTQB-mandatory metadata + step normalisation
# ═══════════════════════════════════════════════════════════════════

class TestBugReportSerialisation:
    """ISTQB metadata fields must round-trip cleanly through dict/session."""

    def test_round_trip_preserves_istqb_fields(self):
        from engine.bug_report import BugReport, bug_to_dict, dict_to_bug
        bug = BugReport(
            id="BUG-001", title="Login does not accept valid credentials",
            severity="Major", priority="High", status="Open",
            environment="Windows 11 / Chrome / Desktop / 1920x1080",
            preconditions="User account exists; site reachable.",
            steps_to_reproduce="1. Open site\n2. Submit credentials",
            actual_result="Login fails with 500.",
            expected_result="User is redirected to dashboard.",
            frequency="Always", affects_version="v1.0",
            found_in_build="Windows11-Chrome@20260424T1500",
            labels=["test_case", "category:negative"],
        )
        round_tripped = dict_to_bug(bug_to_dict(bug))
        assert round_tripped.frequency == "Always"
        assert round_tripped.affects_version == "v1.0"
        assert round_tripped.found_in_build == "Windows11-Chrome@20260424T1500"
        assert round_tripped.labels == ["test_case", "category:negative"]

    def test_legacy_dict_loads_with_defaults(self):
        """A bug snapshot saved before ISTQB fields existed must still load."""
        from engine.bug_report import dict_to_bug
        legacy = {
            "id": "BUG-001", "title": "x", "severity": "Minor",
            "priority": "Medium", "status": "Open", "environment": "",
            "preconditions": "", "steps_to_reproduce": "",
            "actual_result": "x", "expected_result": "y",
        }
        bug = dict_to_bug(legacy)
        assert bug.frequency == "Always"
        assert bug.affects_version == ""
        assert bug.found_in_build == ""


class TestStepsNormalisation:
    """Free-form step blobs must coerce to a clean numbered list."""

    def test_already_numbered_passes_through(self):
        from engine.bug_report import normalize_steps_to_numbered_list
        out = normalize_steps_to_numbered_list("1. Open page\n2. Click button\n3. Observe")
        assert out == "1. Open page\n2. Click button\n3. Observe"

    def test_inline_numbered_splits_correctly(self):
        from engine.bug_report import normalize_steps_to_numbered_list
        out = normalize_steps_to_numbered_list("1. Open page 2. Click button 3. Observe")
        # All three steps must end up on their own lines.
        assert out.count("\n") == 2
        assert out.startswith("1. ")
        assert "2. Click button" in out

    def test_bulleted_input_renumbered(self):
        from engine.bug_report import normalize_steps_to_numbered_list
        out = normalize_steps_to_numbered_list("- Open page\n- Click button\n* Observe")
        assert out.startswith("1. Open page")
        assert "2. Click button" in out
        assert "3. Observe" in out

    def test_single_sentence_becomes_one_step(self):
        from engine.bug_report import normalize_steps_to_numbered_list
        out = normalize_steps_to_numbered_list("User attempts to log in.")
        assert out == "1. User attempts to log in"

    def test_empty_returns_empty(self):
        from engine.bug_report import normalize_steps_to_numbered_list
        assert normalize_steps_to_numbered_list("") == ""
        assert normalize_steps_to_numbered_list("   \n  ") == ""


class TestBugSynthesisFromChecklist:
    """The previous bug — checklist-derived bugs had empty Steps to
    Reproduce — is the regression this whole feature is closing. Pin
    it so the field can never silently go empty again."""

    def test_checklist_steps_are_never_empty(self):
        from engine.qa_testers import _make_steps_for_checklist
        steps = _make_steps_for_checklist(
            objective="Verify that the search bar accepts long queries",
            section="Search",
            environment="Windows 11 / Chrome / Desktop / 1920x1080",
            site_url="https://example.com",
        )
        assert steps  # non-empty
        assert steps.startswith("1. ")
        # Must mention the URL, the section, and end with an observation.
        assert "https://example.com" in steps
        assert "Search" in steps
        assert "Observe" in steps.splitlines()[-1] or "observe" in steps.splitlines()[-1].lower()

    def test_checklist_preconditions_are_never_empty(self):
        from engine.qa_testers import _make_preconditions
        pre = _make_preconditions(
            item_type="checklist", section="Auth",
            environment="Windows / Chrome / Desktop / 1920x1080",
            site_url="https://example.com", item_preconditions="",
        )
        assert pre.endswith(".")
        assert "https://example.com" in pre
        # Should mention the platform/browser combo so a developer can
        # replicate the run without consulting the original test plan.
        assert "Chrome" in pre

    def test_objective_keyword_picks_specific_action(self):
        from engine.qa_testers import _make_steps_for_checklist
        login_steps = _make_steps_for_checklist(
            "Verify login works for valid credentials",
            "Auth", "Win / Chrome / Desktop / 1920x1080", "https://x.com",
        )
        # The keyword "login" should yield the credentials-submission
        # action, not the generic "perform the action: ..." fallback.
        assert "credentials" in login_steps.lower()

    def test_found_in_build_format(self):
        from engine.qa_testers import _make_found_in_build
        out = _make_found_in_build(
            "Windows 11 / Chrome / Desktop / 1920x1080",
            "2026-04-25T15:30:00+00:00",
        )
        # Format: <plat>-<browser>@<YYYYMMDDTHHMM>, no spaces.
        assert " " not in out
        assert "@" in out
        assert out.startswith("Windows11-Chrome")
