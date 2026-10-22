#!/usr/bin/env python3
"""Liquidity-rewards maker.

    python rewards_run.py           dry run: pick markets, print the quotes
    python rewards_run.py --live    rest real two-sided bids (PM_PRIVATE_KEY)

Shares the wallet with run.py: do not run both live on the same funds.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from pm5 import net
from pm5.clob import BookReader
from pm5.config import Config
from pm5.supabase_log import SupabaseLog
from pm5.rewards import (
    MarketQuoter, book_daily, capital_needed, fetch_reward_markets, fill_market_details,
    parse_markets, quote_distance_c, quote_prices, rank,
)

log = logging.getLogger("pm5.rewards")
_LOCK = Path("data/rewards.lock")


def _lock():
    _LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = _LOCK.open("a+b")
    if fh.tell() == 0:
        fh.write(b"\0")
        fh.flush()
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"Another rewards maker is running ({_LOCK.resolve()}).", file=sys.stderr)
        sys.exit(1)
    return fh


STICKY = 1.5  # a market we already quote must be beaten by this factor


def choose(cfg: Config, http: httpx.Client, budget: float, reader: BookReader,
           current: dict | None = None):
    """Best markets by expected USDC/day against their live books.
    `current` = {condition_id: MarketQuoter} we already quote."""
    current = current or {}
    raw = fetch_reward_markets(cfg.clob_url, http)
    markets = parse_markets(raw)
    scored, tried = [], 0
    for m in rank(cfg, markets):
        if tried >= 60:
            break
        if capital_needed(cfg, m) > budget + 1e-9:
            continue
        tried += 1
        q = current.get(m.condition_id)
        if q is not None:
            m = q.m
        elif not fill_market_details(cfg.clob_url, m, http):
            continue
        top = reader.top(m.yes_token)
        daily = book_daily(cfg, m, top, q.mine() if q is not None else ())
        if daily <= 0:
            continue
        scored.append((daily * (STICKY if q is not None else 1.0), daily, m))
    scored.sort(key=lambda x: x[0], reverse=True)
    out, left = [], budget
    for _, daily, m in scored:
        if len(out) >= cfg.rw_max_markets:
            break
        need = capital_needed(cfg, m)
        if need <= left + 1e-9:
            out.append(m)
            left -= need
            log.info("pick %s | ~$%.2f/day vs the book", m.question[:60], daily)
    log.info("%d reward markets, %d with books checked, %d picked for $%.2f",
             len(markets), len(scored), len(out), budget)
    return out


def describe(cfg: Config, reader: BookReader, markets) -> None:
    now = datetime.now(timezone.utc)
    for m in markets:
        top = reader.top(m.yes_token)
        q = quote_prices(cfg, m, top.best_bid, top.best_ask)
        log.info(
            "%s\n    pool $%.0f/day | band Â±%.1fc | min %g sh | competition %.1f | "
            "%.0f days left | est $%.2f/day | needs $%.2f | book %s/%s | quote %s",
            m.question[:90], m.rate_per_day, m.max_spread, m.min_size, m.competition,
            m.days_left(now) or -1, book_daily(cfg, m, top), capital_needed(cfg, m),
            top.best_bid, top.best_ask,
            (f"BUY yes @{q[1]:.3f} + BUY no @{q[2]:.3f} (mid {q[0]:.3f}, "
             f"{quote_distance_c(cfg, m):.1f}c out)") if q else "none",
        )


def _pair_profit(q: MarketQuoter) -> float:
    """Edge locked in completed pairs: each yes+no pair redeems for $1."""
    pairs = min(q.yes.shares, q.no.shares)
    if pairs <= 0:
        return 0.0
    return pairs * (1.0 - q.yes.cost / q.yes.shares - q.no.cost / q.no.shares)


def _record_fill(sb: SupabaseLog, day_book: dict[str, dict], q: MarketQuoter, f) -> None:
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    d = day_book.setdefault(day, {"yes": 0.0, "no": 0.0, "cost": 0.0, "n": 0})
    d[f.side] = d.get(f.side, 0.0) + f.size
    d["cost"] += f.cost
    d["n"] += 1
    sb.fill({
        "ts": now.isoformat(), "mode": "live", "window": f"rewards-{day}",
        "strategy": "rewards", "side": f.side, "price": f.price, "shares": f.size,
        "cost": f.cost, "order_id": f.order_id, "maker": True,
    })


def run_live(cfg: Config, http: httpx.Client, reader: BookReader) -> None:
    from pm5.live import LiveTrader

    cfg.ws_user = False  # poll over HTTP; a few bids do not need the stream
    trader = LiveTrader(cfg)
    if trader.geoblocked:
        log.error("geoblocked: not quoting")
        return
    bal, _ = trader.sync_collateral()
    budget = min(cfg.rw_budget, bal) if bal is not None else cfg.rw_budget
    markets = choose(cfg, http, budget, reader)
    describe(cfg, reader, markets)
    quoters = {m.condition_id: MarketQuoter(cfg, m, trader, reader) for m in markets}
    sb = SupabaseLog()
    seen: dict[str, int] = {}
    day_book: dict[str, dict] = {}
    next_report = next_pick = time.monotonic()
    next_pick += cfg.rw_repick_secs
    try:
        while True:
            for q in list(quoters.values()):
                try:
                    q.step()
                except Exception as e:  # noqa: BLE001
                    log.exception("step %s failed: %s", q.label, e)
                for f in q.fills[seen.get(q.m.condition_id, 0):]:
                    _record_fill(sb, day_book, q, f)
                seen[q.m.condition_id] = len(q.fills)
            now = time.monotonic()
            if now >= next_report:
                next_report = now + cfg.rw_report_secs
                ids = [i for q in quoters.values() for i in q.live_ids()]
                scoring = trader.orders_scoring(ids)
                pct = trader.reward_percentages()
                day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                earned = trader.reward_earnings(day)
                for q in quoters.values():
                    mine = q.live_ids()
                    log.info("%s | scoring %d/%d | share %.2f%%",
                             q.summary(), sum(1 for i in mine if scoring.get(i)), len(mine),
                             pct.get(q.m.condition_id, 0.0))
                log.info("rewards credited today (UTC %s): %s", day,
                         f"${earned:.4f}" if earned is not None else "?")
                d = day_book.get(day, {"yes": 0.0, "no": 0.0, "cost": 0.0, "n": 0})
                sb.rewards_day(day, earned or 0.0, d["yes"], d["no"], d["cost"], d["n"],
                               sum(_pair_profit(q) for q in quoters.values()))
            if now >= next_pick:
                next_pick = now + cfg.rw_repick_secs
                held = {cid for cid, q in quoters.items()
                        if q.yes.shares > 0 or q.no.shares > 0}
                fresh = {m.condition_id: m
                         for m in choose(cfg, http, budget, reader, current=quoters)}
                for cid in list(quoters):
                    if cid not in fresh and cid not in held:
                        quoters.pop(cid).pull_all("dropped at re-pick", wait=True)
                for cid, m in fresh.items():
                    if cid not in quoters and len(quoters) < cfg.rw_max_markets:
                        quoters[cid] = MarketQuoter(cfg, m, trader, reader)
            time.sleep(cfg.rw_poll_secs)
    finally:
        for q in quoters.values():
            q.pull_all("shutdown", wait=True)
            log.info("final: %s", q.summary())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="place real orders")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s", datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    net.bootstrap()
    cfg = Config()
    http = httpx.Client(timeout=30)
    reader = BookReader(cfg.clob_url, http)
    if not args.live:
        describe(cfg, reader, choose(cfg, http, cfg.rw_budget, reader))
        return
    if not cfg.private_key:
        sys.exit("--live needs PM_PRIVATE_KEY")
    lock = _lock()
    try:
        run_live(cfg, http, reader)
    except KeyboardInterrupt:
        log.info("stopped by user")
    finally:
        lock.close()


if __name__ == "__main__":
    main()
