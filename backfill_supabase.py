"""One-shot upload of live windows/fills at or after the 16:40 Israel cutoff."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
from pm5.supabase_log import SupabaseLog, after_cutoff

dec = json.JSONDecoder()
fills_by: dict[str, list] = defaultdict(list)
windows = []
with Path("data/trades.jsonl").open(encoding="utf-8") as fh:
    for line in fh:
        s = line.strip()
        i = 0
        while i < len(s):
            try:
                obj, j = dec.raw_decode(s, i)
            except json.JSONDecodeError:
                break
            if isinstance(obj, dict) and obj.get("mode") == "live" and after_cutoff(obj.get("ts")):
                if obj.get("type") == "fill":
                    fills_by[obj.get("window") or ""].append(obj)
                elif obj.get("type") == "window" and obj.get("traded"):
                    windows.append(obj)
            i = j
            while i < len(s) and s[i] in " \t":
                i += 1

sb = SupabaseLog()
n_f = n_w = 0
seen = set()
for f in (x for xs in fills_by.values() for x in xs):
    key = (f.get("order_id"), f.get("side"), f.get("shares"), f.get("cost"))
    if key in seen:
        continue
    if sb.fill(f):
        n_f += 1
        seen.add(key)
for w in windows:
    slug = w.get("window") or ""
    fs = fills_by.get(slug, [])
    up = sum(float(x.get("shares") or 0) for x in fs if str(x.get("side", "")).lower() == "up")
    dn = sum(float(x.get("shares") or 0) for x in fs if str(x.get("side", "")).lower() == "down")
    strats = sorted({str(x.get("strategy")) for x in fs if x.get("strategy")})
    if sb.window(w, up_shares=up, down_shares=dn, strategies=strats):
        n_w += 1
print(f"uploaded fills={n_f} windows={n_w}")
