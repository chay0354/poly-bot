"""Discovery of the current 5-minute Up/Down market via the Gamma API.

Markets are deterministic: a new one opens every 300s aligned to the Unix
epoch, with slug ``{asset}-updown-5m-{window_start_ts}`` (btc or eth).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import httpx

WINDOW_SECS = 300


@dataclass
class Market:
    slug: str
    condition_id: str
    question: str
    up_token: str
    down_token: str
    tick_size: float
    min_size: float
    neg_risk: bool
    window_start: int  # unix seconds
    window_end: int  # unix seconds

    @property
    def seconds_left(self) -> float:
        return self.window_end - time.time()

    @property
    def seconds_in(self) -> float:
        return time.time() - self.window_start

    def token_for(self, side: str) -> str:
        return self.up_token if side.lower() == "up" else self.down_token

    @property
    def browser_url(self) -> str:
        return f"https://polymarket.com/event/{self.slug}"


def current_window_start(now: float | None = None) -> int:
    now = int(now if now is not None else time.time())
    return now - (now % WINDOW_SECS)


def slug_for(window_start: int, asset: str = "btc") -> str:
    return f"{asset}-updown-5m-{window_start}"


class MarketDiscovery:
    def __init__(self, gamma_url: str, client: httpx.Client | None = None,
                 asset: str = "btc") -> None:
        self._url = gamma_url.rstrip("/")
        self._client = client or httpx.Client(timeout=15)
        self.asset = asset

    def fetch(self, window_start: int) -> Market | None:
        slug = slug_for(window_start, self.asset)
        try:
            r = self._client.get(f"{self._url}/events", params={"slug": slug})
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, json.JSONDecodeError):
            return None
        if not data:
            return None
        ev = data[0]
        markets = ev.get("markets") or []
        if not markets:
            return None
        m = markets[0]
        token_ids = json.loads(m["clobTokenIds"])
        outcomes = json.loads(m["outcomes"])
        # Map outcome label -> token id so we don't rely on positional order.
        by_outcome = {o.lower(): t for o, t in zip(outcomes, token_ids)}
        return Market(
            slug=slug,
            condition_id=m["conditionId"],
            question=m["question"],
            up_token=by_outcome.get("up", token_ids[0]),
            down_token=by_outcome.get("down", token_ids[1]),
            tick_size=float(m.get("orderPriceMinTickSize", 0.01)),
            min_size=float(m.get("orderMinSize", 5)),
            neg_risk=bool(m.get("negRisk", False)),
            window_start=window_start,
            window_end=window_start + WINDOW_SECS,
        )

    def current(self) -> Market | None:
        return self.fetch(current_window_start())

    def official_up_won(self, slug: str) -> bool | None:
        """Polymarket's resolved winner, not our TWAP read.

        11:30 and 11:40 ET on 13 Sep: our close was Down, Gamma paid Up,
        and live CRM booked −$20 on a winning Up fill. Use this once the
        book has snapped to ~0/1 (or Gamma marked it resolved).
        """
        try:
            r = self._client.get(f"{self._url}/events", params={"slug": slug})
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError):
            return None
        if not data:
            return None
        markets = (data[0] or {}).get("markets") or []
        if not markets:
            return None
        m = markets[0]
        raw = m.get("outcomePrices")
        try:
            prices = json.loads(raw) if isinstance(raw, str) else list(raw or [])
            outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else (m.get("outcomes") or [])
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        if len(prices) < 2 or len(outcomes) < 2:
            return None
        by = {str(o).lower(): float(p) for o, p in zip(outcomes, prices)}
        up_p, dn_p = by.get("up"), by.get("down")
        if up_p is None or dn_p is None:
            up_p, dn_p = float(prices[0]), float(prices[1])
        if up_p >= 0.92 and dn_p <= 0.08:
            return True
        if dn_p >= 0.92 and up_p <= 0.08:
            return False
        return None
