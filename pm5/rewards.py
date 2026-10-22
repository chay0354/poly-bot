"""Liquidity-rewards maker: get paid for resting quotes, not for guessing.

Polymarket pays a daily pool per incentivized market to orders resting
within `max_spread` cents of the midpoint, sized ≥ `min_size`, whether or
not they fill (docs.polymarket.com/programs/liquidity-rewards). An order at
distance s from the mid scores ((v − s) / v)² · size; between 0.10 and 0.90
a one-sided book counts a third, so we quote both outcomes: a BUY on Yes
below the mid and a BUY on No below its mid (= an ask on Yes above).

A filled Yes bid plus a filled No bid is a pair that redeems for $1, more
than the two bids cost. A lone fill is inventory: stop quoting that side
until the other side pairs it. A sudden move pulls everything for a while
(the fill that follows news is the expensive one).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from .clob import BookReader, BookTop, Fill, RestingOrder
from .config import Config

log = logging.getLogger("pm5.rewards")

ONE_SIDED_DIVISOR = 3.0
MAX_SHARE = 0.5


@dataclass
class RewardMarket:
    condition_id: str
    question: str
    yes_token: str
    no_token: str
    mid: float
    max_spread: float  # cents
    min_size: float  # shares
    rate_per_day: float  # USDC / day for the whole market
    competition: float
    end: datetime | None
    volume_24h: float = 0.0
    tick: float = 0.01
    neg_risk: bool = False

    def days_left(self, now: datetime) -> float | None:
        if self.end is None:
            return None
        return (self.end - now).total_seconds() / 86400.0


def order_score(max_spread_c: float, dist_c: float) -> float:
    """Per-share score of an order `dist_c` cents from the mid."""
    if max_spread_c <= 0 or dist_c >= max_spread_c or dist_c < 0:
        return 0.0
    return ((max_spread_c - dist_c) / max_spread_c) ** 2


def q_min(q_one: float, q_two: float, mid: float) -> float:
    """Polymarket's two-sidedness rule."""
    if 0.10 <= mid <= 0.90:
        return max(min(q_one, q_two), max(q_one, q_two) / ONE_SIDED_DIVISOR)
    return min(q_one, q_two)


def quote_distance_c(cfg: Config, m: RewardMarket) -> float:
    """How far from the mid we rest, in cents: a fraction of the band, ≥ 1 tick."""
    tick_c = m.tick * 100
    d = max(tick_c, m.max_spread * cfg.rw_dist_frac)
    return math.ceil(d / tick_c - 1e-9) * tick_c


def capital_needed(cfg: Config, m: RewardMarket) -> float:
    """USDC two resting bids reserve: size · (yes bid + no bid)."""
    d = quote_distance_c(cfg, m) / 100
    return m.min_size * max(0.0, 1.0 - 2 * d)


def book_share(cfg: Config, m: RewardMarket, top: BookTop,
               mine: tuple[tuple[str, float, float], ...] = ()) -> float | None:
    """Our expected share of the pool against the resting book, scored the way
    Polymarket scores it. `mine` = our own ("yes"|"no", price, size) bids, which
    are in the book and must not count as competition. The API's
    `market_competitiveness` is not in these units (it ranked a 1% market
    first at "$40/day")."""
    q = quote_prices(cfg, m, top.best_bid, top.best_ask)
    if q is None:
        return None
    mid, yes_px, no_px = q
    bids = sum(order_score(m.max_spread, (mid - p) * 100) * s for p, s in top.bids)
    asks = sum(order_score(m.max_spread, (p - mid) * 100) * s for p, s in top.asks)
    for side, p, s in mine:
        if side == "yes":
            bids -= order_score(m.max_spread, (mid - p) * 100) * s
        else:  # a No bid at p is a Yes ask at 1 − p
            asks -= order_score(m.max_spread, (1 - p - mid) * 100) * s
    others = q_min(max(bids, 0.0), max(asks, 0.0), mid)
    ours = q_min(order_score(m.max_spread, (mid - yes_px) * 100) * m.min_size,
                 order_score(m.max_spread, ((1 - mid) - no_px) * 100) * m.min_size, mid)
    if ours <= 0:
        return None
    # An empty band is a snapshot, not a promise: other makers come back.
    return min(ours / (ours + others), MAX_SHARE)


def book_daily(cfg: Config, m: RewardMarket, top: BookTop,
               mine: tuple[tuple[str, float, float], ...] = ()) -> float:
    share = book_share(cfg, m, top, mine)
    return 0.0 if share is None else m.rate_per_day * share


def _end(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_markets(raw: list[dict]) -> list[RewardMarket]:
    out: list[RewardMarket] = []
    for r in raw:
        toks = r.get("tokens") or []
        if len(toks) != 2:
            continue
        yes = next((t for t in toks if str(t.get("outcome", "")).lower() == "yes"), toks[0])
        no = toks[1] if yes is toks[0] else toks[0]
        rate = sum(float(c.get("rate_per_day") or 0) for c in (r.get("rewards_config") or []))
        try:
            out.append(RewardMarket(
                condition_id=r["condition_id"],
                question=str(r.get("question") or ""),
                yes_token=str(yes["token_id"]),
                no_token=str(no["token_id"]),
                mid=float(yes.get("price") or 0),
                max_spread=float(r.get("rewards_max_spread") or 0),
                min_size=float(r.get("rewards_min_size") or 0),
                rate_per_day=rate,
                competition=float(r.get("market_competitiveness") or 0),
                end=_end(r.get("end_date")),
                volume_24h=float(r.get("volume_24hr") or 0),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def rank(cfg: Config, markets: list[RewardMarket],
         now: datetime | None = None) -> list[RewardMarket]:
    """Eligible markets, biggest pool first: far from resolution, with a mid
    where both sides can be quoted. The real ranking needs the book
    (`book_daily`)."""
    now = now or datetime.now(timezone.utc)
    ok = []
    for m in markets:
        days = m.days_left(now)
        if days is None or days < cfg.rw_min_days:
            continue
        if not (cfg.rw_min_mid <= m.mid <= cfg.rw_max_mid):
            continue
        if m.rate_per_day <= 0 or m.min_size <= 0 or m.max_spread <= 0:
            continue
        if m.min_size > cfg.rw_max_min_size:
            continue
        ok.append(m)
    ok.sort(key=lambda m: m.rate_per_day, reverse=True)
    return ok


def pick(cfg: Config, markets: list[RewardMarket], budget: float,
         now: datetime | None = None) -> list[RewardMarket]:
    """Greedy: the best payers whose two bids fit in `budget`."""
    chosen: list[RewardMarket] = []
    left = budget
    for m in rank(cfg, markets, now):
        need = capital_needed(cfg, m)
        if need <= left + 1e-9:
            chosen.append(m)
            left -= need
        if len(chosen) >= cfg.rw_max_markets:
            break
    return chosen


def fetch_reward_markets(clob_url: str, client: httpx.Client | None = None,
                         max_pages: int = 40) -> list[dict]:
    c = client or httpx.Client(timeout=30)
    out: dict[str, dict] = {}
    cursor = None
    for _ in range(max_pages):
        params = {"page_size": 500}
        if cursor:
            params["next_cursor"] = cursor
        r = c.get(f"{clob_url.rstrip('/')}/rewards/markets/multi", params=params)
        r.raise_for_status()
        j = r.json()
        page = j.get("data") or []
        fresh = [m for m in page if m.get("condition_id") not in out]
        for m in fresh:
            out[m["condition_id"]] = m
        cursor = j.get("next_cursor")
        if not cursor or cursor == "LTE=" or not fresh:
            break
    return list(out.values())


def fill_market_details(clob_url: str, m: RewardMarket,
                        client: httpx.Client | None = None) -> bool:
    """Tick size / neg-risk / still open, from the public /markets endpoint."""
    c = client or httpx.Client(timeout=15)
    try:
        r = c.get(f"{clob_url.rstrip('/')}/markets/{m.condition_id}")
        r.raise_for_status()
        j = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("market %s details failed: %s", m.condition_id[:10], e)
        return False
    if j.get("closed") or not j.get("accepting_orders", True) or not j.get("active", True):
        return False
    m.tick = float(j.get("minimum_tick_size") or 0.01)
    m.neg_risk = bool(j.get("neg_risk"))
    return True


def _floor_tick(p: float, tick: float) -> float:
    return round(math.floor(p / tick + 1e-9) * tick, 4)


def quote_prices(cfg: Config, m: RewardMarket, best_bid: float | None,
                 best_ask: float | None) -> tuple[float, float, float] | None:
    """(mid, yes bid, no bid) for this book, or None when it cannot be quoted.

    Yes book only: the No book is its mirror (No ask = 1 − Yes bid)."""
    if best_bid is None or best_ask is None or best_ask <= best_bid:
        return None
    mid = (best_bid + best_ask) / 2
    if (best_ask - best_bid) * 100 > 2 * m.max_spread:
        return None  # nobody quotes near the mid; it is not a real midpoint
    d = quote_distance_c(cfg, m) / 100
    yes = _floor_tick(mid - d, m.tick)
    no = _floor_tick((1 - mid) - d, m.tick)
    # Post-only: never at or through the opposite best.
    yes = min(yes, round(best_ask - m.tick, 4))
    no = min(no, round((1 - best_bid) - m.tick, 4))
    if yes < m.tick or no < m.tick:
        return None
    if order_score(m.max_spread, (mid - yes) * 100) <= 0:
        return None
    if order_score(m.max_spread, ((1 - mid) - no) * 100) <= 0:
        return None
    return mid, yes, no


@dataclass
class _Side:
    token: str
    name: str  # "yes" / "no"
    order: RestingOrder | None = None
    shares: float = 0.0
    cost: float = 0.0


@dataclass
class MarketQuoter:
    """Two bids on one reward market, with inventory and news guards."""

    cfg: Config
    m: RewardMarket
    trader: object  # LiveTrader (live) — kept untyped so tests can fake it
    reader: BookReader
    clock: object = time.monotonic
    yes: _Side = field(init=False)
    no: _Side = field(init=False)
    last_mid: float | None = None
    last_mid_at: float = 0.0
    cooldown_until: float = 0.0
    fills: list[Fill] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.yes = _Side(self.m.yes_token, "yes")
        self.no = _Side(self.m.no_token, "no")

    @property
    def label(self) -> str:
        return self.m.question[:48]

    def live_ids(self) -> list[str]:
        return [s.order.order_id for s in (self.yes, self.no)
                if s.order is not None and s.order.live and s.order.order_id]

    def mine(self) -> tuple[tuple[str, float, float], ...]:
        """Our resting bids, as `book_share` wants them."""
        return tuple((s.name, s.order.price, s.order.size) for s in (self.yes, self.no)
                     if s.order is not None and not s.order.done)

    def _poll(self, s: _Side) -> None:
        if s.order is None:
            return
        f = self.trader.poll_bid(s.order)
        if f is not None:
            s.shares += f.size
            s.cost += f.cost
            self.fills.append(f)
            log.warning("FILL %s %s %.2f sh @ %.3f | %s | inventory yes %.1f no %.1f",
                        s.name, "", f.size, f.price, self.label, self.yes.shares, self.no.shares)

    def _pull(self, s: _Side, wait: bool = False) -> None:
        if s.order is not None and not s.order.done and not s.order.cancelling:
            f = self.trader.cancel_bid(s.order, wait=wait)
            if f is not None:
                s.shares += f.size
                s.cost += f.cost
                self.fills.append(f)

    def pull_all(self, why: str, wait: bool = False) -> None:
        if self.live_ids():
            log.info("pull %s: %s", self.label, why)
        for s in (self.yes, self.no):
            self._pull(s, wait=wait)

    def _may_quote(self, s: _Side) -> bool:
        """Do not add to a lone leg: one fill per side until it is paired."""
        other = self.no if s is self.yes else self.yes
        return s.shares - other.shares < self.m.min_size * 0.5

    def step(self) -> None:
        now = self.clock()
        for s in (self.yes, self.no):
            self._poll(s)
        top = self.reader.top(self.m.yes_token)
        q = quote_prices(self.cfg, self.m, top.best_bid, top.best_ask)
        if q is None:
            self.pull_all("no quotable midpoint")
            return
        mid, yes_px, no_px = q
        if self.last_mid is not None and abs(mid - self.last_mid) * 100 >= self.cfg.rw_jump_cents:
            self.cooldown_until = now + self.cfg.rw_cooldown_secs
            self.pull_all(f"mid jumped {self.last_mid:.3f} → {mid:.3f}; cooling "
                          f"{self.cfg.rw_cooldown_secs:.0f}s")
        self.last_mid, self.last_mid_at = mid, now
        if now < self.cooldown_until:
            return
        for s, px in ((self.yes, yes_px), (self.no, no_px)):
            o = s.order
            if not self._may_quote(s):
                self._pull(s)
                continue
            if o is not None and not o.done:
                if o.cancelling or o.pending is not None:
                    continue
                if abs(o.price - px) * 100 >= self.cfg.rw_requote_cents - 1e-9:
                    self._pull(s)
                continue
            s.order = self.trader.place_bid(
                s.token, s.name, px, self.m.min_size, self.m.tick, self.m.neg_risk,
            )

    def summary(self) -> str:
        pairs = min(self.yes.shares, self.no.shares)
        return (f"{self.label} | mid {self.last_mid if self.last_mid is not None else float('nan'):.3f}"
                f" | yes {self.yes.shares:.1f} sh ${self.yes.cost:.2f}"
                f" no {self.no.shares:.1f} sh ${self.no.cost:.2f} | pairs {pairs:.1f}")
