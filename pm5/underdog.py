"""Underdog: buy the side the book prices at ~10¢ late in the window.

The mirror of the favorite's losses. Late in a 5-minute window the book is
overconfident: a side offered at 0.08–0.12 in T-120…T-30 won 18–22% of
1.3k recorded btc windows (Sep–Oct), about twice what it costs. A 90¢
favorite is the same mispricing from the other side, which is why buying
it lost.

    cheapest ask in [MIN_PRICE, MAX_PRICE], T-MAX_LEFT … T-MIN_LEFT
    →  FAK buy at that ask, sized to the $1 marketable minimum, hold

Book-only: no fair value, no price to beat, so it trades unwitnessed
windows too. It loses about four windows in five; the daily loss limit,
not a streak counter, is the brake.
"""

from __future__ import annotations

import logging
import math
import time

from .clob import BookTop, Executor, Fill, PendingTake
from .config import Config
from .markets import Market
from .strategy import Leg, Signal

log = logging.getLogger("pm5.underdog")


def dog_shares(cfg: Config, price: float) -> float:
    """Smallest order the CLOB takes at `price`: ≥ min shares and ≥ $1 notional."""
    by_notional = math.ceil(cfg.dog_min_notional / price - 1e-9) if price > 0 else 0
    return float(max(cfg.dog_min_shares, by_notional))


class Underdog:
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

    @staticmethod
    def quote_grid(cfg: Config, market: Market) -> list[tuple[float, list[float]]]:
        """[(shares, [price])] worth pre-signing: size depends on the price."""
        tick = market.tick_size or 0.01
        out: list[tuple[float, list[float]]] = []
        p = round(cfg.dog_min_price / tick) * tick
        while p <= cfg.dog_max_price + 1e-9:
            px = round(p, 2)
            if px > 0:
                out.append((dog_shares(cfg, px), [px]))
            p += tick
        return out

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
            self._next_try = self._clock() + self.cfg.dog_retry_ms / 1000.0
            log.info("underdog %s @ %.2f missed: %s", pt.side, pt.price,
                     pt.reason or self.executor.last_skip or "no fill")
            return []
        self.hits += 1
        self.fills.append(fill)
        log.info("underdog HIT %s %.2f sh @ %.3f", fill.side, fill.size, fill.price)
        return [fill]

    def evaluate(self, up_top: BookTop | None, down_top: BookTop | None) -> Signal | None:
        cfg = self.cfg
        if not cfg.dog_enabled or self.pending is not None:
            return None
        if self.hits >= 1 or self.shots >= cfg.dog_max_shots:
            return None
        if self._clock() < self._next_try:
            return None
        left = self.market.seconds_left
        if left < cfg.dog_min_left or left > cfg.dog_max_left:
            return None
        best: tuple[str, BookTop, float] | None = None
        for side, top in (("up", up_top), ("down", down_top)):
            if top is None or top.best_ask is None:
                continue
            ask = top.best_ask
            if ask < cfg.dog_min_price - 1e-9 or ask > cfg.dog_max_price + 1e-9:
                continue
            if best is None or ask < best[2]:
                best = (side, top, ask)
        if best is None:
            return None
        side, top, ask = best
        shares = dog_shares(cfg, ask)
        if top.offered_at(ask) < shares - 1e-9:
            return None
        if self.executor.bankroll is not None and self.executor.bankroll < shares * ask:
            return None
        return Signal(
            kind="underdog",
            legs=[Leg(
                side=side,
                token_id=self.market.token_for(side),
                max_price=ask,
                stake_usdc=round(shares * ask, 2),
                top=top,
                shares=shares,
            )],
            reason=f"T-{left:.0f}s: {side} offered {ask:.2f} ({shares:.0f} sh = ${shares * ask:.2f})",
        )

    def shoot(self, signal: Signal) -> PendingTake | None:
        leg = signal.legs[0]
        pt = self.executor.take(
            leg.token_id, leg.side, leg.max_price, leg.shares,
            self.market.tick_size or 0.01, self.market.neg_risk,
        )
        self.last_signal = signal
        self.shots += 1
        if pt is None:
            self._next_try = self._clock() + self.cfg.dog_retry_ms / 1000.0
            log.info("underdog %s not sent: %s", leg.side, self.executor.last_skip)
            return None
        self.pending = pt
        log.info("SIGNAL[underdog] %s", signal.reason)
        return pt

    def summary(self) -> str:
        cost = sum(f.cost for f in self.fills)
        return f"{self.hits}/{self.shots} hit, cost ${cost:.2f}"
