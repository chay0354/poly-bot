"""Chainlink BTC/USD price feed via Polymarket's live-data websocket.

This is the *resolution source* for the 5-minute markets, so it is the right
feed to compare against the window open. Emits roughly one update per second.

Subscription message (discovered empirically). NOTE: passing a server-side
``filters`` for this topic silently drops *all* updates, so we subscribe
without a filter and select btc/usd client-side:

    {"action": "subscribe",
     "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "update"}]}

The topic streams several symbols (btc, eth, sol, ...); btc/usd arrives ~1/s.

Each update looks like:

    {"payload": {"symbol": "btc/usd",
                 "timestamp": 1781266577000,    # ms, price-source time
                 "value": 63547.756196979666},
     "topic": "crypto_prices_chainlink", "type": "update"}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import websockets

log = logging.getLogger("pm5.pricefeed")


@dataclass
class Tick:
    price: float
    src_ts: float  # seconds, from the price source
    recv_ts: float  # seconds, local receive time


class ChainlinkFeed:
    """Maintains the latest BTC/USD tick and a short rolling history."""

    SYMBOL = "btc/usd"
    SUBSCRIBE = {
        "action": "subscribe",
        "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "update"}],
    }

    # History must outlive a full 5-minute window so the window-open tick is
    # still present at settlement time (300s later).
    def __init__(self, ws_url: str, host: str, history_secs: float = 360.0) -> None:
        self._url = ws_url
        self._host = host
        self._history_secs = history_secs
        self.latest: Tick | None = None
        self._history: list[Tick] = []
        self._first_src_ts: float | None = None
        self._connected = asyncio.Event()

    async def run(self) -> None:
        """Connect-and-consume loop with reconnect. Run as a background task."""
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    self._url, server_hostname=self._host, ping_interval=20
                ) as ws:
                    await ws.send(json.dumps(self.SUBSCRIBE))
                    self._connected.set()
                    backoff = 1.0
                    log.info("price feed connected")
                    async for raw in ws:
                        self._ingest(raw)
            except Exception as e:  # noqa: BLE001 - reconnect on anything
                self._connected.clear()
                log.warning("price feed disconnected (%s); reconnecting in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _ingest(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if msg.get("topic") != "crypto_prices_chainlink":
            return
        payload = msg.get("payload")
        if not isinstance(payload, dict) or "value" not in payload:
            return
        if payload.get("symbol") != self.SYMBOL:
            return  # topic carries several symbols; keep only BTC
        try:
            price = float(payload["value"])
        except (TypeError, ValueError):
            return
        src_ts = float(payload.get("timestamp", 0)) / 1000.0 or time.time()
        tick = Tick(price=price, src_ts=src_ts, recv_ts=time.time())
        self.latest = tick
        if self._first_src_ts is None:
            self._first_src_ts = src_ts
        self._history.append(tick)
        cutoff = time.time() - self._history_secs
        # Trim from the front; history is naturally time-ordered.
        i = 0
        while i < len(self._history) and self._history[i].recv_ts < cutoff:
            i += 1
        if i:
            self._history = self._history[i:]

    async def wait_connected(self, timeout: float = 15.0) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def witnessed_open(self, window_start: float) -> bool:
        """True only if we were already streaming before the window opened.

        If the bot started mid-window we never saw the true opening price, so
        momentum and paper settlement for that window would be unreliable.
        """
        return self._first_src_ts is not None and self._first_src_ts <= window_start

    def price_at_or_after(self, ts: float) -> float | None:
        """Best estimate of the price at window open: first tick at/after ts."""
        for t in self._history:
            if t.src_ts >= ts:
                return t.price
        return None

    def momentum(self, lookback_secs: float) -> float | None:
        """Signed price change over the last `lookback_secs` of history."""
        if not self.latest or not self._history:
            return None
        cutoff = self.latest.recv_ts - lookback_secs
        ref = None
        for t in self._history:
            if t.recv_ts >= cutoff:
                ref = t
                break
        if ref is None:
            return None
        return self.latest.price - ref.price
