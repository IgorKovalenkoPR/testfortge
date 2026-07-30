"""TestFortge — Test-case / checklist generation + export routes.

  * GET/POST /test-cases   — generate + display test cases
  * GET/POST /checklist    — generate + display checklist
  * GET /export/<fmt>      — markdown/html/csv/xlsx export
"""

from __future__ import annotations

import os
import tempfile

from flask import (Flask, Response, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.utils import secure_filename

from engine.file_parser import split_into_requirements
from engine.qa_persona import is_instruction
from engine.user_story_generator import generate_user_stories
from engine.testcase_generator import (
    generate_test_cases, generate_checklist, generate_traceability,
    generate_from_strategy,
)
from engine.site_recon import recon_site
from engine.test_strategy import build_strategy
import re as _re
from engine.exporter import (
    export_markdown, export_html,
    export_csv_testcases, export_csv_checklist,
    export_xlsx_testcases, export_xlsx_checklist,
)
from engine.imports import parse_test_cases as import_parse_test_cases
from engine.imports import parse_checklist as import_parse_checklist
from engine.job_queue import get_queue, DONE, FAILED

from engine import db as _db
from engine.log import get_logger

from ._shared import (
    reconstruct_stories, reconstruct_test_cases, reconstruct_checklist,
    tc_to_dict, cl_to_dict, story_to_dict, get_session_id,
    parse_page_input, extract_resource_urls, ensure_active_project,
)

# Hard cap on concurrent generation jobs per session — same threshold
# as automation/estimation. Prevents a runaway tab from monopolising
# the worker pool on Render free tier.
MAX_CONCURRENT_GEN_JOBS = 2

_log = get_logger(__name__)


def _recorder_enabled() -> bool:
    """Match the same env-var gate the recorder CLI + MCP tool use."""
    return os.environ.get("RECORDER_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on")


def _browser_control_enabled() -> bool:
    """PR-F Phase 2 — separate gate for the active-driver channel. Off by
    default; the poll/result endpoints 403 until an operator opts in with
    BROWSER_CONTROL_ENABLED=1, so enabling the recorder doesn't silently
    expose a remote-drive surface."""
    return os.environ.get("BROWSER_CONTROL_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on")


# ── PR-E browser-extension helpers ─────────────────────────────
#
# In-memory ``token → {project_id, created_at}`` mapping for active
# extension recordings. Lives only in this worker process; a restart
# loses every in-flight session, and the extension surfaces that as a
# "session expired — restart from TestForTge" toast. Persisting to DB
# would require a second migration and bring no real recovery benefit
# (recording was abandoned anyway), so we deliberately keep it RAM-only.
_RECORDER_SESSIONS: dict[str, dict] = {}

import time as _time


def _purge_oldest_recorder_session() -> None:
    """Drop the single oldest entry — bounded LRU. Called only when
    the dict exceeds the soft cap (1000 entries), so a misbehaving
    integration can't accumulate sessions unboundedly."""
    if not _RECORDER_SESSIONS:
        return
    oldest_token = min(
        _RECORDER_SESSIONS,
        key=lambda t: _RECORDER_SESSIONS[t].get("created_at", 0),
    )
    _RECORDER_SESSIONS.pop(oldest_token, None)


# PR-F — server-side sanitiser for the extension's deep-capture blob.
# Never trust the extension's own caps: a buggy or hostile client could
# POST an unbounded telemetry object. We re-cap every count + string
# length here so one recording can't bloat the SessionDraft row or the
# review page, and coerce types defensively against schema drift.
_TELE_NET_CAP = 500
_TELE_CONSOLE_CAP = 500
_TELE_SNAPSHOT_CAP = 25
_TELE_STR_CAP = 2000


def _sanitise_recorder_telemetry(raw) -> dict | None:
    """Coerce + cap the ``telemetry`` field from /finish. Returns a
    normalised dict, or ``None`` when there's nothing worth storing."""
    if not isinstance(raw, dict):
        return None

    def _s(v, cap=_TELE_STR_CAP):
        return str(v if v is not None else "")[:cap]

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    def _clip(seq, cap):
        return seq[-cap:] if isinstance(seq, list) else []

    net = []
    for item in _clip(raw.get("network"), _TELE_NET_CAP):
        if not isinstance(item, dict):
            continue
        ok = item.get("ok")
        net.append({
            "method": _s(item.get("method"), 12),
            "url": _s(item.get("url"), 500),
            "type": _s(item.get("type"), 40),
            "status": _int(item.get("status")),
            "ok": (bool(ok) if ok is not None else None),
            "mime": _s(item.get("mime"), 80),
            "error": _s(item.get("error"), 200),
            "redirects": _int(item.get("redirects")),
        })

    con = []
    for item in _clip(raw.get("console"), _TELE_CONSOLE_CAP):
        if not isinstance(item, dict):
            continue
        con.append({
            "level": _s(item.get("level"), 16),
            "text": _s(item.get("text")),
            "source": _s(item.get("source"), 24),
            "url": _s(item.get("url"), 500),
        })

    snaps = []
    for item in _clip(raw.get("dom_snapshots"), _TELE_SNAPSHOT_CAP):
        if not isinstance(item, dict):
            continue
        inter_raw = item.get("interactive")
        inter = []
        for e in (inter_raw[:80] if isinstance(inter_raw, list) else []):
            if not isinstance(e, dict):
                continue
            inter.append({
                "tag": _s(e.get("tag"), 20),
                "role": _s(e.get("role"), 40),
                "name": _s(e.get("name"), 120),
                "text": _s(e.get("text"), 120),
                "locator": _s(e.get("locator"), 300),
                "label": _s(e.get("label"), 200),
            })
        snaps.append({
            "url": _s(item.get("url"), 500),
            "title": _s(item.get("title"), 200),
            "text_digest": _s(item.get("text_digest")),
            "interactive": inter,
            "element_count": len(inter),
        })

    meta_raw = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    meta = {
        "debugger_ok": bool(meta_raw.get("debugger_ok")),
        "debugger_error": _s(meta_raw.get("debugger_error"), 200),
    }

    # Nothing captured AND no debugger-status to report → don't store a
    # row-bloating empty blob. But if the debugger failed to attach we
    # DO keep the meta so the review page can explain the thin panel.
    if not (net or con or snaps or meta["debugger_error"]):
        return None

    con_errors = sum(1 for c in con if c["level"] in ("error", "assert"))
    net_fails = sum(1 for n in net
                     if n["ok"] is False or (n["status"] and n["status"] >= 400))
    return {
        "network": net,
        "console": con,
        "dom_snapshots": snaps,
        "meta": meta,
        "counts": {
            "network": len(net),
            "console": len(con),
            "console_errors": con_errors,
            "network_failures": net_fails,
            "dom_snapshots": len(snaps),
        },
    }


def _recorder_cors_headers() -> dict:
    """CORS headers for the recorder API endpoints.

    The extension's content-script runs in the SUT's origin (whatever
    site the operator is recording against). Since we can't pre-list
    every SUT, we accept ``*`` — the endpoints carry their own auth
    (the per-session token from /start) so a public origin can still
    only act on a project it was authorised against.
    """
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "600",
    }


def _recorder_cors_preflight():
    """Empty 204 response for the CORS preflight OPTIONS request."""
    from flask import make_response
    resp = make_response("", 204)
    for k, v in _recorder_cors_headers().items():
        resp.headers[k] = v
    return resp


def _json_with_cors(body: dict, status: int = 200):
    """``jsonify`` + recorder CORS headers in one call."""
    resp = jsonify(body)
    resp.status_code = status
    for k, v in _recorder_cors_headers().items():
        resp.headers[k] = v
    return resp


# ── PR-D session-review helpers ────────────────────────────────


def _parse_review_form(form, proposed_count: int) -> list[dict]:
    """Translate the review template's <form> fields into the same
    shape the JSON body uses, so the POST handler stays one code-path.

    Per row the form ships ``save_<i>=on``, ``suite_<i>=Smoke|...``,
    and ``summary_<i>=...``. Unchecked rows just omit ``save_<i>``.
    """
    out: list[dict] = []
    for i in range(proposed_count):
        if not form.get(f"save_{i}"):
            continue
        out.append({
            "idx":              i,
            "suite":            form.get(f"suite_{i}") or "",
            "summary_override": form.get(f"summary_{i}") or "",
        })
    return out


def _next_section_num(project_id: str) -> int:
    """Find the next free integer section_num for a new TC. Recorded
    flows land in a synthetic ``Section N: Recorded session`` group
    so the operator sees them clustered in /test-cases."""
    tcs = _db.load_test_cases(project_id)
    used = []
    for t in tcs:
        try:
            used.append(int(t.get("section_num") or 0))
        except (TypeError, ValueError):
            continue
    return (max(used) + 1) if used else 1


def _mint_external_id(project_id: str) -> str:
    """Mint a fresh external_id for a recorded TC. Format
    ``REC_<n>`` where n is one past the highest existing REC_ id —
    keeps recorded TCs visually distinct from generated TCs (TC-001,
    SC1_002, ...) without colliding with them."""
    tcs = _db.load_test_cases(project_id)
    highest = 0
    for t in tcs:
        ext = str(t.get("id") or "")
        if ext.startswith("REC_"):
            try:
                highest = max(highest, int(ext[4:]))
            except (TypeError, ValueError):
                continue
    return f"REC_{highest + 1:03d}"


def _human_steps_preview(steps: list[dict]) -> str:
    """Render the recorded steps as numbered text so the legacy
    ``test_steps`` field reads naturally. The runner prefers the
    JSON column anyway; this is purely for the editor view."""
    lines: list[str] = []
    for i, s in enumerate(steps or [], start=1):
        action = (s.get("action") or "").lower()
        target = (s.get("target") or "")[:80]
        value = (s.get("value") or "")[:40]
        kind = (s.get("kind") or "").lower()
        atype = (s.get("assertion_type") or "").lower()
        if kind == "assertion":
            verb = {
                "visible": "Assert visible",
                "text":    "Assert text",
                "url":     "Assert URL",
            }.get(atype, "Assert")
            tail = (
                f" {target!r}" if target else "" if atype != "text"
                else (f" {value!r}" if value else "")
            )
            lines.append(f"{i}. {verb}{tail}")
            continue
        verb_map = {
            "goto":   "Navigate to",
            "click":  "Click",
            "fill":   "Fill",
            "select": "Select",
            "check":  "Check",
            "press":  "Press key",
        }
        verb = verb_map.get(action, action.title() or "Step")
        bits = [verb]
        if target:
            bits.append(target)
        if value and action in ("fill", "select", "press"):
            bits.append(f"= {value!r}")
        lines.append(f"{i}. " + " ".join(bits))
    return "\n".join(lines)


# Stage 2 — site-aware path. Used when the input contains a URL: we
# run crawl → recon → strategy → generate_from_strategy and persist
# both Test Cases and Checklist for the active project. The caller
# decides which surface to render — both buckets are saved either way
# so the sibling page picks up the work without a second click.
_URL_DETECT = _re.compile(r"(https?://[^\s,]+)", _re.IGNORECASE)


def _detect_first_url(raw_lines: list[str]) -> str | None:
    for line in raw_lines or []:
        m = _URL_DETECT.search(line or "")
        if m:
            return m.group(1).rstrip(".,;)")
    return None


def _build_artifacts(url: str, custom_prompt: str,
                     raw_lines: list[str] | None,
                     site_analysis) -> "object":
    """Assemble the grounding bundle for the Test Case Author agent.

    Everything the operator supplied plus everything the crawler saw:
    the prompt steers scope, the requirement lines and attachment text
    describe intent, and the per-page control inventory supplies the
    exact UI labels the authored steps must quote. Without the inventory
    the agent can only write cases against named requirements, which is
    what produced generic steps before.
    """
    from engine.tc_author import Artifacts

    pages: list[dict] = []
    for p in getattr(site_analysis, "pages", None) or []:
        if getattr(p, "error", None):
            continue
        pages.append({
            "url": p.url,
            "title": (p.title or "")[:160],
            "h1": (p.h1 or "")[:160],
            "headings": [h[:90] for h in (p.headings or [])[:10]],
            "nav_links": [n[:70] for n in (p.nav_links or [])[:14]],
            "buttons": [b[:70] for b in (p.buttons or [])[:18]],
            "forms": p.forms or [],
        })

    requirements = [ln.strip() for ln in (raw_lines or []) if (ln or "").strip()]

    return Artifacts(
        url=url,
        custom_prompt=custom_prompt or "",
        requirements=requirements[:120],
        pages=pages,
    )


def _run_site_aware(url: str, pid: str | None,
                    custom_prompt: str,
                    raw_lines: list[str] | None = None) -> dict | None:
    """crawl_site → recon_site → build_strategy → generate_from_strategy.

    Returns ``None`` when the crawl itself failed (SSRF block, network
    timeout, etc) — in that case the caller renders the legacy result
    untouched. Otherwise returns:

        tc_dicts:     site-aware TestCase dicts (``SA1_NNN`` IDs)
        cl_dicts:     site-aware ChecklistItem dicts (``SA_FUNC_NNN``)
        profile:      SiteProfile.to_dict()
        strategy:     TestStrategy.to_dict()
        crawl_errors: list[str] passed through from the crawler

    Important: this function does NOT write TC/CL to the DB. The
    caller concatenates these with the legacy stream and writes
    everything once, so we don't get two ``save_test_cases`` calls
    each wiping the other's rows (``save_test_cases`` is replace-all).
    Only the ``site_profile`` row is persisted here — it has no
    overlap with the legacy stream.
    """
    from engine.site_crawler import crawl_site
    try:
        site_analysis = crawl_site(url)
    except Exception as exc:
        _log.warning("site-aware crawl failed: %s", exc)
        return None
    if site_analysis is None:
        return None
    profile = recon_site(site_analysis)
    strategy = build_strategy(profile)
    artifacts = _build_artifacts(url, custom_prompt, raw_lines, site_analysis)
    tcs, cls = generate_from_strategy(profile, strategy, artifacts=artifacts)

    tc_dicts = [tc_to_dict(tc) for tc in tcs]
    cl_dicts = [cl_to_dict(cl) for cl in cls]

    if pid:
        try:
            _db.save_site_profile(pid, url, profile.to_dict(),
                                  strategy.to_dict())
        except Exception as exc:  # pragma: no cover — best-effort
            _log.warning("site-aware: save_site_profile failed: %s", exc)

    return {
        "tc_dicts": tc_dicts,
        "cl_dicts": cl_dicts,
        "profile": profile.to_dict(),
        "strategy": strategy.to_dict(),
        "crawl_errors": list(getattr(site_analysis, "crawl_errors", []) or []),
    }


def _run_authored_without_url(custom_prompt: str,
                              raw_lines: list[str] | None) -> list[dict]:
    """Author test cases when the input has no URL to crawl.

    Prompt-only and attachment-only input would otherwise never reach the
    Test Case Author agent — the authored stream hangs off the site-aware
    branch, which only runs on a detected URL. ``raw_lines`` already
    carries the parsed text of every uploaded attachment (see
    ``parse_page_input``), so the agent has the requirements to work
    from; what it lacks is a control inventory, which it reports as a gap
    rather than inventing.

    Returns TC dicts to append to the legacy stream, or ``[]`` when the
    LLM is unreachable (the legacy knowledge-base path owns baseline
    coverage either way).
    """
    if not (raw_lines or custom_prompt):
        return []
    try:
        from engine.tc_author import Artifacts
        from engine.testcase_generator import generate_from_artifacts
        artifacts = Artifacts(
            custom_prompt=custom_prompt or "",
            requirements=[ln.strip() for ln in (raw_lines or [])
                          if (ln or "").strip()][:120],
        )
        tcs = generate_from_artifacts(artifacts)
    except Exception as exc:  # pragma: no cover — best-effort
        _log.warning("authoring without URL failed: %s", exc)
        return []
    return [tc_to_dict(tc) for tc in tcs]


def _persist_test_cases(tc_dicts: list[dict]) -> None:
    """Mirror the in-session TC list into Postgres for the active project.

    Best-effort: a DB outage must not block the user from seeing their
    generated cases on screen. Errors are logged and swallowed."""
    pid = ensure_active_project()
    if not pid:
        return
    try:
        _db.save_test_cases(pid, tc_dicts)
    except Exception as exc:  # pragma: no cover — best-effort write
        _log.warning("persist test cases failed: %s", exc)


def _persist_checklist(cl_dicts: list[dict]) -> None:
    """Same contract as :func:`_persist_test_cases` but for Checklist."""
    pid = ensure_active_project()
    if not pid:
        return
    try:
        _db.save_checklist(pid, cl_dicts)
    except Exception as exc:  # pragma: no cover
        _log.warning("persist checklist failed: %s", exc)



def _back_to_caller(default: str = "test_cases_page", extra_qs: str = "") -> str:
    """Resolve the URL to redirect to after a form submission so the
    user stays on the page they came from.

    Looks at the Referer header first; if it points at a page hosted
    by us (and the endpoint is one of the accepted return targets), we
    use it. Otherwise we fall back to ``default`` so a missing or
    spoofed Referer can't bounce the user to an external site.

    ``extra_qs`` is appended verbatim to the resolved URL — callers
    use this to pass ``auto_run=1`` so the destination page knows to
    auto-click the Run button after the upload landed.
    """
    from urllib.parse import urlparse
    target = url_for(default)
    referrer = (request.referrer or "").strip()
    if referrer:
        try:
            host = urlparse(referrer).path or ""
            for ep, prefix in (
                ("test_execution_page", "/test-execution"),
                ("test_cases_page",      "/test-cases"),
                ("checklist_page",       "/checklist"),
            ):
                if host == prefix or host.startswith(prefix + "/") or host == prefix + "/":
                    target = url_for(ep)
                    break
        except Exception:
            pass
    if extra_qs:
        target = target + ("&" if "?" in target else "?") + extra_qs
    return target


def _hydrate_from_db(kind: str) -> list[dict]:
    """Reload the active project's pack from Postgres into the session.

    Why this exists: on the free Render plan the service sleeps after
    ~15 min and ``SESSION_TYPE=filesystem`` sits on an ephemeral disk, so
    every cold start wipes the session store. ``before_request`` also
    deliberately clears ``GENERATED_KEYS`` whenever the session predates
    the current boot. Either way the /test-cases and /checklist pages
    rendered their empty state and the operator saw a morning's work
    apparently vanish — even though the pack was safely in Postgres the
    whole time, written by ``_persist_test_cases`` on every generate.

    /test-execution already restored from the DB like this; the two
    generation pages did not. This closes that gap without a paid plan,
    a second connection pool, or a new dependency.

    ``kind`` is "tc" or "cl". Returns the rows loaded (empty on any
    failure — a DB hiccup must not break the page render).
    """
    pid = session.get("project_id")
    if not pid:
        return []
    loader_name = "load_test_cases" if kind == "tc" else "load_checklist"
    session_key = "test_cases_data" if kind == "tc" else "checklist_data"
    loader = getattr(_db, loader_name, None)
    if loader is None:  # pragma: no cover — defensive
        return []
    try:
        rows = loader(pid) or []
    except Exception as exc:  # pragma: no cover — best-effort read
        _log.warning("hydrate %s from DB failed: %s", kind, exc)
        return []
    if rows:
        session[session_key] = rows
        _log.info("hydrated %d %s rows from DB for project %s",
                  len(rows), kind, pid)
    return rows


def _drain_tc_job_into_session() -> None:
    """If a previous POST left a tc_gen job_id in the session and that
    job is now finished, copy the result into the session keys the
    GET render reads. No-op when no job_id is stored or the job is
    still pending. Best-effort: never raises.
    """
    job_id = session.get("tc_gen_job_id")
    if not job_id:
        return
    try:
        job = get_queue().get(job_id)
    except Exception:
        return
    if not job or job.kind != "tc_gen":
        return
    if job.status == DONE and job.result:
        r = job.result
        session["test_cases_data"]   = r.get("tc_dicts", [])
        session["user_stories"]      = r.get("stories", [])
        session["raw_requirements"]  = r.get("raw_requirements", [])
        session["traceability_data"] = r.get("trace", [])
        session.pop("tc_gen_job_id", None)
    elif job.status == FAILED:
        # Surface the worker error once and stop polling for this id.
        from flask import flash as _flash, g as _g
        _flash(
            (_g.t.get("mvp_gen_failed", "Generation failed") if hasattr(_g, "t")
             else "Generation failed")
            + ": " + (job.error or "unknown"),
            "error",
        )
        session.pop("tc_gen_job_id", None)


def _drain_cl_job_into_session() -> None:
    """Same as :func:`_drain_tc_job_into_session` but for the
    checklist queue. Handles the matching cl_gen_job_id key."""
    job_id = session.get("cl_gen_job_id")
    if not job_id:
        return
    try:
        job = get_queue().get(job_id)
    except Exception:
        return
    if not job or job.kind != "cl_gen":
        return
    if job.status == DONE and job.result:
        r = job.result
        session["checklist_data"]    = r.get("cl_dicts", [])
        session["user_stories"]      = r.get("stories", [])
        session["raw_requirements"]  = r.get("raw_requirements", [])
        session.pop("cl_gen_job_id", None)
    elif job.status == FAILED:
        from flask import flash as _flash, g as _g
        _flash(
            (_g.t.get("mvp_gen_failed", "Generation failed") if hasattr(_g, "t")
             else "Generation failed")
            + ": " + (job.error or "unknown"),
            "error",
        )
        session.pop("cl_gen_job_id", None)


def register(app: Flask) -> None:
    # Hard-coded blocking budget for the legacy sync POST. Render's
    # gunicorn timeout is 300 s; anything we let block beyond ~250 s
    # risks a worker kill and a 502 on the user's tab. The async
    # /test-cases/run-async pair has no such restriction — it submits
    # to JobQueue and returns immediately.
    SYNC_GEN_BUDGET_S = 90

    @app.route("/test-cases", methods=["GET", "POST"])
    def test_cases_page():
        if request.method == "POST":
            # The sync POST is now a thin shim around the JobQueue so a
            # slow LLM never holds the single gunicorn worker hostage
            # past the 300 s ceiling — that's what produced the 502 the
            # operator reported. We submit the same job a JS client
            # would, then block for up to SYNC_GEN_BUDGET_S seconds. If
            # it finishes, render the result; if not, redirect to GET
            # with a flash and let the user refresh once the background
            # job is done.
            raw_lines, errors, custom_prompt = parse_page_input()

            if not raw_lines:
                flash(g.t.get("mvp_no_input",
                              "Please enter requirements or upload files."), "error")
                return render_template("test_cases.html",
                                       test_cases=[], traceability=[],
                                       has_data=False, errors=errors)

            sync_pid = ensure_active_project()
            sync_raw_lines = raw_lines
            sync_custom_prompt = custom_prompt

            def _sync_worker(raw_lines=sync_raw_lines,
                             custom_prompt=sync_custom_prompt,
                             pid=sync_pid):
                # Legacy path always runs — it owns baseline coverage
                # (50+ ISTQB-knowledge test cases per typical site)
                # and the user-stories / traceability surfaces. Stage-2
                # site-aware then appends focused, site-specific TCs
                # with SA-prefixed IDs so the two streams concatenate
                # cleanly. ``_run_site_aware`` writes only the
                # ``site_profile`` row to DB (legacy owns the TC table).
                parsed = split_into_requirements(raw_lines)
                parsed = [r for r in parsed if not is_instruction(r.text)]
                raw_for_persona = (
                    [{"id": r.id, "text": r.text} for r in parsed]
                    if parsed else
                    [{"id": f"RAW-{i+1}", "text": line}
                     for i, line in enumerate(raw_lines) if line.strip()]
                )
                stories = (generate_user_stories(parsed, custom_prompt)
                           if parsed else [])
                crawl_errors: list[str] = []
                tcl = generate_test_cases(stories, custom_prompt,
                                          raw_requirements=raw_for_persona,
                                          crawl_errors_out=crawl_errors)
                trc = generate_traceability(stories, tcl) if tcl else []
                tcd = [tc_to_dict(tc) for tc in tcl]

                # Stage 2: when the input has a URL, append focused
                # site-aware TCs to the legacy stream. Failure is
                # non-fatal — the user still sees the legacy pack.
                site_aware_meta: dict = {}
                url = _detect_first_url(raw_lines)
                if url:
                    site_out = _run_site_aware(url, pid, custom_prompt,
                                               raw_lines=raw_lines)
                    if site_out:
                        tcd.extend(site_out.get("tc_dicts") or [])
                        # crawl_errors from the recon crawler land in
                        # the same warning stream — operator sees one
                        # banner, regardless of which crawler call
                        # noticed the partial failure.
                        for e in site_out.get("crawl_errors") or []:
                            if e and e not in crawl_errors:
                                crawl_errors.append(e)
                        site_aware_meta = {
                            "profile":         site_out.get("profile") or {},
                            "strategy_source": (site_out.get("strategy") or {})
                                                .get("source", ""),
                        }
                else:
                    # No URL to crawl — still author from the prompt and
                    # the attachment text, which raw_lines already
                    # carries. Without this, prompt-only and
                    # attachment-only input never reaches the author
                    # agent and falls back to canned templates.
                    tcd.extend(_run_authored_without_url(custom_prompt,
                                                         raw_lines))

                if pid and tcd:
                    try: _db.save_test_cases(pid, tcd)
                    except Exception: pass

                return {"tc_dicts": tcd,
                        "stories": [story_to_dict(s) for s in stories],
                        "raw_requirements": raw_for_persona,
                        "trace": trc,
                        "crawl_errors": crawl_errors,
                        **site_aware_meta}

            sid = get_session_id(session)
            # Per-session concurrency cap. Sprint 1 Task 5: a runaway tab
            # can otherwise submit /test-cases over and over, fill the
            # 2-worker thread pool with tc_gen jobs, and starve every
            # other route for the duration. The async sibling
            # /test-cases/run-async already enforces a sibling cap; this
            # is the matching gate on the sync POST so neither path is
            # a loophole.
            from flask import current_app as _ca
            _cap = int(_ca.config.get("MAX_CONCURRENT_RUNS", 3) or 3)
            _active = get_queue().count_active_by_meta(
                "tc_gen", "session_id", sid)
            if _active >= _cap:
                flash(
                    f"You already have {_active} generation job(s) in "
                    f"flight (cap is {_cap}). Please wait for them to "
                    f"finish before starting another.",
                    "warning",
                )
                return redirect(url_for("test_cases_page"))

            job_id = get_queue().submit("tc_gen", _sync_worker,
                                        meta={"session_id": sid})
            session["tc_gen_job_id"] = job_id

            import time as _time
            deadline = _time.time() + SYNC_GEN_BUDGET_S
            job = None
            while _time.time() < deadline:
                job = get_queue().get(job_id)
                if job is None or job.status in (DONE, FAILED):
                    break
                _time.sleep(0.5)

            if job is None or job.status != DONE:
                # Job is still running in the background — don't block
                # the worker any longer. Holding screen + flash; the
                # GET path will show results once the background job
                # writes them into the session.
                flash(
                    g.t.get(
                        "mvp_gen_in_background",
                        "Generation is still running in the background. "
                        "Please wait a few seconds and refresh this page."
                    ),
                    "info",
                )
                return redirect(url_for("test_cases_page"))

            r = job.result or {}
            session["test_cases_data"] = r.get("tc_dicts", [])
            session["user_stories"] = r.get("stories", [])
            session["raw_requirements"] = r.get("raw_requirements", [])
            session["traceability_data"] = r.get("trace", [])

            # Surface partial crawler failures so the user knows why some
            # URL-derived test cases might be missing. Generation already
            # fell back to generic ISTQB knowledge for the failed pages.
            _crawl_errors = r.get("crawl_errors") or []
            if _crawl_errors:
                flash(
                    g.t.get(
                        "crawl_partial",
                        "Some pages could not be crawled — generation "
                        "continued on available data: "
                    ) + "; ".join(_crawl_errors[:3]),
                    "warning",
                )

            tc_list = reconstruct_test_cases(session["test_cases_data"])
            trace = session["traceability_data"]
            if not tc_list:
                flash(g.t.get(
                    "mvp_no_quality_requirements",
                    "Could not detect any testable requirements in the provided input."),
                    "error")
                return render_template("test_cases.html",
                                       test_cases=[], traceability=[],
                                       has_data=False, errors=errors)
            return render_template("test_cases.html",
                                   test_cases=tc_list, traceability=trace,
                                   has_data=True, errors=errors,
                                   resource_urls=extract_resource_urls())

            # ── Legacy code path retained below for reference; the
            #    block above replaces it. Falls through harmlessly. ──
            parsed_reqs = split_into_requirements(raw_lines)

            # Filter out instruction lines — "Create test cases...",
            # "Pay attention..." are commands TO the tool, not requirements.
            parsed_reqs = [r for r in parsed_reqs if not is_instruction(r.text)]

            # Even if split_into_requirements finds nothing (e.g. URL-only
            # input), the QA persona can still analyze the raw text.
            raw_reqs_for_persona = ([{"id": r.id, "text": r.text} for r in parsed_reqs]
                                    if parsed_reqs
                                    else [{"id": f"RAW-{i+1}", "text": line}
                                          for i, line in enumerate(raw_lines) if line.strip()])

            if parsed_reqs:
                new_stories = generate_user_stories(parsed_reqs, custom_prompt)
                session["user_stories"] = [story_to_dict(s) for s in new_stories]
                session["raw_requirements"] = [{"id": r.id, "text": r.text} for r in parsed_reqs]
            else:
                new_stories = []
                session["user_stories"] = []
                session["raw_requirements"] = raw_reqs_for_persona

            tc_list = generate_test_cases(new_stories, custom_prompt,
                                          raw_requirements=raw_reqs_for_persona)

            if not tc_list:
                flash(g.t.get("mvp_no_quality_requirements",
                              "Could not detect any testable requirements in the provided input."),
                      "error")
                return render_template("test_cases.html",
                                       test_cases=[], traceability=[],
                                       has_data=False, errors=errors)

            trace = generate_traceability(new_stories, tc_list)
            tc_dicts = [tc_to_dict(tc) for tc in tc_list]
            session["test_cases_data"] = tc_dicts
            session["traceability_data"] = trace
            _persist_test_cases(tc_dicts)
            return render_template("test_cases.html",
                                   test_cases=tc_list, traceability=trace,
                                   has_data=True, errors=errors,
                                   resource_urls=extract_resource_urls())

        # GET — first drain any background job whose result
        # arrived after the previous sync POST already returned. This
        # is the bug operators reported as "I added a URL + file, the
        # spinner finished, but the page is empty": the job was still
        # running when the 60 s sync budget expired, redirected with a
        # flash, and nothing else moved the result into the session.
        _drain_tc_job_into_session()
        tc_data = session.get("test_cases_data", [])
        # Nothing in the session — but the pack may still be in Postgres
        # from before the last cold start. Reload it rather than showing
        # the empty state, which reads as "my work is gone".
        restored_from_db = False
        if not tc_data:
            tc_data = _hydrate_from_db("tc")
            restored_from_db = bool(tc_data)
        trace_data = session.get("traceability_data", [])
        if tc_data:
            tc_list = reconstruct_test_cases(tc_data)
            if restored_from_db:
                # Say so explicitly: the traceability tab and user
                # stories are session-only derivations and will be empty
                # until the next generate, so silence here would look
                # like a second, different bug.
                flash(g.t.get(
                    "tc_restored_from_db",
                    "Restored %(n)d test cases saved for this project. "
                    "Traceability and user stories are rebuilt on the "
                    "next generation.") % {"n": len(tc_list)}, "info")
            return render_template("test_cases.html",
                                   test_cases=tc_list, traceability=trace_data,
                                   has_data=True, errors=[],
                                   resource_urls=extract_resource_urls())

        # Drain a one-shot prefill key set by upstream pages (e.g.
        # /estimation's "Generate test cases from this estimate" CTA).
        # Pop on read so a refresh doesn't re-prefill stale content.
        prefill = session.pop("prefill_input_text", "") or ""
        return render_template("test_cases.html",
                               test_cases=[], traceability=[],
                               has_data=False, errors=[], resource_urls=[],
                               prefill_input_text=prefill)

    @app.route("/checklist", methods=["GET", "POST"])
    def checklist_page():
        if request.method == "POST":
            # Same async-via-JobQueue shim as /test-cases above.
            raw_lines, errors, custom_prompt = parse_page_input()

            if not raw_lines:
                flash(g.t.get("mvp_no_input",
                              "Please enter requirements or upload files."), "error")
                return render_template("checklist.html",
                                       checklist=[], has_data=False, errors=errors)

            sync_pid = ensure_active_project()
            sync_raw_lines = raw_lines
            sync_custom_prompt = custom_prompt

            def _sync_worker(raw_lines=sync_raw_lines,
                             custom_prompt=sync_custom_prompt,
                             pid=sync_pid):
                # Symmetric to test_cases_page: legacy first (baseline
                # coverage from ISTQB-knowledge templates), site-aware
                # appended with SA_*-prefixed IDs. One save_checklist
                # call writes the combined set.
                parsed = split_into_requirements(raw_lines)
                parsed = [r for r in parsed if not is_instruction(r.text)]
                raw_for_persona = (
                    [{"id": r.id, "text": r.text} for r in parsed]
                    if parsed else
                    [{"id": f"RAW-{i+1}", "text": line}
                     for i, line in enumerate(raw_lines) if line.strip()]
                )
                stories = (generate_user_stories(parsed, custom_prompt)
                           if parsed else [])
                crawl_errors: list[str] = []
                cll = generate_checklist(stories, custom_prompt,
                                         raw_requirements=raw_for_persona,
                                         crawl_errors_out=crawl_errors)
                cld = [cl_to_dict(c) for c in cll]

                site_aware_meta: dict = {}
                url = _detect_first_url(raw_lines)
                if url:
                    site_out = _run_site_aware(url, pid, custom_prompt,
                                               raw_lines=raw_lines)
                    if site_out:
                        cld.extend(site_out.get("cl_dicts") or [])
                        for e in site_out.get("crawl_errors") or []:
                            if e and e not in crawl_errors:
                                crawl_errors.append(e)
                        site_aware_meta = {
                            "profile":         site_out.get("profile") or {},
                            "strategy_source": (site_out.get("strategy") or {})
                                                .get("source", ""),
                        }

                if pid and cld:
                    try: _db.save_checklist(pid, cld)
                    except Exception: pass

                return {"cl_dicts": cld,
                        "stories": [story_to_dict(s) for s in stories],
                        "raw_requirements": raw_for_persona,
                        "crawl_errors": crawl_errors,
                        **site_aware_meta}

            sid = get_session_id(session)
            # Per-session concurrency cap — same rationale as the tc_gen
            # gate above. Sprint 1 Task 5: prevent a single tab from
            # monopolising the thread pool with checklist generations.
            from flask import current_app as _ca
            _cap = int(_ca.config.get("MAX_CONCURRENT_RUNS", 3) or 3)
            _active = get_queue().count_active_by_meta(
                "cl_gen", "session_id", sid)
            if _active >= _cap:
                flash(
                    f"You already have {_active} checklist job(s) in "
                    f"flight (cap is {_cap}). Please wait for them to "
                    f"finish before starting another.",
                    "warning",
                )
                return redirect(url_for("checklist_page"))

            job_id = get_queue().submit("cl_gen", _sync_worker,
                                        meta={"session_id": sid})
            session["cl_gen_job_id"] = job_id

            import time as _time
            deadline = _time.time() + SYNC_GEN_BUDGET_S
            job = None
            while _time.time() < deadline:
                job = get_queue().get(job_id)
                if job is None or job.status in (DONE, FAILED):
                    break
                _time.sleep(0.5)

            if job is None or job.status != DONE:
                flash(
                    g.t.get(
                        "mvp_gen_in_background",
                        "Generation is still running in the background. "
                        "Please wait a few seconds and refresh this page."
                    ),
                    "info",
                )
                return redirect(url_for("checklist_page"))

            r = job.result or {}
            session["checklist_data"] = r.get("cl_dicts", [])
            session["user_stories"] = r.get("stories", [])
            session["raw_requirements"] = r.get("raw_requirements", [])

            # Surface partial crawler failures — same logic as /test-cases.
            _crawl_errors = r.get("crawl_errors") or []
            if _crawl_errors:
                flash(
                    g.t.get(
                        "crawl_partial",
                        "Some pages could not be crawled — generation "
                        "continued on available data: "
                    ) + "; ".join(_crawl_errors[:3]),
                    "warning",
                )

            cl_list = reconstruct_checklist(session["checklist_data"])
            if not cl_list:
                flash(g.t.get(
                    "mvp_no_quality_requirements",
                    "Could not detect any testable requirements in the provided input."),
                    "error")
                return render_template("checklist.html",
                                       checklist=[], has_data=False, errors=errors)
            return render_template("checklist.html", checklist=cl_list,
                                   has_data=True, errors=errors,
                                   resource_urls=extract_resource_urls())

            # Legacy code path retained below; replaced above.
            parsed_reqs = split_into_requirements(raw_lines)
            parsed_reqs = [r for r in parsed_reqs if not is_instruction(r.text)]

            raw_reqs_for_persona = ([{"id": r.id, "text": r.text} for r in parsed_reqs]
                                    if parsed_reqs
                                    else [{"id": f"RAW-{i+1}", "text": line}
                                          for i, line in enumerate(raw_lines) if line.strip()])

            if parsed_reqs:
                new_stories = generate_user_stories(parsed_reqs, custom_prompt)
                session["user_stories"] = [story_to_dict(s) for s in new_stories]
                session["raw_requirements"] = [{"id": r.id, "text": r.text} for r in parsed_reqs]
            else:
                new_stories = []
                session["user_stories"] = []
                session["raw_requirements"] = raw_reqs_for_persona

            cl_list = generate_checklist(new_stories, custom_prompt,
                                         raw_requirements=raw_reqs_for_persona)

            if not cl_list:
                flash(g.t.get("mvp_no_quality_requirements",
                              "Could not detect any testable requirements in the provided input."),
                      "error")
                return render_template("checklist.html",
                                       checklist=[], has_data=False, errors=errors)

            cl_dicts = [cl_to_dict(cl) for cl in cl_list]
            session["checklist_data"] = cl_dicts
            _persist_checklist(cl_dicts)
            return render_template("checklist.html", checklist=cl_list,
                                   has_data=True, errors=errors,
                                   resource_urls=extract_resource_urls())

        # GET — drain any background checklist job that finished
        # after the sync POST returned. Same bug as the TC path.
        _drain_cl_job_into_session()
        cl_data = session.get("checklist_data", [])
        # Same cold-start recovery as /test-cases — the pack outlives the
        # session because it lives in Postgres.
        restored_from_db = False
        if not cl_data:
            cl_data = _hydrate_from_db("cl")
            restored_from_db = bool(cl_data)
        if cl_data:
            cl_list = reconstruct_checklist(cl_data)
            if restored_from_db:
                flash(g.t.get(
                    "cl_restored_from_db",
                    "Restored %(n)d checklist items saved for this "
                    "project.") % {"n": len(cl_list)}, "info")
            return render_template("checklist.html",
                                   checklist=cl_list, has_data=True, errors=[],
                                   resource_urls=extract_resource_urls())

        return render_template("checklist.html", checklist=[], has_data=False,
                               errors=[], resource_urls=[])

    # ── Upload existing TC / CL packs ──────────────────────────────
    # Lets a tester import a previously-built test pack so it can be
    # run via /test-execution (manual) or /automation (Playwright).
    # Format is inferred from the uploaded filename's extension.
    _UPLOAD_EXTS = {"xlsx", "csv", "md", "markdown", "json"}

    def _save_upload(file_storage) -> tuple[str, str] | tuple[None, str]:
        """Persist the upload to a temp file. Returns (path, filename)
        or (None, error_message)."""
        if not file_storage or not file_storage.filename:
            return (None, g.t.get("upload_no_file", "No file selected."))
        filename = secure_filename(file_storage.filename)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in _UPLOAD_EXTS:
            return (None, g.t.get(
                "upload_bad_ext",
                f"Unsupported file type ‘{ext}’. Use one of: xlsx, csv, md, json."))
        # Use a tempfile under UPLOAD_FOLDER so the existing 64 MB cap
        # and write-permission probes apply uniformly.
        upload_dir = app.config.get("UPLOAD_FOLDER") or tempfile.gettempdir()
        os.makedirs(upload_dir, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="tc_import_", suffix=f"_{filename}",
                                    dir=upload_dir)
        try:
            with os.fdopen(fd, "wb") as out:
                file_storage.save(out)
        except Exception as exc:
            return (None, f"Could not save the uploaded file: {exc}")
        return (path, filename)

    @app.route("/test-cases/upload", methods=["POST"])
    def test_cases_upload():
        path, filename = _save_upload(request.files.get("upload_file"))
        if not path:
            flash(filename, "error")
            return redirect(_back_to_caller(default="test_cases_page"))
        try:
            cases = import_parse_test_cases(path, filename)
        except Exception as exc:
            flash(f"Import failed: {exc}", "error")
            return redirect(_back_to_caller(default="test_cases_page"))
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        if not cases:
            flash(g.t.get(
                "upload_no_rows",
                "No test cases were recognised in the file. Check that it has a "
                "header row and at least an ‘ID’ + ‘Summary’ + ‘Steps’ column."),
                "error")
            return redirect(_back_to_caller(default="test_cases_page"))

        mode = (request.form.get("upload_mode") or "replace").lower()
        existing = session.get("test_cases_data", []) if mode == "append" else []
        merged = existing + [tc_to_dict(tc) for tc in cases]
        session["test_cases_data"] = merged
        _persist_test_cases(merged)
        # Imported packs don't carry their own user stories, so reset
        # the traceability matrix — it would otherwise reference IDs
        # that no longer exist.
        session.pop("traceability_data", None)

        flash(
            g.t.get("upload_tc_ok",
                    f"Imported {len(cases)} test case(s) from {filename}.")
            + (f" Total now: {len(merged)}." if mode == "append" else ""),
            "success",
        )
        # Stay on whatever page the form was POSTed from. The same
        # upload form lives on /test-cases AND on /test-execution; the
        # operator who hit Upload from the execution page expects to
        # land back there, not on the generation page. Run is then
        # triggered by the user clicking the Run button — uniform
        # behaviour with the generation flow.
        return redirect(_back_to_caller(default="test_cases_page"))

    # ── Inline TC editor: walkthrough binding ────────────────────
    # Sprint 5 follow-up to the merged PR-3 (#12). The walkthrough
    # runner's URL-pattern TC matcher (see
    # ``engine.walkthrough_tc_match.select_tcs_for_url``) reads two
    # per-TC fields, ``url_pattern`` and ``trigger``, that the
    # PR-2 schema migration added to the DB but no UI ever set. This
    # endpoint exposes a minimal in-place editor: PATCH-style POST
    # from the /test-cases card, pure form-encoded body (no JSON →
    # works with progressive-enhancement, no fetch needed), updates
    # the session list AND the DB row atomically. Returns either a
    # JSON ack (when the client sends Accept: application/json from
    # fetch) or a redirect back to /test-cases (for noscript posts).
    @app.route("/test-cases/<tc_id>/walkthrough-meta", methods=["POST"])
    def test_cases_update_walkthrough_meta(tc_id: str):
        tc_data = session.get("test_cases_data", []) or []
        target = None
        for tc in tc_data:
            if tc.get("id") == tc_id:
                target = tc
                break
        if target is None:
            msg = f"Test case {tc_id!r} not found in the active pack."
            if request.accept_mimetypes.best == "application/json":
                return jsonify({"error": "not_found", "message": msg}), 404
            flash(msg, "error")
            return redirect(url_for("test_cases_page"))

        # Sanitise inputs. ``url_pattern`` is a free-form fnmatch glob
        # capped at 200 chars (matches the DB column width); ``trigger``
        # is one of three enum values, anything else falls back to the
        # safe default "manual" so a typo never silently opts a TC into
        # the walkthrough firing path.
        url_pattern = (request.form.get("url_pattern") or "").strip()[:200]
        trigger = (request.form.get("trigger") or "manual").strip().lower()
        if trigger not in ("manual", "walkthrough_url_match", "always"):
            trigger = "manual"

        target["url_pattern"] = url_pattern
        target["trigger"] = trigger
        # Persist the whole pack — the DB save_test_cases path does
        # wipe-and-replace which is fine here because session_data is
        # always the source of truth for the *current* pack.
        session["test_cases_data"] = tc_data
        try:
            _persist_test_cases(tc_data)
        except Exception as exc:
            log.warning("walkthrough-meta: persist failed for %s: %s",
                         tc_id, exc)

        if request.accept_mimetypes.best == "application/json":
            return jsonify({
                "id": tc_id,
                "url_pattern": url_pattern,
                "trigger": trigger,
            })
        flash(
            g.t.get("tc_walkthrough_meta_saved",
                    f"Walkthrough binding for {tc_id} saved."),
            "success",
        )
        return redirect(url_for("test_cases_page") + f"#{tc_id}")

    @app.route("/test-cases/<tc_id>/automation-step-kind", methods=["POST"])
    def test_cases_update_step_kind(tc_id: str):
        """PR-C — patch one or many recorded steps' ``kind`` /
        ``assertion_type`` fields.

        The TC editor dropdown fires this when the operator flips a
        recorded step from "Action" to "Assert visible/text/url". We
        accept either a single-step patch
        (``{"index": N, "kind": "...", "assertion_type": "..."}``) or
        a list under ``steps`` for bulk edits. Out-of-range indices and
        invalid kind/assertion_type values are rejected with 400 so a
        client-side bug can't silently corrupt the recording.

        Gated on the same ``RECORDER_ENABLED`` flag as the recorder
        surfaces — when the host hasn't opted into the pilot the route
        returns 403 instead of writing.
        """
        if not _recorder_enabled():
            return jsonify({"error": "recorder_disabled"}), 403

        tc_data = session.get("test_cases_data", []) or []
        target = None
        for tc in tc_data:
            if tc.get("id") == tc_id:
                target = tc
                break
        if target is None:
            return jsonify({"error": "tc_not_found", "tc_id": tc_id}), 404

        payload = request.get_json(silent=True) or {}
        patches = payload.get("steps")
        if not isinstance(patches, list):
            # Single-patch convenience shape.
            patches = [{
                "index":           payload.get("index"),
                "kind":            payload.get("kind"),
                "assertion_type":  payload.get("assertion_type"),
            }]

        import json as _json
        raw_json = target.get("automation_steps_json") or ""
        if not raw_json:
            return jsonify({"error": "no_recording",
                            "tc_id": tc_id}), 400
        try:
            steps = _json.loads(raw_json)
        except (ValueError, TypeError):
            return jsonify({"error": "corrupt_recording",
                            "tc_id": tc_id}), 400
        if not isinstance(steps, list):
            return jsonify({"error": "corrupt_recording",
                            "tc_id": tc_id}), 400

        changed = 0
        for patch in patches:
            try:
                idx = int(patch.get("index"))
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= len(steps):
                return jsonify({"error": "index_out_of_range",
                                "index": idx,
                                "tc_id": tc_id}), 400
            kind = str(patch.get("kind") or "action").strip().lower()
            atype = str(patch.get("assertion_type") or "").strip().lower()
            if kind not in ("action", "assertion"):
                return jsonify({"error": "invalid_kind",
                                "kind": kind,
                                "tc_id": tc_id}), 400
            if kind == "assertion":
                if atype not in ("visible", "text", "url"):
                    return jsonify({"error": "invalid_assertion_type",
                                    "assertion_type": atype,
                                    "tc_id": tc_id}), 400
            else:
                atype = ""
            step = steps[idx]
            if not isinstance(step, dict):
                continue
            step["kind"] = kind
            step["assertion_type"] = atype
            changed += 1

        # Resave both into session (for the live view) and DB (for the
        # runner's next pass). Wipe-and-replace through the helper so
        # the column stays consistent with the session snapshot.
        target["automation_steps_json"] = _json.dumps(steps, ensure_ascii=False)
        session["test_cases_data"] = tc_data

        pid = session.get("active_project_id") or ""
        if pid:
            try:
                _db.update_tc_automation_steps(pid, tc_id, steps)
            except Exception as exc:
                _log.warning("automation-step-kind persist failed for "
                              "%s/%s: %s", pid, tc_id, exc)

        return jsonify({"ok": True, "tc_id": tc_id,
                        "changed": changed, "total_steps": len(steps)})

    # ── PR-E: browser-extension recorder endpoints ─────────────────
    #
    # The extension posts here from the SUT's tab (cross-origin). Both
    # routes are JSON-only, CORS-enabled for any origin (the extension's
    # content-script runs in the SUT's origin which we cannot predict),
    # and gated on RECORDER_ENABLED so the surface stays invisible when
    # the pilot flag is off. The /start endpoint mints a one-shot token
    # bound to the active project; /finish accepts the captured step
    # list and reuses the PR-D segmenter → classifier → SessionDraft
    # pipeline.

    @app.route("/api/recorder-session/start", methods=["POST", "OPTIONS"])
    def api_recorder_session_start():
        """Mint a fresh recording token for the active project.

        Called from the /test-cases trigger button. The token returned
        is appended to the SUT URL as ``#testfortge-recorder-token=<t>``
        so the extension's content-script can pick it up on the next
        page load without needing the operator to copy-paste anything.

        Body (JSON, optional):
          ``{"project_id": "<pid>"}`` — overrides the session's active
          project. Falls back to ``session['active_project_id']`` when
          omitted (the usual path from the TestForTge UI button).

        Returns ``{token, project_id, finish_url, review_url_template}``
        on success. 403 when RECORDER_ENABLED is off, 400 when neither
        the body nor session carries a project_id.
        """
        if request.method == "OPTIONS":
            return _recorder_cors_preflight()
        if not _recorder_enabled():
            return _json_with_cors({"error": "recorder_disabled"}, 403)
        payload = request.get_json(silent=True) or {}
        pid = (payload.get("project_id")
                or session.get("active_project_id")
                or session.get("project_id") or "").strip()
        if not pid:
            return _json_with_cors({"error": "no_active_project"}, 400)
        # Token is the same shape as PR-D's draft tokens — secrets-grade
        # URL-safe base64 so the value is opaque and can't be guessed.
        import secrets as _secrets
        token = _secrets.token_urlsafe(32)
        # In-memory mapping token → (pid, created_at). The extension's
        # /finish call resolves the project off this. We deliberately
        # do NOT persist to DB — these tokens are short-lived (max
        # session lifetime), don't survive a worker restart, and a
        # restart-orphaned recording just expires gracefully (extension
        # gets 404 on finish and the operator re-starts). Persisting
        # would require a second migration and add no real safety.
        _RECORDER_SESSIONS[token] = {
            "project_id": pid,
            "created_at": _time.time(),
        }
        # Cap the mapping at 1000 entries — far above any realistic
        # concurrent-session count, but keeps a runaway integration
        # from filling worker memory indefinitely.
        if len(_RECORDER_SESSIONS) > 1000:
            _purge_oldest_recorder_session()
        base = request.host_url.rstrip("/")
        return _json_with_cors({
            "token": token,
            "project_id": pid,
            "finish_url": f"{base}/api/recorder-session/finish",
            "review_url_template": f"{base}/test-cases/review-session/{{token}}",
        })

    @app.route("/api/recorder-session/finish", methods=["POST", "OPTIONS"])
    def api_recorder_session_finish():
        """Accept captured steps from the extension, stage as a draft.

        Body (JSON, required):
          ``{"token": "<from /start>", "steps": [<AutomationStep dict>, ...]}``

        Pipeline mirrors PR-D's CLI ``_finish_review_mode``:

          1. Resolve token → project_id (404 if unknown / expired).
          2. Decode steps via ``AutomationStep(**dict)`` defensively.
          3. ``session_segmenter.segment()`` → list[ProposedTC].
          4. ``db.create_session_draft()`` writes the draft row.
          5. Return ``{review_url}`` for the extension to open.

        We consume the token (delete from the in-memory map) regardless
        of segmenter outcome so a stuck recording can't be replayed by
        a hostile or buggy client.
        """
        if request.method == "OPTIONS":
            return _recorder_cors_preflight()
        if not _recorder_enabled():
            return _json_with_cors({"error": "recorder_disabled"}, 403)
        payload = request.get_json(silent=True) or {}
        token = (payload.get("token") or "").strip()
        steps_raw = payload.get("steps") or []
        if not token or not isinstance(steps_raw, list):
            return _json_with_cors({"error": "bad_request"}, 400)
        meta = _RECORDER_SESSIONS.pop(token, None)
        if not meta:
            return _json_with_cors({"error": "unknown_token"}, 404)
        pid = meta["project_id"]

        # Decode steps. Each item is a dict with the AutomationStep
        # field shape — be defensive against the extension sending
        # partial / extra keys. _decode_recorded_steps already handles
        # this exact pattern for PR-D's review-flow.
        from engine.automation_qa import _decode_recorded_steps
        import json as _json
        steps = _decode_recorded_steps(_json.dumps(steps_raw))
        if not steps:
            return _json_with_cors({
                "error": "no_valid_steps",
                "received": len(steps_raw),
            }, 400)

        # Reuse PR-D pipeline verbatim.
        from engine.session_segmenter import segment
        proposed = [p.to_dict() for p in segment(steps)]
        if not proposed:
            return _json_with_cors({
                "error": "segmenter_returned_empty",
            }, 500)

        # PR-F — decode the optional deep-capture blob. Sanitised +
        # capped server-side so a busy or hostile client can't bloat the
        # row. ``None`` when the extension sent nothing (older extension,
        # or debugger never attached and had no error to report).
        telemetry = _sanitise_recorder_telemetry(payload.get("telemetry"))

        # Use the same draft-token shape PR-D uses so the review URL
        # is indistinguishable from a CLI-staged session. Generated
        # fresh per finish — the recorder token from /start is never
        # written to DB.
        import secrets as _secrets
        draft_token = _secrets.token_urlsafe(32)
        row_id = _db.create_session_draft(pid, draft_token, proposed,
                                           telemetry=telemetry)
        if row_id is None:
            return _json_with_cors({"error": "draft_persist_failed"}, 500)

        base = request.host_url.rstrip("/")
        counts = (telemetry or {}).get("counts", {})
        return _json_with_cors({
            "ok": True,
            "review_url": f"{base}/test-cases/review-session/{draft_token}",
            "proposed_tc_count": len(proposed),
            "telemetry_counts": counts,
        })

    # Both /api/recorder-session/{start,finish} carry their own auth
    # (the per-session token from /start; the active project_id binding
    # in session). The global CSRFProtect gate can't apply because:
    #   * /finish is called from the extension's service worker from
    #     the SUT's origin — there's no TestForTge session cookie or
    #     csrf_token in that context.
    #   * /start is called from the modal on /test-cases via fetch(),
    #     but for consistency with /finish we exempt both rather than
    #     thread a CSRF header through the modal-JS handler.
    # Same pattern routes/debug.py uses for /debug/walkthrough.
    _ext = app.extensions.get("csrf") if hasattr(app, "extensions") else None
    if _ext is not None:
        for _fn in (api_recorder_session_start,
                     api_recorder_session_finish):
            try:
                _ext.exempt(_fn)
            except Exception as exc:  # pragma: no cover — defensive
                _log.debug("recorder-session csrf.exempt skipped: %s", exc)

    # ── PR-F Phase 2: browser-control channel (extension ↔ Flask) ──
    #
    # Only two endpoints live here — the ones the EXTENSION calls:
    #   * /poll   — extension pulls its next queued command
    #   * /result — extension posts a command's result
    # The controller side (mint a session, enqueue a command, read the
    # result) is the MCP server talking to the shared DB directly, so it
    # needs no HTTP endpoint here. Both are CSRF-exempt + CORS-open for
    # the same reason the recorder endpoints are: the extension calls
    # them cross-origin from whatever SUT the operator is driving, with
    # no TestForTge cookie. The control token in the body is the auth.

    @app.route("/api/browser/poll", methods=["POST", "OPTIONS"])
    def api_browser_poll():
        """Extension pulls its next command. Body: ``{"token": "<ctl>"}``.
        Returns ``{"command": {command_id, verb, params} | null}``. Also
        bumps the session's liveness so the controller can see the browser
        is attached. Short-poll (returns immediately) to avoid holding a
        sync worker — the extension re-polls on a short interval."""
        if request.method == "OPTIONS":
            return _recorder_cors_preflight()
        if not _browser_control_enabled():
            return _json_with_cors({"error": "control_disabled"}, 403)
        payload = request.get_json(silent=True) or {}
        token = (payload.get("token") or "").strip()
        if not token:
            return _json_with_cors({"error": "bad_request"}, 400)
        # Liveness first — a poll for a stale/sealed token returns 404 so
        # the extension knows to tear its loop down.
        if not _db.touch_browser_control_session(token):
            return _json_with_cors({"error": "unknown_or_stopped"}, 404)
        cmd = _db.dequeue_browser_command(token)
        return _json_with_cors({"command": cmd})

    @app.route("/api/browser/result", methods=["POST", "OPTIONS"])
    def api_browser_result():
        """Extension reports a command's outcome. Body:
        ``{"command_id", "ok": bool, "result": {...}, "error": "..."}``."""
        if request.method == "OPTIONS":
            return _recorder_cors_preflight()
        if not _browser_control_enabled():
            return _json_with_cors({"error": "control_disabled"}, 403)
        payload = request.get_json(silent=True) or {}
        command_id = (payload.get("command_id") or "").strip()
        if not command_id:
            return _json_with_cors({"error": "bad_request"}, 400)
        ok = bool(payload.get("ok"))
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        error = str(payload.get("error") or "")
        wrote = _db.complete_browser_command(command_id, ok, result, error)
        if not wrote:
            return _json_with_cors({"error": "unknown_or_terminal"}, 404)
        return _json_with_cors({"ok": True})

    if _ext is not None:
        for _fn in (api_browser_poll, api_browser_result):
            try:
                _ext.exempt(_fn)
            except Exception as exc:  # pragma: no cover — defensive
                _log.debug("browser-control csrf.exempt skipped: %s", exc)

    # ── PR-D: session-review route (CLI staging → operator confirm) ─

    @app.route("/test-cases/review-session/<token>", methods=["GET"])
    def test_cases_review_session(token: str):
        """GET — render the review screen.

        Looks the draft up by token, validates the active session's
        project_id matches the draft's, and shows N proposed-TC cards
        with summary, step preview, suggested-suite dropdown, and a
        per-card Save / Skip checkbox.

        Token failures (missing / expired / consumed) → 404 with a
        friendly message rather than a generic error so the operator
        understands they're past the 24-h window or already saved.
        """
        if not _recorder_enabled():
            return render_template(
                "review_session.html",
                draft=None,
                error_message=g.t.get(
                    "review_session_pilot_off",
                    "Recorder pilot is not enabled on this host "
                    "(RECORDER_ENABLED=0).",
                ),
            ), 403
        draft = _db.get_session_draft(token)
        if draft is None:
            return render_template(
                "review_session.html",
                draft=None,
                error_message=g.t.get(
                    "review_session_not_found",
                    "This review link is expired, already used, or "
                    "never existed. Recordings stage for 24 hours; "
                    "re-run the CLI to capture again.",
                ),
            ), 404
        # Project guard — the link is unguessable but we still scope
        # the rendering to the currently-active project. An operator
        # juggling multiple projects shouldn't accidentally land TCs
        # into the wrong one because they followed a stale link.
        active_pid = session.get("active_project_id") or session.get("project_id") or ""
        if active_pid and active_pid != draft["project_id"]:
            return render_template(
                "review_session.html",
                draft=None,
                error_message=g.t.get(
                    "review_session_wrong_project",
                    "This review link belongs to a different project. "
                    "Switch projects in the picker and reopen the link.",
                ),
            ), 403
        return render_template("review_session.html",
                                draft=draft,
                                token=token,
                                error_message=None)

    @app.route("/test-cases/review-session/<token>", methods=["POST"])
    def test_cases_review_session_save(token: str):
        """POST — consume the draft and create the selected ProposedTCs
        as real TestCase rows.

        Body shape (form-encoded or JSON):
          ``{"selected": [{"idx": 0, "suite": "Smoke",
                            "summary_override": "..."}, ...]}``

        Out-of-range / unknown-suite entries are rejected with 400 so
        a tampered POST cannot smuggle weird values into the DB.
        ``consume_session_draft`` then seals the row so a refresh of
        the GET cannot double-insert.
        """
        if not _recorder_enabled():
            return jsonify({"error": "recorder_disabled"}), 403

        draft = _db.get_session_draft(token)
        if draft is None:
            return jsonify({"error": "draft_not_found"}), 404

        active_pid = session.get("active_project_id") or session.get("project_id") or ""
        if active_pid and active_pid != draft["project_id"]:
            return jsonify({"error": "wrong_project"}), 403
        pid = draft["project_id"]

        # JSON body or form-encoded — accept both so the template can
        # POST via plain <form> if JS is disabled.
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            selected = payload.get("selected") or []
        else:
            selected = _parse_review_form(request.form,
                                           len(draft["proposed_tcs"]))

        if not isinstance(selected, list) or not selected:
            return jsonify({"error": "no_selection"}), 400

        from engine.suite_classifier import VALID_SUITES

        # Build the TC rows in order. Wrong-index / wrong-suite → 400.
        created_ids: list[int] = []
        created_external_ids: list[str] = []
        proposed = draft["proposed_tcs"]
        next_section_num = _next_section_num(pid)
        import json as _json
        for entry in selected:
            try:
                idx = int(entry.get("idx"))
            except (TypeError, ValueError):
                return jsonify({"error": "invalid_idx"}), 400
            if idx < 0 or idx >= len(proposed):
                return jsonify({"error": "idx_out_of_range",
                                "idx": idx}), 400
            suite = str(entry.get("suite") or "").strip()
            if suite and suite not in VALID_SUITES:
                return jsonify({"error": "invalid_suite",
                                "suite": suite}), 400
            pt = proposed[idx]
            summary_override = (entry.get("summary_override") or "").strip()
            summary = summary_override or pt.get("summary", "") or "Recorded flow"
            steps_dicts = pt.get("steps") or []
            new_ext = _mint_external_id(pid)
            tc = {
                "id": new_ext,
                "section": f"Section {next_section_num}: Recorded session",
                "section_num": next_section_num,
                "summary": summary,
                "preconditions": "",
                "test_steps": _human_steps_preview(steps_dicts),
                "test_data": "",
                "expected_result": pt.get("intent", "") or "",
                "issues": "",
                "comment": "Generated from recorded session "
                           f"(draft {token[:8]}…).",
                "user_story_id": "",
                "category": "Positive",
                "priority": "Medium",
                "status": "Unchecked",
                "testing_type": "Functional",
                "url_pattern": "",
                "trigger": "manual",
                "automation_steps_json": _json.dumps(
                    steps_dicts, ensure_ascii=False),
                "suite": suite or pt.get("suggested_suite", ""),
            }
            new_id = _db.create_test_case(pid, tc)
            if new_id is None:
                return jsonify({"error": "create_failed",
                                "idx": idx}), 500
            created_ids.append(new_id)
            created_external_ids.append(new_ext)
            next_section_num += 1

        # Seal the draft so a refresh of the GET doesn't double-insert.
        _db.consume_session_draft(token)

        # Push the new pack into the session so /test-cases renders
        # the additions immediately without a manual project switch.
        session["test_cases_data"] = _db.load_test_cases(pid)

        return jsonify({
            "ok": True,
            "created_count": len(created_ids),
            "created_external_ids": created_external_ids,
            "redirect_url": url_for("test_cases_page"),
        })

    # ── Async generation pipeline ────────────────────────────────
    # The sync /test-cases and /checklist POST handlers can block for
    # 30–90 s on a busy LLM, which surfaces in the UI as a frozen page.
    # The pair below splits that into:
    #   POST /test-cases/run-async    — submits the job, returns
    #                                   {"job_id": ..., "status": "pending"}
    #   GET  /test-cases/status/<id>  — polled by the modal; reports
    #                                   pending / running / done / failed,
    #                                   and on done writes the result back
    #                                   into session so a redirect to
    #                                   /test-cases renders normally.
    # Same pair exists for /checklist further below.
    @app.route("/test-cases/run-async", methods=["POST"])
    def test_cases_run_async():
        raw_lines, errors, custom_prompt = parse_page_input()
        if not raw_lines:
            return jsonify({"error": "no_input",
                            "message": g.t.get("mvp_no_input",
                                "Please enter requirements or upload files.")}), 400

        sid = get_session_id(session)
        active = get_queue().count_active_by_meta(
            "tc_gen", "session_id", sid)
        if active >= MAX_CONCURRENT_GEN_JOBS:
            resp = jsonify({
                "error": "rate_limited",
                "message": (f"You already have {active} active generation "
                            f"jobs. Wait for them to finish before starting "
                            f"another."),
                "active": active,
                "limit": MAX_CONCURRENT_GEN_JOBS,
            })
            resp.status_code = 429
            resp.headers["Retry-After"] = "20"
            return resp

        # Resolve the active project id NOW, while we still hold a request
        # context (and therefore a real session). The worker thread runs
        # without any request context and cannot touch ``session`` to
        # auto-create a project — so persistence has to be done with the
        # pid we resolve here. Falsy result is fine: persistence becomes a
        # no-op and the in-session result still lights up the page.
        pid = ensure_active_project()

        def _worker(raw_lines=raw_lines, custom_prompt=custom_prompt, pid=pid):
            parsed_reqs = split_into_requirements(raw_lines)
            parsed_reqs = [r for r in parsed_reqs if not is_instruction(r.text)]
            raw_reqs_for_persona = (
                [{"id": r.id, "text": r.text} for r in parsed_reqs]
                if parsed_reqs else
                [{"id": f"RAW-{i+1}", "text": line}
                 for i, line in enumerate(raw_lines) if line.strip()]
            )
            new_stories = (generate_user_stories(parsed_reqs, custom_prompt)
                           if parsed_reqs else [])
            tc_list = generate_test_cases(new_stories, custom_prompt,
                                          raw_requirements=raw_reqs_for_persona)
            trace = generate_traceability(new_stories, tc_list) if tc_list else []
            tc_dicts = [tc_to_dict(tc) for tc in tc_list]

            # Append the authored stream, matching what the sync POST
            # does — site-aware when the input names a URL, artifacts-only
            # otherwise. Without this the async endpoint silently returns
            # a thinner pack than the sync one for the same input.
            url = _detect_first_url(raw_lines)
            if url:
                site_out = _run_site_aware(url, pid, custom_prompt,
                                           raw_lines=raw_lines)
                if site_out:
                    tc_dicts.extend(site_out.get("tc_dicts") or [])
            else:
                tc_dicts.extend(_run_authored_without_url(custom_prompt,
                                                          raw_lines))

            # Persist INSIDE the worker so the polling /status endpoint
            # never has to do a DB round-trip. On free-tier Postgres a
            # cold connection can take 1–2 s and was visibly stalling the
            # poll (browser caps at 6 concurrent requests per origin —
            # one slow /status hangs the modal forever once the cap is
            # hit). Best-effort: a DB outage must not hide the result.
            if pid and tc_dicts:
                try:
                    _db.save_test_cases(pid, tc_dicts)
                except Exception as exc:  # pragma: no cover
                    _log.warning("tc_gen worker persist: %s", exc)
            return {
                "tc_dicts": tc_dicts,
                "stories": [story_to_dict(s) for s in new_stories],
                "raw_requirements": raw_reqs_for_persona,
                "trace": trace,
            }

        job_id = get_queue().submit(
            "tc_gen", _worker, meta={"session_id": sid})
        session["tc_gen_job_id"] = job_id
        return jsonify({"job_id": job_id, "status": "pending"})

    @app.route("/api/pack-info", methods=["GET"])
    def api_pack_info():
        """How many rows the active project has saved in Postgres.

        Lets the client tell two very different situations apart when a
        job id stops resolving:

        * the worker finished, wrote the pack, and *then* the process
          died — reloading shows the work (see ``_hydrate_from_db``);
        * the worker died mid-run — nothing was saved and a retry is the
          only option.

        Both looked identical before ("The generation job was lost"), so
        the UI told users to redo work that was already on disk. The
        probe matters because a blind reload would discard whatever they
        had typed into the form.

        ``kind`` is "tc" (default) or "cl".
        """
        kind = (request.args.get("kind") or "tc").strip().lower()
        pid = session.get("project_id")
        if not pid:
            return jsonify({"count": 0, "project": None})
        loader = getattr(_db,
                         "load_test_cases" if kind != "cl"
                         else "load_checklist", None)
        if loader is None:  # pragma: no cover — defensive
            return jsonify({"count": 0, "project": pid})
        try:
            rows = loader(pid) or []
        except Exception as exc:
            _log.warning("pack-info read failed: %s", exc)
            return jsonify({"count": 0, "project": pid,
                            "error": "db_unavailable"}), 200
        return jsonify({"count": len(rows), "project": pid})

    @app.route("/test-cases/status/<job_id>", methods=["GET"])
    def test_cases_status(job_id):
        # Polling endpoint — must stay cheap and never block. The browser
        # caps concurrent connections to 6 per origin, so a single slow
        # /status response can stall the entire modal. The worker has
        # already persisted to Postgres before reaching DONE; here we
        # only mirror its result into the session.
        job = get_queue().get(job_id)
        if job is None or job.kind != "tc_gen":
            return jsonify({"error": "not_found"}), 404
        payload = job.to_public_dict()
        if job.status == DONE and job.result:
            r = job.result
            session["test_cases_data"] = r.get("tc_dicts", [])
            session["user_stories"] = r.get("stories", [])
            session["raw_requirements"] = r.get("raw_requirements", [])
            session["traceability_data"] = r.get("trace", [])
            # Tell the client where to send the user once it sees DONE.
            # Surfacing the URL in the payload (instead of hard-coding it
            # in the template) means the same /status JSON is enough to
            # drive a redirect even when the page is reopened in another
            # tab and the original template hash is gone.
            payload["redirect_url"] = url_for("test_cases_page")
        return jsonify(payload)

    @app.route("/checklist/run-async", methods=["POST"])
    def checklist_run_async():
        raw_lines, errors, custom_prompt = parse_page_input()
        if not raw_lines:
            return jsonify({"error": "no_input",
                            "message": g.t.get("mvp_no_input",
                                "Please enter requirements or upload files.")}), 400

        sid = get_session_id(session)
        active = get_queue().count_active_by_meta(
            "cl_gen", "session_id", sid)
        if active >= MAX_CONCURRENT_GEN_JOBS:
            resp = jsonify({
                "error": "rate_limited",
                "message": (f"You already have {active} active generation "
                            f"jobs. Wait for them to finish before starting "
                            f"another."),
                "active": active,
                "limit": MAX_CONCURRENT_GEN_JOBS,
            })
            resp.status_code = 429
            resp.headers["Retry-After"] = "20"
            return resp

        # Same rationale as /test-cases/run-async: resolve pid here so the
        # worker can persist without needing a request context, and the
        # /status endpoint never has to make a DB round-trip.
        pid = ensure_active_project()

        def _worker(raw_lines=raw_lines, custom_prompt=custom_prompt, pid=pid):
            parsed_reqs = split_into_requirements(raw_lines)
            parsed_reqs = [r for r in parsed_reqs if not is_instruction(r.text)]
            raw_reqs_for_persona = (
                [{"id": r.id, "text": r.text} for r in parsed_reqs]
                if parsed_reqs else
                [{"id": f"RAW-{i+1}", "text": line}
                 for i, line in enumerate(raw_lines) if line.strip()]
            )
            new_stories = (generate_user_stories(parsed_reqs, custom_prompt)
                           if parsed_reqs else [])
            cl_list = generate_checklist(new_stories, custom_prompt,
                                         raw_requirements=raw_reqs_for_persona)
            cl_dicts = [cl_to_dict(c) for c in cl_list]
            if pid and cl_dicts:
                try:
                    _db.save_checklist(pid, cl_dicts)
                except Exception as exc:  # pragma: no cover
                    _log.warning("cl_gen worker persist: %s", exc)
            return {
                "cl_dicts": cl_dicts,
                "stories": [story_to_dict(s) for s in new_stories],
                "raw_requirements": raw_reqs_for_persona,
            }

        job_id = get_queue().submit(
            "cl_gen", _worker, meta={"session_id": sid})
        session["cl_gen_job_id"] = job_id
        return jsonify({"job_id": job_id, "status": "pending"})

    @app.route("/checklist/status/<job_id>", methods=["GET"])
    def checklist_status(job_id):
        # Polling endpoint — see /test-cases/status for rationale.
        job = get_queue().get(job_id)
        if job is None or job.kind != "cl_gen":
            return jsonify({"error": "not_found"}), 404
        payload = job.to_public_dict()
        if job.status == DONE and job.result:
            r = job.result
            session["checklist_data"] = r.get("cl_dicts", [])
            session["user_stories"] = r.get("stories", [])
            session["raw_requirements"] = r.get("raw_requirements", [])
            payload["redirect_url"] = url_for("checklist_page")
        return jsonify(payload)

    @app.route("/checklist/upload", methods=["POST"])
    def checklist_upload():
        path, filename = _save_upload(request.files.get("upload_file"))
        if not path:
            flash(filename, "error")
            return redirect(_back_to_caller(default="checklist_page"))
        try:
            items = import_parse_checklist(path, filename)
        except Exception as exc:
            flash(f"Import failed: {exc}", "error")
            return redirect(_back_to_caller(default="checklist_page"))
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        if not items:
            flash(g.t.get(
                "upload_no_rows_cl",
                "No checklist items were recognised in the file. Check that it "
                "has a header row and at least an ‘Objective’ column."),
                "error")
            return redirect(_back_to_caller(default="checklist_page"))

        mode = (request.form.get("upload_mode") or "replace").lower()
        existing = session.get("checklist_data", []) if mode == "append" else []
        merged = existing + [cl_to_dict(it) for it in items]
        session["checklist_data"] = merged
        _persist_checklist(merged)

        flash(
            g.t.get("upload_cl_ok",
                    f"Imported {len(items)} checklist item(s) from {filename}.")
            + (f" Total now: {len(merged)}." if mode == "append" else ""),
            "success",
        )
        return redirect(_back_to_caller(default="checklist_page"))

    @app.route("/export/<fmt>")
    @app.route("/export/<fmt>")
    def export(fmt):
        stories = reconstruct_stories(session.get("user_stories", []))
        tc_list = reconstruct_test_cases(session.get("test_cases_data", []))
        cl_list = reconstruct_checklist(session.get("checklist_data", []))

        if stories and not tc_list:
            tc_list = generate_test_cases(stories)
        if stories and not cl_list:
            cl_list = generate_checklist(stories)

        trace = session.get("traceability_data", [])
        if not trace and stories and tc_list:
            trace = generate_traceability(stories, tc_list)

        # Defensive `.get() or {}` so a session whose project_setup
        # key was explicitly set to None doesn't 500 (audit finding).
        name = ((session.get("project_setup") or {}).get(
            "project_name", "project") or "project").replace(" ", "_")

        if fmt == "markdown":
            content = export_markdown(name, stories, tc_list, cl_list, trace, {})
            return Response(content, mimetype="text/markdown",
                            headers={"Content-Disposition": f"attachment; filename=testfortge_{name}.md"})
        if fmt == "html":
            content = export_html(name, stories, tc_list, cl_list, trace, {})
            return Response(content, mimetype="text/html",
                            headers={"Content-Disposition": f"attachment; filename=testfortge_{name}.html"})
        if fmt == "csv-testcases":
            content = export_csv_testcases(tc_list)
            return Response(content, mimetype="text/csv",
                            headers={"Content-Disposition": "attachment; filename=test_cases.csv"})
        if fmt == "csv-checklist":
            content = export_csv_checklist(cl_list)
            return Response(content, mimetype="text/csv",
                            headers={"Content-Disposition": "attachment; filename=checklist.csv"})
        if fmt == "xlsx-testcases":
            content = export_xlsx_testcases(tc_list)
            return Response(
                content,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": "attachment; filename=test_cases.xlsx"})
        if fmt == "xlsx-checklist":
            content = export_xlsx_checklist(cl_list)
            return Response(
                content,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": "attachment; filename=checklist.xlsx"})
        return "Unknown format", 400


__all__ = ["register"]

