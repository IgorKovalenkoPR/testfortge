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
from dataclasses import asdict
from datetime import datetime

from flask import (Flask, current_app, flash, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)
from werkzeug.utils import secure_filename

from engine.log import get_logger
from engine.i18n import get_lang
from engine.qa_estimator import (
    Feature, compute_estimation,
    features_from_text, features_from_site_analysis,
    export_estimation_xlsx,
)
from engine.site_crawler import crawl_site
from engine.file_parser import parse_file, allowed_file
from engine.job_queue import get_queue, DONE, FAILED

from engine import db as _db

from .automation import STORAGE_ROOT, MAX_CONCURRENT_JOBS_PER_SESSION

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

from ._shared import get_session_id, ensure_active_project


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
                session["estimation_result"] = result
                if extracted:
                    session["estimation_extracted_text"] = extracted
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
            result=session.get("estimation_result"),
            last=session.get("estimation_form", {}),
            mockup_env=_mockup_env_state(),
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
            flash(
                f"Estimation failed unexpectedly: "
                f"{type(exc).__name__} — {str(exc)[:200]}. "
                "Try a different source (Text / Mockups / URL) or "
                "open /estimation/diag for diagnostics.",
                "danger",
            )
            return redirect(url_for("estimation_page"))

    def _estimation_run_inner():
        # Input clamping — keep user-supplied numbers inside sane bounds so
        # malformed or abusive values can't blow up computations or exports.
        project_name = (request.form.get("project_name", "").strip())[:120]
        try:
            rate_usd = _clamp(float(request.form.get("rate_usd", "0") or 0),
                              0.0, 10_000.0)
        except ValueError:
            rate_usd = 0.0
        try:
            additional_platforms = int(_clamp(
                int(request.form.get("additional_platforms", "9") or 9),
                0, current_app.config["EST_MAX_ADDITIONAL_PLATFORMS"]))
        except ValueError:
            additional_platforms = 9
        try:
            minutes_per_tc = int(_clamp(
                int(request.form.get("minutes_per_tc", "5") or 5),
                1, current_app.config["EST_MAX_MINUTES_PER_TC"]))
        except ValueError:
            minutes_per_tc = 5
        try:
            buffer_percent = _clamp(
                float(request.form.get("buffer_percent", "12") or 12),
                0.0, float(current_app.config["EST_MAX_BUFFER_PERCENT"]))
        except ValueError:
            buffer_percent = 12.0
        buffer = 1.0 + buffer_percent / 100.0

        # Per-run user-tunable coefficients. UI supplies them as percents
        # (compat/bug/PM) or a plain multiplier (stretch); default if missing.
        try:
            compat_percent = _clamp(
                float(request.form.get("compatibility_rate", "0.3") or 0.3),
                0.0, 100.0)
        except ValueError:
            compat_percent = 0.3
        compatibility_rate = compat_percent / 100.0
        try:
            bug_percent = _clamp(
                float(request.form.get("bug_report_rate", "15") or 15),
                0.0, 100.0)
        except ValueError:
            bug_percent = 15.0
        bug_report_rate = bug_percent / 100.0
        try:
            pm_percent = _clamp(
                float(request.form.get("pm_overhead", "8") or 8),
                0.0, 100.0)
        except ValueError:
            pm_percent = 8.0
        pm_overhead = pm_percent / 100.0
        try:
            max_testing_stretch = _clamp(
                float(request.form.get("max_testing_stretch", "1.5") or 1.5),
                1.0, 10.0)
        except ValueError:
            max_testing_stretch = 1.5

        primary_platform = request.form.get("primary_platform", "Windows 10").strip() or "Windows 10"
        source_choice = request.form.get("source", "url")
        url = request.form.get("url", "").strip()
        text_input = request.form.get("text_input", "").strip()
        figma_url = request.form.get("figma_url", "").strip()
        mockup_context = request.form.get("mockup_context", "").strip()

        session["estimation_form"] = {
            "project_name": project_name, "rate_usd": rate_usd,
            "additional_platforms": additional_platforms,
            "minutes_per_tc": minutes_per_tc, "buffer_percent": int(buffer_percent),
            "compatibility_rate": compat_percent,
            "bug_report_rate": bug_percent,
            "pm_overhead": pm_percent,
            "max_testing_stretch": max_testing_stretch,
            "primary_platform": primary_platform, "url": url, "text_input": text_input,
            "figma_url": figma_url, "mockup_context": mockup_context,
            "source": source_choice,
        }

        features: list = []
        source = "manual"
        source_ref = ""

        # ── Tab 1 — Requirements text ───────────────────────────
        # Combines pasted text + parsed-text-from-attachment so the
        # tester can mix sources within one tab. Both `attachment`
        # (legacy radio value) and `text` (new tab name) hit this
        # branch — backwards-compat with bookmarks.
        if source_choice in ("text", "attachment"):
            collected_lines: list[str] = []
            if text_input:
                collected_lines.append(text_input)
            up = request.files.get("attachment")
            if up and up.filename and allowed_file(up.filename):
                safe_name = secure_filename(up.filename) or "upload.bin"
                save_path = os.path.join(current_app.config["UPLOAD_FOLDER"], safe_name)
                up.save(save_path)
                lines, err = parse_file(save_path, safe_name)
                if err:
                    flash(f"Attachment parse warning: {err}", "warning")
                if lines:
                    collected_lines.extend(lines)
                source_ref = safe_name
            if collected_lines:
                features = features_from_text("\n".join(collected_lines))
                source = "text" if not source_ref else "attachment"
                if not source_ref:
                    source_ref = "pasted input"

        # ── Tab 2 — Mockups (vision pipeline) ────────────────────
        # Image / PDF uploads + optional Figma URL go through
        # engine.mockup_vision which calls Claude vision and emits
        # a feature-list bullet text consumed by features_from_text.
        elif source_choice == "mockups":
            try:
                from engine.mockup_vision import analyse as _vision_analyse
                saved_paths: list[str] = []
                upload_dir = current_app.config["UPLOAD_FOLDER"]
                for up in request.files.getlist("mockup_files"):
                    if not up or not up.filename:
                        continue
                    if not allowed_file(up.filename):
                        flash(f"Skipped unsupported file: {up.filename}",
                              "warning")
                        continue
                    safe = secure_filename(up.filename) or "mockup.bin"
                    p = os.path.join(upload_dir, safe)
                    up.save(p)
                    saved_paths.append(p)
                vres = _vision_analyse(
                    file_paths=saved_paths,
                    figma_url=figma_url,
                    context=mockup_context,
                )
                for w in (vres.warnings or []):
                    flash(w, "warning")
                if vres.error:
                    flash(vres.error, "danger")
                if vres.text:
                    features = features_from_text(vres.text)
                    source = "mockups"
                    label_bits = []
                    if saved_paths:
                        label_bits.append(f"{len(saved_paths)} file(s)")
                    if figma_url:
                        label_bits.append("Figma URL")
                    source_ref = (vres.source_label
                                   or " + ".join(label_bits)
                                   or "uploaded mockups")
                    # Stash the raw bullet text so the operator can
                    # review what the vision model extracted before
                    # generating test cases.
                    session["estimation_extracted_text"] = vres.text
            except Exception as exc:
                log.exception("mockup vision pipeline failed: %s", exc)
                flash(f"Mockup analysis failed: {type(exc).__name__}", "danger")

        # ── Tab 3 — URL crawl ────────────────────────────────────
        elif source_choice == "url":
            if not url:
                flash(
                    "URL tab is selected but no URL was provided.",
                    "warning",
                )
            else:
                try:
                    analysis = crawl_site(url)
                    features = features_from_site_analysis(analysis)
                    if features:
                        source, source_ref = "url", url
                    else:
                        flash(
                            f"Crawled {url} but no testable features "
                            f"were extracted. Try the Text or Mockups "
                            f"tab, or check that the URL serves real "
                            f"HTML content.",
                            "warning",
                        )
                except Exception as exc:
                    log.warning("estimation site crawl failed: %s", exc)
                    flash(
                        f"Could not crawl {url}: "
                        f"{type(exc).__name__} — {str(exc)[:200]}. "
                        "Switch to the Text tab and paste the spec, or "
                        "try a different URL.",
                        "warning",
                    )

        # ── Legacy fallback: try text_input regardless of tab ───
        # If the chosen source produced nothing but the user ALSO
        # pasted something into the text textarea, use that as a
        # fallback. Restores the pre-3-tab-refactor behaviour
        # operators relied on.
        if not features and text_input:
            try:
                features = features_from_text(text_input)
                if features:
                    source = source or "text"
                    source_ref = source_ref or "pasted input (fallback)"
            except Exception as exc:
                log.warning("text fallback features_from_text failed: %s",
                            exc)

        if not features:
            flash(
                "No features could be extracted from the selected "
                "source. Pick a different tab, paste more detailed "
                "content, or check the URL is reachable.",
                "danger",
            )
            return redirect(url_for("estimation_page"))

        # Optional team_size — drives Brooks's-Law overhead. Default 1.
        try:
            team_size = max(1, int(request.form.get("team_size", "1") or 1))
        except (TypeError, ValueError):
            team_size = 1

        result = compute_estimation(
            features=features,
            rate_usd=rate_usd,
            additional_platforms=additional_platforms,
            minutes_per_tc=minutes_per_tc,
            buffer=buffer,
            project_name=project_name,
            primary_platform=primary_platform,
            platforms_list=_DEFAULT_COMPAT_PLATFORMS[:additional_platforms],
            source=source,
            source_ref=source_ref,
            compatibility_rate=compatibility_rate,
            bug_report_rate=bug_report_rate,
            pm_overhead=pm_overhead,
            max_testing_stretch=max_testing_stretch,
            team_size=team_size,
        )

        # Historical calibration — soft hint based on past estimations
        # for the current owner_sid. Never mutates the numbers.
        try:
            _historical_calibration(result, get_session_id())
        except Exception as exc:  # pragma: no cover
            log.warning("history calibration skipped: %s", exc)

        result_dict = asdict(result)
        session["estimation_result"] = result_dict
        _persist_estimation(
            input_payload={
                "source": source,
                "source_ref": source_ref,
                "primary_platform": primary_platform,
                "additional_platforms": additional_platforms,
                "compatibility_rate": compatibility_rate,
                "bug_report_rate": bug_report_rate,
                "pm_overhead": pm_overhead,
                "team_size": team_size,
                "features_count": sum(1 for f in features if not f.is_section),
                "max_testing_stretch": max_testing_stretch,
            },
            result_dict=result_dict,
        )
        flash(
            f"Estimation complete: {result.total_tc} test cases, "
            f"{result.one_plat_total_expected:.1f}h (one platform), "
            f"{result.full_total_expected:.1f}h (full compatibility).",
            "success",
        )
        return redirect(url_for("estimation_page"))

    @app.route("/estimation/run-async", methods=["POST"])
    def estimation_run_async():
        """Submit estimation work to the background JobQueue and return
        a ``job_id`` immediately so the UI can show a progress modal
        and poll ``/estimation/status/<id>`` until done.

        Handles ALL three source types — Text (instant), Mockups
        (vision API takes 5-30 s on Claude), and URL (site crawl can
        stretch to many seconds). Operator-reported on 2026-05-04 that
        the legacy sync /estimation/run path's result template never
        rendered after the recent refactor; standardising on the
        async pattern fixes that AND brings the same progress modal
        the Test Cases / Checklist generators use.

        File uploads are saved to UPLOAD_FOLDER inside this request
        BEFORE the job is submitted — the worker thread has no access
        to ``request.files``.
        """
        source_choice = request.form.get("source", "text").strip()
        url = request.form.get("url", "").strip()
        text_input = request.form.get("text_input", "").strip()
        figma_url = request.form.get("figma_url", "").strip()
        mockup_context = request.form.get("mockup_context", "").strip()

        # Per-session concurrency cap (shared threshold with automation).
        # Sprint 1 Task 5 added the MAX_CONCURRENT_RUNS env knob — when
        # present (and lower than the legacy MAX_CONCURRENT_JOBS_PER_SESSION),
        # it tightens the gate so operators can ratchet the limit down
        # without a code change. Both still ride the same kind+meta query
        # so finished jobs roll off as soon as they complete.
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
            # Site crawls are typically faster than automation runs, so
            # a 15s hint is more than enough in practice.
            resp.headers["Retry-After"] = "15"
            return resp

        # Pre-save uploaded files BEFORE dispatching to the worker,
        # because the worker thread has no request context. Each path
        # records the absolute file paths into ``saved_paths`` so the
        # worker can re-open them via plain pathlib.
        saved_attachment_path = ""
        saved_mockup_paths: list[str] = []
        upload_dir = current_app.config["UPLOAD_FOLDER"]
        if source_choice in ("text", "attachment"):
            up = request.files.get("attachment")
            if up and up.filename and allowed_file(up.filename):
                safe_name = secure_filename(up.filename) or "upload.bin"
                saved_attachment_path = os.path.join(upload_dir, safe_name)
                up.save(saved_attachment_path)
        elif source_choice == "mockups":
            for up in request.files.getlist("mockup_files"):
                if not up or not up.filename:
                    continue
                if not allowed_file(up.filename):
                    continue
                safe = secure_filename(up.filename) or "mockup.bin"
                p = os.path.join(upload_dir, safe)
                up.save(p)
                saved_mockup_paths.append(p)

        # Validation per source type. URL needs the URL; mockups
        # needs at least one image OR a Figma URL; text needs at
        # least pasted text or an attachment.
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

        # Same clamping as /estimation/run — keep numeric inputs sane.
        def _clamp_int(name: str, default: int, lo: int, hi: int) -> int:
            try:
                return int(_clamp(int(request.form.get(name, str(default)) or default), lo, hi))
            except ValueError:
                return default

        def _clamp_float(name: str, default: float, lo: float, hi: float) -> float:
            try:
                return _clamp(float(request.form.get(name, str(default)) or default), lo, hi)
            except ValueError:
                return default

        project_name = (request.form.get("project_name", "").strip())[:120]
        rate_usd = _clamp_float("rate_usd", 0.0, 0.0, 10_000.0)
        additional_platforms = _clamp_int(
            "additional_platforms", 9,
            0, current_app.config["EST_MAX_ADDITIONAL_PLATFORMS"])
        minutes_per_tc = _clamp_int(
            "minutes_per_tc", 5,
            1, current_app.config["EST_MAX_MINUTES_PER_TC"])
        buffer_percent = _clamp_float(
            "buffer_percent", 12.0,
            0.0, float(current_app.config["EST_MAX_BUFFER_PERCENT"]))
        buffer = 1.0 + buffer_percent / 100.0
        compat_percent = _clamp_float("compatibility_rate", 0.3, 0.0, 100.0)
        bug_percent = _clamp_float("bug_report_rate", 15.0, 0.0, 100.0)
        pm_percent = _clamp_float("pm_overhead", 8.0, 0.0, 100.0)
        max_testing_stretch = _clamp_float("max_testing_stretch", 1.5, 1.0, 10.0)
        primary_platform = (request.form.get("primary_platform", "Windows 10").strip()
                            or "Windows 10")

        session["estimation_form"] = {
            "project_name": project_name, "rate_usd": rate_usd,
            "additional_platforms": additional_platforms,
            "minutes_per_tc": minutes_per_tc, "buffer_percent": int(buffer_percent),
            "compatibility_rate": compat_percent,
            "bug_report_rate": bug_percent,
            "pm_overhead": pm_percent,
            "max_testing_stretch": max_testing_stretch,
            "primary_platform": primary_platform,
            "url": url, "text_input": text_input,
            "figma_url": figma_url, "mockup_context": mockup_context,
            "source": source_choice,
        }

        compat_rate = compat_percent / 100.0
        bug_rate = bug_percent / 100.0
        pm_ov = pm_percent / 100.0

        # Capture every primitive the worker needs into closure-local
        # variables — request.form / request.files don't exist in the
        # background thread.
        _src         = source_choice
        _url         = url
        _text_input  = text_input
        _figma_url   = figma_url
        _mockup_ctx  = mockup_context
        _att_path    = saved_attachment_path
        _mockup_paths = list(saved_mockup_paths)

        def _worker():
            """Dispatch by source type, return asdict(EstimationResult)."""
            features: list = []
            source = "manual"
            source_ref = ""
            extracted_text = ""

            if _src in ("text", "attachment"):
                lines: list[str] = []
                if _text_input:
                    lines.append(_text_input)
                if _att_path:
                    try:
                        from os.path import basename
                        parsed_lines, err = parse_file(_att_path,
                                                        basename(_att_path))
                        if parsed_lines:
                            lines.extend(parsed_lines)
                        source_ref = basename(_att_path)
                    except Exception as exc:
                        log.warning("worker parse_file failed: %s", exc)
                if lines:
                    features = features_from_text("\n".join(lines))
                    source = "text" if not source_ref else "attachment"
                    if not source_ref:
                        source_ref = "pasted input"

            elif _src == "mockups":
                try:
                    from engine.mockup_vision import analyse as _va
                    vres = _va(file_paths=_mockup_paths,
                               figma_url=_figma_url,
                               context=_mockup_ctx)
                    if vres.text:
                        features = features_from_text(vres.text)
                        source = "mockups"
                        bits = []
                        if _mockup_paths:
                            bits.append(f"{len(_mockup_paths)} file(s)")
                        if _figma_url:
                            bits.append("Figma URL")
                        source_ref = (vres.source_label
                                       or " + ".join(bits)
                                       or "uploaded mockups")
                        extracted_text = vres.text
                    elif vres.error:
                        raise RuntimeError(
                            f"Mockup analysis: {vres.error}")
                except RuntimeError:
                    raise
                except Exception as exc:
                    raise RuntimeError(
                        f"Mockup analysis failed: "
                        f"{type(exc).__name__} — {exc}") from exc

            elif _src == "url":
                try:
                    analysis = crawl_site(_url)
                    features = features_from_site_analysis(analysis)
                    if features:
                        source, source_ref = "url", _url
                except Exception as exc:
                    raise RuntimeError(
                        f"Could not crawl {_url}: "
                        f"{type(exc).__name__} — {exc}") from exc

            # Legacy fallback — try pasted text regardless of tab.
            if not features and _text_input:
                try:
                    features = features_from_text(_text_input)
                    if features:
                        source = source or "text"
                        source_ref = source_ref or "pasted input (fallback)"
                except Exception:
                    pass

            if not features:
                raise RuntimeError(
                    "No features could be extracted from the selected "
                    "source. Pick a different tab, paste more detailed "
                    "content, or check the URL is reachable.")

            result = compute_estimation(
                features=features,
                rate_usd=rate_usd,
                additional_platforms=additional_platforms,
                minutes_per_tc=minutes_per_tc,
                buffer=buffer,
                project_name=project_name,
                primary_platform=primary_platform,
                platforms_list=_DEFAULT_COMPAT_PLATFORMS[:additional_platforms],
                source=source,
                source_ref=source_ref,
                compatibility_rate=compat_rate,
                bug_report_rate=bug_rate,
                pm_overhead=pm_ov,
                max_testing_stretch=max_testing_stretch,
            )
            payload = asdict(result)
            if extracted_text:
                # Carried back into session via /estimation/status so
                # the "Generate test cases" CTA has the bullet text.
                payload["_extracted_text"] = extracted_text
            return payload

        job_id = get_queue().submit(
            "estimation", _worker,
            meta={
                "source": source_choice,
                "url": url,
                "project_name": project_name,
                "session_id": sid,  # used by count_active_by_meta()
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
            session["estimation_result"] = result
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
            data = session.get("estimation_result") or {}
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
        data = session.get("estimation_result")
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
