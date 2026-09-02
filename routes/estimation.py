"""TestFortge — QA effort estimation routes.

  * GET  /estimation         — form + last result
  * POST /estimation/run     — compute estimation from URL / file / text
  * GET  /estimation/export  — XLSX workbook download

Input sources (first wins): URL crawl → uploaded attachment → pasted text.
User-tunable coefficients (compat rate, bug rate, PM overhead, stretch)
override the defaults; out-of-range values are clipped, not rejected.
"""

from __future__ import annotations

import os
import re
from datetime import datetime

from flask import (Flask, current_app, flash, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)
from werkzeug.utils import secure_filename

from engine.log import get_logger
from engine.i18n import get_lang
from engine.qa_estimator import (
    Feature, compute_estimation,
    export_estimation_xlsx,
)
from engine.estimation_service import (
    EstimationInput, run_estimation,
)
from engine.file_parser import allowed_file
from engine.job_queue import get_queue, DONE, FAILED

from engine import db as _db

from .automation import STORAGE_ROOT, MAX_CONCURRENT_JOBS_PER_SESSION

class _DictShim:
    """Minimal attribute carrier so :func:`_historical_calibration` can
    read ``.features`` / ``.full_total_expected`` off a plain result
    dict without us rebuilding the full ``EstimationResult`` dataclass."""

    class _Feat:
        __slots__ = ("is_section",)

        def __init__(self, d: dict) -> None:
            self.is_section = bool(d.get("is_section"))

    def __init__(self, result_dict: dict) -> None:
        self.features = [self._Feat(f) for f in result_dict.get("features", [])]
        self.full_total_expected = float(
            result_dict.get("full_total_expected") or 0.0)
        self.history_hint = None
        self.history_median_hours_per_feature = None
        self.history_sample_size = None


def _historical_calibration(result, owner_sid: str) -> None:
    """Decorate ``result`` with a soft hint comparing it to the user's
    past estimations of similar size. Never modifies the numbers — only
    populates ``history_hint`` / ``history_median_hours_per_feature`` /
    ``history_sample_size`` so the UI can show a sentence like
    "Past 5 projects averaged 3.2 h/feature; this one is 5.8 h/feature".

    Skips silently when fewer than 3 comparable past estimations exist
    (a single data point is noise, not signal).
    """
    if not owner_sid:
        return
    feat_n = sum(1 for f in (result.features or []) if not f.is_section)
    if feat_n <= 0:
        return
    try:
        peers = _db.list_estimations_by_owner(
            owner_sid=owner_sid, similar_features=feat_n, tolerance=0.4,
            limit=12,
        )
    except Exception as exc:  # pragma: no cover — best-effort
        log.warning("history calibration query failed: %s", exc)
        return

    if len(peers) < 3:
        return

    ratios = []
    for est in peers:
        ihrs = est.get("total_hours") or 0.0
        ifeat = (est.get("input_payload") or {}).get("features_count") or 0
        if ifeat and ihrs:
            ratios.append(ihrs / ifeat)
    if len(ratios) < 3:
        return

    ratios.sort()
    median = ratios[len(ratios) // 2]
    current = result.full_total_expected / max(1, feat_n)
    deviation = (current - median) / median if median else 0.0

    result.history_median_hours_per_feature = round(median, 2)
    result.history_sample_size = len(ratios)
    if abs(deviation) < 0.30:
        result.history_hint = (
            f"In line with {len(ratios)} similar past projects "
            f"(~{median:.1f} h/feature)."
        )
    elif deviation > 0:
        result.history_hint = (
            f"Higher than {len(ratios)} similar past projects: "
            f"history median ~{median:.1f} h/feature, this estimate "
            f"~{current:.1f} h/feature ({deviation*100:+.0f}%). "
            "Consider whether this project genuinely has more risk / "
            "scope, or revisit the per-TC minutes / buffer."
        )
    else:
        result.history_hint = (
            f"Lower than {len(ratios)} similar past projects: "
            f"history median ~{median:.1f} h/feature, this estimate "
            f"~{current:.1f} h/feature ({deviation*100:+.0f}%). "
            "Make sure no testing phase was missed (compatibility, "
            "regression, bug rechecks)."
        )

from ._shared import (ensure_active_project, get_session_id,
                      mirror_pack as _mirror_pack,
                      pack_estimation as _pack_estimation)


def _persist_estimation(input_payload: dict, result_dict: dict) -> None:
    """Append an estimation snapshot to the project's history table.

    Best-effort: a DB outage must not break the user-facing flow,
    so we log + swallow on failure. Each compute = a new row."""
    pid = ensure_active_project()
    if not pid:
        return
    # Pick a single canonical hour figure for the column, falling back
    # in priority order. None when the result_dict is unrecognisable.
    total_hours = (
        result_dict.get("full_total_expected")
        or result_dict.get("one_plat_total_expected")
        or result_dict.get("total_hours")
    )
    try:
        _db.save_estimation(
            pid,
            input_payload=input_payload or {},
            result_payload=result_dict or {},
            total_hours=float(total_hours) if total_hours is not None else None,
        )
    except Exception as exc:  # pragma: no cover — best-effort
        log.warning("persist estimation failed: %s", exc)

log = get_logger(__name__)

ESTIMATION_DIR = os.path.join(STORAGE_ROOT, "estimations")
os.makedirs(ESTIMATION_DIR, exist_ok=True)

_DEFAULT_COMPAT_PLATFORMS = [
    "Windows 11", "Apple MacBook Air 2025", "Apple MacBook Pro",
    "MacBook Air 13 256Gb 2020", "Mac Mini 2018",
    "iPhone 16 Pro Max iOS 18", "iPhone 15 iOS 17",
    "iPhone 16 iOS 18.6", "iPad (9th generation) iOS 18",
]


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def register(app: Flask) -> None:
    def _mockup_env_state() -> dict:
        """Detect what the Mockups tab can actually do given the
        deployment's current environment. Drives an inline banner in
        the template so the operator sees missing prerequisites
        BEFORE clicking Run instead of after a failed pass.

        Cheap (no network calls): we only check env-var presence and
        whether two Python imports succeed. Result is rendered as a
        coloured chip per capability.

        Wrapped in a defensive try/except — a failure here used to
        500 the entire /estimation page (operator-reported after my
        2026-05-04 deploy: URL-tab POST landed on a redirect to GET,
        but GET 500'd if anything inside this helper raised).
        """
        try:
            import os
            state = {
                "anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "figma_pat":     bool(os.environ.get("FIGMA_PAT")),
                "pdf2image_ok":  False,
                "poppler_ok":    False,
                "vision_model":  os.environ.get("ANTHROPIC_MODEL",
                                                 "claude-sonnet-4-5"),
            }
            try:
                import pdf2image  # noqa: F401
                state["pdf2image_ok"] = True
            except Exception:
                pass
            # Cheap probe — pdf2image needs `pdftoppm` from poppler-utils.
            try:
                import shutil as _sh
                state["poppler_ok"] = bool(_sh.which("pdftoppm"))
            except Exception:
                pass
            state["mockups_minimum"] = state["anthropic_key"]
            state["mockups_pdf"] = (state["anthropic_key"]
                                     and state["pdf2image_ok"]
                                     and state["poppler_ok"])
            state["mockups_figma_full"] = (state["anthropic_key"]
                                            and state["figma_pat"])
            return state
        except Exception as exc:
            log.warning("mockup env state probe failed: %s", exc)
            return {
                "anthropic_key": False, "figma_pat": False,
                "pdf2image_ok": False, "poppler_ok": False,
                "vision_model": "unknown",
                "mockups_minimum": False, "mockups_pdf": False,
                "mockups_figma_full": False,
                "_probe_error": str(exc)[:200],
            }

    @app.route("/estimation/diag", methods=["GET"])
    def estimation_diag():
        """Operator-facing JSON dump of what the Mockups tab can /
        cannot do under the current environment. Useful for support
        tickets — single URL the operator can share."""
        return jsonify(_mockup_env_state())

    def _drain_est_job_into_session() -> None:
        """If a previous async dispatch left an ``estimation_job_id`` in
        the session and that job is now DONE, copy its result into the
        session keys the GET render reads. No-op when the id is missing
        or the job is still pending. Mirrors :func:`_drain_tc_job_into_session`
        in routes/generation.py — the same safety net so a user who
        navigates away mid-run still sees their result on return."""
        job_id = session.get("estimation_job_id")
        if not job_id:
            return
        try:
            job = get_queue().get(job_id)
        except Exception:
            return
        if not job or job.kind != "estimation":
            return
        if job.status == DONE and job.result:
            try:
                result = dict(job.result)
                extracted = result.pop("_extracted_text", "")
                if extracted:
                    session["estimation_extracted_text"] = extracted
                _mirror_pack("estimation_result", result)
                _persist_estimation(
                    input_payload=getattr(job, "meta", {}) or {},
                    result_dict=result,
                )
            except Exception as exc:
                log.warning("drain estimation job: %s", exc)
            session.pop("estimation_job_id", None)
        elif job.status == FAILED:
            flash(
                "Estimation failed: " + (job.error or "unknown error"),
                "danger",
            )
            session.pop("estimation_job_id", None)

    @app.route("/estimation", methods=["GET"])
    def estimation_page():
        lang = session.get("lang", "en")
        t = get_lang(lang)
        # Drain any background-finished job before rendering — same
        # safety net the TC / CL pages use.
        try:
            _drain_est_job_into_session()
        except Exception as exc:
            log.debug("estimation drain skipped: %s", exc)
        return render_template(
            "estimation.html",
            t=t, lang=lang,
            result=_pack_estimation(),
            last=session.get("estimation_form", {}),
            mockup_env=_mockup_env_state(),
            # The form's bounds ARE the server's bounds. They were
            # hard-coded in the template and disagreed with the clamps
            # in ``_build_input`` in both directions: ``max="50"`` on a
            # field the server cuts to 30 without a word, and
            # ``max="60"`` on one the config allows 120 of, so raising
            # ``EST_MAX_MINUTES_PER_TC`` changed nothing an operator
            # could reach. Rendering the configured value means the
            # browser's own message states the real limit.
            limits=_form_limits(),
        )

    @app.route("/estimation/run", methods=["POST"])
    def estimation_run():
        # Top-level safety net — converts any unhandled exception into
        # a friendly flash + redirect instead of a 500 page.
        # Operator-reported 2026-05-04: URL-tab estimation hit a 500
        # with no clear cause; the inner code paths each have their
        # own try/except, but a fresh defensive wrapper here ensures
        # the user never sees a Flask traceback for any reason.
        try:
            return _estimation_run_inner()
        except Exception as exc:
            log.exception("estimation_run unhandled: %s", exc)
            # The diagnostics URL is admin-only (route_policy), so
            # telling a member to open it sends them to a 403 while they
            # are already looking at a failure.
            try:
                from engine import permissions as _perm_mod
                where = (" or open /estimation/diag for diagnostics"
                         if _perm_mod.is_admin() else "")
            except Exception:  # pragma: no cover — never break the flash
                where = ""
            flash(
                f"Estimation failed unexpectedly: "
                f"{type(exc).__name__} — {str(exc)[:200]}. "
                f"Try a different source (Text / Mockups / URL){where}.",
                "danger",
            )
            return redirect(url_for("estimation_page"))

    def _form_limits() -> dict[str, int]:
        """The maxima the form must offer, read from the same config the
        server clamps to.

        One source for both halves of the round trip. A number the form
        accepts is a number the server honours, and a number the form
        refuses is one the server would have refused too — neither of
        which was true while the ceilings lived in an HTML attribute.
        """
        return {
            "additional_platforms":
                int(current_app.config["EST_MAX_ADDITIONAL_PLATFORMS"]),
            "minutes_per_tc":
                int(current_app.config["EST_MAX_MINUTES_PER_TC"]),
            "buffer_percent":
                int(current_app.config["EST_MAX_BUFFER_PERCENT"]),
        }

    def _build_input(saved_attachment_path: str = "",
                     saved_mockup_paths: list[str] | None = None) -> EstimationInput:
        """Read clamped + normalised values from ``request.form`` into
        an :class:`EstimationInput`. Upload-saving stays in the route
        (only the route has request.files); pass the saved paths in."""

        def _ci(name: str, default: int, lo: int, hi: int) -> int:
            try:
                return int(_clamp(
                    int(request.form.get(name, str(default)) or default),
                    lo, hi))
            except ValueError:
                return default

        def _cf(name: str, default: float, lo: float, hi: float) -> float:
            try:
                return _clamp(
                    float(request.form.get(name, str(default)) or default),
                    lo, hi)
            except ValueError:
                return default

        project_name = (request.form.get("project_name", "").strip())[:120]
        rate_usd = _cf("rate_usd", 0.0, 0.0, 10_000.0)
        additional_platforms = _ci(
            "additional_platforms", 9,
            0, current_app.config["EST_MAX_ADDITIONAL_PLATFORMS"])
        minutes_per_tc = _ci(
            "minutes_per_tc", 5,
            1, current_app.config["EST_MAX_MINUTES_PER_TC"])
        buffer_percent = _cf(
            "buffer_percent", 12.0,
            0.0, float(current_app.config["EST_MAX_BUFFER_PERCENT"]))
        compat_percent = _cf("compatibility_rate", 0.3, 0.0, 100.0)
        bug_percent = _cf("bug_report_rate", 15.0, 0.0, 100.0)
        pm_percent = _cf("pm_overhead", 8.0, 0.0, 100.0)
        max_testing_stretch = _cf("max_testing_stretch", 1.5, 1.0, 10.0)
        team_size = _ci("team_size", 1, 1, 50)
        primary_platform = (
            request.form.get("primary_platform", "Windows 10").strip()
            or "Windows 10")

        return EstimationInput(
            source_choice=request.form.get("source", "text"),
            url=request.form.get("url", "").strip(),
            text_input=request.form.get("text_input", "").strip(),
            figma_url=request.form.get("figma_url", "").strip(),
            mockup_context=request.form.get("mockup_context", "").strip(),
            attachment_path=saved_attachment_path,
            mockup_paths=list(saved_mockup_paths or []),
            project_name=project_name,
            rate_usd=rate_usd,
            additional_platforms=additional_platforms,
            minutes_per_tc=minutes_per_tc,
            buffer=1.0 + buffer_percent / 100.0,
            primary_platform=primary_platform,
            compatibility_rate=compat_percent / 100.0,
            bug_report_rate=bug_percent / 100.0,
            pm_overhead=pm_percent / 100.0,
            max_testing_stretch=max_testing_stretch,
            team_size=team_size,
        )

    def _snapshot_form(inp: EstimationInput) -> dict:
        """Mirror EstimationInput back to the percent-shaped dict that
        the template re-reads on the next GET (so the operator's last
        values stick in the form)."""
        return {
            "project_name": inp.project_name, "rate_usd": inp.rate_usd,
            "additional_platforms": inp.additional_platforms,
            "minutes_per_tc": inp.minutes_per_tc,
            "buffer_percent": int(round((inp.buffer - 1.0) * 100)),
            "compatibility_rate": inp.compatibility_rate * 100.0,
            "bug_report_rate": inp.bug_report_rate * 100.0,
            "pm_overhead": inp.pm_overhead * 100.0,
            "max_testing_stretch": inp.max_testing_stretch,
            "team_size": inp.team_size,
            "primary_platform": inp.primary_platform,
            "url": inp.url, "text_input": inp.text_input,
            "figma_url": inp.figma_url, "mockup_context": inp.mockup_context,
            "source": inp.source_choice,
        }

    def _save_uploaded_attachment() -> str:
        up = request.files.get("attachment")
        if not (up and up.filename and allowed_file(up.filename)):
            return ""
        safe_name = secure_filename(up.filename) or "upload.bin"
        save_path = os.path.join(
            current_app.config["UPLOAD_FOLDER"], safe_name)
        up.save(save_path)
        return save_path

    def _save_uploaded_mockups(flash_warnings: bool = False) -> list[str]:
        out: list[str] = []
        upload_dir = current_app.config["UPLOAD_FOLDER"]
        for up in request.files.getlist("mockup_files"):
            if not up or not up.filename:
                continue
            if not allowed_file(up.filename):
                if flash_warnings:
                    flash(f"Skipped unsupported file: {up.filename}",
                          "warning")
                continue
            safe = secure_filename(up.filename) or "mockup.bin"
            p = os.path.join(upload_dir, safe)
            up.save(p)
            out.append(p)
        return out

    def _estimation_run_inner():
        source_choice = request.form.get("source", "text")
        att_path = ""
        mockup_paths: list[str] = []
        if source_choice in ("text", "attachment"):
            att_path = _save_uploaded_attachment()
        elif source_choice == "mockups":
            mockup_paths = _save_uploaded_mockups(flash_warnings=True)

        inp = _build_input(
            saved_attachment_path=att_path,
            saved_mockup_paths=mockup_paths,
        )
        session["estimation_form"] = _snapshot_form(inp)

        try:
            out = run_estimation(inp)
        except RuntimeError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("estimation_page"))
        for w in out.warnings:
            flash(w, "warning")

        # Historical calibration — annotate the result dict with a
        # ``history_hint`` comparing this project to past similar ones.
        # _historical_calibration reads .features and .full_total_expected
        # off an object; a tiny shim keeps it dict-driven without
        # rebuilding the full EstimationResult.
        try:
            shim = _DictShim(out.result_dict)
            _historical_calibration(shim, get_session_id())
            for k in ("history_hint", "history_median_hours_per_feature",
                      "history_sample_size"):
                v = getattr(shim, k, None)
                if v is not None:
                    out.result_dict[k] = v
        except Exception as exc:  # pragma: no cover — best-effort
            log.warning("history calibration skipped: %s", exc)

        _mirror_pack("estimation_result", out.result_dict)
        # Mirror the system-suggested team size back into the form
        # snapshot so the next GET render shows the badge with the
        # fresh recommendation rather than the user's last manual
        # value. The hidden override input is still pre-filled with
        # the suggestion, so a no-op submit keeps it stable.
        sug = out.result_dict.get("suggested_team_size")
        if sug:
            form_snapshot = session.get("estimation_form") or {}
            form_snapshot["suggested_team_size"] = int(sug)
            session["estimation_form"] = form_snapshot
        if out.extracted_text:
            session["estimation_extracted_text"] = out.extracted_text
        _persist_estimation(
            input_payload={
                "source": out.source_label,
                "source_ref": out.source_ref,
                "primary_platform": inp.primary_platform,
                "additional_platforms": inp.additional_platforms,
                "compatibility_rate": inp.compatibility_rate,
                "bug_report_rate": inp.bug_report_rate,
                "pm_overhead": inp.pm_overhead,
                "team_size": inp.team_size,
                "features_count": out.features_count,
                "max_testing_stretch": inp.max_testing_stretch,
            },
            result_dict=out.result_dict,
        )
        rd = out.result_dict
        flash(
            f"Estimation complete: {rd.get('total_tc', 0)} test cases, "
            f"{rd.get('one_plat_total_expected', 0):.1f}h (one platform), "
            f"{rd.get('full_total_expected', 0):.1f}h (full compatibility).",
            "success",
        )
        return redirect(url_for("estimation_page"))

    @app.route("/estimation/run-async", methods=["POST"])
    def estimation_run_async():
        """Submit estimation work to the background JobQueue and return
        a ``job_id`` immediately so the UI can show a progress modal
        and poll ``/estimation/status/<id>`` until done.

        File uploads are saved to UPLOAD_FOLDER inside this request
        BEFORE the job is submitted — the worker thread has no access
        to ``request.files``.
        """
        # Per-session concurrency cap (shared threshold with automation).
        sid = get_session_id(session)
        active = get_queue().count_active_by_meta(
            "estimation", "session_id", sid)
        _runs_cap = int(current_app.config.get(
            "MAX_CONCURRENT_RUNS", MAX_CONCURRENT_JOBS_PER_SESSION)
            or MAX_CONCURRENT_JOBS_PER_SESSION)
        limit = min(MAX_CONCURRENT_JOBS_PER_SESSION, _runs_cap)
        if active >= limit:
            resp = jsonify({
                "error": "rate_limited",
                "message": (f"You already have {active} active estimation "
                            f"jobs. Wait for them to finish before starting "
                            f"another."),
                "active": active,
                "limit": limit,
            })
            resp.status_code = 429
            resp.headers["Retry-After"] = "15"
            return resp

        source_choice = request.form.get("source", "text").strip()
        saved_attachment_path = ""
        saved_mockup_paths: list[str] = []
        if source_choice in ("text", "attachment"):
            saved_attachment_path = _save_uploaded_attachment()
        elif source_choice == "mockups":
            saved_mockup_paths = _save_uploaded_mockups()

        # Validation per source type. URL needs the URL; mockups needs
        # at least one image OR a Figma URL; text needs pasted text or
        # an attachment.
        url = request.form.get("url", "").strip()
        text_input = request.form.get("text_input", "").strip()
        figma_url = request.form.get("figma_url", "").strip()
        if source_choice == "url" and not url:
            return jsonify({
                "error": "no_url",
                "message": "URL tab is selected but no URL was provided.",
            }), 400
        if source_choice == "mockups" and not (saved_mockup_paths or figma_url):
            return jsonify({
                "error": "no_mockups",
                "message": "Upload at least one mockup or paste a Figma URL.",
            }), 400
        if (source_choice in ("text", "attachment")
                and not text_input and not saved_attachment_path):
            return jsonify({
                "error": "no_text",
                "message": "Paste requirements text or attach a document.",
            }), 400

        inp = _build_input(
            saved_attachment_path=saved_attachment_path,
            saved_mockup_paths=saved_mockup_paths,
        )
        session["estimation_form"] = _snapshot_form(inp)

        def _worker():
            out = run_estimation(inp)
            payload = dict(out.result_dict)
            if out.extracted_text:
                # Carried back into session via /estimation/status so
                # the "Generate test cases" CTA has the bullet text.
                payload["_extracted_text"] = out.extracted_text
            return payload

        job_id = get_queue().submit(
            "estimation", _worker,
            meta={
                "source": source_choice,
                "url": url,
                "project_name": inp.project_name,
                "session_id": sid,  # used by count_active_by_meta()
                "team_size": inp.team_size,
            },
        )
        session["estimation_job_id"] = job_id
        return jsonify({"job_id": job_id, "status": "pending"})

    @app.route("/estimation/status/<job_id>", methods=["GET"])
    def estimation_status(job_id):
        """Return current status of an estimation job.

        On DONE the worker's result is written to the session
        (``estimation_result`` and, for the Mockups path,
        ``estimation_extracted_text``) and a ``redirect_url`` is
        included in the JSON so the client navigates back to GET
        /estimation where the result template renders normally —
        matches the pattern Test Cases / Checklist generators use.

        On FAILED the error string is surfaced in the JSON so the
        client modal shows a friendly message instead of polling
        forever.
        """
        job = get_queue().get(job_id)
        if job is None or job.kind != "estimation":
            return jsonify({"error": "not_found"}), 404

        payload = job.to_public_dict()
        if job.status == DONE and job.result is not None:
            result = dict(job.result)  # don't mutate the queued payload
            extracted = result.pop("_extracted_text", "")
            _mirror_pack("estimation_result", result)
            if extracted:
                session["estimation_extracted_text"] = extracted
            session.pop("estimation_job_id", None)
            payload["result"] = result
            payload["redirect_url"] = url_for("estimation_page")
            # Mirror the result into the project's estimation history.
            try:
                _persist_estimation(
                    input_payload=getattr(job, "meta", {}) or {},
                    result_dict=result,
                )
            except Exception as exc:
                log.warning("persist on status drain failed: %s", exc)
        elif job.status == FAILED:
            session.pop("estimation_job_id", None)
        return jsonify(payload)

    @app.route("/estimation/to-test-cases", methods=["GET"])
    def estimation_to_test_cases():
        """Bridge: hand the current estimation's feature list to the
        /test-cases generator as pre-filled input.

        Source priority (matches the 3-tab UI):
          1. ``estimation_extracted_text`` — raw vision-extracted
             bullets from the Mockups tab. Highest fidelity.
          2. Feature names from the stored estimation result — works
             for the URL crawl + Requirements text tabs.
          3. The pasted text from the form snapshot — last-resort
             fallback so the operator never lands on an empty page.

        Stashed under ``session["prefill_input_text"]`` which the
        ``_input_block.html`` partial reads exactly once and then
        clears (see /test-cases GET).
        """
        prefill = (session.get("estimation_extracted_text") or "").strip()
        if not prefill:
            data = _pack_estimation() or {}
            features = data.get("features") or []
            if features:
                prefill = "\n".join(
                    f"* {f.get('name', '').strip()}"
                    for f in features
                    if f.get("name")
                )
        if not prefill:
            form = session.get("estimation_form") or {}
            prefill = (form.get("text_input") or "").strip()
        if not prefill:
            flash(
                "No estimation source to convert. Run an estimation "
                "first, then click again.",
                "warning",
            )
            return redirect(url_for("estimation_page"))
        session["prefill_input_text"] = prefill
        # Drop the raw vision text so we don't keep re-prefilling
        # from a stale run after the user navigates around.
        session.pop("estimation_extracted_text", None)
        return redirect(url_for("test_cases_page"))

    @app.route("/estimation/export", methods=["GET"])
    def estimation_export():
        data = _pack_estimation()
        if not data:
            flash("Nothing to export — run an estimation first.", "warning")
            return redirect(url_for("estimation_page"))

        # Re-build an EstimationResult-compatible object from stored features.
        features = [Feature(**f) for f in data.get("features", [])]
        result = compute_estimation(
            features=features,
            rate_usd=data.get("rate_usd", 0),
            additional_platforms=data.get("additional_platforms", 9),
            minutes_per_tc=data.get("minutes_per_tc", 5),
            buffer=data.get("buffer", 1.12),
            project_name=data.get("project_name", "Project"),
            primary_platform=data.get("primary_platform", "Windows 10"),
            platforms_list=(data.get("platforms_list")
                            or _DEFAULT_COMPAT_PLATFORMS[: data.get("additional_platforms", 9)]),
            source=data.get("source", "manual"),
            source_ref=data.get("source_ref", ""),
            compatibility_rate=data.get("compatibility_rate"),
            bug_report_rate=data.get("bug_report_rate"),
            pm_overhead=data.get("pm_overhead"),
            max_testing_stretch=data.get("max_testing_stretch"),
        )

        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", result.project_name or "project")[:40] or "project"
        fname = f"Estimation_{safe_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        out_path = os.path.join(ESTIMATION_DIR, fname)
        export_estimation_xlsx(result, out_path)
        return send_from_directory(ESTIMATION_DIR, fname, as_attachment=True)


__all__ = ["register", "ESTIMATION_DIR"]
