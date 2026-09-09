"""Main trading loop.

Per 5-minute window:
  1. Discover the market (Gamma).
  2. Stream the Chainlink price (used for momentum + paper settlement).
  3. Continuously check the arbitrage strategy.
  4. In the closing seconds, check the momentum strategy.
  5. Execute at most `max_trades_per_window` entries.
  6. When the window closes, settle paper positions against the final price.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from . import net
from .clob import BookReader, Executor, Fill
from .config import Config
from .maker import MakerPair
from .markets import Market, MarketDiscovery, current_window_start
from .pricefeed import ChainlinkFeed
from .recorder import Recorder
from .status import LiveStatus
from .strategy import ArbitrageStrategy, MomentumStrategy, Signal

log = logging.getLogger("pm5.bot")


class Position:
    """Paper position accumulated within a window, settled at close."""

    def __init__(self) -> None:
        self.fills: list[Fill] = []

    def add(self, fill: Fill) -> None:
        self.fills.append(fill)

    @property
    def cost(self) -> float:
        return sum(f.cost for f in self.fills)

    def settle(self, up_won: bool) -> float:
        """Return realized PnL given the resolved outcome (paper only)."""
        payout = 0.0
        for f in self.fills:
            won = (f.side == "up") == up_won
            payout += f.size * (1.0 if won else 0.0)
        return payout - self.cost


class Bot:
    def __init__(self, cfg: Config, status: LiveStatus | None = None) -> None:
        self.cfg = cfg
        self.status = status or LiveStatus(enabled=False)
        net.bootstrap()
        self._http = httpx.Client(timeout=15)
        self.discovery = MarketDiscovery(cfg.gamma_url, self._http)
        self.reader = BookReader(cfg.clob_url, self._http)
        self.executor = Executor(cfg, self.reader)
        self.feed = ChainlinkFeed(cfg.ws_live_url, net.WS_LIVE_HOST)
        self.momentum = MomentumStrategy(cfg, self.feed)
        self.arb = ArbitrageStrategy(cfg, self.reader)
        self.recorder = Recorder(cfg.data_file, enabled=cfg.record, mode=cfg.mode)
        self.session_pnl = 0.0
        self.day_pnl = 0.0

    async def run(self) -> None:
        feed_task = asyncio.create_task(self.feed.run())
        ok = await self.feed.wait_connected(timeout=20)
        if not ok:
            log.error("price feed did not connect; aborting")
            feed_task.cancel()
            return
        bank = self.executor.bankroll
        if self.cfg.mode == "signal":
            log.info(
                "starting in SIGNAL mode | no orders | I'll shout when to click in the browser"
            )
        else:
            log.info(
                "starting in %s mode | stake=$%.2f | bankroll=%s | "
                "momentum TWAP%.0fs Δ≥$%.0f flip≥$%.0f ask $%.2f–$%.2f | maker %s",
                self.cfg.mode.upper(), self.cfg.stake_usdc,
                f"${bank:.2f}" if bank is not None else "unlimited",
                self.cfg.twap_secs, self.cfg.min_delta_usd, self.cfg.min_flip_usd,
                self.cfg.min_price, self.cfg.max_price,
                (f"ON bid {self.cfg.maker_bid:.2f}×2 ${self.cfg.maker_stake_usdc:.0f}/side, "
                 f"hedge≤{self.cfg.maker_hedge_max_price:.2f}")
                if self.cfg.maker_enabled else "off",
            )
        try:
            while True:
                if self._bankroll_exhausted():
                    log.error("paper bankroll exhausted ($%.2f left); stopping",
                              self.executor.bankroll)
                    break
                if self.cfg.daily_loss_limit_usdc > 0 and (
                    self.day_pnl <= -self.cfg.daily_loss_limit_usdc
                ):
                    log.error("daily loss limit hit (%.2f); stopping", self.day_pnl)
                    break
                await self._trade_window()
        finally:
            feed_task.cancel()
            self.recorder.close()

    def _min_trade_cost(self) -> float:
        """Smallest amount needed to place any enabled strategy's next trade."""
        costs = []
        if self.cfg.momentum_enabled:
            costs.append(self.cfg.stake_usdc)
        if self.cfg.arb_enabled:
            costs.append(2 * self.cfg.arb_stake_usdc)  # arb needs both legs
        if self.cfg.maker_enabled:
            costs.append(2 * self.cfg.maker_stake_usdc)  # both bids may fill
        return min(costs) if costs else self.cfg.stake_usdc

    def _bankroll_exhausted(self) -> bool:
        bank = self.executor.bankroll
        return bank is not None and bank < self._min_trade_cost()

    async def _trade_window(self) -> None:
        ws = current_window_start()
        market = self.discovery.fetch(ws)
        if market is None:
            # Market not deployed yet; wait briefly and retry.
            await asyncio.sleep(2)
            return

        witnessed = self.feed.witnessed_open(market.window_start)
        note = "" if witnessed else " (open not witnessed — arb only)"
        log.info("── window %s | %s%s", market.slug, market.question, note)
        position = Position()
        trades = 0
        open_price: float | None = None
        path: list[dict] = []  # price/quote snapshots over the decision zone
        self._window_logged = set()  # dedup repetitive attempt logs within a window
        maker = (
            MakerPair(self.cfg, self.executor, market)
            if self.cfg.maker_enabled and self.cfg.mode != "signal" else None
        )

        try:
            while market.seconds_left > 0:
                if current_window_start() != ws:
                    break  # rolled into the next window

                if witnessed and open_price is None:
                    open_price = self.feed.price_at_or_after(market.window_start)
                    if open_price is not None:
                        log.info("window open price ≈ %.1f", open_price)

                secs_left = market.seconds_left
                sampling = (
                    self.cfg.record and self.cfg.path_secs > 0 and secs_left <= self.cfg.path_secs
                )
                # Read both books at most once per tick, shared by arb / maker /
                # status / path.
                up_top = down_top = None
                if self.status.enabled or self.cfg.arb_enabled or sampling or maker is not None:
                    up_top = self.reader.top(market.up_token)
                    down_top = self.reader.top(market.down_token)

                if sampling:
                    tick = self.feed.latest
                    path.append({
                        "t": round(secs_left, 1),
                        "btc": round(tick.price, 2) if tick else None,
                        "up": up_top.best_ask if up_top else None,
                        "dn": down_top.best_ask if down_top else None,
                    })

                if maker is not None:
                    tick = self.feed.latest
                    btc = tick.price if tick else None
                    fills = maker.step(
                        up_top, down_top, btc=btc, open_price=open_price,
                        sigma=self.feed.realized_vol(300.0),
                    )
                    for f in fills:
                        position.add(f)
                    if fills:
                        self._record_fills(
                            market, Signal("maker", [], f"rest bid {self.cfg.maker_bid:.2f}"),
                            fills, open_price,
                        )
                    pair = maker.complete_pair_signal(up_top, down_top)
                    hedge = None
                    if pair is None and witnessed and open_price is not None:
                        proj = self.feed.projected_close(market.window_end, self.cfg.twap_secs)
                        hedge = maker.hedge_signal(proj, open_price, up_top, down_top)
                    follow = pair or hedge
                    if follow is None:
                        follow = maker.exit_signal(up_top, down_top)
                    if follow is not None:
                        fills = self._execute(follow, market)
                        if fills:
                            for f in fills:
                                position.add(f)
                            if follow.kind == "maker-exit":
                                maker.mark_exited(fills)
                            else:
                                maker.mark_hedged(fills)
                            self._record_fills(market, follow, fills, open_price)

                if trades < self.cfg.max_trades_per_window:
                    # Momentum only when we actually saw the open; arb is always safe.
                    signal = self._pick_signal(market, witnessed, up_top, down_top)
                    if signal is not None:
                        fills = self._execute(signal, market)
                        for f in fills:
                            position.add(f)
                        if fills:
                            trades += 1
                            self._window_logged.clear()  # let any further trade log fresh
                            self._record_fills(market, signal, fills, open_price)

                self._render_status(market, open_price, up_top, down_top, position)
                await asyncio.sleep(self.cfg.poll_interval_secs)
        finally:
            if maker is not None:
                late = maker.close()  # never leave a bid resting into the next window
                for f in late:
                    position.add(f)
                if late:
                    self._record_fills(
                        market, Signal("maker", [], "harvest on window close"),
                        late, open_price,
                    )

        self._settle_window(market, position, open_price, witnessed, path)

    def _pick_signal(
        self, market: Market, allow_momentum: bool, up_top, down_top
    ) -> Signal | None:
        # Arbitrage is risk-free, so prefer it whenever available.
        sig = self.arb.evaluate(market, up_top, down_top)
        if sig is not None:
            return sig
        if allow_momentum:
            return self.momentum.evaluate(market)
        return None

    def _render_status(self, market, open_price, up_top, down_top, position) -> None:
        if not self.status.enabled:
            return
        tick = self.feed.latest
        price = tick.price if tick else None
        delta = (price - open_price) if (price is not None and open_price is not None) else None

        def quote(t):
            if t is None or t.best_bid is None or t.best_ask is None:
                return "  -/-  "
            return f"{t.best_bid:.2f}/{t.best_ask:.2f}"

        parts = [
            market.question.split(", ")[-1] if ", " in market.question else market.slug,
            f"T-{max(0, int(market.seconds_left)):>3d}s",
            f"BTC {price:,.1f}" if price is not None else "BTC   -  ",
            f"Δ {self.status.color_delta(delta)}",
            f"up {quote(up_top)} dn {quote(down_top)}",
            f"pos {self._pos_summary(position)}",
        ]
        if self.executor.bankroll is not None:
            parts.append(f"bank ${self.executor.bankroll:.2f}")
        parts.append(f"sess {self.status.color_pnl(self.session_pnl)}")
        self.status.render("  ".join(parts))

    def _credit_bankroll(self, amount: float) -> None:
        if self.executor.bankroll is not None:
            self.executor.bankroll += amount

    @staticmethod
    def _pos_summary(position: "Position") -> str:
        if not position.fills:
            return "—"
        by_side: dict[str, float] = {}
        for f in position.fills:
            by_side[f.side] = by_side.get(f.side, 0.0) + f.size
        return " ".join(f"{s.upper()} {sz:.1f}sh" for s, sz in by_side.items())

    def _should_log(self, key: str) -> bool:
        """True the first time `key` is seen this window; suppresses repeats.

        Keeps a per-window set so a signal that re-fires every poll (e.g. a
        momentum entry that can't fill for 40s) is logged once, not flooded.
        """
        seen = getattr(self, "_window_logged", None)
        if seen is None:  # e.g. _execute_arb called directly in a unit test
            seen = self._window_logged = set()
        if key in seen:
            return False
        seen.add(key)
        return True

    def _execute(self, signal: Signal, market: Market | None = None) -> list[Fill]:
        if self.cfg.mode == "signal":
            return self._announce_signal(signal, market)
        if signal.kind == "arb":
            return self._execute_arb(signal)
        if signal.kind == "maker-exit":
            return self._execute_exit(signal)
        side = signal.legs[0].side
        if self._should_log(f"sig:mom:{side}"):
            log.info("SIGNAL[%s] %s", signal.kind, signal.reason)
        fills: list[Fill] = []
        for leg in signal.legs:
            fill = self.executor.buy(
                leg.token_id, leg.side, leg.stake_usdc, leg.max_price, leg.top,
                min_price=leg.min_price,
            )
            if fill is not None:
                fills.append(fill)
            elif self.executor.last_skip and self._should_log(
                f"skip:{leg.side}:{self.executor.last_skip}"
            ):
                log.info("  ↳ %s not taken: %s (still trying)", leg.side, self.executor.last_skip)
        return fills

    def _execute_exit(self, signal: Signal) -> list[Fill]:
        side = signal.legs[0].side
        if self._should_log(f"sig:exit:{side}"):
            log.info("SIGNAL[%s] %s", signal.kind, signal.reason)
        fills: list[Fill] = []
        for leg in signal.legs:
            shares = round(leg.stake_usdc / leg.max_price, 2) if leg.max_price else 0.0
            fill = self.executor.sell(
                leg.token_id, leg.side, shares, min_price=leg.max_price, top=leg.top,
            )
            if fill is not None:
                fills.append(fill)
            elif self.executor.last_skip and self._should_log(
                f"skip:exit:{leg.side}:{self.executor.last_skip}"
            ):
                log.info("  ↳ %s not sold: %s (still trying)", leg.side, self.executor.last_skip)
        return fills

    def _announce_signal(self, signal: Signal, market: Market | None) -> list[Fill]:
        """Print a one-shot browser instruction. No order is placed."""
        fills: list[Fill] = []
        actions: list[str] = []
        url = market.browser_url if market is not None else ""
        left = f"{market.seconds_left:.0f}" if market is not None else "?"

        if signal.kind == "arb":
            for leg in signal.legs:
                ask = self._leg_ask(leg)
                if ask is None:
                    if self._should_log(f"sig-skip:{leg.side}:no-ask"):
                        log.info("signal skipped: no ask on %s", leg.side)
                    return []
                actions.append(f"BUY {leg.side.upper()}   pay {ask:.2f} or less")
                fills.append(self._virtual_fill(leg, ask))
        else:
            leg = signal.legs[0]
            ask = self._leg_ask(leg)
            if ask is None:
                if self._should_log(f"sig-skip:{leg.side}:no-ask"):
                    log.info("signal skipped: no ask on %s (still watching)", leg.side)
                return []
            if ask > leg.max_price:
                if self._should_log(f"sig-skip:{leg.side}:cap"):
                    log.info(
                        "signal skipped: %s ask %.2f > cap %.2f (still watching)",
                        leg.side, ask, leg.max_price,
                    )
                return []
            if ask < leg.min_price:
                if self._should_log(f"sig-skip:{leg.side}:floor"):
                    log.info(
                        "signal skipped: %s ask %.2f < floor %.2f, market disagrees "
                        "(still watching)",
                        leg.side, ask, leg.min_price,
                    )
                return []
            actions.append(f"BUY {leg.side.upper()}   pay {ask:.2f} or less")
            fills.append(self._virtual_fill(leg, ask))

        body = "\n".join(f"  {line}" for line in actions)
        extra = f"  then HOLD until the window ends (do not SELL)\n  {left}s left"
        if url:
            extra += f"\n  {url}"
        bar = "=" * 62
        log.info("\n%s\n%s\n%s\n%s\a", bar, body, extra, bar)
        return fills

    def _leg_ask(self, leg) -> float | None:
        if leg.top is not None and leg.top.best_ask is not None:
            return leg.top.best_ask
        top = self.reader.top(leg.token_id)
        return top.best_ask

    @staticmethod
    def _virtual_fill(leg, ask: float) -> Fill:
        shares = round(leg.stake_usdc / ask, 2)
        cost = round(shares * ask, 4)
        return Fill(leg.token_id, leg.side, ask, shares, cost, paper=True)

    def _execute_arb(self, signal: Signal) -> list[Fill]:
        """All-or-none: only fill if every leg is still fillable at signal price.

        A half-filled arbitrage is naked directional risk -- the opposite of the
        intent -- so we abort rather than leg in. We re-read the book fresh (the
        signal snapshot may be a tick stale) so a leg that has moved out of range
        is caught *before* we fill the other one.
        """
        if self._should_log("sig:arb"):
            log.info("SIGNAL[%s] %s", signal.kind, signal.reason)
        tops = {leg.side: self.reader.top(leg.token_id) for leg in signal.legs}
        for leg in signal.legs:
            top = tops[leg.side]
            if top.best_ask is None or top.best_ask > leg.max_price:
                if self._should_log(f"arb-skip:{leg.side}"):
                    log.info("arb aborted: %s leg not fillable (ask=%s)", leg.side, top.best_ask)
                return []
        combined = sum(tops[leg.side].best_ask for leg in signal.legs)
        if combined > 1.0 - self.cfg.arb_min_edge:
            if self._should_log("arb-edge"):
                log.info("arb aborted: edge gone (combined=%.3f)", combined)
            return []
        # Need to afford *both* legs, else we'd leg in and hold directional risk.
        bank = self.executor.bankroll
        if bank is not None and bank < sum(leg.stake_usdc for leg in signal.legs):
            if self._should_log("arb-bankroll"):
                log.info("arb aborted: insufficient bankroll ($%.2f)", bank)
            return []

        fills: list[Fill] = []
        for leg in signal.legs:
            fill = self.executor.buy(
                leg.token_id, leg.side, leg.stake_usdc, leg.max_price, tops[leg.side]
            )
            if fill is not None:
                fills.append(fill)
        if len(fills) != len(signal.legs):
            # Possible in live mode if the book moved between POSTs.
            log.warning("arb partial fill (%d/%d legs); residual directional risk",
                        len(fills), len(signal.legs))
        return fills

    def _settle_window(
        self, market: Market, position: Position, open_price: float | None,
        witnessed: bool, path: list[dict] | None = None,
    ) -> None:
        # The market settles on the Chainlink TWAP over the final `twap_secs`,
        # not the last tick.
        close_price = self.feed.twap(market.window_end - self.cfg.twap_secs, market.window_end)
        if close_price is None and self.feed.latest:
            close_price = self.feed.latest.price
        # Outcome is only trustworthy when we witnessed the true open.
        up_won = (
            (close_price >= open_price)
            if (witnessed and open_price is not None and close_price is not None)
            else None
        )
        window_pnl: float | None = None
        is_live = any(not f.paper for f in position.fills)

        if not position.fills:
            log.info("no trades this window")
        elif is_live:
            # Live fills settle on-chain at resolution; just report exposure.
            log.info("live fills placed this window: cost=$%.2f (settles on-chain)", position.cost)
        elif up_won is None:
            # Should not happen (momentum is gated on `witnessed`), but never
            # fabricate a settlement we can't compute. Refund the paper stake so
            # the bankroll isn't silently drained by an unsettleable window.
            log.warning("cannot settle paper position: missing price (open=%s close=%s witnessed=%s)",
                        open_price, close_price, witnessed)
            self._credit_bankroll(position.cost)
        else:
            window_pnl = position.settle(up_won)
            self.session_pnl += window_pnl
            self.day_pnl += window_pnl
            # Return the payout (winning shares * $1 = cost + pnl) to the bankroll.
            self._credit_bankroll(position.cost + window_pnl)
            prefix = "if you entered, " if self.cfg.mode == "signal" else ""
            log.info(
                "SETTLE %s won | open=%.1f closeTWAP=%.1f | %swindow PnL=%+.2f | session=%+.2f | bankroll=%s",
                "UP" if up_won else "DOWN", open_price, close_price, prefix,
                window_pnl, self.session_pnl,
                f"${self.executor.bankroll:.2f}" if self.executor.bankroll is not None else "∞",
            )

        self._record_window(
            market, position, witnessed, open_price, close_price, up_won, window_pnl, path or []
        )

    def _record_fills(self, market: Market, signal: Signal, fills, open_price) -> None:
        if not self.recorder.enabled:
            return
        tick = self.feed.latest
        btc = tick.price if tick else None
        delta = (btc - open_price) if (btc is not None and open_price is not None) else None
        for f in fills:
            self.recorder.fill(
                strategy=signal.kind,
                window=market.slug,
                window_start=market.window_start,
                window_end=market.window_end,
                side=f.side,
                price=f.price,
                shares=f.size,
                cost=f.cost,
                order_id=f.order_id,
                maker=f.maker,
                btc_price=btc,
                open_price=open_price,
                delta=round(delta, 2) if delta is not None else None,
                seconds_left=round(market.seconds_left, 1),
                signal_reason=signal.reason,
            )

    def _record_window(
        self, market: Market, position: Position, witnessed: bool,
        open_price, close_price, up_won, window_pnl, path,
    ) -> None:
        if not self.recorder.enabled:
            return
        self.recorder.window(
            window=market.slug,
            window_start=market.window_start,
            window_end=market.window_end,
            witnessed=witnessed,
            open_price=open_price,
            close_price=close_price,
            up_won=up_won,
            traded=bool(position.fills),
            n_fills=len(position.fills),
            cost=round(position.cost, 4),
            window_pnl=round(window_pnl, 4) if window_pnl is not None else None,
            session_pnl=round(self.session_pnl, 4),
            # Per-second snapshots over the last `path_secs` of the window:
            # t = seconds to close, btc = Chainlink price, up/dn = best asks.
            path=path,
        )
