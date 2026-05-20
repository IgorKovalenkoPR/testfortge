"""TestForTge — Debug-only routes (feature-flagged, not user-facing).

Currently hosts a single endpoint for the TFWefloLab walkthrough
integration (PR-1):

  * POST /debug/walkthrough — dispatches a ``mode="walkthrough"`` run
    via the detached :mod:`engine.runner_worker`. **Hidden when the
    ``WALKTHROUGH_MODE_ENABLED`` env var is not set to ``"1"``** — the
    endpoint returns 404 in that case so production scans cannot
    enumerate it.

This module exists so the scaffold can be exercised end-to-end (write
config JSON → spawn worker → poll for done.flag → read result.json)
without touching the user-visible ``/test-execution`` route. PR-3 of
the TFWefloLab integration replaces this endpoint with a radio toggle
on the main form; this file goes away then.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime

from flask import Flask, abort, current_app, jsonify, request

from engine.log import get_logger
from engine.walkthrough_runner import feature_enabled

log = get_logger(__name__)


def _require_walkthrough_enabled() -> None:
    """Return-or-abort gate. 404 (not 403) so unauthenticated probes
    cannot tell whether the feature is configured at all — leak nothing
    about the env-var name or whether this build supports walkthrough.
    """
    if not feature_enabled():
        abort(404)


def register(app: Flask) -> None:

    @app.route("/debug/walkthrough", methods=["POST"])
    def debug_walkthrough_dispatch():
        """Spawn a walkthrough run on the detached worker.

        Request body (JSON or form-encoded):
          * ``base_url`` (required) — the URL the walkthrough starts at
          * ``start_urls`` (optional, list[str]) — extra URLs to visit;
            defaults to ``[base_url]``
          * ``max_pages`` (optional int, default 6) — hard cap on pages
          * ``device_timeout_ms`` (optional int, default 480000) — outer
            wall-clock budget for the whole run
          * ``headless`` (optional bool, default true)

        Returns JSON with ``config_id`` and the result/done file paths
        the caller can poll. Spawning the worker on its own session is
        identical to the existing ``/test-execution`` flow so the live
        viewer and result endpoint pick the run up unchanged.
        """
        _require_walkthrough_enabled()

        payload = request.get_json(silent=True) or request.form
        base_url = (payload.get("base_url") or "").strip()
        if not base_url:
            return jsonify({"error": "base_url is required"}), 400

        # ``start_urls`` may arrive as a JSON list (preferred) or a
        # newline-separated string (form-encoded fallback for the
        # operator pasting URLs into a curl request).
        raw_urls = payload.get("start_urls")
        if isinstance(raw_urls, str):
            start_urls = [u.strip() for u in raw_urls.splitlines()
                          if u.strip()]
        elif isinstance(raw_urls, list):
            start_urls = [str(u).strip() for u in raw_urls
                          if str(u).strip()]
        else:
            start_urls = [base_url]
        if not start_urls:
            start_urls = [base_url]

        try:
            max_pages = int(payload.get("max_pages") or 6)
        except (TypeError, ValueError):
            max_pages = 6
        try:
            device_timeout_ms = int(payload.get("device_timeout_ms")
                                     or 480000)
        except (TypeError, ValueError):
            device_timeout_ms = 480000
        # Form-encoded payloads send "true" / "false" strings; honour
        # both shapes.
        headless_raw = payload.get("headless")
        if isinstance(headless_raw, bool):
            headless = headless_raw
        elif isinstance(headless_raw, str):
            headless = headless_raw.strip().lower() not in ("0", "false", "no")
        else:
            headless = True

        storage_root = current_app.config.get("STORAGE_FOLDER") or ""
        if not storage_root:
            log.error("debug/walkthrough: STORAGE_FOLDER not configured")
            return jsonify({"error": "storage not configured"}), 500

        config_id = (datetime.now().strftime("%Y%m%d_%H%M%S_")
                     + uuid.uuid4().hex[:6])
        pending_dir = os.path.join(storage_root, "automation_runs",
                                    "_pending")
        os.makedirs(pending_dir, exist_ok=True)
        config_path = os.path.join(pending_dir, f"{config_id}.json")
        worker_log = os.path.join(pending_dir, f"{config_id}.log")

        # PR-2 knobs — both default-conservative so a curl invocation
        # with only ``base_url`` keeps producing the same scaffold-shape
        # output PR-1 documented. ``axe_enabled`` defaults TRUE because
        # axe-core CDN is reachable in every supported deploy target;
        # operators running offline or behind strict egress firewalls
        # pass ``"axe_enabled": false`` to skip the a11y step.
        axe_raw = payload.get("axe_enabled")
        if isinstance(axe_raw, bool):
            axe_enabled = axe_raw
        elif isinstance(axe_raw, str):
            axe_enabled = axe_raw.strip().lower() not in ("0", "false", "no")
        else:
            axe_enabled = True
        try:
            max_form_fills = int(payload.get("max_form_fills") or 5)
        except (TypeError, ValueError):
            max_form_fills = 5
        # ``test_cases`` is an optional list of TC dicts with at minimum
        # ``id``, ``url_pattern``, ``trigger``. Used to exercise the
        # URL-pattern matcher end-to-end via the debug endpoint without
        # needing the PR-3 DB-hydration flow. Anything that doesn't
        # look like a dict is silently dropped.
        raw_tcs = payload.get("test_cases") or []
        if isinstance(raw_tcs, list):
            test_cases = [tc for tc in raw_tcs if isinstance(tc, dict)]
        else:
            test_cases = []

        config_payload = {
            "config_id": config_id,
            "storage_root": storage_root,
            "mode": "walkthrough",
            "base_url": base_url,
            "headless": headless,
            "runner_kwargs": {"headless": headless},
            "walkthrough": {
                "start_urls":      start_urls,
                "max_pages":       max_pages,
                "device_timeout_ms": device_timeout_ms,
                "max_form_fills":  max_form_fills,
                "axe_enabled":     axe_enabled,
                "test_cases":      test_cases,
            },
        }
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_payload, f)

        log_fh = open(worker_log, "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", "engine.runner_worker", config_path],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            cwd=os.path.dirname(storage_root) or None,
        )
        log.info("debug/walkthrough: dispatched worker pid=%s config=%s "
                 "urls=%d", proc.pid, config_id, len(start_urls))

        return jsonify({
            "config_id": config_id,
            "result_path": os.path.join(pending_dir,
                                         f"{config_id}.result.json"),
            "done_flag":   os.path.join(pending_dir,
                                         f"{config_id}.done.flag"),
            "worker_log":  worker_log,
            "start_urls":  start_urls,
            "max_pages":   max_pages,
        }), 202

    # Operator/curl-driven JSON POST — CSRF token has no meaning here.
    # Skip the global CSRFProtect gate on this single view so a one-line
    # ``curl -X POST .../debug/walkthrough -d '{"base_url":"..."}'``
    # actually reaches the dispatcher. The 404-when-disabled gate above
    # is what prevents the endpoint from being callable on prod by
    # default.
    csrf = app.extensions.get("csrf") if hasattr(app, "extensions") else None
    if csrf is not None:
        try:
            csrf.exempt(debug_walkthrough_dispatch)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("debug/walkthrough: csrf.exempt skipped: %s", exc)


__all__ = ["register"]
