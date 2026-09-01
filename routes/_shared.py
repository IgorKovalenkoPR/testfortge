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
from engine.llm_safety import MAX_CUSTOM_PROMPT_CHARS, cap as _cap_prompt
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
        # Sprint 5 walkthrough binding (read via getattr so old session
        # blobs deserialised before these fields existed still round-
        # trip cleanly).
        "url_pattern":  getattr(tc, "url_pattern", "") or "",
        "trigger":      getattr(tc, "trigger", "manual") or "manual",
        # PR-B / PR-D / PR-3 — carried so a generation round-trip through
        # the session blob does not drop them on the way to save_test_cases.
        "automation_steps_json": getattr(tc, "automation_steps_json", "") or "",
        "suite":        getattr(tc, "suite", "") or "",
        "tc_format":    getattr(tc, "tc_format", "manual") or "manual",
        "gherkin":      getattr(tc, "gherkin", "") or "",
    }


def cl_to_dict(cl: ChecklistItem) -> dict:
    return {
        "id": cl.id, "section": cl.section, "objective": cl.objective,
        "comments": cl.comments, "user_story_id": cl.user_story_id,
        "category": cl.category, "priority": cl.priority, "status": cl.status,
        "testing_type": getattr(cl, "testing_type", "Functional"),
        # PR-2 hierarchy. Dropping these here meant the site-aware
        # generation path persisted a checklist with no numbering at all:
        # the generator produced 1 / 1.1 / 2.7.1, save_checklist knew the
        # columns, and this dict silently lost them in between. Found by
        # the end-to-end pipeline test, which is the only place the whole
        # chain runs. Same class of bug as tc_to_dict dropping `suite`.
        "item_num": getattr(cl, "item_num", "") or "",
        "depth": int(getattr(cl, "depth", 2) or 2),
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

    # Sprint 4 task 4.4: cap the live value that flows into the generator
    # so an adversarially long custom_prompt cannot expand procedurally
    # nor leak into LLM calls beyond the documented limit.
    custom_prompt = _cap_prompt(custom_prompt, MAX_CUSTOM_PROMPT_CHARS)

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
#
# PR-L: persistent signed SID cookie.
#
# Pre-PR-L the SID was stored either as ``session.sid`` (Flask-Session
# filesystem backend, derived from the session file path) or as
# ``session["_tf_sid"]`` (a UUID we minted inside the session dict).
# Both routes live INSIDE the filesystem-backed session store. On
# Render's free tier the container filesystem is wiped on every
# redeploy → the session file disappears → ``_tf_sid`` is regenerated
# from scratch → projects in Postgres tagged with the old SID become
# invisible to the user (the dropdown shows an auto-generated
# "Untitled project YYYY-MM-DD HH:MM" instead). Operator-reported
# repeatedly across this work session.
#
# Fix: store a copy of the SID in a SEPARATE signed cookie
# (``_tfg_sid_v1``) signed by ``SECRET_KEY``. SECRET_KEY is set as a
# Render env-var and persists across redeploys (render.yaml line 45,
# ``generateValue: true`` only fires on first deploy). The browser
# keeps the cookie even when the server-side session file is wiped, so
# the SID survives. ``ensure_active_project`` then re-derives the
# project_id from Postgres via ``list_projects(owner_sid=sid)`` — the
# user sees their original projects again.

_PERSISTENT_SID_COOKIE = "_tfg_sid_v1"
_PERSISTENT_SID_SALT = "tfg-persistent-sid-v1"
# Two years — long enough to survive any realistic gap between user
# visits without being unbounded. Browser will refresh the cookie on
# every response that sets it (we re-set on every request to keep the
# rolling expiry).
_PERSISTENT_SID_MAX_AGE = 60 * 60 * 24 * 365 * 2


def _make_sid_serializer():
    """Build a fresh URLSafeSerializer keyed by the app's SECRET_KEY.

    Constructed on demand because ``current_app`` is only available
    inside a request / app context — we cannot stash a module-level
    serializer at import time.
    """
    from itsdangerous import URLSafeSerializer
    secret = current_app.config["SECRET_KEY"]
    return URLSafeSerializer(secret, salt=_PERSISTENT_SID_SALT)


def _read_persistent_sid_cookie() -> str | None:
    """Return the SID stored in the persistent cookie, or ``None`` if
    the cookie is missing or its signature doesn't validate. Failure
    is silent — the caller falls back to the session-stored SID and
    we re-mint a fresh cookie on the way out.
    """
    raw = request.cookies.get(_PERSISTENT_SID_COOKIE)
    if not raw:
        return None
    try:
        sid = _make_sid_serializer().loads(raw)
    except Exception:
        return None
    if not isinstance(sid, str) or not sid:
        return None
    return sid


def _persistent_sid_cookie_value(sid: str) -> str:
    """Return the signed cookie payload for *sid*. Pure function — the
    after-request hook in ``app.py`` reads this and sets the response
    cookie. Kept here so the signing code lives next to the reader.
    """
    return _make_sid_serializer().dumps(sid)


#: Session key naming the project this session auto-created for itself.
#:
#: ``ensure_active_project`` invents an "Untitled project …" when the caller
#: has nowhere to write. That pin is the product's guess, not the operator's
#: choice, and it is the only one anything may silently replace — a project
#: the operator created or picked in the picker must survive every page they
#: open. ``routes/execution._maybe_restore_pack_from_db`` is the consumer.
AUTOCREATED_KEY = "_project_autocreated"


def get_session_id(session_obj=None) -> str:
    """Return a stable identifier for the caller's session.

    Used by the async routes to cap concurrent jobs per session AND
    (via :func:`ensure_active_project`) to scope projects to the
    browser that owns them.

    Order of preference (PR-L):

    1. ``_tfg_sid_v1`` signed cookie — persists across Render
       redeploys (browser-side, signed by stable ``SECRET_KEY``).
       This is the primary path post-PR-L.
    2. ``session.sid`` — Flask-Session backend identifier. Used for
       new browsers that haven't received the persistent cookie yet
       AND as a migration helper for users with existing sessions:
       on first request post-PR-L their session.sid is promoted to
       the persistent cookie (see ``set_persistent_sid_cookie`` hook
       in app.py).
    3. ``session["_tf_sid"]`` — legacy UUID fallback for sessions
       that lack a ``sid`` attribute.

    The id is opaque — we only compare it for equality, never parse
    or expose it.
    """
    sess = session_obj if session_obj is not None else session
    # 1. Persistent cookie (PR-L)
    sid = _read_persistent_sid_cookie()
    if sid:
        # Mirror into session for code paths that read ``_tf_sid``
        # without going through this function.
        if sess.get("_tf_sid") != sid:
            sess["_tf_sid"] = sid
        return sid
    # 2. Flask-Session backend sid
    sid = getattr(sess, "sid", None)
    if sid:
        # Mirror into ``_tf_sid`` so subsequent ``session["_tf_sid"]``
        # reads work. The after-request hook will also promote this
        # value into the persistent cookie.
        if sess.get("_tf_sid") != sid:
            sess["_tf_sid"] = sid
        return sid
    # 3. Legacy / signed-cookie backend fallback
    sid = sess.get("_tf_sid")
    if not sid:
        sid = uuid.uuid4().hex
        sess["_tf_sid"] = sid
    return sid


def needs_persistent_sid_cookie() -> bool:
    """Return True when the current request lacks the persistent SID
    cookie AND the resolved session id is worth promoting to a
    cookie. Called by the after-request hook in app.py.
    """
    if request.cookies.get(_PERSISTENT_SID_COOKIE):
        return False
    # We only promote when there's an established SID to copy — if
    # ``session`` is brand-new (no project, no test cases, no bugs)
    # we let the next "real" request mint the cookie so we don't
    # spend a Set-Cookie on static asset requests that don't even
    # touch the session.
    try:
        sess = session
        return bool(sess.get("_tf_sid"))
    except Exception:
        return False


def set_persistent_sid_cookie(response):
    """Mutate *response* to attach the persistent SID cookie.

    Called by the after-request hook in app.py when
    :func:`needs_persistent_sid_cookie` returns True. Idempotent:
    re-applies the same SID on every response so the rolling
    two-year expiry stays fresh even when the user opens the app
    after a long idle period.
    """
    try:
        sid = session.get("_tf_sid") or ""
    except Exception:
        return response
    if not sid:
        return response
    try:
        value = _persistent_sid_cookie_value(sid)
    except Exception:
        return response
    # ``secure`` mirrors Flask's session cookie config so we don't
    # accidentally send a cleartext cookie over HTTPS-only deploys.
    secure_cookie = bool(
        current_app.config.get("SESSION_COOKIE_SECURE", False)
    )
    response.set_cookie(
        _PERSISTENT_SID_COOKIE, value,
        max_age=_PERSISTENT_SID_MAX_AGE,
        httponly=True, samesite="Lax", secure=secure_cookie,
        path="/",
    )
    return response


# ── Active-project resolver (Phase 2) ────────────────────────────

def recoverable_project(session_obj=None) -> str:
    """The project a caller with no session pick may be *given back*.

    Recovery, not adoption — and the distinction is the whole function.
    Both resolvers below run when ``session["project_id"]`` is empty,
    which happens for two very different reasons:

    * the session store was wiped (Render's free plan does this on every
      restart) and the caller's own project is sitting in Postgres. Handing
      it back is the point of this code path;
    * the caller has genuinely never picked one — a new member of a team,
      or somebody who just cleared their session. Handing them *anything*
      is wrong.

    ``owner_sid`` separates the two, because every creation path writes it:
    ``routes/projects.py`` passes ``owner_sid=get_session_id()`` on both
    routes, and so does ``ensure_active_project``'s own auto-create. A
    project the caller's browser created is a project the caller may be
    given back without being asked.

    It is then intersected with :func:`visible_projects`, so a project this
    browser once created but whose organisation the caller has since left
    cannot come back through the side door.

    Returns "" when there is nothing to recover, which callers must treat
    as "no active project" rather than as a reason to pick one.
    """
    from engine import db as _db

    sess = session_obj if session_obj is not None else session
    if not hasattr(_db, "list_projects"):
        return ""
    sid = get_session_id(sess)
    if not sid:
        return ""
    own = _db.list_projects(owner_sid=sid) or []
    if not own:
        return ""
    allowed = {p.get("id") for p in (visible_projects(sess) or [])
               if isinstance(p, dict)}
    for pick in own:                      # list_projects sorts recent-first
        if not isinstance(pick, dict):
            continue
        pid = pick.get("id")
        if pid and pid in allowed:
            return pid
    return ""


def resolve_active_project(session_obj=None, *, pin: bool = True) -> str:
    """Active project id, or "" — never creating one as a side effect.

    Same first two steps as :func:`ensure_active_project` (session pick,
    then most-recent project owned by this session id in Postgres) but
    without step 3's auto-create, so read-only paths can resolve the
    project without writing to the DB.

    Needed because the cold-start recovery in
    ``routes/generation._hydrate_from_db`` originally read
    ``session["project_id"]`` directly. On the free plan a restart wipes
    the filesystem session store — the exact case hydration exists for —
    so that key was empty, hydration bailed out, and /test-cases rendered
    its empty state while the project picker (which goes through
    ``ensure_active_project``) correctly showed the project and its pack
    count. The work was in Postgres the whole time and still looked lost.
    """
    from engine import db as _db

    sess = session_obj if session_obj is not None else session
    pid = sess.get("project_id")
    if pid and not belongs_to_another_org(pid, sess):
        return pid
    if pid:
        # The pick is no longer the caller's to act on. Fall through rather
        # than return it: ``recoverable_project`` below already intersects
        # with what the caller may see, so the same request may still land
        # on a project they own. The stale key is left in the session —
        # ``pin=False`` means this call does not write, and an unhonoured
        # pin is harmless once nothing reads it as authority.
        log.warning("resolve_active_project: dropping pinned project_id=%s "
                    "— the caller may no longer act on it", pid[:8])

    sid = get_session_id(sess)
    try:
        if not hasattr(_db, "list_projects"):
            return ""
        # The caller's own project, never the first one they can see.
        #
        # This briefly did take the first *visible* project, to make the
        # picker and the endpoints agree after staging showed a page whose
        # header named a project next to a button answering
        # "no_active_project". The diagnosis was wrong: the header named
        # nothing. ``_project_picker.html`` renders every visible project as
        # an <option> and marks none of them selected when there is no
        # active id, so the browser displays the first — a select box
        # impersonating a choice the server had not made. Making the
        # endpoints adopt that first project made them agree with the
        # display instead of with the truth, and the cost was silent: a new
        # member of a team was handed a colleague's project without asking,
        # and the empty state became unreachable under ORG_MODE.
        #
        # The fix belongs in the picker, which now says "no project
        # selected" out loud. See recoverable_project for why owner_sid is
        # the right scope here even under ORG_MODE.
        pid = recoverable_project(sess)
        if not pid:
            return ""
        # Cache it so the rest of the request behaves as if the session
        # had never been wiped — unless the caller is only reading.
        #
        # ``pin=False`` exists because a read that changes what "active"
        # means is not a read. The pack accessors below call this on every
        # page, and with the write-back unconditional the dashboard
        # resurrected a project pointer immediately after "New session"
        # dropped it: the metrics read re-pinned it, and the page came back
        # claiming an active project the user had just cleared.
        if pin:
            sess["project_id"] = pid
            if hasattr(sess, "modified"):
                sess.modified = True
        log.info("resolve_active_project: recovered project_id=%s for "
                 "sid=%s (session store was wiped)", pid, sid[:8])
        return pid
    except Exception as exc:
        log.debug("resolve_active_project: recovery lookup failed: %s", exc)
        return ""


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
    if pid and not belongs_to_another_org(pid, sess):
        return pid
    if pid:
        # Same refusal as :func:`resolve_active_project`, and here the key
        # is actually cleared: this function writes to the session anyway,
        # and leaving a pick nothing honours makes the picker keep naming a
        # project every route refuses.
        log.warning("ensure_active_project: clearing pinned project_id=%s "
                    "— the caller may no longer act on it", pid[:8])
        sess.pop("project_id", None)
        if hasattr(sess, "modified"):
            sess.modified = True

    sid = get_session_id(sess)

    # Recovery path: give the caller back their own most recent project
    # before falling back to auto-create — otherwise the user gets an
    # empty "Untitled project" while their actual TC pack lives under the
    # old project_id.
    #
    # Their own, not the first one they can see: see recoverable_project.
    # Adopting a visible project here would be the louder half of the same
    # mistake — this function *writes*, so a new member of a team would
    # have had a colleague's project pinned into their session by the mere
    # act of opening a page.
    try:
        pid = recoverable_project(sess)
        if pid:
            log.info(
                "ensure_active_project: rehydrated project_id=%s "
                "for sid=%s (session was empty)", pid, sid[:8])
            sess["project_id"] = pid
            setup = sess.get("project_setup") or {}
            row = _db.get_project(pid) if hasattr(_db, "get_project") else None
            if isinstance(row, dict) and row.get("name"):
                setup.setdefault("project_name", row["name"])
            sess["project_setup"] = setup
            sess.modified = (True if hasattr(sess, "modified")
                             else None)
            return pid
    except Exception as exc:
        log.debug("ensure_active_project: recovery lookup failed: %s",
                  exc)

    name = "Untitled project " + _dt.now().strftime("%Y-%m-%d %H:%M")
    try:
        pid = _db.upsert_project(name=name, owner_sid=sid,
                                 org_id=org_for_new_project())
    except Exception as exc:
        log.warning("ensure_active_project: auto-create failed: %s", exc)
        return ""
    sess["project_id"] = pid
    # Stamped because nobody asked for this project. It is the placeholder
    # a page auto-creates so the caller has somewhere to write, and it is
    # the only pin the product is entitled to overrule later — see
    # ``AUTOCREATED_KEY``.
    sess[AUTOCREATED_KEY] = pid
    setup = sess.get("project_setup") or {}
    setup.setdefault("project_name", name)
    sess["project_setup"] = setup
    sess.modified = True if hasattr(sess, "modified") else None
    return pid


# ── The active project's pack (E3.3 / E3.4) ──────────────────────
#
# Every route module reads a project's artefacts through these. They are
# here rather than in ``engine.workspace`` because they need two things the
# repository deliberately does not know about: the Flask session's notion
# of which project is active, and the "the user asked for a clean slate"
# marker.
#
# The alternative — each module resolving the project and reading the
# repository itself — is how the modules ended up disagreeing in the first
# place. One of them would forget the cleared-pack check, and the symptom
# would be work reappearing after the user cleared it, on one page only.


def pack_cleared(session_obj=None) -> bool:
    """True when the user asked for a clean slate on this boot.

    ``/new-session`` stamps ``_pack_cleared_boot`` and drops
    ``project_id``. Dropping the pointer is not enough on its own, because
    :func:`resolve_active_project` recovers the most recent project owned by
    this session — so the next read finds the project again and, once
    Postgres is the source of truth, hands back the pack that was just
    cleared. The stamp is the discriminator, and it has to gate the read
    rather than only a fallback.

    Boot-stamped, so a genuine restart takes it with the rest of the
    session and cold-start recovery resumes on the next boot.
    """
    sess = session_obj if session_obj is not None else session
    return sess.get("_pack_cleared_boot") == SERVER_START_TIME


def pack_test_cases(session_obj=None) -> list:
    from engine import workspace as _ws
    if pack_cleared(session_obj):
        return []
    return _ws.test_cases(resolve_active_project(session_obj, pin=False))


def pack_checklist(session_obj=None) -> list:
    from engine import workspace as _ws
    if pack_cleared(session_obj):
        return []
    return _ws.checklist(resolve_active_project(session_obj, pin=False))


def pack_bugs(session_obj=None, *, run_id=None) -> list:
    """Bug reports for the active project.

    Not gated on :func:`pack_cleared`. "New session" clears the *generated
    pack*; a filed bug is a finding about a real defect, and making it
    vanish because somebody started a fresh generation would be a data-loss
    bug wearing a feature's clothes. ``/bugs/reset`` is the deliberate way
    to remove them, and it is admin-only.
    """
    from engine import workspace as _ws
    return _ws.bugs(resolve_active_project(session_obj, pin=False),
                    run_id=run_id)


def pack_runs(session_obj=None, *, limit: int = 20) -> list:
    """Execution runs for the active project. Not gated, same reason as
    :func:`pack_bugs` — a run happened."""
    from engine import workspace as _ws
    return _ws.runs(resolve_active_project(session_obj, pin=False),
                    limit=limit)


def pack_estimation(session_obj=None):
    from engine import workspace as _ws
    if pack_cleared(session_obj):
        return None
    return _ws.latest_estimation(resolve_active_project(session_obj, pin=False))


def pack_version(kind: str, session_obj=None) -> int:
    """The active project's current version for *kind*.

    Read this before a read-modify-write and pass it back to
    :func:`store_pack`, so a save that lost a race is refused instead of
    deleting the winner's rows. ``kind`` is ``"test_cases"`` or
    ``"checklist"``.
    """
    from engine import workspace as _ws
    pid = resolve_active_project(session_obj, pin=False)
    return int(_ws.pack_versions(pid).get(kind, 0)) if pid else 0


def mirror_pack(session_key: str, rows, session_obj=None) -> None:
    """Record that a pack was written, and keep the session copy in step.

    The ``_pack_cleared_boot`` pop is not subject to the early return
    below: every path that produces artefacts ends the cleared state, and
    writing that only in some of them is a bug that presents as "my
    generated work is invisible" (see E3.3).

    The mirror itself is a no-op once ``WORKSPACE_DB_FIRST`` is on. Until
    then it is load-bearing for whichever modules have not moved onto these
    accessors yet.
    """
    from engine import workspace as _ws
    sess = session_obj if session_obj is not None else session
    sess.pop("_pack_cleared_boot", None)
    if _ws.db_first():
        return
    sess[session_key] = rows
    if hasattr(sess, "modified"):
        sess.modified = True


def org_for_new_project() -> str | None:
    """The organisation a project created right now should belong to.

    ``None`` when org mode is off, which is every deployment today — the
    column stays NULL and nothing scopes by it.

    This exists because the two halves disagreed. ``visible_projects``
    lists only the caller's organisation's projects under ``ORG_MODE``,
    and nothing ever wrote ``Project.org_id``, so a project vanished from
    the picker the moment it was created. One helper, used by every
    creation path, is the only shape in which the two halves cannot drift
    again — four call sites each remembering to pass an argument is how
    this happened the first time.
    """
    try:
        from engine import permissions as _perm
        if not _perm.org_active():
            return None
        return _perm.current_org_id() or None
    except Exception as exc:  # pragma: no cover — defensive
        log.debug("org_for_new_project unavailable (%s)", exc)
        return None


# A project_id is a 32-char uuid hex string. Validated before any query
# runs, so a malformed id is short-circuited rather than looked up.
_PROJECT_ID_LEN = 32


def is_valid_project_id(s: str | None) -> bool:
    if not s or len(s) != _PROJECT_ID_LEN:
        return False
    return all(c in "0123456789abcdef" for c in s)


def project_access_with_meta(project_id: str | None,
                            session_obj=None) -> tuple[str, dict | None]:
    """May this caller act on *project_id*? A verdict and the row.

    ``"ok"`` — yes, and *meta* is the project. ``"missing"`` — no such
    project. ``"malformed"`` — not an id at all. And two refusals, because
    the two callers of this function want different amounts of it:

    * ``"forbidden_org"`` — the project belongs to an organisation the
      caller is not in.
    * ``"forbidden_owner"`` — a project from before organisations, and
      another browser's session created it.

    Both are a 403 to ``routes.projects._require_project_owner``. Only the
    first revokes a *pinned* project — see
    :func:`belongs_to_another_org`, which is
    where the difference is argued.

    The rule itself is the one E2.3 settled on, and it lives here rather
    than in ``routes.projects`` so that the pin check and the route gate
    cannot answer the same question differently. They already did:
    ``session["project_id"]`` was checked once, when it was picked, and
    trusted on every request after that, while the gate re-checked every
    time — measured, see ``tests/test_project_pin_revocation.py``.

    Two eras, decided per project rather than per instance:

    * **Organisation** — ``ORG_MODE`` is on and the project has an
      ``org_id``: membership in that organisation decides.
    * **Legacy** — everything else: ``owner_sid`` is the only claim
      anyone has to the project, and a NULL one means there is nobody to
      compare against, so it is allowed and logged.
    """
    from engine import db as _db

    if not is_valid_project_id(project_id):
        return "malformed", None

    sess = session_obj if session_obj is not None else session
    meta = _db.get_project(project_id)
    if not meta:
        return "missing", None

    from engine import permissions as _perm

    project_org = meta.get("org_id")
    if _perm.org_active() and project_org:
        user_id = _perm.current_user_id()
        role = _db.get_org_role(project_org, user_id) if user_id else None
        if role is None:
            log.warning(
                "project access denied pid=%s org=%s user=%s — not a member",
                project_id[:8], project_org[:8], (user_id or "-")[:8])
            return "forbidden_org", None
        return "ok", meta

    owner = meta.get("owner_sid")
    if owner is None:
        log.info("project pid=%s has NULL owner_sid — allowing (legacy)",
                 project_id[:8])
        return "ok", meta
    sid = get_session_id(sess)
    if owner != sid:
        log.warning("project access denied pid=%s owner=%s sid=%s",
                    project_id[:8], (owner or "")[:8], (sid or "")[:8])
        return "forbidden_owner", None
    return "ok", meta


def belongs_to_another_org(project_id, session_obj=None) -> bool:
    """The project is an organisation's, and the caller is not in it.

    ``"forbidden_org"`` **only**, and the narrowness is the design rather
    than a shortcut. The other refusal ``project_access_with_meta`` can
    return — a legacy project whose ``owner_sid`` is another browser's —
    would break a feature if it counted here: under ``WORKSPACE_DB_FIRST`` a
    project is the team's shared work, so a colleague's browser legitimately
    holds a pin on a project somebody else's browser created.
    ``tests/test_generation_db_first.py::TestSharedProject`` says so out
    loud, and it is what caught this when the first version of this function
    refused on any verdict but ``"ok"``. It also means this predicate is a
    no-op while ``ORG_MODE`` is off, which is what makes it safe to add to
    an existing route.

    ``"missing"`` does not count either: an id naming a deleted project
    keeps resolving, because callers downstream already answer "no such
    item" for it, and that is not a security question.

    Two callers, both re-checks of something approved once already: the
    pinned ``session["project_id"]`` in the resolvers above, and a run id in
    ``routes/automation.py`` that names the project it belongs to. So a
    database that cannot answer fails **open** rather than taking the
    product away from everybody who is entitled to it.
    """
    try:
        return project_access_with_meta(project_id,
                                        session_obj)[0] == "forbidden_org"
    except Exception as exc:      # pragma: no cover — defensive
        log.debug("project ownership re-check unavailable (%s) — allowing",
                  exc)
        return False


def visible_projects(session_obj=None) -> list:
    """The projects the caller may see, scoped the same way access is.

    Two eras, matching ``routes.projects._require_project_owner``:

    * **Organisation** — ``ORG_MODE`` is on and the caller is in a team:
      the team's projects. Scoping this by browser cookie instead was a
      real bug and not a subtle one — a signed-in admin saw an empty
      dashboard, because a team's projects belong to the organisation and
      have no ``owner_sid`` at all. The access gate already read
      membership; only the *list* was still asking the cookie.
    * **Legacy** — everything else: the anonymous session's own projects,
      exactly as before.

    Best-effort: a DB outage returns an empty list rather than raising, so
    the picker still renders and the user has a way forward.
    """
    sess = session_obj if session_obj is not None else session
    from engine import db as _db

    try:
        from engine import permissions as _perm
        if _perm.org_active():
            org_id = _perm.current_org_id()
            if not org_id:
                # Signed in but in no team yet. Showing another era's
                # projects here would be worse than showing none.
                return []
            return _db.list_projects_for_org(org_id) or []
    except Exception as exc:
        log.debug("visible_projects: org scoping unavailable (%s) — "
                  "falling back to session scope", exc)

    sid = get_session_id(sess)
    return _db.list_projects(owner_sid=sid) or []


# ── Download filenames ───────────────────────────────────────────

#: What may appear in the ASCII ``filename`` parameter unquoted-safely.
_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

#: Cap on either filename form. Long enough for a real project name, short
#: enough that the header stays a header.
MAX_ATTACHMENT_STEM = 120


def attachment_header(stem: str, suffix: str, *,
                      fallback: str = "export") -> str:
    """A ``Content-Disposition`` value for a download named after a project.

    Six export routes built this header by interpolating the project name
    straight into ``f"attachment; filename={name}{suffix}"``, with at most a
    ``.replace(" ", "_")`` in front of it. A project name is free text from a
    form, and three things followed — each measured, not reasoned about:

    * a name containing a newline made **every** export answer 500.
      Werkzeug refuses a header value with a newline in it, which is the
      good news: there was no response splitting. There was also no export;
    * a name containing ``;`` or ``"`` put a second parameter into the
      header. ``Acme; filename=evil.sh`` produced ``filename=testfortge_Acme;
      filename=evil.sh.md``, and which of the two a browser saves under is
      the browser's business, not ours;
    * a Cyrillic name — the ordinary case in this product's other language —
      produced a value that is not latin-1 encodable, which PEP 3333 says a
      WSGI header value must be. The Flask test client hands the string back
      unencoded, so the suite saw 200 and the wire would not.

    ``routes/projects.py``'s own export got this right on its own with a
    regex and a quoted filename; that it was right in one place out of seven
    is what pointed here. This is that rule, in one place, plus RFC 6266's
    ``filename*`` so a Ukrainian project name reaches the person downloading
    it instead of becoming a row of dashes.
    """
    from urllib.parse import quote

    raw = re.sub(r"\s+", "_",
                 str(stem or "").strip())[:MAX_ATTACHMENT_STEM]
    ascii_stem = _FILENAME_UNSAFE.sub("-", raw)
    # Collapse the runs the substitution leaves behind. A wholly non-ASCII
    # name would otherwise fall back to a row of dashes, which is what the
    # one call site that sanitised at all already produced — readable is
    # better, and ``filename*`` below carries the real name regardless.
    ascii_stem = re.sub(r"-{2,}", "-", ascii_stem).strip("-.") or fallback
    header = f'attachment; filename="{ascii_stem}{suffix}"'
    if raw and raw != ascii_stem:
        # Percent-encoded, so a newline or a quote in the name cannot reach
        # the header at all — ``quote`` with no safe characters is the whole
        # defence, and it is why this branch needs no separate escaping.
        header += ("; filename*=UTF-8''"
                   + quote(f"{raw}{suffix}", safe=""))
    return header


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
        projects = visible_projects(sess)
    except Exception as exc:
        log.debug("get_picker_context list_projects failed: %s", exc)
        projects = []
    return {
        "projects": projects or [],
        "active_project_id": sess.get("project_id") or "",
    }


# ── Metric snapshot helpers (Sprint 3 task 3.3) ──────────────────


def kpi_value(metrics: dict | None, key: str, default: float = 0.0) -> float:
    """Read a numeric KPI out of a stored ``DashboardMetricSnapshot``.

    The dashboard metrics dict produced by
    ``engine.test_metrics_generator.compute_session_metrics`` keeps
    KPIs at the top level (``exec_pass_rate``, ``tc_total``,
    ``bug_total``, ``exec_total``, ...) rather than nested in a list
    of ``{name, value, formula}`` records — the ``KPI`` dataclass-
    flavoured list lives on the *other* metrics object, the one built for
    the retired ``/test-metrics`` page. So ``kpi_value`` is intentionally a
    thin
    dict-getter with a defensive numeric coerce, not a list scanner.
    """
    if not isinstance(metrics, dict):
        return default
    val = metrics.get(key, default)
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def kpi_defect_density(metrics: dict | None) -> float:
    """Defect density = total bugs / max(total test cases, 1).

    Matches the QA-standard formula but guards against an empty TC
    pack producing a divide-by-zero spike when the first bug arrives.
    Returns a float in [0, +inf); the trend chart multiplies by 100
    for display.
    """
    if not isinstance(metrics, dict):
        return 0.0
    bugs = kpi_value(metrics, "bug_total")
    tcs = kpi_value(metrics, "tc_total")
    denom = tcs if tcs >= 1 else 1.0
    return round(bugs / denom, 4)


__all__ = [
    # constants
    "SAFE_FOLDER_RE", "SAFE_ASSET_RE", "URL_PATTERN", "GENERATED_KEYS",
    "resolve_active_project",
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
    "get_picker_context", "visible_projects",
    # The active project's pack — the one way route modules read it.
    "pack_cleared", "pack_test_cases", "pack_checklist", "pack_bugs",
    "pack_runs", "pack_estimation", "mirror_pack", "pack_version",
    "ensure_active_project", "AUTOCREATED_KEY",
    # dashboard metric helpers
    "kpi_value", "kpi_defect_density",
]
