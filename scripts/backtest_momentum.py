"""Replay the momentum strategy on recently resolved 5-minute markets.

Uses official Polymarket outcomes + CLOB mid/last prices for the entry ask,
and Binance 1s BTCUSDT as a stand-in for the Chainlink path (same direction
most of the time; noted when they disagree).
"""

from __future__ import annotations

import argparse
import json
import time
from bisect import bisect_right
from datetime import datetime, timedelta, timezone

import httpx

from pm5.markets import WINDOW_SECS, current_window_start, slug_for
from pm5.net import bootstrap


def _ask_at(history: list[tuple[int, float]], ts: int) -> float | None:
    if not history:
        return None
    times = [t for t, _ in history]
    i = bisect_right(times, ts) - 1
    if i < 0:
        return history[0][1]
    return history[i][1]


def fetch_binance_1s(client: httpx.Client, start: int, end: int) -> dict[int, float]:
    prices: dict[int, float] = {}
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + 1000, end)
        r = client.get(
            "https://api.binance.com/api/v3/klines",
            params={
                "symbol": "BTCUSDT",
                "interval": "1s",
                "startTime": cursor * 1000,
                "endTime": chunk_end * 1000,
                "limit": 1000,
            },
            timeout=30,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            cursor = chunk_end + 1
            continue
        for k in rows:
            prices[int(k[0] // 1000)] = float(k[4])
        cursor = int(rows[-1][0] // 1000) + 1
        time.sleep(0.05)
    return prices


def fetch_event(client: httpx.Client, ws: int) -> dict | None:
    r = client.get(
        "https://gamma-api.polymarket.com/events",
        params={"slug": slug_for(ws)},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        return None
    return data[0]


def fetch_clob(client: httpx.Client, token_id: str, ws: int) -> list[tuple[int, float]]:
    r = client.get(
        "https://clob.polymarket.com/prices-history",
        params={"market": token_id, "startTs": ws, "endTs": ws + WINDOW_SECS, "fidelity": 1},
        timeout=20,
    )
    r.raise_for_status()
    hist = r.json().get("history") or []
    return [(int(p["t"]), float(p["p"])) for p in hist]


def replay_window(
    ws: int,
    btc: dict[int, float],
    up_hist: list[tuple[int, float]],
    down_hist: list[tuple[int, float]],
    up_won: bool,
    decide_within: float,
    stop_entry: float,
    min_delta: float,
    max_price: float,
    stake: float,
) -> dict | None:
    open_px = None
    for t in range(ws, ws + 5):
        if t in btc:
            open_px = btc[t]
            break
    if open_px is None:
        return None
    close_px = None
    for t in range(ws + WINDOW_SECS, ws + WINDOW_SECS - 5, -1):
        if t in btc:
            close_px = btc[t]
            break
    if close_px is None:
        return None

    entry = None
    start = ws + WINDOW_SECS - int(decide_within)
    end = ws + WINDOW_SECS - int(stop_entry)
    for t in range(start, end + 1):
        px = btc.get(t)
        if px is None:
            continue
        delta = px - open_px
        if abs(delta) < min_delta:
            continue
        ref = None
        for look in range(t - 10, t + 1):
            if look in btc:
                ref = btc[look]
                break
        if ref is not None:
            mom = px - ref
            if (mom > 0) != (delta > 0) and abs(mom) > min_delta / 2:
                continue
        side = "up" if delta > 0 else "down"
        ask = _ask_at(up_hist if side == "up" else down_hist, t)
        if ask is None or ask > max_price:
            continue
        shares = stake / ask
        won = (side == "up") == up_won
        entry = {
            "ws": ws,
            "t_left": ws + WINDOW_SECS - t,
            "side": side,
            "ask": ask,
            "delta": delta,
            "won": won,
            "pnl": shares * (1.0 if won else 0.0) - stake,
            "cost": stake,
        }
        break
    return {
        "ws": ws,
        "open": open_px,
        "close": close_px,
        "binance_up": close_px >= open_px,
        "up_won": up_won,
        "entry": entry,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--day", default="", help="Local calendar day YYYY-MM-DD (uses --tz).")
    p.add_argument("--tz", type=int, default=3, help="UTC offset hours for --day.")
    p.add_argument("--stake", type=float, default=5.0)
    p.add_argument("--bankroll", type=float, default=0.0)
    p.add_argument("--min-ask", type=float, default=0.0,
                    help="Ignore fills cheaper than this (stale CLOB prints).")
    p.add_argument("--out", default="")
    p.add_argument("--min-delta", type=float, default=8.0)
    p.add_argument("--max-price", type=float, default=0.85)
    p.add_argument("--decide-within", type=float, default=45.0)
    p.add_argument("--stop-entry", type=float, default=3.0)
    args = p.parse_args()

    bootstrap()
    if args.day:
        tz = timezone(timedelta(hours=args.tz))
        day0 = datetime.strptime(args.day, "%Y-%m-%d").replace(tzinfo=tz)
        first = int(day0.timestamp())
        last = int((day0 + timedelta(days=1)).timestamp())
        first -= first % WINDOW_SECS
        last -= last % WINDOW_SECS
        label = f"{args.day} UTC{args.tz:+d}"
    else:
        last = current_window_start()
        first = last - int(args.hours * 3600)
        first -= first % WINDOW_SECS
        label = f"{args.hours:.0f}h"
    windows = list(range(first, last, WINDOW_SECS))

    print(
        f"replaying {len(windows)} windows "
        f"({label}) | min_delta={args.min_delta} max_px={args.max_price} "
        f"last {args.decide_within:.0f}s"
    )

    with httpx.Client(timeout=30) as client:
        print("fetching Binance 1s BTC...")
        btc = fetch_binance_1s(client, first, last)
        print(f"  {len(btc)} ticks")

        results = []
        skipped = 0
        for i, ws in enumerate(windows, 1):
            try:
                ev = fetch_event(client, ws)
            except httpx.HTTPError:
                skipped += 1
                continue
            if not ev:
                skipped += 1
                continue
            m = (ev.get("markets") or [None])[0]
            if not m:
                skipped += 1
                continue
            try:
                prices = json.loads(m["outcomePrices"])
                tokens = json.loads(m["clobTokenIds"])
                outcomes = json.loads(m["outcomes"])
            except (KeyError, TypeError, json.JSONDecodeError):
                skipped += 1
                continue
            by = {o.lower(): (float(pr), tok) for o, pr, tok in zip(outcomes, prices, tokens)}
            if "up" not in by or by["up"][0] not in (0.0, 1.0):
                skipped += 1
                continue
            up_won = by["up"][0] == 1.0
            try:
                up_hist = fetch_clob(client, by["up"][1], ws)
                down_hist = fetch_clob(client, by["down"][1], ws)
            except httpx.HTTPError:
                skipped += 1
                continue
            rec = replay_window(
                ws, btc, up_hist, down_hist, up_won,
                args.decide_within, args.stop_entry, args.min_delta,
                args.max_price, args.stake,
            )
            if rec is None:
                skipped += 1
                continue
            results.append(rec)
            if i % 20 == 0:
                print(f"  {i}/{len(windows)} windows...")
            time.sleep(0.04)

    trades = [r["entry"] for r in results if r["entry"] is not None]
    if args.min_ask > 0:
        trades = [t for t in trades if t["ask"] >= args.min_ask]
    wins = [t for t in trades if t["won"]]
    losses = [t for t in trades if not t["won"]]
    pnl = sum(t["pnl"] for t in trades)
    disagree = sum(1 for r in results if r["binance_up"] != r["up_won"])

    blotter = []
    bank = args.bankroll if args.bankroll > 0 else None
    skipped_broke = 0
    if bank is not None:
        running = bank
        for t in trades:
            if running < t["cost"]:
                skipped_broke += 1
                blotter.append({**t, "taken": False, "bank_after": running})
                continue
            running += t["pnl"]
            blotter.append({**t, "taken": True, "bank_after": round(running, 2)})
        bank = running

    print()
    print("=== momentum backtest ===")
    print(f"resolved windows : {len(results)}  (skipped {skipped})")
    print(f"no signal        : {len(results) - len([r for r in results if r['entry']])}  "
          f"({(len(results)-len([r for r in results if r['entry']]))/max(len(results),1):.0%})")
    print(f"trades           : {len(trades)}")
    if trades:
        print(f"direction wins   : {len(wins)}/{len(trades)}  = {len(wins)/len(trades):.1%}")
        print(f"PnL @ ${args.stake:.0f}/trade : {pnl:+.2f}  (avg {pnl/len(trades):+.2f})")
        print(f"avg entry ask    : {sum(t['ask'] for t in trades)/len(trades):.2f}")
        print(f"BUY UP / DOWN    : {sum(t['side']=='up' for t in trades)} / {sum(t['side']=='down' for t in trades)}")
        buckets = [(0.0, 0.40), (0.40, 0.50), (0.50, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 1.01)]
        print("by entry ask:")
        for lo, hi in buckets:
            sub = [t for t in trades if lo <= t["ask"] < hi]
            if not sub:
                continue
            w = sum(1 for t in sub if t["won"])
            print(
                f"  {lo:.2f}-{hi:.2f}: {w}/{len(sub)} = {w/len(sub):.0%}  "
                f"PnL {sum(t['pnl'] for t in sub):+.2f}"
            )
    print(f"Binance vs official winner disagree: {disagree}/{len(results)}")
    if bank is not None:
        taken = [b for b in blotter if b["taken"]]
        print()
        print(f"=== bankroll ${args.bankroll:.0f} | stake ${args.stake:.0f} ===")
        print(f"trades taken     : {len(taken)}  (skipped broke: {skipped_broke})")
        print(f"end bankroll     : ${bank:.2f}")
        print(f"result           : {bank - args.bankroll:+.2f}")
        for b in taken:
            mark = "WIN " if b["won"] else "LOSS"
            print(
                f"  {slug_for(b['ws'])}  {mark} BUY {b['side'].upper():4} "
                f"@{b['ask']:.2f}  pnl={b['pnl']:+.2f}  bank=${b['bank_after']:.2f}"
            )

    if args.out:
        payload = {
            "hours": args.hours,
            "day": args.day or None,
            "tz": args.tz,
            "range_start": first,
            "range_end": last,
            "stake": args.stake,
            "bankroll_start": args.bankroll or None,
            "bankroll_end": bank,
            "min_ask": args.min_ask,
            "windows": len(results),
            "skipped": skipped,
            "trades": trades,
            "blotter": blotter,
            "disagree": disagree,
        }
        from pathlib import Path
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload), encoding="utf-8")
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
