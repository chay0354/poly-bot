"""Sniper: take the stale ask after a BTC jump.

The mirror image of the maker's losses. When Binance prints a jump, the side
that just became more valuable is — for a few hundred ms — still offered at
its pre-jump price. Whoever hits it first earns (new fair − old ask); on
10 Sep that was 8–12c a share, paid by us. This strategy is the taker in
that race:

    jump ≥ threshold (JumpWatch)  →  side = the one that got dearer
    ask < fair_after − MIN_EDGE, offered size ≥ our shares, jump fresh
    →  FAK buy at that ask, hold to resolution (or scalp back)

One shot per window by default; the fill or the miss is read on later ticks
(`PendingTake`), so the loop never blocks. Paper only fills if the quote
survived the simulated round trip — see `Executor.poll_take`.
"""

from __future__ import annotations

import logging
import time

from .clob import BookTop, Executor, Fill, PendingTake
from .config import Config
from .jumps import JumpRecord
from .markets import Market
from .strategy import Leg, Signal

log = logging.getLogger("pm5.sniper")


class Sniper:
    def __init__(self, cfg: Config, executor: Executor, market: Market,
                 clock=time.monotonic) -> None:
        self.cfg = cfg
        self.executor = executor
        self.market = market
        self._clock = clock
        self.pending: PendingTake | None = None
        self.fills: list[Fill] = []
        self.shots = 0  # takes sent this window (filled or not)
        self.hits = 0  # takes that filled
        self.exited = False
        self.last_signal: Signal | None = None
        self._shot_rec: JumpRecord | None = None  # record we already shot at

    # ----------------------------------------------------------------- state

    @property
    def side(self) -> str | None:
        up = sum(f.size for f in self.fills if f.side == "up")
        dn = sum(f.size for f in self.fills if f.side == "down")
        if abs(up - dn) < 0.01:
            return None
        return "up" if up > dn else "down"

    @property
    def shares(self) -> float:
        up = sum(f.size for f in self.fills if f.side == "up")
        dn = sum(f.size for f in self.fills if f.side == "down")
        return abs(up - dn)

    def avg_price(self) -> float | None:
        side = self.side
        if side is None:
            return None
        buys = [f for f in self.fills if f.side == side and f.size > 0]
        sz = sum(f.size for f in buys)
        return sum(f.cost for f in buys) / sz if sz > 0 else None

    @staticmethod
    def quote_grid(cfg: Config, market: Market) -> tuple[float, list[float]]:
        """(shares, prices) worth pre-signing: every tick in the price band."""
        tick = market.tick_size or 0.01
        lo = round(cfg.snipe_min_price / tick) * tick
        prices: list[float] = []
        p = lo
        while p <= cfg.snipe_max_price + 1e-9:
            prices.append(round(p, 2))
            p += tick
        return cfg.snipe_shares, prices

    # ------------------------------------------------------------------ loop

    def poll(self, up_top: BookTop | None, down_top: BookTop | None) -> list[Fill]:
        """Resolve an in-flight take. Returns the new fill, if any."""
        pt = self.pending
        if pt is None:
            return []
        top = up_top if pt.side == "up" else down_top
        fill = self.executor.poll_take(pt, top)
        if not pt.done:
            return []
        self.pending = None
        if fill is None:
            log.info("snipe %s @ %.2f missed: %s", pt.side, pt.price,
                     pt.reason or self.executor.last_skip or "no fill")
            return []
        self.hits += 1
        self.fills.append(fill)
        log.info("snipe HIT %s %.2f sh @ %.3f", fill.side, fill.size, fill.price)
        return [fill]

    def evaluate(
        self, rec: JumpRecord | None, jump_age_ms: float | None,
        up_top: BookTop | None, down_top: BookTop | None,
    ) -> Signal | None:
        """A take, if the last jump's stale ask is still there and cheap."""
        if not self.cfg.snipe_enabled or rec is None or self.exited:
            return None
        if self.pending is not None or self.hits >= self.cfg.snipe_per_window:
            return None
        if self._shot_rec is rec:
            return None  # one shot per jump; a miss means the quote is gone
        if rec.fair_after is None:
            return None  # no σ yet: fair unknown, no edge to measure
        left = self.market.seconds_left
        if left < self.cfg.snipe_min_left or left > self.cfg.snipe_max_left:
            return None
        age_ms = (self._clock() - rec._t0) * 1000.0
        if age_ms > self.cfg.snipe_max_age_ms:
            return None
        if jump_age_ms is not None and jump_age_ms > self.cfg.snipe_max_age_ms:
            return None
        side = rec.stale_side
        top = up_top if side == "up" else down_top
        if top is None or top.best_ask is None:
            return None
        ask = top.best_ask
        if ask < self.cfg.snipe_min_price or ask > self.cfg.snipe_max_price:
            return None
        edge = rec.fair_after - ask
        if edge < self.cfg.snipe_min_edge - 1e-9:
            return None
        shares = self.cfg.snipe_shares
        if top.offered_at(ask) < shares - 1e-9:
            return None
        if self.executor.bankroll is not None and self.executor.bankroll < shares * ask:
            return None
        return Signal(
            kind="snipe",
            legs=[Leg(
                side=side,
                token_id=self.market.token_for(side),
                max_price=ask,
                stake_usdc=round(shares * ask, 2),
                top=top,
                shares=shares,
            )],
            reason=(
                f"{rec.venue} {rec.delta:+.1f}$ in {rec.window_secs:.0f}s, {age_ms:.0f}ms ago: "
                f"{side} still offered {ask:.2f} vs fair {rec.fair_after:.2f} "
                f"(edge {edge:+.3f}, {top.offered_at(ask):.0f} sh)"
            ),
        )

    def shoot(self, signal: Signal, rec: JumpRecord | None) -> PendingTake | None:
        leg = signal.legs[0]
        pt = self.executor.take(
            leg.token_id, leg.side, leg.max_price, leg.shares,
            self.market.tick_size or 0.01, self.market.neg_risk,
        )
        self._shot_rec = rec
        self.last_signal = signal
        if pt is None:
            log.info("snipe %s not sent: %s", leg.side, self.executor.last_skip)
            return None
        self.shots += 1
        self.pending = pt
        log.info("SIGNAL[snipe] %s", signal.reason)
        return pt

    def exit_signal(self, up_top: BookTop | None, down_top: BookTop | None) -> Signal | None:
        """Scalp mode: sell back once the bid is `snipe_take` above our entry."""
        if not self.cfg.snipe_scalp or self.exited or self.pending is not None:
            return None
        side = self.side
        avg = self.avg_price()
        if side is None or avg is None:
            return None
        top = up_top if side == "up" else down_top
        if top is None or top.best_bid is None:
            return None
        bid = top.best_bid
        if bid < avg + self.cfg.snipe_take - 1e-9:
            return None
        qty = self.shares
        return Signal(
            kind="snipe-exit",
            legs=[Leg(
                side=side,
                token_id=self.market.token_for(side),
                max_price=bid,
                stake_usdc=round(qty * bid, 2),
                top=top,
                shares=qty,
                min_price=round(avg + self.cfg.snipe_take, 2),
            )],
            reason=f"scalp {side} {qty:.1f}sh: bid {bid:.2f} ≥ entry {avg:.2f} + {self.cfg.snipe_take:.2f}",
        )

    def mark_exited(self, fills: list[Fill]) -> None:
        self.fills.extend(fills)
        if self.shares < 0.01:
            self.exited = True
            log.info("snipe exited: %s", self.summary())

    def summary(self) -> str:
        cost = sum(f.cost for f in self.fills)
        s = f"{self.hits}/{self.shots} hit, cost ${cost:.2f}"
        if self.side:
            s += f", holding {self.side.upper()} {self.shares:.1f}sh"
        return s
