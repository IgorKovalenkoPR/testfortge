"""TestFortge — Shared helpers used across route modules.

These were previously module-level helpers in ``app.py``. Extracting them
here lets each ``routes/*.py`` module depend on a stable, framework-free
utility surface without circular imports back into ``app``.

Helpers that need the Flask app or request context (file uploads,
session-backed URL extraction) are still imported from Flask here,
but nothing in this module mutates the app — callers pass the app or
current-request object in where needed.
"""

from __future__ import annotations

import os
import re
import time
import uuid

from flask import current_app, request, session
from werkzeug.utils import secure_filename

from engine.file_parser import parse_file, split_into_requirements, allowed_file  # noqa: F401 re-exported
from engine.qa_persona import is_instruction
from engine.user_story_generator import UserStory
from engine.testcase_generator import TestCase, ChecklistItem
from engine.log import get_logger

log = get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────

# Folder / asset name validation — keeps directory-traversal attempts
# out of filesystem operations.
SAFE_FOLDER_RE = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")
SAFE_ASSET_RE = re.compile(r"^[A-Za-z0-9_\-./]+$")

# URL regex — shared between requirement parsing and resource-URL extraction.
URL_PATTERN = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)
_URL_IN_LINE = re.compile(r"(https?://[^\s,]+)", re.IGNORECASE)

# Session keys wiped by new-session and cross-restart invalidation.
GENERATED_KEYS = (
    "project_setup", "raw_requirements", "user_stories",
    "test_cases_data", "checklist_data", "traceability_data", "custom_prompt",
    "execution_results", "bug_reports_data", "test_runs",
)

# Server-start timestamp — sessions persisted from previous boots get wiped.
SERVER_START_TIME = time.time()


# ── Reconstruction helpers (dict → dataclass) ────────────────────

def reconstruct_stories(data: list[dict]) -> list[UserStory]:
    if not data:
        return []
    return [UserStory(**s) for s in data]


def reconstruct_test_cases(data: list[dict]) -> list[TestCase]:
    if not data:
        return []
    return [TestCase(**tc) for tc in data]


def reconstruct_checklist(data: list[dict]) -> list[ChecklistItem]:
    if not data:
        return []
    return [ChecklistItem(**cl) for cl in data]


# ── Serialization helpers (dataclass → dict) ─────────────────────

def tc_to_dict(tc: TestCase) -> dict:
    return {
        "id": tc.id, "section": tc.section, "section_num": tc.section_num,
        "summary": tc.summary, "preconditions": tc.preconditions,
        "test_steps": tc.test_steps, "test_data": tc.test_data,
        "expected_result": tc.expected_result, "issues": tc.issues,
        "comment": tc.comment, "user_story_id": tc.user_story_id,
        "category": tc.category, "priority": tc.priority, "status": tc.status,
        "testing_type": getattr(tc, "testing_type", "Functional"),
    }


def cl_to_dict(cl: ChecklistItem) -> dict:
    return {
        "id": cl.id, "section": cl.section, "objective": cl.objective,
        "comments": cl.comments, "user_story_id": cl.user_story_id,
        "category": cl.category, "priority": cl.priority, "status": cl.status,
        "testing_type": getattr(cl, "testing_type", "Functional"),
    }


def story_to_dict(s: UserStory) -> dict:
    return {
        "id": s.id, "requirement_id": s.requirement_id, "role": s.role,
        "action": s.action, "benefit": s.benefit, "priority": s.priority,
        "story_points_hint": s.story_points_hint,
        "acceptance_criteria": s.acceptance_criteria, "original_text": s.original_text,
    }


# ── Input parsing ────────────────────────────────────────────────

def parse_page_input(file_field: str = "input_files",
                     text_field: str = "input_text") -> tuple[list[str], list[str], str]:
    """Parse file uploads + text input + custom prompt from any form.

    Instruction-like lines found in the text field (e.g. "Pay attention...",
    "Regenerate...") are automatically extracted from the requirements list
    and appended to the custom_prompt so they influence generation style
    without polluting the requirements/stories.
    """
    raw_lines: list[str] = []
    errors: list[str] = []

    files = request.files.getlist(file_field)
    for f in files:
        if not (f and f.filename and allowed_file(f.filename)):
            continue
        safe_name = secure_filename(f.filename) or "upload.bin"
        filepath = os.path.join(current_app.config["UPLOAD_FOLDER"], safe_name)
        f.save(filepath)
        lines, err = parse_file(filepath, safe_name)
        raw_lines.extend(lines)
        if err:
            errors.append(f"{safe_name}: {err}")

    text_input = request.form.get(text_field, "").strip()
    if text_input:
        raw_lines.extend([l.strip() for l in text_input.splitlines() if l.strip()])

    custom_prompt = request.form.get("custom_prompt", "").strip()

    # Move instruction-like lines from raw_lines into custom_prompt.
    # If an instruction line contains a URL, preserve the URL as a separate
    # requirement line so site-crawling still picks it up.
    actual_lines: list[str] = []
    instruction_lines: list[str] = []
    for line in raw_lines:
        if is_instruction(line):
            instruction_lines.append(line)
            url_match = _URL_IN_LINE.search(line)
            if url_match:
                actual_lines.append(url_match.group(1))
        else:
            actual_lines.append(line)

    if instruction_lines:
        extra = "\n".join(instruction_lines)
        custom_prompt = f"{custom_prompt}\n{extra}".strip() if custom_prompt else extra
        raw_lines = actual_lines

    return raw_lines, errors, custom_prompt


# ── Resource URL extraction ──────────────────────────────────────

def extract_resource_urls() -> list[str]:
    """Extract unique URLs from raw requirements stored in session."""
    raw_reqs = session.get("raw_requirements", [])
    urls: list[str] = []
    seen: set[str] = set()
    for req in raw_reqs:
        text = req.get("text", "") if isinstance(req, dict) else str(req)
        for m in URL_PATTERN.findall(text):
            url = m.rstrip(".,;)")
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


# ── Session identity (for per-session rate limiting) ─────────────

def get_session_id(session_obj=None) -> str:
    """Return a stable identifier for the caller's session.

    Used by the async routes to cap concurrent jobs per session. Order
    of preference:

    1. ``session.sid`` — Flask-Session populates this on filesystem /
       Redis / Memcached backends. It's the real session file key, so
       different browsers/cookies naturally get different ids.
    2. ``session["_tf_sid"]`` — a UUID we mint and store in the session
       itself. Covers the signed-cookie backend (no ``sid`` attribute)
       and any custom backend that doesn't set ``sid``.

    The id is opaque — we only compare it for equality, never parse or
    expose it.
    """
    sess = session_obj if session_obj is not None else session
    sid = getattr(sess, "sid", None)
    if sid:
        return sid
    sid = sess.get("_tf_sid")
    if not sid:
        sid = uuid.uuid4().hex
        sess["_tf_sid"] = sid
    return sid


__all__ = [
    # constants
    "SAFE_FOLDER_RE", "SAFE_ASSET_RE", "URL_PATTERN", "GENERATED_KEYS",
    "SERVER_START_TIME",
    # dataclass <-> dict
    "reconstruct_stories", "reconstruct_test_cases", "reconstruct_checklist",
    "tc_to_dict", "cl_to_dict", "story_to_dict",
    # input parsing
    "parse_page_input",
    # resource helpers
    "extract_resource_urls",
    # session
    "get_session_id",
]
