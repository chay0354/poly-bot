"""Offline tests for the favorite (confirmed 90¢ + breakdown exit)."""

import time

from pm5.clob import BookTop, Fill
from pm5.config import Config
from pm5.favorite import Favorite
from pm5.markets import Market, current_window_start, slug_for


def _mk(seconds_left=60) -> Market:
    now = time.time()
    end = now + seconds_left
    return Market(
        slug=slug_for(current_window_start()), condition_id="0x", question="q",
        up_token="UP", down_token="DOWN", tick_size=0.01, min_size=5,
        neg_risk=False, window_start=int(end - 300), window_end=int(end),
    )


def _book(ask, bid=None, ask_size=100.0):
    return BookTop(bid if bid is not None else round(ask - 0.02, 2), 50.0, ask, ask_size)


def _cfg(**kw) -> Config:
    cfg = Config()
    cfg.mode = "paper"
    cfg.paper_bankroll = 0.0
    cfg.favorite_enabled = True
    cfg.snipe_enabled = False
    cfg.momentum_enabled = False
    cfg.maker_enabled = False
    cfg.favorite_stake_usdc = 0.0
    cfg.favorite_shares = 5
    cfg.favorite_trigger = 0.90
    cfg.favorite_max_price = 0.93
    cfg.favorite_hold_secs = 2.0
    cfg.favorite_chase = 0.0
    cfg.favorite_persist_give = 0.02
    cfg.favorite_min_left = 25.0
    cfg.favorite_max_left = 120.0
    cfg.favorite_stop = 0.70
    cfg.favorite_exit_min_bid = 0.15
    cfg.favorite_exit_slip = 0.03
    cfg.favorite_tape = True
    cfg.favorite_tape_usd = 5.0
    cfg.favorite_other_min = 0.08
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def _fav(seconds_left=60, **cfg_kw):
    now = {"t": 0.0}
    fav = Favorite(_cfg(**cfg_kw), _mk(seconds_left), clock=lambda: now["t"])
    return fav, now


def _fill(side="up", price=0.90, size=5.0):
    return Fill("UP" if side == "up" else "DOWN", side, price, size, price * size, True)


def test_favorite_does_not_fire_on_first_90_tick():
    fav, now = _fav()
    up, dn = _book(0.90), _book(0.12)
    assert fav.evaluate(up, dn, 20.0) is None
    now["t"] += 1.9
    assert fav.evaluate(up, dn, 20.0) is None


def test_favorite_sizes_to_one_dollar_stake():
    fav, now = _fav(favorite_stake_usdc=1.0)
    up, dn = _book(0.91), _book(0.12)
    fav.evaluate(up, dn, 20.0)
    now["t"] += 2.0
    sig = fav.evaluate(up, dn, 20.0)
    assert sig is not None
    assert abs(sig.legs[0].shares - round(1.0 / 0.91, 2)) < 1e-9
    assert sig.legs[0].stake_usdc <= 1.01


def test_favorite_fires_after_hold_when_tape_agrees():
    fav, now = _fav()
    up, dn = _book(0.91), _book(0.12)
    fav.evaluate(up, dn, 20.0)
    now["t"] += 2.0
    sig = fav.evaluate(up, dn, 20.0)
    assert sig is not None and sig.kind == "favorite"
    assert sig.legs[0].side == "up"
    assert abs(sig.legs[0].max_price - 0.91) < 1e-9
    assert abs(sig.legs[0].shares - 5.0) < 1e-9


def test_favorite_chases_two_ticks_up_to_max():
    fav, now = _fav(favorite_chase=0.02, favorite_max_price=0.95)
    up, dn = _book(0.91), _book(0.12)
    fav.evaluate(up, dn, 20.0)
    now["t"] += 2.0
    sig = fav.evaluate(up, dn, 20.0)
    assert sig is not None and abs(sig.legs[0].max_price - 0.93) < 1e-9
    cap, now2 = _fav(favorite_chase=0.02, favorite_max_price=0.92)
    cap.evaluate(up, dn, 20.0)
    now2["t"] += 2.0
    sig2 = cap.evaluate(up, dn, 20.0)
    assert sig2 is not None and abs(sig2.legs[0].max_price - 0.92) < 1e-9


def test_favorite_refuses_ask_above_max():
    fav, now = _fav()
    up, dn = _book(0.96), _book(0.06)
    fav.evaluate(up, dn, 20.0)
    now["t"] += 3.0
    assert fav.evaluate(up, dn, 20.0) is None


def test_favorite_refuses_when_tape_disagrees():
    fav, now = _fav()
    up, dn = _book(0.90), _book(0.12)
    fav.evaluate(up, dn, 20.0)
    now["t"] += 2.0
    assert fav.evaluate(up, dn, -20.0) is None  # tape says Down
    assert fav.evaluate(up, dn, 2.0) is None    # Δ too small to call a side
    assert fav.evaluate(up, dn, 20.0) is not None


def test_favorite_resets_timer_when_90_fades():
    fav, now = _fav()
    up, dn = _book(0.90), _book(0.12)
    fav.evaluate(up, dn, 20.0)
    now["t"] += 1.5
    fav.evaluate(_book(0.85), dn, 20.0)  # fade → timer dies
    now["t"] += 1.5
    assert fav.evaluate(_book(0.90), dn, 20.0) is None  # just re-armed
    now["t"] += 2.0
    assert fav.evaluate(_book(0.90), dn, 20.0) is not None


def test_favorite_skips_dust_complement():
    fav, now = _fav()
    up, dn = _book(0.90), _book(0.03)
    fav.evaluate(up, dn, 20.0)
    now["t"] += 2.0
    assert fav.evaluate(up, dn, 20.0) is None


def test_favorite_skips_outside_time_band():
    fav, now = _fav(seconds_left=200)
    up, dn = _book(0.90), _book(0.12)
    fav.evaluate(up, dn, 20.0)
    now["t"] += 2.0
    assert fav.evaluate(up, dn, 20.0) is None
    late, now2 = _fav(seconds_left=10)
    late.evaluate(up, dn, 20.0)
    now2["t"] += 2.0
    assert late.evaluate(up, dn, 20.0) is None


def test_favorite_exit_on_breakdown_not_a_dip():
    fav, _ = _fav()
    fav.mark_filled([_fill()])
    assert fav.side == "up"
    # 0.88 is a flicker, not a breakdown.
    assert fav.exit_signal(_book(0.90, bid=0.88), _book(0.12), 20.0) is None
    cut = fav.exit_signal(_book(0.72, bid=0.70), _book(0.30), 20.0)
    assert cut is not None and cut.kind == "favorite-exit"
    assert cut.legs[0].side == "up"
    assert abs(cut.legs[0].shares - 5.0) < 1e-9
    assert abs(cut.legs[0].min_price - 0.67) < 1e-9  # bid 0.70 − slip 0.03


def test_favorite_exit_on_tape_flip_while_bid_is_still_alive():
    fav, _ = _fav()
    fav.mark_filled([_fill()])
    # Tape flipped, bid already off the 0.90 (≤ trigger − 0.10).
    cut = fav.exit_signal(_book(0.82, bid=0.80), _book(0.22), -12.0)
    assert cut is not None and cut.kind == "favorite-exit"
    assert "tape flipped" in cut.reason


def test_favorite_does_not_dump_into_dust():
    fav, _ = _fav()
    fav.mark_filled([_fill()])
    assert fav.exit_signal(_book(0.70, bid=0.04), _book(0.90), -50.0) is None


def test_favorite_one_shot_after_exit():
    fav, now = _fav()
    fav.mark_filled([_fill()])
    cut = fav.exit_signal(_book(0.70, bid=0.70), _book(0.30), 20.0)
    assert cut is not None
    fav.mark_exited([Fill("UP", "up", 0.70, -5.0, -3.50, True)])
    assert fav.exited
    up, dn = _book(0.90), _book(0.12)
    now["t"] += 3.0
    assert fav.evaluate(up, dn, 20.0) is None
