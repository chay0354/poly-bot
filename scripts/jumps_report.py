"""The sniper's go / no-go report from data/jumps.jsonl.

    python scripts/jumps_report.py [data/jumps.jsonl] [--edge 0.06] [--size 5]

Gate (from the plan): the sniper only makes sense if the stale quote we
would hit is still there when our order lands. We need, over the jumps
where the model saw an edge ≥ --edge on an ask in the 0.10–0.85 band:
  * median lifetime of the stale ask ≥ 250 ms (our round trip is ~150 ms
    from EU-West), and
  * median edge ≥ taker fee + 2 ticks.
Everything else here is context: how often jumps happen, which venue leads,
and what the ask did 1s / 3s later (did the market really reprice, or did
the "edge" evaporate because fair was wrong).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path


def load(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def q(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, int(round(p * (len(xs) - 1)))))
    return xs[i]


def fee_per_share(p: float, rate: float = 0.07) -> float:
    return rate * p * (1 - p)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default="data/jumps.jsonl")
    ap.add_argument("--edge", type=float, default=0.06, help="PM_SNIPE_MIN_EDGE")
    ap.add_argument("--size", type=float, default=5.0, help="PM_SNIPE_SHARES")
    ap.add_argument("--lo", type=float, default=0.10)
    ap.add_argument("--hi", type=float, default=0.85)
    ap.add_argument("--min-left", type=float, default=20.0)
    ap.add_argument("--max-left", type=float, default=280.0)
    a = ap.parse_args()

    rows = load(Path(a.path))
    if not rows:
        print("no jumps recorded yet")
        return 1
    windows = {r["window"] for r in rows}
    print(f"{len(rows)} jumps over {len(windows)} windows ({len(rows) / max(len(windows), 1):.1f}/window)")
    by_venue = {}
    for r in rows:
        by_venue[r["venue"]] = by_venue.get(r["venue"], 0) + 1
    print("first mover:", ", ".join(f"{k} {v}" for k, v in sorted(by_venue.items(), key=lambda kv: -kv[1])))

    priced = [r for r in rows if r.get("fair_after") is not None and r.get("ask0") is not None]
    print(f"{len(priced)} with a priced ask (σ known, ask present)")

    cand = [
        r for r in priced
        if a.lo <= r["ask0"] <= a.hi
        and a.min_left <= r["left"] <= a.max_left
        and r["edge0"] is not None and r["edge0"] >= a.edge
        and (r.get("avail0") or 0) >= a.size
    ]
    print(f"\n{len(cand)} sniper candidates (ask {a.lo:.2f}–{a.hi:.2f}, edge ≥ {a.edge:.2f}, "
          f"≥ {a.size:.0f} sh, {a.min_left:.0f}–{a.max_left:.0f}s left)"
          f" = {len(cand) / max(len(windows), 1):.2f} per window")
    if not cand:
        print("nothing to measure yet")
        return 0

    life = [r["gone_ms"] for r in cand if r.get("gone_ms") is not None]
    edges = [r["edge0"] for r in cand]
    fees = [fee_per_share(r["ask0"]) for r in cand]
    print(f"stale-ask lifetime ms: p25 {q(life, .25):.0f} | median {q(life, .5):.0f} | p75 {q(life, .75):.0f}"
          f" | ≥150ms {sum(x >= 150 for x in life) / len(life):.0%} | ≥250ms {sum(x >= 250 for x in life) / len(life):.0%}"
          f" | survived 3s {sum(x >= 3000 for x in life) / len(life):.0%}")
    print(f"edge0 (fair − ask): median {statistics.median(edges):.3f} | mean {statistics.mean(edges):.3f}"
          f" | taker fee/sh median {statistics.median(fees):.3f} → needs ≥ {statistics.median(fees) + 0.02:.3f}")

    # Did the market agree? Ask 1s / 3s later on the side we would have bought.
    move1 = [r["ask_1s"] - r["ask0"] for r in cand if r.get("ask_1s") is not None]
    move3 = [r["ask_3s"] - r["ask0"] for r in cand if r.get("ask_3s") is not None]
    if move1:
        print(f"ask 1s later − ask0: median {statistics.median(move1):+.3f} (up in {sum(m > 0 for m in move1) / len(move1):.0%})")
    if move3:
        print(f"ask 3s later − ask0: median {statistics.median(move3):+.3f} (up in {sum(m > 0 for m in move3) / len(move3):.0%})")

    # Honest expected value per share if we had hit ask0 and held to resolution,
    # only where the outcome is known — the fee comes off in shares.
    settled = [r for r in cand if r.get("up_won") is not None]
    if settled:
        pnl = []
        for r in settled:
            won = (r["stale_side"] == "up") == r["up_won"]
            fee = fee_per_share(r["ask0"])
            pnl.append((1.0 if won else 0.0) * (1 - fee / r["ask0"]) - r["ask0"])
        print(f"\nhold-to-resolution on {len(settled)} settled candidates: "
              f"mean {statistics.mean(pnl):+.4f}/sh, win {sum(p > 0 for p in pnl) / len(pnl):.0%}"
              f" → ${statistics.mean(pnl) * a.size:+.3f} per snipe of {a.size:.0f} sh (if it filled)")
        alive = [p for p, r in zip(pnl, settled) if (r.get("gone_ms") or 0) >= 250]
        if alive:
            print(f"  …of which the quote lived ≥250ms ({len(alive)}): mean {statistics.mean(alive):+.4f}/sh")

    med = q(life, .5)
    med_edge = statistics.median(edges)
    need = statistics.median(fees) + 0.02
    go = med >= 250 and med_edge >= need
    print(f"\nGATE: median lifetime {med:.0f}ms {'≥' if med >= 250 else '<'} 250ms, "
          f"median edge {med_edge:.3f} {'≥' if med_edge >= need else '<'} {need:.3f} → "
          f"{'GO' if go else 'NO-GO (keep PM_SNIPE=false)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
