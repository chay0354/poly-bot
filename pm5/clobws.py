"""CLOB WebSocket streams: public order books and the authenticated user feed.

Polling `/book` twice a second plus `get_order` per resting bid made one
loop iteration ~2s of blocking HTTP. The makers who hit our bids work in
milliseconds. These streams replace the polling so the trading loop can run
at a few hundred ms and only touch HTTP to *act* (post / cancel / sell).

Market channel (public):   wss://ws-subscriptions-clob.polymarket.com/ws/market
    subscribe: {"assets_ids": [token, ...], "type": "market"}
    events:    book (full snapshot), price_change (level updates),
               last_trade_price, tick_size_change
    heartbeat: text frame "PING" every 10s -> "PONG"

User channel (auth):       wss://ws-subscriptions-clob.polymarket.com/ws/user
    subscribe: {"auth": {apiKey, secret, passphrase}, "type": "user"}
    events:    order (PLACEMENT / UPDATE / CANCELLATION, with size_matched),
               trade (match lifecycle)

Both convey level lists as {"price": "0.46", "size": "12.5"} strings. Book
snapshots follow the REST convention (bids ascending, asks descending) but
we never rely on order: levels are kept in dicts and sorted on read.

Neither stream replays what was missed during a disconnect. Consumers keep
an HTTP fallback (BookReader.top / LiveTrader.poll_bid) for that, and treat
a stream as authoritative only while `healthy`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import websockets

log = logging.getLogger("pm5.clobws")

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
WS_HOST = "ws-subscriptions-clob.polymarket.com"

PING_SECS = 10.0


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass
class LocalBook:
    """One token's order book, maintained from a snapshot plus deltas."""

    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    updated: float = 0.0  # monotonic time of the last event applied
    ready: bool = False   # False until a full `book` snapshot arrived

    def replace(self, bids, asks) -> None:
        self.bids = {p: s for p, s in _levels(bids)}
        self.asks = {p: s for p, s in _levels(asks)}
        self.ready = True
        self.updated = time.monotonic()

    def apply(self, side: str, price: float, size: float) -> None:
        book = self.bids if side.upper() == "BUY" else self.asks
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size
        self.updated = time.monotonic()

    def bid_levels(self) -> list[tuple[float, float]]:
        """Best first (highest bid)."""
        return sorted(((p, s) for p, s in self.bids.items() if s > 0), reverse=True)

    def ask_levels(self) -> list[tuple[float, float]]:
        """Best first (lowest ask)."""
        return sorted((p, s) for p, s in self.asks.items() if s > 0)


def _levels(raw) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for lvl in raw or []:
        if not isinstance(lvl, dict):
            continue
        p, s = _f(lvl.get("price")), _f(lvl.get("size"))
        if p is None or s is None:
            continue
        out.append((round(p, 4), s))
    return out


class _Stream:
    """Connect / subscribe / PING loop with reconnect, shared by both channels."""

    name = "stream"

    def __init__(self, url: str, host: str | None = None) -> None:
        self._url = url
        self._host = host
        self._ws = None
        self._connected = False
        self._last_msg = 0.0
        self._reconnect = asyncio.Event()
        self._wake = asyncio.Event()  # set when there is something to subscribe

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def healthy(self) -> bool:
        """Connected and heard from the server (PONG counts) within 3 pings."""
        return self._connected and (time.monotonic() - self._last_msg) < PING_SECS * 3

    def _subscribe_frames(self) -> list[dict]:  # pragma: no cover - overridden
        return []

    def _on_message(self, msg) -> None:  # pragma: no cover - overridden
        pass

    def _on_connect(self) -> None:
        pass

    def request_reconnect(self) -> None:
        self._reconnect.set()
        self._wake.set()

    def _has_work(self) -> bool:
        """Anything to subscribe to? Connecting without a subscription just
        gets the socket closed by the server and a reconnect loop."""
        return True

    async def run(self) -> None:
        backoff = 1.0
        while True:
            if not self._has_work():
                self._wake.clear()
                await self._wake.wait()
                continue
            try:
                kwargs = {"ping_interval": None}
                if self._host:
                    kwargs["server_hostname"] = self._host
                async with websockets.connect(self._url, **kwargs) as ws:
                    self._ws = ws
                    self._reconnect.clear()
                    for frame in self._subscribe_frames():
                        await ws.send(json.dumps(frame))
                    self._connected = True
                    self._last_msg = time.monotonic()
                    self._on_connect()
                    backoff = 1.0
                    log.info("%s connected", self.name)
                    await self._pump(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - reconnect on anything
                log.warning("%s disconnected (%s); reconnecting in %.0fs", self.name, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                self._connected = False
                self._ws = None

    async def _pump(self, ws) -> None:
        async def pinger() -> None:
            while True:
                await asyncio.sleep(PING_SECS)
                await ws.send("PING")

        async def reconnect_watch() -> None:
            await self._reconnect.wait()
            await ws.close()

        tasks = [asyncio.create_task(pinger()), asyncio.create_task(reconnect_watch())]
        try:
            async for raw in ws:
                self._last_msg = time.monotonic()
                self.ingest(raw)
        finally:
            for t in tasks:
                t.cancel()

    def ingest(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        if raw == "PONG":
            return
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if isinstance(msg, list):
            for m in msg:
                if isinstance(m, dict):
                    self._on_message(m)
        elif isinstance(msg, dict):
            self._on_message(msg)


class MarketStream(_Stream):
    """Live order books for the tokens we are quoting.

    `watch(tokens)` declares what we need. Adding a token the connection does
    not carry forces a reconnect (a fresh subscription always yields a full
    `book` snapshot; incremental subscribe frames do not reliably). Dropping
    tokens never reconnects — stale books just go quiet and are pruned.
    """

    name = "market stream"

    def __init__(self, url: str = MARKET_WS_URL, host: str | None = WS_HOST,
                 stale_secs: float = 30.0) -> None:
        super().__init__(url, host)
        self._wanted: set[str] = set()
        self._subscribed: set[str] = set()
        self._books: dict[str, LocalBook] = {}
        self._stale_secs = stale_secs

    def watch(self, token_ids) -> None:
        wanted = {str(t) for t in token_ids if t}
        if not wanted:
            return
        self._wanted = wanted
        if not wanted <= self._subscribed:
            for t in wanted:
                self._books.setdefault(t, LocalBook())
            for t in list(self._books):
                if t not in wanted:
                    self._books.pop(t, None)
            self._subscribed = set()
            self.request_reconnect()

    def _has_work(self) -> bool:
        return bool(self._wanted)

    def _subscribe_frames(self) -> list[dict]:
        if not self._wanted:
            return []
        self._subscribed = set(self._wanted)
        for t in self._subscribed:
            self._books.setdefault(t, LocalBook())
        return [{"assets_ids": sorted(self._subscribed), "type": "market"}]

    def _on_connect(self) -> None:
        # A snapshot is coming for every subscribed token; until then the
        # HTTP fallback answers.
        for b in self._books.values():
            b.ready = False

    def _on_message(self, msg: dict) -> None:
        et = msg.get("event_type")
        if et == "book":
            token = str(msg.get("asset_id") or "")
            book = self._books.get(token)
            if book is None:
                return
            book.replace(msg.get("bids"), msg.get("asks"))
        elif et == "price_change":
            for ch in msg.get("price_changes") or []:
                if not isinstance(ch, dict):
                    continue
                token = str(ch.get("asset_id") or "")
                book = self._books.get(token)
                if book is None or not book.ready:
                    continue
                p, s = _f(ch.get("price")), _f(ch.get("size"))
                side = str(ch.get("side") or "")
                if p is None or s is None or not side:
                    continue
                book.apply(side, round(p, 4), s)

    def book(self, token_id: str) -> LocalBook | None:
        """The live book for `token_id`, or None if we cannot vouch for it."""
        if not self.healthy:
            return None
        b = self._books.get(str(token_id))
        if b is None or not b.ready:
            return None
        if time.monotonic() - b.updated > self._stale_secs:
            return None
        return b


class UserStream(_Stream):
    """Our own order events: fills show up here in ms instead of on the next
    `get_order` poll. Tracks cumulative `size_matched` and status per order id.
    """

    name = "user stream"

    def __init__(self, api_key: str, secret: str, passphrase: str,
                 url: str = USER_WS_URL, host: str | None = WS_HOST) -> None:
        super().__init__(url, host)
        self._auth = {"apiKey": api_key, "secret": secret, "passphrase": passphrase}
        self.orders: dict[str, dict] = {}

    def _subscribe_frames(self) -> list[dict]:
        return [{"auth": self._auth, "type": "user"}]

    def _on_message(self, msg: dict) -> None:
        if msg.get("event_type") != "order":
            return
        oid = str(msg.get("id") or "")
        if not oid:
            return
        matched = _f(msg.get("size_matched"))
        prev = self.orders.get(oid)
        # Events can arrive out of order; matched never decreases.
        if prev is not None and matched is not None and matched < prev.get("matched", 0.0):
            matched = prev["matched"]
        self.orders[oid] = {
            "matched": matched if matched is not None else (prev or {}).get("matched", 0.0),
            "status": str(msg.get("status") or "").upper(),
            "event": str(msg.get("type") or "").upper(),
            "ts": time.monotonic(),
        }
        if len(self.orders) > 500:
            oldest = sorted(self.orders.items(), key=lambda kv: kv[1]["ts"])[:250]
            for k, _ in oldest:
                self.orders.pop(k, None)

    def state(self, order_id: str | None) -> dict | None:
        if not order_id or not self.healthy:
            return None
        return self.orders.get(str(order_id))
