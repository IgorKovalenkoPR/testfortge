"""
TestFortge — Senior Automation QA Engineer Persona

ISTQB Test Automation Engineer, 8+ years with Playwright, Selenium, Cypress.
Converts manual TestCase objects into runnable Playwright Python scripts.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field


@dataclass
class AutomationStep:
    action: str          # goto|click|fill|select|check|expect_text|expect_url|wait|screenshot
    target: str = ""     # selector or URL
    value: str = ""      # text to fill / expected text
    raw: str = ""        # original manual step text
    comment: str = ""


@dataclass
class AutomationScript:
    tc_id: str
    summary: str
    base_url: str
    preconditions: list[str] = field(default_factory=list)
    steps: list[AutomationStep] = field(default_factory=list)
    expected_result: str = ""


# ---------- Step parser ----------

_URL_RE = re.compile(r"https?://[^\s)'\"]+")
_QUOTE_RE = re.compile(r"[\"'«»]([^\"'«»]{1,80})[\"'«»]")

_ACTION_PATTERNS = [
    (re.compile(r"\b(navigate|go|open|visit|перейти|відкрити)\b", re.I), "goto"),
    (re.compile(r"\b(click|tap|press|натиснути|клікнути)\b", re.I),     "click"),
    (re.compile(r"\b(enter|type|input|fill|ввести|заповнити)\b", re.I), "fill"),
    (re.compile(r"\b(select|choose|обрати|вибрати)\b", re.I),           "select"),
    (re.compile(r"\b(check|tick|позначити)\b", re.I),                   "check"),
    (re.compile(r"\b(verify|ensure|should|перевірити|має)\b", re.I),    "expect_text"),
    (re.compile(r"\b(wait|зачекати)\b", re.I),                          "wait"),
]


def _detect_action(step_text: str) -> str:
    for rx, action in _ACTION_PATTERNS:
        if rx.search(step_text):
            return action
    return "expect_text"


def _extract_quoted(text: str) -> str:
    m = _QUOTE_RE.search(text)
    return m.group(1) if m else ""


def _extract_url(text: str) -> str:
    m = _URL_RE.search(text)
    return m.group(0) if m else ""


def parse_manual_step(raw_step: str, base_url: str = "") -> AutomationStep:
    """Convert a single manual step line into an AutomationStep."""
    text = raw_step.strip().lstrip("0123456789.)- ")
    action = _detect_action(text)
    quoted = _extract_quoted(text)
    url = _extract_url(text)

    step = AutomationStep(action=action, raw=raw_step)

    if action == "goto":
        step.target = url or base_url
    elif action in ("click", "check"):
        step.target = quoted or _guess_role_target(text)
    elif action == "fill":
        # "Enter 'user@example.com' into Email field"
        step.target = _guess_field_target(text)
        step.value = quoted
    elif action == "select":
        step.target = _guess_field_target(text)
        step.value = quoted
    elif action == "expect_text":
        step.value = quoted or text
    elif action == "wait":
        step.value = _extract_seconds(text)
    return step


def _guess_role_target(text: str) -> str:
    low = text.lower()
    for role in ("submit", "login", "register", "sign in", "sign up",
                 "save", "delete", "edit", "cancel", "ok", "next", "continue"):
        if role in low:
            return f"role=button[name=/{role}/i]"
    return "role=button"


def _guess_field_target(text: str) -> str:
    low = text.lower()
    pairs = [("email", "email"), ("password", "password"),
             ("username", "username"), ("login", "login"),
             ("search", "search"), ("name", "name"), ("phone", "phone")]
    for needle, label in pairs:
        if needle in low:
            return f"placeholder=/{label}/i"
    return "role=textbox"


def _extract_seconds(text: str) -> str:
    m = re.search(r"(\d+)\s*(second|sec|s|секунд)", text, re.I)
    return m.group(1) if m else "2"


# ---------- TC → Script ----------

def tc_to_script(tc: dict, base_url: str = "") -> AutomationScript:
    """Convert a TestCase dict from session['test_cases_data'] into a script."""
    script = AutomationScript(
        tc_id=tc.get("id", ""),
        summary=tc.get("summary", ""),
        base_url=base_url,
        expected_result=tc.get("expected_result", ""),
    )
    pre = tc.get("preconditions", "")
    if pre:
        script.preconditions = [p.strip() for p in pre.splitlines() if p.strip()]
    steps_text = tc.get("test_steps", "") or tc.get("steps", "")
    if isinstance(steps_text, str):
        lines = [ln for ln in steps_text.splitlines() if ln.strip()]
    else:
        lines = list(steps_text)
    if base_url and not any(_URL_RE.search(ln) for ln in lines[:1]):
        lines.insert(0, f"Navigate to {base_url}")
    for line in lines:
        script.steps.append(parse_manual_step(line, base_url))
    # Add expectation from expected result
    if script.expected_result:
        script.steps.append(AutomationStep(
            action="expect_text",
            value=_extract_quoted(script.expected_result) or script.expected_result[:80],
            raw=f"EXPECT: {script.expected_result}",
        ))
    return script


def scripts_from_session(test_cases: list[dict], base_url: str) -> list[AutomationScript]:
    return [tc_to_script(tc, base_url) for tc in test_cases]
