"""Lag taker: buy the side the tape has already decided, while the book lags.

The sniper only fires within ms of a sudden jump. This covers the slower
case (the 0x8dxd pattern on these same 5-minute markets): BTC on
Binance/Coinbase has moved far enough from the price to beat that the
window is all but settled, yet the Polymarket ask still prices it as
merely likely.

    fair = fair_up(Δ since the beat, realized 5-min range, time left)
    fair ≥ MIN_FAIR, min(fair, FAIR_CAP) − ask − taker fee ≥ MIN_EDGE,
    feed print fresh
    →  FAK buy at that ask (pre-signed when possible), hold to resolution

Small edge per trade, so it only pays with a high hit rate. Paper is
latency-honest (`PM_SIM_LATENCY_MS`): a quote that vanishes before our
order would land is a lost race, not a fill. `PM_LAG_KILL_STREAK` losing
windows in a row stand it down for the UTC day.
"""

from __future__ import annotations

import logging
import time

from .clob import BookTop, Executor, Fill, PendingTake, taker_fee_usdc
from .config import Config
from .maker import fair_up
from .markets import Market
from .strategy import Leg, Signal

log = logging.getLogger("pm5.lag")


class LagTaker:
    def __init__(self, cfg: Config, executor: Executor, market: Market,
                 clock=time.monotonic) -> None:
        self.cfg = cfg
        self.executor = executor
        self.market = market
        self._clock = clock
        self.pending: PendingTake | None = None
        self.fills: list[Fill] = []
        self.shots = 0
        self.hits = 0
        self.last_signal: Signal | None = None
        self._next_try = 0.0

    @property
    def side(self) -> str | None:
        up = sum(f.size for f in self.fills if f.side == "up")
        dn = sum(f.size for f in self.fills if f.side == "down")
        if abs(up - dn) < 0.01:
            return None
        return "up" if up > dn else "down"

    @staticmethod
    def quote_grid(cfg: Config, market: Market) -> tuple[float, list[float]]:
        """(shares, prices) worth pre-signing: every tick in the ask band."""
        tick = market.tick_size or 0.01
        prices: list[float] = []
        p = round(cfg.lag_min_price / tick) * tick
        while p <= cfg.lag_max_price + 1e-9:
            prices.append(round(p, 2))
            p += tick
        return cfg.lag_shares, prices

    def fair(self, delta: float | None, sigma: float | None) -> float | None:
        if delta is None or sigma is None or sigma <= 0:
            return None
        return fair_up(delta, sigma, self.market.seconds_left)

    def poll(self, up_top: BookTop | None, down_top: BookTop | None) -> list[Fill]:
        pt = self.pending
        if pt is None:
            return []
        top = up_top if pt.side == "up" else down_top
        fill = self.executor.poll_take(pt, top)
        if not pt.done:
            return []
        self.pending = None
        if fill is None:
            self._next_try = self._clock() + self.cfg.lag_retry_ms / 1000.0
            log.info("lag %s @ %.2f missed: %s", pt.side, pt.price,
                     pt.reason or self.executor.last_skip or "no fill")
            return []
        self.hits += 1
        self.fills.append(fill)
        log.info("lag HIT %s %.2f sh @ %.3f", fill.side, fill.size, fill.price)
        return [fill]

    def evaluate(
        self,
        delta: float | None,
        sigma: float | None,
        feed_age_ms: float | None,
        up_top: BookTop | None,
        down_top: BookTop | None,
    ) -> Signal | None:
        cfg = self.cfg
        if not cfg.lag_enabled or self.pending is not None:
            return None
        if self.hits >= cfg.lag_per_window or self.shots >= cfg.lag_max_shots:
            return None
        if self._clock() < self._next_try:
            return None
        left = self.market.seconds_left
        if left < cfg.lag_min_left or left > cfg.lag_max_left:
            return None
        if feed_age_ms is None or feed_age_ms > cfg.lag_max_feed_age_ms:
            return None  # a stale print is not "the market has not caught up yet"
        p_up = self.fair(delta, sigma)
        if p_up is None:
            return None
        held = self.side
        for side, fair, top in (("up", p_up, up_top), ("down", 1.0 - p_up, down_top)):
            if held is not None and side != held:
                continue
            if fair < cfg.lag_min_fair - 1e-9:
                continue
            if top is None or top.best_ask is None:
                continue
            ask = top.best_ask
            if ask < cfg.lag_min_price - 1e-9 or ask > cfg.lag_max_price + 1e-9:
                continue
            fee = taker_fee_usdc(1.0, ask, cfg.taker_fee_rate)
            edge = min(fair, cfg.lag_fair_cap) - ask - fee
            if edge < cfg.lag_min_edge - 1e-9:
                continue
            shares = cfg.lag_shares
            if top.offered_at(ask) < shares - 1e-9:
                continue
            if self.executor.bankroll is not None and self.executor.bankroll < shares * ask:
                continue
            return Signal(
                kind="lag",
                legs=[Leg(
                    side=side,
                    token_id=self.market.token_for(side),
                    max_price=ask,
                    stake_usdc=round(shares * ask, 2),
                    top=top,
                    shares=shares,
                )],
                reason=(
                    f"Δ {delta:+.1f}$ σ5m {sigma:.0f} T-{left:.0f}s: {side} fair {fair:.3f} "
                    f"vs ask {ask:.2f} (edge {edge:+.3f} after fee, feed {feed_age_ms:.0f}ms)"
                ),
            )
        return None

    def shoot(self, signal: Signal) -> PendingTake | None:
        leg = signal.legs[0]
        pt = self.executor.take(
            leg.token_id, leg.side, leg.max_price, leg.shares,
            self.market.tick_size or 0.01, self.market.neg_risk,
        )
        self.last_signal = signal
        self.shots += 1
        if pt is None:
            self._next_try = self._clock() + self.cfg.lag_retry_ms / 1000.0
            log.info("lag %s not sent: %s", leg.side, self.executor.last_skip)
            return None
        self.pending = pt
        log.info("SIGNAL[lag] %s", signal.reason)
        return pt

    def summary(self) -> str:
        cost = sum(f.cost for f in self.fills)
        return f"{self.hits}/{self.shots} hit, cost ${cost:.2f}"
