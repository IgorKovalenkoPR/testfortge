"""TestFortge — Operations endpoints (Wave D observability).

  * GET /healthz  — **liveness** probe (process up + dirs writable, no DB)
  * GET /readyz   — **readiness** probe (liveness checks PLUS database)
  * GET /metrics  — JSON snapshot of job-queue depth + session counts

All three endpoints are deliberately **unauthenticated** so container
orchestrators (k8s, Docker healthcheck, uptime pingers) can hit them
without secrets. They intentionally expose **no user data** — only
infrastructure-level counts and paths.

Liveness vs readiness — why they are split
------------------------------------------
``/healthz`` is what the platform health-check pings (see
``render.yaml`` → ``healthCheckPath: /healthz``). It answers a single
question: *is this process alive and able to serve HTTP?* It deliberately
does **not** touch the database, because a transient/expired DB must not
take the whole web service offline. On Render's free tier the Postgres
add-on expires roughly every 30 days ("Suspended by Render"); when the
health-check was DB-gated, that expiry flipped ``/healthz`` to 503 and
Render then refused to route ANY traffic — even to pages that need no
DB — leaving the site stuck on the "Application loading" interstitial.

``/readyz`` keeps the deeper contract: it returns 503 the moment the DB
(or any liveness check) is unhappy, so an external monitor / a fronting
load balancer can alert or drain traffic without killing the service.
Point uptime pingers at ``/readyz`` when you want DB-aware alerting.

If you ever deploy behind a LB/CDN that proxies the public internet,
put these behind an internal listener or an IP allowlist. For the
in-house / on-prem deployments TestFortge currently targets, open is
fine.
"""

from __future__ import annotations

import hmac
import os
import time

from flask import Flask, Response, current_app, jsonify, request

from engine import db as _db
from engine.job_queue import DONE, FAILED, PENDING, RUNNING, get_queue
from engine.log import get_logger

log = get_logger(__name__)


def _ops_token() -> str:
    """Read ``OPS_ENDPOINTS_TOKEN`` fresh on each request.

    Reading at request time (instead of caching the module-load value)
    lets tests flip the env var with ``monkeypatch.setenv`` without
    restarting the app. The empty-string default means the gate stays
    disabled by default — existing deployments are not surprised by a
    sudden 401 after upgrading.
    """
    return (os.environ.get("OPS_ENDPOINTS_TOKEN") or "").strip()


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

    def _liveness_checks() -> dict[str, bool]:
        """Filesystem checks that decide whether the process can serve.

        These are the checks that, if failed, mean the container itself
        is broken and no amount of retrying a downstream dependency will
        help. Kept DB-free on purpose — see the module docstring.
        """
        return {
            "session_dir_writable": _check_writable(
                app.config.get("SESSION_FILE_DIR", "")),
            "storage_dir_writable": _check_writable(
                app.config.get("STORAGE_FOLDER", "")),
            "upload_dir_writable": _check_writable(
                app.config.get("UPLOAD_FOLDER", "")),
        }

    @app.route("/healthz", methods=["GET"])
    def healthz():
        """Cheap **liveness** probe (no database).

        Returns 200 when the Flask process is up and the session,
        storage and upload directories exist + are writable. Anything
        else returns 503 with a JSON body listing the failed checks so
        orchestrators get actionable output, not just a status code.

        Deliberately does NOT ping the database: this is the path the
        platform health-check hits, and a DB outage must degrade
        functionality gracefully rather than take the whole service
        down. Use ``/readyz`` for a DB-aware probe.
        """
        checks = _liveness_checks()
        ok = all(checks.values())
        status_code = 200 if ok else 503
        return jsonify({
            "status": "ok" if ok else "degraded",
            "checks": checks,
            "uptime_seconds": round(time.time() - _boot_ts, 1),
        }), status_code

    @app.route("/readyz", methods=["GET"])
    def readyz():
        """**Readiness** probe — liveness checks PLUS the database.

        Returns 200 only when the process is live AND a ``SELECT 1``
        against the configured database succeeds. Returns 503 with a
        JSON body listing every check (including ``database_reachable``)
        otherwise. Intended for external monitors / fronting load
        balancers that want DB-aware alerting or traffic draining
        without tearing the service down the way a failed platform
        health-check would.
        """
        checks = _liveness_checks()
        checks["database_reachable"] = _db.ping()
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

        Sprint 4 task 4.5: when ``OPS_ENDPOINTS_TOKEN`` is set in the
        env, callers must present a matching ``X-Ops-Token`` header.
        When unset, the endpoint stays open (existing deployments are
        unaffected). ``/healthz`` is intentionally not gated — k8s /
        Docker probes need it unauthenticated.
        """
        token = _ops_token()
        if token:
            header = request.headers.get("X-Ops-Token", "")
            if not hmac.compare_digest(
                    header.encode("utf-8"), token.encode("utf-8")):
                # 403, not 401. This was `Response("Forbidden", status=401)`
                # — a 401 whose body used 403's wording, and with no
                # `WWW-Authenticate` header, which RFC 9110 requires on a
                # 401 so a client can learn how to authenticate.
                #
                # 403 is the honest code rather than adding the header:
                # `X-Ops-Token` is a proprietary header, not a registered
                # HTTP auth scheme, so there is no scheme to name. Where
                # this codebase does use real HTTP auth it already sends
                # the header correctly (engine/basic_auth.py).
                return Response("Forbidden\n", status=403,
                                content_type="text/plain; charset=utf-8")
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
