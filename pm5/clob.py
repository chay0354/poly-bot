"""Order book reads and order execution (paper + live)."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field

import httpx

from .config import Config

log = logging.getLogger("pm5.clob")


@dataclass
class BookTop:
    best_bid: float | None
    best_bid_size: float
    best_ask: float | None
    best_ask_size: float
    # Full depth, best first, when the source had it (WS book or REST). The
    # sell path walks `bids` so a thin top level never blocks an exit.
    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)
    source: str = "http"

    def sell_plan(self, shares: float, floor: float) -> tuple[float, float] | None:
        """(worst price, fillable shares) for selling `shares` into bids ≥ floor.

        Walks depth when known; otherwise only the top level is visible and
        we assume it can absorb the order (a FAK then fills what it can).
        """
        levels = self.bids or (
            [(self.best_bid, self.best_bid_size or shares)] if self.best_bid is not None else []
        )
        got = 0.0
        worst: float | None = None
        for price, size in levels:
            if price < floor - 1e-9:
                break
            take = min(size, shares - got)
            if take <= 0:
                break
            got += take
            worst = price
            if got >= shares - 1e-9:
                break
        if worst is None or got <= 0:
            return None
        return worst, round(min(got, shares), 2)


@dataclass
class Fill:
    token_id: str
    side: str  # "up" / "down" (for logging)
    price: float  # avg fill price (paper) or limit price (live)
    size: float  # shares (net of any taker fee, which Polymarket charges in shares)
    cost: float  # USDC spent
    paper: bool
    order_id: str | None = None
    maker: bool = False  # resting bid that got hit (0% fee) vs. a taker buy


@dataclass
class RestingOrder:
    """A post-only GTC bid resting on the book (real or simulated)."""

    token_id: str
    side: str
    price: float
    size: float  # shares requested
    filled: float = 0.0  # shares matched so far
    order_id: str | None = None
    paper: bool = True
    done: bool = False  # cancelled or fully filled
    # Live orders are placed and cancelled off-thread so the trading loop
    # never blocks on the CLOB round trip. `pending` = placement in flight
    # (no id yet); `cancelling` = cancel sent, final fill count not yet
    # confirmed; `failed` = placement rejected (the maker re-posts).
    pending: object | None = field(default=None, repr=False, compare=False)
    cancelling: bool = False
    cancel_future: object | None = field(default=None, repr=False, compare=False)
    cancel_sent_at: float = 0.0
    failed: bool = False

    @property
    def remaining(self) -> float:
        return max(0.0, round(self.size - self.filled, 2))

    @property
    def live(self) -> bool:
        """On the book (or about to be) and not being taken down."""
        return not self.done and not self.cancelling


def taker_fee_usdc(shares: float, price: float, rate: float) -> float:
    """Polymarket taker fee: shares × rate × p × (1-p). Peaks at p=0.5."""
    return shares * rate * price * (1.0 - price)


class BookReader:
    """Reads CLOB order books: from the market WebSocket when it has a fresh
    snapshot, else over plain HTTP (no auth needed)."""

    def __init__(self, clob_url: str, client: httpx.Client | None = None,
                 stream=None) -> None:
        self._url = clob_url.rstrip("/")
        self._client = client or httpx.Client(timeout=10)
        self._last_warn = 0.0
        self.stream = stream  # MarketStream | None
        self.http_reads = 0
        self.ws_reads = 0

    def top(self, token_id: str) -> BookTop:
        if self.stream is not None:
            book = self.stream.book(token_id)
            if book is not None:
                self.ws_reads += 1
                return _top_from_levels(book.bid_levels(), book.ask_levels(), "ws")
        self.http_reads += 1
        try:
            r = self._client.get(f"{self._url}/book", params={"token_id": token_id})
            r.raise_for_status()
            book = r.json()
        except (httpx.HTTPError, ValueError) as e:
            # An empty top looks exactly like a dead book, so say why (throttled).
            now = time.monotonic()
            if now - self._last_warn > 30:
                self._last_warn = now
                log.warning("book read failed (%s); strategies see an empty book", e)
            return BookTop(None, 0.0, None, 0.0)
        # API returns bids ascending (best=last) and asks descending (best=last).
        bids = [(float(l["price"]), float(l["size"])) for l in (book.get("bids") or [])]
        asks = [(float(l["price"]), float(l["size"])) for l in (book.get("asks") or [])]
        bids.sort(reverse=True)
        asks.sort()
        return _top_from_levels(bids, asks, "http")


def _top_from_levels(bids: list[tuple[float, float]], asks: list[tuple[float, float]],
                     source: str) -> BookTop:
    return BookTop(
        best_bid=bids[0][0] if bids else None,
        best_bid_size=bids[0][1] if bids else 0.0,
        best_ask=asks[0][0] if asks else None,
        best_ask_size=asks[0][1] if asks else 0.0,
        bids=bids,
        asks=asks,
        source=source,
    )


class Executor:
    """Places (or simulates) buy and sell orders."""

    def __init__(self, cfg: Config, reader: BookReader) -> None:
        self.cfg = cfg
        self.reader = reader
        # Reason the last buy was skipped (None if it filled). The caller logs
        # this; buy() stays quiet so a persistent skip doesn't flood the log.
        self.last_skip: str | None = None
        # Simulated paper bankroll: debited on fill, credited on settlement.
        # None means "not tracked" (live mode, or PM_PAPER_BANKROLL=0).
        self.bankroll: float | None = (
            cfg.paper_bankroll if (cfg.mode == "paper" and cfg.paper_bankroll > 0) else None
        )
        self._live = None  # lazily-built LiveTrader
        if cfg.mode == "live":
            from .live import LiveTrader  # imported lazily to avoid hard dep in paper mode

            self._live = LiveTrader(cfg)

    def buy(
        self,
        token_id: str,
        side: str,
        stake_usdc: float,
        max_price: float,
        top: BookTop | None = None,
        min_price: float = 0.0,
    ) -> Fill | None:
        """Marketable buy in [`min_price`, `max_price`], spending ~`stake_usdc`.

        Pass `top` to reuse an already-fetched book snapshot (avoids a second
        read and the race it creates -- important for atomic arbitrage fills).
        """
        if top is None:
            top = self.reader.top(token_id)
        if top.best_ask is None:
            self.last_skip = "no asks on book"
            return None
        if top.best_ask > max_price:
            self.last_skip = f"ask {top.best_ask:.2f} > cap {max_price:.2f}"
            return None
        if top.best_ask < min_price:
            self.last_skip = (
                f"ask {top.best_ask:.2f} < floor {min_price:.2f} (market disagrees)"
            )
            return None
        if self.bankroll is not None and self.bankroll < stake_usdc:
            self.last_skip = f"insufficient bankroll (${self.bankroll:.2f})"
            return None

        self.last_skip = None
        price = top.best_ask
        shares = round(stake_usdc / price, 2)
        if self._live is None:
            cost = round(shares * price, 4)
            # Taker fee is charged in shares on buys: we pay `cost`, receive fewer.
            fee = taker_fee_usdc(shares, price, self.cfg.taker_fee_rate)
            net_shares = round(shares - fee / price, 2)
            if self.bankroll is not None:
                self.bankroll -= cost
            log.info("[PAPER] BUY %s %.2f sh @ %.3f = $%.2f (fee $%.3f) | bankroll $%s",
                     side, net_shares, price, cost, fee,
                     f"{self.bankroll:.2f}" if self.bankroll is not None else "∞")
            return Fill(token_id, side, price, net_shares, cost, paper=True)

        return self._live.buy(token_id, side, price, shares, min_price=min_price)

    @property
    def user_stream(self):
        """Authenticated order-event stream (live mode only), for the bot to run."""
        return getattr(self._live, "user_stream", None)

    def sell(
        self,
        token_id: str,
        side: str,
        shares: float,
        min_price: float,
        top: BookTop | None = None,
    ) -> Fill | None:
        """Marketable sell of `shares` into the bids at or above `min_price`.

        Walks the book depth: a thin top level used to block the whole exit
        ("bid size 3 < 10.87") and two $5 legs were then held to a $0
        resolution (21:10 and 01:15 UTC, 10 Sep). Now the order is a
        fill-and-kill at the worst price the depth reaches, and if the depth
        above the floor cannot absorb everything we still sell what it can.

        Fill.size is negative and Fill.cost is negative proceeds so Position
        and paper settlement net the exit correctly.
        """
        if shares <= 0:
            self.last_skip = "nothing to sell"
            return None
        if top is None:
            top = self.reader.top(token_id)
        if top.best_bid is None:
            self.last_skip = "no bids on book"
            return None
        if top.best_bid < min_price:
            self.last_skip = f"bid {top.best_bid:.2f} < floor {min_price:.2f}"
            return None
        plan = top.sell_plan(shares, min_price)
        if plan is None:
            self.last_skip = f"no depth ≥ {min_price:.2f}"
            return None
        worst, qty = plan
        # Never round up: selling 10.88 when we hold 10.87 is a hard reject.
        qty = math.floor(qty * 100 + 1e-9) / 100
        if qty <= 0:
            self.last_skip = "depth too thin"
            return None
        partial = qty + 1e-9 < shares
        if partial:
            log.warning("sell %s: depth ≥ %.2f only covers %.2f of %.2f sh; selling what it can",
                        side, min_price, qty, shares)

        self.last_skip = None
        if self._live is None:
            # Paper: assume the walk fills at each level; book the worst price
            # for the whole lot to stay conservative.
            price = worst
            proceeds = round(qty * price, 4)
            fee = taker_fee_usdc(qty, price, self.cfg.taker_fee_rate)
            net = round(proceeds - fee, 4)
            cost = -net
            if self.bankroll is not None:
                self.bankroll -= cost
            log.info(
                "[PAPER] SELL %s %.2f sh @ %.3f = $%.2f (fee $%.3f) | bankroll $%s",
                side, qty, price, net, fee,
                f"{self.bankroll:.2f}" if self.bankroll is not None else "∞",
            )
            return Fill(token_id, side, price, -qty, cost, paper=True)

        return self._live.sell(token_id, side, worst, qty)

    # ------------------------------------------------------------------ maker

    def place_bid(
        self, token_id: str, side: str, price: float, shares: float,
        tick_size: float, neg_risk: bool,
    ) -> RestingOrder | None:
        """Rest a post-only GTC bid. Paper mode just records it; the bankroll
        is debited when (and as much as) it fills."""
        if self._live is None:
            log.info("[PAPER] REST bid %s %.2f sh @ %.2f", side, shares, price)
            return RestingOrder(token_id, side, price, shares, paper=True)
        return self._live.place_bid(token_id, side, price, shares, tick_size, neg_risk)

    def poll_bid(self, order: RestingOrder, top: BookTop | None = None,
                 force: bool = False) -> Fill | None:
        """Check a resting bid for new fills; returns a Fill for the new shares.

        Paper: we can't see the tape, so a bid counts as hit only when the best
        ask has come down to (or through) our price -- a conservative proxy.
        Live: answered from the user WebSocket when it is healthy; `force`
        insists on an HTTP read (used around cancels).
        """
        if order.done:
            return None
        if order.paper:
            if top is None:
                top = self.reader.top(order.token_id)
            if top.best_ask is None or top.best_ask > order.price:
                return None
            avail = top.best_ask_size if top.best_ask_size > 0 else order.remaining
            qty = round(min(order.remaining, avail), 2)
            if qty <= 0:
                return None
            cost = round(qty * order.price, 4)
            if self.bankroll is not None:
                if self.bankroll < cost:
                    return None
                self.bankroll -= cost
            order.filled = round(order.filled + qty, 2)
            if order.remaining <= 0:
                order.done = True
            log.info("[PAPER] bid HIT %s %.2f sh @ %.2f = $%.2f (maker, no fee)",
                     order.side, qty, order.price, cost)
            return Fill(order.token_id, order.side, order.price, qty, cost,
                        paper=True, maker=True)
        return self._live.poll_bid(order, force=force)

    def cancel_bid(self, order: RestingOrder, wait: bool = False) -> Fill | None:
        """Cancel a rest. A bid can fill in the same second we yank it (11:25
        ET: Down hit, we logged 0 filled, never recorded, never sold, lost
        $5), so live cancels stay `cancelling` until the final matched count
        is confirmed (user stream, else a forced HTTP read) — later polls
        harvest that fill. `wait=True` blocks for the confirmation (window
        close, when the maker object is about to go away).
        """
        if order.paper:
            if order.done:
                return None
            order.done = True
            log.info("[PAPER] cancel bid %s (%.2f/%.2f filled)",
                     order.side, order.filled, order.size)
            return None
        return self._live.cancel_bid(order, wait=wait)

    def presign_bids(self, token_id: str, side: str, shares: float, prices: list[float],
                     tick_size: float, neg_risk: bool) -> None:
        """Live: sign likely bids ahead of time. Paper: nothing to do."""
        if self._live is not None:
            self._live.presign(token_id, side, shares, prices, tick_size, neg_risk)

    def forget_presigned(self, token_ids) -> None:
        if self._live is not None:
            self._live.forget_presigned(token_ids)

    def set_wake(self, cb) -> None:
        """Thread-safe callable invoked when a background CLOB call finishes."""
        if self._live is not None:
            self._live.wake = cb
