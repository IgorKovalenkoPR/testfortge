# QA Forge v2 — Functional Description

## 1. Overview

**QA Forge** is a web-based framework designed for manual QA testers. It automates the generation of test documentation following the **TestFort** format — a professional QA standard used in real testing companies.

**Mission:** Eliminate repetitive, boilerplate QA documentation work and let testers focus on actual testing.

**Tech Stack:**
- Backend: Python 3.11+ / Flask 3.1
- Templates: Jinja2 with i18n (EN/UA)
- Frontend: Vanilla HTML/CSS/JS (no frameworks)
- File Parsing: python-docx, openpyxl, PyPDF2, Pillow
- Export: Markdown, HTML, CSV, XLSX (with formatting)
- Storage: Session-based + file-system project persistence

---

## 2. Target Audience

- Manual QA Testers (Junior to Senior)
- QA Leads planning test documentation
- Small QA teams without enterprise TMS (Test Management Systems)
- Testers preparing for ISTQB certification (all techniques and terminology are ISTQB-aligned)

---

## 3. Core Workflow

```
                    +------------------+
                    |  Project Setup   |
                    |  (domain, platform, |
                    |   features, team)  |
                    +--------+---------+
                             |
              +--------------+--------------+
              |                             |
    +---------v---------+         +---------v---------+
    |   Requirements    |         |  Direct US Input  |
    |  (upload/paste)   |         |  (skip reqs step) |
    +---------+---------+         +---------+---------+
              |                             |
              +-------------+---------------+
                            |
                  +---------v---------+
                  |   User Stories    |
                  |  (auto-generated) |
                  +---------+---------+
                            |
         +------------------+------------------+
         |          |          |        |       |
    +----v---+ +---v----+ +--v---+ +--v--+ +--v------+
    |Test    | |Check-  | |Test  | |Status| |Test     |
    |Cases   | |list    | |Plan  | |Report| |Metrics  |
    +----+---+ +---+----+ +--+---+ +--+--+ +----+----+
         |         |          |        |         |
         +----+----+----+-----+--------+---------+
              |         |
         +----v---+ +---v--------+
         | Export | | Recommenda- |
         | MD/HTML| | tions /     |
         | CSV/   | | Techniques/ |
         | XLSX   | | Tools       |
         +--------+ +------------+
```

**Key Principle:** Every feature works independently. You can:
- Generate a Test Plan without User Stories
- Generate Test Cases from pasted User Stories (skip Requirements)
- View Techniques/Recommendations without any project data
- Export at any stage

---

## 4. Features (14 Pages)

### 4.1 Dashboard (`/`)
- Quick stats for the current project (domain, platform, requirement/story counts)
- Quick-access buttons to all modules
- Saved Projects table (load/delete)
- Feature overview grid (6 capability cards)
- Save Project button (persists session to disk as JSON)

### 4.2 Project Setup (`/setup`)
- **Project Name** — used across all generated artifacts
- **Domain** — E-Commerce, FinTech, Healthcare, SaaS, EdTech, Social Media, IoT, Other
- **Platform** — Web, Mobile Native, Hybrid/PWA, Desktop, API/Backend
- **Tech Stack** — comma-separated (e.g., React, Node.js, PostgreSQL)
- **Team Size** — solo, small (2-5), medium (6-15), large (16+)
- **Product Features** — checkboxes: API, Auth, Payments, File Upload, Real-time
- **Target Markets** — EU, US, UA (affects applicable standards like GDPR, PCI DSS)
- **Budget** — Free Only, Low, Medium, Enterprise (filters tool recommendations)
- **Release Frequency** — daily, weekly, biweekly, monthly, quarterly
- **Modules** — 11 toggleable modules: user_stories, test_cases, checklist, test_plan, status_report, test_metrics, techniques, recommendations, tools, guide, export

All of the above feeds into the **ProjectContext** object, which is used by the Advisor engine to personalize recommendations.

### 4.3 Requirements Input (`/requirements`)
- **Text Input** — paste requirements in any format:
  - Numbered (1., 2., 3.)
  - Bulleted (-, *, bullet)
  - ID-prefixed (REQ-001: ...)
  - Freeform paragraphs
- **File Upload** — drag-and-drop or click, multi-file, up to 100MB total
  - Supported: `.txt`, `.md`, `.docx`, `.xlsx`, `.csv`, `.pdf`, `.png`, `.jpg`
  - DOCX: extracts from paragraphs + tables
  - XLSX/CSV: reads rows as pipe-separated values
  - PDF: extracts text page by page (PyPDF2)
  - Images: metadata extraction (OCR note)
- **Custom Prompt** — additional instructions for generation
- **Insert Example** — pre-fills 10 sample requirements
- Auto-detects requirement IDs or assigns REQ-001, REQ-002, etc.

### 4.4 User Stories (`/user-stories`)
Generated automatically from requirements using keyword analysis:

- **Role Detection** — admin, manager, customer, guest, patient, doctor, student, teacher, user (regex patterns)
- **Action Extraction** — strips prefixes like "The system must...", "Users should..."
- **Benefit Inference** — maps keywords to business value (e.g., "login" -> "I can securely access my account")
- **Priority** — High (must, critical, security), Medium (default), Low (nice to have, optional)
- **Complexity** — S (1-2), M (3-5), L (8-13) story points based on action keywords
- **Acceptance Criteria** — auto-generated (3-6 per story) based on detected keywords:
  - Form validation criteria
  - Auth criteria (session, unauthorized access)
  - Data persistence criteria
  - Empty state handling
  - Error handling (always included)

**Output Format:**
```
US-001 (from REQ-001)
As a [role], I want [action], so that [benefit].
Priority: High | Story Points: M (3-5)
Acceptance Criteria:
  - [ ] The feature works as described
  - [ ] Input fields have proper validation
  - [ ] Error handling works for edge cases
```

### 4.5 Test Cases (`/test-cases`) — TestFort Format
Generated from User Stories with **scenario-based detection** (13 scenario types):

| Scenario | Keywords | Generated Cases |
|----------|----------|-----------------|
| Auth | login, register, password, 2fa | 4 (positive, negative, edge, security) |
| CRUD Create | create, add, new, submit | 3 (positive, negative, edge) |
| CRUD Read | view, display, show, list | 2 (positive, edge/empty) |
| CRUD Update | edit, update, modify | 2 (positive, negative) |
| CRUD Delete | delete, remove, cancel | 2 (positive, negative) |
| Search | search, filter, sort, query | 3 (positive, negative, edge) |
| Payment | payment, checkout, billing | 3 (positive, negative, edge) |
| Upload/Export/Notification/Security/Performance/Integration | Various | 3 each (generic template) |

**TestFort ID Format:** `SC{section}_{number}` — e.g., SC1_001, SC1_002, SC2_001

**Each Test Case contains:**
| Field | Description |
|-------|-------------|
| ID | SC1_001 format |
| Section | Grouped name (Authentication, Create / Add, Search / Filter, etc.) |
| Summary | What the test case verifies |
| Preconditions | Required state before testing |
| Test Steps | Numbered steps (1. Open app, 2. Navigate to..., 3. Enter...) |
| Test Data | Specific data values to use |
| Expected Result | What should happen |
| Issues | Bug references (initially empty) |
| Comment | Additional notes |
| Status | Unchecked / Passed / Failed / Passed but / Blocked |

**UI Features:**
- Filter bar (All / Positive / Negative / Edge Case / Security)
- Section headers with grouping
- Status dropdown with color coding (green=Passed, red=Failed, yellow=Passed but, purple=Blocked)
- Stats grid (total, positive, negative, edge/security counts)
- Traceability Matrix tab (Requirement -> Story -> Test Cases)
- Custom prompt for regeneration
- Export buttons (Markdown, HTML, CSV, XLSX)

### 4.6 Checklist (`/checklist`) — TestFort Format
A compact testing checklist for faster execution:

**TestFort ID Format:** `{PREFIX}_{number}` — e.g., AUTH_001, CRT_002, SRCH_001

**Per User Story generates:**
1. Positive check: "Verify that [action] works correctly with valid data"
2. Negative check: "Verify that [action] rejects invalid/empty input"
3. Edge case check: "Verify that [action] handles boundary values"
4. Acceptance criteria checks (up to 3 from the story)

**Each item contains:**
| Field | Description |
|-------|-------------|
| ID | PREFIX_001 format |
| Section | Grouped by scenario type |
| Objective | What to verify |
| Category | Positive / Negative / Edge Case / Acceptance |
| Priority | High / Medium / Low |
| Status | Dropdown: — / P / F / P/B / BLK |
| Comments | Inline text input |

**Section Prefixes:** AUTH, CRT, VIEW, UPD, DEL, SRCH, PAY, UPL, EXP, NTF, SEC, PERF, API, NAV, PROF, GEN

### 4.7 Test Plan (`/test-plan`) — TestFort 13-Section Template
Full test plan following IEEE-829 / TestFort standard:

| # | Section | Content |
|---|---------|---------|
| 1 | Test Plan Identifier | Auto-generated: {ProjectName}_v1.0_{date} |
| 2 | References | Project Plan, Requirements Specs (placeholder links) |
| 3 | Introduction | Objectives + Team Members table (PM, QA Lead, QA roles/responsibilities) |
| 4 | Scope | In Scope (from domain modules), Out of Scope |
| 5 | Assumptions / Risks | Risk Register table (3 risks with severity, triggers, mitigation) |
| 6 | Features to be Tested | From User Stories or domain critical modules |
| 7 | Features NOT to be Tested | Placeholder for manual exclusion |
| 8 | Test Approach (Strategy) | QA Role, Testing Types (from domain), Bug Severity table (5 levels), Automation note |
| 9 | Pass/Fail Criteria | Table: Function/GUI/Configuration testing criteria |
| 10 | Environmental Needs | From platform must-test items (browsers, devices, tools) |
| 11 | Staffing & Training | Training areas for the testing team |
| 12 | Milestones / Deliverables | Schedule table + Deliverables table |
| 13 | Approvals | Signature lines (PO, Dev Management, PM) |

**UI Features:**
- Table of Contents with anchor links
- Custom prompt for regeneration
- Tables properly rendered with headers
- Subsections with left-border styling

### 4.8 Status Report (`/status-report`) — TestFort Format
Daily/weekly testing status report:

**Input Form:**
- Report Date (date picker)
- Features Tested (comma-separated)
- Platforms Tested (comma-separated)
- Bug References (comma-separated)
- Next Steps (one per line)
- Custom Prompt

**Generated Report:**
```
2026-04-03

What has been done:
- Testing of the Login, Dashboard functionalities on Chrome and Safari devices;
- Proceeded with creating checklist and bug reports for investigated issues.

What's planned:
- Continue regression testing
- Verify bug fixes

Bugs:
New bugs raised: BUG-101, BUG-102
```

### 4.9 Test Metrics (`/test-metrics`) — TestFort Format
Three metric tables with device/browser breakdown:

**1. Coverage Completion Table**
| Device | Overall # checks | Remaining | Percentage |
|--------|-----------------|-----------|------------|
| Chrome Windows | 42 | 42 | 0% |
| Safari macOS | 42 | 42 | 0% |

**2. Test Execution Summary by Platforms**
| Device | Pass | Fail | Pass but | Blocked |
|--------|------|------|----------|---------|
| Chrome | 0 | 0 | 0 | 0 |

**3. Issues Summary** (4 sub-tables)
- Issues by Platform (Web, Desktop, Mobile, All)
- Defect Type (Functional, UI, UX, Enhancement)
- Status for dev (Open, By design, Reopen, Fixed, Need more info, Closed, Duplicate)
- Defect Severity (Blocker, Critical, Major, Minor, Low)

**Input:** comma-separated device list (configurable)

### 4.10 Techniques (`/techniques`)
Context-aware test design technique recommendations:

**8 ISTQB Techniques:**
1. Equivalence Partitioning
2. Boundary Value Analysis
3. Decision Table Testing
4. State Transition Testing
5. Pairwise Testing
6. Error Guessing
7. Exploratory Testing
8. Use Case Testing

**Each card shows:**
- Name and Category (ISTQB FL / ISTQB AL)
- Description
- When to Use
- Example
- Why for Your Project (contextual reasoning based on keywords in stories)
- Relevance badge (Required / Recommended)

**Logic:** EP and BVA are always Required. Others are conditionally recommended based on story keywords (e.g., "login" triggers State Transition, "payment" triggers Decision Table).

### 4.11 Recommendations (`/recommendations`)
Three-tab page with domain/platform-aware recommendations:

**Tab 1: Testing Types** — 12 types with priority reasoning
- Functional, UI/UX, API, Security, Performance, Compatibility, Accessibility, Localization, Mobile-Specific, Database, Usability, Regression
- Each with: Required/Recommended priority, contextual reasoning (e.g., "Critical for the fintech domain.")

**Tab 2: Focus Areas & Edge Cases**
- Critical Modules per domain (e.g., FinTech: "Transaction Processing — Core business logic, directly impacts revenue")
- Edge Cases & Negative Scenarios per domain (e.g., "Concurrent transactions modifying the same account balance")
- Platform: What to Test (e.g., Web: "Cross-browser compatibility", Mobile: "Offline mode")

**Tab 3: Standards**
- ISTQB, ISO 25010, OWASP Top 10, WCAG 2.1/2.2, GDPR, PCI DSS, HIPAA, SOC 2, COPPA
- Each with full name, relevance, reference URL
- Filtered by domain + features + target markets

### 4.12 Tools (`/tools`)
Budget-aware tool recommendations:

**Tab 1: Testing Tools** — 30+ tools across 8 categories:
| Category | Examples |
|----------|----------|
| Test Management | TestRail, Zephyr, qTest, TestLink |
| Bug Tracking | Jira, Bugzilla, Mantis, YouTrack |
| API Testing | Postman, Insomnia, SoapUI |
| Browser Testing | BrowserStack, Sauce Labs, LambdaTest |
| Performance | JMeter, k6, Gatling, LoadRunner |
| Security | OWASP ZAP, Burp Suite, Nessus |
| Accessibility | axe, WAVE, Lighthouse |
| Mobile | Appium, XCUITest, Espresso |

Each tool: name, type (Free/Commercial/Open Source/Freemium), best_for, reasoning

**Tab 2: No-Code Automation** — 6 tools:
Testim, Katalon, mabl, Leapwork, Reflect, Applitools

**Budget filtering:** Free Only shows only free/open-source tools. Enterprise shows all.

### 4.13 Guide (`/guide`)
Step-by-step user guide with 7 workflow steps and FAQ section.

### 4.14 Export (`/export/<format>`)
Export all generated artifacts:

| Format | Content | Description |
|--------|---------|-------------|
| Markdown (.md) | Full doc | Stories + Test Cases + Checklist + Traceability + Techniques + Types + Standards |
| HTML (.html) | Standalone | Same content with embedded CSS, print-friendly, colored badges |
| CSV - Test Cases | TC table | 13-column spreadsheet |
| CSV - Checklist | CL table | 7-column spreadsheet |
| XLSX - Test Cases | TC workbook | Formatted Excel: colored headers, priority/status colors, borders, freeze pane |
| XLSX - Checklist | CL workbook | Formatted Excel: same styling approach |

---

## 5. Knowledge Base

The framework's intelligence is powered by a comprehensive knowledge base (`engine/knowledge_base.py`) containing:

| Data Set | Count | Examples |
|----------|-------|---------|
| Test Design Techniques | 8 | EP, BVA, Decision Table, State Transition, Pairwise, Error Guessing, Exploratory, Use Case |
| Testing Types | 12 | Functional, Security, Performance, Compatibility, Accessibility, etc. |
| Testing Tools | 30+ | TestRail, Jira, Postman, JMeter, OWASP ZAP, etc. |
| No-Code Tools | 6 | Testim, Katalon, mabl, Leapwork, Reflect, Applitools |
| Domains | 8 | E-Commerce, FinTech, Healthcare, SaaS, EdTech, Social Media, IoT, Other |
| Platforms | 5 | Web, Mobile Native, Hybrid/PWA, Desktop, API/Backend |
| Standards | 10+ | ISTQB, ISO 25010, OWASP, WCAG, GDPR, PCI DSS, HIPAA, SOC 2, COPPA |

Each domain includes:
- **Critical Modules** (module name + why it's critical)
- **Key Testing Types** per domain
- **Edge Cases & Negative Scenarios**
- **Applicable Standards**

Each platform includes:
- **Must-Test Items** (what to verify)
- **Recommended Tools**

---

## 6. Advisor Engine

The `engine/advisor.py` module contains 6 recommendation functions:

| Function | Input | Output |
|----------|-------|--------|
| `recommend_techniques()` | ProjectContext + Stories | Filtered techniques with relevance reasoning |
| `recommend_testing_types()` | ProjectContext | Testing types with priority (Required/Recommended) |
| `recommend_tools()` | ProjectContext | Tools filtered by budget and platform |
| `recommend_nocode()` | ProjectContext | No-code automation tools for the platform |
| `recommend_focus_areas()` | ProjectContext | Critical modules, edge cases, platform checks |
| `recommend_standards()` | ProjectContext | Applicable standards by domain/market/features |

**Priority Logic:**
- Required: Domain-critical types (e.g., Security for FinTech, HIPAA for Healthcare)
- Recommended: Universally useful or conditionally relevant types

**Context Signals Used:**
- Domain -> critical modules, key types, edge cases, standards
- Platform -> must-test items, recommended tools
- Features (API, Auth, Payments, etc.) -> additional types and techniques
- Budget -> tool filtering (free/low/medium/enterprise)
- Target Markets (EU, US, UA) -> GDPR, WCAG, COPPA

---

## 7. Internationalization (i18n)

- **EN/UA language toggle** in the sidebar
- 180+ translation keys covering all UI elements
- Language stored in session, switchable via `?lang=en` / `?lang=ua`
- All generated content (knowledge base, test cases, recommendations) is in English
- UI labels toggle between English and Ukrainian

---

## 8. Project Persistence

- **Save:** Session data (setup + requirements + stories) saved as JSON to `storage/` directory
- **Load:** Restore any saved project from the Dashboard
- **Delete:** Remove saved project permanently
- **Metadata:** project_name, domain, platform, saved_at, counts

---

## 9. File Structure

```
F:/Claude_AI_Project/
|-- app.py                          # Flask application (20 routes)
|-- requirements.txt                # Python dependencies
|-- engine/
|   |-- __init__.py
|   |-- advisor.py                  # 6 recommendation functions
|   |-- exporter.py                 # MD, HTML, CSV, XLSX export
|   |-- file_parser.py             # 10+ format parser
|   |-- i18n.py                    # EN/UA translations (180+ keys)
|   |-- knowledge_base.py          # 7 data dictionaries
|   |-- status_report_generator.py # Status report builder
|   |-- test_metrics_generator.py  # Metrics tables builder
|   |-- test_plan_generator.py     # 13-section test plan
|   |-- testcase_generator.py      # Test case + checklist generator
|   |-- user_story_generator.py    # Requirements -> User Stories
|-- templates/
|   |-- base.html                  # Master layout + sidebar
|   |-- index.html                 # Dashboard
|   |-- setup.html                 # Project setup form
|   |-- requirements.html          # Requirements input
|   |-- user_stories.html          # Generated stories display
|   |-- input_stories.html         # Direct story input
|   |-- test_cases.html            # TestFort test cases
|   |-- checklist.html             # TestFort checklist
|   |-- test_plan.html             # 13-section test plan
|   |-- status_report.html         # Status report form + output
|   |-- test_metrics.html          # Metrics tables (3 tabs)
|   |-- techniques.html            # Test design techniques
|   |-- recommendations.html       # Types, focus, standards
|   |-- tools.html                 # Tool recommendations
|   |-- guide.html                 # Step-by-step guide
|-- static/
|   |-- css/style.css              # Complete stylesheet (~800 lines)
|   |-- js/app.js                  # Tabs, filters, drag-drop, status colors
|-- uploads/                       # Temp file storage
|-- storage/                       # Saved projects
```

---

## 10. Running the Application

```bash
# Install dependencies
pip install -r requirements.txt

# Start the server
python app.py

# Open in browser
http://localhost:5000
```

---

## 11. What QA Forge Can Do (Summary)

1. **Parse requirements** from 10+ file formats and convert to structured User Stories
2. **Generate test cases** with TestFort IDs (SC1_001), grouped by section, covering positive/negative/edge/security scenarios
3. **Generate checklists** with section-prefixed IDs (AUTH_001, PAY_002) for rapid execution
4. **Create a 13-section test plan** following IEEE-829 / TestFort template
5. **Build status reports** in TestFort daily/weekly format
6. **Generate test metrics** tables (coverage, execution, issues breakdown)
7. **Recommend test design techniques** based on story keywords (ISTQB-aligned)
8. **Recommend testing types** based on domain and platform
9. **Recommend tools** filtered by budget and platform
10. **Recommend standards** based on domain, markets, and features
11. **Export documentation** in Markdown, HTML, CSV, and formatted Excel
12. **Track test status** with interactive dropdowns on test cases and checklist
13. **Save/load projects** for later use
14. **Support EN/UA** language toggle for all UI elements

---

*Generated for QA Forge v2 — April 2026*
