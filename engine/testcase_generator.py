"""
TestFortge — Test Case & Checklist Generator (TestFort Template)

TestFort Test Case format:
  №  | Summary | Preconditions | Test Steps | Test Data | Expected Result | Issues | Comment
  SC1_001, SC1_002... grouped by Sections

TestFort Checklist format:
  #  | Objective | Comments/Issues
  HDR_001, FTR_001... grouped by Sections with prefix IDs

Statuses: Passed | Failed | Passed but | Blocked | Unchecked

Covers: Positive, Negative, Edge/Boundary cases.
"""

from dataclasses import dataclass, field
from .user_story_generator import UserStory


@dataclass
class TestCase:
    id: str               # SC1_001 format
    section: str           # Section name (e.g. "Header", "Login")
    section_num: int       # Section number
    summary: str           # Test case summary
    preconditions: str     # Preconditions if needed
    test_steps: str        # Numbered steps: "1. Step\n2. Step..."
    test_data: str         # Test data if needed
    expected_result: str   # Expected behavior description
    issues: str = ""       # Issue references
    comment: str = ""      # Additional info
    user_story_id: str = ""
    category: str = ""     # Positive / Negative / Edge Case / Security
    priority: str = "Medium"
    status: str = "Unchecked"  # Passed / Failed / Passed but / Blocked / Unchecked


@dataclass
class ChecklistItem:
    id: str               # HDR_001, FTR_001 format
    section: str           # Section name
    objective: str         # Summary of the checklist item
    comments: str = ""     # Comments / Issues
    user_story_id: str = ""
    category: str = ""     # Positive / Negative / Edge Case
    priority: str = "Medium"
    status: str = "Unchecked"


# ── Section prefix mapping ──────────────────────────────────────

_SECTION_PREFIXES = {
    "auth": ("AUTH", "Authentication"),
    "crud_create": ("CRT", "Create / Add"),
    "crud_read": ("VIEW", "View / Display"),
    "crud_update": ("UPD", "Edit / Update"),
    "crud_delete": ("DEL", "Delete / Remove"),
    "search": ("SRCH", "Search / Filter"),
    "payment": ("PAY", "Payment / Checkout"),
    "upload": ("UPL", "Upload / Import"),
    "export": ("EXP", "Export / Download"),
    "notification": ("NTF", "Notifications"),
    "security": ("SEC", "Security"),
    "performance": ("PERF", "Performance"),
    "integration": ("API", "API / Integration"),
    "navigation": ("NAV", "Navigation"),
    "profile": ("PROF", "Profile / Settings"),
    "generic": ("GEN", "General"),
}

# ── Keyword-based scenario detection ────────────────────────────

_SCENARIO_KEYWORDS = {
    "auth": ["login", "register", "sign up", "sign in", "password", "authentication",
             "logout", "session", "credential", "2fa", "mfa"],
    "crud_create": ["create", "add", "new", "register", "insert", "submit"],
    "crud_read": ["view", "display", "show", "list", "read", "get", "dashboard", "report"],
    "crud_update": ["edit", "update", "modify", "change", "save"],
    "crud_delete": ["delete", "remove", "cancel", "deactivate"],
    "search": ["search", "find", "filter", "sort", "query", "lookup"],
    "payment": ["payment", "pay", "checkout", "billing", "transaction", "purchase", "order"],
    "upload": ["upload", "import", "attach", "file"],
    "export": ["export", "download", "report", "csv", "pdf"],
    "notification": ["notification", "notify", "alert", "email", "sms", "push"],
    "security": ["encrypt", "permission", "role", "access", "authorize", "protect"],
    "performance": ["performance", "load", "speed", "fast", "response time", "scalab"],
    "integration": ["api", "integrate", "third-party", "webhook", "endpoint"],
}


def _detect_scenario(text: str) -> str:
    lower = text.lower()
    for scenario, keywords in _SCENARIO_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return scenario
    return "generic"


def _short(text: str, maxlen: int = 80) -> str:
    text = text.strip().rstrip(".")
    return text if len(text) <= maxlen else text[:maxlen - 3] + "..."


# ── Test Case generators per scenario ───────────────────────────

def _make_tc(section_num: int, idx: int, section: str, prefix: str,
             story: UserStory, category: str, summary: str, preconditions: str,
             steps: list[str], test_data: str, expected: str, comment: str = "") -> TestCase:
    tc_id = f"SC{section_num}_{idx:03d}"
    steps_text = "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps))
    return TestCase(
        id=tc_id, section=section, section_num=section_num,
        summary=summary, preconditions=preconditions,
        test_steps=steps_text, test_data=test_data,
        expected_result=expected, comment=comment,
        user_story_id=story.id, category=category, priority=story.priority,
    )


def _auth_tests(sn: int, story: UserStory, idx: list[int]) -> list[TestCase]:
    prefix, section = _SECTION_PREFIXES["auth"]
    action = _short(story.action)
    cases = []

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Positive",
        f"Verify that {action} is completed successfully with valid credentials",
        "Application is accessible. Test user account is created.",
        ["Open the application", "Navigate to the authentication page",
         "Enter valid credentials", "Click Submit/Login button",
         "Pay attention to the result"],
        "Valid email and password",
        "User should be authenticated successfully and redirected to the expected page."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Negative",
        f"Verify that {action} is rejected when invalid credentials are provided",
        "Application is accessible. Authentication page is opened.",
        ["Open the application", "Navigate to the authentication page",
         "Enter invalid email/username", "Enter incorrect password",
         "Click Submit/Login button"],
        "Invalid email: test@invalid, Wrong password: abc123",
        "Error message should be displayed. User should NOT be authenticated. No sensitive info should be leaked."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Edge Case",
        f"Verify that validation is triggered for empty fields during {action}",
        "Application is accessible. Authentication page is opened.",
        ["Leave all fields empty", "Click Submit/Login button",
         "Fill only email field, leave password empty, click Submit",
         "Fill only password field, leave email empty, click Submit"],
        "",
        "Validation errors should be shown for each required field. Form should not be submitted."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Security",
        f"Verify that brute-force and injection attacks are blocked during {action}",
        "Application is accessible. Authentication page is opened.",
        ["Attempt login with wrong password 5+ times consecutively",
         "Verify account lockout or CAPTCHA appears",
         "Try SQL injection in the email field (e.g., ' OR 1=1 --)",
         "Try XSS payload in input fields"],
        "SQL: ' OR 1=1 --, XSS: <script>alert(1)</script>",
        "Brute-force protection should activate. Injection attacks should be sanitized. No system errors should occur."))
    idx[0] += 1

    return cases


def _crud_create_tests(sn: int, story: UserStory, idx: list[int]) -> list[TestCase]:
    prefix, section = _SECTION_PREFIXES["crud_create"]
    action = _short(story.action)
    cases = []

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Positive",
        f"Verify that {action} is performed successfully with valid data",
        "User is authenticated. Creation form is accessible.",
        ["Navigate to the creation form/page", "Fill all required fields with valid data",
         "Click Save/Submit button", "Verify the new record appears in the list/view"],
        "Valid data for all required fields",
        "New record should be created successfully. Success message should be displayed. Data should be persisted."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Negative",
        f"Verify that {action} is rejected when invalid or missing data is submitted",
        "User is authenticated. Creation form is opened.",
        ["Leave all required fields empty and click Submit",
         "Enter invalid format data (letters in number fields, wrong email format)",
         "Click Save/Submit button"],
        "Empty fields, invalid email: @notvalid, letters in phone field",
        "Record should NOT be created. Clear validation error messages should be displayed for each invalid field."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Edge Case",
        f"Verify that boundary values and special characters are handled correctly for {action}",
        "User is authenticated. Creation form is opened.",
        ["Enter minimum length values (1 character) in text fields",
         "Enter maximum length values in text fields",
         "Enter special characters, Unicode, emoji in text fields",
         "Attempt to submit duplicate data"],
        "Min: 'A', Max: 255 chars, Special: @#$%^&*(), Emoji: test",
        "System should handle boundary values correctly. Special characters should be accepted or properly sanitized."))
    idx[0] += 1

    return cases


def _crud_read_tests(sn: int, story: UserStory, idx: list[int]) -> list[TestCase]:
    prefix, section = _SECTION_PREFIXES["crud_read"]
    action = _short(story.action)
    cases = []

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Positive",
        f"Verify that data is displayed correctly for {action}",
        "User is authenticated. Test data is present in the system.",
        ["Navigate to the view/list page", "Verify all expected data columns/fields are visible",
         "Verify data formatting (dates, numbers, currencies)",
         "Verify pagination works if applicable"],
        "",
        "Data should be displayed correctly, completely, and in the right format."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Edge Case",
        f"Verify that empty state is handled gracefully for {action}",
        "User is authenticated. No data is present for this view.",
        ["Navigate to the view/list page",
         "Verify appropriate empty state message is shown",
         "Verify no errors or broken layout"],
        "",
        "Empty state should be handled gracefully with appropriate message. No broken UI elements should be present."))
    idx[0] += 1

    return cases


def _crud_update_tests(sn: int, story: UserStory, idx: list[int]) -> list[TestCase]:
    prefix, section = _SECTION_PREFIXES["crud_update"]
    action = _short(story.action)
    cases = []

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Positive",
        f"Verify that {action} is saved successfully with valid changes",
        "User is authenticated. Record to be edited is present in the system.",
        ["Navigate to the edit form of an existing record",
         "Verify form is pre-filled with current data",
         "Modify one or more fields with valid data",
         "Click Save/Submit button",
         "Verify changes are persisted and displayed correctly"],
        "Updated valid data",
        "Record should be updated successfully. Changes should be persisted. Success message should be shown."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Negative",
        f"Verify that {action} is rejected when invalid data is submitted",
        "User is authenticated. Edit form is opened.",
        ["Clear a required field", "Enter invalid data format",
         "Click Save/Submit button"],
        "Empty required field, invalid format data",
        "Invalid changes should be rejected. Validation error messages should be displayed. Original data should be preserved."))
    idx[0] += 1

    return cases


def _crud_delete_tests(sn: int, story: UserStory, idx: list[int]) -> list[TestCase]:
    prefix, section = _SECTION_PREFIXES["crud_delete"]
    action = _short(story.action)
    cases = []

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Positive",
        f"Verify that {action} is completed successfully and record is removed",
        "User is authenticated. Record to be deleted is present in the system.",
        ["Select the record to delete", "Click Delete button",
         "Confirm the deletion in the confirmation dialog",
         "Verify record is removed from the list"],
        "",
        "Record should be deleted successfully. Success message should be shown. Record should no longer appear."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Negative",
        f"Verify that {action} is cancelled when confirmation is declined",
        "User is authenticated. Confirmation dialog is displayed.",
        ["Click Cancel on deletion confirmation dialog",
         "Verify the record still exists and is unchanged"],
        "",
        "Record should NOT be deleted. Data should remain intact."))
    idx[0] += 1

    return cases


def _search_tests(sn: int, story: UserStory, idx: list[int]) -> list[TestCase]:
    prefix, section = _SECTION_PREFIXES["search"]
    action = _short(story.action)
    cases = []

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Positive",
        f"Verify that correct results are returned for {action}",
        "User is authenticated. Searchable data is present in the system.",
        ["Enter a valid search term that matches existing data",
         "Submit search", "Verify results match the search criteria",
         "Verify result count is accurate"],
        "Search term matching existing data",
        "Search should return accurate, relevant results. Result count should match."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Negative",
        f"Verify that 'no results' message is displayed when no matches are found for {action}",
        "User is authenticated. Search functionality is available.",
        ["Enter a search term with no matching data",
         "Submit search", "Verify 'No results' message is displayed"],
        "Search term: 'xyznonexistent123'",
        "'No results found' message should be displayed. No errors should occur."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Edge Case",
        f"Verify that special input is handled correctly for {action}",
        "User is authenticated. Search functionality is available.",
        ["Search with special characters (!@#$%^&*)",
         "Search with a very long string (500+ characters)",
         "Search with SQL injection attempt",
         "Search with empty query"],
        "Special: !@#$%, Long: 500+ chars, SQL: ' OR 1=1 --",
        "All edge cases should be handled without errors or security issues."))
    idx[0] += 1

    return cases


def _payment_tests(sn: int, story: UserStory, idx: list[int]) -> list[TestCase]:
    prefix, section = _SECTION_PREFIXES["payment"]
    action = _short(story.action)
    cases = []

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Positive",
        f"Verify that payment is completed successfully for {action}",
        "User is authenticated. Items are added to the cart. Payment gateway is configured.",
        ["Proceed to checkout page", "Verify order summary is correct",
         "Enter valid payment details", "Confirm payment",
         "Verify order confirmation and receipt"],
        "Valid card: 4242 4242 4242 4242, Exp: 12/28, CVC: 123",
        "Payment should complete successfully. Order confirmation should be displayed with correct amount."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Negative",
        f"Verify that declined payment is handled gracefully for {action}",
        "Checkout page is opened. Payment form is displayed.",
        ["Enter payment details that will be declined",
         "Submit payment", "Verify error message is displayed",
         "Verify cart/order is preserved for retry"],
        "Declined card: 4000 0000 0000 0002",
        "Payment decline should be handled gracefully. Clear error message should be displayed. Cart should be preserved."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Edge Case",
        f"Verify that payment timeout and double-charge are prevented for {action}",
        "Payment confirmation step is reached. Network conditions are simulated.",
        ["Simulate network timeout during payment processing",
         "Check if payment was charged or not",
         "Verify no duplicate charges exist",
         "Verify order status is consistent with payment state"],
        "",
        "No duplicate charges should occur. Order and payment states should be consistent.",
        comment="Critical edge case - verify with payment gateway logs"))
    idx[0] += 1

    return cases


def _generic_tests(sn: int, story: UserStory, idx: list[int]) -> list[TestCase]:
    prefix, section = _SECTION_PREFIXES["generic"]
    action = _short(story.action)
    cases = []

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Positive",
        f"Verify that {action} is functioning as expected",
        "System is running. User is authenticated (if applicable).",
        ["Set up required preconditions",
         f"Execute the action: {_short(story.action, 60)}",
         "Verify the expected outcome"],
        "",
        f"Feature should work as specified: {_short(story.original_text, 120)}"))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Negative",
        f"Verify that errors are handled correctly for {action}",
        "System is running. Feature is accessible.",
        ["Provide invalid or missing input",
         "Attempt the action",
         "Verify error message is user-friendly and informative"],
        "Invalid/empty input data",
        "Invalid input should be rejected with appropriate, user-friendly error message."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Edge Case",
        f"Verify that edge cases are handled without errors for {action}",
        "System is running. Feature is accessible.",
        ["Test with boundary values (min, max, empty, overflow)",
         "Test with special characters / Unicode / emoji",
         "Test with concurrent access if applicable"],
        "Boundary: 0, 1, MAX, -1; Special: @#$%^&*()",
        "Edge cases should be handled without errors or data corruption."))
    idx[0] += 1

    return cases


# ── Scenario → generator mapping ─────────────────────────────────

_GENERATORS = {
    "auth": _auth_tests,
    "crud_create": _crud_create_tests,
    "crud_read": _crud_read_tests,
    "crud_update": _crud_update_tests,
    "crud_delete": _crud_delete_tests,
    "search": _search_tests,
    "payment": _payment_tests,
    "upload": _generic_tests,
    "export": _generic_tests,
    "notification": _generic_tests,
    "security": _generic_tests,
    "performance": _generic_tests,
    "integration": _generic_tests,
    "generic": _generic_tests,
}


# ── Custom prompt parsing ────────────────────────────────────────

def _parse_tc_prompt(custom_prompt: str) -> dict:
    """Parse custom prompt into directives for test case generation."""
    directives: dict = {
        "categories": None,       # e.g. ["Positive", "Negative"]
        "extra_scenarios": [],     # forced scenario types
    }
    if not custom_prompt:
        return directives

    lower = custom_prompt.lower()

    # Filter by categories
    requested_cats = []
    if "positive" in lower and "only" in lower:
        requested_cats = ["Positive"]
    elif "negative" in lower and "only" in lower:
        requested_cats = ["Negative"]
    elif "security" in lower and ("only" in lower or "focus" in lower):
        requested_cats = ["Security"]
    else:
        if "positive" in lower:
            requested_cats.append("Positive")
        if "negative" in lower:
            requested_cats.append("Negative")
        if "edge" in lower or "boundary" in lower:
            requested_cats.append("Edge Case")
        if "security" in lower:
            requested_cats.append("Security")
    if requested_cats:
        directives["categories"] = requested_cats

    # Force extra scenarios
    scenario_keywords = {
        "auth": ["auth", "login", "register"],
        "security": ["security", "injection", "xss"],
        "payment": ["payment", "checkout"],
        "search": ["search", "filter"],
        "performance": ["performance", "load"],
    }
    for scenario, kws in scenario_keywords.items():
        if any(kw in lower for kw in kws):
            directives["extra_scenarios"].append(scenario)

    return directives


# ── Public API ───────────────────────────────────────────────────

def generate_test_cases(stories: list[UserStory], custom_prompt: str = "",
                        raw_requirements: list[dict] | None = None) -> list[TestCase]:
    """Generate professional test cases using QA persona.

    The Senior QA Engineer persona analyzes input to determine testing areas
    and generates domain-specific test cases from the ISTQB knowledge base,
    then supplements with story-specific cases for any requirements not
    covered by the knowledge base.
    """
    from .qa_persona import (analyze_input, generate_professional_test_cases,
                             is_instruction)

    # Build requirements list for the persona
    reqs = raw_requirements or []
    if not reqs and stories:
        reqs = [{"text": s.original_text} for s in stories]

    # Filter out instruction lines
    reqs = [r for r in reqs if not is_instruction(r.get("text", ""))]

    analysis = analyze_input(reqs, custom_prompt)
    pro_templates = generate_professional_test_cases(analysis, stories, custom_prompt)

    # Convert TCTemplate → TestCase with TestFort IDs
    all_cases: list[TestCase] = []
    section_map: dict[str, int] = {}  # section_name → section_num
    section_counters: dict[int, int] = {}  # section_num → next idx
    next_section = 1

    directives = _parse_tc_prompt(custom_prompt)

    for tmpl in pro_templates:
        section = tmpl.section or "General"
        if section not in section_map:
            section_map[section] = next_section
            section_counters[next_section] = 1
            next_section += 1

        sn = section_map[section]
        idx = section_counters[sn]
        tc_id = f"SC{sn}_{idx:03d}"
        section_counters[sn] = idx + 1

        steps_text = "\n".join(f"{i+1}. {s}" for i, s in enumerate(tmpl.steps))
        tc = TestCase(
            id=tc_id, section=section, section_num=sn,
            summary=tmpl.summary, preconditions=tmpl.preconditions,
            test_steps=steps_text, test_data=tmpl.test_data,
            expected_result=tmpl.expected_result,
            category=tmpl.category, priority=tmpl.priority,
        )
        all_cases.append(tc)

    # Apply custom prompt category filter
    if directives["categories"]:
        all_cases = [tc for tc in all_cases if tc.category in directives["categories"]]

    # QA Team Lead review — validate and auto-fix documentation quality
    from .qa_team_lead import review_test_cases
    all_cases, _review_report = review_test_cases(all_cases)

    return all_cases


def generate_checklist(stories: list[UserStory], custom_prompt: str = "",
                       raw_requirements: list[dict] | None = None) -> list[ChecklistItem]:
    """Generate a professional low-level checklist using QA persona.

    The Senior QA Engineer persona analyzes input to determine testing areas
    and generates domain-specific checks from the ISTQB knowledge base.
    """
    from .qa_persona import analyze_input, generate_professional_checklist, is_instruction

    # Build requirements list for the persona
    reqs = raw_requirements or []
    if not reqs and stories:
        reqs = [{"text": s.original_text} for s in stories]

    # Filter out instruction lines
    reqs = [r for r in reqs if not is_instruction(r.get("text", ""))]

    analysis = analyze_input(reqs, custom_prompt)
    pro_items = generate_professional_checklist(analysis, custom_prompt)

    # Convert to ChecklistItem with TestFort IDs
    items: list[ChecklistItem] = []
    prefix_counters: dict[str, int] = {}
    from .qa_persona import _SECTION_PREFIXES as QA_PREFIXES

    for item in pro_items:
        prefix = QA_PREFIXES.get(item.section, "GEN")
        if prefix not in prefix_counters:
            prefix_counters[prefix] = 1
        counter = prefix_counters[prefix]

        items.append(ChecklistItem(
            id=f"{prefix}_{counter:03d}",
            section=item.section,
            objective=item.objective,
            category=item.category,
            priority=item.priority,
        ))
        prefix_counters[prefix] = counter + 1

    # QA Team Lead review — validate and auto-fix documentation quality
    from .qa_team_lead import review_checklist
    items, _review_report = review_checklist(items)

    return items


def generate_traceability(stories: list[UserStory], test_cases: list[TestCase]) -> list[dict]:
    """Build traceability: Requirement -> User Story -> Test Cases."""
    matrix = []
    for story in stories:
        linked = [tc for tc in test_cases if tc.user_story_id == story.id]
        matrix.append({
            "requirement_id": story.requirement_id,
            "user_story_id": story.id,
            "user_story": f"As a {story.role}, I want {_short(story.action, 80)}",
            "priority": story.priority,
            "test_case_ids": [tc.id for tc in linked],
            "test_count": len(linked),
            "categories": sorted(set(tc.category for tc in linked)),
        })
    return matrix
