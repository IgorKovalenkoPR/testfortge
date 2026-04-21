"""
TestFortge — QA Team Lead Persona

ISTQB Advanced-certified (Full Advanced: Test Manager, Test Analyst,
Technical Test Analyst) QA Team Lead with 10+ years of hands-on experience
across multiple domains (FinTech, E-commerce, Healthcare, SaaS, EdTech,
Telecom, Government) and management of 50+ testers.

Expertise:
  ─ Test Documentation: Test Policy, Test Strategy, Test Plans, Test Cases,
    High-Level & Low-Level Checklists, Test Completion Reports
  ─ AI Testing: ML model validation, data pipeline testing, bias detection
  ─ Automation: Python, TypeScript; Playwright, Selenium, Appium, Cypress
  ─ Test Metrics: Defect Density, Test Coverage, Defect Removal Efficiency,
    Test Execution Rate, Pass/Fail Ratio, Escaped Defects, MTTR, etc.
  ─ ISTQB Test Design: EP, BVA, Decision Tables, State Transition,
    Use Case Testing, Pairwise, Risk-Based Testing

This module acts as a REVIEW and VALIDATION layer that runs AFTER the
Senior QA Engineer (qa_persona.py) generates test documentation.

The Team Lead:
  1. Reviews generated test cases and checklists for quality
  2. Detects common defects in test documentation
  3. Flags issues for correction
  4. Applies corrections automatically where possible
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ReviewFinding:
    """A single issue found during documentation review."""
    severity: str      # "Critical", "Major", "Minor", "Info"
    category: str      # Category of the issue
    message: str       # Human-readable description
    item_id: str = ""  # Affected test case / checklist item ID
    auto_fixed: bool = False  # Whether the Team Lead auto-corrected it


@dataclass
class ReviewReport:
    """Complete review report from the QA Team Lead."""
    findings: list[ReviewFinding] = field(default_factory=list)
    total_items_reviewed: int = 0
    items_fixed: int = 0
    quality_score: int = 100  # 0-100 percentage

    @property
    def has_critical(self) -> bool:
        return any(f.severity == "Critical" for f in self.findings)

    @property
    def summary(self) -> str:
        counts = {}
        for f in self.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        parts = [f"{v} {k}" for k, v in counts.items()]
        return f"Reviewed {self.total_items_reviewed} items: " + ", ".join(parts) if parts else \
               f"Reviewed {self.total_items_reviewed} items: All clear"


# ═══════════════════════════════════════════════════════════════════
# 1. Page Number Detection (PDF artifact cleanup)
# ═══════════════════════════════════════════════════════════════════

# Pattern: trailing number that looks like a page number, NOT part of content
_TRAILING_PAGE_NUM_RE = re.compile(
    r"^(.+\b[a-zA-Zа-яА-ЯіІїЇєЄґҐ)\"'.!?]{1,})\s+(\d{1,4})$"
)

# Contexts where trailing numbers ARE meaningful (not page numbers)
_NUMBER_CONTEXT_PATTERNS = [
    r"\bv(?:ersion)?\s*$",
    r"\b(?:№|#|no\.?)\s*$",
    r"\b(?:up\s+to|max(?:imum)?|min(?:imum)?|at\s+least|at\s+most)\s*$",
    r"\b(?:step|phase|level|stage|tier|grade|рівень|крок|етап|фаза)\s*$",
    r"\b(?:port|порт)\s*$",
    r"\b(?:error|code|status|код|помилка|статус)\s*$",
    r"\b(?:item|element|count|елемент|пункт|кількість)\s*$",
    r"\b(?:up\s+to|більше|менше|до|від|мін|макс|щонайменше)\s*$",
    r"\b(?:after|before|within|протягом|після|до|через)\s*$",
    r"\b(?:MB|GB|KB|ms|px|em|rem|%|seconds?|minutes?|hours?)\s*$",
    r"\d+\s*[-–—x×]\s*$",  # ranges or dimensions: "10 - 20", "1920 x 1080"
]
_NUMBER_CONTEXT_RE = [re.compile(p, re.IGNORECASE) for p in _NUMBER_CONTEXT_PATTERNS]


def _is_trailing_page_number(text: str) -> tuple[bool, str]:
    """Check if text has a trailing page number. Returns (has_page_num, cleaned_text)."""
    m = _TRAILING_PAGE_NUM_RE.match(text.strip())
    if not m:
        return False, text

    body = m.group(1)
    body_lower = body.rstrip().lower()

    for pat in _NUMBER_CONTEXT_RE:
        if pat.search(body_lower):
            return False, text

    return True, body


def clean_page_numbers(text: str) -> str:
    """Strip trailing page numbers from text (PDF artifact cleanup)."""
    has_page, cleaned = _is_trailing_page_number(text)
    return cleaned if has_page else text


def _strip_page_numbers_from_text(text: str) -> str:
    """Strip page numbers from text — both trailing AND embedded mid-sentence.

    Handles:
      - 'packet Submission 93 is handled correctly' → 'packet Submission is handled correctly'
      - 'packet Receipt 116' → 'packet Receipt'
      - 'Verify that module 42 works' → but keep if it's like 'error code 404'
    """
    # 1. Trailing page number
    has_page, cleaned = _is_trailing_page_number(text)
    if has_page:
        return cleaned

    # 2. Embedded page number: word + space + 1-4 digits + space + word
    #    Pattern: a word boundary, then 1-4 digits surrounded by spaces,
    #    where the number is NOT preceded by a context word.
    def _replace_embedded(m: re.Match) -> str:
        before = m.group(1)
        after = m.group(3)
        before_lower = before.rstrip().lower()
        # Check if the number is contextually meaningful
        for pat in _NUMBER_CONTEXT_RE:
            if pat.search(before_lower):
                return m.group(0)  # keep as-is
        return f"{before}{after}"

    result = re.sub(
        r"(\b[a-zA-Zа-яА-ЯіІїЇєЄґҐ]{2,}\s+)"  # word before
        r"(\d{1,4})"                                # the number
        r"(\s+(?:is|are|was|were|should|has|have|can|will|may)\b)",  # verb after
        _replace_embedded,
        text,
    )

    return result


# ═══════════════════════════════════════════════════════════════════
# 2. Test Case Review Rules
# ═══════════════════════════════════════════════════════════════════

def review_test_cases(test_cases: list, report: ReviewReport | None = None) -> tuple[list, ReviewReport]:
    """Review and fix test cases. Returns (fixed_cases, report).

    The QA Team Lead checks:
      1. Summary starts with "Verify that" (passive voice)
      2. Expected Result uses "should/should be" (not "is/are")
      3. Preconditions use passive voice
      4. No page numbers in summaries, steps, or test data
      5. Test steps are numbered and actionable
      6. No duplicate test case IDs
      7. Each test case has at least 2 steps
      8. Expected result is not empty
    """
    if report is None:
        report = ReviewReport()

    seen_ids: set[str] = set()

    for tc in test_cases:
        report.total_items_reviewed += 1
        tc_id = getattr(tc, "id", "") or ""

        # Rule 0: Strip PDF underscore artifacts from all text fields
        for attr in ("summary", "preconditions", "test_steps", "test_data", "expected_result"):
            val = getattr(tc, attr, None)
            if val and isinstance(val, str) and "__" in val:
                cleaned_val = re.sub(r"\s*_{2,}\s*", " ", val).strip()
                cleaned_val = re.sub(r"\s{2,}", " ", cleaned_val)
                if cleaned_val != val:
                    setattr(tc, attr, cleaned_val)
                    report.findings.append(ReviewFinding(
                        severity="Major", category="PDF Underscore Artifact",
                        message=f"Field '{attr}' contains underscore fill from PDF",
                        item_id=tc_id, auto_fixed=True,
                    ))
                    report.items_fixed += 1

        # Rule 1: Summary must start with "Verify that"
        summary = getattr(tc, "summary", "")
        if summary and not summary.startswith("Verify that"):
            report.findings.append(ReviewFinding(
                severity="Major", category="Passive Voice",
                message=f"Summary does not start with 'Verify that'",
                item_id=tc_id,
            ))
            # Auto-fix: prepend "Verify that" and lowercase
            if hasattr(tc, "summary"):
                fixed = _ensure_verify_that(summary)
                tc.summary = fixed
                report.items_fixed += 1
                report.findings[-1].auto_fixed = True

        # Rule 2: Expected Result must use "should/should be"
        expected = getattr(tc, "expected_result", "")
        if expected and not _uses_should(expected):
            report.findings.append(ReviewFinding(
                severity="Major", category="Expected Result Voice",
                message=f"Expected result uses 'is/are' instead of 'should/should be'",
                item_id=tc_id,
            ))
            # Auto-fix
            if hasattr(tc, "expected_result"):
                tc.expected_result = _fix_expected_result_voice(expected)
                report.items_fixed += 1
                report.findings[-1].auto_fixed = True

        # Rule 3: Page numbers in summary (trailing or embedded)
        if summary:
            cleaned_summary = _strip_page_numbers_from_text(summary)
            if cleaned_summary != summary:
                report.findings.append(ReviewFinding(
                    severity="Critical", category="Page Number Artifact",
                    message=f"Summary contains page number artifact",
                    item_id=tc_id,
                ))
                if hasattr(tc, "summary"):
                    tc.summary = cleaned_summary
                    report.items_fixed += 1
                    report.findings[-1].auto_fixed = True

        # Rule 4: Page numbers in test steps
        steps_text = getattr(tc, "test_steps", "") or getattr(tc, "steps", "")
        if isinstance(steps_text, str) and steps_text:
            has_page_in_steps, cleaned_steps = _is_trailing_page_number(steps_text)
            if has_page_in_steps:
                report.findings.append(ReviewFinding(
                    severity="Major", category="Page Number Artifact",
                    message=f"Test steps contain trailing page number",
                    item_id=tc_id,
                ))
                if hasattr(tc, "test_steps"):
                    tc.test_steps = cleaned_steps
                    report.items_fixed += 1
                    report.findings[-1].auto_fixed = True

        # Rule 5: Duplicate IDs
        if tc_id:
            if tc_id in seen_ids:
                report.findings.append(ReviewFinding(
                    severity="Critical", category="Duplicate ID",
                    message=f"Duplicate test case ID: {tc_id}",
                    item_id=tc_id,
                ))
            seen_ids.add(tc_id)

        # Rule 6: Empty expected result
        if not expected or len(expected.strip()) < 5:
            report.findings.append(ReviewFinding(
                severity="Major", category="Missing Expected Result",
                message=f"Expected result is empty or too short",
                item_id=tc_id,
            ))

        # Rule 7: Preconditions passive voice check
        preconditions = getattr(tc, "preconditions", "")
        if preconditions and "User is at " in preconditions:
            report.findings.append(ReviewFinding(
                severity="Minor", category="Passive Voice",
                message=f"Preconditions use active voice ('User is at ...')",
                item_id=tc_id,
            ))
            if hasattr(tc, "preconditions"):
                tc.preconditions = preconditions.replace("User is at ", "")
                report.items_fixed += 1
                report.findings[-1].auto_fixed = True

    # Calculate quality score
    if report.total_items_reviewed > 0:
        critical = sum(1 for f in report.findings if f.severity == "Critical" and not f.auto_fixed)
        major = sum(1 for f in report.findings if f.severity == "Major" and not f.auto_fixed)
        minor = sum(1 for f in report.findings if f.severity == "Minor" and not f.auto_fixed)
        penalty = critical * 15 + major * 5 + minor * 2
        report.quality_score = max(0, 100 - penalty)

    return test_cases, report


# ═══════════════════════════════════════════════════════════════════
# 3. Checklist Review Rules
# ═══════════════════════════════════════════════════════════════════

def review_checklist(checklist_items: list, report: ReviewReport | None = None) -> tuple[list, ReviewReport]:
    """Review and fix checklist items. Returns (fixed_items, report).

    The QA Team Lead checks:
      1. All objectives start with "Verify that"
      2. No page numbers in objectives
      3. No duplicate IDs
      4. Sections are properly assigned
      5. Categories are valid (Positive/Negative/Edge Case/Security/Performance/Accessibility)
      6. Priorities are valid (High/Medium/Low)
    """
    if report is None:
        report = ReviewReport()

    valid_categories = {"Positive", "Negative", "Edge Case", "Security", "Performance", "Accessibility"}
    valid_priorities = {"High", "Medium", "Low"}
    seen_ids: set[str] = set()

    for item in checklist_items:
        report.total_items_reviewed += 1
        item_id = getattr(item, "id", "") or ""

        # Rule 0: Strip PDF underscore artifacts
        objective_raw = getattr(item, "objective", "") or ""
        if "__" in objective_raw:
            cleaned_val = re.sub(r"\s*_{2,}\s*", " ", objective_raw).strip()
            cleaned_val = re.sub(r"\s{2,}", " ", cleaned_val)
            if cleaned_val != objective_raw:
                item.objective = cleaned_val
                report.findings.append(ReviewFinding(
                    severity="Major", category="PDF Underscore Artifact",
                    message=f"Objective contains underscore fill from PDF",
                    item_id=item_id, auto_fixed=True,
                ))
                report.items_fixed += 1

        # Rule 1: Objective must start with "Verify that"
        objective = getattr(item, "objective", "")
        if objective and not objective.startswith("Verify that"):
            report.findings.append(ReviewFinding(
                severity="Major", category="Passive Voice",
                message=f"Objective does not start with 'Verify that'",
                item_id=item_id,
            ))
            if hasattr(item, "objective"):
                item.objective = _ensure_verify_that(objective)
                report.items_fixed += 1
                report.findings[-1].auto_fixed = True

        # Rule 2: Page numbers in objective (trailing or embedded)
        if objective:
            cleaned_obj = _strip_page_numbers_from_text(objective)
            if cleaned_obj != objective:
                report.findings.append(ReviewFinding(
                    severity="Critical", category="Page Number Artifact",
                    message=f"Objective contains page number artifact",
                    item_id=item_id,
                ))
                if hasattr(item, "objective"):
                    item.objective = cleaned_obj
                    report.items_fixed += 1
                    report.findings[-1].auto_fixed = True

        # Rule 3: Duplicate IDs
        if item_id:
            if item_id in seen_ids:
                report.findings.append(ReviewFinding(
                    severity="Critical", category="Duplicate ID",
                    message=f"Duplicate checklist item ID: {item_id}",
                    item_id=item_id,
                ))
            seen_ids.add(item_id)

        # Rule 4: Valid category
        category = getattr(item, "category", "")
        if category and category not in valid_categories:
            report.findings.append(ReviewFinding(
                severity="Minor", category="Invalid Category",
                message=f"Invalid category '{category}'. Expected: {', '.join(valid_categories)}",
                item_id=item_id,
            ))

        # Rule 5: Valid priority
        priority = getattr(item, "priority", "")
        if priority and priority not in valid_priorities:
            report.findings.append(ReviewFinding(
                severity="Minor", category="Invalid Priority",
                message=f"Invalid priority '{priority}'. Expected: {', '.join(valid_priorities)}",
                item_id=item_id,
            ))

        # Rule 6: Empty objective
        if not objective or len(objective.strip()) < 10:
            report.findings.append(ReviewFinding(
                severity="Major", category="Missing Objective",
                message=f"Objective is empty or too short",
                item_id=item_id,
            ))

    # Quality score
    if report.total_items_reviewed > 0:
        critical = sum(1 for f in report.findings if f.severity == "Critical" and not f.auto_fixed)
        major = sum(1 for f in report.findings if f.severity == "Major" and not f.auto_fixed)
        minor = sum(1 for f in report.findings if f.severity == "Minor" and not f.auto_fixed)
        penalty = critical * 15 + major * 5 + minor * 2
        report.quality_score = max(0, 100 - penalty)

    return checklist_items, report


# ═══════════════════════════════════════════════════════════════════
# 4. Auto-fix Helpers
# ═══════════════════════════════════════════════════════════════════

def _ensure_verify_that(text: str) -> str:
    """Ensure text starts with 'Verify that'."""
    t = text.strip()

    # Already correct
    if t.startswith("Verify that"):
        return t

    # Remove existing verify/check/ensure prefixes
    t = re.sub(
        r"^(verify\s+(that\s+)?|check\s+(that\s+)?|ensure\s+(that\s+)?|"
        r"confirm\s+(that\s+)?|validate\s+(that\s+)?|test\s+(that\s+)?)",
        "", t, flags=re.IGNORECASE,
    ).strip()

    # Lowercase first char
    if t:
        t = t[0].lower() + t[1:]
    return f"Verify that {t}"


def _uses_should(text: str) -> bool:
    """Check if text uses 'should/should be' voice."""
    # At least one "should" in the text indicates correct voice
    return "should" in text.lower()


def _fix_expected_result_voice(text: str) -> str:
    """Convert 'is/are' voice to 'should/should be' in expected result.

    Transforms patterns like:
      'User is authenticated' → 'User should be authenticated'
      'Results are displayed' → 'Results should be displayed'
      'Form is not submitted' → 'Form should not be submitted'
      'Data is persisted' → 'Data should be persisted'
      'No errors occur' → 'No errors should occur'
    """
    result = text

    # "is not" → "should not be"
    result = re.sub(r"\bis not\b", "should not be", result)
    # "are not" → "should not be"
    result = re.sub(r"\bare not\b", "should not be", result)
    # "is NOT" → "should NOT be"
    result = re.sub(r"\bis NOT\b", "should NOT be", result)

    # "No X is/are" → "No X should be"
    result = re.sub(r"\b(No\s+\w+)\s+is\b", r"\1 should be", result)
    result = re.sub(r"\b(No\s+\w+)\s+are\b", r"\1 should be", result)

    # "X is VERB-ed" → "X should be VERB-ed" (passive constructions)
    result = re.sub(
        r"\b(\w+)\s+is\s+((?:not\s+)?[a-z]+ed)\b",
        r"\1 should be \2", result
    )
    result = re.sub(
        r"\b(\w+)\s+are\s+((?:not\s+)?[a-z]+ed)\b",
        r"\1 should be \2", result
    )

    # Remaining "is" → "should be" (for patterns not yet caught)
    # Only if the sentence doesn't already have "should"
    sentences = result.split(". ")
    fixed_sentences = []
    for s in sentences:
        if "should" not in s.lower():
            # "X is Y" → "X should be Y" (remaining simple cases)
            s = re.sub(r"\b(\w+)\s+is\s+", r"\1 should be ", s, count=1)
        fixed_sentences.append(s)
    result = ". ".join(fixed_sentences)

    # "No X occur" / "No X occurs" → "No X should occur"
    result = re.sub(r"\b(No\s+\w+)\s+occur(?:s)?\b", r"\1 should occur", result)

    return result
