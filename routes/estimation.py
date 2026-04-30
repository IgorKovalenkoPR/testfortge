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
    @app.route("/estimation", methods=["GET"])
    def estimation_page():
        lang = session.get("lang", "en")
        t = get_lang(lang)
        return render_template(
            "estimation.html",
            t=t, lang=lang,
            result=session.get("estimation_result"),
            last=session.get("estimation_form", {}),
        )

    @app.route("/estimation/run", methods=["POST"])
    def estimation_run():
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

        session["estimation_form"] = {
            "project_name": project_name, "rate_usd": rate_usd,
            "additional_platforms": additional_platforms,
            "minutes_per_tc": minutes_per_tc, "buffer_percent": int(buffer_percent),
            "compatibility_rate": compat_percent,
            "bug_report_rate": bug_percent,
            "pm_overhead": pm_percent,
            "max_testing_stretch": max_testing_stretch,
            "primary_platform": primary_platform, "url": url, "text_input": text_input,
        }

        features: list = []
        source = "manual"
        source_ref = ""

        # 1. URL source
        if source_choice == "url" and url:
            try:
                analysis = crawl_site(url)
                features = features_from_site_analysis(analysis)
                source, source_ref = "url", url
            except Exception as exc:
                log.warning("estimation site crawl failed: %s", exc)
                flash(f"Crawl failed: {exc}", "warning")

        # 2. Attachment source
        if not features and source_choice == "attachment":
            up = request.files.get("attachment")
            if up and up.filename and allowed_file(up.filename):
                safe_name = secure_filename(up.filename) or "upload.bin"
                save_path = os.path.join(current_app.config["UPLOAD_FOLDER"], safe_name)
                up.save(save_path)
                lines, err = parse_file(save_path, safe_name)
                if err:
                    flash(f"Attachment parse warning: {err}", "warning")
                features = features_from_text("\n".join(lines or []))
                source, source_ref = "attachment", safe_name

        # 3. Manual text
        if not features and text_input:
            features = features_from_text(text_input)
            source, source_ref = "text", "pasted input"

        if not features:
            flash("No features could be extracted — provide a URL, attachment or feature list.",
                  "danger")
            return redirect(url_for("estimation_page"))

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
        )

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
        """Submit site-crawl + feature extraction to the background worker.

        Crawling large sites can stretch into many seconds; return a
        ``job_id`` immediately so the UI can display a progress banner
        and poll ``/estimation/status/<id>`` until done.

        The file-upload and text-paste code paths complete in milliseconds
        and don't need to go async — this endpoint is URL-only.
        """
        url = request.form.get("url", "").strip()
        if not url:
            return jsonify({"error": "no_url",
                            "message": "Provide a URL to crawl."}), 400

        # Per-session concurrency cap (shared threshold with automation).
        sid = get_session_id(session)
        active = get_queue().count_active_by_meta(
            "estimation", "session_id", sid)
        if active >= MAX_CONCURRENT_JOBS_PER_SESSION:
            resp = jsonify({
                "error": "rate_limited",
                "message": (f"You already have {active} active estimation "
                            f"jobs. Wait for them to finish before starting "
                            f"another."),
                "active": active,
                "limit": MAX_CONCURRENT_JOBS_PER_SESSION,
            })
            resp.status_code = 429
            # Site crawls are typically faster than automation runs, so
            # a 15s hint is more than enough in practice.
            resp.headers["Retry-After"] = "15"
            return resp

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
            "primary_platform": primary_platform, "url": url, "text_input": "",
        }

        compat_rate = compat_percent / 100.0
        bug_rate = bug_percent / 100.0
        pm_ov = pm_percent / 100.0

        def _worker():
            analysis = crawl_site(url)
            features = features_from_site_analysis(analysis)
            if not features:
                raise RuntimeError("No features could be extracted from the site.")
            result = compute_estimation(
                features=features,
                rate_usd=rate_usd,
                additional_platforms=additional_platforms,
                minutes_per_tc=minutes_per_tc,
                buffer=buffer,
                project_name=project_name,
                primary_platform=primary_platform,
                platforms_list=_DEFAULT_COMPAT_PLATFORMS[:additional_platforms],
                source="url",
                source_ref=url,
                compatibility_rate=compat_rate,
                bug_report_rate=bug_rate,
                pm_overhead=pm_ov,
                max_testing_stretch=max_testing_stretch,
            )
            return asdict(result)

        job_id = get_queue().submit(
            "estimation", _worker,
            meta={
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

        On success, ``result`` is written to the session and returned
        inline so the client can render without a second request.
        """
        job = get_queue().get(job_id)
        if job is None or job.kind != "estimation":
            return jsonify({"error": "not_found"}), 404

        payload = job.to_public_dict()
        if job.status == DONE and job.result is not None:
            session["estimation_result"] = job.result
            payload["result"] = job.result
            # Mirror the result into the project's estimation history.
            # Async path doesn't have access to the original form values
            # the way the sync handler did, so we fall back to job.meta.
            _persist_estimation(
                input_payload=getattr(job, "meta", {}) or {},
                result_dict=job.result,
            )
        return jsonify(payload)

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
