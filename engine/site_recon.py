"""TestFortge — Site Recon Agent (Stage 2 of architectural rework).

Turns a raw :class:`engine.site_crawler.SiteAnalysis` into a
:class:`SiteProfile` — a compact, LLM-classified description of WHAT
the site is (landing / e-commerce / SaaS / form-heavy / blog / docs),
WHAT user flows it exposes, and WHICH technical hints matter for
testing. The profile is the single input to
:mod:`engine.test_strategy`, which decides which test categories
apply and at what priority.

Why a separate module
---------------------
Today :func:`engine.qa_persona.analyze_input` does rule-based area
detection from ``SiteAnalysis``. It works, but the heuristics are
shallow — "has a form" doesn't tell us whether the form is a
newsletter signup or a multi-step checkout. The Recon Agent gives
Claude Sonnet 4.6 the structural facts plus a small slice of page
content and asks for one structured JSON answer. Result is cached
in the ``site_profile`` table (see ``engine.db.save_site_profile``)
so a second Generate on the same URL skips the API call.

Fallback
--------
When ``ANTHROPIC_API_KEY`` is absent or all 3 retries are exhausted,
:func:`recon_site` returns a deterministic rule-based profile — same
shape, sourced from the existing ``SiteAnalysis`` flags. Callers
should NOT branch on ``profile.source`` for product logic; it exists
only for telemetry and debugging.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any

from engine.llm_client import LLMUnavailable, call_messages
from engine.log import get_logger

_logger = get_logger(__name__)


# Sonnet 4.6 by default; operator can swap via env without code change.
# Separate from the chatbot's ANTHROPIC_MODEL so changing the chat
# model never accidentally drags the recon prompt with it.
# Routed by work kind at call time — see engine/llm_models.py.
_RECON_KIND = "analysis"
_RECON_MAX_TOKENS = int(os.environ.get("RECON_MAX_TOKENS", "1500"))


# Closed enum for site_type — Strategy Agent matches on this verbatim,
# so adding a new value here also requires an update in test_strategy.
SITE_TYPES = (
    "ecommerce", "saas", "landing", "form_heavy",
    "blog", "docs", "marketplace", "portfolio", "generic",
)

# Closed enum for primary_flows entries.
PRIMARY_FLOWS = (
    "auth", "signup", "checkout", "payment", "search",
    "subscription", "lead_capture", "content_browsing",
    "file_upload", "messaging", "navigation",
)


@dataclass
class SiteProfile:
    """Compact LLM-classified description of a crawled site.

    Each field is small enough to round-trip through JSON (DB stores
    the dict verbatim). ``raw_evidence`` is the slice of SiteAnalysis
    facts that fed the LLM — handy for debugging a strange profile.
    """
    url: str
    site_type: str = "generic"            # one of SITE_TYPES
    primary_flows: list[str] = field(default_factory=list)
    tech_hints: list[str] = field(default_factory=list)
    has_auth: bool = False
    has_payment: bool = False
    has_search: bool = False
    has_forms: bool = False
    key_pages: list[dict] = field(default_factory=list)   # [{url, role}]
    description: str = ""
    target_audience: str = ""
    source: str = "rule_based"            # "llm" | "rule_based" | "cache"
    raw_evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ── Public API ────────────────────────────────────────────────────

def recon_site(site_analysis, *, force_llm: bool = False) -> SiteProfile:
    """Classify ``site_analysis`` into a :class:`SiteProfile`.

    Parameters
    ----------
    site_analysis:
        :class:`engine.site_crawler.SiteAnalysis` instance.
    force_llm:
        Bypass the rule-based fallback even when the API key is
        missing — only used by tests that mock ``call_messages``.

    Returns
    -------
    SiteProfile
        Always returns a valid profile. On LLM failure the profile is
        built from ``site_analysis`` flags; the caller can tell from
        ``profile.source`` whether the LLM was reachable.
    """
    evidence = _build_evidence(site_analysis)

    api_key_set = bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())
    if not api_key_set and not force_llm:
        return _rule_based_profile(site_analysis, evidence)

    try:
        raw = _call_llm(evidence)
    except LLMUnavailable as exc:
        _logger.warning("site_recon: LLM unavailable, using rule-based "
                        "fallback: %s", exc)
        return _rule_based_profile(site_analysis, evidence)
    except Exception as exc:  # pragma: no cover — defensive
        _logger.warning("site_recon: LLM call failed: %s", exc)
        return _rule_based_profile(site_analysis, evidence)

    parsed = _parse_llm_response(raw)
    if parsed is None:
        _logger.warning("site_recon: LLM returned unparseable response, "
                        "falling back to rules")
        return _rule_based_profile(site_analysis, evidence)

    return _merge_profile(parsed, site_analysis, evidence, source="llm")


# ── Evidence builder ──────────────────────────────────────────────

def _build_evidence(site_analysis) -> dict:
    """Pack the structural facts the LLM needs into a small dict.

    Keep this <=2 KB — the LLM doesn't need raw HTML, just the
    classifier-relevant signals. The prompt also reproduces this so
    the model knows what each field means.
    """
    if site_analysis is None:
        return {}
    pages_sample = []
    for p in (getattr(site_analysis, "pages", None) or [])[:6]:
        if getattr(p, "error", ""):
            continue
        pages_sample.append({
            "url": getattr(p, "url", ""),
            "title": (getattr(p, "title", "") or "")[:120],
            "h1": (getattr(p, "h1", "") or "")[:120],
            "nav_links": list(getattr(p, "nav_links", []) or [])[:8],
            "buttons": list(getattr(p, "buttons", []) or [])[:6],
            "form_count": len(getattr(p, "forms", []) or []),
            # Layout tables are already excluded by the crawler, so a
            # non-zero count really does mean "this page lists records".
            "grid_count": len(getattr(p, "tables", []) or []),
        })
    return {
        "base_url": getattr(site_analysis, "base_url", "") or "",
        "domain": getattr(site_analysis, "domain", "") or "",
        "page_count": int(getattr(site_analysis, "page_count", 0) or 0),
        "nav_items": list(getattr(site_analysis, "nav_items", []) or [])[:12],
        "features_detected":
            list(getattr(site_analysis, "features_detected", []) or []),
        "has_auth": bool(getattr(site_analysis, "has_auth", False)),
        "has_search": bool(getattr(site_analysis, "has_search", False)),
        "has_forms": bool(getattr(site_analysis, "has_forms", False)),
        "has_payment": bool(getattr(site_analysis, "has_payment", False)),
        "crawler_site_type":
            getattr(site_analysis, "site_type", "generic") or "generic",
        "architecture_notes":
            list(getattr(site_analysis, "architecture_notes", []) or [])[:6],
        "pages": pages_sample,
    }


# ── LLM call ──────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a senior QA architect who reads crawler evidence and "
    "classifies websites for testing. Respond ONLY with one valid JSON "
    "object matching the schema in the user message — no prose, no "
    "markdown fences, no comments."
)


def _build_user_prompt(evidence: dict) -> str:
    return (
        "Classify this crawled site for QA test planning. Return one "
        "JSON object with these keys EXACTLY:\n"
        "  site_type: one of "
        f"{list(SITE_TYPES)}\n"
        "  primary_flows: array, subset of "
        f"{list(PRIMARY_FLOWS)}\n"
        "  tech_hints: array of short strings (e.g. 'wordpress', "
        "'react-spa', 'cdn-images', 'webflow', 'shopify'); empty when "
        "unsure.\n"
        "  has_auth, has_payment, has_search, has_forms: booleans — "
        "use crawler evidence; override only when page content "
        "contradicts a flag.\n"
        "  key_pages: up to 6 objects of shape "
        "{\"url\": string, \"role\": string} where role is one of "
        "[homepage, product, pricing, login, signup, checkout, "
        "contact, about, search, blog, dashboard, docs, other].\n"
        "  description: <=180 chars, plain English, what the site IS, "
        "not what it does.\n"
        "  target_audience: <=120 chars (e.g. 'B2B SaaS buyers', "
        "'retail shoppers', 'developers reading API docs').\n\n"
        "Crawler evidence:\n"
        f"{json.dumps(evidence, ensure_ascii=False, indent=2)}\n"
    )


def _call_llm(evidence: dict) -> str:
    """Issue the Anthropic call. Raises :class:`LLMUnavailable` on
    transient or terminal failure (caller turns that into a rule-based
    fallback)."""
    resp = call_messages(
        kind=_RECON_KIND,
        max_tokens=_RECON_MAX_TOKENS,
        system=[{
            "type": "text",
            "text": _SYSTEM_PROMPT,
            # 5-min cache window — the system block never changes,
            # so two consecutive recon calls in the same session
            # share the cached tokens.
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{
            "role": "user",
            "content": _build_user_prompt(evidence),
        }],
    )
    usage = getattr(resp, "usage", None)
    if usage is not None:
        _logger.info(
            "site_recon usage: input=%s output=%s cache_read=%s",
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
    """Extract the first JSON object from the LLM response.

    Sonnet 4.6 with our system prompt returns clean JSON ~always, but
    we still allow a leading code fence (\\`\\`\\`json) or trailing prose
    just in case — operator-reported defensive parsing earns its keep
    over a quarter when models drift.
    """
    if not raw:
        return None
    m = _JSON_OBJ_RE.search(raw)
    if m is None:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# ── Merge + fallback ──────────────────────────────────────────────

def _coerce_site_type(value: Any) -> str:
    s = str(value or "").strip().lower()
    return s if s in SITE_TYPES else "generic"


def _coerce_flow_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        s = str(v or "").strip().lower()
        if s in PRIMARY_FLOWS and s not in out:
            out.append(s)
    return out


def _coerce_string_list(value: Any, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for v in value:
        s = str(v or "").strip()
        if s and s not in out:
            out.append(s[:60])
        if len(out) >= limit:
            break
    return out


def _coerce_key_pages(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for v in value:
        if not isinstance(v, dict):
            continue
        url = str(v.get("url") or "").strip()[:500]
        role = str(v.get("role") or "").strip().lower()[:40]
        if not url:
            continue
        out.append({"url": url, "role": role or "other"})
        if len(out) >= 6:
            break
    return out


def _merge_profile(parsed: dict, site_analysis, evidence: dict,
                   *, source: str) -> SiteProfile:
    """Normalise LLM output and fold in crawler boolean flags as the
    floor — if the crawler saw a login form but the LLM forgot to set
    has_auth, the flag flips back on. Crawler structural evidence is
    trusted; the LLM is allowed to *add* signals, not subtract them."""
    crawler = {
        "has_auth": bool(getattr(site_analysis, "has_auth", False)),
        "has_payment": bool(getattr(site_analysis, "has_payment", False)),
        "has_search": bool(getattr(site_analysis, "has_search", False)),
        "has_forms": bool(getattr(site_analysis, "has_forms", False)),
    }
    return SiteProfile(
        url=evidence.get("base_url", ""),
        site_type=_coerce_site_type(parsed.get("site_type")),
        primary_flows=_coerce_flow_list(parsed.get("primary_flows")),
        tech_hints=_coerce_string_list(parsed.get("tech_hints")),
        has_auth=bool(parsed.get("has_auth")) or crawler["has_auth"],
        has_payment=bool(parsed.get("has_payment")) or crawler["has_payment"],
        has_search=bool(parsed.get("has_search")) or crawler["has_search"],
        has_forms=bool(parsed.get("has_forms")) or crawler["has_forms"],
        key_pages=_coerce_key_pages(parsed.get("key_pages")),
        description=str(parsed.get("description") or "")[:200],
        target_audience=str(parsed.get("target_audience") or "")[:140],
        source=source,
        raw_evidence=evidence,
    )


def _rule_based_profile(site_analysis, evidence: dict) -> SiteProfile:
    """Deterministic profile from crawler flags only — used when the
    LLM is unavailable. Same shape as the LLM-driven path so the
    Strategy Agent doesn't branch on source.

    Every field is read via :func:`getattr` because tests pass in
    :class:`SimpleNamespace` fakes that may omit one or more
    SiteAnalysis attributes — silently falling back to defaults is
    cheaper than a per-attribute guard at every call site.
    """
    if site_analysis is None:
        return SiteProfile(url="", source="rule_based",
                           raw_evidence=evidence or {})

    has_auth = bool(getattr(site_analysis, "has_auth", False))
    has_payment = bool(getattr(site_analysis, "has_payment", False))
    has_search = bool(getattr(site_analysis, "has_search", False))
    has_forms = bool(getattr(site_analysis, "has_forms", False))
    nav_items = getattr(site_analysis, "nav_items", None) or []
    page_count = int(getattr(site_analysis, "page_count", 0) or 0)
    pages = getattr(site_analysis, "pages", None) or []
    base_url = getattr(site_analysis, "base_url", "") or ""

    flows: list[str] = []
    if has_auth:
        flows.append("auth")
    if has_payment:
        flows.append("checkout")
        flows.append("payment")
    if has_search:
        flows.append("search")
    if has_forms:
        flows.append("lead_capture")
    if nav_items:
        flows.append("navigation")

    crawler_type = (getattr(site_analysis, "site_type", "") or "").lower()
    type_map = {
        "ecommerce": "ecommerce",
        "dashboard": "saas",
        "landing": "landing",
        "wordpress": "blog",
        "spa": "saas",
        "static": "generic",
    }
    site_type = type_map.get(crawler_type, "generic")
    if site_type == "generic":
        if has_payment:
            site_type = "ecommerce"
        elif has_auth and has_forms:
            site_type = "saas"
        elif page_count and page_count <= 2 and has_forms:
            site_type = "landing"
        elif has_forms:
            site_type = "form_heavy"

    key_pages: list[dict] = []
    for p in pages[:4]:
        if getattr(p, "error", ""):
            continue
        purl = getattr(p, "url", "") or ""
        role = "homepage" if purl == base_url else "other"
        title_low = (getattr(p, "title", "") or "").lower()
        if "login" in title_low or "sign in" in title_low:
            role = "login"
        elif "pricing" in title_low:
            role = "pricing"
        elif "contact" in title_low:
            role = "contact"
        elif "about" in title_low:
            role = "about"
        if purl:
            key_pages.append({"url": purl, "role": role})

    return SiteProfile(
        url=base_url,
        site_type=site_type,
        primary_flows=flows,
        tech_hints=list(getattr(site_analysis, "architecture_notes", []) or [])[:4],
        has_auth=has_auth,
        has_payment=has_payment,
        has_search=has_search,
        has_forms=has_forms,
        key_pages=key_pages,
        description="",
        target_audience="",
        source="rule_based",
        raw_evidence=evidence,
    )


__all__ = ["SiteProfile", "recon_site", "SITE_TYPES", "PRIMARY_FLOWS"]
