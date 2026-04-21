"""
TestFortge — User Story Generator

Converts parsed requirements into structured User Stories
following the standard format: As a [role], I want [action], so that [benefit].

Applies keyword analysis to detect:
  - User roles (admin, user, customer, etc.)
  - Actions (CRUD, navigation, search, etc.)
  - Business value / benefit
"""

import re
from dataclasses import dataclass, field
from .file_parser import ParsedRequirement


@dataclass
class UserStory:
    id: str
    requirement_id: str
    role: str
    action: str
    benefit: str
    acceptance_criteria: list[str] = field(default_factory=list)
    original_text: str = ""
    priority: str = "Medium"
    story_points_hint: str = ""


# ── Role detection ───────────────────────────────────────────────

_ROLE_PATTERNS = [
    (r"\badmin(?:istrator)?\b", "administrator"),
    (r"\bmanager\b", "manager"),
    (r"\boperator\b", "operator"),
    (r"\bмодератор\b", "moderator"),
    (r"\badmin\s*role\b", "administrator"),
    (r"\bcustomer\b", "customer"),
    (r"\bbuyer\b", "buyer"),
    (r"\bseller\b", "seller"),
    (r"\bvisitor\b", "visitor"),
    (r"\bguest\b", "guest user"),
    (r"\bregistered\s*user\b", "registered user"),
    (r"\bauthenticated\s*user\b", "authenticated user"),
    (r"\bpatient\b", "patient"),
    (r"\bdoctor\b", "doctor"),
    (r"\bstudent\b", "student"),
    (r"\bteacher\b", "teacher"),
    (r"\buser\b", "user"),
]

_PRIORITY_SIGNALS = {
    "High": ["must", "shall", "critical", "essential", "required", "mandatory",
             "security", "payment", "authentication", "authorization"],
    "Low": ["nice to have", "optional", "could", "may", "consider", "future"],
}

_COMPLEXITY_SIGNALS = {
    "S (1-2)": ["display", "show", "view", "read", "list"],
    "M (3-5)": ["create", "add", "edit", "update", "delete", "filter", "search", "sort"],
    "L (8-13)": ["integrate", "import", "export", "migrate", "workflow", "process",
                  "payment", "checkout", "encrypt", "report", "dashboard", "analytics"],
}


def _detect_role(text: str) -> str:
    lower = text.lower()
    for pattern, role in _ROLE_PATTERNS:
        if re.search(pattern, lower):
            return role
    return "user"


def _detect_priority(text: str) -> str:
    lower = text.lower()
    for prio, keywords in _PRIORITY_SIGNALS.items():
        for kw in keywords:
            if kw in lower:
                return prio
    return "Medium"


def _estimate_complexity(text: str) -> str:
    lower = text.lower()
    for size, keywords in reversed(list(_COMPLEXITY_SIGNALS.items())):
        for kw in keywords:
            if kw in lower:
                return size
    return "M (3-5)"


_MAX_ACTION_LEN = 80


def _extract_action(text: str) -> str:
    """Extract the core action from a requirement."""
    cleaned = text.strip()

    # 1. Strip instruction prefixes (user wrote a command, not a requirement)
    cleaned = re.sub(
        r"^(please\s+)?"
        r"(create|generate|write|make|build|prepare|design|draft|develop|produce)\s+"
        r"(a\s+)?"
        r"(checklist|test\s*cases?|test\s*plan|tests?|test\s*suite|scenarios?|checks?)\s+"
        r"(for|based\s+on|from|of|about)\s+",
        "", cleaned, flags=re.IGNORECASE,
    ).strip()

    # 2. Strip "the following page/url/site:" type prefixes
    #    NOTE: "system", "application", "app" deliberately excluded here —
    #    they are handled by step 5 ("The system must/shall/should …")
    cleaned = re.sub(
        r"^(the\s+)?(following\s+)?(page|url|site|website|web\s*page|service|feature|module)\s*:?\s*",
        "", cleaned, flags=re.IGNORECASE,
    ).strip()

    # 3. Handle bare URLs — extract domain as feature name
    url_match = re.match(r"^(https?://)?([^/\s]+\.[a-z]{2,})(/.*)?\s*$", cleaned, re.IGNORECASE)
    if url_match:
        domain = url_match.group(2)
        path = (url_match.group(3) or "").strip("/").replace("/", " > ").replace("-", " ").replace("_", " ")
        if path:
            cleaned = f"{domain}: {path}"
        else:
            cleaned = domain

    # 4. Strip TestFort-style "Verify that ..." / "Check ..." prefixes
    # so the templated wrapper doesn't double up.
    cleaned = re.sub(
        r"^(verify\s+(that\s+)?|check\s+(that\s+)?|ensure\s+(that\s+)?|confirm\s+(that\s+)?|"
        r"validate\s+(that\s+)?|test\s+(that\s+)?)",
        "", cleaned, flags=re.IGNORECASE,
    ).strip()

    # 5. Remove standard requirement prefixes
    cleaned = re.sub(
        r"^(the\s+)?(system|application|app|platform|software)\s+"
        r"(must|shall|should|will|needs?\s+to|has\s+to)\s+",
        "", cleaned, flags=re.IGNORECASE,
    ).strip()

    # 6. Remove role mentions for cleaner action text
    cleaned = re.sub(
        r"\b(user|admin|customer|visitor)s?\s+(must|shall|should|can|will)\s+(be\s+able\s+to\s+)?",
        "", cleaned, flags=re.IGNORECASE,
    ).strip()

    # 7. Truncate at first sentence terminator if action would be huge
    if len(cleaned) > _MAX_ACTION_LEN:
        # Try to break on sentence boundary
        sentence_end = re.search(r"[.!?]", cleaned[:_MAX_ACTION_LEN + 20])
        if sentence_end:
            cleaned = cleaned[:sentence_end.start()].strip()
        else:
            # Hard cap with ellipsis at word boundary
            truncated = cleaned[:_MAX_ACTION_LEN]
            last_space = truncated.rfind(" ")
            if last_space > _MAX_ACTION_LEN // 2:
                truncated = truncated[:last_space]
            cleaned = truncated.rstrip() + "…"

    # 8. Strip trailing page numbers (e.g. "packet Submission 93" → "packet Submission")
    cleaned = re.sub(r"\s+\d{1,4}$", "", cleaned).strip()

    # 9. Strip PDF underscore artifacts (e.g. "tX Taxation ___________")
    cleaned = re.sub(r"\s*_{2,}\s*", " ", cleaned).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)

    # 10. Lowercase first char
    if cleaned:
        cleaned = cleaned[0].lower() + cleaned[1:]
    return cleaned or text.lower()[:_MAX_ACTION_LEN]


def _extract_benefit(text: str) -> str:
    """Infer business value from the requirement."""
    lower = text.lower()

    benefit_map = [
        (["login", "register", "sign up", "authentication", "password"], "I can securely access my account"),
        (["search", "find", "filter", "sort"], "I can quickly find what I need"),
        (["display", "show", "view", "dashboard"], "I can see relevant information at a glance"),
        (["create", "add", "new"], "I can add new data to the system"),
        (["edit", "update", "modify", "change"], "I can keep information up to date"),
        (["delete", "remove"], "I can manage and clean up data"),
        (["export", "download", "report"], "I can use data outside the system"),
        (["import", "upload"], "I can bring external data into the system"),
        (["notify", "notification", "alert", "email"], "I stay informed about important events"),
        (["payment", "pay", "checkout", "billing"], "I can complete transactions securely"),
        (["encrypt", "security", "protect"], "my data is protected and secure"),
        (["performance", "fast", "speed", "load"], "I have a smooth and responsive experience"),
        (["integrate", "api", "connect"], "the system works seamlessly with other services"),
        (["permission", "role", "access"], "proper access control is maintained"),
        (["log", "audit", "track"], "activities are tracked for compliance and debugging"),
    ]

    for keywords, benefit in benefit_map:
        for kw in keywords:
            if kw in lower:
                return benefit

    return "I can accomplish my goal efficiently"


def _generate_acceptance_criteria(text: str, role: str) -> list[str]:
    """Generate acceptance criteria based on requirement text."""
    criteria = []
    lower = text.lower()

    # Always add happy path
    criteria.append("The feature works as described in the requirement")

    # Input validation
    if any(kw in lower for kw in ["form", "input", "field", "enter", "fill", "type"]):
        criteria.append("All input fields have proper validation (required, format, length)")
        criteria.append("Clear error messages are shown for invalid input")

    # Auth-related
    if any(kw in lower for kw in ["login", "register", "auth", "password", "sign"]):
        criteria.append("Unauthorized users cannot access protected resources")
        criteria.append("Session management works correctly (login, logout, timeout)")

    # Data operations
    if any(kw in lower for kw in ["create", "add", "save", "store"]):
        criteria.append("Data is persisted correctly in the database")
        criteria.append("Success confirmation is shown to the user")

    if any(kw in lower for kw in ["list", "display", "show", "view"]):
        criteria.append("Data is displayed correctly and completely")
        criteria.append("Empty state is handled gracefully")

    if any(kw in lower for kw in ["search", "filter", "sort"]):
        criteria.append("Results are accurate and match the search/filter criteria")
        criteria.append("No results state shows appropriate message")

    if any(kw in lower for kw in ["delete", "remove"]):
        criteria.append("Confirmation dialog is shown before destructive action")
        criteria.append("Related data is handled properly (cascade or restrict)")

    # Performance
    if any(kw in lower for kw in ["performance", "fast", "load", "speed", "second"]):
        criteria.append("Response time meets the specified threshold")

    # Security
    if any(kw in lower for kw in ["encrypt", "security", "protect", "permission"]):
        criteria.append("Security measures are implemented and verified")

    # Default negative criterion
    criteria.append("Error handling works correctly for edge cases")

    return criteria[:6]  # Cap at 6 criteria


def _parse_custom_prompt(custom_prompt: str) -> dict:
    """Parse custom prompt into directives that affect generation."""
    directives: dict = {
        "force_role": None,
        "force_priority": None,
        "extra_criteria": [],
        "focus_keywords": [],
    }
    if not custom_prompt:
        return directives

    lower = custom_prompt.lower()

    # Force role override
    for pattern, role in _ROLE_PATTERNS:
        if re.search(pattern, lower):
            directives["force_role"] = role
            break

    # Force priority
    if any(kw in lower for kw in ["high priority", "critical", "all high"]):
        directives["force_priority"] = "High"
    elif any(kw in lower for kw in ["low priority", "all low"]):
        directives["force_priority"] = "Low"

    # Extra acceptance criteria from prompt
    for line in custom_prompt.splitlines():
        line = line.strip()
        if line.startswith(("-", "*", "+")):
            directives["extra_criteria"].append(line.lstrip("-*+ ").strip())

    # Focus keywords (add to each story for downstream scenario detection)
    focus_map = {
        "security": "security",
        "performance": "performance",
        "api": "api",
        "payment": "payment",
        "auth": "authentication",
        "mobile": "mobile",
        "accessibility": "accessibility",
    }
    for keyword, focus in focus_map.items():
        if keyword in lower:
            directives["focus_keywords"].append(focus)

    return directives


def generate_user_stories(requirements: list[ParsedRequirement],
                          custom_prompt: str = "") -> list[UserStory]:
    """Convert a list of parsed requirements into User Stories."""
    stories = []
    directives = _parse_custom_prompt(custom_prompt)

    for idx, req in enumerate(requirements, start=1):
        role = directives["force_role"] or _detect_role(req.text)
        action = _extract_action(req.text)
        benefit = _extract_benefit(req.text)
        priority = directives["force_priority"] or _detect_priority(req.text)
        complexity = _estimate_complexity(req.text)
        criteria = _generate_acceptance_criteria(req.text, role)

        # Add extra criteria from custom prompt
        if directives["extra_criteria"]:
            criteria.extend(directives["extra_criteria"])

        # Append focus keywords to original_text for downstream detection
        original = req.text
        if directives["focus_keywords"]:
            original = req.text + " [focus: " + ", ".join(directives["focus_keywords"]) + "]"

        story = UserStory(
            id=f"US-{idx:03d}",
            requirement_id=req.id,
            role=role,
            action=action,
            benefit=benefit,
            acceptance_criteria=criteria[:8],  # Cap at 8
            original_text=original,
            priority=priority,
            story_points_hint=complexity,
        )
        stories.append(story)

    return stories
