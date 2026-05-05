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
        elif source_choice == "url" and url:
            try:
                analysis = crawl_site(url)
                features = features_from_site_analysis(analysis)
                source, source_ref = "url", url
            except Exception as exc:
                log.warning("estimation site crawl failed: %s", exc)
                flash(f"Crawl failed: {exc}", "warning")

        if not features:
            flash(
                "No features could be extracted from the selected "
                "source. Switch tabs or provide more content.",
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
