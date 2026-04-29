"""TestFortge — Operations endpoints (Wave D observability).

  * GET /healthz  — liveness/readiness probe (no secrets, no CSRF)
  * GET /metrics  — JSON snapshot of job-queue depth + session counts

Both endpoints are deliberately **unauthenticated** so container
orchestrators (k8s, Docker healthcheck, uptime pingers) can hit them
without secrets. They intentionally expose **no user data** — only
infrastructure-level counts and paths.

If you ever deploy behind a LB/CDN that proxies the public internet,
put these behind an internal listener or an IP allowlist. For the
in-house / on-prem deployments TestFortge currently targets, open is
fine.
"""

from __future__ import annotations

import os
import time

from flask import Flask, current_app, jsonify

from engine import db as _db
from engine.job_queue import DONE, FAILED, PENDING, RUNNING, get_queue
from engine.log import get_logger

log = get_logger(__name__)


def _check_writable(path: str) -> bool:
    """Return True if ``path`` exists and the process can write inside it.

    We probe by creating and immediately removing a dot-file — reading
    the dir's stat mode is not enough on Windows where permissions
    semantics differ from POSIX.
    """
    try:
        if not os.path.isdir(path):
            return False
        probe = os.path.join(path, f".healthz_{os.getpid()}")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError as exc:
        log.debug("healthz write-probe failed on %s: %s", path, exc)
        return False


def register(app: Flask) -> None:
    # Capture process start so /metrics can report uptime cheaply.
    _boot_ts = time.time()

    @app.route("/healthz", methods=["GET"])
    def healthz():
        """Cheap liveness probe.

        Returns 200 when the Flask process is up and both the session
        and storage directories exist + are writable. Anything else
        returns 503 with a JSON body listing the failed checks so
        orchestrators get actionable output, not just a status code.
        """
        checks = {
            "session_dir_writable": _check_writable(
                app.config.get("SESSION_FILE_DIR", "")),
            "storage_dir_writable": _check_writable(
                app.config.get("STORAGE_FOLDER", "")),
            "upload_dir_writable": _check_writable(
                app.config.get("UPLOAD_FOLDER", "")),
            "database_reachable": _db.ping(),
        }
        ok = all(checks.values())
        status_code = 200 if ok else 503
        return jsonify({
            "status": "ok" if ok else "degraded",
            "checks": checks,
            "uptime_seconds": round(time.time() - _boot_ts, 1),
        }), status_code

    @app.route("/metrics", methods=["GET"])
    def metrics():
        """JSON metrics snapshot — Prometheus-friendly field names.

        We emit JSON rather than Prometheus exposition format so the
        endpoint works without an extra dependency. Scrapers that
        expect the classic text format can be added later via a thin
        ``prometheus_client`` shim; the underlying counters here are
        the same.
        """
        q = get_queue()
        jobs = q.list_kind("automation") + q.list_kind("estimation")

        by_status = {PENDING: 0, RUNNING: 0, DONE: 0, FAILED: 0}
        by_kind: dict[str, int] = {}
        for j in jobs:
            by_status[j.status] = by_status.get(j.status, 0) + 1
            by_kind[j.kind] = by_kind.get(j.kind, 0) + 1

        # DB record counts — cheap and useful for sanity-checking a deploy.
        try:
            db_counts = _db.count_records()
        except Exception as exc:  # pragma: no cover — keep /metrics live even if DB is sad
            log.warning("metrics: db count failed: %s", exc)
            db_counts = {"error": "unavailable"}

        return jsonify({
            "uptime_seconds": round(time.time() - _boot_ts, 1),
            "job_queue": {
                "total_tracked": len(jobs),
                "by_status": by_status,
                "by_kind": by_kind,
                "in_flight": by_status[PENDING] + by_status[RUNNING],
            },
            "database": db_counts,
            "limits": {
                "max_content_length": app.config.get("MAX_CONTENT_LENGTH"),
                "chat_message_max_chars": app.config.get(
                    "CHAT_MESSAGE_MAX_CHARS"),
                "chat_history_max_entries": app.config.get(
                    "CHAT_HISTORY_MAX_ENTRIES"),
            },
        })


__all__ = ["register"]
