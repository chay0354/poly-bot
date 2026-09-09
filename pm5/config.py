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
    # Minimum |price - open| (in USD) before momentum will fire.
    min_delta_usd: float = field(default_factory=lambda: _f("PM_MIN_DELTA_USD", 25.0))

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
