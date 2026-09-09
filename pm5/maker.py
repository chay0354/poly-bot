"""Maker-pair strategy: a (mostly) risk-controlled trade in every window.

Early in each 5-minute window the book sits near 0.50 / 0.50. We rest
post-only bids on BOTH sides below the mid (e.g. 0.46 Up + 0.46 Down = 0.92).
Makers pay no fee and earn rebates, so:

* both bids hit  -> we hold one Up + one Down per share pair; the market pays
  $1 per pair no matter what. Cost 0.92, payout 1.00: locked-in profit.
* one bid hits   -> keep the other 0.46 bid on the book and *wait for it*.
  Instant taker-complete at ask 0.54 looks like a $1 lock but the 7% taker
  fee makes it -EV; that was the 3/5 leak. Only take the other side as a
  taker if that leftover bid is already gone *and* fill+ask+fee ≤ $1 (or
  the configured grace/hard cap). If it never comes back, sell the filled
  leg rather than pay 1.04–1.12. TWAP hedge is the last fallback.
* BTC dumps vs the open while a bid is still resting -> cancel the side being
  dumped into before it fills (defensive cancel).
* nothing hits   -> cancel at `maker_cancel_left_secs`, nothing spent.

Fills for the single-leg case come from adverse flow (someone dumping that
side), so this is not free money -- it is a spread-capture strategy whose
edge is the pair discount plus the hedge. Paper it before trusting it.
"""

from __future__ import annotations

import logging
import time

from .clob import BookTop, Executor, Fill, RestingOrder, taker_fee_usdc
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
        self.exited = False
        self._warned_onesided = False
        self._warned_feed = False
        self._warned_incomplete = False
        self._feed_hold = False  # hysteresis so Δ 25→19 does not re-post
        self._stood_down = False  # yanked an unfilled pair this window
        self._naked_since: float | None = None  # monotonic time we became one-sided
        self._blocked: set[str] = set()  # sides we will not re-post (defensive pull)
        self._limit = cfg.maker_defensive_usd  # current toxic-move line (USD)
        self._skip: str | None = None  # why we have not rested yet (for close())
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

    def step(
        self,
        up_top: BookTop | None,
        down_top: BookTop | None,
        btc: float | None = None,
        open_price: float | None = None,
        sigma: float | None = None,
    ) -> list[Fill]:
        """Post / poll / cancel resting bids. Returns any new fills.

        `sigma` is the feed's realized 5-min move; it widens the defensive line
        on a fast tape so noise does not keep us out of every window.
        """
        m = self.market
        left = m.seconds_left
        tops = {"up": up_top, "down": down_top}
        self._limit = self._defensive_limit(left, sigma)

        if (
            not self.hedged
            and not self.exited
            and m.seconds_in >= self.cfg.maker_start_secs
            and left > self._keep_bid_left
        ):
            toxic = self._toxic_side(btc, open_price)
            already_in = bool(self.fills or self._live_orders())
            if self._stood_down and not self.fills:
                self._skip = "stood down after defensive yank"
            elif toxic is not None and not self.fills:
                # Book can still look 0.50/0.50 after a $20 Chainlink move.
                # Posting the pair then yanking one side leaves a naked rest
                # (14:55: posted both, cancelled Down at Δ=+28, Up filled).
                self._skip = f"BTC Δopen={btc - open_price:+.1f} ≥ {self._limit:.0f}"
                if not self._warned_feed:
                    self._warned_feed = True
                    log.info(
                        "maker: BTC already Δopen=%+.1f (line %.0f); waiting for a "
                        "quiet open before resting a pair",
                        btc - open_price, self._limit,
                    )
            elif already_in and not self.posted:
                # One leg is on / filled: always retry the missing bid, even
                # if the book has gone one-sided (that is how we get stuck naked).
                self._post(tops, relax=True)
            elif not self.posted and left <= self.cfg.maker_cancel_left_secs:
                if self._skip is None:
                    self._skip = f"joined too late (T-{left:.0f}s)"
            elif not self.posted:
                if self._book_undecided(tops):
                    self._post(tops)
                else:
                    self._skip = f"book one-sided (up {_fmt(up_top)}, down {_fmt(down_top)})"
                    if not self._warned_onesided:
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

        if self.hedged or self.exited:
            new.extend(self._pull_overfill_bids("already hedged" if self.hedged else "already exited"))
        new.extend(self._maybe_defensive_cancel(btc, open_price))
        new.extend(self._maybe_cancel(left))
        return new

    def close(self) -> list[Fill]:
        """Window is over: make sure nothing is left resting."""
        if not self.orders and not self.fills:
            log.info("maker: never rested this window (%s)", self._skip or "no reason recorded")
        return self._cancel_all("window closed")

    def _defensive_limit(self, left: float, sigma: float | None) -> float:
        """Toxic-move line in USD: fixed floor, widened by the realized tape.

        A $20 move is a decision when BTC drifts $60 a window and noise when
        it swings $300. Scale with the move still possible in the time left.
        """
        limit = self.cfg.maker_defensive_usd
        if limit <= 0:
            return limit
        z = self.cfg.maker_defensive_z
        if sigma is not None and sigma > 0 and z > 0:
            frac = max(min(left, 300.0), 1.0) / 300.0
            limit = max(limit, z * sigma * frac ** 0.5)
        return limit

    @property
    def _keep_bid_left(self) -> float:
        """When already filled on one side, keep the other bid this late."""
        return max(self.cfg.stop_entry_secs, 8.0)

    def complete_pair_signal(
        self, up_top: BookTop | None, down_top: BookTop | None,
    ) -> Signal | None:
        """If one side filled, take the other side to close the pair.

        While the leftover maker bid is still live we do nothing — that 0.46
        fill is the actual edge. A taker buy at 0.52–0.54 plus the 7% fee
        turns a locked $1 into a loss (the live 3/5 leak).

        Only after that bid is gone does the price ramp apply, and the sum
        always includes the taker fee per share:
          * at once            -> avg + ask + fee <= 1.00
          * after grace secs   -> <= maker_pair_max_sum
          * after hard secs    -> <= maker_pair_hard_sum
        Defaults keep those caps at 1.00; sell-to-exit covers the rest.
        """
        if self.hedged or self.exited:
            return None
        side = self.naked_side
        if side is None:
            return None
        other = "down" if side == "up" else "up"
        if self._bid_live(other):
            return None
        top = down_top if other == "down" else up_top
        if top is None or top.best_ask is None:
            return None
        avg = self._avg_price(side)
        if avg is None:
            return None
        ask = top.best_ask
        fee = taker_fee_usdc(1.0, ask, self.cfg.taker_fee_rate)
        all_in = avg + ask + fee
        limit = 1.0
        if self._naked_since is not None:
            age = self._clock() - self._naked_since
            if age >= self.cfg.maker_pair_hard_secs:
                limit = max(1.0, self.cfg.maker_pair_hard_sum)
            elif age >= self.cfg.maker_pair_grace_secs:
                limit = max(1.0, self.cfg.maker_pair_max_sum)
        if all_in > limit + 1e-9:
            return None
        qty = self.naked_shares
        return Signal(
            kind="maker-pair",
            legs=[Leg(
                side=other,
                token_id=self.market.token_for(other),
                max_price=ask,
                stake_usdc=round(qty * ask, 2),
                top=top,
            )],
            reason=(
                f"complete pair: naked {side} @ {avg:.2f} + {other} "
                f"ask {ask:.2f} + fee {fee:.3f} = {all_in:.3f} ≤ {limit:.2f}"
            ),
        )

    def hedge_signal(
        self, proj: TwapProjection | None, open_price: float | None,
        up_top: BookTop | None, down_top: BookTop | None,
    ) -> Signal | None:
        """If our lone leg is projected to lose safely, buy the other side."""
        if self.hedged or self.exited or self.cfg.maker_hedge_max_price <= 0:
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

    def exit_signal(
        self, up_top: BookTop | None, down_top: BookTop | None,
    ) -> Signal | None:
        """Get out of a stuck naked leg.

        Caller must try complete_pair first (that is the ≤ $1.00 lock). We
        fire when the timer runs out *or* our side's bid has already fallen
        `maker_stop_ticks` under the fill — the market decided, waiting only
        makes the bid worse (17:00: 0.42 → 0.38 in 20s). Then take the
        cheaper of selling at the bid and buying the other side as a taker.
        """
        if self.hedged or self.exited or self.cfg.maker_exit_secs <= 0:
            return None
        side = self.naked_side
        if side is None or self._naked_since is None:
            return None
        top = up_top if side == "up" else down_top
        if top is None or top.best_bid is None:
            return None
        avg = self._avg_price(side)
        if avg is None:
            return None
        bid = top.best_bid
        age = self._clock() - self._naked_since
        stop = self.cfg.maker_stop_ticks
        decided = stop > 0 and bid <= avg - stop + 1e-9
        if age < self.cfg.maker_exit_secs and not decided:
            return None
        if bid < self.cfg.maker_exit_min_bid:
            return None
        why = f"bid {bid:.2f} ≤ fill {avg:.2f}−{stop:.2f}" if decided else f"after {age:.0f}s"
        qty = self.naked_shares

        # Two ways out; take the cheaper. Buying the other side locks $1 per
        # pair, so its loss is (avg + ask + fee − 1); selling ours costs
        # (avg − bid). Near the open the two are often within a few cents.
        other = "down" if side == "up" else "up"
        other_top = down_top if other == "down" else up_top
        sell_pnl = qty * (bid - avg)
        if other_top is not None and other_top.best_ask is not None:
            ask = other_top.best_ask
            fee = taker_fee_usdc(1.0, ask, self.cfg.taker_fee_rate)
            all_in = avg + ask + fee
            if qty * (1.0 - all_in) > sell_pnl + 1e-9:
                leftover = self.orders.get(other)
                if leftover is not None and not leftover.done:
                    self._cancel_one(leftover, "exit via pair")
                return Signal(
                    kind="maker-pair",
                    legs=[Leg(
                        side=other,
                        token_id=self.market.token_for(other),
                        max_price=ask,
                        stake_usdc=round(qty * ask, 2),
                        top=other_top,
                    )],
                    reason=(
                        f"exit naked {side} via pair ({why}): {avg:.2f} + {other} ask "
                        f"{ask:.2f} + fee {fee:.3f} = {all_in:.3f}, beats selling @ {bid:.2f}"
                    ),
                )
        return Signal(
            kind="maker-exit",
            legs=[Leg(
                side=side,
                token_id=self.market.token_for(side),
                max_price=bid,
                stake_usdc=round(qty * bid, 2),
                top=top,
                shares=qty,
            )],
            reason=f"exit naked {side} {qty:.1f}sh @ bid {bid:.2f} ({why}, pair never closed)",
        )

    def mark_hedged(self, fills: list[Fill]) -> None:
        self.hedged = True
        self.fills.extend(fills)
        log.info("maker hedged: %s", self.summary())
        # We just bought the other side as a taker. Any bid still resting
        # would now over-fill us (a second, naked leg), so pull them at once.
        # `_pull_overfill_bids` also runs every step so a failed cancel retries.
        self._pull_overfill_bids("pair locked by taker buy")

    def mark_exited(self, fills: list[Fill]) -> None:
        self.exited = True
        self.hedged = True
        self.fills.extend(fills)
        log.info("maker exited: %s", self.summary())
        self._pull_overfill_bids("sold naked leg")

    def _maybe_defensive_cancel(
        self, btc: float | None, open_price: float | None,
    ) -> list[Fill]:
        """Pull bids that a BTC move is about to dump into.

        BTC up vs the witnessed open → Down is the dumped token. BTC down → Up.
        If nothing has filled yet, cancel *both* rests — a leftover 0.46 bid
        is a directional scrap, not a pair. If one side already filled, only
        pull the unfilled toxic bid so the leftover pair bid can still hit.
        """
        found: list[Fill] = []
        if self.hedged or self.exited or self.cfg.maker_defensive_usd <= 0:
            return found
        toxic = self._toxic_side(btc, open_price)
        if toxic is None:
            return found
        delta = btc - open_price
        if not self.fills:
            live = [o for o in self.orders.values() if not o.done]
            if not live:
                # Nothing resting, nothing to protect. Do NOT block the side:
                # if the move fades we want to rest the full pair, not one leg.
                return found
            log.info(
                "maker: defensive cancel pair (BTC Δopen=%+.1f ≥ %.0f, nothing filled)",
                delta, self._limit,
            )
            for o in live:
                fill = self._cancel_one(o, "defensive pair")
                if fill is not None:
                    found.append(fill)
                self._blocked.add(o.side)
            # Sit out only if we actually yanked a pair. A Δ spike while
            # nothing was resting is handled by the hysteresis in
            # _toxic_side; standing down there cost two whole windows (16:25,
            # 16:30) for a move that faded.
            if not found:
                self._stood_down = True
            return found
        self._blocked.add(toxic)
        if self.shares(toxic) > 0.01:
            return found
        order = self.orders.get(toxic)
        if order is None or order.done:
            return found
        log.info(
            "maker: defensive cancel %s bid (BTC Δopen=%+.1f ≥ %.0f)",
            toxic, delta, self._limit,
        )
        fill = self._cancel_one(order, "defensive toxic")
        if fill is not None:
            found.append(fill)
        return found

    def _toxic_side(self, btc: float | None, open_price: float | None) -> str | None:
        if self.cfg.maker_defensive_usd <= 0 or btc is None or open_price is None:
            return None
        delta = btc - open_price
        # 15:10: waited at +25.3, posted at a one-tick dip, then yanked at +24.
        # Once decided, stay decided until BTC is clearly back (75% of threshold).
        limit = self._limit
        if self._feed_hold:
            limit = limit * 0.75
        if abs(delta) < limit:
            self._feed_hold = False
            return None
        self._feed_hold = True
        return "down" if delta > 0 else "up"

    def _pull_overfill_bids(self, why: str) -> list[Fill]:
        """Cancel live bids that can only add a naked leg.

        Once we hedged (or a side is already fully filled), a resting bid on
        that side has no pair to complete -- it just doubles the position.
        """
        found: list[Fill] = []
        wanted = self._wanted_shares()
        for side, order in list(self.orders.items()):
            if order.done:
                continue
            if self.hedged or self.exited or self.shares(side) >= wanted - 0.01:
                log.info("maker: pulling %s bid (%s)", side, why)
                fill = self._cancel_one(order, why)
                if fill is not None:
                    found.append(fill)
        return found

    def _cancel_one(self, order: RestingOrder, why: str) -> Fill | None:
        fill = self.executor.cancel_bid(order)
        if fill is not None:
            self.fills.append(fill)
            log.info("maker: harvested %s %.2f sh on cancel (%s)", fill.side, fill.size, why)
        return fill

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
        lo = self._bid_floor() + tick           # post-only must not cross
        hi = 1.0 - self.cfg.maker_bid + 0.08    # e.g. 0.62 for a 0.46 bid
        for side in ("up", "down"):
            top = tops.get(side)
            if top is None or top.best_ask is None:
                return False
            if not (lo <= top.best_ask <= hi):
                return False
        return True

    def _bid_floor(self) -> float:
        return round(self.cfg.maker_bid - max(self.cfg.maker_bid_give, 0.0), 2)

    def _bid_for(
        self, side: str, tops: dict[str, BookTop | None], relax: bool = False,
    ) -> float | None:
        """Price to rest on `side`: our bid, or one tick under a lower ask.

        None if even the floor would cross (that side has already collapsed).
        `relax` drops the floor: when the other leg is already on, a cheap
        complement is a cheaper pair, not a collapsed side to avoid.
        """
        px = self.cfg.maker_bid
        tick = self.market.tick_size or 0.01
        top = tops.get(side)
        if top is not None and top.best_ask is not None and top.best_ask <= px:
            px = round(top.best_ask - tick, 2)
        floor = self.cfg.maker_exit_min_bid if relax else self._bid_floor()
        if px < floor:
            return None
        return px

    def _avg_price(self, side: str) -> float | None:
        sf = [f for f in self.fills if f.side == side and f.size > 0]
        sz = sum(f.size for f in sf)
        if sz <= 0:
            return None
        return sum(f.cost for f in sf) / sz

    def _wanted_shares(self) -> float:
        px = self.cfg.maker_bid
        return max(self.market.min_size, round(self.cfg.maker_stake_usdc / px, 2))

    def _live_orders(self) -> list[RestingOrder]:
        return [o for o in self.orders.values() if not o.done]

    def _bid_live(self, side: str) -> bool:
        order = self.orders.get(side)
        return order is not None and not order.done

    def _post(self, tops: dict[str, BookTop | None], relax: bool = False) -> None:
        """Rest any missing bid. Only mark posted once BOTH sides are on the book.

        A CLOB/network reject must not burn the rest of the window: we retry
        each tick until both bids land or we give up. A done, unfilled order
        is replaced so a cancel / reject can be retried.
        """
        m = self.market
        tick = m.tick_size or 0.01
        shares = self._wanted_shares()
        attempted = 0
        for side in ("up", "down"):
            if side in self._blocked:
                continue
            if self.shares(side) >= shares - 0.01:
                continue
            existing = self.orders.get(side)
            if existing is not None and not existing.done:
                continue
            px = self._bid_for(side, tops, relax)
            if px is None:
                continue  # that side's ask is under our floor; retry next tick
            attempted += 1
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
        if attempted == 0:
            return
        if not self._warned_incomplete:
            self._warned_incomplete = True
            log.warning(
                "maker: rest incomplete (%d/2 on book); will retry",
                live_or_filled,
            )

    def _maybe_cancel(self, left: float) -> list[Fill]:
        if self.cancelled:
            return []
        if self.fills:
            # Already in on one side: keep the other bid so it can still pair.
            if left <= self._keep_bid_left and self._live_orders():
                return self._cancel_all(f"T-{left:.0f}s leftover (pair still open)")
            return []
        if self.orders and left <= self.cfg.maker_cancel_left_secs:
            return self._cancel_all(f"T-{left:.0f}s cutoff")
        return []

    def _cancel_all(self, why: str) -> list[Fill]:
        if self.cancelled:
            return []
        self.cancelled = True
        live = [o for o in self.orders.values() if not o.done]
        if live:
            log.info("maker: cancelling %d resting bid(s) (%s)", len(live), why)
        found: list[Fill] = []
        for o in live:
            fill = self._cancel_one(o, why)
            if fill is not None:
                found.append(fill)
        return found


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
