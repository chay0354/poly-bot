"""Optional Supabase sink for fills and window P&L.

Failures never raise — the trading loop must not depend on the database.
Uses the secret key (bypasses RLS). Tables: public.windows, public.fills.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("pm5.supabase")

# Israel 16:40 on 2026-09-09 = 13:40 UTC. Nothing older is uploaded.
CUTOFF = datetime(2026, 9, 9, 13, 40, tzinfo=timezone.utc)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def after_cutoff(ts: str | None) -> bool:
    dt = parse_ts(ts)
    return dt is not None and dt >= CUTOFF


def classify(traded: bool, pnl: float | None) -> str | None:
    if not traded:
        return "skip"
    if pnl is None:
        return "open"
    if pnl > 0.005:
        return "win"
    if pnl < -0.005:
        return "loss"
    return "flat"


def estimated_pnl(window_pnl, up_won, up_shares: float, down_shares: float, cost: float):
    if window_pnl is not None:
        return float(window_pnl)
    up, dn, cost = float(up_shares or 0), float(down_shares or 0), float(cost or 0)
    paired = min(up, dn)
    # Balanced pair pays $1/share regardless of winner: payout = paired (+ naked if it won).
    if up_won is True:
        return round(up - cost, 4)
    if up_won is False:
        return round(dn - cost, 4)
    if paired > 0.01 and abs(up - dn) < 0.5:
        return round(paired - cost, 4)
    return None


class SupabaseLog:
    def __init__(self) -> None:
        self._sb = None
        url = os.getenv("SUPABASE_URL") or os.getenv("PM_SUPABASE_URL") or ""
        key = (
            os.getenv("SUPABASE_SECRET_KEY")
            or os.getenv("PM_SUPABASE_KEY")
            or ""
        )
        if not url or not key:
            return
        try:
            from supabase import create_client

            self._sb = create_client(url, key)
            log.info("supabase logging on")
        except Exception as e:  # noqa: BLE001
            log.warning("supabase client failed: %s", e)
            self._sb = None

    @property
    def enabled(self) -> bool:
        return self._sb is not None

    def fill(self, rec: dict[str, Any]) -> bool:
        if self._sb is None or rec.get("mode") != "live" or not after_cutoff(rec.get("ts")):
            return False
        row = {
            "ts": rec.get("ts"),
            "mode": rec.get("mode"),
            "window_slug": rec.get("window"),
            "strategy": rec.get("strategy"),
            "side": rec.get("side"),
            "price": rec.get("price"),
            "shares": rec.get("shares"),
            "cost": rec.get("cost"),
            "order_id": rec.get("order_id"),
            "maker": rec.get("maker"),
        }
        try:
            self._sb.table("fills").insert(row).execute()
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("supabase fill write failed: %s", e)
            return False

    def window(self, rec: dict[str, Any], up_shares: float = 0.0, down_shares: float = 0.0,
               strategies: list[str] | None = None) -> bool:
        if self._sb is None or rec.get("mode") != "live" or not after_cutoff(rec.get("ts")):
            return False
        traded = bool(rec.get("traded"))
        if not traded:
            return False
        pnl = estimated_pnl(
            rec.get("window_pnl"), rec.get("up_won"), up_shares, down_shares, rec.get("cost") or 0,
        )
        row = {
            "ts": rec.get("ts"),
            "mode": rec.get("mode"),
            "window_slug": rec.get("window"),
            "window_start": rec.get("window_start"),
            "window_end": rec.get("window_end"),
            "traded": traded,
            "n_fills": rec.get("n_fills") or 0,
            "cost": rec.get("cost") or 0,
            "up_shares": up_shares,
            "down_shares": down_shares,
            "up_won": rec.get("up_won"),
            "window_pnl": rec.get("window_pnl"),
            "estimated_pnl": pnl,
            "result": classify(traded, pnl),
            "strategies": strategies or [],
        }
        try:
            self._sb.table("windows").upsert(row, on_conflict="mode,window_slug").execute()
            log.info("supabase window %s %s pnl=%s", row.get("window_slug"), row.get("result"), row.get("estimated_pnl"))
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("supabase window write failed: %s", e)
            return False
