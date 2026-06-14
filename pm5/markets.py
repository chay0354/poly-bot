"""Discovery of the current 5-minute BTC up/down market via the Gamma API.

Markets are deterministic: a new one opens every 300s aligned to the Unix
epoch, with slug ``btc-updown-5m-{window_start_ts}``.
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


def current_window_start(now: float | None = None) -> int:
    now = int(now if now is not None else time.time())
    return now - (now % WINDOW_SECS)


def slug_for(window_start: int) -> str:
    return f"btc-updown-5m-{window_start}"


class MarketDiscovery:
    def __init__(self, gamma_url: str, client: httpx.Client | None = None) -> None:
        self._url = gamma_url.rstrip("/")
        self._client = client or httpx.Client(timeout=15)

    def fetch(self, window_start: int) -> Market | None:
        slug = slug_for(window_start)
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
