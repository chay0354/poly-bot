"""Maker-pair strategy: a (mostly) risk-controlled trade in every window.

Early in each 5-minute window the book sits near 0.50 / 0.50. We rest
post-only bids on BOTH sides below the mid (e.g. 0.46 Up + 0.46 Down = 0.92).
Makers pay no fee and earn rebates, so:

* both bids hit  -> we hold one Up + one Down per share pair; the market pays
  $1 per pair no matter what. Cost 0.92, payout 1.00: locked-in profit.
* one bid hits   -> keep the other bid on the book (do not cancel it at the
  usual T-75 cutoff). If the other ask is cheap enough that
  fill_price + ask <= 1, take it immediately and lock the pair. After a short
  grace period still naked, take it while the sum <= maker_pair_max_sum (a
  small known loss instead of a coin flip). TWAP hedge is the last fallback.
* nothing hits   -> cancel at `maker_cancel_left_secs`, nothing spent.

Fills for the single-leg case come from adverse flow (someone dumping that
side), so this is not free money -- it is a spread-capture strategy whose
edge is the pair discount plus the hedge. Paper it before trusting it.
"""

from __future__ import annotations

import logging
import time

from .clob import BookTop, Executor, Fill, RestingOrder
from .config import Config
from .markets import Market
from .pricefeed import TwapProjection
from .strategy import Leg, Signal

log = logging.getLogger("pm5.maker")


class MakerPair:
    def __init__(self, cfg: Config, executor: Executor, market: Market) -> None:
        self.cfg = cfg
        self.executor = executor
        self.market = market
        self.orders: dict[str, RestingOrder] = {}
        self.fills: list[Fill] = []
        self.posted = False
        self.cancelled = False
        self.hedged = False
        self._warned_onesided = False
        self._naked_since: float | None = None  # monotonic time we became one-sided
        self._clock = time.monotonic

    # ----------------------------------------------------------------- state

    def shares(self, side: str) -> float:
        return sum(f.size for f in self.fills if f.side == side)

    @property
    def paired(self) -> float:
        """Shares covered on both sides (each pair pays $1 for sure)."""
        return min(self.shares("up"), self.shares("down"))

    @property
    def naked_side(self) -> str | None:
        up, dn = self.shares("up"), self.shares("down")
        if abs(up - dn) < 0.01:
            return None
        return "up" if up > dn else "down"

    @property
    def naked_shares(self) -> float:
        return abs(self.shares("up") - self.shares("down"))

    def summary(self) -> str:
        cost = sum(f.cost for f in self.fills)
        paired = self.paired
        parts = [f"cost ${cost:.2f}"]
        if paired > 0:
            parts.append(f"paired {paired:.1f}sh (locked ${paired - _pair_cost(self.fills, paired):+.2f})")
        if self.naked_side:
            parts.append(f"naked {self.naked_side.upper()} {self.naked_shares:.1f}sh")
        return ", ".join(parts)

    # ------------------------------------------------------------------ loop

    def step(self, up_top: BookTop | None, down_top: BookTop | None) -> list[Fill]:
        """Post / poll / cancel resting bids. Returns any new fills."""
        m = self.market
        left = m.seconds_left
        tops = {"up": up_top, "down": down_top}

        if m.seconds_in >= self.cfg.maker_start_secs and left > self._keep_bid_left:
            already_in = bool(self.orders or self.fills)
            if already_in and not self.posted:
                # One leg is on / filled: always retry the missing bid, even
                # if the book has gone one-sided (that is how we get stuck naked).
                self._post()
            elif not self.posted and left > self.cfg.maker_cancel_left_secs:
                if self._book_undecided(tops):
                    self._post()
                elif not self._warned_onesided:
                    self._warned_onesided = True
                    log.info("maker: book already one-sided (up ask %s, down ask %s); "
                             "waiting for a fair book before resting bids",
                             _fmt(up_top), _fmt(down_top))

        new: list[Fill] = []
        for side, order in list(self.orders.items()):
            fill = self.executor.poll_bid(order, tops.get(side))
            if fill is not None:
                new.append(fill)
                self.fills.append(fill)
        if new:
            log.info("maker fills: %s", self.summary())
        if self.naked_side is None:
            self._naked_since = None
        elif self._naked_since is None:
            self._naked_since = self._clock()

        if self.hedged:
            self._pull_overfill_bids("already hedged")
        self._maybe_cancel(left)
        return new

    def close(self) -> None:
        """Window is over: make sure nothing is left resting."""
        self._cancel_all("window closed")

    @property
    def _keep_bid_left(self) -> float:
        """When already filled on one side, keep the other bid this late."""
        return max(self.cfg.stop_entry_secs, 8.0)

    def complete_pair_signal(
        self, up_top: BookTop | None, down_top: BookTop | None,
    ) -> Signal | None:
        """If one side filled, take the other side to close the pair.

        The price we accept ramps with how long we have been naked:
          * at once            -> avg_fill + ask <= 1.00 (profit locked)
          * after grace secs   -> <= maker_pair_max_sum  (small known loss)
          * after hard secs    -> <= maker_pair_hard_sum (bigger known loss)
        A first fill is usually the side being dumped, so waiting only lets the
        other side get dearer; a bounded loss beats a $stake coin flip.
        """
        if self.hedged:
            return None
        side = self.naked_side
        if side is None:
            return None
        other = "down" if side == "up" else "up"
        top = down_top if other == "down" else up_top
        if top is None or top.best_ask is None:
            return None
        avg = self._avg_price(side)
        if avg is None:
            return None
        limit = 1.0
        if self._naked_since is not None:
            age = self._clock() - self._naked_since
            if age >= self.cfg.maker_pair_hard_secs:
                limit = max(1.0, self.cfg.maker_pair_hard_sum)
            elif age >= self.cfg.maker_pair_grace_secs:
                limit = max(1.0, self.cfg.maker_pair_max_sum)
        if avg + top.best_ask > limit + 1e-9:
            return None
        qty = self.naked_shares
        return Signal(
            kind="maker-pair",
            legs=[Leg(
                side=other,
                token_id=self.market.token_for(other),
                max_price=top.best_ask,
                stake_usdc=round(qty * top.best_ask, 2),
                top=top,
            )],
            reason=(
                f"complete pair: naked {side} @ {avg:.2f} + {other} "
                f"ask {top.best_ask:.2f} = {avg + top.best_ask:.2f} ≤ {limit:.2f}"
            ),
        )

    def hedge_signal(
        self, proj: TwapProjection | None, open_price: float | None,
        up_top: BookTop | None, down_top: BookTop | None,
    ) -> Signal | None:
        """If our lone leg is projected to lose safely, buy the other side."""
        if self.hedged or self.cfg.maker_hedge_max_price <= 0:
            return None
        side = self.naked_side
        if side is None or proj is None or open_price is None:
            return None
        left = self.market.seconds_left
        if left > self.cfg.decide_within_secs or left < self.cfg.stop_entry_secs:
            return None
        delta = proj.twap - open_price
        losing = (side == "up" and delta < 0) or (side == "down" and delta > 0)
        if not losing or abs(delta) < self.cfg.min_delta_usd:
            return None
        if proj.flip_needed(open_price) < self.cfg.min_flip_usd:
            return None
        other = "down" if side == "up" else "up"
        top = down_top if other == "down" else up_top
        if top is None or top.best_ask is None or top.best_ask > self.cfg.maker_hedge_max_price:
            return None
        qty = self.naked_shares
        leg = Leg(
            side=other,
            token_id=self.market.token_for(other),
            max_price=self.cfg.maker_hedge_max_price,
            stake_usdc=round(qty * top.best_ask, 2),
            top=top,
        )
        return Signal(
            kind="maker-hedge",
            legs=[leg],
            reason=(
                f"naked {side} {qty:.1f}sh losing (TWAP Δopen={delta:+.1f}, "
                f"flip {proj.flip_needed(open_price):.0f} USD); buy {other} @ ≤{top.best_ask:.2f}"
            ),
        )

    def mark_hedged(self, fills: list[Fill]) -> None:
        self.hedged = True
        self.fills.extend(fills)
        log.info("maker hedged: %s", self.summary())
        # We just bought the other side as a taker. Any bid still resting
        # would now over-fill us (a second, naked leg), so pull them at once.
        # `_pull_overfill_bids` also runs every step so a failed cancel retries.
        self._pull_overfill_bids("pair locked by taker buy")

    def _pull_overfill_bids(self, why: str) -> None:
        """Cancel live bids that can only add a naked leg.

        Once we hedged (or a side is already fully filled), a resting bid on
        that side has no pair to complete -- it just doubles the position.
        """
        wanted = self._wanted_shares()
        for side, order in list(self.orders.items()):
            if order.done:
                continue
            if self.hedged or self.shares(side) >= wanted - 0.01:
                log.info("maker: pulling %s bid (%s)", side, why)
                self.executor.cancel_bid(order)

    # --------------------------------------------------------------- helpers

    def _book_undecided(self, tops: dict[str, BookTop | None]) -> bool:
        """Both sides still quoted near 0.50.

        If one side has already collapsed (say Down offered at 0.31) the market
        has made up its mind; bidding just under that ask would buy the side
        being dumped -- the fill we'd get is exactly the one we don't want.
        A pair only makes sense while both asks sit above our bid and below
        the point where the other side has become cheap.
        """
        tick = self.market.tick_size or 0.01
        lo = self.cfg.maker_bid + tick          # post-only must not cross
        hi = 1.0 - self.cfg.maker_bid + 0.08    # e.g. 0.62 for a 0.46 bid
        for side in ("up", "down"):
            top = tops.get(side)
            if top is None or top.best_ask is None:
                return False
            if not (lo <= top.best_ask <= hi):
                return False
        return True

    def _avg_price(self, side: str) -> float | None:
        sf = [f for f in self.fills if f.side == side]
        sz = sum(f.size for f in sf)
        if sz <= 0:
            return None
        return sum(f.cost for f in sf) / sz

    def _wanted_shares(self) -> float:
        px = self.cfg.maker_bid
        return max(self.market.min_size, round(self.cfg.maker_stake_usdc / px, 2))

    def _live_orders(self) -> list[RestingOrder]:
        return [o for o in self.orders.values() if not o.done]

    def _post(self) -> None:
        """Rest any missing bid. Only mark posted once BOTH sides are on the book.

        A CLOB/network reject must not burn the rest of the window: we retry
        each tick until both bids land or we give up. A done, unfilled order
        is replaced so a cancel / reject can be retried.
        """
        m = self.market
        tick = m.tick_size or 0.01
        px = self.cfg.maker_bid
        shares = self._wanted_shares()
        for side in ("up", "down"):
            if self.shares(side) >= shares - 0.01:
                continue
            existing = self.orders.get(side)
            if existing is not None and not existing.done:
                continue
            order = self.executor.place_bid(
                m.token_for(side), side, px, shares, tick, m.neg_risk,
            )
            if order is not None:
                self.orders[side] = order
        live_or_filled = sum(
            1 for side in ("up", "down")
            if self.shares(side) > 0 or (
                side in self.orders and not self.orders[side].done
            )
        )
        if live_or_filled == 2 or (self.shares("up") > 0 and self.shares("down") > 0):
            self.posted = True
            return
        log.warning(
            "maker: rest incomplete (%d/2 on book); will retry",
            live_or_filled,
        )

    def _maybe_cancel(self, left: float) -> None:
        if self.cancelled:
            return
        if self.fills:
            # Already in on one side: keep the other bid so it can still pair.
            if left <= self._keep_bid_left and self._live_orders():
                self._cancel_all(f"T-{left:.0f}s leftover (pair still open)")
            return
        if self.orders and left <= self.cfg.maker_cancel_left_secs:
            self._cancel_all(f"T-{left:.0f}s cutoff")

    def _cancel_all(self, why: str) -> None:
        if self.cancelled:
            return
        self.cancelled = True
        live = [o for o in self.orders.values() if not o.done]
        if live:
            log.info("maker: cancelling %d resting bid(s) (%s)", len(live), why)
        for o in live:
            self.executor.cancel_bid(o)


def _fmt(top: BookTop | None) -> str:
    if top is None or top.best_ask is None:
        return "-"
    return f"{top.best_ask:.2f}"


def _pair_cost(fills: list[Fill], paired: float) -> float:
    """Approximate cost of `paired` shares on each side (avg price per side)."""
    total = 0.0
    for side in ("up", "down"):
        sf = [f for f in fills if f.side == side]
        sz = sum(f.size for f in sf)
        if sz <= 0:
            return 0.0
        avg = sum(f.cost for f in sf) / sz
        total += avg * paired
    return total
