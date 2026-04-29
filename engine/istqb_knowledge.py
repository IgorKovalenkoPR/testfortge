"""
TestForTge — ISTQB CTFL v4.0.1 knowledge base for Tedgie.

This module distils the ISTQB Certified Tester Foundation Level
(syllabus v4.0.1, 2024-09-15) into a structured catalogue Tedgie
can consult when a user asks a testing-theory question.

Two consumers:

1. Rule-based path (``answer_topic`` + ``detect_topic``) — works
   without any LLM and returns canonical ISTQB definitions verbatim.
2. AI path (``istqb_persona_prompt``) — extends Tedgie's system
   prompt with an ISTQB-certified-consultant frame so Anthropic
   responses stay aligned with the syllabus terminology.

The wording of definitions follows the syllabus closely so a
Foundation-Level candidate could rely on Tedgie for revision.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# ── Canonical definitions ─────────────────────────────────────────

# 1. Errors / defects / failures / root causes (§1.2.3)
TERMS_GLOSSARY: dict[str, str] = {
    "error": (
        "An error (mistake) is a human action that produces an incorrect "
        "result. Errors are made by people for many reasons: time pressure, "
        "complexity of the work, processes, infrastructure or interactions, "
        "tiredness, or lack of training."
    ),
    "defect": (
        "A defect (also: fault, bug) is a flaw in a work product. Defects can "
        "live in documentation, source code, build files or any other supporting "
        "artefact. If executed, a defect in code may cause a failure."
    ),
    "failure": (
        "A failure is the externally-visible result of a defect being executed: "
        "the test object does not do what it should, or does something it "
        "shouldn't. Failures may also be caused by environmental conditions "
        "(radiation, EM fields) without a software defect."
    ),
    "root cause": (
        "A root cause is the fundamental reason a problem occurred — the "
        "situation that led to the error. Identified through root-cause "
        "analysis after a failure / defect to help prevent recurrence."
    ),
    "test case": (
        "A test case is a set of preconditions, inputs, actions, expected "
        "results, and postconditions developed to verify whether a test "
        "object satisfies a particular requirement, design or behaviour."
    ),
    "test condition": (
        "A test condition is an aspect of the test basis relevant to achieve "
        "a specific test objective. It answers 'what to test?' and is later "
        "elaborated into concrete test cases."
    ),
    "test basis": (
        "The test basis is the body of knowledge used as the basis for test "
        "analysis and design — requirements, user stories, design documents, "
        "previous test results, the test object itself, etc."
    ),
    "test object": (
        "The test object is the work product (component, system, requirement, "
        "design, code, etc.) being tested."
    ),
    "test objective": (
        "A test objective is the reason or purpose for designing and executing "
        "tests — for example: finding defects, evaluating work products, "
        "ensuring coverage, verifying compliance, building confidence."
    ),
    "testware": (
        "Testware is the set of artefacts produced during the test process: "
        "test plans, test cases, test data, scripts, defect reports, test "
        "logs, environment items, completion reports."
    ),
    "verification": (
        "Verification: checking that the system meets specified requirements "
        "(\"are we building it right?\"). It is one part of testing — the "
        "other being validation."
    ),
    "validation": (
        "Validation: checking that the system meets users' and stakeholders' "
        "needs in its operational environment (\"are we building the right "
        "thing?\")."
    ),
    "static testing": (
        "Static testing examines a work product without executing the code — "
        "via reviews and static analysis. It can find defects directly in "
        "the artefact and is cheaper to apply early in the SDLC."
    ),
    "dynamic testing": (
        "Dynamic testing involves executing the test object on the system "
        "under test. It can trigger failures caused by defects and is the "
        "foundation of all execution-based test techniques (see chapter 4)."
    ),
    "qa": (
        "Quality Assurance (QA) is a process-oriented, preventive approach: "
        "implement and improve processes so a good process produces good "
        "products. It applies to both development AND testing processes and "
        "is the responsibility of everyone on the project. Distinct from "
        "testing, which is product-oriented and corrective."
    ),
    "coverage": (
        "Coverage is the degree to which test items, requirements or code "
        "elements have been exercised by tests. Specific coverage measures "
        "(statement coverage, branch coverage, requirement coverage, etc.) "
        "make this measurable."
    ),
    "traceability": (
        "Traceability links test basis elements ↔ testware ↔ test results ↔ "
        "defects. It enables coverage evaluation, change-impact analysis, "
        "audits, and clearer reporting to stakeholders."
    ),
    "regression testing": (
        "Regression testing checks that previously-tested code still works "
        "after changes elsewhere — a new defect introduced by change is a "
        "regression. Highly automation-friendly because tests are repeated "
        "many times."
    ),
    "confirmation testing": (
        "Confirmation testing (re-testing) verifies that an originally-failing "
        "test now passes after the defect was fixed. Distinct from regression "
        "testing, which checks that the fix did not break other parts."
    ),
    "smoke testing": (
        "Smoke testing is a brief, broad, shallow run that confirms the "
        "build is stable enough for further testing. Failure of smoke "
        "blocks the rest of the test cycle."
    ),
    "test plan": (
        "A test plan describes the test objectives, scope, schedule, "
        "approach, resources, entry/exit criteria, risks and deliverables "
        "for a given test effort. Lives at the project / iteration / level "
        "depending on the SDLC."
    ),
    "entry criteria": (
        "Entry criteria are conditions that must be met before a test "
        "activity can begin (e.g. test environment ready, test data loaded, "
        "smoke build passes, requirements baseline frozen)."
    ),
    "exit criteria": (
        "Exit criteria are conditions that must be met to declare a test "
        "activity complete (e.g. test execution rate, defect-leakage targets, "
        "coverage reached, residual-risk threshold)."
    ),
    "risk": (
        "A risk is a potential event causing negative consequences. Risk "
        "level = likelihood × impact. Tested via risk-based testing, where "
        "test effort is prioritised by risk."
    ),
    "severity": (
        "Severity is the degree of impact a defect has on the test object — "
        "i.e. how badly the system is affected when the defect is exercised. "
        "ISTQB scale (illustrative): Critical / High / Medium / Low. "
        "Severity is technical and assessed by testers; it is independent of "
        "priority, which is a business decision."
    ),
    "priority": (
        "Priority is the urgency with which a defect should be fixed — a "
        "business decision driven by stakeholders, release plans and risk. "
        "ISTQB scale (illustrative): High / Medium / Low. A high-severity "
        "defect can have low priority (rare workflow) and vice-versa "
        "(low-severity but on the demo path)."
    ),
    "defect lifecycle": (
        "The defect lifecycle (defect workflow) is the sequence of states a "
        "defect report moves through after it is logged. Typical states: "
        "New / Open → Assigned → In progress → Fixed → Re-test (confirmation "
        "testing) → Closed, with branches Rejected / Deferred / Duplicate. "
        "Status transitions are enforced by the defect-management tool."
    ),
    "defect management": (
        "Defect management is the process of recognising, recording, "
        "classifying, investigating, fixing and closing defects. ISTQB-mandatory "
        "metadata for each report: id, title, severity, priority, status, "
        "reporter, found-in build, environment, steps to reproduce, expected "
        "vs actual result, attachments and references."
    ),
    "test scenario": (
        "A test scenario (also: test idea) is a high-level description of "
        "what to test — usually one user-flow or business case. A scenario "
        "is later decomposed into concrete test cases with steps, inputs and "
        "expected results. Often used in exploratory testing and BDD."
    ),
}


# 2. Seven testing principles (§1.3)
TESTING_PRINCIPLES: list[tuple[str, str]] = [
    ("Testing shows the presence, not the absence of defects",
     "Tests can demonstrate defects exist but cannot prove their absence."),
    ("Exhaustive testing is impossible",
     "Trying everything is infeasible except in trivial cases. Use techniques, "
     "prioritisation and risk-based testing to focus effort."),
    ("Early testing saves time and money",
     "Defects removed early avoid downstream cost. Both static and dynamic "
     "testing should start as early as possible in the SDLC."),
    ("Defects cluster together",
     "A small number of components hold most of the defects (Pareto). Predicted "
     "and observed clusters guide risk-based testing."),
    ("Tests wear out",
     "Repeating the same tests becomes ineffective at finding new defects. "
     "Maintain and refresh tests; an exception is automated regression where "
     "repetition is the point."),
    ("Testing is context dependent",
     "There is no universal approach. Test differently for safety-critical, "
     "agile-web, mainframe, embedded, etc."),
    ("Absence-of-defects fallacy",
     "Verifying every requirement and fixing every defect can still produce "
     "a system that does not meet user needs. Always validate too."),
]


# 3. Test process activities (§1.4.1)
TEST_PROCESS_ACTIVITIES: list[tuple[str, str]] = [
    ("Test planning",
     "Define test objectives and pick an approach within project constraints. "
     "Output: test plan, schedule, risk register, entry/exit criteria."),
    ("Test monitoring and control",
     "Monitor: ongoing comparison of actual progress vs. plan. Control: take "
     "actions needed to meet objectives. Output: progress reports, control "
     "directives."),
    ("Test analysis",
     "Analyse the test basis to identify testable features and define "
     "prioritised test conditions. Answers 'what to test?' in measurable "
     "coverage terms."),
    ("Test design",
     "Elaborate test conditions into concrete test cases and other testware. "
     "Identify coverage items, test data needs, environment & tool needs. "
     "Answers 'how to test?'."),
    ("Test implementation",
     "Build / acquire testware. Organise cases into procedures and suites. "
     "Create scripts (manual + automated). Set up and verify the test "
     "environment."),
    ("Test execution",
     "Run tests per the schedule. Compare actual vs. expected. Log results, "
     "analyse anomalies, raise defect reports."),
    ("Test completion",
     "At project / iteration milestones: archive testware, raise change "
     "requests for unresolved defects, capture lessons learned, write a "
     "test completion report."),
]


# 4. Test levels (§2.2.1)
TEST_LEVELS: list[tuple[str, str]] = [
    ("Component (Unit) Testing",
     "Tests individual components in isolation. Done usually by developers, "
     "using stubs/drivers/mocks. Test basis: detailed design, code, data model. "
     "Defects found: incorrect logic, off-by-one, exceptions."),
    ("Component Integration Testing",
     "Tests interactions and interfaces between components within a single "
     "system. Test basis: software architecture, sequence diagrams. Defects: "
     "data passing, interface contract violations."),
    ("System Testing",
     "Tests the complete, integrated system end-to-end. Test basis: system "
     "requirements, risk analysis, business processes. Validates that the "
     "product as a whole meets specified requirements."),
    ("System Integration Testing",
     "Tests the integration of the SUT with other systems / external services. "
     "Test basis: interface specs, message protocols. Defects: contract "
     "drift, timing, security at trust boundaries."),
    ("Acceptance Testing",
     "Establishes confidence that the system is fit for purpose for users / "
     "customers / regulators. Includes UAT, OAT (operations), contractual / "
     "regulatory acceptance, alpha and beta testing."),
]


# 5. Test types (§2.2.2)
TEST_TYPES: list[tuple[str, str]] = [
    ("Functional Testing",
     "Evaluates what the system does — behaviour against functional "
     "requirements. Asks: does the feature work correctly?"),
    ("Non-functional Testing",
     "Evaluates how well the system does it — performance, usability, "
     "security, reliability, maintainability, portability, compatibility."),
    ("Black-box Testing",
     "Tests derived from an external view of the test object — specifications "
     "and behaviour, without knowing the internal structure."),
    ("White-box Testing",
     "Tests derived from internal structure — source code, control flow, "
     "data flow. Measures statement / branch coverage."),
    ("Change-related Testing",
     "Confirmation testing (the original defect is fixed) and regression "
     "testing (no other functionality broke)."),
]


# 6. Test techniques (chapter 4)
TEST_TECHNIQUES: dict[str, str] = {
    "equivalence partitioning": (
        "Equivalence Partitioning (EP) divides input/output data into partitions "
        "where every value in a partition is expected to be processed the same way. "
        "Test ONE value per partition. Distinguish valid partitions (expected to "
        "be accepted) from invalid partitions (expected to be rejected). Coverage: "
        "every partition exercised by at least one test."
    ),
    "boundary value analysis": (
        "Boundary Value Analysis (BVA) tests the edges of equivalence partitions, "
        "where defects cluster. Two-value BVA: test the boundary and just-outside "
        "value. Three-value BVA: also test just-inside value. For numeric ranges, "
        "boundaries are min, max, min-1, max+1."
    ),
    "decision table testing": (
        "Decision Table Testing models combinations of conditions and the "
        "actions they trigger. Each column = a rule = one test. Coverage: "
        "every column at least once. Excellent for specs with many "
        "if/and/or rules (rate calculations, eligibility, business rules)."
    ),
    "state transition testing": (
        "State Transition Testing models the test object as states + transitions "
        "triggered by events. Coverage levels: all-states, all-transitions, "
        "all N-switches (sequences of N transitions). Strong fit for stateful "
        "UI / workflows / protocols."
    ),
    "statement coverage": (
        "Statement coverage = % of executable statements exercised by tests. "
        "100% statement coverage does NOT imply 100% branch coverage."
    ),
    "branch coverage": (
        "Branch coverage = % of decision outcomes (true/false branches) "
        "exercised. Stronger than statement coverage; 100% branch implies "
        "100% statement."
    ),
    "error guessing": (
        "Experience-based technique: an experienced tester anticipates likely "
        "defects from history (common mistakes for this kind of feature, this "
        "team, this domain) and writes targeted tests."
    ),
    "exploratory testing": (
        "Tests are designed, executed and interpreted in parallel during a "
        "time-boxed session, often guided by a charter. Best when specs are "
        "thin or when learning the product. Outputs include session notes "
        "and a list of new test ideas."
    ),
    "checklist-based testing": (
        "Test design is anchored in a checklist of conditions (taxonomies, "
        "personas, common-defect lists) rather than in derived test cases. "
        "Each item triggers one or more checks during execution."
    ),
    "atdd": (
        "Acceptance Test-driven Development: stakeholders + devs + testers "
        "agree on acceptance criteria BEFORE coding, then write executable "
        "acceptance tests. Drives a shared definition of done."
    ),
}


# 7. Static-testing reviews (§3)
REVIEW_TYPES: list[tuple[str, str]] = [
    ("Informal review",
     "No formal process; a colleague reads the work product and gives "
     "feedback. Cheap, low rigour."),
    ("Walkthrough",
     "The author leads peers through the work product step by step. Goals: "
     "find defects, share knowledge, build consensus. Defect log optional."),
    ("Technical review",
     "Peer review by technically-qualified reviewers, often with checklists. "
     "Goals: evaluate quality, raise alternatives, ensure consistency."),
    ("Inspection",
     "The most formal review. Defined roles, entry/exit criteria, metrics, "
     "follow-up. Designed to detect maximum defects with minimum cost."),
]


# 8. Test management (chapter 5)
TEST_MANAGEMENT: dict[str, str] = {
    "test plan": TERMS_GLOSSARY["test plan"],
    "entry criteria": TERMS_GLOSSARY["entry criteria"],
    "exit criteria": TERMS_GLOSSARY["exit criteria"],
    "risk-based testing": (
        "Risk-based testing prioritises test effort by product-risk level. "
        "Risk identification → analysis (likelihood × impact) → mitigation "
        "(more / earlier / deeper testing for high-risk areas). Constantly "
        "revisited as the project progresses."
    ),
    "defect report": (
        "A defect report describes an observed failure so it can be diagnosed "
        "and fixed. ISTQB-mandatory metadata: id, summary, severity, priority, "
        "status, reporter, found-in build, affects-version, environment, "
        "preconditions, steps to reproduce, expected vs actual result, "
        "attachments, references."
    ),
    "test estimation": (
        "Estimation predicts the test effort needed. Common techniques: "
        "expert judgement, three-point (PERT: optimistic/most-likely/pessimistic), "
        "wideband Delphi, defect-density extrapolation, story points, "
        "rate-based estimation (per TC, per feature)."
    ),
    "configuration management": (
        "Configuration management ensures testware and the test environment "
        "are in a known, traceable state — versioned testware, versioned test "
        "objects, controlled changes, unique IDs and references between defects, "
        "tests and requirements."
    ),
}


# 9. Test tools categories (chapter 6, abridged)
TOOL_CATEGORIES: list[tuple[str, str]] = [
    ("Test management tools",
     "Manage testware, runs, results, defects, traceability."),
    ("Static-testing tools",
     "Static analysers, review-support tools, complexity / style checkers."),
    ("Test-design and implementation tools",
     "Tools that derive tests from models / data / specifications, plus "
     "test-data generation tools."),
    ("Test-execution tools",
     "Drive test execution: test runners, capture-replay, GUI / API automation "
     "frameworks (e.g. Playwright, Selenium, Cypress)."),
    ("Performance-testing tools",
     "Load, stress, soak, spike testing tools."),
    ("DevOps / CI tools",
     "Pipelines that orchestrate static analysis, unit tests, integration, "
     "deployment and acceptance testing."),
    ("Collaboration & defect-tracking tools",
     "Defect management, communication, agile boards."),
]


# ── Rule-based intent detection ───────────────────────────────────

@dataclass
class IstqbAnswer:
    title: str
    body: str
    follow_up: list[str]


# Topic → list of trigger keywords (EN + UA). All matched lowercase,
# substring-based.
TOPIC_TRIGGERS: dict[str, tuple[str, ...]] = {
    "principles": (
        "seven principle", "7 principle", "testing principle",
        "principles of testing", "istqb principles",
        "principles", "test principle",
        "семи принцип", "сім принцип", "принципи тестування",
        "принципи тест",
    ),
    "process": (
        "test process", "test activities", "test lifecycle",
        "stages of testing", "test phases", "testing process",
        "test workflow", "test stages", "phases of testing",
        "процес тестування", "процес тесту", "етапи тестування",
        "фази тестування", "життєвий цикл тест",
    ),
    "levels": (
        "test level", "test levels", "testing levels", "testing level",
        "levels of testing", "level of testing",
        "unit test", "component test", "integration test",
        "system test", "acceptance test", "uat",
        "рівні тестування", "рівень тестування", "рівні тесту",
        "юніт", "компонентне тест", "інтеграційне", "системне",
        "приймальне",
    ),
    "types": (
        "test type", "test types", "testing type", "testing types",
        "type of testing", "types of testing", "kinds of testing",
        "functional vs non-functional", "non-functional test",
        "black-box", "white-box", "blackbox", "whitebox",
        "grey-box", "gray-box",
        "види тестування", "вид тестування", "типи тестування",
        "тип тестування", "тип тесту", "види тесту",
        "функціональне тестування", "нефункціональне",
    ),
    "techniques": (
        "test technique", "test techniques", "test design technique",
        "test design techniques", "test cases technique",
        "design technique", "design techniques", "test design",
        "техніка тестування", "техніки тестування",
        "техніки дизайну", "дизайн тест",
    ),
    "equivalence": (
        "equivalence partition", "equivalence class",
        "еквівалентн розбит", "еквівалентн клас",
    ),
    "boundary": (
        "boundary value", "boundary test", "boundary analysis", "bva",
        "граничн", "пограничн", "boundary",
    ),
    "decision_table": (
        "decision table", "таблиц рішен", "таблица решен",
    ),
    "state_transition": (
        "state transition", "state-based", "перехід стан",
        "stateful test",
    ),
    "exploratory": (
        "exploratory test", "дослідницьке тестування",
        "дослідниц тест",
    ),
    "regression": (
        "regression test", "регрес", "regression vs",
    ),
    "confirmation": (
        "confirmation test", "re-test", "retest",
        "підтверджуюч", "підтвердж тест",
    ),
    "smoke": (
        "smoke test", "smoke testing", "димне тестування",
    ),
    "static": (
        "static test", "static analys", "static review",
        "статичне тестування", "code review",
    ),
    "reviews": (
        "review type", "informal review", "walkthrough",
        "technical review", "inspection",
        "огляд", "тип огляд", "інспекція",
    ),
    "risk_based": (
        "risk based test", "risk-based test", "risk analysis",
        "ризик-орієнтоване", "ризик-базоване", "risk register",
    ),
    "test_plan": (
        "test plan", "тест-план", "тест план",
    ),
    "entry_exit": (
        "entry criteria", "exit criteria", "definition of done",
        "критерії входу", "критерії виходу",
    ),
    "estimation_theory": (
        "test estimation", "estimation technique", "pert estimation",
        "wideband delphi", "оцінка тестування",
    ),
    "errors_defects": (
        "error vs defect", "defect vs failure", "root cause",
        "error defect failure", "помилка дефект збій",
        "помилка vs дефект",
    ),
    "verification_validation": (
        "verification vs validation", "v&v",
        "verification and validation", "validation and verification",
        "верифікація валідація", "верифікація та валідація",
        "верифікація і валідація",
    ),
    "qa_vs_testing": (
        "qa vs testing", "quality assurance vs", "якa vs тестування",
        "qa is", "what is qa",
    ),
    "tools": (
        "test tools", "tool support", "test management tool",
        "інструменти тестування", "tools for testing",
    ),
    "shift_left": (
        "shift left", "shift-left", "shift left testing",
        "ранне тестирование",
    ),
    "devops": (
        "devops", "ci/cd test", "continuous testing",
    ),
    "atdd": (
        "atdd", "acceptance test driven", "acceptance-test-driven",
    ),
    "coverage": (
        "test coverage", "code coverage", "branch coverage",
        "statement coverage", "покриття",
    ),
    "istqb_general": (
        "istqb", "ctfl", "foundation level", "ictc",
        "сертифікація istqb", "сертификация istqb",
    ),
    "severity": (
        "severity", "defect severity", "bug severity",
        "severity vs priority", "severity scale",
        "критичність", "ступінь дефекту", "критичність дефекту",
    ),
    "priority": (
        "priority", "defect priority", "bug priority",
        "priority vs severity", "priority scale",
        "пріоритет", "пріоритет дефекту", "пріоритет багу",
    ),
    "defect_lifecycle": (
        "defect lifecycle", "defect life cycle", "defect status",
        "bug lifecycle", "bug life cycle", "defect workflow",
        "defect states", "bug states", "defect flow",
        "життєвий цикл дефекту", "життєвий цикл багу",
        "статус дефекту", "статуси дефекту", "статуси багу",
    ),
    "defect_management": (
        "defect management", "defect tracking", "bug tracking",
        "defect handling", "bug management", "defect process",
        "bug report metadata", "what goes into a bug report",
        "управління дефектами", "управління багами",
        "трекінг дефектів", "трекінг багів",
    ),
    "test_scenario": (
        "test scenario", "test idea", "scenario vs test case",
        "test case vs test scenario",
        "тестовий сценарій", "сценарій тестування",
    ),
}


# ── Glossary lookup (definitional questions) ─────────────────────
# Triggered by message patterns like "what is a bug?", "що таке дефект",
# "define test case", "explain failure". The lookup runs *before*
# detect_topic / bug-form classification so an ISTQB definition is
# always preferred over a more conversational intent.

# UA + EN aliases for each TERMS_GLOSSARY key. Lowercased substrings.
GLOSSARY_ALIASES: dict[str, tuple[str, ...]] = {
    "error": ("error", "mistake", "помилка"),
    "defect": ("defect", "fault", "bug", "дефект", "баг", "помилка коду"),
    "failure": ("failure", "збій", "відмова"),
    "root cause": ("root cause", "корінна причина", "першопричина"),
    "test case": ("test case", "тест кейс", "тест-кейс", "тесткейс"),
    "test condition": ("test condition", "тестова умова", "умова тестування"),
    "test basis": ("test basis", "тестова база", "база тестування"),
    "test object": ("test object", "об'єкт тестування", "обʼєкт тестування"),
    "test objective": ("test objective", "мета тестування", "ціль тестування"),
    "testware": ("testware", "тестваре", "тестовий артефакт"),
    "verification": ("verification", "верифікація"),
    "validation": ("validation", "валідація"),
    "static testing": ("static testing", "статичне тестування"),
    "dynamic testing": ("dynamic testing", "динамічне тестування"),
    "qa": ("quality assurance", "qa", "забезпечення якості"),
    "coverage": ("coverage", "покриття"),
    "traceability": ("traceability", "трасування", "трасуванність"),
    "regression testing": ("regression testing", "регресійне тестування",
                           "регресія"),
    "confirmation testing": ("confirmation testing", "re-testing",
                              "підтверджуюче тестування"),
    "smoke testing": ("smoke testing", "smoke", "димне тестування"),
    "test plan": ("test plan", "тест-план", "тест план"),
    "entry criteria": ("entry criteria", "критерії входу"),
    "exit criteria": ("exit criteria", "критерії виходу"),
    "risk": ("risk", "ризик"),
    "severity": ("severity", "критичність", "ступінь дефекту"),
    "priority": ("priority", "пріоритет", "пріоритет дефекту"),
    "defect lifecycle": ("defect lifecycle", "defect life cycle",
                          "bug lifecycle", "defect status",
                          "життєвий цикл дефекту", "статус дефекту"),
    "defect management": ("defect management", "defect tracking",
                           "bug tracking", "defect handling",
                           "управління дефектами"),
    "test scenario": ("test scenario", "test idea",
                       "тестовий сценарій", "сценарій тестування"),
}

# Phrases that signal a definitional intent (not an action). Substring
# match — must appear before / around a glossary alias.
_DEFINE_INTENT_EN: tuple[str, ...] = (
    "what is", "what's", "whats", "define", "definition of",
    "meaning of", "explain ", "explanation of", "tell me about",
    "describe ",
)
_DEFINE_INTENT_UA: tuple[str, ...] = (
    "що таке", "що це", "що значить", "поясни", "розкажи про",
    "розкажи що таке", "значення", "визначення",
)


def detect_glossary_term(message: str) -> str | None:
    """Return the matched TERMS_GLOSSARY key if *message* asks for a
    definition, or None.

    Two-stage detector:
      1. The message must contain at least one definitional cue
         ("what is", "що таке", "define", "поясни", ...).
      2. After stripping that cue, the message must mention an alias
         of a known glossary term.

    Mention-only sentences (e.g. "found a bug") deliberately fall
    through — they are routed by the bug-form classifier instead.
    """
    low = (message or "").strip().lower()
    if not low:
        return None
    has_define_cue = any(cue in low for cue in _DEFINE_INTENT_EN) or \
                     any(cue in low for cue in _DEFINE_INTENT_UA)
    if not has_define_cue:
        return None
    # Pick the longest matching alias to avoid 'bug' winning over
    # 'defect' / 'fault' when both appear (rare but cheap to handle).
    best_term: str | None = None
    best_len = 0
    for term, aliases in GLOSSARY_ALIASES.items():
        for alias in aliases:
            if alias in low and len(alias) > best_len:
                best_term = term
                best_len = len(alias)
    return best_term


# Section reference for each term — quoted in the answer footer so
# students studying for CTFL can jump straight to the syllabus.
_GLOSSARY_REFS: dict[str, str] = {
    "error": "ISTQB CTFL §1.2.3",
    "defect": "ISTQB CTFL §1.2.3",
    "failure": "ISTQB CTFL §1.2.3",
    "root cause": "ISTQB CTFL §1.2.3",
    "test case": "ISTQB CTFL §1.4 / §4",
    "test condition": "ISTQB CTFL §4.1",
    "test basis": "ISTQB CTFL §1.4.1",
    "test object": "ISTQB CTFL §1.1",
    "test objective": "ISTQB CTFL §1.1",
    "testware": "ISTQB CTFL §1.4 / §5",
    "verification": "ISTQB CTFL §1.1.2",
    "validation": "ISTQB CTFL §1.1.2",
    "static testing": "ISTQB CTFL §3.1",
    "dynamic testing": "ISTQB CTFL §1.1 / §4",
    "qa": "ISTQB CTFL §1.1.1",
    "coverage": "ISTQB CTFL §4 / §5",
    "traceability": "ISTQB CTFL §5.3",
    "regression testing": "ISTQB CTFL §2.2.3",
    "confirmation testing": "ISTQB CTFL §2.2.3",
    "smoke testing": "ISTQB CTFL §2.2.2",
    "test plan": "ISTQB CTFL §5.1.1",
    "entry criteria": "ISTQB CTFL §1.4.4",
    "exit criteria": "ISTQB CTFL §1.4.4",
    "risk": "ISTQB CTFL §5.4",
    "severity": "ISTQB CTFL §5.5",
    "priority": "ISTQB CTFL §5.5",
    "defect lifecycle": "ISTQB CTFL §5.5",
    "defect management": "ISTQB CTFL §5.5",
    "test scenario": "ISTQB CTFL §4",
}


def answer_glossary_term(term: str, lang: str = "en") -> "IstqbAnswer | None":
    """Render a definitional answer pulling verbatim from TERMS_GLOSSARY.

    The follow-up suggestions intentionally point at the closest related
    topic so users can drill deeper without retyping a query."""
    body = TERMS_GLOSSARY.get(term)
    if not body:
        return None
    ref = _GLOSSARY_REFS.get(term, "ISTQB CTFL")
    title_en = f"{term.title()} — {ref}"
    follow = {
        "defect": ["What is a failure?",
                   "What is the difference between error, defect and failure?",
                   "Show defect lifecycle"],
        "failure": ["What is a defect?",
                    "Show error vs defect vs failure",
                    "Show seven testing principles"],
        "error": ["What is a defect?",
                  "Root-cause analysis",
                  "Show seven testing principles"],
        "test case": ["What is a test condition?",
                      "Show test design techniques",
                      "Show coverage"],
        "verification": ["What is validation?",
                         "Verification vs validation"],
        "validation": ["What is verification?",
                       "Verification vs validation"],
        "regression testing": ["What is confirmation testing?",
                                "Show test types"],
        "confirmation testing": ["What is regression testing?",
                                  "Show test types"],
        "qa": ["QA vs testing",
                "What is dynamic testing?"],
    }.get(term, ["Show seven testing principles", "Show test process activities"])
    return IstqbAnswer(title=title_en, body=body, follow_up=follow)


def detect_topic(message: str) -> str | None:
    """Return the matching ISTQB topic key for *message* or None."""
    low = (message or "").lower()
    for topic, keys in TOPIC_TRIGGERS.items():
        if any(k in low for k in keys):
            return topic
    return None


# ── Topic → answer renderer ───────────────────────────────────────

def _bullets(items: list[tuple[str, str]]) -> str:
    return "\n".join(f"• **{name}** — {desc}" for name, desc in items)


def _numbered(items: list[tuple[str, str]]) -> str:
    return "\n".join(f"{i}. **{name}** — {desc}"
                     for i, (name, desc) in enumerate(items, 1))


def answer_topic(topic: str, lang: str = "en") -> IstqbAnswer | None:
    """Render the canonical ISTQB answer for a topic. Lang only affects
    the framing language; definitions stay in English (ISTQB syllabus
    is the authoritative source) so candidates studying for the exam
    receive verbatim wording."""
    if topic == "principles":
        return IstqbAnswer(
            title="The seven testing principles (ISTQB CTFL §1.3)",
            body=_numbered(TESTING_PRINCIPLES),
            follow_up=["Why is exhaustive testing impossible?",
                       "What does 'defects cluster together' mean?",
                       "Show test process activities"],
        )
    if topic == "process":
        return IstqbAnswer(
            title="ISTQB test process activities (§1.4.1)",
            body=_bullets(TEST_PROCESS_ACTIVITIES),
            follow_up=["What is a test plan?",
                       "What goes into a test completion report?",
                       "Show entry / exit criteria"],
        )
    if topic == "levels":
        return IstqbAnswer(
            title="Test levels (ISTQB CTFL §2.2.1)",
            body=_bullets(TEST_LEVELS),
            follow_up=["Difference between integration and system testing?",
                       "What is acceptance testing?",
                       "Show test types"],
        )
    if topic == "types":
        return IstqbAnswer(
            title="Test types (ISTQB CTFL §2.2.2)",
            body=_bullets(TEST_TYPES),
            follow_up=["Functional vs non-functional?",
                       "Black-box vs white-box?",
                       "Confirmation vs regression?"],
        )
    if topic == "techniques":
        return IstqbAnswer(
            title="ISTQB test design techniques (chapter 4)",
            body=("Black-box: equivalence partitioning, boundary value "
                  "analysis, decision table testing, state transition "
                  "testing.\n"
                  "White-box: statement coverage, branch coverage.\n"
                  "Experience-based: error guessing, exploratory testing, "
                  "checklist-based testing.\n"
                  "Collaboration-based: collaborative user-story writing, "
                  "acceptance criteria, ATDD."),
            follow_up=["Show equivalence partitioning",
                       "Show boundary value analysis",
                       "Show decision table testing"],
        )
    if topic == "equivalence":
        return IstqbAnswer(
            title="Equivalence Partitioning (§4.2.1)",
            body=TEST_TECHNIQUES["equivalence partitioning"],
            follow_up=["Show boundary value analysis",
                       "When to use decision tables instead?"],
        )
    if topic == "boundary":
        return IstqbAnswer(
            title="Boundary Value Analysis (§4.2.2)",
            body=TEST_TECHNIQUES["boundary value analysis"],
            follow_up=["Show equivalence partitioning",
                       "Two-value vs three-value BVA?"],
        )
    if topic == "decision_table":
        return IstqbAnswer(
            title="Decision Table Testing (§4.2.3)",
            body=TEST_TECHNIQUES["decision table testing"],
            follow_up=["Show state transition testing",
                       "When to use vs equivalence partitioning?"],
        )
    if topic == "state_transition":
        return IstqbAnswer(
            title="State Transition Testing (§4.2.4)",
            body=TEST_TECHNIQUES["state transition testing"],
            follow_up=["Show decision tables",
                       "What's an N-switch coverage?"],
        )
    if topic == "exploratory":
        return IstqbAnswer(
            title="Exploratory Testing (§4.4.2)",
            body=TEST_TECHNIQUES["exploratory testing"],
            follow_up=["Difference from scripted testing?",
                       "What is a test charter?"],
        )
    if topic == "regression":
        return IstqbAnswer(
            title="Regression testing (§2.2.3)",
            body=TERMS_GLOSSARY["regression testing"],
            follow_up=["Confirmation vs regression?",
                       "How to keep regression sets small?"],
        )
    if topic == "confirmation":
        return IstqbAnswer(
            title="Confirmation testing (§2.2.3)",
            body=TERMS_GLOSSARY["confirmation testing"],
            follow_up=["Show regression testing",
                       "Who should run confirmation tests?"],
        )
    if topic == "smoke":
        return IstqbAnswer(
            title="Smoke testing",
            body=TERMS_GLOSSARY["smoke testing"],
            follow_up=["Smoke vs sanity?",
                       "When does smoke run in CI?"],
        )
    if topic == "static":
        return IstqbAnswer(
            title="Static testing (chapter 3)",
            body=TERMS_GLOSSARY["static testing"]
                  + "\n\nValue: defects found early are cheaper to remove "
                    "than defects found in operation. Static testing also "
                    "covers requirements / architecture before code exists.",
            follow_up=["Show review types",
                       "Static vs dynamic testing?"],
        )
    if topic == "reviews":
        return IstqbAnswer(
            title="Review types (§3)",
            body=_bullets(REVIEW_TYPES),
            follow_up=["Roles in a formal inspection?",
                       "Static testing vs dynamic testing?"],
        )
    if topic == "risk_based":
        return IstqbAnswer(
            title="Risk-based testing (§5.2)",
            body=TEST_MANAGEMENT["risk-based testing"],
            follow_up=["What goes into a risk register?",
                       "How to estimate risk likelihood?"],
        )
    if topic == "test_plan":
        return IstqbAnswer(
            title="Test plan (§5.1)",
            body=TEST_MANAGEMENT["test plan"],
            follow_up=["Show entry / exit criteria",
                       "What is a test completion report?"],
        )
    if topic == "entry_exit":
        return IstqbAnswer(
            title="Entry and exit criteria (§5.1.3)",
            body=TERMS_GLOSSARY["entry criteria"] + "\n\n" +
                 TERMS_GLOSSARY["exit criteria"],
            follow_up=["Show test plan contents",
                       "Definition of Done vs exit criteria?"],
        )
    if topic == "estimation_theory":
        return IstqbAnswer(
            title="Test estimation (§5.1.4)",
            body=TEST_MANAGEMENT["test estimation"],
            follow_up=["Show three-point estimation",
                       "Show TestForTge's PERT estimator"],
        )
    if topic == "errors_defects":
        return IstqbAnswer(
            title="Errors, defects, failures, root causes (§1.2.3)",
            body=(TERMS_GLOSSARY["error"] + "\n\n" +
                  TERMS_GLOSSARY["defect"] + "\n\n" +
                  TERMS_GLOSSARY["failure"] + "\n\n" +
                  TERMS_GLOSSARY["root cause"]),
            follow_up=["What is root-cause analysis?",
                       "Why do defects cluster?"],
        )
    if topic == "verification_validation":
        return IstqbAnswer(
            title="Verification and validation (§1.1)",
            body=(TERMS_GLOSSARY["verification"] + "\n\n" +
                  TERMS_GLOSSARY["validation"]),
            follow_up=["Show absence-of-defects fallacy",
                       "How does ATDD support validation?"],
        )
    if topic == "qa_vs_testing":
        return IstqbAnswer(
            title="QA vs Testing (§1.2.2)",
            body=("Testing is product-oriented and corrective — find "
                  "defects in a specific work product.\n\n" +
                  TERMS_GLOSSARY["qa"]),
            follow_up=["Show seven testing principles",
                       "Show test process activities"],
        )
    if topic == "tools":
        return IstqbAnswer(
            title="Test tool categories (chapter 6)",
            body=_bullets(TOOL_CATEGORIES),
            follow_up=["Benefits and risks of tool support?",
                       "How to pilot a new tool?"],
        )
    if topic == "shift_left":
        return IstqbAnswer(
            title="Shift Left (§2.1.5)",
            body=("'Shift left' means moving testing earlier in the SDLC: "
                  "static testing of requirements and design, unit and "
                  "integration testing alongside coding, automated CI checks. "
                  "Goal: find defects when they are cheapest to fix and "
                  "prevent them from reaching later phases."),
            follow_up=["Show DevOps + testing",
                       "Show static testing"],
        )
    if topic == "devops":
        return IstqbAnswer(
            title="DevOps and testing (§2.1.4)",
            body=("DevOps is a set of practices and a culture that integrates "
                  "development and operations. For testing it means: tests run "
                  "automatically in CI/CD pipelines, fast feedback to "
                  "developers, infrastructure-as-code lets you reproduce envs, "
                  "and monitoring extends 'testing in production'."),
            follow_up=["Show shift left",
                       "Show test execution tools"],
        )
    if topic == "atdd":
        return IstqbAnswer(
            title="ATDD — Acceptance Test-driven Development (§4.5.3)",
            body=TEST_TECHNIQUES["atdd"],
            follow_up=["Show acceptance criteria",
                       "Show collaborative user-story writing"],
        )
    if topic == "coverage":
        return IstqbAnswer(
            title="Test coverage",
            body=(TERMS_GLOSSARY["coverage"] + "\n\n"
                  "ISTQB white-box coverage: statement coverage = % executable "
                  "statements exercised; branch coverage = % decision outcomes "
                  "(true/false) exercised. 100% branch implies 100% statement, "
                  "but not vice-versa."),
            follow_up=["Show statement vs branch coverage",
                       "Show traceability"],
        )
    if topic == "severity":
        return IstqbAnswer(
            title="Severity (ISTQB CTFL §5.5)",
            body=TERMS_GLOSSARY["severity"],
            follow_up=["What is priority?",
                       "Severity vs priority — examples",
                       "Show defect lifecycle",
                       "What goes into a bug report?"],
        )
    if topic == "priority":
        return IstqbAnswer(
            title="Priority (ISTQB CTFL §5.5)",
            body=TERMS_GLOSSARY["priority"],
            follow_up=["What is severity?",
                       "Severity vs priority — examples",
                       "Show defect lifecycle",
                       "What goes into a bug report?"],
        )
    if topic == "defect_lifecycle":
        return IstqbAnswer(
            title="Defect lifecycle (ISTQB CTFL §5.5)",
            body=TERMS_GLOSSARY["defect lifecycle"],
            follow_up=["What is confirmation testing?",
                       "What is severity?",
                       "What is priority?",
                       "What goes into a bug report?"],
        )
    if topic == "defect_management":
        return IstqbAnswer(
            title="Defect management (ISTQB CTFL §5.5)",
            body=TERMS_GLOSSARY["defect management"],
            follow_up=["Show defect lifecycle",
                       "What is severity?",
                       "What is priority?",
                       "What is a defect report?"],
        )
    if topic == "test_scenario":
        return IstqbAnswer(
            title="Test scenario (ISTQB CTFL §4)",
            body=TERMS_GLOSSARY["test scenario"],
            follow_up=["What is a test case?",
                       "Test scenario vs test case",
                       "Show test design techniques",
                       "What is exploratory testing?"],
        )
    if topic == "istqb_general":
        return IstqbAnswer(
            title="ISTQB Foundation Level — what it covers",
            body=("ISTQB CTFL v4.0.1 has six chapters:\n"
                  "1. Fundamentals of Testing — what testing is, principles, "
                  "process, roles.\n"
                  "2. Testing Throughout the SDLC — levels, types, "
                  "confirmation/regression, maintenance.\n"
                  "3. Static Testing — reviews, static analysis.\n"
                  "4. Test Analysis & Design — black-box / white-box / "
                  "experience-based / collaboration-based techniques.\n"
                  "5. Managing the Test Activities — planning, monitoring, "
                  "control, risk-based testing, defect management.\n"
                  "6. Test Tools — categories, benefits & risks, selection."),
            follow_up=["Show seven testing principles",
                       "Show test design techniques",
                       "Show test levels"],
        )
    return None


# ── AI-mode prompt extension ──────────────────────────────────────

def istqb_persona_prompt() -> str:
    """One-paragraph extension to Tedgie's system prompt that primes
    Anthropic-backed responses with ISTQB CTFL v4 terminology and a
    Foundation-Level-certified persona. Kept short to leave room in
    the context window for the user's actual question + history."""
    return (
        " You are also ISTQB Certified Tester Foundation Level (CTFL v4.0.1) "
        "qualified. When the user asks about testing theory — principles, "
        "process activities, test levels, test types, design techniques "
        "(equivalence partitioning, boundary value analysis, decision tables, "
        "state transition, exploratory, error guessing), static testing, "
        "reviews (informal / walkthrough / technical / inspection), "
        "test management, risk-based testing, defect lifecycle, coverage, "
        "verification vs validation — answer with ISTQB v4 terminology and "
        "structure. Distinguish error / defect / failure / root cause "
        "precisely. State the seven testing principles verbatim when "
        "asked. Cite the syllabus chapter (§1.3, §2.2.1, §4.2 etc.) when "
        "you give a definition. Be concise (2-6 sentences) unless the "
        "user asks for detail or examples."
    )
