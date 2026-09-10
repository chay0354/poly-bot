"""Offline tests for the latency work: event-driven loop, off-thread CLOB
calls with fill harvesting across an async cancel, Coinbase as a second
leading feed, and pre-signed bids."""

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from pm5.bot import Bot
from pm5.clob import BookTop, Executor, RestingOrder
from pm5.clobws import MarketStream, UserStream
from pm5.config import Config
from pm5.fastfeed import BinanceFeed, CoinbaseFeed, FastFeeds, geo_blocked
from pm5.maker import MakerPair
from pm5.markets import Market, current_window_start, slug_for
from pm5.live import parse_clob_usdc
from pm5.pricefeed import ChainlinkFeed


def _mk(seconds_in=20) -> Market:
    now = time.time()
    start = now - seconds_in
    return Market(
        slug=slug_for(current_window_start()), condition_id="0x", question="q",
        up_token="UP", down_token="DOWN", tick_size=0.01, min_size=5,
        neg_risk=False, window_start=int(start), window_end=int(start + 300),
    )


def _book(ask, bid=None, ask_size=100.0):
    return BookTop(bid if bid is not None else round(ask - 0.02, 2), 50.0, ask, ask_size)


# ------------------------------------------------------------------ feeds

def test_coinbase_ticker_parses_and_ignores_other_frames():
    hits = []
    f = CoinbaseFeed(on_tick=lambda: hits.append(1))
    f._ingest(json.dumps({"type": "subscriptions", "channels": []}))
    f._ingest(json.dumps({"type": "ticker", "product_id": "ETH-USD", "price": "3000",
                          "time": "2026-09-10T07:11:19.611611Z"}))
    assert f.latest is None and not hits
    f._ingest(json.dumps({"type": "ticker", "product_id": "BTC-USD", "price": "78202.69",
                          "time": "2026-09-10T07:11:19.611611Z"}))
    assert f.latest.price == 78202.69
    assert abs(f.latest.src_ts - 1789024279.611611) < 1e-3
    assert hits == [1]
    assert f._subscribe_frame() == {
        "type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"],
    }


def test_binance_451_is_a_geo_block_not_a_blip():
    assert geo_blocked("server rejected WebSocket connection: HTTP 451")
    assert geo_blocked("Restricted location")
    assert not geo_blocked("connection reset")


def test_binance_feed_still_parses_and_wakes():
    hits = []
    f = BinanceFeed("wss://x", on_tick=lambda: hits.append(1))
    now_ms = int(time.time() * 1000)
    f._ingest('{"e":"aggTrade","s":"BTCUSDT","p":"78800.5","T":%d}' % now_ms)
    assert f.latest.price == 78800.5 and hits == [1]


def _seed(feed, ticks):
    """ticks: list of (price, seconds_ago), oldest first."""
    from pm5.pricefeed import Tick
    now = time.time()
    for price, age in ticks:
        feed._history.append(Tick(price=price, src_ts=now - age, recv_ts=now - age))
    feed.latest = feed._history[-1]


def test_fast_feeds_answer_with_the_most_recent_print():
    open_ts = time.time() - 60
    binance = BinanceFeed("wss://x")
    coinbase = CoinbaseFeed()
    # Binance: flat since the open, last print 1s ago. Coinbase: +$20, printed now.
    _seed(binance, [(78000.0, 61), (78000.0, 30), (78001.0, 1.0)])
    _seed(coinbase, [(78005.0, 61), (78005.0, 30), (78025.0, 0.0)])
    ff = FastFeeds([binance, coinbase])
    assert ff.source == "coinbase"
    assert ff.delta_since(open_ts) == pytest.approx(20.0)      # same-venue delta
    # Binance prints again a moment later: it is now the freshest view.
    _seed(binance, [(78002.0, -0.001)])
    assert ff.source == "binance"
    assert ff.delta_since(open_ts) == pytest.approx(2.0)
    # σ is the widest fresh tape.
    assert ff.realized_vol(100) == pytest.approx(20.0)


def test_fast_feeds_skip_a_venue_that_did_not_see_the_open():
    open_ts = time.time() - 60
    binance = BinanceFeed("wss://x")
    coinbase = CoinbaseFeed()
    _seed(binance, [(78000.0, 61), (78000.0, 59.5), (78010.0, 1.0)])
    _seed(coinbase, [(78100.0, 5), (78130.0, 0.0)])  # connected after the open
    ff = FastFeeds([binance, coinbase])
    assert ff.source == "coinbase"                        # freshest print...
    assert ff.delta_since(open_ts) == pytest.approx(10.0)  # ...but Binance covers the open
    assert not coinbase.covers(open_ts) and ff.covers(open_ts)


# ------------------------------------------------------------------ wake hooks

def test_streams_wake_only_on_state_changes():
    hits = []
    ms = MarketStream()
    ms.on_event = lambda: hits.append("m")
    ms.watch(["T1"])
    ms.ingest("PONG")
    ms.ingest(json.dumps({"event_type": "book", "asset_id": "OTHER", "bids": [], "asks": []}))
    assert not hits
    ms.ingest(json.dumps({"event_type": "book", "asset_id": "T1",
                          "bids": [{"price": "0.45", "size": "10"}],
                          "asks": [{"price": "0.47", "size": "10"}]}))
    ms.ingest(json.dumps({"event_type": "price_change", "price_changes": [
        {"asset_id": "T1", "price": "0.46", "size": "5", "side": "BUY"}]}))
    ms.ingest(json.dumps({"event_type": "last_trade_price", "asset_id": "T1"}))
    assert hits == ["m", "m"]

    us = UserStream("k", "s", "p")
    us.on_event = lambda: hits.append("u")
    us.ingest(json.dumps({"event_type": "trade"}))
    us.ingest(json.dumps({"event_type": "order", "id": "0x1", "size_matched": "2", "status": "LIVE"}))
    assert hits[-1] == "u" and hits.count("u") == 1

    cl = ChainlinkFeed("wss://x", "x")
    cl.on_tick = lambda: hits.append("c")
    cl._ingest(json.dumps({"topic": "crypto_prices_chainlink",
                           "payload": {"symbol": "eth/usd", "value": "3000", "timestamp": 1}}))
    cl._ingest(json.dumps({"topic": "crypto_prices_chainlink",
                           "payload": {"symbol": "btc/usd", "value": "78000", "timestamp": 1}}))
    assert hits.count("c") == 1


def test_wait_tick_returns_on_event_and_coalesces():
    cfg = Config()
    cfg.min_tick_secs = 0.02
    bot = Bot.__new__(Bot)
    bot.cfg = cfg
    bot._wake = asyncio.Event()
    bot._last_tick = 0.0

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.call_later(0.01, bot._wake.set)
        t0 = time.monotonic()
        await bot._wait_tick(5.0)           # would be 5s without the event
        first = time.monotonic() - t0
        bot._wake.set()                      # already set → returns after the coalescing gap only
        t1 = time.monotonic()
        await bot._wait_tick(5.0)
        second = time.monotonic() - t1
        return first, second, bot._wake.is_set()

    first, second, still_set = asyncio.run(scenario())
    assert first < 1.0
    assert 0.015 <= second < 1.0
    assert not still_set


# ------------------------------------------------------------------ live trader

class _FakeClob:
    """Just enough of ClobClient for the maker paths, with knobs."""

    def __init__(self):
        self.created = 0
        self.posted = []
        self.cancelled = []
        self.post_error = None
        self.order_state = {}  # id -> (status, matched)
        self.slow = 0.0

    def create_order(self, args, options=None):
        self.created += 1
        return ("signed", args.token_id, args.price, args.size)

    def post_order(self, signed, order_type=None, post_only=False):
        time.sleep(self.slow)
        if self.post_error:
            raise Exception(self.post_error)
        oid = f"0x{len(self.posted) + 1}"
        self.posted.append((signed, oid))
        self.order_state[oid] = ("LIVE", 0.0)
        return {"success": True, "orderID": oid}

    def cancel_orders(self, ids):
        time.sleep(self.slow)
        self.cancelled.extend(ids)
        for oid in ids:
            st, m = self.order_state.get(oid, ("LIVE", 0.0))
            if st == "LIVE":
                self.order_state[oid] = ("CANCELLED", m)

    def get_order(self, oid):
        st, m = self.order_state[oid]
        return {"status": st, "size_matched": str(m)}


def _trader(fake):
    from pm5.live import LiveTrader

    cfg = Config()
    cfg.presign = True
    t = LiveTrader.__new__(LiveTrader)
    t.cfg = cfg
    t.client = fake
    t.user_stream = None
    t._low_balance_until = 0.0
    t._last_http_poll = {}
    t._pool = ThreadPoolExecutor(max_workers=2)
    t._presigned = {}
    t._presign_lock = threading.Lock()
    t.wake = None
    t.CANCEL_CONFIRM_SECS = 0.0
    return t


def _settle(order, timeout=2.0):
    """Wait for the order's background calls (a reject is a normal outcome)."""
    for fut in (order.pending, order.cancel_future):
        if fut is not None:
            try:
                fut.result(timeout=timeout)
            except Exception:  # noqa: BLE001
                pass


def test_place_bid_is_off_thread_and_resolves_on_poll():
    fake = _FakeClob()
    fake.slow = 0.05
    t = _trader(fake)
    t0 = time.monotonic()
    order = t.place_bid("UP", "up", 0.46, 5.0, 0.01, False)
    assert time.monotonic() - t0 < 0.04          # did not wait for the round trip
    assert order.pending is not None and order.order_id is None and order.live
    assert t.poll_bid(order) is None              # still in flight
    _settle(order)
    assert t.poll_bid(order) is None
    assert order.order_id == "0x1" and order.pending is None and not order.failed


def test_rejected_placement_marks_failed_so_the_maker_reposts():
    fake = _FakeClob()
    fake.post_error = "invalid post-only order: order crosses book"
    t = _trader(fake)
    order = t.place_bid("UP", "up", 0.46, 5.0, 0.01, False)
    _settle(order)
    t.poll_bid(order)
    assert order.failed and order.done and order.order_id is None


def test_async_cancel_still_harvests_a_fill_that_landed_meanwhile():
    """11:25 ET lesson, async edition: the cancel returns immediately, and
    the fill that hit as we yanked is picked up by the next poll."""
    fake = _FakeClob()
    t = _trader(fake)
    order = t.place_bid("UP", "up", 0.46, 5.0, 0.01, False)
    _settle(order)
    t.poll_bid(order)
    fake.slow = 0.05
    t0 = time.monotonic()
    assert t.cancel_bid(order) is None
    assert time.monotonic() - t0 < 0.04
    assert order.cancelling and not order.done and not order.live
    assert t.cancel_bid(order) is None            # second pull is a no-op
    _settle(order)
    assert fake.cancelled == ["0x1"]
    # The bid matched in full just before the cancel reached the book.
    fake.order_state["0x1"] = ("MATCHED", 5.0)
    fill = t.poll_bid(order)
    assert fill is not None and fill.size == 5.0 and fill.maker
    assert order.done and order.filled == 5.0
    assert t.poll_bid(order) is None


def test_cancel_wait_blocks_until_confirmed():
    fake = _FakeClob()
    t = _trader(fake)
    order = t.place_bid("UP", "up", 0.46, 5.0, 0.01, False)
    _settle(order)
    t.poll_bid(order)
    fake.order_state["0x1"] = ("LIVE", 2.0)      # partially hit before the pull
    fill = t.cancel_bid(order, wait=True)
    assert fake.cancelled == ["0x1"] and order.done
    assert fill is not None and fill.size == 2.0


def test_cancel_of_a_pending_placement_waits_for_the_id():
    fake = _FakeClob()
    fake.slow = 0.05
    t = _trader(fake)
    order = t.place_bid("UP", "up", 0.46, 5.0, 0.01, False)
    t.cancel_bid(order, wait=True)
    assert order.order_id == "0x1" and fake.cancelled == ["0x1"] and order.done


def test_presigned_bid_skips_signing_at_post_time():
    fake = _FakeClob()
    t = _trader(fake)
    t.presign("UP", "up", 5.0, [0.44, 0.45, 0.46], 0.01, False)
    for _ in range(50):
        with t._presign_lock:
            if len(t._presigned) == 3:
                break
        time.sleep(0.01)
    assert fake.created == 3
    order = t.place_bid("UP", "up", 0.45, 5.0, 0.01, False)
    _settle(order)
    assert fake.created == 3                      # reused the signed order
    assert fake.posted[-1][0] == ("signed", "UP", 0.45, 5.0)
    with t._presign_lock:
        assert len(t._presigned) == 2             # consumed once, never reused
    order = t.place_bid("UP", "up", 0.50, 5.0, 0.01, False)
    _settle(order)
    assert fake.created == 4                      # not pre-signed → signed inline
    t.forget_presigned(["UP"])
    with t._presign_lock:
        assert not t._presigned


def test_quote_grid_covers_the_fair_band():
    cfg = Config()
    cfg.maker_bid = 0.46
    cfg.maker_skew_max = 0.06
    cfg.maker_stake_usdc = 2.0
    shares, prices = MakerPair.quote_grid(cfg, _mk())
    assert shares == 5                            # min order size beats $2/0.46
    assert prices[0] == 0.40 and prices[-1] == 0.52 and len(prices) == 13


# ------------------------------------------------------------------ maker + async cancel

def test_maker_does_not_repull_or_double_post_while_a_cancel_is_in_flight():
    cfg = Config()
    cfg.mode = "paper"
    cfg.paper_bankroll = 0.0
    cfg.maker_enabled = True
    cfg.maker_bid = 0.46
    cfg.maker_stake_usdc = 2.0
    cfg.maker_start_secs = 3
    cfg.maker_cancel_left_secs = 75
    cfg.maker_fair = True
    ex = Executor(cfg, reader=None)
    m = _mk()
    mk = MakerPair(cfg, ex, m)
    cancels = []

    def slow_cancel(order, wait=False):
        # Live-style: the cancel goes out, confirmation comes on a later poll.
        if order.done:
            return None
        cancels.append(order.side)
        order.cancelling = True
        return None

    def confirming_poll(order, top=None, force=False):
        if order.cancelling and confirm["now"]:
            order.done = True
        return None

    confirm = {"now": False}
    ex.cancel_bid = slow_cancel
    ex.poll_bid = confirming_poll
    now = [1000.0]
    mk._clock = lambda: now[0]

    mk.step(_book(0.52), _book(0.52), sigma=150.0, fast_delta=0.0)
    assert set(mk.orders) == {"up", "down"}
    # Book leans against Up hard enough to pull it (no crossing fill in paper).
    mk.step(_book(0.47), _book(0.55), sigma=150.0, fast_delta=0.0)
    assert cancels == ["up"] and mk.orders["up"].cancelling
    # While the cancel is unconfirmed: no second pull, no re-post of Up.
    up_before = mk.orders["up"]
    mk.step(_book(0.47), _book(0.55), sigma=150.0, fast_delta=0.0)
    mk.step(_book(0.47), _book(0.55), sigma=150.0, fast_delta=0.0)
    assert cancels == ["up"] and mk.orders["up"] is up_before
    # Confirmation arrives, fair is back and the pull cooldown has passed:
    # Up is posted again.
    confirm["now"] = True
    now[0] += cfg.maker_repost_secs + 0.1
    mk.step(_book(0.52), _book(0.52), sigma=150.0, fast_delta=0.0)
    mk.step(_book(0.52), _book(0.52), sigma=150.0, fast_delta=0.0)
    assert mk.orders["up"] is not up_before and mk.orders["up"].live


def test_maker_cools_down_after_a_pull_instead_of_reposting_on_the_next_print():
    """Paper 10:31: at 37 Hz a $2 flicker swung fair 0.55↔0.59 and the maker
    cycled pull-pair → re-post → pull-pair twice a second for 30s."""
    cfg = Config()
    cfg.mode = "paper"
    cfg.paper_bankroll = 0.0
    cfg.maker_enabled = True
    cfg.maker_bid = 0.46
    cfg.maker_stake_usdc = 2.0
    cfg.maker_start_secs = 3
    cfg.maker_cancel_left_secs = 75
    cfg.maker_fair = True
    cfg.maker_repost_secs = 2.0
    ex = Executor(cfg, reader=None)
    mk = MakerPair(cfg, ex, _mk())
    now = [1000.0]
    mk._clock = lambda: now[0]
    posts = []
    real_place = ex.place_bid

    def counting_place(*a, **kw):
        posts.append(a[1])
        return real_place(*a, **kw)

    ex.place_bid = counting_place

    mk.step(_book(0.52), _book(0.52), sigma=30.0, fast_delta=0.0)
    assert sorted(posts) == ["down", "up"]
    # Δ flickers to +7: Down unquotable → pull the pair.
    mk.step(_book(0.52), _book(0.52), sigma=30.0, fast_delta=7.0)
    assert not mk._live_orders() and mk._pull_hold == {"up": 1000.0, "down": 1000.0}
    # Next prints flicker back to flat: no re-post inside the cooldown...
    for _ in range(5):
        now[0] += 0.1
        mk.step(_book(0.52), _book(0.52), sigma=30.0, fast_delta=0.0)
    assert len(posts) == 2 and "cooling down" in (mk._skip or "")
    # ...and a fresh pair once it has passed.
    now[0] += 2.0
    mk.step(_book(0.52), _book(0.52), sigma=30.0, fast_delta=0.0)
    assert len(posts) == 4 and len(mk._live_orders()) == 2


def test_maker_reposts_after_a_failed_placement():
    cfg = Config()
    cfg.mode = "paper"
    cfg.paper_bankroll = 0.0
    cfg.maker_enabled = True
    cfg.maker_bid = 0.46
    cfg.maker_stake_usdc = 2.0
    cfg.maker_start_secs = 3
    cfg.maker_cancel_left_secs = 75
    cfg.maker_fair = True
    ex = Executor(cfg, reader=None)
    mk = MakerPair(cfg, ex, _mk())
    mk.step(_book(0.52), _book(0.52), sigma=150.0, fast_delta=0.0)
    first = mk.orders["up"]
    assert mk.posted
    # Off-thread placement came back rejected.
    first.failed = True
    first.done = True
    mk.step(_book(0.52), _book(0.52), sigma=150.0, fast_delta=0.0)
    mk.step(_book(0.52), _book(0.52), sigma=150.0, fast_delta=0.0)
    assert mk.orders["up"] is not first and mk.orders["up"].live


def test_parse_clob_usdc_splits_total_from_locked_bids():
    # 11:05 live: site Cash $12, CLOB total $3.63 with $2.25 already reserved.
    text = (
        "not enough balance / allowance: the balance is not enough -> "
        "balance: 3626057, sum of active orders: 2250000, "
        "sum of matched orders: 0, order amount (inc. fees): 2200000"
    )
    total, locked = parse_clob_usdc(text)
    assert total == pytest.approx(3.626057)
    assert locked == pytest.approx(2.25)
    assert (total - locked) == pytest.approx(1.376057)


def test_maker_pulls_the_lone_leg_when_the_other_cannot_be_funded():
    """11:05: one bid reserved the cash, the other was refused, the lone
    rest sat on the book. Pull it — a pair we cannot fund is directional."""
    cfg = Config()
    cfg.mode = "paper"
    cfg.paper_bankroll = 0.0
    cfg.maker_enabled = True
    cfg.maker_bid = 0.46
    cfg.maker_stake_usdc = 2.0
    cfg.maker_start_secs = 3
    cfg.maker_cancel_left_secs = 75
    cfg.maker_fair = True
    ex = Executor(cfg, reader=None)
    mk = MakerPair(cfg, ex, _mk())
    mk.step(_book(0.52), _book(0.52), sigma=150.0, fast_delta=0.0)
    assert len(mk._live_orders()) == 2
    mk.orders["up"].done = True
    ex._live = SimpleNamespace(funds_tight=True)
    mk.step(_book(0.52), _book(0.52), sigma=150.0, fast_delta=0.0)
    assert mk._live_orders() == [] and not mk.fills
    assert "locked" in (mk._skip or "")
    assert ex._live.funds_tight is False
