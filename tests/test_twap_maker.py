"""Offline tests for TWAP settlement math, fee model and the maker-pair engine."""

import time

from pm5.clob import BookTop, Executor, Fill, taker_fee_usdc
from pm5.config import Config
from pm5.maker import MakerPair
from pm5.markets import Market, current_window_start, slug_for
from pm5.pricefeed import ChainlinkFeed, Tick, TwapProjection, _twap
from pm5.strategy import MomentumStrategy


def _mk(seconds_left=30, seconds_in=None) -> Market:
    now = time.time()
    if seconds_in is not None:
        start = now - seconds_in
        end = start + 300
    else:
        end = now + seconds_left
        start = end - 300
    return Market(
        slug=slug_for(current_window_start()), condition_id="0x", question="q",
        up_token="UP", down_token="DOWN", tick_size=0.01, min_size=5,
        neg_risk=False, window_start=int(start), window_end=int(end),
    )


def _feed_with(prices):
    """prices: list of (src_ts, price)."""
    f = ChainlinkFeed("wss://x", "x")
    for ts, p in prices:
        f._history.append(Tick(price=p, src_ts=ts, recv_ts=time.time()))
    f.latest = f._history[-1]
    f._first_src_ts = f._history[0].src_ts
    return f


def _book(ask, bid=None, ask_size=100.0):
    return BookTop(bid if bid is not None else round(ask - 0.02, 2), 50.0, ask, ask_size)


# ---------------------------------------------------------------- TWAP math

def test_twap_step_function():
    h = [Tick(100, 0, 0), Tick(110, 10, 0), Tick(120, 20, 0)]
    # 0-10 @100, 10-20 @110, 20-30 @120 (last tick holds to end)
    assert _twap(h, 0, 30) == 110.0
    assert _twap(h, 5, 15) == 105.0
    assert _twap(h, 25, 30) == 120.0


def test_projection_and_flip_requirement():
    end = 1000.0
    # Inside the last 60s: 30s known at open+40, now at open+40.
    f = _feed_with([(end - 300, 50000.0), (end - 60, 50040.0), (end - 30, 50040.0)])
    proj = f.projected_close(end, 60, now=end - 30)
    assert abs(proj.twap - 50040.0) < 1e-6
    assert proj.known_secs == 30 and proj.remaining_secs == 30
    # To flip, the remaining 30s must average open - 40 => a drop of 80 from here.
    assert abs(proj.flip_needed(50000.0) - 80.0) < 1e-6
    # With only 10s left the same lead needs a 240 USD average drop.
    proj = f.projected_close(end, 60, now=end - 10)
    assert abs(proj.flip_needed(50000.0) - 240.0) < 1e-6
    # Nothing locked yet: flip == |delta|.
    p0 = TwapProjection(50040.0, 0.0, 60.0, 50040.0, 50040.0)
    assert p0.flip_needed(50000.0) == 40.0


def test_momentum_requires_flip_margin():
    cfg = Config()
    cfg.min_delta_usd = 10
    cfg.min_flip_usd = 60
    cfg.decide_within_secs = 45
    cfg.stop_entry_secs = 3
    m = _mk(seconds_left=40)
    end = m.window_end
    # +30 lead, only 20s of 60 locked: flip needs 30*60/40 = 45 < 60 -> no signal
    f = _feed_with([(m.window_start, 50000.0), (end - 60, 50030.0), (time.time(), 50030.0)])
    assert MomentumStrategy(cfg, f).evaluate(m) is None
    # With 15s left (45 locked): flip needs 30*60/15 = 120 -> fires
    m2 = _mk(seconds_left=15)
    f2 = _feed_with([(m2.window_start, 50000.0), (m2.window_end - 60, 50030.0), (time.time(), 50030.0)])
    sig = MomentumStrategy(cfg, f2).evaluate(m2)
    assert sig is not None and sig.legs[0].side == "up"
    assert "flip needs" in sig.reason


# ---------------------------------------------------------------- fees

def test_taker_fee_peaks_at_half():
    assert round(taker_fee_usdc(100, 0.5, 0.07), 4) == 1.75
    assert round(taker_fee_usdc(100, 0.85, 0.07), 4) == round(taker_fee_usdc(100, 0.15, 0.07), 4)
    assert taker_fee_usdc(100, 0.99, 0.07) < taker_fee_usdc(100, 0.5, 0.07)


def test_paper_buy_charges_fee_in_shares():
    cfg = Config()
    cfg.mode = "paper"
    cfg.paper_bankroll = 0.0
    ex = Executor(cfg, reader=None)
    f = ex.buy("UP", "up", 5.0, 0.85, top=_book(0.50))
    assert f.cost == 5.0
    # 10 shares gross, fee 0.175 USDC = 0.35 shares
    assert f.size == 9.65


# ---------------------------------------------------------------- maker

def _maker(seconds_in=20, bankroll=0.0):
    cfg = Config()
    cfg.mode = "paper"
    cfg.paper_bankroll = bankroll
    cfg.maker_enabled = True
    cfg.maker_bid = 0.46
    cfg.maker_stake_usdc = 5.0
    cfg.maker_start_secs = 10
    cfg.maker_cancel_left_secs = 75
    ex = Executor(cfg, reader=None)
    m = _mk(seconds_in=seconds_in)
    return cfg, ex, m, MakerPair(cfg, ex, m)


def test_maker_posts_both_sides_and_locks_pair():
    cfg, ex, m, mk = _maker()
    # Book at 0.50/0.52 both sides: bids rest, nothing filled.
    fills = mk.step(_book(0.52), _book(0.52))
    assert fills == [] and mk.posted
    assert set(mk.orders) == {"up", "down"}
    assert mk.orders["up"].price == 0.46 and mk.orders["up"].size == 10.87
    # Up ask drops to our bid -> Up hit.
    fills = mk.step(_book(0.46), _book(0.56))
    assert len(fills) == 1 and fills[0].side == "up" and fills[0].maker
    assert mk.naked_side == "up"
    # Later Down ask comes down too -> pair locked, cost 0.92 per $1 pair.
    fills = mk.step(_book(0.60), _book(0.45))
    assert len(fills) == 1 and fills[0].side == "down"
    assert mk.naked_side is None and mk.paired == 10.87
    assert all(o.done for o in mk.orders.values())
    total_cost = sum(f.cost for f in mk.fills)
    assert round(10.87 - total_cost, 2) == round(10.87 * 0.08, 2)


def test_maker_waits_for_an_undecided_book():
    cfg, ex, m, mk = _maker()
    # Up already dumped to 0.30: the market has decided; don't chase it.
    assert mk.step(_book(0.30), _book(0.72)) == []
    assert not mk.posted and mk.orders == {}
    # Still one-sided a bit later -> still waiting.
    mk.step(_book(0.40), _book(0.62))
    assert not mk.posted
    # Book comes back to ~0.50/0.50 -> post the pair at the configured bid.
    mk.step(_book(0.52), _book(0.51))
    assert mk.posted and {o.price for o in mk.orders.values()} == {0.46}


def test_maker_cancels_at_cutoff_and_on_close():
    cfg, ex, m, mk = _maker(seconds_in=300 - 70)  # 70s left, past the 75s cutoff
    mk.step(_book(0.52), _book(0.52))
    assert not mk.posted  # too late to post
    cfg, ex, m, mk = _maker(seconds_in=20)
    mk.step(_book(0.52), _book(0.52))
    assert mk.posted and not mk.cancelled
    m.window_end = int(time.time() + 60)  # jump to 60s left
    mk.step(_book(0.52), _book(0.52))
    assert mk.cancelled and all(o.done for o in mk.orders.values())
    # Once cancelled, a crossing ask must not fill.
    assert mk.step(_book(0.40), _book(0.40)) == []


def test_maker_hedges_naked_leg_when_twap_says_it_loses():
    cfg, ex, m, mk = _maker(seconds_in=20)
    cfg.min_delta_usd = 10
    cfg.min_flip_usd = 20
    cfg.maker_hedge_max_price = 0.60
    mk.step(_book(0.52), _book(0.52))  # post
    mk.step(_book(0.46), _book(0.56))  # Up hit only
    assert mk.naked_side == "up"
    # Not in the decision zone yet -> no hedge.
    proj = TwapProjection(49950.0, 40.0, 20.0, 49950.0, 49950.0)
    assert mk.hedge_signal(proj, 50000.0, _book(0.10), _book(0.55)) is None
    m.window_end = int(time.time() + 20)
    # Up is losing (TWAP 50 below open, flip needs 150): hedge with Down ≤ 0.60.
    sig = mk.hedge_signal(proj, 50000.0, _book(0.10), _book(0.55))
    assert sig is not None and sig.kind == "maker-hedge"
    assert sig.legs[0].side == "down" and sig.legs[0].max_price == 0.60
    assert sig.legs[0].stake_usdc == round(10.87 * 0.55, 2)
    # Too expensive to hedge -> hold.
    assert mk.hedge_signal(proj, 50000.0, _book(0.10), _book(0.70)) is None
    # Up winning -> no hedge.
    proj_up = TwapProjection(50050.0, 40.0, 20.0, 50050.0, 50050.0)
    assert mk.hedge_signal(proj_up, 50000.0, _book(0.90), _book(0.10)) is None


def test_maker_retries_when_rest_fails():
    cfg, ex, m, mk = _maker()
    attempts = {"n": 0}
    real = ex.place_bid

    def flaky(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            return None
        return real(*args, **kwargs)

    ex.place_bid = flaky
    mk.step(_book(0.52), _book(0.52))
    assert not mk.posted and mk.orders == {}
    mk.step(_book(0.52), _book(0.52))
    assert mk.posted and set(mk.orders) == {"up", "down"}


def test_maker_retries_only_the_missing_leg():
    cfg, ex, m, mk = _maker()
    attempts = {"n": 0}
    real = ex.place_bid

    def second_leg_fails_once(*args, **kwargs):
        attempts["n"] += 1
        # First tick: up ok, down rejected. Second tick: down ok.
        if attempts["n"] == 2:
            return None
        return real(*args, **kwargs)

    ex.place_bid = second_leg_fails_once
    mk.step(_book(0.52), _book(0.52))
    assert not mk.posted and set(mk.orders) == {"up"}
    mk.step(_book(0.52), _book(0.52))
    assert mk.posted and set(mk.orders) == {"up", "down"}
    assert attempts["n"] == 3  # did not re-post the live Up bid


def test_maker_keeps_other_bid_after_one_fill():
    cfg, ex, m, mk = _maker()
    mk.step(_book(0.52), _book(0.52))
    mk.step(_book(0.46), _book(0.56))  # Up hit only
    assert mk.naked_side == "up"
    m.window_end = int(time.time() + 60)  # past the empty-book T-75 cutoff
    mk.step(_book(0.60), _book(0.56))
    assert not mk.cancelled
    assert not mk.orders["down"].done  # leftover bid stays so it can still pair


def test_maker_retries_missing_leg_on_onesided_book():
    cfg, ex, m, mk = _maker()
    attempts = {"n": 0}
    real = ex.place_bid

    def flaky(token_id, side, *rest, **kwargs):
        attempts["n"] += 1
        if side == "down" and attempts["n"] < 3:
            return None
        return real(token_id, side, *rest, **kwargs)

    ex.place_bid = flaky
    mk.step(_book(0.52), _book(0.52))
    assert set(mk.orders) == {"up"}
    # Down has collapsed to 0.42 while Up is only *resting*: do not chase it
    # (a 0.41 bid there just buys the dumped side).
    mk.step(_book(0.58), _book(0.42))
    assert "down" not in mk.orders
    # Book comes back near fair: the missing leg is retried at once.
    mk.step(_book(0.52), _book(0.50))
    assert "down" in mk.orders and mk.posted


def test_maker_waits_for_leftover_maker_bid():
    cfg, ex, m, mk = _maker()
    mk.step(_book(0.52), _book(0.52))
    mk.step(_book(0.46), _book(0.56))  # Up hit; Down 0.46 bid still live
    # Cheap Down ask would have been an instant taker lock — do not take it
    # while the leftover maker bid can still fill at 0.46.
    assert mk.complete_pair_signal(_book(0.90), _book(0.50)) is None
    assert not mk.orders["down"].done


def test_maker_completes_pair_when_other_ask_locks():
    cfg, ex, m, mk = _maker()
    mk.step(_book(0.52), _book(0.52))
    mk.step(_book(0.46), _book(0.56))
    ex.cancel_bid(mk.orders["down"])  # leftover bid gone; now a taker lock is ok
    sig = mk.complete_pair_signal(_book(0.90), _book(0.50))
    assert sig is not None and sig.kind == "maker-pair"
    assert sig.legs[0].side == "down"
    assert sig.legs[0].max_price == 0.50
    # 0.46+0.54 + fee ≈ 1.017 > 1.00 — the "locked profit" that was actually -EV.
    assert mk.complete_pair_signal(_book(0.90), _book(0.54)) is None
    # Other ask too expensive even before fee (0.46+0.60 > 1).
    assert mk.complete_pair_signal(_book(0.90), _book(0.60)) is None


def test_maker_pair_counts_taker_fee_in_the_lock():
    cfg, ex, m, mk = _maker()
    mk.step(_book(0.52), _book(0.52))
    mk.step(_book(0.46), _book(0.56))
    ex.cancel_bid(mk.orders["down"])
    # Headline 0.46+0.53 = 0.99 looks locked; fee (~1.7c) pushes it over $1.
    assert mk.complete_pair_signal(_book(0.90), _book(0.53)) is None
    # Cheap enough that fill+ask+fee still ≤ 1.00.
    sig = mk.complete_pair_signal(_book(0.90), _book(0.50))
    assert sig is not None and sig.legs[0].max_price == 0.50


def test_maker_accepts_small_loss_pair_after_grace():
    cfg, ex, m, mk = _maker()
    cfg.maker_pair_grace_secs = 15
    cfg.maker_pair_max_sum = 1.04
    cfg.maker_pair_hard_secs = 999
    now = {"t": 1000.0}
    mk._clock = lambda: now["t"]
    mk.step(_book(0.52), _book(0.52))
    mk.step(_book(0.46), _book(0.56))  # Up hit; Down ask 0.56 -> 1.02 + fee ≈ 1.037
    ex.cancel_bid(mk.orders["down"])
    # Fresh naked leg: only a <= 1.00 pair is taken.
    assert mk.complete_pair_signal(_book(0.90), _book(0.56)) is None
    now["t"] += 14
    assert mk.complete_pair_signal(_book(0.90), _book(0.56)) is None
    # Grace over: lock the pair for a known ~4c/share all-in loss.
    now["t"] += 1
    sig = mk.complete_pair_signal(_book(0.90), _book(0.56))
    assert sig is not None and sig.legs[0].side == "down" and sig.legs[0].max_price == 0.56
    # But never beyond the cap (0.46 + 0.59 + fee > 1.04).
    assert mk.complete_pair_signal(_book(0.90), _book(0.59)) is None


def test_maker_hard_cap_closes_pair_when_other_side_ran_away():
    """The 9:15 case: Up filled, Down jumped to 0.62 and never came back."""
    cfg, ex, m, mk = _maker()
    cfg.maker_pair_grace_secs = 5
    cfg.maker_pair_max_sum = 1.04
    cfg.maker_pair_hard_secs = 20
    cfg.maker_pair_hard_sum = 1.12
    now = {"t": 1000.0}
    mk._clock = lambda: now["t"]
    mk.step(_book(0.52), _book(0.52))
    mk.step(_book(0.46), _book(0.62))  # Up hit; Down ask 0.62 -> sum 1.08
    ex.cancel_bid(mk.orders["down"])
    assert mk.complete_pair_signal(_book(0.90), _book(0.62)) is None
    now["t"] += 10  # past grace, sum 1.08 > 1.04 -> still waiting
    assert mk.complete_pair_signal(_book(0.90), _book(0.62)) is None
    now["t"] += 10  # 20s naked: pay up to 1.12 to bound the loss at 6c/share
    sig = mk.complete_pair_signal(_book(0.90), _book(0.62))
    assert sig is not None and sig.legs[0].side == "down" and sig.legs[0].max_price == 0.62
    # Beyond the hard cap the market has decided; a lock would cost as much as the leg.
    assert mk.complete_pair_signal(_book(0.90), _book(0.67)) is None


def test_maker_pulls_resting_bid_after_pair_buy():
    cfg, ex, m, mk = _maker()
    mk.step(_book(0.52), _book(0.52))
    mk.step(_book(0.46), _book(0.56))  # Up hit, Down bid still resting
    assert not mk.orders["down"].done
    # Bot buys Down as a taker to lock the pair.
    fill = ex.buy("DOWN", "down", round(10.87 * 0.53, 2), 0.53, top=_book(0.53))
    mk.mark_hedged([fill])
    # The leftover Down bid must be gone so it cannot over-fill us.
    assert mk.orders["down"].done
    # And a crossing ask later must not add a second Down leg.
    assert mk.step(_book(0.60), _book(0.40)) == []
    assert abs(mk.shares("down") - mk.shares("up")) < 0.5


def test_maker_paper_fill_respects_bankroll():
    cfg, ex, m, mk = _maker(bankroll=3.0)  # can't afford a $5 leg
    mk.step(_book(0.52), _book(0.52))
    assert mk.step(_book(0.46), _book(0.52)) == []
    assert ex.bankroll == 3.0


def test_maker_defensive_cancel_pulls_dumped_side():
    cfg, ex, m, mk = _maker()
    cfg.maker_defensive_usd = 20
    mk.step(_book(0.52), _book(0.52))
    assert not mk.orders["down"].done and not mk.orders["up"].done
    # +10 is noise: both bids stay.
    mk.step(_book(0.52), _book(0.52), btc=50010.0, open_price=50000.0)
    assert not mk.orders["down"].done and not mk.orders["up"].done
    # BTC dumped up, nothing filled: yank the whole pair, not a naked leftover.
    mk.step(_book(0.52), _book(0.52), btc=50025.0, open_price=50000.0)
    assert mk.orders["down"].done and mk.orders["up"].done
    assert "down" in mk._blocked
    # Must not re-post while the feed is still decided.
    mk.posted = False
    mk.step(_book(0.52), _book(0.52), btc=50025.0, open_price=50000.0)
    assert mk.orders["down"].done and mk.orders["up"].done


def test_maker_does_not_post_when_feed_already_moved():
    """14:55: book still 0.50/0.50 but Chainlink already +$28 vs open."""
    cfg, ex, m, mk = _maker()
    cfg.maker_defensive_usd = 20
    mk.step(_book(0.52), _book(0.52), btc=50028.0, open_price=50000.0)
    assert mk.orders == {} and not mk.posted
    # 15:10: Δ dipped under $20 for a tick — still hold, do not rest.
    mk.step(_book(0.52), _book(0.52), btc=50019.0, open_price=50000.0)
    assert mk.orders == {} and not mk.posted


def test_maker_rests_full_pair_after_an_unposted_spike_fades():
    """16:25/16:30: Δ spiked before we rested; we then sat out the window."""
    cfg, ex, m, mk = _maker()
    cfg.maker_defensive_usd = 20
    mk.step(_book(0.52), _book(0.52), btc=50069.0, open_price=50000.0)
    assert mk.orders == {} and not mk._stood_down and mk._blocked == set()
    # Back inside the hysteresis band (< 15): rest BOTH sides, never one.
    mk.step(_book(0.52), _book(0.52), btc=50010.0, open_price=50000.0)
    assert set(mk.orders) == {"up", "down"} and mk.posted


def test_maker_exit_sells_exact_naked_shares():
    cfg, ex, m, mk = _maker()
    mk.step(_book(0.52), _book(0.52))
    mk.step(_book(0.46), _book(0.56))  # Up hit: 10.87 sh
    mk._naked_since = mk._clock() - 30
    sig = mk.exit_signal(_book(0.44, bid=0.42), _book(0.60))
    assert sig is not None and sig.legs[0].shares == 10.87
    # stake/price re-rounding gives 10.88 -> the CLOB rejects that.
    leg = sig.legs[0]
    assert round(leg.stake_usdc / leg.max_price, 2) == 10.88


def test_fast_feed_parses_binance_and_reports_delta_since_open():
    from pm5.fastfeed import BinanceFeed

    f = BinanceFeed("wss://x")
    now_ms = int(time.time() * 1000)
    f._ingest('{"e":"aggTrade","s":"BTCUSDT","p":"78800.5","T":%d}' % (now_ms - 4000))
    f._ingest('{"e":"aggTrade","s":"BTCUSDT","p":"78831.0","T":%d}' % now_ms)
    f._ingest("garbage")
    assert f.fresh
    assert f.delta_since(time.time() - 5) == 30.5
    # No trade at/after the open we were asked about -> honest None.
    assert f.delta_since(time.time() + 60) is None
    # We connected well after that open: the first tick we hold is not the
    # open, so the move since it is unknown (not "since we connected").
    assert not f.covers(time.time() - 60)
    assert f.delta_since(time.time() - 60) is None
    f.latest = Tick(78831.0, time.time() - 30, time.time() - 30)
    assert not f.fresh and f.delta_since(time.time() - 60) is None


def test_maker_defensive_cancel_uses_binance_lead():
    """Chainlink still shows +6 while Binance is already +31: pull Down now."""
    cfg, ex, m, mk = _maker()
    cfg.maker_defensive_usd = 20
    mk.step(_book(0.52), _book(0.52))
    mk.step(_book(0.52), _book(0.52), btc=50006.0, open_price=50000.0, fast_delta=31.0)
    assert mk.orders["down"].done and mk.orders["up"].done
    assert mk._delta_src == "binance"
    # Binance reverted to +3 even though Chainlink lags at +30: not toxic.
    cfg, ex, m, mk = _maker()
    cfg.maker_defensive_usd = 20
    mk.step(_book(0.52), _book(0.52))
    mk.step(_book(0.52), _book(0.52), btc=50030.0, open_price=50000.0, fast_delta=3.0)
    assert not mk.orders["down"].done and not mk.orders["up"].done


def test_maker_does_not_chase_collapsing_side_for_a_resting_leg():
    """19:15: Up merely resting, Down ask collapsed to 0.25 -> we bid 0.24 and ate it."""
    cfg, ex, m, mk = _maker()
    cfg.maker_bid_give = 0.02
    mk.step(_book(0.52), _book(0.52))
    # Down bid never filled; Down ask collapses. Up still just resting.
    mk.orders["down"].done = True
    mk.posted = False
    mk.step(_book(0.76), _book(0.25))
    assert mk.orders["down"].done  # no new Down bid at 0.24
    # But if Up had FILLED, a 0.24 Down completes the pair at 0.70: take it.
    cfg, ex, m, mk = _maker()
    cfg.maker_bid_give = 0.02
    mk.step(_book(0.52), _book(0.52))
    mk.step(_book(0.46), _book(0.56))  # Up filled
    mk.orders["down"].done = True
    mk.posted = False
    mk.step(_book(0.76), _book(0.25))
    assert not mk.orders["down"].done and mk.orders["down"].price == 0.24


def test_maker_steps_under_a_low_ask_instead_of_sitting_out():
    """12:00 ET at T+3s: book 0.45/0.56. Rest Up @0.44 + Down @0.46 (=0.90)."""
    cfg, ex, m, mk = _maker()
    mk.step(_book(0.45), _book(0.56))
    assert mk.posted
    assert mk.orders["up"].price == 0.44 and mk.orders["down"].price == 0.46
    assert mk.orders["up"].size == mk.orders["down"].size == 10.87
    # Below the floor (0.42) that side has collapsed: still sit out.
    cfg, ex, m, mk = _maker()
    mk.step(_book(0.41), _book(0.60))
    assert not mk.posted and mk.orders == {}


def test_maker_defensive_line_scales_with_realized_vol():
    """9 Sep: BTC swung $300+/window; a fixed $20 line kept us out all hour."""
    cfg, ex, m, mk = _maker()
    cfg.maker_defensive_usd = 20
    # Quiet tape: floor applies.
    assert mk._defensive_limit(290.0, sigma=60.0) == 20
    # Fast tape at the open: 0.15 × 300 ≈ 45.
    assert abs(mk._defensive_limit(300.0, sigma=300.0) - 45.0) < 0.5
    # Same tape near the close: less time for a reversal → tighter (~20).
    assert abs(mk._defensive_limit(60.0, sigma=300.0) - 20.1) < 0.5
    # Δ=+25 on a fast tape is noise: we post and keep both bids.
    mk.step(_book(0.52), _book(0.52), btc=50025.0, open_price=50000.0, sigma=300.0)
    assert mk.posted and set(mk.orders) == {"up", "down"}
    mk.step(_book(0.52), _book(0.52), btc=50025.0, open_price=50000.0, sigma=300.0)
    assert not mk.orders["down"].done and not mk.orders["up"].done
    # +60 is a decision even on that tape.
    mk.step(_book(0.52), _book(0.52), btc=50060.0, open_price=50000.0, sigma=300.0)
    assert mk.orders["down"].done and mk.orders["up"].done


def test_realized_vol_is_trailing_range():
    f = _feed_with([(0, 100.0), (50, 110.0), (100, 100.0), (150, 130.0), (200, 100.0)])
    assert f.realized_vol(300) == 30.0
    # Only the last 60s: ticks at 150 (130) and 200 (100).
    assert f.realized_vol(60) == 30.0
    # Trending tape of tiny ticks still reads as the full travel.
    f = _feed_with([(i, 100.0 + i) for i in range(0, 300, 1)])
    assert f.realized_vol(300) == 299.0
    # Not enough history for the horizon → None.
    assert f.realized_vol(1000) is None


def test_maker_harvests_fill_that_hit_during_defensive_cancel():
    """11:25 ET: Down filled as we yanked; local order still said 0/10.87."""
    cfg, ex, m, mk = _maker()
    cfg.maker_defensive_usd = 20
    mk.step(_book(0.52), _book(0.52))
    hidden = Fill("DOWN", "down", 0.46, 10.87, 5.0002, paper=True, maker=True)
    real = ex.cancel_bid

    def harvest(order):
        if order.side == "down" and order.filled == 0:
            order.filled = 10.87
            order.done = True
            return hidden
        return real(order)

    ex.cancel_bid = harvest
    fills = mk.step(_book(0.52), _book(0.52), btc=50025.0, open_price=50000.0)
    assert any(f.side == "down" and f.size > 10 for f in fills)
    assert mk.shares("down") > 10
    assert not mk._stood_down


def test_maker_does_not_repost_after_defensive_yank():
    """After a +24 cancel, cancelled orders must not retrigger _post spam."""
    cfg, ex, m, mk = _maker()
    cfg.maker_defensive_usd = 20
    mk.step(_book(0.52), _book(0.52))
    mk.step(_book(0.52), _book(0.52), btc=50024.0, open_price=50000.0)
    assert mk._stood_down and all(o.done for o in mk.orders.values())
    attempts = {"n": 0}
    real = ex.place_bid

    def count(*args, **kwargs):
        attempts["n"] += 1
        return real(*args, **kwargs)

    ex.place_bid = count
    mk.step(_book(0.52), _book(0.52), btc=50010.0, open_price=50000.0)
    mk.step(_book(0.52), _book(0.52))
    assert attempts["n"] == 0


def test_maker_defensive_keeps_other_bid_after_fill():
    cfg, ex, m, mk = _maker()
    cfg.maker_defensive_usd = 20
    mk.step(_book(0.52), _book(0.52))
    mk.step(_book(0.46), _book(0.56))  # Up hit; leftover Down bid is the hedge
    assert mk.naked_side == "up" and not mk.orders["down"].done
    mk.step(_book(0.60), _book(0.56), btc=49970.0, open_price=50000.0)
    # BTC down makes Up the dumped side, but Up already filled — keep Down.
    assert not mk.orders["down"].done


def test_maker_exit_sells_naked_leg_after_hard_window():
    """The 10:25 case: Down filled, Up ask ran past 1.12, hold was a $5 coin flip."""
    cfg, ex, m, mk = _maker()
    cfg.maker_pair_hard_secs = 20
    cfg.maker_pair_hard_sum = 1.12
    cfg.maker_exit_secs = 20
    cfg.maker_exit_min_bid = 0.10
    cfg.maker_stop_ticks = 0  # timer-only for this scenario
    now = {"t": 1000.0}
    mk._clock = lambda: now["t"]
    mk.step(_book(0.52), _book(0.52))
    mk.step(_book(0.60), _book(0.46))  # Down hit; Up ask 0.60 -> sum 1.06
    # Up 0.74 + 0.46 + fee = 1.21 loses more than selling Down at 0.28 (0.18).
    expensive_up = _book(0.74, bid=0.72)
    cheap_dn = _book(0.30, bid=0.28)
    assert mk.complete_pair_signal(expensive_up, cheap_dn) is None
    assert mk.exit_signal(expensive_up, cheap_dn) is None
    now["t"] += 19
    assert mk.exit_signal(expensive_up, cheap_dn) is None
    now["t"] += 1
    # Pair still cannot close at 1.12 (0.46+0.74); sell Down at 0.28.
    assert mk.complete_pair_signal(expensive_up, cheap_dn) is None
    sig = mk.exit_signal(expensive_up, cheap_dn)
    assert sig is not None and sig.kind == "maker-exit"
    assert sig.legs[0].side == "down" and sig.legs[0].max_price == 0.28
    fill = ex.sell("DOWN", "down", mk.naked_shares, min_price=0.28, top=cheap_dn)
    assert fill is not None and fill.size < 0
    mk.mark_exited([fill])
    assert mk.exited and mk.naked_side is None
    assert abs(mk.shares("down")) < 0.01
    # Leftover Up bid is gone; no second entry.
    assert mk.orders["up"].done
    assert mk.step(_book(0.40), _book(0.40)) == []


def test_maker_exit_stops_early_when_market_decides_against_leg():
    """17:00: Up filled 0.46, bid drifted 0.42 -> 0.38 while we waited 20s."""
    cfg, ex, m, mk = _maker()
    cfg.maker_exit_secs = 30
    cfg.maker_stop_ticks = 0.04
    now = {"t": 1000.0}
    mk._clock = lambda: now["t"]
    mk.step(_book(0.52), _book(0.52))
    mk.step(_book(0.46), _book(0.56))  # Up hit @ 0.46
    now["t"] += 5
    # Undecided (bid 0.44): keep waiting for the pair.
    assert mk.exit_signal(_book(0.46, bid=0.44), _book(0.56)) is None
    # Bid 0.42 = fill − 0.04: decided. Down ask 0.62 → pair 0.46+0.62+fee ≈ 1.10
    # loses more than selling at 0.42 → sell now, not at T+30s.
    sig = mk.exit_signal(_book(0.44, bid=0.42), _book(0.62))
    assert sig is not None and sig.kind == "maker-exit"
    assert sig.legs[0].max_price == 0.42 and sig.legs[0].shares == 10.87


def test_maker_exit_prefers_cheaper_pair_completion():
    cfg, ex, m, mk = _maker()
    cfg.maker_exit_secs = 30
    cfg.maker_stop_ticks = 0.04
    now = {"t": 1000.0}
    mk._clock = lambda: now["t"]
    mk.step(_book(0.52), _book(0.52))
    mk.step(_book(0.46), _book(0.56))  # Up hit @ 0.46
    now["t"] += 31
    # Timer fired. Selling Up at 0.40 loses 0.06/sh; buying Down at 0.53
    # (+fee 0.017) loses 0.007/sh and locks the pair. Take the pair.
    sig = mk.exit_signal(_book(0.42, bid=0.40), _book(0.53))
    assert sig is not None and sig.kind == "maker-pair"
    assert sig.legs[0].side == "down" and sig.legs[0].max_price == 0.53
    # The leftover Down maker bid was pulled so the taker buy can't double-fill.
    assert mk.orders["down"].done


def test_paper_sell_nets_position_and_credits_bankroll():
    from pm5.bot import Position

    cfg = Config()
    cfg.mode = "paper"
    cfg.paper_bankroll = 10.0
    cfg.taker_fee_rate = 0.07
    ex = Executor(cfg, reader=None)
    pos = Position()
    buy = ex.buy("DOWN", "down", 5.0, 0.46, top=_book(0.46))
    pos.add(buy)
    sold = ex.sell("DOWN", "down", buy.size, min_price=0.20, top=_book(0.90, bid=0.20))
    pos.add(sold)
    # Net flat: settlement PnL is -(buy cost - sell proceeds), either outcome.
    assert abs(pos.fills[0].size + pos.fills[1].size) < 0.02
    pnl_up = pos.settle(up_won=True)
    pnl_dn = pos.settle(up_won=False)
    assert abs(pnl_up - pnl_dn) < 0.02
    assert pnl_up < 0  # we sold cheaper than we bought
    assert ex.bankroll > 5.0  # sale credited cash back
