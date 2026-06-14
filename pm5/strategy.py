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


class MomentumStrategy:
    """In the closing seconds, back the side the Chainlink price already favors.

    Resolution compares the window's closing price to its opening price. If, with
    little time left, the live price is comfortably above (below) the open, "Up"
    ("Down") is the likely winner -- buy it while it is still cheaper than its
    fair value of ~1.0.
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
        delta = tick.price - open_price
        if abs(delta) < self.cfg.min_delta_usd:
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
        )
        return Signal(
            kind="momentum",
            legs=[leg],
            reason=(
                f"Δopen={delta:+.1f} USD, {left:.0f}s left, "
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
