"""Exchange trade streams: an *early warning*, not a settlement source.

The market makers who hit our resting bids price off the big spot venues,
which lead the Chainlink feed by a few seconds. Every adverse maker fill on
9 Sep was a sweep that Chainlink only showed after the fact. We watch these
feeds purely to pull a bid before it is run over; the open, the TWAP and
settlement stay on Chainlink (that is what the market resolves on).

Sources (public, no key):
    Binance  aggTrade  {"e":"aggTrade","s":"BTCUSDT","p":"78800.10","T":1788975600123,...}
    Coinbase ticker    {"type":"ticker","product_id":"BTC-USD","price":"78800.10",
                        "time":"2026-09-10T07:11:19.611611Z",...}

`FastFeeds` runs several sources and answers with whichever printed most
recently — the first venue to move is the one that matters, and from a
server near Polymarket (US-East) Coinbase's ticks arrive ~80ms before
Binance's. Deltas are always measured within one venue: BTCUSDT and BTC-USD
carry a basis of a few dollars, so "Coinbase now − Binance at open" would be
noise dressed as signal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Callable

import websockets

from .pricefeed import Tick

log = logging.getLogger("pm5.fastfeed")


class TradeFeed:
    """One venue's trade tape kept as a rolling history. Subclasses parse."""

    name = "feed"
    # Prints arrive every ~100–300ms; a first tick more than this after `ts`
    # means we were not listening at `ts`.
    GAP_SECS = 3.0

    def __init__(self, url: str, history_secs: float = 360.0, stale_secs: float = 5.0,
                 on_tick: Callable[[], None] | None = None) -> None:
        self._url = url
        self._history_secs = history_secs
        self._stale_secs = stale_secs
        self.latest: Tick | None = None
        self._history: list[Tick] = []
        # Called after every tick (same thread) so the trading loop can wake
        # on the move instead of on its next poll.
        self.on_tick = on_tick

    # ---------------------------------------------------------------- stream

    def _subscribe_frame(self) -> dict | None:  # pragma: no cover - overridden
        return None

    def _parse(self, msg: dict) -> tuple[float, float] | None:  # pragma: no cover
        """(price, src_ts) or None for a frame that is not a trade."""
        return None

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self._url, ping_interval=20) as ws:
                    backoff = 1.0
                    if self.latest is not None and time.time() - self.latest.recv_ts > self.GAP_SECS:
                        # A long gap means the history no longer covers the
                        # tape: a window open inside the gap must read as
                        # unknown, not as the first tick after reconnect.
                        self._history.clear()
                    frame = self._subscribe_frame()
                    if frame is not None:
                        await ws.send(json.dumps(frame))
                    log.info("fast feed connected (%s)", self.name)
                    async for raw in ws:
                        self._ingest(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - reconnect on anything
                log.warning("fast feed %s disconnected (%s); reconnecting in %.0fs",
                            self.name, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _ingest(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        parsed = self._parse(msg)
        if parsed is None:
            return
        price, src_ts = parsed
        now = time.time()
        tick = Tick(price=price, src_ts=src_ts or now, recv_ts=now)
        self.latest = tick
        self._history.append(tick)
        cutoff = now - self._history_secs
        i = 0
        while i < len(self._history) and self._history[i].recv_ts < cutoff:
            i += 1
        if i:
            self._history = self._history[i:]
        if self.on_tick is not None:
            self.on_tick()

    # --------------------------------------------------------------- queries

    @property
    def fresh(self) -> bool:
        return self.latest is not None and time.time() - self.latest.recv_ts <= self._stale_secs

    def covers(self, ts: float) -> bool:
        """True if the history was already streaming at `ts` (so a price read
        at `ts` is the tape, not the moment we connected)."""
        return bool(self._history) and self._history[0].src_ts <= ts + self.GAP_SECS

    def price_at_or_after(self, ts: float) -> float | None:
        if not self.covers(ts):
            return None
        for t in self._history:
            if t.src_ts >= ts:
                return t.price
        return None

    def delta_since(self, ts: float) -> float | None:
        """Move since `ts` (window open), or None if we cannot know it
        honestly: we were not streaming at `ts`, no trade at/after `ts` in
        history, or the stream is stale. Connecting at T+143s and calling the
        first tick "the open" is how the 08:57 window nearly got a blind pair.
        """
        if not self.fresh:
            return None
        ref = self.price_at_or_after(ts)
        if ref is None:
            return None
        return self.latest.price - ref

    def realized_vol(self, secs: float) -> float | None:
        """High−low over the last `secs` (USD), same definition as the
        Chainlink feed's. Usable within a couple of minutes of connecting and
        it is the tape the fair-value model should be calibrated on (the
        makers hitting us trade off it). None until half the horizon is
        covered."""
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


class BinanceFeed(TradeFeed):
    """BTCUSDT aggTrade stream (subscription is in the URL)."""

    name = "binance"

    def _parse(self, msg: dict) -> tuple[float, float] | None:
        if "p" not in msg:
            return None
        try:
            price = float(msg["p"])
        except (TypeError, ValueError):
            return None
        src_ts = float(msg.get("T") or 0) / 1000.0
        return price, src_ts


COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"


class CoinbaseFeed(TradeFeed):
    """BTC-USD `ticker` channel (one frame per match, ~3/s)."""

    name = "coinbase"

    def __init__(self, url: str = COINBASE_WS_URL, product: str = "BTC-USD", **kw) -> None:
        super().__init__(url, **kw)
        self._product = product

    def _subscribe_frame(self) -> dict:
        return {"type": "subscribe", "product_ids": [self._product], "channels": ["ticker"]}

    def _parse(self, msg: dict) -> tuple[float, float] | None:
        if msg.get("type") != "ticker" or msg.get("product_id") != self._product:
            return None
        try:
            price = float(msg["price"])
        except (KeyError, TypeError, ValueError):
            return None
        return price, _iso_ts(msg.get("time"))


def _iso_ts(s) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class FastFeeds:
    """Several venues, one answer: the venue that printed most recently.

    Each venue keeps its own tape, so a delta is always same-venue. Whoever
    moved last is the freshest view of the market — when Binance jumps $20
    and Coinbase has not printed yet, the $20 shows immediately rather than
    averaged down to $10.
    """

    def __init__(self, feeds: list[TradeFeed]) -> None:
        if not feeds:
            raise ValueError("FastFeeds needs at least one feed")
        self.feeds = feeds
        self.delta_source = feeds[0].name  # venue behind the last delta_since()

    async def run(self) -> None:
        await asyncio.gather(*(f.run() for f in self.feeds))

    def _ranked(self, ts: float | None = None) -> list[TradeFeed]:
        """Fresh feeds (covering `ts` if given), most recent print first."""
        out = [f for f in self.feeds if f.fresh and (ts is None or f.covers(ts))]
        out.sort(key=lambda f: f.latest.recv_ts, reverse=True)
        return out

    @property
    def fresh(self) -> bool:
        return any(f.fresh for f in self.feeds)

    @property
    def latest(self) -> Tick | None:
        r = self._ranked()
        return r[0].latest if r else None

    @property
    def source(self) -> str | None:
        r = self._ranked()
        return r[0].name if r else None

    def covers(self, ts: float) -> bool:
        return any(f.covers(ts) for f in self.feeds)

    def delta_since(self, ts: float) -> float | None:
        for f in self._ranked(ts):
            d = f.delta_since(ts)
            if d is not None:
                self.delta_source = f.name
                return d
        return None

    def realized_vol(self, secs: float) -> float | None:
        # The widest fresh tape: the model should be calibrated on the
        # venue that actually moved, and a quieter venue would understate σ.
        vols = [v for v in (f.realized_vol(secs) for f in self.feeds if f.fresh) if v is not None]
        return max(vols) if vols else None
