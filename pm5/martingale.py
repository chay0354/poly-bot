"""Martingale on the 5-minute btc windows, side picked by the rarer pattern.

One bet per window, early, while the chosen side is offered at
MIN_PRICE…MAX_PRICE (≈ a coin flip). Stakes walk PM_MART_STAKES (1, 2, 4, 8):
a win goes back to the first stake, a loss moves to the next, and after the
last stake loses the sequence is over and it starts again at the first.

Side: of the two ways the last three official outcomes can be continued
(e.g. U U D → U U D U or U U D D), bet the one that came up less often over
the last PM_MART_LOOKBACK windows. Windows are independent, so this carries
no edge; it is the owner's rule. The stake ladder caps the damage at
sum(stakes) per sequence.

The next stake depends on the previous bet's result, so no bet is placed
until that window's outcome is known.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time

import httpx

from .clob import BookTop, Executor, Fill, PendingTake
from .config import Config
from .markets import WINDOW_SECS, Market
from .strategy import Leg, Signal

log = logging.getLogger("pm5.martingale")


def mart_shares(stake: float, price: float) -> float:
    """Fewest shares (in 0.01s) costing ≥ `stake` at `price` whose cost is a
    whole number of cents: the CLOB rejects a marketable buy under $1 and a
    buy whose USDC amount has more than 2 decimals (2.39 × 0.42 = 1.0038)."""
    cents = round(price * 100)
    n = math.ceil(stake / price * 100 - 1e-9)
    while cents > 0 and (n * cents) % 100:
        n += 1
    return n / 100


def pick_side(history: list[bool], last3: list[bool]) -> tuple[str, int, int]:
    """("up"|"down", count of last3+Up, count of last3+Down) over `history`
    (oldest first, True = Up won). Ties go to Up."""
    up = down = 0
    for i in range(len(history) - 3):
        if history[i:i + 3] == last3:
            if history[i + 3]:
                up += 1
            else:
                down += 1
    return ("down" if down < up else "up"), up, down


class Martingale:
    """Lives across windows: the stake ladder and the outcome history."""

    def __init__(self, cfg: Config, executor: Executor, asset: str = "btc",
                 clock=time.monotonic, http: httpx.Client | None = None) -> None:
        self.cfg = cfg
        self.executor = executor
        self.asset = asset
        self._clock = clock
        self._http = http
        self.outcomes: dict[int, bool] = {}  # window_start → Up won (official)
        self._lock = threading.Lock()
        self._fetching: set[int] = set()
        # A restart forgets the ladder; PM_MART_START_STEP resumes it.
        self.step = min(max(cfg.mart_start_step, 0), len(cfg.mart_stakes) - 1)
        self.bet_window: int | None = None  # window of the bet awaiting its result
        self.bet_side: str | None = None
        # Per window:
        self.market: Market | None = None
        self.pending: PendingTake | None = None
        self.fills: list[Fill] = []
        self.shots = 0
        self.last_signal: Signal | None = None
        self._next_try = 0.0
        self._next_backfill = 0.0

    @property
    def stakes(self) -> list[float]:
        return self.cfg.mart_stakes

    # --- outcome history ---------------------------------------------------

    def _gamma_outcome(self, ws: int) -> bool | None:
        slug = f"{self.asset}-updown-5m-{ws}"
        c = self._http or httpx.Client(timeout=15)
        try:
            # Without closed=true Gamma lists only open markets.
            r = c.get(f"{self.cfg.gamma_url}/markets", params={"slug": slug, "closed": "true"})
            r.raise_for_status()
            rows = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not rows:
            return None
        m = rows[0]
        if not m.get("closed"):
            return None
        try:
            prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
            outs = [str(x).lower() for x in json.loads(m.get("outcomes") or "[]")]
        except (ValueError, TypeError):
            return None
        if len(prices) != 2 or max(prices) < 0.99:
            return None
        up_i = outs.index("up") if "up" in outs else 0
        return prices[up_i] > 0.5

    def _fetch(self, windows: list[int]) -> None:
        for ws in windows:
            won = self._gamma_outcome(ws)
            with self._lock:
                self._fetching.discard(ws)
                if won is not None:
                    self.outcomes.setdefault(ws, won)

    def backfill(self, now_ws: int) -> None:
        """Fetch missing official outcomes for the lookback (background)."""
        need = []
        with self._lock:
            for k in range(1, self.cfg.mart_lookback + 4):
                ws = now_ws - k * WINDOW_SECS
                if ws not in self.outcomes and ws not in self._fetching:
                    self._fetching.add(ws)
                    need.append(ws)
        if need:
            threading.Thread(target=self._fetch, args=(need,), daemon=True).start()

    def record(self, ws: int, up_won: bool | None) -> None:
        """Our own settlement of a window (Chainlink TWAP); Gamma may refine it."""
        if up_won is not None:
            with self._lock:
                self.outcomes.setdefault(ws, up_won)

    def _history(self, now_ws: int) -> list[bool] | None:
        with self._lock:
            seq = []
            for k in range(self.cfg.mart_lookback + 3, 0, -1):
                won = self.outcomes.get(now_ws - k * WINDOW_SECS)
                if won is not None:
                    seq.append(won)
                elif k <= 3:
                    return None  # the last three must be known
        return seq

    # --- stake ladder ------------------------------------------------------

    def _resolve_last_bet(self) -> bool:
        """Apply the previous bet's result to the ladder. False = not known yet."""
        if self.bet_window is None:
            return True
        with self._lock:
            won = self.outcomes.get(self.bet_window)
        if won is None:
            return False
        if (self.bet_side == "up") == won:
            log.info("martingale: %s won → back to $%.2f", self.bet_side, self.stakes[0])
            self.step = 0
        else:
            self.step += 1
            if self.step >= len(self.stakes):
                log.warning("martingale: lost %d in a row — sequence over, back to $%.2f",
                            len(self.stakes), self.stakes[0])
                self.step = 0
            else:
                log.info("martingale: %s lost → next stake $%.2f",
                         self.bet_side, self.stakes[self.step])
        self.bet_window = self.bet_side = None
        return True

    # --- per window --------------------------------------------------------

    def begin(self, market: Market) -> None:
        self.market = market
        self.pending = None
        self.fills = []
        self.shots = 0
        self.last_signal = None
        self._next_try = 0.0
        self.backfill(market.window_start)

    def poll(self, up_top: BookTop | None, down_top: BookTop | None) -> list[Fill]:
        pt = self.pending
        if pt is None:
            return []
        fill = self.executor.poll_take(pt, up_top if pt.side == "up" else down_top)
        if not pt.done:
            return []
        self.pending = None
        if fill is None:
            self._next_try = self._clock() + self.cfg.mart_retry_ms / 1000.0
            log.info("martingale %s @ %.2f missed: %s", pt.side, pt.price,
                     pt.reason or self.executor.last_skip or "no fill")
            return []
        self.fills.append(fill)
        self.bet_window, self.bet_side = self.market.window_start, fill.side
        log.info("martingale BET %s %.2f sh @ %.3f = $%.2f (stake %d/%d)", fill.side,
                 fill.size, fill.price, fill.cost, self.step + 1, len(self.stakes))
        return [fill]

    def evaluate(self, up_top: BookTop | None, down_top: BookTop | None) -> Signal | None:
        cfg, m = self.cfg, self.market
        if not cfg.mart_enabled or m is None or self.pending is not None or self.fills:
            return None
        if self.shots >= cfg.mart_max_shots or self._clock() < self._next_try:
            return None
        left = m.seconds_left
        if left < cfg.mart_min_left or left > cfg.mart_max_left:
            return None
        hist = self._history(m.window_start) if self._resolve_last_bet() else None
        if hist is None or len(hist) < 4:
            if self._clock() >= self._next_backfill:  # Gamma resolves a minute or two late
                self._next_backfill = self._clock() + 15.0
                self.backfill(m.window_start)
            return None
        side, n_up, n_down = pick_side(hist, hist[-3:])
        top = up_top if side == "up" else down_top
        if top is None or top.best_ask is None:
            return None
        ask = top.best_ask
        if ask < cfg.mart_min_price - 1e-9 or ask > cfg.mart_max_price + 1e-9:
            return None
        stake = self.stakes[self.step]
        shares = mart_shares(stake, ask)
        if top.offered_at(ask) < shares - 1e-9:
            return None
        if self.executor.bankroll is not None and self.executor.bankroll < shares * ask:
            return None
        last3 = "".join("U" if w else "D" for w in hist[-3:])
        return Signal(
            kind="martingale",
            legs=[Leg(side=side, token_id=m.token_for(side), max_price=ask,
                      stake_usdc=round(shares * ask, 2), top=top, shares=shares)],
            reason=(f"last {last3}: {last3}U seen {n_up}×, {last3}D seen {n_down}× in "
                    f"{len(hist)} → {side} @ {ask:.2f}, stake ${stake:g} "
                    f"({self.step + 1}/{len(self.stakes)})"),
        )

    def shoot(self, signal: Signal) -> PendingTake | None:
        leg = signal.legs[0]
        m = self.market
        pt = self.executor.take(leg.token_id, leg.side, leg.max_price, leg.shares,
                                m.tick_size or 0.01, m.neg_risk)
        self.last_signal = signal
        self.shots += 1
        if pt is None:
            self._next_try = self._clock() + self.cfg.mart_retry_ms / 1000.0
            log.info("martingale %s not sent: %s", leg.side, self.executor.last_skip)
            return None
        self.pending = pt
        log.info("SIGNAL[martingale] %s", signal.reason)
        return pt

    def summary(self) -> str:
        return f"stake step {self.step + 1}/{len(self.stakes)}, {len(self.outcomes)} outcomes known"
