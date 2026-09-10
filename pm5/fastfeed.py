"""Binance BTCUSDT trade stream: an *early warning*, not a settlement source.

The market makers who hit our resting bids price off Binance, which leads
the Chainlink feed by a few seconds. Every adverse maker fill on 9 Sep was a
sweep that Chainlink only showed after the fact. We watch Binance purely to
pull a bid before it is run over; the open, the TWAP and settlement stay on
Chainlink (that is what the market resolves on).

Public stream, no key. Payload (aggTrade):
    {"e":"aggTrade","s":"BTCUSDT","p":"78800.10","q":"0.01","T":1788975600123,...}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

from .pricefeed import Tick

log = logging.getLogger("pm5.fastfeed")


class BinanceFeed:
    def __init__(self, url: str, history_secs: float = 360.0, stale_secs: float = 5.0) -> None:
        self._url = url
        self._history_secs = history_secs
        self._stale_secs = stale_secs
        self.latest: Tick | None = None
        self._history: list[Tick] = []

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self._url, ping_interval=20) as ws:
                    backoff = 1.0
                    log.info("fast feed connected (binance)")
                    async for raw in ws:
                        self._ingest(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - reconnect on anything
                log.warning("fast feed disconnected (%s); reconnecting in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _ingest(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(msg, dict) or "p" not in msg:
            return
        try:
            price = float(msg["p"])
        except (TypeError, ValueError):
            return
        src_ts = float(msg.get("T") or 0) / 1000.0 or time.time()
        now = time.time()
        tick = Tick(price=price, src_ts=src_ts, recv_ts=now)
        self.latest = tick
        self._history.append(tick)
        cutoff = now - self._history_secs
        i = 0
        while i < len(self._history) and self._history[i].recv_ts < cutoff:
            i += 1
        if i:
            self._history = self._history[i:]

    @property
    def fresh(self) -> bool:
        return self.latest is not None and time.time() - self.latest.recv_ts <= self._stale_secs

    def price_at_or_after(self, ts: float) -> float | None:
        for t in self._history:
            if t.src_ts >= ts:
                return t.price
        return None

    def delta_since(self, ts: float) -> float | None:
        """Binance move since `ts` (window open), or None if we cannot know
        it honestly: no trade at/after `ts` in history, or the stream is stale.
        """
        if not self.fresh:
            return None
        ref = self.price_at_or_after(ts)
        if ref is None:
            return None
        return self.latest.price - ref

    def realized_vol(self, secs: float) -> float | None:
        """High−low over the last `secs` (USD), same definition as the
        Chainlink feed's. Binance trades every ~100ms, so this is usable
        within a couple of minutes of connecting and it is the tape the
        fair-value model should be calibrated on (the makers hitting us
        trade off it). None until half the horizon is covered."""
        if not self.fresh or len(self._history) < 2:
            return None
        cutoff = self.latest.src_ts - secs
        hi = lo = None
        for t in self._history:
            if t.src_ts < cutoff:
                continue
            hi = t.price if hi is None else max(hi, t.price)
            lo = t.price if lo is None else min(lo, t.price)
        covered = self.latest.src_ts - max(cutoff, self._history[0].src_ts)
        if hi is None or covered < secs * 0.5:
            return None
        return hi - lo
