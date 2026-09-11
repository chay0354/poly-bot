"""Measure how long a stale quote survives after a BTC jump.

The one number the sniper lives or dies on. After Binance prints a $25 move
the side that just got more valuable is still offered at its old price for
*some* milliseconds; whoever hits it first earns (new fair − old ask). We
lost that race from the other side all of 10 Sep. Before spending a cent on
being the taker we record every jump and watch the book:

    t0        jump detected (our clock)   → snapshot both tops, fair before/after
    gone_ms   first book event where a buy of `min_size` at ≤ ask0 is no longer
              possible on the stale side (level lifted / eaten / pulled)
    ask_1s    stale side's best ask 1s later      ask_3s  … 3s later
    up_won    filled in at window close

One JSON line per jump in `data/jumps.jsonl`. Zero orders, zero risk.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .clob import BookTop
from .fastfeed import Jump
from .maker import fair_up

log = logging.getLogger("pm5.jumps")


def sigma_window(range_5m: float, window_secs: float) -> float:
    """σ of the move over `window_secs` implied by the realized 5-min range
    (same random-walk scaling `fair_up` uses: σ_5m ≈ range / 1.6)."""
    return (range_5m / 1.6) * math.sqrt(max(window_secs, 1e-3) / 300.0)


def jump_threshold(range_5m: float | None, window_secs: float, k: float, min_usd: float) -> float:
    """A move counts as a jump when it beats both the dollar floor and
    k·σ for that window. No σ yet → dollar floor only."""
    if range_5m is None or range_5m <= 0 or k <= 0:
        return min_usd
    return max(min_usd, k * sigma_window(range_5m, window_secs))


def available_at(top: BookTop | None, price: float) -> float:
    """Shares offered at or below `price` (what a FAK buy at `price` can take)."""
    return 0.0 if top is None else top.offered_at(price)


@dataclass
class JumpRecord:
    ts: str
    window: str
    window_start: int
    left: float
    venue: str
    delta: float  # USD over `window_secs`
    window_secs: float
    delta_open: float | None  # venue's move since the window open (for fair)
    sigma_5m: float | None
    threshold: float
    stale_side: str  # side whose ask is now too cheap
    ask0: float | None
    ask0_size: float
    avail0: float  # shares buyable at ≤ ask0 on the stale side at t0
    bid0: float | None
    other_ask0: float | None
    fair_before: float | None  # P(stale side) before the jump
    fair_after: float | None  # … after
    edge0: float | None  # fair_after − ask0
    gone_ms: float | None = None
    ask_1s: float | None = None
    ask_3s: float | None = None
    up_won: bool | None = None
    _t0: float = field(default=0.0, repr=False)
    _closed: bool = field(default=False, repr=False)

    @property
    def open(self) -> bool:
        return not self._closed

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("_t0", None)
        d.pop("_closed", None)
        return d


class JumpWatch:
    """Per-window jump observer. `observe` runs every tick with fresh tops."""

    REARM_SECS = 2.0  # a drift re-triggering every tick is one jump, not fifty
    MIN_LEFT = 5.0

    def __init__(self, market, path: str | None, window_secs: float, k: float,
                 min_usd: float, min_size: float = 5.0, clock=time.monotonic) -> None:
        self.market = market
        self.path = path
        self.window_secs = window_secs
        self.k = k
        self.min_usd = min_usd
        self.min_size = min_size
        self._clock = clock
        self.records: list[JumpRecord] = []
        self._last_open: float = -1e9
        self.last: JumpRecord | None = None

    # ------------------------------------------------------------------ tick

    def observe(
        self, jump: Jump | None, sigma_5m: float | None, delta_open: float | None,
        up_top: BookTop | None, down_top: BookTop | None,
    ) -> JumpRecord | None:
        """Update open records from the books; open a new one on a fresh jump.
        Returns the record just opened (the sniper's trigger), else None."""
        now = self._clock()
        self._update(now, up_top, down_top)
        if jump is None:
            return None
        left = self.market.seconds_left
        if left < self.MIN_LEFT or now - self._last_open < self.REARM_SECS:
            return None
        thr = jump_threshold(sigma_5m, self.window_secs, self.k, self.min_usd)
        if abs(jump.delta) < thr:
            return None
        side = "up" if jump.delta > 0 else "down"
        top = up_top if side == "up" else down_top
        other = down_top if side == "up" else up_top
        fair_b = fair_a = None
        if sigma_5m is not None and sigma_5m > 0 and delta_open is not None:
            p_after = fair_up(delta_open, sigma_5m, left)
            p_before = fair_up(delta_open - jump.delta, sigma_5m, left)
            fair_a = p_after if side == "up" else 1.0 - p_after
            fair_b = p_before if side == "up" else 1.0 - p_before
        ask0 = top.best_ask if top is not None else None
        rec = JumpRecord(
            ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            window=self.market.slug,
            window_start=int(self.market.window_start),
            left=round(left, 2),
            venue=jump.venue,
            delta=round(jump.delta, 2),
            window_secs=self.window_secs,
            delta_open=round(delta_open, 2) if delta_open is not None else None,
            sigma_5m=round(sigma_5m, 2) if sigma_5m is not None else None,
            threshold=round(thr, 2),
            stale_side=side,
            ask0=ask0,
            ask0_size=top.best_ask_size if top is not None else 0.0,
            avail0=round(available_at(top, ask0), 2) if ask0 is not None else 0.0,
            bid0=top.best_bid if top is not None else None,
            other_ask0=other.best_ask if other is not None else None,
            fair_before=round(fair_b, 4) if fair_b is not None else None,
            fair_after=round(fair_a, 4) if fair_a is not None else None,
            edge0=round(fair_a - ask0, 4) if (fair_a is not None and ask0 is not None) else None,
            _t0=now,
        )
        if ask0 is None:
            rec.gone_ms = 0.0  # nothing was offered to begin with
        self.records.append(rec)
        self.last = rec
        self._last_open = now
        log.info(
            "jump %s %+.1f$/%.1fs (thr %.1f, %s) T-%.0fs: %s ask %s×%.0f, fair %s→%s, edge %s",
            jump.venue, jump.delta, self.window_secs, thr, _fmt(sigma_5m), left, side,
            _fmt(ask0), rec.ask0_size, _fmt(fair_b), _fmt(fair_a), _fmt(rec.edge0),
        )
        return rec

    def _update(self, now: float, up_top, down_top) -> None:
        for rec in self.records:
            if not rec.open:
                continue
            top = up_top if rec.stale_side == "up" else down_top
            age = now - rec._t0
            if rec.gone_ms is None and rec.ask0 is not None:
                if available_at(top, rec.ask0) < self.min_size - 1e-9:
                    rec.gone_ms = round(age * 1000.0, 1)
            ask = top.best_ask if top is not None else None
            if rec.ask_1s is None and age >= 1.0:
                rec.ask_1s = ask
            if age >= 3.0:
                rec.ask_3s = ask
                rec._closed = True
                if rec.gone_ms is None:
                    rec.gone_ms = 3000.0  # still there after 3s: censored at the horizon
                if rec.ask0 is not None:
                    log.info("jump %s: stale %s ask %s lived %.0fms; 1s %s, 3s %s",
                             rec.ts[11:23], rec.stale_side, _fmt(rec.ask0), rec.gone_ms,
                             _fmt(rec.ask_1s), _fmt(rec.ask_3s))

    # ----------------------------------------------------------------- close

    def settle(self, up_won: bool | None) -> int:
        """Window over: fill in the outcome and append every record."""
        now = self._clock()
        for rec in self.records:
            rec.up_won = up_won
            if rec.gone_ms is None and rec.ask0 is not None:
                rec.gone_ms = round((now - rec._t0) * 1000.0, 1)
        n = self._write()
        self.records = []
        return n

    def _write(self) -> int:
        if not self.path or not self.records:
            return 0
        p = Path(self.path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                for rec in self.records:
                    fh.write(json.dumps(rec.to_json()) + "\n")
        except OSError as e:
            log.warning("jumps write failed: %s", e)
            return 0
        return len(self.records)


def _fmt(v) -> str:
    return "?" if v is None else f"{v:.2f}"
