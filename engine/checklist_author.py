"""TestFortge — the Low-Level Checklist Author agent.

The deterministic generator in :mod:`engine.checklist_rules` already
reproduces the reference shape from real markup, which is why it shipped
first and why this is not on the critical path. What it cannot do is
judge: it enumerates every heading the crawler found, in the order it
found them, and it cannot read a requirements document or an attached
spec at all.

This agent buys three things the enumeration cannot:

* **Section naming and order.** A crawler sees ``<h2>`` text; it does not
  know that two of those headings are one surface, or that a section
  belongs before another in the order a tester walks the page.
* **Which interactions are worth a row.** The enumerator gives every
  accordion the same three sub-checks. A reader of the page can tell that
  one accordion holds pricing and another holds boilerplate.
* **Artefacts with no markup.** Requirements, an attached spec, an
  operator instruction — none of which the crawler can see.

Everything else is deliberately NOT the agent's job. Numbering is applied
by :func:`engine.checklist_rules.assign_numbers`, the terminology rules are
enforced in code afterwards, and a check naming a control the artefacts do
not evidence is dropped. The model writes prose and picks structure; it
does not get to decide what counts as evidence or how a row is worded.

Fallback is total: no API key, a refused call, unparseable JSON, or an
empty result all land on the deterministic walk, and
``AuthoredChecklist.source`` says which ran. Product logic must not branch
on it — it is for the operator, not for control flow.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from engine import checklist_rules as _rules
from engine import glossary
from engine.llm_client import LLMUnavailable, call_messages
from engine.log import get_logger

_logger = get_logger(__name__)

# Routed by work kind at call time — see engine/llm_models.py.
_AUTHOR_KIND = "authoring"

# A 60-row checklist is ~4k tokens of JSON. 6k leaves room for the
# rationale and gaps without letting a runaway response cost real money.
_AUTHOR_MAX_TOKENS = int(os.environ.get("CL_AUTHOR_MAX_TOKENS", "6000"))

# Same reasoning as tc_author: llm_client defaults to 60 s, which is not
# enough for a multi-thousand-token response, and authoring always happens
# on a JobQueue worker rather than in the request.
_AUTHOR_TIMEOUT = int(os.environ.get("CL_AUTHOR_TIMEOUT_S", "240"))

#: Runaway guard. The reference deliverable is 57 rows for one page; a
#: multi-page pack can legitimately reach a few hundred.
_MAX_CHECKS = int(os.environ.get("CL_AUTHOR_MAX_CHECKS", "400"))

_STYLE_DIR = os.path.join(os.path.dirname(__file__), "qa_knowledge", "style")


def _author_enabled() -> bool:
    """Kill switch, matching the tc_author convention.

    Default on. ``CL_AUTHOR_ENABLED=0`` falls the whole app back to the
    deterministic walk without a deploy.
    """
    return (os.environ.get("CL_AUTHOR_ENABLED", "1").strip().lower()
            not in ("0", "false", "no", "off"))


# ── Data model ───────────────────────────────────────────────────────

@dataclass
class Artifacts:
    """Everything the agent may ground itself in.

    Anything absent narrows the grounding. The agent never invents a
    control no artefact evidences — that is the anti-pattern
    ``checklist_style.yaml`` names as "inventing UI the artifacts do not
    evidence", and it is enforced after the call as well as asked for in
    the prompt.
    """
    url: str = ""
    custom_prompt: str = ""
    requirements: list[str] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)
    pages: list[dict] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any((self.url, self.custom_prompt, self.requirements,
                        self.attachments, self.pages))


@dataclass
class AuthoredChecklist:
    """The agent's output, in the shape the rest of the module expects."""
    checklist: _rules.LowLevelChecklist = field(
        default_factory=_rules.LowLevelChecklist)
    source: str = "deterministic"        # "llm" | "deterministic"
    rationale: str = ""
    #: Surfaces the agent could not cover. Surfaced to the operator so a
    #: thin sheet reads as a known gap rather than as full coverage.
    gaps: list[str] = field(default_factory=list)
    #: Wording findings that survived normalisation.
    lint_findings: list[str] = field(default_factory=list)
    #: Checks dropped because no artefact evidenced their control.
    dropped: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.checklist.total


# ── Prompt ───────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def checklist_style_text() -> str:
    """Raw ``checklist_style.yaml`` — prompt material, not parsed.

    Raw because the ``evidence:`` and ``reviewer:`` comments are the part
    that stops the model treating a house convention as negotiable, and
    parsing would drop them.
    """
    try:
        with open(os.path.join(_STYLE_DIR, "checklist_style.yaml"),
                  encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        _logger.warning("checklist_author: cannot read the style asset: %s",
                        exc)
        return ""


_SYSTEM_HEAD = """You are the Low-Level Checklist Author for a QA \
consultancy. You write the sheet a tester walks with, one observable \
check per row.

You are NOT writing test cases. A checklist row carries no steps, no \
preconditions, no test data and no separate expected result — the \
observation IS the row. A row that needs three steps to reach belongs in \
the Test Cases module, not here.

Hard rules, in priority order:

1. Only name controls the ARTEFACTS below evidence. If the control \
inventory does not name a control, you may not write a row about it. \
Report the surface under "gaps" instead. A row that fails because the \
control never existed is worse than a missing row.
2. Follow the writing standard exactly. It is measured from this team's \
own reviewed deliverable and the reviewer quotes in it are not \
negotiable.
3. Sections are SURFACES a tester walks — Header, Page Content, Footer — \
never test types. No "Positive checks" section.
4. Do NOT number anything. Numbering is applied afterwards. Return the \
rows in the order a tester walks them and mark each row's depth.
5. Prefer fewer, sharper rows over padding. Every row a reviewer would \
send back costs more than the row was worth."""

_SCHEMA_HINT = """{
  "rationale": "one or two sentences on how you walked the surface",
  "surface": "\\"Mobile Application Testing Services\\" page",
  "gaps": ["Pricing page was not crawled — no rows written for it"],
  "sections": [
    {
      "name": "Header",
      "checks": [
        {
          "objective": "Verify that the Homepage is opened after clicking the logo",
          "depth": 2,
          "category": "Positive",
          "priority": "High",
          "testing_type": "Functional"
        },
        {
          "objective": "Verify that all sub-items are visible and clickable from the \\"Services\\" drop-down menu",
          "depth": 2,
          "category": "Positive",
          "priority": "High",
          "testing_type": "Functional"
        }
      ]
    }
  ]
}"""


def _system_blocks() -> list[dict]:
    """System message, cached on every static part.

    Three breakpoints of the four the API allows: head, the checklist
    writing standard, and the terminology pair. The user prompt carries
    the artefacts and varies per call, so it gets none.
    """
    blocks: list[dict] = [{
        "type": "text",
        "text": _SYSTEM_HEAD,
        "cache_control": {"type": "ephemeral"},
    }]
    style = checklist_style_text()
    if style:
        blocks.append({
            "type": "text",
            "text": "=== WRITING STANDARD (checklist_style.yaml) ===\n"
                    + style,
            "cache_control": {"type": "ephemeral"},
        })
    terminology = _terminology_block()
    if terminology:
        blocks.append({
            "type": "text",
            "text": terminology,
            "cache_control": {"type": "ephemeral"},
        })
    return blocks


def _terminology_block() -> str:
    """The reviewer's wording rules plus the glossary, as one block."""
    parts: list[str] = []
    wording = glossary.wording_rules_text()
    if wording:
        parts.append(
            "=== WORDING AND TERMINOLOGY DISCIPLINE (wording_rules.yaml) "
            "===\nEvery rule here quotes the reviewing team lead's own "
            "comment on a real deliverable. Treat the `reviewer:` lines as "
            "non-negotiable.\n" + wording)
    gloss = glossary.glossary_text()
    if gloss:
        parts.append(
            "=== UI TERMINOLOGY GLOSSARY (ui_terms.en.yaml) ===\nName every "
            "element with its `term`. The `avoid` list is what a reviewer "
            "sends back.\n" + gloss)
    return "\n\n".join(parts)


def _build_user_prompt(artifacts: Artifacts, profile: Any = None) -> str:
    from engine.tc_author import build_control_inventory

    blocks: list[str] = ["=== ARTEFACTS FOR THIS SURFACE ==="]
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
            "Control inventory captured by the crawler. These are the ONLY "
            "controls you may write rows about, and the ONLY labels you may "
            "quote:\n" + inventory)
    else:
        blocks.append(
            "No control inventory was captured. Write rows only for what "
            "the requirements and attachments below evidence, and list "
            "every surface you could not evidence under \"gaps\".")

    # What the deterministic walk already produces. Handing it over turns
    # the task from "invent a checklist" into "improve this one", which is
    # both a smaller ask and a floor on the result.
    if artifacts.pages:
        try:
            baseline = _rules.build_checklist(artifacts.pages,
                                              url=artifacts.url)
            if baseline.total:
                blocks.append(
                    "A deterministic enumeration of the same surface is "
                    "below. Treat it as a FLOOR, not a draft to trim: keep "
                    "every row that is still right, merge the ones that say "
                    "the same thing, name sections better, and add what "
                    "walking the page tells you that enumerating it "
                    "cannot.\n" + _baseline_text(baseline))
        except Exception as exc:  # pragma: no cover — best-effort
            _logger.debug("checklist_author: baseline failed: %s", exc)

    if artifacts.requirements:
        reqs = "\n".join(f"- {r[:600]}" for r in artifacts.requirements[:80]
                         if (r or "").strip())
        if reqs:
            blocks.append("Requirements / notes supplied by the operator:\n"
                          + reqs)

    if artifacts.attachments:
        att_lines: list[str] = []
        for att in artifacts.attachments[:8]:
            excerpt = (att.get("excerpt") or "").strip()
            if excerpt:
                att_lines.append(
                    f"--- {(att.get('name') or 'attachment').strip()} ---\n"
                    + excerpt[:4000])
        if att_lines:
            blocks.append("Attachment excerpts:\n" + "\n\n".join(att_lines))

    if artifacts.custom_prompt:
        blocks.append(
            "Operator instruction — this narrows or steers the scope and "
            "outranks the default coverage breadth:\n"
            + artifacts.custom_prompt[:2000])

    blocks.append("=== RETURN JSON MATCHING THIS SHAPE EXACTLY ===\n"
                  + _SCHEMA_HINT)
    return "\n\n".join(blocks)


def _baseline_text(checklist: _rules.LowLevelChecklist,
                   limit: int = 140) -> str:
    lines: list[str] = []
    for section in checklist.sections:
        lines.append(f"[{section.name}]")
        for check in section.checks:
            if len(lines) > limit:
                lines.append("  … (truncated)")
                return "\n".join(lines)
            indent = "  " if check.depth == 2 else "    "
            lines.append(f"{indent}{check.objective}")
    return "\n".join(lines)


# ── LLM call ─────────────────────────────────────────────────────────

def _call_llm(artifacts: Artifacts, profile: Any) -> str:
    resp = call_messages(
        timeout=_AUTHOR_TIMEOUT,
        kind=_AUTHOR_KIND,
        max_tokens=_AUTHOR_MAX_TOKENS,
        system=_system_blocks(),
        messages=[{"role": "user",
                   "content": _build_user_prompt(artifacts, profile)}],
    )
    usage = getattr(resp, "usage", None)
    if usage is not None:
        _logger.info(
            "checklist_author usage: input=%s output=%s cache_read=%s",
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


# ── Coercion + enforcement ───────────────────────────────────────────

_CATEGORIES = ("Positive", "Negative", "Edge case", "Security",
               "Performance", "Accessibility")

#: "1.", "2)", "2.7.1 ", "- ", "* " — anything the model prefixed a row
#: with. Hierarchical forms included, since those are the ones it reaches
#: for after reading a numbered reference sheet.
_LEADING_MARKER_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*[.)]?\s+|[-*•]\s+)")
_PRIORITIES = ("High", "Medium", "Low")


def _coerce_category(value: Any) -> str:
    raw = str(value or "").strip().lower()
    for cat in _CATEGORIES:
        if raw == cat.lower():
            return cat
    return "Negative" if raw.startswith("neg") else "Positive"


def _coerce_priority(value: Any) -> str:
    raw = str(value or "").strip().title()
    return raw if raw in _PRIORITIES else "Medium"


def evidenced_labels(artifacts: Artifacts) -> set[str]:
    """Every label the artefacts actually name, lower-cased.

    The set the "do not invent controls" rule is checked against. Built
    from the crawler records plus the requirement and attachment text,
    because a control named only in a spec is still evidenced.
    """
    out: set[str] = set()

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if text:
            out.add(text.lower())

    for page in artifacts.pages or []:
        if not isinstance(page, dict):
            continue
        for key in ("title", "h1"):
            _add(page.get(key))
        for key in ("headings", "nav_links", "buttons"):
            for item in (page.get(key) or []):
                _add(item)
        for key in ("header_links", "footer_links", "legal_links"):
            for link in (page.get(key) or []):
                if isinstance(link, dict):
                    _add(link.get("text"))
        for group in (page.get("nav_groups") or []):
            if isinstance(group, dict):
                _add(group.get("label"))
                for child in (group.get("children") or []):
                    _add(child)
        for social in (page.get("social_links") or []):
            if isinstance(social, dict):
                _add(social.get("network"))
        for form in (page.get("forms") or []):
            if not isinstance(form, dict):
                continue
            _add(form.get("submit_text"))
            for fld in (form.get("fields") or form.get("inputs") or []):
                if isinstance(fld, dict):
                    for key in ("label", "name", "placeholder", "id"):
                        _add(fld.get(key))
        for table in (page.get("tables") or []):
            if isinstance(table, dict):
                _add(table.get("caption"))
                for col in (table.get("columns") or []):
                    _add(col)

    for text in list(artifacts.requirements or []):
        _add(text)
    for att in (artifacts.attachments or []):
        if isinstance(att, dict):
            _add(att.get("excerpt"))
    _add(artifacts.custom_prompt)
    return out


_QUOTED_RE = re.compile(r'"([^"]{2,})"')


def unevidenced_quotes(objective: str, evidence: set[str]) -> list[str]:
    """Quoted labels in ``objective`` that no artefact contains.

    Only QUOTED strings are checked. A quoted label is a claim that those
    exact words appear on screen, which is checkable; unquoted prose is
    description, which is not. Substring matching, because a heading is
    routinely quoted as part of a longer sentence.
    """
    missing: list[str] = []
    blob = " || ".join(evidence)
    for quoted in _QUOTED_RE.findall(objective or ""):
        needle = quoted.strip().lower()
        if len(needle) < 3:
            continue
        if needle not in blob:
            missing.append(quoted)
    return missing


def normalise_check(check: _rules.Check) -> tuple[_rules.Check, list[str]]:
    """Apply the terminology rules, then report what survives.

    The same pass the deterministic path uses, so an authored row and an
    enumerated one are held to one standard.
    """
    # Strip any numbering or bullet the model wrote despite being told not
    # to. Numbering is ours (assign_numbers), and leaving "7.4" in the text
    # both duplicates the № column and defeats the opener fix below, which
    # would prepend a second "Verify that" in front of the digits.
    check.objective = _LEADING_MARKER_RE.sub("", check.objective or "")
    check.objective = glossary.normalise_text(check.objective,
                                              kind="objective")
    if check.objective:
        # The reviewer's "typo: Verify" comment, enforced rather than
        # requested — the opener is mechanical and the model should not
        # spend attention on it. Both halves matter: a row opening with a
        # lower-case "verify" satisfies the linter's startswith check and
        # still reads as a typo on the sheet.
        if not check.objective.lower().startswith("verify"):
            check.objective = ("Verify that " + check.objective[0].lower()
                               + check.objective[1:])
        check.objective = check.objective[0].upper() + check.objective[1:]
    check.category = _coerce_category(check.category)
    check.priority = _coerce_priority(check.priority)
    check.depth = 3 if int(check.depth or 2) >= 3 else 2
    return check, glossary.lint_text(check.objective, kind="objective")


def _coerce_result(payload: dict, artifacts: Artifacts
                   ) -> tuple[_rules.LowLevelChecklist, list[str], list[str]]:
    """Parsed JSON → ``(checklist, lint_findings, dropped)``."""
    out = _rules.LowLevelChecklist(
        surface=str(payload.get("surface") or "").strip(),
        url=artifacts.url,
        source="llm",
    )
    evidence = evidenced_labels(artifacts)
    lint: list[str] = []
    dropped: list[str] = []
    total = 0

    for raw_section in (payload.get("sections") or []):
        if not isinstance(raw_section, dict):
            continue
        name = re.sub(r"\s+", " ", str(raw_section.get("name") or "")).strip()
        if not name:
            continue
        section = _rules.Section(name=name[:120])
        for raw_check in (raw_section.get("checks") or []):
            if total >= _MAX_CHECKS:
                break
            objective = ""
            if isinstance(raw_check, dict):
                objective = str(raw_check.get("objective") or "").strip()
            elif isinstance(raw_check, str):
                raw_check, objective = {}, raw_check.strip()
            if not objective:
                continue

            check = _rules.Check(
                objective=objective,
                section=name,
                category=raw_check.get("category", "Positive"),
                priority=raw_check.get("priority", "Medium"),
                testing_type=str(raw_check.get("testing_type")
                                 or "Functional"),
                depth=int(raw_check.get("depth") or 2),
            )
            check, findings = normalise_check(check)

            # Evidence gate. The prompt asks for this; asking is not
            # enforcement, and a row naming a control that never existed
            # fails for the wrong reason — which is worse than a gap,
            # because a gap is visible and a wrong failure is not.
            invented = unevidenced_quotes(check.objective, evidence)
            if invented:
                dropped.append(
                    f"{check.objective[:80]} — no artefact names "
                    + ", ".join(f'"{i}"' for i in invented[:3]))
                continue

            section.checks.append(check)
            total += 1
            lint.extend(f"{name}: {f}" for f in findings)
        if section.checks:
            out.sections.append(section)

    out.gaps = [str(g).strip() for g in (payload.get("gaps") or [])
                if str(g).strip()][:20]
    return _rules.assign_numbers(out), lint, dropped


# ── Entry point ──────────────────────────────────────────────────────

def author_checklist(*, artifacts: Artifacts | None = None,
                     profile: Any = None,
                     force_llm: bool = False) -> AuthoredChecklist:
    """Author a low-level checklist, falling back to the enumeration.

    ``force_llm`` calls the model without ``ANTHROPIC_API_KEY`` — for
    tests that monkeypatch :func:`engine.llm_client.call_messages`.

    Never raises. Every failure path lands on
    :func:`engine.checklist_rules.build_checklist`, and ``source`` reports
    which produced the result.
    """
    artifacts = artifacts or Artifacts()

    def _fallback(reason: str) -> AuthoredChecklist:
        built = _rules.build_checklist(artifacts.pages, url=artifacts.url)
        if reason:
            _logger.info("checklist_author: %s — using the enumeration",
                         reason)
        return AuthoredChecklist(checklist=built, source="deterministic",
                                 gaps=list(built.gaps))

    if artifacts.is_empty():
        return _fallback("no artefacts")
    if not _author_enabled():
        return _fallback("CL_AUTHOR_ENABLED=0")
    if not force_llm and not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return _fallback("no API key")

    try:
        raw = _call_llm(artifacts, profile)
    except LLMUnavailable as exc:
        return _fallback(f"LLM unavailable: {exc}")
    except Exception as exc:  # pragma: no cover — never lose the pack
        _logger.warning("checklist_author: call failed: %s", exc)
        return _fallback("call failed")

    payload = _parse_llm_response(raw)
    if payload is None:
        return _fallback("unparseable response")

    try:
        built, lint, dropped = _coerce_result(payload, artifacts)
    except Exception as exc:  # pragma: no cover — defensive
        _logger.warning("checklist_author: coercion failed: %s", exc)
        return _fallback("coercion failed")

    if not built.total:
        # An empty authored sheet is worse than an enumerated one — the
        # enumeration at least covers the surface.
        return _fallback("the agent returned no usable rows")

    result = AuthoredChecklist(
        checklist=built,
        source="llm",
        rationale=str(payload.get("rationale") or "").strip()[:600],
        gaps=list(built.gaps),
        lint_findings=lint[:40],
        dropped=dropped[:40],
    )
    if dropped:
        # Said out loud rather than silently swallowed: the operator needs
        # to know the agent reached for controls the crawl never saw.
        n = len(dropped)
        result.gaps.append(
            f"{n} authored row{'s' if n != 1 else ''} "
            f"{'were' if n != 1 else 'was'} dropped for naming controls no "
            f"artefact evidences.")
    _logger.info("checklist_author: %d rows over %d sections "
                 "(%d dropped, %d lint findings)",
                 built.total, len(built.sections), len(dropped), len(lint))
    return result


def to_checklist_items(result: AuthoredChecklist) -> list:
    """Convert to the module's ``ChecklistItem`` dataclass."""
    return _rules.to_checklist_items(result.checklist)


__all__ = [
    "Artifacts", "AuthoredChecklist",
    "author_checklist", "to_checklist_items",
    "checklist_style_text", "evidenced_labels", "unevidenced_quotes",
    "normalise_check",
]
