"""Text classification + slug helpers used by the QA persona.

Separated from qa_persona so routes and engines can import the small,
side-effect-free helpers without dragging the full YAML loader.
"""

from __future__ import annotations

import re

_INSTRUCTION_PATTERNS_EN = [
    r"^(please\s+)?(create|generate|re-?generate|write|make|build|prepare|design|draft|develop|produce)\s+",
    r"^(should|must|need\s+to|have\s+to)\s+(be\s+)?(covered|included|tested|checked|verified)",
    r"^(include|add|cover|ensure|focus\s+on)\s+",
    r"^(positive|negative|edge|boundary|security|performance)\s+(cases?|scenarios?|tests?|checks?)\s+(should|must|need)",
    r"^(pay\s+attention|note\s+that|keep\s+in\s+mind|make\s+sure|remember\s+that|consider\s+that)\s+",
    r"^(each|every|all)\s+(acceptance|test|user\s+stor|scenario|requirement).*\bshould\b",
    r"^re-?generate\b",
]
_INSTRUCTION_PATTERNS_UA = [
    r"^(створи|згенеруй|перегенеруй|напиши|зроби|підготуй|розроби|побудуй)\s+",
    r"^(мають?|повинн[іа]|потрібно|необхідно)\s+(бути\s+)?(покрит|включен|перевірен|протестован)",
    r"^(додай|включи|забезпеч|покрий|перевір)\s+",
    r"^(позитивні|негативні|граничні|edge|boundary)\s+(сценарії|випадки|тести|перевірки)\s+(мають|повинні|потрібно)",
    r"^(зверни\s+увагу|врахуй|пам.ятай|переконайся)\s+",
    r"^перегенеруй\b",
]

_INSTRUCTION_RE = [re.compile(p, re.IGNORECASE) for p in
                   _INSTRUCTION_PATTERNS_EN + _INSTRUCTION_PATTERNS_UA]


def is_instruction(text: str) -> bool:
    stripped = text.strip()
    for pat in _INSTRUCTION_RE:
        if pat.search(stripped):
            return True
    return False


def detect_flows(text: str) -> list[str]:
    """Return named flow keys (e.g. ``checkout_flow``) triggered by *text*."""
    try:
        from .knowledge_base import FLOW_PLAYBOOKS
    except Exception:  # pragma: no cover
        return []
    lower = (text or "").lower()
    hits: list[str] = []
    for key, pb in FLOW_PLAYBOOKS.items():
        for trg in pb.get("triggers", []):
            if trg.lower() in lower:
                hits.append(key)
                break
    return hits


# Patterns that look like CSS-generated instance IDs we never want
# to surface as part of a human-readable form name.
_INSTANCE_ID_RE = re.compile(
    r"#?(wpcf7-f\d+-o\d+|gform_\d+|elementor-\w+|"
    r"[A-Fa-f0-9]{8,}|"
    r"\w*-?id-?\d+\w*)",
    re.IGNORECASE,
)


_GENERIC_SUBMIT_TEXTS = {
    "submit", "send", "go", "ok", "next", "continue", "click here",
    "відправити", "надіслати", "далі",
}


def humanise(s: str) -> str:
    """'wpcf7-f14405' → 'wpcf7 f14405'; 'first_name' → 'first name'."""
    s = re.sub(r"[-_]+", " ", s).strip()
    s = re.sub(r"\s+", " ", s)
    s = s.strip(":;,.!? ")
    return s


def looks_like_id(s: str) -> bool:
    return bool(_INSTANCE_ID_RE.search(s)) or len(s) > 60


def looks_generic_submit(text: str) -> bool:
    return text.strip().lower() in _GENERIC_SUBMIT_TEXTS


def sanitise_action(action: str) -> str:
    """'/contact#wpcf7-f14405-o1?foo=bar' → 'contact'."""
    s = re.sub(r"^https?://[^/]+", "", action).strip()
    s = s.split("?", 1)[0]
    s = s.split("#", 1)[0]
    s = _INSTANCE_ID_RE.sub("", s)
    s = s.strip(" /")
    if not s or s == "root":
        return ""
    last = s.split("/")[-1]
    return humanise(last)


def slugify_section(label: str) -> str:
    label = (label or "").strip()
    label = re.sub(r"\s*[\|\-–——]\s*[^|\-–——]+$", "", label)
    label = label[:60].rstrip(" .|-—")
    return label or "Page"


def path_label(url: str) -> str:
    m = re.match(r"https?://([^/]+)(/.*)?", url)
    if not m:
        return url[:40]
    host, path = m.group(1), (m.group(2) or "/")
    label = path.strip("/").replace("/", " > ") or host
    return label.replace("-", " ").replace("_", " ")[:60]


def form_label(form: dict) -> str:
    """Best-effort human-readable label for a crawled form."""
    heading = (form.get("heading") or "").strip()
    if heading:
        return humanise(heading) + " form"

    submit = (form.get("submit_text") or "").strip()
    if submit and not looks_generic_submit(submit):
        return humanise(submit) + " form"

    placeholders = []
    for f in (form.get("fields") or []):
        if f.get("type") in ("hidden", "submit", "button"):
            continue
        for key in ("placeholder", "label", "name"):
            val = (f.get(key) or "").strip()
            if val and not looks_like_id(val):
                placeholders.append(humanise(val))
                break
    placeholders = [p for p in placeholders if p][:3]
    if placeholders:
        return f"form ({', '.join(placeholders)})"

    action = (form.get("action") or "").strip()
    if action:
        cleaned = sanitise_action(action)
        if cleaned:
            return f"{cleaned} form"

    return "form"
