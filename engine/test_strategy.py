"""TestFortge — Test Strategy Agent (Stage 2 of architectural rework).

Given a :class:`engine.site_recon.SiteProfile`, returns a
:class:`TestStrategy` — a matrix mapping each test category to a
ranked list of concrete check specs that apply to THIS site.

Categories
----------
Closed set, matches the user requirement (UI/UX, Usability,
Accessibility, Performance, Compatibility, Localization, i18n) plus
the always-on Functional bucket. The matrix may omit a category
when the site doesn't justify it (e.g. no Performance focus on a
3-page landing).

ISTQB grounding
---------------
The system prompt embeds a curated grounding block pulled from the
local ISTQB RAG corpus (:mod:`engine.istqb_rag`). One retrieval per
call, scored against the SiteProfile description + flows, so the
LLM gets terminology + test-design technique hints (EP, BVA,
decision tables) without inventing them.

Fallback
--------
Same contract as Site Recon: missing API key or exhausted retries
return a deterministic rule-based strategy. ``strategy.source``
indicates which path was taken.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any

from engine.llm_client import LLMUnavailable, call_messages
from engine.log import get_logger
from engine.site_recon import SiteProfile

_logger = get_logger(__name__)


_STRATEGY_MODEL = os.environ.get("ANTHROPIC_MODEL_SONNET", "claude-sonnet-4-6")
_STRATEGY_MAX_TOKENS = int(os.environ.get("STRATEGY_MAX_TOKENS", "4000"))


# Closed enum — generator and Live Executor (Stage 3) both consume
# this verbatim, so adding a category requires updates downstream.
CATEGORIES = (
    "Functional",
    "UI_UX",
    "Usability",
    "Accessibility",
    "Performance",
    "Compatibility",
    "Localization",
    "Internationalization",
)

PRIORITIES = ("High", "Medium", "Low")


@dataclass
class CheckSpec:
    """One concrete check the suite generator can turn into a TC/CL.

    ``url_pattern`` is the fnmatch glob the Live Executor (Stage 3)
    matches against the current page URL to decide whether to fire
    this check during walkthrough — empty string means "any page".
    """
    objective: str                # "Verify primary CTA is keyboard-reachable"
    rationale: str = ""           # 1-line "why this matters here"
    priority: str = "Medium"      # High | Medium | Low
    url_pattern: str = ""         # fnmatch glob; empty = all pages
    istqb_technique: str = ""     # EP | BVA | Decision Table | ...

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TestStrategy:
    """Per-category list of CheckSpecs, prioritised."""
    site_url: str
    matrix: dict[str, list[CheckSpec]] = field(default_factory=dict)
    source: str = "rule_based"
    rationale: str = ""           # 1-paragraph "why this matrix for this site"

    def to_dict(self) -> dict:
        return {
            "site_url": self.site_url,
            "matrix": {
                cat: [c.to_dict() for c in checks]
                for cat, checks in self.matrix.items()
            },
            "source": self.source,
            "rationale": self.rationale,
        }

    def total_checks(self) -> int:
        return sum(len(v) for v in self.matrix.values())


# ── Public API ────────────────────────────────────────────────────

def build_strategy(profile: SiteProfile, *,
                   force_llm: bool = False) -> TestStrategy:
    """Produce a :class:`TestStrategy` for ``profile``."""
    api_key_set = bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())
    if not api_key_set and not force_llm:
        return _rule_based_strategy(profile)

    grounding = _build_grounding(profile)
    try:
        raw = _call_llm(profile, grounding)
    except LLMUnavailable as exc:
        _logger.warning("test_strategy: LLM unavailable, fallback: %s", exc)
        return _rule_based_strategy(profile)
    except Exception as exc:  # pragma: no cover — defensive
        _logger.warning("test_strategy: LLM call failed: %s", exc)
        return _rule_based_strategy(profile)

    parsed = _parse_llm_response(raw)
    if parsed is None:
        _logger.warning("test_strategy: unparseable LLM response, fallback")
        return _rule_based_strategy(profile)

    return _build_strategy_from_parsed(parsed, profile, source="llm")


# ── ISTQB grounding ──────────────────────────────────────────────

def _build_grounding(profile: SiteProfile) -> str:
    """Retrieve up to 3 ISTQB chunks relevant to the profile's flows.

    Returns a plain-text block we paste into the system prompt — kept
    in the system block so prompt-cache survives between successive
    Strategy calls on similar sites.
    """
    try:
        from engine import istqb_rag
    except Exception:  # pragma: no cover — corpus optional
        return ""

    # Build a single query from the site signals — we don't need
    # separate retrievals per category; one focused query gives the
    # model the vocabulary it needs across all categories.
    parts = [profile.site_type]
    parts.extend(profile.primary_flows)
    if profile.has_auth:
        parts.append("authentication login")
    if profile.has_payment:
        parts.append("payment checkout")
    if profile.has_forms:
        parts.append("forms validation")
    parts.append("web application test design technique")
    query = " ".join(parts)

    try:
        hits = istqb_rag.search(query, k=3)
    except Exception as exc:  # pragma: no cover — best-effort
        _logger.debug("test_strategy: RAG search failed: %s", exc)
        return ""

    if not hits:
        return ""

    chunks = []
    for h in hits:
        text = (h.get("text") or "").strip()
        if not text:
            continue
        ref = f"{h.get('source', 'ISTQB')} · page {h.get('page', '?')}"
        chunks.append(f"[{ref}]\n{text[:600]}")
    return "\n\n".join(chunks)


# ── LLM call ─────────────────────────────────────────────────────

_SYSTEM_PROMPT_HEAD = (
    "You are a senior QA strategist designing a test matrix for a "
    "specific website. You receive a structured site profile from "
    "the recon agent and emit a category→checks matrix as JSON.\n\n"
    "Rules:\n"
    "1. Use ONLY these categories: "
    + ", ".join(CATEGORIES) + ".\n"
    "2. OMIT a category when it doesn't apply (a 3-page landing "
    "doesn't need a Performance suite — say so by omitting the key).\n"
    "3. Each check is a single, specific, verifiable objective. "
    "Avoid duplicates across categories.\n"
    "4. Priorities: High = must-pass before release; Medium = catch "
    "regressions; Low = nice-to-have.\n"
    "5. url_pattern uses fnmatch globs (e.g. '/checkout/*'); empty "
    "string means 'every page'.\n"
    "6. Reference ISTQB techniques when relevant: EP "
    "(Equivalence Partitioning), BVA (Boundary Value Analysis), "
    "Decision Table, State Transition, Use Case.\n"
    "7. Respond with ONE valid JSON object — no prose, no markdown."
)


def _system_blocks(grounding: str) -> list[dict]:
    """Return the system-message blocks with prompt caching on the
    static parts (head + grounding). Caching the grounding block in
    particular saves ~400 tokens on every re-run."""
    blocks = [{
        "type": "text",
        "text": _SYSTEM_PROMPT_HEAD,
        "cache_control": {"type": "ephemeral"},
    }]
    if grounding:
        blocks.append({
            "type": "text",
            "text": "ISTQB grounding (verbatim from syllabus):\n\n"
                    + grounding,
            "cache_control": {"type": "ephemeral"},
        })
    return blocks


def _build_user_prompt(profile: SiteProfile) -> str:
    schema_hint = (
        "{\n"
        '  "rationale": "1-paragraph summary of why this matrix fits THIS site",\n'
        '  "matrix": {\n'
        '    "Functional": [\n'
        '      {"objective": "...", "rationale": "...", '
        '"priority": "High|Medium|Low", "url_pattern": "...", '
        '"istqb_technique": "EP|BVA|..."}\n'
        "    ],\n"
        '    "UI_UX": [...], "Usability": [...], "Accessibility": [...],\n'
        '    "Performance": [...], "Compatibility": [...],\n'
        '    "Localization": [...], "Internationalization": [...]\n'
        "  }\n"
        "}\n"
    )
    return (
        "Site profile:\n"
        + json.dumps(profile.to_dict(), ensure_ascii=False, indent=2)
        + "\n\nReturn JSON matching this shape exactly:\n"
        + schema_hint
        + "\nKeep each category between 3 and 8 checks. Total checks "
          "across all categories: 18-45 depending on site complexity."
    )


def _call_llm(profile: SiteProfile, grounding: str) -> str:
    resp = call_messages(
        model=_STRATEGY_MODEL,
        max_tokens=_STRATEGY_MAX_TOKENS,
        system=_system_blocks(grounding),
        messages=[{
            "role": "user",
            "content": _build_user_prompt(profile),
        }],
    )
    usage = getattr(resp, "usage", None)
    if usage is not None:
        _logger.info(
            "test_strategy usage: input=%s output=%s cache_read=%s",
            getattr(usage, "input_tokens", 0),
            getattr(usage, "output_tokens", 0),
            getattr(usage, "cache_read_input_tokens", 0),
        )
    chunks: list[str] = []
    for block in getattr(resp, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            chunks.append(text)
    return "\n".join(chunks).strip()


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_llm_response(raw: str) -> dict | None:
    if not raw:
        return None
    m = _JSON_OBJ_RE.search(raw)
    if m is None:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# ── Coerce + merge ───────────────────────────────────────────────

def _coerce_priority(value: Any) -> str:
    s = str(value or "").strip().title()
    return s if s in PRIORITIES else "Medium"


def _coerce_check(raw: dict) -> CheckSpec | None:
    if not isinstance(raw, dict):
        return None
    objective = str(raw.get("objective") or "").strip()
    if not objective:
        return None
    return CheckSpec(
        objective=objective[:300],
        rationale=str(raw.get("rationale") or "").strip()[:280],
        priority=_coerce_priority(raw.get("priority")),
        url_pattern=str(raw.get("url_pattern") or "").strip()[:200],
        istqb_technique=str(raw.get("istqb_technique") or "").strip()[:80],
    )


def _build_strategy_from_parsed(parsed: dict, profile: SiteProfile,
                                 *, source: str) -> TestStrategy:
    matrix: dict[str, list[CheckSpec]] = {}
    raw_matrix = parsed.get("matrix") or {}
    if not isinstance(raw_matrix, dict):
        return _rule_based_strategy(profile)

    for cat, raw_checks in raw_matrix.items():
        if cat not in CATEGORIES:
            continue
        if not isinstance(raw_checks, list):
            continue
        checks: list[CheckSpec] = []
        for raw in raw_checks:
            spec = _coerce_check(raw)
            if spec is None:
                continue
            checks.append(spec)
            if len(checks) >= 12:
                break
        if checks:
            matrix[cat] = checks

    if not matrix:
        return _rule_based_strategy(profile)

    return TestStrategy(
        site_url=profile.url,
        matrix=matrix,
        source=source,
        rationale=str(parsed.get("rationale") or "")[:600],
    )


# ── Rule-based fallback ──────────────────────────────────────────

def _rule_based_strategy(profile: SiteProfile) -> TestStrategy:
    """Deterministic strategy from SiteProfile flags. Same shape as
    the LLM path so downstream code never branches on source.

    Coverage is intentionally narrower than what the LLM produces —
    this is a safety net, not the primary path. Roughly 12-25 checks
    instead of the LLM's 18-45.
    """
    matrix: dict[str, list[CheckSpec]] = {}

    # Functional — always on.
    func: list[CheckSpec] = [
        CheckSpec(
            objective="Verify homepage loads under 3s and "
                      "renders title + main H1",
            rationale="Baseline reachability check",
            priority="High", url_pattern="/",
            istqb_technique="Use Case",
        ),
        CheckSpec(
            objective="Verify all internal navigation links resolve "
                      "without 404/5xx",
            rationale="Common regression after content updates",
            priority="High",
        ),
        CheckSpec(
            objective="Verify no JavaScript errors are logged in the "
                      "browser console on any page",
            rationale="Console errors usually break interactions",
            priority="High",
        ),
    ]
    if profile.has_auth:
        func.append(CheckSpec(
            objective="Verify successful login routes to the "
                      "post-login destination",
            priority="High", url_pattern="*/login*",
            istqb_technique="Use Case",
        ))
        func.append(CheckSpec(
            objective="Verify login fails with appropriate error for "
                      "wrong credentials",
            priority="High", url_pattern="*/login*",
            istqb_technique="EP",
        ))
    if profile.has_forms:
        func.append(CheckSpec(
            objective="Verify forms reject empty required fields with "
                      "inline validation",
            priority="High", istqb_technique="EP",
        ))
        func.append(CheckSpec(
            objective="Verify form boundaries (max length, min length, "
                      "format) are enforced",
            priority="Medium", istqb_technique="BVA",
        ))
    if profile.has_payment:
        func.append(CheckSpec(
            objective="Verify checkout flow advances through every step "
                      "to confirmation",
            priority="High", url_pattern="*/checkout*",
            istqb_technique="State Transition",
        ))
        func.append(CheckSpec(
            objective="Verify invalid card numbers are rejected at "
                      "payment step",
            priority="High", url_pattern="*/checkout*",
            istqb_technique="EP",
        ))
    if profile.has_search:
        func.append(CheckSpec(
            objective="Verify search returns matching results for a "
                      "valid query",
            priority="Medium", istqb_technique="EP",
        ))
        func.append(CheckSpec(
            objective="Verify search shows empty-state for no matches",
            priority="Medium",
        ))
    matrix["Functional"] = func

    # UI/UX — every site benefits from at least these.
    matrix["UI_UX"] = [
        CheckSpec(
            objective="Verify visual hierarchy: primary CTA stands out "
                      "from secondary actions",
            priority="Medium",
        ),
        CheckSpec(
            objective="Verify images render without broken-image icons "
                      "(naturalWidth>0)",
            priority="Medium",
        ),
        CheckSpec(
            objective="Verify hover/focus states on interactive elements",
            priority="Low",
        ),
    ]

    # Usability — light by default; expanded when the site has flows.
    use: list[CheckSpec] = [
        CheckSpec(
            objective="Verify users can complete the primary task in "
                      "≤3 clicks from the homepage",
            priority="Medium",
        ),
    ]
    if profile.has_forms:
        use.append(CheckSpec(
            objective="Verify form labels are persistent (not "
                      "placeholder-only)",
            priority="Medium",
        ))
    matrix["Usability"] = use

    # Accessibility — always on; a11y debt is expensive to retrofit.
    matrix["Accessibility"] = [
        CheckSpec(
            objective="Verify all interactive elements are reachable "
                      "via keyboard (Tab order is logical)",
            priority="High",
        ),
        CheckSpec(
            objective="Verify text-to-background colour contrast meets "
                      "WCAG 2.1 AA (4.5:1)",
            priority="High",
        ),
        CheckSpec(
            objective="Verify all images have meaningful alt attributes",
            priority="Medium",
        ),
        CheckSpec(
            objective="Verify all form inputs have associated labels "
                      "(for/id pair or aria-label)",
            priority="High",
        ),
    ]

    # Performance — skipped on tiny landings, light otherwise.
    if profile.site_type != "landing" or len(profile.key_pages) > 3:
        matrix["Performance"] = [
            CheckSpec(
                objective="Verify LCP (Largest Contentful Paint) under "
                          "2.5s on a wired connection",
                priority="Medium",
            ),
            CheckSpec(
                objective="Verify no single image asset exceeds 500KB",
                priority="Low",
            ),
        ]

    # Compatibility — always on for any non-trivial site.
    matrix["Compatibility"] = [
        CheckSpec(
            objective="Verify layout renders without overflow at "
                      "375px (mobile) and 1440px (desktop) widths",
            priority="High",
        ),
        CheckSpec(
            objective="Verify primary flows in current Chrome, "
                      "Firefox, and Safari",
            priority="Medium",
        ),
    ]

    # Localization / i18n — only when there's any sign of multilingual
    # content; skipped for single-language sites to keep the matrix
    # focused.
    notes_blob = " ".join(profile.tech_hints).lower()
    if any(w in notes_blob for w in ("i18n", "lang", "translate",
                                      "locale", "rtl")):
        matrix["Localization"] = [
            CheckSpec(
                objective="Verify all visible strings come from the "
                          "translation file (no hard-coded English on "
                          "non-English pages)",
                priority="Medium",
            ),
        ]
        matrix["Internationalization"] = [
            CheckSpec(
                objective="Verify date and number formats follow the "
                          "active locale",
                priority="Medium",
            ),
        ]

    return TestStrategy(
        site_url=profile.url,
        matrix=matrix,
        source="rule_based",
        rationale=(
            "Rule-based fallback: covers Functional + UI/UX + "
            "Usability + Accessibility on every site, scales "
            "Performance / Compatibility / i18n with crawler flags."
        ),
    )


__all__ = [
    "CheckSpec", "TestStrategy", "build_strategy",
    "CATEGORIES", "PRIORITIES",
]
