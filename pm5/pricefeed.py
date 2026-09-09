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


@dataclass
class TwapProjection:
    """Where the settlement TWAP lands if the price holds from now to close."""

    twap: float
    known_secs: float      # part of the TWAP window already locked in
    remaining_secs: float  # part still to come
    known_mean: float      # TWAP of the locked-in part
    last: float            # latest price

    def flip_needed(self, open_price: float) -> float:
        """USD move (from the latest price) BTC must *average* over the
        remaining seconds to flip the outcome. Positive = the current side is
        safe by that much; grows quickly as the window locks in.

        Returns +inf when nothing remains (outcome fixed) and the projection is
        on one side, 0 when projection == open.
        """
        delta = self.twap - open_price
        if delta == 0:
            return 0.0
        if self.remaining_secs <= 0:
            return float("inf")
        # Final TWAP flips when the remaining-period mean m_r satisfies
        # (known*known_mean + remaining*m_r)/L == open  =>
        # m_r = (L*open - known*known_mean)/remaining.
        total = self.known_secs + self.remaining_secs
        m_r = (total * open_price - self.known_secs * self.known_mean) / self.remaining_secs
        return abs(self.last - m_r)

    @property
    def locked_frac(self) -> float:
        total = self.known_secs + self.remaining_secs
        return self.known_secs / total if total else 0.0


def _twap(history: list[Tick], start_ts: float, end_ts: float) -> float | None:
    """Step-function time-weighted mean of `history` over [start_ts, end_ts]."""
    area = 0.0
    covered = 0.0
    prev: Tick | None = None
    for t in history:
        if prev is not None and prev.src_ts < end_ts and t.src_ts > start_ts:
            a = max(prev.src_ts, start_ts)
            b = min(t.src_ts, end_ts)
            if b > a:
                area += prev.price * (b - a)
                covered += b - a
        prev = t
    # The last tick holds until end_ts.
    if prev is not None and prev.src_ts < end_ts:
        a = max(prev.src_ts, start_ts)
        if end_ts > a:
            area += prev.price * (end_ts - a)
            covered += end_ts - a
    if covered <= 0:
        # Range lies before every tick we have: use the first tick as the
        # best available estimate.
        return history[0].price if history else None
    return area / covered


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

    def twap(self, start_ts: float, end_ts: float) -> float | None:
        """Time-weighted average price over [start_ts, end_ts] (source time).

        Ticks are treated as a step function (each price holds until the next
        tick), which is how the Chainlink TWAP stream the market resolves on
        behaves. Returns None if no tick covers the range.
        """
        if end_ts <= start_ts or not self._history:
            return None
        return _twap(self._history, start_ts, end_ts)

    def projected_close(self, window_end: float, lookback: float,
                        now: float | None = None) -> "TwapProjection | None":
        """Project the settlement TWAP assuming the price holds from now on.

        The market resolves on the TWAP over the final `lookback` seconds. Part
        of that window has already elapsed and is locked in; the rest is
        unknown and is filled with the latest price.
        """
        if not self.latest or not self._history:
            return None
        now = self.latest.src_ts if now is None else now
        start = window_end - lookback
        known = min(max(now - start, 0.0), lookback)
        remaining = lookback - known
        if known <= 0:
            known_mean = self.latest.price
        else:
            known_mean = _twap(self._history, start, now)
            if known_mean is None:
                known_mean = self.latest.price
        proj = (known * known_mean + remaining * self.latest.price) / lookback
        return TwapProjection(proj, known, remaining, known_mean, self.latest.price)

    def realized_vol(self, secs: float) -> float | None:
        """How far BTC has actually travelled lately: high−low over the last
        `secs` of source time, in USD.

        Tells the maker whether a $20 move is noise (BTC swinging $300 per
        window) or a real decision (quiet tape). Range, not tick variance:
        Chainlink ticks are small and trend together, so summed squared
        increments read a $300 window as a $50 one (9 Sep, line stuck at 20).
        None until at least half the horizon is covered.
        """
        if not self.latest or len(self._history) < 2:
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
