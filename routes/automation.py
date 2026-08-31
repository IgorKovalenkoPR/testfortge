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

import hmac
import os

from flask import (Flask, Response, abort, flash, jsonify, redirect,
                   render_template, request, send_from_directory, session,
                   url_for)

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
from engine.automation_paths import STORAGE_ROOT  # re-exported below

from engine import allure_ingest
from engine import permissions as _perm
from engine import storage as _storage
from engine import automation_codegen as codegen
from engine import db as _db
from engine import gherkin

from ._shared import (SAFE_ASSET_RE, belongs_to_another_org, get_session_id,
                      pack_test_cases, reconstruct_test_cases,
                      resolve_active_project)

log = get_logger(__name__)

# Per-session concurrency cap for /automation/run-async + /estimation/run-async.
# Prevents a single user from monopolising the (small) worker pool.
MAX_CONCURRENT_JOBS_PER_SESSION = int(
    os.environ.get("MAX_CONCURRENT_JOBS_PER_SESSION", "3")
)


def _ingest_token() -> str:
    """The shared secret CI presents to post results back.

    Read per call rather than at import: the value comes from the
    environment and an operator setting it on Render should not need a
    redeploy of this module to take effect.

    No token means the ingest endpoint is OFF. That is the safe default —
    an unauthenticated endpoint that writes run history is an endpoint
    anyone can use to write run history.
    """
    return (os.environ.get("AUTOMATION_INGEST_TOKEN") or "").strip()


def _project_base_url(project_id: str | None) -> str:
    """The project's own base URL, for the generated playwright.config."""
    if not project_id:
        return ""
    try:
        project = _db.get_project(project_id) or {}
    except Exception as exc:  # pragma: no cover — best-effort
        log.debug("automation: base_url lookup failed: %s", exc)
        return ""
    return str(project.get("base_url") or "")


def register(app: Flask) -> None:
    @app.route("/automation", methods=["GET"])
    def automation_page():
        """The Automation module — codegen out, Allure results in.

        Was a redirect to Test Execution while automation meant "the
        built-in Python runner". It is a module again because the TS
        pipeline is a different thing with a different lifecycle: the suite
        is generated here, runs somewhere with browsers, and reports back.
        Test Execution stays the place you launch and watch a run.
        """
        pid = resolve_active_project(session)
        cases = reconstruct_test_cases(pack_test_cases())
        if not cases and pid:
            try:
                cases = reconstruct_test_cases(_db.load_test_cases(pid))
            except Exception as exc:  # pragma: no cover — best-effort
                log.warning("automation: DB reload failed: %s", exc)

        targeted = [tc for tc in cases if gherkin.is_automation_targeted(tc)]
        coverage = codegen.coverage_report(targeted).to_dict() if targeted \
            else None

        runs: list[dict] = []
        # Only when there *is* a project. ``list_automation_runs(None)``
        # means "every run on this instance" — it adds no WHERE clause — so
        # with no active project this page rendered every organisation's run
        # history, labels and failure messages included. Nothing asked for
        # that view; it was the falsy-argument default showing through.
        # ``routes/dashboard.py`` guards the same helper by hand for the same
        # reason, which is one hand-guard too many for one footgun: see the
        # note on the helper itself.
        if pid:
            try:
                runs = _db.list_automation_runs(pid, limit=20)
            except Exception as exc:  # pragma: no cover — best-effort
                log.warning("automation: run history failed: %s", exc)

        return render_template(
            "automation.html",
            total_cases=len(cases),
            targeted_cases=len(targeted),
            coverage=coverage,
            runs=runs,
            latest=runs[0] if runs else None,
            ingest_enabled=bool(_ingest_token()),
            bundle_version=codegen.BUNDLE_VERSION,
            base_url=session.get("automation_base_url", "")
                     or _project_base_url(pid),
        )

    @app.route("/automation/bundle.zip", methods=["GET"])
    def automation_bundle():
        """The generated TypeScript + Playwright + Allure project."""
        pid = resolve_active_project(session)
        cases = reconstruct_test_cases(pack_test_cases())
        if not cases and pid:
            try:
                cases = reconstruct_test_cases(_db.load_test_cases(pid))
            except Exception as exc:  # pragma: no cover
                log.warning("automation bundle: DB reload failed: %s", exc)
        targeted = [tc for tc in cases if gherkin.is_automation_targeted(tc)]
        if not targeted:
            # Explaining beats an empty archive — an empty zip reads as a
            # broken download rather than as "you did not ask for BDD".
            return ("No automation-targeted test cases. Regenerate the pack "
                    "with the BDD format selected, or switch individual "
                    "cases to BDD in the Test Cases editor.", 409)

        name = ((session.get("project_setup") or {}).get("project_name")
                or "testfortge").replace(" ", "_")
        payload = codegen.bundle_zip(
            targeted,
            base_url=session.get("automation_base_url", "")
                     or _project_base_url(pid),
            project_name=name,
            locators=codegen.locators_for_project(pid or ""),
        )
        return Response(
            payload, mimetype="application/zip",
            headers={"Content-Disposition":
                     f"attachment; filename={name}_automation.zip"})

    @app.route("/automation/allure-results", methods=["POST"])
    def automation_allure_results():
        """Ingest an ``allure-results`` archive from a local or CI run.

        Token-authenticated rather than session-authenticated: the caller
        is a CI job with no browser and no cookie. Disabled outright when
        no token is configured — an open ingest endpoint would let anyone
        write run history into someone's project.

        The token is **per instance**, and ``project_id`` comes from the
        form with only an existence check, so the secret authorises a write
        into *any* project on the instance including another organisation's.
        That is authentication standing in for authorisation, and it is
        stated here rather than left to be discovered: closing it needs a
        per-project ingest token, which is an ops decision about where the
        secret lives and how a team rotates it, not something to invent in
        this function. Single-tenant today (``ORG_MODE`` is off in
        production), which is why it is recorded rather than patched.
        """
        expected = _ingest_token()
        if not expected:
            return jsonify({
                "error": "ingest_disabled",
                "message": ("Set AUTOMATION_INGEST_TOKEN on the service to "
                            "enable result ingestion."),
            }), 403
        supplied = (request.headers.get("X-TFG-Token")
                    or request.form.get("token") or "")
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            return jsonify({"error": "bad_token"}), 401

        upload = request.files.get("results")
        raw = upload.read() if upload is not None else request.get_data()
        if not raw:
            return jsonify({
                "error": "empty",
                "message": ("Attach the zipped allure-results directory as "
                            "the 'results' file field."),
            }), 400

        summary = allure_ingest.parse_archive(raw)
        if summary.total == 0:
            # 422, not 400: the request was well-formed, its contents were
            # not what the endpoint needs. The warning says which.
            return jsonify({"error": "no_results",
                            "warnings": summary.warnings}), 422

        pid = (request.form.get("project_id") or "").strip() \
            or resolve_active_project(session) or None
        if pid:
            try:
                if _db.get_project(pid) is None:
                    pid = None
            except Exception:  # pragma: no cover — best-effort
                pid = None

        origin = (request.form.get("origin") or "").strip().lower()
        if origin not in ("local", "ci"):
            origin = "ci" if request.headers.get("X-TFG-Token") else "unknown"

        try:
            run_id = _db.save_automation_run(
                pid, summary.to_dict(), origin=origin,
                label=(request.form.get("label") or "").strip())
        except Exception as exc:
            log.exception("automation ingest: save failed")
            return jsonify({"error": "save_failed", "message": str(exc)}), 500

        return jsonify({
            "run_id": run_id,
            "project_id": pid,
            **allure_ingest.to_metrics(summary),
            "warnings": summary.warnings,
        }), 201

    @app.route("/automation/runs/<int:run_id>", methods=["GET"])
    def automation_run_detail(run_id):
        """Drill-down for one ingested run.

        Scoped to the run's own project, which it was not: the route took a
        sequential integer and asked nobody, so any signed-in member of any
        team could read any run's label, case names and failure messages by
        counting upwards. The listing it is reached from is project-scoped,
        so nothing legitimate depended on the wider answer.

        404 rather than 403, matching ``/api/edit/*``: telling a caller that
        run 41 exists but is not theirs is the one thing this route should
        not say.

        A run with **no** project is still served. Nothing owns it — it was
        ingested without one — and it appears in no listing either, so
        refusing it would take away the only way to reach a run somebody
        just posted and got a ``run_id`` back for. That is the same answer
        ``project_access_with_meta`` gives a project with a NULL
        ``owner_sid``, for the same reason.
        """
        run = None
        try:
            run = _db.get_automation_run(run_id)
        except Exception as exc:  # pragma: no cover — best-effort
            log.warning("automation run detail failed: %s", exc)
        if run is None:
            abort(404)
        if belongs_to_another_org(run.get("project_id")):
            abort(404)
        return render_template("automation_run.html", run=run,
                              summary=run.get("summary") or {})

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
        tc_data = pack_test_cases()
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
            project_id=session.get("project_id") or "",
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
        tc_data = pack_test_cases()
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
            project_id=session.get("project_id") or "",
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
        """Serve one artefact by key — from this disk, or from the bucket.

        Local disk is tried **first**, and that ordering is the point rather
        than an optimisation. Two kinds of thing answer to this route: keys
        minted by ``engine.blobs`` (uploads, which go wherever E8.2's backend
        says) and paths the Playwright runner wrote straight to
        ``STORAGE_ROOT/automation_runs/...`` while a run was in progress. The
        second kind is on this disk whatever the configured backend is, so
        checking the disk first keeps run screenshots working on the day an
        organisation switches to R2 — the alternative is a page of broken
        images for artefacts that are sitting right there.

        When it is not on disk, the backend is asked where it is. Under S3
        that is a **presigned URL and a redirect**, not a proxy: ADR 0002
        §4.4 — R2's egress is free only while the bytes do not pass through
        this process, and a 512 MB dyno is the wrong thing to put in front
        of every thumbnail.
        """
        # Reject traversal / NUL / absolute paths outright.
        if (".." in path.split("/") or ".." in path.split("\\")
                or "\x00" in path or not SAFE_ASSET_RE.fullmatch(path)):
            abort(400)
        # The live directory is off-limits here. It is one directory per
        # instance rather than per project, so its contents belong to
        # whichever run is executing — and /test-execution/live/{frame,
        # strip,info} now check that the caller's project owns that run
        # before serving it. This route takes a path and asks nobody, so
        # leaving it open would be a second door to the bytes the other
        # three just started guarding: the frame of another
        # organisation's run, reachable by a fixed, guessable name.
        if "_live" in path.split("/"):
            abort(404)
        # Confirm the resolved path still lives under STORAGE_ROOT.
        asset_root = os.path.realpath(STORAGE_ROOT)
        target = os.path.realpath(os.path.join(asset_root, path))
        if not target.startswith(asset_root + os.sep):
            abort(400)
        if os.path.isfile(target):
            return send_from_directory(STORAGE_ROOT, path)

        backend = _storage.backend_for(_perm.current_org_id())
        if backend.name == "local":
            # Nothing else to try, and send_from_directory's own 404 is the
            # honest answer — on the free plan the usual cause is that the
            # dyno restarted and took the ephemeral disk with it.
            return send_from_directory(STORAGE_ROOT, path)
        try:
            location = backend.locate(path)
        except _storage.StorageError as exc:
            log.info("no signed URL for %s: %s", path[:80], exc)
            abort(404)
        if not location.url:      # pragma: no cover — a local Location here
            abort(404)
        return redirect(location.url)

    # The ingest endpoint carries its own token auth and is called by a CI
    # job from another origin — there is no TestForTge session cookie and
    # no csrf_token in that context, so the global CSRFProtect gate cannot
    # apply. Same pattern the recorder endpoints use in routes/generation.py.
    _ext = app.extensions.get("csrf") if hasattr(app, "extensions") else None
    if _ext is not None:
        try:
            _ext.exempt(automation_allure_results)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("automation allure-results csrf.exempt skipped: %s", exc)


__all__ = ["register", "STORAGE_ROOT"]
