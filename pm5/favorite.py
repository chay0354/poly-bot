"""Favorite: buy a side that has *stayed* decided, cut a real breakdown.

Buying the first 90¢ print is −EV (our own paths: 86% win, need ~91% after
the fee). Waiting until 40¢ to sell is also wrong — that locks most of the
loss. This is the version that can survive:

    1. The ask must reach TRIGGER (0.90) and *stay* in [TRIGGER, MAX] for
       HOLD_SECS. A one-tick 0.90 that fades is the fake-out we used to buy.
    2. Refuse asks above MAX (0.93). Winning 7¢ to risk 93¢ is the $10-to-
       make-$1 trap.
    3. Only in the middle-late window (enough time that 90¢ means the
       market is resolving, enough time left to exit).
    4. The tape (fast-feed Δ vs the price to beat) must already agree.
       A 90¢ Up while ETH/BTC is red is the book lying.
    5. Stop on a *persisted* breakdown: bid ≤ STOP for EXIT_HOLD seconds,
       and only after EXIT_GRACE from the fill. A one-tick 50¢ (and a tape
       flip at 80¢) was the overnight −$8 to −$16. Do not FAK under MIN_BID.

One shot per window. The buy is a normal taker (the quote has been sitting
for seconds — we are not racing). The exit is a FAK sell that may walk
STOP − SLIP.
"""

from __future__ import annotations

import logging
import time

from .clob import BookTop, Fill
from .config import Config
from .markets import Market
from .strategy import Leg, Signal

log = logging.getLogger("pm5.favorite")


class Favorite:
    def __init__(self, cfg: Config, market: Market, clock=time.monotonic) -> None:
        self.cfg = cfg
        self.market = market
        self._clock = clock
        # When each side's ask first printed ≥ trigger this window.
        self._seen_at: dict[str, float | None] = {"up": None, "down": None}
        self.fills: list[Fill] = []
        self.exited = False
        self.last_signal: Signal | None = None
        self._filled_at: float | None = None
        self._broke_at: float | None = None

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

    def mark_filled(self, fills: list[Fill]) -> None:
        self.fills.extend(fills)
        if self._filled_at is None and self.side is not None:
            self._filled_at = self._clock()

    def mark_exited(self, fills: list[Fill]) -> None:
        self.fills.extend(fills)
        if self.shares < 0.01:
            self.exited = True
            log.info("favorite exited: %s", self.summary())

    def summary(self) -> str:
        cost = sum(f.cost for f in self.fills)
        s = f"cost ${cost:.2f}"
        if self.side:
            s += f", holding {self.side.upper()} {self.shares:.1f}sh"
        return s

    # ------------------------------------------------------------------ loop

    def watch(self, up_top: BookTop | None, down_top: BookTop | None) -> None:
        """Track how long each side has been sitting at/above the trigger."""
        now = self._clock()
        floor = self.cfg.favorite_trigger - self.cfg.favorite_persist_give
        for side, top in (("up", up_top), ("down", down_top)):
            ask = top.best_ask if top is not None else None
            if ask is not None and ask >= floor - 1e-9:
                if self._seen_at[side] is None:
                    self._seen_at[side] = now
            else:
                self._seen_at[side] = None

    def evaluate(
        self,
        up_top: BookTop | None,
        down_top: BookTop | None,
        tape_delta: float | None,
    ) -> Signal | None:
        if not self.cfg.favorite_enabled or self.exited or self.side is not None:
            return None
        left = self.market.seconds_left
        if left < self.cfg.favorite_min_left or left > self.cfg.favorite_max_left:
            return None
        self.watch(up_top, down_top)
        now = self._clock()
        hold = self.cfg.favorite_hold_secs
        lo, hi = self.cfg.favorite_trigger, self.cfg.favorite_max_price
        tape = self._tape_side(tape_delta)

        for side, top in (("up", up_top), ("down", down_top)):
            if top is None or top.best_ask is None:
                continue
            ask = top.best_ask
            if ask < lo - 1e-9 or ask > hi + 1e-9:
                continue
            armed = self._seen_at[side]
            if armed is None or now - armed < hold - 1e-9:
                continue
            other = down_top if side == "up" else up_top
            other_ask = other.best_ask if other is not None else None
            if other_ask is not None and other_ask < self.cfg.favorite_other_min - 1e-9:
                continue  # dust complement: the 0.90 is a broken book, not a decision
            if self.cfg.favorite_tape:
                if tape is None or tape != side:
                    continue
            shares, stake = self._size(ask)
            if shares < 0.01:
                continue
            if top.offered_at(ask) < shares - 1e-9 and top.best_ask_size + 1e-9 < shares:
                continue
            limit = round(min(hi, ask + self.cfg.favorite_chase), 2)
            age = now - armed
            sig = Signal(
                kind="favorite",
                legs=[Leg(
                    side=side,
                    token_id=self.market.token_for(side),
                    max_price=limit,
                    stake_usdc=stake,
                    top=top,
                    shares=shares,
                    min_price=self.cfg.favorite_trigger - self.cfg.favorite_persist_give,
                )],
                reason=(
                    f"{side} ask {ask:.2f} held {age:.1f}s in "
                    f"{lo:.2f}–{hi:.2f}, T-{left:.0f}s, tape {tape_delta:+.1f}"
                    if tape_delta is not None else
                    f"{side} ask {ask:.2f} held {age:.1f}s in {lo:.2f}–{hi:.2f}, T-{left:.0f}s"
                ),
            )
            self.last_signal = sig
            return sig
        return None

    def exit_signal(
        self,
        up_top: BookTop | None,
        down_top: BookTop | None,
        tape_delta: float | None,
    ) -> Signal | None:
        """Sell on a persisted breakdown. A 50¢ flicker is not one."""
        if self.exited or self.side is None:
            return None
        side = self.side
        top = up_top if side == "up" else down_top
        if top is None or top.best_bid is None:
            return None
        bid = top.best_bid
        if bid < self.cfg.favorite_exit_min_bid:
            return None  # hole — FAK-walking $20 to 15–34¢ is worse than holding
        qty = self.shares
        if qty < 0.01:
            return None
        now = self._clock()
        if (
            self._filled_at is not None
            and now - self._filled_at < self.cfg.favorite_exit_grace_secs - 1e-9
        ):
            return None

        # Tape-only cuts (bid 0.53–0.80) were −EV: the 5-min still paid.
        if bid <= self.cfg.favorite_stop + 1e-9:
            if self._broke_at is None:
                self._broke_at = now
        else:
            self._broke_at = None
        if (
            self._broke_at is None
            or now - self._broke_at < self.cfg.favorite_exit_hold_secs - 1e-9
        ):
            return None

        floor = round(max(
            self.cfg.favorite_exit_min_bid,
            bid - self.cfg.favorite_exit_slip,
        ), 2)
        why = (
            f"bid {bid:.2f} ≤ stop {self.cfg.favorite_stop:.2f} "
            f"for {now - self._broke_at:.1f}s"
        )
        sig = Signal(
            kind="favorite-exit",
            legs=[Leg(
                side=side,
                token_id=self.market.token_for(side),
                max_price=bid,
                stake_usdc=round(qty * bid, 2),
                top=top,
                shares=qty,
                min_price=floor,
            )],
            reason=f"cut {side} {qty:.1f}sh: {why}",
        )
        self.last_signal = sig
        return sig

    def _size(self, ask: float) -> tuple[float, float]:
        """(shares, stake_usdc). Stake wins when set so each fill is ~$1."""
        stake = self.cfg.favorite_stake_usdc
        if stake > 0 and ask > 0:
            shares = round(stake / ask, 2)
            return shares, round(shares * ask, 2)
        shares = self.cfg.favorite_shares
        return shares, round(shares * ask, 2)

    def _tape_side(self, delta: float | None) -> str | None:
        if delta is None:
            return None
        need = self.cfg.favorite_tape_usd
        if abs(delta) < need - 1e-9:
            return None  # too small to call a side
        return "up" if delta > 0 else "down"
