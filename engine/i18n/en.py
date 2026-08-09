"""TestFortge — English translations."""

TRANSLATIONS = {
    # Navigation
    "nav_dashboard": "Dashboard",
    "nav_setup": "Project Setup",
    "nav_requirements": "Requirements",
    "nav_user_stories": "User Stories",
    "nav_test_cases": "Test Cases",
    "nav_checklist": "Checklist",
    "nav_techniques": "Techniques",
    "nav_recommendations": "Recommendations",
    "nav_tools": "Tools",
    "nav_status_report": "Status Report",
    "nav_test_metrics": "Test Metrics",
    "nav_guide": "Guide",
    "nav_export": "Export",

    # Sidebar
    "sidebar_generate": "Generate:",
    "sidebar_advise": "Advise:",
    "sidebar_project": "Project:",

    # Dashboard
    "dashboard_title": "Dashboard",
    "dashboard_subtitle": "TestForTge — Framework for optimizing manual testing",
    "new_project": "New Project",
    "new_session": "New Session",
    "new_session_confirm": "Clear current session data and start fresh?",
    "guide": "Guide",
    "current_project": "Current Project",
    "saved_projects": "Saved Projects",
    "features_title": "TestForTge Features",
    "load": "Load",
    "delete": "Delete",
    "save_project": "Save Project",
    "dash_requirements": "Requirements",
    "dash_user_stories": "User Stories",
    "dash_acceptance_criteria": "Acceptance Criteria",
    "dash_feat_req_title": "Requirements → User Stories",
    "dash_feat_req_desc": "Automatically parse requirements into User Stories in \"As a... I want... So that...\" format",
    "dash_feat_tc_title": "Test Cases & Checklists",
    "dash_feat_tc_desc": "Generate positive, negative and edge case test cases or compact checklists in TestFort format",
    "dash_feat_plan_title": "Test Plan",
    "dash_feat_plan_desc": "13-section test plan following TestFort/IEEE-829 template with team roles, risks, and schedule",
    "dash_feat_report_title": "Status Report & Metrics",
    "dash_feat_report_desc": "Daily/weekly status reports and test metrics: coverage, execution summary, issues breakdown",
    "dash_feat_tech_title": "Test Design Techniques",
    "dash_feat_tech_desc": "Recommended test design techniques (EP, BVA, Decision Table) with examples",
    "dash_feat_tools_title": "Tools & No-Code",
    "dash_feat_tools_desc": "Testing tool recommendations and no-code automation with reasoning",

    # Setup
    "setup_title": "Project Setup",
    "setup_subtitle": "Configure project parameters for personalized recommendations",
    "project_name": "Project Name",
    "project_name_placeholder": "e.g. E-Commerce Platform",
    "domain": "Product Domain",
    "platform": "Platform",
    "tech_stack": "Technology Stack",
    "tech_stack_placeholder": "e.g. React, Node.js, PostgreSQL, Docker",
    "team_size": "Team Size",
    "budget": "Tools Budget",
    "release_frequency": "Release Frequency",
    "product_features": "Product Features",
    "has_api": "Has API",
    "has_auth": "Authentication / Registration",
    "has_payments": "Payments / Billing",
    "has_file_upload": "File Upload",
    "has_realtime": "Real-time (WebSocket, Chat)",
    "target_markets": "Target Markets",
    "modules_title": "Framework Modules",
    "modules_subtitle": "Select which TestForTge modules to use for this project:",
    "next": "Next",
    "back": "Back",

    # Domain options
    "domain_ecommerce": "E-Commerce",
    "domain_fintech": "FinTech / Banking",
    "domain_healthcare": "Healthcare / MedTech",
    "domain_saas": "SaaS Platform",
    "domain_edtech": "EdTech / E-Learning",
    "domain_social": "Social Media / Messaging",
    "domain_iot": "IoT / Smart Devices",
    "domain_other": "General / Other",

    # Platform options
    "platform_web": "Web Application",
    "platform_mobile": "Native Mobile App",
    "platform_hybrid": "Hybrid / PWA",
    "platform_desktop": "Desktop Application",
    "platform_api": "API / Backend Service",

    # Team size options
    "team_solo": "Solo (1 QA)",
    "team_small": "Small (2-5)",
    "team_medium": "Medium (5-15)",
    "team_large": "Large (15+)",

    # Budget options
    "budget_low": "Low (Open Source only)",
    "budget_medium": "Medium (Freemium OK)",
    "budget_high": "High (Commercial OK)",

    # Release frequency options
    "release_daily": "Daily / CD",
    "release_weekly": "Weekly",
    "release_biweekly": "Bi-weekly (Sprint)",
    "release_monthly": "Monthly",
    "release_quarterly": "Quarterly",

    # Requirements
    "req_title": "Requirements Input",
    "req_subtitle": "Upload requirements or enter them manually",
    "text_input": "Text Input",
    "file_upload": "File Upload",
    "file_upload_hint": "Drag files here or click to select",
    "supported_formats": "Supported formats: .txt, .md, .docx, .xlsx, .csv, .pdf, .png, .jpg, .jpeg, .mp4, .webm, .avi, .mov, .mkv, .flv, .wmv, .gif",
    "generate_btn": "Generate",
    "insert_example": "Insert Example",
    "custom_prompt": "Additional Instructions (optional)",
    "custom_prompt_hint": "e.g. 'focus on security and edge cases', 'cover only smoke tests', 'add negative scenarios for the login flow', 'check WCAG accessibility'. Plain English or Ukrainian. The generator detects testing types, narrowing keywords ('only', 'just', 'тільки'), and area focus.",
    "input_placeholder": "Paste requirements, user stories, test data, or any relevant information...",

    # Shared input block titles
    "input_title_default": "Input: Requirements, Project Documents, or Attachments",
    "input_title_stories": "Input: Requirements, User Stories, or Attachments",
    "input_title_scope": "Input: Requirements, Scope Documents, or Attachments",
    "input_title_additional": "Additional Data: Attachments & Instructions",

    # Common
    "step": "Step",
    "priority": "Priority",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "category": "Category",
    "positive": "Positive",
    "negative": "Negative",
    "edge_case": "Edge Case",
    "security": "Security",
    "status": "Status",
    "passed": "Passed",
    "failed": "Failed",
    "passed_but": "Passed but",
    "blocked": "Blocked",
    "unchecked": "Unchecked",
    "export_markdown": "Export Markdown",
    "export_html": "Export HTML",
    "export_csv": "Export CSV",
    "export_xlsx_tc": "Export XLSX (TC)",
    "export_xlsx_cl": "Export XLSX (CL)",
    "traceability_matrix": "Traceability Matrix",
    "all": "All",
    "filter": "Filter",
    "count": "Count",
    "requirement": "Requirement",
    "user_story": "User Story",
    "reasoning": "Reasoning",
    "subtypes": "Subtypes",
    "module": "Module",
    "why_critical": "Why Critical",
    "recommended_tools": "Recommended tools",
    "notes": "Notes",
    "required_badge": "Required",
    "recommended_badge": "Recommended",
    "existing_data_hint": "You can also generate from existing project data (User Stories, Test Cases) if they are already created.",
    "go": "Go",

    # User Stories
    "us_generated": "Generated",
    "us_from_requirements": "User Stories from",
    "us_requirements_word": "requirements",
    "us_parsed_requirements": "Parsed Requirements",
    "us_as_a": "As a",
    "us_i_want": "I want",
    "us_so_that": "so that",
    "us_acceptance_criteria": "Acceptance Criteria",

    # Test Cases
    "tc_title": "Test Cases",
    "tc_subtitle": "TestFort-format test cases grouped by sections",
    "tc_summary": "Summary",
    "tc_preconditions": "Preconditions",
    "tc_steps": "Test Steps",
    "tc_test_data": "Test Data",
    "tc_expected": "Expected Result",
    "tc_issues": "Issues",
    "tc_comment": "Comment",
    "tc_section": "Section",
    "tc_how_to_title": "How to generate Test Cases",
    "tc_how_to_desc": "Enter requirements or upload files above, then click Generate. You can also:",
    "tc_how_to_1": "Upload requirements documents (DOCX, TXT, PDF) with functional specs",
    "tc_how_to_2": "Add a custom prompt to focus on specific testing areas (security, performance)",
    "tc_how_to_3": "Or navigate to User Stories first and generate from them",
    "tc_how_to_4": "Upload existing test data to extend current test suite",

    # Checklist
    "cl_title": "Checklist",
    "cl_subtitle": "TestFort-format checklist with section prefixes",
    "cl_objective": "Objective",
    "cl_comments": "Comments / Issues",
    "cl_how_to_title": "How to generate a Checklist",
    "cl_how_to_desc": "Enter requirements or upload files above, then click Generate.",

    # Status Report
    "sr_title": "Status Report",
    "sr_subtitle": "Daily/weekly testing status report",
    "sr_date": "Report Date",
    "sr_what_done": "What has been done",
    "sr_what_planned": "What's planned",
    "sr_bugs": "Bugs",
    "sr_features_tested": "Features Tested",
    "sr_platforms_tested": "Platforms Tested",
    "sr_bugs_found": "Bug References",
    "sr_next_steps": "Next Steps",
    "sr_report_details": "Report Details",
    "sr_features_placeholder": "e.g. Login, Registration, Dashboard",
    "sr_platforms_placeholder": "e.g. Chrome, Firefox, iOS Safari",
    "sr_bugs_placeholder": "e.g. BUG-101, BUG-102, BUG-103",
    "sr_stats_summary": "Statistics Summary",
    "sr_project_stats": "Project Stats",

    # Test Metrics
    "tm_title": "Test Metrics",
    "tm_subtitle": "Coverage, execution summary, and issues breakdown",
    "tm_input_data": "Metrics Input Data",
    "tm_devices_label": "Devices / Browsers (comma-separated)",
    "tm_devices_placeholder": "e.g. Chrome Windows, Safari macOS, iPhone 14 Pro",
    "tm_tc_execution": "Test Case Execution Data",
    "tm_bugs_data": "Bugs / Defects Data",
    "tm_total_tc": "Total Test Cases",
    "tm_total_bugs": "Total Bugs",
    "tm_bugs_critical": "Critical / Blocker",
    "tm_bugs_major": "Major",
    "tm_bugs_minor": "Minor",
    "tm_bugs_low": "Low",
    "tm_kpi_title": "Key Performance Indicators (KPIs)",
    "tm_coverage": "Coverage Completion Table",
    "tm_execution": "Test Execution Summary by Platforms",
    "tm_issues": "Issues Summary",
    "tm_device": "Device",
    "tm_overall": "Overall # checks",
    "tm_remaining": "Remaining",
    "tm_percentage": "Percentage",
    "tm_pass": "Pass",
    "tm_fail": "Fail",
    "tm_pass_but": "Pass but",
    "tm_by_platform": "Issues by Platform",
    "tm_by_type": "Defect Type",
    "tm_by_status": "Status (for dev team)",
    "tm_by_severity": "Defect Severity",
    "tm_how_to_title": "How to generate Test Metrics",
    "tm_how_to_desc": "Enter your test execution data above (test case counts, bug counts), then click Generate. You can also:",
    "tm_how_to_1": "Upload a test report file (CSV, XLSX, TXT) with execution data",
    "tm_how_to_2": "Leave fields empty to auto-populate from generated Test Cases in this project",
    "tm_how_to_3": "Add devices/browsers to see per-platform coverage breakdown",
    "tm_history_title": "Metrics over time",
    "tm_history_subtitle": "Pass rate (%) and defect density (×100) per snapshot. Activate a project and run something to start the timeline.",
    "tm_history_empty": "No snapshots yet — they’re written automatically when you load the dashboard or finish an automation run.",

    # Techniques
    "tech_title": "Test Design Techniques",
    "tech_subtitle": "Recommended test design techniques for your project",
    "tech_found": "Techniques Found",
    "when_to_use": "When to use",
    "example": "Example",
    "why_for_project": "Why for your project",
    "tech_how_to_title": "How to get Technique Recommendations",
    "tech_how_to_desc": "Upload project requirements or enter a description above, then click Generate. Techniques are recommended dynamically based on your project's actual content:",
    "tech_how_to_1": "Authentication features → Security testing techniques",
    "tech_how_to_2": "Forms and inputs → Boundary Value Analysis, Equivalence Partitioning",
    "tech_how_to_3": "APIs → API testing, State Transition Testing",
    "tech_how_to_4": "Payment flows → Decision Table Testing, Error Guessing",

    # Recommendations
    "rec_title": "Testing Recommendations",
    "rec_subtitle": "Testing types, focus areas, and standards",
    "rec_testing_types": "Testing Types",
    "rec_focus_areas": "Focus Areas & Edge Cases",
    "rec_standards": "Standards",
    "rec_critical_modules": "Critical Modules",
    "rec_edge_cases": "Edge Cases & Negative Scenarios",
    "rec_platform_checks": "Platform: What to Test",
    "rec_domain_standards": "Domain Standards",
    "rec_how_to_title": "How to get Recommendations",
    "rec_how_to_desc": "Upload project requirements or enter a description above, then click Generate. Recommendations are personalized based on your project content:",
    "rec_how_to_1": "Testing Types — Functional, Security, Performance, Accessibility, etc. based on detected features",
    "rec_how_to_2": "Focus Areas — Critical modules, edge cases, platform-specific checks",
    "rec_how_to_3": "Standards — Relevant ISO, OWASP, WCAG, GDPR based on your domain and features",

    # Tools
    "tools_title": "Tools Recommendations",
    "tools_subtitle": "Recommended testing tools with reasoning",
    "tools_testing": "Testing Tools",
    "tools_nocode": "No-Code Automation",
    "tools_best_for": "Best for",
    "tools_nocode_empty": "No-code automation is not recommended for the current project configuration, or the module is disabled in settings.",
    "tools_how_to_title": "How to get Tool Recommendations",
    "tools_how_to_desc": "Upload project requirements or enter a description above, then click Generate. Tool recommendations are based on detected features:",
    "tools_how_to_1": "API features → Postman, Swagger, REST Assured",
    "tools_how_to_2": "Security features → OWASP ZAP, Burp Suite",
    "tools_how_to_3": "Performance concerns → JMeter, k6, Lighthouse",
    "tools_how_to_4": "Mobile platform → Appium, BrowserStack",

    # Input Stories
    "is_title": "User Stories — Direct Input",
    "is_subtitle": "Enter existing User Stories to generate Test Cases, Checklists, or other artifacts",
    "is_placeholder": "Enter your User Stories here, one per line...\n\nExamples:\nUser can register with email and password\nUser can login with 2FA\nUser can search products by category",
    "is_hint": "One story per line. Each line will be converted into a User Story.",
    "is_generate_target": "Generate Target",

    # Target markets
    "market_eu": "EU (GDPR, WCAG)",
    "market_us": "US (CCPA, ADA)",
    "market_ua": "Ukraine",
    "market_global": "Global",

    # Module names for setup
    "mod_user_stories": "Requirements → User Stories",
    "mod_user_stories_desc": "Auto-parse requirements into User Stories",
    "mod_test_cases": "User Stories → Test Cases",
    "mod_test_cases_desc": "Generate detailed test cases with steps (TestFort format)",
    "mod_checklists": "User Stories → Checklists",
    "mod_checklists_desc": "Compact checklists for quick testing",
    "mod_status_report": "Status Report",
    "mod_status_report_desc": "Daily/weekly testing status reports",
    "mod_test_metrics": "Test Metrics",
    "mod_test_metrics_desc": "Coverage, execution summary, issues breakdown",
    "mod_techniques": "Test Design Techniques",
    "mod_techniques_desc": "Recommended test design techniques (EP, BVA, Decision Table)",
    "mod_testing_types": "Testing Types & Focus Areas",
    "mod_testing_types_desc": "Testing types and focus areas based on product",
    "mod_tools": "Testing Tools",
    "mod_tools_desc": "Testing tool recommendations",
    "mod_nocode": "No-Code Automation",
    "mod_nocode_desc": "No-code / low-code automation tools",
    "mod_focus_areas": "Focus Areas & Standards",
    "mod_focus_areas_desc": "Critical modules, edge cases, standards & certifications",

    # Requirements page
    "req_placeholder": "Paste requirements here...\n\nSupported formats:\n• Numbered list: 1. The system must...\n• Bullet list: - Users shall be able to...\n• REQ-ID: REQ-001: The application must...",
    "req_example_title": "Example Requirements",
    "req_example_text": "1. The system must allow users to register using email and password.\n2. Users must be able to log in using their credentials.\n3. The application must display a product catalog with filtering.\n4. Users must be able to add items to cart.\n5. Checkout must support credit card payments via Stripe.\n6. Order status must be trackable in real time.\n7. Admin panel must allow managing products.\n8. The system must send email notifications.\n9. Search must support filters by category, price, and rating.\n10. User profile must allow editing personal info.",

    # Status report extra
    "sr_next_steps_placeholder": "One step per line",

    # Delete confirm
    # E8.5. The old text was "Delete project?", which asks about the
    # project and not about the data — and the data is what goes: every
    # bug's wording, every screenshot, permanently. A confirmation that
    # understates what it destroys is a confirmation nobody reads twice.
    "delete_confirm": ("Delete this project and everything in it — test "
                       "cases, checklist, bugs, runs and every uploaded "
                       "file? This cannot be undone. Export first if you "
                       "want a copy."),
    "export_project": "Export",
    "backup_project": "Back up",
    "backup_project_title": ("Write a bundle of this project to the "
                             "configured storage"),
    "export_project_title": "Download everything this project holds, as a zip",

    # User stories page
    "us_id": "ID",
    "us_requirement_text": "Requirement Text",
    "us_stories_label": "User Stories",
    "us_original": "Original",

    # Guide
    "guide_title": "Step-by-Step Guide",
    "guide_subtitle": "How to use TestForTge — framework for manual testers",
    "guide_intro": "TestForTge is a framework that helps minimize routine in manual testing: from requirements to ready test documentation. Each module can be enabled/disabled depending on project needs. All features work independently — you can use only what you need.",
    "guide_step1_title": "Project Setup",
    "guide_step1_what": "Configure project parameters for personalized recommendations.",
    "guide_step1_1": "Enter project name",
    "guide_step1_2": "Select domain (E-Commerce, FinTech, Healthcare, SaaS, etc.) — affects testing type recommendations, edge cases, and standards",
    "guide_step1_3": "Select platform (Web, Mobile Native, Hybrid, Desktop, API) — determines platform-specific checklists",
    "guide_step1_4": "Specify technology stack (React, Node.js, PostgreSQL)",
    "guide_step1_5": "Configure features: API, authentication, payments, real-time",
    "guide_step1_6": "Select target markets (EU, US) — affects standards (GDPR, WCAG)",
    "guide_step1_7": "Enable/disable framework modules",
    "guide_step2_title": "Requirements → User Stories",
    "guide_step2_what": "Upload requirements and get structured User Stories in \"As a... I want... So that...\" format.",
    "guide_step2_1": "Text input — supports numbered lists, bullet lists, REQ-ID format",
    "guide_step2_2": "File upload (up to 100MB): .txt, .md, .docx, .xlsx, .csv, .pdf, .png, .jpg",
    "guide_step2_3": "Custom prompt — add specific instructions for generation",
    "guide_step2_result": "Each requirement becomes a User Story with role, priority, story points, and acceptance criteria.",
    "guide_step3_title": "Test Cases & Checklists",
    "guide_step3_what": "Generate test cases or checklists from User Stories in TestFort format.",
    "guide_step3_1": "Test Cases — SC1_001 IDs, sections, Summary/Preconditions/Steps/Test Data/Expected Result/Issues/Comment, status tracking (Passed/Failed/Passed but/Blocked/Unchecked)",
    "guide_step3_2": "Checklists — PREFIX_001 IDs (HDR_, AUTH_, CRT_, etc.), section headers, Objective/Comments columns",
    "guide_step3_3": "Traceability Matrix — Requirement → User Story → Test Cases",
    "guide_step3_4": "Standalone mode — enter User Stories directly via Direct Input",
    "guide_step4_title": "Test Plan",
    "guide_step4_what": "13-section test plan following TestFort/IEEE-829 template.",
    "guide_step4_desc": "Includes: Introduction, Test Strategy, Test Schedule, Resources, Risks, Entry/Exit Criteria, Deliverables, and more. Works independently — generates based on project setup even without user stories.",
    "guide_step5_title": "Status Report & Test Metrics",
    "guide_step5_sr": "Daily/weekly report with what was done, what's planned, and bug references.",
    "guide_step5_tm": "Coverage completion table, test execution summary by platforms, issues breakdown (by platform, type, status, severity).",
    "guide_step6_title": "Techniques, Recommendations & Tools",
    "guide_step6_1": "Test Design Techniques — EP, BVA, Decision Table, State Transition, Pairwise, etc.",
    "guide_step6_2": "Testing Recommendations — testing types, focus areas, edge cases, standards (ISTQB, OWASP, WCAG)",
    "guide_step6_3": "Tools — testing tools and no-code automation with reasoning, based on budget and team size",
    "guide_step7_title": "Export & Storage",
    "guide_step7_1": "Markdown — full documentation with all sections",
    "guide_step7_2": "HTML — standalone file for browser viewing or printing",
    "guide_step7_3": "CSV (Test Cases) — for import into TestRail, Zephyr, qase.io",
    "guide_step7_4": "CSV (Checklist) — for Google Sheets or Excel",
    "guide_step7_storage": "Projects are saved locally in the /storage folder. TestForTge works fully offline — no data is sent to external servers.",
    "guide_faq": "FAQ",
    "guide_faq1_q": "Can I use only some modules?",
    "guide_faq1_a": "Yes! In Project Setup, select only the modules you need. Each feature also works independently — you can go directly to any page.",
    "guide_faq2_q": "What file formats are supported?",
    "guide_faq2_a": ".txt, .md, .docx, .xlsx, .csv, .pdf, .png, .jpg, .jpeg. File size limit: 100MB.",
    "guide_faq3_q": "Is project storage secure?",
    "guide_faq3_a": "Yes. Everything is stored locally on your computer. For NDA compliance, we recommend using an encrypted disk (BitLocker, VeraCrypt).",
    "guide_faq4_q": "How to import test cases into TestRail/Zephyr?",
    "guide_faq4_a": "Export to CSV format. TestRail: Settings → Import → CSV. Zephyr Scale: Import Test Cases → CSV.",
    "guide_what": "What:",
    "guide_result": "Result:",
    "guide_status_report": "Status Report:",
    "guide_test_metrics": "Test Metrics:",

    # MVP
    "mvp_no_input": "Please enter requirements or upload files.",
    "mvp_no_quality_requirements": "Could not detect any testable requirements in the provided input. Please add clearer requirements describing features or functionality (e.g. 'User can log in', 'The system displays a search form').",
    "mvp_feat_input_title": "Text & File Input",
    "mvp_feat_input_desc": "Enter requirements as text or upload files: documents, spreadsheets, PDFs, images, and video",
    "mvp_feat_export_title": "Export",
    "mvp_feat_export_desc": "Export to Markdown, HTML, CSV, or XLSX for import into TestRail, Zephyr, qase.io",

    # Language
    "language": "Language",
    "lang_en": "English",
    "lang_ua": "Ukrainian",

    # Test Execution
    "nav_test_execution": "Test Execution",
    "nav_bug_reports": "Bug Reports",
    "sidebar_execute": "Execute:",
    "te_title": "Test Execution",
    "te_subtitle": "Run test cases and checklists with status tracking",
    "te_select_source": "Select Test Source",
    "te_source_tc": "Test Cases",
    "te_source_cl": "Checklist",
    "te_no_data": "No test cases or checklists generated yet. Generate them first.",
    "te_environment": "Test Environment",
    "te_platform": "Platform",
    "te_browser": "Browser",
    "te_device": "Device / Mobile Web",
    "te_screen_size": "Screen Size",
    "te_custom_option": "Custom...",
    "te_testing_types": "Testing Types",
    "te_select_types": "Select one or more testing types",
    "te_tester": "Assigned Tester",
    "te_start": "Start Test Execution",
    "te_status": "Status",
    "te_comment": "Comment",
    "te_passed": "Passed",
    "te_failed": "Failed",
    "te_blocked": "Blocked",
    "te_save_results": "Save Results",
    "te_report_bug": "Report Bug",
    "te_results_saved": "Test execution results saved successfully",
    "te_total": "Total",
    "te_pass_rate": "Pass Rate",

    # Bug Reports
    "bug_title": "Bug Reports",
    "bug_subtitle": "Jira-style ISTQB-aligned defect reports",
    "bug_create": "Create Bug Report",
    "bug_id": "Bug ID",
    "bug_summary": "Summary",
    "bug_severity": "Severity",
    "bug_priority": "Priority",
    "bug_status": "Status",
    "bug_environment": "Environment",
    "bug_preconditions": "Preconditions",
    "bug_steps": "Steps to Reproduce",
    "bug_actual": "Actual Result",
    "bug_expected": "Expected Result",
    "bug_linked_item": "Linked Item",
    "bug_reporter": "Reporter",
    "bug_component": "Component",
    "bug_created": "Created",
    "bug_no_bugs": "No bug reports created yet.",
    "bug_save": "Save Bug Report",
    "bug_saved": "Bug report created successfully",
    "bug_export": "Export Bug Reports",
    # Run filter (scope the listing to one Test Execution run)
    "bug_run_filter_label": "Run:",
    "bug_run_all": "All runs",
    "bug_run_latest": "Latest run",
    "bug_run_apply": "Apply",
    "bug_run_clear": "Clear run filter",
    "bug_frequency": "Frequency",
    "bug_affects_version": "Affects Version",
    "bug_found_in_build": "Found in Build",
    "bug_assignee": "Assignee",
    "bug_labels": "Labels",
    "bug_comment": "Comment",

    # PR-A: walkthrough-bug "no attachments" banner (EN)
    "bug_no_attachments_title": "No attachments captured",
    "bug_no_attachments_body": (
        "No screenshot or video was linked to this bug. Possible reasons: "
        "(1) the run was started without a Base URL, so Playwright was "
        "skipped; (2) the page screenshot was captured but did not reach "
        "the finding (known wiring gap); (3) the run terminated before "
        "evidence could be saved. Re-run with a Base URL set to maximise "
        "the chance of attached evidence."
    ),
    "bug_no_attachments_link": "Open Test Execution",

    # Automation QA (Senior Automation QA Engineer)
    "nav_automation": "Automation QA",
    "auto_title": "Automation QA",
    "auto_subtitle": "Run automated test cases and capture per-step evidence.",
    "auto_config": "Configuration",
    "auto_base_url": "Base URL",
    "auto_scope": "Scope",
    "auto_scope_all": "All Test Cases",
    "auto_scope_section": "By Section",
    "auto_headless": "Headless",
    "auto_headless_yes": "Yes (no window)",
    "auto_headless_no": "No (show browser)",
    "auto_record_video": "Record video",
    "yes": "Yes",
    "no": "No",
    "creds_title": "Test Account",
    "creds_help": "Optional. Provide a login/password if test cases need to run behind authentication, or let TestForTge generate a throw-away test account.",
    "creds_help_te": "Optional. Supply credentials when the checklist or test cases require a logged-in user, or generate a throw-away test account on the fly.",
    "creds_mode_none": "No authentication needed",
    "creds_mode_provided": "Use existing account",
    "creds_mode_generated": "Generate test account",
    "creds_login_url": "Login page URL",
    "creds_username": "Username / email",
    "creds_password": "Password",
    "creds_register_url": "Registration URL (for generated accounts)",
    "creds_display_name": "Display name",
    "creds_user_selector": "Username field selector",
    "creds_pass_selector": "Password field selector",
    "creds_submit_selector": "Submit button selector",
    "creds_advanced": "Advanced: registration & custom selectors",
    "creds_generated_label": "Generated test account:",
    "creds_kept": "(kept from previous run)",
    "creds_generate_now": "Generate test account now",
    "chat_title": "Tedgie",
    "chat_subtitle": "TestForTge QA Assistant",
    "chat_greeting": "Hi! I'm Tedgie — the TestForTge QA Assistant. Ask me about Estimation, Test Cases, Checklist, Test Execution, Automation QA, Bug Reports — or type \"report a bug\" and I'll open a bug-report form for you.",
    "chat_placeholder": "Type a question or paste a requirement…",
    "chat_send": "Send",
    "chat_open": "Open QA assistant",
    "chat_close": "Close",
    "chat_reset": "Reset conversation",
    "back_to_top": "Back to top",
    "auto_tc_count": "Test Cases available",
    "auto_run_btn": "Generate & Run Automation",
    "auto_summary": "Automation Run Summary",
    "auto_total": "Total",
    "auto_pass_rate": "Pass Rate",
    "auto_duration": "Duration",
    "auto_coverage": "Automation Coverage",
    "auto_automated_vs_manual": "Automated / Manual",
    "auto_show_steps": "Show steps with screenshots",
    "auto_before": "Before",
    "auto_after": "After",
    "auto_video": "Watch video",
    "auto_no_tc": "No Test Cases yet. Generate them first on the Test Cases page.",

    # Estimation (QA Team Lead)
    "nav_estimation": "Estimation",
    "est_title": "QA Effort Estimation",
    "est_subtitle": "Authored by the QA Team Lead — Manual QA/Testing estimation for potential projects",
    "est_persona_label": "QA Team Lead",
    "est_persona_desc": "ISTQB Advanced certified, 10+ years across FinTech, E-commerce, Healthcare, SaaS, EdTech, Telecom, Government. Produces estimations using the proven template.",
    "est_config": "Configuration",
    "est_project_name": "Project name",
    "est_project_name_ph": "e.g., AI meeting notes Manual Testing",
    "est_rate": "Rate, USD/hour",
    "est_additional_platforms": "Additional compatibility platforms (N)",
    "est_minutes_per_tc": "Minutes per test case",
    "est_buffer": "Buffer, %",
    "est_primary_platform": "Primary platform",
    "est_coefficients": "Coefficients (editable)",
    "est_coefficients_hint": "Defaults match the QA Team Lead reference template. Adjust to fit your project risk profile.",
    "est_compat_rate": "Compatibility rate, %",
    "est_compat_rate_hint": "Per extra platform (default 0.3%).",
    "est_bug_rate": "Bug reporting rate, %",
    "est_bug_rate_hint": "Share of Functional + Regression (default 15%).",
    "est_pm_overhead": "PM overhead, %",
    "est_pm_overhead_hint": "Project management share (default 8%).",
    "est_max_stretch": "MAX stretch (× MIN)",
    "est_max_stretch_hint": "How much MAX exceeds MIN (default 1.5×).",
    "est_source": "Source of features",
    "est_source_url": "Crawl from URL",
    "est_source_file": "Upload attachment",
    "est_source_text": "…or paste feature list",
    "est_source_text_ph": "- Feature one\n- Feature two\n- Feature three",
    "est_run": "Run Estimation",
    "est_summary": "Estimation Summary",
    "est_total_tc": "Total test cases",
    "est_features_hours": "Checklist hours",
    "est_one_plat_total": "Total (One Platform)",
    "est_full_total": "Total (Full Compatibility)",
    "est_cost_one": "Cost without compatibility",
    "est_cost_full": "Cost with compatibility",
    "est_source_label": "Source",
    "est_export": "Download XLSX",
    "est_tasks_title": "Estimation Breakdown (hours)",
    "est_task": "Task",
    "est_min": "MIN",
    "est_max": "MAX",
    "est_expected": "Expected",
    "est_pm": "Project management",
    "est_one_plat_subtotal": "Testing/QA (One Platform)",
    "est_full_subtotal": "Testing/QA (Full Compatibility)",
    "est_cost_title": "Total Cost",
    "est_cost_scope": "Scope",
    "est_features_title": "Features / Test Case Coverage",
    "est_feature": "Module / Page",
    "est_tc": "Test cases",
    "est_comment": "Comment",
    "est_total": "Total",
    "est_total_hours": "Total (hours)",

    # ── 2026-05-02 fixes: 502, upload-from-execution, modal a11y ──
    'tc_gen_retry': 'Retry generation',
    'cl_gen_retry': 'Retry generation',
    'mvp_gen_in_background': 'Generation is still running in the background. Please wait a few seconds and refresh this page.',
    'te_empty_title': 'Nothing to run yet',
    'te_empty_hint': 'Generate a test pack from requirements, or upload one you already have. The uploaded pack lands in the same session so you can run it on this page right away.',
    'te_empty_generate_tc': 'Generate test cases',
    'te_empty_generate_cl': 'Generate checklist',
    'te_upload_tc_title': 'Upload existing test cases',
    'te_upload_cl_title': 'Upload existing checklist',
    'te_upload_tc_hint': 'Drop an XLSX / CSV / MD / JSON pack — TestForTge maps the columns to its fields and stores the cases in the session for execution on this page.',
    'te_upload_cl_hint': 'Drop an XLSX / CSV / MD / JSON checklist; items go straight into the session for execution.',
    'te_upload_file': 'File',
    'te_upload_replace': 'Replace current pack',
    'te_upload_append': 'Append to current pack',
    'te_upload_btn': 'Upload & start running',
    'te_upload_more': 'Upload another pack (replace or append)',
    'te_live_pause': 'Pause',
    'te_live_resume': 'Resume',
    'te_live_open_tab': 'Open frame in new tab',
    'te_watch_live': 'Watch live (opens in new tab)',
    # ── Password reset and address confirmation (E1.7) ─────────────
    #
    # Every string on the "forgot" page is deliberately independent of
    # whether the address has an account. Wording that varies with what was
    # found is the enumeration leak engine/auth.py closes on the sign-in
    # page — closing it there and reopening it in a translation would be a
    # strange way to lose it.
    'reset_title': 'Reset your password',
    'reset_intro': "Enter the address you sign in with and we will send you "
                   "a link to choose a new password.",
    'reset_email_label': 'Email',
    'reset_submit': 'Send me a link',
    'reset_sent': "If that address has an account, a reset link is on its "
                  "way. It works once and expires in an hour.",
    'reset_no_provider': "This instance cannot send email yet, so the link "
                         "will not arrive — ask whoever runs the server to "
                         "reset your password for you.",
    'reset_back': 'Back to sign in',
    'reset_new_title': 'Choose a new password',
    'reset_new_intro': 'Pick something you have not used here before.',
    'reset_new_label': 'New password',
    'reset_confirm_label': 'New password again',
    'reset_length_hint': 'At least %d characters. A short phrase of a few '
                         'words works well and is easier to remember than a '
                         'scrambled word.',
    'reset_new_submit': 'Save the new password',
    'reset_will_sign_out': "Saving this signs you out everywhere else and "
                           "cancels any other reset link.",
    'reset_dead_title': 'That link no longer works',
    'reset_dead_body': "Reset links work once and expire after an hour. "
                       "Asking for a new one also cancels the old one.",
    'reset_ask_again': 'Ask for a new link',
    'reset_forgot_link': 'Forgot your password?',

    'verify_title': 'Confirm your address',
    'verify_done_title': 'Address confirmed',
    'verify_done_body': '%s is confirmed. Thank you.',
    'verify_continue': 'Continue to TestForTge',
    'verify_dead_title': 'That link no longer works',
    'verify_dead_body': "Confirmation links work once and expire after a "
                        "day. You can ask for a new one from any page while "
                        "signed in.",
    'verify_banner': "Your email address is not confirmed yet.",
    'verify_banner_why': "It was not us who delivered your invitation, so "
                         "nothing has proved this address belongs to you.",
    'verify_banner_send': 'Send me a confirmation link',
    # ── Attaching evidence to a bug by hand (E4.5a) ────────────────
    'bug_attach_label': 'Attach evidence',
    'bug_attach_submit': 'Attach',
    'bug_attach_hint': 'Screenshot, video, PDF or log.',
    'bug_attach_ok': 'Attached to the bug report.',
    'bug_attach_none': 'Choose a file to attach.',
    'bug_attach_no_project': 'Pick or create a project first.',
    'bug_attach_missing': 'That bug is not in this project.',
    'bug_attach_failed': 'That file could not be saved. Nothing was attached.',

    # ── Pages that predate the dictionary (M-2) ────────────────────
    # Sign-in, invitation, 403 and the team page were written after the
    # i18n pass and never went through it, so a Ukrainian user met English
    # at the two moments they have no way around: signing in and being
    # refused. The English wording here is byte-identical to what these
    # templates rendered before — the strings moved, they were not
    # rewritten.
    'login_title': 'Sign in',
    'login_sub': 'TestForTge — QA documentation, execution and metrics '
                 'for your team.',
    'login_email': 'Email',
    'login_password': 'Password',
    'login_submit': 'Sign in',
    'login_or': 'or',
    'login_google': 'Continue with Google',
    'login_invite_only': 'TestForTge is invite-only. Ask an admin on your '
                         'team to send you an invitation.',

    'invite_page_title': 'Accept invitation',
    'invite_expired_title': 'This invitation has expired',
    'invite_expired_body': 'Invitation links are valid for seven days, and '
                           'are cancelled when a newer one is sent to the '
                           'same address.',
    'invite_expired_ask': 'Ask an admin on your team to send a new '
                          'invitation.',
    'invite_have_account': 'Already have an account? Sign in.',
    'invite_join_title': 'Join %s',
    'invite_role_intro': 'You were invited as',
    'invite_role_with': 'with the',
    'invite_role_suffix': 'role.',
    'invite_name_label': 'Your name',
    'invite_name_placeholder': 'How your name appears on test cases and bugs',
    'invite_password_label': 'Choose a password',
    'invite_password_hint': 'At least %d characters. A short phrase of a few '
                            'words is stronger than a scrambled word, and '
                            'easier to remember — there are no symbol or '
                            'digit requirements.',
    'invite_confirm_label': 'Confirm password',
    'invite_submit': 'Create account and join',
    'invite_google': 'Join with Google instead',
    'invite_google_hint': 'Use the Google account for %s — the invitation is '
                          'tied to that address.',

    'forbidden_title': 'You do not have access to this',
    'forbidden_admin_only': 'Creating projects and changing settings is '
                            'limited to admins on your team.',
    'forbidden_page_title': 'Not allowed',
    # Split around the role name so the <strong> stays in the
    # template: a dictionary that carries markup has to be piped
    # through |safe, and |safe over a formatted string is how an
    # injection gets in.
    'forbidden_needs_role_pre': 'This action needs the',
    'forbidden_needs_role_post': 'role.',
    'forbidden_ask': 'Ask an admin on your team to change your role, or to '
                     'do this for you.',
    'forbidden_back': 'Back to the dashboard',

    'om_title': 'Team',
    'om_no_team_title': 'You are not on a team yet',
    'om_no_team_body': 'Ask whoever set up TestForTge for your organisation '
                       'to invite you, or create an organisation from '
                       'Settings.',
    # Plural forms, ``|`` separated — see engine/i18n/plural.py.
    'om_member_word': 'member|members',
    'om_invite_word': 'pending invitation|pending invitations',
    'om_invite_heading': 'Invite someone',
    'om_invite_email_label': 'Email address',
    'om_invite_email_placeholder': 'colleague@company.com',
    'om_invite_role_label': 'Role',
    'om_role_user': 'User — works with everything',
    'om_role_admin': 'Admin — also creates projects and changes settings',
    'om_invite_submit': 'Create invitation',
    'om_invite_note': 'You will get a link to send them yourself — '
                      'TestForTge does not send email on this plan. Links '
                      'are valid for 7 days, and inviting the same address '
                      'again cancels the earlier link.',
    'om_members_heading': 'Members',
    'om_col_name': 'Name',
    'om_col_email': 'Email',
    'om_col_role': 'Role',
    'om_col_actions': 'Actions',
    'om_you': 'you',
    'om_deactivated': 'deactivated',
    'om_role_for': 'Role for %s',
    'om_save': 'Save',
    'om_remove': 'Remove',
    'om_only_admin': 'You are the only admin. Promote someone else before '
                     'changing your own role or removing yourself — '
                     'otherwise nobody could create projects or change '
                     'settings.',
    'om_pending_heading': 'Pending invitations',
    'om_col_expires': 'Expires',
    'om_cancel': 'Cancel',

    # ── Settings (M-2) ─────────────────────────────────────────────
    # Whole sentences rather than fragments wherever the English builds
    # one out of inline conditionals: "project/projects, belongs/belong,
    # It was/They were" is a shape only English has, and a translator
    # handed those five fragments cannot produce a Ukrainian sentence
    # from them. The plural forms are ``|`` separated (engine/i18n/plural).
    'os_page_title': 'Settings',
    'os_no_team_title': 'No team selected',
    'os_no_team_body': 'Settings belong to a team. Ask an admin to invite '
                       'you to one.',
    'os_heading': 'Settings',
    'os_config_for': 'Configuration for %s.',
    'os_config_for_member': 'Configuration for %s. Only admins can change '
                            'these — you are seeing them so you know how the '
                            'team is set up.',
    'os_team_heading': 'Team',
    'os_team_name_label': 'Name',
    'os_save': 'Save',

    'os_orphans_heading': 'Projects with no team',
    'os_orphans_body': '%d project on this server belongs to no team. It was '
                       'created before teams existed, so it does not appear '
                       'in any project list — including for whoever created '
                       'it.|%d projects on this server belong to no team. '
                       'They were created before teams existed, so they do '
                       'not appear in any project list — including for '
                       'whoever created them.',
    'os_orphans_more': '…and %d more.',
    'os_orphans_ambiguous': 'There are %d teams on this server, so claiming '
                            'in bulk is refused: nothing here records which '
                            'team these projects belonged to, and guessing '
                            'would hand one team\'s work to another. Whoever '
                            'runs the server has to assign them individually.',
    'os_orphans_claim': 'Claim %d project for %s|Claim %d projects for %s',
    'os_orphans_after': 'It becomes visible to every member of %s, and the '
                        'change is recorded in the audit trail. There is no '
                        'undo button — an admin would have to move it back '
                        'individually.|They become visible to every member of '
                        '%s, and the change is recorded in the audit trail. '
                        'There is no undo button — an admin would have to '
                        'move them back individually.',

    'os_ai_heading': 'AI usage and cost',
    'os_ai_byok': 'This team uses its own Anthropic API key',
    'os_ai_byok_hint': '(ending %s)',
    'os_ai_byok_tail': '. Generation bills to your own account, and the '
                       'platform allowance below does not apply.',
    'os_ai_platform_key': "This team uses the platform's shared API key, "
                          'capped by the monthly allowance below.',
    'os_ai_no_key': 'No API key is configured anywhere, so AI generation is '
                    'unavailable. Test cases, checklists and estimation still '
                    'work from the built-in rule engines and the ISTQB '
                    'knowledge base — they are just not LLM-assisted.',
    'os_ai_meter': '%s of %s used this month',
    'os_ai_over_pill': 'allowance reached',
    'os_ai_over_note': 'AI generation is falling back to the built-in rule '
                       'engines until the allowance resets or an admin raises '
                       'it. Nothing is failing — the output is just not '
                       'LLM-assisted.',
    'os_ai_no_cap': 'No monthly allowance is set, so generation is not '
                    'capped.',
    'os_ai_budget_label': 'Monthly allowance (USD)',
    'os_ai_budget_note': "0 removes the cap. Applies only to spend on the "
                         "platform's shared key — a team using its own key is "
                         "never capped.",
    'os_ai_this_month': 'This month',
    'os_col_feature': 'Feature',
    'os_col_calls': 'Calls',
    'os_col_input': 'Input',
    'os_col_output': 'Output',
    'os_col_cost': 'Cost',
    'os_col_total': 'Total',
    'os_ai_no_calls': 'No AI calls recorded this month.',

    'os_byok_heading': 'Your own Anthropic API key',
    'os_byok_intro': "Supplying a key means your team's AI usage bills to "
                     'your own Anthropic account instead of the platform\'s, '
                     'and stops counting against the allowance above. It is '
                     'encrypted before it is stored and is never shown again '
                     'afterwards.',
    'os_byok_unavailable': 'This instance cannot store API keys — the server '
                           'has no encryption key configured '
                           '(<code>TESTFORTGE_ENCRYPTION_KEY</code>). Rather '
                           'than keep your key in plain text, saving is '
                           'refused.',
    'os_byok_replace': 'Replace key',
    'os_byok_label': 'API key',
    'os_byok_save': 'Save key',
    'os_byok_where': 'From console.anthropic.com → API keys. There is no way '
                     'to reveal a stored key — to change it, paste a new one.',
    'os_byok_remove': 'Remove key',
    'os_byok_removed_note': 'Generation goes back to the platform key and its '
                            'allowance.',

    'os_models_heading': 'Models',
    'os_col_model': 'Model',
    'os_models_note': 'Set on the server, not per team. Cheaper models handle '
                      'work whose output can be checked automatically.',

    'os_capacity_heading': 'Capacity',
    'os_capacity_meter': '%s of %s used on this server',
    'os_capacity_pill': 'getting full',
    'os_capacity_unreadable': 'The database size could not be read just now.',
    'os_capacity_team_heading': 'What this team is holding',
    'os_col_artefact': 'Artefact',
    'os_col_rows': 'Rows',
    'os_capacity_estimate': 'That is about %s — an estimate from row counts, '
                            'since a shared table cannot report one team\'s '
                            'share exactly.',
    'os_capacity_over_quota': 'This team is past its %s-row guideline.',
    'os_capacity_over_warn': 'That is over %d%% of the %s-row guideline.',
    'os_capacity_biggest_pre': 'The largest part of that is',
    'os_capacity_biggest_post': '. Old execution results and closed bug '
                                'reports from finished releases are usually '
                                'the cheapest thing to remove, and deleting a '
                                'project you have finished with takes its '
                                'artefacts with it.',
    'os_waking_heading': 'Waking up',
    'os_waking_body': 'This service went to sleep and had to start again '
                      '<strong>%(n)d</strong> time in the last 24 hours. It '
                      'sleeps after about %(idle)s minutes with no visitors, '
                      'and the first request after that waits %(wait)s '
                      'seconds while it starts.|This service went to sleep '
                      'and had to start again <strong>%(n)d</strong> times in '
                      'the last 24 hours. It sleeps after about %(idle)s '
                      'minutes with no visitors, and the first request after '
                      'that waits %(wait)s seconds while it starts.',
    'os_waking_noticeable': 'At that rate somebody is waiting most times they '
                            'open it.',
    'os_waking_unreadable': 'How often this service restarts could not be '
                            'read just now.',
    'os_waking_keepalive': 'Whoever runs the server can shorten that wait '
                           'during working hours by setting '
                           '<code>KEEPALIVE_URL</code> in the repository\'s '
                           'Actions variables — see '
                           '<code>.github/workflows/keepalive.yml</code>, '
                           'which also explains why it is not simply pinged '
                           'around the clock.',

    'os_mail_heading': 'Email',
    'os_mail_from': 'Invitations and password-reset links are sent from',
    'os_mail_sent': '%s of %s messages sent in the last 24 hours.',
    'os_mail_used_up': 'The allowance is used up, so invitations are not '
                       'being emailed until it frees up — the invite page '
                       'hands out a link to send by hand instead.',
    'os_mail_none': 'This instance cannot send email, so nothing is emailed '
                    'automatically. Invitations still work: the admin gets a '
                    'link to pass on. Password reset does not, because the '
                    'link has nowhere to go — an admin has to set the '
                    'password for whoever is locked out.',
    'os_mail_how': 'Whoever runs the server can switch it on by setting '
                   '<code>RESEND_API_KEY</code> and <code>MAIL_FROM</code>.',

    'os_storage_heading': 'Storage',
    'os_storage_s3_pre': 'Screenshots, videos and export bundles go to object '
                         'storage',
    'os_storage_s3_at': 'at %s',
    'os_storage_s3_post': '. They survive a restart.',
    'os_storage_local': "Screenshots, videos and export bundles are stored on "
                        "the server's own disk. <strong>That disk is "
                        "temporary</strong> — a restart takes the files with "
                        "it, and the bug or run that references them keeps "
                        "the link, which then leads nowhere.",
    'os_storage_how': 'Whoever runs the server can point this at '
                      'S3-compatible storage (Backblaze B2, AWS S3, '
                      'Cloudflare R2, Wasabi, MinIO) by setting '
                      '<code>STORAGE_BACKEND=s3</code> and the '
                      '<code>STORAGE_S3_*</code> values. No new deploy is '
                      'needed.',
    'os_storage_usage': 'This team is using %(size)s across %(n)d file.|'
                        'This team is using %(size)s across %(n)d files.',
    'os_storage_truncated': 'Counting stopped after the first %d — the real '
                            'figure is higher.',
    'os_storage_unmeasured': 'How much is in use could not be measured just '
                             'now.',
    'os_storage_not_configurable': 'Choosing storage per team, from this '
                                   'page, is not available on this instance '
                                   'yet.',
    'os_storage_own_heading': 'Your own storage',
    'os_storage_own_intro': 'Point this team at an S3-compatible bucket you '
                            'control — Backblaze B2, AWS S3, Cloudflare R2, '
                            'Wasabi or MinIO. New screenshots, videos and '
                            'export bundles go there instead of to the '
                            'server. The secret is encrypted before it is '
                            'stored and is never shown again.',
    'os_storage_configured': 'Configured: %(bucket)s at %(url)s, access key '
                             'ending %(hint)s.',
    'os_storage_endpoint': 'Endpoint',
    'os_storage_bucket': 'Bucket',
    'os_storage_region': 'Region',
    'os_storage_region_hint': 'leave empty for Cloudflare R2',
    'os_storage_access_key': 'Access key',
    'os_storage_replace_secret': 'Replace secret',
    'os_storage_secret': 'Secret key',
    'os_storage_https': 'Use HTTPS',
    'os_storage_save': 'Save storage',
    'os_storage_test': 'Test connection',
    'os_storage_note': 'Both buttons write a small file, read it back and '
                       'delete it — a check that only asked whether the '
                       'bucket exists would pass for a key that cannot '
                       'upload. <strong>Save refuses settings that fail the '
                       'test</strong>, because storing them would send this '
                       "team's files to the server's temporary disk while the "
                       'page said otherwise.',
    'os_storage_keep_secret': 'Leave the secret blank to keep the one already '
                              'stored.',
    'os_storage_use_server': "Use the server's storage",
    'os_storage_use_server_note': 'Files already in your bucket stay there. '
                                  'Nothing is moved or deleted, so pages '
                                  'referencing them will stop finding them.',

    # ── Keys the Ukrainian file had and this one did not (M-2) ────
    # The reverse of the gap M-2 was written for. These render through
    # ``t.get('key', 'English fallback')``, so English looked right and
    # the dictionaries were asymmetric — which meant the parity gate
    # could not be a simple set comparison until the English text was
    # lifted out of the templates it was hiding in. Values are the
    # fallbacks verbatim.
    'pp_active_project': 'Active project:',
    'pp_none': 'No projects yet — create one →',
    'pp_switch': 'Switch',
    'pp_new_placeholder': 'New project name…',
    'pp_create': 'Create',
    'pp_hint': 'All activities (estimations, test cases, checklist items, '
               'runs and bugs) attach to the active project automatically. '
               'Switch any time — your work persists in the database.',
    'recent_projects_title': 'Recent projects',
    'recent_projects_hint': 'Click Open to load a project — your TCs, checklist, bugs, '
                            'last estimation and run history come back from the '
                            'database.',
    'recent_projects_open': 'Open →',
    'recent_projects_active': 'Active',
    'recent_projects_updated': 'Last activity:',
    'recent_projects_more': 'Showing the 5 most recent — ',
    'active_project_title': 'Active Project',
    'active_project_hint': 'All generated artefacts (estimation, test cases, '
                           'checklists, bug reports, run results, metrics) are saved '
                           'against the project you select here.',
    'active_project_label': 'Active',
    'active_project_none': 'No active project. Create one below — every module needs '
                           'an active project to save data.',
    'base_url_label': 'Base URL',
    'description_label': 'Description',
    'description_placeholder': 'Short context — optional',
    'create_and_activate': 'Create & activate',
    'move_artefacts_title': 'Move artefacts to another project',
    'move_artefacts_hint': 'Reassign every artefact (test cases, checklists, bug '
                           'reports, runs, estimations) currently in the active '
                           'project to a different one. Useful when you generated work '
                           'in the wrong project (or in an auto-created Untitled one).',
    'move_target_existing': 'Move to existing project',
    'move_target_pick': 'pick a project',
    'move_target_new': 'Or create a new one',
    'move_target_new_placeholder': 'New project name',
    'move_confirm': 'Move all artefacts of the active project? This rewrites '
                    'their project_id.',
    'move_btn': 'Move artefacts',
    'rename': 'Rename',
    'save': 'Save',
    'cancel': 'Cancel',
    'activate': 'Activate',

    # ── The last three strings that never reached the dictionary (M-2) ──
    'brand_aria': 'Testfort + Forge',
    'chat_aria': 'Testfort QA Assistant',
    'pp_counts': '(%(tc)d TC · %(cl)d CL · %(bugs)d bugs)',
    'ie_edited_title': 'Changed by a person — a regeneration will not '
                       'overwrite it',

    # ── Strings that lived only as template fallbacks (M-2) ───────
    # These keys were referenced as ``t.get('key', 'English')`` and
    # existed in neither dictionary, so they rendered English in both
    # languages — and no comparison of the two files could see it,
    # because both were equally missing. The English below is each
    # fallback verbatim; the Ukrainian in ua.py is new.
    # Grouped by the template that asks for them.

    # _bulk_bar.html
    "bulk_selected": "selected",
    "bulk_what": "What to change",
    "bulk_apply": "Apply",
    "bulk_clear": "Clear",
    "bulk_delete": "Delete",
    "bulk_select_one": "Select",
    "bulk_select_all": "Select all",

    # _import_mapping.html
    "import_map_title": "Match the columns in your file",
    "import_map_hint": "These are the column names found in the last file "
                       "you chose. Point the ones you need at the right "
                       "field, then choose the file again and upload.",
    "import_map_skip": "not in this file",
    "import_map_required": "At least one of these is needed for a row to be "
                           "a row.",

    # _input_block.html
    "tc_format_label": "Test case format",
    "tc_format_manual": "Manual — TestFort columns for a tester to walk",
    "tc_format_gherkin": "Manual + BDD — also carries Given/When/Then for "
                         "the automation module",
    "tc_format_hint": "The TestFort columns are written either way — BDD "
                      "adds a Given/When/Then view derived from them, and "
                      "marks the cases for the Automation module to pick "
                      "up. Export the .feature files from the Export menu.",

    # automation.html
    "automation_title": "Automation",
    "automation_sub": "Generate a TypeScript + Playwright suite from the "
                      "BDD test cases, run it where the browsers live, and "
                      "send the Allure results back here.",
    "automation_how_title": "How this works",
    "automation_step1": "Generate",
    "automation_step1_body": "download a self-contained Node project built "
                             "from this project's BDD test cases, carrying "
                             "the locators the recorder already learned.",
    "automation_step2": "Run",
    "automation_step2_body": "on your machine or via the bundled GitHub "
                             "Actions workflow. That is where the browsers "
                             "are.",
    "automation_step3": "Report back",
    "automation_step3_body": "posts allure-results here. TestForTge parses "
                             "it in Python, so the numbers below need no "
                             "JVM.",
    "automation_pack": "Test pack",
    "automation_no_bdd": "No automation-targeted test cases yet.",
    "automation_all_manual": "case(s) in this project, all in manual format.",
    "automation_go_generate": "Generate with the BDD format selected",
    "automation_bdd_cases": "BDD cases",
    "automation_bound": "steps bound",
    "automation_runnable": "run end to end",
    "automation_skipping": "report skipped",
    "automation_unbound": "unbound actions",
    "automation_honesty": "A step the generator cannot bind never reports "
                          "green. Those scenarios report skipped — neither "
                          "passed nor failed — and every pass rate here is "
                          "computed over executed scenarios only. MANUAL- "
                          "ASSERTIONS.md in the bundle lists each one by "
                          "test-case id.",
    "automation_manual_list": "Assertions to verify manually",
    "automation_case": "Test case",
    "automation_assertion": "Assertion",
    "automation_precond_list": "Preconditions to set up by hand",
    "automation_precond_hint": "These scenarios skip before acting. An "
                               "unmet precondition means the scenario would "
                               "exercise a different situation than the "
                               "case describes. A precondition naming a URL "
                               "is already automated; what is left needs "
                               "data or state, which belongs in a fixture.",
    "automation_precondition": "Precondition",
    "automation_unbound_list": "Unbound action steps — these scenarios FAIL "
                               "rather than skip",
    "automation_unbound_hint": "A missing action is a defect in the test "
                               "case, not a gap in the library — the step "
                               "drifted from the house verb vocabulary in "
                               "wording_rules.yaml. Fix the case, or add "
                               "the pattern to steps/actions.ts.",
    "automation_download": "Download the suite",
    "automation_base_url": "BASE_URL",
    "automation_ingest": "Result ingestion",
    "automation_ingest_on": "Enabled. Post a zipped allure-results "
                            "directory to",
    "automation_ingest_on2": "with the token in an X-TFG-Token header.",
    "automation_ingest_off": "Disabled. Set AUTOMATION_INGEST_TOKEN on the "
                             "service to accept results. Until then every "
                             "upload is refused — an unauthenticated ingest "
                             "endpoint is one anybody can write run history "
                             "into.",
    "automation_runs": "Ingested runs",
    "automation_no_runs": "No runs ingested yet. Run the suite, then npm "
                          "run upload.",
    "automation_when": "When",
    "automation_origin": "Origin",
    "automation_label": "Label",
    "automation_pass_rate": "Pass rate",
    "automation_skipped": "Skipped",
    "automation_duration": "Duration",
    "view": "View",

    # automation_run.html
    "automation_run_title": "Automation run",
    "automation_broken": "Broken",
    "automation_status_legend": "Failed = an assertion did not hold, so the "
                                "product is wrong. Broken = the test threw "
                                "before it could assert, so the test or the "
                                "environment is wrong. Skipped = nobody "
                                "checked. The pass rate counts executed "
                                "scenarios only.",
    "automation_warnings": "Warnings about the results",
    "automation_by_suite": "By suite",
    "section": "Suite",
    "automation_failures": "Failures",
    "automation_scenario": "Scenario",
    "automation_where": "Failed at",
    "automation_message": "Message",
    "automation_skipped_h": "Skipped — nobody checked these",
    "automation_skipped_hint": "Each carries the reason the runner gave. An "
                               "assertion or precondition the generated "
                               "library could not bind lands here rather "
                               "than reporting green.",
    "automation_reason": "Reason",
    "automation_flaky": "Flaky",
    "automation_back": "Back to Automation",

    # base.html
    "nav_runs": "Runs",
    "nav_team": "Team",
    "nav_settings": "Settings",
    "nav_sign_out": "Sign out",

    # bug_reports.html
    "bug_reset_button": "Reset Project bugs",
    "bug_reset_title": "Reset Project bugs?",
    "bug_reset_body": "This deletes every bug attached to the active "
                      "project. The project itself, its test cases, and its "
                      "checklists are NOT touched. The action cannot be "
                      "undone.",
    "bug_reset_count_suffix": "bug(s) will be deleted.",
    "bug_reset_cancel": "Cancel",
    "bug_reset_confirm": "Yes, delete all bugs",
    "bug_source_filter_label": "Source:",
    "bug_source_all": "All",
    "bug_source_walkthrough": "Walkthrough only",
    "bug_source_manual_tc": "TC / manual only",
    "bug_source_filtered_count": "showing",
    "bug_source_filtered_unit": "bug(s)",
    "bug_area_filter_label": "Area:",
    "bug_area_all": "All",
    "bug_select_all": "Select all",
    "bug_select_one": "Select for bulk action",
    "bug_edited": "edited",
    "bug_attachments": "Attachment(s)",
    "bug_bulk_selected": "selected",
    "bug_bulk_pick_action": "— action —",
    "bug_bulk_close": "Close",
    "bug_bulk_status": "Set status",
    "bug_bulk_severity": "Set severity",
    "bug_bulk_priority": "Set priority",
    "bug_bulk_fix_version": "Set fix version",
    "bug_bulk_assign": "Assign to",
    "bug_bulk_delete": "Delete",
    "bug_bulk_fix_version_ph": "e.g. 2.4.0",
    "bug_bulk_assign_ph": "assignee name or email",
    "bug_bulk_apply": "Apply",
    "bug_bulk_clear": "Clear",
    "bug_no_bugs_for_filter": "No bug reports match the current filter.",
    "bug_clear_source_filter": "Clear filter",

    # checklist.html
    "cl_gen_title": "Generating checklist…",
    "cl_gen_stage_init": "Analyzing input…",
    "cl_gen_stage_stories": "Drafting user stories…",
    "cl_gen_stage_items": "Generating checklist items…",
    "cl_gen_stage_almost": "Almost done — finalising…",
    "cl_gen_stage_resuming": "Session refreshed — resuming…",
    "cl_gen_session_expired": "Your session expired. Reload the page and "
                              "generate again.",
    "cl_gen_reload": "Reload page",
    "cl_gen_stage_fallback": "Generation could not finish — try again.",
    "cl_gen_offline": "Generation could not finish — try again.",
    "cl_gen_lost": "The generation job was lost — retrying directly.",
    "cl_gen_unstable": "Server returned errors — retrying directly.",
    "cl_gen_stage_done": "Done — loading checklist…",
    "cl_gen_failed": "Generation failed",
    "cl_gen_watchdog": "Generation is taking longer than expected — "
                       "finishing directly.",
    "cl_gen_bad_response": "The server returned an unexpected response — "
                           "try again.",
    "cl_gen_offline_submit": "Could not reach the server — check your "
                             "connection and try again.",
    "cl_upload_title": "Upload existing checklist",
    "cl_upload_hint": "Drop an existing checklist (XLSX / CSV / MD / JSON) "
                      "— TestForTge maps the columns and stores the items "
                      "in the session for manual or automated execution. "
                      "Recognised headers include any variant of ID, "
                      "Section, Objective, Category, Priority, Status, "
                      "Testing Type.",
    "cl_upload_file": "File",
    "cl_upload_replace": "Replace current checklist",
    "cl_upload_append": "Append to current checklist",
    "cl_upload_btn": "Upload",
    "te_resource_urls": "Resource Pages",
    "cl_add_item": "New checklist item",
    "cl_editor_hint": "Click any field to edit it. Changes are saved for "
                      "the whole team.",
    "cl_section": "Section",
    "cl_rename_section": "Rename this section",
    "cl_add_here": "Add an item to this section",
    "cl_edited": "edited",
    "cl_move_up": "Move up",
    "cl_move_down": "Move down",
    "cl_move_section": "Move to another section",
    "cl_delete": "Delete this item",

    # estimation.html
    "est_gen_title": "Computing estimation…",
    "est_gen_stage_init": "Analyzing input…",
    "est_team_size": "Team size",
    "est_team_suggested": "Suggested",
    "est_team_override": "Override",
    "est_team_size_hint": "TestForTge suggests a headcount from the "
                          "estimate. Override only if you have a fixed team "
                          "— it feeds Brooks\\",
    "est_tab_text": "Requirements text",
    "est_tab_mockups": "Mockups",
    "est_tab_url": "URL crawl",
    "est_tab_text_hint": "Paste the spec, user stories, or a feature list. "
                         "The estimator parses bullet points, tables, and "
                         "free-form prose. You can also attach a .docx / "
                         ".pdf / .xlsx with the same contents — it will be "
                         "merged with the textarea below.",
    "est_text_input_label": "Paste requirements",
    "est_text_attachment_label": "Or attach a document",
    "est_text_attachment_hint": "Supported: .txt, .md, .csv, .xlsx, .docx, "
                                ".pdf. The file is parsed for text only.",
    "est_tab_mockups_hint": "Upload design screens (PNG / JPG / PDF) or "
                            "paste a Figma file URL. The estimator analyses "
                            "the visual UI to identify forms, navigation, "
                            "dialogs, error states, and other testable "
                            "elements — then computes Min/Max effort and a "
                            "starter test-case set.",
    "est_mockups_files_label": "Upload mockups (one or many)",
    "est_mockups_files_hint": "PNG / JPG / WebP / PDF. PDFs are split into "
                              "one image per page. Up to 20 images per run.",
    "est_mockups_figma_label": "Or a Figma URL",
    "est_mockups_figma_hint": "Public Figma file or frame URL. The cover "
                              "image is fetched and analysed; for richer "
                              "extraction add the file ID to FIGMA_PAT in "
                              "env.",
    "est_mockups_context_label": "Optional context",
    "est_mockups_context_ph": "e.g. \\\"This is the checkout flow for an "
                              "e-commerce app — focus on payment "
                              "validation.\\\"",
    "est_tab_url_hint": "Paste the production / staging URL. The estimator "
                        "crawls the site, identifies pages and forms, "
                        "infers the architecture (CMS, SPA, e-commerce, …), "
                        "and produces an effort breakdown.",
    "est_gen_stage_text_init": "Parsing requirements…",
    "est_gen_stage_text_features": "Extracting features…",
    "est_gen_stage_text_compute": "Computing Min/Max hours per phase…",
    "est_gen_stage_mock_init": "Preparing mockups…",
    "est_gen_stage_mock_vision": "Asking Claude vision to identify testable "
                                 "elements…",
    "est_gen_stage_mock_features": "Building feature list from the analysis…",
    "est_gen_stage_mock_compute": "Computing Min/Max hours per phase…",
    "est_gen_stage_url_init": "Resolving URL…",
    "est_gen_stage_url_crawl": "Crawling pages, forms, and architecture…",
    "est_gen_stage_url_features": "Mapping detected features to the test "
                                  "budget…",
    "est_gen_stage_url_compute": "Computing Min/Max hours per phase…",
    "est_gen_offline": "Estimation could not finish — try again.",
    "est_gen_retry": "Retry",
    "est_gen_lost": "The estimation job was lost — try again.",
    "est_gen_unstable": "Server returned errors — try again.",
    "est_gen_stage_done": "Done — loading results…",
    "est_gen_failed": "Estimation failed",
    "est_gen_watchdog": "Estimation is taking longer than expected — try "
                        "again.",
    "est_session_expired": "Your session expired. Reload the page and run "
                           "again.",
    "est_gen_bad_response": "The server returned an unexpected response — "
                            "try again.",
    "est_gen_dispatch_failed": "Could not start estimation — try again.",
    "est_to_tc": "Generate test cases from this estimate →",
    "est_history_label": "Calibration vs your past projects",
    "est_pert_toggle": "Show PERT confidence (advanced)",
    "est_pert_what": "What this is.",
    "est_pert_explainer": "A second-opinion view of the same estimate, "
                          "expressed as a",
    "est_pert_formula": "Formulae: μ = (O + 4M + P) / 6 · σ = (P − O) / 6 ·",
    "est_pert_expected": "PERT expected (μ)",
    "est_pert_sigma": "Std. deviation (σ)",
    "est_pert_68": "68% band (μ ± σ)",
    "est_pert_95": "95% band (μ ± 2σ)",
    "est_edit_title": "Adjust the estimate",
    "est_edit_hint": "Change what drives the numbers — the hours and costs "
                     "are recomputed on the server, so what you see here is "
                     "what is stored.",
    "est_recalculate": "Recalculate",
    "est_revert": "Back to the model's numbers",
    "est_diff_title": "Edited — the model computed this",
    "est_diff_what": "Number",
    "est_diff_model": "Model",
    "est_diff_now": "Now",
    "est_diff_delta": "Change",

    # index.html
    "dash_period": "Period",
    "dash_run": "Run",
    "dash_all": "All",
    "dash_tester": "Tester",
    "dash_suite": "Suite",
    "dash_environment": "Environment",
    "dash_apply": "Apply",
    "dash_clear": "Clear",
    "dash_export": "Export CSV",
    "dash_filtered_note": "These numbers are filtered. The export follows "
                          "the same filter.",
    "dash_kpi_title": "KPIs against target",
    "dash_target": "target",
    "dash_no_target": "no target set",
    "dash_customise": "Customise this dashboard",
    "dash_widgets_title": "What to show",
    "dash_widgets_hint": "Yours alone — it does not change what anyone else "
                         "sees.",
    "dash_save_layout": "Save layout",
    "dash_targets_title": "KPI targets for this project",
    "dash_targets_hint": "Shared by everyone on the project. Leave a field "
                         "empty for no target.",
    "dash_save_targets": "Save targets",

    # review_session.html
    "review_session_title": "Review recorded session",
    "review_session_subtitle": "The recorder split your session into the "
                               "proposed flows below. Tick the ones to "
                               "keep, optionally tweak the suite tag, then "
                               "save — each becomes a new test case in the "
                               "active project, with its recorded steps "
                               "attached. Saved cases show up on the Test "
                               "Cases page and can be re-run any time from "
                               "Test Execution (pick them by suite), so a "
                               "recording is reusable across future testing "
                               "rounds.",
    "review_session_blocked": "Review unavailable",
    "review_session_proposed_n": "proposed test case(s) for project",
    "review_session_expires": "Link expires",
    "review_session_deepcapture_off": "Deep capture (network + console) was "
                                      "unavailable for this session — the "
                                      "debugger could not attach (DevTools "
                                      "open on the tab, a chrome:// page, "
                                      "or the banner was dismissed). Steps "
                                      "were still recorded.",
    "review_session_errors_detected": "issue(s) detected during this session",
    "review_session_failed_requests": "failed request(s)",
    "review_session_console_errors": "console error(s)",
    "review_session_errors_hint": "These are candidates for bug reports — "
                                  "expand the panel below to inspect.",
    "review_session_telemetry_title": "Session telemetry",
    "review_session_requests": "requests",
    "review_session_console_msgs": "console msgs",
    "review_session_snapshots": "DOM snapshots",
    "review_session_network": "Network requests",
    "review_session_status": "Status",
    "review_session_method": "Method",
    "review_session_url": "URL",
    "review_session_type": "Type",
    "review_session_console": "Console output",
    "review_session_dom": "DOM snapshots",
    "review_session_controls": "interactive controls",
    "review_session_save_label": "Save",
    "review_session_intent": "Intent",
    "review_session_summary_label": "Summary (editable)",
    "review_session_suite_label": "Suite tag",
    "review_session_classifier_hint": "Classifier suggests",
    "review_session_steps_preview": "Preview captured steps",
    "review_session_save_all": "Save selected",
    "review_session_cancel": "Cancel — discard recording",

    # test_cases.html
    "tc_gen_title": "Generating test cases…",
    "tc_gen_stage_init": "Analyzing input…",
    "tc_gen_stage_stories": "Drafting user stories…",
    "tc_gen_stage_cases": "Generating test cases…",
    "tc_gen_stage_trace": "Computing traceability matrix…",
    "tc_gen_stage_almost": "Almost done — finalising…",
    "tc_gen_stage_resuming": "Session refreshed — resuming…",
    "tc_gen_stage_saved": "Finished — the server restarted before the page "
                          "updated.",
    "tc_gen_saved_pack": "Generation finished and NUM test cases were saved "
                         "for this project, but the server restarted before "
                         "the page could update. Reload to see them.",
    "tc_gen_show_saved": "Show saved test cases",
    "tc_gen_stage_fallback": "Generation could not finish — try again.",
    "tc_gen_worker_died": "The server restarted before the run finished, so "
                          "nothing was saved. This happens on the free plan "
                          "when a generation exceeds the memory limit — try "
                          "a narrower scope or fewer pages.",
    "tc_gen_session_expired": "Your session expired. Reload the page and "
                              "generate again.",
    "tc_gen_reload": "Reload page",
    "tc_gen_offline": "Generation could not finish — try again.",
    "tc_gen_unstable": "Server returned errors — retrying directly.",
    "tc_gen_stage_done": "Done — loading test cases…",
    "tc_gen_failed": "Generation failed",
    "tc_gen_watchdog": "Generation is taking longer than expected — "
                       "finishing directly.",
    "tc_gen_bad_response": "The server returned an unexpected response — "
                           "try again.",
    "tc_gen_offline_submit": "Could not reach the server — check your "
                             "connection and try again.",
    "tc_upload_title": "Upload existing test cases",
    "tc_upload_hint": "Drop an existing pack (XLSX / CSV / MD / JSON) — "
                      "TestForTge maps the columns and stores the cases in "
                      "the session for manual or automated execution. "
                      "Supported headers include any case-insensitive "
                      "variant of TC ID, Section, Summary, Preconditions, "
                      "Test Steps, Test Data, Expected Result, Category, "
                      "Priority, Status, Testing Type.",
    "tc_upload_file": "File",
    "tc_upload_replace": "Replace current pack",
    "tc_upload_append": "Append to current pack",
    "tc_upload_btn": "Upload",
    "filter_suite_label": "Suite",
    "filter_suite_all": "All suites",
    "recorder_pending_title": "Pending recording sessions",
    "recorder_pending_hint": "These recordings are waiting for review — "
                             "open one to pick which captured test cases to "
                             "keep and which suite they land in. Unreviewed "
                             "sessions expire 24 h after capture.",
    "recorder_pending_review": "Review session",
    "recorder_pending_tc_count": "proposed test case(s)",
    "recorder_pending_captured": "captured",
    "ext_recorder_start": "Start session recording",
    "ext_recorder_hint": "Walk through your scenario in any tab — Stop and "
                         "the captured TCs land in a review screen.",
    "ext_recorder_install": "Install the extension",
    "ext_recorder_modal_title": "Start session recording",
    "ext_recorder_modal_hint": "The TestForTge recorder extension must be "
                               "installed in this Chrome profile. The "
                               "recorder will open the URL below in a new "
                               "tab — walk the scenario, then click Stop on "
                               "the floating overlay to land the captured "
                               "steps in a review screen.",
    "ext_recorder_url_label": "Start URL",
    "ext_recorder_cancel": "Cancel",
    "ext_recorder_launch": "Launch",
    "tc_create": "New test case",
    "tc_editor_hint": "Click any field to edit it. Changes are saved for "
                      "the whole team.",
    "tc_suite": "Suite",
    "tc_edited": "edited",
    "tc_step_edit": "Edit step",
    "tc_step_up": "Move up",
    "tc_step_down": "Move down",
    "tc_step_delete": "Delete step",
    "tc_step_add": "Add step",
    "tc_delete": "Delete this test case",
    "tc_gherkin_summary": "BDD view (Given / When / Then)",
    "tc_gherkin_hint": "Derived from the columns above. Edit the case, not "
                       "this — the .feature export re-derives on every "
                       "download.",
    "tc_walkthrough_meta_title": "Walkthrough binding",
    "tc_url_pattern_label": "URL pattern (fnmatch glob)",
    "tc_trigger_label": "Trigger",
    "tc_trigger_manual": "manual — only user-driven runs",
    "tc_trigger_url_match": "walkthrough_url_match — fires on matching pages",
    "tc_trigger_always": "always — fires on every walkthrough page",
    "tc_walkthrough_meta_hint": "Empty URL pattern + trigger \"always\" makes "
                                "the walkthrough fire this TC on every "
                                "visited page. trigger=\"manual\" disables "
                                "walkthrough firing entirely.",
    "tc_recorder_title": "Record steps",
    "tc_recorder_has_steps": "Has recorded steps",
    "tc_recorder_hint": "Run locally — opens Chromium so you can click "
                        "through the scenario. Steps land on this TC "
                        "automatically.",
    "tc_recorder_url_label": "Start URL",
    "tc_recorder_copy": "Copy command",
    "tc_recorder_step_kind_hint": "Flip a step to an assertion (visible / "
                                  "text / URL) to make the runner check "
                                  "expectations instead of issuing a click.",
    "tc_recorder_step_action": "Recorded step",
    "tc_recorder_step_kind": "Kind",
    "export_feature": "Export .feature (BDD)",

    # test_execution.html
    "te_overlay_slow": "The run is taking longer than expected. The page "
                       "will not reload automatically — close this dialog "
                       "and refresh manually to see whatever results have "
                       "landed in the session.",
    "te_empty_body": "The active project has no test cases or checklist "
                     "items in your session. Generate a pack first — or "
                     "switch to a project that already has one via the "
                     "picker above.",
    "te_empty_to_tc": "Generate test cases →",
    "te_empty_to_cl": "Generate checklist",
    "te_empty_to_est": "Scope at /estimation",
    "te_pack_loaded": "Pack loaded",
    "te_pack_tc": "test case(s)",
    "te_pack_cl": "checklist item(s)",
    "te_pack_clear_confirm": "Discard the current pack and start over?",
    "te_pack_clear": "Clear pack",
    "te_upload_drop": "Drop file here, or click to browse",
    "te_upload_btn_pack": "Upload pack",
    "te_subtab_results": "Results",
    "te_subtab_findings": "Findings",
    "te_item_summary": "Summary",
    "te_evidence_video": "recorded video",
    "te_wt_bindings_label": "URL-pattern matched these test cases during "
                            "the walkthrough:",
    "te_wt_sev": "Severity",
    "te_wt_area": "Area",
    "te_wt_message": "Finding",
    "te_wt_bug": "Bug",
    "te_wt_more": "Details",
    "te_wt_user_impact": "User impact:",
    "te_wt_fix_hint": "Fix hint:",
    "te_wt_dev_detail": "Dev detail:",
    "te_wt_console": "Console errors:",
    "te_open_runs_title": "Unfinished manual runs",
    "te_open_runs_hint": "Started and not closed. Resuming lands on the "
                         "first item without a verdict.",
    "run": "Run",
    "started": "Started",
    "te_resume": "Resume",
    "te_run_mode_title": "Run Mode",
    "te_run_mode_hint": "TC/Checklist-driven runs the items you selected "
                        "below. QA walkthrough ignores selection and "
                        "explores the URL autonomously, raising findings "
                        "for broken images, accessibility issues, dead "
                        "navigation, and more.",
    "te_mode_tc": "Automated — built-in engine",
    "te_mode_tc_help": "Playwright drives the items you select below. Watch "
                       "it live or leave it in the background.",
    "te_mode_manual": "Manual — walk it yourself",
    "te_mode_manual_help": "One item at a time with its steps and expected "
                           "result on screen, one click per verdict. "
                           "Resumable.",
    "te_assignee": "Assign the manual walk to",
    "te_assignee_me": "Me",
    "te_assignee_hint": "The assignee is who can record verdicts in the "
                        "run. Admins can always open any run in the "
                        "project.",
    "te_mode_walkthrough": "QA walkthrough",
    "te_mode_walkthrough_help": "Autonomous exploration + opportunistic TC "
                                "matching by URL.",
    "te_site_sweep": "Also sweep the site for Performance, Security, "
                     "Accessibility and UI defects the pack does not cover",
    "te_site_sweep_hint": "Runs 53 checks against the live URL and files "
                          "every failure as a bug in its own quality- "
                          "attribute category. Independent of the items "
                          "selected below.",
    "te_mode_ts_note": "Running the TypeScript + Playwright suite? That "
                       "lives in",
    "te_mode_ts_note2": "it is generated there, run where the browsers are, "
                        "and its Allure results are ingested back into that "
                        "same page.",
    "te_wt_options": "Walkthrough options",
    "te_wt_max_pages": "Max pages to visit",
    "te_wt_max_pages_hint": "Including the entry URL. Each page runs the "
                            "full heuristic battery.",
    "te_wt_max_form_fills": "Max forms to fill",
    "te_wt_max_form_fills_hint": "Forms are filled with sample data; they "
                                 "are NOT submitted.",
    "te_wt_device_timeout": "Device timeout (ms)",
    "te_wt_device_timeout_hint": "Hard wall-clock kill per device. Default "
                                 "8 min matches TFWefloLab.",
    "te_wt_axe": "Run axe-core accessibility scan",
    "te_wt_axe_hint": "Loads axe-core from CDN, then evaluates axe.run() "
                      "per page. Disable for offline / strict egress "
                      "environments.",
    "te_wt_tc_binding": "Test Case binding",
    "te_wt_binding_url": "URL pattern (fire TCs whose url_pattern matches "
                         "the visited page)",
    "te_wt_binding_ignore": "Ignore TCs (heuristics only)",
    "te_wt_tc_binding_hint": "When binding is on, TCs marked "
                             "trigger=walkthrough_url_match / always become "
                             "candidates. Pre-PR-2 TCs default to "
                             "trigger=manual and are not fired.",
    "te_new_run": "New Test Run",
    "te_select_items": "Select Items to Test",
    "te_select_items_hint": "Select specific items or leave all checked to "
                            "test everything",
    "te_none": "None",
    "te_selected_label": "Selected",
    "te_manual_status_hint": "Leave Status as \"Auto\" to let the tester "
                             "simulate / run real checks, or set Passed / "
                             "Failed / Blocked manually. Attach an existing "
                             "bug ID to link without creating a new one.",
    "te_status_auto": "Auto",
    "te_bug_placeholder": "Bug ID",
    "te_auto_config": "Automation Configuration",
    "te_auto_hint": "Optional. When you supply a Base URL the Web / Mobile- "
                    "Web environments drive a real headless browser, "
                    "capture screenshots, optionally record video, and "
                    "auto-link any failures to bug reports.",
    "te_live_callout_title": "Watch the run live",
    "te_live_callout_body": "On cloud servers you cannot see the browser "
                            "window directly, but you can stream a frame- "
                            "by-frame view of every screenshot Playwright "
                            "takes. Open the live view in a new tab BEFORE "
                            "clicking Run below.",
    "te_live_open": "Open live view in new tab ↗",
    "te_base_url": "Base URL",
    "te_base_url_badge": "required for live + screenshots + video",
    "te_base_url_hint_v2": "Without Base URL the run uses the deterministic "
                           "simulator only — no live preview, no recorded "
                           "video, no screenshots, no bug-report "
                           "attachments. Paste a target URL to enable real "
                           "Playwright execution.",
    "te_record_video": "Record video",
    "te_record_no": "No (faster)",
    "te_record_yes": "Yes — save .webm with visible click / scroll / redirect",
    "te_record_video_hint": "When on, every action is slowed and elements "
                            "flash a red outline before being clicked so "
                            "the .webm is legible.",
    "te_env_hint": "Pick one or more environments. Each selected "
                   "environment produces its own run record with status "
                   "tracking, bug links, screenshots and (when enabled) "
                   "video.",
    "te_env_web_help": "desktop browser on Windows / macOS / Linux",
    "te_env_mw_help": "a browser on a phone / tablet",
    "te_env_ios_help": "native iOS app on iPhone / iPad",
    "te_env_android_help": "native Android app on phone / tablet",
    "te_os_version_label": "OS / Version",
    "te_version": "Version",
    "te_optional": "optional",
    "te_web_version_ph": "e.g. Chrome 138 on Win 11",
    "te_mw_os_version": "OS / Version",
    "te_resolution": "Device resolution",
    "te_os_version": "OS version",
    "te_mw_version_ph": "e.g. iOS 17.4 / Android 14",
    "te_device_model": "Device model",
    "te_ios_version_ph": "e.g. iOS 17.4.1",
    "te_app_build": "App build",
    "te_app_build_ph": "e.g. 1.42.0 (build 5310)",
    "te_android_version_ph": "e.g. Android 14",
    "te_suite_filter_label": "Run only suite:",
    "te_suite_filter_all": "All suites",
    "te_suite_filter_untagged": "Untagged only",
    "te_suite_filter_hint": "— ticks / unticks the TC checkboxes by suite "
                            "tag.",
    "te_start_run": "Run Test Execution",
    "te_overlay_title": "Live automation view — run in progress",
    "te_overlay_body": "TestForTge is running your selected items across "
                       "every chosen environment. This tab will reload "
                       "automatically once the run completes — please don\\",
    "te_overlay_live_link": "Open live frame-by-frame view in a new tab ↗",

    # test_execution_live.html
    "te_live_title": "Live automation view",
    "te_live_subtitle": "Real-time filmstrip of the headless browser. Open "
                        "this page BEFORE clicking Run on Test Execution — "
                        "frames will start streaming once Playwright takes "
                        "its first screenshot.",
    "te_live_help_title": "Nothing is streaming yet.",
    "te_live_help_body": "Live view only updates while a Playwright run is "
                         "in progress. Make sure you set a Base URL on the "
                         "Test Execution page before clicking Run — without "
                         "it the simulator runs and no frames are captured.",
    "te_live_help_back": "Back to Test Execution",
    "te_live_idle_title": "No automation run has been dispatched yet",
    "te_live_idle_body": "This page streams the headless browser only while "
                         "a Playwright run is in progress. To start one, go "
                         "to Test Execution, set a Base URL, and click Run "
                         "Test Execution.",
    "te_live_idle_cta": "Go to Test Execution →",
    "te_live_hint": "Frames refresh once per second. After the run "
                    "completes you can review per-case screenshots and "
                    "recorded video on the Test Execution results page.",
    "te_live_back": "Back to Test Execution",

    # test_execution_manual.html
    "manual_run_title": "Manual run",
    "manual_run_sub": "One item at a time, with everything needed to judge "
                      "it on screen.",
    "manual_run_judged": "judged",
    "manual_run_passed_of": "passed of",
    "manual_run_executed": "executed",
    "manual_run_done": "Every item has a verdict",
    "manual_run_done_hint": "Close the run to write its totals, or step "
                            "back through any item to correct a verdict — a "
                            "correction overwrites the old one rather than "
                            "adding a second result.",
    "manual_run_close": "Close the run",
    "manual_run_missing_title": "This item is no longer in the pack.",
    "manual_run_missing_body": "It was part of the run when the walk "
                               "started and has since been deleted or "
                               "regenerated, so there is nothing left to "
                               "check. Record it as Skipped — the run keeps "
                               "its original total, so the coverage number "
                               "stays honest.",
    "manual_run_empty_title": "This item has no steps and no expected "
                              "result yet.",
    "manual_run_empty_body": "There is nothing here to judge. Write it in "
                             "the Test Cases or Checklist module first, "
                             "then reload this page — the walk reads the "
                             "pack fresh on every render, so it will pick "
                             "the content up without restarting the run.",
    "manual_run_cl_hint": "Checklist item — the objective above is the "
                          "whole check.",
    "manual_run_already": "Already judged",
    "manual_run_bug": "bug",
    "manual_run_overwrite": "submitting again overwrites it.",
    "manual_run_notes": "What did you see?",
    "manual_run_notes_hint": "The actual result, in your words. Required "
                             "reading if this becomes a bug — it is what "
                             "the report will say happened.",
    "manual_run_file_bug": "File a bug from this item (Failed / Passed but "
                           "only)",
    "manual_run_prev": "Previous",
    "manual_run_next": "Next",
    "manual_run_close_early": "Close the run early",
    "manual_run_close_early_hint": "Closing early is fine — the run records "
                                   "how many items actually got a verdict, "
                                   "so a partial walk is never reported as "
                                   "full coverage.",
    "manual_run_all": "All items",
    "manual_run_item": "Item",

    # test_execution_runs.html
    "runs_title": "Runs",
    "runs_hint_no_auth": "Every run in this project. Assignment needs "
                         "authentication — with it off there is nobody to "
                         "assign a run to, so there is nothing to filter "
                         "by.",
    "runs_hint_mine": "Runs assigned to you.",
    "runs_hint_all": "Every run in this project.",
    "runs_scope_mine": "Assigned to me",
    "runs_scope_all": "All runs",
    "mode": "Mode",
    "runs_open": "Open",
    "runs_closed": "Closed",
    "runs_review": "Review",
    "runs_empty_mine": "Nothing is assigned to you in this project yet.",
    "runs_empty": "This project has no runs yet.",
    "runs_start": "Start a run",
    # Keys a template builds at render time (`'dash_period_' ~ period`), so
    # no scanner can see the whole name — they are listed here in full for
    # every value PERIODS can take, which is what makes them checkable.
    "dash_period_7d": "Last 7 days",
    "dash_period_30d": "Last 30 days",
    "dash_period_90d": "Last 90 days",
    "dash_period_all": "All time",
    "recent_projects_more_link": "%d projects total in your account",
    # Keys a template builds at render time (`'dash_period_' ~ period`), so
    # no scanner can see the whole name — they are listed here in full for
    # every value PERIODS can take, which is what makes them checkable.
    "dash_period_7d": "Last 7 days",
    "dash_period_30d": "Last 30 days",
    "dash_period_90d": "Last 90 days",
    "dash_period_all": "All time",
    "recent_projects_more_link": "%d projects total in your workspace",
}
