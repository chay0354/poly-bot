"""Offline tests: fair-value quoting, depth-walking exits, CLOB WebSocket
ingestion, and the persisted daily ledger."""

import json
import time

from pm5.clob import BookReader, BookTop, Executor, Fill
from pm5.clobws import MarketStream, UserStream
from pm5.config import Config
from pm5.ledger import DayLedger
from pm5.maker import MakerPair, fair_up
from pm5.markets import Market, current_window_start, slug_for


def _mk(seconds_in=20) -> Market:
    now = time.time()
    start = now - seconds_in
    return Market(
        slug=slug_for(current_window_start()), condition_id="0x", question="q",
        up_token="UP", down_token="DOWN", tick_size=0.01, min_size=5,
        neg_risk=False, window_start=int(start), window_end=int(start + 300),
    )


def _book(ask, bid=None, ask_size=100.0, bids=None):
    top = BookTop(bid if bid is not None else round(ask - 0.02, 2), 50.0, ask, ask_size)
    if bids is not None:
        top.bids = bids
        top.best_bid, top.best_bid_size = bids[0]
    return top


def _maker(seconds_in=20, **cfg_over):
    cfg = Config()
    cfg.mode = "paper"
    cfg.paper_bankroll = 0.0
    cfg.maker_enabled = True
    cfg.maker_bid = 0.46
    cfg.maker_stake_usdc = 2.0
    cfg.maker_start_secs = 3
    cfg.maker_cancel_left_secs = 75
    cfg.maker_fair = True
    for k, v in cfg_over.items():
        setattr(cfg, k, v)
    ex = Executor(cfg, reader=None)
    m = _mk(seconds_in=seconds_in)
    return cfg, ex, m, MakerPair(cfg, ex, m)


# ------------------------------------------------------------------ fair value

def test_fair_up_reads_the_tape_not_a_fixed_line():
    # Same $10 move: decisive on a quiet tape, noise on a fast one.
    quiet = fair_up(10.0, 60.0, 280.0)
    fast = fair_up(10.0, 300.0, 280.0)
    assert quiet > 0.60 and 0.52 < fast < 0.56
    assert abs(fair_up(0.0, 100.0, 280.0) - 0.5) < 1e-9
    # Symmetric and monotone.
    assert abs(fair_up(-10.0, 60.0, 280.0) - (1 - quiet)) < 1e-9
    assert fair_up(20.0, 60.0, 280.0) > quiet
    # Late in the window the same move is more decisive (less time to revert).
    assert fair_up(10.0, 100.0, 60.0) > fair_up(10.0, 100.0, 280.0)


def test_maker_sits_out_unwitnessed_window_until_a_delta_exists():
    # Bot started mid-window: Chainlink never saw the open and Binance history
    # does not reach back to it either. Resting 0.46 blind into a 0.65/0.36
    # book is the adverse fill we lost to after every restart.
    cfg, ex, m, _ = _maker(seconds_in=70)
    mk = MakerPair(cfg, ex, m, witnessed=False)
    mk.step(_book(0.52), _book(0.52), btc=None, open_price=None, sigma=150.0, fast_delta=None)
    assert not mk.orders and not mk.posted
    assert "open not witnessed" in (mk._skip or "")

    # Once Binance can give a Δ vs the open we quote normally.
    mk.step(_book(0.52), _book(0.52), btc=None, open_price=None, sigma=150.0, fast_delta=0.0)
    assert mk.posted and set(mk.orders) == {"up", "down"}

    # A witnessed window with a Chainlink Δ never hits the gate.
    cfg, ex, m, mk = _maker(seconds_in=70)
    mk.step(_book(0.52), _book(0.52), btc=50000.0, open_price=50000.0, sigma=150.0)
    assert mk.posted


def test_maker_quotes_at_fair_minus_edge_and_skews():
    cfg, ex, m, mk = _maker()
    # Flat tape: Δ=0, sigma known → fair 0.50/0.50 → 0.46 both sides.
    mk.step(_book(0.52), _book(0.52), sigma=150.0, fast_delta=0.0)
    assert mk.orders["up"].price == 0.46 and mk.orders["down"].price == 0.46
    assert mk.posted

    # New window, BTC already +$12 on a $150 tape → fair up ≈ 0.55.
    cfg, ex, m, mk = _maker()
    mk.step(_book(0.58), _book(0.48), sigma=150.0, fast_delta=12.0)
    up, dn = mk.orders["up"].price, mk.orders["down"].price
    assert up > 0.46 > dn                      # skewed toward the winning side
    assert up <= cfg.maker_bid + cfg.maker_skew_max
    assert dn >= cfg.maker_bid - cfg.maker_skew_max
    assert round(up + dn, 2) <= round(1.0 - 2 * cfg.maker_edge, 2) + 0.011  # pair ≤ ~0.92


def test_maker_pulls_bid_when_fair_moves_against_it():
    cfg, ex, m, mk = _maker()
    mk.step(_book(0.52), _book(0.52), sigma=150.0, fast_delta=0.0)
    assert not mk.orders["up"].done and not mk.orders["down"].done
    # BTC drops $8: fair up ≈ 0.47 → the 0.46 Up bid has < 0.02 edge → pulled.
    # Down (fair 0.53) is now 0.07 above its bid → re-quoted higher.
    now = [1000.0]
    mk._clock = lambda: now[0]
    mk.step(_book(0.50), _book(0.54), sigma=150.0, fast_delta=-8.0)
    assert mk.orders["up"].done
    assert mk.requotes >= 1
    # Next tick the re-quoted Down is straight back on the book; the pulled
    # Up waits out the re-post cooldown, then returns at its fair-based price.
    mk.step(_book(0.50), _book(0.54), sigma=150.0, fast_delta=-8.0)
    assert mk.orders["up"].done and not mk.orders["down"].done
    assert "cooling down" in (mk._skip or "")
    now[0] += cfg.maker_repost_secs + 0.1
    mk.step(_book(0.50), _book(0.54), sigma=150.0, fast_delta=-8.0)
    assert not mk.orders["up"].done and not mk.orders["down"].done
    assert mk.orders["up"].price < 0.46 <= mk.orders["down"].price
    assert not mk.fills


def test_maker_unquotable_side_pulls_pair_without_standing_down():
    cfg, ex, m, mk = _maker()
    now = [1000.0]
    mk._clock = lambda: now[0]
    mk.step(_book(0.52), _book(0.52), sigma=60.0, fast_delta=0.0)
    assert len(mk._live_orders()) == 2
    # Quiet tape, Binance +$15 while the book still shows 0.52/0.52 (Binance
    # leads): fair up ≈ 0.67 → Down target 0.29 < 0.40 → no pair possible.
    mk.step(_book(0.52), _book(0.52), sigma=60.0, fast_delta=15.0)
    assert mk._live_orders() == [] and not mk.fills
    assert not mk._stood_down and not mk._blocked
    assert "unquotable" in (mk._skip or "")
    # Book catches up; still nothing while the move holds.
    now[0] += 5.0
    mk.step(_book(0.72), _book(0.30), sigma=60.0, fast_delta=15.0)
    assert mk._live_orders() == [] and not mk.fills
    # Move fades: quote the full pair again, no window lost.
    mk.step(_book(0.52), _book(0.52), sigma=60.0, fast_delta=1.0)
    assert len(mk._live_orders()) == 2


def test_maker_trusts_a_leaning_book_over_a_flat_model():
    """09:05 live: Δ≈0 so the model said 0.50/0.50 while the book asked 0.41
    for Up. We bid 0.40 under it, got hit, sold at 0.36. The book's asks bound
    p (1 − ask_other ≤ p ≤ ask_side); the more pessimistic estimate wins."""
    cfg, ex, m, mk = _maker()
    mk.step(_book(0.41), _book(0.60), sigma=150.0, fast_delta=0.0)
    assert mk._live_orders() == [] and not mk.fills
    assert "unquotable" in (mk._skip or "")
    assert abs(mk._fair_side("up") - 0.405) < 1e-9   # (0.41 + 0.40) / 2
    # A balanced book leaves the model alone.
    cfg, ex, m, mk = _maker()
    mk.step(_book(0.52), _book(0.52), sigma=150.0, fast_delta=0.0)
    assert abs(mk._fair_side("up") - 0.5) < 1e-9
    assert len(mk._live_orders()) == 2
    # Book leans against a resting side (asks 0.47 / 0.55 → p_up 0.46, no edge
    # left on the 0.46 bid) → that bid is pulled before anyone hits it.
    mk.step(_book(0.47), _book(0.55), sigma=150.0, fast_delta=0.0)
    assert mk.orders["up"].done and not mk.fills


def test_maker_does_not_thrash_requotes_under_a_low_ask():
    """09:05 live: target 0.46, ask 0.41 → posted 0.40, then 'requote 0.40 →
    0.46' cancelled and re-posted 0.40 fifteen times. The requote test must
    use the price we can actually post."""
    cfg, ex, m, mk = _maker()
    now = {"t": 1000.0}
    mk._clock = lambda: now["t"]
    # Wide but balanced book: asks 0.45 / 0.57 → book p_up = (0.45+0.43)/2 = 0.44,
    # fair up 0.44 → target 0.40, stepped under the 0.45 ask stays 0.40.
    mk.step(_book(0.45), _book(0.57), sigma=150.0, fast_delta=0.0)
    up = mk.orders["up"]
    assert not up.done and up.price == 0.40
    for _ in range(5):
        now["t"] += 1.5
        mk.step(_book(0.45), _book(0.57), sigma=150.0, fast_delta=0.0)
    assert mk.orders["up"] is up and not up.done
    assert mk.requotes == 0


def test_maker_falls_back_to_dollar_line_without_sigma():
    cfg, ex, m, mk = _maker()
    cfg.maker_defensive_usd = 20
    # No sigma → no fair → old behaviour: +$25 is toxic, nothing rests.
    mk.step(_book(0.52), _book(0.52), sigma=None, fast_delta=25.0)
    assert mk._live_orders() == []
    assert "Δopen" in (mk._skip or "") and "unquotable" not in mk._skip


def test_exit_leg_carries_a_walkable_floor():
    cfg, ex, m, mk = _maker()
    cfg.maker_exit_secs = 30
    cfg.maker_stop_ticks = 0.04
    cfg.maker_exit_slip = 0.03
    now = {"t": 1000.0}
    mk._clock = lambda: now["t"]
    mk.step(_book(0.52), _book(0.52))
    mk.step(_book(0.46), _book(0.56))  # Up hit @ 0.46
    now["t"] += 31
    sig = mk.exit_signal(_book(0.44, bid=0.42), _book(0.62))
    assert sig is not None and sig.kind == "maker-exit"
    assert sig.legs[0].max_price == 0.42
    assert sig.legs[0].min_price == 0.39  # may walk 3 ticks under the top bid


# ---------------------------------------------------------------- depth sells

def test_sell_walks_depth_instead_of_refusing_thin_top():
    """21:10 / 01:15 UTC: top bid 3 sh < 10.87 → 'bid size 3.00 < 10.87',
    leg held to a $0 resolution. Now the order walks down the book."""
    cfg = Config()
    cfg.mode = "paper"
    cfg.paper_bankroll = 0.0
    cfg.taker_fee_rate = 0.0
    ex = Executor(cfg, reader=None)
    top = _book(0.60, bids=[(0.42, 3.0), (0.41, 4.0), (0.40, 50.0)])
    fill = ex.sell("UP", "up", 10.87, min_price=0.39, top=top)
    assert fill is not None and fill.size == -10.87
    assert fill.price == 0.40  # worst level reached
    assert ex.last_skip is None


def test_sell_partial_when_depth_above_floor_is_short():
    cfg = Config()
    cfg.mode = "paper"
    cfg.paper_bankroll = 0.0
    ex = Executor(cfg, reader=None)
    top = _book(0.60, bids=[(0.42, 3.0), (0.41, 4.0), (0.20, 100.0)])
    fill = ex.sell("UP", "up", 10.87, min_price=0.39, top=top)
    assert fill is not None and fill.size == -7.0 and fill.price == 0.41


def test_sell_still_respects_floor():
    cfg = Config()
    cfg.mode = "paper"
    cfg.paper_bankroll = 0.0
    ex = Executor(cfg, reader=None)
    assert ex.sell("UP", "up", 5.0, min_price=0.50, top=_book(0.60, bid=0.42)) is None
    assert "floor" in ex.last_skip


def test_sell_plan_without_depth_uses_top():
    top = BookTop(0.42, 3.0, 0.60, 5.0)
    assert top.sell_plan(10.0, 0.10) == (0.42, 3.0)
    assert top.sell_plan(2.0, 0.10) == (0.42, 2.0)


# ------------------------------------------------------------ market stream

def _book_event(token, bids, asks):
    return json.dumps({
        "event_type": "book", "asset_id": token, "market": "0x", "timestamp": "1",
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
    })


def test_market_stream_book_and_deltas_feed_the_reader():
    ms = MarketStream()
    ms.watch(["UP", "DOWN"])
    ms._subscribe_frames()  # what run() would send
    ms._connected = True
    ms._last_msg = time.monotonic()
    reader = BookReader("https://clob", client=None, stream=ms)
    reader._client = None  # any HTTP attempt would blow up: we must not need it

    # REST/WS convention: bids ascending, asks descending; we never rely on it.
    ms.ingest(_book_event("UP", [(0.44, 10), (0.46, 20)], [(0.55, 5), (0.52, 8)]))
    top = reader.top("UP")
    assert top.source == "ws"
    assert (top.best_bid, top.best_bid_size) == (0.46, 20)
    assert (top.best_ask, top.best_ask_size) == (0.52, 8)
    assert top.bids == [(0.46, 20), (0.44, 10)] and top.asks == [(0.52, 8), (0.55, 5)]

    # Level updates: size 0 removes, otherwise replaces.
    ms.ingest(json.dumps({
        "event_type": "price_change", "market": "0x", "timestamp": "2",
        "price_changes": [
            {"asset_id": "UP", "price": "0.46", "size": "0", "side": "BUY"},
            {"asset_id": "UP", "price": "0.51", "size": "3", "side": "SELL"},
            {"asset_id": "DOWN", "price": "0.50", "size": "9", "side": "BUY"},  # no snapshot yet
        ],
    }))
    top = reader.top("UP")
    assert top.best_bid == 0.44 and top.best_ask == 0.51
    assert ms.book("DOWN") is None  # deltas without a snapshot are not a book
    assert reader.ws_reads == 2 and reader.http_reads == 0


def test_market_stream_unhealthy_or_stale_yields_no_book():
    ms = MarketStream(stale_secs=0.05)
    ms.watch(["UP"])
    ms._subscribe_frames()
    ms._connected = True
    ms._last_msg = time.monotonic()
    ms.ingest(_book_event("UP", [(0.46, 1)], [(0.52, 1)]))
    assert ms.book("UP") is not None
    time.sleep(0.06)
    assert ms.book("UP") is None  # stale
    ms._last_msg = time.monotonic()
    ms.ingest(_book_event("UP", [(0.46, 1)], [(0.52, 1)]))
    ms._connected = False
    assert ms.book("UP") is None  # disconnected


def test_market_stream_watch_reconnects_only_for_new_tokens():
    ms = MarketStream()
    ms.watch(["A", "B"])
    ms._subscribe_frames()
    ms._reconnect.clear()
    ms.watch(["A"])           # subset: keep the connection
    assert not ms._reconnect.is_set()
    ms.watch(["A", "C"])      # new token: needs a fresh subscription
    assert ms._reconnect.is_set()


def test_user_stream_tracks_matched_size_and_status():
    us = UserStream("k", "s", "p")
    us._connected = True
    us._last_msg = time.monotonic()
    us.ingest(json.dumps({"event_type": "order", "id": "o1", "size_matched": "0",
                          "status": "LIVE", "type": "PLACEMENT"}))
    us.ingest(json.dumps({"event_type": "order", "id": "o1", "size_matched": "5",
                          "status": "MATCHED", "type": "UPDATE"}))
    st = us.state("o1")
    assert st["matched"] == 5.0 and st["status"] == "MATCHED"
    # Out-of-order stale event never lowers matched.
    us.ingest(json.dumps({"event_type": "order", "id": "o1", "size_matched": "2",
                          "status": "LIVE", "type": "UPDATE"}))
    assert us.state("o1")["matched"] == 5.0
    assert us.state("nope") is None
    us._connected = False
    assert us.state("o1") is None


# ---------------------------------------------------------------- day ledger

def test_day_ledger_persists_and_rolls(tmp_path, monkeypatch):
    path = tmp_path / "day.json"
    led = DayLedger(str(path))
    led.add(-1.5)
    led.add(-2.0)
    assert round(led.today(), 2) == -3.5
    # A restart the same UTC day picks the total back up.
    again = DayLedger(str(path))
    assert round(again.today(), 2) == -3.5 and again.windows == 2
    # A new UTC day starts from zero.
    monkeypatch.setattr("pm5.ledger.utc_today", lambda: "2099-01-01")
    assert again.today() == 0.0
    assert json.loads(path.read_text())["date"] == "2099-01-01"


def test_loss_limit_uses_persisted_ledger(tmp_path):
    from pm5.bot import Bot

    cfg = Config()
    cfg.mode = "live"
    cfg.daily_loss_limit_usdc = 5.0
    bot = Bot.__new__(Bot)
    bot.cfg = cfg
    bot.ledger = DayLedger(str(tmp_path / "day.json"))
    assert not bot._loss_limit_hit()
    bot.ledger.add(-5.0)
    assert bot._loss_limit_hit()
    # Fresh process, same file, same day: still stopped.
    bot2 = Bot.__new__(Bot)
    bot2.cfg = cfg
    bot2.ledger = DayLedger(str(tmp_path / "day.json"))
    assert bot2._loss_limit_hit()


def test_live_window_estimate_feeds_the_ledger():
    from pm5.bot import Bot, Position
    from pm5.pricefeed import Tick

    cfg = Config()
    cfg.mode = "live"
    bot = Bot.__new__(Bot)
    bot.cfg = cfg
    bot.session_pnl = 0.0
    bot.ledger = DayLedger(None)
    bot.recorder = type("R", (), {"enabled": False})()
    bot.executor = type("E", (), {"bankroll": None})()
    bot.feed = type("F", (), {"latest": Tick(price=99.0, src_ts=0, recv_ts=0),
                              "twap": lambda self, a, b: 99.0})()
    m = _mk()
    # Naked Up leg held to a Down resolution: −cost.
    pos = Position()
    pos.add(Fill("UP", "up", 0.46, 5.0, 2.30, paper=False, maker=True))
    bot._settle_window(m, pos, open_price=100.0, witnessed=True)
    assert round(bot.ledger.today(), 2) == -2.30
    # A locked pair: +$1/sh − cost, whichever way it resolves.
    pos = Position()
    pos.add(Fill("UP", "up", 0.46, 5.0, 2.30, paper=False, maker=True))
    pos.add(Fill("DOWN", "down", 0.46, 5.0, 2.30, paper=False, maker=True))
    bot._settle_window(m, pos, open_price=100.0, witnessed=True)
    assert round(bot.ledger.today(), 2) == round(-2.30 + (5.0 - 4.60), 2)
