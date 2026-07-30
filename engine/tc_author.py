"""TestFortge — Test Case Author Agent.

Turns artifacts (comments/prompt, URL recon, crawler control inventory,
attachment excerpts) plus an optional :class:`engine.test_strategy.
TestStrategy` into fully-written test cases: summary, preconditions,
numbered steps, test data and expected result.

Why this module exists
----------------------
Before it, the two generation paths both stopped short of writing a real
case body:

* the Stage-2 site-aware path turned every ``CheckSpec.objective`` into
  the same three placeholder steps ("Navigate to the matching page /
  Perform the action described in the objective / Observe the rendered
  state") and the same generic expected result, so 40 cases shared one
  body;
* the legacy persona path emitted canned templates whose steps say
  "Navigate to the relevant page/feature" and "Perform the action".

Neither names a single real control, which is the property that makes a
manual case executable by a tester who did not write it.

House style
-----------
``engine/qa_knowledge/style/house_style.yaml`` and
``coverage_rules.yaml`` carry the writing standard and the coverage
model, both reverse-engineered from a 4,808-case QA-team deliverable
(the Odoo Test Plan). They are pasted verbatim into the cached system
block — editing the YAML changes generated output with no code change.

Fallback
--------
Same contract as Site Recon and Test Strategy: no API key, or retries
exhausted, and we fall back to a deterministic expansion that is 1:1
with the input checks. ``AuthoringResult.source`` reports which path
ran; product logic must not branch on it.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from functools import lru_cache
from typing import Any

from engine.llm_client import LLMUnavailable, call_messages
from engine.log import get_logger

_logger = get_logger(__name__)

_AUTHOR_MODEL = os.environ.get("ANTHROPIC_MODEL_SONNET", "claude-sonnet-4-6")

# Output budget. ~8k tokens is 30-45 authored cases, which matches what
# the coverage model asks for from a form with a dozen controls, and it
# completes inside _AUTHOR_TIMEOUT at Sonnet's output rate.
_AUTHOR_MAX_TOKENS = int(os.environ.get("TC_AUTHOR_MAX_TOKENS", "8000"))

# Per-attempt timeout, passed explicitly because llm_client defaults to
# 60 s. That default is right for Recon (1.5k output) and Strategy (4k),
# but an 8k-token authoring response needs ~2 minutes at Sonnet's output
# rate — on the default the call would time out, burn all three retries,
# and silently fall back to the deterministic expansion, so the agent
# would effectively never run in production.
#
# Safe to be this generous: authoring always happens on a JobQueue worker
# thread, not in the request. The sync POST blocks for at most
# SYNC_GEN_BUDGET_S (90 s) and then hands off to the background drain.
_AUTHOR_TIMEOUT = int(os.environ.get("TC_AUTHOR_TIMEOUT_S", "300"))


def _author_enabled() -> bool:
    """Kill switch for the authored stream.

    Default on — authoring is the point of the module. Set
    ``TC_AUTHOR_ENABLED=0`` to fall back to the deterministic expansion
    across the whole app without a code deploy, matching how the other
    feature flags on this service are operated.
    """
    return (os.environ.get("TC_AUTHOR_ENABLED", "1").strip().lower()
            not in ("0", "false", "no", "off"))

# Ceiling on how many cases we accept from one call. The coverage model
# is deliberately expansive (a create form with 12 controls owes 30-45
# cases), so this is set well above the LLM's practical output, purely
# as a runaway guard.
_MAX_CASES = int(os.environ.get("TC_AUTHOR_MAX_CASES", "300"))

_STYLE_DIR = os.path.join(os.path.dirname(__file__), "qa_knowledge", "style")

CATEGORIES = ("Positive", "Negative")
PRIORITIES = ("High", "Medium", "Low")

# Weak modals the house style bans from the expected-result column: they
# make the assertion unfalsifiable. Measured absence in the corpus — the
# 4,808 reference rows never use one as the assertion verb.
_WEAK_MODAL_RE = re.compile(
    r"\b(should(?:\s+be)?|must(?:\s+be)?|shall(?:\s+be)?|ought\s+to(?:\s+be)?|"
    r"is\s+expected\s+to(?:\s+be)?|are\s+expected\s+to(?:\s+be)?)\b",
    re.IGNORECASE,
)

# Step text that carries no information. Present verbatim in the old
# templates; rejected on the way in so it cannot come back via the LLM.
_GENERIC_STEP_RE = re.compile(
    r"^(?:"
    r"navigate to the (?:relevant|matching|target)\b|"
    r"perform the action(?:\s+described)?(?:\s+in the objective)?\.?$|"
    r"observe the (?:result|outcome)\.?$|"
    r"go to the relevant\b|"
    r"open the (?:relevant|appropriate) page\.?$|"
    r"do the (?:action|steps?)\b|"
    r"repeat (?:the )?(?:above|previous)\b|"
    r"test (?:the )?(?:feature|functionality)\.?$"
    r")",
    re.IGNORECASE,
)

# Feedback assertions a negative case must carry alongside the
# "action was refused" half.
_FEEDBACK_TOKENS = (
    "warning", "error", "message", "highlight", "alert", "notification",
    "dialog", "confirmation", "is not displayed", "are not displayed",
    "is not created", "is not saved", "is not added", "is not persisted",
    "is not changed", "is unchanged", "remains", "is still",
    "no results", "empty state", "is hidden", "is disabled",
    "404", "403", "400", "422", "409",
)


# ── Data model ───────────────────────────────────────────────────────

@dataclass
class Artifacts:
    """Everything the author agent is allowed to ground itself in.

    Anything absent simply narrows the grounding; the agent never
    invents a control that no artifact evidences (house style
    anti-pattern "Inventing UI that the artifacts do not evidence").
    """
    url: str = ""
    custom_prompt: str = ""
    requirements: list[str] = field(default_factory=list)
    # [{"name": "spec.pdf", "excerpt": "..."}]
    attachments: list[dict] = field(default_factory=list)
    # Crawler page records — the shape ``qa_persona.AnalysisResult.
    # site_pages`` already produces: url/title/h1/headings/nav_links/
    # buttons/forms.
    pages: list[dict] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any((self.url, self.custom_prompt, self.requirements,
                        self.attachments, self.pages))


@dataclass
class AuthoredCase:
    """One fully-written manual test case, house-style compliant."""
    summary: str
    preconditions: str = ""
    steps: list[str] = field(default_factory=list)
    test_data: str = ""
    expected_result: str = ""
    category: str = "Positive"          # Positive | Negative
    priority: str = "Medium"            # High | Medium | Low
    section: str = "Functional"
    testing_type: str = "Functional"
    url_pattern: str = ""
    comment: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AuthoringResult:
    cases: list[AuthoredCase] = field(default_factory=list)
    source: str = "deterministic"        # "llm" | "deterministic"
    rationale: str = ""
    # Surfaces the agent could not cover because the artifacts did not
    # evidence them. Surfaced to the operator so thin coverage reads as
    # a known gap rather than as a complete suite.
    gaps: list[str] = field(default_factory=list)
    # House-style findings that survived normalisation.
    lint_findings: list[str] = field(default_factory=list)

    def sections(self) -> list[str]:
        seen: list[str] = []
        for c in self.cases:
            if c.section not in seen:
                seen.append(c.section)
        return seen


# ── House-style assets ───────────────────────────────────────────────

@lru_cache(maxsize=2)
def _load_style_asset(filename: str) -> str:
    """Read a style YAML as raw text.

    Raw text, not parsed YAML: the file is prompt material, and the
    comments in it carry the measured evidence that keeps the model from
    treating a house convention as negotiable. Parsing would drop them.
    """
    path = os.path.join(_STYLE_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:  # pragma: no cover — asset ships with the repo
        _logger.warning("tc_author: cannot read style asset %s: %s",
                        filename, exc)
        return ""


def house_style_text() -> str:
    return _load_style_asset("house_style.yaml")


def coverage_rules_text() -> str:
    return _load_style_asset("coverage_rules.yaml")


# ── Control inventory ────────────────────────────────────────────────

def build_control_inventory(pages: list[dict], *, max_pages: int = 12) -> str:
    """Render crawler page records as a per-surface control inventory.

    This is the single most important part of the prompt: the corpus's
    quality comes from every step naming a real control with its exact
    label, and the model can only do that if it is handed the labels.
    """
    if not pages:
        return ""

    lines: list[str] = []
    for page in pages[:max_pages]:
        url = (page.get("url") or "").strip()
        title = (page.get("title") or "").strip()
        header = f"SURFACE {url}" if url else "SURFACE (unnamed)"
        lines.append(header)
        if title:
            lines.append(f"  title: {title}")
        h1 = (page.get("h1") or "").strip()
        if h1:
            lines.append(f"  h1: {h1}")

        headings = [h for h in (page.get("headings") or []) if h]
        if headings:
            lines.append("  headings: " + " | ".join(headings[:8]))

        # Nav and button labels are quoted here because the house style
        # requires each step to quote the control label verbatim — hand
        # the model the quotes so it copies them rather than paraphrasing.
        nav = [n for n in (page.get("nav_links") or []) if n]
        if nav:
            lines.append("  nav links: "
                         + " | ".join(f'"{n}"' for n in nav[:12]))

        buttons = [b for b in (page.get("buttons") or []) if b]
        if buttons:
            lines.append("  buttons / actions: "
                         + " | ".join(f'"{b}"' for b in buttons[:16]))

        for f_ix, form in enumerate(page.get("forms") or [], start=1):
            method = (form.get("method") or "GET").upper()
            action = (form.get("action") or "").strip()
            lines.append(f"  form #{f_ix} ({method}"
                         + (f" → {action}" if action else "") + "):")
            for fld in (form.get("fields") or form.get("inputs") or [])[:30]:
                lines.append("    - " + _describe_field(fld))
        lines.append("")

    if len(pages) > max_pages:
        lines.append(f"(+{len(pages) - max_pages} further surfaces crawled "
                     f"but not listed)")
    return "\n".join(lines).rstrip()


def _describe_field(fld: Any) -> str:
    """One inventory line for a single form control."""
    if not isinstance(fld, dict):
        return f'"{str(fld)[:60]}" (type=text)'
    label = (fld.get("label") or fld.get("name") or fld.get("placeholder")
             or fld.get("id") or "unnamed")
    ftype = (fld.get("type") or "text").lower()
    bits = [f'"{str(label)[:60]}" (type={ftype}']
    if fld.get("required"):
        bits.append(", required")
    for attr in ("maxlength", "minlength", "min", "max", "pattern"):
        val = fld.get(attr)
        if val not in (None, "", False):
            bits.append(f", {attr}={val}")
    options = fld.get("options") or fld.get("values")
    bits.append(")")
    out = "".join(bits)
    if isinstance(options, (list, tuple)) and options:
        opts = " / ".join(str(o)[:24] for o in list(options)[:8])
        out += f" options: {opts}"
    return out


# ── Prompt assembly ──────────────────────────────────────────────────

_SYSTEM_HEAD = (
    "You are a senior manual QA engineer at TestFort writing test cases "
    "for a client deliverable. Your output is a spreadsheet another "
    "tester will execute without ever speaking to you.\n\n"
    "You are given two standards documents, then the artifacts for one "
    "specific product. Follow the standards literally — they were "
    "reverse-engineered from a 4,808-case deliverable that this client "
    "already accepted, and the measured evidence is quoted inside them.\n\n"
    "Non-negotiable output rules:\n"
    "1. Every step names ONE real control with its exact label in double "
    "quotes, taken from the control inventory you were given. If the "
    "inventory does not evidence a control, do not write a step for it.\n"
    "2. Step 1 of every case is navigation, written as a breadcrumb.\n"
    "3. The expected result is declarative present tense. The words "
    "\"should\", \"must\" and \"shall\" are forbidden.\n"
    "4. Every Negative case asserts BOTH that the action was refused AND "
    "the user-visible feedback (highlighted field, warning text, nothing "
    "persisted).\n"
    "5. Enumerate controls, not requirement sentences. A form with N "
    "fields and M relational drop-downs owes roughly N + 4M cases.\n"
    "6. Group cases into sections named after the UI surface they "
    "exercise, in the walk order the coverage model prescribes.\n"
    "7. Record what you could NOT cover in \"gaps\" rather than "
    "inventing it.\n"
    "8. Respond with ONE valid JSON object. No prose, no markdown fence."
)

_SCHEMA_HINT = """{
  "rationale": "2-3 sentences: which surfaces you walked and why this coverage shape",
  "gaps": ["surface or control the artifacts did not evidence well enough to cover"],
  "cases": [
    {
      "section": "Job Positions grid",
      "summary": "Verify that User can filter Job Positions using the \\"Internal\\" filter",
      "preconditions": "Job Positions are created",
      "steps": ["Go to HR module -> Job Positions grid",
                "Click on the \\"Internal\\" filter button"],
      "test_data": "",
      "expected_result": "User can filter Job Positions using the \\"Internal\\" filter. Only internal positions remain in the grid.",
      "category": "Positive",
      "priority": "Medium",
      "testing_type": "Functional",
      "url_pattern": "/hr/job-positions*"
    }
  ]
}"""


def _system_blocks(grounding: str) -> list[dict]:
    """System message with prompt caching on every static part.

    The two style assets are ~10k tokens combined and identical on every
    call, so caching them is the difference between this agent being
    affordable and not.
    """
    blocks: list[dict] = [{
        "type": "text",
        "text": _SYSTEM_HEAD,
        "cache_control": {"type": "ephemeral"},
    }]
    style = house_style_text()
    if style:
        blocks.append({
            "type": "text",
            "text": "=== WRITING STANDARD (house_style.yaml) ===\n" + style,
            "cache_control": {"type": "ephemeral"},
        })
    coverage = coverage_rules_text()
    if coverage:
        blocks.append({
            "type": "text",
            "text": "=== COVERAGE MODEL (coverage_rules.yaml) ===\n" + coverage,
            "cache_control": {"type": "ephemeral"},
        })
    if grounding:
        blocks.append({
            "type": "text",
            "text": "=== ISTQB grounding (verbatim from syllabus) ===\n"
                    + grounding,
            "cache_control": {"type": "ephemeral"},
        })
    return blocks


def _build_grounding(profile, artifacts: Artifacts) -> str:
    """Retrieve up to 3 ISTQB chunks relevant to what we are testing."""
    try:
        from engine import istqb_rag
    except Exception:  # pragma: no cover — corpus optional
        return ""

    parts: list[str] = ["test case design technique manual testing"]
    if profile is not None:
        parts.append(getattr(profile, "site_type", "") or "")
        parts.extend(getattr(profile, "primary_flows", None) or [])
        if getattr(profile, "has_forms", False):
            parts.append("form validation boundary value analysis")
        if getattr(profile, "has_auth", False):
            parts.append("authentication authorisation")
        if getattr(profile, "has_payment", False):
            parts.append("payment transaction")
    if artifacts.custom_prompt:
        parts.append(artifacts.custom_prompt[:200])
    query = " ".join(p for p in parts if p)

    try:
        hits = istqb_rag.search(query, k=3)
    except Exception as exc:  # pragma: no cover — best-effort
        _logger.debug("tc_author: RAG search failed: %s", exc)
        return ""

    chunks: list[str] = []
    for h in hits or []:
        text = (h.get("text") or "").strip()
        if not text:
            continue
        ref = f"{h.get('source', 'ISTQB')} · page {h.get('page', '?')}"
        chunks.append(f"[{ref}]\n{text[:600]}")
    return "\n\n".join(chunks)


def _build_user_prompt(profile, strategy, artifacts: Artifacts) -> str:
    blocks: list[str] = ["=== ARTIFACTS FOR THIS PRODUCT ==="]

    if artifacts.url:
        blocks.append(f"Target URL: {artifacts.url}")

    if profile is not None:
        try:
            blocks.append("Site profile (from the recon agent):\n"
                          + json.dumps(profile.to_dict(), ensure_ascii=False,
                                       indent=2))
        except Exception:  # pragma: no cover — defensive
            pass

    inventory = build_control_inventory(artifacts.pages)
    if inventory:
        blocks.append(
            "Control inventory captured by the crawler. These are the "
            "ONLY labels you may quote in steps:\n" + inventory)
    else:
        blocks.append(
            "No control inventory was captured. Write cases against the "
            "controls named in the requirements and attachments below, "
            "and list every surface you could not evidence under "
            "\"gaps\".")

    if artifacts.requirements:
        reqs = "\n".join(f"- {r[:600]}" for r in artifacts.requirements[:80]
                         if (r or "").strip())
        if reqs:
            blocks.append("Requirements / notes supplied by the operator:\n"
                          + reqs)

    if artifacts.attachments:
        att_lines: list[str] = []
        for att in artifacts.attachments[:8]:
            name = (att.get("name") or "attachment").strip()
            excerpt = (att.get("excerpt") or "").strip()
            if not excerpt:
                continue
            att_lines.append(f"--- {name} ---\n{excerpt[:4000]}")
        if att_lines:
            blocks.append("Attachment excerpts:\n" + "\n\n".join(att_lines))

    if artifacts.custom_prompt:
        blocks.append(
            "Operator instruction — this narrows or steers the scope and "
            "outranks the default coverage breadth:\n"
            + artifacts.custom_prompt[:2000])

    if strategy is not None and getattr(strategy, "matrix", None):
        strat_lines: list[str] = []
        for cat, checks in strategy.matrix.items():
            for chk in checks:
                obj = getattr(chk, "objective", "")
                if not obj:
                    continue
                pri = getattr(chk, "priority", "Medium")
                pat = getattr(chk, "url_pattern", "") or ""
                tech = getattr(chk, "istqb_technique", "") or ""
                suffix = " ".join(x for x in (
                    f"[{pri}]",
                    f"url={pat}" if pat else "",
                    f"technique={tech}" if tech else "",
                ) if x)
                strat_lines.append(f"- ({cat}) {obj} {suffix}".rstrip())
        if strat_lines:
            blocks.append(
                "Test-strategy checks to expand. Each is an OBJECTIVE, not "
                "a finished case — expand each into as many concrete cases "
                "as the coverage model demands, and add the cases the "
                "coverage model requires that the strategy missed:\n"
                + "\n".join(strat_lines[:120]))

    blocks.append("=== RETURN JSON MATCHING THIS SHAPE EXACTLY ===\n"
                  + _SCHEMA_HINT)
    blocks.append(
        "Order cases by section, sections in the coverage model's walk "
        "order. Use the section-level precondition convention: put a fact "
        "in every case only when it is case-specific. Do not number the "
        "steps — the exporter numbers them.")
    return "\n\n".join(blocks)


# ── LLM call ─────────────────────────────────────────────────────────

def _call_llm(profile, strategy, artifacts: Artifacts, grounding: str) -> str:
    resp = call_messages(
        timeout=_AUTHOR_TIMEOUT,
        model=_AUTHOR_MODEL,
        max_tokens=_AUTHOR_MAX_TOKENS,
        system=_system_blocks(grounding),
        messages=[{"role": "user",
                   "content": _build_user_prompt(profile, strategy,
                                                 artifacts)}],
    )
    usage = getattr(resp, "usage", None)
    if usage is not None:
        _logger.info(
            "tc_author usage: input=%s output=%s cache_read=%s",
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
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ── Coercion ─────────────────────────────────────────────────────────

def _coerce_category(value: Any) -> str:
    s = str(value or "").strip().title()
    if s in CATEGORIES:
        return s
    # The old vocabulary carried "Edge Case" / "Security" as sibling
    # categories. The corpus flags Positive/Negative only, so fold
    # anything else onto the polarity it asserts.
    if s in ("Edge Case", "Edge", "Boundary", "Security"):
        return "Negative"
    return "Positive"


def _coerce_priority(value: Any) -> str:
    s = str(value or "").strip().title()
    return s if s in PRIORITIES else "Medium"


def _coerce_case(raw: Any) -> AuthoredCase | None:
    if not isinstance(raw, dict):
        return None
    summary = str(raw.get("summary") or "").strip()
    if not summary:
        return None

    steps_raw = raw.get("steps") or []
    if isinstance(steps_raw, str):
        steps_raw = [s for s in steps_raw.splitlines()]
    steps: list[str] = []
    for s in steps_raw:
        s = _strip_step_number(str(s or "").strip())
        if s:
            steps.append(s[:400])

    return AuthoredCase(
        summary=summary[:300],
        preconditions=str(raw.get("preconditions") or "").strip()[:500],
        steps=steps[:20],
        test_data=str(raw.get("test_data") or "").strip()[:500],
        expected_result=str(raw.get("expected_result") or "").strip()[:900],
        category=_coerce_category(raw.get("category")),
        priority=_coerce_priority(raw.get("priority")),
        section=str(raw.get("section") or "Functional").strip()[:120]
                or "Functional",
        testing_type=str(raw.get("testing_type") or "Functional").strip()[:40]
                     or "Functional",
        url_pattern=str(raw.get("url_pattern") or "").strip()[:200],
        comment=str(raw.get("comment") or "").strip()[:300],
    )


_STEP_NUM_RE = re.compile(r"^\s*\d+\s*[.)\]:-]\s*")


def _strip_step_number(text: str) -> str:
    """Drop a leading "3. " so the exporter owns the numbering.

    Without this a re-export double-numbers the column ("1. 1. Go to …"),
    which is what happens today when recorded steps flow back in.
    """
    return _STEP_NUM_RE.sub("", text).strip()


# ── House-style lint + normalisation ─────────────────────────────────

# The alternate title grammar the corpus uses for the systematic
# error-message sweep: "<Surface>: <attempted action>".
_ERROR_MSG_TITLE_RE = re.compile(
    r"^[A-Z][^:]{3,80}:\s+(?:Trying to|Go to|Attempt|Proceed|Leave|Remove|"
    r"Submit|Enter|Click)\b")


def _is_error_message_title(summary: str) -> bool:
    return bool(_ERROR_MSG_TITLE_RE.match(summary or ""))


def _ensure_verify_that(summary: str) -> str:
    """Canonicalise a title onto the house opener."""
    s = (summary or "").strip()
    if not s:
        return s
    if s.startswith("Verify that "):
        return s
    low = s.lower()
    if low.startswith("verify that"):
        return "Verify that " + s[len("verify that"):].lstrip()
    for opener in ("verify the ability to ", "verify ability to "):
        if low.startswith(opener):
            return "Verify that User can " + s[len(opener):].lstrip()
    if low.startswith("verify "):
        rest = s[len("verify "):].lstrip()
        return "Verify that " + (rest[:1].lower() + rest[1:] if rest else "")
    for opener in ("check that ", "ensure that ", "confirm that ",
                   "validate that ", "test that "):
        if low.startswith(opener):
            return "Verify that " + s[len(opener):].lstrip()
    for opener in ("check ", "ensure ", "confirm ", "validate ", "test "):
        if low.startswith(opener):
            rest = s[len(opener):].lstrip()
            return "Verify that " + (rest[:1].lower() + rest[1:] if rest
                                     else "")
    return "Verify that " + (s[:1].lower() + s[1:])


# Weak-modal → declarative rewrites, longest pattern first so
# "should not be" is handled before "should be".
_MODAL_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bshould\s+not\s+be\b", re.I),   "is not"),
    (re.compile(r"\bmust\s+not\s+be\b", re.I),     "is not"),
    (re.compile(r"\bshall\s+not\s+be\b", re.I),    "is not"),
    (re.compile(r"\bshould\s+be\b", re.I),         "is"),
    (re.compile(r"\bmust\s+be\b", re.I),           "is"),
    (re.compile(r"\bshall\s+be\b", re.I),          "is"),
    (re.compile(r"\bought\s+to\s+be\b", re.I),     "is"),
    (re.compile(r"\b(?:is|are)\s+expected\s+to\s+be\b", re.I), "is"),
    (re.compile(r"\b(?:is|are)\s+expected\s+to\b", re.I),      "is"),
)

# Irregular third-person-singular forms. Everything else is conjugated
# by :func:`_third_person_singular`.
_IRREGULAR_3SG: dict[str, str] = {
    "be": "is", "have": "has", "do": "does", "go": "goes",
}

_BARE_MODAL_RE = re.compile(
    r"\b(?:should|must|shall)\s+(?:not\s+)?([a-z][a-z-]+)\b", re.I)


def _third_person_singular(verb: str) -> str:
    """Conjugate a bare English verb for a singular subject.

    Handles the regular orthography rules; hyphenated compounds inflect
    on the final element ("round-trip" → "round-trips").
    """
    low = verb.lower()
    if low in _IRREGULAR_3SG:
        return _IRREGULAR_3SG[low]
    if "-" in low:
        head, _, tail = low.rpartition("-")
        return f"{head}-{_third_person_singular(tail)}"
    if low.endswith(("s", "sh", "ch", "x", "z", "o")):
        return low + "es"
    if len(low) > 1 and low.endswith("y") and low[-2] not in "aeiou":
        return low[:-1] + "ies"
    return low + "s"


def _plural_subject(prefix: str) -> bool:
    """Cheap number agreement for the subject preceding the modal.

    Only used to pick "is" vs "are"; a wrong guess is a grammar wart in
    a sentence that is still unambiguous, whereas leaving the modal in
    place leaves the assertion unfalsifiable.
    """
    tail = prefix.rstrip().rstrip(",")
    if not tail:
        return False
    low = tail.lower()
    if " and " in low.split(".")[-1]:
        return True
    if low.endswith(("they", "these", "those", "both", "all", "fields",
                     "errors", "messages", "values", "records", "rows",
                     "items", "results", "steps", "buttons", "links",
                     "options", "columns", "tabs", "controls")):
        return True
    last = re.split(r"[\s\"'()]+", low)[-1] if low else ""
    if last.endswith("s") and not last.endswith(("ss", "us", "is", "'s",
                                                  "status", "address")):
        return True
    return False


def normalise_expected_result(text: str) -> str:
    """Rewrite an expected result into the house declarative voice.

    ``"The record should be created. Errors should not be shown."`` →
    ``"The record is created. Errors are not shown."``
    """
    if not text:
        return text
    out = text

    # Number-agreement pass on the "<subject> should be" forms.
    def _sub_be(m: re.Match) -> str:
        start = m.start()
        neg = "not" in m.group(0).lower()
        plural = _plural_subject(out[:start])
        verb = "are" if plural else "is"
        return f"{verb} not" if neg else verb

    for pattern, default in _MODAL_REWRITES:
        if default in ("is", "is not"):
            out = pattern.sub(_sub_be, out)
        else:  # pragma: no cover — every current rewrite is a be-form
            out = pattern.sub(default, out)

    # Remaining bare "should <verb>" forms. A plural subject keeps the
    # base form ("Errors occur"), a singular one takes the -s ending
    # ("The message states"), and a negated form takes do/does support.
    def _sub_bare(m: re.Match) -> str:
        verb = m.group(1).lower()
        neg = " not " in m.group(0).lower()
        plural = _plural_subject(out[:m.start()])
        if neg:
            return ("do not " if plural else "does not ") + verb
        return verb if plural else _third_person_singular(verb)

    out = _BARE_MODAL_RE.sub(_sub_bare, out)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


def has_weak_modal(text: str) -> bool:
    return bool(_WEAK_MODAL_RE.search(text or ""))


def is_generic_step(text: str) -> bool:
    return bool(_GENERIC_STEP_RE.match((text or "").strip()))


def asserts_feedback(text: str) -> bool:
    low = (text or "").lower()
    return any(tok in low for tok in _FEEDBACK_TOKENS)


def append_feedback_assertion(text: str) -> str:
    """Add the "and this is what the user sees" half to a refusal.

    A negative case that only says the action failed drops the assertion
    most likely to catch a real defect: on the reference corpus's
    dedicated error-message sheet, 8 of 25 rows failed on missing or
    misdirected feedback, not on the refusal itself.
    """
    return ((text or "").rstrip(". ")
            + ". The action is refused, and an error message names the "
              "reason. Nothing is persisted.")


def lint_case(case: AuthoredCase) -> list[str]:
    """House-style findings for one case. Empty list == compliant."""
    out: list[str] = []
    summary = case.summary or ""

    if not summary.startswith("Verify that") \
            and not _is_error_message_title(summary):
        out.append("summary does not open with 'Verify that'")
    if len(summary) > 220:
        out.append("summary exceeds 220 characters")
    if has_weak_modal(summary):
        out.append("summary uses a weak modal (should/must/shall)")

    if not case.steps:
        out.append("no steps")
    elif len(case.steps) < 2:
        out.append("fewer than 2 steps")
    for step in case.steps:
        if is_generic_step(step):
            out.append(f"generic placeholder step: {step[:60]!r}")

    if not (case.expected_result or "").strip():
        out.append("expected result is empty")
    elif has_weak_modal(case.expected_result):
        out.append("expected result uses a weak modal (should/must/shall)")

    if case.category == "Negative" and case.expected_result \
            and not asserts_feedback(case.expected_result):
        out.append("negative case does not assert the user-visible feedback")

    if case.category not in CATEGORIES:
        out.append(f"unknown category {case.category!r}")
    if case.priority not in PRIORITIES:
        out.append(f"unknown priority {case.priority!r}")

    return out


def normalise_case(case: AuthoredCase) -> tuple[AuthoredCase, list[str]]:
    """Auto-fix what can be fixed; return the case and residual findings.

    Fixes applied: title opener, weak modals in the summary and expected
    result, leading step numbers, dropped generic steps, and the missing
    feedback half of a negative expected result.
    """
    fixed: list[str] = []

    if case.summary and not case.summary.startswith("Verify that") \
            and not _is_error_message_title(case.summary):
        case.summary = _ensure_verify_that(case.summary)
        fixed.append("summary opener")

    if has_weak_modal(case.summary):
        case.summary = normalise_expected_result(case.summary)
        fixed.append("summary modal")

    case.steps = [_strip_step_number(s) for s in case.steps]
    kept = [s for s in case.steps if s and not is_generic_step(s)]
    if len(kept) != len([s for s in case.steps if s]):
        fixed.append("generic steps dropped")
    case.steps = kept

    if has_weak_modal(case.expected_result):
        case.expected_result = normalise_expected_result(case.expected_result)
        fixed.append("expected-result voice")

    if case.category == "Negative" and case.expected_result \
            and not asserts_feedback(case.expected_result):
        case.expected_result = append_feedback_assertion(
            case.expected_result)
        fixed.append("negative feedback assertion")

    case.category = _coerce_category(case.category)
    case.priority = _coerce_priority(case.priority)

    residual = lint_case(case)
    if fixed:
        _logger.debug("tc_author: normalised case %r (%s)",
                      case.summary[:60], ", ".join(fixed))
    return case, residual


# ── Deterministic expansion (fallback path) ──────────────────────────

_VERIFY_PREFIX_RE = re.compile(
    r"^\s*verify\s+(?:that\s+)?", re.IGNORECASE)

# Objective phrasings that mean "this must be refused".
#
# Deliberately narrow. An earlier, looser version matched bare "without",
# "error" and HTTP status codes, which classified "Verify that all
# internal navigation links resolve without 404/5xx" as a refusal case
# and gave it a nonsensical body ("confirm nothing was persisted"). Only
# phrasings that assert a refusal or an absence belong here.
_NEGATIVE_OBJECTIVE_RE = re.compile(
    r"(?:"
    r"\b(?:cannot|can\s?not|can'?t|can`t|unable\s+to)\b"
    r"|\b(?:is|are|does|do)\s+not\b"
    r"|\bnot\s+(?:displayed|shown|visible|available|allowed|permitted|"
    r"created|saved|persisted|accessible|reachable|editable)\b"
    r"|\bwithout\s+(?:the\s+)?(?:required|valid|permission|permissions|"
    r"rights|access|filling|specifying|selecting|entering|logging)\b"
    r"|\b(?:invalid|malformed|reject\w*|refus\w*|deni\w*|forbidden|"
    r"blocked|unauthoris\w*|unauthoriz\w*|duplicate)\b"
    r"|\b(?:left\s+empty|is\s+empty|no\s+matches|no\s+results|"
    r"empty[-\s]state)\b"
    # "fails with an error" is a refusal; a bare "fail" is not, so the
    # preposition is required ("completes without a timeout" must stay
    # Positive).
    r"|\bfails?\s+(?:with|for)\b"
    r"|\bwrong\s+(?:credentials|password|value)\b"
    r")",
    re.IGNORECASE,
)


def objective_core(objective: str) -> str:
    """Strip the house opener so the phrase can be reused in a step."""
    core = _VERIFY_PREFIX_RE.sub("", objective or "").strip()
    return core.rstrip(".").strip()


def infer_category(objective: str) -> str:
    return ("Negative" if _NEGATIVE_OBJECTIVE_RE.search(objective or "")
            else "Positive")


def _readable_pattern(url_pattern: str) -> str:
    """'/checkout/*' → 'the /checkout/ area'."""
    pat = (url_pattern or "").strip()
    if not pat or pat in ("*", "/*"):
        return ""
    cleaned = pat.replace("*", "").strip()
    if not cleaned or cleaned == "/":
        return ""
    return cleaned


def navigation_step(url_pattern: str = "", *, profile=None,
                    site_url: str = "") -> str:
    """Build step 1 as a breadcrumb, per the house style."""
    base = site_url or (getattr(profile, "url", "") if profile else "")
    area = _readable_pattern(url_pattern)
    if base and area:
        return f"Go to {base.rstrip('/')} -> {area}"
    if area:
        return f"Go to {area}"
    if base:
        return f"Open {base} in the browser"
    return "Open the application under test in the browser"


def _test_data_hint(objective: str) -> str:
    """Concrete values for the data-dependent objectives we can detect."""
    low = (objective or "").lower()
    hints: list[str] = []
    if "email" in low:
        hints.append("Valid: tester@example.com; Invalid: tester@, @example.com")
    if any(k in low for k in ("password", "credential", "login", "sign in")):
        hints.append("Valid password: Str0ngP@ss!2026; Invalid: wrong-pass-123")
    if any(k in low for k in ("boundary", "max length", "min length",
                              "length", "bva")):
        hints.append("Min: 1 char; Max: the documented limit; Max+1: one over")
    if any(k in low for k in ("numeric", "number", "amount", "quantity",
                              "duration")):
        hints.append("In range: 42; Out of range: -1 and 999999999")
    if "date" in low or "deadline" in low:
        hints.append("Past: yesterday; Future: tomorrow; Invalid: 31/02/2026")
    if any(k in low for k in ("unicode", "cyrillic", "non-latin",
                             "localis", "localiz")):
        hints.append("Cyrillic: Тестове Ім'я; CJK: 测试; Emoji: 🙂")
    if "card" in low or "payment" in low:
        hints.append("Approved test card: 4242 4242 4242 4242; "
                     "Declined: 4000 0000 0000 0002")
    if any(k in low for k in ("upload", "file", "attachment")):
        hints.append("Accepted: sample.pdf (1 MB); Rejected: sample.exe; "
                     "Oversized: 50 MB file")
    if "search" in low:
        hints.append("Matching term: a value known to exist; "
                     "Non-matching: xyznonexistent123")
    return " | ".join(hints)


def expand_check(check, *, profile=None, category: str = "",
                 section: str = "Functional",
                 testing_type: str = "Functional") -> AuthoredCase:
    """Deterministic expansion of one :class:`CheckSpec` into a case.

    This is the no-API-key path. It cannot name controls it was never
    given, so it stays honest: the action step quotes the objective's own
    phrasing instead of the placeholder text the old code emitted, the
    navigation step is built from the check's ``url_pattern``, and the
    assertion step names what to look at.
    """
    objective = str(getattr(check, "objective", "") or "").strip()
    url_pattern = str(getattr(check, "url_pattern", "") or "").strip()
    rationale = str(getattr(check, "rationale", "") or "").strip()
    technique = str(getattr(check, "istqb_technique", "") or "").strip()
    priority = _coerce_priority(getattr(check, "priority", "Medium"))
    cat = _coerce_category(category or infer_category(objective))

    core = objective_core(objective)
    steps = [navigation_step(url_pattern, profile=profile)]

    # The objective is phrased as an assertion, so the action step
    # introduces it rather than pretending it is already imperative —
    # "Attempt: login fails with an error" would not parse as an
    # instruction.
    if cat == "Negative":
        steps.append(f"Provoke the condition the objective describes: {core}")
        steps.append("Pay attention to the field highlighting and to the "
                     "message the application displays")
        steps.append("Reopen the surface and confirm the rejected input was "
                     "not persisted")
        expected = (
            f"{core[:1].upper() + core[1:] if core else 'The action is refused'}. "
            f"The application refuses the action, highlights the offending "
            f"input, and displays a message that names the reason. Nothing "
            f"is persisted.")
    else:
        steps.append(f"Exercise the behaviour the objective describes: {core}")
        steps.append("Pay attention to the resulting page state, and check "
                     "the browser console and network log for errors")
        expected = (
            f"{core[:1].upper() + core[1:] if core else 'The behaviour matches the objective'}. "
            f"No console errors and no failed network requests are logged.")

    preconditions = rationale or (
        "Application is reachable. User is logged in when the surface "
        "requires authentication.")
    if technique:
        # Terminate the rationale before appending, otherwise two
        # sentences run together ("Baseline reachability check Design
        # technique: Use Case.").
        if preconditions and preconditions[-1] not in ".!?":
            preconditions += "."
        preconditions = f"{preconditions} Design technique: {technique}."

    case = AuthoredCase(
        summary=_ensure_verify_that(objective),
        preconditions=preconditions,
        steps=steps,
        test_data=_test_data_hint(objective),
        expected_result=expected,
        category=cat,
        priority=priority,
        section=section,
        testing_type=testing_type,
        url_pattern=url_pattern,
    )
    case, _ = normalise_case(case)
    return case


# ── Public API ───────────────────────────────────────────────────────

def author_test_cases(*, profile=None, strategy=None,
                      artifacts: Artifacts | None = None,
                      force_llm: bool = False) -> AuthoringResult:
    """Author a full test-case pack from artifacts (+ optional strategy).

    Parameters
    ----------
    profile:
        :class:`engine.site_recon.SiteProfile` or ``None``.
    strategy:
        :class:`engine.test_strategy.TestStrategy` or ``None``. When
        given, its checks are the coverage spine the author expands.
    artifacts:
        :class:`Artifacts` — prompt, requirements, attachment excerpts
        and the crawler control inventory.
    force_llm:
        Call the LLM even without ``ANTHROPIC_API_KEY`` — for tests that
        monkeypatch :func:`engine.llm_client.call_messages`.

    Returns
    -------
    AuthoringResult
        Always populated when there is anything to work from. Never
        raises: an LLM failure degrades to the deterministic expansion.
    """
    arts = artifacts or Artifacts()

    if not _author_enabled():
        _logger.info("tc_author: disabled via TC_AUTHOR_ENABLED")
        return _deterministic_result(profile, strategy, arts)

    api_key_set = bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())
    if not api_key_set and not force_llm:
        return _deterministic_result(profile, strategy, arts)

    grounding = _build_grounding(profile, arts)
    try:
        raw = _call_llm(profile, strategy, arts, grounding)
    except LLMUnavailable as exc:
        _logger.warning("tc_author: LLM unavailable, fallback: %s", exc)
        return _deterministic_result(profile, strategy, arts)
    except Exception as exc:  # pragma: no cover — defensive
        _logger.warning("tc_author: LLM call failed: %s", exc)
        return _deterministic_result(profile, strategy, arts)

    parsed = _parse_llm_response(raw)
    if parsed is None:
        _logger.warning("tc_author: unparseable LLM response, fallback")
        return _deterministic_result(profile, strategy, arts)

    raw_cases = parsed.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        _logger.warning("tc_author: LLM returned no cases, fallback")
        return _deterministic_result(profile, strategy, arts)

    cases: list[AuthoredCase] = []
    findings: list[str] = []
    for raw_case in raw_cases[:_MAX_CASES]:
        case = _coerce_case(raw_case)
        if case is None:
            continue
        case, residual = normalise_case(case)
        # A case with no usable steps is not executable — drop it rather
        # than ship a row a tester cannot run.
        if len(case.steps) < 2 or not case.expected_result:
            findings.append(f"dropped unexecutable case: {case.summary[:70]!r}")
            continue
        cases.append(case)
        findings.extend(f"{case.summary[:50]!r}: {r}" for r in residual)

    if not cases:
        _logger.warning("tc_author: every LLM case failed lint, fallback")
        return _deterministic_result(profile, strategy, arts)

    gaps = [str(g)[:300] for g in (parsed.get("gaps") or [])
            if isinstance(g, (str, int, float))]

    return AuthoringResult(
        cases=cases,
        source="llm",
        rationale=str(parsed.get("rationale") or "")[:800],
        gaps=gaps[:20],
        lint_findings=findings[:60],
    )


def _deterministic_result(profile, strategy,
                         artifacts: "Artifacts | None" = None
                         ) -> AuthoringResult:
    """No-LLM path: rule engine first, 1:1 strategy expansion second.

    When the crawler captured a control inventory, :mod:`engine.tc_rules`
    enumerates it against the coverage model and produces control-level
    coverage — what the author agent is asked for, minus the judgement, at
    zero cost. This is the primary path on the free plan, where a paid LLM
    is not an option.

    Falling back to the 1:1 strategy expansion keeps the older contract
    for callers with no inventory to enumerate (prompt-only input, or a
    site whose crawl failed): same pack shape, stable IDs, better bodies.
    """
    from engine.testcase_generator import _testing_type_label

    if artifacts is not None and getattr(artifacts, "pages", None):
        try:
            from engine.tc_rules import enumerate_from_artifacts
            rule_cases = enumerate_from_artifacts(artifacts)
        except Exception as exc:  # pragma: no cover — defensive
            _logger.warning("tc_author: rule engine failed: %s", exc)
            rule_cases = []
        if rule_cases:
            _logger.info("tc_author: rule engine produced %d cases from the "
                         "control inventory", len(rule_cases))
            return AuthoringResult(
                cases=rule_cases,
                source="rules",
                rationale=("Enumerated from the crawled control inventory "
                           "against coverage_rules.yaml — no model "
                           "involved, so every case is grounded in an "
                           "attribute the page actually declares."),
            )

    cases: list[AuthoredCase] = []
    if strategy is None or not getattr(strategy, "matrix", None):
        return AuthoringResult(cases=[], source="deterministic")

    for cat, checks in strategy.matrix.items():
        for chk in checks or []:
            cases.append(expand_check(
                chk, profile=profile, section=cat,
                testing_type=_testing_type_label(cat),
            ))
    return AuthoringResult(
        cases=cases,
        source="deterministic",
        rationale=("Deterministic expansion — one case per strategy check. "
                   "No control inventory was available to enumerate."),
    )


__all__ = [
    "Artifacts", "AuthoredCase", "AuthoringResult",
    "author_test_cases", "expand_check",
    "build_control_inventory",
    "house_style_text", "coverage_rules_text",
    "lint_case", "normalise_case", "normalise_expected_result",
    "has_weak_modal", "is_generic_step", "asserts_feedback",
    "append_feedback_assertion",
    "objective_core", "infer_category", "navigation_step",
    "CATEGORIES", "PRIORITIES",
]
