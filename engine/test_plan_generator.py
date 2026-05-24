"""
TestFortge — Test Plan Generator (TestFort Template)

Generates a structured Test Plan with 13 sections following the TestFort template:
  1. Test Plan Identifier
  2. References
  3. Introduction (Objectives, Team Members)
  4. Scope (In Scope / Out of Scope)
  5. Assumptions / Risks
  6. Features to be Tested
  7. Features NOT to be Tested
  8. Test Approach / Strategy
  9. Item Pass/Fail Criteria
  10. Environmental Needs
  11. Staffing and Training Needs
  12. Milestones / Deliverables
  13. Approvals
"""

from dataclasses import dataclass, field
from datetime import date
from .advisor import ProjectContext
from .knowledge_base import DOMAIN_FOCUS, PLATFORM_SPECIFICS, TESTING_TYPES


@dataclass
class TestPlanSection:
    number: str
    title: str
    content: str
    subsections: list[dict] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)


@dataclass
class TestPlan:
    project_name: str
    version: str
    date: str
    sections: list[TestPlanSection] = field(default_factory=list)


def generate_test_plan(ctx: ProjectContext, features: list[str] = None,
                       custom_prompt: str = "",
                       raw_lines: list[str] | None = None,
                       file_texts: list[str] | None = None,
                       site_analysis=None) -> TestPlan:
    """Generate a full Test Plan based on project context and TestFort template.

    When ``raw_lines`` / ``file_texts`` / ``site_analysis`` are supplied
    (e.g. by ``routes/test_plan.py`` after parsing the input block) the
    Features (Section 6) and In-Scope (Section 4.1) lists are enriched
    with site-specific data instead of falling back to domain defaults.
    All three parameters are optional — when ``None`` the generator
    behaves exactly as before (backward-compat with existing callers).
    """
    if features is None:
        features = []

    plan = TestPlan(
        project_name=ctx.project_name,
        version="1.0",
        date=date.today().strftime("%d.%m.%Y"),
    )

    domain_info = DOMAIN_FOCUS.get(ctx.domain, DOMAIN_FOCUS["other"])
    platform_info = PLATFORM_SPECIFICS.get(ctx.platform, PLATFORM_SPECIFICS["web"])
    sys_name = ctx.project_name

    # ── Section 1: Test Plan Identifier ──
    plan.sections.append(TestPlanSection(
        number="1",
        title="Test Plan Identifier",
        content=f"{sys_name}_v{plan.version}_{plan.date}",
    ))

    # ── Section 2: References ──
    plan.sections.append(TestPlanSection(
        number="2",
        title="References",
        content="List of all documents that support this test plan, refers to the actual version/release number of the document as stored in the configuration management system.",
        subsections=[
            {"title": "2.1 Project Plan", "content": "(version, link)."},
            {"title": "2.2 Requirements Specifications", "content": "(version, link)."},
        ],
    ))

    # ── Section 3: Introduction ──
    objectives = (
        f"The main objective of testing is: to assure that the {sys_name} system meets the full requirements, "
        f"including quality requirements (functional and non-functional requirements) and to fit metrics for each "
        f"quality requirement and satisfies the use case scenarios and to maintain the quality of the product. "
        f"At the end of the project development cycle, the users should find that the project has met or exceeded "
        f"all of their expectations as detailed in the requirements.\n\n"
        f"Any changes, additions, or deletions to the requirements document, Functional Specification, or Design "
        f"Specification will be documented and tested at the highest level of quality allowed within the remaining "
        f"time of the project and within the ability of the test team.\n\n"
        f"The secondary objectives of testing will be: to identify all issues and associated risks, to communicate "
        f"all known issues to the project team, and to ensure that all issues are addressed in an appropriately before release."
    )

    team_roles = [
        {"role": "Project Manager", "responsibilities": "1. To act as primary contact for development and QA team.\n2. Responsible for Project schedule and the overall success of the project.", "staff": ""},
        {"role": "QA Lead", "responsibilities": "1. To participate in the project plan creation/update process.\n2. Planning and organization of the test process for the release.\n3. To coordinate with QA analysts/engineers on any issues/problems encountered during testing.\n4. To report progress on work assignments to the PM.", "staff": ""},
        {"role": "QA", "responsibilities": "1. To understand the requirements.\n2. To write and execute the test cases.\n3. To prepare RTM.\n4. To review the test cases, RTM.\n5. Defect reporting and tracking.\n6. Defect rechecking and regression testing.\n7. To prepare the test data.\n8. To coordinate with QA Lead for any issues or problems encountered during test preparation/execution/defect handling.", "staff": ""},
    ]

    plan.sections.append(TestPlanSection(
        number="3",
        title="Introduction",
        content="The Test Plan has been created to communicate the test approach for team members. It includes the objectives, scope, schedule, risks, and approach. This document will identify clearly what the test deliverables will be and what is deemed in and out of scope.",
        subsections=[
            {"title": "3.1 Objectives", "content": objectives},
            {"title": "3.2 Team Members", "content": ""},
        ],
        tables=[{"title": "Team Members", "headers": ["Role", "Responsibilities", "Staff Member"], "rows": [[r["role"], r["responsibilities"], r["staff"]] for r in team_roles]}],
    ))

    # ── Section 4: Scope ──
    in_scope_items = [
        f"Testing of all functional, application performance, security and use cases requirements listed in the Use Case document.",
        f"Quality requirements and fit metrics of the {sys_name} system.",
        f"End-to-end testing and testing of interfaces of all systems that interact with the {sys_name} system.",
    ]

    # Enrichment from URL crawl — surface concrete navigation/forms so
    # the In-Scope section reflects the actual site rather than reading
    # like a generic template.
    if site_analysis is not None:
        nav_items = list(getattr(site_analysis, "nav_items", []) or [])[:8]
        if nav_items:
            in_scope_items.append(
                "Navigation flows covering: " + ", ".join(nav_items)
            )
        # Collect form names across all crawled pages
        form_names: list[str] = []
        for page in getattr(site_analysis, "pages", []) or []:
            for form in getattr(page, "forms", None) or []:
                fields_desc = ", ".join(
                    (f.get("name") or f.get("placeholder")
                     or f.get("type", "")).strip()
                    for f in form.get("fields", [])
                    if f.get("type") not in ("hidden", "submit", "button")
                )
                if fields_desc:
                    form_names.append(fields_desc)
        if form_names:
            in_scope_items.append(
                "Form validation across: " + "; ".join(form_names[:5])
            )
    out_scope_items = [
        f"Functional requirements testing for systems outside {sys_name} system.",
        "Testing of Business SOPs, disaster recovery, and Business Continuity Plan.",
    ]

    plan.sections.append(TestPlanSection(
        number="4",
        title="Scope",
        content="",
        subsections=[
            {"title": "4.1 In Scope", "content": f"The {sys_name} Test Plan defines the unit, integration, system, regression, and Client Acceptance testing approach.\n\nThe test scope includes the following:\n" + "\n".join(f"- {item}" for item in in_scope_items)},
            {"title": "4.2 Out of Scope", "content": f"The following points are considered out of scope for the Test Plan of the {sys_name} system and testing scope:\n" + "\n".join(f"- {item}" for item in out_scope_items)},
        ],
    ))

    # ── Section 5: Assumptions / Risks ──
    risks = [
        {"id": "1.0", "risk": "Scope Creep - as testers become more familiar with the tool, they will request more functionality.", "severity": "High", "trigger": "Delays in the implementation date", "mitigation": "Each iteration, functionality will be closely monitored. Priorities will be set and discussed by stakeholders. All additional functionality will be logged as Change Requests."},
        {"id": "2.0", "risk": "Changes to the functionality may require additional time because already written test cases may become irrelevant.", "severity": "Medium", "trigger": "Big changes in the requirements", "mitigation": "To estimate additional functionality separately and add it to the Test Plan with all artefacts (additional resources will be needed)."},
        {"id": "3.0", "risk": "Weekly delivery is not possible because the developer/QA engineer should urgently work on another project or got sick.", "severity": "Medium", "trigger": "The product did not get delivered on schedule", "mitigation": "Replacement of the team members should be included in the plan before the project starts or immediately after its launch."},
    ]

    plan.sections.append(TestPlanSection(
        number="5",
        title="Assumptions / Risks",
        content="",
        subsections=[
            {"title": "5.1 Assumptions", "content": "This section lists assumptions that are made specific to this project:\n\n- Business analyst and development team members will be available to provide support, training and defect resolution to the QA team."},
            {"title": "5.2 Risks", "content": "The following risks have been identified and the appropriate action identified to mitigate their impact on the project."},
        ],
        tables=[{"title": "Risk Register", "headers": ["#", "Risk", "Severity", "Trigger", "Mitigation Plan"], "rows": [[r["id"], r["risk"], r["severity"], r["trigger"], r["mitigation"]] for r in risks]}],
    ))

    # ── Section 6: Features to be Tested ──
    # Build features from the strongest source available, in priority
    # order: explicit features list (from existing TCs) → crawler-detected
    # features → bullet lines from text input → uploaded-file bullets →
    # domain defaults. Each source contributes uniquely; we de-dupe while
    # preserving order so domain modules don't crowd out site specifics.
    features_list: list[str] = []
    seen: set[str] = set()

    def _add(item: str, tag: str = ""):
        item = (item or "").strip()
        if not item:
            return
        key = item.lower()
        if key in seen:
            return
        seen.add(key)
        features_list.append(f"{item} {tag}".strip() if tag else item)

    for f in features or []:
        _add(f)

    if site_analysis is not None:
        for f in getattr(site_analysis, "features_detected", []) or []:
            _add(f, "(from URL crawl)")
        for nav in (getattr(site_analysis, "nav_items", []) or [])[:6]:
            _add(nav, "(from URL crawl)")

    if raw_lines:
        for ln in raw_lines:
            ln_stripped = (ln or "").strip(" \t-*•")
            if not ln_stripped or ln_stripped.startswith("http"):
                continue
            if len(features_list) >= 20:
                break
            _add(ln_stripped[:120], "(from input)")

    if file_texts:
        for txt in file_texts:
            for raw in (txt or "").splitlines():
                line_stripped = raw.strip(" \t-*•")
                if not line_stripped or len(line_stripped) < 4:
                    continue
                if len(features_list) >= 20:
                    break
                _add(line_stripped[:120], "(from attachment)")
            if len(features_list) >= 20:
                break

    if not features_list:
        features_list = [m["module"]
                         for m in domain_info.get("critical_modules", [])[:8]]

    plan.sections.append(TestPlanSection(
        number="6",
        title="Features to be Tested",
        content="Below, there is a list of the areas to be focused on during testing of the application.\n\n" + "\n".join(f"- {f}" for f in features_list),
    ))

    # ── Section 7: Features NOT to be Tested ──
    plan.sections.append(TestPlanSection(
        number="7",
        title="Features NOT to be Tested",
        content="This is a listing of what is NOT to be tested from both Users viewpoints of what the system does and a configuration management/version control view.\n\n- (To be defined based on project scope)",
    ))

    # ── Section 8: Test Approach (Strategy) ──
    testing_types_content = []
    type_keys = domain_info.get("key_testing_types", ["functional", "regression"])
    for tk in type_keys:
        tt = TESTING_TYPES.get(tk)
        if tt:
            testing_types_content.append(f"**{tt['name']}:** {tt['description']}")

    severity_table = [
        ["1.0", "Blocker", "This defect blocks a feature and doesn't allow to test/check the functionality."],
        ["2.0", "Critical", "It is a highly severe defect, it collapses the system. However, certain parts of the system remain functional."],
        ["3.0", "Major", "The failed function is unusable but there exists an acceptable alternative method to achieve the required results."],
        ["4.0", "Minor", "The defect that does not result in the termination and does not damage the usability of the system."],
        ["5.0", "Low", "It won't cause any major break-down of the system, mainly cosmetic issue."],
    ]

    plan.sections.append(TestPlanSection(
        number="8",
        title="Test Approach (Strategy)",
        content=(
            f"The project is using an agile approach, with {ctx.release_frequency} iterations. "
            f"At the end of each iteration, requirements identified for that iteration will be delivered and tested.\n\n"
            f"Test cases will be created during exploratory testing.\n"
            f"Team also must use experience-based testing and error guessing using skills and intuition, "
            f"along with the experience with similar applications or technologies."
        ),
        subsections=[
            {"title": "8.1 QA Role in the Test Process", "content": (
                "**Understanding Requirements:**\n"
                "Requirement specifications will be sent by the client. Understanding of requirements will be done by QA Engineer.\n\n"
                "**Preparing Test Cases:**\n"
                "QA Engineer will prepare the test cases based on exploratory testing. This will cover all scenarios for requirements.\n\n"
                "**Preparing Test Matrix:**\n"
                "QA Engineer will prepare a test matrix which maps test cases to respective requirement. This will ensure the coverage for requirements.\n\n"
                "**Reviewing Test Cases and Matrix:**\n"
                "Peer review will be conducted for test cases and test matrix by QA Lead. Any comments or suggestions on test cases and test coverage will be provided by the reviewer.\n\n"
                "**Executing Test Cases:**\n"
                "Test cases will be executed by respective QA Engineer based on designed scenarios, test cases, and test data. "
                "Test result (Pass/Fail/Pass but/Blocked) will be updated in the test case document.\n\n"
                "**Retesting and Regression Testing:**\n"
                "Retesting for fixed bugs will be done by respective QA Engineer once resolved by the developer."
            )},
            {"title": "8.2 Testing Types", "content": "\n\n".join(testing_types_content)},
            {"title": "8.3 Bug Severity Definition", "content": "The bug Severity levels will be defined as outlined in the following table below."},
            {"title": "8.4 Test Automation", "content": "Automated unit tests are part of the development process, but no automated functional tests are planned at this time."},
        ],
        tables=[{"title": "Bug Severity", "headers": ["Severity ID", "Severity", "Severity Description"], "rows": severity_table}],
    ))

    # ── Section 9: Pass/Fail Criteria ──
    criteria_table = [
        ["Function Testing", "All planned tests have been executed.\nAll identified defects have been addressed.\nBugs with Critical and Major priority are fixed."],
        ["GUI Testing", "Each page successfully verified to remain consistent with the benchmark version or within the acceptable standard."],
        ["Configuration Testing", "For each combination of the application and platform, transactions are successfully completed without failure."],
    ]
    plan.sections.append(TestPlanSection(
        number="9",
        title="Item Pass/Fail Criteria",
        content="",
        tables=[{"title": "Pass/Fail Criteria", "headers": ["Test Types", "Pass/Fail Criteria (Completion Criteria)"], "rows": criteria_table}],
    ))

    # ── Section 10: Environmental Needs ──
    env_items = platform_info.get("must_test", [])[:6]
    plan.sections.append(TestPlanSection(
        number="10",
        title="Environmental Needs",
        content="As testing Environment, it was decided to test the Application on:\n\n" + "\n".join(f"- {e}" for e in env_items) + "\n\nSpecial test tools that are required:\n- (To be defined based on project needs)",
    ))

    # ── Section 11: Staffing and Training Needs ──
    plan.sections.append(TestPlanSection(
        number="11",
        title="Staffing and Training Needs",
        content=(
            "In order to provide complete and proper testing, the following areas need to be addressed in terms of training.\n\n"
            "- Basic training for understanding system business logic and main functionality.\n"
            "- The tester will need to be trained on the tools used for the project.\n"
            "- The tester will need to be trained to use the selected bug tracking system and test case management tool."
        ),
    ))

    # ── Section 12: Milestones / Deliverables ──
    schedule_table = [
        ["Review Requirements documents", "", "", "1 d", ""],
        ["Creating the testing documentation", "", "", "2 d", ""],
        ["Functional testing + Compatibility testing", "", "", "TBD", ""],
        ["Bug reporting", "", "", "", ""],
        ["Deploy to QA test environment", "", "", "", ""],
        ["Bug rechecking", "", "", "", ""],
        ["Regression testing", "", "", "", ""],
        ["Resolution of final defects", "", "", "", ""],
    ]
    deliverables_table = [
        ["Test Plan", "Project Manager; QA Lead", ""],
        ["Checklist", "All team members", ""],
        ["Test Cases", "All team members", ""],
        ["Traceability Matrix", "Project Manager; QA Lead", ""],
        ["Bug reports", "All team members", ""],
        ["Test Metrics", "All team members", ""],
        ["Final Reports", "Project Manager; QA Lead", ""],
    ]

    plan.sections.append(TestPlanSection(
        number="12",
        title="Milestones / Deliverables",
        content="",
        subsections=[
            {"title": "12.1 Test Schedule", "content": ""},
            {"title": "12.2 Deliverables", "content": ""},
        ],
        tables=[
            {"title": "Test Schedule", "headers": ["Task Name", "Start", "Finish", "Effort", "Comments"], "rows": schedule_table},
            {"title": "Deliverables", "headers": ["Deliverable", "For", "Date / Milestone"], "rows": deliverables_table},
        ],
    ))

    # ── Section 13: Approvals ──
    plan.sections.append(TestPlanSection(
        number="13",
        title="Approvals",
        content=(
            "Product Owner ______________________________________________________\n\n"
            "Development Management ____________________________________________\n\n"
            "Project Manager ____________________________________________________"
        ),
    ))

    # Apply custom prompt modifications
    if custom_prompt:
        lower = custom_prompt.lower()

        # Add custom notes as a dedicated subsection to Introduction
        intro_section = plan.sections[2]  # Section 3: Introduction
        intro_section.subsections.append({
            "title": "3.3 Additional Notes",
            "content": custom_prompt,
        })

        # Adjust scope based on keywords
        scope_section = plan.sections[3]  # Section 4: Scope
        if any(kw in lower for kw in ["security", "penetration", "owasp"]):
            scope_section.subsections[0]["content"] += "\n- Security and penetration testing of all user-facing endpoints."
        if any(kw in lower for kw in ["performance", "load", "stress"]):
            scope_section.subsections[0]["content"] += "\n- Performance and load testing under expected peak traffic."
        if any(kw in lower for kw in ["api", "endpoint", "integration"]):
            scope_section.subsections[0]["content"] += "\n- API contract testing and integration verification."
        if any(kw in lower for kw in ["mobile", "ios", "android"]):
            scope_section.subsections[0]["content"] += "\n- Mobile platform testing (iOS/Android) across multiple device form factors."
        if any(kw in lower for kw in ["accessibility", "wcag", "a11y"]):
            scope_section.subsections[0]["content"] += "\n- Accessibility testing (WCAG 2.1 AA compliance)."

        # Add custom risks
        risks_section = plan.sections[4]  # Section 5: Assumptions / Risks
        if any(kw in lower for kw in ["tight deadline", "deadline", "urgent"]):
            if risks_section.tables:
                risks_section.tables[0]["rows"].append(
                    ["4.0", "Tight deadline may reduce test coverage.",
                     "High", "Approaching release date",
                     "Prioritize critical path testing. Use risk-based test selection."]
                )
        if any(kw in lower for kw in ["new team", "inexperienced", "junior"]):
            if risks_section.tables:
                risks_section.tables[0]["rows"].append(
                    ["5.0", "Team members unfamiliar with the product domain.",
                     "Medium", "Misunderstanding of requirements",
                     "Provide product training sessions. Assign mentors for junior team members."]
                )

    return plan
