"""Offline tests for the jump detector, the jump study and the sniper."""

import asyncio
import json
import time

from pm5.clob import BookTop, Executor
from pm5.config import Config
from pm5.fastfeed import BinanceFeed, CoinbaseFeed, FastFeeds
from pm5.jumps import JumpWatch, jump_threshold, sigma_window
from pm5.markets import Market, current_window_start, slug_for
from pm5.pricefeed import Tick
from pm5.sniper import Sniper


def _mk(seconds_left=150) -> Market:
    now = time.time()
    end = now + seconds_left
    return Market(
        slug=slug_for(current_window_start()), condition_id="0x", question="q",
        up_token="UP", down_token="DOWN", tick_size=0.01, min_size=5,
        neg_risk=False, window_start=int(end - 300), window_end=int(end),
    )


def _book(ask, bid=None, ask_size=100.0, asks=None):
    top = BookTop(bid if bid is not None else round(ask - 0.02, 2), 50.0, ask, ask_size)
    if asks is not None:
        top.asks = asks
    return top


def _feed(name="binance", prices=(), now=None):
    """prices: list of (seconds_ago, price) → a feed whose history is those prints."""
    f = BinanceFeed("wss://x") if name == "binance" else CoinbaseFeed("wss://x")
    now = now or time.time()
    for ago, p in sorted(prices, key=lambda x: -x[0]):
        f._history.append(Tick(price=p, src_ts=now - ago, recv_ts=now - ago))
    f.latest = f._history[-1] if f._history else None
    return f


def _cfg(**kw) -> Config:
    cfg = Config()
    cfg.mode = "paper"
    cfg.paper_bankroll = 0.0
    cfg.taker_fee_rate = 0.07
    cfg.snipe_enabled = True
    cfg.snipe_shares = 5
    cfg.snipe_min_edge = 0.06
    cfg.snipe_max_age_ms = 800
    cfg.sim_latency_ms = 150
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


# ------------------------------------------------------------------ jump()

def test_jump_measures_the_move_over_our_clock_window():
    f = _feed(prices=[(5.0, 78000.0), (1.5, 78002.0), (0.4, 78010.0), (0.1, 78025.0)])
    j = f.jump(1.0)
    # Reference = last print at or before 1s ago (78002 @ −1.5s).
    assert j is not None and j.venue == "binance"
    assert abs(j.delta - 23.0) < 1e-9 and j.start_price == 78002.0 and j.end_price == 78025.0
    assert j.age < 0.5
    # History does not reach back a full window → honest None.
    g = _feed(prices=[(0.4, 78010.0), (0.1, 78025.0)])
    assert g.jump(1.0) is None
    # Stale stream → None.
    h = _feed(prices=[(30.0, 78000.0), (20.0, 78050.0)])
    assert h.jump(1.0) is None


def test_fastfeeds_jump_uses_the_venue_that_printed_last():
    b = _feed("binance", [(3.0, 78000.0), (0.5, 78000.0)])  # quiet
    c = _feed("coinbase", [(3.0, 78000.0), (0.05, 78030.0)])  # just moved
    ff = FastFeeds([b, c])
    j = ff.jump(1.0)
    assert j is not None and j.venue == "coinbase" and abs(j.delta - 30.0) < 1e-9


def test_jump_threshold_scales_with_the_tape():
    # σ_1s from a $120 5-min range: 120/1.6/√300 ≈ 4.33 → 3σ ≈ 13
    assert abs(sigma_window(120.0, 1.0) - 4.330) < 0.01
    assert abs(jump_threshold(120.0, 1.0, 3.0, 10.0) - 12.99) < 0.01
    # Quiet tape: the dollar floor holds.
    assert jump_threshold(30.0, 1.0, 3.0, 10.0) == 10.0
    # No σ yet: dollar floor.
    assert jump_threshold(None, 1.0, 3.0, 10.0) == 10.0


# ------------------------------------------------------------- JumpWatch

def test_jumpwatch_records_stale_quote_lifetime(tmp_path):
    m = _mk(seconds_left=150)
    now = {"t": 1000.0}
    w = JumpWatch(m, str(tmp_path / "jumps.jsonl"), 1.0, 3.0, 10.0, min_size=5,
                  clock=lambda: now["t"])
    b = _feed(prices=[(2.0, 78000.0), (0.05, 78030.0)])
    j = b.jump(1.0)
    up, dn = _book(0.52, asks=[(0.52, 20.0), (0.55, 50.0)]), _book(0.52)
    # +$30 with σ_5m=120, Δopen=+30 → Up is the stale side; fair up ≈ Φ(30/...)
    rec = w.observe(j, 120.0, 30.0, up, dn)
    assert rec is not None and rec.stale_side == "up" and rec.ask0 == 0.52
    assert rec.avail0 == 20.0 and rec.fair_after > rec.fair_before
    assert rec.edge0 is not None and abs(rec.edge0 - (rec.fair_after - 0.52)) < 1e-9
    # Same jump next tick: re-arm window, no second record.
    now["t"] += 0.1
    assert w.observe(j, 120.0, 30.0, up, dn) is None
    assert rec.gone_ms is None
    # 180ms later the 0.52 level is down to 3 shares: a 5-share take no longer fits.
    now["t"] += 0.08
    thin = _book(0.52, asks=[(0.52, 3.0), (0.55, 50.0)])
    w.observe(None, 120.0, 30.0, thin, dn)
    assert rec.gone_ms == 180.0
    # 1s / 3s marks and the outcome at settle; one JSON line written.
    now["t"] += 1.0
    w.observe(None, 120.0, 30.0, _book(0.58), dn)
    assert rec.ask_1s == 0.58
    now["t"] += 2.0
    w.observe(None, 120.0, 30.0, _book(0.60), dn)
    assert rec.ask_3s == 0.60 and not rec.open
    assert w.settle(True) == 1
    line = json.loads((tmp_path / "jumps.jsonl").read_text().strip())
    assert line["stale_side"] == "up" and line["gone_ms"] == 180.0 and line["up_won"] is True
    assert "_t0" not in line


def test_jumpwatch_ignores_small_moves_and_censors_survivors():
    m = _mk(seconds_left=150)
    now = {"t": 1000.0}
    w = JumpWatch(m, None, 1.0, 3.0, 10.0, min_size=5, clock=lambda: now["t"])
    small = _feed(prices=[(2.0, 78000.0), (0.05, 78008.0)]).jump(1.0)
    assert w.observe(small, 120.0, 8.0, _book(0.52), _book(0.52)) is None
    big = _feed(prices=[(2.0, 78000.0), (0.05, 77960.0)]).jump(1.0)
    rec = w.observe(big, 120.0, -40.0, _book(0.52), _book(0.52))
    assert rec is not None and rec.stale_side == "down"
    # Quote never moves: after 3s it is recorded as surviving the horizon.
    now["t"] += 3.1
    w.observe(None, 120.0, -40.0, _book(0.52), _book(0.52))
    assert rec.gone_ms == 3000.0 and rec.ask_3s == 0.52


# ---------------------------------------------------------------- Sniper

def _armed(seconds_left=150, delta=30.0, ask=0.52, ask_size=20.0, **cfg_kw):
    cfg = _cfg(**cfg_kw)
    m = _mk(seconds_left)
    ex = Executor(cfg, reader=None)
    now = {"t": 1000.0}
    clk = lambda: now["t"]  # noqa: E731
    w = JumpWatch(m, None, 1.0, 3.0, 10.0, min_size=5, clock=clk)
    sn = Sniper(cfg, ex, m, clock=clk)
    j = _feed(prices=[(2.0, 78000.0), (0.05, 78000.0 + delta)]).jump(1.0)
    up = _book(ask, asks=[(ask, ask_size), (round(ask + 0.03, 2), 50.0)])
    dn = _book(round(1.0 - ask + 0.02, 2))
    rec = w.observe(j, 120.0, delta, up, dn)
    return cfg, ex, m, w, sn, rec, j, up, dn, now


def test_sniper_takes_the_stale_ask_after_a_jump():
    cfg, ex, m, w, sn, rec, j, up, dn, now = _armed()
    assert rec is not None and rec.edge0 >= 0.06
    sig = sn.evaluate(rec, j.age * 1000, up, dn)
    assert sig is not None and sig.kind == "snipe"
    leg = sig.legs[0]
    assert leg.side == "up" and leg.max_price == 0.52 and leg.shares == 5
    pt = sn.shoot(sig, rec)
    assert pt is not None and sn.pending is pt and sn.shots == 1
    # Same jump, second tick: no double shot.
    assert sn.evaluate(rec, j.age * 1000, up, dn) is None


def test_sniper_skips_without_edge_or_size_or_freshness():
    cfg, ex, m, w, sn, rec, j, up, dn, now = _armed()
    # Ask already repriced to fair − 0.02: no edge.
    dear = _book(round(rec.fair_after - 0.02, 2), asks=[(round(rec.fair_after - 0.02, 2), 20.0)])
    assert sn.evaluate(rec, j.age * 1000, dear, dn) is None
    # Only 3 shares offered at the stale price.
    thin = _book(0.52, asks=[(0.52, 3.0), (0.60, 50.0)])
    assert sn.evaluate(rec, j.age * 1000, thin, dn) is None
    # Jump detected 900ms ago: the book has had its chance to reprice.
    now["t"] += 0.9
    assert sn.evaluate(rec, j.age * 1000, up, dn) is None


def test_sniper_respects_window_band_and_no_sigma():
    cfg, ex, m, w, sn, rec, j, up, dn, now = _armed(seconds_left=10)
    assert sn.evaluate(rec, j.age * 1000, up, dn) is None  # < min_left
    cfg, ex, m, w, sn, rec, j, up, dn, now = _armed(seconds_left=290)
    assert sn.evaluate(rec, j.age * 1000, up, dn) is None  # > max_left
    # No σ → fair unknown → no shot even though the jump was recorded.
    cfg = _cfg()
    m = _mk(150)
    w = JumpWatch(m, None, 1.0, 3.0, 10.0, min_size=5)
    j = _feed(prices=[(2.0, 78000.0), (0.05, 78030.0)]).jump(1.0)
    rec = w.observe(j, None, None, _book(0.52), _book(0.52))
    assert rec is not None and rec.fair_after is None
    assert Sniper(cfg, Executor(cfg, reader=None), m).evaluate(rec, 50.0, _book(0.52), _book(0.52)) is None


def test_paper_take_fills_only_if_the_quote_survives_the_round_trip():
    cfg = _cfg(sim_latency_ms=150)
    ex = Executor(cfg, reader=None)
    pt = ex.take("UP", "up", 0.52, 5, 0.01, False)
    assert pt is not None and pt.paper and not pt.done
    # 50ms in: quote still there, order still in flight → nothing yet.
    pt.sent_at -= 0.05
    pt.deadline -= 0.05
    assert ex.poll_take(pt, _book(0.52, asks=[(0.52, 20.0)])) is None and not pt.done
    # Quote eaten before we land: a miss, not a fill.
    assert ex.poll_take(pt, _book(0.55, asks=[(0.55, 20.0)])) is None
    assert pt.done and "quote gone" in pt.reason
    # Survives past the deadline → fills at the stale price, fee in shares.
    pt2 = ex.take("UP", "up", 0.52, 5, 0.01, False)
    pt2.deadline = pt2.sent_at  # landed
    fill = ex.poll_take(pt2, _book(0.52, asks=[(0.52, 20.0)]))
    assert fill is not None and fill.side == "up" and fill.price == 0.52
    assert fill.cost == 2.6 and 4.75 < fill.size < 5.0  # 5 − fee/price
    assert pt2.done and pt2.fill is fill


def test_sniper_poll_books_the_fill_and_scalps_back():
    cfg, ex, m, w, sn, rec, j, up, dn, now = _armed(snipe_scalp=True, snipe_take=0.04)
    sig = sn.evaluate(rec, j.age * 1000, up, dn)
    pt = sn.shoot(sig, rec)
    pt.deadline = pt.sent_at
    fills = sn.poll(up, dn)
    assert len(fills) == 1 and sn.hits == 1 and sn.pending is None and sn.side == "up"
    # Effective entry is 2.60 / 4.83 net shares ≈ 0.538 (the fee is in the
    # price we really paid). Bid 0.56 < 0.538 + 0.04: keep holding.
    assert 0.535 < sn.avg_price() < 0.54
    assert sn.exit_signal(_book(0.58, bid=0.56), dn) is None
    ex_sig = sn.exit_signal(_book(0.60, bid=0.58), dn)
    assert ex_sig is not None and ex_sig.kind == "snipe-exit"
    assert ex_sig.legs[0].side == "up" and ex_sig.legs[0].max_price == 0.58
    assert abs(ex_sig.legs[0].shares - sn.shares) < 1e-9
    # One hit per window: no second shot even on a fresh jump.
    j2 = _feed(prices=[(2.0, 78030.0), (0.05, 78070.0)]).jump(1.0)
    now["t"] += 2.5
    rec2 = w.observe(j2, 120.0, 70.0, _book(0.60, asks=[(0.60, 20.0)]), dn)
    assert rec2 is not None and sn.evaluate(rec2, 50.0, _book(0.60, asks=[(0.60, 20.0)]), dn) is None


def test_sniper_quote_grid_covers_the_price_band():
    cfg = _cfg(snipe_min_price=0.10, snipe_max_price=0.85)
    shares, prices = Sniper.quote_grid(cfg, _mk())
    assert shares == 5 and prices[0] == 0.10 and prices[-1] == 0.85 and len(prices) == 76


def _twap_feed(ticks):
    """ticks: list of (src_ts, value) on the crypto_prices_twap_sixty topic."""
    from pm5.pricefeed import TwapFeed

    f = TwapFeed("wss://x", "x")
    for ts, v in ticks:
        f._ingest(json.dumps({
            "topic": "crypto_prices_twap_sixty", "type": "update",
            "payload": {"symbol": "btc/usd", "value": v, "timestamp": int(ts * 1000), "window_s": 60},
        }))
    return f


def test_twap_feed_reads_its_own_topic_and_steps():
    f = _twap_feed([(1000, 100.0), (1001, 101.0), (1003, 103.0)])
    # Spot frames on the other topic are ignored.
    f._ingest(json.dumps({"topic": "crypto_prices_chainlink",
                          "payload": {"symbol": "btc/usd", "value": 999.0, "timestamp": 1004000}}))
    assert f.latest.price == 103.0
    # Step function: the value in force at `ts` is the last update stamped ≤ ts.
    assert f.value_at(1000) == 100.0
    assert f.value_at(1002) == 101.0
    assert f.value_at(1003.5) == 103.0
    # Before our first update we did not see it: None, never the next tick.
    assert f.value_at(999) is None and not f.witnessed_open(999)
    assert f.witnessed_open(1000)


def test_resolution_prefers_the_twap_stream():
    from pm5.bot import Bot

    bot = Bot.__new__(Bot)
    bot.cfg = _cfg()
    m = _mk(seconds_left=295)  # 5s in
    ws, we = m.window_start, m.window_end
    # Spot says the open tick was 100.0; the TWAP stream in force at the open says 100.4.
    bot.feed = type("F", (), {
        "latest": Tick(price=100.0, src_ts=ws, recv_ts=ws),
        "price_at_or_after": lambda self, ts: 100.0,
        "witnessed_open": lambda self, ts: True,
        "twap": lambda self, a, b: 100.2,
    })()
    bot.twap_feed = _twap_feed([(ws - 30, 100.5), (ws - 1, 100.4)])
    # The update stamped at the open has not landed yet and we are <10s in: wait.
    assert bot._open_ref(m) == (None, "twap60")
    bot.twap_feed._ingest(json.dumps({"topic": "crypto_prices_twap_sixty", "payload": {
        "symbol": "btc/usd", "value": 100.3, "timestamp": ws * 1000}}))
    assert bot._open_ref(m) == (100.3, "twap60")
    assert bot._witnessed(ws) is True and bot._witnessed(ws - 60) is False
    # Close: the TWAP value stamped at the window end, once it is in.
    bot.twap_feed._ingest(json.dumps({"topic": "crypto_prices_twap_sixty", "payload": {
        "symbol": "btc/usd", "value": 100.25, "timestamp": we * 1000}}))
    assert asyncio.run(bot._close_ref(m)) == (100.25, "twap60")
    # Stream down → the spot approximation, labelled as such.
    bot.twap_feed = None
    assert bot._open_ref(m) == (100.0, "spot")
    assert asyncio.run(bot._close_ref(m)) == (100.2, "spot")


def test_offered_at_walks_depth():
    top = BookTop(0.50, 10.0, 0.52, 3.0, asks=[(0.52, 3.0), (0.53, 4.0), (0.60, 100.0)])
    assert top.offered_at(0.52) == 3.0
    assert top.offered_at(0.53) == 7.0
    assert top.offered_at(0.51) == 0.0
    # Depth unknown: top level only.
    assert BookTop(0.50, 10.0, 0.52, 8.0).offered_at(0.52) == 8.0
    assert BookTop(0.50, 10.0, None, 0.0).offered_at(0.52) == 0.0
