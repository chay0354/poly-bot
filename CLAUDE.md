# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A trading bot for Polymarket's 5-minute "Bitcoin Up or Down" binary markets. A new
market opens every 300s; it resolves **Up** if the Chainlink BTC/USD price at the
window's close is ≥ its price at the open. Two strategies run: end-of-window
**momentum** and cross-side **arbitrage**. Ships with a **paper** mode (simulates
against live order books, no money) and a **live** mode.

## Commands

```bash
# One-time setup
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# Run (paper mode is the default — no credentials needed)
.venv/bin/python run.py

# Windows: creates .venv if missing, UTF-8 console, restarts on crash
.\start.ps1          # mode per .env
.\start.ps1 -Paper   # force paper

# Tune behavior with env vars inline (see .env.example for all knobs)
PM_MIN_DELTA_USD=3 PM_DECIDE_WITHIN=60 .venv/bin/python run.py

# Tests (all offline, no network)
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m pytest tests/test_logic.py::test_arb_execute_aborts_when_one_leg_moved -q
```

Live mode: set `PM_MODE=live` plus `PM_PRIVATE_KEY` (and `PM_SIGNATURE_TYPE` /
`PM_FUNDER_ADDRESS` for proxy wallets) in `.env`. `run.py` refuses to start in
live mode without a key.

## Architecture

`pm5/bot.py` is the orchestrator. `Bot.run()` loops one **window** at a time; each
window (`_trade_window`) wires the pieces together:

- **Discovery** (`markets.py`) — markets are found by a *deterministic slug*
  `btc-updown-5m-{window_start}` (`window_start = now - now % 300`), never by
  search. One Gamma API call yields the Up/Down `clobTokenIds`, condition id, tick
  size.
- **Price feed** (`pricefeed.py`) — a background asyncio task streams the Chainlink
  BTC/USD websocket. **This is the market's resolution source**, so it's the right
  feed to compare against the open. It runs continuously across windows.
- **Signals** (`strategy.py`) — each poll, `_pick_signal` tries **arbitrage first**
  (risk-free), then momentum. Arbitrage uses the order book (`clob.py`); momentum
  uses the feed.
- **Books + fills over WebSocket** (`clobws.py`) — `MarketStream` keeps live
 order books for the tokens the bot `watch()`es (full `book` snapshot, then
 `price_change` deltas); `BookReader.top` reads from it and only falls back to
 HTTP `/book` when the stream is not healthy or has no snapshot. `UserStream`
 (auth, live mode) carries our own order events so `LiveTrader.poll_bid` sees
 fills in ms and only reconciles over HTTP every few seconds. With both books
 off the socket the loop runs at `PM_FAST_POLL` (0.25s) instead of
 `PM_POLL_INTERVAL`. The next window's books are subscribed ~20s early.
- **Execution** (`clob.py` → `live.py`) — `Executor.buy` simulates in paper mode or
 delegates to `LiveTrader` in live mode. `Executor.sell` walks the bid depth
 (`BookTop.sell_plan`) and the live order is a **FAK** down to that price — a
 thin top level must never block an exit (two $5 legs were carried to a $0
 resolution on 10 Sep because of a "top size < shares" refusal).
- **Settlement** — the market resolves on the **Chainlink 60s TWAP** at close vs.
 the open snapshot (rules changed Aug 2026), so paper positions settle against
 `feed.twap(end-60, end)`, and momentum decides on `feed.projected_close(...)`
 plus its `flip_needed` margin — not on the last tick. Live positions settle
 on-chain.
- **Maker pair** (`maker.py`, `PM_MAKER`) — rests post-only GTC bids on both
  sides early in the window; paper fills are simulated when the best ask crosses
  our bid. After one fill, wait for the leftover maker bid — do not taker-buy
  the other side while it is still live, and count the 7% taker fee in any
 pair-complete cap. **Fair-value quoting** (`maker.fair_up`, `PM_MAKER_FAIR`):
 once the realized 5-min range is known, p_up = Φ(Binance Δ since open / σ of
 the move still possible); each bid rests at fair − `PM_MAKER_EDGE`, is pulled
 when fair − bid < `PM_MAKER_PULL_EDGE`, re-quoted when ≥ `PM_MAKER_REQUOTE`
 off target, and stays inside bid ± `PM_MAKER_SKEW_MAX`. A side whose target
 falls under the band is *unquotable*; with nothing filled we quote neither
 (never a lone leg) and quote again when fair returns — no stand-down. Until σ
 is known, the older fixed line (`PM_MAKER_DEFENSIVE_USD`, vol-scaled by
 `PM_MAKER_DEFENSIVE_Z`) does the same job. If a naked leg cannot pair after
 `PM_MAKER_EXIT_SECS` (or its bid is `PM_MAKER_STOP` under the fill) we sell it
 rather than hold to resolution; the sell may walk `PM_MAKER_EXIT_SLIP` under
 the top bid. `MakerPair.close()` must always run at window end (it's in a
 `finally`) so no bid is left resting into the next market.
- **Daily loss limit** (`ledger.py`, `PM_DAILY_LOSS_LIMIT`) — per-UTC-day P&L
 persisted to `data/day_pnl_{mode}.json`. Live windows are *estimated*
 (`supabase_log.estimated_pnl`: pairs pay $1, exits are realized, a held leg
 settles on the witnessed outcome). When hit, the loop sleeps to the next UTC
 day instead of exiting, because a supervisor would just restart it.
- **Paper bankroll** — paper mode tracks a simulated balance (`PM_PAPER_BANKROLL`)
  on the `Executor`: debited on each fill, credited the payout on settlement. The
  run loop stops when it can't fund the next trade (`_bankroll_exhausted`).
  `bankroll is None` means untracked (live mode or `PM_PAPER_BANKROLL=0`).
- **Recording** (`recorder.py`) — every fill and every window outcome is appended
  to `data/trades.jsonl` (gitignored) for later study. Window records are written
  even when no trade happened, and carry a `path` of per-second `{t, btc, up, dn}`
  snapshots over the last `PM_PATH_SECS` (decision zone) for backtesting.

### Load-bearing invariants (don't break these)

- **The Chainlink subscription must NOT send a server-side `filters`.** Including
  `filters:{"symbol":"btc/usd"}` silently drops *every* tick. The topic streams
  many symbols; we subscribe filter-less (`type:"update"`) and select `btc/usd`
  **client-side** in `ChainlinkFeed._ingest`. This is the single most expensive
  bug to rediscover.
- **Feed history must outlive a full window.** `ChainlinkFeed(history_secs=360)`
  is deliberately > 300s so the window-open tick is still present at settlement.
- **"Witnessed open" gating.** Momentum and paper settlement only run for windows
  where the feed was already streaming *before* the window opened
  (`feed.witnessed_open`). A bot started mid-window never saw the true open, so it
  runs **arbitrage-only** for that window. Arbitrage is book-based and always safe.
 The maker gets `witnessed=` too: in an unwitnessed window it rests only once
 Binance can give a Δ vs the open (a blind 0.46 rest at T+70s into a leaning
 book is a guaranteed adverse fill).
- **Arbitrage is all-or-none** (`_execute_arb`). It re-reads both books fresh and
  aborts unless every leg is still fillable — a half-filled arb is naked
  directional risk, the opposite of the intent.
- **`net.bootstrap()` must run before any Polymarket HTTP/WS call.** The local
  resolver here does not resolve `*.polymarket.com`; `net.py` falls back to
  Cloudflare DoH for those hosts only (resolution workaround, not a firewall
  bypass). `Bot.__init__` calls it.
- **`py_clob_client` is imported lazily, only in `live.py`.** Keep it that way so
  paper mode never needs signing code or a private key.

### Order book convention

CLOB `/book` (and the WS `book` event) return **bids ascending and asks
descending**, so the best bid/ask are both the *last* element. `BookReader.top`
normalizes both sources into a `BookTop` whose `bids`/`asks` are **best first** —
read from it, don't re-parse books elsewhere.

## Config

All runtime behavior is environment-driven via `pm5/config.py` (`Config`
dataclass), loaded from `.env`. `.env.example` documents every `PM_*` knob. The
defaults (e.g. `PM_DECIDE_WITHIN=45`) intentionally enter only in the final
seconds of a window; lowering thresholds to "see more trades" trades on noise.
