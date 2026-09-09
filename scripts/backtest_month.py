"""Month-long momentum replay with a fresh daily bankroll."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_ROOT / "scripts"))

from backtest_momentum import replay_window  # noqa: E402
from pm5.markets import WINDOW_SECS, slug_for  # noqa: E402
from pm5.net import bootstrap  # noqa: E402


async def _get(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str, params: dict):
    for attempt in range(6):
        async with sem:
            try:
                r = await client.get(url, params=params, timeout=30)
                if r.status_code == 429:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            except httpx.HTTPError:
                if attempt == 5:
                    return None
                await asyncio.sleep(0.4 * (attempt + 1))
    return None


async def fetch_binance_1s(client: httpx.AsyncClient, start: int, end: int) -> dict[int, float]:
    chunks = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + 1000, end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end
    sem = asyncio.Semaphore(8)
    prices: dict[int, float] = {}

    async def one(a: int, b: int) -> None:
        data = await _get(
            client,
            sem,
            "https://api.binance.com/api/v3/klines",
            {
                "symbol": "BTCUSDT",
                "interval": "1s",
                "startTime": a * 1000,
                "endTime": b * 1000,
                "limit": 1000,
            },
        )
        if not data:
            return
        for k in data:
            prices[int(k[0] // 1000)] = float(k[4])

    print(f"fetching Binance 1s ({len(chunks)} chunks)...", flush=True)
    await asyncio.gather(*(one(a, b) for a, b in chunks))
    print(f"  {len(prices)} ticks", flush=True)
    return prices


async def fetch_window_meta(client: httpx.AsyncClient, sem: asyncio.Semaphore, ws: int):
    data = await _get(
        client, sem, "https://gamma-api.polymarket.com/events", {"slug": slug_for(ws)}
    )
    if not data:
        return None
    ev = data[0] if isinstance(data, list) else None
    if not ev:
        return None
    m = (ev.get("markets") or [None])[0]
    if not m:
        return None
    try:
        prices = json.loads(m["outcomePrices"])
        tokens = json.loads(m["clobTokenIds"])
        outcomes = json.loads(m["outcomes"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    by = {o.lower(): (float(pr), tok) for o, pr, tok in zip(outcomes, prices, tokens)}
    if "up" not in by or by["up"][0] not in (0.0, 1.0):
        return None
    return {
        "ws": ws,
        "up_won": by["up"][0] == 1.0,
        "up_tok": by["up"][1],
        "down_tok": by["down"][1],
    }


async def fetch_clob(client: httpx.AsyncClient, sem: asyncio.Semaphore, token_id: str, ws: int):
    data = await _get(
        client,
        sem,
        "https://clob.polymarket.com/prices-history",
        {"market": token_id, "startTs": ws, "endTs": ws + WINDOW_SECS, "fidelity": 1},
    )
    if not data:
        return []
    return [(int(p["t"]), float(p["p"])) for p in data.get("history") or []]


def btc_would_signal(ws: int, btc: dict[int, float], decide_within: float, stop_entry: float, min_delta: float) -> bool:
    open_px = next((btc[t] for t in range(ws, ws + 5) if t in btc), None)
    if open_px is None:
        return False
    start = ws + WINDOW_SECS - int(decide_within)
    end = ws + WINDOW_SECS - int(stop_entry)
    for t in range(start, end + 1):
        px = btc.get(t)
        if px is None:
            continue
        delta = px - open_px
        if abs(delta) < min_delta:
            continue
        ref = next((btc[look] for look in range(t - 10, t + 1) if look in btc), None)
        if ref is not None:
            mom = px - ref
            if (mom > 0) != (delta > 0) and abs(mom) > min_delta / 2:
                continue
        return True
    return False


def walk_days(
    trades: list[dict],
    tz: timezone,
    daily_bank: float,
    min_ask: float,
    start_d: datetime,
    end_d: datetime,
) -> list[dict]:
    by_day: dict[str, list] = defaultdict(list)
    for t in trades:
        if t["ask"] < min_ask:
            continue
        day = datetime.fromtimestamp(t["ws"], tz=tz).strftime("%Y-%m-%d")
        by_day[day].append(t)
    days = []
    cursor = start_d.date()
    last_day = (end_d - timedelta(seconds=1)).date()
    while cursor <= last_day:
        day = cursor.isoformat()
        cursor += timedelta(days=1)
        if day not in by_day:
            days.append({
                "day": day, "trades": 0, "wins": 0, "losses": 0,
                "pnl": 0.0, "end": daily_bank, "skipped_broke": 0,
            })
            continue
        bank = daily_bank
        taken = []
        skipped_broke = 0
        for t in by_day[day]:
            if bank < t["cost"]:
                skipped_broke += 1
                continue
            bank += t["pnl"]
            taken.append(t)
        wins = sum(1 for t in taken if t["won"])
        days.append({
            "day": day,
            "trades": len(taken),
            "wins": wins,
            "losses": len(taken) - wins,
            "pnl": round(bank - daily_bank, 2),
            "end": round(bank, 2),
            "skipped_broke": skipped_broke,
        })
    return days


async def run(args) -> None:
    bootstrap()
    tz = timezone(timedelta(hours=args.tz))
    start_d = datetime.strptime(args.month + "-01", "%Y-%m-%d").replace(tzinfo=tz)
    if start_d.month == 12:
        end_d = start_d.replace(year=start_d.year + 1, month=1)
    else:
        end_d = start_d.replace(month=start_d.month + 1)
    first = int(start_d.timestamp())
    last = int(end_d.timestamp())
    first -= first % WINDOW_SECS
    last -= last % WINDOW_SECS
    windows = list(range(first, last, WINDOW_SECS))
    print(
        f"month {args.month} UTC{args.tz:+d} | {len(windows)} windows | "
        f"daily bank ${args.daily_bankroll:.0f} | stake ${args.stake:.0f}",
        flush=True,
    )

    limits = httpx.Limits(max_connections=40, max_keepalive_connections=20)
    async with httpx.AsyncClient(timeout=30, limits=limits, http2=True) as client:
        btc = await fetch_binance_1s(client, first, last)

        print("fetching Gamma outcomes...", flush=True)
        gsem = asyncio.Semaphore(20)
        metas = []
        batch = 200
        for i in range(0, len(windows), batch):
            chunk = windows[i : i + batch]
            part = await asyncio.gather(*(fetch_window_meta(client, gsem, ws) for ws in chunk))
            metas.extend(part)
            print(f"  gamma {min(i + batch, len(windows))}/{len(windows)}", flush=True)

        candidates = []
        skipped = 0
        for ws, meta in zip(windows, metas):
            if meta is None:
                skipped += 1
                continue
            if not btc_would_signal(ws, btc, args.decide_within, args.stop_entry, args.min_delta):
                continue
            candidates.append(meta)
        print(f"signal candidates: {len(candidates)} (skipped unresolved {skipped})", flush=True)

        print("fetching CLOB history for candidates...", flush=True)
        csem = asyncio.Semaphore(20)

        async def books(meta):
            up_h, dn_h = await asyncio.gather(
                fetch_clob(client, csem, meta["up_tok"], meta["ws"]),
                fetch_clob(client, csem, meta["down_tok"], meta["ws"]),
            )
            return meta, up_h, dn_h

        trades = []
        for i in range(0, len(candidates), batch):
            chunk = candidates[i : i + batch]
            got = await asyncio.gather(*(books(m) for m in chunk))
            for meta, up_h, dn_h in got:
                rec = replay_window(
                    meta["ws"], btc, up_h, dn_h, meta["up_won"],
                    args.decide_within, args.stop_entry, args.min_delta,
                    args.max_price, args.stake,
                )
                if rec and rec["entry"]:
                    trades.append(rec["entry"])
            print(f"  clob {min(i + batch, len(candidates))}/{len(candidates)}", flush=True)

    raw_days = walk_days(trades, tz, args.daily_bankroll, 0.0, start_d, end_d)
    real_days = walk_days(trades, tz, args.daily_bankroll, args.min_ask, start_d, end_d)
    raw_pnl = sum(d["pnl"] for d in raw_days)
    real_pnl = sum(d["pnl"] for d in real_days)
    real_tr = sum(d["trades"] for d in real_days)
    real_w = sum(d["wins"] for d in real_days)

    print()
    print(f"=== {args.month} | ${args.daily_bankroll:.0f}/day reset | stake ${args.stake:.0f} ===")
    print(f"realistic (ask>={args.min_ask:.2f})")
    print(f"  days traded : {sum(1 for d in real_days if d['trades'])}/{len(real_days)}")
    print(f"  fills       : {real_tr}  wins {real_w}/{real_tr} = {real_w / max(real_tr, 1):.1%}")
    print(f"  MONTH PnL   : {real_pnl:+.2f}")
    print(f"raw (all asks) MONTH PnL: {raw_pnl:+.2f}")
    print("daily realistic:")
    for d in real_days:
        print(f"  {d['day']}  {d['wins']}/{d['trades']}  {d['pnl']:+.2f}  end ${d['end']:.2f}")

    payload = {
        "month": args.month,
        "tz": args.tz,
        "stake": args.stake,
        "daily_bankroll": args.daily_bankroll,
        "min_ask": args.min_ask,
        "windows": len(windows),
        "skipped": skipped,
        "trades": trades,
        "days_raw": raw_days,
        "days_realistic": real_days,
        "month_pnl_raw": raw_pnl,
        "month_pnl_realistic": real_pnl,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload), encoding="utf-8")
    print(f"wrote {args.out}", flush=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--month", default="2026-08")
    p.add_argument("--tz", type=int, default=3)
    p.add_argument("--stake", type=float, default=5.0)
    p.add_argument("--daily-bankroll", type=float, default=10_000.0)
    p.add_argument("--min-ask", type=float, default=0.40)
    p.add_argument("--min-delta", type=float, default=8.0)
    p.add_argument("--max-price", type=float, default=0.85)
    p.add_argument("--decide-within", type=float, default=45.0)
    p.add_argument("--stop-entry", type=float, default=3.0)
    p.add_argument("--out", default="data/month_2026-08.json")
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
