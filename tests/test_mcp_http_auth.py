"""Tests for the bearer-auth middleware on the HTTP MCP server.

We don't boot a real FastMCP HTTP transport here — that requires the
session manager's lifespan and is exercised end-to-end on Render. The
middleware itself is pure ASGI plumbing: token-equality check before
delegation. We unit-test it by wiring an ASGI stub through it and
asserting on the 200 / 401 outcomes.
"""

from __future__ import annotations

import os

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_server import http_server


def _stub_inner_app(name: str = "ok") -> Starlette:
    """Tiny Starlette app that returns 200 + a body so we know the
    middleware delegated correctly."""

    async def handler(_request):
        return PlainTextResponse(name)

    return Starlette(routes=[Route("/", handler), Route("/mcp", handler)])


class TestBearerAuthMiddleware:
    TOKEN = "sk-mcp-test-" + "a" * 32  # plausible-looking secret

    def _client(self, token: str = TOKEN) -> TestClient:
        wrapped = http_server.BearerAuthMiddleware(
            _stub_inner_app(), token=token
        )
        return TestClient(wrapped)

    def test_request_with_valid_token_passes_through(self):
        client = self._client()
        resp = client.get(
            "/mcp",
            headers={"Authorization": f"Bearer {self.TOKEN}"},
        )
        assert resp.status_code == 200
        assert resp.text == "ok"

    def test_missing_header_is_rejected_with_401(self):
        client = self._client()
        resp = client.get("/mcp")
        assert resp.status_code == 401
        body = resp.json()
        assert "missing" in body["error"].lower() or "malformed" in body["error"].lower()

    def test_wrong_token_is_rejected_with_401(self):
        client = self._client()
        resp = client.get(
            "/mcp",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401
        assert "invalid" in resp.json()["error"].lower()

    def test_non_bearer_scheme_is_rejected(self):
        client = self._client()
        resp = client.get(
            "/mcp",
            headers={"Authorization": f"Basic {self.TOKEN}"},
        )
        assert resp.status_code == 401

    def test_bearer_case_insensitive(self):
        """Per RFC 7235, the scheme is case-insensitive. The middleware
        should accept ``bearer`` and ``BEARER`` as well as ``Bearer``."""
        client = self._client()
        for prefix in ("bearer", "BEARER", "Bearer", "BeArEr"):
            resp = client.get(
                "/mcp",
                headers={"Authorization": f"{prefix} {self.TOKEN}"},
            )
            assert resp.status_code == 200, f"prefix={prefix!r} was rejected"

    def test_empty_token_in_constructor_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            http_server.BearerAuthMiddleware(_stub_inner_app(), token="")

    def test_build_app_without_env_raises(self, monkeypatch):
        # ``build_app`` falls back to MCP_BEARER_TOKEN when its arg is
        # None. With the env unset boot must refuse — we never want a
        # silently-unauthenticated HTTP MCP server in front of write
        # tools.
        monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="MCP_BEARER_TOKEN"):
            http_server.build_app()

    def test_build_app_with_token_returns_callable(self):
        # Passing the token explicitly bypasses the env lookup. We
        # don't exercise the inner FastMCP app here — just confirm
        # build_app wires the middleware around it.
        app = http_server.build_app(token=self.TOKEN)
        assert callable(app)
        # The middleware is the outermost layer.
        assert isinstance(app, http_server.BearerAuthMiddleware)

    def test_healthz_bypasses_auth(self):
        """Render's platform healthcheck hits ``/healthz`` without an
        Authorization header. The middleware MUST let that through —
        otherwise Render marks the service as unhealthy and restarts
        it every probe interval."""
        app = http_server.build_app(token=self.TOKEN)
        client = TestClient(app)
        # No header at all.
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.text == "ok"

    def test_other_paths_still_require_auth_with_built_app(self):
        """Sanity: the auth bypass is path-scoped, not global. Any
        other path must still 401 without a header."""
        app = http_server.build_app(token=self.TOKEN)
        client = TestClient(app)
        resp = client.get("/mcp")
        assert resp.status_code == 401
