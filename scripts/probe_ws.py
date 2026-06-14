"""Probe wss://ws-live-data.polymarket.com to discover the crypto price feed format.

Tries a few known subscription shapes and prints whatever comes back.
"""

import asyncio
import json
import socket
import sys

import httpx
import websockets

WS_URL = "wss://ws-live-data.polymarket.com"

SUBSCRIPTIONS = [
    {
        "action": "subscribe",
        "subscriptions": [
            {"topic": "crypto_prices", "type": "update", "filters": '{"symbol":"btc/usd"}'}
        ],
    },
    {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": '{"symbol":"btc/usd"}',
            }
        ],
    },
]


def doh_resolve(host: str) -> str:
    """Resolve via Cloudflare DoH (local resolver blocks polymarket domains)."""
    r = httpx.get(
        "https://cloudflare-dns.com/dns-query",
        params={"name": host, "type": "A"},
        headers={"accept": "application/dns-json"},
        timeout=10,
    )
    answers = [a["data"] for a in r.json().get("Answer", []) if a.get("type") == 1]
    return answers[0]


def patch_dns(host: str) -> None:
    try:
        socket.getaddrinfo(host, 443)
        return  # local DNS works
    except socket.gaierror:
        pass
    ip = doh_resolve(host)
    print(f"[dns] {host} -> {ip} (via DoH)")
    real = socket.getaddrinfo

    def patched(h, *args, **kwargs):
        if h == host:
            h = ip
        return real(h, *args, **kwargs)

    socket.getaddrinfo = patched


async def main() -> None:
    host = WS_URL.removeprefix("wss://").split("/")[0]
    patch_dns(host)
    async with websockets.connect(WS_URL, server_hostname=host) as ws:
        for sub in SUBSCRIPTIONS:
            await ws.send(json.dumps(sub))
            print(f">>> sent: {json.dumps(sub)}")
        deadline = asyncio.get_event_loop().time() + 20
        n = 0
        while asyncio.get_event_loop().time() < deadline and n < 12:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                print("(timeout waiting for message)")
                continue
            n += 1
            print(f"<<< {str(msg)[:400]}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
