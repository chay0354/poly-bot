"""Offline tests for the liquidity-rewards maker."""

from datetime import datetime, timedelta, timezone

from pm5.clob import BookTop, Fill, RestingOrder
from pm5.config import Config
from pm5.rewards import (
    MAX_SHARE, MarketQuoter, RewardMarket, book_daily, book_share, capital_needed, order_score,
    parse_markets, pick, q_min,
    quote_distance_c, quote_prices,
)

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


def _cfg(**kw) -> Config:
    cfg = Config()
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def _m(**kw) -> RewardMarket:
    base = dict(condition_id="0xc", question="q", yes_token="Y", no_token="N", mid=0.5,
                max_spread=4.5, min_size=20, rate_per_day=50, competition=10,
                end=NOW + timedelta(days=60), tick=0.01)
    base.update(kw)
    return RewardMarket(**base)


def test_score_falls_quadratically_to_zero_at_the_band_edge():
    assert order_score(4.5, 0) == 1.0
    assert abs(order_score(4.5, 1.5) - (3 / 4.5) ** 2) < 1e-12
    assert order_score(4.5, 4.5) == 0.0


def test_one_sided_book_counts_a_third_inside_10_90():
    assert q_min(9, 0, 0.5) == 3
    assert q_min(9, 0, 0.05) == 0
    assert q_min(9, 6, 0.5) == 6


def test_quote_both_sides_inside_the_band_without_crossing():
    cfg = _cfg(rw_dist_frac=0.35)
    m = _m()
    assert quote_distance_c(cfg, m) == 2.0
    mid, yes, no = quote_prices(cfg, m, 0.49, 0.51)
    assert mid == 0.5 and yes == 0.48 and no == 0.48
    assert yes < 0.51 and no < 1 - 0.49


def test_no_quote_when_the_book_has_no_real_midpoint():
    assert quote_prices(_cfg(), _m(), 0.20, 0.80) is None
    assert quote_prices(_cfg(), _m(), None, 0.5) is None


def test_book_share_counts_competition_and_skips_our_own_bids():
    cfg = _cfg(rw_dist_frac=0.35)
    m = _m(min_size=20)
    crowded = BookTop(best_bid=0.49, best_bid_size=500, best_ask=0.51, best_ask_size=500,
                      bids=[(0.49, 500)], asks=[(0.51, 500)])
    s = book_share(cfg, m, crowded)
    assert s is not None and s < 0.05
    assert abs(book_daily(cfg, m, crowded) - 50 * s) < 1e-9
    with_us = BookTop(best_bid=0.49, best_bid_size=1, best_ask=0.51, best_ask_size=1,
                      bids=[(0.49, 1), (0.48, 20)], asks=[(0.51, 1), (0.52, 20)])
    alone = book_share(cfg, m, with_us, mine=(("yes", 0.48, 20), ("no", 0.48, 20)))
    assert alone == MAX_SHARE  # only 1-share dust left once our bids are removed


def test_pick_respects_budget_days_and_mid():
    cfg = _cfg(rw_min_days=14, rw_min_mid=0.15, rw_max_mid=0.85, rw_max_markets=5)
    ms = [
        _m(condition_id="a", rate_per_day=100),
        _m(condition_id="soon", rate_per_day=500, end=NOW + timedelta(days=3)),
        _m(condition_id="edge", rate_per_day=500, mid=0.05),
        _m(condition_id="big", rate_per_day=500, min_size=200),
        _m(condition_id="b", rate_per_day=10),
    ]
    got = [m.condition_id for m in pick(cfg, ms, budget=capital_needed(cfg, ms[0]) + 1, now=NOW)]
    assert got == ["a"]


def test_parse_markets_maps_yes_and_no_tokens():
    raw = [{"condition_id": "0x1", "question": "Q?", "rewards_max_spread": 3, "rewards_min_size": 20,
            "market_competitiveness": 2, "end_date": "2026-12-31 00:00:00+00",
            "tokens": [{"token_id": "n1", "outcome": "No", "price": 0.4},
                       {"token_id": "y1", "outcome": "Yes", "price": 0.6}],
            "rewards_config": [{"rate_per_day": 5}, {"rate_per_day": 2}]}]
    m = parse_markets(raw)[0]
    assert (m.yes_token, m.no_token, m.mid, m.rate_per_day) == ("y1", "n1", 0.6, 7)
    assert m.end == datetime(2026, 12, 31, tzinfo=timezone.utc)


class _Trader:
    def __init__(self):
        self.placed = []
        self.fill_next: dict[str, float] = {}

    def place_bid(self, token, side, price, shares, tick, neg_risk):
        o = RestingOrder(token, side, price, shares, paper=False, order_id=f"{side}{len(self.placed)}")
        self.placed.append(o)
        return o

    def poll_bid(self, o, force=False):
        n = self.fill_next.pop(o.side, 0)
        if not n:
            return None
        o.filled += n
        o.done = o.filled >= o.size
        return Fill(o.token_id, o.side, o.price, n, n * o.price, paper=False, maker=True)

    def cancel_bid(self, o, wait=False):
        o.done = True
        return None


class _Reader:
    def __init__(self, bid, ask):
        self.bid, self.ask = bid, ask

    def top(self, token):
        return BookTop(self.bid, 100, self.ask, 100)


def test_quoter_posts_both_sides_then_stops_adding_to_a_lone_fill():
    cfg = _cfg(rw_dist_frac=0.35, rw_jump_cents=3)
    t = _Trader()
    q = MarketQuoter(cfg, _m(), t, _Reader(0.49, 0.51), clock=lambda: 0.0)
    q.step()
    assert sorted(o.side for o in t.placed) == ["no", "yes"]
    t.fill_next["yes"] = 20
    q.step()
    assert q.yes.shares == 20
    q.step()
    assert [o.side for o in t.placed].count("yes") == 1  # no second yes bid
    assert q.no.order is not None and q.no.order.live    # still pairing


class _BookReader:
    """A book with levels, where our own bids show up like anyone's."""

    def __init__(self, bids, asks, trader):
        self.bids, self.asks, self.t = bids, asks, trader

    def top(self, token):
        bids, asks = list(self.bids), list(self.asks)
        for o in self.t.placed:
            if not o.done:
                if o.side == "yes":
                    bids.append((o.price, o.size))
                else:
                    asks.append((round(1 - o.price, 4), o.size))
        bids.sort(reverse=True)
        asks.sort()
        return BookTop(bids[0][0] if bids else None, bids[0][1] if bids else 0,
                       asks[0][0] if asks else None, asks[0][1] if asks else 0,
                       bids=bids, asks=asks)


def test_our_own_bids_do_not_move_the_mid_we_quote_around():
    cfg = _cfg(rw_dist_frac=0.15, rw_jump_cents=3)
    t = _Trader()
    q = MarketQuoter(cfg, _m(max_spread=6.5), t, _BookReader([(0.40, 50)], [(0.50, 50)], t),
                     clock=lambda: 0.0)
    q.step()
    assert len(t.placed) == 2  # yes 0.44, no 0.54 — both inside the old spread
    for _ in range(5):
        q.step()
    assert len(t.placed) == 2  # no chase: the mid is read without our quotes


def test_lone_leg_rests_a_profitable_pairing_bid_even_on_a_thin_book():
    cfg = _cfg(rw_dist_frac=0.35)
    t = _Trader()
    q = MarketQuoter(cfg, _m(), t, _BookReader([(0.10, 5)], [(0.40, 5)], t),
                     clock=lambda: 0.0, pair_only=True)
    q.yes.shares, q.yes.cost = 10, 2.9  # bought yes at 0.29
    q.step()
    assert [(o.side, o.price, o.size) for o in t.placed] == [("no", 0.7, 10)]


def test_rejected_bid_waits_before_reposting():
    cfg = _cfg(rw_dist_frac=0.35)
    t = _Trader()
    clock = [0.0]
    q = MarketQuoter(cfg, _m(), t, _Reader(0.49, 0.51), clock=lambda: clock[0])
    q.step()
    for o in t.placed:
        o.failed = o.done = True
    q.step()
    assert len(t.placed) == 2
    clock[0] = 61.0
    q.step()
    assert len(t.placed) == 4


def test_quoter_pulls_everything_on_a_jump_and_cools_down():
    cfg = _cfg(rw_dist_frac=0.35, rw_jump_cents=3, rw_cooldown_secs=600)
    t = _Trader()
    r = _Reader(0.49, 0.51)
    clock = [0.0]
    q = MarketQuoter(cfg, _m(), t, r, clock=lambda: clock[0])
    q.step()
    r.bid, r.ask = 0.55, 0.57
    clock[0] = 5.0
    q.step()
    assert not q.live_ids()
    n = len(t.placed)
    clock[0] = 100.0
    q.step()
    assert len(t.placed) == n  # still cooling
