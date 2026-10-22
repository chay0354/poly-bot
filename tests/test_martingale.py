"""Offline tests for the martingale strategy."""

from pm5.clob import BookTop, Fill, PendingTake
from pm5.config import Config
from pm5.markets import WINDOW_SECS
from pm5.martingale import Martingale, mart_shares, pick_side


class _Market:
    def __init__(self, ws, left=280.0):
        self.window_start, self.seconds_left = ws, left
        self.tick_size, self.neg_risk = 0.01, False

    def token_for(self, side):
        return side


class _Exec:
    bankroll = None
    last_skip = None

    def __init__(self):
        self.sent = []

    def take(self, token, side, price, shares, tick, neg_risk):
        pt = PendingTake(token, side, price, shares, paper=False, sent_at=0.0)
        self.sent.append(pt)
        return pt

    def poll_take(self, pt, top):
        pt.done = True
        return Fill(pt.token_id, pt.side, pt.price, pt.shares, pt.shares * pt.price, paper=False)


def _top(ask):
    return BookTop(ask - 0.01, 100, ask, 100, bids=[(ask - 0.01, 100)], asks=[(ask, 100)])


def _cfg(**kw):
    cfg = Config()
    cfg.mart_enabled = True
    cfg.mart_stakes = [1.0, 2.0, 4.0, 8.0]
    cfg.mart_lookback = 20
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def test_shares_cover_the_stake():
    assert mart_shares(1, 0.51) * 0.51 >= 1
    assert mart_shares(8, 0.47) * 0.47 >= 8


def test_pick_side_bets_the_rarer_continuation():
    U, D = True, False
    hist = [U, U, D, U, U, U, D, U, U, D, D, U, U, D]  # last3 = U U D
    side, n_up, n_down = pick_side(hist, hist[-3:])
    assert (n_up, n_down) == (2, 1)  # UUDU twice, UUDD once
    assert side == "down"


def _run_window(mt, ex, ws, won_up):
    """One window: bet, then settle with `won_up`. Returns the stake sent."""
    mt.begin(_Market(ws))
    sig = mt.evaluate(_top(0.50), _top(0.50))
    assert sig is not None
    mt.shoot(sig)
    mt.poll(_top(0.50), _top(0.50))
    stake = sig.legs[0].stake_usdc
    mt.record(ws, won_up if sig.legs[0].side == "up" else not won_up)
    return stake


def test_ladder_doubles_on_loss_resets_on_win_and_after_four():
    ex = _Exec()
    mt = Martingale(_cfg(), ex)
    ws0 = 1_000_000 * WINDOW_SECS
    for k in range(1, 25):
        mt.outcomes[ws0 - k * WINDOW_SECS] = k % 2 == 0
    mt.backfill = lambda now_ws: None
    stakes = []
    ws = ws0
    for won in (False, False, True, False, False, False, False, True):
        stakes.append(round(_run_window(mt, ex, ws, won)))
        ws += WINDOW_SECS
    assert stakes == [1, 2, 4, 1, 2, 4, 8, 1]


def test_no_bet_until_the_last_bet_is_settled():
    ex = _Exec()
    mt = Martingale(_cfg(), ex)
    ws0 = 1_000_000 * WINDOW_SECS
    for k in range(1, 25):
        mt.outcomes[ws0 - k * WINDOW_SECS] = k % 3 == 0
    mt.backfill = lambda now_ws: None
    mt.begin(_Market(ws0))
    mt.shoot(mt.evaluate(_top(0.5), _top(0.5)))
    mt.poll(_top(0.5), _top(0.5))
    mt.begin(_Market(ws0 + WINDOW_SECS))  # ws0's outcome not known yet
    assert mt.evaluate(_top(0.5), _top(0.5)) is None


def test_skips_when_the_side_is_outside_40_60():
    ex = _Exec()
    mt = Martingale(_cfg(), ex)
    ws0 = 1_000_000 * WINDOW_SECS
    for k in range(1, 25):
        mt.outcomes[ws0 - k * WINDOW_SECS] = True
    mt.backfill = lambda now_ws: None
    mt.begin(_Market(ws0))
    assert mt.evaluate(_top(0.70), _top(0.70)) is None
