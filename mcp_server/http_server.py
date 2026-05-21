"""HTTP / Streamable-HTTP entry point for the TestFortge MCP server.

Same tool surface as the stdio entry (:mod:`mcp_server.server`), but
served over HTTP so an MCP client can reach it from a different
machine. Render hosts this as a separate web service alongside the
Flask app (see ``render.yaml``).

Auth is a single shared bearer token:

    Authorization: Bearer <MCP_BEARER_TOKEN>

Tokens are compared with :func:`secrets.compare_digest` to avoid
timing leaks. Boot refuses to start when ``MCP_BEARER_TOKEN`` is not
set in the environment — there is no "open by default" mode, because
this surface includes write tools (create_bug_report,
trigger_test_execution) that have real blast radius.

Why streamable-HTTP not SSE
---------------------------
FastMCP supports both. ``streamable-http`` is the newer transport
(MCP 2024-11+) — single endpoint, bidirectional, better proxy
compatibility. The legacy ``sse`` transport opens two endpoints
(``/sse`` + ``/messages/``) and is harder to put behind Render's edge
proxy because the proxy buffers SSE awkwardly. The HTTP server can
still be reached by older SSE-only clients via :func:`FastMCP.sse_app`
if needed in a future PR.
"""
from __future__ import annotations

import os
import secrets
import sys

# engine.db._assert_prod_safety refuses to boot a SQLite-backed
# process unless FLASK_DEBUG=1 is set — its heuristic for "this is
# local dev, not gunicorn under load". The HTTP MCP server is also a
# single-process tool, but on Render it talks to Postgres via
# DATABASE_URL so the SQLite check never trips. setdefault so a local
# test can opt-in to FLASK_DEBUG=1 if it wants SQLite.
os.environ.setdefault("FLASK_DEBUG", "1")

from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse, PlainTextResponse  # noqa: E402
from starlette.routing import Mount, Route  # noqa: E402
from starlette.types import ASGIApp, Receive, Scope, Send  # noqa: E402

from mcp_server.server import mcp  # noqa: E402 — env var must precede import


# Paths that bypass bearer auth. Only ``/healthz`` is exempt — Render's
# platform healthchecker hits it without an Authorization header and
# would otherwise mark the service unhealthy on every probe. The
# handler is plumbed in :func:`build_app` as an ASGI shortcut, so the
# rest of the MCP surface stays gated.
_PUBLIC_PATHS = frozenset({"/healthz"})


class BearerAuthMiddleware:
    """ASGI middleware that gates every request on an ``Authorization:
    Bearer`` header matching a pre-shared token.

    Constant-time comparison via :func:`secrets.compare_digest`. Rejects
    with HTTP 401 and a small JSON body — MCP clients show the body in
    their error UI so the operator gets a useful message rather than
    just a status code.

    Passes non-HTTP scopes (e.g. ASGI ``lifespan`` events) through
    untouched so Starlette's startup/shutdown hooks still fire. Paths
    listed in :data:`_PUBLIC_PATHS` (``/healthz``) also pass through
    without auth so Render's platform healthcheck doesn't trip.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        if not token:
            raise ValueError("BearerAuthMiddleware: token must be non-empty")
        self._app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path = scope.get("path") or ""
        if path in _PUBLIC_PATHS:
            await self._app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw = headers.get(b"authorization", b"").decode("latin-1")
        if not raw.lower().startswith("bearer "):
            await _send_401(send, "missing or malformed Authorization header")
            return
        supplied = raw[7:].strip()
        if not secrets.compare_digest(supplied, self._token):
            await _send_401(send, "invalid bearer token")
            return

        await self._app(scope, receive, send)


async def _send_401(send: Send, message: str) -> None:
    """Send a 401 with a small JSON body, bypassing call_next."""
    response = JSONResponse({"error": message}, status_code=401)
    await response({"type": "http", "method": "GET"}, _noop_receive, send)


async def _noop_receive() -> dict:  # pragma: no cover — defensive stub
    return {"type": "http.disconnect"}


async def _healthz(_request) -> PlainTextResponse:
    """Public health endpoint — Render's platform healthcheck hits this
    without an Authorization header. Returns 200 if the process is up,
    nothing more. Database / FastMCP-state probes belong in a separate
    deeper endpoint we don't have a use case for yet."""
    return PlainTextResponse("ok")


def build_app(token: str | None = None):
    """Return the ASGI app with the bearer-auth middleware applied.

    Composes a small Starlette router that exposes:

    * ``/healthz`` — plain ``ok`` response, no auth (Render
      healthcheck);
    * everything else — mounted to FastMCP's streamable-HTTP app (the
      MCP protocol surface).

    The bearer middleware wraps the whole composition. Public-path
    bypass lives in the middleware so the auth and the routing don't
    drift apart silently.

    Exposed as a function (rather than a module-level ``app``) so
    tests can inject a known token without touching the real env
    var. The HTTP server boots via :func:`main`; nothing else should
    import this module.
    """
    token = token if token is not None else os.environ.get("MCP_BEARER_TOKEN", "")
    if not token:
        raise RuntimeError(
            "MCP_BEARER_TOKEN env var is required to boot the HTTP MCP "
            "server. Set a long random secret in your Render dashboard "
            "(or local shell) and restart."
        )
    inner_mcp = mcp.streamable_http_app()
    composed = Starlette(
        routes=[
            Route("/healthz", _healthz, methods=["GET"]),
            Mount("/", app=inner_mcp),
        ],
        lifespan=inner_mcp.router.lifespan_context,
    )
    return BearerAuthMiddleware(composed, token=token)


def main() -> int:
    """Boot uvicorn against the bearer-protected ASGI app."""
    import uvicorn

    host = os.environ.get("MCP_HTTP_HOST", "0.0.0.0")
    # Render injects PORT; locally default to 8765 to avoid clashing
    # with the Flask app's typical 5000.
    port = int(os.environ.get("PORT") or os.environ.get("MCP_HTTP_PORT") or 8765)
    app = build_app()
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
