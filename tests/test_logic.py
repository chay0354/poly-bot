"""Offline unit tests for strategy + settlement logic (no network)."""

import io
import json
import logging
import time

from pm5.bot import Bot, Position
from pm5.clob import BookTop, Fill
from pm5.config import Config
from pm5.status import LiveStatus
from pm5.markets import Market, current_window_start, slug_for
from pm5.pricefeed import ChainlinkFeed, Tick
from pm5.strategy import ArbitrageStrategy, MomentumStrategy


def make_market(seconds_left=30) -> Market:
    ws = current_window_start()
    end = time.time() + seconds_left
    return Market(
        slug=slug_for(ws), condition_id="0xabc", question="BTC up or down",
        up_token="UP", down_token="DOWN", tick_size=0.01, min_size=5,
        neg_risk=False, window_start=int(time.time() - (300 - seconds_left)),
        window_end=int(end),
    )


def test_slug_format():
    assert slug_for(1781263200) == "btc-updown-5m-1781263200"
    m = make_market()
    assert m.browser_url.startswith("https://polymarket.com/event/btc-updown-5m-")


def test_window_alignment():
    assert current_window_start(1781263205) == 1781263200
    assert current_window_start(1781263499) == 1781263200
    assert current_window_start(1781263500) == 1781263500


def test_position_settlement_up_wins():
    pos = Position()
    pos.add(Fill("UP", "up", 0.50, 10, 5.0, paper=True))
    # bought 10 UP shares for $5; UP wins -> payout 10, pnl +5
    assert pos.settle(up_won=True) == 5.0
    assert pos.settle(up_won=False) == -5.0


def test_arb_settlement_is_risk_free():
    pos = Position()
    pos.add(Fill("UP", "up", 0.48, 10, 4.8, paper=True))
    pos.add(Fill("DOWN", "down", 0.49, 10, 4.9, paper=True))
    # combined cost 9.7 for guaranteed 10 payout regardless of outcome
    assert round(pos.settle(up_won=True), 2) == 0.30
    assert round(pos.settle(up_won=False), 2) == 0.30


class _FeedStub(ChainlinkFeed):
    def __init__(self, open_price, last_price, mom):
        self._open = open_price
        self._mom = mom
        self.latest = Tick(price=last_price, src_ts=time.time(), recv_ts=time.time())

    def price_at_or_after(self, ts):
        return self._open

    def momentum(self, lookback_secs):
        return self._mom


def test_momentum_fires_up_when_price_above_open():
    cfg = Config()
    cfg.momentum_enabled = True
    cfg.min_delta_usd = 8
    cfg.decide_within_secs = 45
    cfg.stop_entry_secs = 3
    feed = _FeedStub(open_price=63000, last_price=63050, mom=12)
    strat = MomentumStrategy(cfg, feed)
    sig = strat.evaluate(make_market(seconds_left=30))
    assert sig is not None
    assert sig.legs[0].side == "up"


def test_momentum_suppressed_on_small_delta():
    cfg = Config()
    cfg.min_delta_usd = 8
    feed = _FeedStub(open_price=63000, last_price=63003, mom=1)
    strat = MomentumStrategy(cfg, feed)
    assert strat.evaluate(make_market(seconds_left=30)) is None


def test_momentum_suppressed_when_momentum_fades():
    cfg = Config()
    cfg.min_delta_usd = 8
    # price above open (delta +50) but short-term momentum strongly down
    feed = _FeedStub(open_price=63000, last_price=63050, mom=-30)
    strat = MomentumStrategy(cfg, feed)
    assert strat.evaluate(make_market(seconds_left=30)) is None


def test_momentum_only_in_closing_window():
    cfg = Config()
    cfg.min_delta_usd = 8
    cfg.decide_within_secs = 45
    feed = _FeedStub(open_price=63000, last_price=63050, mom=12)
    strat = MomentumStrategy(cfg, feed)
    # 200s left -> too early
    assert strat.evaluate(make_market(seconds_left=200)) is None


class _ReaderStub:
    def __init__(self, up_ask, down_ask):
        self._up = up_ask
        self._down = down_ask

    def top(self, token_id):
        ask = self._up if token_id == "UP" else self._down
        return BookTop(best_bid=ask - 0.02, best_bid_size=100, best_ask=ask, best_ask_size=100)


def test_arb_fires_when_combined_below_one():
    cfg = Config()
    cfg.arb_enabled = True
    cfg.arb_min_edge = 0.02
    strat = ArbitrageStrategy(cfg, _ReaderStub(up_ask=0.47, down_ask=0.48))
    sig = strat.evaluate(make_market())
    assert sig is not None
    assert {leg.side for leg in sig.legs} == {"up", "down"}


def test_arb_silent_when_no_edge():
    cfg = Config()
    cfg.arb_min_edge = 0.02
    strat = ArbitrageStrategy(cfg, _ReaderStub(up_ask=0.50, down_ask=0.50))
    assert strat.evaluate(make_market()) is None


def test_pricefeed_keeps_only_btc():
    feed = ChainlinkFeed("wss://x", "x")
    feed._ingest(
        '{"topic":"crypto_prices_chainlink","payload":{"symbol":"eth/usd","value":3000,"timestamp":1000}}'
    )
    assert feed.latest is None  # eth ignored
    feed._ingest(
        '{"topic":"crypto_prices_chainlink","payload":{"symbol":"btc/usd","value":63000,"timestamp":2000}}'
    )
    assert feed.latest is not None and feed.latest.price == 63000


def test_witnessed_open_requires_tick_before_window():
    feed = ChainlinkFeed("wss://x", "x")
    # First tick at src_ts=1000; a window that opened at 900 was witnessed,
    # one that opened at 1100 (after we started) was not.
    feed._ingest(
        '{"topic":"crypto_prices_chainlink","payload":{"symbol":"btc/usd","value":63000,"timestamp":1000000}}'
    )
    # First tick at src_ts=1000s. A window that opened at 1100 (after we started
    # streaming) was witnessed; one that opened at 900 (before we started) was not.
    assert feed.witnessed_open(1100) is True
    assert feed.witnessed_open(900) is False


def test_arb_execute_aborts_when_one_leg_moved(monkeypatch):
    """If one leg's ask jumps above its limit, neither leg should fill."""
    from pm5.bot import Bot

    cfg = Config()
    cfg.mode = "paper"
    cfg.arb_min_edge = 0.02
    bot = Bot.__new__(Bot)  # skip network __init__
    bot.cfg = cfg

    # Reader now reports the up ask jumped to 0.55 (above the 0.50 the signal saw).
    bot.reader = _ReaderStub(up_ask=0.55, down_ask=0.48)

    placed = []

    class _Exec:
        bankroll = None

        def buy(self, token_id, side, stake, max_price, top=None):
            placed.append(side)
            return Fill(token_id, side, max_price, 10, max_price * 10, paper=True)

    bot.executor = _Exec()

    from pm5.strategy import Leg, Signal

    legs = [
        Leg("up", "UP", 0.50, 5, BookTop(0.48, 100, 0.50, 100)),
        Leg("down", "DOWN", 0.48, 5, BookTop(0.46, 100, 0.48, 100)),
    ]
    sig = Signal(kind="arb", legs=legs, reason="test")
    fills = bot._execute_arb(sig)
    assert fills == []  # aborted
    assert placed == []  # nothing placed


def test_arb_execute_fills_both_when_stable():
    from pm5.bot import Bot

    cfg = Config()
    cfg.mode = "paper"
    cfg.arb_min_edge = 0.02
    bot = Bot.__new__(Bot)
    bot.cfg = cfg
    bot.reader = _ReaderStub(up_ask=0.48, down_ask=0.48)

    placed = []

    class _Exec:
        bankroll = None

        def buy(self, token_id, side, stake, max_price, top=None):
            placed.append(side)
            return Fill(token_id, side, max_price, 10, max_price * 10, paper=True)

    bot.executor = _Exec()

    from pm5.strategy import Leg, Signal

    legs = [
        Leg("up", "UP", 0.48, 5, BookTop(0.46, 100, 0.48, 100)),
        Leg("down", "DOWN", 0.48, 5, BookTop(0.46, 100, 0.48, 100)),
    ]
    sig = Signal(kind="arb", legs=legs, reason="test")
    fills = bot._execute_arb(sig)
    assert len(fills) == 2
    assert set(placed) == {"up", "down"}


class _FakeTTY(io.StringIO):
    def isatty(self):
        return True


def test_status_noop_when_not_tty():
    # A plain StringIO is not a TTY, so the status line must stay silent.
    s = LiveStatus(enabled=True, stream=io.StringIO())
    assert s.enabled is False
    s.render("hello")
    # nothing should have been written / no crash
    s.clear()
    s.finalize()


def test_status_draws_and_clears_on_tty():
    stream = _FakeTTY()
    s = LiveStatus(enabled=True, stream=stream)
    assert s.enabled is True
    s.render("line one")
    out = stream.getvalue()
    assert "line one" in out
    assert "\033[K" in out  # erase sequence used
    s.clear()
    assert stream.getvalue().endswith("\033[K")  # cleared, no trailing text


def test_status_disabled_flag_overrides_tty():
    s = LiveStatus(enabled=False, stream=_FakeTTY())
    assert s.enabled is False


class _AlwaysSkipExec:
    """Executor stub: never fills, always reports the same skip reason."""

    last_skip = "no asks on book"

    def buy(self, *a, **k):
        self.last_skip = "no asks on book"
        return None


def _momentum_signal():
    from pm5.strategy import Leg, Signal

    return Signal(kind="momentum", legs=[Leg("down", "DOWN", 0.85, 5, None)], reason="Δopen=-100")


def test_execute_dedups_repeated_momentum_skip(caplog):
    cfg = Config()
    cfg.mode = "paper"
    bot = Bot.__new__(Bot)
    bot.cfg = cfg
    bot._window_logged = set()
    bot.executor = _AlwaysSkipExec()
    sig = _momentum_signal()

    with caplog.at_level(logging.INFO, logger="pm5.bot"):
        for _ in range(25):  # 25 polls, same stuck situation
            assert bot._execute(sig) == []

    signals = [r for r in caplog.records if "SIGNAL[momentum]" in r.getMessage()]
    skips = [r for r in caplog.records if "not taken" in r.getMessage()]
    assert len(signals) == 1  # logged once, not flooded
    assert len(skips) == 1


def test_execute_relogs_after_window_reset(caplog):
    cfg = Config()
    cfg.mode = "paper"
    bot = Bot.__new__(Bot)
    bot.cfg = cfg
    bot._window_logged = set()
    bot.executor = _AlwaysSkipExec()
    sig = _momentum_signal()

    with caplog.at_level(logging.INFO, logger="pm5.bot"):
        bot._execute(sig)
        bot._execute(sig)
        bot._window_logged.clear()  # simulates a new window / post-fill reset
        bot._execute(sig)

    signals = [r for r in caplog.records if "SIGNAL[momentum]" in r.getMessage()]
    assert len(signals) == 2  # once per "window"


def test_recorder_writes_jsonl(tmp_path):
    from pm5.recorder import Recorder

    path = tmp_path / "data" / "trades.jsonl"
    rec = Recorder(str(path), enabled=True, mode="paper")
    rec.fill(strategy="momentum", window="w1", side="down", price=0.59, shares=8.47, cost=5.0)
    rec.window(window="w1", witnessed=True, open_price=100.0, close_price=101.0,
               up_won=True, traded=True, n_fills=1, window_pnl=1.02, session_pnl=1.02)
    rec.close()

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    fill = json.loads(lines[0])
    window = json.loads(lines[1])
    assert fill["type"] == "fill" and fill["strategy"] == "momentum" and fill["side"] == "down"
    assert window["type"] == "window" and window["up_won"] is True and window["window_pnl"] == 1.02
    # Every record is stamped with time + mode.
    assert "ts" in fill and fill["mode"] == "paper"


def test_recorder_noop_when_disabled(tmp_path):
    from pm5.recorder import Recorder

    path = tmp_path / "trades.jsonl"
    rec = Recorder(str(path), enabled=False)
    rec.fill(side="up")
    rec.window(window="w1")
    rec.close()
    assert not path.exists()


def test_settlement_records_window(tmp_path):
    """A settled paper window writes a window record with the outcome + PnL."""
    from pm5.recorder import Recorder

    cfg = Config()
    cfg.mode = "paper"
    bot = Bot.__new__(Bot)
    bot.cfg = cfg
    bot.session_pnl = 0.0
    bot.day_pnl = 0.0
    bot.executor = type("E", (), {"bankroll": None})()
    bot.recorder = Recorder(str(tmp_path / "trades.jsonl"), enabled=True, mode="paper")

    # Feed stub exposing the closing price.
    bot.feed = type("F", (), {"latest": Tick(price=63435.8, src_ts=0, recv_ts=0)})()

    market = make_market()
    market.window_start = 1781270100
    pos = Position()
    pos.add(Fill("UP", "up", 0.83, 6.02, 5.0, paper=True))  # bought UP

    sampled_path = [
        {"t": 30.0, "btc": 63430.0, "up": 0.90, "dn": 0.11},
        {"t": 10.0, "btc": 63435.0, "up": 0.95, "dn": 0.06},
    ]
    bot._settle_window(market, pos, open_price=63410.1, witnessed=True, path=sampled_path)
    bot.recorder.close()

    records = [json.loads(line) for line in (tmp_path / "trades.jsonl").read_text().splitlines()]
    window = next(r for r in records if r["type"] == "window")
    assert window["up_won"] is True  # close 63435.8 >= open 63410.1
    assert window["traded"] is True and window["n_fills"] == 1
    # UP won: 6.02 shares pay $6.02 for $5.00 cost -> +1.02
    assert round(window["window_pnl"], 2) == 1.02
    assert round(bot.session_pnl, 2) == 1.02
    # The decision-zone price/quote path is persisted for backtesting.
    assert window["path"] == sampled_path
    assert window["path"][0]["t"] == 30.0 and window["path"][1]["up"] == 0.95


def _book(ask):
    return BookTop(best_bid=ask - 0.02, best_bid_size=100, best_ask=ask, best_ask_size=100)


def test_bankroll_debits_on_fill_and_blocks_when_empty():
    from pm5.clob import Executor

    cfg = Config()
    cfg.mode = "paper"
    cfg.paper_bankroll = 8.0
    ex = Executor(cfg, reader=None)
    assert ex.bankroll == 8.0

    # Buy $5 at ask 0.50 -> 10 shares, cost $5.00, bankroll 8 -> 3.
    f1 = ex.buy("UP", "up", stake_usdc=5.0, max_price=0.85, top=_book(0.50))
    assert f1 is not None and f1.cost == 5.0
    assert ex.bankroll == 3.0

    # Next $5 buy can't be afforded (3 < 5) -> skipped with a reason.
    f2 = ex.buy("UP", "up", stake_usdc=5.0, max_price=0.85, top=_book(0.50))
    assert f2 is None
    assert "insufficient bankroll" in ex.last_skip


def test_momentum_refuses_cheap_ask_when_book_disagrees():
    """A favored side offered at 0.01-0.32 in the closing seconds is the side
    the book already thinks is losing; the floor must refuse it."""
    from pm5.clob import Executor

    cfg = Config()
    cfg.mode = "paper"
    cfg.paper_bankroll = 0.0
    ex = Executor(cfg, reader=None)

    f = ex.buy("UP", "up", stake_usdc=5.0, max_price=0.85, top=_book(0.28), min_price=0.50)
    assert f is None
    assert "floor" in ex.last_skip and "market disagrees" in ex.last_skip

    # Inside the band the buy goes through.
    f = ex.buy("UP", "up", stake_usdc=5.0, max_price=0.85, top=_book(0.62), min_price=0.50)
    assert f is not None and f.price == 0.62

    # No floor (arb legs) keeps the old behaviour.
    f = ex.buy("UP", "up", stake_usdc=5.0, max_price=0.85, top=_book(0.28))
    assert f is not None


def test_momentum_leg_carries_floor_from_config():
    cfg = Config()
    cfg.min_price = 0.55
    cfg.min_delta_usd = 8.0
    feed = ChainlinkFeed("wss://x", "x")
    m = make_market(seconds_left=30)
    now_ms = int(time.time() * 1000)
    feed._ingest(
        '{"topic":"crypto_prices_chainlink","payload":{"symbol":"btc/usd",'
        f'"value":100000,"timestamp":{(m.window_start + 1) * 1000}}}}}'
    )
    feed._ingest(
        '{"topic":"crypto_prices_chainlink","payload":{"symbol":"btc/usd",'
        f'"value":100050,"timestamp":{now_ms}}}}}'
    )
    sig = MomentumStrategy(cfg, feed).evaluate(m)
    assert sig is not None
    assert sig.legs[0].side == "up"
    assert sig.legs[0].min_price == 0.55


def test_bankroll_unlimited_when_zero():
    from pm5.clob import Executor

    cfg = Config()
    cfg.mode = "paper"
    cfg.paper_bankroll = 0.0
    ex = Executor(cfg, reader=None)
    assert ex.bankroll is None  # not tracked
    for _ in range(100):
        assert ex.buy("UP", "up", 5.0, 0.85, top=_book(0.50)) is not None


def test_bankroll_credited_on_settlement():
    from pm5.clob import Executor

    cfg = Config()
    cfg.mode = "paper"
    cfg.paper_bankroll = 100.0
    bot = Bot.__new__(Bot)
    bot.cfg = cfg
    bot.session_pnl = 0.0
    bot.day_pnl = 0.0
    bot.recorder = type("R", (), {"enabled": False})()
    bot.executor = Executor(cfg, reader=None)
    bot.feed = type("F", (), {"latest": Tick(price=101.0, src_ts=0, recv_ts=0)})()

    # Simulate a bought position: bankroll debited at fill time.
    fill = bot.executor.buy("UP", "up", 5.0, 0.85, top=_book(0.50))  # 10 sh @ 0.50, cost 5
    assert bot.executor.bankroll == 95.0
    pos = Position()
    pos.add(fill)

    market = make_market()
    market.window_start = 1
    bot._settle_window(market, pos, open_price=100.0, witnessed=True)
    # UP won (close 101 >= open 100): 10 shares pay $10 -> bankroll 95 + 10 = 105.
    assert bot.executor.bankroll == 105.0
    assert round(bot.session_pnl, 2) == 5.0


def test_bankroll_exhausted_stop_condition():
    from pm5.clob import Executor

    cfg = Config()
    cfg.mode = "paper"
    cfg.paper_bankroll = 100.0
    cfg.momentum_enabled = True
    cfg.arb_enabled = False
    cfg.stake_usdc = 5.0
    bot = Bot.__new__(Bot)
    bot.cfg = cfg
    bot.executor = Executor(cfg, reader=None)

    bot.executor.bankroll = 4.99  # below the $5 stake
    assert bot._bankroll_exhausted() is True
    bot.executor.bankroll = 5.0
    assert bot._bankroll_exhausted() is False


def test_signal_mode_announces_and_does_not_buy(caplog):
    from pm5.strategy import Leg, Signal

    cfg = Config()
    cfg.mode = "signal"
    cfg.max_price = 0.85
    bot = Bot.__new__(Bot)
    bot.cfg = cfg
    bot._window_logged = set()
    bot.reader = _ReaderStub(up_ask=0.62, down_ask=0.40)

    bought = []

    class _Exec:
        last_skip = None
        bankroll = None

        def buy(self, *a, **k):
            bought.append(True)
            raise AssertionError("signal mode must not place orders")

    bot.executor = _Exec()
    market = make_market(seconds_left=28)
    sig = Signal(
        kind="momentum",
        legs=[Leg("up", "UP", 0.85, 5, None)],
        reason="Δopen=+12.0 USD, 28s left",
    )

    with caplog.at_level(logging.INFO, logger="pm5.bot"):
        fills = bot._execute(sig, market)

    assert bought == []
    assert len(fills) == 1
    assert fills[0].side == "up" and fills[0].paper is True
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "BUY UP" in msg
    assert "0.62" in msg
    assert "do not SELL" in msg
    assert market.browser_url in msg


def test_signal_mode_skips_when_ask_above_cap(caplog):
    from pm5.strategy import Leg, Signal

    cfg = Config()
    cfg.mode = "signal"
    bot = Bot.__new__(Bot)
    bot.cfg = cfg
    bot._window_logged = set()
    bot.reader = _ReaderStub(up_ask=0.92, down_ask=0.10)
    bot.executor = type("E", (), {"last_skip": None, "bankroll": None})()

    sig = Signal(kind="momentum", legs=[Leg("up", "UP", 0.85, 5, None)], reason="x")
    with caplog.at_level(logging.INFO, logger="pm5.bot"):
        assert bot._execute(sig, make_market()) == []
    assert any("ask 0.92 > cap 0.85" in r.getMessage() for r in caplog.records)


def test_pos_summary_groups_by_side():
    pos = Position()
    assert Bot._pos_summary(pos) == "—"
    pos.add(Fill("UP", "up", 0.5, 10, 5.0, paper=True))
    pos.add(Fill("UP", "up", 0.5, 5, 2.5, paper=True))
    pos.add(Fill("DOWN", "down", 0.5, 4, 2.0, paper=True))
    summary = Bot._pos_summary(pos)
    assert "UP 15.0sh" in summary
    assert "DOWN 4.0sh" in summary
