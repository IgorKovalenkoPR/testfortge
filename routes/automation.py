"""TestFortge — Automation QA routes (Playwright-driven).

  * GET  /automation                      — setup + last-report view
  * POST /automation/generate-account     — throw-away test account
  * POST /automation/run                  — run Playwright scripts (sync fallback)
  * POST /automation/run-async            — submit to background worker, return job_id
  * GET  /automation/status/<job_id>      — poll job progress + result
  * GET  /automation/asset/<path>         — serve video/screenshot assets

Assets live under ``STORAGE_ROOT/automation_runs/...`` and are served
with traversal guards: the resolved target must stay inside STORAGE_ROOT.

Long-running jobs run on the shared :class:`engine.job_queue.JobQueue`
so the request thread returns immediately. The synchronous
``/automation/run`` route is kept as a JS-less fallback and for tests.
"""

from __future__ import annotations

import os

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)

from engine.log import get_logger
from engine.i18n import get_lang
from engine.automation_qa import scripts_from_session
from engine.automation_runner import AutomationRunner
from engine.automation_report import report_to_dict, compute_automation_metrics
from engine.test_credentials import (
    credentials_from_form, credentials_from_session, credentials_to_session,
    generate_test_account,
)
from engine.job_queue import get_queue, DONE, FAILED

from ._shared import SAFE_ASSET_RE, get_session_id

log = get_logger(__name__)

# Storage root shared with the estimation module (both live under /storage).
STORAGE_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "storage",
)
os.makedirs(STORAGE_ROOT, exist_ok=True)

# Per-session concurrency cap for /automation/run-async + /estimation/run-async.
# Prevents a single user from monopolising the (small) worker pool.
MAX_CONCURRENT_JOBS_PER_SESSION = int(
    os.environ.get("MAX_CONCURRENT_JOBS_PER_SESSION", "3")
)


def register(app: Flask) -> None:
    @app.route("/automation", methods=["GET"])
    def automation_page():
        lang = session.get("lang", "en")
        t = get_lang(lang)
        tc_data = session.get("test_cases_data", [])
        report = session.get("automation_report")
        metrics = None
        if report:
            metrics = compute_automation_metrics(report, len(tc_data))
        cred = credentials_from_session(session.get("automation_credentials"))
        return render_template(
            "automation.html", t=t, lang=lang,
            tc_count=len(tc_data), report=report, metrics=metrics,
            last_base_url=session.get("automation_base_url", ""),
            cred=cred.as_public_dict(),
        )

    @app.route("/automation/generate-account", methods=["POST"])
    def automation_generate_account():
        """Throw-away test account pre-fills the Automation form."""
        base_url = request.form.get("base_url", "").strip()
        register_url = request.form.get("cred_register_url", "").strip()
        login_url = request.form.get("cred_login_url", "").strip()
        domain = ""
        if base_url:
            try:
                from urllib.parse import urlparse
                domain = urlparse(base_url).netloc or "testfortge.test"
            except Exception as exc:
                log.debug("domain parse failed for automation: %s", exc)
                domain = "testfortge.test"
        cred = generate_test_account(base_domain=domain or "testfortge.test",
                                     register_url=register_url,
                                     login_url=login_url)
        session["automation_credentials"] = credentials_to_session(cred)
        flash(f"Generated test account: {cred.username}", "success")
        return redirect(url_for("automation_page"))

    @app.route("/automation/run", methods=["POST"])
    def automation_run():
        tc_data = session.get("test_cases_data", [])
        if not tc_data:
            flash("No test cases to automate. Generate Test Cases first.", "warning")
            return redirect(url_for("automation_page"))

        base_url = request.form.get("base_url", "").strip()
        headless = request.form.get("headless", "1") == "1"
        record_video = request.form.get("record_video", "1") == "1"
        session["automation_base_url"] = base_url

        scripts = scripts_from_session(tc_data, base_url)
        credentials = credentials_from_form(request.form)
        session["automation_credentials"] = credentials_to_session(credentials)
        runner = AutomationRunner(
            storage_root=STORAGE_ROOT,
            base_url=base_url,
            headless=headless,
            record_video=record_video,
            credentials=credentials if credentials.is_active() else None,
        )
        try:
            report = runner.run(scripts)
            session["automation_report"] = report_to_dict(report)
            flash(
                f"Automation run complete: {report.passed} passed / "
                f"{report.failed} failed / {report.blocked} blocked",
                "success",
            )
        except Exception as e:
            log.exception("automation run failed")
            flash(f"Automation run failed: {e}", "danger")
        return redirect(url_for("automation_page"))

    @app.route("/automation/run-async", methods=["POST"])
    def automation_run_async():
        """Submit an automation run to the background worker pool.

        Returns JSON ``{"job_id": "...", "status": "pending"}`` immediately.
        The UI should poll ``/automation/status/<job_id>`` for progress
        and, once ``status == "done"``, reload ``/automation`` to display
        the report (stored in the session by the job on completion).
        """
        tc_data = session.get("test_cases_data", [])
        if not tc_data:
            return jsonify({"error": "no_test_cases",
                            "message": "Generate Test Cases first."}), 400

        # Per-session concurrency cap — a single user can't flood the pool.
        sid = get_session_id(session)
        active = get_queue().count_active_by_meta(
            "automation", "session_id", sid)
        if active >= MAX_CONCURRENT_JOBS_PER_SESSION:
            resp = jsonify({
                "error": "rate_limited",
                "message": (f"You already have {active} active automation "
                            f"jobs. Wait for them to finish before starting "
                            f"another."),
                "active": active,
                "limit": MAX_CONCURRENT_JOBS_PER_SESSION,
            })
            resp.status_code = 429
            # Suggest a conservative retry window — automation runs are
            # typically 30s–2min, so 30s is a reasonable first probe.
            resp.headers["Retry-After"] = "30"
            return resp

        base_url = request.form.get("base_url", "").strip()
        headless = request.form.get("headless", "1") == "1"
        record_video = request.form.get("record_video", "1") == "1"
        session["automation_base_url"] = base_url

        scripts = scripts_from_session(tc_data, base_url)
        credentials = credentials_from_form(request.form)
        session["automation_credentials"] = credentials_to_session(credentials)

        # Capture just the primitives the worker needs — it runs in a
        # thread without a request context, so we can't touch the session
        # from inside the job function.
        runner = AutomationRunner(
            storage_root=STORAGE_ROOT,
            base_url=base_url,
            headless=headless,
            record_video=record_video,
            credentials=credentials if credentials.is_active() else None,
        )

        def _worker(scripts_arg=scripts, runner_arg=runner):
            report = runner_arg.run(scripts_arg)
            return report_to_dict(report)

        job_id = get_queue().submit(
            "automation", _worker,
            meta={
                "base_url": base_url,
                "script_count": len(scripts),
                "session_id": sid,  # used by count_active_by_meta()
            },
        )
        # Track the active job in the session so the /automation page can
        # resume polling after a reload.
        session["automation_job_id"] = job_id
        return jsonify({"job_id": job_id, "status": "pending"})

    @app.route("/automation/status/<job_id>", methods=["GET"])
    def automation_status(job_id):
        """Return current status of an automation job.

        On success, the report payload is written to the session and
        returned inline so the caller can render immediately without a
        second round-trip.
        """
        job = get_queue().get(job_id)
        if job is None or job.kind != "automation":
            return jsonify({"error": "not_found"}), 404

        payload = job.to_public_dict()
        if job.status == DONE and job.result is not None:
            # Persist the completed report to session exactly like the
            # synchronous path does, so the /automation page renders it.
            session["automation_report"] = job.result
            payload["report"] = job.result
        elif job.status == FAILED:
            flash(f"Automation run failed: {job.error}", "danger")
        return jsonify(payload)

    @app.route("/automation/asset/<path:path>")
    def automation_asset(path):
        # Reject traversal / NUL / absolute paths outright.
        if (".." in path.split("/") or ".." in path.split("\\")
                or "\x00" in path or not SAFE_ASSET_RE.fullmatch(path)):
            abort(400)
        # Confirm the resolved path still lives under STORAGE_ROOT.
        asset_root = os.path.realpath(STORAGE_ROOT)
        target = os.path.realpath(os.path.join(asset_root, path))
        if not target.startswith(asset_root + os.sep):
            abort(400)
        return send_from_directory(STORAGE_ROOT, path)


__all__ = ["register", "STORAGE_ROOT"]
