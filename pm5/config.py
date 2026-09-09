"""Configuration loaded from environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    # --- Mode ---
    # signal = shout a browser instruction (no orders, no wallet).
    # paper  = simulate fills against the live order book, no real money.
    # live   = sign and post real orders via the CLOB.
    mode: str = field(default_factory=lambda: os.getenv("PM_MODE", "signal").lower())

    # --- Wallet / API (only needed in live mode) ---
    private_key: str = field(default_factory=lambda: os.getenv("PM_PRIVATE_KEY", ""))
    # Proxy/funder address for Polymarket email or browser wallets. Leave blank
    # when trading directly from an EOA.
    funder_address: str = field(default_factory=lambda: os.getenv("PM_FUNDER_ADDRESS", ""))
    # Signature type: 0 = EOA, 1 = email/magic proxy, 2 = browser (Gnosis) proxy.
    signature_type: int = field(default_factory=lambda: _i("PM_SIGNATURE_TYPE", 0))

    chain_id: int = 137
    clob_url: str = "https://clob.polymarket.com"
    gamma_url: str = "https://gamma-api.polymarket.com"
    ws_live_url: str = "wss://ws-live-data.polymarket.com"

    # --- Risk / sizing ---
    stake_usdc: float = field(default_factory=lambda: _f("PM_STAKE_USDC", 5.0))
    max_price: float = field(default_factory=lambda: _f("PM_MAX_PRICE", 0.85))
    # Momentum floor: a favored side offered *below* this in the closing seconds
    # means the order book disagrees with our feed (the ask is cheap because the
    # market already thinks that side is losing). Live fills at 0.01-0.32 all
    # resolved against us; refuse them instead of treating cheap as good.
    min_price: float = field(default_factory=lambda: _f("PM_MIN_PRICE", 0.50))
    max_trades_per_window: int = field(
        default_factory=lambda: _i("PM_MAX_TRADES_PER_WINDOW", 1)
    )
    # Simulated paper bankroll (USDC). The bot stops when it can no longer fund a
    # trade. 0 = unlimited (no bankroll stop). Paper mode only.
    paper_bankroll: float = field(default_factory=lambda: _f("PM_PAPER_BANKROLL", 100.0))
    # Optional extra cap: stop after this much cumulative loss. 0 = disabled
    # (the bankroll above is the primary stop).
    daily_loss_limit_usdc: float = field(
        default_factory=lambda: _f("PM_DAILY_LOSS_LIMIT", 0.0)
    )

    # --- Momentum strategy ---
    momentum_enabled: bool = field(default_factory=lambda: _b("PM_MOMENTUM", True))
    # Enter only inside the last N seconds of the window.
    decide_within_secs: float = field(default_factory=lambda: _f("PM_DECIDE_WITHIN", 45.0))
    # Hard cutoff: stop entering this many seconds before close.
    stop_entry_secs: float = field(default_factory=lambda: _f("PM_STOP_ENTRY", 3.0))
    # Minimum |projected settlement TWAP - open| (in USD) before momentum fires.
    min_delta_usd: float = field(default_factory=lambda: _f("PM_MIN_DELTA_USD", 25.0))
    # The market settles on the Chainlink TWAP over the final N seconds of the
    # window (not the last tick). Part of that window is already locked in when
    # we decide; the rest is unknown.
    twap_secs: float = field(default_factory=lambda: _f("PM_TWAP_SECS", 60.0))
    # Momentum also requires that BTC would have to *average* at least this many
    # USD away from its current price over the remaining seconds to flip the
    # outcome. Grows fast as the TWAP window locks in, so this is what makes a
    # late entry safe rather than a guess.
    min_flip_usd: float = field(default_factory=lambda: _f("PM_MIN_FLIP_USD", 20.0))

    # --- Maker-pair strategy (a trade in every window) ---
    # Rest post-only bids on BOTH Up and Down early in the window, priced so
    # that the pair sums below $1. Makers pay 0% fee (takers pay 7% × p(1-p))
    # and earn rebates. If both bids fill the $1 payout is locked in at a
    # profit; if only one fills we hold a cheap directional position and may
    # hedge it in the closing seconds if the TWAP projection turns against us.
    maker_enabled: bool = field(default_factory=lambda: _b("PM_MAKER", False))
    maker_bid: float = field(default_factory=lambda: _f("PM_MAKER_BID", 0.46))
    maker_stake_usdc: float = field(default_factory=lambda: _f("PM_MAKER_STAKE_USDC", 5.0))
    # Post the bids this many seconds after the window opens (let the book form).
    maker_start_secs: float = field(default_factory=lambda: _f("PM_MAKER_START", 10.0))
    # Cancel whatever is still unfilled when this many seconds are left.
    maker_cancel_left_secs: float = field(default_factory=lambda: _f("PM_MAKER_CANCEL_LEFT", 75.0))
    # If exactly one side filled and the TWAP projection says it is losing,
    # buy the other side (taker) up to this ask to cap the loss. 0 = never hedge.
    maker_hedge_max_price: float = field(default_factory=lambda: _f("PM_MAKER_HEDGE_MAX", 0.60))
    # Leftover maker bid gets first chance. Taker-complete only after that
    # bid is gone, and only when avg + ask + taker_fee <= the cap (1.00 is
    # a true lock; anything above is a known loss — prefer sell-to-exit).
    maker_pair_grace_secs: float = field(default_factory=lambda: _f("PM_MAKER_PAIR_GRACE", 5.0))
    maker_pair_max_sum: float = field(default_factory=lambda: _f("PM_MAKER_PAIR_MAX_SUM", 1.00))
    maker_pair_hard_secs: float = field(default_factory=lambda: _f("PM_MAKER_PAIR_HARD", 20.0))
    maker_pair_hard_sum: float = field(default_factory=lambda: _f("PM_MAKER_PAIR_HARD_SUM", 1.00))
    # Pull the unfilled bid on the side BTC just moved against (that side is
    # about to be dumped into our rest). 0 = never. Measured vs the witnessed
    # window open (same feed the market settles on).
    maker_defensive_usd: float = field(default_factory=lambda: _f("PM_MAKER_DEFENSIVE_USD", 20.0))
    # Still naked after this many seconds *and* the pair cannot close at the
    # hard cap: sell the filled leg at the bid instead of holding $stake to
    # resolution. 0 = never sell-to-exit.
    maker_exit_secs: float = field(default_factory=lambda: _f("PM_MAKER_EXIT_SECS", 20.0))
    # Don't dump into a dust bid; hold if the best bid is below this.
    maker_exit_min_bid: float = field(default_factory=lambda: _f("PM_MAKER_EXIT_MIN_BID", 0.10))

    # --- Fees ---
    # Polymarket crypto taker fee: shares × rate × p × (1-p). Makers pay 0.
    # Applied to paper fills so paper P&L is honest.
    taker_fee_rate: float = field(default_factory=lambda: _f("PM_TAKER_FEE_RATE", 0.07))

    # --- Arbitrage strategy ---
    arb_enabled: bool = field(default_factory=lambda: _b("PM_ARB", False))
    # Buy both sides when (ask_up + ask_down) <= 1 - this edge (after fees).
    arb_min_edge: float = field(default_factory=lambda: _f("PM_ARB_MIN_EDGE", 0.02))
    arb_stake_usdc: float = field(default_factory=lambda: _f("PM_ARB_STAKE_USDC", 5.0))

    # --- Engine ---
    poll_interval_secs: float = field(default_factory=lambda: _f("PM_POLL_INTERVAL", 1.0))
    log_level: str = field(default_factory=lambda: os.getenv("PM_LOG_LEVEL", "INFO"))
    # Live, in-place status line. Auto-disables when stdout isn't a TTY.
    live_status: bool = field(default_factory=lambda: _b("PM_LIVE_STATUS", True))
    # Append trade + window records to a JSONL file for later study.
    record: bool = field(default_factory=lambda: _b("PM_RECORD", True))
    data_file: str = field(default_factory=lambda: os.getenv("PM_DATA_FILE", "data/trades.jsonl"))
    # Record the BTC price + up/down asks each second over the last N seconds of
    # a window (the decision zone), so the strategy can be backtested. 0 = off.
    path_secs: float = field(default_factory=lambda: _f("PM_PATH_SECS", 120.0))

    def require_live_creds(self) -> None:
        if self.mode == "live" and not self.private_key:
            raise SystemExit(
                "PM_MODE=live but PM_PRIVATE_KEY is not set. Refusing to start.\n"
                "Set credentials in .env or switch to PM_MODE=paper."
            )
