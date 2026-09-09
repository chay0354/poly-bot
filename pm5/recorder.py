"""Append-only JSONL recorder for trades and window outcomes.

One JSON object per line, so the file is crash-safe (each record is flushed)
and trivially loadable for analysis, e.g.:

    import pandas as pd
    df = pd.read_json("data/trades.jsonl", lines=True)
    fills = df[df.type == "fill"]
    windows = df[df.type == "window"]

Two record types:
- ``fill``   — one executed trade, with the market context at entry.
- ``window`` — one 5-minute window's outcome (open/close/winner/PnL), written at
  settlement; emitted even for windows with no trade, which is useful for
  studying what a strategy *would* have done.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .supabase_log import SupabaseLog

log = logging.getLogger("pm5.recorder")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Recorder:
    def __init__(self, path: str, enabled: bool = True, mode: str = "paper") -> None:
        self.enabled = enabled
        self.mode = mode
        self._fh = None
        self._warned = False
        self._sb = SupabaseLog()
        self._fills: dict[str, list[dict]] = defaultdict(list)
        if not enabled:
            return
        p = Path(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            self._fh = p.open("a", encoding="utf-8")
            log.info("recording trades to %s", p)
        except OSError as e:
            log.warning("could not open data file %s (%s); recording disabled", path, e)
            self.enabled = False

    def _write(self, rec: dict) -> dict:
        full = {"ts": _utc_now(), "mode": self.mode, **rec}
        if self._fh is not None:
            try:
                self._fh.write(json.dumps(full) + "\n")
                self._fh.flush()
            except (OSError, TypeError) as e:
                if not self._warned:  # don't spam if the disk/serialization is unhappy
                    log.warning("recorder write failed: %s", e)
                    self._warned = True
        return full

    def fill(self, **fields) -> None:
        full = self._write({"type": "fill", **fields})
        slug = full.get("window") or ""
        self._fills[slug].append(full)
        self._sb.fill(full)

    def window(self, **fields) -> None:
        full = self._write({"type": "window", **fields})
        slug = full.get("window") or ""
        fills = self._fills.pop(slug, [])
        up = sum(float(f.get("shares") or 0) for f in fills if str(f.get("side", "")).lower() == "up")
        dn = sum(float(f.get("shares") or 0) for f in fills if str(f.get("side", "")).lower() == "down")
        strats = sorted({str(f.get("strategy")) for f in fills if f.get("strategy")})
        self._sb.window(full, up_shares=up, down_shares=dn, strategies=strats)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
