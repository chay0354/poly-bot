"""Tail trades.jsonl into Supabase. Does not talk to the CLOB or the bot.

Only uploads records at/after 2026-09-09 16:40 Israel (13:40 UTC).
Safe to run next to a live bot: read-only on the log file.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from pm5.supabase_log import SupabaseLog, after_cutoff  # noqa: E402

log = logging.getLogger("pm5.sync")
DATA = Path("data/trades.jsonl")
STATE = Path("data/supabase_sync.json")


def _load_state() -> dict:
    if not STATE.exists():
        return {"offset": 0, "windows": [], "fills": []}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"offset": 0, "windows": [], "fills": []}


def _save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st), encoding="utf-8")


def _fill_key(rec: dict) -> str:
    return "|".join([
        str(rec.get("mode") or ""),
        str(rec.get("order_id") or rec.get("ts") or ""),
        str(rec.get("side") or ""),
        str(rec.get("shares") or ""),
        str(rec.get("cost") or ""),
    ])


def _iter_objs(chunk: str):
    dec = json.JSONDecoder()
    i = 0
    n = len(chunk)
    while i < n:
        while i < n and chunk[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        try:
            obj, j = dec.raw_decode(chunk, i)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            yield obj
        i = j


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    sb = SupabaseLog()
    if not sb.enabled:
        raise SystemExit("SUPABASE_URL / SUPABASE_SECRET_KEY missing")
    st = _load_state()
    seen_w = set(st.get("windows") or [])
    seen_f = set(st.get("fills") or [])
    pending_fills: dict[str, list] = defaultdict(list)
    log.info("watching %s from offset %s (cutoff 16:40 Israel)", DATA, st["offset"])
    while True:
        if DATA.exists():
            with DATA.open("r", encoding="utf-8") as fh:
                fh.seek(st["offset"])
                chunk = fh.read()
                st["offset"] = fh.tell()
            for rec in _iter_objs(chunk):
                if not after_cutoff(rec.get("ts")):
                    continue
                typ = rec.get("type")
                slug = rec.get("window") or ""
                if typ == "fill":
                    key = _fill_key(rec)
                    if key in seen_f:
                        continue
                    if sb.fill(rec):
                        seen_f.add(key)
                    pending_fills[slug].append(rec)
                elif typ == "window" and rec.get("traded"):
                    wkey = f"{rec.get('mode')}|{slug}"
                    fills = pending_fills.get(slug, [])
                    up = sum(float(f.get("shares") or 0) for f in fills if str(f.get("side", "")).lower() == "up")
                    dn = sum(float(f.get("shares") or 0) for f in fills if str(f.get("side", "")).lower() == "down")
                    strats = sorted({str(f.get("strategy")) for f in fills if f.get("strategy")})
                    if sb.window(rec, up_shares=up, down_shares=dn, strategies=strats):
                        seen_w.add(wkey)
            st["windows"] = list(seen_w)[-400:]
            st["fills"] = list(seen_f)[-2000:]
            _save_state(st)
        time.sleep(2)


if __name__ == "__main__":
    main()
