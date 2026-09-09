# Polymarket 5-Minute BTC Up/Down Bot

A trading bot for Polymarket's **"Bitcoin Up or Down"** 5-minute binary markets.
Every 300 seconds a new market opens that resolves **Up** if the Chainlink
BTC/USD price at the window's close is ≥ its price at the open, otherwise
**Down**. Each side is a token that pays \$1.00 if it wins, \$0.00 if it loses.

The bot ships with two strategies and three run modes: **signal** (default —
prints a loud terminal alert so you click in the Polymarket browser yourself),
**paper** (simulates fills, no money), and **live** (places real orders).

> ⚠️ **Risk warning.** These markets are essentially a coin-flip on short-term
> BTC noise, with a bid/ask spread and (sometimes) fees working against you.
> Paper-trade first. Only ever fund a wallet with money you can afford to lose.
> This software is provided as-is, with no warranty. Nothing here is financial
> advice.

---

## How it works

```
┌─────────────────┐   slug = btc-updown-5m-{window_start}
│  Gamma API      │──────────────────────────────► market discovery (markets.py)
│  (HTTP, public) │   → condition id, Up/Down token ids, tick size
└─────────────────┘

┌─────────────────────────┐  topic: crypto_prices_chainlink, symbol btc/usd
│  ws-live-data.polymarket│──────────────────────► live price feed (pricefeed.py)
│  (Chainlink stream, WS) │  ~1 tick/sec; THIS is the resolution source
└─────────────────────────┘

┌─────────────────┐  GET /book?token_id=...
│  CLOB API       │──────────────────────────────► order book reads (clob.py)
│  (HTTP, public) │  best bid / best ask per side
└─────────────────┘

┌─────────────────┐  signed FOK market orders (live mode only)
│  CLOB API       │◄──────────────────────────────  execution (live.py)
│  py-clob-client │
└─────────────────┘
```

Main loop (`pm5/bot.py`), once per 5-minute window:

1. **Discover** the current market from the Gamma API (deterministic slug).
2. Check whether we **witnessed the open** — i.e. the price feed was already
   streaming before the window started. If not, the window is treated as
   *arbitrage-only* (we can't trust a momentum signal without the true open).
3. Each second:
   - **Arbitrage** — if `ask(Up) + ask(Down) ≤ 1 − edge`, buy both sides for a
     risk-free payout. Preferred whenever available.
   - **Maker pair** (`PM_MAKER=true`) — shortly after the open, rest post-only
     bids on *both* Up and Down below the mid (e.g. 0.46 + 0.46). Makers pay no
     fee. If both get hit the \$1 payout is locked at a discount; if only one
     is hit we hold a cheap directional leg and try to complete the pair; if
     the other side never comes back we sell the filled leg rather than hold
     a coin flip. A sudden Chainlink move vs the open cancels the unfilled
     bid on the side about to be dumped. Unfilled bids are also cancelled at
     `PM_MAKER_CANCEL_LEFT`. This is the "trade in every window" strategy.
   - **Momentum** — only in the closing seconds, and only if we witnessed the
     open. The market settles on the **Chainlink 60s TWAP** at close vs. the
     open snapshot, so the bot projects where that TWAP lands and how far BTC
     would have to *average* away from here for the rest of the window to flip
     the result (`PM_MIN_FLIP_USD`). Only then does it buy the winning side.
4. At window close, **settle** (paper mode) against the 60s TWAP and log PnL.
   Paper taker fills are charged Polymarket's crypto taker fee
   (`7% × p × (1−p)` per share) so paper results aren't flattering.

### Why the Chainlink feed (not Binance)

The market resolves against **Chainlink's BTC/USD data stream**, not Binance or
spot. The bot subscribes to exactly that stream so its notion of "the price"
matches the resolution source. The subscription shape lives in
`pm5/pricefeed.py`.

---

## Setup

Requires Python 3.9+ (developed on 3.14).

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env       # then edit .env
```

Run in **signal mode** (default — no credentials needed). The bot watches the
live market and, when a strategy fires, prints a loud instruction such as
`BUY UP` / `BUY DOWN` plus the Polymarket URL. You place the trade yourself.

```bash
.venv/bin/python run.py
```

You'll see live market discovery, the window open price, alerts when to click,
and (after each window) what the PnL *would* have been if you followed it.

To simulate fills instead of alerting, set `PM_MODE=paper` in `.env`.

### Paper bankroll

Paper mode runs against a simulated bankroll (`PM_PAPER_BANKROLL`, default
\$100). Each trade debits its cost; each winning settlement credits the payout
back. When the bankroll can no longer fund a trade, **the bot stops
automatically** (`paper bankroll exhausted`). Set `PM_PAPER_BANKROLL=0` for an
unlimited bankroll (never stops on funds). The optional `PM_DAILY_LOSS_LIMIT`
(default 0 = off) is a separate cap that stops the bot after that much
cumulative loss; whichever triggers first wins.

### Recorded data

Every run appends records to `data/trades.jsonl` (one JSON object per line,
gitignored) for later study:

- `fill` — each executed trade, with the market context at entry (strategy,
  side, price, shares, cost, BTC price, window open, Δ, seconds left, signal).
- `window` — each 5-minute window's outcome at settlement (open, close, winner,
  whether it was witnessed, PnL), written even for windows with no trade — so
  you can study what a strategy *would* have done. Includes a `path`: per-second
  snapshots over the last `PM_PATH_SECS` (default 120s, the decision zone), each
  `{t, btc, up, dn}` = seconds-to-close, Chainlink price, and the up/down best
  asks. This lets you backtest both the entry *direction* (what the price showed
  at T-30s) and the *PnL* (the ask you'd have paid). Set `PM_PATH_SECS=0` to omit.

Load it for analysis:

```python
import pandas as pd
df = pd.read_json("data/trades.jsonl", lines=True)
fills, windows = df[df.type == "fill"], df[df.type == "window"]
```

Disable with `PM_RECORD=false`, or change the path with `PM_DATA_FILE`.

### Live status line

While running in a terminal, a status line redraws once per second at the
bottom of the screen:

```
9:00AM-9:05AM ET  T- 35s  BTC 63,361.3  Δ -11.5  up 0.41/0.43 dn 0.57/0.59  pos DOWN 8.5sh  sess -5.00
```

That's the current window, seconds to close, the live Chainlink BTC price, its
move since the window open (`Δ`, green up / red down, or `open?` when the bot
started mid-window and didn't witness the open), the best bid/ask on each side,
your open position, and session PnL. Event logs (signals, fills, settlement)
scroll above it. Set `PM_LIVE_STATUS=false` to turn it off; it also auto-disables
when output is piped to a file.

---

## Going live

1. Fund a Polygon wallet with **USDC.e** and a little MATIC/POL for gas. If you
   deposit via the Polymarket website (email or browser wallet), your funds sit
   in a **proxy** — set `PM_FUNDER_ADDRESS` to that deposit address and
   `PM_SIGNATURE_TYPE` to `1` (email/magic) or `2` (browser). If you trade
   directly from an EOA, use `PM_SIGNATURE_TYPE=0` and leave the funder blank.
2. Put the wallet's **private key** in `.env` as `PM_PRIVATE_KEY`. The `.env`
   file is gitignored — never commit it.
3. Set `PM_MODE=live`. Start with a tiny `PM_STAKE_USDC` (e.g. 1) and confirm
   the first orders behave before sizing up.

In live mode the bot derives L2 API credentials from your key on startup and
places **Fill-or-Kill** marketable buy orders capped at a protective limit
price. Positions settle on-chain when the market resolves.

A second `run.py` process is refused (`data/bot.lock`) so two copies cannot
trade the same wallet at once.

---

## Deploy 24/7 on Railway

This is a **long-running worker**, not a website. Do **not** deploy to Vercel.

1. Push this repo to GitHub (never commit `.env`).
2. In [Railway](https://railway.app): **New project → Deploy from GitHub repo**.
3. Add a service from the repo. Railway will build the `Dockerfile`.
4. Under **Variables**, copy every `PM_*` key from `.env.example`. For live:

   | Variable | Typical live value |
   |---|---|
   | `PM_MODE` | `live` |
   | `PM_PRIVATE_KEY` | your key (Railway secret, not git) |
   | `PM_SIGNATURE_TYPE` | `3` for current Polymarket email/deposit wallets |
   | `PM_FUNDER_ADDRESS` | your Polymarket profile / API address |
   | `PM_STAKE_USDC` | `5` |
   | `PM_MAKER` | `true` |
   | `PM_MAKER_BID` | `0.46` |
   | `PM_MAKER_STAKE_USDC` | `5` |
   | `PM_MAKER_PAIR_GRACE` | `5` |
   | `PM_MAKER_PAIR_MAX_SUM` | `1.00` |
   | `PM_MAKER_PAIR_HARD` | `20` |
   | `PM_MAKER_PAIR_HARD_SUM` | `1.00` |
   | `PM_MAKER_DEFENSIVE_USD` | `20` |
   | `PM_MAKER_EXIT_SECS` | `20` |
   | `PM_MAKER_EXIT_MIN_BID` | `0.10` |
   | `PM_MAKER_HEDGE_MAX` | `0.60` |
   | `PM_MOMENTUM` | `true` |
   | `PM_MIN_PRICE` | `0.50` |
   | `PM_MAX_PRICE` | `0.85` |
   | `PM_MIN_DELTA_USD` | `25` |
   | `PM_ARB` | `false` |
   | `PM_LIVE_STATUS` | `false` |
   | `SUPABASE_URL` | project URL (optional; bot writes settled P&L) |
   | `SUPABASE_SECRET_KEY` | `sb_secret_…` (Railway secret, not git) |

   Run **only one** live worker. Stop any local `run.py` first or you will double-trade.

5. Use an **always-on** worker (no sleep). The bot service has no HTTP port.
6. After deploy, logs should show `starting in LIVE mode` and `price feed connected`.
7. **Dashboard (optional second service):** same image, start command
   `python -u dashboard.py`. Railway sets `PORT`. Add `SUPABASE_URL` and
   `SUPABASE_SECRET_KEY`. Do **not** put the private key on this service.

If Railway offers a "web" vs **worker** process, pick worker / empty start command override so it runs `python -u run.py` from `railway.toml`.

---

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `PM_MODE` | `signal` | `signal` alerts you; `paper` simulates; `live` places real orders |
| `PM_PRIVATE_KEY` | — | Wallet private key (live only) |
| `PM_SIGNATURE_TYPE` | `0` | 0 = EOA, 1 = email proxy, 2 = browser proxy, 3 = deposit wallet |
| `PM_FUNDER_ADDRESS` | — | Proxy/deposit address for sig types 1/2/3 |
| `PM_STAKE_USDC` | `5` | USDC per momentum entry |
| `PM_MAX_PRICE` | `0.85` | Don't buy a side above this price |
| `PM_MIN_PRICE` | `0.50` | Don't buy a favored side below this (cheap = book disagrees) |
| `PM_MAX_TRADES_PER_WINDOW` | `1` | Cap entries per 5-min window |
| `PM_DAILY_LOSS_LIMIT` | `50` | Stop trading after this much paper/realized loss |
| `PM_MOMENTUM` | `true` | Enable the momentum strategy |
| `PM_DECIDE_WITHIN` | `45` | Only enter momentum within N s of close |
| `PM_STOP_ENTRY` | `3` | Stop entering N s before close |
| `PM_MIN_DELTA_USD` | `25` | Min |projected TWAP − open| (USD) for momentum |
| `PM_TWAP_SECS` | `60` | Settlement TWAP window (per the market rules) |
| `PM_MIN_FLIP_USD` | `20` | BTC must need to average ≥ this far away to flip the result |
| `PM_MAKER` | `false` | Enable the maker-pair strategy (a trade every window) |
| `PM_MAKER_BID` | `0.46` | Resting bid price on each side (pair costs 2× this) |
| `PM_MAKER_STAKE_USDC` | `5` | USDC per side for the resting bids |
| `PM_MAKER_START` | `10` | Post bids this many seconds after the open |
| `PM_MAKER_CANCEL_LEFT` | `75` | Cancel unfilled bids with this many seconds left |
| `PM_MAKER_HEDGE_MAX` | `0.60` | Hedge a losing lone leg up to this ask (0 = never) |
| `PM_MAKER_PAIR_GRACE` | `5` | Seconds naked before allowing pair sum ≤ MAX_SUM |
| `PM_MAKER_PAIR_MAX_SUM` | `1.00` | Close the pair up to this fill+ask+fee after grace |
| `PM_MAKER_PAIR_HARD` | `20` | Seconds naked before allowing pair sum ≤ HARD_SUM |
| `PM_MAKER_PAIR_HARD_SUM` | `1.00` | Last-resort pair close (all-in, including taker fee) |
| `PM_MAKER_DEFENSIVE_USD` | `20` | Cancel the unfilled dumped-side bid after this BTC move vs open (0 = off) |
| `PM_MAKER_EXIT_SECS` | `20` | Seconds naked before selling the filled leg if the pair cannot close (0 = off) |
| `PM_MAKER_EXIT_MIN_BID` | `0.10` | Don't sell-to-exit into a bid below this |
| `SUPABASE_URL` | — | Optional P&L logging |
| `SUPABASE_SECRET_KEY` | — | Secret key; backend only |
| `PM_TAKER_FEE_RATE` | `0.07` | Crypto taker fee rate used for paper fills |
| `PM_ARB` | `true` | Enable the arbitrage strategy |
| `PM_ARB_MIN_EDGE` | `0.02` | Required `1 − (ask_up+ask_down)` edge |
| `PM_ARB_STAKE_USDC` | `5` | USDC per side for arbitrage |
| `PM_POLL_INTERVAL` | `1` | Seconds between checks |
| `PM_LOG_LEVEL` | `INFO` | Logging verbosity |

---

## Project layout

```
pm5/
  net.py        DNS bootstrap (DoH fallback) for Polymarket hosts
  config.py     Env-driven configuration
  markets.py    Gamma market discovery (deterministic slug)
  pricefeed.py  Chainlink BTC/USD websocket feed
  clob.py       Order-book reads + executor (paper/live dispatch)
  live.py       py-clob-client order placement (live only)
  strategy.py   Momentum + arbitrage signals
  bot.py        Main per-window trading loop + paper settlement
run.py          Entry point
tests/          Offline unit tests for the strategy/settlement logic
```

### A note on DNS

`pm5/net.py` resolves the Polymarket hostnames and, **only if the local
resolver fails to resolve them**, falls back to DNS-over-HTTPS (Cloudflare) for
those specific hosts. Traffic still goes directly over TLS with the correct
SNI; this just works around a filtered/broken local DNS. If your DNS resolves
`gamma-api.polymarket.com` fine, the fallback never triggers.

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

These cover slug/window math, paper settlement (including the risk-free
arbitrage payout), and the momentum/arbitrage signal gating — all offline, no
network.
