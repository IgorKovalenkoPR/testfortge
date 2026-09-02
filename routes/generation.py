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

from engine import workspace as _workspace
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
from engine import gherkin as _gherkin
from engine import permissions as _perm
from engine.log import get_logger

from ._shared import (
    attachment_header,
    reconstruct_stories, reconstruct_test_cases, reconstruct_checklist,
    tc_to_dict, cl_to_dict, story_to_dict, get_session_id,
    parse_page_input, extract_resource_urls, ensure_active_project,
    resolve_active_project, SERVER_START_TIME,
    pack_cleared, pack_test_cases, pack_checklist, mirror_pack,
    pack_version,
)

# Hard cap on concurrent generation jobs per session — same threshold
# as automation/estimation. Prevents a runaway tab from monopolising
# the worker pool on Render free tier.
MAX_CONCURRENT_GEN_JOBS = 2

_log = get_logger(__name__)


def _no_input_message(errors: list[str]) -> str:
    """"Nothing to work with", and *why* when the parser knows.

    ``parse_page_input`` returns a list of ``"<file>: <reason>"`` strings
    and both async routes used to drop it, so a ``.doc`` upload — accepted
    by ``allowed_file``, refused by the parser with "save the file as
    .docx" — was answered with "Please enter requirements or upload
    files.". The operator had uploaded a file, the product knew exactly
    why it could not use it, and said neither thing.

    Measured on the auth preview: the page named no file, showed no
    warning, and the message it did show was untrue.
    """
    base = g.t.get("mvp_no_input",
                   "Please enter requirements or upload files.")
    if not errors:
        return base
    return base + " " + " ".join(errors)


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
    every SUT, we accept ``*``.

    The sentence that used to end this paragraph — "the endpoints carry
    their own auth (the per-session token from /start) so a public origin
    can still only act on a project it was authorised against" — was true
    of ``/finish`` and false of ``/start``, which is the endpoint that
    *issues* the authorisation and had none of its own. ``/start`` now
    requires a session role, so the claim holds for both.

    The wildcard stays on both. On ``/start`` it grants nothing now: ``*``
    forbids credentials, so a cross-origin fetch cannot carry the cookie the
    role gate needs, and SameSite=Lax would not send it anyway. Removing it
    would mean unpicking the shared preflight for no reachable gain.
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
            "uncheck": "Uncheck",
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
            # Grid inventory — drives the list_surface half of the
            # deterministic coverage model (sorting, paging, filters,
            # bulk actions). Without it those cases cannot be justified.
            "tables": getattr(p, "tables", None) or [],
            "grid_controls": getattr(p, "grid_controls", None) or {},
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
                    raw_lines: list[str] | None = None,
                    tc_format: str = "manual") -> dict | None:
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
    tc_dicts = _gherkin.apply_format(tc_dicts, tc_format)

    # Low-level checklist (PR-2). Built by walking the crawled surfaces into
    # the shape of the team's reviewed deliverable — Header / Page Content /
    # Footer, hierarchically numbered, one observable check per row. It
    # REPLACES the area-template checklist on a site-aware run rather than
    # appending to it: the templates answer "what does a login form owe",
    # this answers "what does THIS page owe", and shipping both gives the
    # tester two overlapping sheets to walk.
    ll_gaps: list[str] = []
    try:
        from engine import checklist_author as _cla
        from engine import checklist_rules as _clr
        pages = [_page_to_dict(pg)
                 for pg in (getattr(site_analysis, "pages", None) or [])]
        # The author agent when a key is configured, the enumeration when
        # not — author_checklist falls back internally, so this call has
        # one shape either way and the free tier keeps working untouched.
        authored = _cla.author_checklist(
            artifacts=_cla.Artifacts(
                url=url, pages=pages, custom_prompt=custom_prompt or "",
                requirements=[ln.strip() for ln in (raw_lines or [])
                              if (ln or "").strip()][:120]),
            profile=profile)
        if authored.total:
            cl_dicts = [cl_to_dict(ci)
                        for ci in _cla.to_checklist_items(authored)]
            ll_gaps = list(authored.gaps)
            if authored.lint_findings:
                # Wording the agent got wrong that normalisation could not
                # fix. Logged rather than hidden — it is the signal that
                # the prompt needs work, not the operator.
                _log.info("checklist author: %d residual wording findings",
                          len(authored.lint_findings))
            _log.info("checklist source=%s rows=%d",
                      authored.source, authored.total)
    except Exception as exc:  # pragma: no cover — never block generation
        _log.warning("low-level checklist build failed: %s", exc)

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
        # Surfaces the checklist could not evidence — an unstructured Footer,
        # sections beyond the cap. Flashed to the operator so a thin sheet
        # reads as a known limitation rather than as the whole product.
        "checklist_gaps": ll_gaps,
    }


def _page_to_dict(page) -> dict:
    """PageInfo → plain dict, for the generators that take dicts."""
    if isinstance(page, dict):
        return page
    try:
        from dataclasses import asdict, is_dataclass
        if is_dataclass(page):
            return asdict(page)
    except Exception:  # pragma: no cover — defensive
        pass
    return {k: v for k, v in vars(page).items() if not k.startswith("_")}


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


def _requested_tc_format() -> str:
    """The format the operator asked for on this request.

    Two states, not three: the manual columns are populated either way, so
    a "manual + BDD" option would store exactly the same row as "BDD". The
    flag records only whether the automation module should pick the case
    up. See engine.gherkin.apply_format.
    """
    from engine import gherkin as _gk
    try:
        raw = request.form.get("tc_format", "")
    except Exception:  # pragma: no cover — outside a request context
        raw = ""
    return _gk.coerce_format(raw)


# ── The pack: reading and writing it (E3.3) ──────────────────────
#
# Every access to this project's test cases and checklist goes through the
# four functions below, which go through engine.workspace. Before E3.3 the
# module read ``session["test_cases_data"]`` in eleven places and wrote it
# in nine, each with its own idea of when to fall back to Postgres — which
# is why the same pack could be present on one page and missing on another.
#
# While WORKSPACE_DB_FIRST is off these behave exactly as the old code did,
# session included. When it is on, the session stops carrying the pack at
# all, and the row shrinks from a few hundred kilobytes to nothing.


# The pack accessors live in routes/_shared so every module reads the
# project the same way, including the "the user cleared this" check —
# E3.4 moved them there when execution, estimation and the dashboard
# needed the identical logic. These aliases keep the local call sites
# short.
_pack_cleared = pack_cleared
_tc_rows = pack_test_cases
_cl_rows = pack_checklist
_mirror = mirror_pack


def _conflict_response(exc, *, redirect_to: str = "test_cases_page"):
    """One answer to a lost race, for every site that can lose one.

    409, not 400 or 500: the request was well-formed and the server is
    healthy — the caller's copy of the pack is simply out of date, and the
    fix is to reload and redo. A single responder because a conflict is
    confusing enough without three pages explaining it three ways.
    """
    message = (
        "Someone else changed this project while you were working. "
        "Reload the page to see their version, then make your change again."
    )
    if request.accept_mimetypes.best == "application/json":
        return jsonify({
            "error": "conflict",
            "message": message,
            # Enough for a client to say how far behind it is, without
            # revealing anything about who made the change.
            "expected_version": getattr(exc, "expected", None),
            "current_version": getattr(exc, "actual", None),
        }), 409
    flash(message, "error")
    # 409 with a redirect: the status is the machine-readable fact and the
    # flash is the human one. A bare 302 would let a fetch() caller believe
    # the edit landed.
    return redirect(url_for(redirect_to)), 409


def _store_test_cases(tc_dicts: list[dict], *,
                      expected_version: int | None = None) -> None:
    """Write the pack: database first, session mirror second.

    Best-effort on the database: an outage must not stop the user seeing
    what was just generated. Errors are logged and swallowed, which is the
    behaviour the old ``_persist_test_cases`` had and the reason a pack can
    briefly exist only on screen.
    """
    pid = ensure_active_project()
    if pid:
        try:
            # E4.7: a regeneration keeps what a person edited, and says so.
            _workspace.save_test_cases(pid, tc_dicts,
                                       expected_version=expected_version,
                                       protect_edits=True)
            _flash_merge_report("test_cases")
        except _db.WriteConflict:
            # Never swallowed. A conflict means somebody else's work is at
            # stake, and the caller has to tell the user rather than let the
            # save look as though it landed.
            raise
        except Exception as exc:  # pragma: no cover — best-effort write
            _log.warning("persist test cases failed: %s", exc)
    _mirror("test_cases_data", tc_dicts)


def _import_mapping(kind: str) -> dict:
    """The column mapping the user chose, from ``map_<target>`` form fields.

    Empty when they did not choose one, which is the normal case — the
    automatic matcher handles a file whose headers use the usual words.
    """
    from engine import import_preview
    out = {}
    for target in import_preview.ALIASES[kind]:
        value = (request.form.get(f"map_{target}") or "").strip()
        if value:
            out[target] = value
    return out


def _import_headers(path: str, filename: str) -> list:
    """The uploaded file's column names, read before the file is removed."""
    from engine import imports as _imports
    try:
        return _imports.read_headers(path, filename)
    except Exception as exc:      # pragma: no cover — unreadable file
        _log.info("could not read import headers: %s", exc)
        return []


def _flash_import_mapping(kind: str, headers: list, mapping: dict):
    """Report what the headers were taken to mean, and offer the form.

    Returns the ``Mapping`` so the caller can say why nothing imported: a file
    whose columns match nothing produced "0 rows" before this, which told the
    user their file was wrong when only its vocabulary was.
    """
    from engine import import_preview
    analysis = import_preview.analyse(kind, headers, override=mapping)
    session[_IMPORT_HEADERS_KEY] = {"kind": kind, "headers": analysis.headers}
    return analysis


#: Where the last upload's headers live, so the mapping form can offer the
#: file's own column names. Just the header row — a few short strings, not
#: the pack, so this does not reintroduce what E3 took out of the session.
_IMPORT_HEADERS_KEY = "import_headers"


def _flash_merge_report(kind: str) -> None:
    """Tell the user what the regeneration kept (E4.7).

    A merge nobody can see is as confusing as the overwrite it replaced: they
    clicked Generate, some rows did not change, and nothing said why.
    """
    try:
        report = _db.take_merge_report(kind)
    except Exception:      # pragma: no cover — reporting only
        return
    if report is None:
        return
    message = report.message()
    if message:
        flash(message, "info")


def _store_checklist(cl_dicts: list[dict], *,
                     expected_version: int | None = None) -> None:
    """Same contract as :func:`_store_test_cases`, for the checklist."""
    pid = ensure_active_project()
    if pid:
        try:
            _workspace.save_checklist(pid, cl_dicts,
                                      expected_version=expected_version,
                                      protect_edits=True)
            _flash_merge_report("checklist")
        except _db.WriteConflict:
            raise
        except Exception as exc:  # pragma: no cover
            _log.warning("persist checklist failed: %s", exc)
    _mirror("checklist_data", cl_dicts)


#: Retained names for the call sites that still use them.
#:
#: The async workers persist to Postgres themselves, so a job drain could
#: in principle mirror into the session and stop there. It does not: making
#: the drain's correctness depend on a closure three hundred lines away
#: means a stubbed job, or a worker whose own write failed, loses the result
#: with nothing in the logs. ``save_test_cases`` is wipe-and-replace with
#: identical content, so writing again is idempotent and costs one query on
#: the rare GET that actually drains.
_persist_test_cases = _store_test_cases
_persist_checklist = _store_checklist



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
    rows = _tc_rows() if kind == "tc" else _cl_rows()
    if rows:
        # Mirror so the modules that still read the session key directly
        # (execution, automation, chat — E3.4) see the recovered pack too.
        # ``_mirror`` is a no-op once the database is the source of truth,
        # at which point they will be reading through the repository and
        # will not need it.
        _mirror("test_cases_data" if kind == "tc" else "checklist_data", rows)
        _log.info("hydrated %d %s rows from DB for project %s",
                  len(rows), kind, resolve_active_project())
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
        _store_test_cases(r.get("tc_dicts", []))
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
        _store_checklist(r.get("cl_dicts", []))
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
            # Read the knob inside the request, not inside the worker —
            # the worker runs on a JobQueue thread with no request context.
            sync_tc_format = _requested_tc_format()

            def _sync_worker(raw_lines=sync_raw_lines,
                             custom_prompt=sync_custom_prompt,
                             pid=sync_pid,
                             tc_format=sync_tc_format):
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
                                               raw_lines=raw_lines,
                                               tc_format=tc_format)
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

                # Stamp the combined list — the legacy template stream
                # and the site-aware stream both belong to the format the
                # operator asked for. _run_site_aware already stamped its
                # own half; re-stamping is idempotent.
                tcd = _gherkin.apply_format(tcd, tc_format)

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
            _store_test_cases(r.get("tc_dicts", []))
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
                        "continued on available data: %(errors)s"
                    ) % {"errors": "; ".join(_crawl_errors[:3])},
                    "warning",
                )

            tc_list = reconstruct_test_cases(r.get("tc_dicts", []))
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
            _store_test_cases(tc_dicts)
            session["traceability_data"] = trace
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
        # One read. ``restored_from_db`` drives the "we found your work"
        # flash, so it has to mean "the session had none of this" — which
        # is exactly the cold-start case the message is for.
        had_in_session = bool(session.get("test_cases_data"))
        tc_data = _hydrate_from_db("tc")
        restored_from_db = bool(tc_data) and not had_in_session
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
                checklist_gaps: list[str] = []
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
                        # The builders work out what they left out and
                        # say so; until now the answer was collected
                        # here and dropped. `checklist_gaps` appeared
                        # once in the whole codebase — on the line that
                        # built it — so a thin sheet read as the whole
                        # product, which is the one thing the comment
                        # over that line says must not happen.
                        for gp in site_out.get("checklist_gaps") or []:
                            if gp and gp not in checklist_gaps:
                                checklist_gaps.append(gp)
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
                        "checklist_gaps": checklist_gaps,
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
            _store_checklist(r.get("cl_dicts", []))
            session["user_stories"] = r.get("stories", [])
            session["raw_requirements"] = r.get("raw_requirements", [])

            # Surface partial crawler failures — same logic as /test-cases.
            _crawl_errors = r.get("crawl_errors") or []
            if _crawl_errors:
                flash(
                    g.t.get(
                        "crawl_partial",
                        "Some pages could not be crawled — generation "
                        "continued on available data: %(errors)s"
                    ) % {"errors": "; ".join(_crawl_errors[:3])},
                    "warning",
                )

            # What the builders could not cover. Deliberately not
            # truncated the way crawl_errors is: a report of what was
            # missed that itself misses things reads as complete, which
            # is the failure it exists to prevent. Long lists get a
            # count instead of a silent cut.
            _cl_gaps = [gp for gp in (r.get("checklist_gaps") or []) if gp]
            if _cl_gaps:
                shown, rest = _cl_gaps[:3], _cl_gaps[3:]
                tail = ("" if not rest else
                        " " + g.t.get(
                            "checklist_gaps_more",
                            "(+%(n)d more not listed)") % {"n": len(rest)})
                flash(
                    g.t.get(
                        "checklist_gaps_partial",
                        "Some checks could not be derived — the sheet is "
                        "thinner than the site: "
                    ) + "; ".join(shown) + tail,
                    "warning",
                )

            cl_list = reconstruct_checklist(r.get("cl_dicts", []))
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
            _store_checklist(cl_dicts)
            return render_template("checklist.html", checklist=cl_list,
                                   has_data=True, errors=errors,
                                   resource_urls=extract_resource_urls())

        # GET — drain any background checklist job that finished
        # after the sync POST returned. Same bug as the TC path.
        _drain_cl_job_into_session()
        # Same shape as the /test-cases GET above.
        had_in_session = bool(session.get("checklist_data"))
        cl_data = _hydrate_from_db("cl")
        restored_from_db = bool(cl_data) and not had_in_session
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
                "Unsupported file type ‘%(ext)s’. Use one of: xlsx, csv, "
                "md, json.") % {"ext": ext})
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
            mapping = _import_mapping("test_cases")
            # Read while the file still exists: the finally below unlinks it,
            # and the mapping report needs the header row. Measured — without
            # this the report said "no header row" for every file.
            headers = _import_headers(path, filename)
            cases = import_parse_test_cases(path, filename, mapping=mapping)
        except Exception as exc:
            flash(f"Import failed: {exc}", "error")
            return redirect(_back_to_caller(default="test_cases_page"))
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        if not cases:
            # E4.8: say *why*. A file whose columns are called "Scenario" and
            # "Actions" is not a broken file, and "0 rows" is not a diagnosis.
            analysis = _flash_import_mapping("test_cases", headers, mapping)
            flash(analysis.message(), "error")
            return redirect(_back_to_caller(default="test_cases_page"))

        mode = (request.form.get("upload_mode") or "replace").lower()
        # An append is a read-modify-write; a replace is not, and asking
        # for a version there would refuse an upload the user explicitly
        # asked to overwrite with.
        pack_v = pack_version("test_cases") if mode == "append" else None
        existing = _tc_rows() if mode == "append" else []
        incoming = [tc_to_dict(tc) for tc in cases]
        skipped: list[str] = []
        if mode == "append":
            # E4.8: uploading the same file twice used to double the pack, and
            # E4.4a's uniqueness pass would renumber the copies — so the
            # duplicates looked like new work.
            from engine import import_preview
            incoming, skipped = import_preview.dedup(existing, incoming)
        merged = existing + incoming
        try:
            _store_test_cases(merged, expected_version=pack_v)
        except _db.WriteConflict as exc:
            _log.info("test-case upload conflict: %s", exc)
            return _conflict_response(exc)
        # Imported packs don't carry their own user stories, so reset
        # the traceability matrix — it would otherwise reference IDs
        # that no longer exist.
        session.pop("traceability_data", None)

        # Three keys, because the operator reads one sentence. The two
        # fragments used to be appended as English f-strings *outside* the
        # ``t.get``, so they stayed English whatever the dictionary said.
        flash(
            g.t.get("upload_tc_ok",
                    "Imported %(n)d test case(s) from %(file)s.")
            % {"n": len(incoming), "file": filename}
            + (g.t.get("upload_total_now", " Total now: %(n)d.")
               % {"n": len(merged)} if mode == "append" else "")
            + (g.t.get("upload_skipped",
                       " Skipped %(n)d already in this project (%(ids)s).")
               % {"n": len(skipped), "ids": ", ".join(skipped[:5])}
               if skipped else ""),
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
        # Read the version with the pack: everything between here and
        # the write below is a read-modify-write, which is exactly
        # where a lost update comes from.
        pack_v = pack_version("test_cases")
        tc_data = _tc_rows()
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
        # Whole-pack write: save_test_cases is wipe-and-replace and
        # ``tc_data`` is the pack as it now stands, edit included. Guarded by
        # the version read above, so a colleague's concurrent change becomes
        # a 409 instead of a silent deletion of their rows.
        try:
            _store_test_cases(tc_data, expected_version=pack_v)
        except _db.WriteConflict as exc:
            _log.info("walkthrough-meta conflict on %s: %s", tc_id, exc)
            return _conflict_response(exc)

        if request.accept_mimetypes.best == "application/json":
            return jsonify({
                "id": tc_id,
                "url_pattern": url_pattern,
                "trigger": trigger,
            })
        flash(
            g.t.get("tc_walkthrough_meta_saved",
                    "Walkthrough binding for %(tc)s saved.")
            % {"tc": tc_id},
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

        # Read the version with the pack: everything between here and
        # the write below is a read-modify-write, which is exactly
        # where a lost update comes from.
        pack_v = pack_version("test_cases")
        tc_data = _tc_rows()
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

        # Resave for the live view and for the runner's next pass.
        target["automation_steps_json"] = _json.dumps(steps, ensure_ascii=False)
        try:
            _store_test_cases(tc_data, expected_version=pack_v)
        except _db.WriteConflict as exc:
            _log.info("step-kind conflict on %s: %s", tc_id, exc)
            return _conflict_response(exc)

        # resolve_active_project(), not session["active_project_id"] —
        # which has never been a session key. It is a template variable
        # (routes/_shared.get_picker_context sets it from
        # session["project_id"]), so this read was always "" and the
        # update_tc_automation_steps call below never ran. The step kind
        # reached the live view and never the runner it was for, which is
        # the whole point of the endpoint.
        pid = resolve_active_project()
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

    @app.route("/recorder/extension.zip", methods=["GET"])
    def recorder_extension_zip():
        """Serve the recorder extension as a zip the tester can unpack.

        Why this route exists at all: the install instructions said
        "select the ``extension/`` folder from your TestForTge checkout",
        and a tester who only has the web app has no checkout. The
        extension is not optional decoration — without it the "Start
        session recording" button mints a token, opens a tab, and nothing
        ever reads it, because the capture happens in the extension's
        content-script. So the one documented way to make that button
        work required being a developer.

        What this does NOT do, and the UI says so: Chrome cannot install
        a zip. The tester still unpacks it and uses Developer mode →
        Load unpacked. A .crx would be installable, but Chrome refuses
        CRX files that did not come from the Web Store, so it would trade
        an honest two-step install for one that silently fails. This
        removes the checkout, not the pilot.

        Gated on RECORDER_ENABLED like every other recorder surface, so a
        host outside the pilot does not serve a download for a feature it
        has switched off.
        """
        if not _recorder_enabled():
            return jsonify({"error": "recorder_disabled"}), 403

        root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "extension")
        if not os.path.isdir(root):
            # A deployment that shipped without the folder. 404 rather
            # than an empty archive: a zip with nothing in it would look
            # like a broken extension rather than a missing one.
            _log.warning("extension folder not found at %s", root)
            return jsonify({"error": "extension_not_bundled"}), 404

        payload, stamp = _extension_archive(root)
        from flask import send_file
        import io as _io2
        response = send_file(
            _io2.BytesIO(payload), mimetype="application/zip",
            as_attachment=True, download_name="testfortge-recorder.zip",
            max_age=0)
        # The tester re-downloads after an update, and a cached copy of
        # yesterday's extension is the kind of bug nobody thinks to
        # suspect. ETag off the folder's newest mtime.
        response.set_etag(f"ext-{stamp}")
        return response

    @app.route("/api/recorder-session/start", methods=["POST", "OPTIONS"])
    @_perm.require_role("user")
    def api_recorder_session_start():
        """Mint a fresh recording token for the active project.

        **Session-authenticated, unlike its sibling.** This route sat in
        ``route_policy.OPEN`` with the reason "extension token auth", and it
        is the one route on the recorder surface with no token to check —
        it is the route that *mints* the token. The extension does not call
        it either: ``extension/popup.js`` says so out loud and routes Start
        through the page precisely because the project comes from the
        session cookie a cross-site fetch could not carry. So the only
        caller is a signed-in, same-origin page, and until this decorator
        the consequence was measured, not supposed: an anonymous caller who
        knew a project id got a token for it, posted steps to ``/finish``,
        and saved a test case into that organisation's project through the
        review flow. Four requests, no credentials. See
        ``tests/test_recorder_token_scope.py``.

        Called from the /test-cases trigger button. The token returned
        is appended to the SUT URL as ``#testfortge-recorder-token=<t>``
        so the extension's content-script can pick it up on the next
        page load without needing the operator to copy-paste anything.

        Body (JSON, optional):
          ``{"project_id": "<pid>"}`` — overrides the session's active
          project. Falls back to ``session['project_id']`` when omitted
          (the usual path from the TestForTge UI button). The docstring
          said ``active_project_id`` until E3.3; that has never been a
          session key, only a template variable derived from this one.

        Returns ``{token, project_id, finish_url, review_url_template}``
        on success. 403 when RECORDER_ENABLED is off, 400 when neither
        the body nor session carries a project_id.
        """
        if request.method == "OPTIONS":
            return _recorder_cors_preflight()
        if not _recorder_enabled():
            return _json_with_cors({"error": "recorder_disabled"}, 403)
        payload = request.get_json(silent=True) or {}
        # resolve_active_project(), not session["project_id"] — the same
        # correction line 1595 already carries, in a route written
        # without it. The session key is empty on any request whose
        # session did not set it: a fresh sign-in, or the free plan
        # wiping the filesystem session store on restart. The project
        # itself is in Postgres and the picker renders it correctly, so
        # the page showed an active project while this endpoint answered
        # "no_active_project" — found by walking the recorder end to end
        # on staging, which nothing had done before.
        pid = (payload.get("project_id") or "").strip()
        if pid:
            # A caller-named project is a caller-named *write capability*,
            # so it is checked rather than taken. Nothing sends it today —
            # the page posts an empty body — but it is documented above, and
            # a documented parameter that skips the gate every other
            # project-scoped write honours is the gap itself.
            from ._shared import belongs_to_another_org
            if belongs_to_another_org(pid) or _db.get_project(pid) is None:
                return _json_with_cors({"error": "unknown_project"}, 404)
        else:
            pid = resolve_active_project()
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
        active_pid = resolve_active_project()
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

    @app.route("/test-cases/review-session/<token>/discard",
               methods=["POST"])
    def test_cases_review_session_discard(token: str):
        """Throw a recording away, for real.

        The control offering this was an ``<a href="/test-cases">`` — it
        navigated away and discarded nothing, while its label said
        "Cancel — discard recording". The draft stayed in the pending
        banner, still openable and still savable by anyone holding the
        link, for the whole 24 h TTL. An operator who pressed it and saw
        the recording still listed had no way to read that except as the
        product not working.

        Sealing rather than deleting: ``consume_session_draft`` is the
        same call the save path makes, so a discarded recording behaves
        exactly like a saved one — gone from the banner, refusing a
        replay of the review URL — and the row remains for the sweeper
        and for anyone asking what happened to a capture.
        """
        if not _recorder_enabled():
            return jsonify({"error": "recorder_disabled"}), 403

        draft = _db.get_session_draft(token)
        if draft is None:
            # Already discarded, already saved, or expired. Not an error
            # worth a page: the operator wanted it gone and it is gone.
            flash("That recording is no longer pending.", "info")
            return redirect(url_for("test_cases_page"))

        # Same scope check as the save path: a draft belongs to the
        # project it was captured in, and a stale link must not reach
        # into whichever project happens to be active now.
        active_pid = resolve_active_project()
        if active_pid and active_pid != draft["project_id"]:
            return jsonify({"error": "wrong_project"}), 403

        _db.consume_session_draft(token)
        from engine import permissions as _perm
        _db.append_audit(entity="session_draft", action="discard",
                         org_id=_perm.current_org_id(),
                         user_id=_perm.current_user_id(),
                         diff={"project_id": draft["project_id"]})
        flash("Recording discarded. Nothing was added to the pack.",
              "success")
        return redirect(url_for("test_cases_page"))

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

        # A *mismatch* guard, not an ownership guard, and the difference is
        # load-bearing: ``active_pid`` is empty for a caller who has no
        # project, so the condition never fires for one — which is every
        # anonymous caller, and this route is deliberately open ("the token
        # IS the credential", for a browser that may never sign in).
        # Ownership therefore lives upstream, where the recording token is
        # minted: /api/recorder-session/start is role-gated and checks a
        # caller-named project, so a draft can only name a project its
        # creator was entitled to. Do not read this line as the boundary.
        active_pid = resolve_active_project()
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

        # Surface the additions on /test-cases immediately, without a
        # manual project switch. The rows were just written above, so this
        # is a cache refresh rather than a save — and a no-op once the
        # database is the source of truth.
        _workspace.invalidate(pid, "test_cases")
        _mirror("test_cases_data", _workspace.test_cases(pid))

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
            # ``errors`` names the files the parser could not use, and
            # this is the one place the operator will look for it. It was
            # destructured here and dropped: uploading a .doc produced
            # ".doc format is not supported directly. Please save the
            # file as .docx", which nothing rendered — so the answer to
            # "here is my file" was "Please enter requirements or upload
            # files.", which is not true and hides a fixable reason.
            return jsonify({
                "error": "no_input",
                "message": _no_input_message(errors)}), 400

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
        # Same reason as the sync path: the worker thread has no request
        # context, so the knob is read here and closed over.
        wanted_format = _requested_tc_format()

        def _worker(raw_lines=raw_lines, custom_prompt=custom_prompt, pid=pid,
                    tc_format=wanted_format):
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
                                           raw_lines=raw_lines,
                                           tc_format=tc_format)
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
            tc_dicts = _gherkin.apply_format(tc_dicts, tc_format)
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
        # Same reason as _hydrate_from_db: a lost job is often reported
        # right after the restart that wiped the session.
        pid = resolve_active_project()
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
            _store_test_cases(r.get("tc_dicts", []))
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
            # ``errors`` names the files the parser could not use, and
            # this is the one place the operator will look for it. It was
            # destructured here and dropped: uploading a .doc produced
            # ".doc format is not supported directly. Please save the
            # file as .docx", which nothing rendered — so the answer to
            # "here is my file" was "Please enter requirements or upload
            # files.", which is not true and hides a fixable reason.
            return jsonify({
                "error": "no_input",
                "message": _no_input_message(errors)}), 400

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
            _store_checklist(r.get("cl_dicts", []))
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
            mapping = _import_mapping("checklist")
            # Read while the file still exists: the finally below unlinks it,
            # and the mapping report needs the header row. Measured — without
            # this the report said "no header row" for every file.
            headers = _import_headers(path, filename)
            items = import_parse_checklist(path, filename, mapping=mapping)
        except Exception as exc:
            flash(f"Import failed: {exc}", "error")
            return redirect(_back_to_caller(default="checklist_page"))
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        if not items:
            analysis = _flash_import_mapping("checklist", headers, mapping)
            flash(analysis.message(), "error")
            return redirect(_back_to_caller(default="checklist_page"))

        mode = (request.form.get("upload_mode") or "replace").lower()
        pack_v = pack_version("checklist") if mode == "append" else None
        existing = _cl_rows() if mode == "append" else []
        incoming = [cl_to_dict(it) for it in items]
        skipped: list[str] = []
        if mode == "append":
            from engine import import_preview
            incoming, skipped = import_preview.dedup(existing, incoming)
        merged = existing + incoming
        try:
            _store_checklist(merged, expected_version=pack_v)
        except _db.WriteConflict as exc:
            _log.info("checklist upload conflict: %s", exc)
            return _conflict_response(exc, redirect_to="checklist_page")

        flash(
            g.t.get("upload_cl_ok",
                    "Imported %(n)d checklist item(s) from %(file)s.")
            % {"n": len(incoming), "file": filename}
            + (g.t.get("upload_total_now", " Total now: %(n)d.")
               % {"n": len(merged)} if mode == "append" else "")
            + (g.t.get("upload_skipped",
                       " Skipped %(n)d already in this project (%(ids)s).")
               % {"n": len(skipped), "ids": ", ".join(skipped[:5])}
               if skipped else ""),
            "success",
        )
        return redirect(_back_to_caller(default="checklist_page"))

    @app.route("/export/<fmt>")
    def export(fmt):
        stories = reconstruct_stories(session.get("user_stories", []))
        tc_list = reconstruct_test_cases(_tc_rows())
        cl_list = reconstruct_checklist(_cl_rows())
        # Fall back to the project's stored pack when the session has
        # none — after a cold start, or in a second tab, the artefacts are
        # in Postgres and only there. /automation/bundle.zip already did
        # this; export did not, so the same project exported an empty file
        # from one tab and a full one from another.
        _pid = resolve_active_project(session)
        if _pid:
            if not tc_list:
                try:
                    tc_list = reconstruct_test_cases(_db.load_test_cases(_pid))
                except Exception as exc:  # pragma: no cover — best-effort
                    _log.warning("export: TC reload failed: %s", exc)
            if not cl_list:
                try:
                    cl_list = reconstruct_checklist(_db.load_checklist(_pid))
                except Exception as exc:  # pragma: no cover — best-effort
                    _log.warning("export: CL reload failed: %s", exc)

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
                            headers={"Content-Disposition":
                                     attachment_header(
                                         f"testfortge_{name}", ".md")})
        if fmt == "html":
            content = export_html(name, stories, tc_list, cl_list, trace, {})
            return Response(content, mimetype="text/html",
                            headers={"Content-Disposition":
                                     attachment_header(
                                         f"testfortge_{name}", ".html")})
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
        if fmt == "feature":
            # One .feature file per section, zipped. Only automation-
            # targeted cases go in: a manual-only pack has nothing for a
            # runner to bind, and shipping an empty archive reads as a
            # failure rather than as "you did not ask for BDD".
            targeted = [tc for tc in tc_list
                        if _gherkin.is_automation_targeted(tc)]
            if not targeted:
                return ("No automation-targeted test cases in this pack. "
                        "Regenerate with the BDD format selected, or switch "
                        "individual cases to BDD in the editor.", 409)
            content = _feature_archive(targeted, name)
            return Response(
                content, mimetype="application/zip",
                headers={"Content-Disposition":
                         attachment_header(f"{name}_features", ".zip")})
        return "Unknown format", 400


#: Cached extension archive, keyed on the folder's newest mtime. The
#: folder is ~114 KB and changes only on deploy, so rebuilding it per
#: download is pure waste; keying on mtime rather than caching forever
#: means a redeploy is picked up without a restart.
_EXTENSION_CACHE: dict[str, tuple[bytes, str]] = {}

#: Never shipped to a tester: editor droppings and OS metadata that would
#: make Chrome's "Load unpacked" complain about unexpected files.
_EXTENSION_SKIP = ("__pycache__", ".DS_Store", "Thumbs.db", ".gitkeep")


def _extension_archive(root: str) -> tuple[bytes, str]:
    """Zip the extension folder. Returns ``(bytes, mtime_stamp)``.

    Paths inside the archive are rooted at ``testfortge-recorder/`` so
    unzipping produces one folder to point Chrome at, rather than
    scattering manifest.json and friends into the tester's Downloads.
    """
    import io as _io
    import zipfile

    files: list[tuple[str, str]] = []
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXTENSION_SKIP
                       and not d.startswith(".")]
        for name in sorted(filenames):
            if name in _EXTENSION_SKIP or name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            files.append((full, f"testfortge-recorder/{rel}"))
            try:
                newest = max(newest, os.path.getmtime(full))
            except OSError:      # pragma: no cover — raced with a deploy
                pass

    stamp = f"{newest:.0f}-{len(files)}"
    cached = _EXTENSION_CACHE.get(stamp)
    if cached is not None:
        return cached

    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for full, arcname in files:
            zf.write(full, arcname)
    payload = buf.getvalue()
    # One entry only: the stamp changes on deploy and the old bytes are
    # dead weight in a 512 MB dyno.
    _EXTENSION_CACHE.clear()
    _EXTENSION_CACHE[stamp] = (payload, stamp)
    return payload, stamp


def _feature_archive(cases: list, project_name: str) -> bytes:
    """Zip of one ``.feature`` per section, plus a README naming the gaps.

    The Gherkin is derived here rather than read from the column, so the
    archive always matches the manual columns the client signed off — see
    :func:`engine.gherkin.ensure_gherkin` for why the column holds only
    hand-edited text.
    """
    import io as _io
    import zipfile

    buf = _io.BytesIO()
    findings: list[str] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for feature in _gherkin.features_from_test_cases(cases):
            text = feature.render()
            issues = _gherkin.lint(text)
            if issues:
                findings.append(f"{feature.name}: " + "; ".join(issues))
            zf.writestr(f"features/{_gherkin.feature_filename(feature.name)}",
                        text)
        readme = [
            f"# {project_name} — BDD feature files",
            "",
            f"{len(cases)} automation-targeted test cases, "
            f"grouped into one .feature per section.",
            "",
            "Generated from the manual test cases, which stay the source of "
            "truth. Re-export after editing a case rather than editing a "
            ".feature by hand — a .feature that drifts from the signed-off "
            "case is worse than none.",
        ]
        if findings:
            readme += ["", "## Findings", ""]
            readme += [f"- {f}" for f in findings]
        zf.writestr("README.md", "\n".join(readme) + "\n")
    return buf.getvalue()


__all__ = ["register"]

