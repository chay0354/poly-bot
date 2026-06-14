"""Order book reads and order execution (paper + live)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .config import Config

log = logging.getLogger("pm5.clob")


@dataclass
class BookTop:
    best_bid: float | None
    best_bid_size: float
    best_ask: float | None
    best_ask_size: float


@dataclass
class Fill:
    token_id: str
    side: str  # "up" / "down" (for logging)
    price: float  # avg fill price (paper) or limit price (live)
    size: float  # shares
    cost: float  # USDC spent
    paper: bool
    order_id: str | None = None


class BookReader:
    """Reads CLOB order books over plain HTTP (no auth needed)."""

    def __init__(self, clob_url: str, client: httpx.Client | None = None) -> None:
        self._url = clob_url.rstrip("/")
        self._client = client or httpx.Client(timeout=10)

    def top(self, token_id: str) -> BookTop:
        try:
            r = self._client.get(f"{self._url}/book", params={"token_id": token_id})
            r.raise_for_status()
            book = r.json()
        except (httpx.HTTPError, ValueError):
            return BookTop(None, 0.0, None, 0.0)
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        # API returns bids ascending (best=last) and asks descending (best=last).
        best_bid = float(bids[-1]["price"]) if bids else None
        best_bid_size = float(bids[-1]["size"]) if bids else 0.0
        best_ask = float(asks[-1]["price"]) if asks else None
        best_ask_size = float(asks[-1]["size"]) if asks else 0.0
        return BookTop(best_bid, best_bid_size, best_ask, best_ask_size)


class Executor:
    """Places (or simulates) buy orders."""

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
    ) -> Fill | None:
        """Marketable buy up to `max_price`, spending ~`stake_usdc`.

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
        if self.bankroll is not None and self.bankroll < stake_usdc:
            self.last_skip = f"insufficient bankroll (${self.bankroll:.2f})"
            return None

        self.last_skip = None
        price = top.best_ask
        shares = round(stake_usdc / price, 2)
        if self._live is None:
            cost = round(shares * price, 4)
            if self.bankroll is not None:
                self.bankroll -= cost
            log.info("[PAPER] BUY %s %.2f sh @ %.3f = $%.2f | bankroll $%s",
                     side, shares, price, cost,
                     f"{self.bankroll:.2f}" if self.bankroll is not None else "∞")
            return Fill(token_id, side, price, shares, cost, paper=True)

        return self._live.buy(token_id, side, price, shares)
