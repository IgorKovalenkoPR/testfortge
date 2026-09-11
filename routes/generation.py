"""TestFortge — Test-case / checklist generation + export routes.

  * GET/POST /test-cases   — generate + display test cases
  * GET/POST /checklist    — generate + display checklist
  * GET /export/<fmt>      — markdown/html/csv/xlsx export

The Web Recorder left for :mod:`routes.recorder` — its extension build,
its token-authenticated endpoints and the capture-review pipeline. It was
a different surface wearing the same file: a Chrome extension posting
cross-origin with its own token, against a product page posting a form.
Measured before the cut, those eight routes borrowed exactly one helper
from everything defined here.

``recorder_enabled`` is imported back, because
``/test-cases/<id>/automation-step-kind`` belongs to the generation flow
and is gated on the same pilot flag.
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
# The pilot flag the recorder owns. One route here shares it —
# see the module docstring.
from .recorder import recorder_enabled
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
        if not recorder_enabled():
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

