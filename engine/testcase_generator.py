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
        f"Log in with a valid account and confirm the session opens ({action})",
        "Application is reachable. A pre-registered test account exists with a known password.",
        ["Open the application base URL in the browser",
         "Click the Sign In / Login link in the Header",
         "Enter the registered email into the email field",
         "Enter the matching password into the password field",
         "Click the Submit button",
         "Observe the URL and any authenticated UI elements that appear"],
        "Valid email: existing registered address. Password: the one set for that account.",
        "The URL changes to the post-login page (dashboard / account). Session cookie is set. The Header shows the user menu instead of the Sign In link. No error banner is rendered."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Negative",
        f"Attempt login with a non-matching password and confirm the attempt is rejected ({action})",
        "Application is reachable. Login page is opened.",
        ["Enter a registered email into the email field",
         "Enter a password that does not match the stored hash",
         "Click the Submit button",
         "Inspect the response body and any visible error banner"],
        "Email: valid registered address. Password: deliberately wrong value, e.g. wrong-pass-123.",
        "The URL stays on the login page. An error banner states the login was refused. The response body does not disclose whether the email or the password was the failing field. No session cookie is issued."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Edge Case",
        f"Submit the login form with missing fields and confirm client-side validation fires ({action})",
        "Application is reachable. Login page is opened.",
        ["Leave both the email and the password fields empty and click Submit",
         "Enter only the email, leave the password empty, then click Submit",
         "Clear the email, enter only the password, then click Submit"],
        "",
        "In each case the form does not issue a network request; the empty field is highlighted and an inline validation message identifies which field is required. The login page URL is unchanged."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Security",
        f"Probe the login form for brute-force throttling and injection handling ({action})",
        "Application is reachable. Login page is opened. Rate-limit window is reset for the test account.",
        ["Submit the login form six times in a row with a wrong password for the same email",
         "Observe whether the next attempt shows a lockout message or a CAPTCHA challenge",
         "Enter the SQL-injection payload into the email field and click Submit",
         "Enter the XSS payload into the email field and click Submit",
         "Open DevTools → Network and inspect the response for each attempt"],
        "SQL payload: ' OR 1=1 --  |  XSS payload: <script>alert(1)</script>",
        "After the throttling threshold the server responds with 429 or a CAPTCHA is rendered. The SQL payload is stored as literal text; no server-side error (500) is returned. The XSS payload is escaped in the DOM and the alert does not fire."))
    idx[0] += 1

    return cases


def _crud_create_tests(sn: int, story: UserStory, idx: list[int]) -> list[TestCase]:
    prefix, section = _SECTION_PREFIXES["crud_create"]
    action = _short(story.action)
    cases = []

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Positive",
        f"Submit the creation form with valid data and confirm the record is persisted ({action})",
        "User is authenticated. Creation form route is reachable from the menu.",
        ["Click the Create / New / Add button in the list view",
         "Enter valid values into each required field in the form",
         "Click the Save button",
         "Open the list view again and locate the newly created row",
         "Click the row to open the detail view and compare each field with the submitted values"],
        "Valid data for every required field (respecting stated min/max lengths).",
        "The POST request returns HTTP 200 or 201 with a record id. A success toast is rendered. The list view shows the new row at the expected position, and the detail view displays the exact submitted values."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Negative",
        f"Submit the creation form with empty or malformed input and confirm the server rejects it ({action})",
        "User is authenticated. Creation form is opened.",
        ["Leave every required field empty and click Save",
         "Enter an email without an @ sign into an email field",
         "Enter letters into a numeric field",
         "Click Save after each mutation and inspect the response in DevTools → Network"],
        "Empty value, malformed email: notvalid, letters in a numeric field: abc.",
        "The server returns HTTP 400 / 422 or client-side validation blocks the request. No new record appears in the list view. Each failing field is highlighted with an inline message identifying the rule that was violated."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Edge Case",
        f"Test boundary lengths, Unicode payloads, and duplicate submission for {action}",
        "User is authenticated. Creation form is opened.",
        ["Enter a single-character value into each text field and click Save",
         "Enter a value at the documented maximum length into each text field and click Save",
         "Enter Unicode and emoji characters into a text field and click Save",
         "Open a second browser tab, submit the identical record again, and click Save"],
        "Min: A. Max: 255 chars of lorem ipsum. Unicode: Тест Ω 你好 . Duplicate: identical primary-key values.",
        "Boundary-length submissions persist exactly as entered. Unicode / emoji round-trip through the detail view with the same code points. The duplicate attempt is rejected with HTTP 409 or a uniqueness-violation message instead of creating a second record."))
    idx[0] += 1

    return cases


def _crud_read_tests(sn: int, story: UserStory, idx: list[int]) -> list[TestCase]:
    prefix, section = _SECTION_PREFIXES["crud_read"]
    action = _short(story.action)
    cases = []

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Positive",
        f"Load the list view with seeded data and compare cell values against the source ({action})",
        "User is authenticated. At least three seed records exist in the backing store.",
        ["Open the list view URL in the browser",
         "Count the rows rendered in the table body and compare with the seed count",
         "Read the first row top-to-bottom and compare each cell with the source record",
         "Scroll the horizontal axis (or widen the viewport) and confirm every declared column header is visible",
         "Locate the first date cell and confirm the format matches the documented locale (e.g. YYYY-MM-DD)",
         "Click the next-page control and confirm the URL gains a page parameter and the table swaps to the next slice"],
        "At least three seeded records with varied dates, numbers and currency values.",
        "The row count matches the seed count exactly. Every declared column header is present. Date, number and currency cells render in the documented format. Pagination advances the URL and the table body, and the page-number control reflects the new page."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Edge Case",
        f"Load the list view with zero rows and confirm the empty state is rendered ({action})",
        "User is authenticated. No records exist for this view (fresh tenant or filtered to zero).",
        ["Open the list view URL in the browser",
         "Scan the page for the empty-state element (icon + message + call-to-action)",
         "Open DevTools → Console and confirm no red errors were logged",
         "Inspect the layout — the Header, Footer and sidebars are still present and aligned"],
        "",
        "A dedicated empty-state block is rendered with an explanatory message. No JavaScript errors appear in the console. The page chrome (Header, Footer, navigation) remains laid out; no ghost rows or skeleton loaders are stuck on screen."))
    idx[0] += 1

    return cases


def _crud_update_tests(sn: int, story: UserStory, idx: list[int]) -> list[TestCase]:
    prefix, section = _SECTION_PREFIXES["crud_update"]
    action = _short(story.action)
    cases = []

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Positive",
        f"Edit an existing record, save, and confirm the change round-trips through the detail view ({action})",
        "User is authenticated. A known record exists and its current values are captured.",
        ["Click the target row in the list view to open the detail view",
         "Click the Edit button",
         "Confirm each form field is pre-populated with the existing value",
         "Change at least one field to a new valid value and note the exact new value",
         "Click Save",
         "Reload the detail view via the browser refresh control",
         "Compare every field with the notes taken before the edit and with the new value entered"],
        "A new valid value for one field, different from the current stored value.",
        "The PUT / PATCH request returns HTTP 200. The success toast references the updated record. After reload the edited field shows the new value, every other field is unchanged, and the updated_at timestamp has advanced."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Negative",
        f"Attempt to save invalid or missing data and confirm the original values are preserved ({action})",
        "User is authenticated. The edit form of an existing record is open.",
        ["Clear a required field and click Save",
         "Enter a malformed value (e.g. bad email format) into a validated field and click Save",
         "Leave the form open, navigate back to the list view, then reopen the same record"],
        "Empty value for a required field. Malformed email: notvalid.",
        "The server returns HTTP 400 / 422 or the client blocks submission. An inline error identifies the failing field. When the record is reopened from the list view its fields match the pre-edit values, confirming no partial write reached the store."))
    idx[0] += 1

    return cases


def _crud_delete_tests(sn: int, story: UserStory, idx: list[int]) -> list[TestCase]:
    prefix, section = _SECTION_PREFIXES["crud_delete"]
    action = _short(story.action)
    cases = []

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Positive",
        f"Delete an existing record through the confirmation dialog and confirm it no longer appears ({action})",
        "User is authenticated. A known record exists (id recorded).",
        ["Open the list view",
         "Note the row count in the table footer / pagination summary",
         "Click the target row to open the detail view",
         "Click the Delete button",
         "Click Confirm in the modal dialog",
         "Wait for the redirect back to the list view",
         "Search the list for the deleted record id",
         "Try to open the detail URL of the deleted record directly in the address bar"],
        "",
        "The DELETE request returns HTTP 200 or 204. The row count decreases by one. A confirmation toast is displayed. Searching for the deleted id yields no results. Opening its detail URL returns HTTP 404 (or the configured not-found view)."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Negative",
        f"Open the delete confirmation dialog, click Cancel, and confirm the record is untouched ({action})",
        "User is authenticated. The delete confirmation modal is visible for a known record.",
        ["Click the Cancel button (or the close-X control) on the confirmation modal",
         "Verify the modal closes without issuing a network request (check DevTools → Network)",
         "Open the same record from the list view",
         "Compare every field with the pre-cancel values"],
        "",
        "No DELETE request is sent. The record is still present in the list view and the detail view shows the original field values with an unchanged updated_at."))
    idx[0] += 1

    return cases


def _search_tests(sn: int, story: UserStory, idx: list[int]) -> list[TestCase]:
    prefix, section = _SECTION_PREFIXES["search"]
    action = _short(story.action)
    cases = []

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Positive",
        f"Search for a known seeded term and compare the returned rows against expectations ({action})",
        "User is authenticated. A seed record exists whose title/name contains the known term.",
        ["Click the search input in the Header / toolbar",
         "Enter the known term exactly as stored",
         "Press Enter or click the Search button",
         "Count the rows in the result table and compare with the pre-computed expected count",
         "Read the first result row and confirm the term is present in the expected column"],
        "Search term: a substring that appears in exactly one seeded record.",
        "The request returns HTTP 200 within 2 s. The result count shown in the UI equals the expected count. Each returned row contains the search term in at least one displayed column. The query is reflected in the URL so the result set is shareable."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Negative",
        f"Search for a term that does not exist and confirm the empty-results state is shown ({action})",
        "User is authenticated. Search bar is visible.",
        ["Click the search input",
         "Enter a string that is guaranteed not to exist in the dataset",
         "Press Enter",
         "Observe the results area and any suggestion text"],
        "Search term: xyznonexistent123.",
        "Zero rows are rendered. An explicit 'No results' message is shown in place of the table. No red error banner or console error appears. The URL still carries the query so the empty state is bookmarkable."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Edge Case",
        f"Submit unusual payloads to the search input and inspect how they are handled ({action})",
        "User is authenticated. Search bar is visible.",
        ["Enter a string of special characters into the search input and press Enter",
         "Paste a 500-character string into the search input and press Enter",
         "Enter the SQL-injection payload into the search input and press Enter",
         "Clear the input entirely and press Enter"],
        "Special: !@#$%^&*  |  Long: 500+ chars  |  SQL: ' OR 1=1 --",
        "Each query returns within the timeout with HTTP 200 or 400. The long string is either accepted or rejected with a length-limit message; the server does not 500. The SQL payload is treated as literal text and does not return the full dataset. The empty query either shows the full list or an explicit prompt; no stack trace is shown."))
    idx[0] += 1

    return cases


def _payment_tests(sn: int, story: UserStory, idx: list[int]) -> list[TestCase]:
    prefix, section = _SECTION_PREFIXES["payment"]
    action = _short(story.action)
    cases = []

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Positive",
        f"Complete a checkout with a test-mode approved card and verify the order + receipt ({action})",
        "User is authenticated. Cart contains at least one item with a known price. Payment gateway is in test mode.",
        ["Click the cart icon in the Header to open the cart view",
         "Note the displayed subtotal, tax and grand total",
         "Click the Checkout button",
         "Enter the gateway test card number, expiry and CVC into the payment form",
         "Click Pay / Place Order",
         "Wait for the redirect to the order-confirmation page",
         "Compare the amount on the confirmation page with the grand total noted earlier",
         "Open the gateway dashboard (or the orders admin) and locate the transaction id"],
        "Test card: 4242 4242 4242 4242. Expiry: 12/28. CVC: 123.",
        "The POST /checkout request returns HTTP 200 with an order id. The confirmation page shows the same grand total the cart displayed. The receipt email (if configured) is queued. The gateway records exactly one authorisation for the expected amount."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Negative",
        f"Attempt checkout with a declined test card and confirm the cart is preserved ({action})",
        "User is authenticated. Checkout page is open with a populated cart. Payment gateway is in test mode.",
        ["Enter the declined test card number, any valid expiry, and any CVC into the payment form",
         "Click Pay / Place Order",
         "Inspect the error banner rendered on the checkout page",
         "Click the cart icon in the Header and verify the line items and totals"],
        "Declined test card: 4000 0000 0000 0002. Expiry: 12/28. CVC: 123.",
        "The gateway response is surfaced as a red error banner stating the card was declined, without leaking raw gateway codes or stack traces. The cart contents and totals are unchanged, so the user can retry with a different card."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Edge Case",
        f"Interrupt the payment request and verify no duplicate charge occurs on retry ({action})",
        "User is authenticated. Checkout page is open. DevTools is open to throttle / block the payment request.",
        ["Enter valid test card details into the payment form",
         "Open DevTools \u2192 Network and set the payment endpoint to Offline",
         "Click Pay / Place Order and wait for the client timeout",
         "Re-enable the Network and click Pay / Place Order a second time",
         "Open the gateway dashboard and count authorisations for this order",
         "Compare the order status in the admin with the charge state in the gateway"],
        "Test card: 4242 4242 4242 4242.",
        "Exactly one authorisation exists in the gateway dashboard. The order status in the admin matches the charge state (both paid, or both unpaid). The user never sees a 500 page; the retry either succeeds or is rejected with an idempotency-replay message.",
        comment="Critical edge case \u2014 pair with a gateway-logs audit after each release."))
    idx[0] += 1

    return cases


def _generic_tests(sn: int, story: UserStory, idx: list[int]) -> list[TestCase]:
    prefix, section = _SECTION_PREFIXES["generic"]
    action = _short(story.action)
    story_text = _short(story.original_text, 140)
    cases = []

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Positive",
        f"Exercise the happy path for: {action}",
        f"System is reachable. If the feature needs authentication, the user is logged in. Relevant seed data exists per the story: {story_text}",
        ["Open the entry point referenced by the story (page, menu item or API endpoint)",
         f"Perform the action described by the story: {_short(story.action, 80)}",
         "Capture the network response code and body via DevTools \u2192 Network",
         "Observe the UI change that the story promises (new row, toast, navigation, updated counter)"],
        "",
        f"The action returns HTTP 2xx. The UI change promised by the story is visible without a page reload. The backing store reflects the change on a subsequent read. Story under test: {story_text}"))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Negative",
        f"Drive the feature with invalid or missing input and confirm the rejection is explicit ({action})",
        "System is reachable. Feature entry point is open.",
        ["Leave a required field empty (or omit a required parameter) and trigger the action",
         "Supply a malformed value (wrong type, out-of-range number, bad email format) and trigger the action",
         "Inspect the HTTP response code and the rendered error message for each attempt"],
        "Empty required field. Malformed value for a typed field.",
        "Each invalid attempt returns HTTP 400 / 422 (or is blocked client-side before the network request). The error message names the failing field and the rule that was violated. No partial write reaches the backing store."))
    idx[0] += 1

    cases.append(_make_tc(sn, idx[0], section, prefix, story, "Edge Case",
        f"Push boundary, Unicode, and concurrent inputs through the feature ({action})",
        "System is reachable. Feature entry point is open.",
        ["Submit the documented minimum value (0, 1 or an empty-but-allowed payload) and capture the response",
         "Submit the documented maximum value (max-length string, max-allowed number) and capture the response",
         "Submit a Unicode-heavy payload (Cyrillic, CJK, emoji) and reopen the record",
         "Open a second browser session and trigger the same action at the same time from both sessions"],
        "Min: 0 / A. Max: documented upper bound. Unicode: Тест Ω 你好 . Concurrency: same payload from two sessions.",
        "Min and max values persist exactly as entered. Unicode round-trips through the detail view with matching code points. Concurrent submissions either both succeed with distinct ids or the second is rejected with a conflict message; the store never ends up in a half-written state."))
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
