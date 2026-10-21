"""Offline tests for the lag taker (tape decided, book lagging)."""

import time

from pm5.clob import BookTop, Executor
from pm5.config import Config
from pm5.lag import LagTaker
from pm5.markets import Market, current_window_start, slug_for


def _mk(seconds_left=30) -> Market:
    end = time.time() + seconds_left
    return Market(
        slug=slug_for(current_window_start()), condition_id="0x", question="q",
        up_token="UP", down_token="DOWN", tick_size=0.01, min_size=5,
        neg_risk=False, window_start=int(end - 300), window_end=int(end),
    )


def _book(ask, ask_size=100.0):
    return BookTop(round(ask - 0.02, 2), 50.0, ask, ask_size)


def _cfg(**kw) -> Config:
    cfg = Config()
    cfg.mode = "paper"
    cfg.paper_bankroll = 0.0
    cfg.taker_fee_rate = 0.07
    cfg.sim_latency_ms = 150
    cfg.lag_enabled = True
    cfg.lag_shares = 5
    cfg.lag_min_fair = 0.93
    cfg.lag_min_edge = 0.04
    cfg.lag_min_price = 0.60
    cfg.lag_max_price = 0.95
    cfg.lag_min_left = 5
    cfg.lag_max_left = 90
    cfg.lag_max_feed_age_ms = 1000
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def _lag(cfg=None, left=30, clock=time.monotonic):
    cfg = cfg or _cfg()
    return LagTaker(cfg, Executor(cfg, reader=None), _mk(left), clock=clock)


def test_buys_the_decided_side_while_its_ask_lags():
    lag = _lag()
    # $20 over the beat, $100 range, T-30: fair ≈ 0.99 vs ask 0.90.
    sig = lag.evaluate(20.0, 100.0, 50.0, _book(0.90), _book(0.12))
    assert sig is not None and sig.kind == "lag"
    leg = sig.legs[0]
    assert leg.side == "up" and leg.max_price == 0.90 and leg.shares == 5


def test_down_side_mirrors():
    sig = _lag().evaluate(-20.0, 100.0, 50.0, _book(0.12), _book(0.88))
    assert sig is not None and sig.legs[0].side == "down"


def test_no_shot_when_book_already_caught_up():
    # fair ≈ 0.99, ask 0.97: above max price and no edge.
    assert _lag().evaluate(20.0, 100.0, 50.0, _book(0.97), _book(0.04)) is None
    # Within band but edge after fee < 0.04.
    assert _lag(_cfg(lag_max_price=0.99)).evaluate(
        20.0, 100.0, 50.0, _book(0.96), _book(0.05)) is None


def test_no_shot_when_tape_has_not_decided():
    # $5 over the beat on a $300 range: fair well under 0.93.
    assert _lag().evaluate(5.0, 300.0, 50.0, _book(0.70), _book(0.32)) is None


def test_stale_feed_or_unknown_sigma_blocks():
    lag = _lag()
    assert lag.evaluate(20.0, 100.0, 2500.0, _book(0.90), _book(0.12)) is None
    assert lag.evaluate(20.0, 100.0, None, _book(0.90), _book(0.12)) is None
    assert lag.evaluate(20.0, None, 50.0, _book(0.90), _book(0.12)) is None
    assert lag.evaluate(None, 100.0, 50.0, _book(0.90), _book(0.12)) is None


def test_time_band_and_thin_offer():
    assert _lag(left=150).evaluate(40.0, 100.0, 50.0, _book(0.90), _book(0.12)) is None
    assert _lag(left=3).evaluate(20.0, 100.0, 50.0, _book(0.90), _book(0.12)) is None
    assert _lag().evaluate(20.0, 100.0, 50.0, _book(0.90, ask_size=2), _book(0.12)) is None


def test_paper_take_fills_if_quote_survives_then_one_per_window():
    t = [0.0]
    lag = _lag(clock=lambda: t[0])
    up = _book(0.90)
    sig = lag.evaluate(20.0, 100.0, 50.0, up, _book(0.12))
    pt = lag.shoot(sig)
    assert pt is not None and lag.pending is pt
    assert lag.evaluate(20.0, 100.0, 50.0, up, _book(0.12)) is None  # in flight
    pt.deadline = time.monotonic() - 0.001  # latency elapsed
    fills = lag.poll(up, _book(0.12))
    assert len(fills) == 1 and fills[0].side == "up" and lag.side == "up"
    assert lag.evaluate(20.0, 100.0, 50.0, up, _book(0.12)) is None  # per_window=1


def test_vanished_quote_is_a_miss_then_retry_after_cooldown():
    t = [0.0]
    lag = _lag(clock=lambda: t[0])
    sig = lag.evaluate(20.0, 100.0, 50.0, _book(0.90), _book(0.12))
    lag.shoot(sig)
    assert lag.poll(_book(0.95), _book(0.06)) == []  # ask moved up before we landed
    assert lag.pending is None and lag.hits == 0
    assert lag.evaluate(20.0, 100.0, 50.0, _book(0.90), _book(0.12)) is None  # cooldown
    t[0] += 1.0
    assert lag.evaluate(20.0, 100.0, 50.0, _book(0.90), _book(0.12)) is not None


def test_quote_grid_covers_ask_band():
    shares, prices = LagTaker.quote_grid(_cfg(), _mk())
    assert shares == 5 and prices[0] == 0.60 and prices[-1] == 0.95 and len(prices) == 36
