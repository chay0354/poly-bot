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
    MarketQuoter, RewardMarket, book_daily, capital_needed, fetch_reward_markets, fill_market_details,
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
    for m in out:  # best first: leftover budget makes its bids bigger
        per_share = capital_needed(cfg, m) / m.size
        cap = m.min_size * max(cfg.rw_max_size_mult, 1.0)
        extra = min(cap - m.size, left / per_share) if per_share > 0 else 0.0
        if extra >= 1:
            m.quote_size = m.size + int(extra)
            left -= int(extra) * per_share
            log.info("size %s → %g sh", m.question[:60], m.quote_size)
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


def _free_budget(cfg: Config, trader, quoters: dict) -> float:
    """Cash for new two-sided quotes: the CLOB balance (which still counts our
    resting bids) minus what pairing-only bids need, capped by the budget."""
    bal, _ = trader.sync_collateral()
    if bal is None:
        return 0.0
    reserved = 0.0
    for q in quoters.values():
        if not q.pair_only:
            continue
        lone, short = (q.yes, q.no) if q.yes.shares > q.no.shares else (q.no, q.yes)
        if lone.shares > 0:
            reserved += (lone.shares - short.shares) * max(0.0, 1 - lone.cost / lone.shares)
    free = max(0.0, min(cfg.rw_budget, bal - reserved))
    log.info("cash $%.2f, pairing bids need $%.2f, free for quoting $%.2f", bal, reserved, free)
    return free


def load_positions(cfg: Config, http: httpx.Client, trader, reader: BookReader) -> dict:
    """Pairing-only quoters for lone legs held from an earlier run, so a
    restart does not forget inventory."""
    user = cfg.funder_address or getattr(trader, "address", "")
    out: dict = {}
    if not user:
        return out
    try:
        r = http.get("https://data-api.polymarket.com/positions",
                     params={"user": user, "sizeThreshold": 1, "limit": 500})
        r.raise_for_status()
        rows = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("positions read failed: %s", e)
        return out
    by_cid: dict[str, list] = {}
    for p in rows:
        if p.get("redeemable") or float(p.get("curPrice") or 0) in (0.0, 1.0):
            continue
        by_cid.setdefault(p.get("conditionId"), []).append(p)
    for cid, ps in by_cid.items():
        try:
            j = http.get(f"{cfg.clob_url}/markets/{cid}").json()
        except (httpx.HTTPError, ValueError):
            continue
        toks = j.get("tokens") or []
        rw = j.get("rewards") or {}
        if len(toks) != 2 or j.get("closed") or not rw.get("rates"):
            continue
        yes = next((t for t in toks if str(t.get("outcome", "")).lower() == "yes"), toks[0])
        no = toks[1] if yes is toks[0] else toks[0]
        m = RewardMarket(
            condition_id=cid, question=str(j.get("question") or ""),
            yes_token=str(yes["token_id"]), no_token=str(no["token_id"]),
            mid=float(yes.get("price") or 0.5), max_spread=float(rw.get("max_spread") or 0),
            min_size=float(rw.get("min_size") or 0),
            rate_per_day=sum(float(x.get("rewards_daily_rate") or 0) for x in rw["rates"]),
            competition=0.0, end=None, tick=float(j.get("minimum_tick_size") or 0.01),
            neg_risk=bool(j.get("neg_risk")),
        )
        q = MarketQuoter(cfg, m, trader, reader, pair_only=True)
        for p in ps:
            side = q.yes if str(p.get("asset")) == m.yes_token else q.no
            side.shares += float(p.get("size") or 0)
            side.cost += float(p.get("size") or 0) * float(p.get("avgPrice") or 0)
        # The wallet may hold the owner's own positions: only adopt legs no
        # bigger than one of our bids could have bought.
        biggest = m.min_size * max(cfg.rw_max_size_mult, 1.0) + 1e-6
        if max(q.yes.shares, q.no.shares) > biggest:
            log.info("not ours (too big), leaving alone: %s", q.label)
            continue
        if abs(q.yes.shares - q.no.shares) >= 1:
            out[cid] = q
            log.info("holding %s | yes %.1f sh $%.2f no %.1f sh $%.2f", q.label,
                     q.yes.shares, q.yes.cost, q.no.shares, q.no.cost)
    return out


def run_live(cfg: Config, http: httpx.Client, reader: BookReader) -> None:
    from pm5.live import LiveTrader

    cfg.ws_user = False  # poll over HTTP; a few bids do not need the stream
    trader = LiveTrader(cfg)
    if trader.geoblocked:
        log.error("geoblocked: not quoting")
        return
    quoters = load_positions(cfg, http, trader, reader)
    markets = choose(cfg, http, _free_budget(cfg, trader, quoters), reader)
    describe(cfg, reader, markets)
    for m in markets:
        q = quoters.get(m.condition_id)
        if q is not None:
            q.pair_only = False
        else:
            quoters[m.condition_id] = MarketQuoter(cfg, m, trader, reader)
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
                active = {cid: q for cid, q in quoters.items() if not q.pair_only}
                fresh = {m.condition_id: m for m in choose(
                    cfg, http, _free_budget(cfg, trader, quoters), reader, current=active)}
                for cid, q in list(quoters.items()):
                    if cid in fresh:
                        q.pair_only = False
                    elif abs(q.yes.shares - q.no.shares) >= 1:
                        if not q.pair_only:
                            log.info("%s: dropped at re-pick; only pairing its lone leg", q.label)
                        q.pair_only = True
                    else:
                        quoters.pop(cid).pull_all("dropped at re-pick", wait=True)
                for cid, m in fresh.items():
                    if cid not in quoters:
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
