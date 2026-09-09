"""Trading signals: end-of-window momentum and cross-side arbitrage."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .clob import BookReader, BookTop
from .config import Config
from .markets import Market
from .pricefeed import ChainlinkFeed

log = logging.getLogger("pm5.strategy")


@dataclass
class Signal:
    kind: str  # "momentum" | "arb"
    # For momentum: one leg. For arb: two legs (buy both sides).
    legs: list["Leg"]
    reason: str


@dataclass
class Leg:
    side: str  # "up" / "down"
    token_id: str
    max_price: float
    stake_usdc: float
    # Book snapshot the signal was based on, so the executor can fill against
    # the same prices without a racy re-read.
    top: "BookTop | None" = None
    # Refuse asks below this (0 = no floor). Arb legs are cheap by design and
    # leave it at 0; momentum uses cfg.min_price.
    min_price: float = 0.0


class MomentumStrategy:
    """In the closing seconds, back the side the settlement TWAP already favors.

    The market resolves Up if the Chainlink TWAP over the final `twap_secs` of
    the window is >= the opening price. Once we are inside that TWAP window,
    part of the average is locked in, so we can compute (a) where the TWAP
    lands if the price holds, and (b) how far BTC would have to *average* away
    from here for the rest of the window to flip the result. Both must clear
    their thresholds before we buy.
    """

    def __init__(self, cfg: Config, feed: ChainlinkFeed) -> None:
        self.cfg = cfg
        self.feed = feed

    def evaluate(self, market: Market) -> Signal | None:
        if not self.cfg.momentum_enabled:
            return None
        left = market.seconds_left
        if left > self.cfg.decide_within_secs or left < self.cfg.stop_entry_secs:
            return None
        tick = self.feed.latest
        if tick is None:
            return None
        open_price = self.feed.price_at_or_after(market.window_start)
        if open_price is None:
            return None
        proj = self.feed.projected_close(market.window_end, self.cfg.twap_secs)
        if proj is None:
            return None
        delta = proj.twap - open_price
        if abs(delta) < self.cfg.min_delta_usd:
            return None
        flip = proj.flip_needed(open_price)
        if flip < self.cfg.min_flip_usd:
            return None
        # Require short-term momentum to agree with the window delta (no fade
        # right before close).
        mom = self.feed.momentum(lookback_secs=10.0)
        if mom is not None and (mom > 0) != (delta > 0) and abs(mom) > self.cfg.min_delta_usd / 2:
            return None
        side = "up" if delta > 0 else "down"
        leg = Leg(
            side=side,
            token_id=market.token_for(side),
            max_price=self.cfg.max_price,
            stake_usdc=self.cfg.stake_usdc,
            min_price=self.cfg.min_price,
        )
        return Signal(
            kind="momentum",
            legs=[leg],
            reason=(
                f"TWAP Δopen={delta:+.1f} USD ({proj.locked_frac:.0%} locked, "
                f"flip needs {flip:.0f} USD avg), {left:.0f}s left, "
                f"price={tick.price:.1f}, open={open_price:.1f}"
            ),
        )


class ArbitrageStrategy:
    """Buy both Up and Down when their combined ask is below $1 minus an edge.

    Holding one of each guarantees a $1 payout at resolution, so a combined cost
    below $1 (net of fees) is risk-free profit.
    """

    def __init__(self, cfg: Config, reader: BookReader) -> None:
        self.cfg = cfg
        self.reader = reader

    def evaluate(
        self, market: Market, up: BookTop | None = None, down: BookTop | None = None
    ) -> Signal | None:
        if not self.cfg.arb_enabled:
            return None
        # Reuse caller-provided book snapshots when available (avoids a re-read).
        if up is None:
            up = self.reader.top(market.up_token)
        if down is None:
            down = self.reader.top(market.down_token)
        if up.best_ask is None or down.best_ask is None:
            return None
        combined = up.best_ask + down.best_ask
        if combined > 1.0 - self.cfg.arb_min_edge:
            return None
        legs = [
            Leg("up", market.up_token, up.best_ask, self.cfg.arb_stake_usdc, up),
            Leg("down", market.down_token, down.best_ask, self.cfg.arb_stake_usdc, down),
        ]
        return Signal(
            kind="arb",
            legs=legs,
            reason=f"ask_up={up.best_ask:.2f} + ask_down={down.best_ask:.2f} = {combined:.2f}",
        )
