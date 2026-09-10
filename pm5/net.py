"""Network helpers.

The local resolver on some machines refuses to resolve Polymarket domains. We
fall back to DNS-over-HTTPS (Cloudflare) only for the hosts that fail, and only
for resolution -- traffic still goes directly to the resolved IPs over TLS with
the correct SNI/Host. This does not bypass any firewall, just a broken/filtered
local DNS.
"""

from __future__ import annotations

import socket
import threading

import httpx

_DOH_URL = "https://cloudflare-dns.com/dns-query"
_lock = threading.Lock()
_overrides: dict[str, str] = {}
_patched = False


def _doh_resolve(host: str) -> str | None:
    try:
        r = httpx.get(
            _DOH_URL,
            params={"name": host, "type": "A"},
            headers={"accept": "application/dns-json"},
            timeout=10,
        )
        r.raise_for_status()
        for ans in r.json().get("Answer", []):
            if ans.get("type") == 1:  # A record
                return ans["data"]
    except Exception:
        return None
    return None


def ensure_resolvable(*hosts: str) -> None:
    """Make `hosts` resolvable, falling back to DoH if the local resolver fails.

    Idempotent. Safe to call repeatedly.
    """
    global _patched
    with _lock:
        for host in hosts:
            if host in _overrides:
                continue
            try:
                socket.getaddrinfo(host, 443)
                continue  # local DNS already works
            except socket.gaierror:
                pass
            ip = _doh_resolve(host)
            if ip:
                _overrides[host] = ip

        if _overrides and not _patched:
            _install_patch()
            _patched = True


def _install_patch() -> None:
    real_getaddrinfo = socket.getaddrinfo

    def patched(host, *args, **kwargs):
        target = _overrides.get(host, host)
        return real_getaddrinfo(target, *args, **kwargs)

    socket.getaddrinfo = patched


# Hosts the bot talks to.
GAMMA_HOST = "gamma-api.polymarket.com"
CLOB_HOST = "clob.polymarket.com"
WS_LIVE_HOST = "ws-live-data.polymarket.com"
WS_CLOB_HOST = "ws-subscriptions-clob.polymarket.com"


def bootstrap() -> None:
    """Resolve every Polymarket host the bot needs, up front."""
    ensure_resolvable(GAMMA_HOST, CLOB_HOST, WS_LIVE_HOST, WS_CLOB_HOST)
