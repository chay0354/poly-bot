"""Offline tests for the underdog (buy the ~10c side late, hold)."""

import time

from pm5.clob import BookTop, Executor
from pm5.config import Config
from pm5.markets import Market, current_window_start, slug_for
from pm5.underdog import Underdog, dog_shares


def _mk(seconds_left=60) -> Market:
    end = time.time() + seconds_left
    return Market(
        slug=slug_for(current_window_start()), condition_id="0x", question="q",
        up_token="UP", down_token="DOWN", tick_size=0.01, min_size=5,
        neg_risk=False, window_start=int(end - 300), window_end=int(end),
    )


def _book(ask, ask_size=100.0):
    return BookTop(round(max(ask - 0.02, 0.01), 2), 50.0, ask, ask_size)


def _cfg(**kw) -> Config:
    cfg = Config()
    cfg.mode = "paper"
    cfg.paper_bankroll = 0.0
    cfg.sim_latency_ms = 0
    cfg.dog_enabled = True
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def _dog(cfg=None, left=60):
    cfg = cfg or _cfg()
    return Underdog(cfg, Executor(cfg, reader=None), _mk(left))


def test_size_meets_the_dollar_marketable_minimum():
    cfg = _cfg()
    assert dog_shares(cfg, 0.10) == 10
    assert dog_shares(cfg, 0.12) == 9  # 9 × 0.12 = 1.08
    assert dog_shares(cfg, 0.05) == 20
    assert dog_shares(cfg, 0.30) == 5  # 5-share floor


def test_buys_the_cheap_side_in_the_late_window():
    sig = _dog().evaluate(_book(0.91), _book(0.10))
    assert sig is not None and sig.kind == "underdog"
    leg = sig.legs[0]
    assert leg.side == "down" and leg.max_price == 0.10 and leg.shares == 10


def test_skips_outside_the_price_band_or_time_window():
    assert _dog().evaluate(_book(0.75), _book(0.27)) is None
    assert _dog().evaluate(_book(0.98), _book(0.03)) is None
    assert _dog(left=200).evaluate(_book(0.90), _book(0.10)) is None
    assert _dog(left=15).evaluate(_book(0.90), _book(0.10)) is None


def test_needs_the_size_offered():
    assert _dog().evaluate(_book(0.90), _book(0.10, ask_size=6)) is None


def test_one_hit_per_window_then_holds():
    dog = _dog()
    up, dn = _book(0.90), _book(0.10)
    assert dog.shoot(dog.evaluate(up, dn)) is not None
    fills = []
    for _ in range(50):
        fills = dog.poll(up, dn)
        if fills:
            break
        time.sleep(0.01)
    assert fills and fills[0].side == "down"
    assert dog.evaluate(up, dn) is None
