"""Unit tests for the SSRF allowlist (engine.security).

Covers the five guarantees from docs/plans/sprint_1_security.md, Task 1:
  1. localhost variants are blocked.
  2. RFC1918 + link-local + cloud metadata are blocked.
  3. DNS rebinding (public name → private IP) is blocked.
  4. Public HTTPS is allowed.
  5. AutomationRunner._run_step on a blocked URL marks the step "failed"
     with a comment containing "blocked by SSRF policy".
"""
from __future__ import annotations

import os
import socket
from unittest.mock import patch, MagicMock

import pytest

from engine.security import (
    is_safe_external_url,
    require_safe_url,
    UnsafeUrlError,
)


# ── Fixture: make sure SSRF_ALLOWLIST_BYPASS is OFF for each test ──

@pytest.fixture(autouse=True)
def _no_bypass(monkeypatch):
    """Each test runs with the allowlist enforced."""
    monkeypatch.delenv("SSRF_ALLOWLIST_BYPASS", raising=False)
    yield


# ── 1. localhost variants ─────────────────────────────────────────

def test_blocks_localhost_variants():
    """All flavours of `127.0.0.1` and ipv6 loopback are refused."""
    blocked = [
        "http://127.0.0.1/",
        "http://127.0.0.1:5000/admin",
        "http://localhost/",
        "http://localhost:8080/",
        "http://[::1]/",
        "http://127.0.0.5/",          # 127/8 — anywhere in the range
        "http://0.0.0.0/",            # "this network"
    ]
    for url in blocked:
        assert not is_safe_external_url(url), (
            f"{url!r} should be refused by SSRF policy")
        with pytest.raises(UnsafeUrlError):
            require_safe_url(url)


# ── 2. RFC1918 + link-local ───────────────────────────────────────

def test_blocks_rfc1918_and_link_local():
    """Private LAN ranges and the cloud-metadata endpoint are refused."""
    blocked = [
        "http://10.0.0.5/",             # RFC1918
        "http://10.255.255.255/api",
        "http://172.16.0.1/",           # RFC1918
        "http://172.31.255.1/",
        "http://192.168.1.1/",          # RFC1918 (home routers)
        "http://192.168.0.100:8080/",
        "http://169.254.169.254/latest/meta-data/",  # AWS / GCP metadata
        "http://169.254.1.1/",           # link-local generic
        "http://[fe80::1]/",             # IPv6 link-local
        "http://[fc00::1]/",             # IPv6 unique-local
    ]
    for url in blocked:
        assert not is_safe_external_url(url), (
            f"{url!r} should be refused by SSRF policy")
        with pytest.raises(UnsafeUrlError):
            require_safe_url(url)


# ── 3. DNS rebinding ──────────────────────────────────────────────

def test_dns_rebinding_blocked(monkeypatch):
    """A public hostname that resolves to a private IP is still blocked.

    Real DNS-rebinding attacks: an attacker controls evil.example.com
    DNS, returns 8.8.8.8 on first lookup, then 127.0.0.1 on the second.
    Our guard re-resolves at each call and refuses any private answer.
    """
    # Stub getaddrinfo: any hostname returns one A record at 192.168.1.5.
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("192.168.1.5", port or 0),
        )]
    monkeypatch.setattr("engine.security.socket.getaddrinfo", fake_getaddrinfo)

    url = "https://evil.example.com/"
    assert not is_safe_external_url(url)
    with pytest.raises(UnsafeUrlError):
        require_safe_url(url)


def test_dns_rebinding_mixed_answers_blocked(monkeypatch):
    """If getaddrinfo returns BOTH a public and a private IP, refuse."""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
             ("8.8.8.8", port or 0)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
             ("10.0.0.1", port or 0)),
        ]
    monkeypatch.setattr("engine.security.socket.getaddrinfo", fake_getaddrinfo)

    assert not is_safe_external_url("https://mixed.example.com/")


# ── 4. Public HTTPS allowed ───────────────────────────────────────

def test_allows_public_https(monkeypatch):
    """A normal public hostname/IP combo passes."""
    # Stub DNS so the test never hits the real network.
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("93.184.216.34", port or 0),   # public IP (example.com)
        )]
    monkeypatch.setattr("engine.security.socket.getaddrinfo", fake_getaddrinfo)

    assert is_safe_external_url("https://example.com/")
    assert is_safe_external_url("http://example.com:8080/path?q=1")
    require_safe_url("https://example.com/")     # does not raise


def test_allows_public_ip_literal():
    """An IPv4 literal that's already public skips DNS and passes."""
    assert is_safe_external_url("https://8.8.8.8/")
    assert is_safe_external_url("http://1.1.1.1/")


def test_rejects_non_http_schemes():
    """file:// / gopher:// / ftp:// would bypass any DNS-based guard."""
    for url in ("file:///etc/passwd",
                "gopher://example.com/",
                "ftp://example.com/",
                "javascript:alert(1)",
                "",
                "not a url"):
        assert not is_safe_external_url(url)


def test_bypass_env_disables_guard(monkeypatch):
    """``SSRF_ALLOWLIST_BYPASS=1`` makes require_safe_url a no-op."""
    monkeypatch.setenv("SSRF_ALLOWLIST_BYPASS", "1")
    # Even an obviously-internal URL passes when bypass is on.
    require_safe_url("http://127.0.0.1:5000/admin")    # no exception
    # is_safe_external_url itself still returns the truth-value — only
    # require_safe_url honors the opt-out.
    assert not is_safe_external_url("http://127.0.0.1/")


# ── 5. AutomationRunner integration: step marked "failed" ─────────

def test_step_goto_marks_failed(tmp_path):
    """A goto step targeting 127.0.0.1 surfaces as a failed StepResult
    whose comment contains 'blocked by SSRF policy'.

    We construct a *minimal* fake page so the AutomationRunner._run_step
    code path can execute up to the SSRF guard without needing a real
    Playwright browser. Only the methods touched before the guard fires
    are stubbed.
    """
    from engine.automation_runner import AutomationRunner
    from engine.automation_qa import AutomationStep

    runner = AutomationRunner(
        storage_root=str(tmp_path),
        base_url="http://example.com",   # passes guard, just a fallback
        headless=True,
        record_video=False,
        screenshot_before_steps=False,
    )

    # A fake page that records goto() calls. The SSRF guard fires before
    # goto() is reached so the recorder must stay empty.
    fake_page = MagicMock()
    fake_page.url = ""               # used by `cur = page.url`
    # _screenshot writes a real file via page.screenshot — short-circuit
    # the runner's _screenshot to a noop so we don't need Playwright.
    runner._screenshot = MagicMock(return_value=None)
    # _live_pump and _visible_scroll also call page methods — stub them.
    runner._live_pump = MagicMock(return_value=None)
    runner._visible_scroll = MagicMock(return_value=None)

    step = AutomationStep(action="goto", target="http://127.0.0.1:5000/admin",
                           raw="Navigate to admin panel")
    tc_dir = str(tmp_path / "tc_001")
    os.makedirs(tc_dir, exist_ok=True)

    sr = runner._run_step(fake_page, step, idx=1, tc_dir=tc_dir)

    assert sr.status == "failed", (
        f"expected failed; got status={sr.status!r} comment={sr.comment!r}")
    assert "blocked by SSRF policy" in sr.comment, (
        f"expected SSRF reason in comment; got {sr.comment!r}")
    # Verify goto was NEVER actually called.
    fake_page.goto.assert_not_called()
