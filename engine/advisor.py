"""
TestFortge — Advisor Engine (Dynamic)

Provides DYNAMIC recommendations based on:
  1. Project context (domain, platform, features)
  2. Actual content analysis (requirements text, user stories, uploaded docs)
  3. Keyword detection from all available project data

All recommendations are personalized per project — not static.
"""

from .knowledge_base import (
    TEST_DESIGN_TECHNIQUES, TESTING_TYPES, TESTING_TOOLS,
    NOCODE_TOOLS, DOMAIN_FOCUS, PLATFORM_SPECIFICS, STANDARDS,
)
from .user_story_generator import UserStory


# ═══════════════════════════════════════════════════════════════════
# Project context dataclass
# ═══════════════════════════════════════════════════════════════════

class ProjectContext:
    """Holds all project setup information for generating recommendations."""

    def __init__(self, **kwargs):
        self.project_name: str = kwargs.get("project_name", "Project")
        self.domain: str = kwargs.get("domain", "other")
        self.platform: str = kwargs.get("platform", "web")
        self.tech_stack: list[str] = kwargs.get("tech_stack", [])
        self.team_size: str = kwargs.get("team_size", "small")
        self.has_api: bool = kwargs.get("has_api", True)
        self.has_payments: bool = kwargs.get("has_payments", False)
        self.has_auth: bool = kwargs.get("has_auth", True)
        self.has_file_upload: bool = kwargs.get("has_file_upload", False)
        self.has_realtime: bool = kwargs.get("has_realtime", False)
        self.target_markets: list[str] = kwargs.get("target_markets", [])
        self.budget: str = kwargs.get("budget", "medium")
        self.release_frequency: str = kwargs.get("release_frequency", "biweekly")
        self.enabled_modules: list[str] = kwargs.get("enabled_modules", [
            "user_stories", "test_cases", "checklists", "techniques",
            "testing_types", "tools", "nocode", "focus_areas",
        ])


# ═══════════════════════════════════════════════════════════════════
# Content analyzer — detects features from actual text
# ═══════════════════════════════════════════════════════════════════

def _analyze_content(content_text: str) -> dict:
    """Analyze all project content to detect features, patterns, and concerns."""
    lower = content_text.lower() if content_text else ""

    return {
        "has_auth": any(kw in lower for kw in [
            "login", "register", "sign in", "sign up", "password", "authentication",
            "session", "token", "oauth", "sso", "2fa", "mfa", "credential"]),
        "has_payments": any(kw in lower for kw in [
            "payment", "checkout", "billing", "transaction", "credit card", "stripe",
            "paypal", "invoice", "subscription", "pricing", "order"]),
        "has_api": any(kw in lower for kw in [
            "api", "endpoint", "rest", "graphql", "webhook", "integration",
            "third-party", "microservice", "json", "request", "response"]),
        "has_search": any(kw in lower for kw in [
            "search", "filter", "sort", "query", "catalog", "browse"]),
        "has_file_ops": any(kw in lower for kw in [
            "upload", "download", "import", "export", "file", "attachment", "csv", "pdf"]),
        "has_realtime": any(kw in lower for kw in [
            "realtime", "real-time", "websocket", "chat", "notification", "push", "live"]),
        "has_crud": any(kw in lower for kw in [
            "create", "edit", "update", "delete", "manage", "add", "remove", "modify"]),
        "has_workflow": any(kw in lower for kw in [
            "workflow", "status", "state", "transition", "approval", "step", "stage", "pipeline"]),
        "has_roles": any(kw in lower for kw in [
            "role", "permission", "admin", "user role", "access control", "authorization",
            "privilege", "moderator"]),
        "has_data_tables": any(kw in lower for kw in [
            "table", "list", "grid", "dashboard", "report", "analytics", "chart", "graph"]),
        "has_forms": any(kw in lower for kw in [
            "form", "input", "field", "validation", "required field", "submit"]),
        "has_mobile": any(kw in lower for kw in [
            "mobile", "ios", "android", "responsive", "touch", "swipe", "app store"]),
        "has_security": any(kw in lower for kw in [
            "security", "encrypt", "ssl", "xss", "injection", "vulnerability", "owasp",
            "penetration", "firewall", "audit"]),
        "has_performance": any(kw in lower for kw in [
            "performance", "load", "speed", "scalab", "concurrent", "response time", "latency"]),
        "has_accessibility": any(kw in lower for kw in [
            "accessibility", "wcag", "screen reader", "a11y", "aria", "contrast"]),
        "has_localization": any(kw in lower for kw in [
            "localization", "i18n", "translation", "language", "locale", "multilingual"]),
        "has_database": any(kw in lower for kw in [
            "database", "sql", "query", "migration", "schema", "backup", "postgres", "mysql"]),
        "has_email": any(kw in lower for kw in [
            "email", "notification", "smtp", "template", "newsletter"]),
        "content_length": len(lower),
    }


# ═══════════════════════════════════════════════════════════════════
# 1. Test Design Techniques Advisor (dynamic)
# ═══════════════════════════════════════════════════════════════════

def recommend_techniques(ctx: ProjectContext, stories: list[UserStory] = None,
                         content_text: str = "") -> list[dict]:
    """Recommend test design techniques based on actual project content."""
    recommendations = []
    if stories is None:
        stories = []

    # Combine story text with content_text
    all_text = content_text + " " + " ".join(s.original_text.lower() for s in stories)
    analysis = _analyze_content(all_text)

    # Foundation techniques (always)
    always = ["equivalence_partitioning", "boundary_value_analysis"]
    for key in always:
        tech = TEST_DESIGN_TECHNIQUES[key]
        recommendations.append({
            **tech,
            "relevance": "high",
            "reasoning": "Foundational ISTQB technique. Recommended for every project.",
        })

    # Conditional techniques based on CONTENT analysis
    if analysis["has_roles"] or analysis["has_payments"] or analysis["has_workflow"]:
        tech = TEST_DESIGN_TECHNIQUES["decision_table"]
        reasons = []
        if analysis["has_roles"]:
            reasons.append("role-based access control")
        if analysis["has_payments"]:
            reasons.append("payment flows with multiple conditions")
        if analysis["has_workflow"]:
            reasons.append("business rules with condition combinations")
        recommendations.append({
            **tech,
            "relevance": "high",
            "reasoning": f"Your project has {', '.join(reasons)}. Decision Table will cover all condition combinations.",
        })

    if analysis["has_workflow"] or analysis["has_auth"]:
        tech = TEST_DESIGN_TECHNIQUES["state_transition"]
        reasons = []
        if analysis["has_workflow"]:
            reasons.append("workflows with state transitions")
        if analysis["has_auth"]:
            reasons.append("authentication states (logged in/out/locked)")
        recommendations.append({
            **tech,
            "relevance": "high",
            "reasoning": f"Your project has {', '.join(reasons)}. State Transition testing will verify all valid and invalid transitions.",
        })

    if analysis["has_mobile"] or ctx.platform in ("web", "mobile_native", "mobile_hybrid"):
        tech = TEST_DESIGN_TECHNIQUES["pairwise_testing"]
        recommendations.append({
            **tech,
            "relevance": "medium",
            "reasoning": f"Multiple platform/browser/device combinations detected. Pairwise reduces test count while maintaining coverage.",
        })

    if analysis["has_forms"] or analysis["has_crud"]:
        tech = TEST_DESIGN_TECHNIQUES["error_guessing"]
        reasons = []
        if analysis["has_forms"]:
            reasons.append("form inputs requiring validation")
        if analysis["has_crud"]:
            reasons.append("data operations (create/edit/delete)")
        recommendations.append({
            **tech,
            "relevance": "high",
            "reasoning": f"Your project has {', '.join(reasons)}. Error guessing catches edge cases that formal techniques miss.",
        })
    else:
        tech = TEST_DESIGN_TECHNIQUES["error_guessing"]
        recommendations.append({
            **tech, "relevance": "medium",
            "reasoning": "Complements formal techniques. Helps find defects based on tester experience.",
        })

    # Exploratory — relevance depends on project maturity
    tech = TEST_DESIGN_TECHNIQUES["exploratory_testing"]
    if analysis["content_length"] < 100:
        recommendations.append({
            **tech, "relevance": "high",
            "reasoning": "Limited requirements available. Exploratory testing is critical when documentation is sparse.",
        })
    else:
        recommendations.append({
            **tech, "relevance": "medium",
            "reasoning": "Complements scripted tests. Discovers issues not covered by predefined test cases.",
        })

    # Use Case — high relevance when there are clear user flows
    tech = TEST_DESIGN_TECHNIQUES["use_case_testing"]
    if analysis["has_auth"] or analysis["has_payments"] or analysis["has_workflow"]:
        recommendations.append({
            **tech, "relevance": "high",
            "reasoning": "Clear user flows detected (authentication, transactions, workflows). Use Case testing validates end-to-end business scenarios.",
        })
    else:
        recommendations.append({
            **tech, "relevance": "medium",
            "reasoning": "Tests complete business processes from the user's perspective.",
        })

    return recommendations


# ═══════════════════════════════════════════════════════════════════
# 2. Testing Types Advisor (dynamic)
# ═══════════════════════════════════════════════════════════════════

def recommend_testing_types(ctx: ProjectContext, content_text: str = "") -> list[dict]:
    """Recommend testing types based on domain, platform, AND actual content."""
    domain_info = DOMAIN_FOCUS.get(ctx.domain, DOMAIN_FOCUS["other"])
    key_types = set(domain_info.get("key_testing_types", []))
    analysis = _analyze_content(content_text)

    # Dynamically add types based on content analysis
    if analysis["has_auth"] or analysis["has_security"]:
        key_types.add("security")
    if analysis["has_api"]:
        key_types.add("api_testing")
    if analysis["has_performance"]:
        key_types.add("performance")
    if analysis["has_mobile"] or ctx.platform in ("mobile_native", "mobile_hybrid"):
        key_types.add("mobile_specific")
    if analysis["has_accessibility"]:
        key_types.add("accessibility")
    if analysis["has_localization"]:
        key_types.add("localization")
    if analysis["has_database"]:
        key_types.add("database")
    if analysis["has_data_tables"] or analysis["has_forms"]:
        key_types.add("ui_ux")

    # Always include functional and regression
    key_types.update({"functional", "regression"})

    recommendations = []
    for type_key in key_types:
        ttype = TESTING_TYPES.get(type_key)
        if not ttype:
            continue

        reasoning_parts = []
        is_required = type_key in {"functional", "regression"}

        # Content-driven reasoning
        if type_key == "security":
            parts = []
            if analysis["has_auth"]:
                parts.append("authentication system")
            if analysis["has_payments"]:
                parts.append("payment processing")
                is_required = True
            if analysis["has_security"]:
                parts.append("security requirements mentioned")
                is_required = True
            reasoning_parts.append(f"Required: project has {', '.join(parts or ['user data handling'])}.")

        elif type_key == "api_testing":
            if analysis["has_api"]:
                reasoning_parts.append("API endpoints detected in requirements. Validate contracts, error handling, and data integrity.")
                is_required = True

        elif type_key == "performance":
            if analysis["has_performance"]:
                reasoning_parts.append("Performance requirements explicitly mentioned.")
                is_required = True
            elif analysis["has_realtime"]:
                reasoning_parts.append("Real-time features require load and stress testing.")

        elif type_key == "accessibility":
            if analysis["has_accessibility"]:
                reasoning_parts.append("Accessibility requirements detected. WCAG 2.1 compliance testing needed.")
                is_required = True
            elif any(m in ctx.target_markets for m in ["EU", "US"]):
                reasoning_parts.append("WCAG 2.1 compliance recommended for EU/US markets.")

        elif type_key == "mobile_specific":
            reasoning_parts.append("Mobile platform detected. Test gestures, orientation, offline mode, push notifications.")

        elif type_key in domain_info.get("key_testing_types", []):
            reasoning_parts.append(f"Critical for the {domain_info['name']} domain.")
            is_required = True

        if not reasoning_parts:
            reasoning_parts.append(ttype["description"])

        recommendations.append({
            "key": type_key,
            "name": ttype["name"],
            "description": ttype["description"],
            "subtypes": ttype.get("subtypes", []),
            "priority": "Required" if is_required else "Recommended",
            "reasoning": " ".join(reasoning_parts),
        })

    # Sort: Required first
    recommendations.sort(key=lambda r: (0 if r["priority"] == "Required" else 1, r["name"]))
    return recommendations


# ═══════════════════════════════════════════════════════════════════
# 3. Tools Advisor (dynamic)
# ═══════════════════════════════════════════════════════════════════

def recommend_tools(ctx: ProjectContext, content_text: str = "") -> list[dict]:
    """Recommend testing tools based on project context and content."""
    analysis = _analyze_content(content_text)
    recommendations = []

    # Always recommend test management & bug tracking
    always_categories = ["test_management", "bug_tracking"]
    conditional = {
        "api_testing": ctx.has_api or analysis.get("has_api", False),
        "browser_testing": ctx.platform in ("web", "mobile_hybrid"),
        "performance_testing": (ctx.domain in ("e-commerce", "fintech", "saas")
                                or ctx.has_realtime or analysis.get("has_performance", False)),
        "security_testing": (ctx.has_payments or ctx.domain in ("fintech", "healthcare")
                             or analysis.get("has_security", False)),
        "accessibility_testing": (any(m in ctx.target_markets for m in ["EU", "US"])
                                  or analysis.get("has_accessibility", False)),
        "mobile_testing": ctx.platform in ("mobile_native", "mobile_hybrid") or analysis.get("has_mobile", False),
        "collaboration": True,
    }

    budget_filter = {
        "free": ["Open Source", "Free", "Freemium"],
        "low": ["Open Source", "Free", "Freemium"],
        "medium": ["Open Source", "Free", "Freemium", "Commercial"],
        "high": ["Open Source", "Free", "Freemium", "Commercial"],
    }
    allowed_types = budget_filter.get(ctx.budget, budget_filter["medium"])

    for cat_key in always_categories:
        cat = TESTING_TOOLS.get(cat_key)
        if cat:
            filtered = [t for t in cat["tools"] if t["type"] in allowed_types]
            if filtered:
                recommendations.append({
                    "category": cat["category"],
                    "priority": "Required",
                    "tools": filtered[:3],
                })

    for cat_key, should_include in conditional.items():
        if should_include:
            cat = TESTING_TOOLS.get(cat_key)
            if cat:
                filtered = [t for t in cat["tools"] if t["type"] in allowed_types]
                if filtered:
                    recommendations.append({
                        "category": cat["category"],
                        "priority": "Recommended",
                        "tools": filtered[:3],
                    })

    return recommendations


# ═══════════════════════════════════════════════════════════════════
# 4. No-Code Automation Advisor
# ═══════════════════════════════════════════════════════════════════

def recommend_nocode(ctx: ProjectContext) -> list[dict]:
    """Recommend no-code automation tools when applicable."""
    platform_map = {
        "web": "Web",
        "mobile_native": "Mobile",
        "mobile_hybrid": "Web",
        "desktop": "Desktop",
    }
    target_platform = platform_map.get(ctx.platform, "Web")

    recommendations = []
    for tool in NOCODE_TOOLS:
        if target_platform in tool["platforms"]:
            reasoning = tool["reasoning"]
            relevance_score = 1

            if ctx.release_frequency in ("daily", "weekly"):
                reasoning += " Frequent releases require regression automation."
                relevance_score += 1
            if ctx.team_size == "small":
                reasoning += " Suitable for a small team."
                relevance_score += 1
            if ctx.budget == "free" and tool["type"] in ("Commercial", "AI-powered"):
                continue

            recommendations.append({
                "name": tool["name"],
                "type": tool["type"],
                "platforms": tool["platforms"],
                "description": tool["description"],
                "reasoning": reasoning,
                "best_for": tool["best_for"],
                "relevance": relevance_score,
            })

    return recommendations[:4]


# ═══════════════════════════════════════════════════════════════════
# 5. Focus Areas Advisor (dynamic)
# ═══════════════════════════════════════════════════════════════════

def recommend_focus_areas(ctx: ProjectContext, content_text: str = "") -> dict:
    """Recommend focus areas based on domain AND actual content analysis."""
    domain_info = DOMAIN_FOCUS.get(ctx.domain, DOMAIN_FOCUS["other"])
    platform_info = PLATFORM_SPECIFICS.get(ctx.platform, PLATFORM_SPECIFICS["web"])
    analysis = _analyze_content(content_text)

    # Start with domain defaults
    critical_modules = list(domain_info["critical_modules"])
    edge_cases = list(domain_info["edge_cases"])

    # Add content-driven critical modules
    if analysis["has_auth"] and not any("auth" in m.get("module", "").lower() for m in critical_modules):
        critical_modules.append({
            "module": "Authentication & Authorization",
            "why": "Detected in your requirements. Incorrect auth logic can expose sensitive data."
        })
    if analysis["has_payments"] and not any("payment" in m.get("module", "").lower() for m in critical_modules):
        critical_modules.append({
            "module": "Payment Processing",
            "why": "Detected in your requirements. Payment bugs directly impact revenue and compliance."
        })
    if analysis["has_search"] and not any("search" in m.get("module", "").lower() for m in critical_modules):
        critical_modules.append({
            "module": "Search & Filtering",
            "why": "Detected in your requirements. Search accuracy directly affects user experience."
        })
    if analysis["has_file_ops"]:
        critical_modules.append({
            "module": "File Upload / Export",
            "why": "Detected in your requirements. File operations need size limits, format validation, and security checks."
        })

    # Add content-driven edge cases
    if analysis["has_forms"]:
        edge_cases.append("Empty form submission, max-length input, special characters in all fields")
    if analysis["has_auth"]:
        edge_cases.append("Brute-force login attempts, expired sessions, concurrent logins from different devices")
    if analysis["has_payments"]:
        edge_cases.append("Double payment, payment timeout, partial refund, currency conversion rounding")
    if analysis["has_realtime"]:
        edge_cases.append("Connection drop during real-time updates, message ordering, reconnection handling")

    return {
        "domain": domain_info["name"],
        "critical_modules": critical_modules,
        "edge_cases": edge_cases,
        "platform_must_test": platform_info["must_test"],
        "platform_tools": platform_info["recommended_tools"],
        "standards": domain_info.get("standards", []),
        "analysis": analysis,
    }


# ═══════════════════════════════════════════════════════════════════
# 6. Standards Advisor (dynamic)
# ═══════════════════════════════════════════════════════════════════

def recommend_standards(ctx: ProjectContext, content_text: str = "") -> list[dict]:
    """Recommend applicable standards based on domain, features, AND content."""
    domain_info = DOMAIN_FOCUS.get(ctx.domain, DOMAIN_FOCUS["other"])
    domain_standards = domain_info.get("standards", [])
    analysis = _analyze_content(content_text)

    recommendations = []
    always = ["ISTQB", "OWASP Top 10", "ISO 25010"]

    for std_key, std_info in STANDARDS.items():
        is_relevant = False
        reasoning = std_info["relevance"]

        if std_key in always:
            is_relevant = True
        else:
            for ds in domain_standards:
                if std_key.lower() in ds.lower():
                    is_relevant = True
                    reasoning = f"Required for the {domain_info['name']} domain. {reasoning}"
                    break

        # Content-driven relevance
        if std_key == "GDPR" and ("EU" in ctx.target_markets or analysis.get("has_auth")):
            is_relevant = True
            if "EU" in ctx.target_markets:
                reasoning = "Required for the EU market. " + reasoning
        if std_key == "WCAG 2.1/2.2":
            if analysis.get("has_accessibility"):
                is_relevant = True
                reasoning = "Accessibility requirements detected in your content. " + reasoning
            elif any(m in ctx.target_markets for m in ["EU", "US"]):
                is_relevant = True
                reasoning = "Recommended for EU/US markets. " + reasoning
        if std_key == "PCI DSS" and (ctx.has_payments or analysis.get("has_payments")):
            is_relevant = True
            reasoning = "Payment processing detected. PCI DSS compliance required. " + reasoning

        if is_relevant:
            recommendations.append({
                "name": std_key,
                "full_name": std_info["full_name"],
                "relevance": reasoning,
            })

    return recommendations
