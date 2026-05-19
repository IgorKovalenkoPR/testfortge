"""TestFortge — SSRF allowlist for outbound HTTP fetches.

Single source of truth for "is it safe to navigate / fetch this URL?".
Used to wrap every site where operator-controlled input flows into
``page.goto`` (Playwright), ``urllib.request.urlopen``, or
``requests.get``.

The threat: TestForTge runs on-prem and operators paste arbitrary URLs
into requirements / base URLs / mockup links. Without a guard, those
URLs can target ``127.0.0.1`` (admin endpoints), RFC1918 ranges
(internal services), or ``169.254.169.254`` (AWS/GCP cloud metadata)
and exfiltrate screenshots / videos / page HTML through our own
bug-report and crawl pipelines.

Public API
----------
``is_safe_external_url(url)`` — boolean check, never raises.
``require_safe_url(url)`` — raises :class:`UnsafeUrlError` if blocked.
``UnsafeUrlError`` — subclass of :class:`ValueError`.

DNS rebinding mitigation
------------------------
We resolve every A/AAAA record for the hostname via
:func:`socket.getaddrinfo`. If *any* answer is internal we refuse,
so an attacker can't bypass us with a record that mixes one public IP
and one ``127.0.0.1``.

Opt-out
-------
Setting ``SSRF_ALLOWLIST_BYPASS=1`` in the environment short-circuits
:func:`require_safe_url` to a no-op. Operators running TestForTge
against an explicit staging box at ``192.168.x.y`` use this. Documented
in CHANGELOG.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse


# Networks we always refuse. ``ipaddress.IPv4Address.is_private`` already
# covers most of these but we keep the explicit list so the policy is
# auditable in one place. ``0.0.0.0/8`` and ``169.254.0.0/16`` matter
# specifically because the latter hosts cloud metadata.
_BLOCKED_NETS: list = [
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8",      # loopback
        "10.0.0.0/8",       # RFC1918
        "172.16.0.0/12",    # RFC1918
        "192.168.0.0/16",   # RFC1918
        "169.254.0.0/16",   # link-local + cloud metadata (AWS/GCP/Azure)
        "0.0.0.0/8",        # "this network"
        "::1/128",          # IPv6 loopback
        "fc00::/7",         # IPv6 unique-local
        "fe80::/10",        # IPv6 link-local
    )
]

_ALLOWED_SCHEMES = {"http", "https"}

# Names that bind to the local box but skip the IP heuristics if anyone
# tries to feed them in raw.
_BLOCKED_HOSTNAMES = {
    "localhost",
    "ip6-localhost",
    "ip6-loopback",
    # Common metadata aliases.
    "metadata.google.internal",
    "metadata",
}


class UnsafeUrlError(ValueError):
    """Raised when an outbound URL is rejected by the SSRF policy."""


def _bypass_active() -> bool:
    """Read the ``SSRF_ALLOWLIST_BYPASS`` env flag fresh each call.

    Fresh-read keeps the test suite simple: monkeypatching the env var
    in a single test doesn't leak into later tests.
    """
    return os.environ.get("SSRF_ALLOWLIST_BYPASS", "").strip() == "1"


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    """Return True if the resolved IP must be refused."""
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    for net in _BLOCKED_NETS:
        # ``ip in net`` raises if address families don't match; guard.
        try:
            if ip in net:
                return True
        except TypeError:
            continue
    return False


def is_safe_external_url(url: str) -> bool:
    """Return True iff ``url`` is safe to fetch from a server context.

    Never raises. A return value of ``False`` means: bad scheme, no
    host, the host failed DNS, or *any* resolved IP is internal.
    """
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in _BLOCKED_HOSTNAMES:
        return False

    # Literal IP — skip DNS, check directly.
    try:
        ip_lit = ipaddress.ip_address(host)
    except ValueError:
        ip_lit = None
    if ip_lit is not None:
        return not _ip_is_blocked(ip_lit)

    # Hostname — resolve every record. DNS-rebinding-resistant: if any
    # answer is private/loopback/link-local, refuse the whole URL.
    port = parsed.port
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return False
    except Exception:
        return False
    if not infos:
        return False
    for fam, _stype, _proto, _canon, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except (ValueError, IndexError):
            return False
        if _ip_is_blocked(ip):
            return False
    return True


def require_safe_url(url: str) -> None:
    """Raise :class:`UnsafeUrlError` unless ``url`` passes the allowlist.

    Honors the ``SSRF_ALLOWLIST_BYPASS=1`` env opt-out. Use this at
    every outbound fetch site where the URL is operator-controlled.
    """
    if _bypass_active():
        return
    if not is_safe_external_url(url):
        # Truncate the displayed URL — operator paste-bombs are common.
        shown = (url or "")[:200]
        raise UnsafeUrlError(f"URL blocked by SSRF policy: {shown!r}")
