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
    # Pending JobQueue ids — without these in the wipe list, /new-session
    # leaves a finished tc_gen / cl_gen job behind and the next GET
    # /test-cases drains it back into the freshly-cleared session.
    "tc_gen_job_id", "cl_gen_job_id",
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
    # Light "learning across requests": when the operator leaves the
    # textarea empty, fall back to the last non-empty instruction they
    # used in this session. Persist freshly-typed instructions so the
    # next page (or next generation) sees them. Cap to 1 KB to keep
    # the session pickle small.
    try:
        from flask import session as _session
        if custom_prompt:
            _session["last_custom_prompt"] = custom_prompt[:1024]
        elif _session.get("last_custom_prompt"):
            custom_prompt = _session["last_custom_prompt"]
    except Exception:
        pass

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


# ── Active-project resolver (Phase 2) ────────────────────────────

def ensure_active_project(session_obj=None) -> str:
    """Return the active project id, creating one if the user hasn't
    explicitly picked one yet.

    Resolution order:
      1. ``session["project_id"]`` — explicit current pick.
      2. Most recent project owned by this session id (Render free-tier
         wipes session files on restart, but the cookie + Postgres data
         persist; without this fallback the user sees a brand-new
         "Untitled project" every time the dyno wakes from sleep, with
         the original test-case pack orphaned. Operator-reported on
         2026-05-04.)
      3. Auto-create a fresh project with a timestamped name.

    Centralising this logic keeps every Phase-2 hook (estimation, TC,
    checklist, bugs, runs, Tedgie submissions) on the same code path —
    no module has to re-implement the "no active project" UX.
    """
    from datetime import datetime as _dt
    # Local import to avoid circular dependency between routes/_shared
    # and engine.db (which doesn't depend on us, but the import order
    # at module-load is fragile when chained with Flask).
    from engine import db as _db

    sess = session_obj if session_obj is not None else session
    pid = sess.get("project_id")
    if pid:
        return pid

    sid = get_session_id(sess)

    # Recovery path: the cookie carries the same SID across Render
    # restarts (SECRET_KEY is preserved per render.yaml), so any project
    # already created by this session is still tagged with this owner_
    # sid in Postgres. Pick the most recent one before falling back to
    # auto-create — otherwise the user gets an empty "Untitled project"
    # while their actual TC pack lives under the old project_id.
    try:
        if hasattr(_db, "list_projects"):
            existing = _db.list_projects(owner_sid=sid) or []
            if existing:
                # list_projects returns most-recent-first per its sort.
                # Defensive: also accept created_at desc if present.
                pick = existing[0]
                pid = pick.get("id") if isinstance(pick, dict) else None
                if pid:
                    log.info(
                        "ensure_active_project: rehydrated project_id=%s "
                        "from owner_sid=%s (session was empty)",
                        pid, sid[:8])
                    sess["project_id"] = pid
                    setup = sess.get("project_setup") or {}
                    if isinstance(pick, dict) and pick.get("name"):
                        setup.setdefault("project_name", pick["name"])
                    sess["project_setup"] = setup
                    sess.modified = (True if hasattr(sess, "modified")
                                     else None)
                    return pid
    except Exception as exc:
        log.debug("ensure_active_project: owner_sid lookup failed: %s",
                  exc)

    name = "Untitled project " + _dt.now().strftime("%Y-%m-%d %H:%M")
    try:
        pid = _db.upsert_project(name=name, owner_sid=sid)
    except Exception as exc:
        log.warning("ensure_active_project: auto-create failed: %s", exc)
        return ""
    sess["project_id"] = pid
    setup = sess.get("project_setup") or {}
    setup.setdefault("project_name", name)
    sess["project_setup"] = setup
    sess.modified = True if hasattr(sess, "modified") else None
    return pid


def get_picker_context(session_obj=None) -> dict:
    """Build the ``projects`` + ``active_project_id`` template kwargs
    that ``_project_picker.html`` needs.

    Centralised so every module's GET handler doesn't have to repeat
    the same DB call. Best-effort: a DB outage returns an empty list
    instead of raising — the picker still renders its "Create new"
    form so the user has a way forward.

    Returned dict shape::

        {
            "projects": [{ "id": "...", "name": "...",
                           "test_cases_count": int,
                           "checklist_count": int,
                           "bug_count": int, ... }, ...],
            "active_project_id": "uuid-hex-or-empty-string",
        }
    """
    sess = session_obj if session_obj is not None else session
    try:
        from engine import db as _db
        sid = get_session_id(sess)
        projects = (_db.list_projects(owner_sid=sid)
                    if hasattr(_db, "list_projects") else [])
    except Exception as exc:
        log.debug("get_picker_context list_projects failed: %s", exc)
        projects = []
    return {
        "projects": projects or [],
        "active_project_id": sess.get("project_id") or "",
    }


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
    # project picker
    "get_picker_context",
    "ensure_active_project",
]
